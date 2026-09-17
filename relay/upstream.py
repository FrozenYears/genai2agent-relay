from __future__ import annotations

from typing import Any

import requests

from .config import RelayConfig
from .engine import TextCompletionRequest, UpstreamReply


class UpstreamError(RuntimeError):
    pass


class UpstreamClient:
    def __init__(self, config: RelayConfig, session: requests.Session | None = None) -> None:
        self.config = config
        self.session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.config.upstream_api_key:
            headers["Authorization"] = f"Bearer {self.config.upstream_api_key}"
        return headers

    def complete(self, request: TextCompletionRequest) -> UpstreamReply:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "stream": False,
            "max_tokens": request.max_tokens,
            **request.options,
            **request.sampling,
        }
        try:
            response = self.session.post(
                self.config.upstream_base_url + "/v1/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=(self.config.upstream_connect_timeout, self.config.upstream_read_timeout),
            )
        except requests.RequestException as exc:
            raise UpstreamError(f"Cannot reach first-hop service: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise UpstreamError(f"First-hop service returned HTTP {response.status_code}")
        try:
            data = response.json()
            message = data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise UpstreamError("First-hop service returned an invalid Chat Completions response") from exc
        if message.get("tool_calls"):
            raise UpstreamError("First-hop service emitted an unsupported native tool call")
        return UpstreamReply(
            content=message.get("content") or "",
            reasoning=message.get("reasoning_content") or "",
            usage=data.get("usage") if isinstance(data.get("usage"), dict) else {},
        )

    def models(self) -> dict[str, Any]:
        try:
            response = self.session.get(
                self.config.upstream_base_url + "/v1/models",
                headers=self._headers(),
                timeout=(self.config.upstream_connect_timeout, self.config.upstream_read_timeout),
            )
        except requests.RequestException as exc:
            raise UpstreamError(f"Cannot reach first-hop service: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise UpstreamError(f"First-hop service returned HTTP {response.status_code}")
        try:
            value = response.json()
        except ValueError as exc:
            raise UpstreamError("First-hop model endpoint returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise UpstreamError("First-hop model endpoint returned an invalid object")
        return value
