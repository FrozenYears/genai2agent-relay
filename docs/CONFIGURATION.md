# 详细配置与兼容说明

基础启动见[根 README](../README.md)。以下命令在 relay 仓库根目录运行；首跳的网页凭据只配置在首跳。

## 安装与运行

环境要求：Python 3.10 或更高版本、Git、uv。以下命令将依赖安装到本仓库的 `.venv`，不修改系统 Python。环境用法见 [uv 文档](https://docs.astral.sh/uv/pip/environments/)。

先按第一跳 README 启动当前分支的第一跳，再启动 relay。第一跳当前绑定 `0.0.0.0:31100`，不能视为只对本机开放；请设置第一跳本地 API Key，并用防火墙限制到受信网络。下面的 relay 仍绑定 `127.0.0.1`。这些开发启动命令不构成面向公网的生产 WSGI 部署保证。

### WSL / Linux

将独立仓库克隆至 Linux 文件系统：

```bash
git clone --branch main https://github.com/FrozenYears/genai2agent-relay.git
cd genai2agent-relay
uv venv .venv
uv pip install --python .venv/bin/python -e .
cp .env.example .env
```

编辑 `.env` 后，在当前目录下启动：

```bash
.venv/bin/genai2agent-relay
# 等价入口：.venv/bin/python main.py
```

### Windows PowerShell

```powershell
git clone --branch main https://github.com/FrozenYears/genai2agent-relay.git
Set-Location genai2agent-relay
uv venv .venv
uv pip install --python .venv\Scripts\python.exe -e .
Copy-Item .env.example .env
```

编辑 `.env` 后直接启动，无需激活虚拟环境或修改执行策略：

```powershell
.\.venv\Scripts\genai2agent-relay.exe
# 等价入口：.\.venv\Scripts\python.exe main.py
```

如果在父工作区中操作，先进入其 `genai2agent-relay` 子目录。Windows 的 `D:\shanghaitech-genai2api\genai2agent-relay` 在 WSL 中通常对应 `/mnt/d/shanghaitech-genai2api/genai2agent-relay`；WSL 与 Windows 的 `.venv/bin`、`.venv\Scripts` 不可混用，建议分开检出并各建环境。示例假设两跳能通过同一环境的 `127.0.0.1` 互访；不要直接把跨 WSL/Windows 的回环互通当作保证。

## 鉴权与 `.env`

注意区分三层不同的凭据边界：

| 凭据 | 配置项 | 用途 |
| --- | --- | --- |
| 学校统一身份认证账号密码或网页 JWT | 第一跳 `GENAI_TOKEN` | 第一跳管理网页登录凭据；绝不能用作 relay 客户端凭据 |
| 第一跳本地代理 Key | 第一跳 `API_KEY`；relay `UPSTREAM_API_KEY` | relay 访问第一跳的本地鉴权；两处配置必须完全一致 |
| relay 本地 Key | relay `RELAY_API_KEY`；客户端 API Key | 客户端访问 relay 的本地鉴权；建议与第一跳 Key 设为不同值 |

第一跳仓库的 `.env` 配置示例：

```dotenv
GENAI_TOKEN=student_id@password
API_KEY=replace-with-first-hop-local-key
PORT=31100
```

本仓库 relay 的 `.env` 配置：

```dotenv
UPSTREAM_BASE_URL=http://127.0.0.1:31100
UPSTREAM_TOOL_MODE=native
UPSTREAM_API_KEY=replace-with-first-hop-local-key
RELAY_API_KEY=replace-with-relay-local-key
HOST=127.0.0.1
PORT=31110
UPSTREAM_CONNECT_TIMEOUT=10
UPSTREAM_READ_TIMEOUT=1800
UPSTREAM_ACTION_RETRIES=2
MAX_ACTION_BYTES=1048576
```

将示例值替换为实际值，切勿提交 `.env`。这里的两处本地 Key 均非学校官方 API Key。relay 接受 `Authorization: Bearer <RELAY_API_KEY>` 或 `x-api-key: <RELAY_API_KEY>`。若未配置 `RELAY_API_KEY`，下游请求将不受鉴权保护；`/health` 始终免鉴权。不要向无关服务发送学校账号凭据或本地 Key。非本机回环部署需额外配置 TLS、访问控制和网络防火墙。

## 传输模式

### 默认 `native` 模式

只要底层 Chat 协议支持表达，relay 会完整保留 system/developer 角色、工具定义、tool_choice、并行约束、assistant 调用记录、调用 ID 及工具执行结果。第一跳返回的 SSE 流会被即时消费。native 工具调用仅在上游正常结束且通过完整批次校验后交付。native 调用失败不会触发 relay 端的生成重试、文本模式降级或模型切换。

### 显式 `text` 模式

仅在对接纯文本后端时配置 `UPSTREAM_TOOL_MODE=text`。学校当前部署依然使用本地第一跳及默认 native 模式。文本模式会将工具定义与历史记录编码在普通对话文本中；system/developer 指令会被转为上下文，当前工具 Schema 会置于最新用户轮次附近。历史调用保留 ID，工具结果通过 ID 关联，支持乱序结果。用户与结果中的控制标记会进行可逆转义，不会篡改附件或历史调用信封。

新的可执行调用必须是单个完整的最终信封：

```text
@@ACTION@@{"calls":[{"operation":"read_file","parameters":{"path":"README.md"}}]}@@END_ACTION@@
```

单次调用可附带唯一字符串 `id`；若未提供，relay 会自动分配。正文引用的示例、思考过程以及代码块包裹的 JSON 不会被重试成工具调用。系统不提供定界符容错修复、参数名推断更正、类型猜测，也不会从损坏输出中挑选最后一段有效信封。

`UPSTREAM_ACTION_RETRIES` 默认允许额外重试 2 次。该重试仅在尚未交付任何正文或思考、且上游输出了以 action 标记开头的损坏信封时生效。完全空回复、流中断、输出截断、普通对话示例及客户端工具执行均不在此重试预算内。

## 流式交付、批次校验与错误处理

三种客户端协议均会在模型生成完成前发送首包开启事件。正文与思考内容增量实时到达。工具参数会持续缓冲，直至上游正常生成结束并通过整批校验；分块的参数碎片绝不会作为可执行调用交付。Responses 协议事件具备单调递增的 `sequence_number` 与稳定的生命周期 ID。

上游发出的心跳在 Chat/Responses 协议中体现为 SSE 注释，在 Messages 协议中体现为 `ping` 事件。relay 不维护独立的心跳定时任务；心跳由配套的第一跳在等待后端响应时发出。

校验逻辑强制执行以下规则：

- 每一项调用必须命中当前允许调用的工具，且符合其 JSON Schema。Schema 仅支持本地引用，不支持拉取远程 Schema。
- 严格执行 `none`、`required`、指定工具选择以及 `parallel_tool_calls=false`。Messages 协议的 `disable_parallel_tool_use` 会映射为相同约束。
- 批次中任何一项调用校验不通过，整批调用全部拒绝。不会剔除未知工具后再交付其余合法调用。
- 存在重复 JSON 键、非有限数字、重复 ID、多个 action 信封、不完整 SSE/JSON 以及截断输出均会被直接拒绝。
- OpenAI 函数定义中若缺少 `parameters`，视为无参函数；显式传 `{}` 仍保留为开放 Schema。Anthropic 工具定义必须包含 `input_schema`。

响应头发送后发生的故障会以协议对应的错误事件返回，不发送表示成功的终止事件。非流式请求的上游或解析故障返回 HTTP 502。第一跳中安全的 `message`、`type` 与 `code` 字段会被截断限长并脱敏凭据后透传；请求头、调用栈及原始错误响应体不会透传。

`MAX_ACTION_BYTES` 默认值为 `1048576`。流式读取过程中，它统计正文、思考、工具名、ID 及参数累计占用的 UTF-8 字节数。单条不完整的 SSE 事件同样受该上限约束。重复的 SSE 元数据与心跳不占用此累计载荷配额。该配置用于限制整个响应的消费体量，不仅限于工具参数大小。

客户端中断流式连接会同步断开上游 HTTP 响应。若在响应头返回前连接挂起，其等待时长受网络超时限制；无法保证立即中止远端大模型的后台推理。整批校验不是文件权限审查，也不是 OS 沙箱，无法证明写入的文件内容符合预期。客户端必须自行对工具进行权限控制，重要写入操作应通过执行、回读或哈希校验等方式确认。

## 配置项

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `UPSTREAM_TOOL_MODE` | `native` | `native` 或显式 `text`；不会自动降级 |
| `UPSTREAM_BASE_URL` | `http://127.0.0.1:31100` | 当前部署对应的本地第一跳地址 |
| `UPSTREAM_API_KEY` | 未设置 | 第一跳本地 Bearer Key；必须与第一跳 `API_KEY` 一致 |
| `RELAY_API_KEY` | 未设置 | 下游访问 relay 的 Bearer / `x-api-key` |
| `HOST` | `127.0.0.1` | relay 监听地址；不影响第一跳配置 |
| `PORT` | `31110` | relay 监听端口 |
| `UPSTREAM_CONNECT_TIMEOUT` | `10` | relay 连接第一跳的超时时间（秒） |
| `UPSTREAM_READ_TIMEOUT` | `1800` | 上游 socket 两次数据读取间的等待超时（秒）；不是单次任务总耗时上限 |
| `UPSTREAM_ACTION_RETRIES` | `2` | 文本模式下交付前对损坏信封的额外纠错次数；native 模式下忽略 |
| `MAX_ACTION_BYTES` | `1048576` | 累计载荷及单条未完整 SSE 事件的字节上限；最小值为 `1024` |

### 第一跳的 8 / 3 / 900 恢复预算

以下配置属于**第一跳仓库**的 `.env` 或启动参数，不是 relay 环境变量：

| 第一跳环境变量 | 第一跳 CLI 参数 | 默认值与约束 |
| --- | --- | --- |
| `GENAI_EMPTY_CONTINUATIONS` | `--empty-continuations` | 完全空完成额外继续 `8` 次，整数 `0..30` |
| `GENAI_TOOL_REPAIR_ATTEMPTS` | `--tool-repair-attempts` | 未交付坏工具批次额外纠错 `3` 次，整数 `0..10` |
| `GENAI_TOOL_REQUEST_TIMEOUT_SECONDS` | `--tool-request-timeout` | 所有生成共享 `900` 秒，有限正数，不含附件预处理 |

完全空完成要求原始正文、思考、调用均为空且正常 `stop`。坏工具纠错要求正常结束后发现明确的协议、JSON、Schema 或工具选择错误，且没有正文、思考或批次交付。纠错由模型重新生成，不靠猜参数或修字符串。

两类次数累计、不相互重置，默认每请求最多 `1+8+3=12` 次生成。900 秒窗口不因心跳或新一代续期。恢复保留完整原历史与工具约束，只补真实次数和安全错误反馈，不积累虚构结果、不切模型、不降级、不重放客户端操作。已交付正文或思考、网络错误、断流、截断、取消或到期不重放；普通非空计划不会被猜成未完成。普通无工具请求不使用这些恢复或工具 deadline。

空预算耗尽为 `upstream_empty_response`，纠错耗尽保留具体错误码并说明耗尽，deadline 为 `upstream_request_timeout`。失败没有成功终态，未校验调用不交付。第一跳关闭已取得的 socket；尚未取得响应头时，主响应可按窗口失败，worker 等底层请求返回后清理，不能保证即时取消远端生成。

第一跳单次网络 connect/read timeout 为 10/300 秒。relay 的 10/1800 秒约束的是 relay 到第一跳的网络等待；它们和共享生成窗口、agent 整场任务预算不同。调大 read timeout 不会延长第一跳的共享窗口。

### 可选的外部任务监督

第一跳仓库提供可选 [OMP 监督器 `supervise.py`](https://github.com/FrozenYears/shanghaitech-genai2api/blob/main/validation/native-first-endurance/supervise.py)。它在同一真实 OMP 会话中，每轮结束后运行操作者指定的独立验收，对失败或停滞反馈继续；默认额外 8 次反馈、整场 45 分钟，单次 RPC 等待 1200 秒。

监督器不会自动安装或接管所有客户端。整场预算耗尽、权限/身份故障或无法确认会话停稳时，会保存状态并以非零状态退出；没有跨进程自动恢复，也不是 OS 沙箱。它可以在限定预算内减少人工逐次反馈，不能保证无人值守无限成功。配置与使用条件见[第一跳 README](https://github.com/FrozenYears/shanghaitech-genai2api/blob/main/README.md)。

## 兼容性与未支持特性

- 支持函数工具、自定义文本工具以及 Responses 的命名空间。命名空间在内部拼接为 `namespace.leaf` 处理，对外拆分为 `namespace` 和 `name` 两个字段返回。自定义文本工具通过 `{"input": string}` 参数定义的上游函数外壳进行包装。
- 暂未实现 Custom grammar，调用将返回 HTTP 400。需要 grammar 工具的客户端（如 Codex）必须使用兼容的工具声明，或使用支持该特性的服务提供方。
- 不提供 Responses 服务端状态维护功能：不支持 `previous_response_id`、`item_reference` 与 `store=true`，此类请求会被拒绝。每次请求必须发送完整的上下文历史。
- 附件会直接透传至第一跳。图片和文档数据块会原样保留，不会被降级压平为 base64 文本。项目不提供 Files API（`/v1/files`）、附件缓存或持久化的 `file_id` 存储。
- 网页端思考开关会映射为布尔值 `thinking`，联网搜索声明会映射为 `web_search`。不支持精确调节思考预算或模拟完整的服务方联网搜索流程。这些网页适配不构成服务方官方 API 行为的全面保证。
- 原生历史记录仅保留 Chat 传输格式能表达的思考元数据。不同提供方之间的加密思考数据无法通用互换。Messages 输出使用 `genai-compat-no-signature`，而非真实的 Anthropic 签名；Responses 输出不会重构加密的思考状态。

附件、Pi 与透传的历史验证见[功能透传说明](genai2api-feature-passthrough.zh-CN.md)，不代表本次重构重新验证了所有附件组合。

## 自定义后端

自定义后端须迁移至 `stream(TextCompletionRequest) -> Iterator[RelayEvent]` 接口。后端生成的事件包括 `text`、`reasoning`、`ping` 和终止的 `reply`；引擎对外统一产出经过校验的 `result`。`run()` 同样复用该流式接口。取消请求时迭代器必须妥善释放上游资源。旧版 `TextActionRelay` 及非流式接口已直接删除，未保留向后兼容垫片。纯文本后端必须显式开启文本模式。

## Token 计数

`POST /v1/messages/count_tokens` 与 `POST /v1/responses/input_tokens` 返回本地估算，不是厂商精确计数。
