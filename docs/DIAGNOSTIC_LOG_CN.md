# Bridge 独立诊断日志

独立诊断日志默认关闭。启用插件专属配置中的 `diagnostic_log_enabled` 后，日志写入：

```text
data/plugin_data/astrbot_plugin_embodiment_bridge/embodiment_bridge.log
```

可选配置：

```text
diagnostic_log_enabled=true
diagnostic_platform_log_enabled=false
diagnostic_plugin_timing_enabled=true
diagnostic_log_max_bytes=1048576
diagnostic_log_backup_count=3
```

`diagnostic_plugin_timing_enabled` 只在独立诊断日志开启时生效。启用后，临创建的
EventBus 轮次会对已注册插件的协程钩子记录方法边界耗时：插件显示名、模块、hook、方法名、
priority、状态、是否停止事件、异常类型和 `duration_ms`。耗时是 wall-clock 时间，包含该钩子
内部的异步等待、Provider、数据库和网络等待。它不会记录任意私有 helper 的逐函数耗时，也不
记录正文、提示词、身份、Provider 配置、凭据、音频或路径。默认关闭；关闭后已安装的包装会被
解除，不保留运行时开销。

文件达到大小上限后轮转为 `.1`、`.2` 等备份。写入使用有界异步队列和进程内锁；目录只读、磁盘满或轮转失败时，日志器进入降级状态并停止继续写入，不影响 HTTP、SSE、LLM、STT、TTS 或插件生命周期。

## Bridge 自有诊断接口

Bridge 声明 `series.diagnostics@1.0` 提供方，可被“核”的 series diagnostics 自动发现和聚合。提供方复用插件自己的有界内存环形快照；JSONL 文件、Dashboard 管理 Page 和日志归属仍由 Bridge 独立管理：

```text
GET /api/v1/plugins/extensions/astrbot_plugin_embodiment_bridge/pairing/diagnostics
```

该路由受 AstrBot Dashboard/plugin-scope 认证保护，返回的事件只含阶段、组件、错误类型、错误 code、状态和耗时等固定脱敏字段。该 Bridge 管理接口使用 `embodiment_bridge.diagnostics@1.0` 标识；系列提供方读取同一内存快照时投影为 `series.diagnostics@1.0`，不会把文件日志交给其他插件，也不会改变存储策略。

Operator Page 的“Bridge 脱敏诊断”区域调用该接口并展示固定字段，不展示正文、身份或凭据。

## EventBus 生命周期耗时

具身消息进入 AstrBot EventBus 时会记录一条受限的生命周期时间线：

```text
event_created -> event_enqueued -> event_cleanup_called -> event_woken -> event_completed
```

每个阶段只记录相对首个事件的毫秒数、事件类型、UMO 是否为三段合法结构、消息类型和有界队列长度；不会记录原始平台、用户、机器人或会话标识。若达到截止时间，会额外记录 `event_wait_timeout`：

- `not_consumed_or_scheduler_missing`：尚未观察到 AstrBot Scheduler 的 cleanup；优先检查平台实例 ID、UMO 路由和配置 Scheduler。
- `pipeline_pending`：事件已经进入管线但仍未完成；继续检查 Provider、记忆、工具、后处理或 TTS 阶段。

`last_event_timing` 和 `last_event_cleanup_called` 会随认证后的健康状态返回，便于把“事件未消费”和“管线处理过慢”分开判断。

若 `diagnostic_platform_log_enabled=true`，插件还会向 `astrbot.plugin.astrbot_plugin_embodiment_bridge` 专属 logger 输出同样的结构化摘要，供 AstrBot 平台日志页显示。插件不注册 handler、不修改 root logger，也不把独立文件改成总日志；该开关默认关闭。升级复制的旧 `quest_avatar_bridge.log*` 只作历史保留，新进程不会继续写入。

插件方法记录的事件形如：

```text
plugin_hook.completed plugin_name=情 plugin_module=... hook=OnLLMRequestEvent method=on_llm_request duration_ms=7385 status=ok
```

当前覆盖 LLM/Agent/工具/发送阶段的 10 类 AstrBot 协程 hook，不覆盖普通平台消息、命令、
插件加载生命周期或异步生成器；因此它是“具身对话链钩子耗时”而不是任意 Python 函数的全量
profiler。需要进一步定位某个插件内部 helper，应由该插件提供自己的脱敏 span。

## 分层 Trace（timing.span.completed）

启用 `diagnostic_log_enabled=true` 后，每个具身轮次会建立一棵仅存在于进程内的脱敏 Trace。
它覆盖轮次、STT、EventBus/直连 Provider、关系/记忆/环境适配器、TTS 和终端交付；Trace 不会
进入 HTTP/SSE 正式协议，也不会把用户文本、Provider ID、路径、身份或凭据写入日志。关闭诊断
日志时不会创建监控任务或 span。

典型事件为 `timing.span.completed`，字段含义如下：

| 字段 | 含义 |
| --- | --- |
| `wall_ms` | Span 从开始到结束的墙钟耗时，包含异步等待 |
| `active_ms` | 扣除已记录的嵌套并发区间和显式等待后的估算活跃时间 |
| `queue_wait_ms` / `lock_wait_ms` / `provider_wait_ms` | 队列、锁、Provider 等待时间 |
| `provider_request_offset_ms` / `provider_first_token_offset_ms` / `provider_end_offset_ms` | 相对本轮开始的 Provider 请求、首 Token/块和结束标记偏移 |
| `provider_first_token_ms` / `provider_total_ms` | 请求标记到首 Token/块、请求标记到结束标记的耗时；Core 未提供标记时为 0 |
| `cache_hit` / `retry_count` / `timeout` / `fallback` | 固定布尔/计数诊断标记；未知时使用安全默认值 |
| `event_loop_lag_ms` | 50 ms 采样器观察到的本轮最大事件循环延迟 |
| `span_id` / `parent_span_id` / `trace_id` | 仅用于同一轮脱敏关联，不是会话或用户标识 |

`active_ms` 不是 CPU profiler 结果，而是基于墙钟区间的估算；并发子任务会先做区间合并，
因此不会把同时等待的插件重复相加。Hook 的 span 仍是方法边界耗时，若要定位某个插件内部
helper，应由该插件自行提供相同字段的 span。Provider 的首 Token/首 chunk 等 Core Trace
标记会以 `timing.trace_point` 记录；当前 AstrBot 未提供标记时，Span 仍可区分排队、请求和
交付阶段，但不会伪造首 Token 时间。

## 动作边界

普通对话轮次不再启动快速动作 Provider，也不把动作工具、动作枚举、动作反馈或历史动作事实
交给主 LLM。主模型只输出 `should_reply` 与 `reply_text`；本地明确指令、触碰/手势通道和客户
端回执仍可驱动动作。为兼容旧适配器，编排层还会在直连/降级路径再次把遗留动作字段收敛为
`talk` 或 `idle`，并记录 `avatar.action.sanitized`。动作执行完成必须以客户端回执为准，主模型
不得声称动作已经完成。回执事实只保留在本地动作控制器和诊断链，不进入 EventBus、记忆或
普通对话上下文。

日志绝不写入 Bridge/API/Provider key、JWT、URL、路径、正文、音频或任何 session/turn/person/platform/user/bot 标识。平台 logger 写入失败同样不会影响插件行为。
