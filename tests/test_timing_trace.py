from __future__ import annotations

import asyncio

from astrbot_plugin_embodiment_bridge.core.timing_trace import TimingTrace


class Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def record(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


def test_trace_records_required_bounded_fields_and_nested_spans() -> None:
    async def scenario() -> None:
        sink = Sink()
        trace = TimingTrace(sink, enabled=True, trace_id="trace-test")
        parent = trace.start_span("agent", kind="agent")
        child = trace.start_span(
            "provider.queue",
            kind="provider",
            parent_id=parent,
            category="queue",
        )
        await asyncio.sleep(0)
        trace.finish_span(
            child,
            status="completed",
            queue_wait_ms=1,
            cache_hit=True,
            retry_count=0,
            timeout=False,
            fallback=False,
        )
        trace.finish_span(parent, status="completed")
        await trace.close()
        spans = [fields for event, fields in sink.events if event == "timing.span.completed"]
        assert len(spans) == 2
        provider = next(item for item in spans if item["span_name"] == "provider.queue")
        for key in (
            "wall_ms",
            "active_ms",
            "queue_wait_ms",
            "lock_wait_ms",
            "provider_wait_ms",
            "start_offset_ms",
            "end_offset_ms",
            "event_loop_lag_ms",
        ):
            assert isinstance(provider[key], int)
            assert provider[key] >= 0
        assert provider["cache_hit"] is True
        assert provider["retry_count"] == 0
        assert provider["timeout"] is False
        assert provider["fallback"] is False
        assert provider["parent_span_id"] == parent

    asyncio.run(scenario())


def test_trace_disabled_has_no_events_and_does_not_require_a_running_loop() -> None:
    sink = Sink()
    trace = TimingTrace(sink, enabled=False)
    span = trace.start_span("disabled")
    assert trace.finish_span(span) is False
    trace.trace_point("disabled")
    assert sink.events == []


def test_async_wait_span_emits_safe_defaults_and_provider_wait() -> None:
    async def scenario() -> None:
        sink = Sink()
        trace = TimingTrace(sink, enabled=True, trace_id="trace-await")
        async with trace.async_span(
            "llm.provider_request",
            kind="llm_provider",
            category="provider",
        ):
            await asyncio.sleep(0)
        await trace.close()
        fields = next(
            fields
            for event, fields in sink.events
            if event == "timing.span.completed"
        )
        assert fields["span_name"] == "llm.provider_request"
        assert fields["provider_wait_ms"] >= fields["wall_ms"]
        assert fields["active_ms"] == 0
        assert fields["cache_hit"] is False
        assert fields["retry_count"] == 0
        assert fields["timeout"] is False
        assert fields["fallback"] is False

    asyncio.run(scenario())


def test_provider_markers_are_projected_without_payload_data() -> None:
    sink = Sink()
    trace = TimingTrace(sink, enabled=True, trace_id="trace-provider")
    span = trace.start_span("provider.request", kind="llm_provider")
    trace.trace_point("provider.request_sent")
    trace.trace_point("provider.first_token")
    trace.trace_point("provider.completed")
    trace.finish_span(span, status="completed")
    fields = next(
        fields for event, fields in sink.events if event == "timing.span.completed"
    )
    assert fields["provider_request_offset_ms"] >= 0
    assert fields["provider_first_token_offset_ms"] >= 0
    assert fields["provider_end_offset_ms"] >= 0
    assert fields["provider_first_token_ms"] >= 0
    assert fields["provider_total_ms"] >= 0
    assert "prompt" not in fields


def test_provider_markers_are_scoped_to_the_active_provider_span() -> None:
    sink = Sink()
    trace = TimingTrace(sink, enabled=True, trace_id="trace-provider-scoped")
    first = trace.start_span("llm.first", kind="llm_provider")
    trace.trace_point("provider.first_token")
    trace.trace_point("provider.completed")
    trace.finish_span(first, status="completed")
    second = trace.start_span("tts.second", kind="tts", category="provider")
    trace.trace_point("provider.first_token")
    trace.finish_span(second, status="completed")
    spans = {
        fields["span_name"]: fields
        for event, fields in sink.events
        if event == "timing.span.completed"
    }
    assert spans["llm.first"]["provider_first_token_ms"] >= 0
    assert spans["tts.second"]["provider_first_token_ms"] >= 0
    assert spans["tts.second"]["provider_request_offset_ms"] >= spans["llm.first"]["provider_request_offset_ms"]


def test_trace_close_is_idempotent_and_ignores_late_callbacks() -> None:
    async def scenario() -> None:
        sink = Sink()
        trace = TimingTrace(sink, enabled=True, trace_id="trace-close")
        span = trace.start_span("turn", kind="turn")
        await trace.close()
        await trace.close()
        before = len(sink.events)
        assert trace.start_span("late")
        assert trace.finish_span(span) is False
        trace.trace_point("provider.completed")
        assert len(sink.events) == before

    asyncio.run(scenario())


def test_eventbus_queue_wait_closes_when_consumed_without_trace_probe() -> None:
    sink = Sink()
    trace = TimingTrace(sink, enabled=True, trace_id="trace-queue")
    trace.start_named_span("eventbus.queue_wait", kind="eventbus", category="queue")
    processing = trace.mark_event_consumed()
    assert processing
    trace.finish_span(processing, status="completed")
    queue_fields = next(
        fields
        for event, fields in sink.events
        if event == "timing.span.completed"
        and fields["span_name"] == "eventbus.queue_wait"
    )
    assert queue_fields["queue_wait_ms"] >= 0
