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
- `action_result.request.json` / `action_result.response.json` 固定客户端动作执行回执；`action_id` 示例仅用于 schema 联调，生产值必须取自服务端 `avatar.intent`，不得由客户端自建。
- `session_start.request.json` 声明客户端动作能力；服务端响应返回注册表交集。省略该字段表示旧客户端，继续支持原动作但不会收到 `crouch`。
- `avatar.intent` 的 `method/parameters/transition/source` 是有界动作方法扩展；`method` 必须等于兼容字段 `gesture`。
- 可执行 `avatar.intent` 样本中的 `action_id` 是规范化占位值。真实 contract tests 会先校验服务端生成值的格式，再规范化比较，避免把随机 token 固化到 fixture。
- `manifest.json` 同时固定受保护上下文的失败关闭来源规则和系列插件消费边界；Unity 不得提交或覆盖服务端可信 `api_principal/client_id/platform_id`。

fixtures 使用固定测试 ID，不得直接用于生产用户标识。修改协议、枚举、事件顺序或错误码时，必须同步生产实现、本文档和 contract tests。
