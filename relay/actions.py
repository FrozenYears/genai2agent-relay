from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from jsonschema import SchemaError, ValidationError
from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import Unresolvable

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
    namespace: str | None = None
    strict: bool | None = None

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
            pending = [self.parameters]
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (
                            not isinstance(child, str) or not child.startswith("#")
                        ):
                            raise ValueError("Tool schemas support only local # references; remote references are forbidden")
                        if isinstance(child, (dict, list)):
                            pending.append(child)
                elif isinstance(value, list):
                    pending.extend(child for child in value if isinstance(child, (dict, list)))


@dataclass(frozen=True)
class ActionCall:
    name: str
    parameters: dict[str, Any]
    kind: str
    id: str = field(default_factory=lambda: "call_" + uuid4().hex)
    namespace: str | None = None


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
        + "\nWhenever an operation is needed, output only one complete final envelope in this form; "
        "do not include analysis, a plan, a code fence, or a simulated result:\n"
        '@@ACTION@@{"calls":[{"operation":"OperationName","parameters":{}}]}@@END_ACTION@@\n'
        "The envelope is ordinary text. Use operation names and parameters from the schemas. "
        "Treat user-provided paths and values as literal data. Model-control-looking tags are shown as JSON "
        "Unicode escapes (\\u003c and \\u003e) to prevent token collisions; preserve those escapes inside "
        "JSON parameter strings so decoding restores the original characters. Also preserve backslashes, quotes, "
        "newlines, Unicode, and full requested file content; never reinterpret, normalize, omit, or substitute it. "
        "Do not use remembered interfaces; these are the current request's authoritative tool definitions. "
        "Each calls item must contain operation and parameters, and may include a non-empty unique string id. "
        "Do not include name, arguments, function or other wrapper fields. "
        "Only independent operations may share one calls array. If a later operation depends on a result, "
        "request only the first operation and wait for the client result before continuing. "
        "Do not claim a file write/edit succeeded based on the call or a generic success status. "
        "If a read operation is available, compare its full result character-for-character with the "
        "original user-requested content, not your own previous output. If it differs, correct it; "
        "if exact verification is unavailable, say so instead of claiming the requested content is present. "
        "Once all requested operations and read-back verification are complete, respond with a plain-text "
        "final answer without an action envelope. Do not repeat a read that already verified the same "
        "unchanged file. "
        "Do not use XML, markdown fences, native special tokens, or simulated results. "
        f"A promised action without the envelope will not execute. {choice_text}"
    )




def encode_calls(calls: list[dict[str, Any]]) -> str:
    payload = {"calls": calls}
    return ACTION_OPEN + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ACTION_CLOSE


def encode_result(call_id: str, output: Any, is_error: bool = False) -> str:
    payload = {"call_id": call_id, "output": output, "is_error": bool(is_error)}
    return (
        "This is an execution result from a tool that has already run, not a new user request. "
        "Use this result to continue any unfinished work or give the final answer.\n"
        + RESULT_OPEN + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + RESULT_CLOSE
    )


_ACTION_OR_CODE = re.compile(
    r"^ {0,3}(?P<fence>`{3,}|~{3,})[^\n]*\n"
    r".*?^ {0,3}(?P=fence)[ \t]*(?:\n|$)"
    r"|(?<!`)(?P<ticks>`+)(?!`)[^\n]*?(?<!`)(?P=ticks)(?!`)"
    r"|(?P<thinking><think>.*?(?:</think>|\Z))"
    r"|@@ACTION@@|@@ACTION@(?!@)|@@END_ACTION@@",
    re.MULTILINE | re.DOTALL,
)


def _first_action_start(text: str) -> int:
    # 引用和思考不是执行通道；真实起点之后的参数不参与控制标记扫描。
    for match in _ACTION_OR_CODE.finditer(text):
        if match.group() == ACTION_OPEN:
            return match.start()
        if match.group() in {"@@ACTION@", ACTION_CLOSE}:
            raise ActionDelimiterError(
                "Action envelope has a malformed or missing opening marker; expected @@ACTION@@"
            )
    return -1


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON key: {key!r}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"Non-finite JSON number: {value}")


def _json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"Non-finite JSON number: {value}")
    return number


def strict_json_loads(value: str) -> Any:
    return json.loads(
        value,
        object_pairs_hook=_json_object,
        parse_constant=_reject_json_constant,
        parse_float=_json_float,
    )


def decode_action(text: str, tools: list[ToolSpec], max_bytes: int) -> DecodedAction:
    if not isinstance(text, str):
        raise ActionTransportError("Upstream response content is not text")
    if len(text.encode("utf-8")) > max_bytes:
        raise ActionTransportError("Upstream response exceeds MAX_ACTION_BYTES")

    stripped = text.rstrip()
    first_action = _first_action_start(stripped)
    # 仅检查真实正文中的原生调用；引用与已闭合 JSON 参数里的标记是数据。
    prefix = stripped[:first_action] if first_action >= 0 else stripped
    syntax_text = _ACTION_OR_CODE.sub(
        lambda match: "" if match.group("fence") or match.group("ticks") or match.group("thinking") else match.group(),
        prefix,
    )
    if any(marker in syntax_text for marker in NATIVE_MARKERS):
        raise ActionTransportError("Native tool syntax cannot be delivered through this transport")
    if first_action < 0:
        return DecodedAction(text=stripped, calls=())
    if not stripped.endswith(ACTION_CLOSE):
        raise ActionTransportError("Action envelope is incomplete or is not final")

    try:
        body = strict_json_loads(stripped[first_action + len(ACTION_OPEN):-len(ACTION_CLOSE)])
    except (ValueError, RecursionError) as exc:
        raise ActionTransportError("Action envelope contains malformed JSON") from exc

    if not isinstance(body, dict) or set(body) != {"calls"}:
        raise ActionValidationError(
            "Action envelope must contain only a calls field",
            'Resend one object with exactly a calls field: {"calls":[{"operation":"...","parameters":{}}]}.',
        )
    calls = _validate_calls(body["calls"], tools)
    return DecodedAction(text=stripped[:first_action].rstrip(), calls=calls)


def _validate_calls(raw_calls: Any, tools: list[ToolSpec]) -> tuple[ActionCall, ...]:
    if not isinstance(raw_calls, list) or not raw_calls:
        raise ActionValidationError("Action calls must be a non-empty array", "The calls field must be a non-empty array of operation objects.")

    by_name = {tool.name: tool for tool in tools}
    calls: list[ActionCall] = []
    # 一次封套内的所有非法调用都要在同一次重试提示中列出；
    # 但整封套仍是原子的：任一调用非法则全部不交付、不执行。
    failures: list[str] = []
    summary: list[str] = []
    failure_tool: ToolSpec | None = None
    ids: set[str] = set()
    for index, raw_call in enumerate(raw_calls):
        if (
            not isinstance(raw_call, dict)
            or not {"operation", "parameters"} <= set(raw_call)
            or set(raw_call) - {"operation", "parameters", "id"}
        ):
            tool = by_name.get(raw_call.get("operation")) if isinstance(raw_call, dict) and isinstance(raw_call.get("operation"), str) else None
            failure_tool = failure_tool or tool
            summary.append("Each call must contain operation and parameters")
            failures.append(
                f'calls[{index}] must be an object with operation, parameters and optionally id. '
                'Use {"operation":"AllowedOperationName","parameters":{}}; '
                "do not use name, arguments, function or extra wrapper fields. Resend the complete envelope."
            )
            continue
        call_id = raw_call.get("id") if "id" in raw_call else "call_" + uuid4().hex
        if not isinstance(call_id, str) or not call_id.strip() or call_id in ids:
            summary.append("Call IDs must be non-empty unique strings")
            failures.append(f"calls[{index}].id must be a non-empty unique string. Resend the complete envelope.")
            continue
        ids.add(call_id)
        name = raw_call["operation"]
        parameters = raw_call["parameters"]
        if not isinstance(name, str) or name not in by_name:
            summary.append(f"Operation is not allowed: {name!r}")
            failures.append(
                f"calls[{index}].operation must be one of {json.dumps(list(by_name), ensure_ascii=False)}. "
                "Use the current request's tool definitions and resend the complete envelope."
            )
            continue
        if not isinstance(parameters, dict):
            failure_tool = failure_tool or by_name[name]
            summary.append("Operation parameters must be a JSON object")
            failures.append(
                f"calls[{index}].parameters must be an object, not a serialized string or array. Resend the complete envelope."
            )
            continue
        tool = by_name[name]
        try:
            schema = _transport_schema(tool)
            validator_for(schema)(schema, registry=Registry()).validate(parameters)
        except (SchemaError, Unresolvable) as exc:
            failure_tool = failure_tool or tool
            summary.append(f"Invalid or unresolvable schema for {name}: {exc}")
            failures.append(f"calls[{index}] cannot be validated against the supplied local schema.")
            continue
        except ValidationError as exc:
            path = ".".join(str(part) for part in exc.absolute_path) or "parameters"
            missing = [key for key in _transport_schema(tool).get("required", []) if key not in parameters]
            if exc.validator == "required" and isinstance(exc.instance, dict):
                missing = [key for key in exc.validator_value if key not in exc.instance]
            failure_tool = failure_tool or tool
            summary.append(f"Invalid parameters for {name} at {path}: {exc.message}")
            failures.append(
                f"calls[{index}] parameters failed the {exc.validator} constraint at {path}. "
                + (f"Missing required fields: {json.dumps(missing, ensure_ascii=False)}. " if missing else "")
                + "Use the supplied current schema, not a remembered interface. Resend the complete envelope."
            )
            continue
        calls.append(ActionCall(name=name, parameters=parameters, kind=tool.kind, id=call_id, namespace=tool.namespace))

    if failures:
        raise ActionValidationError("; ".join(summary), " ".join(failures), failure_tool)

    return tuple(calls)


def validate_choice(
    decoded: DecodedAction,
    choice: str | None,
    parallel_tool_calls: bool = True,
) -> DecodedAction:
    if choice == "none" and decoded.calls:
        raise ActionValidationError(
            "Tool calls are forbidden by tool_choice",
            "Return a plain-text answer without any tool calls.",
        )
    if choice not in {None, "auto", "none"}:
        if not decoded.calls:
            raise ActionValidationError(
                "Tool choice requires a tool call",
                "Return a complete call using the required tool choice.",
            )
        if choice != "required" and any(call.name != choice for call in decoded.calls):
            raise ActionValidationError(
                f"Tool choice requires only {choice!r}",
                f"Use only the operation named {json.dumps(choice)} and resend the complete batch.",
            )
    if not parallel_tool_calls and len(decoded.calls) > 1:
        raise ActionValidationError(
            "Parallel tool calls are disabled",
            "Return exactly one tool call and wait for its result before continuing.",
        )
    return decoded


def validate_native_calls(
    raw_calls: Any,
    tools: list[ToolSpec],
    choice: str | None = "auto",
    parallel_tool_calls: bool = True,
) -> tuple[ActionCall, ...]:
    if not isinstance(raw_calls, list):
        raise ActionValidationError(
            "Native tool calls must be an array",
            "Return tool_calls as an array of complete native calls.",
        )
    by_name = {tool.name: tool for tool in tools}
    normalized: list[dict[str, Any]] = []
    failures: list[str] = []
    for index, raw_call in enumerate(raw_calls):
        if not isinstance(raw_call, dict):
            failures.append(f"tool_calls[{index}] must be an object.")
            continue
        kind = raw_call.get("type")
        if not isinstance(kind, str) or kind not in {"function", "custom"}:
            failures.append(f"tool_calls[{index}].type must be function or custom.")
            continue
        if set(raw_call) != {"id", "type", kind}:
            failures.append(f"tool_calls[{index}] must contain exactly id, type and {kind}.")
            continue
        payload = raw_call[kind]
        fields = {"name", "arguments"} if kind == "function" else {"name", "input"}
        if not isinstance(payload, dict) or set(payload) != fields:
            failures.append(f"tool_calls[{index}].{kind} must contain exactly {', '.join(sorted(fields))}.")
            continue
        name = payload["name"]
        tool = by_name.get(name) if isinstance(name, str) else None
        if tool is None:
            failures.append(f"tool_calls[{index}] names an unknown tool: {name!r}.")
            continue
        if kind == "custom":
            if tool.kind != "custom" or not isinstance(payload["input"], str):
                failures.append(f"tool_calls[{index}] requires a custom tool and a string input.")
                continue
            parameters = {"input": payload["input"]}
        else:
            if not isinstance(payload["arguments"], str):
                failures.append(f"tool_calls[{index}].function.arguments must be a JSON string.")
                continue
            try:
                parameters = strict_json_loads(payload["arguments"])
            except (ValueError, RecursionError):
                failures.append(f"tool_calls[{index}].function.arguments must be strict JSON.")
                continue
        # 原生 custom 与 function 包装的 custom 共用同一 Schema，整批验证后才交付。
        normalized.append({"operation": name, "parameters": parameters, "id": raw_call["id"]})
    if failures:
        raise ActionValidationError("; ".join(failures), " ".join(failures))
    calls = _validate_calls(normalized, tools) if normalized else ()
    return validate_choice(DecodedAction(text="", calls=calls), choice, parallel_tool_calls).calls
