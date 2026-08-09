# AstrBot 与 Unity 本机联调指南

本文档用于在开发网络中联调 AstrBot Quest Avatar Bridge 与 Unity 前端。公开协议见 [API_CN.md](API_CN.md)，可执行样本见 [`fixtures/protocol_v1/`](../fixtures/protocol_v1/)。

## 1. 联调边界

- 生产插件只使用 AstrBot 公开 HTTP Web API 和 SSE，不注册 WebSocket。
- `tests/http_harness.py` 中的 fake LLM/STT/TTS 只由 pytest 导入，不存在生产配置开关，也不会随 AstrBot 启动。
- 本地 contract test 使用 `127.0.0.1` 临时随机端口，不接受局域网或公网连接。
- Unity 仍只负责上报事实、播放音频、模型能力检查和执行语义意图。

## 2. 前置条件

- AstrBot 版本满足 `metadata.yaml` 中的 `astrbot_version`。
- 插件安装在 AstrBot 的 `data/plugins/astrbot_plugin_quest_avatar_bridge/`。
- 已配置可用的聊天模型 Provider，并取得它的 Provider ID。
- Unity Editor 或 Quest 设备能够访问 AstrBot Dashboard 所在主机。
- 如果要联调真实语音：已在 AstrBot Provider 设置中启用并选定默认 STT/TTS Provider。

安装生产依赖：

```powershell
python -m pip install -r requirements.txt
```

仅运行 contract tests 时安装开发依赖：

```powershell
python -m pip install -r requirements-dev.txt
```

## 3. 配置安全密钥

生成 32 字节随机桥接密钥：

```powershell
$bridgeBytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Fill($bridgeBytes)
[Convert]::ToBase64String($bridgeBytes)
```

在 AstrBot 插件配置页设置：

| 配置 | 联调要求 |
|---|---|
| `bridge_api_key` | 使用上一步生成的随机值，至少 32 字符 |
| `bridge_service_enabled` | 总服务开关，默认 `true`；也可从「Quest 角色设置」Page 即时启停 |
| `pairing_listener_enabled` | 默认 `false`；容器私网统一入口时显式设为 `true` |
| `pairing_listener_host` / `pairing_listener_port` | 只允许 IP 字面量与 1024–65535 端口；容器通常用 `0.0.0.0` / `8520` |
| `pairing_listener_upstream_url` | 只允许无路径、无认证信息的 loopback HTTP IP，例如 `http://127.0.0.1:6185` |
| `pairing_listener_public_url` | Quest 可达的主机 base URL 或精确 exchange URL；不猜宿主机 IP |
| `pairing_exchange_proxy_url` | 可选旧外部代理 fallback；内置 listener 未就绪或 public URL 不合法时才使用 |
| `pairing_trusted_proxy_ip` | 仅供旧外部代理路径；内置 listener 始终使用直接 peer IP，不信任转发来源头 |
| `allow_private_http_pairing` | 只在受控私网启用；由服务端固定，快速绑定页不显示，公网继续强制 HTTPS |
| `pairing_public_url` / `pairing_astrbot_api_key` | 服务端快速绑定使用的 Quest 地址与专用 plugin-scope Key；不进入 Page、二维码或日志 |
| Bot/User 规范身份 | 在「Quest 角色设置」页明确填写或从“情”解析；只存插件数据目录的 `server_identity.json`，AstrBot 配置 Page 不回显；配对交换只下发占位值 |
| `pairing_ttl_seconds` | 服务端固定的一次性凭证 TTL，默认 120 秒；Page 只显示剩余时间 |
| `chat_provider_id` | 明确选择聊天模型 Provider |
| `persona_source_mode` | 默认 `astrbot`，继承 AstrBot 正式人格；仅兼容旧手动设定时选 `manual_override` |
| `astrbot_persona_id` | 可选的服务端 Quest 人格；留空继承 AstrBot 明确默认人格，不允许 Unity 提交 |
| `relationship_person_id` | 可选；由管理员在角色设置页选择，后端解析唯一活跃私聊账号并同步 Quest 只读绑定，不新增 owner |
| `persona_prompt` / 四个 `character_*` 字段 | 仅 `manual_override` 兼容模式生效；未知经历必须明确不知道，且不由 `relationship_person_id` 推断 |
| `max_sessions` | 按开发设备数量设置，保持较小值 |
| `max_audio_seconds` | 联调时建议保持默认或更小 |
| `enable_astrbot_stt` | 真实 STT 联调时设为 `true`，否则 `audio/end` 返回 SSE `stt_unavailable` |
| `enable_plugin_mimo_stt` / `plugin_mimo_stt_*` | 可选插件独立 MiMo ASR；只使用本插件专属配置，不修改 AstrBot 全局 STT |
| `enable_astrbot_tts` | 真实 TTS 联调时设为 `true`，否则只发送文字和意图 |
| `enable_voice_hub_tts` | 默认 `true`；安装“声”后优先消费 `voice.audio_output@1.0` |
| `trusted_client_id` / `trusted_platform_id` | 由 AstrBot 管理员在服务端配置；留空会关闭受保护关系上下文 |
| `enable_global_knowledge` | 只读取 `global`，不得改成 private user scope |
| `enable_environment_context` | 只读取 provider 缓存，不触发实时网络请求 |
| `enable_runtime_diagnostics` | 启动与显式 health 只读诊断，不执行更新操作 |
| `stt_timeout_seconds` / `tts_timeout_seconds` | 按 Provider 延迟设置，保持有界，不要设为无限 |
| `max_tts_audio_seconds` | 限制单轮 Provider WAV 读入和输出时长 |

「Quest 快速绑定」Page 只生成一次性二维码和短码，不承担模型、人格、连接或身份设置。聊天模型、AstrBot 人格、自然人范围和服务启停均由管理员在「Quest 角色设置」Page 或本插件专属配置中管理；Bridge 仍只接受公开 API/契约并在缺失、超时或畸形响应时安全降级，不访问 Core 或其他插件私有配置。

还需要在 AstrBot 中创建一个具有 `plugin` scope 的 API Key。Unity 的每个请求必须同时携带：

```http
Authorization: Bearer <ASTRBOT_PLUGIN_SCOPE_API_KEY>
X-Quest-Avatar-Key: <bridge_api_key>
```

两把密钥用途不同，不能只配置其中一个。

内置 listener 不需要、不保存也不注入专用 plugin-scope 服务 Key。它只在精确 exchange 路径直接调用共享 `PairingExchangeService`；正常 Bridge 路径仍把 Quest 自己的两层认证原样转发给 AstrBot。推荐私网最小值：

```text
pairing_listener_enabled=true
pairing_listener_host=0.0.0.0
pairing_listener_port=8520
pairing_listener_upstream_url=http://127.0.0.1:6185
pairing_listener_public_url=http://192.168.50.10:8520
allow_private_http_pairing=true
```

旧 [nginx_8520_pairing.example.conf](nginx_8520_pairing.example.conf) 继续作为可选兼容方案。只做 Docker 端口映射、却不启用内置 listener，也不部署外部代理，仍不会产生首次配对入口。

### 3.1 真实语音 Provider 检查

插件不接受 STT/TTS Provider ID 字符串并自行寻找对象，而是读取 AstrBot 当前选中的默认 Provider。这与 4.26.8 的公开 `Context.get_using_stt_provider()` / `get_using_tts_provider()` 契约一致。启用前用 AstrBot 的 Provider 页面或 `/provider` 确认两类 Provider 都可用。

安装“声”时，Bridge 优先消费无消息副作用的 `voice.audio_output@1.0`，只读 provider 管理的 PCM16 WAV，且不删除/移动源文件。未安装或失败时，只有 `enable_astrbot_tts=true` 才会回退 AstrBot Core TTS。Core Provider 必须返回本地、未压缩 PCM16 WAV；MP3、浮点/压缩/截断或超限文件产生 `tts_failed`，文字和 `reply.end(audio_sent=false)` 保留。不得调用 `voice.delivery@1.0` 或内部 `synthesize_text()`。

STT 输入在 `audio/end` 后一次性写为 16000 Hz 单声道 PCM16 WAV，再调用 Provider。当前没有 `asr.partial`，也不会把原始 PCM 写入插件安装目录；临时文件只在 `data/plugin_data/astrbot_plugin_quest_avatar_bridge/stt_input/` 中短暂存在。

## 4. 选择正确地址

### Unity Editor 与 AstrBot 在同一台电脑

```text
http://127.0.0.1:6185/api/v1/plugins/extensions/astrbot_plugin_quest_avatar_bridge
```

### Quest 真机访问电脑上的 AstrBot

Quest 中的 `127.0.0.1` 指向头显自身，不能用于访问电脑。应使用电脑的局域网 IPv4，例如：

```text
http://192.168.1.10:8520/api/v1/plugins/extensions/astrbot_plugin_quest_avatar_bridge
```

要求电脑和 Quest 位于互通的可信开发网络。Windows 防火墙只应允许专用网络和必要端口，不要创建面向公用网络或任意来源的宽泛规则。

如果 Android/Quest 构建阻止明文 HTTP，优先配置本地可信 HTTPS 反向代理。确需调试明文流量时，只在 Unity 调试构建中使用受限的网络安全配置，不得把放宽策略带入发布构建。

## 5. HTTP 与 SSE 冒烟联调

以下命令假设：
### 4.1 Docker 与 8520 检查

Docker 配置必须发布端口，例如：

```yaml
ports:
  - "8520:8520"
```

这只把 host 8520 转发到 container 8520；它不会在容器内启动进程。插件 `initialize()` 成功后，`GET /health` 和 Dashboard 的 `pairing/overview` 会给出脱敏状态：

```json
{
  "enabled": true,
  "ready": true,
  "bind_host": "0.0.0.0",
  "port": 8520,
  "upstream_kind": "loopback_http",
  "reason": "ready"
}
```

`enabled=true, ready=false` 时先检查 `reason`：`invalid_*` 表示配置校验失败，`bind_failed` 表示端口占用或权限问题，`start_failed` 表示 listener 初始化异常。插件其余 6185/25520 官方路由仍应可用。`ready=true` 但 reason 为 `pairing_listener_public_url_missing` 或 URL 校验错误时，socket 已监听，但内置入口不会写入新配对二维码；若旧代理也未配置，则 `bootstrap_ready=false`。

8520 正常接口上游断线返回 no-store 的稳定 503 JSON。SSE 已开始后如果上游断线，只关闭当前流，不伪造 `reply.end` 或旧轮事件；Unity 应按现有重连/新会话规则处理。


```powershell
$bridgeBase = "http://127.0.0.1:6185/api/v1/plugins/extensions/astrbot_plugin_quest_avatar_bridge"
$astrbotKey = "<ASTRBOT_PLUGIN_SCOPE_API_KEY>"
$bridgeKey = "<bridge_api_key>"
```

### 5.1 健康检查

```powershell
curl.exe --fail-with-body "$bridgeBase/health" -H "Authorization: Bearer $astrbotKey" -H "X-Quest-Avatar-Key: $bridgeKey"
```

先检查 `protocol_version=1.0`，再根据 `stt_available` 和 `tts_available` 决定是否启用音频输入/输出。

### 5.2 创建会话

在插件根目录执行：

```powershell
curl.exe --fail-with-body "$bridgeBase/session/start" -H "Authorization: Bearer $astrbotKey" -H "X-Quest-Avatar-Key: $bridgeKey" -H "Content-Type: application/json" --data-binary "@fixtures/protocol_v1/session_start.request.json"
```

### 5.3 建立 SSE

另开一个终端并保持连接：

```powershell
curl.exe -N "$bridgeBase/events/smoke-session" -H "Authorization: Bearer $astrbotKey" -H "X-Quest-Avatar-Key: $bridgeKey" -H "Accept: text/event-stream"
```

首先应看到 `: connected`。Unity 必须按空行拆分 frame，不能把一次网络读取当成一个完整事件。

### 5.4 上报交互

```powershell
curl.exe --fail-with-body "$bridgeBase/interaction" -H "Authorization: Bearer $astrbotKey" -H "X-Quest-Avatar-Key: $bridgeKey" -H "Content-Type: application/json" --data-binary "@fixtures/protocol_v1/interaction.request.json"
```

SSE 至少会返回 `avatar.intent`；是否继续返回文字和音频由角色决策及 adapter 状态决定。

### 5.5 打断与关闭

```powershell
curl.exe --fail-with-body "$bridgeBase/interrupt" -H "Authorization: Bearer $astrbotKey" -H "X-Quest-Avatar-Key: $bridgeKey" -H "Content-Type: application/json" --data-binary "@fixtures/protocol_v1/interrupt.request.json"
```

```powershell
curl.exe --fail-with-body "$bridgeBase/session/close" -H "Authorization: Bearer $astrbotKey" -H "X-Quest-Avatar-Key: $bridgeKey" -H "Content-Type: application/json" --data-binary "@fixtures/protocol_v1/session_close.request.json"
```

关闭会话后 SSE 应结束。再次使用相同 `session_id` 前必须确认旧会话已关闭。

## 6. Unity 客户端实现要求

- 启动时先调用 health；协议版本不等于 `1.0` 时停止接入并提示升级。
- 创建会话成功后立即建立 SSE，再发送 turn 或 interaction。
- 使用支持自定义 Header 的长期 HTTP 请求读取 SSE，不依赖浏览器 `EventSource`。
- SSE 数据可能跨多次网络读取，也可能一次读取包含多个 frame；必须按 UTF-8 行和空行解析。
- 以 `(session_id, turn_id)` 路由所有事件；旧 `turn_id` 的迟到数据直接丢弃。
- 收到用户说话或本地打断事实时，先停止旧音频/动作，再 POST interrupt。
- 断线后停止旧轮本地播放，使用同一会话重连 SSE；若返回 404，再创建新会话。
- 同一会话同时只允许一个 SSE。重连前必须关闭并释放旧请求对象。
- `reply.audio.chunk` 需要 Base64 解码为 PCM16、单声道、24000 Hz，并按真实播放进度驱动嘴型。
- `reply.audio.chunk` 是有序且受背压保护的事件；客户端应持续消费 SSE，不应并行创建第二条流来“抢速度”。
- `audio/chunk` 的 `sequence` 从 0 严格递增；HTTP 成功响应只代表进入有界缓冲区，收到 `audio/end` 的 `202` 后再等待 `asr.final` 或 SSE `error`。
- 当前 `ConversationController` 的语音轮会在 `turn/start` 发送 `text: null`（某些 Unity `JsonUtility` 版本可能省略该字段或发 `""`），Bridge 三种形状都按 `awaiting_audio` 处理；文本输入可发送最多 8192 个字符。
- Unity 默认每 80 ms 采集一块 PCM16，即 2560 字节；`sequence` 必须从 0 连续递增。收到 `interrupt` 的成功响应后，旧轮任何迟到事件都必须按 `(session_id, turn_id)` 丢弃。
- Unity 必须对白名单做二次校验，并按当前模型能力把不支持动作降级为 `idle`。
- 不得把 `head_pat` 固定映射为开心，也不得把 `cheek_pinch` 固定映射为害羞。

## 7. 复用 fixtures

Unity 测试可以直接读取：

```text
fixtures/protocol_v1/manifest.json
fixtures/protocol_v1/*.request.json
fixtures/protocol_v1/*.response.json
fixtures/protocol_v1/*.error.json
fixtures/protocol_v1/*.event.json
fixtures/protocol_v1/audio_turn.events.sse
fixtures/protocol_v1/tts_failure.events.sse
fixtures/protocol_v1/audio_flow_cases.json
```

`manifest.json` 是枚举、路由、错误码和事件顺序的测试事实源。JSON 对象字段顺序没有语义；SSE fixture 的 frame 顺序和每个 `data:` JSON 内容有语义。

运行后端真实 HTTP/SSE contract tests：

```powershell
python -m pytest -q tests/test_http_contract_smoke.py tests/test_protocol_fixtures.py
```

这些测试会打开本机随机端口，通过真实 HTTP 请求调用生产 handler，并逐帧读取生产 SSE generator。fake 只替代外部 LLM/STT/TTS，不替代认证、schema、会话管理、打断或编码逻辑。

## 8. 安全检查表

- 不把任一密钥提交到 Git、fixtures、日志或 Unity 源码。
- 不在 `PlayerPrefs` 中明文长期保存生产密钥；使用受控配置下发或平台安全存储。
- 开发、测试和生产使用不同密钥；设备丢失或人员变更时立即轮换。
- 非本机流量使用 HTTPS；不要依赖同一 Wi-Fi 就自动安全。
- 不启用宽泛 CORS。Unity 原生 HTTP 客户端不需要浏览器跨域放行。
- 保持请求体、音频时长、会话数和事件队列上限，不为联调取消安全限制。
- 日志只记录状态和错误类型，不记录正文、音频、用户标识或认证头。
- 不要为了绕过 Provider 格式错误而关闭 WAV 校验、大小上限或队列背压。

## 9. 常见问题

| 现象 | 检查项 |
|---|---|
| 401 `astrbot_auth_required` | `Authorization` 是否为有效、具有 plugin scope 的 Bearer Key |
| 401 `bridge_auth_failed` | `X-Quest-Avatar-Key` 是否与插件配置完全一致 |
| 403 `session_ownership_mismatch` | 后续请求是否换了 AstrBot API Key |
| 404 `session_not_found` | 会话是否被关闭、插件是否重载、服务是否重启 |
| 409 `session_conflict` | session 是否重复、是否已有活动轮或 SSE |
| 503 `bridge_service_disabled` | 在「Quest 角色设置」Page 重新启动服务；关闭服务会主动清理旧会话 |
| 422 `schema_validation_failed` | 对照 manifest、请求 fixture 和 Pydantic 字段范围 |
| SSE 无事件 | 是否先创建会话、SSE 是否仍连接、interaction 是否被去抖 |
| `stt_unavailable` | `enable_astrbot_stt` 是否打开，以及 AstrBot 是否选中了可用 STT Provider |
| `stt_failed` | Provider 是否能读取插件生成的 PCM16 16000 Hz WAV；查看错误类型而不是重试旧轮数据 |
| `tts_failed` | Provider 是否返回未压缩 PCM16 WAV；文字仍应可见，`reply.end.audio_sent` 应为 `false` |
| Quest 无法访问电脑 | 不要使用 127.0.0.1；检查局域网 IP、专用网络防火墙和 AP 隔离 |
