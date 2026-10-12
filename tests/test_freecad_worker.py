"""FreeCADWorker guard rails that need no FreeCAD installation."""

import socket

import pytest

from cemsim.freecad_worker import FreeCADWorker, WorkerError


def test_start_refuses_a_port_that_something_else_already_serves():
    """A second worker used to be declared healthy because the *other* process answered the ping."""
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        w = FreeCADWorker(port=port, freecad=["/nonexistent/AppRun"])
        with pytest.raises(WorkerError, match="already in use"):
            w.start()
        assert w.proc is None, "nothing may have been launched"
    w.stop()


def test_remote_workers_cannot_be_started_without_a_token_file(capsys):
    from cemsim.freecad_worker import main

    assert main(["start", "--remote"]) == 2
    assert "requires --token-file" in capsys.readouterr().err


def test_stale_recovery_data_is_cleared_inside_the_sandbox_only(tmp_path):
    """FreeCAD's recovery dialog on a persistent home wedged a worker's GUI dispatch."""
    from cemsim.freecad_worker import clear_stale_recovery

    home = tmp_path / "worker"
    stale = home / ".cache" / "FreeCAD" / "v1-1" / "Cache"
    (stale / "FreeCAD_Doc_x_123").mkdir(parents=True)
    (stale / "FreeCAD_123.lock").write_text("")
    (stale / "FreeCAD_Doc_x_123" / "fc_recovery_file.fcstd").write_bytes(b"PK")
    keep = home / ".local" / "share" / "FreeCAD" / "v1-1" / "freecad_mcp_settings.json"
    keep.parent.mkdir(parents=True)
    keep.write_text("{}")

    assert clear_stale_recovery(home) is True
    assert not (home / ".cache" / "FreeCAD").exists()
    assert keep.exists(), "settings (the token!) must survive"
    assert clear_stale_recovery(home) is False  # nothing left to do

    # a cache directory that is a symlink out of the sandbox must not be followed
    outside = tmp_path / "outside"
    (outside / "important").mkdir(parents=True)
    other = tmp_path / "other"
    (other / ".cache").mkdir(parents=True)
    (other / ".cache" / "FreeCAD").symlink_to(outside)
    assert clear_stale_recovery(other) is False
    assert (outside / "important").exists()
