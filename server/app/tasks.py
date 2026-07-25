from __future__ import annotations

import asyncio
import sys
import threading
import traceback
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .llm import ProgressCallback


@dataclass(slots=True)
class TaskState:
    gen_id: str
    status: str  # "running" | "succeeded" | "failed"
    percent: int
    stage: str  # English stage text
    error: str


_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="texdeck-task")
_lock = threading.Lock()
_states: dict[str, TaskState] = {}
_subscribers: dict[str, list[asyncio.Queue[TaskState]]] = {}
# Captured when start() runs inside an event loop; workers use it for broadcast
_loop: asyncio.AbstractEventLoop | None = None


def _snapshot(state: TaskState) -> TaskState:
    return TaskState(
        gen_id=state.gen_id,
        status=state.status,
        percent=state.percent,
        stage=state.stage,
        error=state.error,
    )


def _set_and_broadcast(state: TaskState) -> None:
    snap = _snapshot(state)
    with _lock:
        _states[state.gen_id] = snap
        queues = list(_subscribers.get(state.gen_id, []))
        loop = _loop
    if loop is None:
        # start() outside an event loop (e.g. unit tests): update dict only
        return
    for queue in queues:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, snap)
        except RuntimeError:
            # event loop already closed; drop this push
            pass


def start(gen_id: str, job: Callable[[ProgressCallback], None]) -> None:
    global _loop
    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        # no running loop (tests): update state dict only, no broadcast
        _loop = None

    initial = TaskState(
        gen_id=gen_id,
        status="running",
        percent=0,
        stage="Queued",
        error="",
    )
    _set_and_broadcast(initial)

    def progress(stage: str, percent: int) -> None:
        _set_and_broadcast(
            TaskState(
                gen_id=gen_id,
                status="running",
                percent=percent,
                stage=stage,
                error="",
            )
        )

    def runner() -> None:
        try:
            job(progress)
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            _set_and_broadcast(
                TaskState(
                    gen_id=gen_id,
                    status="failed",
                    percent=0,
                    stage="Failed",
                    error=str(exc),
                )
            )
            return
        _set_and_broadcast(
            TaskState(
                gen_id=gen_id,
                status="succeeded",
                percent=100,
                stage="Done",
                error="",
            )
        )

    _executor.submit(runner)


def get(gen_id: str) -> TaskState | None:
    with _lock:
        state = _states.get(gen_id)
        return _snapshot(state) if state is not None else None


async def watch(gen_id: str) -> AsyncIterator[TaskState]:
    queue: asyncio.Queue[TaskState] = asyncio.Queue()
    with _lock:
        _subscribers.setdefault(gen_id, []).append(queue)
        current = _states.get(gen_id)
        if current is not None:
            queue.put_nowait(_snapshot(current))
    try:
        while True:
            state = await queue.get()
            yield state
            if state.status in ("succeeded", "failed"):
                return
    finally:
        with _lock:
            subs = _subscribers.get(gen_id)
            if subs is not None:
                try:
                    subs.remove(queue)
                except ValueError:
                    pass
                if not subs:
                    _subscribers.pop(gen_id, None)
