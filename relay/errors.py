from __future__ import annotations

import re
from typing import Any, Iterable


# 只输出诊断字段；认证值和不透明编码串不能随错误穿透两跳服务。
_CREDENTIAL = re.compile(
    r'''(?ix)\b(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|api[_ -]?key|'''
    r'''access[_ -]?token|refresh[_ -]?token|id[_ -]?token|token|password|passwd|secret|'''
    r'''credentials?|session[_ -]?(?:id|token))\b["']?\s*[:=]\s*'''
    r'''(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\r\n]+)'''
)
_AUTH = re.compile(r'''(?i)\b(?:bearer|basic)\s+["']?[A-Za-z0-9._~+/-]+=*["']?''')
_TOKEN = re.compile(r"\b(?:sk-|gh[pousr]_|github_pat_)[A-Za-z0-9_-]+|[A-Za-z0-9_+/=-]{32,}(?:\.[A-Za-z0-9_+/-]+)*")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_URL_AUTH = re.compile(r"(?i)(https?://)[^/\s@]+@")
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,95}\Z")


def safe_message(value: str, secrets: Iterable[str] = ()) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[redacted]")
    value = _CREDENTIAL.sub("[redacted credential]", value)
    value = _AUTH.sub("[redacted authorization]", value)
    value = _JWT.sub("[redacted token]", value)
    value = _URL_AUTH.sub(r"\1[redacted]@", value)
    value = _TOKEN.sub("[redacted token]", value)
    value = " ".join("".join(char if char.isprintable() else " " for char in value).split())
    return value[:512] or "First-hop service returned an error"


def _identifier(value: Any, fallback: str | None, secrets: Iterable[str]) -> str | None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        return fallback
    return value if safe_message(value, secrets) == value else fallback


class UpstreamError(RuntimeError):
    def __init__(self, message: str, *, error_type: str = "api_error", code: str | None = "upstream_error",
                 secrets: Iterable[str] = ()) -> None:
        self.error = {
            "message": safe_message(message, secrets),
            "type": _identifier(error_type, "api_error", secrets),
            "code": _identifier(code, None, secrets),
        }
        super().__init__(self.error["message"])

    @classmethod
    def from_first_hop(cls, value: Any, fallback: str, secrets: Iterable[str] = ()) -> UpstreamError:
        value = value if isinstance(value, dict) else {}
        message = value.get("message")
        return cls(message if isinstance(message, str) and message.strip() else fallback,
                   error_type=value.get("type"), code=value.get("code"), secrets=secrets)


def error_fields(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, UpstreamError):
        return dict(exc.error)
    return {"type": "api_error", "code": "upstream_error", "message": safe_message(str(exc))}
