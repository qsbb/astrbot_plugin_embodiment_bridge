# AstrBot 人格继承审计

审计目标版本：AstrBot 4.27.1。Bridge 只使用官方开发文档与 `PersonaManager` 的公开方法，不读取 `cmd_config.json`、数据库对象、Provider 配置或其他用户会话。

## 可用公开能力

插件通过 `context.persona_manager` 使用三个异步方法：

- `await get_persona(persona_id)`：精确读取管理员已选择的人格；不存在时抛出 `ValueError`。
- `await get_all_personas()`：为 Dashboard 管理 Page 生成安全 ID 列表。Bridge 丢弃 `system_prompt`、`begin_dialogs`、`tools`、`skills`、`custom_error_message` 和数据库字段。
- `await get_default_persona_v3(None)`：没有显式 Quest 人格时读取 AstrBot 明确默认人格。参数固定为 `None`，不会制造 Quest 的消息平台 UMO。

AstrBot 的 `conversation_manager` 虽可读取 Conversation 的 `persona_id`，但 Quest HTTP session 当前没有合法的 AstrBot Conversation ID 或统一消息来源映射。Bridge 因此不枚举其他用户对话、不根据 Unity 的 user/bot/client 字段拼接 UMO，也不接受 Unity 提交 persona ID。管理员保存的 `astrbot_persona_id` 是当前唯一受信任的 Quest 会话人格选择。

## 运行时选择顺序

1. `active_quest_persona_id` 指向一个完整、校验通过的独立人格文件：只在“临”的 Bridge 轮次使用该具身人格。
2. 没有启用独立人格，且 `persona_source_mode=manual_override`：使用“临”原有四个手动兼容字段。
3. 没有启用独立人格，且 `persona_source_mode=astrbot`、`astrbot_persona_id` 非空：精确读取该 AstrBot 人格。
4. 没有启用独立人格，且 AstrBot 人格 ID 为空：调用 `get_default_persona_v3(None)`。
5. 已启用文件缺失、损坏或来源哈希不符：清空运行时独立人格并失败关闭，不自动换到另一个独立人格。AstrBot 实时人格读取失败时仍使用无姓名、无虚构经历的通用 Quest MR 身份。

显式人格失效时不会自动改用默认人格或列表中第一个人格。默认读取只采用官方 `get_default_persona_v3(None)` 的结果，不使用可能在缺失配置时选中列表第一项的内部缓存字段。

## 导入与转换

管理员 Page 可选择 AstrBot 已有人格 ID或明确默认人格，由后端调用公开 PersonaManager 读取原文；浏览器不能提交任意来源正文冒充 AstrBot 人格。转换使用单独保存的 `persona_converter_provider_id`，只从 `Context.get_all_providers()` 的 Chat Completion 安全投影中选择，不读取 Provider 原始配置、API Key、Base URL 或请求头。

转换器要求严格单一 JSON 响应，把 QQ、群聊、打字和消息长度等渠道规则转换为自然面对面表达，同时保留来源明确给出的身份、性格、偏好和关系边界。角色主观上是与用户处于同一世界、同一空间的人；不得自称 AI、机器人、投影、虚拟角色、Quest、Unity 或应用。视觉、听觉、空间、距离、姿态、环境和触碰只有在运行系统本轮明确提供时才可当作真实感知。

转换成功先生成一个 30 分钟、最多 32 项、仅内存的一次性草稿 token；预览不会创建文件，也不返回来源正文。管理员确认并保存后，服务端才在 `StarTools.get_data_dir()/personas/qp_<32位随机十六进制>.json` 原子创建 ready 文件。每个文件包含严格 schema、来源快照与 SHA-256、转换版本和报告；列表只投影摘要，完整内容必须显式打开单个人格。

转换模型后端等待上限为 120 秒。Operator Page 对人格转换单独等待 135 秒，并每秒显示真实已用时间；普通管理请求仍保持 10 秒超时。页面显示的是当前阶段和时间，不伪造百分比。转换完成只表示已收到并校验预览，仍须依次点击保存和启用。

保存和启用是两个动作。启用只写 `active_quest_persona_id`，验证成功后才更新运行时；配置保存失败保持旧人格。保存“实时人格来源”会在同一次配置提交中清空该 ID。QQ 和其他平台事件没有服务端 `quest_avatar_bridge=true` 标记，因此不会收到独立人格覆盖。

## 安全边界

每个 turn 在 LLM 调用前异步读取一次人格，得到该轮不可变快照。人格正文只作为身份、性格和表达风格数据包嵌入 system prompt；Bridge 在其前后重复声明不可覆盖的 Protocol 1.0 JSON schema、认证授权、安全边界、动作枚举与模型无关约束。知识、环境、关系和用户输入遵循同一不可信指令边界。

独立人格正文和转换来源都以 JSON 数据封套传给模型，并转义 `<`、`>` 与 `&`，不能通过伪造闭合标签逃出数据区。文件 ID 只能由服务端生成且严格匹配 `qp_[0-9a-f]{32}`；文件读写拒绝路径片段与符号链接，使用临时文件、`fsync` 和原子替换，支持的平台上权限收敛为 `0600`。

Operator Page 只返回 `source_mode`、`source`、`status`、`persona_selected`、`astrbot_persona_id`、`name_configured`、安全 ID 列表和手动兼容字段。它不返回 AstrBot 人格正文。独立诊断日志记录 `persona.convert.*`、`persona.save.*`、`persona.activate.*` 和 `persona.overlay.*` 的脱敏阶段、状态、错误码与耗时；不写人格 ID、姓名、正文、来源快照、草稿令牌或 Provider ID。

`relationship_person_id` 始终只传给“情”的关系快照适配器；它不参与人格目录、人格选择或 system prompt 身份推断。

## 当前限制

Bridge 当前配置的 `chat_provider_id` 若显示 `selected_missing`，人格导入与文件管理仍可完成，但真实“你是谁”对话会在普通对话模型查找阶段失败。人格转换模型与普通对话模型独立选择；任一 Provider 被删除后都不会自动改选。转换模型缺失只禁用再次转换，不影响已经保存并启用的人格。
