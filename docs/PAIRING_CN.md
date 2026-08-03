# Quest 快速绑定

本功能用于把 Quest 客户端安全地绑定到当前 AstrBot Bridge。长期 `astrbot_api_key` 和 `bridge_api_key` 不会直接写进二维码；二维码只包含短期、单次使用的兑换 token。

## 后端准备

1. 在 `astrbot_plugin_quest_avatar_bridge` 插件配置中设置至少 32 字符的随机 `bridge_api_key`。
2. 选择聊天模型 Provider；如需受保护的关系上下文，同时设置 `trusted_client_id` 和 `trusted_platform_id`。
3. 为 AstrBot 部署一个 Quest 能直接访问、证书可信的 HTTPS 地址。公网 IP 也必须有 Quest 信任且与 IP 匹配的 TLS 证书；不能只暴露明文 `http://IP:端口`。
4. 在 AstrBot 已安装插件页面打开本插件的「Quest 快速绑定」Page。

## Page 中填写的位置

- `公开 HTTPS 地址`：例如 `https://bot.example.com`，不要填写 Dashboard 页面路径。
- `端口`：反向代理使用 443 时可留空；自定义 HTTPS 端口可填 `7443`。
- `AstrBot API Key`：具有本插件 `plugin` scope 的 API Key。它只进入本次内存配对会话，成功生成后页面输入框会清空。
- `客户端 ID`：必须与服务端 `trusted_client_id` 一致；Page 会优先使用服务端配置值。
- `用户 ID`、`机器人 ID`：Bridge 会话必填身份。
- `群组 ID`、`关系档案 ID`：按实际会话选填。
- `有效期`：60–300 秒，默认 120 秒。

点击「生成配对」后，Page 显示二维码、6 位短码、兑换倒计时和 Quest 兑换状态。关闭或重载插件会使未完成的配对立即失效。

## Quest 中的操作

当前可工作的流程：

1. 按左手柄菜单键，打开面前的 World Space 菜单。
2. 点击 `PAIR BACKEND`。
3. 点击 `SET HTTPS SERVER`，输入 Page 中的公开地址和端口。可输入：
   - `bot.example.com:7443`
   - `https://bot.example.com:7443`
   - 完整插件 Base URL
4. 用面板数字键输入 Page 上的 6 位短码。
5. 点击 `CONNECT`。Quest 成功兑换后会原子写入 `quest_avatar_bridge.json`，并立即重载配置、建立 AstrBot 会话，不需要重启应用。

配对服务器地址会保存在 Quest `PlayerPrefs` 中，下一次只需输入新的 6 位短码。

## 二维码扫描现状

Page 已生成正式的一次性配对二维码，Quest 客户端也已提供 `IPairingCodeScanner` 可替换接口和 `SCAN QR` 按钮。当前项目使用 `com.unity.xr.meta-openxr@1.0.2`；该版本只能控制 Passthrough 合成层，不能向应用提供透视相机像素，因此本版本的 `SCAN QR` 会明确提示使用 6 位短码。

后续升级到支持 Meta Passthrough Camera API 的 SDK 后，只需实现扫描 provider，把二维码 JSON 交给 `BackendPairingController.PairWithQrPayload()`；后端协议、Page 和配置写入无需修改。

## 一次性配对协议

二维码 JSON 只包含：

```json
{
  "type": "astrbot.quest.pair",
  "version": "1.0",
  "exchange_url": "https://bot.example.com/api/v1/plugins/extensions/astrbot_plugin_quest_avatar_bridge/pairing/exchange",
  "token": "一次性高熵 token"
}
```

配对 API：

| 方法 | 路径 | 认证 |
|---|---|---|
| GET | `/pairing/overview` | AstrBot Dashboard/Page |
| POST | `/pairing/create` | AstrBot Dashboard/Page |
| POST | `/pairing/status` | AstrBot Dashboard/Page |
| POST | `/pairing/revoke` | AstrBot Dashboard/Page |
| POST | `/pairing/exchange` | 一次性 token 或 6 位短码 |

安全属性：

- token 使用至少 32 个随机字节生成，服务端只保留 SHA-256 摘要。
- token/短码成功兑换一次后立即失效；并发请求只有一个能成功。
- 过期、撤销、插件重载都会清除内存中的长期密钥副本。
- 兑换按远端地址限速，错误 token、过期和重放统一失败关闭。
- 配对响应带 `Cache-Control: no-store`，插件不会记录 token 或长期密钥。
