# GenAI2AgentRelay

双跳反代的第二跳：向客户端提供 Chat Completions、Messages 和 Responses 接口，与第一跳交换结构化工具和 SSE。

[启动](#快速开始) · [接入地址](#客户端接入) · [工作原理](#工具调用如何处理) · [详细配置](docs/CONFIGURATION.md)

> [!NOTE]
> 使用 `feat/native-first-streaming` 分支，先按[第一跳 README](https://github.com/FrozenYears/shanghaitech-genai2api/blob/feat/native-first-streaming/README.md)启动 31100 服务。
> 学校网页登录、附件处理与工具适配均由第一跳负责。

## 快速开始

需 Git、uv 及 Python 3.10+；依赖仅安装到本仓库 `.venv`。

```bash
git clone --branch feat/native-first-streaming https://github.com/FrozenYears/genai2agent-relay.git
cd genai2agent-relay
uv venv .venv
```

Linux / WSL 安装：

```bash
uv pip install --python .venv/bin/python -e .
cp .env.example .env
```

<details>
<summary>Windows PowerShell 安装</summary>

```powershell
uv pip install --python .venv/Scripts/python.exe -e .
Copy-Item .env.example .env
```

WSL 与 Windows 环境独立，不共用 `.venv`；跨环境回环地址互通需自行确认。

</details>

编辑本仓库 `.env`：

```dotenv
UPSTREAM_BASE_URL=http://127.0.0.1:31100
UPSTREAM_TOOL_MODE=native
UPSTREAM_API_KEY=replace-with-first-hop-key
RELAY_API_KEY=replace-with-relay-key
HOST=127.0.0.1
PORT=31110
UPSTREAM_CONNECT_TIMEOUT=10
UPSTREAM_READ_TIMEOUT=1800
UPSTREAM_ACTION_RETRIES=2
MAX_ACTION_BYTES=1048576
```

| 配置 | 要求 |
| --- | --- |
| `UPSTREAM_API_KEY` | 与第一跳 `API_KEY` 相同，不是学校网页令牌 |
| `RELAY_API_KEY` | 客户端本地 Key；未设时不鉴权 |
| `UPSTREAM_ACTION_RETRIES` | 仅 text 模式生效；native 不叠加重试 |
| `MAX_ACTION_BYTES` | 正文、思考、工具载荷及未闭合 SSE 事件的字节上限 |

启动并保持第一跳同时运行：

```bash
.venv/bin/python main.py
```

Windows 使用 `.venv/Scripts/python.exe main.py`。

> [!WARNING]
> relay 默认仅监听 `127.0.0.1`，但第一跳当前监听 `0.0.0.0`。
> 请设置两处本地 Key 并限制网络访问，切勿提交 `.env` 或直接暴露到公网。

## 客户端接入

客户端 Key 使用 `RELAY_API_KEY`，支持 Bearer 或 `x-api-key`。

| 协议 | Base URL | 请求路径 |
| --- | --- | --- |
| Chat Completions | `http://127.0.0.1:31110/v1` | `/chat/completions` |
| Responses | `http://127.0.0.1:31110/v1` | `/responses` |
| Messages | `http://127.0.0.1:31110` | `/v1/messages` |

```bash
curl -fsS http://127.0.0.1:31110/health
curl -fsS http://127.0.0.1:31110/v1/models \
  -H 'Authorization: Bearer replace-with-relay-key'
```

- `/health` 免鉴权，只说明服务可达，不验证学校登录或模型生成。
- 模型名称使用 `/v1/models` 返回的 `id`。
- Claude Code 配置见[适配指南](docs/claude-code-adapter-reference.zh-CN.md)。

## 工具调用如何处理

1. relay 保留客户端工具 Schema、角色、调用 ID 及历史结果，转发给第一跳。
2. 网页无原生工具通道时，第一跳将工具规则写入提示，并解析模型输出的调用文本。
3. 第一跳与 relay 校验完整批次，通过后按客户端协议交付。
4. 本地 agent 执行工具并回传结果；relay 不执行命令、不读取参数指定文件。

[网页模型的工具调用示例](https://github.com/FrozenYears/shanghaitech-genai2api/blob/feat/native-first-streaming/README.md#网页模型如何调用工具)

| 模式 | 用途 | 生成重试 |
| --- | --- | --- |
| `native`（默认） | 两跳间传结构化工具与 SSE | relay 不重试；由第一跳恢复 |
| `text`（显式） | 仅支持文本的 backend | 未交付内容的坏封套，默认额外 2 次 |

- 正文/思考增量返回；工具参数收齐并整批校验后交付，不剔除坏项做部分执行。
- 第一跳默认空继续 8 次、工具纠错 3 次，共享 900 秒请求窗口；在第一跳配置。
- 已交付内容、断流、截断和网络错误不重放；失败不发成功终态。

> [!IMPORTANT]
> `native` 不代表学校网页开放了原生函数输入。
> 无 Files API、Responses 服务端状态或 custom grammar；工具授权由客户端负责，代理不是 OS 沙箱。

## 与重构前的区别

| 项目 | 原版 | 当前 |
| --- | --- | --- |
| 上游 | `complete()` 等完整文本，拒绝原生调用 | 默认 `stream()`，接受结构化调用 |
| 下游 SSE | 完整文本校验后才输出 | 开始事件先发，正文/思考增量返回 |
| 校验与恢复 | 已有 Schema 校验，文本纠错在 relay | 严格整批交付，native 请求恢复集中第一跳 |

完整基线与兼容代价见[架构对照](https://github.com/FrozenYears/shanghaitech-genai2api/blob/feat/native-first-streaming/docs/STAGE_NATIVE_FIRST.md#5-与双跳原仓库及本地重构前分支的对照)。自定义 backend 需迁移到 `stream()`，详见[配置说明](docs/CONFIGURATION.md#自定义后端)。

## 实测与文档

- 已记录的完整双跳 + omp 监督器测试中，DeepSeek、Kimi 均完成 20 阶段，最终 71 项验收通过；Kimi 用到 4 次自动反馈。
- 这不是 relay 单独的成功率或任意任务保证。[实测报告](https://github.com/FrozenYears/shanghaitech-genai2api/blob/feat/native-first-streaming/docs/TEST_REPORT_deepseek_pro.md)
- [完整配置与限制](docs/CONFIGURATION.md) · [附件与思考透传](docs/genai2api-feature-passthrough.zh-CN.md) · [历史修复](docs/action-envelope-fixes.zh-CN.md)

独立测试：在现有虚拟环境安装 pytest 后运行 `.venv/bin/python -m pytest tests -q`；Windows 替换解释器路径。部署检查不能只看单元测试。

基于 [negativegluon/genai2agent-relay](https://github.com/negativegluon/genai2agent-relay)，采用 [MIT License](LICENSE)。
