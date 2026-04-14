from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from openai import OpenAI

from git_query_tools import GatewayClient, get_available_tools, run_tool


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("git-query.agent")

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
        raise RuntimeError("DMXAPI_API_KEY is not configured. Please set it in agent/.env.")

    return OpenAI(api_key=api_key, base_url=base_url)


def get_default_model() -> str:
    return _load_env_values().get("DMXAPI_MODEL", "gpt-5.4").strip() or "gpt-5.4"


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


def get_planner_prompt() -> str:
    tools = [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
        for tool in get_available_tools()
    ]
    return (
        "你是一个 Git 查询 Agent 的规划器。"
        "你的任务是根据用户问题，从可用 tools 中选择最合适的一个，并补出 JSON 参数。"
        "查询对象优先是本体对象，不是文件名。"
        "如果用户说的是学校、student、teacher、school 这类对象，应优先提取到 ontology_name。"
        "只有当用户明确提到了具体文件名，例如 student.json，才使用 filename。"
        "默认上下文里的 filename 只能作为弱提示，不能覆盖用户问题里提到的本体对象。"
        "中英文对象名视为同一个查询目标，例如 school 和 学校。"
        "你只能输出一个 JSON 对象，不能输出 Markdown、解释或代码块。"
        '输出结构必须严格为 {"tool_name":"...","arguments":{...},"reason":"..."}。'
        '如果用户问题无法由当前 tools 回答，输出 {"tool_name":"none","arguments":{},"reason":"..."}。'
        "如果用户没有明确给出 project_id，可以使用上下文中的默认值。"
        "可用 tools 如下："
        + json.dumps(tools, ensure_ascii=False)
    )


def get_answer_prompt() -> str:
    return (
        "你是一个 Git 查询问答 Agent。"
        "你会收到用户问题和 tool 执行结果。"
        "请基于 tool 结果用简洁中文回答，不要编造结果中没有出现的信息。"
        "如果 tool_result 里有 ontology_name，就优先用本体名来回答，而不是只说文件名。"
        "优先给出结论，再补版本号、星标、时间等关键事实。"
        "你只能输出纯文本答案，不要输出 Markdown 代码块。"
    )


def _normalize_plan(text: str) -> dict[str, Any]:
    fallback = {"tool_name": "none", "arguments": {}, "reason": text.strip() or "planner returned empty result"}
    if not text.strip():
        return fallback

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return fallback

    if not isinstance(parsed, dict):
        return fallback

    tool_name = str(parsed.get("tool_name", "none")).strip() or "none"
    arguments = parsed.get("arguments", {})
    if not isinstance(arguments, dict):
        arguments = {}
    reason = str(parsed.get("reason", "")).strip() or fallback["reason"]
    return {
        "tool_name": tool_name,
        "arguments": arguments,
        "reason": reason,
    }


def _build_planner_message(question: str, project_id: str | None, filename: str | None) -> str:
    payload = {
        "question": question,
        "context": {
            "default_project_id": project_id,
            "default_filename": filename,
        },
    }
    return json.dumps(payload, ensure_ascii=False)


class GitQueryAgent:
    def __init__(
        self,
        model: str | None = None,
        gateway_client: GatewayClient | None = None,
    ) -> None:
        self.model = (model or get_default_model()).strip() or get_default_model()
        self.gateway_client = gateway_client or GatewayClient()

    def plan(
        self,
        question: str,
        project_id: str | None = None,
        filename: str | None = None,
        include_raw: bool = False,
    ) -> dict[str, Any]:
        if not question.strip():
            raise ValueError("question must be a non-empty string")

        client = build_client()
        planner_message = _build_planner_message(question, project_id, filename)
        logger.info("Planning git query tool with model=%s", self.model)
        output_text, raw_response = call_llm(
            client=client,
            model=self.model,
            message=planner_message,
            system_prompt=get_planner_prompt(),
        )
        plan = _normalize_plan(output_text)
        result = {
            "model": self.model,
            "status": "success",
            "plan": plan,
            "text": output_text,
        }
        if include_raw:
            result["raw"] = raw_response
        return result

    def answer(
        self,
        question: str,
        project_id: str | None = None,
        filename: str | None = None,
        include_raw: bool = False,
    ) -> dict[str, Any]:
        plan_result = self.plan(
            question=question,
            project_id=project_id,
            filename=filename,
            include_raw=include_raw,
        )
        plan = dict(plan_result["plan"])

        if project_id and not plan["arguments"].get("project_id"):
            plan["arguments"]["project_id"] = project_id
        if filename and not plan["arguments"].get("filename") and not plan["arguments"].get("ontology_name"):
            plan["arguments"]["filename"] = filename

        if plan["tool_name"] == "none":
            return {
                "model": self.model,
                "status": "unsupported",
                "question": question,
                "plan": plan,
                "tool_result": None,
                "answer": f"当前工具集还不能直接回答这个问题。原因：{plan['reason']}",
                "planner_text": plan_result["text"],
            }

        tool_result = run_tool(
            name=plan["tool_name"],
            arguments=plan["arguments"],
            client=self.gateway_client,
        )

        answer_payload = {
            "question": question,
            "plan": plan,
            "tool_result": tool_result,
        }
        client = build_client()
        logger.info("Generating final git query answer with model=%s", self.model)
        answer_text, raw_response = call_llm(
            client=client,
            model=self.model,
            message=json.dumps(answer_payload, ensure_ascii=False),
            system_prompt=get_answer_prompt(),
        )

        result = {
            "model": self.model,
            "status": "success",
            "question": question,
            "plan": plan,
            "tool_result": tool_result,
            "answer": answer_text.strip(),
            "planner_text": plan_result["text"],
        }
        if include_raw:
            result["raw"] = {
                "planner": plan_result.get("raw"),
                "answer": raw_response,
            }
        return result
