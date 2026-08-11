# 具身客户端快速绑定

本功能用于把具身客户端安全地绑定到当前 AstrBot Bridge。长期 `astrbot_api_key` 和 `bridge_api_key` 不会直接写进二维码；二维码只包含短期、单次使用的兑换 token。

## 后端准备

1. 在 `astrbot_plugin_embodiment_bridge` 插件配置中设置至少 32 字符的随机 `bridge_api_key`。
2. 在本插件配置中设置 `pairing_public_url` 和具有 `plugin` scope 的具身客户端专用 `pairing_astrbot_api_key`。Bot/User 在具身服务控制台明确填写，或通过“情”的自然人绑定解析；它们只保存在插件数据目录，不进入 AstrBot 配置 Page。客户端 ID 使用服务端 `trusted_client_id`；旧版空值仍兼容采用 `quest-living-room`。
3. 选择聊天模型 Provider；如需受保护的关系上下文，由 AstrBot 管理员在服务端同时设置固定的 `trusted_client_id` 和真实原始 `trusted_platform_id`。配对页和 Unity 都不能替代这项可信配置。
4. 私网优先启用内置 listener：配置 `pairing_listener_enabled`、监听 IP/端口、固定 loopback 上游和 `pairing_listener_public_url`。它直接复用同一个配对状态机，不要求新客户端预先持有 AstrBot API Key。
5. 旧 `pairing_exchange_proxy_url` 仍可作为外部 Nginx 兼容 fallback；它不再是私网唯一入口。AstrBot 的 `register_web_api` 本身仍不支持匿名例外，详情见 [PAIRING_BOOTSTRAP_AUDIT_CN.md](PAIRING_BOOTSTRAP_AUDIT_CN.md)。
6. 公网必须部署客户端信任的 HTTPS。受控私网可显式启用 `allow_private_http_pairing`，但只接受私网 IP 字面量；内置 listener 不终止公网 TLS。
7. 在 AstrBot 已安装插件页面打开本插件的“具身客户端快速绑定”Page。

快速绑定 Page 不再承担角色或连接设置。它不会读取、显示或让操作者填写客户端 IP、AstrBot API Key、平台身份、客户端 ID、用户/机器人/群组 ID、关系档案 ID 或有效期。

关系自然人由“临”的具身服务控制台管理。服务端会通过“情”解析唯一活跃私聊账号，再通过“序”的既有只读绑定契约授权；该过程不新增主人，也不把真实 Bot/User/UMO 返回 Page 或客户端。

## Page 操作

Bridge 就绪后，Page 只显示“生成绑定二维码”。点击后显示二维码、6 位短码、倒计时和客户端兑换状态。配对默认 120 秒有效；关闭或重载插件会使未完成的配对立即失效。

复制短码或撤销配对期间，对应按钮会暂时禁用，并在请求结束后恢复。如果服务端返回当前 Page 尚不认识的配对状态，Page 会停止复制、撤销和轮询，提示重新生成配对码，避免把未知状态误判为仍可使用。

## 伴夏（Quest）中的操作

当前可工作的流程：

1. 按左手柄菜单键，打开面前的 World Space 菜单。
2. 点击 `PAIR BACKEND`。
3. 首次使用短码时点击 `SET HTTPS SERVER`，输入管理员配置的 `pairing_public_url` 主机和端口。可输入：
   - `bot.example.com:7443`
   - `https://bot.example.com:7443`
   - 完整插件 Base URL
4. 用面板数字键输入 Page 上的 6 位短码。
5. 点击 `CONNECT`。伴夏成功兑换后会原子写入 `embodiment_bridge.json`，并立即重载配置、建立 AstrBot 会话，不需要重启应用。

配对服务器地址会保存在伴夏 `PlayerPrefs` 中，下一次只需输入新的 6 位短码。

兑换配置中的 Bot/User 是固定的 `server-managed-*` 占位值。客户端可以继续保存并回传这些值，服务端会在每次 `session/start` 使用已经验证并复核的规范身份覆盖；设备端无法选择或推断真实平台账号。

## 二维码扫描现状

Page 已生成正式的一次性配对二维码，伴夏也已提供 `IPairingCodeScanner` 可替换接口和 `SCAN QR` 按钮。当前项目使用 `com.unity.xr.meta-openxr@1.0.2`；该版本只能控制 Passthrough 合成层，不能向应用提供透视相机像素，因此本版本的 `SCAN QR` 会明确提示使用 6 位短码。

后续升级到支持 Meta Passthrough Camera API 的 SDK 后，只需实现扫描 provider，把二维码 JSON 交给 `BackendPairingController.PairWithQrPayload()`；后端协议、Page 和配置写入无需修改。

## 一次性配对协议

二维码 JSON 只包含：

```json
{
  "type": "astrbot.quest.pair",
  "version": "1.0",
  "exchange_url": "https://bot.example.com/api/v1/plugins/extensions/astrbot_plugin_embodiment_bridge/pairing/exchange",
  "token": "一次性高熵 token"
}
```

同名 `pairing/exchange` 有两条传输路径：AstrBot 官方注册路由仍受 plugin-scope 外层认证；内置 8520 listener 的精确 POST 路径位于该外层认证之外，只校验一次性 token/短码并按直接 peer IP 限速，且不注入任何长期身份。

| 方法 | 路径 | 认证 |
|---|---|---|
| GET | `/pairing/overview` | AstrBot Dashboard/Page |
| GET | `/pairing/operator-settings` | AstrBot Dashboard/Page |
| POST | `/pairing/operator-settings` | AstrBot Dashboard/Page |
| GET | `/pairing/identity-candidates` | AstrBot Dashboard/Page |
| POST | `/pairing/identity-selection` | AstrBot Dashboard/Page |
| POST | `/pairing/create` | AstrBot Dashboard/Page |
| POST | `/pairing/status` | AstrBot Dashboard/Page |
| POST | `/pairing/revoke` | AstrBot Dashboard/Page |
| POST | `/pairing/exchange` | 内置 listener：一次性 token/6 位短码；官方注册路由：plugin-scope 身份；外部代理：兼容方案 |

安全属性：

- token 使用至少 32 个随机字节生成，服务端只保留 SHA-256 摘要。
- token/短码成功兑换一次后立即失效；并发请求只有一个能成功。
- 过期、撤销、插件重载都会清除内存中的长期密钥副本。
- 兑换按远端地址限速，错误 token、过期和重放统一失败关闭。
- 快速绑定 Page 不预先收集客户端 IP；高熵 QR token 依靠单次使用和短有效期保护，6 位短码同时受每来源和全局限速。旧完整创建请求仍可显式提供 `expected_remote_ip` 以增加来源绑定。
- 限速同时包含每来源预算和全局预算，降低分布式枚举 6 位码的风险。
- 配对响应带 `Cache-Control: no-store`，插件不会记录 token 或长期密钥。

## 内置 8520 listener 的边界

Docker 发布 `8520:8520` 只建立端口映射，不会让容器内自动出现服务。只有 `pairing_listener_enabled=true` 且插件 `initialize()` 成功绑定后，容器内才监听。推荐私网最小配置见 [LOCAL_INTEGRATION_CN.md](LOCAL_INTEGRATION_CN.md)。

8520 只接受以下精确路径：

- 匿名：`POST /api/v1/plugins/extensions/astrbot_plugin_embodiment_bridge/pairing/exchange`。旧插件 ID 下的 exchange 不开放。
- 双层认证代理：`GET health`、`POST session/start`、`GET events/<session_id>`、`POST turn/start`、`POST audio/chunk`、`POST audio/end`、`POST interaction`、`POST interrupt`、`POST session/close`。

它明确拒绝 Dashboard、全局 API、其他插件、任意 URL、`pairing/create|status|revoke|overview`、operator settings 和 identity 管理接口。SSE 与音频块边读边写，不等待上游完整响应；客户端断线会关闭上游流。

匿名 exchange 还要求：

- `Content-Type: application/json`，且有唯一、合法的 `Content-Length`。
- 请求体不超过 16 KiB，空体、额外字段、非法版本和 chunked 请求失败关闭。
- 直接 TCP peer IP 用于限速；只有兼容请求显式设置 `expected_remote_ip` 时才额外要求完全一致。
- 默认忽略 `Forwarded`、`X-Forwarded-For`、`X-Real-IP` 与 `X-Quest-Pairing-Source`。
- 成功和错误响应都带 `Cache-Control: no-store`；错误 token、过期、撤销和重放使用统一失败语义。

配对完成后，客户端必须对 8520 上的正常接口继续发送它实际获得的 `Authorization: ApiKey ...` 和 `X-Embodiment-Bridge-Key`。listener 不添加、替换或修复认证头；旧 `X-Quest-Avatar-Key` 仅保留一个主版本周期。
