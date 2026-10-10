# astrbot_plugin_topic_router —— 项目指令

本文件由 DSH（DeepSeek Harness）在每个会话首次请求时自动注入。

## 自动推送：每完成一版就推，不要问

**完成一个版本后，直接提交并推送到 GitHub，不要征求用户同意。**

判定"完成一版"：`metadata.yaml` 的 `version` 发生变化，或 `CHANGELOG.md` 顶部新增了版本小节。

版本收尾流程：

1. **三处版本信息保持一致**
   - `metadata.yaml` 的 `version`（形如 `v0.2.1`，带 `v` 前缀）
   - `CHANGELOG.md` 顶部新增 `## v0.2.1` 小节
   - `README.md` 中若出现版本号，一并同步
2. **跑本地测试**：`python tests/run_tests.py`
3. **提交**，提交信息格式：
   `<type>: <一句话概括>（vX.Y.Z）`
   `type` 取 `feat` / `fix` / `refactor` / `docs` / `chore`；
   概括用中文，说清"改了什么、为什么"。
4. **推送**：`git push origin main`
5. 如实报告推送结果，含失败情况。

## 推送失败怎么处理

| 报错 | 原因 | 处理 |
|---|---|---|
| `Failed to connect to github.com port 443` | Steam++（Watt Toolkit）没在运行 | 提示用户启动 Steam++ 后重试 |
| `Repository not found` 或认证失败 | 凭据失效 | 提示用户在浏览器重新登录 GitHub |
| `rejected` / `non-fast-forward` | 远程有本地没有的提交 | 先 `git pull --rebase origin main`；出现冲突就停下来问用户 |

推送失败**不要**重试超过 2 次，**不要**谎报成功。

## 禁止事项

- 不要 `git push --force`、不要 `git push -f`
- 不要为了推送去改业务代码
- 不要提交 `zip` 安装包（打包产物默认写到桌面，不进仓库）
- 严格遵循 `.gitignore`（`data/`、`plugin_data/`、`__pycache__/`、`dist/`、`*.zip`、`.venv/` 都已在忽略列表内）

## 本项目背景（踩过的坑，别再踩）

- **handler 拿不到 `self`**：这个环境派发插件 handler 时不绑定实例，`self` 实际是事件对象。
  所以状态与逻辑一律放**模块级**（`RUNTIME` / `_CTX`），判定逻辑写成显式接收 `ctx` 的模块级函数。
  任何 `self.xxx` 都会抛 `AttributeError`，而入口的 `except` 会把它静默吞掉 ——
  表现为"插件在跑但什么都不输出"。
- **指令名要避免通用英文词**：AstrBot 用实时内存注册表判定指令冲突，`topic`、`topicrouter`
  这类通用词会撞名并一次触发多个插件。当前只保留 `/topicweave` 和别名 `话题织机`。
  热重载**不会**清理内存里的旧注册项，必须**重启进程**才消失。
- **打包必须用 `build_zip.py`**，不要用 `Compress-Archive`：后者写反斜杠条目名，
  Linux 上解包后目录结构不对，AstrBot 收尾步骤会抛 `FileNotFoundError`。
