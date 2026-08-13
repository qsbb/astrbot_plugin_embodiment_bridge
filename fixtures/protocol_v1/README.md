# Protocol 1.0 fixtures

本目录是 Unity 与 AstrBot 联调共用的稳定协议样本。

- `*.request.json` 可直接作为对应 POST 接口的请求体。
- `*.response.json` 和 `*.error.json` 是稳定 HTTP 响应样本。
- `*.event.json` 是单个 SSE `data:` JSON 样本。
- `audio_turn.events.sse` 是成功语音轮的完整 SSE 字节序列。
- `tts_failure.events.sse` 固定文字成功但 TTS 失败时的事件顺序。
- `audio_flow_cases.json` 固定输入/输出 PCM 约束及请求级错误矩阵。
- `audio_chunk.*.request.json` 是 Unity 可直接复用的负向请求样本。
- `unity_audio_turn_start.request.json` 固定 Unity `JsonUtility` 语音轮的 `text: null` 形状；后端也兼容省略字段和精确空字符串。
- `manifest.json` 固定路由、枚举、关键事件、错误响应和成功事件顺序。
- `spatial_context.payload.json` / `spatial_context.response.json` 固定脱敏房间语义快照的严格字段与 revision 响应；该扩展使用独立整数 `schema_version=1`，不接受通用 `protocol_version` 字段。
- `manifest.json` 同时固定受保护上下文的失败关闭来源规则和系列插件消费边界；Unity 不得提交或覆盖服务端可信 `api_principal/client_id/platform_id`。

fixtures 使用固定测试 ID，不得直接用于生产用户标识。修改协议、枚举、事件顺序或错误码时，必须同步生产实现、本文档和 contract tests。
