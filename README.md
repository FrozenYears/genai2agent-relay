# GenAI2AgentRelay

GenAI2AgentRelay converts native function-tool requests into a validated
plain-text action transport. It is designed as a second hop behind
[`jollyxenon/shanghaitech-genai2api`](https://github.com/jollyxenon/shanghaitech-genai2api),
or any other upstream that accepts ordinary OpenAI-style chat messages but
cannot reliably return native tool calls.

The built-in HTTP adapter exposes OpenAI Chat Completions, Anthropic Messages,
and OpenAI Responses. The conversion engine itself has no Flask, SSE, or
coding-agent dependency.

## How it works

```text
OpenAI-compatible client
  POST /v1/chat/completions with messages + tools
               |
               v
GenAI2AgentRelay
  1. removes native tool and system-role channels
  2. renders tool schemas into ordinary conversation text
  3. encodes earlier calls/results as text envelopes
               |
               v
Plain-text chat completion backend
  jollyxenon/shanghaitech-genai2api by default
               |
               v
Complete @@ACTION@@ JSON envelope
  buffered -> parsed -> allowlisted -> JSON-Schema validated
               |
               v
OpenAI-compatible tool_calls response
```

The upstream model requests an operation using ordinary text:

```text
@@ACTION@@{"calls":[{"operation":"read_file","parameters":{"path":"README.md"}}]}@@END_ACTION@@
```

Tool execution remains entirely in the downstream client. This service never
executes a command, reads a requested file, or invokes a submitted tool.

## Safety properties

- Native `tools` and `system` roles are never sent to the first hop.
- Only a complete, final action envelope is considered a call.
- Closed code quotations of the action envelope remain documentation, not executable actions. However, without a normal action envelope, JSON call arrays/objects in code fences or `<parameter name="calls">` blocks referencing current tools trigger correction before delivery. Such examples may also trigger correction intentionally. Alternate formats are never executed directly; ordinary data and valid action parameters remain unchanged.
- Each operation must occur in the current request's allowlist.
- Parameters are validated against the tool's supplied JSON Schema.
- Partial, oversized, malformed, unknown, and native-token calls are rejected.
- Invalid model serialization can be retried before any call is returned.
- Outside closed code quotations, `@@ACTION@` and an end marker before a valid opening marker trigger serialization retry instead of plain-text completion. Delimiters are never auto-repaired for execution.
- Request bodies, arguments, and credentials are not logged.

This is a transport boundary, not a permission system. The downstream client
must still authorize and sandbox tool execution.

JSON syntax retries include the parse reason and location within the envelope body; their client-facing errors are unchanged. Empty or reasoning-only replies request continuation using the same retry budget instead of ending the turn successfully; exhaustion returns an upstream error. Normal text replies remain valid without tools. See [封套与纯思考回复小修记录](docs/action-envelope-fixes.zh-CN.md) for comparisons, verification, and rollback.

The full current tool schemas are appended near the latest user turn instead of the start of history; original instructions, history, and attachments are retained. Structural/parameter retries include actionable feedback and the relevant tool schema when identifiable, without translating arguments automatically. An upstream `finish_reason=length` is rejected immediately with an explicit truncation error, even if a complete-looking call is present: no same-budget serialization retry or partial execution. No additional logging, dependencies, or retry allowance is introduced.
Empty-response retries append the continuation instruction to the current user turn instead of creating consecutive user messages; reasoning is not copied as assistant text. This reduces retry-shape drift for long contexts but does not bypass upstream context or output limits.

## Run the two-hop setup

First start `jollyxenon/shanghaitech-genai2api` according to its own README. It
must expose `POST /v1/chat/completions`; `GET /v1/models` is used for model-list
passthrough. A typical first-hop configuration listens only on loopback:

```dotenv
GENAI_TOKEN=replace-with-your-login-token
HOST=127.0.0.1
PORT=31100
API_FORMAT=openai
API_KEY=replace-with-a-random-first-hop-key
```

Install and start this second hop:

```bash
git clone https://github.com/negativegluon/genai2agent-relay.git
cd genai2agent-relay
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

Configure `.env`:

```dotenv
UPSTREAM_BASE_URL=http://127.0.0.1:31100
UPSTREAM_API_KEY=replace-with-a-random-first-hop-key
RELAY_API_KEY=replace-with-a-different-downstream-key
HOST=127.0.0.1
PORT=31110
```

Then run:

```bash
genai2agent-relay
```

`python main.py` is equivalent when run from the repository root.

## Send a tool request

Point any OpenAI Chat Completions client at `http://127.0.0.1:31110/v1`.
For example:

```bash
curl http://127.0.0.1:31110/v1/chat/completions \
  -H 'Authorization: Bearer replace-with-a-downstream-key' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "chatglm",
    "messages": [{"role": "user", "content": "Read README.md"}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "read_file",
        "description": "Read one text file",
        "parameters": {
          "type": "object",
          "properties": {"path": {"type": "string"}},
          "required": ["path"],
          "additionalProperties": false
        }
      }
    }]
  }'
```

The response uses the normal `choices[0].message.tool_calls` shape. Historical
assistant calls and `role: tool` results are translated back into text before
the next upstream turn. `tool_choice` values `auto`, `none`, `required`, and a
named function are supported.

Streaming requests on every protocol are accepted, but deliberately buffered:
the relay waits for and validates the complete upstream response before
emitting downstream SSE chunks. This increases time to first event and prevents
a partial envelope from becoming executable.

## API surface

- `GET /health`
- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/messages`
- `POST /v1/messages/count_tokens`
- `POST /v1/responses`
- `POST /v1/responses/input_tokens`

All three protocols share one conversion engine and one tool allowlist. They
differ only in how requests and results are mapped to and from the text action
transport:

| Protocol | Tool definition | Tool request | Tool result history |
| --- | --- | --- | --- |
| Chat Completions | `tools[].function.parameters` | `message.tool_calls` | `role: tool` |
| Messages | `tools[].input_schema` | `content[].tool_use` | `content[].tool_result` |
| Responses | `tools[].parameters` | `output[].function_call` | `function_call_output` |

`system` and `instructions` are merged into ordinary user context; a system
role is never sent to the first hop. Anthropic `tool_choice` values `auto`,
`any`, `none`, and `{type: tool, name}` map onto the same internal choices as
their Chat Completions equivalents.

The two token-counting endpoints return a local character-based estimate. This
transport never performs an extra model round trip just to count tokens.

Streaming is accepted on all three protocols but deliberately buffered: the
relay waits for and validates the complete upstream response before emitting
events. Anthropic callers receive `message_start`/`content_block_*`/
`message_delta`/`message_stop`; Responses callers receive
`response.created`/`response.output_item.*`/`response.completed`.

For a short, operational Claude Code setup, see
[Claude Code adapter reference](docs/claude-code-adapter-reference.zh-CN.md).

## First-hop capability pass-through

Recent `shanghaitech-genai2api` builds expose image and document attachments, a
thinking switch, and platform web search. The relay carries those capabilities
through unchanged:

- Client thinking controls collapse to one boolean `thinking` field; levels are
  not forwarded as unknown strings.
- `web_search: true`, `web_search_options`, or a declared `web_search*` tool sets
  the upstream `web_search` boolean. Search declarations are never treated as
  locally executable functions.
- Image blocks (`image_url` / `input_image` / Anthropic `image`) and document
  blocks (`file` / `input_file` / Anthropic `document`) keep their attachments
  instead of being flattened into base64 prompt text. Image URLs, including
  data URLs, are forwarded as-is: current first-hop builds hand them to the
  platform directly without an upload step, while documents are still uploaded
  by the first hop.
- Chain-of-thought is returned where each protocol expects it: Chat
  `reasoning_content`, Anthropic `thinking` blocks, Responses `reasoning` items.

Details, field mapping, and the Pi Agent validation results are in
[GenAI2API feature pass-through](docs/genai2api-feature-passthrough.zh-CN.md).

## Protocol-neutral engine

The reusable API lives in `relay.engine`:

```python
from relay.engine import (
    RelayRequest,
    TextActionRelay,
    TextCompletionRequest,
    TextMessage,
    UpstreamReply,
)


class MyBackend:
    def complete(self, request: TextCompletionRequest) -> UpstreamReply:
        # Adapt request.messages to any plain-text completion service.
        return UpstreamReply(content="ordinary model text")


relay = TextActionRelay(MyBackend(), retries=1, max_action_bytes=1_048_576)
result = relay.run(RelayRequest(
    model="my-model",
    messages=(TextMessage(role="user", content="hello"),),
))
```

`TextCompletionBackend` is a structural protocol: another backend only needs a
`complete(TextCompletionRequest) -> UpstreamReply` method. Downstream adapters can map
their native tool definitions to `ToolSpec`, then map `RelayResult.action.calls`
back to their own response format.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `UPSTREAM_BASE_URL` | `http://127.0.0.1:31100` | First-hop base URL |
| `UPSTREAM_API_KEY` | unset | Optional first-hop Bearer key |
| `RELAY_API_KEY` | unset | Optional downstream Bearer or `x-api-key` key |
| `HOST` | `127.0.0.1` | Listen address |
| `PORT` | `31110` | Listen port |
| `UPSTREAM_CONNECT_TIMEOUT` | `10` | Connection timeout in seconds |
| `UPSTREAM_READ_TIMEOUT` | `1800` | Completion timeout in seconds |
| `UPSTREAM_ACTION_RETRIES` | `2` | Shared retries after invalid serialization or empty replies; at most 3 attempts total. Length truncation is not retried. |
| `MAX_ACTION_BYTES` | `1048576` | Maximum buffered response size |

Keep both hops on loopback unless remote access is protected with TLS,
authentication, and network policy. `.env` is ignored by Git.


## Relationship to the upstream project

This repository is an independent second-hop adapter, not a fork. It consumes
only the public HTTP interface of `jollyxenon/shanghaitech-genai2api`. The text
action technique was extracted from a locally validated relay and generalized
without project paths, accounts, credentials, or deployment-specific logic.

Both projects use the MIT License.
