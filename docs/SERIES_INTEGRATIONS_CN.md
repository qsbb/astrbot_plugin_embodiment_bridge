# 凝心溯溪系列后端复用

Quest Unity 客户端只连接 `astrbot_plugin_quest_avatar_bridge` 的 HTTP/SSE 接口；它不直接调用“知、言、序、情、境、声、核”或“枢”。所有复用发生在 Bridge 后端，并且只消费提供方显式声明的版本化契约。

## 当前接入矩阵

| 模块 | 契约 | Bridge 调用 | 频率 | 失败行为 |
|---|---|---|---|---|
| 知 | `active_learner.knowledge@1.0` / `recall` | 仅 `scope="global"` | 普通文本/语音轮 | 不注入知识证据 |
| 序 | `identity.control_plane@1.0` / `upsert_quest_owner_binding`；`identity.quest_binding_control@1.0` / `upsert_quest_binding`；`identity.quest_session_authorization@1.0` / `authorize_quest_session` | 明确身份设置可同步主人；自然人映射只保存 Quest 只读绑定且不新增 owner；会话时使用原始平台身份授权 | 保存/每次会话 | 已安装但拒绝时失败关闭，不与本地配置合并放行 |
| 情 | `relationship.snapshot@1.0` / `get_relationship_snapshot` | 只读派生快照 | 序授权成功后的每轮 | 使用中性关系，不阻断对话 |
| 境 | `environment.opportunity@1.0` / `get_cached_opportunity` | 仅 `cached_read`，请求链不联网 | 每轮 | 不注入环境事实 |
| 声 | `voice.audio_output@1.0` / `render_pcm_wav` | 读取 provider 管理的 PCM16 WAV，下混/重采样到 24 kHz 单声道 | 每次有文字回复且需要音频 | 保留文字；可使用已显式启用的 AstrBot Core TTS fallback |
| 核 | `update_manager.series_runtime@1.0` / `get_series_runtime_snapshot` | 只读运行态快照 | 启动/显式健康检查 | 跳过诊断，不阻断对话 |
| 言 | 不接入 | `conversation.proactive_delivery` 会生成或发送主动消息，与 Bridge 的 turn cancellation/SSE 交付冲突 | 不调用 | Bridge 独立完成当前 Quest 轮次 |
| 枢 | 不接入 | 提供方尚未向 ServiceRegistry 注册上述服务，不能猜 service 名 | 不调用 | 继续按明确插件 ID 与契约声明直连 |

“知”的 `private:<user_id>` 没有平台维度，可能发生跨平台同号碰撞，因此当前禁止使用。“境”的实时私有方法和“声”的 `voice.delivery@1.0` 也不消费：前者没有稳定跨插件契约，后者带 AstrBot 消息投递副作用。

## 受保护上下文授权

关系快照默认关闭。可直接在“临”的「Quest 角色设置」Page 集中配置：

- `trusted_client_id`：固定 Quest 客户端标识；`session.start.client_id` 必须精确匹配。
- `trusted_platform_id`：原始 AstrBot 平台 ID，例如 `aiocqhttp`。

`api_principal` 来自 AstrBot 已认证请求的 `request.username`，不是 Unity JSON。`platform_id` 和授权使用的 `client_id` 来自 Bridge 配置。`bot_id`、`user_id` 保持原始权限身份，禁止用“情”的自然人映射替换。

“序”已安装时，“临”通过 `identity.control_plane@1.0` 提交 `sha256:<64hex>` principal 摘要以及 client/platform/bot/user 五项结构化字段；“序”一次原子保存主人和 Quest 精确白名单，永不保存或返回原始 API principal。未安装“序”时，“临”只对自身配置的 API principal、client、platform、bot、user 和私聊范围全部精确匹配后开放受保护上下文。只要检测到“序”提供方，任何契约不兼容、拒绝、超时或错误都不会回退本地授权。

从“情”选择自然人时使用另一条 `identity.quest_binding_control@1.0`：它只更新 `quest_session_read_only_bindings`，绝不写入 `owner_users`。命中后只开放 Quest 私聊只读上下文，`owner_confirmed=false` 且 `grants_platform_action=false`。

Bridge 向“序”提交的对象恰好包含：

```json
{
  "api_principal": "astrbot-api",
  "client_id": "quest-living-room",
  "platform_id": "aiocqhttp",
  "bot_id": "bot-test",
  "user_id": "user-test",
  "group_id": null
}
```

只有契约名、major、capability、method 和返回 schema 全部兼容，并且返回 `authorized/read_only_context/owner_confirmed` 时，当前 session 才能读取关系快照。超时、缺插件、配置缺失、群聊、畸形对象或任何非 authorized 结果都失败关闭。

旧协议中的 `relationship_profile_id` 为兼容保留，但 Bridge 不把客户端声明值传给受保护快照；“情”使用服务端默认 profile，防止客户端越权选择其他关系档案。

## 音频所有权

`voice.audio_output@1.0` 的成功结果必须声明：

```json
{
  "contract_name": "voice.audio_output",
  "contract_version": "1.0",
  "capability": "render_pcm_wav",
  "status": "ok",
  "error_code": "",
  "path": "absolute-provider-managed-path.wav",
  "container": "wav",
  "encoding": "pcm_s16le",
  "sample_width": 2,
  "ownership": "provider_managed",
  "consumer_may_delete": false
}
```

Bridge 会再次校验响应字段、绝对路径和实际 WAV 内容，然后才下混/重采样。Bridge 不删除、不移动 provider 文件。取消会原样传播，旧 turn 的迟到音频仍由 generation token 丢弃。

## 提示词隔离

公共知识和缓存环境以 JSON 数据字段传给角色决策模型。系统提示明确把它们视为不可信事实证据：其中任何要求修改权限、安全边界、动作白名单、人格或输出格式的文本都必须忽略。
