# GenAI2AgentRelay

GenAI2AgentRelay 是连接 agent 客户端与学校网页代理的本地 HTTP 服务。Claude Code、Codex、omp 等客户端把请求发到这里；relay 转给第一跳，再把模型回复和工具调用按客户端协议返回。

它不自带模型、不登录学校账号，也不替客户端执行工具。

## 目录

- [部署与接入](#部署与接入)
  - [快速开始](#快速开始)
  - [Windows PowerShell 安装](#windows-powershell-安装)
  - [客户端接入](#客户端接入)
- [功能与设计](#功能与设计)
  - [与第一跳的区别](#与第一跳的区别)
  - [工具调用如何处理](#工具调用如何处理)
  - [与重构前的区别](#与重构前的区别)
- [实测与资料](#实测与资料)

## 部署与接入

> [!NOTE]
> 使用 `main` 分支，先按[第一跳 README](https://github.com/FrozenYears/shanghaitech-genai2api/blob/main/README.md)启动 31100 服务。
> 学校网页登录、附件处理与工具适配均由第一跳负责。

### 快速开始

需 Git、uv 及 Python 3.10+；依赖仅安装到本仓库 `.venv`。

```bash
git clone --branch main https://github.com/FrozenYears/genai2agent-relay.git
cd genai2agent-relay
uv venv .venv
```

Linux / WSL 安装：

```bash
uv pip install --python .venv/bin/python -e .
cp .env.example .env
```

#### Windows PowerShell 安装

```powershell
uv pip install --python .venv/Scripts/python.exe -e .
Copy-Item .env.example .env
```

WSL 与 Windows 环境独立，不共用 `.venv`；跨环境回环地址互通需自行确认。

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

### 客户端接入

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

## 功能与设计

```mermaid
flowchart LR
    A[agent 客户端] <-->|三种兼容协议| R["第二跳 · 31110<br/>genai2agent-relay"]
    R <-->|Chat Completions 与 SSE| F["第一跳 · 31100<br/>shanghaitech-genai2api"]
    F <-->|网页登录与聊天请求| W[学校 GenAI 网页后端]
```

### 与第一跳的区别

| 服务 | 主要对接谁 | 负责什么 |
| --- | --- | --- |
| 第一跳 `shanghaitech-genai2api` | 学校网页后端 | 管理网页登录，处理附件，把工具定义写入网页提示，并解析模型输出 |
| 第二跳 `genai2agent-relay` | agent 客户端和第一跳 | 将三种客户端协议转换为第一跳的 Chat 请求，保留工具 ID/历史结果，转发流式回复并校验工具批次 |

- 学校账号只配置在第一跳；relay 用本地 `UPSTREAM_API_KEY` 访问它。
- 客户端只配置 relay 地址和 `RELAY_API_KEY`，不需要知道学校网页请求格式。
- 第一跳本身也提供三种兼容接口，合适的客户端可直接连 31100；双跳部署时统一连 relay 的 31110。

两跳名称沿用项目约定，客户端请求先到第二跳。relay 不绕过第一跳访问学校，也不会因多加一层代理就让模型获得原生函数输入或本地文件权限。

### 工具调用如何处理

1. relay 保留客户端工具 Schema、角色、调用 ID 及历史结果，转发给第一跳。
2. 网页无原生工具通道时，第一跳将工具规则写入提示，并解析模型输出的调用文本。
3. 第一跳与 relay 校验完整调用批次，通过后按客户端协议交付。
4. 本地 agent 执行工具并回传结果；relay 不执行命令、不读取参数指定文件。

[网页模型的工具调用示例](https://github.com/FrozenYears/shanghaitech-genai2api/blob/main/README.md#网页模型如何调用工具)

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

### 与重构前的区别

| 项目 | 原版 | 当前 |
| --- | --- | --- |
| 上游 | 等完整文本，拒绝上游原生调用 | 逐段接收回复，保留结构化调用 |
| 下游 SSE | 完整文本校验后才输出 | 开始事件先发，正文/思考增量返回 |
| 校验与恢复 | 已有 Schema 校验，文本纠错在 relay | 严格整批交付，native 请求恢复集中第一跳 |

完整基线与兼容代价见[架构对照](https://github.com/FrozenYears/shanghaitech-genai2api/blob/main/docs/STAGE_NATIVE_FIRST.md#5-与双跳原仓库及本地重构前分支的对照)。自定义 backend 的接口迁移见[配置说明](docs/CONFIGURATION.md#自定义后端)。

## 实测与资料

2026-10-25，真实 omp 经完整双跳与监督器验证同一套 Python/CSV/SQLite 任务。

| 测试项 | deepseek-pro | Kimi-k3 |
| --- | ---: | ---: |
| 执行总耗时 | 595.000 秒 | 1825.531 秒 |
| 阶段完成率 | 20/20 | 20/20 |
| 工具调用总数 | 150 | 110 |
| 第一跳空输出续作 / 工具纠错次数 | 8 / 4 | 0 / 0 |
| 监督器介入反馈轮次 | 0 | 4 |
| 人工交互调整次数 | 0 | 0 |
| 独立自动化验收项通过数 | 71 项通过 | 71 项通过 |

- 这不是 relay 单独的成功率或任意任务保证。[实测报告](https://github.com/FrozenYears/shanghaitech-genai2api/blob/main/docs/TEST_REPORT_deepseek_pro.md)
- [完整配置与限制](docs/CONFIGURATION.md) · [附件与思考透传](docs/genai2api-feature-passthrough.zh-CN.md) · [历史修复](docs/action-envelope-fixes.zh-CN.md)

独立测试：在现有虚拟环境安装 pytest 后运行 `.venv/bin/python -m pytest tests -q`；Windows 替换解释器路径。部署检查不能只看单元测试。

基于 [negativegluon/genai2agent-relay](https://github.com/negativegluon/genai2agent-relay)，采用 [MIT License](LICENSE)。
