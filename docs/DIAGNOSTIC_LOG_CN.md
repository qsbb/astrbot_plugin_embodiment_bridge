# Bridge 独立诊断日志

独立诊断日志默认关闭。启用插件专属配置中的 `diagnostic_log_enabled` 后，日志写入：

```text
data/plugin_data/astrbot_plugin_quest_avatar_bridge/quest_avatar_bridge.log
```

可选配置：

```text
diagnostic_log_enabled=true
diagnostic_platform_log_enabled=false
diagnostic_log_max_bytes=1048576
diagnostic_log_backup_count=3
```

文件达到大小上限后轮转为 `.1`、`.2` 等备份。写入使用有界异步队列和进程内锁；目录只读、磁盘满或轮转失败时，日志器进入降级状态并停止继续写入，不影响 HTTP、SSE、LLM、STT、TTS 或插件生命周期。

## Bridge 自有诊断接口

Bridge 声明 `series.diagnostics@1.0` 提供方，可被“核”的 series diagnostics 自动发现和聚合。提供方复用插件自己的有界内存环形快照；JSONL 文件、Dashboard 管理 Page 和日志归属仍由 Bridge 独立管理：

```text
GET /api/v1/plugins/extensions/astrbot_plugin_quest_avatar_bridge/pairing/diagnostics
```

该路由受 AstrBot Dashboard/plugin-scope 认证保护，返回的事件只含阶段、组件、错误类型、错误 code、状态和耗时等固定脱敏字段。该 Bridge 管理接口仍使用 `quest_avatar_bridge.diagnostics@1.0` 标识；系列提供方读取同一内存快照时投影为 `series.diagnostics@1.0`，不会把文件日志交给其他插件，也不会改变存储策略。

Operator Page 的“Bridge 脱敏诊断”区域调用该接口并展示固定字段，不展示正文、身份或凭据。

若 `diagnostic_platform_log_enabled=true`，插件还会向 `astrbot.plugin.astrbot_plugin_quest_avatar_bridge` 专属 logger 输出同样的结构化摘要，供 AstrBot 平台日志页显示。插件不注册 handler、不修改 root logger，也不把独立文件改成总日志；该开关默认关闭。

日志绝不写入 Bridge/API/Provider key、JWT、URL、路径、正文、音频或任何 session/turn/person/platform/user/bot 标识。平台 logger 写入失败同样不会影响插件行为。
