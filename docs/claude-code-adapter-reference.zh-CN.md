# Claude Code 适配流程参考

本项目不内置 Anthropic Messages API。Claude Code 需要一个独立的薄协议适配层，
把 `/v1/messages` 转成中继已经支持的 `/v1/chat/completions`。

推荐链路：

```text
Claude Code :31120
  -> LiteLLM（Anthropic Messages 转 OpenAI Chat）
  -> GenAI2AgentRelay :31110（tool call 转文本 action）
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

3. 在单独虚拟环境安装并启动 LiteLLM：

```bash
python3 -m venv .venv-litellm
. .venv-litellm/bin/activate
python -m pip install 'litellm[proxy]'

export RELAY_API_KEY='replace-with-the-key-used-by-this-relay'
export LITELLM_MASTER_KEY='replace-with-another-local-key'
litellm --config examples/litellm-claude-code.yaml --host 127.0.0.1 --port 31120
```

4. 在另一个终端启动 Claude Code：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:31120
export ANTHROPIC_AUTH_TOKEN='replace-with-the-litellm-master-key'
export ANTHROPIC_MODEL=chatglm
claude
```

不要把 Claude Code 直接指向 `31110`。本中继没有 `/v1/messages`；`31120` 才是
Claude Code 的 Anthropic 协议入口。

## 各层职责

| 层 | 只负责什么 |
| --- | --- |
| LiteLLM | Anthropic `messages/tool_use/tool_result` 与 OpenAI Chat 格式互转 |
| GenAI2AgentRelay | `tools/tool_calls` 与严格文本 action envelope 互转和校验 |
| shanghaitech-genai2api | 登录 GenAI，并提供普通 Chat Completions HTTP 接口 |
| Claude Code | 真正执行工具，并应用自身权限策略 |

本中继会缓冲完整模型回复，确认 action envelope 闭合且参数符合 JSON Schema 后才
返回 tool call。因此 Claude Code 首个流式事件会较晚出现，这是预期行为。

## 自己实现适配层时

不希望引入 LiteLLM 时，只需要实现以下薄映射，action 逻辑无需复制：

| Anthropic Messages | 中继核心 |
| --- | --- |
| `system` | 合并到首条普通用户上下文，不向上游发送 system role |
| `tools[].name/input_schema` | `ToolSpec(name, parameters)` |
| assistant `tool_use` | `encode_calls(...)` 后作为 assistant 文本历史 |
| user `tool_result` | `encode_result(...)` 后作为 user 文本历史 |
| 当前请求 | `RelayRequest` |
| `RelayResult.action.calls` | assistant `tool_use` blocks |

调用入口是：

```python
from relay.engine import RelayRequest, TextActionRelay, TextMessage
from relay.actions import ToolSpec, encode_calls, encode_result
```

流式适配器也必须先调用 `TextActionRelay.run()` 得到完整结果，再按 Anthropic 顺序
输出 `message_start`、`content_block_*`、`message_delta`、`message_stop`。不要边接收
上游文本边输出 `tool_use`，否则截断的 JSON 可能被误执行。

## 快速排错

- `404 /v1/messages`：Claude Code 错连到 `31110`，应连接协议适配层 `31120`。
- `401`：分别检查 LiteLLM master key、中继 `RELAY_API_KEY`、第一跳 `API_KEY`。
- 普通回答正常但没有工具：确认 LiteLLM 转发后的请求仍含 `tools`，且模型最终输出
  了闭合的 `@@ACTION@@...@@END_ACTION@@`。
- action 被截断：适当提高请求 `max_tokens`；过大的完整响应还需要提高
  `MAX_ACTION_BYTES`。
- 首事件等待时间长：这是完整缓冲校验的代价，不代表请求卡死。
