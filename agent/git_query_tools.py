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
    try:
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
    except Exception as exc:
        resolve_error = str(exc)

    query_aliases = _expand_lookup_aliases(ontology_name)
    for candidate in list_project_ontology_candidates(str(project_id).strip(), client=gateway_client):
        candidate_values = set()
        candidate_values.update(_expand_lookup_aliases(str(candidate.get("filename", ""))))
        candidate_values.update(_expand_lookup_aliases(str(candidate.get("filename_stem", ""))))
        candidate_values.update(_expand_lookup_aliases(str(candidate.get("ontology_name", ""))))
        if query_aliases & candidate_values:
            return str(candidate["filename"]).strip(), {
                "mode": "ontology_name_fallback_scan",
                "input": ontology_name,
                "matched_by": "fallback_scan",
                "matched_candidate": candidate,
                "resolve_error": resolve_error,
            }

    raise RuntimeError(f"ontology resolve failed for query {ontology_name}: {resolve_error}")


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

INFER_CAUSAL_LOGIC_TOOL = ToolDefinition(
    name="infer_causal_logic",
    description="检索指定项目中的本体关系图，合并输入的新本体，基于规则推导新的逻辑属性或关系。例如 A 是 B 的父亲、B 是 C 的父亲，可推导 A 是 C 的爷爷。",
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 ID，例如 demo。"},
            "new_ontology": {
                "type": "object",
                "description": "新本体 JSON，例如 {\"name\":\"C\",\"interactions\":[{\"target\":\"B\",\"type\":\"父亲\"}]}。若和库内同名，则用于覆盖当前库内该本体参与推理。",
            },
            "filename": {"type": "string", "description": "可选：如果未直接提供 new_ontology，可从当前项目读取该本体文件。"},
            "ontology_name": {"type": "string", "description": "可选：如果未直接提供 new_ontology，可用本体名解析并读取。"},
        },
        "required": ["project_id"],
        "additionalProperties": False,
    },
)

REVIEW_ONTOLOGY_ATTRIBUTE_TOOL = ToolDefinition(
    name="review_ontology_attribute",
    description="评估新增属性是否适合挂到某个本体上，并给出语义类型、兼容性判断、建议新增类别和可选补丁。例如 学生 + 抓小偷 => 条件成立，可建议 警校学生 或 见义勇为学生。",
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 ID，例如 demo。"},
            "filename": {"type": "string", "description": "本体文件名，例如 student.json。可选。"},
            "ontology_name": {"type": "string", "description": "本体名称或对象名，例如 学生。可选。"},
            "current_ontology": {
                "type": "object",
                "description": "可选：当前本体 JSON。若提供则不读取项目当前版本。",
            },
            "proposed_ontology": {
                "type": "object",
                "description": "可选：修改后的完整本体 JSON。工具会和当前本体 diff，自动识别新增属性。",
            },
            "added_attributes": {
                "description": "可选：直接指定新增属性。可以是字符串、字符串数组或属性对象数组。",
            },
        },
        "required": ["project_id"],
        "additionalProperties": False,
    },
)

REVIEW_ONTOLOGY_CONSISTENCY_TOOL = ToolDefinition(
    name="review_ontology_consistency",
    description="评估某个本体当前已有能力和关系是否符合本体主体语义，并给出不成立或条件成立时建议新增的类别。例如学生本体已有抓小偷能力，可建议警校学生或见义勇为学生。",
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 ID，例如 demo。"},
            "filename": {"type": "string", "description": "本体文件名，例如 student.json。可选。"},
            "ontology_name": {"type": "string", "description": "本体名称或对象名，例如 学生。可选。"},
            "current_ontology": {
                "type": "object",
                "description": "可选：当前本体 JSON。若提供则不读取项目当前版本。",
            },
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


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_logic_value(value: Any) -> str:
    return _normalize_lookup_value(_as_text(value))


FATHER_RELATIONS = {"父亲", "爸爸", "父", "father", "dad"}
MOTHER_RELATIONS = {"母亲", "妈妈", "母", "mother", "mom"}
SON_RELATIONS = {"儿子", "子", "son"}
DAUGHTER_RELATIONS = {"女儿", "daughter"}

TRANSITIVE_RELATION_GROUPS = [
    {
        "aliases": {"导致", "造成", "引起", "触发", "cause", "causes", "leadsto", "leads_to"},
        "inferred_type": "间接导致",
        "rule_id": "transitive_cause",
    },
    {
        "aliases": {"影响", "作用于", "影响到", "affect", "affects", "influence", "influences"},
        "inferred_type": "间接影响",
        "rule_id": "transitive_influence",
    },
    {
        "aliases": {"依赖", "依赖于", "需要", "依靠", "dependson", "depends_on", "requires"},
        "inferred_type": "间接依赖",
        "rule_id": "transitive_dependency",
    },
    {
        "aliases": {"包含", "包括", "下辖", "拥有", "contain", "contains", "include", "includes", "has"},
        "inferred_type": "包含",
        "rule_id": "transitive_contains",
    },
    {
        "aliases": {"属于", "隶属于", "归属于", "memberof", "member_of", "belongsto", "belongs_to"},
        "inferred_type": "属于",
        "rule_id": "transitive_belongs_to",
    },
    {
        "aliases": {"管理", "管辖", "监管", "负责", "manage", "manages", "supervise", "supervises"},
        "inferred_type": "间接管理",
        "rule_id": "transitive_management",
    },
    {
        "aliases": {"上级", "父类", "上位概念", "superclass", "parentclass", "parent_class"},
        "inferred_type": "上级",
        "rule_id": "transitive_superclass",
    },
    {
        "aliases": {"子类", "下位概念", "subclass", "childclass", "child_class"},
        "inferred_type": "子类",
        "rule_id": "transitive_subclass",
    },
    {
        "aliases": {"位于", "在", "locatedin", "located_in", "in"},
        "inferred_type": "位于",
        "rule_id": "transitive_location",
    },
    {
        "aliases": {"早于", "先于", "前置于", "before", "precedes"},
        "inferred_type": "早于",
        "rule_id": "transitive_before",
    },
    {
        "aliases": {"晚于", "后于", "after", "follows"},
        "inferred_type": "晚于",
        "rule_id": "transitive_after",
    },
]

TRANSITIVE_RELATION_INDEX: dict[str, dict[str, str]] = {}
for group in TRANSITIVE_RELATION_GROUPS:
    for alias in group["aliases"]:
        TRANSITIVE_RELATION_INDEX[_normalize_lookup_value(alias)] = {
            "inferred_type": group["inferred_type"],
            "rule_id": group["rule_id"],
        }


PROPERTY_RELATION_ALIASES = {
    "属性",
    "性质",
    "特征",
    "特点",
    "味道",
    "口味",
    "颜色",
    "状态",
    "是",
    "具有",
    "has_property",
    "property",
    "taste",
    "color",
    "is",
}

MADE_INTO_RELATION_ALIASES = {
    "制成",
    "做成",
    "加工成",
    "榨成",
    "磨成",
    "变成",
    "生产出",
    "生成",
    "酿成",
    "made_into",
    "processed_into",
    "turns_into",
}

MADE_FROM_RELATION_ALIASES = {
    "由制成",
    "由...制成",
    "来源于",
    "来自",
    "原料是",
    "以为原料",
    "以...为原料",
    "made_from",
    "processed_from",
    "source_is",
}

PROPERTY_RELATION_TYPES = {_normalize_lookup_value(item) for item in PROPERTY_RELATION_ALIASES}
MADE_INTO_RELATION_TYPES = {_normalize_lookup_value(item) for item in MADE_INTO_RELATION_ALIASES}
MADE_FROM_RELATION_TYPES = {_normalize_lookup_value(item) for item in MADE_FROM_RELATION_ALIASES}
SINGLE_VALUE_PROPERTY_TYPES = {
    _normalize_lookup_value(item)
    for item in {"味道", "口味", "颜色", "状态", "taste", "color", "status"}
}
SUSPICIOUS_BIDIRECTIONAL_RULE_IDS = {
    "transitive_contains",
    "transitive_belongs_to",
    "transitive_location",
    "transitive_before",
    "transitive_after",
    "transitive_superclass",
    "transitive_subclass",
}
FAMILY_NORMALIZED_RELATIONS = {"father", "mother", "son", "daughter", "parent"}


def _normalize_family_relation(value: Any) -> str:
    normalized = _normalize_logic_value(value)
    if normalized in {_normalize_logic_value(item) for item in FATHER_RELATIONS}:
        return "father"
    if normalized in {_normalize_logic_value(item) for item in MOTHER_RELATIONS}:
        return "mother"
    if normalized in {_normalize_logic_value(item) for item in SON_RELATIONS}:
        return "son"
    if normalized in {_normalize_logic_value(item) for item in DAUGHTER_RELATIONS}:
        return "daughter"
    return normalized


def _inverse_family_relation(value: Any) -> str:
    normalized = _normalize_family_relation(value)
    if normalized in {"son", "daughter"}:
        return "parent"
    return ""


def _ontology_display_name(data: dict[str, Any], fallback: str = "") -> str:
    name = _as_text(data.get("name"))
    return name or fallback


def _extract_relation_triples(data: dict[str, Any], source_filename: str = "") -> list[dict[str, Any]]:
    subject = _ontology_display_name(data, source_filename)
    if not subject:
        return []

    triples: list[dict[str, Any]] = []
    interactions = data.get("interactions") or []
    if not isinstance(interactions, list):
        return triples

    for index, interaction in enumerate(interactions):
        if not isinstance(interaction, dict):
            continue
        relation_type = _as_text(interaction.get("type"))
        target = _as_text(interaction.get("target"))
        if not relation_type or not target:
            continue
        normalized_subject = _normalize_logic_value(subject)
        normalized_type = _normalize_family_relation(relation_type)
        normalized_target = _normalize_logic_value(target)
        if not normalized_subject or not normalized_type or not normalized_target:
            continue
        triples.append({
            "subject": subject,
            "type": relation_type,
            "target": target,
            "normalized_subject": normalized_subject,
            "normalized_type": normalized_type,
            "normalized_target": normalized_target,
            "source_filename": source_filename,
            "source_index": index,
        })
        inverse_relation = _inverse_family_relation(relation_type)
        if inverse_relation:
            triples.append({
                "subject": target,
                "type": f"父母(由{relation_type}反推)",
                "target": subject,
                "normalized_subject": normalized_target,
                "normalized_type": inverse_relation,
                "normalized_target": normalized_subject,
                "source_filename": source_filename,
                "source_index": index,
                "derived_from_inverse": {
                    "subject": subject,
                    "type": relation_type,
                    "target": target,
                },
            })
    return triples


def _read_project_current_ontologies(project_id: str, client: GatewayClient) -> list[dict[str, Any]]:
    path = f"/xg/timelines/{quote(str(project_id).strip(), safe='')}"
    response = client.get_json(path)
    timelines = response.get("timelines") or []
    ontologies: list[dict[str, Any]] = []
    for item in timelines:
        if not isinstance(item, dict):
            continue
        filename = _as_text(item.get("filename"))
        if not filename:
            continue
        try:
            read_response = client.get_json(
                f"/xg/read/{quote(str(project_id).strip(), safe='')}/{quote(filename, safe='')}"
            )
        except Exception as exc:
            ontologies.append({
                "filename": filename,
                "data": {},
                "read_error": str(exc),
            })
            continue
        data = read_response.get("data")
        if isinstance(data, dict):
            ontologies.append({
                "filename": filename,
                "data": data,
                "read_error": "",
            })
    return ontologies


def _coerce_ontology_payload(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("new_ontology must be a JSON object")


def _coerce_json_object(value: Any, field_name: str) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"{field_name} must be a JSON object")


def _attribute_text(value: Any) -> str:
    if isinstance(value, dict):
        target = _as_text(value.get("target"))
        relation_type = _as_text(value.get("type"))
        name = _as_text(value.get("name") or value.get("value"))
        parts = [part for part in [name, relation_type, target] if part]
        return " ".join(parts)
    return _as_text(value)


def _normalize_added_attributes(value: Any) -> list[dict[str, Any]]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [{"field": "attributes", "value": value, "text": value}]
    if isinstance(value, dict):
        return [{
            "field": _as_text(value.get("field")) or "attributes",
            "value": value.get("value", value),
            "text": _attribute_text(value.get("value", value)),
        }]
    if isinstance(value, list):
        result: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                result.append({
                    "field": _as_text(item.get("field")) or "attributes",
                    "value": item.get("value", item),
                    "text": _attribute_text(item.get("value", item)),
                })
            else:
                result.append({"field": "attributes", "value": item, "text": _attribute_text(item)})
        return [item for item in result if _as_text(item.get("text"))]
    return [{"field": "attributes", "value": value, "text": _attribute_text(value)}]


def _hashable_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _diff_added_attributes(current: dict[str, Any], proposed: dict[str, Any]) -> list[dict[str, Any]]:
    additions: list[dict[str, Any]] = []
    for field in ("abilities", "interactions"):
        current_values = current.get(field) if isinstance(current.get(field), list) else []
        proposed_values = proposed.get(field) if isinstance(proposed.get(field), list) else []
        current_set = {_hashable_json(item) for item in current_values}
        for item in proposed_values:
            if _hashable_json(item) not in current_set:
                additions.append({
                    "field": field,
                    "value": item,
                    "text": _attribute_text(item),
                })

    ignored_fields = {"abilities", "interactions", "probability"}
    for field, value in proposed.items():
        if field in ignored_fields:
            continue
        if field not in current or current.get(field) != value:
            additions.append({
                "field": field,
                "value": value,
                "text": f"{field} {_attribute_text(value)}".strip(),
            })
    return [item for item in additions if _as_text(item.get("text"))]


def _ontology_profile(ontology: dict[str, Any]) -> dict[str, Any]:
    name = _ontology_display_name(ontology)
    subject_parts = [
        name,
        _as_text(ontology.get("category")),
        _as_text(ontology.get("type")),
        _as_text(ontology.get("class")),
        _as_text(ontology.get("ontology_type")),
    ]
    subject_text = " ".join(part for part in subject_parts if part)
    normalized_subject = _normalize_logic_value(subject_text)
    text_parts = [name]
    for ability in ontology.get("abilities") or []:
        text_parts.append(_attribute_text(ability))
    for interaction in ontology.get("interactions") or []:
        text_parts.append(_attribute_text(interaction))
    text = " ".join(text_parts)
    normalized = _normalize_logic_value(text)

    def detect_roles(source: str, allow_contextual: bool) -> set[str]:
        detected: set[str] = set()
        student_tokens = ["学生", "student"] if not allow_contextual else ["学生", "学习", "作业", "课程", "student"]
        if any(token in source for token in student_tokens):
            detected.add("学生")
        if any(token in source for token in ["警察", "公安", "警校", "辅警", "治安", "巡逻", "police"]):
            detected.add("警务相关人员")
        if any(token in source for token in ["保安", "安保", "security"]):
            detected.add("保安")
        if any(token in source for token in ["老师", "教师", "教学", "teacher"]):
            detected.add("教师")
        if any(token in source for token in ["家长", "父母", "父亲", "母亲", "爸爸", "妈妈", "parent"]):
            detected.add("家长")
        if any(token in source for token in ["汽车维修", "修车", "汽修", "维修技师", "汽车维修工"]):
            detected.add("汽车维修工")
        if any(token in source for token in ["群众", "居民", "市民", "受害人", "人员"]):
            detected.add("群众")
        if any(token in source for token in ["苹果", "柠檬", "葡萄", "小麦", "面粉", "面包", "饮料", "食品", "水果", "汁", "酒"]):
            detected.add("食品或物品")
        if any(token in source for token in ["团队", "组织", "管理处", "公司", "机构"]):
            detected.add("组织")
        if any(token in source for token in ["广场", "区域", "道路", "地点"]):
            detected.add("地点")
        return detected

    roles = detect_roles(normalized_subject, allow_contextual=False)
    if not roles:
        roles = detect_roles(normalized, allow_contextual=True)
    if not roles and name:
        roles.add("未知主体")

    return {
        "name": name,
        "roles": sorted(roles),
        "profile_text": text,
    }


def _analyze_attribute_semantics(attribute: dict[str, Any]) -> dict[str, Any]:
    text = _as_text(attribute.get("text"))
    normalized = _normalize_logic_value(text)

    if any(token in normalized for token in ["抓小偷", "抓捕", "制止盗窃", "报警", "巡逻", "治安", "执法", "安保处置"]):
        return {
            "attribute": attribute,
            "semantic_type": "治安处置行为",
            "required_subject_roles": ["警务相关人员", "保安", "群众", "受害人", "治安志愿者"],
            "risk_level": "medium",
            "explanation": "该属性涉及治安或安全处置，需要主体具备合法身份、现场角色或事件场景。",
        }
    if any(token in normalized for token in ["学习", "上课", "上学", "听课", "完成作业", "考试", "科研", "课程"]):
        return {
            "attribute": attribute,
            "semantic_type": "学习行为",
            "required_subject_roles": ["学生", "学员", "研究人员"],
            "risk_level": "low",
            "explanation": "该属性属于学习或训练场景，通常适合学生、学员或研究人员。",
        }
    if any(token in normalized for token in ["写作业", "做作业", "交作业", "完成家庭作业"]):
        return {
            "attribute": attribute,
            "semantic_type": "学生作业行为",
            "required_subject_roles": ["学生", "学员"],
            "risk_level": "low",
            "explanation": "该属性属于学生完成作业的基础行为，不适合作为教师、家长等主体的稳定能力。",
        }
    if any(token in normalized for token in ["教学", "授课", "批改作业", "指导学生", "备课"]):
        return {
            "attribute": attribute,
            "semantic_type": "教学行为",
            "required_subject_roles": ["教师", "导师", "培训人员"],
            "risk_level": "low",
            "explanation": "该属性属于教学职责，通常需要教师或导师类主体。",
        }
    if any(token in normalized for token in ["榨成", "加工成", "制成", "磨成", "酿成", "由制成", "食用", "饮用", "味道", "颜色"]):
        return {
            "attribute": attribute,
            "semantic_type": "物品加工或物品属性",
            "required_subject_roles": ["食品或物品"],
            "risk_level": "low",
            "explanation": "该属性描述物品加工、来源或物品固有属性。",
        }
    if any(token in normalized for token in ["修汽车", "维修汽车", "修车", "车辆维修", "汽车维修", "更换轮胎", "发动机维修"]):
        return {
            "attribute": attribute,
            "semantic_type": "汽车维修行为",
            "required_subject_roles": ["汽车维修工", "维修技师", "汽修机构"],
            "risk_level": "medium",
            "explanation": "该属性属于专业汽车维修行为，需要维修职业或机构主体，不应挂到普通家长等生活角色上。",
        }
    if any(token in normalized for token in ["救治", "诊断", "开药", "手术", "医疗"]):
        return {
            "attribute": attribute,
            "semantic_type": "医疗行为",
            "required_subject_roles": ["医生", "护士", "医疗机构"],
            "risk_level": "high",
            "explanation": "该属性涉及医疗专业行为，需要具备医疗主体资质。",
        }
    if any(token in normalized for token in ["贷款", "放款", "授信", "审批资金", "金融"]):
        return {
            "attribute": attribute,
            "semantic_type": "金融业务行为",
            "required_subject_roles": ["银行", "金融机构", "信贷人员"],
            "risk_level": "high",
            "explanation": "该属性涉及金融业务，需要具备金融机构或授权人员角色。",
        }

    return {
        "attribute": attribute,
        "semantic_type": "通用属性",
        "required_subject_roles": [],
        "risk_level": "low",
        "explanation": "当前规则未识别出强约束语义，默认作为通用属性处理。",
    }


def _judge_attribute_compatibility(profile: dict[str, Any], semantic: dict[str, Any]) -> dict[str, Any]:
    roles = set(profile.get("roles") or [])
    semantic_type = str(semantic.get("semantic_type") or "")
    name = str(profile.get("name") or "当前本体")
    text = _attribute_text((semantic.get("attribute") or {}).get("value"))

    if semantic_type == "治安处置行为":
        if roles & {"警务相关人员", "保安"}:
            return {
                "status": "accepted",
                "is_reasonable": True,
                "confidence": 0.88,
                "reason": f"{name} 已具备警务或安保角色，新增“{text}”这类治安处置行为基本成立。",
                "suggested_categories": ["治安处置人员"],
            }
        if "学生" in roles:
            return {
                "status": "rejected_suggestion",
                "is_reasonable": False,
                "confidence": 0.86,
                "reason": f"{name} 是学生本体，“{text}”不是学生的基础能力，不能直接挂到学生本体上；若业务确实需要，应拆出更细类别。",
                "suggested_categories": ["警校学生", "见义勇为学生", "治安志愿者"],
            }
        if roles & {"群众", "未知主体"}:
            return {
                "status": "rejected_suggestion",
                "is_reasonable": False,
                "confidence": 0.78,
                "reason": f"{name} 的基础角色不足以承载“{text}”这类治安处置行为；应先拆出治安协助或见义勇为类主体。",
                "suggested_categories": ["见义勇为人员", "治安协助人员"],
            }
        return {
            "status": "rejected_suggestion",
            "is_reasonable": False,
            "confidence": 0.82,
            "reason": f"{name} 的主体角色是 {', '.join(sorted(roles)) or '未知'}，不适合直接承载“{text}”这类治安处置行为。",
            "suggested_categories": [],
        }

    if semantic_type == "物品加工或物品属性":
        if "食品或物品" in roles:
            return {
                "status": "accepted",
                "is_reasonable": True,
                "confidence": 0.86,
                "reason": f"{name} 是食品或物品类本体，新增“{text}”这类加工或物品属性基本成立。",
                "suggested_categories": ["加工食品", "物品属性本体"],
            }
        return {
            "status": "rejected_suggestion",
            "is_reasonable": False,
            "confidence": 0.78,
            "reason": f"{name} 不是物品类主体，不建议直接挂载“{text}”这类物品加工或固有属性。",
            "suggested_categories": [],
        }

    if semantic_type in {"学习行为", "学生作业行为"}:
        if "学生" in roles:
            return {
                "status": "accepted",
                "is_reasonable": True,
                "confidence": 0.9,
                "reason": f"{name} 是学生相关本体，新增“{text}”这类学习行为成立。",
                "suggested_categories": ["学生"],
            }
        return {
            "status": "rejected_suggestion",
            "is_reasonable": False,
            "confidence": 0.82,
            "reason": f"{name} 当前不是学生或学员主体，“{text}”不是其基础属性，不建议直接挂载。",
            "suggested_categories": ["学员", "培训对象"],
        }

    if semantic_type == "教学行为":
        if "教师" in roles:
            return {
                "status": "accepted",
                "is_reasonable": True,
                "confidence": 0.9,
                "reason": f"{name} 是教师相关本体，新增“{text}”这类教学行为成立。",
                "suggested_categories": ["教师"],
            }
        return {
            "status": "rejected_suggestion",
            "is_reasonable": False,
            "confidence": 0.82,
            "reason": f"{name} 当前角色不具备明确教学职责，新增“{text}”需要补充教师、助教或培训人员类别。",
            "suggested_categories": ["助教", "培训人员"],
        }

    if semantic_type == "汽车维修行为":
        if roles & {"汽车维修工", "维修技师", "汽修机构"}:
            return {
                "status": "accepted",
                "is_reasonable": True,
                "confidence": 0.9,
                "reason": f"{name} 是维修相关主体，新增“{text}”这类汽车维修行为成立。",
                "suggested_categories": ["汽车维修工"],
            }
        return {
            "status": "rejected_suggestion",
            "is_reasonable": False,
            "confidence": 0.88,
            "reason": f"{name} 的基础角色不具备汽车维修职责，“{text}”不是其基础属性，不建议直接挂载。",
            "suggested_categories": ["汽车维修工", "维修技师", "汽修机构"],
        }

    if semantic_type in {"医疗行为", "金融业务行为"}:
        return {
            "status": "conditional",
            "is_reasonable": False,
            "confidence": 0.74,
            "reason": f"{semantic_type} 是强资质行为，{name} 需要补充明确授权或专业身份后才建议成立。",
            "suggested_categories": semantic.get("required_subject_roles", []),
        }

    return {
        "status": "rejected_suggestion",
        "is_reasonable": False,
        "confidence": 0.55,
        "reason": f"“{text}”未被识别为 {name} 的基础属性；在强判别模式下不建议直接挂载，需先补充更明确的主体类别或规则。",
        "suggested_categories": [],
    }


def _build_semantic_patch(compatibility: dict[str, Any], semantic: dict[str, Any]) -> dict[str, Any]:
    categories = compatibility.get("suggested_categories") or []
    if not categories or not compatibility.get("is_reasonable"):
        return {}
    category = str(categories[0])
    semantic_type = str(semantic.get("semantic_type") or "")
    patch: dict[str, Any] = {"category": category}
    if semantic_type and semantic_type != "通用属性":
        patch["interactions"] = [{"target": semantic_type, "type": "参与"}]
    return patch


def review_ontology_attribute(
    project_id: str,
    filename: str | None = None,
    ontology_name: str | None = None,
    current_ontology: dict[str, Any] | str | None = None,
    proposed_ontology: dict[str, Any] | str | None = None,
    added_attributes: Any = None,
    client: GatewayClient | None = None,
) -> dict[str, Any]:
    if not str(project_id).strip():
        raise ValueError("project_id is required")

    gateway_client = client or GatewayClient()
    normalized_project_id = str(project_id).strip()
    current_payload = _coerce_json_object(current_ontology, "current_ontology")
    proposed_payload = _coerce_json_object(proposed_ontology, "proposed_ontology")
    resolution: dict[str, Any] = {"mode": "inline_current_ontology" if current_payload else "unresolved"}
    resolved_filename = _as_text(filename)

    if not current_payload:
        resolved_filename, resolution = resolve_ontology_filename(
            project_id=normalized_project_id,
            filename=filename,
            ontology_name=ontology_name,
            client=gateway_client,
        )
        content = get_version_content(
            project_id=normalized_project_id,
            filename=resolved_filename,
            client=gateway_client,
        )
        current_payload = content["content"]

    attributes = _normalize_added_attributes(added_attributes)
    if not attributes and proposed_payload:
        attributes = _diff_added_attributes(current_payload, proposed_payload)

    profile = _ontology_profile(current_payload)
    reviews: list[dict[str, Any]] = []
    for attribute in attributes:
        semantic = _analyze_attribute_semantics(attribute)
        compatibility = _judge_attribute_compatibility(profile, semantic)
        reviews.append({
            "attribute": attribute,
            "semantic": {
                "semantic_type": semantic["semantic_type"],
                "required_subject_roles": semantic["required_subject_roles"],
                "risk_level": semantic["risk_level"],
                "explanation": semantic["explanation"],
            },
            "compatibility": compatibility,
            "suggested_patch": _build_semantic_patch(compatibility, semantic),
        })

    status_order = {"rejected_suggestion": 3, "conditional": 2, "accepted": 1}
    overall_status = "no_new_attribute"
    if reviews:
        overall_status = max(
            (str(item["compatibility"].get("status") or "accepted") for item in reviews),
            key=lambda value: status_order.get(value, 0),
        )

    return {
        "tool_name": REVIEW_ONTOLOGY_ATTRIBUTE_TOOL.name,
        "project_id": normalized_project_id,
        "filename": resolved_filename,
        "ontology_name": profile.get("name") or _as_text(ontology_name),
        "target_resolution": resolution,
        "subject_profile": profile,
        "summary": {
            "added_attribute_count": len(attributes),
            "accepted_count": len([item for item in reviews if item["compatibility"].get("status") == "accepted"]),
            "conditional_count": len([item for item in reviews if item["compatibility"].get("status") == "conditional"]),
            "rejected_suggestion_count": len([item for item in reviews if item["compatibility"].get("status") == "rejected_suggestion"]),
            "overall_status": overall_status,
        },
        "reviews": reviews,
    }


def _current_ontology_attributes(ontology: dict[str, Any]) -> list[dict[str, Any]]:
    attributes: list[dict[str, Any]] = []
    for ability in ontology.get("abilities") or []:
        text = _attribute_text(ability)
        if text:
            attributes.append({
                "field": "abilities",
                "value": ability,
                "text": text,
            })
    for interaction in ontology.get("interactions") or []:
        text = _attribute_text(interaction)
        if text:
            attributes.append({
                "field": "interactions",
                "value": interaction,
                "text": text,
            })
    return attributes


def review_ontology_consistency(
    project_id: str,
    filename: str | None = None,
    ontology_name: str | None = None,
    current_ontology: dict[str, Any] | str | None = None,
    client: GatewayClient | None = None,
) -> dict[str, Any]:
    if not str(project_id).strip():
        raise ValueError("project_id is required")

    gateway_client = client or GatewayClient()
    normalized_project_id = str(project_id).strip()
    current_payload = _coerce_json_object(current_ontology, "current_ontology")
    resolution: dict[str, Any] = {"mode": "inline_current_ontology" if current_payload else "unresolved"}
    resolved_filename = _as_text(filename)

    if not current_payload:
        resolved_filename, resolution = resolve_ontology_filename(
            project_id=normalized_project_id,
            filename=filename,
            ontology_name=ontology_name,
            client=gateway_client,
        )
        content = get_version_content(
            project_id=normalized_project_id,
            filename=resolved_filename,
            client=gateway_client,
        )
        current_payload = content["content"]

    profile = _ontology_profile(current_payload)
    attributes = _current_ontology_attributes(current_payload)
    reviews: list[dict[str, Any]] = []
    suggested_category_set: set[str] = set()

    for attribute in attributes:
        semantic = _analyze_attribute_semantics(attribute)
        compatibility = _judge_attribute_compatibility(profile, semantic)
        for category in compatibility.get("suggested_categories") or []:
            if category:
                suggested_category_set.add(str(category))
        reviews.append({
            "attribute": attribute,
            "semantic": {
                "semantic_type": semantic["semantic_type"],
                "required_subject_roles": semantic["required_subject_roles"],
                "risk_level": semantic["risk_level"],
                "explanation": semantic["explanation"],
            },
            "compatibility": compatibility,
            "suggested_patch": _build_semantic_patch(compatibility, semantic),
        })

    status_order = {"rejected_suggestion": 3, "conditional": 2, "accepted": 1}
    overall_status = "no_reviewable_attribute"
    if reviews:
        overall_status = max(
            (str(item["compatibility"].get("status") or "accepted") for item in reviews),
            key=lambda value: status_order.get(value, 0),
        )

    grouped = {
        "accepted": [item for item in reviews if item["compatibility"].get("status") == "accepted"],
        "conditional": [item for item in reviews if item["compatibility"].get("status") == "conditional"],
        "rejected_suggestion": [item for item in reviews if item["compatibility"].get("status") == "rejected_suggestion"],
    }

    return {
        "tool_name": REVIEW_ONTOLOGY_CONSISTENCY_TOOL.name,
        "project_id": normalized_project_id,
        "filename": resolved_filename,
        "ontology_name": profile.get("name") or _as_text(ontology_name),
        "target_resolution": resolution,
        "subject_profile": profile,
        "summary": {
            "reviewed_attribute_count": len(attributes),
            "accepted_count": len(grouped["accepted"]),
            "conditional_count": len(grouped["conditional"]),
            "rejected_suggestion_count": len(grouped["rejected_suggestion"]),
            "overall_status": overall_status,
            "suggested_categories": sorted(suggested_category_set),
        },
        "reviews": reviews,
        "grouped_reviews": grouped,
    }


def _grandparent_relation(parent_relation: str, child_parent_relation: str) -> tuple[str, str]:
    if parent_relation == "father" and child_parent_relation == "father":
        return "爷爷", "father_father_grandfather"
    if parent_relation == "father" and child_parent_relation == "mother":
        return "外公", "father_mother_maternal_grandfather"
    if parent_relation == "mother" and child_parent_relation == "father":
        return "奶奶", "mother_father_grandmother"
    if parent_relation == "mother" and child_parent_relation == "mother":
        return "外婆", "mother_mother_maternal_grandmother"
    if parent_relation == "father" and child_parent_relation == "parent":
        return "祖父或外祖父", "father_parent_grandfather"
    if parent_relation == "mother" and child_parent_relation == "parent":
        return "祖母或外祖母", "mother_parent_grandmother"
    if parent_relation == "parent" and child_parent_relation in {"father", "mother", "parent"}:
        return "祖辈", "parent_parent_grandparent"
    return "", ""


def _append_inferred_relation(
    inferred: list[dict[str, Any]],
    seen: set[tuple[str, str, str]],
    existing: set[tuple[str, str, str]],
    subject: str,
    relation_type: str,
    target: str,
    rule_id: str,
    rule: str,
    evidence: list[dict[str, Any]],
) -> None:
    key = (
        _normalize_logic_value(subject),
        _normalize_family_relation(relation_type),
        _normalize_logic_value(target),
    )
    display_key = (subject, relation_type, target)
    if key in existing or display_key in seen:
        return
    seen.add(display_key)
    inferred.append({
        "subject": subject,
        "type": relation_type,
        "target": target,
        "rule_id": rule_id,
        "rule": rule,
        "evidence": evidence,
    })


def _infer_transitive_relation(
    first: dict[str, Any],
    second: dict[str, Any],
) -> tuple[str, str]:
    if first["normalized_target"] != second["normalized_subject"]:
        return "", ""
    first_rule = TRANSITIVE_RELATION_INDEX.get(str(first.get("normalized_type", "")))
    second_rule = TRANSITIVE_RELATION_INDEX.get(str(second.get("normalized_type", "")))
    if not first_rule or not second_rule:
        return "", ""
    if first_rule["rule_id"] != second_rule["rule_id"]:
        return "", ""
    return first_rule["inferred_type"], first_rule["rule_id"]


def _infer_family_relations(triples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing = {
        (
            item["normalized_subject"],
            _normalize_family_relation(item["type"]),
            item["normalized_target"],
        )
        for item in triples
    }
    inferred: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for first in triples:
        for second in triples:
            if first is second:
                continue
            if first["normalized_target"] != second["normalized_subject"]:
                continue

            relation_type, rule_id = _grandparent_relation(
                str(first.get("normalized_type")),
                str(second.get("normalized_type")),
            )
            if relation_type:
                _append_inferred_relation(
                    inferred=inferred,
                    seen=seen,
                    existing=existing,
                    subject=first["subject"],
                    relation_type=relation_type,
                    target=second["target"],
                    rule_id=rule_id,
                    rule=f"{first['subject']} 是 {first['target']} 的{first['type']}，"
                         f"{second['subject']} 是 {second['target']} 的{second['type']}，"
                         f"因此 {first['subject']} 是 {second['target']} 的{relation_type}",
                    evidence=[first, second],
                )

            relation_type, rule_id = _infer_transitive_relation(first, second)
            if relation_type:
                _append_inferred_relation(
                    inferred=inferred,
                    seen=seen,
                    existing=existing,
                    subject=first["subject"],
                    relation_type=relation_type,
                    target=second["target"],
                    rule_id=rule_id,
                    rule=f"{first['subject']} {first['type']} {first['target']}，"
                         f"{second['subject']} {second['type']} {second['target']}，"
                         f"因此 {first['subject']} {relation_type} {second['target']}",
                    evidence=[first, second],
                )
    return inferred


def _infer_property_transfer_relations(triples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing = {
        (
            item["normalized_subject"],
            _normalize_family_relation(item["type"]),
            item["normalized_target"],
        )
        for item in triples
    }
    inferred: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    property_triples = [
        item for item in triples
        if str(item.get("normalized_type", "")) in PROPERTY_RELATION_TYPES
    ]
    if not property_triples:
        return inferred

    for property_item in property_triples:
        source_name = property_item["subject"]
        property_type = property_item["type"]
        property_value = property_item["target"]
        normalized_source = property_item["normalized_subject"]

        for relation_item in triples:
            relation_type = str(relation_item.get("normalized_type", ""))
            product_name = ""
            if (
                relation_type in MADE_INTO_RELATION_TYPES
                and relation_item["normalized_subject"] == normalized_source
            ):
                product_name = relation_item["target"]
            elif (
                relation_type in MADE_FROM_RELATION_TYPES
                and relation_item["normalized_target"] == normalized_source
            ):
                product_name = relation_item["subject"]

            if not product_name:
                continue

            _append_inferred_relation(
                inferred=inferred,
                seen=seen,
                existing=existing,
                subject=product_name,
                relation_type=property_type,
                target=property_value,
                rule_id="property_transfer_to_processed_item",
                rule=(
                    f"{source_name} 的{property_type}是 {property_value}，"
                    f"{relation_item['subject']} {relation_item['type']} {relation_item['target']}，"
                    f"因此 {product_name} 的{property_type}也可推导为 {property_value}"
                ),
                evidence=[property_item, relation_item],
            )

    return inferred


def _relation_display(item: dict[str, Any]) -> str:
    return f"{item.get('subject')} {item.get('type')} {item.get('target')}"


def _logic_issue(
    code: str,
    severity: str,
    title: str,
    detail: str,
    suggestion: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "detail": detail,
        "suggestion": suggestion,
        "evidence": evidence,
    }


def _infer_relation_category(item: dict[str, Any]) -> str:
    relation_type = str(item.get("normalized_type", ""))
    if relation_type in FAMILY_NORMALIZED_RELATIONS:
        return f"family:{relation_type}"
    if relation_type in PROPERTY_RELATION_TYPES:
        return f"property:{relation_type}"
    if relation_type in MADE_INTO_RELATION_TYPES:
        return "made_into"
    if relation_type in MADE_FROM_RELATION_TYPES:
        return "made_from"
    transitive_rule = TRANSITIVE_RELATION_INDEX.get(relation_type)
    if transitive_rule:
        return str(transitive_rule.get("rule_id") or "")
    return relation_type


def _find_logic_issues(
    triples: list[dict[str, Any]],
    inferred_relations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    seen_issue_keys: set[tuple[Any, ...]] = set()

    def add_issue(issue: dict[str, Any], key: tuple[Any, ...]) -> None:
        if key in seen_issue_keys:
            return
        seen_issue_keys.add(key)
        issues.append(issue)

    relation_by_pair: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    property_by_subject_type: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for item in triples:
        subject = str(item.get("normalized_subject", ""))
        target = str(item.get("normalized_target", ""))
        relation_type = str(item.get("normalized_type", ""))
        if not subject or not target or not relation_type:
            continue

        if subject == target:
            add_issue(
                _logic_issue(
                    code="self_relation",
                    severity="medium",
                    title="关系自指",
                    detail=f"发现本体关系指向自身：{_relation_display(item)}。",
                    suggestion="检查该关系是否录入错误；除非业务明确允许自指，否则建议删除或改为正确目标。",
                    evidence=[item],
                ),
                ("self_relation", subject, relation_type, target),
            )

        category = _infer_relation_category(item)
        relation_by_pair.setdefault((subject, target, category), []).append(item)
        if relation_type in SINGLE_VALUE_PROPERTY_TYPES:
            property_by_subject_type.setdefault((subject, relation_type), []).append(item)

    for (subject, property_type), items in property_by_subject_type.items():
        values = {str(item.get("normalized_target", "")) for item in items if item.get("normalized_target")}
        if len(values) > 1:
            add_issue(
                _logic_issue(
                    code="conflicting_single_value_property",
                    severity="high",
                    title="单值属性冲突",
                    detail=f"{items[0].get('subject')} 的 {items[0].get('type')} 出现多个不同取值。",
                    suggestion="颜色、味道、状态这类单值属性应保留一个当前有效值，或改造成多维属性字段。",
                    evidence=items,
                ),
                ("conflicting_single_value_property", subject, property_type),
            )

    for (subject, target, category), forward_items in relation_by_pair.items():
        if subject == target:
            continue
        reverse_items = relation_by_pair.get((target, subject, category), [])
        if not reverse_items:
            continue
        if category.startswith("family:") or category in SUSPICIOUS_BIDIRECTIONAL_RULE_IDS:
            evidence = forward_items[:1] + reverse_items[:1]
            add_issue(
                _logic_issue(
                    code="suspicious_bidirectional_relation",
                    severity="high" if category.startswith("family:") else "medium",
                    title="可疑双向关系",
                    detail=f"同一类方向性关系同时双向存在：{_relation_display(evidence[0])}；{_relation_display(evidence[1])}。",
                    suggestion="检查是否把主语和宾语写反，或是否应该使用另一种反向关系表达。",
                    evidence=evidence,
                ),
                ("suspicious_bidirectional_relation", min(subject, target), max(subject, target), category),
            )

    explicit_properties = {
        (
            str(item.get("normalized_subject", "")),
            str(item.get("normalized_type", "")),
        ): item
        for item in triples
        if str(item.get("normalized_type", "")) in SINGLE_VALUE_PROPERTY_TYPES
    }
    for inferred in inferred_relations:
        relation_type = _normalize_logic_value(inferred.get("type"))
        if relation_type not in SINGLE_VALUE_PROPERTY_TYPES:
            continue
        subject = _normalize_logic_value(inferred.get("subject"))
        target = _normalize_logic_value(inferred.get("target"))
        explicit = explicit_properties.get((subject, relation_type))
        if explicit and str(explicit.get("normalized_target", "")) != target:
            add_issue(
                _logic_issue(
                    code="inferred_property_conflicts_with_explicit_property",
                    severity="high",
                    title="推导属性与显式属性冲突",
                    detail=(
                        f"规则推导出 {inferred.get('subject')} 的{inferred.get('type')}为 {inferred.get('target')}，"
                        f"但当前数据中显式记录为 {explicit.get('target')}。"
                    ),
                    suggestion="检查原料属性、制成关系或成品显式属性是否存在录入错误；若加工会改变属性，应补充例外规则。",
                    evidence=[inferred, explicit],
                ),
                ("inferred_property_conflict", subject, relation_type),
            )

    severity_order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda item: (severity_order.get(str(item.get("severity")), 9), item.get("code", "")))
    return issues


def _inferred_properties_for_new_ontology(
    inferred_relations: list[dict[str, Any]],
    new_ontology_name: str,
) -> list[dict[str, Any]]:
    if not _as_text(new_ontology_name):
        return []
    normalized_new_name = _normalize_logic_value(new_ontology_name)
    properties: list[dict[str, Any]] = []
    for relation in inferred_relations:
        subject = _as_text(relation.get("subject"))
        target = _as_text(relation.get("target"))
        relation_type = _as_text(relation.get("type"))
        if _normalize_logic_value(subject) == normalized_new_name:
            properties.append({
                "field": "interactions",
                "value": {"target": target, "type": relation_type},
                "direction": "outgoing",
                "reason": relation.get("rule"),
                "evidence": relation.get("evidence", []),
            })
        if _normalize_logic_value(target) == normalized_new_name:
            properties.append({
                "field": relation_type,
                "value": subject,
                "direction": "incoming",
                "reason": relation.get("rule"),
                "evidence": relation.get("evidence", []),
            })
    return properties


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


def infer_causal_logic(
    project_id: str,
    new_ontology: dict[str, Any] | str | None = None,
    filename: str | None = None,
    ontology_name: str | None = None,
    client: GatewayClient | None = None,
) -> dict[str, Any]:
    if not str(project_id).strip():
        raise ValueError("project_id is required")

    gateway_client = client or GatewayClient()
    normalized_project_id = str(project_id).strip()
    payload = _coerce_ontology_payload(new_ontology)

    resolution: dict[str, Any] = {"mode": "project"}
    if not payload:
        if _as_text(filename) or _as_text(ontology_name):
            try:
                resolved_filename, resolution = resolve_ontology_filename(
                    project_id=normalized_project_id,
                    filename=filename,
                    ontology_name=ontology_name,
                    client=gateway_client,
                )
                content = get_version_content(
                    project_id=normalized_project_id,
                    filename=resolved_filename,
                    client=gateway_client,
                )
                payload = content["content"]
            except Exception as exc:
                fallback_name = _as_text(ontology_name)
                if not fallback_name:
                    raise
                payload = {
                    "name": fallback_name,
                    "interactions": [],
                }
                resolution = {
                    "mode": "new_ontology_name_fallback",
                    "input": fallback_name,
                    "detail": "ontology was not found in the current project; using the supplied ontology_name as a new ontology payload",
                    "resolve_error": str(exc),
                }
    else:
        resolution = {"mode": "inline_new_ontology"}

    new_ontology_name = _ontology_display_name(payload) if payload else ""

    current_ontologies = _read_project_current_ontologies(normalized_project_id, gateway_client)
    normalized_new_name = _normalize_logic_value(new_ontology_name)
    merged_ontologies: list[dict[str, Any]] = []
    replaced_existing = False
    read_errors: list[dict[str, str]] = []

    for item in current_ontologies:
        data = item.get("data")
        filename_value = _as_text(item.get("filename"))
        read_error = _as_text(item.get("read_error"))
        if read_error:
            read_errors.append({"filename": filename_value, "error": read_error})
            continue
        if not isinstance(data, dict):
            continue
        if normalized_new_name and _normalize_logic_value(_ontology_display_name(data, filename_value)) == normalized_new_name:
            merged_ontologies.append({"filename": filename_value or "__new_ontology__", "data": payload})
            replaced_existing = True
        else:
            merged_ontologies.append({"filename": filename_value, "data": data})

    if payload and not replaced_existing:
        merged_ontologies.append({"filename": "__new_ontology__", "data": payload})

    triples: list[dict[str, Any]] = []
    ontology_summaries: list[dict[str, Any]] = []
    for item in merged_ontologies:
        data = item.get("data") if isinstance(item, dict) else {}
        filename_value = _as_text(item.get("filename")) if isinstance(item, dict) else ""
        if not isinstance(data, dict):
            continue
        ontology_triples = _extract_relation_triples(data, filename_value)
        triples.extend(ontology_triples)
        ontology_summaries.append({
            "filename": filename_value,
            "name": _ontology_display_name(data, filename_value),
            "relation_count": len(ontology_triples),
            "is_input_ontology": bool(normalized_new_name) and _normalize_logic_value(_ontology_display_name(data, filename_value)) == normalized_new_name,
        })

    inferred_relations = _infer_family_relations(triples)
    inferred_relations.extend(_infer_property_transfer_relations(triples))
    inverse_inferred_relations = [
        {
            "subject": item["subject"],
            "type": "父母",
            "target": item["target"],
            "rule_id": "inverse_child_parent",
            "rule": (
                f"{item['derived_from_inverse']['subject']} 是 "
                f"{item['derived_from_inverse']['target']} 的{item['derived_from_inverse']['type']}，"
                f"因此 {item['subject']} 是 {item['target']} 的父母"
            ),
            "evidence": [item["derived_from_inverse"]],
        }
        for item in triples
        if isinstance(item, dict) and isinstance(item.get("derived_from_inverse"), dict)
    ]
    inferred_relations = inverse_inferred_relations + inferred_relations
    logic_issues = _find_logic_issues(triples, inferred_relations)
    inferred_properties = _inferred_properties_for_new_ontology(
        inferred_relations=inferred_relations,
        new_ontology_name=new_ontology_name,
    )

    return {
        "tool_name": INFER_CAUSAL_LOGIC_TOOL.name,
        "project_id": normalized_project_id,
        "target_resolution": resolution,
        "new_ontology_name": new_ontology_name,
        "input_replaced_existing_ontology": replaced_existing,
        "summary": {
            "ontology_count": len(ontology_summaries),
            "base_relation_count": len(triples),
            "inferred_relation_count": len(inferred_relations),
            "inferred_property_count_for_new_ontology": len(inferred_properties),
            "logic_issue_count": len(logic_issues),
            "high_logic_issue_count": len([item for item in logic_issues if item.get("severity") == "high"]),
            "read_error_count": len(read_errors),
        },
        "inferred_properties": inferred_properties,
        "inferred_relations": inferred_relations,
        "logic_issues": logic_issues,
        "supported_rules": [
            "父亲 + 父亲 => 爷爷",
            "父亲 + 母亲 => 外公",
            "母亲 + 父亲 => 奶奶",
            "母亲 + 母亲 => 外婆",
            "自指关系 => 逻辑问题",
            "方向性关系双向互指 => 逻辑问题",
            "颜色/味道/状态多个取值 => 逻辑问题",
            "原料属性推导与成品显式属性冲突 => 逻辑问题",
            "导致/造成/引起 + 导致/造成/引起 => 间接导致",
            "影响/作用于 + 影响/作用于 => 间接影响",
            "依赖/需要 + 依赖/需要 => 间接依赖",
            "包含/包括/下辖 + 包含/包括/下辖 => 包含",
            "属于/隶属于 + 属于/隶属于 => 属于",
            "管理/管辖/负责 + 管理/管辖/负责 => 间接管理",
            "父类/上位概念 + 父类/上位概念 => 上级",
            "子类/下位概念 + 子类/下位概念 => 子类",
            "位于/在 + 位于/在 => 位于",
            "早于/先于 + 早于/先于 => 早于",
            "晚于/后于 + 晚于/后于 => 晚于",
            "原料属性 + 制成/由制成关系 => 成品继承该属性",
        ],
        "base_relations": triples,
        "ontologies": ontology_summaries,
        "read_errors": read_errors,
    }


def get_available_tools() -> list[ToolDefinition]:
    return [
        COMMUNITY_TOP_VERSION_TOOL,
        OFFICIAL_RECOMMENDATION_TOOL,
        FILE_TIMELINE_TOOL,
        VERSION_CONTENT_TOOL,
        COMPARE_VERSIONS_TOOL,
        FIND_GOVERNANCE_GAPS_TOOL,
        INFER_CAUSAL_LOGIC_TOOL,
        REVIEW_ONTOLOGY_ATTRIBUTE_TOOL,
        REVIEW_ONTOLOGY_CONSISTENCY_TOOL,
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
    if name == INFER_CAUSAL_LOGIC_TOOL.name:
        return infer_causal_logic(
            project_id=str(arguments.get("project_id", "")),
            new_ontology=arguments.get("new_ontology"),
            filename=str(arguments.get("filename", "") or ""),
            ontology_name=str(arguments.get("ontology_name", "") or ""),
            client=client,
        )
    if name == REVIEW_ONTOLOGY_ATTRIBUTE_TOOL.name:
        return review_ontology_attribute(
            project_id=str(arguments.get("project_id", "")),
            filename=str(arguments.get("filename", "") or ""),
            ontology_name=str(arguments.get("ontology_name", "") or ""),
            current_ontology=arguments.get("current_ontology"),
            proposed_ontology=arguments.get("proposed_ontology"),
            added_attributes=arguments.get("added_attributes"),
            client=client,
        )
    if name == REVIEW_ONTOLOGY_CONSISTENCY_TOOL.name:
        return review_ontology_consistency(
            project_id=str(arguments.get("project_id", "")),
            filename=str(arguments.get("filename", "") or ""),
            ontology_name=str(arguments.get("ontology_name", "") or ""),
            current_ontology=arguments.get("current_ontology"),
            client=client,
        )
    raise ValueError(f"unsupported tool: {name}")
