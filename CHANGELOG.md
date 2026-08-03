# Changelog

## Unreleased

## 0.1.1 - 2026-08-03

### Added

- 新增凝心溯溪系列适配器：知、序、情、境、声的只读能力按公开契约接入，缺失时安全降级，不猜测私有接口。
- 新增配对 Page，使用一次性二维码和 6 位短码完成 Quest 绑定，不在页面、二维码或本地表单存储长期密钥。
- 新增通过真实本机 HTTP 路由和生产 SSE generator 执行的 contract/smoke 测试，外部 LLM/STT/TTS 使用 fake adapter。
- 新增 `fixtures/protocol_v1/` 稳定 JSON/SSE 样本，固定协议版本、路由、枚举、错误响应和事件顺序。
- 新增基于 AstrBot 4.26.8 默认 STT/TTS Provider 的生产 adapter；默认关闭，显式启用后支持 16000 Hz PCM16 输入和 24000 Hz PCM16 输出。
- 新增 TTS PCM WAV 校验、立体声下混、采样率转换、超时和输出时长限制。

### Tests

- 覆盖 health、session、interaction、interrupt、close 完整链路，以及输入音频错误矩阵、STT/TTS 故障、SSE 断线重连、重复 session、背压和迟到旧轮隔离。

### Documentation

- 新增本机 AstrBot/Unity 联调步骤、安全密钥、局域网地址、Windows 防火墙和 HTTPS 边界说明。
- 记录 AstrBot Provider 输出限制、voice_hub 契约边界和生产语音启用步骤。

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
