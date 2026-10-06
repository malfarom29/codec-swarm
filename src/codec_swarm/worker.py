"""The worker process: runs missions for the dashboard, so a long step never slows a page and a dashboard restart
never stops a mission. `codec-swarm up` starts it and restarts it if it dies; `codec-swarm worker` runs it alone."""

from __future__ import annotations

import os
import signal
import sys
import uuid

import anyio

from codec_swarm.dispatch import execute
from codec_swarm.service import MissionService
from codec_swarm.store.commands import Command, CommandQueue, WorkerRunning

POLL_SECONDS = 0.5
BEAT_SECONDS = 2.0
WORKER_RUNNING_EXIT = 3
MAX_MISSIONS = 4  # missions running at once; commands for one mission always run one at a time


class Worker:
    def __init__(self, service: MissionService, queue: CommandQueue, max_missions: int = MAX_MISSIONS, parent: int | None = None) -> None:
        self.service = service
        self.queue = queue
        self._parent = parent  # the dashboard that started this worker: when it's gone, so is the worker
        self.id = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._slots = anyio.Semaphore(max_missions)

    async def run(self, stop: anyio.Event | None = None) -> None:
        stop = stop or anyio.Event()
        for command in self.queue.register(self.id):
            self._pick_up(command)
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(self._beat, stop)
            while not stop.is_set():
                await self._slots.acquire()
                command = self.queue.claim(self.id)
                if command is None:
                    self._slots.release()
                    with anyio.move_on_after(POLL_SECONDS):
                        await stop.wait()
                    continue
                tasks.start_soon(self._run, command)
            tasks.cancel_scope.cancel()  # interrupted commands are picked up again by the next worker
        self.queue.retire(self.id)

    def _pick_up(self, command: Command) -> None:
        """A previous worker died while running this. A start or a gate answer is safe to run again (an answer whose
        gate has closed since is skipped); anything else resumes the mission from its last checkpoint."""
        self.service.events.append(command.ticket, "mission.interrupted", {"command": command.kind})
        started = any(e.kind == "mission.started" for e in self.service.events.list(command.ticket))
        if command.kind == "start" and not started:
            self.queue.submit(command.ticket, "start", command.args)
        elif command.kind == "answer":
            self.queue.submit(command.ticket, "answer", {**command.args, "if_waiting": True})
        else:
            self.queue.submit(command.ticket, "recover")

    async def _run(self, command: Command) -> None:
        try:
            await execute(self.service, command.ticket, command.kind, command.args)
        except Exception as error:
            self.queue.finish(command.id, f"{type(error).__name__}: {error}")
        else:
            self.queue.finish(command.id)
        finally:
            self._slots.release()

    async def _beat(self, stop: anyio.Event) -> None:
        while not stop.is_set():
            if self._parent is not None and os.getppid() != self._parent:
                stop.set()  # orphaned: `codec-swarm up` died without stopping us; the next one starts its own worker
                return
            self.queue.beat(self.id)
            with anyio.move_on_after(BEAT_SECONDS):
                await stop.wait()


def run_worker(root: str, parent: int | None = None) -> int:
    service = MissionService(root)
    queue = CommandQueue(service.db)

    async def main() -> None:
        stop = anyio.Event()
        with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
            async with anyio.create_task_group() as tasks:

                async def on_signal() -> None:
                    async for _ in signals:
                        stop.set()
                        return

                tasks.start_soon(on_signal)
                await Worker(service, queue, parent=parent).run(stop)
                tasks.cancel_scope.cancel()
        await service.aclose()

    try:
        anyio.run(main)
    except WorkerRunning as error:
        print(f"codec-swarm worker: {error}", file=sys.stderr)
        return WORKER_RUNNING_EXIT
    return 0
