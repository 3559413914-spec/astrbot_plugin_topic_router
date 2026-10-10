# 更新日志

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


