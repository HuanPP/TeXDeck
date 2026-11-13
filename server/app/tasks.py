from __future__ import annotations

import asyncio
import contextvars
import sys
import threading
import traceback
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .llm import ProgressCallback


class TaskBusy(RuntimeError):
    """A job is already running (or queued) for this generation."""


@dataclass(slots=True)
class TaskState:
    gen_id: str
    status: str  # "running" | "succeeded" | "failed"
    percent: int
    stage: str  # English stage text
    error: str


_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="texdeck-task")
_lock = threading.Lock()
_states: dict[str, TaskState] = {}
_running: set[str] = set()  # gen_ids with a queued or running job
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


def running_count() -> int:
    """Jobs queued or running; a restart would kill them."""
    with _lock:
        return len(_running)


def is_running(gen_id: str) -> bool:
    with _lock:
        return gen_id in _running


def bind_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """The server's event loop, where progress is pushed to WebSocket watchers.
    Bound at startup: jobs are also started from sync routes, which run in a
    worker thread with no loop of their own."""
    global _loop
    _loop = loop


def start(gen_id: str, job: Callable[[ProgressCallback], None]) -> None:
    try:
        bind_loop(asyncio.get_running_loop())
    except RuntimeError:
        pass  # a sync route's worker thread: keep the loop bound at startup

    with _lock:
        if gen_id in _running:
            raise TaskBusy(f"A job is already running for generation {gen_id}")
        _running.add(gen_id)

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
        finally:
            with _lock:
                _running.discard(gen_id)
        _set_and_broadcast(
            TaskState(
                gen_id=gen_id,
                status="succeeded",
                percent=100,
                stage="Done",
                error="",
            )
        )

    try:
        # The job runs as whoever started it (their model and sign-ins).
        _executor.submit(contextvars.copy_context().run, runner)
    except BaseException:
        with _lock:
            _running.discard(gen_id)
        raise


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
