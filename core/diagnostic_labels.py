"""集中式中文标签表：诊断事件 / 服务状态 / 人格转换阶段。

本模块是 ``pages/operator/app.js`` 历史「错误码 → 中文」查表函数的服务端
归宿。诊断事件、服务状态快照与人格转换任务在**下发时**附加 ``*_label``
可选字段（纯加法、向后兼容；事件名与原因码字段保持不变），前端只做
``payload.label || String(code)`` 渲染。文案以 app.js 既有查表为准。
"""

from __future__ import annotations

# ── 诊断原因码（原 app.js diagnosticReasonLabel）──────────────────────────
REASON_LABELS: dict[str, str] = {
    "owner_not_configured": "“序”尚未为这组 Quest 原始身份配置主人",
    "quest_identity_not_allowlisted": "Quest 原始身份不在“序”的允许列表",
    "local_identity_not_configured": "“临”的本地 Quest 身份尚未配置完整",
    "local_api_principal_mismatch": "Quest 使用的 AstrBot API Key 与本地绑定不一致",
    "local_quest_identity_mismatch": "具身客户端、平台、Bot 或主人用户与本地绑定不一致",
    "invalid_user_id": "Quest 用户 ID 无效或仍是占位值",
    "missing_user_id": "Quest 用户 ID 缺失",
    "invalid_bot_id": "Quest Bot ID 无效",
    "missing_bot_id": "Quest Bot ID 缺失",
    "client_id_mismatch": "具身客户端 ID 与服务端配置不一致",
    "trusted_platform_not_configured": "尚未配置可进入 EventBus 的 AstrBot 平台",
    "trusted_platform_unavailable": "已配置的 AstrBot 平台当前不可用",
    "astrbot_event_api_unavailable": "当前 AstrBot 不提供消息事件接口",
    "astrbot_message_pipeline_unavailable": "AstrBot 消息链路不可用",
    "astrbot_pipeline_reply_required_missing": "AstrBot 消息链未生成本轮明确要求的文字回复",
    "astrbot_pipeline_timeout": "AstrBot 消息链处理超时",
    "astrbot_pipeline_empty_reply": "AstrBot 消息链没有返回可用内容",
    "astrbot_pipeline_event_stopped": "AstrBot 消息事件已由插件接管，但未留下可用正文",
    "astrbot_pipeline_not_woken": "AstrBot 消息事件未通过唤醒规则",
    "astrbot_pipeline_reply_capture_empty": "AstrBot 已执行发送，但回复捕获为空",
    "stt_empty": "没有识别到有效语音",
    "stt_unavailable": "语音识别服务未配置",
    "stt_failed": "语音识别失败",
    "llm_failed": "模型生成失败",
    "tts_failed": "语音合成失败，文字回复仍可能可用",
    "audio_upload_backpressure": "音频上传速度跟不上录音",
    "audio_http_request_failed": "音频上传请求失败",
    "turn_failed": "对话生成失败",
    "interaction_failed": "触碰交互决策失败",
    "fast_action_disabled": "异步快速动作已关闭",
    "fast_action_provider_not_configured": "尚未选择快速动作模型",
    "fast_action_selected_missing": "已选快速动作模型当前不可用",
    "fast_action_provider_catalog_unavailable": "快速动作模型目录当前不可用",
    "fast_action_timeout": "快速动作模型等待超时，将尝试保守的本地动作兜底",
    "fast_action_invalid_output": "快速动作模型返回了不符合动作协议的内容",
    "fast_action_failed": "快速动作决策失败，普通回复不受影响",
    "fast_action_enabled": "已由独立快速动作模型处理",
    "fast_action_selected": "快速动作已先于主回复选定",
    "autonomous_greeting": "根据明确的问候或告别选择自然挥手",
    "autonomous_introduction": "根据自我介绍语境选择自然挥手",
    "autonomous_appreciation": "根据感谢或道歉语境选择轻微鞠躬",
    "autonomous_agreement": "根据明确赞同语境选择自然点头",
    "autonomous_celebration": "根据明确庆祝语境选择自然抬手",
    "autonomous_gesture_cooldown": "短时间内已做过相同自主动作，本轮保持待机",
    "reply_path_selected": "主回复链路已先选定动作",
    "conversion_timeout": "人格转换模型等待超时",
    "conversion_first_chunk_timeout": "人格转换模型首个流块等待超时",
    "conversion_stream_idle_timeout": "人格转换模型输出流长时间无新数据",
    "conversion_stream_unsupported": "所选模型 Provider 不支持流式人格转换",
    "conversion_response_too_large": "人格转换模型返回内容过大",
    "conversion_provider_failed": "人格转换模型调用失败",
    "conversion_response_invalid": "人格转换结果无法解析",
    "conversion_schema_invalid": "人格转换结果结构不符合要求",
    "conversion_schema_unsupported": "人格转换结果版本不受支持",
    "persona_conversion_failed": "人格转换任务失败",
    "conversion_job_in_progress": "已有其他人格转换正在运行",
    "conversion_job_not_found": "人格转换任务不存在或已经过期",
    "response_first_event_timeout": "后端接收后没有返回首个事件",
    "response_event_stall_timeout": "后端事件流在回复结束前停滞",
    "ready": "链路就绪",
}

# ── 诊断事件名（原 app.js diagnosticEventLabel）───────────────────────────
EVENT_LABELS: dict[str, str] = {
    "session.authorization": "完成身份授权检查",
    "session.authorization_error": "身份授权检查异常",
    "session.started": "会话已建立",
    "sse.connected": "SSE 已连接",
    "sse.disconnected": "SSE 已断开",
    "turn.accepted": "后端已接收轮次",
    "audio.received": "音频上传完成",
    "stt.started": "开始语音识别",
    "stt.completed": "语音识别完成",
    "stt.error": "语音识别失败",
    "message_pipeline.selected": "选择回复链路",
    "message_pipeline.started": "进入 AstrBot EventBus",
    "message_pipeline.completed": "AstrBot EventBus 返回",
    "message_pipeline.blocked": "AstrBot EventBus 被阻止",
    "message_pipeline.fallback": "消息链路发生降级",
    "message_pipeline.stopped_after_fast_action": "EventBus 已完成动作轮但没有正文",
    "message_pipeline.required_reply_missing": "EventBus 缺少明确要求的文字回复",
    "fast_action.started": "快速动作模型开始判断",
    "fast_action.provider_resolved": "快速动作模型已定位",
    "fast_action.request_queued": "快速动作请求已发出",
    "fast_action.first_chunk": "快速动作模型首个流块已到达",
    "fast_action.provider_completed": "快速动作模型生成完成",
    "fast_action.parsed": "快速动作结果解析完成",
    "fast_action.parsed_no_action": "快速动作模型决定本轮不做动作",
    "fast_action.parse_invalid": "快速动作结果格式无效",
    "fast_action.timeout": "快速动作模型等待超时",
    "fast_action.provider_error": "快速动作模型调用失败",
    "fast_action.provider_unavailable": "快速动作模型当前不可用",
    "fast_action.local_fallback_selected": "已选择保守的本地自主动作",
    "fast_action.explicit_selected": "明确动作命令已立即选定",
    "fast_action.explicit_rejected": "不安全或歧义动作命令已拒绝",
    "fast_action.completed": "快速动作判断完成",
    "fast_action.cancelled": "EventBus 已选动作，快速动作模型已取消",
    "fast_action.skipped": "快速动作已回退或跳过",
    "fast_action.error": "快速动作判断失败",
    "fast_action.settings_updated": "快速动作设置已更新",
    "avatar.action.eventbus_outcome": "EventBus 动作工具结果",
    "avatar.action.main_delivery_parallel": "正文与动作并行交付",
    "avatar.action.reply_wait_for_arbitration": "回复结束前等待动作仲裁",
    "avatar.action.arbitration_winner": "动作仲裁胜者已确定",
    "avatar.intent.emitted": "动作意图已下发",
    "avatar.intent.dropped": "动作意图未下发",
    "reply_text_first_emitted": "首个文字事件已下发",
    "reply_audio_first_emitted": "首个音频事件已下发",
    "audio.upload.completed": "音频上传汇总完成",
    "avatar.action.tool_skipped": "主回复动作工具已切换",
    "avatar.action.tool_superseded": "主回复动作工具已被快速动作替代",
    "avatar.intent.skipped": "重复动作意图已抑制",
    "llm.completed": "模型生成完成",
    "llm.error": "模型生成失败",
    "tts.completed": "语音合成完成",
    "tts.error": "语音合成失败",
    "persona.convert.started": "开始转换具身人格",
    "persona.convert.completed": "具身人格预览转换完成",
    "persona.convert.failed": "具身人格转换失败",
    "persona.convert.job.queued": "人格转换后台任务已排队",
    "persona.convert.job.cancelled": "人格转换后台任务已取消",
    "persona.convert.cancelled": "人格转换后台任务已取消",
    "persona.convert.source.started": "开始读取人格来源",
    "persona.convert.source.completed": "人格来源读取完成",
    "persona.convert.model.started": "转换模型开始生成",
    "persona.convert.model.first_chunk": "转换模型首个流块已到达",
    "persona.convert.model.streaming": "转换模型正在持续生成",
    "persona.convert.model.completed": "转换模型生成完成",
    "persona.convert.validation.started": "开始校验转换结果结构",
    "persona.convert.validation.completed": "转换结果结构校验完成",
    "persona.convert.draft.created": "转换预览草稿已就绪",
    "persona.convert.progress": "人格转换任务实时进度",
    "persona.save.started": "开始保存具身人格",
    "persona.save.completed": "具身人格文件保存完成",
    "persona.save.failed": "具身人格保存失败",
    "persona.activate.started": "开始切换当前具身人格",
    "persona.activate.completed": "当前具身人格切换完成",
    "persona.activate.failed": "具身人格切换失败",
    "persona.overlay.injected": "具身人格已注入 Quest 对话",
    "persona.overlay.skipped": "本轮未注入具身人格",
    "reply.completed": "回复交付完成",
    "reply.failed": "回复交付失败",
    "http.health": "健康检查完成",
    "http.error": "HTTP 请求失败",
    "plugin_hook_profiler.scan": "插件钩子扫描完成",
    "plugin_hook.completed": "插件钩子执行完成",
}

# ── 诊断环节 / 组件（原 app.js diagnosticStageLabel）──────────────────────
STAGE_LABELS: dict[str, str] = {
    "configuration": "配置",
    "authorization": "身份授权",
    "identity": "身份授权",
    "session": "会话",
    "health": "健康检查",
    "sse": "实时事件",
    "transport": "HTTP 传输",
    "audio_input": "音频上传",
    "audio_upload": "音频上传",
    "microphone": "麦克风",
    "stt": "语音识别",
    "message_pipeline": "AstrBot/EventBus",
    "action": "角色动作",
    "eventbus": "AstrBot/EventBus",
    "llm": "模型生成",
    "tts": "语音合成",
    "reply": "回复交付",
    "turn": "对话轮次",
    "audio_playback": "音频播放",
    "interrupt": "打断",
    "persona": "人格转换",
    "persona_conversion": "人格转换",
    "persona_source": "人格来源读取",
    "persona_model": "人格模型生成",
    "persona_validation": "人格结构校验",
}

# ── 诊断状态（原 app.js diagnosticStatusLabel）────────────────────────────
STATUS_LABELS: dict[str, str] = {
    "ok": "正常",
    "ready": "就绪",
    "authorized": "已授权",
    "connected": "已连接",
    "completed": "完成",
    "processing": "处理中",
    "uploading": "上传中",
    "awaiting_audio": "等待音频",
    "limited": "受限",
    "unavailable": "不可用",
    "fallback": "已降级",
    "blocked": "已阻止",
    "error": "错误",
    "failed": "失败",
    "timeout": "超时",
    "disconnected": "已断开",
    "cancelled": "已取消",
    "no_action": "无需动作",
    "selected": "已选择",
    "superseded": "已由更早结果接管",
    "closed": "已关闭",
}

# ── 角色动作（原 app.js diagnosticActionLabel）────────────────────────────
ACTION_LABELS: dict[str, str] = {
    "idle": "待机",
    "talk": "说话",
    "wave": "挥手",
    "bow": "鞠躬",
    "dance": "播放选定舞蹈",
    "dance_next": "切换下一支舞蹈",
    "raise_hand": "抬手",
    "turn_half": "转身",
    "sit": "坐下",
    "lie": "躺下",
    "nod": "点头",
    "sway": "轻微摆动",
    "crouch": "下蹲",
    "handshake": "握手反应",
    "head_pat": "摸头反应",
    "cheek_pinch": "捏脸反应",
    "refuse": "拒绝",
    "step_back": "后退",
}

# ── 动作来源（原 app.js diagnosticActionSourceLabel）──────────────────────
ACTION_SOURCE_LABELS: dict[str, str] = {
    "explicit_request": "用户明确命令",
    "fast_provider": "快速动作模型",
    "local_context_fallback": "本地社交动作兜底",
    "eventbus_tool": "AstrBot 动作工具",
    "direct_model": "直接回复模型",
    "interaction_policy": "触碰交互策略",
    "fallback": "基础动作兜底",
    "fast_provider_pending": "等待快速动作模型",
    "fast_provider_fallback": "快速动作本地兜底",
    "main_reply_suppressed": "主回复动作已抑制",
    "eventbus_tool_fallback": "EventBus 动作兜底",
}

# ── 计时跨度（原 app.js diagnosticSpanLabel）──────────────────────────────
SPAN_LABELS: dict[str, str] = {
    "stt.turn": "语音识别",
    "stt.streaming_wait": "流式识别等待",
    "stt.final_emit": "识别结果下发",
    "turn.processing": "整轮处理",
    "quest_chain.event_create": "决策事件创建",
    "quest_chain.build_request": "构建请求",
    "quest_chain.request_hooks": "模型前置插件钩子",
    "quest_chain.llm": "LLM 模型生成",
    "quest_chain.response_hooks": "模型后置插件钩子",
    "context.history_snapshot": "历史快照",
    "context.relationship_snapshot": "关系快照",
    "tts.pipeline": "语音合成",
    "tts.segment": "语音合成段",
    "reply.audio_emit": "回复音频下发",
    "eventbus.generate": "EventBus 生成",
    "eventbus.queue_wait": "EventBus 排队",
    "eventbus.processing": "EventBus 处理",
}

_HOOK_SPAN_PREFIX = "quest_chain.hook."

# ── 人格转换任务状态（供任务载荷 status_label 使用）───────────────────────
CONVERSION_JOB_STATUS_LABELS: dict[str, str] = {
    "queued": "排队中",
    "running": "处理中",
    "completed": "完成",
    "failed": "失败",
    "cancelled": "已取消",
}

# ── 人格转换阶段（原 app.js personaConversionStageLabel）──────────────────
PERSONA_CONVERSION_STAGE_LABELS: dict[str, str] = {
    "accepted": "任务已受理",
    "source_lookup": "正在读取来源人格",
    "source_ready": "来源人格读取完成",
    "provider_wait": "正在等待转换模型首个流块",
    "provider_first_chunk": "转换模型已开始响应",
    "provider_streaming": "转换模型正在持续生成",
    "provider_response": "模型生成已返回",
    "response_validation": "正在校验转换结果结构",
    "response_validated": "转换结果结构校验完成",
    "preview_ready": "转换预览已就绪",
    "failed": "转换失败",
    "cancelled": "转换已取消",
}

# ── 服务状态原因（原 app.js serviceReasonLabel）───────────────────────────
SERVICE_REASON_LABELS: dict[str, str] = {
    "ready": "服务运行正常",
    "service_disabled": "服务已由管理员关闭",
    "disabled": "内置 8520 监听尚未启用",
    "not_started": "监听器尚未启动",
    "bind_failed": "监听端口绑定失败",
    "start_failed": "监听器启动失败",
    "invalid_enabled": "监听开关配置无效",
    "invalid_bind_host": "监听地址配置无效",
    "invalid_port": "监听端口配置无效",
    "invalid_upstream_url": "AstrBot 回环上游配置无效",
    "listener_unavailable": "内置监听器不可用",
    "pairing_listener_public_url_missing": "服务已运行，但尚未配置客户端可达地址",
}

# ── 服务状态徽章（原 app.js renderServiceStatus 内联表）───────────────────
SERVICE_STATUS_LABELS: dict[str, str] = {
    "running": "运行中",
    "stopped": "已关闭",
    "degraded": "需检查",
}

# ── 诊断日志状态（原 app.js loadDiagnostics 内联表）───────────────────────
DIAGNOSTICS_STATUS_LABELS: dict[str, str] = {
    "ready": "可用",
    "memory_only": "内存诊断可用（文件日志未启用）",
    "disabled": "未启用",
    "unavailable": "暂不可用",
}


def reason_label(code: object) -> str:
    value = str(code or "")
    return REASON_LABELS.get(value) or value or "未发现明确错误码"


def event_label(value: object) -> str:
    raw = str(value or "")
    return EVENT_LABELS.get(raw) or raw or "诊断事件"


def stage_label(value: object) -> str:
    raw = str(value or "")
    return STAGE_LABELS.get(raw) or raw or "运行链路"


def status_label(value: object) -> str:
    raw = str(value or "")
    return STATUS_LABELS.get(raw) or raw or "状态未知"


def action_label(value: object) -> str:
    raw = str(value or "")
    return ACTION_LABELS.get(raw) or raw or "未知动作"


def action_source_label(value: object) -> str:
    raw = str(value or "")
    return ACTION_SOURCE_LABELS.get(raw) or raw or "未知来源"


def span_label(name: object) -> str:
    value = str(name or "")
    if not value:
        return "未知跨度"
    if value.startswith(_HOOK_SPAN_PREFIX):
        return f"钩子：{value[len(_HOOK_SPAN_PREFIX):]}"
    return SPAN_LABELS.get(value) or value


def persona_conversion_stage_label(stage: object) -> str:
    raw = str(stage or "")
    return PERSONA_CONVERSION_STAGE_LABELS.get(raw) or raw or "正在处理转换任务"


def conversion_job_status_label(status: object) -> str:
    raw = str(status or "")
    return CONVERSION_JOB_STATUS_LABELS.get(raw) or raw or "状态未知"


def service_reason_label(reason: object) -> str:
    raw = str(reason or "")
    return SERVICE_REASON_LABELS.get(raw) or raw or "服务状态需要检查"


def service_status_label(status: object) -> str:
    raw = str(status or "")
    return SERVICE_STATUS_LABELS.get(raw) or raw or "未知"


def diagnostics_status_label(status: object) -> str:
    raw = str(status or "")
    return DIAGNOSTICS_STATUS_LABELS.get(raw) or raw


def enrich_diagnostic_event(event: dict[str, object]) -> dict[str, object]:
    """为 diagnostics 投影事件附加 ``*_label`` 可选字段（纯加法、就地补充）。

    输入事件需已包含 ``event``/``component``/``span_name``/``status``/
    ``reason_code``/``code``/``phase``/``operation``/``action_source`` 等
    既有字段；本函数只新增 label 字段，不改任何既有键。
    """

    component = str(event.get("component") or "")
    span_name = str(event.get("span_name") or "")
    if component:
        event["component_label"] = stage_label(component)
    if span_name:
        event["span_label"] = span_label(span_name)
    event["event_label"] = event_label(event.get("event"))
    event["status_label"] = status_label(event.get("status"))
    reason = str(event.get("reason_code") or event.get("code") or "")
    if reason:
        event["reason_label"] = reason_label(reason)
    phase = str(event.get("phase") or "")
    if phase:
        event["phase_label"] = persona_conversion_stage_label(phase)
    operation = str(event.get("operation") or "")
    if operation:
        event["operation_label"] = action_label(operation)
    action_source = str(event.get("action_source") or "")
    if action_source:
        event["action_source_label"] = action_source_label(action_source)
    return event
