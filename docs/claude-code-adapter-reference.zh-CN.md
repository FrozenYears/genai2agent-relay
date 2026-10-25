# Claude Code 适配流程参考

中继提供 Anthropic Messages API，Claude Code 可直接使用 `/v1/messages`。
默认链路为 Claude Code → relay（结构化 Chat 工具/SSE）→ 第一跳 → GenAI 网页。
工具真正由 Claude Code 执行，并受客户端自己的权限策略控制。

## 启动

先启动当前版本的第一跳，再在 relay 的隔离环境中启动中继。两个仓库需一起升级。

```dotenv
UPSTREAM_BASE_URL=http://127.0.0.1:31100
UPSTREAM_TOOL_MODE=native
UPSTREAM_API_KEY=replace-with-first-hop-key
RELAY_API_KEY=replace-with-a-different-relay-key
HOST=127.0.0.1
PORT=31110
```

```bash
genai2agent-relay
```

在另一终端配置 Claude Code：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:31110
export ANTHROPIC_AUTH_TOKEN='replace-with-relay-key'
export ANTHROPIC_MODEL=deepseek-pro
claude
```

模型名称必须是实际后端的模型 ID。未配置 `RELAY_API_KEY` 时中继不做鉴权，
不应将这样的服务暴露到非受信网络。

## 当前协议映射

| Messages 输入 | 默认 native 模式的上游表达 |
| --- | --- |
| `system` | 保留为系统上下文 |
| `tools[].input_schema` | Chat function parameters |
| assistant `tool_use` | 带原 ID 的 assistant tool_calls |
| user `tool_result` | 按 tool_use_id 关联的 tool 消息，保留错误标志和附件 |
| `tool_choice: {type: any}` | required |
| `tool_choice: {type: tool, name}` | 指定工具 |
| `disable_parallel_tool_use: true` | parallel_tool_calls=false |

首个 `message_start` 不再等待模型完成；正文和独立思考持续产生增量，
上游心跳转换为 `ping`。工具参数必须完整闭合、整批通过 Schema/choice 校验后
才转换为 `tool_use`。上游失败产生 `error`，不发送成功 `message_stop`。

这里的 native 指中继与第一跳之间使用结构化工具接口，不能据此声称学校网页
已经开放原生函数输入。第一跳仍负责网页所需的文本提示及严格原生标记解析。
Messages 的 thinking signature 仍是兼容占位，不是 Anthropic 签名。

## 文本后端与独立 API

只有确实使用纯文本后端时，才显式设置 `UPSTREAM_TOOL_MODE=text`。该模式将
system/developer 转为普通上下文、工具定义追加到当前轮，并要求最终 action
封套；保留调用 ID，不能再靠结果顺序猜测归属。native 失败不会自动降级或重放。

若已获得学校独立 API 的授权，可将 `UPSTREAM_BASE_URL` 和 `UPSTREAM_API_KEY`
改为获批 API base 与专属 Key，跳过网页第一跳。公开接入指南及边界见 README；
网页登录 JWT、学号密码不能作为该 API 的 Key。

## 排错

- `401`：分别检查 relay Key 和上游 Key，不要混用。
- 必需工具缺失、参数不符合 Schema、并行禁用却返回多调用：会明确失败，不会删除坏成员后执行其余操作。
- `finish_reason=length` 或提前断流：不会交付部分调用，调整输出预算后由客户端重新发起完整请求。
- `400`：检查不支持的工具类型、custom grammar、状态引用或缺失的 input_schema。
- 首事件正常但等待工具较久：观察心跳；长参数仍需等完整校验。不能靠提前执行参数片段缩短等待。
- 客户端取消：关闭消费的流即可传播关闭上游资源；尚未收到上游响应头时，清理仍受请求返回/超时限制。

Chat、Messages、Responses 共用同一份白名单、参数校验与原子批次约束。
Token-count 接口仍是本地字符估算，不额外请求模型。
