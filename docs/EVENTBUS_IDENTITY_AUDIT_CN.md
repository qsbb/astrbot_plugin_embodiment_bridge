# EventBus 身份与平台事件审计

## 根因

旧版本直接继承 `AstrMessageEvent` 并在 Bridge 中构造通用事件。这样虽然能进入 EventBus，但绕过了 AstrBot 平台适配器的公开事件工厂：外部插件若按具体平台事件类型、原生 `raw_message` 形状或适配器方法过滤，就会忽略该事件；依赖 `MessageSession`/UMO 的记忆和后处理钩子也只能看到不完整的上下文。

AstrBot 4.27.1 的公开 `Platform.create_event(AstrBotMessage)` 会按当前平台返回具体的适配器事件类型，并由 `AstrMessageEvent` 使用 `platform_meta.id`、消息类型和会话 ID 构造标准 `MessageSession`/UMO。Bridge 现在通过 `Context.get_platform_inst()` 获取管理员已绑定的平台实例，构造标准 `AstrBotMessage` 后只调用这个公开工厂，再把事件提交到 `Context.get_event_queue()`。

## 身份边界

- `self_id`、发送者 `user_id`、`group_id`、会话 ID 和平台实例 ID 只来自已经通过序授权的服务端会话；Unity 不能覆盖它们。
- `event.role` 保持 AstrBot 默认的 `member`，并明确设置 `_api_key_allow_admin_role=false`；Quest 事件不会继承管理员权限。
- 事件额外上下文 `embodiment_bridge.identity_context` 只供受控的 Bridge/记忆适配器读取，标记 `trusted=true`，不改变 AstrBot 权限判断；旧键保留一个主版本周期。
- `raw_message` 是明确标记为 `embodiment_bridge` 的最小、稳定兼容元数据，不伪造平台网络凭据或未获知的昵称。通用插件应优先使用 `AstrMessageEvent` 的公开访问器；依赖真实平台私有字段的插件仍需自行提供兼容契约。

## 可验证行为

Bridge 的回归测试验证：平台实例必须暴露公开 `create_event`；缺失时安全降级且不入队；存在时返回由工厂创建的事件、标准绑定身份和非管理员角色，并保留文本回复捕获、SSE/TTS 既有链路。Bridge 不调用私有事件构造器，不猜测平台适配器接口，也不修改其他插件。
