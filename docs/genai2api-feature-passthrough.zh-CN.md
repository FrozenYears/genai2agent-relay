# 上游 GenAI2API 新能力透传与 Pi 验收

日期：2026-09-18。环境：WSL Ubuntu，Pi 0.85.1，上游 `shanghaitech-genai2api`
（31100），本仓库 relay（31101）。

本文记录第二跳（relay）对上游新增能力的适配，以及用 Pi Agent 做的端到端验收。
上游能力的实现与平台限制见上游仓库的 `README.md`、`FEATURE_VALIDATION.md` 与
`PI_REAL_WORLD_VALIDATION.md`。

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
  不会把 base64 当正文拼进提示词。

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

隔离配置放在 `/tmp/pi-relay-validation/agent/models.json`：两个 provider
（`genai` 与带 `samplingParams: {"web_search": true}` 的 `genai-search`）都指向
relay 的 `http://127.0.0.1:31101/v1`，模型 id 仍是真实的 `deepseek-pro`。
启动参数固定为
`--no-extensions --no-skills --no-prompt-templates --no-themes --no-context-files --no-approve --offline --no-session`。

| 场景 | 命令要点 | 结果 |
|---|---|---|
| 思维链与正文分块 | `--no-tools --thinking high` | 思考块 299 字符、正文块 197 字符，两个独立块；解答正确 |
| 联网搜索 | `--provider genai-search --no-tools` | 上游日志 `search=True`、补充来源 10 条；回答引用图书馆官网 |
| 图片理解 | `--no-tools` + `@inspection.png` | 上游日志 `images=1`、`attachment uploaded`；正确识别紫色矩形、橙色圆形与数字 `739204` |
| 工具调用 | `--tools read`，读取 `handover.txt` | 模型发出 `read` 工具调用，Pi 执行后正确报出编号 `PI-739204`、项目、时间、负责人 |
| 文档附件 | relay Chat 接口发送 `file` 块（`brief.txt`） | 上游日志 `documents=1`、`parsed_chars=30`；模型正确读出项目代号、预算、截止日期 |

补充说明：

- Pi 0.85.1 没有原生文档附件入口，`@文件` 只会按文本读取；文档透传用 relay 接口
  直接验证，覆盖 Chat 的 `file` 块。Responses 的 `input_file`、Anthropic 的
  `document` 由本地构造的协议测试覆盖。
- 图片与文档上传本身很快（毫秒级到几十毫秒），长等待发生在上游取图与模型生成，
  极端情况下仍可能超过上游 300 秒超时。

## 已知限制

- 思考只有开关，没有档位或预算；上游不返回思考时 relay 不会补造。
- 搜索正文编号与平台补充来源编号不一定对应，relay 原样转发，不伪造引用关系。
- 附件不缓存、不做去重，多轮请求会把历史附件重新上传一次。
- relay 的流式响应仍然是先缓冲完整上游结果再下发，思考与正文的分块是逻辑分块，
  不是上游增量实时转发。
