# Changelog

## Unreleased

## 0.1.0 - 2026-08-01

### Added

- 新增 AstrBot `v4.26.8` 公开 Web API 上的 HTTP POST + SSE Transport。
- 新增 session、turn、PCM16 audio、interaction、interrupt、close 和 health 接口。
- 新增严格 Pydantic 协议、意图白名单、大小限制、双层认证和会话归属检查。
- 新增独立有界事件队列、generation token、可取消轮次和慢客户端关键事件保护。
- 新增基于 `context.llm_generate()` 的回复与结构化意图联合决策。
- 新增确定性 `InteractionPolicy`，覆盖强度、持续时间、动作冷却和明确安全边界。
- 新增可选 `relationship.snapshot@1.0` 只读适配。
- 新增可替换 STT/TTS 边界及 fake 驱动的协议测试。
- 新增面向 Unity 前端的完整中文接口文档。

### Security

- 所有接口要求 AstrBot `plugin` scope API Key 与独立 `X-Quest-Avatar-Key`。
- 未知动作、表情、注视值和模型相关额外字段不会透传 Unity。
- 日志不记录用户正文、音频、认证密钥或关系标识。

### Limitations

- 默认 STT/TTS adapter 关闭；文本决策链可用，真实 PCM 输入识别和输出合成待稳定契约。
- 尚未进行 Quest 3 真机、Unity 音频/嘴型、动作映射和网络延迟验证。
