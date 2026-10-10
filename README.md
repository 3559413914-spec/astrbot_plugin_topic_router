# astrbot_plugin_topic_router

让机器人自己判断用户这句话是**接着上一个话题**，还是**开了新话题**。

它挂在 AstrBot 的 `on_llm_request` 钩子上（内置 Agent Runner 的必经之路），每条要发给
模型的用户消息都会先过一遍判定，然后按你选定的模式行动。默认模式是 **mark**：
只记录、只更新状态，**绝不改上下文**。

## 为什么默认不自动切

话题判定误判的代价是不对称的：

- 把「延续」误判成「延续」→ 没损失；
- 把「延续」误判成「新话题」→ 会把用户正在聊的上下文切走，用户会觉得机器人失忆了。

所以插件的默认姿势是「先观察，后行动」：先跑几天 `mark`，看日志里的判定准不准，
再把 `mode` 改成 `auto`。

## 四种模式

| mode | 行为 | 建议 |
| --- | --- | --- |
| `off` | 完全不判定 | 临时关掉 |
| `mark` | **默认**。只写日志、维护话题状态，请求一个字都不改 | 先跑这个 |
| `hint` | 额外往本轮注入一段临时提示（不进历史），告诉模型"已换话题" | 不想换对话、但想让模型别串味 |
| `auto` | 置信度 ≥ `auto_threshold` 时新建一个对话并切过去，旧对话完整留档 | 确认判定准了再开 |

`auto` 做的事和用户手打 `/new` 等价：旧对话不删、随时可以用 `/ls` `/switch` 切回去。
区别只是由插件在后台完成，并且给群里回一句提示（可用 `new_conversation_notice` 关掉）。

## 安装（飞牛 NAS）

1. 打开 `http://<你的NAS地址>:6185` → `插件` → `安装插件` → `从文件安装`；
2. 上传桌面上的 `astrbot_plugin_topic_router-0.1.0.zip`（用 `build_zip.py` 打的包）；3. 上传后 AstrBot 会自动解包、注册并加载（装好后在插件列表里能看到它）；
4. 进 `插件配置` 确认这几项：
   - `mode` 保持 `mark`；
   - `provider_id` 填一个便宜的模型，比如 `deepseek/deepseek-v4-flash`；
   - `debug_log` 打开。

也可以直接把插件目录丢进 NAS 的 `AstrBot/data/plugins/` 下再重启。

### 打包必须用 `build_zip.py`，不要用系统自带压缩

```powershell
python build_zip.py          # 输出到桌面：astrbot_plugin_topic_router-<版本>.zip
```

> 包内目录名固定是 `astrbot_plugin_topic_router/`（AstrBot 认 metadata.yaml 里的 `name`，
> 与压缩包文件名无关）。

**这不是洁癖，是硬要求。** 在 Windows 上用资源管理器「发送到 → 压缩文件夹」或
`Compress-Archive` 打这个包，装到 NAS（Linux）上会直接失败：

```
FileNotFoundError: [Errno 2] No such file or directory:
  '/tmp/.astrbot/.plugin-upload-xxx/plugin/astrbot_plugin_topic_router'
```

原因有两层：

1. **分隔符**：Windows 的压缩工具用反斜杠写条目名。Linux 上反斜杠是**合法的文件名
   字符**，不是路径分隔符，`os.path.normpath` 不会规范化它，于是解包后根本得不到
   `astrbot_plugin_topic_router/` 这个目录；
2. **缺目录条目**：Windows 压缩工具通常只写文件条目、不写目录条目。AstrBot 的
   `_resolve_archive_root_dir` 仍然能从文件条目推断出根目录名（父路径就是包名），
   但解包时没有任何条目让 `extractall` 去**创建**这层目录，收尾步骤
   `os.listdir(<target>/<包名>)` 就抛了上面那个错。

`build_zip.py` 做的正是这两件事：条目名一律正斜杠，且显式写入 `包名/` 目录条目。
另外它**不包含 `tests/`**：包里一旦有子目录，AstrBot 推断出的根目录会退化成整个
目标目录，收尾时会发生"把目录往它自己里面搬"的错乱。

> `tests/verify_zip_install.py` 会用 AstrBot v4.28.1 的官方代码、在 POSIX 路径语义下
> 把这个流程整条跑一遍：既验证当前交付包能装成功，也验证"反斜杠 / 缺目录条目"确实
> 会复现上面那个报错。

## 它挂在哪里（v0.1.4 起）

判定由**消息事件**驱动：`@filter.event_message_type(EventMessageType.ALL)`。

为什么不用 `on_llm_request` 当主入口：AstrBot 的 handler 注册表用
`f"{模块}_{函数名}"` 当键（4.28.1 `core/star/register/star_handler.py:38`），
**键里不含插件版本**，命中已有条目时直接复用旧元数据。于是只要方法还叫
`on_llm_request`，卸载重装后命中的仍是**第一次安装**留下的那条记录——里面存着旧版
函数对象，新代码永远不会被调用（实机表现：函数入口日志一次不出现，却报旧签名的
`TypeError: ... missing 1 required positional argument: 'req'`）。消息事件的调用约定
是确定的（永远只传 `event`），所以判定、状态、日志都由它驱动。

请求钩子仍然保留，但做了两件事：

1. 降级为**可选增强**：只在能拿到 `ProviderRequest` 时执行 `hint` / `auto` 的请求
   改写（拿不到就先挂起，等下一次拿到请求再执行）；默认的 `mark` 模式完全不依赖它；
2. **方法名带版本号**（`hook_llm_request_v015`）：换个从未注册过的名字，才不会命中
   旧条目。改这个方法名时请务必同时改版本号，否则会退回"复用旧函数"的状态。

两个入口可能针对同一条消息先后触发，所以做了去重：键是平台的
`message_id + timestamp`（拿不到时才退回正文），因此**用户真的重发同一句话仍然
算新的一轮**，只有同一条消息被重复投递才会跳过。

## 它怎么判定：两层

**第一层，零成本启发式**（每条消息都跑）：

- 与「话题名称 + 话题摘要 + 上一条消息」的字符 2-gram 相似度；
- 承接词 / 回指词（继续、还有、那个、它…）；
- 显式换话题词（换个话题、顺便问、另一个问题…）；
- 距上一条消息的时间间隔（冷启动）；
- 消息长度（「嗯」「好的」「继续」这类短消息永远算延续）。

**第二层，模型判定**（只在以下情况才花钱）：

- 启发式落在模糊区间（相似度介于 `sim_new_topic` 和 `sim_continue` 之间）；
- 会话里第一次出现话题；
- 或者你打开了 `always_llm`。

模型用一次单轮调用，要求返回：

```json
{"verdict":"continue|new_topic","confidence":0.0-1.0,
 "topic_name":"当前话题的简短名称","topic_summary":"一句话说明",
 "reason":"不超过30字的理由"}
```

解析失败、超时、报错**一律按「延续」处理** —— 判定失败不能影响正常回复。

合并两个信号时是不对称的：只有两边都指向新话题、且置信度够高，才会真的判新话题；
模型半信半疑时一律退回延续。

## 指令

| 指令 | 作用 |
| --- | --- |
| `/topic` | 查看当前话题、轮数、本小时判定次数、开关状态 |
| `/topic new [名称]` | 手动翻篇：新建对话并重置话题（等于 `/new` + 重置状态） |
| `/topic forget` | 忘记当前话题，下一条消息重新建档 |
| `/topic off` / `/topic on` | 关掉 / 打开**本会话**的自动判断 |

## 配置项

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `mode` | `mark` | 见上表 |
| `provider_id` | `deepseek/deepseek-v4-flash` | 判定用模型，格式 `provider_source_id/model`；留空则跟随当前会话模型 |
| `auto_threshold` | `0.8` | 自动切换所需置信度 |
| `min_new_topic_confidence` | `0.6` | 低于它一律当延续，宁可不切 |
| `always_llm` | `false` | 每轮都问模型（贵，一般不用） |
| `sim_continue` | `0.5` | 相似度高于它就判延续，不问模型 |
| `sim_new_topic` | `0.12` | 相似度低于它且无承接词就判新话题 |
| `time_gap_minutes` | `45` | 冷启动间隔，超过它倾向判新话题 |
| `min_message_length` | `6` | 短于它的消息不判定 |
| `history_turns` | `3` | 送给模型的近期消息条数 |
| `timeout_seconds` | `8` | 判定模型超时，超时按延续 |
| `max_checks_per_hour` | `30` | 每个会话每小时的模型判定上限（0 不限） |
| `new_conversation_notice` | `true` | `auto` 切换时是否在群里说一句 |
| `debug_log` | `true` | 把判定明细写进日志 |

不知道 `provider_id` 填什么？插件启动时会把可用的模型 ID 全部打进日志，直接抄一个。

## 验证

1. 装好后看 AstrBot 日志里有没有：

   ```
   [astrbot_plugin_topic_router] 已加载 | 模式 mark | 判定模型 deepseek/deepseek-v4-flash | 状态文件 ...
   [astrbot_plugin_topic_router] 可用的模型 ID（可直接填进 provider_id）：...
   ```

2. 在群里发一句和当前话题无关的话，日志里应出现：

   ```
   [astrbot_plugin_topic_router] NEW-TOPIC | <umo> | confidence=0.90 | source=llm | ...
   ```

   接着追问同一件事，应出现 `CONTINUE`。

3. `/topic` 能看到话题名、轮数和本小时判定次数。

状态文件在 `<AstrBot数据目录>/plugin_data/astrbot_plugin_topic_router/topic_state.json`，
里面每个会话一条记录，直接删掉就等于全部重置。

## 排错

| 现象 | 处理 |
| --- | --- |
| 安装时报 `FileNotFoundError: .../plugin/astrbot_plugin_topic_router` | 包是用系统压缩工具打的（反斜杠条目名 / 缺目录条目）。用 `python build_zip.py` 重新打包再上传，见上一节 |
| 日志里出现 `TypeError: on_llm_request() missing 1 required positional argument: 'req'` | 这是 ≤ v0.1.4 的**注册表复用**问题：AstrBot 按"模块+函数名"复用 handler 元数据，卸载重装后命中的是第一次安装留下的旧函数对象。v0.1.5 把钩子改名为 `hook_llm_request_v015` 绕开了它；判定本身自 v0.1.4 起走消息事件，不受影响。先确认日志里有 `模块已导入 v0.1.5` |
| 装完看不到 `模块已导入 <版本>` | 这份代码没被执行：多半是上传时选到了旧文件（同名不同时间戳）。在插件列表核对版本，必要时卸载 + 重启 AstrBot 再装 |
| 日志里没有 `已加载` | `on_astrbot_loaded` 在热加载时可能已经错过；改用 `模块已导入` / `实例已构造` 确认加载 |
| 一直看不到判定日志 | `debug_log` 关了，或 `mode` 是 `off`，或 `enabled` 是 `false` |
| 判定结果和实际不符，总是判延续 | 看日志里的 `heuristic_confidence`：如果启发式就判了延续，说明 `sim_continue` 太低或 `sim_new_topic` 太高，先调这两个 |
| `[WARN] 判定模型调用失败` | `provider_id` 写错了或该 provider 不可用；去日志里抄一个可用的 ID |
| `[WARN] 判定模型超时` | 网络慢或模型慢，把 `timeout_seconds` 调大，或换成更快的模型 |
| 每轮都被判成新话题 | 说明话题名一直是空的：确认判定模型能正常返回 JSON（关掉思维链模型或换成 flash 这类） |
| 群聊里每个人都互相干扰 | 本插件按「会话（UMO）」隔离；如果平台开了 `unique_session`，每个人是独立会话，不会互相影响 |

## 已知边界

- **只在内置 Agent Runner 下生效**：`provider_settings.agent_runner_type` 必须是 `local`。
  切到 Dify / Coze / DashScope / DeerFlow 时 `on_llm_request` 钩子不触发，插件静默不工作。
- **钩子的 `req` 参数可能缺席**：官方 4.28.1 的调用点确实把 `ProviderRequest` 传给了
  `call_event_hook`，但实机运行中这个钩子出现过只传 `event` 的情况（表现为
  `TypeError: on_llm_request() missing 1 required positional argument: 'req'`）。
  本插件的 `req` 是**可选参数**，拿不到时会依次尝试 `kwargs["request"]` 和事件
  extra 里的 `provider_request`；三者都没有时，`mark` 模式照常判定和记录（日志里
  `has_request: false`），`hint` / `auto` 只记一条 warn 后跳过——不会做半吊子改写。
- **相似度是字面级的**：中文靠字符 2-gram，同义改写的相似度会偏低，这类情况靠模型判定兜住。
- **判定会占用一点时间**：只有需要问模型的那一轮会多等一次单轮调用（约几百毫秒），
  其余轮次是纯本地计算。
- **状态按会话保存**：同一份 `topic_state.json` 里按 UMO 分条，会话之间不串。

## 离线测试

用 AstrBot 自带的 Python 运行时跑（不需要启动 AstrBot，也不会真的调用模型）：

```powershell
$py = "$env:LOCALAPPDATA\AstrBot\backend\python\python.exe"
$env:PYTHONPATH = "$env:LOCALAPPDATA\AstrBot\backend\app;$env:USERPROFILE\.astrbot\data\site-packages"
& $py tests\run_tests.py              # 插件逻辑：90 项
& $py tests\verify_zip_install.py     # 安装流程：20 项
```

- `run_tests.py` 覆盖：按 AstrBot 的加载规则导入插件、metadata/schema 合法性、启发式打分、
  模型输出解析、mark/hint/auto 三种模式、对话切换后的状态作废、每小时上限、模型异常与超时、
  `/topic` 指令全部分支。
- `verify_zip_install.py` 用 AstrBot **v4.28.1 的官方解包代码**（`zip_updater.py` 的
  `_resolve_archive_root_dir` / `_finalize_extracted_archive`），把模块内的 `os` 换成
  POSIX 路径语义替身，在 Windows 上复现 NAS（Linux）的行为：坏包必然报错、当前交付包
  必然装成功。

> Windows 上 `verify_zip_install.py` 的第 [2] 组会 skip：Windows 文件系统不允许文件名
> 里出现反斜杠，物理上造不出 Linux 那种扁平结果；同一步的分水岭已由 [2b] 的纯逻辑断言
> 覆盖。把仓库放到 NAS 上跑一次就能走完整分支。
