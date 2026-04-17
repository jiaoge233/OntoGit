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

FILE_TIMELINE_TOOL = ToolDefinition(
    name="get_file_timeline",
    description="查询指定项目下某个本体的版本时间线和最近变化。",
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 ID，例如 demo。"},
            "filename": {"type": "string", "description": "本体文件名，例如 student.json。可选。"},
            "ontology_name": {"type": "string", "description": "本体名称或对象名，例如 学校 或 school。可选。"},
            "limit": {"type": "integer", "description": "最多返回多少条版本记录，默认 10。"},
        },
        "required": ["project_id"],
        "additionalProperties": False,
    },
)

VERSION_CONTENT_TOOL = ToolDefinition(
    name="get_version_content",
    description="读取某个本体当前工作区内容，或读取指定版本的本体内容。",
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 ID，例如 demo。"},
            "filename": {"type": "string", "description": "本体文件名，例如 student.json。可选。"},
            "ontology_name": {"type": "string", "description": "本体名称或对象名，例如 学校 或 school。可选。"},
            "version_id": {"type": "integer", "description": "要读取的历史版本号。未提供时读取当前工作区。"},
        },
        "required": ["project_id"],
        "additionalProperties": False,
    },
)

COMPARE_VERSIONS_TOOL = ToolDefinition(
    name="compare_versions",
    description="比较某个本体的两个版本内容，输出 JSON 字段级差异。",
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 ID，例如 demo。"},
            "filename": {"type": "string", "description": "本体文件名，例如 student.json。可选。"},
            "ontology_name": {"type": "string", "description": "本体名称或对象名，例如 学校 或 school。可选。"},
            "left_version_id": {"type": "integer", "description": "左侧版本号。"},
            "right_version_id": {"type": "integer", "description": "右侧版本号。"},
        },
        "required": ["project_id", "left_version_id", "right_version_id"],
        "additionalProperties": False,
    },
)

FIND_GOVERNANCE_GAPS_TOOL = ToolDefinition(
    name="find_governance_gaps",
    description="扫描项目或单个本体的治理缺口，包括官方推荐缺失、官方与社区分歧、概率缺失或偏低、版本树复杂度等。",
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 ID，例如 demo。"},
            "filename": {"type": "string", "description": "本体文件名，例如 student.json。可选。"},
            "ontology_name": {"type": "string", "description": "本体名称或对象名，例如 学校 或 school。可选。"},
            "limit": {"type": "integer", "description": "项目级扫描时最多分析多少个本体，默认 20。"},
        },
        "required": ["project_id"],
        "additionalProperties": False,
    },
)


def _parse_probability_score(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric / 100.0 if numeric > 1 else numeric

    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("%"):
            return float(text[:-1].strip()) / 100.0
        numeric = float(text)
        return numeric / 100.0 if numeric > 1 else numeric
    except ValueError:
        return None


def _gap(
    code: str,
    severity: str,
    title: str,
    detail: str,
    suggestion: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "detail": detail,
        "suggestion": suggestion,
        "evidence": evidence or {},
    }


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


def get_file_timeline(
    project_id: str,
    filename: str | None = None,
    ontology_name: str | None = None,
    limit: int | None = None,
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
    path = f"/xg/timelines/{quote(str(project_id).strip(), safe='')}"
    response = gateway_client.get_json(path)
    timelines = response.get("timelines") or []

    timeline = None
    for item in timelines:
        if str(item.get("filename", "")).strip() == resolved_filename:
            timeline = item
            break
    if timeline is None:
        raise RuntimeError(f"timeline not found for {project_id}/{resolved_filename}")

    history = timeline.get("history") or []
    ordered_history = sorted(
        [entry for entry in history if isinstance(entry, dict)],
        key=lambda entry: int(entry.get("version_id") or entry.get("currvision") or 0),
        reverse=True,
    )
    normalized_limit = int(limit or 10)
    if normalized_limit > 0:
        returned_history = ordered_history[:normalized_limit]
    else:
        returned_history = ordered_history

    latest = returned_history[0] if returned_history else {}
    parent_sets = {
        tuple(entry.get("parent_version_ids") or [])
        for entry in history
        if isinstance(entry, dict)
    }
    branch_count = len({entry.get("primary_parent_version_id") for entry in history if isinstance(entry, dict) and entry.get("primary_parent_version_id") is not None})

    return {
        "tool_name": FILE_TIMELINE_TOOL.name,
        "project_id": str(project_id).strip(),
        "filename": resolved_filename,
        "ontology_name": str(ontology_name or "").strip() or latest.get("object_name"),
        "target_resolution": resolution,
        "version_count": int(timeline.get("version_count") or len(history)),
        "latest_version_id": timeline.get("latest_version_id") or latest.get("version_id"),
        "latest_message": latest.get("msg") or latest.get("message"),
        "latest_committer": latest.get("committer"),
        "latest_time": latest.get("time"),
        "branch_count": max(branch_count, 1) if history else 0,
        "parent_shape_count": len(parent_sets),
        "history": returned_history,
        "raw": timeline,
    }


def get_version_content(
    project_id: str,
    filename: str | None = None,
    ontology_name: str | None = None,
    version_id: int | str | None = None,
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

    normalized_version_id = str(version_id or "").strip()
    if normalized_version_id:
        path = (
            f"/xg/version-read/{quote(str(project_id).strip(), safe='')}/"
            f"{quote(normalized_version_id, safe='')}"
            f"?filename={quote(resolved_filename, safe='')}"
        )
        response = gateway_client.get_json(path)
        data = response.get("data", response)
        source = "version"
    else:
        path = (
            f"/xg/read/{quote(str(project_id).strip(), safe='')}/"
            f"{quote(resolved_filename, safe='')}"
        )
        response = gateway_client.get_json(path)
        data = response.get("data")
        source = "current"

    if not isinstance(data, dict):
        raise RuntimeError(f"version content for {project_id}/{resolved_filename} is not a JSON object")

    return {
        "tool_name": VERSION_CONTENT_TOOL.name,
        "project_id": str(project_id).strip(),
        "filename": resolved_filename,
        "ontology_name": str(ontology_name or "").strip() or data.get("name"),
        "target_resolution": resolution,
        "source": source,
        "version_id": int(normalized_version_id) if normalized_version_id else None,
        "name": data.get("name"),
        "agent": data.get("agent"),
        "abilities": data.get("abilities"),
        "interactions": data.get("interactions"),
        "probability": data.get("probability"),
        "content": data,
        "raw": response,
    }


def _json_path(parent: str, key: str | int) -> str:
    if parent == "":
        return str(key)
    return f"{parent}.{key}"


def _summarize_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return {"type": "list", "length": len(value), "preview": value[:3]}
    if isinstance(value, dict):
        return {"type": "object", "keys": sorted(list(value.keys()))[:8]}
    return str(value)


def _diff_json(left: Any, right: Any, path: str = "") -> list[dict[str, Any]]:
    if isinstance(left, dict) and isinstance(right, dict):
        changes: list[dict[str, Any]] = []
        left_keys = set(left.keys())
        right_keys = set(right.keys())
        for key in sorted(left_keys - right_keys):
            changes.append({
                "path": _json_path(path, key),
                "change_type": "removed",
                "left": _summarize_value(left[key]),
                "right": None,
            })
        for key in sorted(right_keys - left_keys):
            changes.append({
                "path": _json_path(path, key),
                "change_type": "added",
                "left": None,
                "right": _summarize_value(right[key]),
            })
        for key in sorted(left_keys & right_keys):
            changes.extend(_diff_json(left[key], right[key], _json_path(path, key)))
        return changes

    if isinstance(left, list) and isinstance(right, list):
        if left == right:
            return []
        return [{
            "path": path or "$",
            "change_type": "modified",
            "left": _summarize_value(left),
            "right": _summarize_value(right),
        }]

    if left != right:
        return [{
            "path": path or "$",
            "change_type": "modified",
            "left": _summarize_value(left),
            "right": _summarize_value(right),
        }]
    return []


def compare_versions(
    project_id: str,
    left_version_id: int | str,
    right_version_id: int | str,
    filename: str | None = None,
    ontology_name: str | None = None,
    client: GatewayClient | None = None,
) -> dict[str, Any]:
    if not str(project_id).strip():
        raise ValueError("project_id is required")
    if not str(left_version_id).strip() or not str(right_version_id).strip():
        raise ValueError("left_version_id and right_version_id are required")

    gateway_client = client or GatewayClient()
    resolved_filename, resolution = resolve_ontology_filename(
        project_id=str(project_id).strip(),
        filename=filename,
        ontology_name=ontology_name,
        client=gateway_client,
    )
    left = get_version_content(
        project_id=project_id,
        filename=resolved_filename,
        version_id=left_version_id,
        client=gateway_client,
    )
    right = get_version_content(
        project_id=project_id,
        filename=resolved_filename,
        version_id=right_version_id,
        client=gateway_client,
    )
    changes = _diff_json(left["content"], right["content"])

    added = [item for item in changes if item["change_type"] == "added"]
    removed = [item for item in changes if item["change_type"] == "removed"]
    modified = [item for item in changes if item["change_type"] == "modified"]

    return {
        "tool_name": COMPARE_VERSIONS_TOOL.name,
        "project_id": str(project_id).strip(),
        "filename": resolved_filename,
        "ontology_name": str(ontology_name or "").strip() or right.get("name") or left.get("name"),
        "target_resolution": resolution,
        "left_version_id": int(left_version_id),
        "right_version_id": int(right_version_id),
        "summary": {
            "total_changes": len(changes),
            "added": len(added),
            "removed": len(removed),
            "modified": len(modified),
        },
        "changes": changes,
        "left_content": left["content"],
        "right_content": right["content"],
    }


def find_governance_gaps(
    project_id: str,
    filename: str | None = None,
    ontology_name: str | None = None,
    limit: int | None = None,
    client: GatewayClient | None = None,
) -> dict[str, Any]:
    if not str(project_id).strip():
        raise ValueError("project_id is required")

    gateway_client = client or GatewayClient()
    normalized_project_id = str(project_id).strip()
    timeline_path = f"/xg/timelines/{quote(normalized_project_id, safe='')}"
    timeline_response = gateway_client.get_json(timeline_path)
    timelines = [
        item for item in (timeline_response.get("timelines") or [])
        if isinstance(item, dict) and str(item.get("filename", "")).strip()
    ]

    resolution: dict[str, Any] = {"mode": "project", "input": normalized_project_id}
    if str(filename or "").strip() or str(ontology_name or "").strip():
        resolved_filename, resolution = resolve_ontology_filename(
            project_id=normalized_project_id,
            filename=filename,
            ontology_name=ontology_name,
            client=gateway_client,
        )
        timelines = [
            item for item in timelines
            if str(item.get("filename", "")).strip() == resolved_filename
        ]
        if not timelines:
            raise RuntimeError(f"timeline not found for {normalized_project_id}/{resolved_filename}")

    normalized_limit = int(limit or 20)
    if normalized_limit > 0:
        timelines = timelines[:normalized_limit]

    findings: list[dict[str, Any]] = []
    analyzed_files: list[dict[str, Any]] = []

    for timeline in timelines:
        target_filename = str(timeline.get("filename", "")).strip()
        history = [
            entry for entry in (timeline.get("history") or [])
            if isinstance(entry, dict)
        ]
        ordered_history = sorted(
            history,
            key=lambda entry: int(entry.get("version_id") or entry.get("currvision") or 0),
            reverse=True,
        )
        latest = ordered_history[0] if ordered_history else {}
        latest_version_id = latest.get("version_id") or latest.get("currvision")
        object_name = str(latest.get("object_name", "") or "").strip()
        file_gaps: list[dict[str, Any]] = []

        if not ordered_history:
            file_gaps.append(_gap(
                code="no_version_history",
                severity="high",
                title="缺少版本历史",
                detail=f"{target_filename} 没有可分析的版本历史。",
                suggestion="先补齐本体版本提交，再进入治理推荐流程。",
            ))

        try:
            official = get_official_recommendation(
                project_id=normalized_project_id,
                filename=target_filename,
                client=gateway_client,
            )
        except Exception as exc:
            official = {"error": str(exc), "recommended_version_id": None}

        try:
            community = get_community_top_version(
                project_id=normalized_project_id,
                filename=target_filename,
                client=gateway_client,
            )
        except Exception as exc:
            community = {"error": str(exc), "recommended_version_id": None, "stars": 0}

        official_version_id = official.get("recommended_version_id")
        community_version_id = community.get("recommended_version_id")
        community_stars = int(community.get("stars") or community.get("community_score") or 0)

        if not official_version_id:
            file_gaps.append(_gap(
                code="missing_official_recommendation",
                severity="high",
                title="缺少官方推荐版本",
                detail=f"{target_filename} 当前没有明确的官方推荐版本。",
                suggestion="由治理方评审当前版本树后设置官方推荐版本。",
                evidence={"community_version_id": community_version_id, "community_stars": community_stars},
            ))

        if official_version_id and latest_version_id and str(official_version_id) != str(latest_version_id):
            file_gaps.append(_gap(
                code="official_not_latest",
                severity="medium",
                title="官方推荐不是最新版本",
                detail=f"官方推荐 V{official_version_id}，最新版本是 V{latest_version_id}。",
                suggestion="检查最新版本是否需要纳入官方评审；如果最新版本不稳定，应保留现状并记录理由。",
                evidence={"official_version_id": official_version_id, "latest_version_id": latest_version_id},
            ))

        if official_version_id and community_version_id and str(official_version_id) != str(community_version_id):
            severity = "high" if community_stars >= 3 else "medium"
            file_gaps.append(_gap(
                code="official_community_divergence",
                severity=severity,
                title="官方推荐与社区星标最高版本不一致",
                detail=f"官方推荐 V{official_version_id}，社区推荐 V{community_version_id}，社区星标 {community_stars}。",
                suggestion="触发一次人工复核，判断社区高星版本是否应晋升为官方推荐，或补充官方推荐理由。",
                evidence={
                    "official_version_id": official_version_id,
                    "community_version_id": community_version_id,
                    "community_stars": community_stars,
                },
            ))

        if community_version_id and community_stars <= 0:
            file_gaps.append(_gap(
                code="no_community_signal",
                severity="low",
                title="缺少社区星标信号",
                detail=f"{target_filename} 当前没有有效星标数据。",
                suggestion="保留官方轨道判断，同时引导用户对可信版本进行星标反馈。",
                evidence={"community_version_id": community_version_id, "community_stars": community_stars},
            ))

        parent_shapes = {
            tuple(entry.get("parent_version_ids") or [])
            for entry in ordered_history
        }
        parent_count = len({
            entry.get("primary_parent_version_id")
            for entry in ordered_history
            if entry.get("primary_parent_version_id") is not None
        })
        if parent_count > 1 or len(parent_shapes) > 2:
            file_gaps.append(_gap(
                code="branching_version_tree",
                severity="medium",
                title="版本树存在分叉治理压力",
                detail=f"{target_filename} 存在多个父版本形态或分叉路径。",
                suggestion="对分叉版本做合并评审，明确主线版本和废弃分支。",
                evidence={"parent_count": parent_count, "parent_shape_count": len(parent_shapes)},
            ))

        current_probability = None
        current_probability_score = None
        current_read_error = ""
        try:
            current = get_version_content(
                project_id=normalized_project_id,
                filename=target_filename,
                client=gateway_client,
            )
            object_name = object_name or str(current.get("name") or "").strip()
            current_probability = current.get("probability")
            current_probability_score = _parse_probability_score(current_probability)
        except Exception as exc:
            current_read_error = str(exc)

        if current_read_error:
            file_gaps.append(_gap(
                code="current_content_unreadable",
                severity="medium",
                title="当前本体内容不可读",
                detail=f"{target_filename} 当前工作区内容读取失败。",
                suggestion="检查当前工作区文件是否存在、JSON 是否合法，以及网关鉴权是否正常。",
                evidence={"error": current_read_error},
            ))
        elif current_probability_score is None:
            file_gaps.append(_gap(
                code="missing_probability",
                severity="medium",
                title="缺少概率字段",
                detail=f"{target_filename} 当前本体内容没有可解析的 probability。",
                suggestion="重新执行概率推理并把概率写回当前版本的本体数据。",
                evidence={"probability": current_probability},
            ))
        elif current_probability_score < 0.6:
            file_gaps.append(_gap(
                code="low_probability",
                severity="high",
                title="本体真实性概率偏低",
                detail=f"{target_filename} 当前 probability 为 {current_probability}。",
                suggestion="先进入治理复核，不建议直接设为官方推荐。",
                evidence={"probability": current_probability, "score": current_probability_score},
            ))

        analyzed_files.append({
            "filename": target_filename,
            "ontology_name": object_name,
            "version_count": len(ordered_history),
            "latest_version_id": latest_version_id,
            "official_version_id": official_version_id,
            "community_version_id": community_version_id,
            "community_stars": community_stars,
            "probability": current_probability,
            "gap_count": len(file_gaps),
        })
        for item in file_gaps:
            findings.append({
                "filename": target_filename,
                "ontology_name": object_name,
                **item,
            })

    severity_order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda item: (severity_order.get(str(item.get("severity")), 9), item.get("filename", ""), item.get("code", "")))
    summary = {
        "analyzed_file_count": len(analyzed_files),
        "gap_count": len(findings),
        "high": len([item for item in findings if item.get("severity") == "high"]),
        "medium": len([item for item in findings if item.get("severity") == "medium"]),
        "low": len([item for item in findings if item.get("severity") == "low"]),
    }

    return {
        "tool_name": FIND_GOVERNANCE_GAPS_TOOL.name,
        "project_id": normalized_project_id,
        "scope": "single_ontology" if resolution.get("mode") in {"filename", "ontology_name"} else "project",
        "target_resolution": resolution,
        "summary": summary,
        "findings": findings,
        "analyzed_files": analyzed_files,
    }


def get_available_tools() -> list[ToolDefinition]:
    return [
        COMMUNITY_TOP_VERSION_TOOL,
        OFFICIAL_RECOMMENDATION_TOOL,
        FILE_TIMELINE_TOOL,
        VERSION_CONTENT_TOOL,
        COMPARE_VERSIONS_TOOL,
        FIND_GOVERNANCE_GAPS_TOOL,
    ]


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
    if name == FILE_TIMELINE_TOOL.name:
        return get_file_timeline(
            project_id=str(arguments.get("project_id", "")),
            filename=str(arguments.get("filename", "") or ""),
            ontology_name=str(arguments.get("ontology_name", "") or ""),
            limit=int(arguments.get("limit") or 10),
            client=client,
        )
    if name == VERSION_CONTENT_TOOL.name:
        return get_version_content(
            project_id=str(arguments.get("project_id", "")),
            filename=str(arguments.get("filename", "") or ""),
            ontology_name=str(arguments.get("ontology_name", "") or ""),
            version_id=arguments.get("version_id"),
            client=client,
        )
    if name == COMPARE_VERSIONS_TOOL.name:
        return compare_versions(
            project_id=str(arguments.get("project_id", "")),
            filename=str(arguments.get("filename", "") or ""),
            ontology_name=str(arguments.get("ontology_name", "") or ""),
            left_version_id=arguments.get("left_version_id", ""),
            right_version_id=arguments.get("right_version_id", ""),
            client=client,
        )
    if name == FIND_GOVERNANCE_GAPS_TOOL.name:
        return find_governance_gaps(
            project_id=str(arguments.get("project_id", "")),
            filename=str(arguments.get("filename", "") or ""),
            ontology_name=str(arguments.get("ontology_name", "") or ""),
            limit=int(arguments.get("limit") or 20),
            client=client,
        )
    raise ValueError(f"unsupported tool: {name}")
