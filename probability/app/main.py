import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error, parse, request

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("probability.api")
SAFE_ERROR_DETAIL = "请稍后重试"

load_dotenv()


def build_client() -> OpenAI:
    api_key = os.getenv("DMXAPI_API_KEY")
    base_url = os.getenv("DMXAPI_BASE_URL", "https://www.dmxapi.cn/v1")

    if not api_key:
        raise RuntimeError("DMXAPI_API_KEY is not configured. Please set it in .env.")

    return OpenAI(api_key=api_key, base_url=base_url)


def get_default_model() -> str:
    return os.getenv("DMXAPI_MODEL", "gpt-5.4")


def get_llm_retry_attempts() -> int:
    try:
        return max(int(os.getenv("DMXAPI_RETRY_ATTEMPTS", "3")), 1)
    except ValueError:
        return 3


def get_llm_retry_backoff_seconds() -> float:
    try:
        return max(float(os.getenv("DMXAPI_RETRY_BACKOFF_SECONDS", "0.8")), 0.0)
    except ValueError:
        return 0.8


def is_git_cache_enabled() -> bool:
    return os.getenv("PROBABILITY_GIT_CACHE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def get_xiaogugit_base_url() -> str:
    return os.getenv("XIAOGUGIT_BASE_URL", "http://127.0.0.1:8000").strip().rstrip("/")


def get_xiaogugit_timeout() -> float:
    try:
        return max(float(os.getenv("XIAOGUGIT_TIMEOUT", "2")), 0.1)
    except ValueError:
        return 2.0


def get_xiaogugit_auth_headers() -> dict[str, str]:
    token = os.getenv("XIAOGUGIT_BEARER_TOKEN", "").strip()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def get_probability_prompt() -> str | None:
    return os.getenv(
        "DMXAPI_SYSTEM_PROMPT_PROBABILITY",
        (
            "你是一个专业、准确的本体概率判断专家。你的任务是根据用户输入内容，判断该对象作为真实本体的概率。"
            "你必须严格遵守以下规则："
            "1. 只能输出一个百分比结果，禁止输出原因、解释、JSON、Markdown、代码块、前后缀文本或任何其他内容。"
            "2. 输出格式必须严格为数字加百分号，例如 97%、2%、100%。"
            "3. 不允许输出小数，不允许输出区间，不允许输出多个结果，不允许输出换行。"
            "4. 你必须根据输入中的 name、abilities、interactions 综合判断后，只返回最终百分比。"
            "5. 即使输入信息不足、含糊、异常，也只输出一个百分比。"
        ),
    )


def get_probability_reason_prompt() -> str | None:
    return os.getenv(
        "DMXAPI_SYSTEM_PROMPT_PROBABILITY_REASON",
        (
            "你是一个专业、准确的本体概率判断专家。你的任务是根据用户输入内容，判断该对象作为真实本体的概率，"
            "并给出简明中文原因。你必须严格遵守以下规则："
            '1. 只能输出一个 JSON 对象，禁止输出 Markdown、代码块、额外说明或任何非 JSON 内容。'
            '2. 输出结构必须严格为 {"probability":"97%","reason":"中文原因"}，且只能包含这两个字段。'
            "3. probability 必须是百分比字符串，例如 97%、2%、100%，不得使用小数。"
            "4. reason 必须使用中文，结合 name、abilities、interactions 简要说明判断依据。"
            "5. 即使输入信息不足、含糊、异常，也必须严格按上述 JSON 结构输出。"
        ),
    )


def build_input(message: str, system_prompt: str | None) -> str | list[dict[str, Any]]:
    if not system_prompt:
        return message

    return [
        {
            "role": "system",
            "content": [{"type": "input_text", "text": system_prompt}],
        },
        {
            "role": "user",
            "content": [{"type": "input_text", "text": message}],
        },
    ]


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="User prompt text")
    model: str = Field(default_factory=get_default_model)
    include_raw: bool = Field(default=False, description="Whether to return the full raw model response")


class ChatResponse(BaseModel):
    model: str
    text: str
    raw: dict[str, Any] | None = None


def _http_json(url: str, timeout: float, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"Accept": "application/json", **get_xiaogugit_auth_headers()}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url=url, headers=headers, method=method, data=body)
    with request.urlopen(req, timeout=timeout) as response:
        raw_body = response.read().decode("utf-8")
    parsed = json.loads(raw_body or "{}")
    if not isinstance(parsed, dict):
        raise RuntimeError("expected JSON object response")
    return parsed


def _http_get_json(url: str, timeout: float) -> dict[str, Any]:
    return _http_json(url, timeout)


def _ensure_xiaogugit_auth() -> None:
    if get_xiaogugit_auth_headers():
        return

    username = os.getenv("XIAOGUGIT_AUTH_USERNAME", "").strip()
    password = os.getenv("XIAOGUGIT_AUTH_PASSWORD", "").strip()
    if not username or not password:
        return

    login_url = f"{get_xiaogugit_base_url()}/auth/login"
    login_response = _http_json(
        login_url,
        timeout=get_xiaogugit_timeout(),
        method="POST",
        payload={"username": username, "password": password},
    )
    token = str(login_response.get("access_token") or "").strip()
    if token:
        os.environ["XIAOGUGIT_BEARER_TOKEN"] = token


def _extract_business_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_data = payload.get("data")
    if isinstance(raw_data, dict):
        return raw_data

    ignored_keys = {
        "include_raw",
        "project_id",
        "filename",
        "ontology_name",
        "model",
    }
    return {key: value for key, value in payload.items() if key not in ignored_keys}


def _canonical_for_probability_match(value: Any) -> Any:
    ignored_keys = {
        "probability",
        "reason",
        "status",
        "detail",
        "include_raw",
        "project_id",
        "filename",
        "ontology_name",
        "model",
    }
    if isinstance(value, dict):
        return {
            key: _canonical_for_probability_match(item)
            for key, item in sorted(value.items())
            if key not in ignored_keys
        }
    if isinstance(value, list):
        return [_canonical_for_probability_match(item) for item in value]
    if isinstance(value, str):
        return value.strip()
    return value


def _payload_matches_git_data(payload_data: dict[str, Any], git_data: dict[str, Any]) -> bool:
    return _canonical_for_probability_match(payload_data) == _canonical_for_probability_match(git_data)


def _resolve_git_cache_target(payload: dict[str, Any], business_payload: dict[str, Any]) -> tuple[str, str] | None:
    project_id = str(payload.get("project_id") or os.getenv("PROBABILITY_DEFAULT_PROJECT_ID", "")).strip()
    filename = str(payload.get("filename") or "").strip()
    ontology_name = str(payload.get("ontology_name") or business_payload.get("name") or "").strip()
    if not project_id or not (filename or ontology_name):
        return None

    base_url = get_xiaogugit_base_url()
    timeout = get_xiaogugit_timeout()
    if filename:
        return project_id, filename

    url = (
        f"{base_url}/ontology-resolve"
        f"?project_id={parse.quote(project_id, safe='')}"
        f"&query={parse.quote(ontology_name, safe='')}"
    )
    resolved = _http_get_json(url, timeout)
    resolved_filename = str(resolved.get("filename") or "").strip()
    if not resolved_filename:
        return None
    return project_id, resolved_filename


def _lookup_probability_from_git(payload: dict[str, Any], with_reason: bool) -> ChatResponse | None:
    if not is_git_cache_enabled():
        return None

    _ensure_xiaogugit_auth()
    business_payload = _extract_business_payload(payload)
    target = _resolve_git_cache_target(payload, business_payload)
    if not target:
        return None

    project_id, filename = target
    base_url = get_xiaogugit_base_url()
    timeout = get_xiaogugit_timeout()
    url = (
        f"{base_url}/read/{parse.quote(project_id, safe='')}/"
        f"{parse.quote(filename, safe='')}"
    )
    current = _http_get_json(url, timeout)
    data = current.get("data")
    if not isinstance(data, dict):
        logger.info("probability git cache miss: current data is not a JSON object")
        return None

    if not _payload_matches_git_data(business_payload, data):
        logger.info(
            "probability git cache miss: payload does not match git data project_id=%s filename=%s",
            project_id,
            filename,
        )
        return None

    probability = str(data.get("probability") or "").strip()
    if not probability:
        logger.info(
            "probability git cache miss: matched git data has no probability project_id=%s filename=%s",
            project_id,
            filename,
        )
        return None

    if with_reason:
        text = json.dumps(
            {
                "probability": probability,
                "reason": "命中 Git 版本库中已保存的概率结果，未重复调用模型。",
            },
            ensure_ascii=False,
        )
    else:
        text = probability

    return ChatResponse(
        model="git-cache",
        text=text,
        raw={
            "source": "git-cache",
            "project_id": project_id,
            "filename": filename,
        } if bool(payload.get("include_raw", False)) else None,
    )


def extract_text_from_chat_completion(response: Any) -> str:
    try:
        return response.choices[0].message.content or ""
    except (AttributeError, IndexError, TypeError):
        return ""


def extract_probability_message(payload: dict[str, Any]) -> tuple[str, bool]:
    include_raw = bool(payload.get("include_raw", False))

    if "message" in payload:
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            raise HTTPException(status_code=422, detail="message must be a non-empty string")
        return message, include_raw

    business_payload = _extract_business_payload(payload)
    if not business_payload:
        raise HTTPException(status_code=422, detail="request body must contain JSON fields")

    return json.dumps(business_payload, ensure_ascii=False), include_raw


def call_llm(
    client: OpenAI, model: str, message: str, system_prompt: str | None
) -> tuple[str, dict[str, Any]]:
    attempts = get_llm_retry_attempts()
    backoff_seconds = get_llm_retry_backoff_seconds()
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return _call_llm_once(client, model, message, system_prompt)
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            sleep_seconds = backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                "LLM call failed, retrying attempt %s/%s after %.1fs: %s",
                attempt + 1,
                attempts,
                sleep_seconds,
                exc,
            )
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

    raise RuntimeError(f"LLM call failed after {attempts} attempts: {last_error}") from last_error


def _call_llm_once(
    client: OpenAI, model: str, message: str, system_prompt: str | None
) -> tuple[str, dict[str, Any]]:
    if hasattr(client, "responses"):
        response = client.responses.create(
            model=model,
            input=build_input(message, system_prompt),
        )
        return getattr(response, "output_text", "") or "", response.model_dump()

    messages: list[dict[str, Any]] = []

    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    messages.append({"role": "user", "content": message})

    response = client.chat.completions.create(
        model=model,
        messages=messages,
    )
    return extract_text_from_chat_completion(response), response.model_dump()


app = FastAPI(
    title="DMXAPI LLM Backend",
    description="Backend service for probability-related LLM calls",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request, call_next):
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


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/llm/chat", response_model=ChatResponse)
def chat(payload: ChatRequest) -> ChatResponse:
    try:
        client = build_client()
        output_text, raw_response = call_llm(
            client, payload.model, payload.message, get_probability_prompt()
        )
    except Exception as exc:
        logger.exception("LLM chat request failed: %s", exc)
        raise HTTPException(status_code=500, detail=SAFE_ERROR_DETAIL) from exc

    return ChatResponse(
        model=payload.model,
        text=output_text,
        raw=raw_response if payload.include_raw else None,
    )


def _run_probability_request(payload: dict[str, Any], system_prompt: str | None, with_reason: bool = False) -> ChatResponse:
    try:
        cached = _lookup_probability_from_git(payload, with_reason=with_reason)
        if cached is not None:
            logger.info(
                "probability git cache hit project_id=%s filename=%s",
                cached.raw.get("project_id") if cached.raw else "-",
                cached.raw.get("filename") if cached.raw else "-",
            )
            return cached
    except (error.HTTPError, error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
        logger.info("probability git cache skipped: %s", exc)

    message, include_raw = extract_probability_message(payload)
    model = get_default_model()

    try:
        client = build_client()
        output_text, raw_response = call_llm(client, model, message, system_prompt)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("probability LLM request failed: %s", exc)
        raise HTTPException(status_code=500, detail=SAFE_ERROR_DETAIL) from exc

    return ChatResponse(
        model=model,
        text=output_text,
        raw=raw_response if include_raw else None,
    )


@app.post("/api/llm/probability", response_model=ChatResponse)
def probability(payload: dict[str, Any]) -> ChatResponse:
    return _run_probability_request(payload, get_probability_prompt(), with_reason=False)


@app.post("/api/llm/probability-reason", response_model=ChatResponse)
def probability_reason(payload: dict[str, Any]) -> ChatResponse:
    return _run_probability_request(payload, get_probability_reason_prompt(), with_reason=True)


if __name__ == "__main__":
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    reload_enabled = os.getenv("UVICORN_RELOAD", "false").lower() == "true"

    uvicorn.run("app.main:app", host=host, port=port, reload=reload_enabled)
