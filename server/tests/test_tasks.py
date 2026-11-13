"""Background jobs push their progress to WebSocket watchers."""

from __future__ import annotations

import asyncio
import threading

from app import tasks


def test_a_job_started_from_a_sync_route_still_reaches_watchers() -> None:
    """Sync routes run in a worker thread with no event loop; the job's events
    must still reach the server loop (it once dropped them all)."""

    async def scenario() -> list[str]:
        tasks.bind_loop(asyncio.get_running_loop())
        seen: list[str] = []

        async def follow() -> None:
            async for state in tasks.watch("sync-route-job"):
                seen.append(state.status)

        watcher = asyncio.create_task(follow())
        await asyncio.sleep(0)
        started = threading.Thread(target=tasks.start, args=("sync-route-job", lambda report: report("Working", 50)))
        started.start()
        started.join()
        await asyncio.wait_for(watcher, timeout=5)
        return seen

    try:
        seen = asyncio.run(scenario())
    finally:
        tasks.bind_loop(None)
    assert seen[-1] == "succeeded"
