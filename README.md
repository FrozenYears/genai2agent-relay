# GenAI2AgentRelay

A native-first streaming gateway for OpenAI Chat Completions, Anthropic Messages,
and OpenAI Responses. Tool execution always remains in the downstream agent;
this service does not execute commands or access files named in tool arguments.

## Transport modes

`UPSTREAM_TOOL_MODE=native` is the default. The relay forwards tool definitions,
system/developer roles, assistant calls, and tool results to an OpenAI-compatible
Chat Completions backend. It consumes upstream SSE immediately, preserves call
IDs, validates the entire final call batch, and adapts it to the client protocol.
It never downgrades a failed native request to another model generation.

The default backend is `shanghaitech-genai2api` on port 31100. That first hop still
uses the university web-chat endpoint: it handles website login, attachments,
and the website's text-based tool fallback. Its support for parsing Kimi/DeepSeek
native syntax does not prove that the website accepts native tool request fields.
Upgrade both repositories together for the new default path.

两跳端口、各层职责与重构前后差异见根项目的
[原生优先阶段说明](../STAGE_NATIVE_FIRST.md)；阶段小验收不等同于正式服务长时实际工作验收。

Use `UPSTREAM_TOOL_MODE=text` only for an explicitly text-only backend. This mode
encodes tool definitions and history in ordinary conversation text. Historical
calls retain their IDs and results refer to those IDs, including out-of-order
results. The current schemas are placed near the latest user turn; system and
developer instructions become ordinary context. User/result control markers are
reversibly escaped without modifying attachments or historical call envelopes.

An actual text-mode call must be a single final envelope:

```text
@@ACTION@@{"calls":[{"operation":"read_file","parameters":{"path":"README.md"}}]}@@END_ACTION@@
```

A call may include an explicit unique string `id`; otherwise one is assigned.
Quoted examples and inline thinking are not executable actions. Code-fenced JSON
examples are no longer retried into actions. There is no delimiter repair,
parameter renaming, type guessing, or selection of the last valid envelope from
an invalid response.

## Streaming and execution boundary

All three client protocols send their opening event before model completion.
Ordinary text and reasoning arrive incrementally. Tool arguments are buffered
until normal upstream termination and full validation; a partial argument delta
is never an executable call. Responses events have monotonically increasing
`sequence_number` values and stable lifecycle IDs.

The relay forwards upstream heartbeat comments as Chat/Responses comments or
Messages `ping` events. It does not invent a separate timed heartbeat worker.
The accompanying first hop emits heartbeats while waiting for its backend.

Errors after headers are sent arrive as protocol error events, without a success
completion. Non-streaming failures return HTTP 502. Closing a consumed stream
closes the upstream HTTP response. A blocked connection before response headers
is still bounded by the configured timeouts, not guaranteed to cancel instantly.

The relay itself never retries model generation in native mode. Its paired first
hop permits one continuation only for a tool-enabled request that finished with
zero text, zero reasoning, zero calls, and a normal `stop`. It preserves history
and asks explicitly to continue; no client tool call was delivered before it.
Reasoning/text already emitted, malformed calls, truncation, and transport errors
are never replayed by that rule. A second empty result is `upstream_empty_response`.
Text mode may correct a clearly attempted malformed action envelope only before
text/reasoning delivery. Neither path retries tool execution.

Structured first-hop errors retain safe `message`, `type`, and `code` across
Chat, Messages, and Responses, including non-streaming HTTP failures. The relay
limits error size, masks credentials, and does not forward headers, tracebacks,
or entire bodies. Schema/parse failures are not collapsed into an undiagnosable
generic stream error. Failed SSE has no successful terminal event.

## Validation and compatibility

- Every call must name a currently allowed tool and satisfy its JSON Schema.
- `none`, `required`, named tool choice, and `parallel_tool_calls=false` are enforced.
  Messages `disable_parallel_tool_use` maps to the same constraint.
- A bad member rejects the complete batch: no partial execution or dropping unknown tools.
- Duplicate JSON keys, non-finite numbers, duplicate IDs, multiple action envelopes,
  incomplete SSE/JSON, and output truncation are rejected.
- Schema references are local only; validation never fetches remote schemas.
- Missing OpenAI function `parameters` means a no-argument function. Explicit `{}`
  remains an open schema. Anthropic tools require `input_schema`.
- Function tools, custom **text** tools, and Responses namespaces are supported.
  Namespaces use `namespace.leaf` internally and return separate `namespace` / `name`.
  Custom tools use an upstream function wrapper with `{"input": string}` parameters.
- Custom grammar is not implemented and returns HTTP 400, rather than claiming
  grammar-constrained generation. Clients such as Codex that require grammar tools
  must use compatible declarations or a provider with the required native support.
- `previous_response_id`, `item_reference`, and server-side storage (`store=true`)
  are rejected. Send complete history on every request.
- Native history preserves call IDs and reasoning metadata where expressible in
  the Chat transport. Cross-provider encrypted reasoning is not interchangeable.
  Messages output still uses `genai-compat-no-signature`, not a genuine Anthropic
  signature. Responses output has no reconstructed encrypted reasoning state.

These checks are a transport boundary, not authorization or a guarantee that a
model's proposed file contents are correct. The agent must authorize/sandbox tools,
inspect actual results, and verify important writes with execution/read-back/hash.

## Run the two-hop setup

Start the current `shanghaitech-genai2api` build on loopback first. Then install
this independent repository in an isolated environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

Configure `.env` without committing credentials:

```dotenv
UPSTREAM_BASE_URL=http://127.0.0.1:31100
UPSTREAM_TOOL_MODE=native
UPSTREAM_API_KEY=replace-with-first-hop-key
RELAY_API_KEY=replace-with-a-different-downstream-key
HOST=127.0.0.1
PORT=31110
```

Run `genai2agent-relay` or `python main.py`. Client API bases are
`http://127.0.0.1:31110/v1` for Chat/Responses and
`http://127.0.0.1:31110` for Messages.

For Claude Code configuration see
[the adapter reference](docs/claude-code-adapter-reference.zh-CN.md).

## 当前上游范围

本阶段因无法申请独立 API Key，明确放弃学校独立官方 API 路径，不再提供远程
base 或绕过网页第一跳的接入方案。当前部署固定使用本地第一跳：
`UPSTREAM_BASE_URL=http://127.0.0.1:31100`，并保持 `UPSTREAM_TOOL_MODE=native`。

这不取消认证：学校网页登录 JWT／账号认证仍由第一跳管理；
`UPSTREAM_API_KEY` 是第一跳的本地代理 Key，`RELAY_API_KEY` 是客户端访问
relay 的本地 Key，二者不是学校独立 API Key。不要混用或向不相关服务发送凭据。

## API surface and first-hop features

- `GET /health`, `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/messages`, `POST /v1/messages/count_tokens`
- `POST /v1/responses`, `POST /v1/responses/input_tokens`

Token counts are local character-based estimates. Image/document blocks are
preserved rather than flattened into base64 prompt text. For the website first
hop, thinking settings map to a boolean `thinking` switch and search declarations
map to `web_search`; precise effort/budget and native hosted search lifecycles are
not emulated. These website extensions are not universal provider API features.
Historical Pi/attachment evidence is in
[feature pass-through](docs/genai2api-feature-passthrough.zh-CN.md); it is not a
claim that every attachment combination was revalidated in this refactor.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `UPSTREAM_BASE_URL` | `http://127.0.0.1:31100` | 本地第一跳地址；当前两跳链路必需 |
| `UPSTREAM_TOOL_MODE` | `native` | `native` or explicit `text` fallback |
| `UPSTREAM_API_KEY` | unset | 第一跳的本地 Bearer Key，与第一跳配置一致 |
| `RELAY_API_KEY` | unset | Downstream Bearer or `x-api-key` |
| `HOST` | `127.0.0.1` | Listen address |
| `PORT` | `31110` | Listen port |
| `UPSTREAM_CONNECT_TIMEOUT` | `10` | Connect timeout, seconds |
| `UPSTREAM_READ_TIMEOUT` | `1800` | Socket read timeout, seconds |
| `UPSTREAM_ACTION_RETRIES` | `2` | Text-mode serialization retries before delivery; native mode ignores it |
| `MAX_ACTION_BYTES` | `1048576` | Decoded response payload budget; also bounds one incomplete SSE event |

The payload budget counts text, reasoning, tool names, IDs, and arguments while
reading. Repeated SSE metadata and heartbeats do not consume the payload budget.
Protect non-loopback deployments with TLS, authentication, and network policy.

## Embedded engine

`relay.engine.ActionRelay` accepts a backend implementing
`stream(TextCompletionRequest) -> Iterator[RelayEvent]`. Backend events are
`text`, `reasoning`, `ping`, and a final `reply`; the engine emits a validated
`result`. `run()` consumes that same stream for non-streaming callers. Iterators
must close their upstream resources on cancellation. The old `TextActionRelay`
complete-only interface has been removed rather than retained as a shim.

## Verification and license

Run `python -m pytest tests -q` in an isolated environment with pytest installed.
The parent workspace's pixi Python can also run these tests from this directory.
Existing action-envelope fix notes describe the pre-refactor implementation;
this README is the current contract.

Both this repository and `shanghaitech-genai2api` use the MIT License.
