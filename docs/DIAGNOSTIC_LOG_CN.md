# Bridge 独立诊断日志

独立诊断日志默认关闭。启用插件专属配置中的 `diagnostic_log_enabled` 后，日志写入：

```text
data/plugin_data/astrbot_plugin_quest_avatar_bridge/quest_avatar_bridge.log
```

可选配置：

```text
diagnostic_log_enabled=true
diagnostic_log_max_bytes=1048576
diagnostic_log_backup_count=3
```

文件超过大小上限后轮转为 `.1`、`.2` 等备份文件。写入使用进程内锁；目录只读、磁盘满或轮转失败时，日志器进入降级状态并停止重试，不影响 HTTP、SSE、LLM、STT、TTS 或插件生命周期。

日志器不导入 Python `logging`，不注册 root handler，也不调用 AstrBot `logger`。记录内容限于初始化、terminate、listener、pairing、health、session/turn/SSE 状态、能力状态、异常类型、错误 code 和耗时。

以下内容会被丢弃，不会写入日志：Bridge Key、JWT、任何 Provider/API key、URL/base URL、原始音频、回复正文，以及 session、turn、person、platform、user、bot、group、client 等身份标识。

## `series.diagnostics@1.0` 只读提供方

“临”向“核”公开以下固定方法，不接受写入参数，也不暴露日志文件内容以外的插件状态：

```text
diagnostic_log_contract() -> {
  "name": "series.diagnostics",
  "version": "1.0",
  "series_id": "ningxin_suxi",
  "plugin_id": "astrbot_plugin_quest_avatar_bridge",
  "plugin_name": "临",
  "capabilities": ("read_events", "clear_events"),
  "storage": "memory_only",
  "astrbot_log_propagation": false
}
diagnostic_events(after_seq=0, limit=200) -> bounded snapshot
diagnostic_clear() -> None
```

正常快照包含 `contract`、`plugin_id`、`plugin_name`、`stream_id`、`events`、`next_seq`、`dropped_before`，并标记 `status=ready/reason=READY`。事件只来自插件自己的安全字段；不含密钥、认证头、会话身份、原始音频或回复正文。`diagnostic_log_enabled=false` 返回 `status=disabled/reason=DIAGNOSTIC_DISABLED`；文件写入失败后返回 `status=unavailable/reason=DIAGNOSTIC_UNAVAILABLE`，并停止继续写入。

“核”以完整声明、插件元数据中的精确 `qsbb` 仓库及运行时插件 ID 三方一致作为动态发现边界，不需要静态登记“临”。“核”按 1 秒独立超时读取该提供方；缺失、关闭、畸形或超时只影响“临”这一行，其他系列日志继续显示。清空会更换 `stream_id`，让页面丢弃旧游标。提供方只保留当前进程内的有界快照，原有 JSONL 落盘、轮转和失败关闭行为不变。
