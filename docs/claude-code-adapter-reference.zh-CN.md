# Claude Code 适配流程参考

本项目已经内置 Anthropic Messages API。Claude Code 可以直接指向本中继的
`/v1/messages`，不再需要额外的 LiteLLM 协议转换层。

推荐链路：

```text
Claude Code :31110
  -> GenAI2AgentRelay（Anthropic Messages -> 文本 action）
  -> shanghaitech-genai2api :31100
  -> GenAI
```

## 最短操作步骤

1. 先启动 `shanghaitech-genai2api`，确认端口 `31100` 可用。

2. 启动本中继：

```bash
cp .env.example .env
# 在 .env 中设置 UPSTREAM_BASE_URL、UPSTREAM_API_KEY、RELAY_API_KEY
genai2agent-relay
curl http://127.0.0.1:31110/health
```

3. 在另一个终端启动 Claude Code，直接指向本中继：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:31110
export ANTHROPIC_AUTH_TOKEN='replace-with-the-relay-api-key'
export ANTHROPIC_MODEL=chatglm
claude
```

`ANTHROPIC_AUTH_TOKEN` 的值就是本中继的 `RELAY_API_KEY`。如果 `.env` 里没有
设置 `RELAY_API_KEY`，中继不做鉴权，此时该变量可以随便填一个非空值。

## 各层职责

| 层 | 只负责什么 |
| --- | --- |
| Claude Code | 真正执行工具，并应用自身权限策略 |
| GenAI2AgentRelay | Messages / Responses / Chat 三种协议与严格文本 action envelope 互转和校验 |
| shanghaitech-genai2api | 登录 GenAI，并提供普通 Chat Completions HTTP 接口 |

本中继会缓冲完整模型回复，确认 action envelope 闭合且参数符合 JSON Schema 后才
返回 `tool_use`。因此 Claude Code 首个流式事件会较晚出现，这是预期行为。

## 协议映射细节

中继把 Messages 请求按下面的方式翻译成内部的文本 action 传输：

| Anthropic Messages | 中继内部 |
| --- | --- |
| `system`（字符串或 block 数组） | 合并到首条普通用户上下文，不向上游发送 system role |
| `tools[].name/input_schema` | `ToolSpec(name, parameters)` |
| 上下文里的 assistant `tool_use` | `encode_calls(...)` 后作为 assistant 文本历史 |
| 上下文里的 user `tool_result` | `encode_result(...)` 后作为 user 文本历史 |
| `tool_choice: {type: any}` | `required` |
| `tool_choice: {type: tool, name}` | 指定名称的工具 |
| 模型输出 | `RelayRequest` |
| `RelayResult.action.calls` | assistant `tool_use` blocks |

流式适配器也先调用 `TextActionRelay.run()` 得到完整结果，再按 Anthropic 顺序输出
`message_start`、`content_block_start`、`content_block_delta`、`content_block_stop`、
`message_delta`、`message_stop`。不会边接收上游文本边输出 `tool_use`，否则截断的
JSON 可能被误执行。

## 另外两个协议入口

同一套 action 逻辑同时暴露给 OpenAI Chat Completions 和 OpenAI Responses：

- `POST /v1/chat/completions`：`tools[].function.parameters` / `message.tool_calls` / `role: tool`
- `POST /v1/responses`：`tools[].parameters` / `output[].function_call` / `function_call_output`
- `POST /v1/messages/count_tokens` 与 `POST /v1/responses/input_tokens`：按字符数本地估算，不额外请求模型

三种协议共用同一份工具白名单与 JSON Schema 校验，任何一个协议都不会放宽校验。

## 快速排错

- `404 /v1/messages`：确认中继版本已包含 Messages 适配层，并且 Claude Code 连的是
  本中继端口（默认 `31110`）。
- `401`：分别检查中继 `RELAY_API_KEY` 与第一跳 `API_KEY`。
- 普通回答正常但没有工具：确认请求仍含 `tools`，且模型最终输出了闭合的
  `@@ACTION@@...@@END_ACTION@@`。
- action 被截断：适当提高请求 `max_tokens`；过大的完整响应还需要提高
  `MAX_ACTION_BYTES`。
- 首事件等待时间长：这是完整缓冲校验的代价，不代表请求卡死。
