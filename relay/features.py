"""Map downstream thinking and search controls onto first-hop request options.

The first hop exposes two verified switches: ``thinking`` (boolean) and
``web_search`` (boolean). Every client-side spelling of those controls is
reduced to those two explicit booleans, so the upstream never has to guess.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

SEARCH_TOOL_PREFIX = "web_search"
EFFORT_LEVELS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
ANTHROPIC_THINKING_MODES = {"enabled", "adaptive", "disabled"}


def is_search_tool(tool: Any) -> bool:
    """A client-managed search declaration, not a locally executable function."""
    return isinstance(tool, dict) and str(tool.get("type", "")).startswith(SEARCH_TOOL_PREFIX)


def _thinking_from_body(body: dict[str, Any], api_format: str) -> bool | None:
    if "thinking" in body:
        thinking = body["thinking"]
        if isinstance(thinking, bool):
            return thinking
        if isinstance(thinking, dict):
            mode = thinking.get("type")
            if mode not in ANTHROPIC_THINKING_MODES:
                raise ValueError("thinking.type must be enabled, adaptive or disabled")
            if "budget_tokens" in thinking:
                logger.info("thinking.budget_tokens only toggles thinking; the platform has no budget control")
            return mode != "disabled"
        raise ValueError("thinking must be a boolean or an Anthropic thinking object")

    effort: Any = None
    if api_format == "responses":
        reasoning = body.get("reasoning")
        if reasoning is not None:
            if not isinstance(reasoning, dict):
                raise ValueError("reasoning must be an object")
            effort = reasoning.get("effort")
    elif api_format == "chat":
        effort = body.get("reasoning_effort")
    if effort is None:
        return None
    if effort not in EFFORT_LEVELS:
        raise ValueError(f"unsupported reasoning effort: {effort!r}")
    if effort != "none":
        logger.info("reasoning effort=%s mapped to thinking=true; the platform has no levels", effort)
    return effort != "none"


def _search_disabled_by_choice(choice: Any, search_names: set[str]) -> bool:
    if choice is None:
        return False
    if isinstance(choice, str):
        return choice == "none"
    if not isinstance(choice, dict):
        return False
    if choice.get("type") == "none":
        return True
    if choice.get("type") in {"function", "tool", "custom"}:
        name = choice.get("name") or (choice.get("function") or {}).get("name")
        return name not in search_names
    return False


def request_options(body: dict[str, Any], tools: Any, api_format: str) -> dict[str, Any]:
    """Return the first-hop options implied by one downstream request body."""
    options: dict[str, Any] = {}

    thinking = _thinking_from_body(body, api_format)
    if thinking is not None:
        options["thinking"] = thinking

    declared = [tool for tool in tools or [] if is_search_tool(tool)]
    search = body.get("web_search")
    if search is not None and not isinstance(search, bool):
        raise ValueError("web_search must be a boolean")
    if search is None:
        search_names = {str(tool.get("name") or "web_search") for tool in declared}
        chosen = body.get("tool_choice")
        disabled = _search_disabled_by_choice(chosen, search_names)
        enabled = "web_search_options" in body or (bool(declared) and not disabled)
        if enabled:
            search = True
    if search is not None:
        options["web_search"] = search
    return options
