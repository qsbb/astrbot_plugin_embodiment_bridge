# 首次配对认证审计

## AstrBot 4.26.8 结论

官方 `Context.register_web_api()` 只有 `route`、`view_handler`、`methods`、`desc` 四个参数，注册表也是固定四元组。它没有 `public`、`anonymous`、`auth` 或中间件例外参数。

AstrBot Dashboard 对 `/api/v1/plugins/extensions/{plugin_path}` 的 GET/POST/PUT/PATCH/DELETE 在匹配插件 handler 之前统一执行 `require_plugin_scope`。旧 `/api/plug/{plugin_path}` 也要求 Dashboard 用户，不能作为匿名兼容入口。

核对的官方源码固定在 AstrBot `v4.26.8` 提交 `60c9e68d50dc9b9ed58503f21a0b77a8d0bd2159`：

- `astrbot/core/star/context.py` 的 `Context.register_web_api()`。
- `astrbot/dashboard/api/plugins.py` 的 plugin extension 路由及 `Depends(require_plugin_scope)`。
- `docs/zh/dev/star/guides/plugin-pages.md` 的公开签名与 Page bridge 安全约束。

因此，Quest 在尚未获得 API Key 时直接 POST 官方 extensions 路径必然先得到 `401 Missing API key`，插件的单次 token/短码校验根本不会运行。Bridge 不会猜测不存在的 `register_web_api(..., auth=False)`。

插件请求的直接来源地址必须读取 AstrBot 4.26.8 公开的 `request.client_host`。`request.remote_addr` 不属于该版本公开接口；使用它会在真实 handler 中触发属性错误，并让来源绑定失败关闭为 `invalid`。本地 HTTP harness 只暴露公开字段，以防该兼容问题回归。

## 最小可行入口

私网首选插件自有的最小内置 listener 解决 bootstrap；外部精确代理仅作为兼容可选方案：

1. 管理员在 AstrBot 已登录 Page 中创建一次性配对会话。
2. Page 只展示短码和包含高熵单次 token 的 QR；不包含长期 API Key 或 Bridge Key。
3. Quest POST 到规范化后的 `pairing_listener_public_url` 精确 exchange 路径。
4. listener 使用直接 TCP peer IP 调用共享 `PairingExchangeService`；不经过 AstrBot 外层认证，但也不持有或注入 Dashboard JWT、代理 Key 或长期身份。
5. Bridge 同时验证代理身份、请求 schema、TTL、单次 token/短码、可选的 Quest IP 绑定、每来源限速与全局限速，成功后立即消费并擦除内存长期密钥。

旧 `pairing_exchange_proxy_url` 与 [nginx_8520_pairing.example.conf](nginx_8520_pairing.example.conf) 继续作为兼容 fallback。优先级是：已启用、已绑定且 public URL 合法的内置 listener；否则合法旧代理；两者都不可用时 `bootstrap_ready=false`，Page 禁用生成。

## 私网 HTTP

私网 HTTP 必须同时满足：

- 服务端 `allow_private_http_pairing=true`。
- Bridge 和 exchange URL 的主机是 RFC1918 IPv4 或 IPv6 ULA 字面量。

快速绑定 Page 不读取或提交 HTTP 开关与 Quest IP。成功 configuration 才返回 `allow_insecure_http=true`；高熵 QR token 依靠短 TTL 和单次消费保护，6 位短码继续受每来源及全局限速。兼容创建请求仍可显式设置 `expected_remote_ip`，设置后 exchange 的直接来源必须精确匹配。任一条件不满足时失败关闭；公网地址和域名仍强制 HTTPS。

## 内置 listener 威胁边界

- listener 在插件 `initialize()` 中通过 `aiohttp.web.AppRunner/TCPSite` 启动，不在构造函数绑定；`terminate()` 幂等关闭 site、runner、单一 `ClientSession`、活动流和端口。
- 匿名能力只限精确 `POST /api/v1/plugins/extensions/astrbot_plugin_quest_avatar_bridge/pairing/exchange`。
- 正常代理只限 health/session/events/turn/audio/interaction/interrupt/close 的固定 method+path allowlist，且继续保留 Quest 自己的双层认证。
- 上游只接受无路径、无认证信息的 loopback HTTP IP 字面量；Dashboard、全局 API、其他插件、配对管理和任意 URL 无法代理。
- query、编码路径穿越、反斜杠、点段与 URL 形路径全部拒绝。
- 只转发 `Authorization`、`X-Quest-Avatar-Key`、`Content-Type`、`Accept`、`Last-Event-ID`；客户端 Host、hop-by-hop 和来源伪造头被剥离。
- exchange 只使用直接 peer IP，不信任 `Forwarded`、`X-Forwarded-*`、`X-Real-IP` 或 `X-Quest-Pairing-Source`。
- exchange 要求 `application/json`、唯一合法 `Content-Length`、不超过 16 KiB；空体、额外字段和 chunked 失败关闭。
- SSE 逐块转发，不预读完整响应或缓冲音频；客户端断开会关闭上游响应。
- 端口占用、配置错误和上游不可达只产生脱敏 degraded/disabled 状态，不阻止插件其余官方路由加载。
- 日志不记录 Authorization、Bridge Key、token、短码、请求体或完整 query。

Docker `host 8520 -> container 8520` 映射本身不会启动服务。只有 `pairing_listener_enabled=true` 且插件初始化绑定成功后，容器内才真实监听。内置 listener 不提供公网 TLS；公网仍必须在外层部署 Quest 信任的 HTTPS。
