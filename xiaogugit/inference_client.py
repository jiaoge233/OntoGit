from __future__ import annotations

import json
from typing import Any
from urllib import error, request


class DangGuInferenceClient:
    def __init__(self, inference_url: str = "", timeout: int = 10):
        self.inference_url = (inference_url or "").strip()
        self.timeout = max(int(timeout or 10), 1)

    def _build_request_body(self, payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    def _normalize_response(self, result: dict[str, Any]) -> dict[str, Any]:
        if "probability" in result or "reason" in result:
            return {
                "probability": str(result.get("probability", "")),
                "reason": str(result.get("reason", "")),
                "status": "success",
            }

        text = result.get("text", "")
        if isinstance(text, str) and text.strip():
            try:
                parsed_text = json.loads(text)
            except json.JSONDecodeError:
                return {
                    "probability": "",
                    "reason": text,
                    "status": "success",
                }
            if isinstance(parsed_text, dict):
                return {
                    "probability": str(parsed_text.get("probability", "")),
                    "reason": str(parsed_text.get("reason", "")),
                    "status": "success",
                }

        return {
            "probability": "",
            "reason": "",
            "status": "success",
        }

    def infer_change(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.inference_url:
            return {
                "probability": "",
                "reason": "",
                "status": "skipped",
                "detail": "XG_INFERENCE_URL is not configured",
            }

        req = request.Request(
            self.inference_url,
            data=json.dumps(self._build_request_body(payload), ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw_body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"inference service returned HTTP {exc.code}: {body or exc.reason}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"inference service is unreachable: {exc.reason}") from exc

        try:
            result = json.loads(raw_body or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("inference service returned invalid JSON") from exc

        if not isinstance(result, dict):
            raise RuntimeError("inference service returned a non-object response")

        return self._normalize_response(result)
