# astrbot_plugin_embodiment_bridge 项目记忆（AI 协作规范）

## 硬约束（最高优先级，任何任务前重读）

1. **只改凝心溯溪系列仓库**：本插件（临桥）与 banxia（Quest/手机客户端，
   `D:\banxia_build` 构建工程对应仓库）。**绝不**直接改 AstrBot 核心、
   其他插件（含 menglimi 系列第三方插件）、远端容器。交付方式：提交推送
   GitHub → 用户手动更新远端插件。
2. 第三方插件（如 astrbot_plugin_reality_companion）**只读参考，零改动**。
3. 新端点/新消息类型必须保持向后兼容（客户端旧版本仍在跑）；
   配置 schema 变更要带迁移。

## 双端同步原则（用户钦定，2026-08）

> **无论开发什么功能，只要不是设备生态独占的，VR 端和手机端总是同步。**

- banxia 客户端将有两个形态：Quest（VR）与 Android 手机端（移植方案见
  banxia 仓库 `PHONE_PORT_PLAN_CN.md`）。服务端（本插件）对所有客户端
  端点**一视同仁**——不得出现"仅手机端可用的对话能力"或"仅 VR 端可用的
  下行事件"；端点协议层不做平台分支
- 设备生态独占能力（如手机随身摄像头单帧）在协议上体现为**可选字段/
  可选端点**，不是平台特化协议

## 关键事实速查

- 协议：`/pairing/exchange`（一次性码/QR → 持久双 API 钥）、
  `/session/start`、`/turn/start`、`/audio/chunk|end`、`/playback/receipt`、
  `/spatial/context`、SSE 事件流 `/events/`；鉴权 Authorization: ApiKey +
  X-Embodiment-Bridge-Key 双头
- `/spatial/context` 是上下文注入模板：StrictModel 校验 → session 内存态 →
  `<embodiment_spatial_context_json>` overlay 注入对话上下文
- 多模态规划（摄像头单帧，用户决策唯一采纳的 reality_companion 功能）：
  `turn/start` 扩展 image 字段 → 视觉模型；失败回执复刻
  `must_not_claim_observed` 治理设计——角色不得编造画面
- 测试基线：`pytest -q` 543 通过 / 27 项既有失败（改动后必须回归）
- 模型端：用户 AstrBot 跑于远端容器（UTC-8 时区）

## 协作习惯

- 根因分析 + 具体修复；审计结论本地复算验证后再采信
- 每里程碑独立 commit + push
- 服务端改动先跑测试再推送
