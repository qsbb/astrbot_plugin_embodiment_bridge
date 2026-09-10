# 可选 HTTP / HTTPS 部署

## 方案

伴夏支持用户自行选择传输方式：

- 受控局域网可使用 `http://`，必须显式开启 `allow_private_http_pairing`；公网明文 HTTP 还需要服务端 `allow_remote_http_pairing` 和客户端分别显式开启。
- 不带 scheme 的地址默认按 `https://` 解释；明文必须显式写 `http://`，HTTPS 失败不会自动降级。
- HTTPS 可使用任意高端口，例如 `18443`，不要求占用 80/443，也不要求 Caddy。
- Bridge 内置 listener 使用 `pairing_listener_tls_enabled`、`pairing_listener_tls_cert_path` 和 `pairing_listener_tls_key_path`。三个字段必须配置正确，否则 TLS listener 不启动；若另有合法外部 HTTPS fallback，非 TLS listener 故障可保持 Bridge 服务 degraded。

示例：

```json
{
  "pairing_listener_enabled": true,
  "pairing_listener_host": "0.0.0.0",
  "pairing_listener_port": 18443,
  "pairing_listener_tls_enabled": true,
  "pairing_listener_tls_cert_path": "/data/certs/bridge.crt",
  "pairing_listener_tls_key_path": "/data/certs/bridge.key",
  "pairing_listener_public_url": "https://example.example:18443",
  "pairing_public_url": "https://example.example:18443"
}
```

路由器只需转发 TCP `18443` 到 Bridge listener。必须限制只暴露该端口；不要把 AstrBot 管理端口、NAS、SSH、SMB 或 Docker socket 暴露到公网。

## 配对

Quest 不依赖扫码。正式主流程是：

1. 管理页生成一次性 6 位码；
2. Quest/手机输入服务器地址和端口；
3. 输入 6 位码；
4. 配对成功后自动保存并连接。

二维码仅是未来可选快捷入口，不包含长期 API key。

## 自签证书注意事项

自签 TLS 解决加密，不解决首次服务器身份；QR 解决近场配对，不等于 TLS 信任。若浏览器没有预信任 CA、受管根证书、独立可信验证器或人工指纹核验，主动局域网 MITM 无法安全排除。

伴夏的 `certificate_pin_sha256` 是完整叶子证书 DER 的 SHA-256 十六进制摘要。设置后只允许匹配该证书，且 pin 绑定精确 `scheme + host + 有效端口`；pin 不匹配、证书名称/CA/TLS 握手失败时不会切换到其他 authority，也不会切换到 HTTP。

用 OpenSSL 复算叶子证书指纹时必须对 DER 计算，而不是对 PEM 文本或整条链计算：

```bash
openssl x509 -in bridge.crt -outform DER | sha256sum
# macOS 没有 sha256sum 时：
openssl x509 -in bridge.crt -outform DER | shasum -a 256
```

输出的 64 位十六进制值直接填写，不要加 `sha256:` 前缀、冒号或空白。证书续期会改变叶子指纹，必须在服务端和伴夏端同时更新；pin-only 不能单独替代域名/CA 校验。使用公共 CA 且不配置 pin 时，伴夏仍执行平台正常的证书链和主机名校验。

## 配置失败与 fallback

- TLS 已启用但证书/私钥缺失、不可读、不是 PEM 或 pin 与叶子证书不一致：内置 TLS listener fail-closed，不会以 HTTP 复用同一端口。
- 若存在合法的外部 HTTPS `pairing_exchange_proxy_url`，非 TLS listener 故障可让已认证 Bridge 服务保持 degraded，并继续使用该 HTTPS fallback。
- active TLS 配置故障不会回退到外部未知 authority；修复证书路径、公开 URL 和 pin 后重载插件。
- 上游固定为 loopback HTTP；公网只转发 TCP listener 端口，不要把 AstrBot Dashboard、SSH、SMB、Docker socket 或管理端口暴露出去。

## HTTP 风险

HTTP 会明文传输 API key、Bridge key、文本和音频。只在本人可控的私网或明确接受风险的公网环境使用；公网 HTTP 还必须同时打开服务端和客户端的 remote-HTTP opt-in。
