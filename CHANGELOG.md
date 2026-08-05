# Changelog

## Unreleased

## 0.2.1 - 2026-08-04

### Added

- 新增独立「Quest 角色设置」Page：管理员可以从安全投影中选择 Chat Completion Provider，并点击读取“情”的 relationship.identity_candidates@1.0 脱敏自然人候选后保存。
- 新增 Page 发现、响应式布局、JavaScript 语法、脱敏字段和权限边界测试。

### Security

- 快速绑定 Page 继续只生成一次性二维码/短码，不承载模型、自然人或长期密钥管理。
- 角色设置 Page 只显示 id/model/adapter_type/provider_type 与 person_id/display_name/account_count；不读取 Provider 原始配置、平台 UID、Bot ID、UMO 或关系私有存储。
- 自然人保存仍由后端重新读取正式候选契约并校验；绑定不替代原始平台身份，也不授予 owner、白名单或管理权限。

## 0.2.0 - 2026-08-04

### Added

- 新增 Bridge 插件独立 MiMo STT，可直接使用插件专属配置调用 `mimo-v2.5-asr`，不读取、不启用也不修改 AstrBot 全局 STT Provider。
- 新增服务端快速绑定默认值：连接地址、Quest 专用 API Key、客户端与会话身份、TTL 均由本插件专属配置注入，不进入 Page、二维码、状态响应或日志。

### Changed

- 新增默认关闭的内置 aiohttp Quest listener，可在 `initialize()` 中监听容器 8520，并在 `terminate()` 中幂等释放 site、runner、client session、活动流和端口。
- 内置 listener 只匿名接受精确 `POST pairing/exchange`，并直接复用现有 `PairingManager` 的 token/短码、IP 绑定、TTL、撤销、单次消费与双重限速状态。
- 配对后的 health/session/SSE/turn/audio/interaction/interrupt/close 仅按 method+path allowlist 流式代理到固定 loopback HTTP 上游；不代理 Dashboard、其他插件或配对管理接口。
- 配对交换现在按 AstrBot 4.26.8 公开请求接口读取 `request.client_host`，不再使用不存在的 `request.remote_addr`；可信代理来源和 Quest IP 绑定因此能在真实插件路由中正常工作。
- 首次配对不再假设 `register_web_api` 支持匿名 exchange；内置 listener 是私网首选入口，旧精确反向代理配置保留为兼容 fallback；两者都不可用时 Page 失败关闭。
- 快速绑定 Page 精简为生成二维码、短码、状态、倒计时、复制和撤销；不再显示或采集 Quest IP、长期密钥、平台/客户端身份、会话 ID、角色设置或有效期选项。
- 私网 HTTP 配对要求服务端显式开关和私网 IP 字面量，成功配置才返回 `allow_insecure_http=true`；公网地址和域名仍强制 HTTPS。
- 加固“知、序、情、境、声、核”consumer：契约版本格式、能力、方法、安全元数据和返回 schema 均显式校验；缺失、超时和畸形响应按契约降级。
- `session/start` 响应新增不含身份值的 `protected_context` 授权状态；未配置服务端可信客户端/平台来源时默认失败关闭，基础 Quest 对话保持可用。
- `GET /health` 现在显式刷新“核”的只读运行态快照，并公开 global-only、cached-only、关系授权门控及 Voice Hub PCM 可用性。

### Security

- 一次性配对保留每来源和全局双重限速；快速绑定依靠高熵单次 token、短 TTL 和单次消费，兼容创建请求仍可显式绑定 Quest IP。内置 exchange 使用直连 peer IP，官方注册路由仍要求 AstrBot 身份，旧外部代理路径仅在可信直连代理时接受来源覆盖。
- 内置 listener 不持有、不注入 Dashboard JWT 或代理服务 Key，不信任任何 Forwarded/X-Forwarded/X-Real-IP 来源头，也不记录认证头、token、短码、请求体或完整查询参数。
- listener 端口占用、配置错误或 loopback 上游不可达只产生脱敏 degraded 状态，不阻止插件其余官方路由加载。
- 拒绝空白群作用域、跨平台 private knowledge、实时环境私有方法、主动消息投递和未注册枢服务；不信任 Unity 自报的 API 主体或平台身份。

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
