# 更新日志

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
- **文档**：方法名带版本号的原因写进了代码注释与 README，避免以后有人改回去。

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


