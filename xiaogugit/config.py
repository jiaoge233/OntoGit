from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


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


def _read_bool(values: dict[str, str], key: str, default: bool) -> bool:
    raw_value = values.get(key)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _read_int(values: dict[str, str], key: str, default: int) -> int:
    raw_value = values.get(key)
    if raw_value is None:
        return default
    return int(raw_value)


def _build_merged_env() -> dict[str, str]:
    base_env = _read_env_file(BASE_DIR / ".env")
    initial_mode = _normalize_env(os.environ.get("XG_ENV") or base_env.get("XG_ENV"))
    mode_env = _read_env_file(BASE_DIR / f".env.{initial_mode}")

    merged: dict[str, str] = {}
    merged.update(base_env)
    merged.update(mode_env)
    merged.update(os.environ)
    merged["XG_ENV"] = _normalize_env(merged.get("XG_ENV"))
    return merged


@dataclass(frozen=True)
class Settings:
    env: str
    host: str
    port: int
    storage_root: str
    docs_enabled: bool
    reload: bool
    auth_secret: str
    auth_cookie_name: str
    auth_username: str
    auth_password: str
    inference_url: str
    inference_timeout: int
    inference_retry_attempts: int
    inference_retry_backoff_seconds: float
    redis_enabled: bool
    redis_host: str
    redis_port: int
    redis_password: str
    redis_db: int
    redis_key_prefix: str
    redis_socket_timeout: float

    @property
    def docs_url(self) -> str | None:
        return "/docs" if self.docs_enabled else None

    @property
    def redoc_url(self) -> str | None:
        return "/redoc" if self.docs_enabled else None

    @property
    def openapi_url(self) -> str | None:
        return "/openapi.json" if self.docs_enabled else None

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    def public_dict(self) -> dict[str, object]:
        return {
            "env": self.env,
            "docs_enabled": self.docs_enabled,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    values = _build_merged_env()
    env = values["XG_ENV"]

    if env == "production":
        default_host = "0.0.0.0"
        default_port = 8000
        default_storage_root = str(BASE_DIR / "storage" / "prod")
        default_docs_enabled = False
        default_reload = False
    else:
        default_host = "127.0.0.1"
        default_port = 8000
        default_storage_root = str(BASE_DIR / "storage" / "dev")
        default_docs_enabled = True
        default_reload = True

    storage_root = values.get("XG_STORAGE_ROOT", default_storage_root)
    storage_path = Path(storage_root)
    if not storage_path.is_absolute():
        storage_path = (BASE_DIR / storage_path).resolve()

    return Settings(
        env=env,
        host=values.get("XG_HOST", default_host),
        port=_read_int(values, "XG_PORT", default_port),
        storage_root=str(storage_path),
        docs_enabled=_read_bool(values, "XG_DOCS_ENABLED", default_docs_enabled),
        reload=_read_bool(values, "XG_RELOAD", default_reload),
        auth_secret=values.get("XG_AUTH_SECRET", "xiaogugit-auth-secret"),
        auth_cookie_name=values.get("XG_AUTH_COOKIE_NAME", "xg_session"),
        auth_username=values.get("XG_AUTH_USERNAME", "mogong"),
        auth_password=values.get("XG_AUTH_PASSWORD", "123456"),
        inference_url=values.get("XG_INFERENCE_URL", "http://127.0.0.1:5000/api/llm/probability-reason").strip(),
        inference_timeout=_read_int(values, "XG_INFERENCE_TIMEOUT", 10),
        inference_retry_attempts=_read_int(values, "XG_INFERENCE_RETRY_ATTEMPTS", 3),
        inference_retry_backoff_seconds=float(values.get("XG_INFERENCE_RETRY_BACKOFF_SECONDS", "0.5")),
        redis_enabled=_read_bool(values, "XG_REDIS_ENABLED", False),
        redis_host=values.get("XG_REDIS_HOST", "127.0.0.1").strip(),
        redis_port=_read_int(values, "XG_REDIS_PORT", 6379),
        redis_password=values.get("XG_REDIS_PASSWORD", "").strip(),
        redis_db=_read_int(values, "XG_REDIS_DB", 0),
        redis_key_prefix=values.get("XG_REDIS_KEY_PREFIX", "xg").strip() or "xg",
        redis_socket_timeout=float(values.get("XG_REDIS_SOCKET_TIMEOUT", "1.5")),
    )
