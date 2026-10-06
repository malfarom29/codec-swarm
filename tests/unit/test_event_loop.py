"""A long check must not freeze the event loop: the dashboard shares it with running missions."""

import anyio

from codec_swarm.domain import Mission
from codec_swarm.harness.config import Check
from codec_swarm.plugins.checks import ChecksOnlyJudge


def test_a_slow_check_leaves_the_event_loop_free(tmp_path):
    judge = ChecksOnlyJudge(lambda m: (tmp_path, (Check(id="slow", run="sleep 1"),)))
    ticks = 0

    async def main():
        nonlocal ticks
        async with anyio.create_task_group() as tasks:

            async def tick():
                nonlocal ticks
                while True:
                    ticks += 1
                    await anyio.sleep(0.05)

            tasks.start_soon(tick)
            await judge.evaluate(Mission(ticket="T-1", repo="r"), [])
            tasks.cancel_scope.cancel()

    anyio.run(main)
    assert ticks >= 10  # about 20 while the check sleeps; 1 if it blocked the loop
