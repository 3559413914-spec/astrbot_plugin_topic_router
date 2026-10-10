# 话题路由器 · Topic Router

> 让机器人自己判断用户这句话是**接着上一个话题**，还是**开了新话题**。

AstrBot 的对话是一整条上下文。当用户在同一个会话里从"帮我画个电路板"跳到"顺便看看 NAS 硬盘"
时，模型会把两件事混在一起回答；等到上下文攒到 50 条被压缩，话题边界就彻底糊了。

这个插件在每条用户消息到达时做一次轻量判定，输出「延续 / 新话题」+ 置信度，然后按你选的
模式行动。**默认只记录，不改任何上下文**——先看几天它判得准不准，再决定要不要自动翻篇。

```
[话题路由器] NEW-TOPIC | webchat:... | confidence=0.90 | source=llm | topic=硬盘扩容 | ...
[话题路由器] CONTINUE  | webchat:... | confidence=0.95 | source=heuristic | topic=电路板设计 | ...
```

| | |
| --- | --- |
| 适用 | AstrBot ≥ 4.24（v4.x） |
| 依赖 | **零第三方依赖**（纯标准库） |
| 许可 | MIT |
| 模式 | `off` / `mark`（默认）/ `hint` / `auto` |
| 指令 | `/topicweave`（别名 `话题织机`） |

---

## 目录

- [它能做什么](#它能做什么)
- [安装：先选你的部署方式](#安装先选你的部署方式)
  - [方式 A：WebUI 上传（所有平台通用，推荐）](#方式-awebui-上传所有平台通用推荐)
  - [方式 B：Docker 部署的 AstrBot](#方式-bdocker-部署的-astrbot)
  - [方式 C：Windows 桌面客户端](#方式-cwindows-桌面客户端)
  - [方式 D：飞牛 NAS（fnOS）](#方式-d飞牛-nasfnos)
  - [方式 E：macOS / Linux 源码部署](#方式-emacos--linux-源码部署)
- [装完之后的配置](#装完之后的配置)
- [验证它真的在工作](#验证它真的在工作)
- [使用条件与边界](#使用条件与边界)
- [配置项](#配置项)
- [指令](#指令)
- [排错](#排错)
- [开发与测试](#开发与测试)

---

## 它能做什么

**四种模式，按侵入性从小到大：**

| mode | 行为 | 适合 |
| --- | --- | --- |
| `off` | 完全不判定 | 临时关掉 |
| `mark` | **默认**。只写日志、维护话题状态，请求一个字都不改 | 先跑这个，观察准确率 |
| `hint` | 额外往本轮注入一段临时提示（不进历史），告诉模型"已换话题" | 不想换对话，但想让模型别串味 |
| `auto` | 置信度 ≥ `auto_threshold` 时新建一个对话并切过去，旧对话完整留档 | 确认判定准了再开 |

`auto` 做的事和用户手打 `/new` 完全等价：旧对话不删、随时能用 `/ls` `/switch` 切回去，
区别只是由插件在后台完成，并给群里回一句提示（可用 `new_conversation_notice` 关掉）。

**为什么默认不自动切？** 因为误判的代价不对称：

- 把「延续」误判成「延续」→ 没损失；
- 把「延续」误判成「新话题」→ 会把用户正在聊的上下文切走，用户会觉得机器人失忆了。

所以插件的默认姿势是「先观察，后行动」。

**判定分两层，省钱：**

1. **零成本启发式**（每条消息都跑）：与当前话题的字符相似度、承接词（继续/还有/那个）、
   显式换话题词（换个话题/顺便问）、时间间隔、消息长度；
2. **模型判定**（只在模糊区间才调用一次便宜模型，默认 `deepseek/deepseek-v4-flash`）。

模型说错话、超时、报错**一律按「延续」处理**——判定失败绝不影响正常回复。

---

## 安装：先选你的部署方式

不管你用哪种方式跑 AstrBot，插件最终都装在同一个地方：

| 部署方式 | AstrBot 数据目录 | 插件目录 |
| --- | --- | --- |
| Docker | 容器内 `/AstrBot/data`（你挂载的宿主机目录） | `/AstrBot/data/plugins/` |
| 桌面客户端 | `~/.astrbot/data` | `~/.astrbot/data/plugins/` |
| 源码 / CLI | `<启动目录>/data`（可用 `ASTRBOT_ROOT` 改） | `<启动目录>/data/plugins/` |
| 飞牛 NAS 应用 | 应用数据目录（fnOS 里是 `/vol1/@appdata/astrbot/data`） | 同上 `plugins/` |

> Windows 的 `~` 是 `C:\Users\<你的用户名>`；macOS / Linux 的 `~` 是 `/Users/<你>`、
> `/home/<你>`。

**装完生效方式**：WebUI 上传安装会**自动加载**，一般不需要重启；升级已装的版本时，
建议**重启一次 AstrBot 进程**（原因见 [排错](#排错)：热重载不会清掉内存里的旧注册项）。

---

### 方式 A：WebUI 上传（所有平台通用，推荐）

不需要碰命令行、不需要进容器，**所有部署方式都适用**。

1. 打开 AstrBot 管理面板（默认 `http://<你的IP>:6185`，Docker 部署时**不能用 localhost**）；
2. 左侧进 **插件 → 插件**（地址是 `/extension/plugins`）；
3. 右上角 **安装插件 → 从文件安装**，选择 `astrbot_plugin_topic_router-<版本>.zip`；
4. 等日志出现 `Installed plugin astrbot_plugin_topic_router-... successfully.`；
5. 在插件列表里确认它已**启用**，然后进 **插件配置** 填 `provider_id`（见下一节）。

也可以在 **插件市场** 里搜名字安装（如果它已上架）。

> ⚠️ **不要用系统自带的"压缩文件夹"重新打包**。Windows 的压缩工具会写反斜杠条目名，
> 装到 Linux 上的 AstrBot 会直接失败（`FileNotFoundError`）。要自己打包请用仓库里的
> `python build_zip.py`，详见 [README 末尾](#自己打包)。

---

### 方式 B：Docker 部署的 AstrBot

官方镜像 `soulter/astrbot`，数据卷挂到容器内 `/AstrBot/data`。

**B1. 用 WebUI 上传（推荐）**：直接照 [方式 A](#方式-awebui-上传所有平台通用推荐) 走，
宿主机不需要任何操作。

**B2. 手动把插件目录拷进容器**：

```bash
# 1) 看容器名（官方示例里叫 astrbot）
docker ps

# 2) 把解压出来的插件目录拷进去
docker cp astrbot_plugin_topic_router <容器名>:/AstrBot/data/plugins/

# 3) 重启容器让它加载
docker restart <容器名>

# 4) 看日志确认加载
docker logs -f <容器名> | grep -i topic
```

**B3. 直接在宿主机放（不用进容器）**：因为 `/AstrBot/data` 就是挂载点，找到你启动容器时
`-v` 的那一侧目录，把插件目录放进去即可：

```bash
# 例如启动命令是 -v $PWD/data:/AstrBot/data
cp -r astrbot_plugin_topic_router ./data/plugins/
docker restart <容器名>
```

**Docker Compose 用户**：`compose.yml` 里 `volumes:` 写的宿主机路径就是上面那个目录。
国内网络拉不动镜像时，官方给的办法是把 `image: soulter/astrbot:latest` 换成
`m.daocloud.io/docker.io/soulter/astrbot:latest`。

**群晖 / 威联通 / Unraid / CasaOS / 1Panel**：本质都是"找到映射到 `/AstrBot/data` 的宿主机
目录 → 把插件目录放进 `plugins/` → 重启容器"。图形界面里找"容器 → 详情 → 卷/存储"就能看到映射。

---

### 方式 C：Windows 桌面客户端

[AstrBot-desktop](https://github.com/AstrBotDevs/AstrBot-desktop) 支持 Windows / macOS / Linux，
下载对应安装包（`.exe` / `.dmg` / `.deb` / `.rpm`）装完启动即可，数据在 `~/.astrbot`。

**C1. WebUI 上传（推荐）**：桌面客户端自带管理面板，按 [方式 A](#方式-awebui-上传所有平台通用推荐)
操作。

**C2. 直接放目录**：

1. 完全退出 AstrBot 桌面客户端（托盘图标也要退出）；
2. 把 `astrbot_plugin_topic_router` 整个目录复制到：
   ```
   C:\Users\<你的用户名>\.astrbot\data\plugins\
   ```
   最终结构必须是 `.astrbot\data\plugins\astrbot_plugin_topic_router\metadata.yaml`；
3. 重新启动客户端。

**C3. 只想在本地试一下、不启动 AstrBot**：仓库的 `tests/test_portability.py` 用桩模块跑，
任意 Python 3.10+ 都能执行，用来确认插件逻辑本身没问题：

```powershell
python tests\test_portability.py
```

---

### 方式 D：飞牛 NAS（fnOS）

fnOS 上 AstrBot 通常以"应用"形式运行，数据目录在应用私有目录里（本插件实测环境为
`/vol1/@appdata/astrbot/data`）。**优先走 WebUI 上传**，这样不需要 SSH：

1. 浏览器打开 `http://<NAS 的 IP>:6185` → 用管理员账号登录（默认用户名 `astrbot`）；
2. **插件 → 插件 → 安装插件 → 从文件安装** → 上传 zip；
3. 在 **插件配置** 里填 `provider_id`，保存；
4. 如果日志里没有出现 `模块已导入 <版本>`，去 fnOS 的**应用面板把 AstrBot 停止再启动**
   （或在 SSH 里 `docker restart <容器名>`）。fnOS 的 AstrBot 一般是容器化的，
   重启应用 = 重启容器。

**想手动放文件（需要能访问应用数据目录）**：

```bash
# 通过 SSH 登录 NAS 后
# 1) 上传 zip 到任意目录，先解压
unzip astrbot_plugin_topic_router-*.zip -d /tmp/
# 2) 放进插件目录（注意目录名必须保持 astrbot_plugin_topic_router）
cp -r /tmp/astrbot_plugin_topic_router /vol1/@appdata/astrbot/data/plugins/
# 3) 确认结构正确
ls /vol1/@appdata/astrbot/data/plugins/astrbot_plugin_topic_router/metadata.yaml
# 4) 重启 AstrBot 应用（fnOS 面板里停止→启动，或 docker restart）
```

> 权限提醒：`/vol1/@appdata/...` 属于应用私有目录，某些 fnOS 版本需要 root 或 sudo 才能写入。
> 如果文件管理器进不去，就用 WebUI 上传，没必要为装插件去改权限。

**升级/重装注意**：插件本体在 `data/plugins/` 里，属于应用数据的一部分。常规升级 AstrBot 应用
不会动它，但**卸载应用时如果勾选了"同时删除数据"就会丢**。建议定期备份整个
`data/plugins/` 与 `data/plugin_data/` 目录——后者存着本插件的话题状态
（`plugin_data/astrbot_plugin_topic_router/topic_state.json`）。

---

### 方式 E：macOS / Linux 源码部署

按照官方 [手动部署](https://docs.astrbot.app/deploy/astrbot/cli.html)：

```bash
git clone https://github.com/AstrBotDevs/AstrBot
cd AstrBot
uv sync          # 或 pip install -r requirements.txt
uv run main.py   # 启动；数据目录就是当前目录下的 data/
```

然后：

```bash
# 把插件放进数据目录（注意：这里是 <AstrBot 启动目录>/data，不是 ~/.astrbot）
cp -r astrbot_plugin_topic_router ./data/plugins/
# 重启 AstrBot
```

源码部署时数据目录跟随**启动时的工作目录**，想固定位置可以设环境变量：

```bash
export ASTRBOT_ROOT=/opt/astrbot      # 之后数据都在 /opt/astrbot/data
```

---

## 装完之后的配置

进 **插件 → 插件配置 → 话题路由器**，至少确认这三项：

| 配置 | 建议值 | 说明 |
| --- | --- | --- |
| `mode` | `mark` | 先观察。确认判定准确后再考虑 `auto` |
| `provider_id` | `deepseek/deepseek-v4-flash` | 判定用的模型，格式 `provider_source_id/model`；**填错不影响回复**，只是判定失败 |
| `debug_log` | 打开 | 不然日志里看不到判定结果 |

**不知道 `provider_id` 填什么？** 插件每次加载都会把可用的模型 ID 打进日志，抄一个即可：

```
[话题路由器] 可用的模型 ID（可直接填进 provider_id）：deepseek/deepseek-v4-flash、...
```

> 判定用的模型和你聊天用的模型是分开的。用便宜的快模型即可，成本可以忽略（每轮最多
> 一次单轮调用，且只有启发式拿不准时才触发）。

---

## 验证它真的在工作

发一句话给机器人，然后在 AstrBot 日志里找这两行：

```
[话题路由器] handle_message_v017 被调用 | vX.Y.Z | umo=... | msg='你好' | stopped=False
[话题路由器] NEW-TOPIC | webchat:... | confidence=0.90 | source=llm | topic=... | ...
```

再发一句**接着刚才话题**的话，第二行应该变成 `CONTINUE`。

也可以用指令自查：发给机器人 `/topicweave`，会回一份当前话题状态。

---

## 使用条件与边界

| 条件 | 说明 |
| --- | --- |
| AstrBot ≥ 4.24 | 只用到 `TextPart.mark_as_temp`（4.24+），且**仅 `hint` 模式**需要；`mark` 模式不受版本影响 |
| **内置 Agent Runner** | `provider_settings.agent_runner_type` 必须是 `local`。切到 Dify / Coze / DashScope / DeerFlow 时钩子不触发，插件静默不工作 |
| 一个可用的 LLM Provider | 判定要一次模型调用（可指定便宜模型）。不配也能跑，退化成纯启发式 |
| 平台无关 | QQ / 微信 / Telegram / Discord / WebChat 等所有平台都适用，只要消息能到达 LLM |

**已知边界：**

- **相似度是字面级的**：中文用字符 2-gram，同义改写的相似度会偏低，这类情况靠模型判定兜住；
- **判定会占用一点时间**：只有需要问模型的那一轮会多等一次单轮调用（约几百毫秒），
  其余轮次是纯本地计算；
- **状态按会话（UMO）隔离**：群聊里开 `unique_session` 时每个人是独立会话，互不干扰；
- **插件不会删除任何对话**：`auto` 只新建对话，旧对话完整保留。

---

## 配置项

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `mode` | `mark` | 见上文四种模式 |
| `provider_id` | `deepseek/deepseek-v4-flash` | 判定用模型；留空则跟随当前会话模型 |
| `auto_threshold` | `0.8` | `auto` 模式自动切换所需置信度 |
| `min_new_topic_confidence` | `0.6` | 低于它一律当延续，宁可不切 |
| `always_llm` | `false` | 每轮都问模型（贵，一般不用） |
| `sim_continue` | `0.5` | 相似度高于它就判延续，不问模型 |
| `sim_new_topic` | `0.12` | 相似度低于它且无承接词就判新话题 |
| `time_gap_minutes` | `45` | 冷启动间隔，超过它倾向判新话题 |
| `min_message_length` | `6` | 短于它的消息（如"嗯""继续"）不判定 |
| `history_turns` | `3` | 送给判定模型的近期消息条数 |
| `timeout_seconds` | `8` | 判定模型超时，超时按延续处理 |
| `max_checks_per_hour` | `30` | 每个会话每小时的模型判定上限（0 不限） |
| `new_conversation_notice` | `true` | `auto` 切换时是否在群里说一句 |
| `debug_log` | `true` | 把判定明细写进日志 |

状态文件：`<数据目录>/plugin_data/astrbot_plugin_topic_router/topic_state.json`
（每个会话一条记录，直接删文件等于全部重置）。

---

## 指令

| 指令 | 作用 |
| --- | --- |
| `/topicweave` | 查看当前话题、轮数、本小时判定次数、开关状态 |
| `/topicweave new [名称]` | 手动翻篇：新建对话并重置话题（等于 `/new` + 重置状态） |
| `/topicweave forget` | 忘记当前话题，下一条消息重新建档 |
| `/topicweave off` / `/topicweave on` | 关掉 / 打开**本会话**的自动判断 |
| `/topicweave conflicts` | 列出当前所有指令冲突（哪个指令被哪几个插件注册） |

别名：`话题织机`。

> 指令名为什么是 `topicweave` 而不是 `topic`：AstrBot 用**实时内存注册表**判定指令冲突，
> `topic`、`topicrouter` 这类通用英文词很容易和别的插件撞名，撞了会一次触发多个插件。

---

## 排错

| 现象 | 处理 |
| --- | --- |
| 安装时报 `FileNotFoundError: .../plugin/astrbot_plugin_topic_router` | 包是用系统压缩工具打的（反斜杠条目名 / 缺目录条目）。用 `python build_zip.py` 重新打包，或直接用 release 里的包 |
| 日志里完全没有插件的行 | 插件没装上或被禁用：去 **插件 → 插件** 看列表状态与版本 |
| 有 `模块已导入` 但没有判定日志 | `mode` 是 `off`、或 `enabled` 是 `false`、或 `debug_log` 关了；也可能是这个会话被 `/topicweave off` 关了 |
| 判定结果和实际不符，老是判延续 | 看日志里的 `heuristic_confidence`：启发式就判了延续的话，调低 `sim_continue` 或调高 `sim_new_topic` |
| `[WARN] 判定模型调用失败` | `provider_id` 写错或该 provider 不可用；去日志里抄一个可用的 ID |
| `[WARN] 判定模型超时` | 网络慢或模型慢：调大 `timeout_seconds`，或换成更快的模型 |
| 每轮都被判成新话题 | 说明话题名一直是空的：确认判定模型能正常返回 JSON（换成 flash 这类快的、非推理模型） |
| 升级版本后行为没变 / 报旧版本的错 | **重启 AstrBot 进程**。热重载不会清掉内存注册表里的旧 handler，只有重启才彻底 |
| 提示"检测到指令冲突" | 发 `/topicweave conflicts` 看是谁跟谁撞了；如果是别的插件之间撞名，跟本插件无关 |
| 群聊里几个人互相干扰 | 本插件按会话（UMO）隔离；平台开 `unique_session` 后每人独立 |

---

## 开发与测试

四套离线测试，都不需要启动 AstrBot（除了第一套需要 AstrBot 的 Python 环境来 import 真实 API）：

```bash
# 1) 插件逻辑：124 项（需要 AstrBot 的 python；桌面版在 ~/.astrbot 之外）
python tests/run_tests.py

# 2) 可移植性：16 项（用桩模块，任意 Python 3.10+）
python tests/test_portability.py

# 3) 注册表复用复现：9 项（验证 AstrBot 的 handler 注册表行为）
python tests/reproduce_hook_dispatch.py

# 4) zip 安装流程：21 项（需要 AstrBot v4.28.1 源码，见脚本内提示）
python tests/verify_zip_install.py
```

### 自己打包

```bash
python build_zip.py            # 输出 astrbot_plugin_topic_router-<版本>.zip 到桌面
```

**必须用这个脚本，不要用系统右键压缩**：Windows 的压缩工具写反斜杠条目名、且不写目录条目，
Linux 上的 AstrBot 解包后会抛 `FileNotFoundError`；此外它还会把 `tests/` 带进去，导致安装时
"把目录往自己里面搬"的错乱。

### 代码结构

| 文件 | 作用 |
| --- | --- |
| `main.py` / `__init__.py` | 插件入口，只做转出 |
| `router.py` | handler（模块级）+ 判定流程编排 + 指令 |
| `detection.py` | 启发式打分、模型提示词、结果解析与合并（纯函数，易测） |
| `state.py` | 话题状态持久化（按会话分条） |
| `_conf_schema.json` | WebUI 配置项定义 |
| `metadata.yaml` | 插件元信息 |

> **给改代码的人**：这个插件的 handler 是**模块级函数**（不是类方法），状态放在模块级
> `RUNTIME` / `_CTX` 上，方法体里不允许出现 `self.xxx`。原因写在 `router.py` 文件头——
> 少数环境的 AstrBot 派发 handler 时不绑定实例，`self` 会是事件对象，任何 `self.xxx` 都会抛
> `AttributeError`，而入口的 `except` 会把它静默吞掉（表现为"插件在跑但什么都不输出"）。
> 模块级写法对两种派发契约都安全。

## 许可

MIT，见 [LICENSE](LICENSE)。
