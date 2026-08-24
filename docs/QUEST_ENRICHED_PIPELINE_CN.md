# Quest 专用富化直管链 · 开发方案

> 状态：**已实现（P0–P3 完成，待部署验证）**
> 归属插件：astrbot_plugin_embodiment_bridge（凝心溯溪-临，系列插件，可修改）
> 关联分析：`/data/dsh/home/dsh/optimization_plan.md`（根因与 memory_companion 优化建议，由用户另行提 PR）

---

## 0. 实施状态（本次交付）

**P0 调研**：完成。确认了钩子超时熔断方案的全部可行性前置（func_tool=None、conversation=None、合成事件访问器、`context.llm_generate` 直管 API、回复提取）。

**P1 配置**：完成。`_conf_schema.json` 新增 6 键：`quest_chain_mode`（main/bridge/auto，默认 main）、`quest_chain_per_hook_budget_seconds`（默认 6.0）、`quest_chain_total_hook_budget_seconds`（默认 10.0）、`quest_chain_llm_timeout_seconds`（默认 30.0）、`quest_chain_memory_cache_ttl_seconds`（默认 30.0）、`quest_chain_excluded_plugins`（逗号分隔，默认空）。

**P2 核心模块**：完成。新增 `adapters/quest_enriched_pipeline.py`（`QuestEnrichedPipelineAdapter`）：复用 `_build_capture_event` 合成事件 → 构建 ProviderRequest → 逐钩子 `asyncio.wait_for` 超时熔断（慢钩子回滚 + 贡献缓存兜底）→ 直管 `context.llm_generate` → OnLLMResponseEvent 钩子 → 提取自然文本回复。

**P3 接线 + 页面**：完成。
- `main.py`：实例化适配器并注入 `TurnOrchestrator`；新增 `_quest_chain_mode()`/`_quest_chain_excluded_plugins()`。
- `core/turn_orchestrator.py`：`_decide_and_deliver` 增加 `quest_bridge` 分支；抽出 `_attempt_quest_bridge`/`_attempt_eventbus`/`_attempt_direct`/`_read_knowledge_env` 辅助方法；auto 模式失败回退主链路，再回退直管。
- `core/operator_settings.py`：`quest_chain_snapshot()` + `save_quest_chain_settings()`（含运行时热更新 orchestrator + 适配器），6 键加入持久化白名单。
- `transport/pairing.py`：`QuestChainSettingsRequest` 模型 + `pairing/quest-chain-settings` GET/POST 路由与 handler。
- `pages/operator/`：分段控件（主链路/临独立链路/自动回退）+ 条件显隐（选 main 隐藏 bridge 设置，选 bridge 隐藏 main 说明，auto 全显）+ 保存热更新。

**P4 测试**：完成。新增 `tests/test_quest_enriched_pipeline.py`（20 个用例：可用性判定、片段提取 append/prepend、回复提取、历史投影、贡献缓存/TTL、generate 端到端、慢钩子熔断不阻塞、钩子异常容错、空回复抛错、插件排除）。同步 `manifest.json`（+2 路由）与 `test_plugin_protocol.py`（路由数 57→59）。容器内全量 pytest：基线 28 失败（test_astrbot_pipeline/test_action_terminal_integration 的既有环境问题，与本次无关），本版 28 失败——**零新增回归**，新模块 20 用例全过。

**待办（用户侧）**：推送到 GitHub 后，由用户手动更新远端 AstrBot 的 **临** 插件，再跑 P4 远程回归（`/tmp/latency_test5.py` + `/tmp/extract_spans.py`）。

---

## 1. 背景与目标

### 1.1 问题
Quest 具身对话当前走「共享事件总线」路径，被「我会牢牢记住你」(memory_companion) 的 `on_llm_request` 钩子拖慢：正常 15.6s、撞后台 SenseNova 整理任务时挂 40.9s 直至 45s 超时熔断。AstrBot 的 `call_event_hook` 对钩子**串行 await、无超时**，单个慢钩子即可拖死整轮。

### 1.2 目标
在临侧实现 **Quest 双模链路**：同一入口下并存两条可切换的 Quest 专用链路。

- **模式一 · AstrBot 主链路（`main`）**：即现有共享事件总线路径，完整钩子链、无超时保护——作为对照基线保留。
- **模式二 · 临独立链路（`bridge`）**：新增，复刻主链路的钩子富化，但带钩子超时熔断 + 直管 LLM，隔离 QQ 洪峰。
- **自动模式（`auto`）**：默认先走临独立链路，其不可用/失败时自动回退主链路；主链路再不行且允许时回退直管 JSON。

设计要点：

1. **复刻 AstrBot 主链路**：临独立链路保留 OnLLMRequestEvent 钩子富化（记忆/知识/人设/关系/环境/情绪），**不绕过插件生态**；
2. **钩子超时熔断**：每个钩子独立 `asyncio.wait_for`，慢钩子（memory_companion）超时即熔断，用缓存兜底，绝不拖死整轮；
3. **直管 LLM 调用**：临独立链路不进共享事件总线，彻底隔离 QQ 洪峰与 `session_lock_manager`；
4. **双模可切换、可对比**：一个配置键切换链路；两路的耗时埋点对齐，便于实测对比哪种更优；
5. **页面可视化配置**：在临的 operator 页面切换模式，且**按模式条件显隐**——选 `main` 时隐藏 bridge 专属设置，选 `bridge` 时隐藏 main 专属说明（详见 4.9）；
6. **QQ 零影响**：QQ 仍走原共享事件总线完整链路；
7. **可回退**：默认 `main`（现状），`bridge`/`auto` 灰度验证后再启用。

### 1.3 非目标
- 不修改 AstrBot 核心、memory_companion、private_companion 或任何非系列插件；
- 不改变 QQ 链路任何行为；
- v1 不实现 LLM 主动调用记忆工具（`memory_companion_recall` 等 func_tool 多轮调用）——记忆经由 `on_llm_request` 注入已获得，工具调用留待 v2。

---

## 2. 现状架构（已核实）

### 2.1 现有两条决策路径（`core/turn_orchestrator.py::_decide_and_deliver`）

```
DecideEngine._decide_and_deliver(session, turn, user_text, interaction)
  │
  ├─ use_message_pipeline == True（当前线上：enable_astrbot_message_pipeline=true）
  │     └─ AstrBotMessagePipelineAdapter.generate()   [adapters/astrbot_pipeline.py]
  │           · _build_capture_event() 造合成事件（BRIDGE_EVENT_MARKER 等标记）
  │           · queue.put_nowait(event)  ← 进【共享事件总线】，与 QQ 同队列
  │           · 等 event.wait_completed()，AstrBot 内部跑完整钩子链+LLM
  │           · captured_text() 取自然文本回复
  │           ⚠️ 慢点：memory_companion 钩子串行无超时
  │
  └─ use_message_pipeline == False（直管兜底，当前 allow_direct_provider_fallback=False 关闭）
        └─ AstrBotLLMAdapter.generate()   [adapters/astrbot_llm.py]
              · 已能拉取 relationship(情)/knowledge(知)/environment(境)/persona/history
              · context.llm_generate(chat_provider_id=...)  ← 直调 DeepSeek
              · 输出【JSON 结构化】{should_reply, reply_text}，由 IntentParser 解析
              ⚠️ 缺失：memory_companion 记忆注入 + 其他插件钩子富化
```

### 2.2 关键事实（决定设计）

- 直管路径**已具备**系列插件上下文拉取能力（情/知/境/persona/history），缺的只是**钩子富化**与**记忆注入**。
- 直管路径当前产出 **JSON 结构化**回复；事件总线路径产出**自然文本**。本方案产出自然文本（对齐事件总线语义）。
- 钩子（`EventType.OnLLMRequestEvent`）是无超时协程，签名 `(event, req)`，可经 `star_handlers_registry.get_handlers_by_event_type(type, plugins_name=event.plugins_name)` 取出后自行包 `wait_for`。
- `ProviderRequest`（`astrbot/core/provider/entities.py`）核心字段：`prompt` / `system_prompt` / `contexts` / `func_tool` / `conversation` / `image_urls`。绝大多数钩子只读写 `req.system_prompt` 与 `req.contexts`。
- `_build_capture_event()` 已能造出带全部桥接标记的合法 `AstrMessageEvent`，可直接复用。

---

## 3. 设计方案（双模）

### 3.1 核心思路

在临侧用一个**链路选择器**统一调度两种 Quest 链路，二者共享同一个合成事件构造与回复提取逻辑：

- **主链路（`main`）**：复用现有 `AstrBotMessagePipelineAdapter`——`_build_capture_event` 造事件 → `queue.put_nowait` 入共享总线 → 等 `wait_completed()`。完整钩子链、无超时，作为对照基线原样保留。
- **临独立链路（`bridge`）**：新增 `QuestEnrichedPipelineAdapter`——复用 `_build_capture_event` 造事件但**不入共享队列**；改为手动构建 `ProviderRequest` → 按 AstrBot 相同优先级逐个调 OnLLMRequestEvent 钩子（每个包 `wait_for`）→ 直管调 chat_provider → 过响应/装饰钩子。本质是"把 AstrBot 的 ProcessStage 搬来临侧，并给每个环节装上保险丝"。

### 3.2 双模架构图

```
Quest 伴夏 turn.start
  │
  ▼
TurnOrchestrator._decide_and_deliver
  │  interaction is None 且已授权 → 进入双模分发
  ▼
QuestChainRouter（新增，按 quest_chain.mode 分发）
  │
  ├─ mode = "main"（AstrBot 主链路，现状）
  │     └─ AstrBotMessagePipelineAdapter.generate()
  │           _build_capture_event → queue.put_nowait(共享总线) → wait_completed
  │           完整钩子链，无超时保护（对照基线）
  │
  ├─ mode = "bridge"（临独立链路，新增）
  │     └─ QuestEnrichedPipelineAdapter.generate()
  │           ① _build_capture_event()      复用：合成事件 + 桥接标记
  │           ② _build_provider_request()   新增：prompt/contexts/system_prompt/conversation
  │           ③ _run_request_hooks()        新增：OnLLMRequestEvent 钩子，逐个 wait_for(预算)
  │                                           系列插件全保留；memory_companion 超时→熔断+缓存兜底
  │           ④ _call_llm()                 新增：直管 chat_provider.text_chat(包超时)
  │           ⑤ _run_response_hooks()       新增：OnLLMResponseEvent/装饰钩子(带超时)
  │           ⑥ 提取自然文本 → ModelDecision
  │
  └─ mode = "auto"（自动回退，推荐灰度）
        先试 bridge 链路 → 不可用/失败则回退 main 链路 → 再不行且允许则回退直管 JSON
  ▼
SSE reply.end → TTS → Quest
```

### 3.3 双模对比

| | 主链路 `main`（现） | **临独立链路 `bridge`（新）** | 直管 JSON（现，关闭） |
|---|---|---|---|
| 记忆/知识/人设富化 | ✅ 全部钩子 | ✅ 全部钩子（带超时） | ❌ 仅情/知/境适配器 |
| LLM 调用 | 共享总线 | **直管** | 直管 |
| 输出 | 自然文本 | 自然文本 | JSON 结构化 |
| 抗 QQ 洪峰 | ❌ 同队列争抢 | ✅ 独立直管 | ✅ |
| 抗慢钩子 | ❌ 无超时 | ✅ 逐钩子熔断 | n/a（不跑钩子） |
| 典型耗时 | 26~45s | 目标 <20s | 快但缺记忆 |
| QQ 影响 | — | 无 | 无 |

### 3.4 模式语义

| mode | 行为 | 适用 |
|---|---|---|
| `main` | 仅走 AstrBot 主链路（=现状） | 默认/对照基线/临独立链未验证前 |
| `bridge` | 仅走临独立链路，失败即报错（不回退，便于暴露问题） | 验证临独立链稳定性时 |
| `auto` | bridge 优先，失败回退 main，再不行回退直管 JSON | 灰度/长期运行 |

> 双模共用埋点口径（span/trace_id 一致），可用 `/tmp/extract_spans.py` 对同一问题分别跑两种模式，直接对比 `turn.processing` 与各钩子 wall_ms。

---

## 4. 模块设计（新增/改动）

### 4.1 新增 `adapters/quest_enriched_pipeline.py`

新类 `QuestEnrichedPipelineAdapter`，公共接口对齐 `AstrBotMessagePipelineAdapter.generate(...)`（返回 `ModelDecision`），便于 `turn_orchestrator` 无侵入接线。

核心方法：

```python
class QuestEnrichedPipelineAdapter:
    def __init__(self, context, logger, *, enabled, platform_id,
                 chat_provider_id, per_hook_budget_seconds,
                 total_hook_budget_seconds, llm_timeout_seconds,
                 memory_cache_ttl_seconds, excluded_plugins,
                 diagnostic_log=None): ...

    async def generate(self, *, session, user_text, fast_action_active=False,
                       fast_action_feedback=None, action_facts=None) -> ModelDecision: ...

    # 内部
    async def _build_provider_request(self, event, session, user_text) -> ProviderRequest: ...
    async def _run_request_hooks(self, event, req, trace) -> None: ...     # OnLLMRequestEvent
    async def _call_llm(self, event, req, trace) -> LLMResponse: ...
    async def _run_response_hooks(self, event, req, resp, trace) -> None: ...
    def abort_current_event(self, reason="aborted") -> None: ...           # 对齐中止语义
    def status_snapshot(self) -> dict: ...
```

### 4.2 ProviderRequest 构造（`_build_provider_request`）

最小可用构造，覆盖绝大多数钩子的读取需求：

- `prompt = user_text`
- `contexts = history_snapshot(session)`（沿用现有 `sessions.history_snapshot`，格式 `[{role, content}]`）
- `system_prompt`：基础人设——优先 Quest 具象人格（复用 `inject_quest_persona` 同源），否则 AstrBot 选中人格（`persona_adapter.resolve()`）。**注意**：人设主要由各插件钩子注入，这里只放基底，避免与钩子重复。
- `conversation`：经 `context.conversation_manager` 取当前会话对象（memory_companion 等钩子会读 `req.conversation`/`unified_msg_origin`）。取不到则置 `None` 并记诊断。
- `func_tool`：v1 置 `None`（dialogue-only，不开放 LLM 工具多轮调用）。部分过滤工具的钩子（声 `filter_tts_tool`、sanitize 系列）在 `func_tool=None` 时应自然短路，需在 Phase 0 验证。

> 显式不复用 `build_main_agent`：其 `reset_coro`（人设/知识库/工作区/图片字幕等重活）正是潜在慢点，搬进来等于把问题带进来。本方案只取"钩子富化"这一核心。

### 4.3 钩子超时调度器（`_run_request_hooks`）

复刻 `call_event_hook` 语义，但每钩子包独立超时 + 全局钩子预算：

```python
handlers = star_handlers_registry.get_handlers_by_event_type(
    EventType.OnLLMRequestEvent, plugins_name=event.plugins_name)
deadline = monotonic() + total_hook_budget_seconds
for h in handlers:
    plugin = star_map[h.handler_module_path].name
    if plugin in self.excluded_plugins:
        continue
    remaining = deadline - monotonic()
    if remaining <= 0:
        record("hook_budget_exhausted", ...); break
    timeout = min(self.per_hook_budget_seconds, remaining)
    try:
        await asyncio.wait_for(h.handler(event, req), timeout=timeout)
        record("hook_ok", plugin=plugin, wall_ms=...)
    except asyncio.TimeoutError:
        record("hook_timeout", plugin=plugin, budget=timeout)
        self._apply_cached_contribution(plugin, req)   # 见 4.6
        continue                                       # 熔断，放行后续钩子
    if event.is_stopped():
        record("hook_stopped", plugin=plugin); break
```

要点：
- **不 `assert iscoroutinefunction`**，对非协程处理器容错跳过并记诊断；
- 钩子异常（非超时）沿用 AstrBot 行为——记日志后继续，不让单插件异常中断链路；
- 每个钩子的 `wall_ms`/`status` 写入 `diagnostic_log`，复用现有 span 体系（trace_id 关联），便于回归对比。

### 4.4 直管 LLM 调用（`_call_llm`）

```python
provider = await context.get_using_provider_async(event.unified_msg_origin)  # 或按 chat_provider_id 精确取
resp = await asyncio.wait_for(provider.text_chat(req), timeout=self.llm_timeout_seconds)
```

- 优先按 `chat_provider_id`（现有配置 `deepseek/deepseek-v4-flash`）精确取 provider，取不到回退 `get_using_provider_async`；
- 独立 `llm_timeout_seconds`（默认 30s），超时报 `quest_pipeline_llm_timeout`，绝不无声卡死；
- 不进共享事件总线，不触碰 `session_lock_manager`，与 QQ 完全隔离。

### 4.5 响应后处理钩子（`_run_response_hooks`）

- `OnLLMResponseEvent`：逐钩子（带超时），允许插件改写 `resp`；
- 装饰类钩子（`OnDecoratingResultEvent`，如加前缀/t2i）：逐钩子（带超时）；
- 提取 `resp.completion_text`（或 result_chain 纯文本）作为回复，截断 4000 字，返回 `ModelDecision(should_reply=True, reply_text=...)`；
- 空回复 + 文本必需（`requires_text_reply`）→ 抛 `MessagePipelineEmpty`，对齐现有空回复语义。

### 4.6 记忆缓存兜底（`_apply_cached_contribution`）

- 维护 `per-session` 的"钩子贡献缓存"：`{plugin_name: (injected_system_prompt_fragment, timestamp)}`；
- 钩子**成功**时，记录该插件对 `req.system_prompt` 的增量片段与时间戳；
- 钩子**超时**时，若缓存未过期（`memory_cache_ttl_seconds`，默认 30s）则注入上次成功片段（有旧记忆 > 没记忆）；
- 缓存只读增量、不写库，无副作用。

> 这是"熔断后优雅降级"的关键：memory_companion 慢时用 30s 内的近实时记忆，快时用实时记忆。

### 4.7 配置项（`_conf_schema.json` 新增 `quest_chain` 节）

| 键 | 默认 | 说明 |
|---|---|---|
| `mode` | `main` | 链路模式：`main`(主链路) / `bridge`(临独立链路) / `auto`(自动回退) |
| `per_hook_budget_seconds` | `6.0` | 临独立链·单个钩子超时 |
| `total_hook_budget_seconds` | `10.0` | 临独立链·钩子链总预算 |
| `llm_timeout_seconds` | `30.0` | 临独立链·直管 LLM 超时 |
| `memory_cache_ttl_seconds` | `30.0` | 临独立链·钩子贡献缓存 TTL |
| `excluded_plugins` | `[]` | 临独立链·彻底跳过的插件名（可选） |

> `mode` 之外的项只在 `bridge`/`auto` 生效；`main` 模式完全沿用现有行为，不读取这些项。
> 配置热更新：改 `mode` 无需重启，下一轮对话即生效（沿用现有 config 监听机制）。
> 上述项均可在临的 **operator 页面**可视化设置，且按模式条件显隐（详见 4.9）；页面之外也可直接改 AstrBot 配置。

### 4.8 `turn_orchestrator` 接线（QuestChainRouter）

新增轻量 `QuestChainRouter`（可放在 `core/` 或 `adapters/`），在 `_decide_and_deliver` 取代当前二选一：

```python
async def _decide_and_deliver(...):
    if interaction is None and session.protected_context_authorized:
        decision = await self._await_traced(
            turn, "quest_chain.decide",
            self.quest_chain_router.decide(
                session=session, turn=turn, user_text=user_text,
                history=history, relationship=relationship,
            ),
            kind="quest_chain", category="await",
        )
        # router 内部按 mode 分发：
        #   main   -> AstrBotMessagePipelineAdapter.generate()
        #   bridge -> QuestEnrichedPipelineAdapter.generate()
        #   auto   -> try bridge; except -> main; except -> direct JSON(若允许)
    else:
        ...  # interaction 分支维持现状
```

- 每种模式包 `eventbus_terminal_deadline_seconds`（45s）整轮兜底；临独立链内部各环超时远小于该值，保证"先内部熔断、后整轮兜底"；
- `auto` 模式的回退只捕获"链路不可用/超时/空回复"类异常，不吞编程错误；
- 中止语义对齐：两种模式都在超时/打断/会话关闭时调用各自 `abort_current_event`（复用现有 `_abort_synthetic_event`）；
- 埋点口径统一：`quest_chain.decide` span 记录 `mode`、`selected_path`、`fallback_from`、`duration_ms`，与现有 eventbus span 兼容。

### 4.9 临·operator 页面设置（双模切换 + 条件显隐）

模式切换放在临的 **operator 页面**（`pages/operator/`），**复用现有「人格工作区」的分段控件 + `hidden` 显隐模式**（`setPersonaWorkflowMode` 同款机制），不引入新 UI 框架。

**4.9.1 布局**（新增一个 `setting-panel`，与现有"最简模式"等面板并列）：

```
┌─ Quest 链路模式 ────────────────────────────┐
│ [ AstrBot 主链路 | 临独立链路 | 自动回退 ]      │ ← 分段控件 data-quest-chain-mode
│                                             │
│ ┌─ bridge 专属设置区 (id=quest-chain-bridge-fields) ─┐
│ │ 单钩子超时 [6.0]s   钩子总预算 [10.0]s            │   仅 bridge/auto 显示
│ │ LLM 超时 [30.0]s    记忆缓存 TTL [30.0]s          │
│ │ 跳过插件 [________________]                        │
│ └─────────────────────────────────────────────┘
│ ┌─ main 专属说明区 (id=quest-chain-main-fields) ────┐
│ │ 主链路走 AstrBot 共享事件总线，完整插件钩子，          │   仅 main 显示
│ │ 无钩子超时保护；耗时受 memory_companion 影响。        │
│ └─────────────────────────────────────────────┘
│ [保存模式]  状态: …                            │
└─────────────────────────────────────────────┘
```

**4.9.2 条件显隐规则**（核心需求）：

| 选中模式 | bridge 专属设置区 | main 专属说明区 |
|---|---|---|
| `main` | **隐藏** | 显示 |
| `bridge` | 显示 | **隐藏** |
| `auto` | 显示（auto 会用到 bridge 链路） | 显示（auto 可能回退 main） |

- 实现：新增 `setQuestChainMode(mode)`，切换分段控件 `.active`/`aria-selected`，并按上表设置两个字段区容器的 `.hidden`——与 `setPersonaWorkflowMode` 完全一致；
- **未保存的切换也即时显隐**（纯前端，改 `hidden` 不落库），点「保存模式」才提交后端；
- 隐藏字段区的值仍保留在 DOM/内存，切换回来不丢失未保存输入；
- `config_writable !== true` 时全部禁用（沿用现有 `operatorSettings.config_writable` 门）。

**4.9.3 后端 API**（`identity_control_plane.py` / operator settings 路由，与现有 `pairing/dialogue-mode` 同构）：

- `GET pairing/quest-chain-settings` → 返回 `{ mode, per_hook_budget_seconds, ..., config_writable }`；
- `POST pairing/quest-chain-settings` → 校验并保存 `quest_chain.*` 到 AstrBot 配置对象，返回最新设置供 `renderQuestChainSettings` 回填；
- 保存后触发配置热更新监听，下一轮对话即生效（无需重启）。

**4.9.4 渲染函数**：新增 `renderQuestChainSettings(settings)`，在 operator 页初始化时调用，回填模式与字段值、设置 `config_writable` 门、并调用 `setQuestChainMode(mode)` 初始化显隐。

---

## 5. Phase 0：技术风险与原型验证点

正式开发前先做最小原型，验证以下**不确定点**（任一不成立则调整设计）：

1. **钩子对 `func_tool=None` 的健壮性**：声/私人伴侣的 sanitize 类钩子在无工具集时是否安全短路，还是抛异常。（验证方式：构造最小 req 跑一遍钩子链，看诊断日志有无异常）
2. **`req.conversation` 的必需性**：memory_companion 的 `handle_llm_request` 在 `conversation=None` 或缺失时是否仍能注入记忆，还是直接 return。（决定 4.2 是否必须取会话对象）
3. **钩子对合成事件的兼容**：现有 `_build_capture_event` 造的事件已带全部标记，但需确认钩子读取 `unified_msg_origin`/`get_message_str` 等访问器在直管（未入总线）场景下行为一致。
4. **`get_using_provider_async` 在直管场景的可用性**：umo 未在总线注册会话时能否取到 provider；不行则强制走 `chat_provider_id` 精确取。
5. **回复提取口径**：确认目标 provider 的 `LLMResponse.completion_text` 为非流式完整文本（与事件总线 `send()` 捕获口径一致）。

> Phase 0 用一个临时脚本在容器内直接 import 钩子注册表 + 构造 req 跑一遍，输出每钩子 wall_ms 与最终回复，确认无误后再写正式模块。

---

## 6. 分阶段实施计划

| 阶段 | 内容 | 产出 | 验收 |
|---|---|---|---|
| **P0** 原型验证 | 容器内临时脚本验证第 5 节 5 个不确定点 | 验证结论记录 | 5 点全部明确，无阻断 |
| **P1** 临独立链核心 | `quest_enriched_pipeline.py`：事件构造 + req 构造 + 钩子超时调度 + 直管 LLM | bridge 模式可返回自然文本回复 | 单轮 < 钩子预算+LLM 内必返回 |
| **P2** 后处理 + 缓存兜底 | 响应/装饰钩子 + 记忆贡献缓存 | 完整富化 + 熔断降级 | memory_companion 超时仍有旧记忆 |
| **P3** 双模路由 + 配置 + 页面 + 观测 | `QuestChainRouter` + `_conf_schema.json`(`quest_chain.mode`) + operator 页双模切换与条件显隐(4.9) + 统一埋点 | main/bridge/auto 三模式可切换、页面可配置 | `mode=main` 时行为=现状；页面显隐正确 |
| **P4** 测试 + 推送 | 单测 + 容器内双模三连问对比回归 + 推送 GitHub | commit + 更新说明 | 见第 7 节验收标准 |

每阶段独立提交，便于回退。P1/P2 只新增独立模块、不动现有路径，天然安全；P3 才接 orchestrator，接入前 `mode=main` 保证线上不变。

---

## 7. 测试方案

- **单元测试**（`tests/`）：钩子超时熔断、缓存兜底、空回复语义、`auto` 回退链、provider 取用失败路径、mode 分发正确性。
- **双模对比回归**：同一组问题分别跑 `main` 与 `bridge`，用 `/tmp/latency_test5.py`（单 SSE 长连接三连问）+ `/tmp/extract_spans.py <trace_id>` 对比：
  - `turn.processing` 总时长（bridge 目标 <20s vs main 基线 26~45s）；
  - 每钩子 wall_ms（确认 memory_companion 在 bridge 下被钳在预算内、main 下不钳）；
  - 回复文本质量抽查（bridge 是否仍带记忆/人设/关系，与 main 相当）。
- **QQ 不受影响**：跑 Quest 双模期间，观察 QQ 群回复 span 与改动前一致。
- **页面 UI**：operator 页「Quest 链路模式」分段控件可切换三模式；选 `main` 隐藏 bridge 专属设置、选 `bridge` 隐藏 main 专属说明、`auto` 两者皆显；未保存切换即时显隐且不切回丢输入；保存后 `quest_chain.mode` 落库且下一轮生效。
- **验收标准**：
  1. `mode=main` 时 Quest/QQ 行为与当前完全一致（回归零差异）；
  2. `mode=bridge` 连续 3 问均 <20s，无 45s 卡死；
  3. memory_companion 慢时 bridge 不卡死且有兜底记忆；
  4. `mode=auto` 在 bridge 注入故障时正确回退 main；
  5. 页面模式切换与条件显隐符合 4.9.2 规则。

---

## 8. 回退与兼容性

- `quest_chain.mode=main`（默认）→ 完全走现有共享事件总线路径，零行为变化；
- `bridge` 任何异常 → `auto` 下回退 `main`，再不行且 `allow_direct_provider_fallback=True` 回退直管 JSON，不影响 QQ；
- 不改动 `_build_capture_event`、`astrbot_pipeline.py` 现有逻辑；主链路与独立链共用同一事件构造/回复提取，仅调度方式不同；
- QQ 链路（共享事件总线）代码路径零改动；
- 双模共存于同一插件，仅靠 `mode` 切换，无需卸载/重装。

---

## 9. 交付与更新流程

1. 本地 `astrbot_plugin_embodiment_bridge` 仓库分阶段提交（分支 main）；
2. 推送到 GitHub；
3. **明确告知需更新插件：凝心溯溪-临（embodiment_bridge）**，由你手动更新到远端 AstrBot；
4. 你在临的 **operator 页面**将「Quest 链路模式」切到 `bridge` 或 `auto`（页面按模式条件显隐对应设置区）；灰度验证稳定后可长期开启。

> 硬约束自查：本方案仅修改/新增临（embodiment_bridge）内文件，不触碰 AstrBot 核心、容器、或任何非系列插件源码。
