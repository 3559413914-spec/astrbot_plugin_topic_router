# 更新日志

## v0.6.0

**已上架 AstrBot 插件市场；仓库地址迁移到 `xun-hen`；README 安装章重排。**

### 插件已上架市场

README 的安装部分重写：**方式 A = 从插件市场安装**（最简单，推荐），
**方式 B = WebUI 上传 zip**（离线/内网/指定版本），
**方式 C = 按部署类型装到对应目录**（Docker / Windows 桌面 / 飞牛 NAS / 源码，降为 C1~C4 子节）。

原来的编号有问题：A（上传）和 B（Docker）在同一个层级，但一个是"装法"、一个是"部署类型"，
读者按顺序看会乱。现在改成先讲装法、再讲"你的部署类型该放哪个目录"。

### 仓库地址迁移到 xun-hen

仓库已从 `3559413914-spec` 迁移到 **`xun-hen/astrbot_plugin_topic_router`**（旧地址 GitHub 会
重定向，但文档里不该再写旧地址）。同步更新：

- `metadata.yaml` 的 `repo` 与 `social_link`；
- README 里的 Releases 链接；
- 作者署名统一为 **[@xun-hen](https://github.com/xun-hen)**（寻痕）。

> 注意：`xunhen`（无连字符）是**另一个人的账号**，之前误用过这个名字，本版已全部纠正。

### 本地 git 身份

这个仓库里曾经有仓库级的 `user.name = 3559413914-spec`，会覆盖全局身份，导致提交作者一直是旧名字。
本版删掉该仓库级配置，之后的提交使用全局身份 `xun-hen`。

## v0.5.3

**修：`provider_id` 配错时判定模型静默失效（实机踩到）。**

实机日志（2026-10-11）暴露的问题：插件启动时打印

```
[话题路由器] 已加载 v0.5.1 | 模式 mark | 判定模型 deepseek/deepseek-v4-flash | ...
```

但同一份日志里 AstrBot 加载的 Provider 实际是 `deepseek/deepseek-flash`（没有 `v4`）。
这个 ID 不存在，于是 `context.llm_generate()` 抛 `ProviderNotFoundError`，被"失败即延续"
的兜底逻辑吞掉 —— 结果是**判定模型那层完全没工作，只剩启发式**，而日志上几乎看不出来
（只在 `_ask_llm` 里留一行 warning）。

三处修改：

1. **`_resolve_provider` 增加存在性校验**：配置的 ID 会先用
   `provider_manager.get_provider_by_id()` 查一遍；查不到就告警并**回退到当前会话模型**，
   不再静默失效。顺带把可用的 ID 一起打进告警，方便直接抄。
2. **启动日志主动告警**：`on_loaded_v017` 里比对"配置的 ID"与"可用列表"，不一致时打印
   `⚠ 配置的判定模型 'xxx' 不在可用列表里 …`。
3. **默认值改为留空**（`_conf_schema.json`）：留空 = 跟随当前会话模型，最不容易配错。
   原来硬写 `deepseek/deepseek-v4-flash` 是在没有对应 Provider 的环境里保证出错。

新增测试 `[15] provider_id 配错时的回退`：配错时回退到当前会话模型、配对时按配置使用、
留空时跟随会话，以及**回退之后模型判定确实执行了**（不是静默失效）。
测试 135 → 139 项。

## v0.5.2

- 修 CHANGELOG 的 Markdown 格式：v0.5.1 小节和 v0.5.0 标题之间补上空行（之前贴在一起，
  渲染出来会粘成一坨）；
- 打包器新增 `check_changelog_format()`：打包前检查首行标题、每个版本小节前后是否有
  空行、同一版本号是否重复出现、以及是否包含当前版本小节，不合格拒绝打包。

## v0.5.1

- 作者署名从 `dsh` 改为 GitHub 账号 **[@xun-hen](https://github.com/xun-hen)**（`metadata.yaml`
  的 `author` 字段与 README 同步）；插件市场上展示的作者名即取此字段。

## v0.5.0

**为发布到 AstrBot 插件市场做收尾：元数据补全、图标、贡献指南。**

### 元数据补全（`metadata.yaml`）

对照官方[插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)补齐可选字段——
必填字段（`name` / `desc` / `version` / `author`）本来就合法，缺的是让市场卡片能看的那批：

- `display_name: 话题路由器`（展示名，之前卡片上只有 `astrbot_plugin_topic_router`）；
- `short_desc`（卡片短描述，缺了会回退显示一大段 `desc`）；
- `tags`（市场分类与搜索用）；
- `social_link`；
- `desc` 改成多行，写清特性。

**刻意没填 `astrbot_version`**：官方格式是 PEP 440（如 `">=4.17.0"`），但本插件只在
AstrBot 4.28.1 上验证过，`hint` 模式依赖的 `TextPart.mark_as_temp()` 是 4.24+ 才有。
填一个没验证过的下限，会把本来能用的用户直接挡在门外，所以等有实际不兼容反馈再加。

### 新增插件图标 `logo.png`

256×256、1:1、圆角。图形是两个话题分支：下方亮线是当前话题，上方虚线从分岔点离开代表
新话题，分岔处的白点表示"上下文在这里切换"。

注意 AstrBot 的机制是**把 `logo.png` 放在插件目录**，元数据里**没有** logo 字段。

### 打包器加了图标自检

`check_logo()` 会在打包前校验：PNG 有效性、是否 1:1、是否 256×256、是否超过 512KB。
和版本一致性自检一样，不合格直接拒绝打包。

### `AGENTS.md` → `CONTRIBUTING.md`

原来的 `AGENTS.md` 是给 AI 会话用的项目指令，里面写着推送冻结记录、本机路径等只对本地环境
有意义的内容，不适合放在公开仓库。现在改成面向贡献者的 `CONTRIBUTING.md`：讲清环境、五套
测试怎么跑、提交规范，并把六条"被实机踩出来的约束"整理成「改代码前必读」
（handler 必须是模块级、入口要吞异常、指令名避开通用词、失败即延续等）。

"""

## v0.4.1

**README 顶部新增「原理」一节**：一张流程图 + 三个关键设计，放在最前面，读者不必翻到
后面才知道它怎么判的。

- 流程图讲清顺序：启发式（免费、每条都跑）→ 结论明确就直接用 → 拿不准才问一次便宜模型
  → 模型失败一律当「延续」；
- 三个关键设计：① 两个信号都指向新话题才敢判新话题；② 不拿"字面不重叠"当证据（中文追问
  字面相似度天然为 0）；③ 判定是旁路，失败不影响正常聊天；
- 原来的「它能做什么」里那段较细的两层机制说明删掉，改为指向「原理」，避免重复。

## v0.4.0

**用真实聊天记录建评测，并据此修掉一个高频误判；判定模型现在能看到机器人上一句回复。**

这一版的两个改动都是"先测再改"，不是拍脑袋。

### 新增：真实数据评测台 `tests/eval_topic_detection.py`

- 直接读 AstrBot 的 `data_v4.db`，把**真实会话**（本次实测 5 个会话 / 65 轮，最长一段
  42 轮）灌进判定流水线，输出混淆矩阵、精确率/召回率、模型调用占比、置信度分布、
  误判样例；
- 支持换阈值跑敏感性分析（`--sim-new-topic` 等），结果可写 JSON 便于前后对比；
- `--compare-legacy` 一条命令跑出"旧启发式 vs 当前"的行为对比，防止以后回退；
- 零网络：纯启发式评估不发任何请求；真值口径（时间间隔代理 / 手工标注 / 保底口径）
  在报告里写明，不假装是真值。

### 修掉：短句被凭"字面不重叠"判成新话题

评测第一轮就暴露了问题：65 轮里**启发式直接判了 38 轮新话题、模型一次都没问**，
其中 **30 轮是短句**——「我没有代理啊」「等等吧还没做完」「为啥直接闪退？」这类追问
与话题名的字符相似度是 0.00（中文追问换词彻底，字面重叠天然趋近 0），却被当成新话题。

改动：**字面低相似在短句上没有信息量，这类消息不再由启发式定性，而是交给模型判语义。**

| 指标（同一份 65 轮真实数据） | 旧 | 新 |
| --- | --- | --- |
| 判定为新话题的轮数 | 39 | **13** |
| 其中短句（≤25 字）被判新话题 | 31 | **5** |
| 会去问模型的轮数 | 13 | 37 |

另外调整了判定优先级，避免新规则带来副作用：

- **冷启动提到最前**：隔了 45 分钟再开口，即使只有「嗯」「在吗」两三个字也判新话题，
  不会为了短句白问一次模型；
- **极短纯指代（≤12 字，如「那它呢」）直接判延续**，不花模型调用；
- 长句零重叠仍由启发式直接判新话题（保留原有的省钱路径）。

新增配置 `short_message_length`（默认 25）：设成 0 即恢复旧行为（不推荐）。

### 新增：机器人上一句回复进入判定提示词

判定模型此前只看到"当前话题 + 最近几条**用户**消息"，看不到上一轮机器人答了什么，
缺少承接关系的锚点——而很多换话题恰好发生在回答之后（"好的，那再问个别的"）。

- 新增 `on_llm_response` 钩子捕获回复，按会话临时中转，落进 `TopicState.last_assistant_text`
  （截断 1000 字，避免状态文件被长回复撑大）；
- 判定提示词新增一行 `机器人上一句回复：…`；
- 新增配置 `include_assistant_reply`（默认开），关掉可略微省 token；
- 日志的判定明细里多一个 `has_reply` 字段，便于确认回复确实被用上了。

### 测试

- `tests/run_tests.py` 130 项 → **135 项**：新增短追问句交模型、极短指代不调模型、
  长句零重叠仍直接判新话题、冷启动优先于短句规则、提示词包含回复等用例；
- `tests/test_portability.py` 补 `on_llm_response` 桩装饰器。

## v0.3.2

**按官方文档与源码核实，修正部署文档里几处不准的说法。**

- **数据目录口径统一**：官方文档对"数据目录"的写法不统一（`data/`、`AstrBot/data`、
  `~/.astrbot`…）。实际规则只有一条：根目录 = `ASTRBOT_ROOT` 环境变量 > 桌面打包运行时
  `~/.astrbot` > 其他情况**当前工作目录**，再拼 `data/`。目录表据此补齐了
  **一键启动器的两种数据位置**（新版可视化在 `~/.astrbot_launcher`，旧版 PowerShell
  脚本在与 `.bat` 同级目录）。
- **桌面客户端默认只监听本机**：WebUI 是 `127.0.0.1:6185`，要让别的设备访问得改
  `cmd_config.json` 的 `dashboard.host` 为 `0.0.0.0`（或用 `ASTRBOT_DASHBOARD_HOST`）。
- **Python 要求写准**：AstrBot v4 要求 **Python ≥ 3.12**（旧版安装脚本的 3.10–3.13 是脚本
  自身要求，不是 AstrBot 的要求）；补上 `uv run --no-sync main.py` 与 `uv tool install
  astrbot` 两种官方装法及其限制（uv tool 方式不能在 WebUI 里升级版本）。
- **Docker 补全**：贴出官方 `compose.yml` 要点（唯一环境变量是 `TZ`、6185/6199 两个端口、
  `./data:/AstrBot/data`）；补上容器内代理该怎么写（`host.docker.internal` / docker0 网关）；
  说明群晖·威联通官方没有专章，CasaOS·宝塔·1Panel·K8s 有。
- **zip 规则按源码写精确**：`metadata.yaml` 必须在根目录、四个必填字段、UTF-8、≤1MB、
  `name` 必须是合法 Python 标识符；**外层套一层目录可以，多层没有官方承诺**；
  安装后立即加载，不需要重启。
- 修正上一版 fnOS 章节（见 v0.3.1）：该环境是 fnOS **原生应用**而非 Docker，重启方式不同。

## v0.3.1

## v0.3.0

**面向所有用户重写文档：不再只讲飞牛 NAS，覆盖各种部署方式。**

- README 重写为使用者视角：开头讲清它能做什么，然后是**五种部署方式**的完整步骤：
  - 方式 A：WebUI 上传（所有平台通用，推荐）
  - 方式 B：Docker 部署的 AstrBot（docker cp / 宿主机挂载目录 / 群晖·威联通·CasaOS·1Panel）
  - 方式 C：Windows 桌面客户端（含 %USERPROFILE%\.astrbot\data\plugins 路径）
  - 方式 D：飞牛 NAS（fnOS）
  - 方式 E：macOS / Linux 源码部署（含 ASTRBOT_ROOT）
- 新增「插件到底装在哪个目录」对照表：Docker 是容器内 /AstrBot/data/plugins（官方 compose
  的挂载点），桌面版是 ~/.astrbot/data/plugins，源码部署是 <启动目录>/data/plugins。
- 新增速查：装完怎么配置、怎么验证真的在工作、使用条件与边界、完整配置表、指令表、排错表。
- metadata.yaml：描述与 help 改为面向使用者的文案，补上 
epo 地址，help 用多行格式
  列出全部子指令。
- 测试脚本去掉剩余的本机绝对路径写法，测试说明同时给出 Windows / Linux-macOS 两种跑法。

## v0.2.2

**可移植性收尾：确认别人也能用。**

- 测试脚本不再写死本机绝对路径：AstrBot 运行路径改为自动探测
  （ASTRBOT_APP_PATH / ASTRBOT_SITE_PACKAGES 环境变量优先，其次桌面版默认位置）；
  
erify_zip_install.py 找不到上游 v4.28.1 源码时给出明确指引并跳过，而不是报错。
- 新增 	ests/test_portability.py：用**桩模块**隔离 AstrBot 依赖，因此任意 Python 3.10+
  都能跑。它同时模拟两种 handler 派发契约（绑定方法 / 未绑定函数）与零参数调用
  （on_astrbot_loaded 的真实形态），16 项断言全过 —— 这是别人装上也能用的证据。
- 新增 
equirements.txt（本插件零第三方依赖）与 README「谁能用 / 使用条件」一节，
  写清版本、Runner、Provider 三个前置条件。

## v0.2.1

**根治：所有 handler 改成模块级函数、不声明 self。**

实机重启后的纯信号日志（僵尸条目已清空，只剩真相）：

`
core.core_lifecycle:372  hook(on_astrbot_loaded) -> None - on_loaded_v017
TypeError: TopicRouterPlugin.on_loaded_v017() missing 1 required positional argument: 'self'
`

AstrBot 的调用是 wait handler.handler()（core/core_lifecycle.py:375），**不传 self**。
正常环境里注册表存的是**绑定方法**（self 已绑定）所以没事；这个环境里注册进去的是
**未绑定的类函数**，于是：

- 要求 self 的 handler → missing 1 required positional argument: 'self'；
- (self, event, req) → missing 'req'（event 落进 self）；
- (self, *args) → 事件被塞进 self（日志里 self=WebChatMessageEvent）。

**修法**：handle_message_v017 / hook_llm_request_v017 / command_topic_v017 /
on_loaded_v017 全部改为**模块级函数**，签名是 (*args, **kwargs)，用 isinstance
自己认出事件与请求。模块级函数没有 self 这个位置，AstrBot 怎么调都落对。

- _extract() 允许零参数（on_astrbot_loaded 就是 handler() 零参调用）。
- 新增回归用例 [12] handler 的形态：逐个断言四个 handler 是模块级、无必填参数，
  并像 AstrBot 那样**零参数调用 on_loaded_v017()**、以三种方式调用消息 handler。
- 状态仍全部在模块级 RUNTIME / _CTX 上。

## v0.2.0

- **改：去掉英文别名 	opicrouter**（只留 话题织机）。	opic 和 	opicrouter 都是
  容易与别的插件撞名的英文常用词；指令冲突是 AstrBot **实时读内存注册表**判定的
  （command_management._collect_descriptors），撞了就会一次触发多个插件。
- **新增：/topicweave conflicts** —— 直接读 handler 注册表，把当前所有冲突的
  「指令名 ← 是哪些插件注册的」列出来并写进日志，方便定位到底该改谁。
- 说明：面板上如果仍显示 /topic 冲突，那一条**不是本插件当前版本注册的**
  （v0.1.9 起注册的是 	opicweave）。AstrBot 的卸载/热重载不会清掉内存注册表里的
  旧条目，**重启进程**后才会消失。

## v0.1.9

- **改：指令名 `/topic` → `/topicweave`**（别名 `话题织机`、`topicrouter`）。
  AstrBot 的「检测到指令冲突」把 `/topic` 和别的插件判成了同一指令，一次触发会让
  多个插件同时响应。`topic` 太通用，换成不会撞的名字；两个别名方便输入。
- `/topicweave` 的用法与之前完全一致：`/topicweave`、`/topicweave new [名称]`、
  `/topicweave forget`、`/topicweave off|on`。

## v0.1.8

**架构修正：状态与逻辑从 `self` 搬到模块级——这是本插件能在这个环境跑起来的关键。**

实机（4.28.1 + webchat）反复出现同一个现象：插件加载正常、版本日志正常，但判定
日志一条都没有。日志最终给出了铁证：

```
hook_llm_request_v016 被调用 | self=WebChatMessageEvent | has_event=False | args=['ProviderRequest']
AttributeError: 'WebChatMessageEvent' object has no attribute '_request_from_event'
```

**这个环境派发插件 handler 时没有把实例绑到 `self`，`self` 拿到的是事件对象。**
于是任何 `self.xxx` 都会抛 `AttributeError`；而 `handle_message` 的入口日志外面正好
包着 `except`，异常被静默吞掉 → 表现就是"插件在跑，但什么都不输出"。

- 状态（config / store / recent / pending_actions / ctx）全部移到模块级单例
  `RUNTIME` 与 `_CTX`；
- 判定逻辑改为**模块级函数**（显式接收 `ctx`），可离线测试；
- 三个 handler（`handle_message_v017` / `hook_llm_request_v017` /
  `command_topic_v017`）只做"认参数 → 转调模块函数"，参数识别只用 `isinstance`，
  方法体里**不出现任何 `self.xxx`**；
- `on_loaded_v017` 同样不依赖 `self`；
- 测试同步到新架构（模块级状态需要显式重置），111 项全过。

## v0.1.7

- **修：判定处理器可能压根没轮到执行**。AstrBot 的 `star_request.py` 是
  `for handler in activated_handlers: if event.is_stopped(): break`，而任何一个
  handler 抛异常都会 `event.stop_event()`。实机上注册表里残留的旧 handler 先抛
  `TypeError` → 事件被停掉 → 排在后面的 `on_message` **一次都没执行**，日志里因此
  只有报错、没有判定结果。
  - 判定处理器改名为 `handle_message_v017`（同样避开"按名复用旧条目"），并加**入口
    日志**，一眼看出"轮到我没有"；
  - `_detect` 外层加兜底：判定层任何异常都自己吞掉并记全栈，绝不 `stop_event()`
    连累同一条消息上的其它插件。
- 附：那两条零点报错来自注册表里的**内存僵尸条目**，与磁盘上的代码无关，**重启
  AstrBot 进程**后消失。

## v0.1.6

- **修：钩子的 `self` 可能不是插件实例**。实机日志（4.28.1 + WebChat）显示，改名后的
  钩子确实被调用了，但第一个位置参数是事件对象：
  `AttributeError: 'WebChatMessageEvent' object has no attribute '_request_from_event'`
  ——也就是这个派发链把函数按**未绑定**方式存/调用，`self` 落到了事件上。
  现在钩子用 `*args/**kwargs` 接住一切，自己按类型认出"事件"与"请求"，并且
  **不依赖 `self` 做任何判断**（`_looks_like_event` 提升为模块级函数），方法体加了
  总兜底：任何异常只记一行 warning，不再往日志里刷 traceback。
- 判定主路径（消息事件）不受影响；`hint` / `auto` 在这类派发下会自动降级。

## v0.1.5

**找到并修掉了真正的根因：AstrBot 的 handler 注册表按"模块+函数名"复用旧条目。**

`astrbot/core/star/register/star_handler.py:38`（4.28.1）：

```python
handler_full_name = f"{awaitable.__module__}_{awaitable.__name__}"
md = star_handlers_registry.get_handler_by_full_name(handler_full_name)
if md:
    return md          # 命中就复用旧元数据，新函数根本不注册
```

这个键**不含插件版本**。于是从 v0.1.0 起，卸载重装时模块路径与方法名都没变，
注册表命中的始终是**第一次安装**留下的那条记录——里面存着旧的 `on_llm_request`
函数对象。结果是：新代码确实被加载、实例也构造了（日志能看到 `模块已导入 v0.1.4`），
但被派发的永远是旧的 `(self, event, req)`，所以怎么改签名都没用，连函数入口的日志
都一次不会出现。

- **修**：把请求钩子改名为 `hook_llm_request_v015`。名字变了 → full name 变了 →
  拿不到旧条目 → 注册到的是本类当前的方法。
- **验证**：新增 `tests/reproduce_hook_dispatch.py`，用官方注册代码复现"同名被复用"，
  并验证改名后新方法真的被执行（9 项断言）。

## v0.1.4

- **改：判定入口从 LLM 请求钩子换成消息事件**（`@filter.event_message_type(ALL)`）。
  消息事件的调用约定是确定的（永远 `handler(event)`），不受上面的注册表复用问题影响。
- **`on_llm_request` 降级为可选增强**：只在能拿到 `ProviderRequest` 时执行 `hint` /
  `auto` 需要的请求改写；拿不到就先挂起，等下一次钩子拿到请求再执行。
- **新增去重**：去重键用平台的 `message_id + timestamp`（拿不到时才退回正文），
  所以用户真的重发同一句话仍然算新的一轮。
- 判定日志新增 `has_request` 字段。

## v0.1.3

- **新增诊断**：模块导入时打印版本号与磁盘真实路径，并在插件数据目录写
  `imported.txt` 标记文件；每次请求钩子被调用也打印调用现场。
  正是这两样定位到了"包是新的、跑的却是旧函数"。
- 新增 `CHANGELOG.md`（AstrBot 会提示插件缺少 changelog）。

## v0.1.2

- `on_llm_request` 的 `req` 改为可选参数并加多路回退（事后看无效，因为压根没进函数）。
- 新增 `build_zip.py`（正斜杠条目 + 目录条目，输出到桌面）、
  `tests/verify_zip_install.py`（用 AstrBot v4.28.1 官方解包代码在 POSIX 语义下
  验证安装流程）。

## v0.1.1

- 首次尝试修复，无效：报错是"参数没被传进来"，而我只改了参数默认值。

## v0.1.0

- 首个版本：启发式 + 便宜模型的二层话题判定，`off / mark / hint / auto` 四种模式，
  `/topic` 指令，按会话保存的话题状态。
