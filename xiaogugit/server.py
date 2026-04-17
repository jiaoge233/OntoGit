import base64
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from config import get_settings
from inference_client import DangGuInferenceClient
from manager import XiaoGuGitManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("xiaogugit.api")
SAFE_ERROR_DETAIL = "请稍后重试"

settings = get_settings()
app = FastAPI(
    title="本体Git 核心 API",
    description="一个面向本体/结构化数据版本管理的轻量级 Git 包装服务。可访问 /ui 使用简易前端页面。",
    version="0.2.0",
    docs_url=settings.docs_url,
    redoc_url=settings.redoc_url,
    openapi_url=settings.openapi_url,
)
xg = XiaoGuGitManager(
    root_dir=settings.storage_root,
    redis_enabled=settings.redis_enabled,
    redis_host=settings.redis_host,
    redis_port=settings.redis_port,
    redis_password=settings.redis_password,
    redis_db=settings.redis_db,
    redis_key_prefix=settings.redis_key_prefix,
    redis_socket_timeout=settings.redis_socket_timeout,
)
inference_client = DangGuInferenceClient(
    inference_url=settings.inference_url,
    timeout=settings.inference_timeout,
    retry_attempts=settings.inference_retry_attempts,
    retry_backoff_seconds=settings.inference_retry_backoff_seconds,
)
logger.info(
    "inference client configured: url=%s timeout=%ss retries=%s",
    settings.inference_url or "(empty — probability calls will be skipped)",
    settings.inference_timeout,
    settings.inference_retry_attempts,
)
_project_locks_guard = Lock()
_project_locks: dict[str, Lock] = {}
FRONTEND_FILE = Path(__file__).with_name("frontend.html")
MODERN_FRONTEND_FILE = Path(__file__).with_name("frontend_modern.html")
VISUAL_FRONTEND_FILE = Path(__file__).with_name("frontend_visual.html")
VISUAL_MODERN_FRONTEND_FILE = Path(__file__).with_name("frontend_visual_modern.html")
LOGIN_FRONTEND_FILE = Path(__file__).with_name("login.html")

PUBLIC_PATHS = {
    "/login",
    "/auth/login",
    "/auth/logout",
    "/health",
}
HTML_PAGE_PATHS = {
    "/",
    "/ui",
    "/ui-visual",
    "/ui-modern",
    "/ui-visual-modern",
}
PUBLIC_PREFIXES = (
    "/openapi.json",
    "/docs",
    "/redoc",
)


def _get_project_lock(project_id: str) -> Lock:
    with _project_locks_guard:
        lock = _project_locks.get(project_id)
        if lock is None:
            lock = Lock()
            _project_locks[project_id] = lock
        return lock


class WriteReq(BaseModel):
    project_id: str
    filename: str
    data: Dict[str, Any]
    message: str
    agent_name: str
    committer_name: str
    basevision: int


class DeleteReq(BaseModel):
    project_id: str
    filename: str
    message: str = "System: 删除本体"
    committer_name: str = "System"
    agent_name: Optional[str] = None
    purge_history: bool = True


class WriteInferReq(WriteReq):
    inference_message: str = "System: inference probability update"
    inference_agent_name: str = "端故推理引擎"
    inference_committer_name: str = "端故推理引擎"


class ProjectInitReq(BaseModel):
    project_id: str
    name: Optional[str] = None
    description: str = ""
    status: str = "开发中"


class ProjectStatusReq(BaseModel):
    project_id: str
    status: str
    operator: str = "System"


class VersionStarReq(BaseModel):
    project_id: str
    version_id: int
    filename: Optional[str] = None
    increment: int = 1


class VersionUnstarReq(BaseModel):
    project_id: str
    version_id: int
    filename: Optional[str] = None
    decrement: int = 1


class OfficialRecommendationSetReq(BaseModel):
    project_id: str
    filename: str
    version_id: int
    operator: str = "System"
    reason: str = ""


class OfficialRecommendationClearReq(BaseModel):
    project_id: str
    filename: str
    operator: str = "System"
    reason: str = ""


class LoginReq(BaseModel):
    username: str
    password: str


def _sign_access_token(payload: dict[str, str]) -> str:
    payload_json = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_json).decode("ascii").rstrip("=")
    signature = hmac.new(settings.auth_secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{signature}"


def _decode_access_token(token: str | None) -> dict[str, str] | None:
    if not token or "." not in token:
        return None

    payload_b64, signature = token.rsplit(".", 1)
    expected_signature = hmac.new(settings.auth_secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        return None

    padding = "=" * (-len(payload_b64) % 4)
    try:
        payload_raw = base64.urlsafe_b64decode(f"{payload_b64}{padding}".encode("ascii"))
        payload = json.loads(payload_raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _extract_bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    if not authorization.lower().startswith("bearer "):
        return None
    token = authorization[7:].strip()
    return token or None


def _get_authenticated_user(request: Request) -> str | None:
    token = _extract_bearer_token(request) or request.cookies.get(settings.auth_cookie_name)
    payload = _decode_access_token(token)
    username = payload.get("username") if payload else None
    return username if isinstance(username, str) and username == settings.auth_username else None


def _is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def _build_unauthorized_response(request: Request):
    wants_html = request.url.path in HTML_PAGE_PATHS or "text/html" in request.headers.get("accept", "")
    if wants_html:
        return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)
    return JSONResponse({"detail": "Unauthorized"}, status_code=401)


@app.middleware("http")
async def require_login(request: Request, call_next):
    if _is_public_path(request.url.path):
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "%s %s -> %s (%.1fms)",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response
    if _get_authenticated_user(request):
        if request.method == "POST" and request.url.path == "/write-and-infer":
            client_host = request.client.host if request.client else "-"
            logger.info("incoming POST /write-and-infer (client=%s)", client_host)
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "%s %s -> %s (%.1fms)",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response
    response = _build_unauthorized_response(request)
    logger.info("%s %s -> %s", request.method, request.url.path, response.status_code)
    return response


def _handle_error(exc: Exception):
    logger.exception("request failed: %s", exc)
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=SAFE_ERROR_DETAIL) from exc
    if isinstance(exc, FileNotFoundError):
        raise HTTPException(status_code=404, detail=SAFE_ERROR_DETAIL) from exc
    raise HTTPException(status_code=500, detail=SAFE_ERROR_DETAIL) from exc


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _get_authenticated_user(request):
        return RedirectResponse(url="/ui-visual", status_code=303)
    return LOGIN_FRONTEND_FILE.read_text(encoding="utf-8")


@app.post("/auth/login")
async def login(req: LoginReq):
    if req.username != settings.auth_username or req.password != settings.auth_password:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    access_token = _sign_access_token({"username": settings.auth_username})
    response = JSONResponse(
        {
            "status": "success",
            "user": settings.auth_username,
            "access_token": access_token,
            "token_type": "Bearer",
            "redirect_to": "/ui-visual",
        }
    )
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=access_token,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )
    return response


@app.post("/auth/logout")
async def logout():
    response = JSONResponse({"status": "success"})
    response.delete_cookie(settings.auth_cookie_name, path="/")
    return response


@app.get("/auth/me")
async def me(request: Request):
    username = _get_authenticated_user(request)
    if not username:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"authenticated": True, "user": username}


@app.get("/ui", response_class=HTMLResponse)
async def ui():
    return FRONTEND_FILE.read_text(encoding="utf-8")


@app.get("/ui-visual", response_class=HTMLResponse)
async def ui_visual():
    return VISUAL_FRONTEND_FILE.read_text(encoding="utf-8")


@app.get("/ui-modern", response_class=HTMLResponse)
async def ui_modern():
    return MODERN_FRONTEND_FILE.read_text(encoding="utf-8")


@app.get("/ui-visual-modern", response_class=HTMLResponse)
async def ui_visual_modern():
    return VISUAL_MODERN_FRONTEND_FILE.read_text(encoding="utf-8")


@app.get("/")
async def index():
    return {
        "service": "本体Git",
        "status": "running",
        "docs": settings.docs_url,
        "config": settings.public_dict(),
        "supported_status": sorted(list(xg.ALLOWED_STATUS)),
    }


@app.get("/health")
async def health():
    return {"status": "ok", "env": settings.env}


@app.post("/projects/init")
async def init_project(req: ProjectInitReq):
    try:
        return xg.init_project(req.project_id, req.name, req.description, req.status)
    except Exception as exc:
        _handle_error(exc)


@app.get("/projects")
async def list_projects():
    try:
        return {"projects": xg.list_projects()}
    except Exception as exc:
        _handle_error(exc)


@app.get("/projects/{project_id}")
async def project_detail(project_id: str):
    try:
        return {"project": xg.get_project_info(project_id)}
    except Exception as exc:
        _handle_error(exc)


@app.post("/projects/status")
async def update_project_status(req: ProjectStatusReq):
    try:
        return xg.update_project_status(req.project_id, req.status, req.operator)
    except Exception as exc:
        _handle_error(exc)


@app.get("/projects/{project_id}/files")
async def list_project_files(project_id: str):
    try:
        return {"files": xg.list_files(project_id)}
    except Exception as exc:
        _handle_error(exc)


@app.get("/ontology-resolve")
async def ontology_resolve(project_id: str, query: str):
    try:
        return xg.resolve_ontology_query(project_id, query)
    except Exception as exc:
        _handle_error(exc)


@app.get("/versions/{project_id}/{filename:path}")
async def file_versions(
    project_id: str,
    filename: str,
    min_stars: int = Query(0, ge=0),
    sort_by: str = Query("version"),
    order: str = Query("asc"),
):
    try:
        return xg.list_versions(project_id, filename, min_stars=min_stars, sort_by=sort_by, order=order)
    except Exception as exc:
        _handle_error(exc)


@app.get("/versions/{project_id}")
async def project_versions(
    project_id: str,
    filename: Optional[str] = Query(None),
    min_stars: int = Query(0, ge=0),
    sort_by: str = Query("version"),
    order: str = Query("asc"),
):
    try:
        return xg.list_versions(project_id, filename, min_stars=min_stars, sort_by=sort_by, order=order)
    except Exception as exc:
        _handle_error(exc)


@app.get("/timelines/{project_id}")
async def file_timelines(project_id: str):
    try:
        return {"timelines": xg.get_all_file_timelines(project_id)}
    except Exception as exc:
        _handle_error(exc)


@app.get("/version-detail/{project_id}/{version_id}")
async def version_detail(project_id: str, version_id: str, filename: Optional[str] = Query(None)):
    try:
        return {"version": xg.get_version_detail(project_id, version_id, filename)}
    except Exception as exc:
        _handle_error(exc)


@app.get("/version-read/{project_id}/{version_id}")
async def version_read(project_id: str, version_id: str, filename: Optional[str] = Query(None)):
    try:
        return xg.read_version_by_id(project_id, version_id, filename)
    except Exception as exc:
        _handle_error(exc)


@app.get("/projects/{project_id}/commits/{commit_id}")
async def commit_detail(project_id: str, commit_id: str):
    try:
        return {"commit": xg.get_commit_detail(project_id, commit_id)}
    except Exception as exc:
        _handle_error(exc)


@app.post("/write")
async def write(req: WriteReq):
    try:
        return xg.write_version(
            req.project_id,
            req.filename,
            req.data,
            req.message,
            req.agent_name,
            req.committer_name,
            req.basevision,
        )
    except Exception as exc:
        _handle_error(exc)


@app.post("/delete")
async def delete(req: DeleteReq):
    try:
        if req.purge_history:
            return xg.purge_file_history(
                req.project_id,
                req.filename,
            )
        return xg.delete_version(
            req.project_id,
            req.filename,
            req.message,
            req.committer_name,
            req.agent_name,
        )
    except Exception as exc:
        _handle_error(exc)


@app.get("/read/{project_id}/{filename}")
async def read(project_id: str, filename: str, commit_id: Optional[str] = Query(None)):
    try:
        data = xg.read_version(project_id, filename, commit_id)
        return {"data": data}
    except Exception as exc:
        _handle_error(exc)


@app.get("/log/{project_id}")
async def log(project_id: str, filename: Optional[str] = Query(None)):
    try:
        return {"history": xg.get_log(project_id, filename)}
    except Exception as exc:
        _handle_error(exc)


@app.get("/diff")
async def diff(
    project_id: str,
    filename: str,
    base: Optional[str] = Query(None),
    target: Optional[str] = Query(None),
    base_version_id: Optional[str] = Query(None),
    target_version_id: Optional[str] = Query(None),
):
    try:
        if base_version_id and target_version_id:
            return xg.diff_versions(project_id, base_version_id, target_version_id)
        if not base or not target:
            raise ValueError("base 和 target 不能为空")
        return {"diff": xg.get_diff(project_id, filename, base, target)}
    except Exception as exc:
        _handle_error(exc)


@app.get("/version-diff")
async def version_diff(project_id: str, base_version_id: str, target_version_id: str, filename: Optional[str] = Query(None)):
    try:
        return xg.diff_versions(project_id, base_version_id, target_version_id, filename)
    except Exception as exc:
        _handle_error(exc)


@app.post("/rollback")
async def rollback(
    project_id: str,
    commit_id: Optional[str] = Query(None),
    version_id: Optional[str] = Query(None),
):
    try:
        if version_id:
            return xg.rollback_version_by_id(project_id, version_id)
        if not commit_id:
            raise ValueError("commit_id 或 version_id 必须提供一个")
        return xg.rollback(project_id, commit_id)
    except Exception as exc:
        _handle_error(exc)


@app.post("/version-rollback")
async def version_rollback(project_id: str, version_id: str, filename: Optional[str] = Query(None)):
    try:
        return xg.rollback_version_by_id(project_id, version_id, filename)
    except Exception as exc:
        _handle_error(exc)


@app.post("/write-and-infer")
async def write_and_infer(req: WriteInferReq):
    try:
        logger.info(
            "write-and-infer: accepted project_id=%s filename=%s basevision=%s",
            req.project_id,
            req.filename,
            req.basevision,
        )
        with _get_project_lock(req.project_id):
            def infer_probability_with_fallback():
                result = inference_client.infer_change(
                    {
                        "project_id": req.project_id,
                        "filename": req.filename,
                        "data": req.data,
                    }
                )
                if not str(result.get("probability", "")).strip():
                    logger.info(
                        "[DangGuInference] empty probability from contextual payload, retrying legacy payload"
                    )
                    result = inference_client.infer_change(req.data)
                if not str(result.get("probability", "")).strip():
                    raise RuntimeError("inference service returned empty probability")
                return result

            def sync_amended_commit_id(write_result):
                version_id = write_result.get("version_id")
                if version_id is None:
                    return write_result
                try:
                    tree = xg.get_file_version_tree(req.project_id, req.filename)
                    for version in tree.get("versions", []):
                        if str(version.get("version_id")) == str(version_id):
                            write_result["commit_id"] = version.get("id") or write_result.get("commit_id")
                            break
                except Exception as exc:
                    logger.warning("failed to sync amended commit id: %s", exc)
                return write_result

            write_result = xg.write_version(
                req.project_id,
                req.filename,
                req.data,
                req.message,
                req.agent_name,
                req.committer_name,
                req.basevision,
            )
            if write_result.get("status") != "success":
                current_data = xg.read_version(req.project_id, req.filename)
                if isinstance(current_data, dict) and not str(current_data.get("probability", "")).strip():
                    try:
                        inference_result = infer_probability_with_fallback()
                        logger.info(
                            "[DangGuInference] %s",
                            json.dumps(
                                {
                                    "project_id": req.project_id,
                                    "filename": req.filename,
                                    "probability": inference_result.get("probability", ""),
                                    "reason": inference_result.get("reason", ""),
                                    "status": inference_result.get("status", ""),
                                    "detail": inference_result.get("detail", ""),
                                    "write_status": write_result.get("status", "no_change"),
                                },
                                ensure_ascii=False,
                            ),
                        )
                        updated_data = xg.update_current_version_fields(
                            req.project_id,
                            req.filename,
                            {
                                "probability": inference_result.get("probability", ""),
                            },
                        )
                        sync_amended_commit_id(write_result)
                        return {
                            "status": "success",
                            "write_result": write_result,
                            "inference_result": inference_result,
                            "probability_update_result": {
                                "status": "success",
                                "filename": req.filename,
                                "version_id": write_result.get("version_id"),
                                "commit_id": write_result.get("commit_id"),
                                "updated_fields": ["probability"],
                                "detail": "content unchanged; probability field was missing and has been updated",
                            },
                            "data": updated_data,
                        }
                    except Exception as exc:
                        logger.warning(
                            "write-and-infer probability update failed after no-change write: project_id=%s filename=%s error=%s",
                            req.project_id,
                            req.filename,
                            exc,
                        )
                        return {
                            "status": "partial_success",
                            "write_result": write_result,
                            "inference_result": locals().get("inference_result"),
                            "probability_update_result": {
                                "status": "failed",
                                "filename": req.filename,
                                "version_id": write_result.get("version_id"),
                                "commit_id": write_result.get("commit_id"),
                                "updated_fields": ["probability"],
                                "detail": SAFE_ERROR_DETAIL,
                            },
                            "data": None,
                        }
                return {
                    "status": write_result.get("status", "no_change"),
                    "write_result": write_result,
                    "inference_result": None,
                    "probability_update_result": None,
                }

            try:
                inference_result = infer_probability_with_fallback()
                logger.info(
                    "[DangGuInference] %s",
                    json.dumps(
                        {
                            "project_id": req.project_id,
                            "filename": req.filename,
                            "probability": inference_result.get("probability", ""),
                            "reason": inference_result.get("reason", ""),
                            "status": inference_result.get("status", ""),
                            "detail": inference_result.get("detail", ""),
                        },
                        ensure_ascii=False,
                    ),
                )

                updated_data = xg.update_current_version_fields(
                    req.project_id,
                    req.filename,
                    {
                        "probability": inference_result.get("probability", ""),
                    },
                )
                sync_amended_commit_id(write_result)
            except Exception as exc:
                logger.warning(
                    "write-and-infer probability update failed: project_id=%s filename=%s error=%s",
                    req.project_id,
                    req.filename,
                    exc,
                )
                return {
                    "status": "partial_success",
                    "write_result": write_result,
                    "inference_result": locals().get("inference_result"),
                    "probability_update_result": {
                        "status": "failed",
                        "filename": req.filename,
                        "version_id": write_result.get("version_id"),
                        "commit_id": write_result.get("commit_id"),
                        "updated_fields": ["probability"],
                        "detail": SAFE_ERROR_DETAIL,
                    },
                    "data": None,
                }

            return {
                "status": "success",
                "write_result": write_result,
                "inference_result": inference_result,
                "probability_update_result": {
                    "status": "success",
                    "filename": req.filename,
                    "version_id": write_result.get("version_id"),
                    "commit_id": write_result.get("commit_id"),
                    "updated_fields": ["probability"],
                },
                "data": updated_data,
            }
    except Exception as exc:
        _handle_error(exc)


@app.post("/version-star")
async def version_star(req: VersionStarReq):
    try:
        return xg.star_version(req.project_id, req.version_id, req.filename, req.increment)
    except Exception as exc:
        _handle_error(exc)


@app.post("/version-unstar")
async def version_unstar(req: VersionUnstarReq):
    try:
        return xg.unstar_version(req.project_id, req.version_id, req.filename, req.decrement)
    except Exception as exc:
        _handle_error(exc)


@app.get("/version-recommend/official")
async def version_recommend_official(project_id: str, filename: str):
    try:
        return xg.get_official_recommended_version(project_id, filename)
    except Exception as exc:
        _handle_error(exc)


@app.post("/version-recommend/official/set")
async def version_recommend_official_set(req: OfficialRecommendationSetReq):
    try:
        with _get_project_lock(req.project_id):
            return xg.set_official_recommendation(
                req.project_id,
                req.filename,
                req.version_id,
                req.operator,
                req.reason,
            )
    except Exception as exc:
        _handle_error(exc)


@app.post("/version-recommend/official/clear")
async def version_recommend_official_clear(req: OfficialRecommendationClearReq):
    try:
        with _get_project_lock(req.project_id):
            return xg.clear_official_recommendation(
                req.project_id,
                req.filename,
                req.operator,
                req.reason,
            )
    except Exception as exc:
        _handle_error(exc)


@app.get("/version-recommend/official/history")
async def version_recommend_official_history(project_id: str, filename: str):
    try:
        return xg.get_official_recommendation_history(project_id, filename)
    except Exception as exc:
        _handle_error(exc)


@app.get("/version-recommend/community")
async def version_recommend_community(project_id: str, filename: str):
    try:
        return xg.get_community_recommended_version(project_id, filename)
    except Exception as exc:
        _handle_error(exc)


@app.get("/version-recommend/community/history")
async def version_recommend_community_history(project_id: str, filename: str):
    try:
        return xg.get_community_recommendation_history(project_id, filename)
    except Exception as exc:
        _handle_error(exc)


@app.get("/community-leaderboard")
async def community_leaderboard(project_id: str):
    try:
        return xg.get_community_leaderboard(project_id)
    except Exception as exc:
        _handle_error(exc)


if __name__ == "__main__":
    import uvicorn

    if settings.reload:
        uvicorn.run("server:app", host=settings.host, port=settings.port, reload=True)
    else:
        uvicorn.run(app, host=settings.host, port=settings.port)
