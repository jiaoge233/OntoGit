import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

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

load_dotenv()


def build_client() -> OpenAI:
    api_key = os.getenv("DMXAPI_API_KEY")
    base_url = os.getenv("DMXAPI_BASE_URL", "https://www.dmxapi.cn/v1")

    if not api_key:
        raise RuntimeError("DMXAPI_API_KEY is not configured. Please set it in .env.")

    return OpenAI(api_key=api_key, base_url=base_url)


def get_default_model() -> str:
    return os.getenv("DMXAPI_MODEL", "gpt-5.4")


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

    business_payload = {key: value for key, value in payload.items() if key != "include_raw"}
    if not business_payload:
        raise HTTPException(status_code=422, detail="request body must contain JSON fields")

    return json.dumps(business_payload, ensure_ascii=False), include_raw


def call_llm(
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
        raise HTTPException(status_code=500, detail=f"LLM call failed: {exc}") from exc

    return ChatResponse(
        model=payload.model,
        text=output_text,
        raw=raw_response if payload.include_raw else None,
    )


def _run_probability_request(payload: dict[str, Any], system_prompt: str | None) -> ChatResponse:
    message, include_raw = extract_probability_message(payload)
    model = get_default_model()

    try:
        client = build_client()
        output_text, raw_response = call_llm(client, model, message, system_prompt)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"LLM call failed: {exc}") from exc

    return ChatResponse(
        model=model,
        text=output_text,
        raw=raw_response if include_raw else None,
    )


@app.post("/api/llm/probability", response_model=ChatResponse)
def probability(payload: dict[str, Any]) -> ChatResponse:
    return _run_probability_request(payload, get_probability_prompt())


@app.post("/api/llm/probability-reason", response_model=ChatResponse)
def probability_reason(payload: dict[str, Any]) -> ChatResponse:
    return _run_probability_request(payload, get_probability_reason_prompt())


if __name__ == "__main__":
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    reload_enabled = os.getenv("UVICORN_RELOAD", "false").lower() == "true"

    uvicorn.run("app.main:app", host=host, port=port, reload=reload_enabled)
