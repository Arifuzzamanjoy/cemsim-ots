"""Fault injection on the real link: FreeCAD killed, restarted, GUI thread blocked.

These run on their own worker (not the shared session one) because they kill FreeCAD.
"""

import threading
import time

import pytest

from cemsim.freecad_bridge import FreeCADBridge, explain
from cemsim.freecad_worker import FreeCADWorker, find_freecad
from cemsim.sim import Simulator

pytestmark = pytest.mark.freecad


@pytest.fixture(scope="module")
def victim(tmp_path_factory):
    if find_freecad() is None:
        pytest.skip("FreeCAD not found")
    w = FreeCADWorker(home=tmp_path_factory.mktemp("victim"))  # random port: independent of the 9875 fixture
    w.start()
    yield w
    w.stop()


@pytest.fixture
def sim():
    s = Simulator()
    s.load_file("nominal")
    s.run(20)
    return s


def test_kill_then_restart_recovers_without_operator_action(victim, sim):
    b = FreeCADBridge(victim.host, victim.port, victim.token)
    b.build()
    b.push(b.state_from_sim(sim))
    assert b.diagnose()["ok"]

    victim.kill()  # SIGKILL: no clean shutdown, like an OOM kill
    d = b.diagnose()
    assert d["reachable"] is False and not d["ok"]
    assert "not reachable" in d["problems"][0]
    with pytest.raises(Exception) as e:
        b.push(b.state_from_sim(sim))
    assert "not reachable" in explain(e.value) or "refused" in str(e.value).lower()

    victim.restart_freecad()  # new FreeCAD process, same port, empty session
    assert b.diagnose()["ok"]
    assert b.DOC not in b.list_documents(), "a restarted FreeCAD must not have the old model"
    sim.run(5)
    b.push(b.state_from_sim(sim))  # notices the missing model, rebuilds, applies
    assert b.DOC in b.list_documents()
    assert b.readback()["n_objects"] >= 80


def test_blocked_gui_thread_is_reported_and_status_stays_responsive(victim):
    """A long GUI-thread task must not make the RPC layer unobservable.

    The addon documents that get_rpc_status never waits for the GUI thread; verify it, and
    that our health report names the running operation while it lasts.
    """
    c = victim.client()
    done = threading.Event()

    def long_task():
        try:
            c.execute("import time\ntime.sleep(8)\n")
        finally:
            done.set()

    t = threading.Thread(target=long_task, daemon=True)
    t.start()
    time.sleep(1.5)
    t0 = time.perf_counter()
    d = c.diagnose()
    dt = time.perf_counter() - t0
    assert dt < 2.0, f"status took {dt:.1f}s while the GUI thread was busy"
    assert d["reachable"] and d["authenticated"]
    assert d["gui_state"] in ("healthy", "busy", "running") or d["gui_operation"], d
    done.wait(timeout=30)
    assert c.diagnose()["ok"], "GUI must be healthy again after the task finished"


def test_a_display_that_dies_during_start_up_is_replaced(monkeypatch, tmp_path):
    """Seen when a worker restarts while the previous Xvfb is still shutting down: the display number
    was reported and then the X server died, so FreeCAD could never connect.  The launcher must
    notice and start a new one instead of failing (or reusing the dead display)."""
    from cemsim import freecad_worker as fw

    real = fw._start_xvfb
    shown = []

    def flaky(log):
        proc, disp = real(log)
        shown.append(disp)
        if len(shown) == 1:
            proc.kill()
            proc.wait()
        return proc, disp

    monkeypatch.setattr(fw, "_start_xvfb", flaky)
    w = FreeCADWorker(home=tmp_path / "w")
    try:
        w.start()
        assert len(shown) == 2, "a second X server must have been started"
        assert w.client().diagnose()["ok"]
    finally:
        w.stop()
