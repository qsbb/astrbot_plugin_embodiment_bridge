# AstrBot 4.26.8 本地加载失败审计

审计日期：2026-08-04。范围只包括本地 `astrbot_plugin_quest_avatar_bridge`；没有连接、上传、重载或重装远端 AstrBot。

## 已验证事实

- `metadata.yaml` 是无 BOM 的 UTF-8，插件名为 `astrbot_plugin_quest_avatar_bridge`，作者为 `qsbb`，版本为 `0.2.0`；与 `main.py` 的 `__version__` 一致。
- 主类 `QuestAvatarBridgePlugin` 继承 `Star`，构造参数是 `Context` 与 `AstrBotConfig`，类名符合 AstrBot 4.26.8 的插件发现规则。
- 所有 21 个 HTTP/SSE、配对与 Dashboard 管理接口均只使用 `Context.register_web_api(route, handler, methods, desc)` 四参数公开签名；没有 `register_websocket`、匿名路由参数或旧装饰器。
- Page 位于 `pages/pairing/` 和 `pages/operator/`，标题资源位于 `.astrbot-plugin/i18n/zh-CN.json`。AstrBot Pages 按该目录结构自动发现，不需要 `page.json` 或额外注册方法。
- 运行时依赖为 `pydantic`、`qrcode`、兼容范围 `aiohttp>=3.11.18,<4`，以及 Python 3.13+ 条件依赖 `audioop-lts`。AstrBot 4.26.8 本身要求 Python 3.12+；本地审计环境为 Python 3.12。
- 未安装“知、序、情、境、声、核”任一提供方、未配置配对代理或提供方返回畸形契约时，初始化均降级，不抛出加载异常。`terminate()` 会关闭一次性配对状态，取消会话/轮次，关闭 SSE 队列并释放 LLM、STT、TTS 与只读适配器。
- 内置 listener 默认关闭；监听配置非法、端口占用/权限失败、public URL 缺失、loopback 上游不可达以及旧 `pairing_exchange_proxy_url` 缺失都只会让 listener/bootstrap 降级或失败关闭，不应导致插件模块加载失败。基础 AstrBot 官方路由仍可用。

## 已复现并修复的公开 API 不兼容

AstrBot 4.26.8 的 `astrbot.api.web.request` 公开直接来源字段是 `request.client_host`，没有 `request.remote_addr`。旧代码访问后者时会得到属性错误并降级为 `invalid`，使可信代理来源与 Quest IP 绑定永远无法通过。

Bridge 已改为只读取 `request.client_host`，HTTP harness 也只暴露该公开字段，并用真实本机 HTTP 路由覆盖配对 create/exchange/replay。这个问题会破坏首次配对，但它发生在插件 handler 已加载之后，不能单独解释“模块导入或热加载立即失败”。

## 已复现并缓解的构造期路由残留

AstrBot 4.26.8 的 `Context.registered_web_apis` 是进程级注册表，公开 API 只有注册，没有注销。旧构造顺序在 9 条 Bridge 路由注册之后才创建配对管理器与配对 API；一旦后续构造失败，已经注册的绑定方法会继续引用半初始化对象，插件删除或失败清理也不能撤销这些路由。

Bridge 现已先完成所有组件构造和配对 bootstrap 校验，再把 21 条路由注册作为构造函数最后的动作。回归测试会强制配对 API 构造抛错，并验证 Context 中没有留下任何路由。该修复消除了插件自身后续构造失败造成的残留；如果 AstrBot 的 `register_web_api` 本身在第 N 条注册时异常，插件侧仍无法原子回滚，这需要 Core 提供 unregister 或事务式注册能力，本插件不会修改 Core。

## 目前无法证实的远端根因

远端插件已由用户手动删除，且本轮禁止连接远端，因此没有远端 failed-plugin traceback、实际安装目录清单或上传包哈希可供比对。不能把任何推测描述为唯一根因。

审计期间曾在插件根目录存在完整 AstrBot 源码临时目录 `.tmp_astrbot_core_4268_audit`，现已删除，并由 `.gitignore` 的 `.tmp_*/` 规则阻止误提交。如果上传工具直接压缩工作目录而不遵守 Git 忽略规则，它仍可能把 `.git`、缓存或临时目录带入包中；因此该临时目录只能列为包污染风险，不能在缺少远端包证据时认定为已证实根因。

## 最小后续验证建议

在用户重新授权安装之前不要操作远端。未来应从显式文件白名单构建干净包，排除 `.git`、`.tmp_*`、`__pycache__`、`.pytest_cache` 和 `.ruff_cache`；记录包哈希。安装后先读取官方 failed-plugin traceback，确认 `activated=true`，再启用并验收内置 8520 listener；外部精确代理只作为兼容方案。不要用 `.gitignore` 代替打包白名单。

## 内置 listener 生命周期验收

本地回归必须确认：

- 插件构造阶段不绑定端口；只有 `initialize()` 启动 listener。
- 端口占用时 `initialize()` 不抛出导致插件加载失败，health/overview 仅公开 `bind_failed` 等脱敏 reason。
- `terminate()` 可重复调用，且关闭后同一端口能被新 listener 立即重新绑定。
- 活动 SSE 在下游断线或 terminate 时释放上游连接，不残留 `ClientSession`、runner 或后台任务。
- 内置 listener 不增加 `register_web_api` 路由；AstrBot 注册路由数现为 21（包含两个 persona 管理路由和一个脱敏诊断路由）。

生产白名单必须新增 `transport/builtin_listener.py`，并包含本次接线修改的 `main.py`、`core/pairing.py`、`transport/pairing.py`、`transport/http_sse.py`、`requirements.txt`、`_conf_schema.json`、专项测试及配套文档。打包时不得包含本地测试缓存或真实配置/密钥。
