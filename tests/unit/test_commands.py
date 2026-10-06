"""The command queue between the dashboard and the worker."""

from codec_swarm.store.commands import CommandQueue


def test_commands_for_one_mission_run_one_at_a_time_in_order(tmp_path):
    q = CommandQueue(tmp_path / "swarm.db")
    a1 = q.submit("A", "start", {"x": 1})
    a2 = q.submit("A", "answer", {"answer": "approve"})
    b1 = q.submit("B", "start")
    first = q.claim("w")
    assert first.id == a1 and first.args == {"x": 1}
    assert q.claim("w").id == b1  # A has a command running, so its next one waits
    assert q.claim("w") is None
    q.finish(a1)
    assert q.claim("w").id == a2
    assert q.is_busy("A") and q.is_busy("B")
    q.finish(a2, "ValueError: boom")
    q.finish(b1)
    assert not q.is_busy("A") and q.get(a2).status == "failed" and q.get(a2).error == "ValueError: boom"


def test_a_new_worker_marks_what_a_dead_one_left_running(tmp_path):
    q = CommandQueue(tmp_path / "swarm.db")
    q.submit("A", "start")
    running = q.claim("old")
    assert [(c.ticket, c.kind) for c in q.register("new")] == [("A", "start")]
    assert q.get(running.id).status == "interrupted"
    assert q.worker_alive()
    q.retire("new")
    assert not q.worker_alive()


def test_a_second_live_worker_is_refused(tmp_path):
    import os
    import subprocess
    import sys

    import pytest

    from codec_swarm.store.commands import WorkerRunning

    q = CommandQueue(tmp_path / "swarm.db")
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        q._conn.execute("INSERT INTO workers (id, pid) VALUES (?, ?)", ("other", other.pid))
        with pytest.raises(WorkerRunning):
            q.register("me")
    finally:
        other.kill()
        other.wait()
    assert q.register("me") == []  # the other one is gone now
    assert os.getpid() in [r[0] for r in q._conn.execute("SELECT pid FROM workers")]
