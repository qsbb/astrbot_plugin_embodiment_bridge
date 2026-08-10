# Quest Avatar Bridge 实时异步审计

审计范围仅为“临”的 HTTP/SSE、session/turn、LLM/STT/TTS、interaction 和独立诊断日志。Protocol 1.0、端点与 Unity 字段未改变。

## 本来已异步

- 生产代码没有 `requests`、同步 urllib 网络调用、`time.sleep`、`Future.result()` 或 `run_until_complete()`。
- HTTP 和内置 listener 使用 aiohttp；SSE 使用异步生成器、心跳和每会话有界事件队列。
- STT 临时 WAV 写入/删除、TTS WAV 解析与重采样、同步只读环境/身份 provider 调用均通过 `asyncio.to_thread()` 卸载。
- 关系、知识与缓存环境读取并发执行；LLM、STT、TTS turn 任务可取消，发送前复核 session、turn 和 generation。
- 普通 Quest 对话仍通过 AstrBot EventBus 或公开的整轮 `context.llm_generate()` 回退链生成，`reply.text.delta` 在 TTS 之前下发。人格转换是独立管理任务：它精确复用管理员所选的已实例化 Chat Provider，并直接消费公开 `text_chat_stream()`，从而报告等待首块、持续生成和完整返回阶段；两条链路不会互相替代。
- 慢 SSE 客户端对关键事件施加背压；`asr.partial` 可合并，文字 delta 可丢弃，意图、音频、结束和错误不会为了非关键事件被丢弃。

## 已证实并修复的串行问题

1. 旧 interaction 通过 `begin_turn(cancel_previous=True)` 占用主 turn，任何触碰都会取消正常 LLM/TTS。现在 interaction 使用独立 `i:<event_id>` 槽位，单会话最多并发 2 个；普通触碰不打断主对话，只有显式 `/interrupt` 才取消指定 turn。
2. 旧 `DiagnosticLog.record()` 在事件循环线程内直接执行目录创建、stat、open/write 和轮转。现在 `record()` 只做脱敏和有界内存入队，单一 writer 通过 `asyncio.to_thread()` 落盘；关闭时有 2 秒 flush 上限，写失败和超时不影响对话。
3. 旧 TTS 对整段回复执行一次文件式合成。现在回复先完整发送文字 delta，再按安全句段顺序合成；单 producer 与容量 2 队列限制预取，不无限并发，取消会终止 producer，所有音频发送仍复核 generation。

## 保留限制

- STT 仍在 `audio/end` 后进行整轮识别，不产生 `asr.partial`；VAD、回声消除和唤醒词属于 Unity/设备侧。
- TTS provider 本身仍可能在生成单个句段 WAV 时等待完整文件；Bridge 不能把未公开的文件式 provider 伪装成 token/audio streaming API。
- 单会话只允许一个 SSE 消费者；断线后仅保留尚未消费的队列事件，不重放已经消费的事件。
