import json
import asyncio
from typing import AsyncGenerator
from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

router = APIRouter()

KEEPALIVE_SECONDS = 15
MAX_QUEUE_SIZE = 200

sse_subscribers: dict[str, set[asyncio.Queue]] = {}


def register_subscriber(session_id: str) -> asyncio.Queue:
    """Register a subscriber queue for a session."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
    subscribers = sse_subscribers.setdefault(session_id, set())
    subscribers.add(queue)
    return queue


def unregister_subscriber(session_id: str, queue: asyncio.Queue):
    """Unregister a subscriber queue and cleanup empty session buckets."""
    subscribers = sse_subscribers.get(session_id)
    if not subscribers:
        return
    subscribers.discard(queue)
    if not subscribers:
        sse_subscribers.pop(session_id, None)


async def push_event(session_id: str, event_type: str, data: dict):
    """Push an event to all active subscriber queues for a session."""
    subscribers = list(sse_subscribers.get(session_id, set()))
    if not subscribers:
        return

    payload = {"data": json.dumps({"event": event_type, "data": data}, ensure_ascii=False)}
    for queue in subscribers:
        while queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        await queue.put(payload)


async def event_generator(session_id: str) -> AsyncGenerator:
    """Generate SSE events for a session."""
    queue = register_subscriber(session_id)

    try:
        while True:
            event = await queue.get()
            yield event
    except asyncio.CancelledError:
        pass
    finally:
        unregister_subscriber(session_id, queue)


@router.get("/sessions/{session_id}/stream")
async def stream_session(session_id: str):
    """SSE stream for session updates"""
    return EventSourceResponse(
        event_generator(session_id),
        ping=KEEPALIVE_SECONDS,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
