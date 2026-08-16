# 凝心溯溪-临

![凝心溯溪-临](logo.png)

**让虚拟角色走进现实空间。**

`astrbot_plugin_embodiment_bridge` 是 AstrBot 具身桥接插件，连接 VR/MR、桌面角色与实体设备，支持实时对话、语音、动作、表情、触碰和空间感知。它把客户端上报的文字、语音与交互事实送入 AstrBot 正式消息链，再通过 HTTP/SSE 返回文字、PCM 音频和模型无关的角色意图。

协议不绑定设备和模型格式。当前官方参考客户端是运行于 Meta Quest 3 的 [伴夏（Banxia）](https://github.com/qsbb/banxia)，目前实现 PMX/VMD、手追、物理接触、彩透和房间交互。

## 参与项目

本项目希望先提供一条可运行、可验证的具身 AI 接入路径，以此抛砖引玉，而不是把当前实现当作唯一答案。欢迎通过 [Issues](https://github.com/qsbb/astrbot_plugin_embodiment_bridge/issues) 反馈设备兼容、协议、安全和交互体验问题，也欢迎提交 Pull Request，一起完善客户端适配、服务端能力和文档。

遇到问题建议优先提交 Issue；如需进一步沟通，也可以通过 QQ：`1483904397` 联系作者。反馈时请尽量附上版本、运行环境、复现步骤和脱敏日志，请勿发送 API Key、绑定密钥或其他敏感信息。

提交内容请说明使用环境，并确认拥有所附代码、图片、模型、动作和音频的必要授权。

## 功能

- 让具身客户端的普通文字和语音进入 AstrBot EventBus，经过已配置的人格、历史、记忆、知识、工具及后处理插件。
- 使用 Protocol 1.0 提供 HTTP 上行与 SSE 下行，支持取消、迟到事件隔离、有界队列和慢客户端背压。
- 接收 PCM16 16 kHz 单声道输入；通过 AstrBot STT Provider 完成整轮识别。
- 优先复用“声”的 `voice.audio_output@1.0`，也可回退 AstrBot Core TTS；统一输出 PCM16 24 kHz 单声道音频。
- 输出受白名单约束的情绪、动作和注视意图，不向客户端发送骨骼、Morph、动画路径或 Unity 对象。
- 上报握手、摸头、捏脸、注视和说话等交互事实，由后端结合身份、关系和边界决定反应。
- 接收按会话隔离的脱敏房间语义快照，只包含地面、座位、床、桌、墙、门窗计数和场景能力布尔值；不上传图像、网格、坐标、尺寸或房间标识。
- 提供一次性二维码与 6 位短码配对，客户端无需手工搬运长期密钥和完整 API 路径。
- 提供直连/交互决策模型、正式平台、身份、STT、人格转换、可选关系增强、服务启停和诊断管理页面。
- 可以单独安装；安装凝心溯溪系列插件后，通过公开、版本化契约复用知识、身份、关系、环境、语音和诊断能力。

## 前后端仓库

| 项目 | 职责 | 仓库 |
|---|---|---|
| 凝心溯溪-临 | AstrBot 消息链、身份授权、配对、STT/TTS、动作意图和诊断 | [qsbb/astrbot_plugin_embodiment_bridge](https://github.com/qsbb/astrbot_plugin_embodiment_bridge) |
| 伴夏（Banxia） | Unity/XR 客户端、PMX/VMD、手追、物理接触、彩透、房间理解和音频播放 | [qsbb/banxia](https://github.com/qsbb/banxia) |

伴夏是 Protocol 1.0 的参考客户端，不是唯一客户端。第三方客户端遵守认证、事件顺序、音频格式和意图白名单后，也可以复用本插件；两个项目保持独立版本和独立发布。

## 消息链路

```mermaid
flowchart LR
    C["伴夏或其他具身客户端"] -->|"HTTP：文字、PCM16、交互事实"| B["凝心溯溪-临"]
    B -->|"服务端规范身份"| E["AstrBot EventBus"]
    E --> P["人格、记忆、知识、工具与后处理"]
    P --> E
    E -->|"捕获最终回复"| B
    B -->|"SSE：文字、PCM16、角色意图"| C
```

Bridge 使用服务端保存的 Bot、User 和可信平台创建正式 AstrBot 消息事件。客户端不能自报管理员身份、平台、人格或自然人；AstrBot 生成的回复只返回当前具身客户端，不会重复发送到绑定的 QQ 等原平台。

`allow_direct_provider_fallback` 默认关闭。正式消息链不可用时会明确报错，避免静默绕过记忆、知识和后处理插件；触碰等受控交互仍使用独立的兼容决策路径。

## 快速开始

运行要求：AstrBot `>=4.26,<5`、一个可用的 Chat Completion Provider，以及客户端能够访问的 AstrBot 主机。STT、TTS 和其他凝心溯溪插件均为可选能力。

1. 将仓库目录安装为 `data/plugins/astrbot_plugin_embodiment_bridge/`，安装 [requirements.txt](requirements.txt) 后重载插件。
2. 在 AstrBot 管理后台创建并启用平台或会话默认聊天模型；需要语音输入时，再创建一个正式 STT Provider。
3. 在 AstrBot“设置 → API Key 管理”创建一把具身客户端专用 API Key，至少授予 `plugin` scope。密钥明文通常只显示一次。
4. 打开插件的“具身服务控制台”Page。若需要正式 EventBus、记忆和其他消息插件，选择平台并完成高级身份验证；如果不想配置 Bot/User 或安装“序”，可开启“基础对话模式”，只选择一个临的聊天 Provider，Quest 对话会隔离直连该 Provider，不进入 EventBus。
5. 在插件配置中启用内置 listener。私网 Docker 部署的最小示例：

   ```text
   bridge_service_enabled=true
   pairing_listener_enabled=true
   pairing_listener_host=0.0.0.0
   pairing_listener_port=8520
   pairing_listener_upstream_url=http://127.0.0.1:6185
   pairing_listener_public_url=http://192.168.50.10:8520
   pairing_public_url=http://192.168.50.10:8520
   allow_private_http_pairing=true
   ```

6. Docker 同时映射 `8520:8520`。端口映射本身不会创建监听器，控制台必须显示 listener ready。
7. 打开“具身客户端快速绑定”Page 生成 6 位短码。伴夏只需输入域名或 IP、端口和短码即可完成绑定。
8. 通过控制台状态、认证后的 `/health` 和脱敏日志确认 EventBus、身份、STT/TTS 与 listener 状态。

公网部署必须使用客户端信任的 HTTPS，并在防火墙或反向代理继续限制来源与速率。内置 8520 listener 不提供 TLS，也不是 Dashboard 或任意 URL 的通用代理。

完整安装、Docker、curl 与安全说明见 [本地联调文档](docs/LOCAL_INTEGRATION_CN.md)，配对步骤见 [快速绑定文档](docs/PAIRING_CN.md)。

## 配置摘要

推荐优先通过“具身服务控制台”管理模型、平台、身份、人格和端口，避免直接编辑内部迁移字段。

| 配置 | 默认值 | 作用 |
|---|---:|---|
| `bridge_service_enabled` | `true` | 启停具身对话服务；关闭时清理现有会话和 listener |
| `chat_provider_id` | 空 | 触碰/动作决策及显式直连回退使用的 Provider；不覆盖正常 EventBus 对话的默认模型 |
| `fast_action_enabled` | `true` | 使用独立快速模型异步判断动作，并与主回复动作工具进行同轮单动作仲裁 |
| `fast_action_provider_id` | 空 | 快速动作专用 Chat Completion Provider；与普通对话模型独立，建议选择低延迟模型 |
| `fast_action_timeout_seconds` | `6.0` | 快速动作调用上限；动作决策与文字/TTS并行，正文不等待动作模型；超时只在 `reply.end` 前安全回退。Provider 首 token 较慢时建议保持 6 秒以上 |
| `enable_astrbot_message_pipeline` | `true` | 让普通文字和语音进入 AstrBot 正式消息链 |
| `allow_direct_provider_fallback` | `false` | 正式链路失败时是否允许直连 Provider；不建议用它掩盖配置问题 |
| `quest_direct_dialogue_mode` | `false` | 无平台身份的基础对话模式；不进入 EventBus，不需要 Bot/User 或“序”，关系/记忆/其他消息插件不会注入 |
| `pairing_listener_enabled` | `false` | 启用严格白名单内置 listener |
| `pairing_listener_port` | `8520` | listener 监听端口，范围 1024–65535 |
| `pairing_listener_public_url` | 空 | 客户端可访问的 listener 地址 |
| `pairing_public_url` | 空 | 配对成功后下发的正常 Bridge 基地址 |
| `allow_private_http_pairing` | `false` | 仅在受控私网 IP 上允许明文 HTTP |
| `bridge_api_key` | 空 | Bridge 第二层认证密钥；至少 32 个随机字符，可由身份保存流程生成 |
| `pairing_astrbot_api_key` | 空 | 具身客户端专用、具有 `plugin` scope 的 AstrBot API Key |
| `trusted_client_id` | 空 | 服务端固定的客户端标识 |
| `trusted_platform_id` | 空 | 创建正式 EventBus 消息所使用的已加载平台实例 |
| `relationship_person_id` | 空 | 可选的“情”自然人映射；不授予 owner 或管理权限 |
| `astrbot_stt_provider_id` | 空 | 显式选择的 STT Provider；留空关闭语音识别 |
| `enable_voice_hub_tts` | `true` | 优先使用“声”的 PCM WAV 契约 |
| `enable_astrbot_tts` | `false` | 允许回退到 AstrBot 当前 Core TTS Provider |
| `persona_source_mode` | `astrbot` | 继承 AstrBot 人格或启用兼容手动覆盖 |
| `persona_converter_provider_id` | 空 | 只用于把 AstrBot 人格转换成具身人格的独立 Provider |
| `active_quest_persona_id` | 空 | 当前启用的具身人格文件；空值表示实时继承 AstrBot |

Provider 下拉列表和管理响应只返回必要的安全摘要，不返回 API Key、Base URL、请求头或原始 Provider 配置。完整配置项和人格边界见 [API 文档](docs/API_CN.md) 与 [人格集成文档](docs/PERSONA_INTEGRATION_CN.md)。

快速动作结果分为两层：快速动作模型与 AstrBot EventBus 动作工具可以并行准备，但同一轮只允许一个白名单动作保留；明确动作命令优先，快速模型与主回复工具通过有界保留标记互斥。主回复最多看到“动作计划已发送、尚未确认执行”的有界快照，因此不得声称动作已经完成；客户端通过双重认证回报 `completed/rejected/interrupted` 后，后续同会话 EventBus 轮次才会把该终态作为身体事实读取。明确的整句动作命令（包括“下蹲/蹲下/crouch/squat”）直接由严格解析器选择，不等待快速 Provider；否定、引用、假设、讨论或多动作表达不会猜测执行。

## Protocol 1.0

新客户端使用以下基地址：

```text
/api/v1/plugins/extensions/astrbot_plugin_embodiment_bridge
```

配对后的正常请求，包括 SSE 和 `/health`，必须同时携带：

```http
Authorization: ApiKey <具有 plugin scope 的专用 Key>
X-Embodiment-Bridge-Key: <bridge_api_key>
```

| 方法 | 路径 | 成功状态 | 用途 |
|---|---|---:|---|
| POST | `/session/start` | 201 | 创建绑定到认证主体的会话 |
| GET | `/events/<session_id>` | 200 | 建立该会话唯一的 SSE 下行流 |
| POST | `/turn/start` | 202 | 开始文字或语音轮次 |
| POST | `/audio/chunk` | 202 | 上传 PCM16 单声道 16 kHz 输入块 |
| POST | `/audio/end` | 202 | 完成输入并启动 STT 与决策 |
| POST | `/interaction` | 202 | 上报交互事实 |
| POST | `/action/result` | 200 | 回报服务端动作意图的客户端执行状态 |
| POST | `/interrupt` | 200 | 取消轮次并阻止迟到事件 |
| POST | `/session/close` | 200 | 关闭并清理会话 |
| POST | `/spatial/context` | 200 | 更新按会话隔离的脱敏房间语义快照 |
| GET | `/health` | 200 | 读取协议与能力的脱敏状态 |

SSE 事件包括 `asr.partial`、`asr.final`、`avatar.intent`、`reply.text.delta`、`reply.audio.chunk`、`reply.end` 和 `error`。当前文件式 STT 不产生 `asr.partial`；输出音频固定为 PCM16 单声道 24 kHz。

空间快照只包含有界的物体计数与能力布尔值，30 秒未刷新即失效；官方客户端以 15 秒低频续租，并对相同内容去重。图像、网格、坐标、尺寸、锚点和自由文本不会进入该通道。

交互事实只接受 `handshake`、`head_pat`、`cheek_pinch`、`gaze` 和 `speaking`。角色意图使用受控的 `action_id + method + parameters + transition + source`，并保留 `gesture` 等旧字段。转身只允许有界角度，下蹲只允许深度与保持时间，未知字段、枚举或越界值不会透传给客户端。

`session.start.supported_actions` 是可选能力声明；服务端和模型只使用声明值与服务端注册动作的交集。省略它的旧客户端继续使用旧动作集，但不会收到新增 `crouch`。可执行动作的 `avatar.intent` 会附带服务端生成的 `action_id`。客户端以 `/action/result` 回报 `accepted -> started -> completed`，或回报 `rejected` / `interrupted`；`idle`、`talk` 无需回执。只有经过双层认证且匹配原动作计划的终态，才会作为有界、短时的身体事实注入后续具身 EventBus 轮次。

内置 listener 的匿名能力只限新插件 ID 下的精确 `POST .../pairing/exchange`。它接受一次性 token 或 6 位短码，并实施凭据过期、单次消费、正文限制、来源限速和全局限速；其他运行接口仍必须携带双层认证。

完整请求 schema、SSE 顺序、管理端点、错误码、背压与重连语义见 [Protocol 1.0 API 文档](docs/API_CN.md)。机器可读样本位于 [fixtures/protocol_v1/](fixtures/protocol_v1/)。

## 从旧插件 ID 升级

`1.0.0` 将内部插件 ID 从 `astrbot_plugin_quest_avatar_bridge` 改为 `astrbot_plugin_embodiment_bridge`。

升级前请备份旧插件目录、`data/config/astrbot_plugin_quest_avatar_bridge_config.json` 和 `data/plugin_data/astrbot_plugin_quest_avatar_bridge/`，停止旧插件后使用新目录名安装，并完整重启 AstrBot。AstrBot 当前没有 Web API 注销接口，只热重载可能让旧路由继续留在进程中。

首次启动会以非破坏方式迁移已知配置并复制旧数据：

- 已存在的新配置和新数据优先，不会与旧内容静默合并。
- 旧配置文件和旧数据目录不会被删除。
- 符号链接、Junction、超限或畸形输入会失败关闭。
- 旧 URL 中的插件根路径会迁移到新 ID；密钥不会写入日志或管理响应。

已绑定客户端有一个主版本周期的兼容窗口：旧十个运行 API 路径与 `X-Quest-Avatar-Key` 仍可通过双层认证访问。旧插件 ID 下的匿名 `pairing/exchange` 不开放，因此建议升级后重新生成短码并配对。Protocol 1.0 的 QR type、既有配置字段以及跨插件 `identity.quest_*`、`relationship.quest_*` 契约名暂时保持不变。

## 凝心溯溪系列

“临”可以单独安装。推荐按需要组合凝心溯溪系列；提供方缺失或契约不兼容时，临会按各能力边界降级，不读取其他插件的私有配置或内部存储。

| 模块 | 作用 | 仓库 |
|---|---|---|
| 知 | 知识学习、检索与验证 | [astrbot_plugin_active_learner](https://github.com/qsbb/astrbot_plugin_active_learner) |
| 言 | 对话节奏、消息链与表达控制；具身消息通过 EventBus 自然经过其钩子 | [astrbot_plugin_conversation_flow](https://github.com/qsbb/astrbot_plugin_conversation_flow) |
| 序 | 身份、主人和精确授权 | [astrbot_plugin_identity_guardian](https://github.com/qsbb/astrbot_plugin_identity_guardian) |
| 情 | 自然人映射、关系状态与边界 | [astrbot_plugin_relationship](https://github.com/qsbb/astrbot_plugin_relationship) |
| 境 | 环境事实、机会与预警 | [astrbot_plugin_environment_awareness](https://github.com/qsbb/astrbot_plugin_environment_awareness) |
| 声 | TTS、音色和 PCM 音频输出契约 | [astrbot_plugin_voice_hub](https://github.com/qsbb/astrbot_plugin_voice_hub) |
| 核 | 系列更新、诊断聚合与安全边界 | [astrbot_plugin_update_manager](https://github.com/qsbb/astrbot_plugin_update_manager) |
| 枢 | 跨插件编排基础设施；临当前不消费其 resolver | [astrbot_plugin_orchestration_hub](https://github.com/qsbb/astrbot_plugin_orchestration_hub) |
| 临 | 具身客户端桥接（本仓库） | [astrbot_plugin_embodiment_bridge](https://github.com/qsbb/astrbot_plugin_embodiment_bridge) |

临实际消费的契约、调用频率、授权条件和失败行为见 [凝心溯溪系列集成文档](docs/SERIES_INTEGRATIONS_CN.md)。

## 文档

- [Protocol 1.0 与管理 API](docs/API_CN.md)
- [本地部署、Docker、curl 与安全配置](docs/LOCAL_INTEGRATION_CN.md)
- [一次性配对流程](docs/PAIRING_CN.md)
- [AstrBot 人格继承与具身人格转换](docs/PERSONA_INTEGRATION_CN.md)
- [凝心溯溪系列契约集成](docs/SERIES_INTEGRATIONS_CN.md)
- [独立诊断日志](docs/DIAGNOSTIC_LOG_CN.md)
- [机器可读协议样本](fixtures/protocol_v1/)

## 使用与参考

本插件直接运行于 [AstrBot](https://github.com/AstrBotDevs/AstrBot)，并使用 [Pydantic](https://github.com/pydantic/pydantic)、[aiohttp](https://github.com/aio-libs/aiohttp)、[python-qrcode](https://github.com/lincolnloop/python-qrcode) 与 Python 3.13+ 条件依赖 `audioop-lts`。依赖由 `requirements.txt` 单独安装，源码未复制进本仓库。

以下项目用于研究协议拆分、实时事件、打断、异步管线和具身客户端组织方式。“参考”表示阅读公开设计后独立实现，不表示复制其源码、素材或品牌：

| 项目 | 参考点 | 上游许可 |
|---|---|---|
| [伴夏](https://github.com/qsbb/banxia) | Protocol 1.0 官方客户端与设备侧实现 | MPL-2.0；第三方组件和用户资源按各自条款 |
| [OpenAI Realtime Console](https://github.com/openai/openai-realtime-console) | 实时音频事件、打断和调试可观察性 | MIT |
| [Gemini Live API Web Console](https://github.com/google-gemini/live-api-web-console) | PCM 队列、全双工会话和多模态通道 | Apache-2.0 |
| [Pipecat](https://github.com/pipecat-ai/pipecat) | 异步处理管线、轮次和 barge-in | BSD-2-Clause |
| [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber) | VAD、可取消会话与表情映射 | MIT；其 Live2D 样例模型另有条款 |
| [Together Companion](https://github.com/menglimi/astrbot_plugin_together_companion) | AstrBot 消息链、连续识别和房间连接思路 | 仓库未声明许可证；仅作行为参考，未复制或分发代码 |

更完整的依赖、参考项目和头像授权边界见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 许可证

本仓库原创源码采用 [Mozilla Public License 2.0](LICENSE)。修改 MPL 覆盖的源码文件并分发时，需要继续公开这些文件的源码和修改；它不会自动要求与本项目组合的独立文件采用同一许可证。

AstrBot、Python 依赖、参考项目、头像及其他第三方内容继续遵守各自许可证或授权，MPL-2.0 不会替代其上游条款。
