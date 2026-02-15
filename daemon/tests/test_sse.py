"""Tests for SSE queueing and stream lifecycle behavior."""
import asyncio
import json

import pytest

from api import sse


@pytest.mark.asyncio
async def test_push_event_drops_oldest_when_queue_full(monkeypatch):
    sse.sse_subscribers.clear()
    monkeypatch.setattr(sse, "MAX_QUEUE_SIZE", 2)

    queue = sse.register_subscriber("session-a")
    assert queue.maxsize == 2

    await sse.push_event("session-a", "evt-1", {"n": 1})
    await sse.push_event("session-a", "evt-2", {"n": 2})
    await sse.push_event("session-a", "evt-3", {"n": 3})

    assert queue.qsize() == 2
    first = json.loads((await queue.get())["data"])
    second = json.loads((await queue.get())["data"])

    assert first["event"] == "evt-2"
    assert second["event"] == "evt-3"
    sse.unregister_subscriber("session-a", queue)


@pytest.mark.asyncio
async def test_event_generator_cleans_queue_when_last_subscriber_leaves():
    sse.sse_subscribers.clear()

    session_id = "session-b"
    gen = sse.event_generator(session_id)

    next_event_task = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0)
    await sse.push_event(session_id, "flag", {"severity": "high"})

    event = await next_event_task
    payload = json.loads(event["data"])
    assert payload["event"] == "flag"
    assert len(sse.sse_subscribers.get(session_id, set())) == 1

    await gen.aclose()
    assert session_id not in sse.sse_subscribers


@pytest.mark.asyncio
async def test_push_event_fans_out_to_multiple_subscribers():
    sse.sse_subscribers.clear()
    session_id = "session-c"

    queue_a = sse.register_subscriber(session_id)
    queue_b = sse.register_subscriber(session_id)

    await sse.push_event(session_id, "alignment", {"status": "drifting"})

    payload_a = json.loads((await queue_a.get())["data"])
    payload_b = json.loads((await queue_b.get())["data"])

    assert payload_a["event"] == "alignment"
    assert payload_b["event"] == "alignment"

    sse.unregister_subscriber(session_id, queue_a)
    sse.unregister_subscriber(session_id, queue_b)
