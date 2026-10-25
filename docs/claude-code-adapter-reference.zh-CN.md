# Claude Code 适配流程参考

中继提供 Anthropic Messages API，Claude Code 可直接使用 `/v1/messages`。
默认链路为 Claude Code → relay（结构化 Chat 工具/SSE）→ 第一跳 → GenAI 网页。
工具真正由 Claude Code 执行，并受客户端自己的权限策略控制。

## 启动

先启动当前版本的第一跳，再在 relay 的隔离环境中启动中继。两个仓库需一起升级。

第一跳的空继续与时限在根项目配置，不要写入下面 relay 的 `.env`：

| 第一跳环境变量 | 启动参数 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `GENAI_EMPTY_CONTINUATIONS` | `--empty-continuations` | `3` | 额外次数，整数 `0..10`；含首次最多 4 次，0 仅禁用额外继续 |
| `GENAI_TOOL_REQUEST_TIMEOUT_SECONDS` | `--tool-request-timeout` | `600` | 首次与所有继续共享的生成窗口（秒），有限正数 |

第一跳 `Config` 拒绝布尔值配置。在根项目启动第一跳的示例：

```bash
pixi run serve --empty-continuations 3 --tool-request-timeout 600
```

随后在 relay 目录配置：

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
Messages 的 thinking signature 仍为 `genai-compat-no-signature` 兼容占位，不是
Anthropic 签名；保留可表达的历史元数据不表示网页消费它们，也不能恢复跨供应商
加密 reasoning 状态。Responses 不重建加密思考状态。

### 空完成恢复与失败边界

默认 native 模式只由第一跳处理严格空完成，relay 不叠加生成重试，避免预算相乘。
仅启用工具协议恢复（`recover_empty=True`）且**零正文、零思考、零调用、正常
`stop`**时可继续。默认额外 3 次、含首次最多 4 次；仍全部为空则返回
`upstream_empty_response`。每次以原 `chatInfo` 加真实“第 k/N 次”标记及明确继续指令，
完整原历史不变，不累积空 assistant 或旧继续提示，不换模型、不降级 text，
不重放客户端工具。已产生正文/思考、坏 JSON/Schema、解析错误、断流或截断不重放。

所有工具协议请求从首次上游生成起共享默认 600 秒 deadline，不含附件预处理；
ping/心跳、下一次继续都不续期，额外次数设为 0 也仍有总时限。到期即报
`upstream_request_timeout`，即使已有输出或正在缓冲调用，也不交付未校验调用。
普通无工具请求不空继续，也不施加这个工具请求总时限。

第一跳网络 connect/read timeout 为 10/300 秒，relay 上游 read timeout 默认为
1800 秒，都是网络等待约束，不是共享生成窗口；持续收到 ping 不代表永不超时。
整场 Claude Code 任务可能跨多个请求和本地工具执行，也不等于该单请求窗口。
次数或时间耗尽仍会中断当前 HTTP；代理报告真实失败而不伪造成功，客户端可根据
完整历史、真实工具结果及错误显式继续。增加预算只覆盖未交付内容的空完成，
并不保证学校平台不再空返或整场任务永不中断。完整 decision 图见
[阶段说明第四节](../../STAGE_NATIVE_FIRST.md#4-sse-生命周期终态与取消)。

## 文本模式与当前上游范围

只有确实使用纯文本后端时，才显式设置 `UPSTREAM_TOOL_MODE=text`。该模式将
system/developer 转为普通上下文、工具定义追加到当前轮，并要求最终 action
封套；保留调用 ID，不能再靠结果顺序猜测归属。native 失败不会自动降级或重放。

本阶段因无法申请独立 API Key，明确放弃学校独立官方 API 路径，不使用远程
base 或跳过第一跳的直连方案。`UPSTREAM_BASE_URL=http://127.0.0.1:31100`
仍须保留，默认 `native` 不变。学校网页登录认证由第一跳负责；
`UPSTREAM_API_KEY` 与 `RELAY_API_KEY` 均为本地代理鉴权，不是学校独立 API Key。

完整链路及重构差异见根项目的 [原生优先阶段说明](../../STAGE_NATIVE_FIRST.md)。
阶段短样本只能说明当时的协议行为，不能代替正式服务上的长时实际工作验收。

附件仍由第一跳逐请求处理，不提供 `/v1/files`、持久 `file_id` 或附件缓存。
Responses 的 `previous_response_id`、`item_reference`、`store=true` 不支持；
每轮发送完整历史。custom text 工具可包装为函数，custom grammar 则明确拒绝，
不能把 Schema 验收称为生成端 grammar constrained decoding。

## 排错

- `401`：分别检查 relay Key 和上游 Key，不要混用。
- 必需工具缺失、参数不符合 Schema、并行禁用却返回多调用：会明确失败，不会删除坏成员后执行其余操作。
- `finish_reason=length` 或提前断流：不会交付部分调用，调整输出预算后由客户端重新发起完整请求。
- `400`：检查不支持的工具类型、custom grammar、状态引用或缺失的 input_schema。
- 首事件正常但等待工具较久：观察心跳；长参数仍需等完整校验，ping 不续期。不能靠提前执行参数片段缩短等待。
- `upstream_empty_response`：严格空完成的额外次数已耗尽，或没有可交付答案；不是工具已执行或任务成功。
- `upstream_request_timeout`：共享生成窗口已到期，不是整场任务已经完成；检查真实结果后再由客户端显式继续，不能让 relay native 暗中重试。
- 客户端取消或总时限到期：关闭已取得的实际 HTTP 响应/socket；尚未收到上游响应头的阻塞连接不能立即打断，主响应仍按总时限失败，worker 返回后清理且不再发新继续，不能保证立即停止学校远端推理。

Chat、Messages、Responses 共用同一份白名单、参数校验与原子批次约束。
Token-count 接口仍是本地字符估算，不额外请求模型。
