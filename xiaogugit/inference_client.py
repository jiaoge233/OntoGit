from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib import error, request


logger = logging.getLogger("xiaogugit.inference_client")


class DangGuInferenceClient:
    def __init__(
        self,
        inference_url: str = "",
        timeout: int = 10,
        retry_attempts: int = 3,
        retry_backoff_seconds: float = 0.5,
    ):
        self.inference_url = (inference_url or "").strip()
        self.timeout = max(int(timeout or 10), 1)
        self.retry_attempts = max(int(retry_attempts or 1), 1)
        self.retry_backoff_seconds = max(float(retry_backoff_seconds or 0), 0.0)

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

        last_error: Exception | None = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                return self._infer_change_once(payload)
            except RuntimeError as exc:
                last_error = exc
                if attempt >= self.retry_attempts:
                    break
                sleep_seconds = self.retry_backoff_seconds * (2 ** (attempt - 1))
                logger.warning(
                    "inference service call failed, retrying attempt %s/%s after %.1fs: %s",
                    attempt + 1,
                    self.retry_attempts,
                    sleep_seconds,
                    exc,
                )
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)

        raise RuntimeError(f"inference service failed after {self.retry_attempts} attempts: {last_error}") from last_error

    def _infer_change_once(self, payload: dict[str, Any]) -> dict[str, Any]:
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
