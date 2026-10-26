# Claude Code 适配流程参考

中继提供 Anthropic Messages API，Claude Code 可直接使用 `/v1/messages`。
默认链路为 Claude Code → relay（结构化 Chat 工具/SSE）→ 第一跳 → GenAI 网页。
工具真正由 Claude Code 执行，并受客户端自己的权限策略控制。

## 启动

先启动当前版本的第一跳，再在 relay 的隔离环境中启动中继。两个仓库需一起升级。

第一跳恢复预算与时限在根项目配置，不要写入下面 relay 的 `.env`：

| 第一跳环境变量 | 启动参数 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `GENAI_EMPTY_CONTINUATIONS` | `--empty-continuations` | `8` | 空完成额外次数，整数 `0..30` |
| `GENAI_TOOL_REPAIR_ATTEMPTS` | `--tool-repair-attempts` | `3` | 未交付坏调用额外纠错，整数 `0..10` |
| `GENAI_TOOL_REQUEST_TIMEOUT_SECONDS` | `--tool-request-timeout` | `900` | 首次、空继续和纠错共享秒数，有限正数 |

第一跳 `Config` 拒绝布尔值配置。在根项目启动第一跳的示例：

```bash
pixi run serve --empty-continuations 8 --tool-repair-attempts 3 --tool-request-timeout 900
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

### 自动恢复与失败边界

默认native只在第一跳恢复，relay不叠加生成重试。严格空stop（零原始正文/思考/调用）默认额外继续8次；正常结束但显式协议、JSON、Schema或选择约束不合法，且本代未交付时，默认由模型纠错3次。有纠错预算时，第一跳暂存本代正文、思考与调用，校验通过后才交付；SSE起始与心跳即时发送，但正文显示会延后。无工具或关闭工具纠错时保留实时增量。代理不补参数、不修括号、不放行部分批次。

原`chatInfo`加真实次数和安全反馈，完整原历史与约束不变，不积累虚构assistant/tool结果、不换模型、不降级text、不重放客户端工具。两类预算独立累计、不重置，默认最多12代。已交付内容、网络错误、断流、截断、取消或到期不重放；`auto`下的普通计划仍可正常结束，`required/指定工具`只返回计划属于选择约束错误。失败不发成功终态，空耗尽为`upstream_empty_response`，纠错耗尽保留原错误码。

所有工具协议生成共用默认 900 秒 deadline，不含附件预处理；ping/心跳、新一代和恢复类别切换不续期。到期为 `upstream_request_timeout`，不交付未校验调用。0 只关闭对应次数；普通无工具请求不使用这些预算/deadline。第一跳网络 connect/read 为 10/300 秒，relay read 默认1800秒，与该累计窗口及整场任务预算不同。

代理不知道业务是否完成。根项目可选 omp 监督器使用独立真实验收，在协议失败、验收失败或停滞后自动反馈继续，默认额外8次、整场45分钟；单HTTP耗尽不必立即等待人工。它只用于显式启动的 omp 工作流，不自动改变 Claude Code 的工作循环。任务总预算耗尽或安全故障会保存检查点并明确停止，不无限重试、不伪报成功。当前没有跨进程自动恢复。

完整状态图和安全边界见[架构第四节](https://github.com/FrozenYears/shanghaitech-genai2api/blob/main/docs/STAGE_NATIVE_FIRST.md#4-sse-生命周期终态与取消)；监督器用法见[根 README](https://github.com/FrozenYears/shanghaitech-genai2api/blob/main/README.md)。

## 文本模式与当前上游范围

只有确实使用纯文本后端时，才显式设置 `UPSTREAM_TOOL_MODE=text`。该模式将
system/developer 转为普通上下文、工具定义追加到当前轮，并要求最终 action
封套；保留调用 ID，不能再靠结果顺序猜测归属。native 失败不会自动降级或重放。

本阶段因无法申请独立 API Key，明确放弃学校独立官方 API 路径，不使用远程
base 或跳过第一跳的直连方案。`UPSTREAM_BASE_URL=http://127.0.0.1:31100`
仍须保留，默认 `native` 不变。学校网页登录认证由第一跳负责；
`UPSTREAM_API_KEY` 与 `RELAY_API_KEY` 均为本地代理鉴权，不是学校独立 API Key。

完整链路及重构差异见根项目的 [原生优先阶段说明](https://github.com/FrozenYears/shanghaitech-genai2api/blob/main/docs/STAGE_NATIVE_FIRST.md)。
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
