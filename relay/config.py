from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _nonnegative_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = int(raw)
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


@dataclass(frozen=True)
class RelayConfig:
    upstream_base_url: str
    upstream_api_key: str | None = None
    relay_api_key: str | None = None
    host: str = "127.0.0.1"
    port: int = 31110
    upstream_connect_timeout: float = 10.0
    upstream_read_timeout: float = 1800.0
    upstream_action_retries: int = 2
    max_action_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        parsed = urlsplit(self.upstream_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("UPSTREAM_BASE_URL must be an absolute HTTP(S) URL")
        if self.port < 1 or self.port > 65535:
            raise ValueError("PORT must be between 1 and 65535")
        if self.max_action_bytes < 1024:
            raise ValueError("MAX_ACTION_BYTES must be at least 1024")

    @classmethod
    def from_env(cls) -> "RelayConfig":
        upstream = os.environ.get("UPSTREAM_BASE_URL", "http://127.0.0.1:31100").strip()
        return cls(
            upstream_base_url=upstream.rstrip("/"),
            upstream_api_key=os.environ.get("UPSTREAM_API_KEY") or None,
            relay_api_key=os.environ.get("RELAY_API_KEY") or None,
            host=os.environ.get("HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=int(os.environ.get("PORT", "31110")),
            upstream_connect_timeout=_positive_float("UPSTREAM_CONNECT_TIMEOUT", 10.0),
            upstream_read_timeout=_positive_float("UPSTREAM_READ_TIMEOUT", 1800.0),
            upstream_action_retries=_nonnegative_int("UPSTREAM_ACTION_RETRIES", 2),
            max_action_bytes=_nonnegative_int("MAX_ACTION_BYTES", 1_048_576),
        )
