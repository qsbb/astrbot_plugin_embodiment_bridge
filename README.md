# astrbot_plugin_quest_avatar_bridge

凝心溯溪系列 Quest 角色桥接模块。它把 Meta Quest 3 上 Unity MMD/VRM 前端上报的对话与交互事实交给 AstrBot 决策，再通过 SSE 返回模型无关的文字、音频和角色意图。

插件基于 AstrBot 当前公开的 `Context.register_web_api()`、事件队列、消息事件、`Context.llm_generate()`、`Context.persona_manager`、`Context.get_using_stt_provider()` 和 `Context.get_using_tts_provider()`。普通文字/语音默认只进入已授权的正式 EventBus；直连 Provider 回退必须由管理员显式开启，触碰动作仍使用受控兼容决策。人格接入按 AstrBot 4.27.1 的公开接口实现；不注册 WebSocket，也不修改 AstrBot Core、service hub 或 orchestration hub。

## 项目信息

- 作者：`qsbb`
- 中文名：凝心溯溪-临
- 源码仓库：<https://github.com/qsbb/astrbot_plugin_quest_avatar_bridge>
- Unity 前端接口：[docs/API_CN.md](docs/API_CN.md)
- 本机联调与安全配置：[docs/LOCAL_INTEGRATION_CN.md](docs/LOCAL_INTEGRATION_CN.md)
- AstrBot 4.26.8 本地加载失败审计：[docs/LOAD_FAILURE_AUDIT_CN.md](docs/LOAD_FAILURE_AUDIT_CN.md)
- Unity 可复用协议样本：[fixtures/protocol_v1/](fixtures/protocol_v1/)
- 实时异步审计：[docs/ASYNC_AUDIT_CN.md](docs/ASYNC_AUDIT_CN.md)
- AstrBot 人格继承审计：[docs/PERSONA_INTEGRATION_CN.md](docs/PERSONA_INTEGRATION_CN.md)
- 后续动作/设备联调待办：[docs/TODO_CN.md](docs/TODO_CN.md)

## 职责边界

Unity 负责感知、播放和执行：

- 上报握手、摸头、捏脸、注视、说话和打断等事实。
- 播放 PCM16 音频，并按真实音频在本地驱动嘴型。
- 把语义动作、表情和注视意图映射到当前 PMX/VRM 模型。
- 检查当前模型是否支持对应表现，并在不支持时安全降级。

插件负责角色决策：

- 结合角色设定、当前会话和公开关系快照决定说什么、是否回应。
- 决定接受、拒绝、害羞、不舒服、回避等反应。
- 对 LLM 输出做严格白名单校验，再限制强度、持续时间、冷却和边界。
- 只返回 `emotion`、`gesture`、`look_at` 等语义值，不返回骨骼、Morph、Unity 对象或动画路径。

触碰名称不是情绪映射。`head_pat` 不保证得到 `happy`，`cheek_pinch` 也不保证得到 `shy`。

## 运行要求

- AstrBot `>=4.26,<5`。
- 一个已启用的聊天模型 Provider，并把其 ID 写入 `chat_provider_id`。
- 一个至少 32 字符的随机 `bridge_api_key`。
- 一个具有 `plugin` scope 的 AstrBot API Key，供 Unity 调用 `/api/v1/plugins/extensions/...`。
- AstrBot 主机与 Quest 在可达网络中。正式网络建议使用 HTTPS 反向代理，不要把 Dashboard 直接暴露到公网。
- 可选：AstrBot 已实例化可用的 STT/TTS Provider；只有需要生产语音输入或输出时才配置。STT 在「Quest 角色设置」Page 中显式选择，Core TTS 仍使用 AstrBot 当前默认 Provider。

把插件目录安装到 AstrBot 的 `data/plugins/astrbot_plugin_quest_avatar_bridge/` 后安装 `requirements.txt`，在 AstrBot 插件配置页设置以上配置并重载插件。运行数据目录由 `StarTools.get_data_dir()` 解析到 `data/plugin_data/astrbot_plugin_quest_avatar_bridge/`；插件不会向安装目录写运行数据。

## 配对页

插件自带「Quest 快速绑定」配对页：Page 只负责生成一次性二维码和 6 位短码，不显示或采集 Quest IP、AstrBot API Key、平台/客户端身份、会话 ID 或有效期。这些值由 Bridge 插件专属配置在服务端注入，Quest 端无需手动搬运长期密钥。

AstrBot 的插件 extensions 路由仍全部先要求 `plugin` scope，`register_web_api` 没有匿名例外。插件现在可以在 `initialize()` 中启动一个独立、最小化的 aiohttp listener：匿名能力只限精确 `POST /api/v1/plugins/extensions/astrbot_plugin_quest_avatar_bridge/pairing/exchange`；正常 Quest 路径按 method+path 白名单流式转发至容器内 loopback AstrBot，并原样保留 Quest 自己的双层认证。它不是 Dashboard 或任意 URL 的反向代理。

内置 listener 默认关闭。私网容器部署可显式设置：

```text
pairing_listener_enabled=true
pairing_listener_host=0.0.0.0
pairing_listener_port=8520
pairing_listener_upstream_url=http://127.0.0.1:6185
pairing_listener_public_url=http://192.168.50.10:8520
allow_private_http_pairing=true
pairing_public_url=http://192.168.50.10:8520
pairing_astrbot_api_key=<具有 plugin scope 的 Quest 专用 Key>
```

Quest 客户端使用 AstrBot 4.27 的官方 `Authorization: ApiKey <key>` 认证方案。请在“Quest 角色设置”页面填写专用 Key 并点击“保存并验证身份”；后端会通过严格 loopback HTTP 调用只读证明端点，让 AstrBot 先解析出 `api_key:<key_id>`，随后只把不可逆摘要写入身份白名单。Bot/User 始终由服务端保存，快速绑定只向 Quest 下发固定占位值，`session/start` 再以服务端规范身份覆盖设备声明。

Docker 的 `8520:8520` 端口映射本身不会创建监听器；只有插件初始化成功且配置合法时才会真正绑定。旧 `pairing_exchange_proxy_url` 与 [Nginx 示例](docs/nginx_8520_pairing.example.conf) 继续作为可选兼容方案，优先级低于已就绪且 public URL 合法的内置 listener。详见 [首次配对审计](docs/PAIRING_BOOTSTRAP_AUDIT_CN.md)。

复制短码或撤销配对期间，对应按钮会暂时禁用并显示“正在处理”，成功或失败后都会恢复，避免连续点击产生重复请求。如果服务端返回了当前页面尚不认识的配对状态，页面会显示中文提示并停止复制、撤销和轮询，避免把未知状态误当成仍可使用。

前置条件：已设置至少 32 字符的 `bridge_api_key`、Quest 专用 `pairing_astrbot_api_key` 和 Quest 可达的 `pairing_public_url`，并已选择聊天模型 Provider；Bot/User 在「Quest 角色设置」页明确填写，或通过“情”的自然人绑定自动解析。原始 Bot/User 只写插件数据目录中的服务端身份文件，不进入 AstrBot 配置 Page。公网必须使用 Quest 信任的 HTTPS；受控私网可显式使用私网 IP 字面量 HTTP。详细步骤与 Quest 端操作见 [docs/PAIRING_CN.md](docs/PAIRING_CN.md)。

## 角色设置页

插件另提供「Quest 角色设置」管理员 Page，与快速绑定页分离：

- 页面顶部集中显示 Quest Bridge 运行状态、内置 listener 地址、活跃会话、SSE、待发事件以及对话/EventBus/身份/STT/TTS/动作能力；管理员可即时启停服务，并在 1024-65535 范围修改 listener 端口（默认仍为 8520）。修改端口会持久化设置、同步已配置公开 URL 的端口、关闭旧会话并重启 listener；Docker 部署仍需映射相同宿主机端口。
- “聊天模型”只枚举 AstrBot 当前已实例化的 Chat Completion Provider，显示 id 和 model，保存时只提交 Provider ID；不会读取 Provider API Key、Base URL、请求头或原始配置。
- “正式消息链路”从 AstrBot 已加载平台目录选择 `trusted_platform_id`。目录仅投影实例 ID、适配器类型和显示名，保存时再通过公开 `Context.get_platform_inst()` 验证该实例当前存在；成功后立即同步身份授权和 EventBus 适配器。页面会显示 `ready`、未配置、平台不可用或 AstrBot API 不可用等脱敏状态。
- “具身人格工作区”提供实时继承、导入转换和临独立人格三种流程。导入时只提交 AstrBot 人格 ID，由后端精确读取来源；转换模型从 AstrBot 已配置的 Chat Completion Provider 中单独选择，不会替换普通对话模型。
- 转换结果先作为 30 分钟、一次性、仅内存草稿返回预览，确认保存后才在插件数据目录的 `personas/qp_<随机ID>.json` 中创建一个独立文件。人格列表只返回摘要；正文只有显式打开单个人格或保存结果时才返回 Dashboard 管理页。
- 保存与启用分离。启用后的临专用人格只覆盖带 `quest_avatar_bridge` 标记的 EventBus 轮次和 Bridge 受控回退决策；QQ 与其他 AstrBot 对话仍使用原人格。保存“实时人格来源”会在同一次配置提交中停用临专用人格。
- 临专用人格把角色定义为与用户身处同一世界、同一物理空间的人；但只有系统本轮明确提供的视觉、听觉、空间、距离、姿态、环境和触碰事实可作为真实感知，不能补造房间物体、用户表情或身体接触。
- “Quest 身份”一次保存 client/platform/bot/user 和专用 API Key；Bridge Key 缺失时由服务端生成。后端先通过 AstrBot 官方认证层的 loopback 证明取得 principal 摘要，再在检测到“序”时通过 `identity.control_plane@1.0` 原子同步主人和摘要白名单；未安装时使用“临”本地精确绑定，已安装但拒绝时绝不合并本地配置放行。
- 只有统一或本地身份授权成功且服务端 `trusted_platform_id` 对应平台实例仍在线时，Bridge 才按绑定的原始平台、Bot 与用户构造私聊消息来源并进入 EventBus；Quest 不能自选平台、人格或管理员身份。未授权或旧版 AstrBot 不支持该入口时，默认明确报错；只有显式开启 `allow_direct_provider_fallback` 才允许兼容 Provider 回退。
- 原有姓名、自称、自我描述和关系定位字段继续保留，但只有显式选择 `persona_source_mode=manual_override` 时才覆盖 AstrBot 人格。默认升级路径是 `astrbot`，不会要求重复维护角色设定。
- 点击“从‘情’读取”后，只消费 relationship.identity_candidates@1.0，展示 person_id、display_name 和 account_count；不调用“情”的 identities Page、私有 registry 或内部方法。
- 管理设置优先通过新 Core 的 `save_config_async()` 持久化；AstrBot 4.26.5 只有原子同步 `save_config()` 时，临会在后台线程调用该公开接口，并继续用插件级锁串行化管理写入，避免把页面错误降级为只读。
- 保存自然人时后端会重新读取正式候选目录，并通过仅服务端可见的 `relationship.quest_event_identity@1.0` 解析该自然人在当前活跃 AstrBot 平台上的唯一完整私聊账号；随后复用已验证 principal，通过“序”的 `identity.quest_binding_control@1.0` 保存 Quest 只读精确绑定，未安装“序”时使用本地严格绑定。该操作不修改 owner_users。原始账号和 UMO 不进入 Page 响应、Quest 或日志。
- `session/start` 始终用服务端规范身份覆盖设备的 Bot/User 占位声明，并在每个新会话重新向“情”复核；插件升级后也会自动修复已有自然人选择，无需把真实账号重新下发给设备。身份同步处于 pending 或复核不一致时，新会话失败关闭。
- 自然人选择提供身份映射事实，但本身不授予 owner、白名单、管理或平台操作权限；契约缺失、平台不在线、群聊/账号不完整、多账号歧义或授权控制面拒绝时失败关闭，不自动换人，也不读取“情”的私有 registry 兜底。
- `relationship_person_id` 绝不用于推断角色姓名、自称或身份；关系快照只影响语气、主动性和边界。
- “临”独立日志与设备端使用同一诊断结构：当前根因、链路、输入、耗时和阶段时间线（最新在下）；不渲染原始 JSON、正文、密钥或身份值。
- 人格转换使用可恢复的后台任务，并直接复用所选 AstrBot Chat Provider 的公开流式接口；Page 每秒读取来源、等待首块、持续生成、结构校验和预览阶段并实时刷新下方脱敏日志。转换不读取 Provider 私有配置，不自动换模型，也不展示输出正文或模型隐藏推理。
- EventBus 模式由 AstrBot 正式会话与插件钩子负责人格、历史、工具和时间/环境上下文；回退模式每个 turn 异步取得一次人格稳定快照。两条路径都不能覆盖 Protocol 1.0、认证授权和动作白名单。

模型也可以在插件配置页通过 chat_provider_id 的 Provider 下拉框设置；自然人候选的点击读取入口只在「Quest 角色设置」Page 中提供。两个 Page 都通过 AstrBot Page Bridge 和 Dashboard 身份调用本插件受保护端点；管理端点拒绝 API Key principal。专用 API Key 只允许密码框写入、提交后立即清空，后端永不回显，页面不使用本地存储。

临专用人格的平面配置只保存两个索引字段；完整人格正文不写入 AstrBot 插件配置：

| 配置 | 默认 | 作用 |
|---|---:|---|
| `persona_converter_provider_id` | 空 | 单独用于人格转换的 AstrBot Provider ID |
| `active_quest_persona_id` | 空 | 当前启用的服务端随机人格 ID；空值表示实时继承 AstrBot |

## 生产 STT/TTS 配置

语音能力默认关闭。先在 AstrBot 的 Provider 设置中实例化 STT/TTS Provider，再在本插件中配置：

| 配置 | 默认 | 作用 |
|---|---:|---|
| `astrbot_stt_provider_id` | 空 | 在「Quest 角色设置」Page 选择一个已实例化的正式 `STTProvider`；留空关闭语音识别 |
| `enable_astrbot_tts` | `false` | 调用 `get_using_tts_provider().get_audio()` |
| `enable_voice_hub_tts` | `true` | 优先消费“声”的 `voice.audio_output@1.0`；失败前未发送字节时可回退 Core TTS |
| `trusted_client_id` | 空 | 服务端固定 Quest 客户端 ID；空值会关闭受保护上下文 |
| `trusted_platform_id` | 空 | 服务端固定原始平台 ID；可在「Quest 角色设置」Page 验证并保存，空值会关闭受保护上下文与 EventBus 正式消息链路 |
| `allow_direct_provider_fallback` | `false` | 普通文字/语音是否允许在正式消息链路不可用时回退到直连 Provider；关闭可确保不会静默绕过记忆、知和后处理插件 |
| `stt_timeout_seconds` | `45` | 单次整轮识别超时 |
| `tts_timeout_seconds` | `60` | 单次整轮合成超时 |
| `max_tts_audio_seconds` | `120` | 规范化后输出时长上限 |
| `server_timing_enabled` | `false` | 是否在既有 `reply.end` 中附加服务端耗时摘要 |

启用 `server_timing_enabled` 后，正常或失败的既有 `reply.end` 事件可附加可选
`server_timing@1.0` 对象；不会新增 SSE 事件，也不会改变 Protocol 1.0 的事件顺序：

```json
{
  "contract": "server_timing@1.0",
  "stt_ms": 123,
  "decision_ms": 456,
  "decision_path": "astrbot_event_bus",
  "tts_first_chunk_ms": 789,
  "tts_total_ms": 1200,
  "turn_total_ms": 1800
}
```

所有耗时均为受限非负整数，单位为毫秒。`decision_path` 只允许
`astrbot_event_bus` 或 `direct_provider`；STT/TTS 未执行或不可用时对应耗时为 `0`。
`decision_ms` 从服务端决策阶段开始计时，`tts_first_chunk_ms` 从 TTS 开始到首个成功
进入服务端事件队列的音频块，`tts_total_ms` 到所有音频块入队完成，`turn_total_ms`
从服务端开始处理该轮到 `reply.end` 入队。它们都不包含客户端录音、网络传输或 SSE
客户端 flush 时间。

STT adapter 把 Unity 上传的原始 PCM16 封装为 16000 Hz、单声道 WAV，临时文件位于 AstrBot `data/plugin_data/astrbot_plugin_quest_avatar_bridge/stt_input/`，完成、失败、取消和插件终止时都会清理。

STT 下拉目录只读取 Provider 的安全元数据 `id`、`model`、`adapter_type`、`provider_type`，不会读取或返回 Provider 原始配置、API 地址、API Key 或 headers。显式选择的 Provider 被删除或停用时状态为 `selected_missing`，不会静默切换到其他 Provider。AstrBot 当前没有面向普通 Star 插件的稳定 STT tool/contract；第三方语音识别若要被选择，必须通过 AstrBot 正式 Provider 机制注册为 `STTProvider`。

旧版 Bridge 私有 MiMo URL、Key、model 配置不再作为推荐入口，也不会在配置 Page 或管理响应中显示。管理员保存新的 `astrbot_stt_provider_id` 时会清理这些旧私有字段且绝不回显旧密钥；升级后尚未选择正式 Provider 时安全关闭 STT。旧 `enable_astrbot_stt=true` 且未设置新 ID 的安装仅保留临时默认 Provider 兼容路径，建议尽快在 Page 中完成显式选择。

AstrBot 4.26.8 的 TTS Provider 契约只保证返回音频文件路径，不保证采样格式。本插件因此只接受本地、未压缩 PCM WAV，源文件必须是 PCM16、单声道或立体声、8000-192000 Hz；随后下混并重采样为 24000 Hz 单声道 PCM16。MP3、压缩 WAV、浮点 WAV、截断文件和超限音频都会产生 `tts_failed`，不会把未知字节发给 Unity。Provider 返回的文件归 AstrBot/Provider 管理，本插件只读且不删除。

配置后调用 `/health` 确认 `stt_available=true` 和 `tts_available=true`；STT 的脱敏状态会区分 ready、未选择、所选 Provider 缺失和旧配置待迁移。TTS 优先使用“声”明确声明的 `voice.audio_output@1.0`；该开关和契约语义没有改变。契约缺失、不兼容或合成失败且尚未输出字节时，才使用显式启用的 AstrBot Core TTS。Bridge 不调用 `voice.delivery@1.0` 或内部 `synthesize_text()`，也没有生产 mock 开关。

## 完整 URL

以下示例假设 AstrBot Dashboard 地址是 `http://192.168.1.10:6185`：

```text
BASE=http://192.168.1.10:6185/api/v1/plugins/extensions/astrbot_plugin_quest_avatar_bridge

POST http://192.168.1.10:6185/api/v1/plugins/extensions/astrbot_plugin_quest_avatar_bridge/session/start
GET  http://192.168.1.10:6185/api/v1/plugins/extensions/astrbot_plugin_quest_avatar_bridge/events/<session_id>
POST http://192.168.1.10:6185/api/v1/plugins/extensions/astrbot_plugin_quest_avatar_bridge/turn/start
POST http://192.168.1.10:6185/api/v1/plugins/extensions/astrbot_plugin_quest_avatar_bridge/audio/chunk
POST http://192.168.1.10:6185/api/v1/plugins/extensions/astrbot_plugin_quest_avatar_bridge/audio/end
POST http://192.168.1.10:6185/api/v1/plugins/extensions/astrbot_plugin_quest_avatar_bridge/interaction
POST http://192.168.1.10:6185/api/v1/plugins/extensions/astrbot_plugin_quest_avatar_bridge/interrupt
POST http://192.168.1.10:6185/api/v1/plugins/extensions/astrbot_plugin_quest_avatar_bridge/session/close
GET  http://192.168.1.10:6185/api/v1/plugins/extensions/astrbot_plugin_quest_avatar_bridge/health
```

把 IP、端口和协议替换为实际部署值。主接入使用 `/api/v1/plugins/extensions/...`；不要依赖 AstrBot 的旧 `/api/plug/...` 兼容入口。

## 认证头

所有接口，包括 SSE 和 health，都必须同时携带两层认证：

```http
Authorization: Bearer <ASTRBOT_API_KEY_WITH_PLUGIN_SCOPE>
X-Quest-Avatar-Key: <bridge_api_key>
```

POST 还必须带：

```http
Content-Type: application/json
```

会话归属绑定到 AstrBot 验证后的 API Key 身份。创建会话、打开 SSE、提交轮次和关闭会话必须使用同一 AstrBot API Key；仅知道 `session_id` 和桥接密钥不能越权访问其他 API Key 创建的会话。

## 请求示例

### 1. 创建会话

```http
POST /api/v1/plugins/extensions/astrbot_plugin_quest_avatar_bridge/session/start
Authorization: Bearer <ASTRBOT_API_KEY_WITH_PLUGIN_SCOPE>
X-Quest-Avatar-Key: <bridge_api_key>
Content-Type: application/json

{
  "type": "session.start",
  "protocol_version": "1.0",
  "session_id": "s1",
  "client_id": "quest3-living-room",
  "user_id": "123456",
  "bot_id": "bot-main",
  "group_id": "",
  "relationship_profile_id": ""
}
```

`user_id` 和 `bot_id` 用于可选的 `relationship.snapshot@1.0` 查询，不会返回关系插件的原始好感、信任或熟悉度分数。

### 2. 打开 SSE

```http
GET /api/v1/plugins/extensions/astrbot_plugin_quest_avatar_bridge/events/s1
Authorization: Bearer <ASTRBOT_API_KEY_WITH_PLUGIN_SCOPE>
X-Quest-Avatar-Key: <bridge_api_key>
Accept: text/event-stream
```

每个会话同时只允许一个 SSE 消费者。连接会先收到注释心跳，业务事件使用 `event:` 和 JSON `data:`：

```text
: connected

event: avatar.intent
data: {"type":"avatar.intent","protocol_version":"1.0","session_id":"s1","turn_id":"t3","in_reply_to_event_id":"e9","emotion":"shy","gesture":"step_back","look_at":"away","intensity":0.65,"duration_ms":1800,"reason_code":"boundary_soft_refusal"}

event: reply.text.delta
data: {"type":"reply.text.delta","protocol_version":"1.0","session_id":"s1","turn_id":"t3","text":"先别捏得这么用力。"}

event: reply.end
data: {"type":"reply.end","protocol_version":"1.0","session_id":"s1","turn_id":"t3","status":"completed","text_sent":true,"audio_sent":false}
```

### 3. 文本轮次

文本轮次可直接验证完整的 LLM 与意图链路，不依赖 STT：

```json
{
  "type": "turn.start",
  "protocol_version": "1.0",
  "session_id": "s1",
  "turn_id": "t3",
  "text": "今天过得怎么样？",
  "cancel_previous": true
}
```

文本轮 `text` 最长 8192 个字符。语音轮可省略 `text`，或发送 `null`/精确空字符串；三种形状都会进入 `awaiting_audio`，随后使用 `audio/chunk` 和 `audio/end`。

### 4. 输入音频

输入固定为 PCM16、小端、单声道、16000 Hz。每块建议 40-100 毫秒，对应 1280-3200 个原始字节；`sequence` 从 0 开始严格连续。

```json
{
  "type": "audio.chunk",
  "protocol_version": "1.0",
  "session_id": "s1",
  "turn_id": "t4",
  "sequence": 0,
  "format": "pcm16",
  "sample_rate": 16000,
  "channels": 1,
  "data": "<BASE64_PCM16>"
}
```

结束音频：

```json
{
  "type": "audio.end",
  "protocol_version": "1.0",
  "session_id": "s1",
  "turn_id": "t4"
}
```

### 5. 交互事实

```json
{
  "type": "interaction",
  "protocol_version": "1.0",
  "session_id": "s1",
  "event_id": "e9",
  "name": "head_pat",
  "phase": "start",
  "strength": 0.7,
  "duration_ms": 0,
  "hand": "right"
}
```

允许的交互名称为 `handshake|head_pat|cheek_pinch|gaze|speaking`，阶段为 `start|update|end|cancel`。交互事件会去重和去抖；被接受的交互建立独立的 `i:<event_id>` 决策轮次，不会默认打断正常 LLM/TTS。只有 Unity 明确调用 `/interrupt` 才取消指定轮次；单会话并发 interaction 有界。

### 6. 打断

```json
{
  "type": "interrupt",
  "protocol_version": "1.0",
  "session_id": "s1",
  "turn_id": "t3",
  "reason": "user_started_speaking"
}
```

打断会提升会话 generation、清除该轮待发事件并取消任务。LLM/TTS Provider 即使在取消后迟到返回，发送前的 generation 复核也会丢弃结果；旧轮不会再产生文字、音频、动作或 `reply.end`。

### 7. 关闭会话

```json
{
  "type": "session.close",
  "protocol_version": "1.0",
  "session_id": "s1"
}
```

## 下行事件

| 类型 | 说明 |
|---|---|
| `asr.partial` | 可合并的临时识别文本；当前文件式 STT adapter 不产生 |
| `asr.final` | 最终识别文本 |
| `reply.text.delta` | 可重建的回复文本增量 |
| `reply.audio.chunk` | Base64 PCM16、单声道、24000 Hz 音频 |
| `avatar.intent` | 必须保留的模型无关角色意图 |
| `reply.end` | 必须保留的轮次终止标记；`completed` 为完成，`failed` 为错误终止 |
| `error` | 必须保留的轮次错误 |

慢客户端导致队列满时，插件可以合并 `asr.partial`，并优先丢弃可重建的 partial 或文字增量。`asr.final`、`reply.audio.chunk`、`avatar.intent`、`reply.end` 和 `error` 不会被主动丢弃；当队列只剩这些受保护事件时，生产任务等待客户端消费。interrupt 会取消等待并清除旧轮事件，因此背压不会让旧音频越过 generation 边界。

LLM、STT 或 interaction 决策失败会固定发送 `error`，随后发送 `reply.end(status=failed,text_sent=false,audio_sent=false)`，Unity 不应继续停留在 Thinking。显式 interrupt 保持严格取消语义，确认后不发送旧 turn 的 `reply.end`。

## 意图白名单

```text
emotion: neutral | happy | shy | surprised | concerned | uncomfortable
gesture: idle | talk | wave | bow | dance | dance_next | raise_hand | turn_half | sit | lie | nod | sway | handshake | head_pat | cheek_pinch | refuse | step_back
look_at: user | hand | away | none
```

LLM 输出必须是单个严格 JSON 对象。未知枚举、额外字段、Markdown 包裹、越界数值或缺失字段会安全降级为 `neutral/idle/none`，不会透传到 Unity。确定性策略随后再次限制强度、持续时间、动作冷却和明确关系边界。

## 与现有插件的关系

- `astrbot_plugin_active_learner`：只消费 `active_learner.knowledge@1.0` 的 `global` 检索；禁止 `private:<user_id>`。
- `astrbot_plugin_identity_guardian`：按服务端可信 API 主体、客户端 ID、平台 ID 与原始 bot/user 绑定授权；任何失败只关闭受保护上下文。
- `astrbot_plugin_relationship`：仅在“序”授权当前会话后消费 `relationship.snapshot@1.0` 的只读派生字段。
- `astrbot_plugin_environment_awareness`：只消费 `environment.opportunity@1.0` 的缓存事实，不调用实时私有方法。
- `astrbot_plugin_conversation_flow`：EventBus 对话会经过其正式消息钩子；Bridge 从最终消息链取正文并保留自己的有界取消 token。回退和触碰决策仍使用本地近期历史。
- `astrbot_plugin_voice_hub`：合成消息事件标记为由 Quest 处理 TTS，避免正式消息钩子重复合成；随后 Bridge 通过 `voice.audio_output@1.0` 对最终正文做一次流式输出。
- `astrbot_plugin_update_manager`：只在启动和显式 health 读取 `update_manager.series_runtime@1.0`，不更新、安装、启停、重载或联网。
- `astrbot_plugin_orchestration_hub`：当前提供方未注册服务，因此不调用 resolver，也不猜 service 名。

## 当前限制

- STT 与 AstrBot Core TTS 默认禁用；STT 只有显式选择已实例化的正式 `STTProvider`（或命中临时旧默认兼容路径）才可用。“声”PCM 输出默认启用但可安全缺失。没有任何可用 TTS 时文本决策链仍可用，音频输入在 `audio/end` 后产生 `stt_unavailable`。
- STT 是整轮文件式识别：插件在 `audio/end` 后才调用 Provider，不产生 `asr.partial`，也不执行 VAD、回声消除或唤醒词检测。
- TTS Provider 仍是文件式合成，但 Bridge 会按安全句段顺序调用 Provider，并通过容量为 2 的生产者-消费者队列尽早发送已完成句段；不会无限并发。AstrBot 4.26.8 的 `get_audio(text)` 不接受结构化 emotion，因此角色意图中的情绪不会被猜测性地传成 Provider 参数。
- TTS Provider 若不返回可解析的 PCM WAV，会安全产生 `tts_failed`；文字、意图和最终 `reply.end(audio_sent=false)` 保留。
- 没有 Quest 真机网络、麦克风、扬声器回声、嘴型、模型动作兼容性或 72 Hz 性能验证；这些属于 Unity/设备验收。
- 会话历史和交互事实只在内存中有界保存。当前不持久化用户正文或原始音频。
- 第一版仅实现 HTTP POST + SSE。后续只有在目标 AstrBot 版本提供公开稳定 WebSocket API 后，才替换 Transport；业务层不需要改动。

## 开发验证

在插件根目录执行：

```text
python -m pytest -q
ruff check .
python -m compileall .
```

测试使用 fake/stub，不访问真实 LLM、STT、TTS 或 Quest 设备。
其中 `tests/test_http_contract_smoke.py` 会在 `127.0.0.1` 随机端口启动测试专用服务器，通过真实 HTTP 请求和生产 SSE generator 验证路由、认证、会话、打断、重连与编码；fake 只替代外部 adapter，不会进入生产插件。


## Series plugin integrations

“临”声明 `series.diagnostics@1.0` 提供方，因此可被“核”自动发现并聚合；同时保留插件自有 JSONL、有界内存时间线和 Dashboard 脱敏诊断接口。系列读取始终使用这份内存时间线，文件落盘仍由 `diagnostic_log_enabled` 独立控制，且不会改变日志归属。可选配置 `diagnostic_platform_log_enabled=true` 后，固定脱敏摘要会写入 `astrbot.plugin.astrbot_plugin_quest_avatar_bridge` 专属 logger；默认关闭，不挂接 root handler。诊断只包含原因码、HTTP 状态、耗时和汇总计数，不暴露密钥、认证头、会话身份、原始音频或回复正文，也不会阻塞其他请求。

Quest calls only this Bridge. Backend reuse of knowledge, identity authorization, relationship snapshots, cached environment facts, Voice Hub PCM output, and runtime diagnostics is documented in [docs/SERIES_INTEGRATIONS_CN.md](docs/SERIES_INTEGRATIONS_CN.md). Conversation proactive delivery and orchestration-hub resolution are intentionally not consumed in normal Quest turns.
