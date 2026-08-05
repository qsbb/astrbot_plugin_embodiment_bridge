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
