"""Message content handling shared by every protocol adapter.

Internally one message content is either plain text (``str``) or a list of
blocks in the first hop's Chat Completions shape:

- ``{"type": "text", "text": "..."}``
- ``{"type": "image_url", "image_url": {"url": "..."}}``
- ``{"type": "file", "file": {...}}``

Only image and file blocks are considered attachments; everything the model
should read as text goes through :func:`text_of`.
"""

from __future__ import annotations

import json
from typing import Any

ATTACHMENT_TYPES = ("image_url", "file")
_TEXT_TYPES = ("text", "input_text", "output_text", "summary_text")
_ATTACHMENT_KINDS = ("image", "input_image", "document", "input_file")


def text_of(content: Any) -> str:
    """Return the readable text of a message content, ignoring attachments."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return "\n".join(part for item in content if (part := text_of(item)))
    if isinstance(content, dict):
        kind = content.get("type")
        if kind in ATTACHMENT_TYPES or kind in _ATTACHMENT_KINDS:
            return ""
        if isinstance(content.get("text"), str):
            return content["text"]
        if "content" in content:
            return text_of(content["content"])
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def attachments_of(content: Any) -> list[dict[str, Any]]:
    """Return attachment blocks of a message content in upstream shape."""
    if not isinstance(content, (list, tuple)):
        return []
    return [
        dict(part)
        for part in content
        if isinstance(part, dict) and part.get("type") in ATTACHMENT_TYPES
    ]


def build_content(texts: list[str], attachments: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    """Join text parts and attachments into one message content."""
    text = "\n".join(part for part in texts if part)
    if not attachments:
        return text
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    blocks.extend(attachments)
    return blocks


def append_text(content: Any, extra: str) -> str | list[dict[str, Any]]:
    """Return content with ``extra`` appended as a trailing text block."""
    if not extra:
        return content if content is not None else ""
    if isinstance(content, (list, tuple)):
        return [*content, {"type": "text", "text": extra}]
    base = content if isinstance(content, str) else ""
    return f"{base}\n\n{extra}" if base else extra


def prepend_text(content: Any, extra: str) -> str | list[dict[str, Any]]:
    """Return content with ``extra`` placed before its text."""
    if not extra:
        return content if content is not None else ""
    if isinstance(content, (list, tuple)):
        return [{"type": "text", "text": extra}, *content]
    base = content if isinstance(content, str) else ""
    return f"{extra}\n\n{base}" if base else extra


def image_block(url: Any, detail: Any = None) -> dict[str, Any] | None:
    if not isinstance(url, str) or not url:
        return None
    image: dict[str, Any] = {"url": url}
    if isinstance(detail, str) and detail:
        image["detail"] = detail
    return {"type": "image_url", "image_url": image}


def file_block(file: Any) -> dict[str, Any] | None:
    if not isinstance(file, dict):
        return None
    if not file.get("file_data") and not file.get("file_url"):
        return None
    return {"type": "file", "file": dict(file)}


def data_url(media_type: Any, data: Any) -> str | None:
    if not isinstance(data, str) or not data:
        return None
    if data.startswith("data:"):
        return data
    kind = media_type if isinstance(media_type, str) and media_type else "application/octet-stream"
    return f"data:{kind};base64,{data}"
