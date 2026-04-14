from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from openai import OpenAI


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("warehouse.agent")

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
    agent_base_env = _read_env_file(BASE_DIR / ".env")
    xg_base_env = _read_env_file(ROOT_DIR / "xiaogugit" / ".env")
    initial_mode = _normalize_env(os.environ.get("XG_ENV") or agent_base_env.get("XG_ENV") or xg_base_env.get("XG_ENV"))
    agent_mode_env = _read_env_file(BASE_DIR / f".env.{initial_mode}")
    xg_mode_env = _read_env_file(ROOT_DIR / "xiaogugit" / f".env.{initial_mode}")

    merged: dict[str, str] = {}
    merged.update(xg_base_env)
    merged.update(xg_mode_env)
    merged.update(agent_base_env)
    merged.update(agent_mode_env)
    merged.update(os.environ)
    return merged


def build_client() -> OpenAI:
    values = _load_env_values()
    api_key = values.get("DMXAPI_API_KEY", "").strip()
    base_url = values.get("DMXAPI_BASE_URL", "https://www.dmxapi.cn/v1").strip()

    if not api_key:
        raise RuntimeError("DMXAPI_API_KEY is not configured. Please set it in xiaogugit/.env.")

    return OpenAI(api_key=api_key, base_url=base_url)


def get_default_model() -> str:
    return _load_env_values().get("DMXAPI_MODEL", "gpt-5.4").strip() or "gpt-5.4"


def get_warehouse_agent_prompt() -> str:
    return _load_env_values().get(
        "DMXAPI_SYSTEM_PROMPT_WAREHOUSE_AGENT",
        (
            "你是一个专业的数据仓库治理 Agent。"
            "你的任务是阅读用户提供的 JSON 业务上下文，输出结构化治理分析，帮助数据中台判断当前本体或元数据变更的风险和后续动作。"
            "你必须严格遵守以下规则："
            "1. 只能输出一个 JSON 对象，禁止输出 Markdown、代码块或额外说明。"
            '2. 输出结构必须严格为 {"summary":"中文摘要","risks":["风险1"],"suggested_actions":["动作1"],"affected_objects":["对象1"],"decision":"observe|review|recommend_official|recompute"}。'
            "3. summary 必须是简洁中文。"
            "4. risks、suggested_actions、affected_objects 必须是 JSON 数组，即使为空也必须返回空数组。"
            "5. decision 只能从 observe、review、recommend_official、recompute 中选择一个。"
            "6. 请优先从本体定义变化、概率风险、社区与官方分歧、影响范围这四个角度进行判断。"
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


def extract_text_from_chat_completion(response: Any) -> str:
    try:
        return response.choices[0].message.content or ""
    except (AttributeError, IndexError, TypeError):
        return ""


def call_llm(client: OpenAI, model: str, message: str, system_prompt: str | None) -> tuple[str, dict[str, Any]]:
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


def _normalize_analysis(text: str) -> dict[str, Any]:
    fallback = {
        "summary": text.strip() or "未获得有效分析结果",
        "risks": [],
        "suggested_actions": [],
        "affected_objects": [],
        "decision": "observe",
    }

    if not text.strip():
        return fallback

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return fallback

    if not isinstance(parsed, dict):
        return fallback

    decision = str(parsed.get("decision", "observe")).strip() or "observe"
    if decision not in {"observe", "review", "recommend_official", "recompute"}:
        decision = "observe"

    def _ensure_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if str(item).strip()]

    return {
        "summary": str(parsed.get("summary", "")).strip() or fallback["summary"],
        "risks": _ensure_list(parsed.get("risks")),
        "suggested_actions": _ensure_list(parsed.get("suggested_actions")),
        "affected_objects": _ensure_list(parsed.get("affected_objects")),
        "decision": decision,
    }


class DataWarehouseAgent:
    def __init__(self, model: str | None = None):
        self.model = (model or get_default_model()).strip() or get_default_model()

    def analyze(self, payload: dict[str, Any], include_raw: bool = False) -> dict[str, Any]:
        if not isinstance(payload, dict) or not payload:
            raise ValueError("payload must be a non-empty JSON object")

        message = json.dumps(payload, ensure_ascii=False)
        client = build_client()
        logger.info("Running warehouse agent analysis with model=%s", self.model)
        output_text, raw_response = call_llm(
            client=client,
            model=self.model,
            message=message,
            system_prompt=get_warehouse_agent_prompt(),
        )
        analysis = _normalize_analysis(output_text)
        result = {
            "model": self.model,
            "status": "success",
            "analysis": analysis,
            "text": output_text,
        }
        if include_raw:
            result["raw"] = raw_response
        return result
