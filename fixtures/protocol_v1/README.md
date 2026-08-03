# Protocol 1.0 fixtures

本目录是 Unity 与 AstrBot 联调共用的稳定协议样本。

- `*.request.json` 可直接作为对应 POST 接口的请求体。
- `*.response.json` 和 `*.error.json` 是稳定 HTTP 响应样本。
- `*.event.json` 是单个 SSE `data:` JSON 样本。
- `audio_turn.events.sse` 是成功语音轮的完整 SSE 字节序列。
- `tts_failure.events.sse` 固定文字成功但 TTS 失败时的事件顺序。
- `audio_flow_cases.json` 固定输入/输出 PCM 约束及请求级错误矩阵。
- `audio_chunk.*.request.json` 是 Unity 可直接复用的负向请求样本。
- `manifest.json` 固定路由、枚举、关键事件、错误响应和成功事件顺序。

fixtures 使用固定测试 ID，不得直接用于生产用户标识。修改协议、枚举、事件顺序或错误码时，必须同步生产实现、本文档和 contract tests。
