# 上游 GenAI2API 新能力透传与 Pi 验收

日期：2026-09-18（上午复验）。环境：WSL Ubuntu，Pi 0.85.1，上游
`shanghaitech-genai2api`（`f08f248`，端口 31100），本仓库 relay（端口 31101）。

本文记录第二跳（relay）对上游新增能力的适配，以及用 Pi Agent 做的端到端验收。
上游能力的实现与平台限制见上游仓库的 `README.md`、`FEATURE_VALIDATION.md` 与
`PI_REAL_WORLD_VALIDATION.md`。

## 上游 2026-09-18 的图片直传变更

上游 `f08f248`（*fix: 图片改为直传，不再经过图片服务上传*）把图片从“先上传到
图片服务、再用返回地址和宽高”改成“直接把 data URL 或公网 URL 交给平台读取”，
并删除了前端令牌提取逻辑。文档仍然走上传。

对 relay 的影响：**客户端契约没有变化，relay 不需要改代码。** relay 一直是把
客户端的 `image_url` / `input_image` / Anthropic `image` 块原样放进发给上游的
消息内容里，由上游决定怎么交给平台；relay 既不调用图片服务，也不生成
`width` / `height`。本次复验确认：

- 图片在上游只记 `images=N`、`prepare_ms=0`，不再出现
  `attachment uploaded kind=image`；
- 文档仍上传，日志为 `attachment uploaded kind=document ...`，`prepare_ms` 为
  实际上传耗时（本次 55 ms）。

真正变化的是延迟和依赖：图片不再需要前端令牌，也不再经过一次上传。同一张
`inspection.png`（2590 字节 base64）经 relay 直传时本机实测 0.22–3.19 秒；上传
方案在同一链路里出现过数十秒到超时。

## 透传的请求字段

relay 不解析附件内容，也不自己实现思考或搜索。它只做两件事：把客户端附件块
按上游可识别的形状搬运过去，把客户端的思考、搜索开关归纳为上游的两个布尔参数。

| 能力 | 客户端写法 | relay 发给上游的字段 |
|---|---|---|
| 深度思考开 | Chat `thinking: true` / `reasoning_effort: high`；Anthropic `thinking: {type: enabled}`；Responses `reasoning: {effort: high}` | `thinking: true` |
| 深度思考关 | 上面对应的 `false` / `none` / `{type: disabled}` | `thinking: false` |
| 不指定 | 完全不传相关字段 | 不发送，保留平台默认 |
| 联网搜索 | `web_search: true`、`web_search_options: {}`，或声明 `web_search*` 类型工具 | `web_search: true` |
| 图片 | Chat/Responses `image_url`、`input_image`（URL 或 data URL）；Anthropic `image.source` | `{"type":"image_url","image_url":{"url":...}}` |
| 文档 | Chat `file`；Responses `input_file`；Anthropic `document` | `{"type":"file","file":{"file_data"/"file_url",...}}` |

要点：

- 思考档位（`minimal`/`low`/`medium`/`high`/`xhigh`/`max`）在上游只有开关语义，
  relay 统一收敛成 `thinking` 布尔值，不再透传档位字符串，避免上游因未知档位报错。
- 只有 `tool_choice` 明确禁用工具（`none` / `{type:none}` / 指定了非搜索函数）时，
  工具声明才不会自动打开搜索。显式 `web_search: true` 始终生效。
- 搜索声明不会进入本地工具白名单；它由平台执行，relay 不会把它当成可执行函数。
- 消息内容在 relay 内部只保留 `text`、`image_url`、`file` 三类块；未知块被忽略，
  不会把 base64 当正文拼进提示词。图片的 URL 原样转发，data URL 和公网 URL 都
  交给上游处理。

## 思维链输出

上游的 `reasoning_content` 在三种协议里分别以各自的原生位置返回：

| 协议 | 返回位置 |
|---|---|
| Chat Completions | `choices[0].message.reasoning_content`（流式为同名字段的 delta） |
| Anthropic Messages | `content[]` 里 `type: thinking` 的块（流式为 `thinking_delta` + `signature_delta`） |
| OpenAI Responses | `output[]` 里 `type: reasoning` 的条目（流式为 `response.reasoning_summary_text.delta`） |

因此 Pi 会把思考与正文显示成两个独立内容块，而不是拼在一段文字里。

Responses 路径的历史 `reasoning` 条目不会再次喂给上游；Anthropic 历史里的
thinking 块同样被忽略。relay 不会凭正文猜测哪一句属于思考。

## Pi Agent 验收

隔离配置沿用 `/tmp/pi-relay-validation/agent/models.json`：两个 provider
（`genai` 与带 `samplingParams: {"web_search": true}` 的 `genai-search`）都指向
relay 的 `http://127.0.0.1:31101/v1`，模型 id 仍是真实的 `deepseek-pro`。
启动参数固定为
`--no-extensions --no-skills --no-prompt-templates --no-themes --no-context-files --no-approve --offline --no-session`。

本轮 2026-09-18 上午的实测（Pi 进程、relay、上游三层都真实运行）：

| 场景 | 命令要点 | 结果 |
|---|---|---|
| 思维链与正文分块 | `--no-tools --thinking high` | 思考块 112 字符、正文块 18 字符，两个独立块；答案正确（蓝 16、红 22、绿 12） |
| 联网搜索 | `--provider genai-search --no-tools` | relay 日志 `reasoning effort=medium → thinking=true`，上游 `search=True`、补充来源 10 条；回答给出学院官网 `sist.shanghaitech.edu.cn` |
| 图片理解（Pi / Responses） | `--no-tools` + `@inspection.png` | 上游 `images=1`、`prepare_ms=0`、3.19 秒；正确识别左侧紫色正方形、右侧橙色圆形与数字 `739204` |
| 图片（Anthropic 入口） | relay `/v1/messages` 发送 base64 `image` 块 | 0.63 秒，同样三项全部正确 |
| 网络图片（Responses） | relay `/v1/responses` 发送 `input_image` 公网 URL | 1.33 秒，正确回答是 Python 官方标志 |
| 多轮历史图片追问 | 第二轮带上第一轮的图片与回答 | 第二轮 0.25 秒回答 `739204`；第一轮 52.74 秒（平台侧波动，非上传） |
| 工具调用 | `--tools read`，读取 `handover.txt` | 模型发出 `read` 工具调用，Pi 执行后正确报出编号 `PI-739204`、项目、时间、负责人 |
| 文档附件 | relay Chat 接口发送 `file` 块（`brief.txt`） | 上游 `documents=1`、`parsed_chars=30`、`prepare_ms=55`；模型正确读出项目代号、预算、截止日期 |

原始 Pi JSON 事件与 relay/上游日志摘录保留在 `/tmp/pi-relay-validation/run2/`。
关键日志原文（上游按 `model=deepseek-pro` 记录）：

```text
# 思考（relay 与上游）
relay.features reasoning effort=high mapped to thinking=true; the platform has no levels
request features model=deepseek-pro thinking=True search=False images=0 documents=0 prepare_ms=0

# 搜索
request features model=deepseek-pro thinking=True search=True images=0 documents=0 prepare_ms=0
search sources count=10

# 图片：直传后没有 attachment uploaded kind=image
request features model=deepseek-pro thinking=True search=False images=1 documents=0 prepare_ms=0
[req_96891ee302704f8f] completed in 3.19s      # Pi 单图
[req_81e62877b29c4927] completed in 0.63s      # Anthropic 图片
[req_15d3c410f27440df] completed in 1.33s      # Responses 网络图片
[req_4afcbb4d8d994275] completed in 0.25s      # 多轮历史图片追问

# 文档：仍然上传
attachment uploaded kind=document bytes=71 parsed_chars=30
request features model=deepseek-pro thinking=default search=False images=0 documents=1 prepare_ms=55
```

补充说明：

- Pi 0.85.1 没有原生文档附件入口，`@文件` 只会按文本读取；文档透传用 relay 接口
  直接验证，覆盖 Chat 的 `file` 块。Responses 的 `input_file`、Anthropic 的
  `document` 由本地构造的协议测试覆盖。
- 图片直传后不再有“上传”这一步，慢只可能出现在平台取图或模型生成；本轮单图
  0.22–3.19 秒，一次 52.74 秒属于平台侧波动，不代表上传或 relay 出了问题。
- 搜索场景即使不显式指定思考，Pi 也会按模型默认档位发出 `reasoning_effort`，
  relay 同样收敛成 `thinking: true`。

## 已知限制

- 思考只有开关，没有档位或预算；上游不返回思考时 relay 不会补造。
- 搜索正文编号与平台补充来源编号不一定对应，relay 原样转发，不伪造引用关系。
- 附件不缓存、不做去重：图片每轮以 data URL 原样重发（体积约为原图 4/3），
  文档每轮重新上传。单张内联图片须小于 20 MiB（上游限制）。
- relay 的流式响应仍然是先缓冲完整上游结果再下发，思考与正文的分块是逻辑分块，
  不是上游增量实时转发。
- 上游对 `max_tokens` 现在会本地强制生效；relay 在客户端未指定时使用自己的默认
  值（8192），客户端应显式给出所需上限。
