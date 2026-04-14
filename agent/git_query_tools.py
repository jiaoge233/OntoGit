from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def _normalize_env(value: str | None) -> str:
    normalized = (value or "development").strip().lower()
    alias_map = {
        "dev": "development",
        "development": "development",
        "prod": "production",
        "production": "production",
    }
    return alias_map.get(normalized, "development")


def _load_env_values() -> dict[str, str]:
    gateway_base = _read_env_file(ROOT_DIR / "gateway" / ".env")
    initial_mode = _normalize_env(os.environ.get("XG_ENV"))
    gateway_mode = _read_env_file(ROOT_DIR / "gateway" / f".env.{initial_mode}")

    merged: dict[str, str] = {}
    merged.update(gateway_base)
    merged.update(gateway_mode)
    merged.update(os.environ)
    return merged


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


class GatewayClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        bearer_token: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        env_values = _load_env_values()
        resolved_base = (base_url or env_values.get("GATEWAY_BASE_URL") or "http://127.0.0.1:8080").strip()
        self.base_url = resolved_base.rstrip("/")
        self.api_key = (api_key or env_values.get("GATEWAY_SERVICE_API_KEY") or "").strip()
        self.bearer_token = (bearer_token or env_values.get("GATEWAY_BEARER_TOKEN") or "").strip()
        self.timeout = timeout

    def get_json(self, path: str) -> dict[str, Any]:
        return self.request_json(method="GET", path=path)

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request_json(method="POST", path=path, payload=payload)

    def login(self, username: str, password: str) -> str:
        response = self.post_json(
            "/auth/login",
            {
                "username": username,
                "password": password,
            },
        )
        token = str(response.get("access_token", "")).strip()
        if not token:
            raise RuntimeError("login succeeded but no access_token was returned")
        self.bearer_token = token
        return token

    def request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.base_url + path
        headers = {"Accept": "application/json"}
        body: bytes | None = None
        if payload is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        elif self.api_key:
            headers["X-API-Key"] = self.api_key

        request = Request(url=url, headers=headers, method=method.upper(), data=body)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                response_text = response.read().decode("utf-8")
        except HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"gateway returned HTTP {exc.code}: {body_text}") from exc
        except URLError as exc:
            raise RuntimeError(
                f"failed to connect to gateway at {self.base_url}. "
                "Please make sure the gateway service is running."
            ) from exc
        return json.loads(response_text)


def _normalize_lookup_value(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized.endswith(".json"):
        normalized = normalized[:-5]
    return "".join(ch for ch in normalized if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _expand_lookup_aliases(value: str) -> set[str]:
    alias_map = {
        "student": {"学生"},
        "teacher": {"老师", "教师"},
        "school": {"学校"},
        "course": {"课程"},
        "class": {"班级"},
    }
    normalized = _normalize_lookup_value(value)
    values = {normalized} if normalized else set()
    for key, aliases in alias_map.items():
        if normalized == key or normalized in aliases:
            values.add(key)
            values.update(aliases)
    return {_normalize_lookup_value(item) for item in values if _normalize_lookup_value(item)}


def list_project_ontology_candidates(
    project_id: str,
    client: GatewayClient | None = None,
) -> list[dict[str, Any]]:
    if not str(project_id).strip():
        raise ValueError("project_id is required")

    gateway_client = client or GatewayClient()
    timeline_path = f"/xg/timelines/{quote(str(project_id).strip(), safe='')}"
    timeline_response = gateway_client.get_json(timeline_path)
    timelines = timeline_response.get("timelines") or []

    candidates: list[dict[str, Any]] = []
    for timeline in timelines:
        filename = str(timeline.get("filename", "")).strip()
        if not filename:
            continue

        read_path = (
            f"/xg/read/{quote(str(project_id).strip(), safe='')}/"
            f"{quote(filename, safe='')}"
        )
        ontology_name = ""
        try:
            read_response = gateway_client.get_json(read_path)
            data = read_response.get("data")
            if isinstance(data, dict):
                ontology_name = str(data.get("name", "")).strip()
        except Exception:
            ontology_name = ""

        filename_stem = filename[:-5] if filename.lower().endswith(".json") else filename
        candidates.append(
            {
                "filename": filename,
                "filename_stem": filename_stem,
                "ontology_name": ontology_name,
            }
        )

    return candidates


def resolve_ontology_filename(
    project_id: str,
    filename: str | None = None,
    ontology_name: str | None = None,
    client: GatewayClient | None = None,
) -> tuple[str, dict[str, Any]]:
    filename = str(filename or "").strip()
    ontology_name = str(ontology_name or "").strip()
    if filename:
        return filename, {"mode": "filename", "input": filename}
    if not ontology_name:
        raise ValueError("either filename or ontology_name is required")

    gateway_client = client or GatewayClient()
    path = (
        "/xg/ontology-resolve"
        f"?project_id={quote(str(project_id).strip(), safe='')}"
        f"&query={quote(ontology_name, safe='')}"
    )
    response = gateway_client.get_json(path)
    candidate = response.get("candidate") or {}
    resolved_filename = str(response.get("filename", "")).strip()
    if not resolved_filename:
        raise RuntimeError(f"ontology resolve returned empty filename for query {ontology_name}")
    return resolved_filename, {
        "mode": "ontology_name",
        "input": ontology_name,
        "matched_by": response.get("matched_by"),
        "matched_candidate": candidate,
    }


COMMUNITY_TOP_VERSION_TOOL = ToolDefinition(
    name="get_community_top_version",
    description="查询指定项目下某个本体当前社区星标最高的推荐版本。",
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 ID，例如 demo。"},
            "filename": {"type": "string", "description": "本体文件名，例如 student.json。可选。"},
            "ontology_name": {"type": "string", "description": "本体名称或对象名，例如 学校 或 school。可选。"},
        },
        "required": ["project_id"],
        "additionalProperties": False,
    },
)

OFFICIAL_RECOMMENDATION_TOOL = ToolDefinition(
    name="get_official_recommendation",
    description="查询指定项目下某个本体当前官方推荐的版本。",
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 ID，例如 demo。"},
            "filename": {"type": "string", "description": "本体文件名，例如 student.json。可选。"},
            "ontology_name": {"type": "string", "description": "本体名称或对象名，例如 学校 或 school。可选。"},
        },
        "required": ["project_id"],
        "additionalProperties": False,
    },
)


def get_community_top_version(
    project_id: str,
    filename: str | None = None,
    ontology_name: str | None = None,
    client: GatewayClient | None = None,
) -> dict[str, Any]:
    if not str(project_id).strip():
        raise ValueError("project_id is required")

    gateway_client = client or GatewayClient()
    resolved_filename, resolution = resolve_ontology_filename(
        project_id=str(project_id).strip(),
        filename=filename,
        ontology_name=ontology_name,
        client=gateway_client,
    )
    path = (
        "/xg/version-recommend/community"
        f"?project_id={quote(str(project_id).strip(), safe='')}"
        f"&filename={quote(str(resolved_filename).strip(), safe='')}"
    )
    response = gateway_client.get_json(path)

    recommended = response.get("version") or response.get("recommended_version") or {}
    return {
        "tool_name": COMMUNITY_TOP_VERSION_TOOL.name,
        "project_id": str(project_id).strip(),
        "filename": str(resolved_filename).strip(),
        "ontology_name": str(ontology_name or "").strip() or recommended.get("object_name"),
        "target_resolution": resolution,
        "recommended_version_id": response.get("recommended_version_id", recommended.get("version_id")),
        "community_score": recommended.get("community_score", recommended.get("stars", 0)),
        "stars": recommended.get("stars", 0),
        "community_rank": recommended.get("community_rank"),
        "message": recommended.get("msg", recommended.get("message")),
        "committer": recommended.get("committer"),
        "time": recommended.get("time"),
        "raw": response,
    }


def get_official_recommendation(
    project_id: str,
    filename: str | None = None,
    ontology_name: str | None = None,
    client: GatewayClient | None = None,
) -> dict[str, Any]:
    if not str(project_id).strip():
        raise ValueError("project_id is required")

    gateway_client = client or GatewayClient()
    resolved_filename, resolution = resolve_ontology_filename(
        project_id=str(project_id).strip(),
        filename=filename,
        ontology_name=ontology_name,
        client=gateway_client,
    )
    path = (
        "/xg/version-recommend/official"
        f"?project_id={quote(str(project_id).strip(), safe='')}"
        f"&filename={quote(str(resolved_filename).strip(), safe='')}"
    )
    response = gateway_client.get_json(path)

    recommended = response.get("version") or {}
    return {
        "tool_name": OFFICIAL_RECOMMENDATION_TOOL.name,
        "project_id": str(project_id).strip(),
        "filename": str(resolved_filename).strip(),
        "ontology_name": str(ontology_name or "").strip() or recommended.get("object_name"),
        "target_resolution": resolution,
        "source": response.get("source"),
        "recommended_version_id": response.get("recommended_version_id", recommended.get("version_id")),
        "official_status": recommended.get("official_status"),
        "official_reason": recommended.get("official_reason"),
        "official_operator": recommended.get("official_operator"),
        "official_at": recommended.get("official_at"),
        "message": recommended.get("msg", recommended.get("message")),
        "committer": recommended.get("committer"),
        "time": recommended.get("time"),
        "raw": response,
    }


def get_available_tools() -> list[ToolDefinition]:
    return [COMMUNITY_TOP_VERSION_TOOL, OFFICIAL_RECOMMENDATION_TOOL]


def run_tool(name: str, arguments: dict[str, Any], client: GatewayClient | None = None) -> dict[str, Any]:
    if name == COMMUNITY_TOP_VERSION_TOOL.name:
        return get_community_top_version(
            project_id=str(arguments.get("project_id", "")),
            filename=str(arguments.get("filename", "") or ""),
            ontology_name=str(arguments.get("ontology_name", "") or ""),
            client=client,
        )
    if name == OFFICIAL_RECOMMENDATION_TOOL.name:
        return get_official_recommendation(
            project_id=str(arguments.get("project_id", "")),
            filename=str(arguments.get("filename", "") or ""),
            ontology_name=str(arguments.get("ontology_name", "") or ""),
            client=client,
        )
    raise ValueError(f"unsupported tool: {name}")
