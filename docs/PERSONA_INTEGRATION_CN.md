# AstrBot 人格继承审计

审计目标版本：AstrBot 4.27.1。Bridge 只使用官方开发文档与 `PersonaManager` 的公开方法，不读取 `cmd_config.json`、数据库对象、Provider 配置或其他用户会话。

## 可用公开能力

插件通过 `context.persona_manager` 使用三个异步方法：

- `await get_persona(persona_id)`：精确读取管理员已选择的人格；不存在时抛出 `ValueError`。
- `await get_all_personas()`：为 Dashboard 管理 Page 生成安全 ID 列表。Bridge 丢弃 `system_prompt`、`begin_dialogs`、`tools`、`skills`、`custom_error_message` 和数据库字段。
- `await get_default_persona_v3(None)`：没有显式 Quest 人格时读取 AstrBot 明确默认人格。参数固定为 `None`，不会制造 Quest 的消息平台 UMO。

AstrBot 的 `conversation_manager` 虽可读取 Conversation 的 `persona_id`，但 Quest HTTP session 当前没有合法的 AstrBot Conversation ID 或统一消息来源映射。Bridge 因此不枚举其他用户对话、不根据 Unity 的 user/bot/client 字段拼接 UMO，也不接受 Unity 提交 persona ID。管理员保存的 `astrbot_persona_id` 是当前唯一受信任的 Quest 会话人格选择。

## 选择顺序

1. `persona_source_mode=manual_override`：显式兼容覆盖，仅使用“临”原有四个手动身份字段和 `persona_prompt`。
2. `persona_source_mode=astrbot` 且 `astrbot_persona_id` 非空：精确读取该人格。
3. `persona_source_mode=astrbot` 且 ID 为空：调用 `get_default_persona_v3(None)`。
4. 精确人格被删除、为空、响应畸形、接口异常或 1 秒超时：使用无姓名、无虚构经历的通用 Quest MR 身份。

显式人格失效时不会自动改用默认人格或列表中第一个人格。默认读取只采用官方 `get_default_persona_v3(None)` 的结果，不使用可能在缺失配置时选中列表第一项的内部缓存字段。

## 安全边界

每个 turn 在 LLM 调用前异步读取一次人格，得到该轮不可变快照。人格正文只作为身份、性格和表达风格数据包嵌入 system prompt；Bridge 在其前后重复声明不可覆盖的 Protocol 1.0 JSON schema、认证授权、安全边界、动作枚举与模型无关约束。知识、环境、关系和用户输入遵循同一不可信指令边界。

Operator Page 只返回 `source_mode`、`source`、`status`、`persona_selected`、`astrbot_persona_id`、`name_configured`、安全 ID 列表和手动兼容字段。它不返回 AstrBot 人格正文。独立诊断日志只允许固定枚举的 `persona_source`、`persona_status` 和布尔状态，不写人格 ID、姓名或正文。

`relationship_person_id` 始终只传给“情”的关系快照适配器；它不参与人格目录、人格选择或 system prompt 身份推断。

## 当前限制

Bridge 当前配置的 `chat_provider_id` 若显示 `selected_missing`，人格继承仍可完成并在 Page 中验收，但真实“你是谁”对话会在 Provider 查找阶段失败。管理员必须在「Quest 角色设置」Page 手动选择一个现存 Chat Completion Provider；Bridge 不会自动改选或修改全局 Provider。
