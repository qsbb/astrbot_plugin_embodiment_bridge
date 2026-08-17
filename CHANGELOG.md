# Changelog

## Unreleased

## 1.0.14 - 2026-08-17

### Fixed

- 为 Bridge 创建的 AstrBot EventBus 轮次增加 29 秒独立终帧截止线；超时安全取消等待并发送一次脱敏错误和一次 `reply.end(status=failed)`。
- 为动作首事件、EventBus 阶段、终帧发送与取消增加同 trace 脱敏诊断，并保证成功或失败终帧 exactly-once。
- 合法 action-only 轮次保留空成功终帧；客户端区分首事件、回复停滞和动作后缺终帧超时。

## 1.0.13 - 2026-08-17

### Fixed

- 快速动作模型超时、不可用或明确返回无动作时，对短且无歧义的问候/告别、自我介绍、感谢/道歉、赞同和庆祝语境增加保守的本地社交动作兜底；相同自主动作设有短时防抖，EventBus 动作工具和模型已选动作仍优先，普通事实问答、引用和元语言不会触发。
- EventBus 已选动作后立即取消仍在运行的独立快速动作请求，避免它继续占用 Provider 直到 6 秒截止线；唯一动作仲裁和客户端能力白名单保持不变。
- 独立诊断页增加本地自主动作事件、原因与来源的中文标签；音频分块仍只记录一次上传汇总，自动滚动开关继续持久化并允许手动停留查看历史。

## 1.0.12 - 2026-08-17

### Fixed

- 将旧版 `v2 + 4秒` 快速动作策略运行时迁移到 6 秒，覆盖日志中模型在约 4.3 秒刚超过旧截止线便被取消的问题；管理页再次保存后写入 v3，仍允许管理员明确选择新的超时值。
- 保留 v1.0.11 的自主动作提示和 EventBus 动作工具仲裁；迁移只影响独立动作模型的等待边界，不切换 Provider、不绕过 AstrBot 正式消息链，也不改变正文或 TTS 模型。

## 1.0.11 - 2026-08-17

### Fixed

- 明确区分“执行动作”和“同时需要文字回复”：动作可继续快速下发，AstrBot EventBus 仍须经过人格、记忆和后处理生成正文；明确要求回复却得到空正文时返回专用诊断，不再伪装成成功的静默动作轮。
- 扩展严格单动作语法，支持“自然地挥手，并简短回复我”等常见复合表达；仍拒绝否定、引用、假设、多动作和越界参数。
- 快速动作已抢先选择时不再向主模型暴露第二个动作工具，晚到竞争返回可审计的 superseded，并保留正文生成职责。
- 为问候、感谢、道歉、主动参与和明显庆祝补充受约束的自主动作映射；坐、躺、下蹲、转身和切舞仍保持明确请求优先。

## 1.0.10 - 2026-08-17

### Fixed

- 快速动作结果明确区分已选择动作、模型主动不动作和无效输出；无效完整 JSON 会立即失败并降级，不再等待流超时。
- 快速动作提示不再同时要求“日常自主动作”和“普通对话一律不动作”，允许在有意义的情绪与互动语境中选择自然动作。
- 快速动作诊断补充配置超时、实际超时和解析结果，并修正 Provider 已就绪时仍显示旧 `not_configured` 状态的问题。
- EventBus 后处理插件调用 `stop_event()` 接管投递时，临会继续读取最终 `MessageEventResult`，不再丢失已经生成的正文；真正的动作独占空回复按合法静默动作轮收口。
- Operator Page 补齐动作无效输出、主动不动作和 EventBus 动作独占轮的中文诊断标签。

## 1.0.9 - 2026-08-16

### Added

- 快速动作设置页新增 0.5–15 秒超时输入，显示 configured/effective timeout 和独立 policy revision；旧 4 秒默认只做可审计运行时迁移，不覆盖显式设置。
- 流式快速动作解析支持增量/累计 chunk，在严格完整动作 JSON 成立时提前结束并有界关闭 Provider；新增多 JSON、尾部垃圾、延迟流尾和卡死关闭回归测试。
- 诊断时间线补齐动作仲裁、EventBus 工具调用、意图下发和音频上传汇总的中文标签与元数据。

### Fixed

- 诊断日志刷新关闭自动滚动时保留原 scrollTop/距底部锚点，重新开启时才滚到底部。

## 1.0.8 - 2026-08-16

### Fixed

- 快速动作模型不再阻塞正文或 TTS；动作任务在同一轮继续与 EventBus 工具竞选，并在 `reply.end` 前完成唯一动作仲裁或安全回退。
- 快速动作默认超时从 4 秒调整为 6 秒，覆盖常见 Provider 首 token 延迟；超时、空结果、EventBus 未调用和仲裁胜者均写入脱敏阶段诊断。
- 音频分块不再逐条写入 HTTP 202 诊断，改为 `audio.upload.completed` 汇总；上传异常仍保留错误诊断。
- Operator Page 的独立日志增加默认开启的“自动滚动”开关，关闭后刷新不抢回阅读位置，重新开启立即滚到底部。

## 1.0.7 - 2026-08-16

### Added

- 允许快速动作通道与 AstrBot EventBus 动作工具在同一轮进行受控竞选；有明确情绪或互动语境时可自主选择一个轻微动作。

### Fixed

- 修正快速动作启用时 EventBus 工具被提前禁用，导致主模型无法发起自主动作的问题。
- 增加同轮动作保留标记，确保快速动作与 EventBus 不重复投递，未授权具身上下文也不会丢失动作仲裁。

## 1.0.6 - 2026-08-16

### Fixed

- 对已计划动作增加同轮完成话术硬门禁；模型违反提示时改为“正在开始”，只有后续认证 `completed` 回执可作为完成事实。
- `avatar.intent` 刚下发时的诊断状态改为 `planned`，不再提前记录为 `completed`。
- 客户端声明空 `supported_actions` 时，快速动作模型收到空白名单，不再回退暴露完整动作目录。

## 1.0.5 - 2026-08-16

### Added

- Protocol 1.0 会话可选声明 `supported_actions`，服务端只从客户端能力与动作注册表的交集中选择动作；省略声明的旧客户端保持原动作集合。
- 角色意图新增模型无关的 `method / parameters / transition / source` 结构化动作方法，同时保留 `gesture` 等旧字段兼容。
- 新增有界 `crouch` 动作及“下蹲、蹲下、crouch、squat”严格显式命令；明确命令不等待快速动作 Provider。

### Changed

- 快速动作模型与主回复继续并行；同轮 EventBus 现在可读取一个非阻塞、有界的动作控制器快照。快照只区分处理中、无动作与已发送计划，并固定 `execution_confirmed=false`，不会把计划伪装成已执行事实。
- 快速 Provider 超时、失败、返回空动作或偏离严格整句动作命令时，复用既有保守解析器执行用户明确要求的白名单动作；否定、引用、假设、讨论、多动作和不完整表达仍失败关闭为 `talk/idle`。
- 计划和 `accepted` 状态不再允许同轮回复声称动作已经完成；客户端不支持、资源缺失或执行失败时返回明确状态，并只以后续认证终态作为身体事实。

### Security

- 同轮动作快照不含用户正文、Provider、任务对象、动作 ID、回执或自由文本；实际完成、拒绝和中断仍只信任双重认证的 `/action/result` 终态，并仅注入后续已授权具身轮次。
- 动作参数、过渡时长、来源和客户端能力均使用严格白名单与数值边界；未知字段、越界参数和未协商动作失败关闭。

## 1.0.4 - 2026-08-15

### Added

- 增加默认开启、可独立选模的异步快速动作通道；动作专用模型与 AstrBot EventBus 主回复并行，只输出严格白名单 `avatar.intent`，不生成回复或改写对话历史。
- Operator Page 增加快速动作开关、专用 Provider 选择、运行状态与脱敏诊断事件。
- 增加双层认证的 `POST /action/result` 动作执行回执；可执行 `avatar.intent` 带服务端生成的 `action_id`，并按 `planned -> accepted -> started -> completed/rejected/interrupted` 管理有界会话生命周期。
- 后续已授权具身 EventBus 轮次可读取最近的客户端确认终态；`planned`、`accepted` 与 `started` 不会被表述为已经发生的身体事实。

### Changed

- 快速动作关闭、未配置或所选 Provider 缺失时，继续使用原有主回复链路动作工具；快速通道已经接管本轮后若超时或失败，则仅发送本地 `talk/idle` 兜底，不让主模型再次选择动作。普通对话、记忆、人格、工具与后处理链路保持不变。
- Protocol 1.0 客户端可继续忽略新增的可选 `avatar.intent.action_id`，也不强制提交动作回执；原有 SSE 顺序、文字、音频与 `reply.end` 语义保持不变。

### Security

- 动作回执必须同时通过 AstrBot API 与 Bridge Key 认证、会话所有权、服务端动作 ID、轮次和动作匹配校验；严格拒绝重放变体、过期计划、非法跳转及任意附加字段。回执事实只含枚举动作、终态、原因和有界时长，不包含自由文本、身份或权限。

## 1.0.3 - 2026-08-14

### Added

- 新增双层认证的 `POST spatial/context`：仅接收严格、有界的房间物体计数与场景能力布尔值，按会话内存保存，在关闭时销毁，并在 30 秒未刷新后自动失效。
- 已授权的具身 EventBus 轮次可读取当前会话的服务端验证空间快照；普通 QQ、未授权轮次和其他会话保持隔离。

### Security

- 空间快照拒绝图像、网格、坐标、尺寸、房间/锚点标识和自由文本；revision 必须单调递增，相同 revision 只有完全相同内容才幂等，且空间事实不授予身份、工具或动作权限。

## 1.0.2 - 2026-08-13

### Added

- 新增无平台身份的 Quest 基础对话模式；不安装“序”时无需填写 Bot ID、主人用户 ID 或配置平台实例，且明确隔离 AstrBot EventBus、关系、记忆和其他消息插件。
- Operator Page 将 Quest 身份分为基础绑定与高级身份设置，未完成首次验证时自动展开必要字段。
- 快速配对在基础对话模式下使用服务端隔离范围值，不伪造真实 Bot/User 身份；Protocol 1.0 请求校验保持严格。

### Documentation

- 补充不安装“序”的配置路径、模式边界和安全说明。

## 1.0.1 - 2026-08-13

### Changed

- 更新插件与仓库简介，直接说明“让虚拟角色走进现实空间”的定位，并明确支持 VR/MR、桌面角色与实体设备。

## 1.0.0 - 2026-08-11

### Breaking changes

- 插件内部 ID 与仓库名迁移为 `astrbot_plugin_embodiment_bridge`，正式 API 根路径同步迁移到 `/api/v1/plugins/extensions/astrbot_plugin_embodiment_bridge`。安装目录必须使用新 ID，并在升级后完整重启 AstrBot，避免旧版无法注销的 Web API 残留。
- 插件自有诊断契约迁移为 `embodiment_bridge.diagnostics@1.0`，独立日志迁移为 `embodiment_bridge.log`，AstrBot 日志命名空间迁移为 `astrbot.plugin.astrbot_plugin_embodiment_bridge`。

### Migration

- 首次启动会从 `data/plugin_data/astrbot_plugin_quest_avatar_bridge/` 向新数据目录执行非破坏、原子复制；拒绝符号链接或 Junction，旧目录和旧文件始终保留，并写入不含秘密的迁移标记。
- AstrBot 以安装目录命名的旧配置文件会按已知 schema 导入：只填充仍为默认值的字段，不覆盖管理员已设置的新值，不导入未知字段；成功持久化后设置一次性迁移标记，旧配置文件保持不变。
- 已绑定客户端可在一个主版本周期内继续使用旧运行 API 路径和 `X-Quest-Avatar-Key`，但仍必须通过 AstrBot API Key 与 Bridge Key 双重认证。8520 不开放旧匿名 exchange；新绑定必须使用新路径。
- Protocol 1.0 的 QR type、既有配置字段和跨插件 `identity.quest_*` / `relationship.quest_*` 契约名暂时保留，避免只升级“临”就破坏客户端、“序”和“情”的既有 1.0 联动。

### Changed

- 新客户端首选 `X-Embodiment-Bridge-Key`，EventBus 使用 `embodiment_bridge` 主标记；旧 Header 和旧事件标记仅作为有界兼容别名。
- 插件入口类改为 `EmbodimentBridgePlugin`，并保留旧类名的源码兼容别名。
- Bridge 创建且已授权的具身 EventBus 轮次会在服务端保守识别整句明确动作祈使；命中后复用现有严格动作 handler 预选白名单 intent，并继续原有单次 EventBus 模型调用。
- 否定、假设、引用、转述和动作讨论语境禁止动作工具；多动作歧义和不完整表达仍交给请求级 `quest_avatar_action` 兼容工具，模型未调用时继续使用 `talk`。

### Added

- 新增根目录 `logo.png`，作为凝心溯溪-临在 AstrBot 插件列表和详情页中的头像。
- README 新增前后端职责、EventBus 架构、快速配置、Protocol 1.0 摘要、凝心溯溪系列互荐、参考项目与贡献说明。
- 原创源码采用 MPL-2.0，并补充第三方依赖、参考项目与头像边界声明。

### Fixed

- 服务开关和监听端口切换以会话创建门为边界；启动或换端口失败会尝试恢复持久化配置与旧监听器，避免产生无传输会话。
- 修正文档中已经过期的 `Authorization: Bearer` 示例为 AstrBot 当前使用的 `Authorization: ApiKey`，并同步 TTS 分段流水线和内置 listener 配对语义。

### Diagnostics

- 增加显式动作解析、预选、工具跳过和异常模型覆盖拒绝的脱敏事件，只记录白名单动作名、状态与原因码，不记录用户正文或身份。

### Security

- 旧数据迁移只允许两个精确插件专属目录，拒绝重解析点并通过同级 staging 原子提交；已有新数据永远优先且不会与旧数据静默合并。
- 8520 匿名面仍只有新 ID 下的精确 `POST pairing/exchange`；旧路径兼容只覆盖需要双重认证的九个 Protocol 1.0 运行接口。

## 0.4.22 - 2026-08-11

### Added

- AstrBot EventBus 的 Quest 轮次会收到请求级 `quest_avatar_action` 工具和同轮动作使用约束，模型可在同一回复链路中选择严格白名单动作；工具与约束不会注册或注入全局，QQ 与其他平台事件不会看到或执行它。
- 动作白名单新增 `dance_next`、`raise_hand`、`turn_half`、`sit` 与 `lie`，并继续拒绝任意动画路径、骨骼名、未知动作和额外参数。

### Fixed

- EventBus 回复不再把所有动作固定降级为 `talk`；同一 Quest 事件上由工具确认的动作会作为 `avatar.intent` 返回 Unity，没有动作调用时仍保持原有说话姿势。
- 每轮最多接受一个动作决定；重复调用、非 Quest 事件、未知动作和动作结果写入失败全部失败关闭。

### Diagnostics

- 新增动作工具暴露、动作接受、拒绝和内部失败的脱敏诊断，只记录白名单动作名、状态、原因码、手势与有界时长，不记录用户文本、回复正文、身份或凭据。

## 0.4.21 - 2026-08-11

### Fixed

- 人格转换不再等待 `Context.llm_generate()` 一次性返回，而是直接复用管理员所选、已实例化的 AstrBot Chat Provider 的公开 `text_chat_stream()`；不读取 Provider 配置、API Key、Base URL 或请求头，也不自动切换 Provider。
- 转换请求将 Provider 内层传输尝试限制为一次，并分别限制首个流块等待、流空闲和总时长，避免不可用上游在内部重试后占满完整的 300 秒预算。
- 流式结果按增量块聚合，并以最终完整块为权威结果；缺少最终块时可使用完整增量，但仍执行原有严格 JSON schema 校验。输出超过 100000 字符立即失败关闭。
- 不支持流式生成的第三方 Provider 会返回明确错误，不再退回不可观察的整轮调用或静默换模型；取消、超时和异常都会有界关闭流。

### Diagnostics

- 人格转换增加“等待首个流块、首个流块到达、持续生成、完整结果返回”阶段，并区分首块超时、流空闲超时和总超时。日志和 Page 不记录输出正文、隐藏推理、Provider ID 或任何凭据。

## 0.4.20 - 2026-08-11

### Fixed

- Operator Page 的人格转换改为可恢复的后台任务，不再依赖单个长时间 HTTP 请求；页面刷新后可从当前标签页恢复任务，转换模型等待上限由 120 秒提高到有界的 300 秒。
- 转换任务支持显式取消；插件热重载或关闭时会取消并等待仍在运行的转换，避免旧任务继续占用模型或向失效实例写入草稿。
- 后台任务绑定 Dashboard 管理员与请求指纹，不同管理员或不同人格来源不能复用现有任务；转换期间锁定来源、人格文件和编辑器，避免结果落入已切换的编辑上下文。
- 任务受理时快照转换 Provider 并纳入请求指纹；新后台入口与旧同步兼容入口共用单任务门禁，运行期间切换配置不会改变既有任务，也不能绕过并发限制。

### Diagnostics

- 人格转换新增来源读取、模型生成、结果结构校验、预览就绪、失败与取消阶段；只展示真实可观测阶段和耗时，不伪造或暴露模型隐藏推理。
- Operator Page 下方独立日志每秒自动刷新，并将当前后台转换的阶段和动态耗时作为脱敏实时进度行显示；页面隐藏时暂停日志轮询，恢复可见后立即补读。

### Compatibility

- 保留原有同步 `pairing/persona-convert` 管理端点；新增的启动、状态与取消端点继续只接受 Dashboard 身份，且不向匿名 8520 listener 暴露。

## 0.4.19 - 2026-08-11

### Fixed

- 人格转换请求不再沿用普通 Page 的 10 秒超时；前端等待上限与后端 120 秒转换上限对齐并留出缓冲，避免后端已生成预览、浏览器却丢失一次性草稿令牌。
- 人格转换期间显示来源读取、模型生成与结果校验阶段及真实已用秒数；完成后明确区分“预览未保存”“文件已保存”“当前已启用”。
- 保存当前已启用的人格时，Page 会明确提示运行时人格已立即更新，不再误报“没有自动启用”。

### Diagnostics

- “临”独立日志增加脱敏的人格转换、保存、启用和 Quest 对话注入事件，只记录阶段、状态、错误码与耗时，不记录 Provider、人设 ID、来源正文、转换正文或草稿令牌。

## 0.4.18 - 2026-08-10

### Added

- 增加可选 `server_timing@1.0` 摘要，在既有 `reply.end` 中提供脱敏的 STT、决策路径、TTS 和整轮服务端耗时；默认关闭，不新增 SSE 事件或改变 Protocol 1.0 顺序。

### Changed

- Quest 语音输入改为通过 `astrbot_stt_provider_id` 或 Operator Page 显式选择 AstrBot 已实例化的正式 `STTProvider`；目录只暴露 `id`、`model`、`adapter_type`、`provider_type`，所选实例缺失时安全关闭且不自动换模型。
- 旧 Bridge 私有 MiMo URL、Key、model 不再作为推荐或可见配置入口；迁移会清理旧私有字段且绝不回显密钥。AstrBot 当前没有普通 Star 插件稳定 STT contract，第三方能力须通过正式 STT Provider 机制注册。
- “声”的 `enable_voice_hub_tts` 开关和 `voice.audio_output@1.0` 首选 TTS 语义保持不变。

### Fixed

- 管理 Page 将 Bridge 连接与六个设置区域的首屏读取解耦；单个接口失败只禁用对应区域，不再把全局 Bridge 标记为不可用，并提供失败区域的独立重试。
- Operator Page 的 JS 与 CSS 增加同步缓存戳，避免 Dashboard 继续执行旧版事件绑定脚本。
- 配置写入优先使用新 Core 的 `save_config_async()`，并兼容 AstrBot 4.26.5 仅提供的原子 `save_config()`；旧 Core 不再把全部管理控件误判为只读。

## 0.4.17 - 2026-08-10

### Fixed

- 页面先绑定交互事件，再等待 AstrBot Page Bridge 上下文；Bridge 延迟或未注入时不再整页失去响应。
- 增加 Bridge 连接超时、请求超时和可点击的重试连接按钮，直接打开页面时会明确显示原因。

## 0.4.16 - 2026-08-10

### Fixed

- 改从公开的 `astrbot.api.event` 模块导入 AstrBot 事件过滤器，使插件可在
  AstrBot 4.26.8 与 4.27.1 正常加载；测试桩同步官方模块结构，不再提供实际
  不存在的 `astrbot.api.filter` 属性。

## 0.4.15 - 2026-08-10

### Added

- 「Quest 角色设置」新增具身人格工作区，可从 AstrBot 已有人格导入转换、手动新建/编辑、预览、保存、显式打开、重转、启用、停用与删除；转换 Provider 可从已配置模型中单独选择。
- 每个人格以服务端随机 ID 独立保存在插件数据目录；转换预览采用 30 分钟一次性内存草稿，确认保存前不创建人格文件。
- 补齐 `series.diagnostics@1.0` 自声明，复用现有有界内存诊断事件供系列聚合读取；插件 JSONL、Dashboard 接口和日志归属保持独立。

### Changed

- 启用的临专用人格只作用于带 `quest_avatar_bridge` 标记的 EventBus 轮次和 Bridge 回退决策，QQ 与其他 AstrBot 会话继续使用原人格。保存实时 AstrBot 人格来源会原子停用临专用人格。
- 角色存在方式转换为与用户同处现实空间的人，同时严格限制只能相信系统明确提供的视觉、听觉、空间、环境和触碰事实。

### Security

- 人格目录和模型目录只返回白名单摘要字段；正文需显式打开单个人格。管理端点只接受 Dashboard 身份，不经 8520 匿名 listener 暴露。
- 人格文件采用严格 schema、来源哈希、原子替换、路径穿越/符号链接防护和最佳努力 `0600` 权限；人格数据容器会转义可提前闭合标签的字符。

### Tests

- 新增人格转换、文件事务、草稿一次性消费、Provider 脱敏、Quest-only EventBus 注入、运行时激活以及完整 HTTP 管理闭环回归。

## 0.4.14 - 2026-08-09

### Fixed

- 保存“情”中的自然人时，新增消费服务端 `relationship.quest_event_identity@1.0`：仅解析该自然人在当前活跃 AstrBot 平台上的唯一完整私聊账号，并同步正式 EventBus 的 Platform/Bot/User 身份，修复占位身份通过唤醒后被白名单或会话门禁中止的问题。
- 解析成功后复用已验证的 principal 摘要调用“序”新增的 `identity.quest_binding_control@1.0`，只保存 Quest 私聊只读绑定且不写入 `owner_users`；未安装“序”时仍使用“临”本地精确绑定。多账号歧义、群聊、账号不完整、平台不在线或契约不兼容时明确失败，不读取“情”的私有 registry 兜底。
- `session/start` 现在使用服务端规范 Bot/User 身份覆盖设备占位声明，并在每次新会话重新向“情”复核当前自然人账号；已有选择会在插件初始化时自动修复。二维码与配对交换只下发固定占位身份，原始账号不再进入 Quest。
- 身份同步使用专用串行锁和 `pending -> ready` 失败关闭状态；“序”拒绝、本地保存失败或中途重启时不会用半同步身份启动新会话。

### Security

- 原始 Platform/Bot/User/UMO 仅在服务端插件调用中使用；Bot/User 存入插件数据目录的原子服务端身份文件，旧 AstrBot 配置会自动迁移并清空。管理 Page 只返回是否已配置，Quest 只收到占位身份，独立日志也不记录这些值。自然人身份事实本身不授予 owner、白名单、管理或平台操作权限。

## 0.4.13 - 2026-08-09

### Fixed

- EventBus 已知空回复不再被压缩成泛化的 `turn_failed`；现在保留未唤醒、唤醒后中止、发送捕获为空、无输出等精确安全错误码，并继续以 `reply.end(status=failed)` 正常收束轮次。
- “临”独立诊断与 health 快照新增事件是否唤醒、是否中止、是否观察到发送三个脱敏状态，便于区分 AstrBot 白名单/会话门禁与回复捕获问题。

## 0.4.12 - 2026-08-09

### Fixed

- 修复 Quest 身份页已显示 AstrBot API Key 配置完成，保存时却仍强制要求重新输入密钥的问题；现在输入框留空会复用后端已有密钥并通过 AstrBot 认证层重新验证，原始密钥仍不回显、不进入响应或日志。

## 0.4.11 - 2026-08-09

### Changed

- Quest 身份保存改为使用 AstrBot 官方 `ApiKey` 认证层证明调用方身份，只对认证后生成的 `api_key:<key_id>` principal 做 SHA-256 摘要；不再从原始 API Key 猜测 principal。
- “Quest 角色设置”页面每次保存身份都通过 Page Bridge 提交；后端再从严格 loopback 地址调用只读 principal 证明端点。密钥不进入页面存储、证明响应或日志。
- 管理端点现在明确拒绝 API Key principal，只接受 Dashboard 身份；只读 principal 证明端点则只接受具有 `plugin` scope 的 API Key，并且不经 8520 listener 暴露。

### Fixed

- 修复“序”中 Quest 白名单摘要与 AstrBot 运行时 principal 永远不一致、导致 `quest_identity_not_allowlisted` 的问题。
- 修复未安装“序”时本地精确绑定在插件重载后仍按原始 API Key 构造错误 principal 的问题；现在持久化不可逆的已认证 principal 摘要。

## 0.4.10 - 2026-08-09

### Added

- 「Quest 角色设置」Page 新增内置 listener 端口输入与即时应用按钮，允许配置 1024-65535，默认值保持 8520。

### Changed

- 修改端口会持久化设置、同步已配置公开 URL 的端口、关闭旧端口上的 Quest 会话并重启 listener；页面会直接显示新端口和启动结果。

## 0.4.9 - 2026-08-09

### Added

- 「Quest 角色设置」Page 可集中保存 Quest 客户端、平台、Bot、主人用户与专用 API Key；Bridge Key 缺失时自动生成且所有密钥始终只写不回显。
- 新增 `identity.control_plane@1.0` 消费端：“序”存在时写入统一主人与 SHA-256 principal 摘要白名单，未安装时使用“临”自身的精确本地绑定。

### Changed

- 管理页诊断改为与 Quest 设备端相同的“当前根因、链路、输入、耗时、阶段时间线（最新在下）”视图，不显示原始 JSON。
- 文档与测试中的部署专用账号和局域网地址已替换为通用测试样例。

### Fixed

- 修复只安装“临”时无法授权受保护 Quest 上下文的问题；本地回退仍要求 API principal、client、platform、bot、user 与私聊范围全部精确匹配。
- 修复身份保存后快速绑定仍使用旧 Bot、用户或 API Key 的问题。

## 0.4.8 - 2026-08-09

### Added

- 独立诊断新增会话授权、音频接收、STT、AstrBot EventBus、LLM、TTS 与回复交付阶段事件，并仅记录稳定原因码、HTTP 状态、耗时和汇总计数。
- 「Quest 角色设置」Page 新增中文根因摘要与阶段时间线，不再要求管理员阅读原始 JSON。

### Changed

- 关闭 JSONL 文件日志时仍保留有界、脱敏的内存诊断；磁盘写入失败时对话不受影响，内存时间线继续可读。

### Fixed

- 修复 `reason_code`、授权结果与安全汇总计数被诊断脱敏白名单静默丢弃的问题，`owner_not_configured` 等真实失败原因现在可直接定位。

## 0.4.7 - 2026-08-09

### Added

- 「Quest 角色设置」Page 新增服务总状态、监听地址、活跃会话、SSE、队列与六项能力摘要，并提供受 Dashboard 身份保护的即时启动/关闭按钮。
- 新增受保护的 `GET pairing/service-status` 与 `POST pairing/service-control`；关闭会持久化设置、停止内置 listener、取消并清空现有 Quest 会话，管理 Page 与认证 health 保持可用以便诊断和重新开启。

### Changed

- 相同所有者和完全相同身份字段重复执行 `session/start` 时刷新“序”的授权结果并复用会话；任何身份字段变化仍返回 `409 session_conflict`。
- 消息链路失败事件保留经过白名单收敛的授权原因，Quest 可区分原始账号未绑定、客户端不匹配和可信平台不可用；未知内部原因继续统一收敛。

### Fixed

- 服务关闭现在稳定返回 `503 bridge_service_disabled`，不再因异常类型未映射而误报 500。
- 内置 listener 可在不热重载插件的情况下停止、释放端口并重新启动。

## 0.4.6 - 2026-08-09

### Added

- 新增白名单 Avatar Skill 注册表；LLM 可通过结构化 `action.name` 和受限参数调用 `wave`、`bow`、`dance`、`nod`、`sway` 及触碰反应，最终仍只输出语义 `avatar.intent`。

### Changed

- 普通文字/语音默认不再静默回退到直连 Provider；AstrBot 正式消息链路未授权或不可用时返回明确错误，避免绕过记忆、知识和后处理插件。触碰交互仍保留受控兼容决策。
- 管理员角色设置页从 AstrBot 已加载平台目录选择可信平台实例，不再要求手工猜测实例 ID；目录只返回实例 ID、适配器类型和显示名。

## 0.4.5 - 2026-08-08

### Fixed

- 通过 AstrBot 公开 `Platform.create_event()` 构造 EventBus 消息，保留真实平台事件类型、标准 MessageSession/UMO 和受信绑定身份，避免普通插件与记忆/后处理钩子只能看到 Bridge 自定义事件。

### Changed

- Bridge 不再声明 `series.diagnostics@1.0` 提供方，避免被“核”聚合；独立 JSONL、内存快照、Dashboard 脱敏诊断接口继续可用。
- 可选将固定脱敏诊断摘要写入 Bridge 专属 logger，默认关闭且不挂接 root handler。

## 0.4.4 - 2026-08-08

### Added

- 「Quest 角色设置」管理员 Page 新增可信 AstrBot 平台实例 ID 的读取与保存入口；保存前使用公开 `Context.get_platform_inst()` 验证实例，成功后立即同步身份授权与 EventBus 适配器，无需重载插件。
- EventBus 诊断新增 `availability_reason`，明确区分未配置、AstrBot API 不可用、平台实例不在线、功能关闭和可用状态。

### Security

- 平台选择入口继续受 Dashboard 身份保护，不进入快速绑定 Page、二维码、Quest 协议或 8520 匿名交换面；不枚举平台账号、不读取原始平台配置，也不授予 AstrBot 管理员权限。
- 配置持久化失败、平台 ID 非法或平台实例不存在时保持原运行时选择，禁止半更新。

## 0.4.3 - 2026-08-08

### Added

- 已授权的 Quest 文字与语音输入可作为受控合成消息事件进入 AstrBot EventBus，沿用真实平台会话来源，经过正式人格、会话、工具和插件钩子后再回传 Protocol 1.0。
- 独立诊断新增对话生成模式及最终 `reply/silent/error` 结果，不记录输入、转写或回复正文。

### Changed

- 直接 `context.llm_generate()` 改为消息管线不可用时的兼容回退，并继续负责触碰动作决策；Quest 下行 TTS 仍由 Bridge 自己流式输出，合成消息事件会阻止服务端重复 TTS。
- 空消息管线结果不再伪装成正常完成，而是作为明确失败进入既有 `error + reply.end(status=failed)` 收口。

### Security

- 只有通过既有身份适配器授权且服务端配置的原始平台仍在线时才进入 EventBus；Quest 自报字段不能选择平台、人格或 Provider，合成事件固定禁止继承 AstrBot 管理员身份。

## 0.4.2 - 2026-08-05

### Added

- Quest 角色身份默认从 AstrBot 4.27.1 公开 `persona_manager` 读取：管理员可为 Quest 显式选择人格，未选择时继承 AstrBot 明确默认人格；人格删除、读取超时或响应畸形时安全回退通用 MR 身份。
- 「Quest 角色设置」Page 只列出脱敏人格 ID 与来源、状态、布尔标签，不返回人格正文、预设对话、工具、技能或错误模板。

### Changed

- 现有四个手动角色字段保留为兼容配置，但仅在管理员明确启用 `manual_override` 时生效；默认 `astrbot` 模式不会让旧手动字段覆盖 AstrBot 人格。
- 每个 LLM turn 异步读取一个有界超时的人格快照。AstrBot 人格作为受限的身份与表达数据注入，不能覆盖 Protocol 1.0 JSON schema、认证授权、安全边界、动作白名单或模型无关边界。

### Security

- Unity 的 `session/start` 及其他 Protocol 1.0 请求不接受 persona 内容或 persona ID；`relationship_person_id` 仍只用于关系快照，不能参与人格选择或身份推断。
- 独立诊断日志只记录 `persona_source`、`persona_status` 和配置布尔值，不记录人格 ID、姓名或正文。

## 0.4.1 - 2026-08-05

### Fixed

- LLM、STT 和 interaction 决策失败现在固定发送 `error` 后再发送 `reply.end(status=failed)`，避免 Unity 永久停留在 Thinking。
- 新增 Dashboard 认证下的脱敏诊断投影，不依赖“核”的动态发现即可读取阶段、错误类型、耗时和状态；不暴露正文、标识、路径或密钥。

## 0.4.0 - 2026-08-05

### Added

- 增加完整自声明的只读 `series.diagnostics@1.0` 提供方，可由“核”依据系列 ID、插件 ID 与官方仓库元数据自动发现，无需在“核”中登记；诊断关闭或写入失败时返回固定 `disabled/unavailable` 状态，不改变原有 JSONL 落盘、轮转和失败关闭行为。
- 「Quest 角色设置」Page 新增角色姓名、自称、自我描述及与用户关系定位；后端生成明确的 Quest 混合现实 persona system prompt，空配置不臆造姓名或经历。

### Changed

- interaction 使用独立且有界的决策 turn，不再因触碰默认取消正常文本、LLM 或 TTS；显式 `/interrupt` 语义保持不变。
- TTS 改为顺序句段合成和容量为 2 的有界生产者-消费者流水线，文本 delta 继续优先发送，取消后不再产生旧轮音频或结束事件。
- 插件独立诊断日志改为异步有界写队列，磁盘操作在线程中执行，terminate 在有界超时内 flush；写入故障仍不影响对话。
- `/health` 新增脱敏的独立诊断日志可用状态，便于不读取日志正文地验收远端写入降级情况。

### Security

- persona Page/API 不显示 Bridge、Provider 或 API Key；`relationship_person_id` 仅选择关系快照，不参与角色身份推断；诊断只记录 persona 是否配置的布尔状态。

## 0.3.1 - 2026-08-05

### Added

- 完成系列诊断统一契约接入，保留原有诊断日志与降级行为。

## 0.3.0 - 2026-08-05

### Added

- 新增默认关闭的插件独立诊断日志，写入插件专属数据目录，支持并发安全大小轮转和写入失败降级；生产组件不再把日志转发到 AstrBot 总日志，也不记录认证凭据、原始音频、回复正文或会话身份标识。

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
