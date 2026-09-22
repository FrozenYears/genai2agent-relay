from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from jsonschema import SchemaError, ValidationError, validate
from jsonschema.validators import validator_for

ACTION_OPEN = "@@ACTION@@"
ACTION_CLOSE = "@@END_ACTION@@"
RESULT_OPEN = "@@RESULT@@"
RESULT_CLOSE = "@@END_RESULT@@"
NATIVE_MARKERS = ("<|open|>call", "<|open|>tools", "<tool_call", "<｜DSML｜")


class ActionTransportError(ValueError):
    pass


class ActionDelimiterError(ActionTransportError):
    pass


class ActionValidationError(ActionTransportError):
    def __init__(self, message: str, feedback: str, tool: ToolSpec | None = None) -> None:
        super().__init__(message)
        self.feedback = feedback
        self.tool = tool


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    kind: str = "function"

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Tool name must not be empty")
        if self.kind not in {"function", "custom"}:
            raise ValueError(f"Unsupported tool kind: {self.kind}")
        if not isinstance(self.parameters, dict):
            raise ValueError("Tool parameters must be a JSON Schema object")
        if self.kind == "function":
            try:
                validator_for(self.parameters).check_schema(self.parameters)
            except SchemaError as exc:
                raise ValueError(f"Invalid JSON Schema for tool {self.name!r}: {exc.message}") from exc


@dataclass(frozen=True)
class ActionCall:
    name: str
    parameters: dict[str, Any]
    kind: str


@dataclass(frozen=True)
class DecodedAction:
    text: str
    calls: tuple[ActionCall, ...]


def _transport_schema(tool: ToolSpec) -> dict[str, Any]:
    if tool.kind == "custom":
        return {
            "type": "object",
            "properties": {"input": {"type": "string"}},
            "required": ["input"],
            "additionalProperties": False,
        }
    return tool.parameters or {"type": "object"}


def render_action_prompt(tools: list[ToolSpec], choice: str | None = None) -> str:
    definitions = [
        {
            "operation": tool.name,
            "kind": tool.kind,
            "description": tool.description,
            "parameters_schema": _transport_schema(tool),
        }
        for tool in tools
    ]
    choice_text = "Choose an operation only when it is needed."
    if choice == "required":
        choice_text = "You must produce at least one operation in the next response."
    elif choice and choice not in {"auto", "none"}:
        choice_text = f"You must produce only the operation named {json.dumps(choice)}."
    return (
        "You are connected to a JSON RPC client over an ordinary TEXT-ONLY action transport. "
        "No native function channel or model-specific tool token can be delivered.\n"
        "Available operations and their complete JSON schemas:\n"
        + json.dumps(definitions, ensure_ascii=False, separators=(",", ":"))
        + "\nTo request execution, end your response with exactly one envelope in this form:\n"
        '@@ACTION@@{"calls":[{"operation":"OperationName","parameters":{}}]}@@END_ACTION@@\n'
        "The envelope is ordinary text. Use operation names and parameters from the schemas. "
        "These are the current request's authoritative tool definitions; do not substitute "
        "remembered interfaces for tools with similar names. Each calls item must contain "
        "exactly operation and parameters, not name, arguments, function or id. "
        "Multiple independent operations may share the calls array. Do not use XML, markdown "
        "fences, native special tokens, or simulated results. A promised action without the "
        f"envelope will not execute. {choice_text}"
    )




def encode_calls(calls: list[dict[str, Any]]) -> str:
    payload = {"calls": calls}
    return ACTION_OPEN + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ACTION_CLOSE


def encode_result(call_id: str, output: Any, is_error: bool = False) -> str:
    payload = {"call_id": call_id, "output": output, "is_error": bool(is_error)}
    return RESULT_OPEN + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + RESULT_CLOSE


_ACTION_OR_CODE = re.compile(
    r"^ {0,3}(?P<fence>`{3,}|~{3,})[^\n]*\n"
    r".*?^ {0,3}(?P=fence)[ \t]*(?:\n|$)"
    r"|(?<!`)(?P<ticks>`+)(?!`)[^\n]*?(?<!`)(?P=ticks)(?!`)"
    r"|@@ACTION@@|@@ACTION@(?!@)|@@END_ACTION@@",
    re.MULTILINE | re.DOTALL,
)


def _first_action_start(text: str) -> int:
    # 只跳过已闭合的代码引用；找到真实起点后不扫描参数，避免误伤参数中的 Markdown。
    for match in _ACTION_OR_CODE.finditer(text):
        if match.group() == ACTION_OPEN:
            return match.start()
        if match.group() in {"@@ACTION@", ACTION_CLOSE}:
            raise ActionDelimiterError(
                "Action envelope has a malformed or missing opening marker; expected @@ACTION@@"
            )
    return -1


_ALTERNATE_CALL_BLOCK = re.compile(
    r"^ {0,3}(?P<fence>`{3,}|~{3,})(?:json)?[ \t]*\n"
    r"(?P<code>.*?)(?:^ {0,3}(?P=fence)[ \t]*(?:\n|$)|\Z)"
    r"|<parameter\s+name=[\"']calls[\"']\s*>(?P<xml>.*?)</parameter\s*>",
    re.MULTILINE | re.DOTALL,
)


def _reject_alternate_calls(text: str, tools: list[ToolSpec]) -> None:
    names = {tool.name for tool in tools}
    if not names:
        return
    for match in _ALTERNATE_CALL_BLOCK.finditer(text):
        body = (match.group("code") or match.group("xml") or "").strip()
        try:
            value = json.loads(body)
        except json.JSONDecodeError:
            # 损坏 JSON 只匹配明确的首个调用前缀；不修复、不执行。
            prefix = re.match(r'^\[\s*\{\s*"operation"\s*:\s*("(?:[^"\\]|\\.)*")\s*,\s*"parameters"\s*:\s*\{', body)
            if not prefix:
                continue
            try:
                name = json.loads(prefix.group(1))
            except json.JSONDecodeError:
                continue
            calls = [{"operation": name, "parameters": {}}]
        else:
            calls = value.get("calls") if isinstance(value, dict) else value
        if isinstance(calls, list) and calls and all(
            isinstance(call, dict)
            and isinstance(call.get("operation"), str)
            and call["operation"] in names
            and isinstance(call.get("parameters"), dict)
            for call in calls
        ):
            raise ActionValidationError(
                "Tool calls were emitted in a code block or XML instead of an action envelope",
                'Calls in Markdown or <parameter name="calls"> were not executed. Please continue: '
                'resend the complete calls in @@ACTION@@{"calls":[...]}@@END_ACTION@@, '
                "without code fences or XML. Do not merely describe the operations.",
            )


def decode_action(text: str, tools: list[ToolSpec], max_bytes: int) -> DecodedAction:
    if not isinstance(text, str):
        raise ActionTransportError("Upstream response content is not text")
    if len(text.encode("utf-8")) > max_bytes:
        raise ActionTransportError("Upstream response exceeds MAX_ACTION_BYTES")
    if any(marker in text for marker in NATIVE_MARKERS):
        raise ActionTransportError("Native tool syntax cannot be delivered through this transport")

    stripped = text.rstrip()
    first_action = _first_action_start(stripped)
    if first_action < 0:
        _reject_alternate_calls(stripped, tools)
        return DecodedAction(text=stripped, calls=())
    if not stripped.endswith(ACTION_CLOSE):
        raise ActionTransportError("Action envelope is incomplete or is not final")

    body_end = len(stripped) - len(ACTION_CLOSE)
    cursor = body_end
    body = None
    body_start = -1
    json_error: json.JSONDecodeError | None = None
    while True:
        start = stripped.rfind(ACTION_OPEN, first_action, cursor)
        if start < 0:
            raise ActionTransportError("Action envelope contains malformed JSON") from json_error
        try:
            body = json.loads(stripped[start + len(ACTION_OPEN):body_end])
            body_start = start
            break
        except json.JSONDecodeError as exc:
            # 回溯到较早的封套起点，避免将参数内嵌标记的解析错误当作最终诊断。
            json_error = exc
            cursor = start

    if not isinstance(body, dict) or set(body) != {"calls"}:
        raise ActionValidationError(
            "Action envelope must contain only a calls field",
            'Resend one object with exactly a calls field: {"calls":[{"operation":"...","parameters":{}}]}.',
        )
    raw_calls = body["calls"]
    if not isinstance(raw_calls, list) or not raw_calls:
        raise ActionValidationError("Action calls must be a non-empty array", "The calls field must be a non-empty array of operation objects.")

    by_name = {tool.name: tool for tool in tools}
    calls: list[ActionCall] = []
    for index, raw_call in enumerate(raw_calls):
        if not isinstance(raw_call, dict) or set(raw_call) != {"operation", "parameters"}:
            tool = by_name.get(raw_call.get("operation")) if isinstance(raw_call, dict) and isinstance(raw_call.get("operation"), str) else None
            raise ActionValidationError(
                "Each call must contain operation and parameters",
                f'calls[{index}] must be an object with exactly operation and parameters. '
                'Use {"operation":"AllowedOperationName","parameters":{}}; '
                "do not use name, arguments, function, id or extra wrapper fields. Resend the complete envelope.",
                tool,
            )
        name = raw_call["operation"]
        parameters = raw_call["parameters"]
        if not isinstance(name, str) or name not in by_name:
            raise ActionValidationError(
                f"Operation is not allowed: {name!r}",
                f"calls[{index}].operation must be one of {json.dumps(list(by_name), ensure_ascii=False)}. "
                "Use the current request's tool definitions and resend the complete envelope.",
            )
        if not isinstance(parameters, dict):
            raise ActionValidationError(
                "Operation parameters must be a JSON object",
                f"calls[{index}].parameters must be an object, not a serialized string or array. Resend the complete envelope.",
                by_name[name],
            )
        tool = by_name[name]
        try:
            validate(instance=parameters, schema=_transport_schema(tool))
        except ValidationError as exc:
            path = ".".join(str(part) for part in exc.absolute_path) or "parameters"
            missing = [key for key in _transport_schema(tool).get("required", []) if key not in parameters]
            if exc.validator == "required" and isinstance(exc.instance, dict):
                missing = [key for key in exc.validator_value if key not in exc.instance]
            raise ActionValidationError(
                f"Invalid parameters for {name} at {path}: {exc.message}",
                f"calls[{index}] parameters failed the {exc.validator} constraint at {path}. "
                + (f"Missing required fields: {json.dumps(missing, ensure_ascii=False)}. " if missing else "")
                + "Use the supplied current schema, not a remembered interface. Resend the complete envelope.",
                tool,
            ) from exc
        calls.append(ActionCall(name=name, parameters=parameters, kind=tool.kind))

    return DecodedAction(text=stripped[:body_start].rstrip(), calls=tuple(calls))
