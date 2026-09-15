# 使用自己的 GitHub API Token 发布

该方案不需要安装 `gh`。仓库根目录中的发布脚本会：

1. 使用 GitHub REST API 检查身份；
2. 创建个人仓库（若尚不存在）；
3. 设置无凭据的 HTTPS `origin`；
4. 使用一次性 `GIT_ASKPASS` 推送 `main`。

Token 不会写入 Git remote、Git 配置、仓库文件或提交历史。

## 1. 创建 Token

最简单可靠的选择是 classic Personal Access Token：

- 发布公开仓库：勾选 `public_repo`；
- 发布私有仓库：勾选完整 `repo`。

也可以使用 fine-grained token，但需要将资源所有者设为你的个人账号，允许访问全部
仓库，并授予仓库 Administration 和 Contents 的读写权限。组织账号可能还要求组织
管理员批准。

Token 只会在创建时显示一次。不要把它写入 `.env`、命令参数、Git remote 或聊天。

## 2. 在仓库根目录运行

推荐直接交互输入，输入内容不会显示，也不会进入 shell history：

```bash
cd /path/to/GenAI2AgentRelay
./scripts/publish-github.sh
```

默认创建并推送公开仓库 `genai2agent-relay`。也可以指定名称与可见性：

```bash
./scripts/publish-github.sh my-relay public
./scripts/publish-github.sh my-private-relay private
```

如果 Token 已经由你自己的密码管理器、安全 CI secret 或当前进程注入：

```bash
GITHUB_TOKEN="$GITHUB_TOKEN" ./scripts/publish-github.sh
```

不要直接写成 `GITHUB_TOKEN=github_pat_xxx ...`，因为这可能进入 shell history。

## 3. 完成后的检查

```bash
git remote -v
git status
git log -1 --oneline
```

remote URL 中不应出现用户名、密码或 Token。

## 常见错误

- `HTTP 401 Bad credentials`：Token 错误、已过期或被撤销。
- `HTTP 403 Resource not accessible`：Token 权限不足，或组织限制了 Token。
- `HTTP 422`：仓库名不可用，或账号策略不允许创建该仓库。
- API 创建成功但 push 返回 `403`：Token 能管理仓库但缺少 Contents 写权限。
- `The worktree is not clean`：先提交或暂存本地修改，脚本不会发布未提交文件。
