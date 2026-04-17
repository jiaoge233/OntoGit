from __future__ import annotations

import json
import logging
import socket
import time
from typing import Any
from urllib import error, request


logger = logging.getLogger("xiaogugit.inference_client")


def _payload_summary(payload: dict[str, Any], max_keys: int = 12) -> str:
    keys = list(payload.keys())[:max_keys]
    suffix = "..." if len(payload) > max_keys else ""
    return f"keys=[{', '.join(keys)}]{suffix}"


def _truncate(s: str, max_len: int = 120) -> str:
    s = (s or "").strip().replace("\n", " ")
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


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
            logger.info("inference skipped: XG_INFERENCE_URL is empty")
            return {
                "probability": "",
                "reason": "",
                "status": "skipped",
                "detail": "XG_INFERENCE_URL is not configured",
            }

        last_error: Exception | None = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                return self._infer_change_once(payload, attempt=attempt)
            except Exception as exc:
                if not isinstance(exc, RuntimeError):
                    exc = RuntimeError(f"unexpected inference client error: {exc}")
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

    def _infer_change_once(self, payload: dict[str, Any], *, attempt: int) -> dict[str, Any]:
        body_bytes = json.dumps(self._build_request_body(payload), ensure_ascii=False).encode("utf-8")
        logger.info(
            "inference request attempt=%s/%s POST %s body_bytes=%s %s",
            attempt,
            self.retry_attempts,
            self.inference_url,
            len(body_bytes),
            _payload_summary(payload),
        )
        req = request.Request(
            self.inference_url,
            data=body_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.perf_counter()
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw_body = response.read().decode("utf-8")
                http_status = response.getcode()
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.warning(
                "inference HTTP error attempt=%s/%s elapsed_ms=%.1f code=%s body_preview=%s",
                attempt,
                self.retry_attempts,
                elapsed_ms,
                exc.code,
                _truncate(body, 200),
            )
            raise RuntimeError(f"inference service returned HTTP {exc.code}: {body or exc.reason}") from exc
        except error.URLError as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.warning(
                "inference unreachable attempt=%s/%s elapsed_ms=%.1f reason=%s",
                attempt,
                self.retry_attempts,
                elapsed_ms,
                exc.reason,
            )
            raise RuntimeError(f"inference service is unreachable: {exc.reason}") from exc
        except (TimeoutError, socket.timeout) as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.warning(
                "inference timeout attempt=%s/%s elapsed_ms=%.1f timeout=%ss",
                attempt,
                self.retry_attempts,
                elapsed_ms,
                self.timeout,
            )
            raise RuntimeError(f"inference service timed out after {self.timeout}s") from exc
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.warning(
                "inference unexpected error attempt=%s/%s elapsed_ms=%.1f error=%s",
                attempt,
                self.retry_attempts,
                elapsed_ms,
                exc,
            )
            raise RuntimeError(f"unexpected inference client error: {exc}") from exc

        elapsed_ms = (time.perf_counter() - started) * 1000
        try:
            result = json.loads(raw_body or "{}")
        except json.JSONDecodeError as exc:
            logger.warning(
                "inference invalid JSON attempt=%s/%s elapsed_ms=%.1f http=%s raw_preview=%s",
                attempt,
                self.retry_attempts,
                elapsed_ms,
                http_status,
                _truncate(raw_body, 200),
            )
            raise RuntimeError("inference service returned invalid JSON") from exc

        if not isinstance(result, dict):
            logger.warning(
                "inference non-object response attempt=%s/%s elapsed_ms=%.1f http=%s",
                attempt,
                self.retry_attempts,
                elapsed_ms,
                http_status,
            )
            raise RuntimeError("inference service returned a non-object response")

        normalized = self._normalize_response(result)
        logger.info(
            "inference OK attempt=%s/%s elapsed_ms=%.1f http=%s probability=%s reason_preview=%s",
            attempt,
            self.retry_attempts,
            elapsed_ms,
            http_status,
            _truncate(str(normalized.get("probability", "")), 40),
            _truncate(str(normalized.get("reason", "")), 120),
        )
        return normalized
