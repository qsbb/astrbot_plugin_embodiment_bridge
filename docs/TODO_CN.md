# AstrBot Embodiment Bridge 后续待办

当前 HTTP POST + SSE 对话链路已经可以由 Unity `ConversationController` 使用：文本轮、PCM16 语音轮、SSE 音频、打断和会话关闭均走公开协议。后端决策仍可能产生 `avatar.intent`，但文字/语音交付不依赖 Unity 是否成功执行动作。

下一阶段只在 Bridge 与 Unity/Quest 联调中完成以下事项：

- 使用真实 Quest 麦克风验证 16000 Hz、单声道、PCM16 的 40–100 ms 分块、背压和断线重连。
- 使用真实扬声器/音频播放器验证 24000 Hz、单声道、PCM16 SSE 块按播放进度驱动嘴型。
- 对每个目标 PMX/VRM 模型执行 `avatar.intent` 白名单能力检查，确认不支持的语义动作降级为 `idle`，而不是由后端返回模型细节。
- 验证 `head_pat`、`cheek_pinch` 等事实不会在 Unity 端被固定映射为开心/害羞；接受、拒绝、害羞、回避和不回应均由后端决策。
- 验证 Quest 真机在新轮/本地说话/网络断线时先停止旧音频和动作，并按 `(session_id, turn_id)` 丢弃迟到旧事件。

这些是设备验收事项，不改变当前公开协议，也不引入生产环境 fake LLM/STT/TTS 开关。
