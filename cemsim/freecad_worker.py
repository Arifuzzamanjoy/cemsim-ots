"""Run a real FreeCAD (GUI build on a virtual display) with the FreeCAD-MCP addon, headless.

Why not `freecadcmd`?  The addon's RPC server imports FreeCADGui + Qt and ferries every
request onto the GUI thread through a Qt timer, so it needs the GUI build.  On a server that
means: a virtual X display (Xvfb) and software OpenGL (Mesa), which also makes screenshots
(`get_active_screenshot`) work.

What a worker is:

    sandbox HOME/                       one per worker, so workers never share settings/state
      .local/share/FreeCAD/v1-1/
        Mod/FreeCADMCP/                 the vendored addon (third_party/freecad-mcp)
        freecad_mcp_settings.json       auth token, allowed IPs (0600)
      start_rpc.py                      startup macro: starts the RPC server on *our* port
      rpc_token                         the token (0600)
      freecad.log

The addon's own auto-start is hard-wired to port 9875, so a startup macro starts the server
on the requested port instead (several workers can then share a host).

    python -m cemsim.freecad_worker start --port 9875        # foreground; Ctrl-C / SIGTERM stops
    python -m cemsim.freecad_worker find                      # where is FreeCAD?

Library use (tests, the platform's worker pool):

    with FreeCADWorker() as w:
        client = FreeCADClient("127.0.0.1", w.port, w.token)
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import secrets
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .freecad_bridge import FreeCADClient

REPO = Path(__file__).resolve().parent.parent
ADDON_SRC = REPO / "third_party" / "freecad-mcp" / "addon" / "FreeCADMCP"
ENV_ROOT = "CEMSIM_FREECAD"  # path to an extracted AppImage dir (has AppRun), or to a freecad executable

START_MACRO = """\
import FreeCAD
from PySide import QtCore


def _start():
    try:
        # a worker has no human to answer FreeCAD's "Document Recovery" dialog, which would block the
        # RPC dispatch; so never create recovery files (stale ones are removed before launch)
        FreeCAD.ParamGet("User parameter:BaseApp/Preferences/Document").SetBool("AutoSaveEnabled", False)
        from rpc_server import rpc_server

        FreeCAD.Console.PrintMessage("[cemsim-worker] " + rpc_server.start_rpc_server(port=%(port)d) + "\\n")
    except Exception as e:
        FreeCAD.Console.PrintError("[cemsim-worker] RPC start failed: %%r\\n" %% (e,))


QtCore.QTimer.singleShot(500, _start)
"""


log = logging.getLogger("cemsim.freecad_worker")


class WorkerError(RuntimeError):
    """The worker could not be started or did not become healthy."""


# ----------------------------------------------------------------- discovery
def find_freecad() -> list[str] | None:
    """Command prefix that launches the FreeCAD GUI build, or None.

    Order: $CEMSIM_FREECAD, /opt/freecad-*/AppRun (newest first), ~/freecad*/AppRun, PATH.
    """
    env = os.environ.get(ENV_ROOT)
    cands: list[Path] = []
    if env:
        p = Path(env).expanduser()
        cands += [p / "AppRun"] if p.is_dir() else [p]
    cands += sorted(Path("/opt").glob("freecad*/AppRun"), reverse=True)
    cands += sorted(Path.home().glob("freecad*/AppRun"), reverse=True)
    for c in cands:
        if c.is_file() and os.access(c, os.X_OK):
            # AppRun dispatches on its first argument: "freecad" = GUI, "freecadcmd" = console
            return [str(c), "freecad"] if c.name == "AppRun" else [str(c)]
    for name in ("FreeCAD", "freecad"):
        w = shutil.which(name)
        if w:
            return [w]
    return None


def _console_variant(gui_cmd: list[str]) -> list[str]:
    if Path(gui_cmd[0]).name == "AppRun":
        return [gui_cmd[0], "freecadcmd"]
    return [str(Path(gui_cmd[0]).with_name("freecadcmd"))]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_xvfb(log) -> tuple[subprocess.Popen, str]:
    """Start Xvfb on a free display; returns (process, ':N')."""
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        raise WorkerError("Xvfb not found (apt install xvfb), or pass display=':N' to use an existing X server")
    r, w = os.pipe()
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv
            [xvfb, "-displayfd", str(w), "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
            pass_fds=(w,),
            stdout=log,
            stderr=log,
        )
    finally:
        os.close(w)
    try:
        ready, _, _ = select.select([r], [], [], 15.0)
        line = os.read(r, 64).decode().strip() if ready else ""
    finally:
        os.close(r)
    if not line.isdigit():
        proc.kill()
        raise WorkerError("Xvfb did not report a display number")
    return proc, f":{line}"


def _wait_x(display: str, timeout: float = 15.0, server: subprocess.Popen | None = None) -> bool:
    """Wait until the X server on `display` accepts connections; False if it never does (unix sockets)
    or if `server` (the Xvfb we started) has already exited."""
    num = display.rpartition(":")[2].split(".")[0]
    path = f"/tmp/.X11-unix/X{num}"  # noqa: S108 - the well-known X11 socket we connect to, not a temp file
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server is not None and server.poll() is not None:
            return False
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(1.0)
                s.connect(path)
                return True
        except OSError:
            time.sleep(0.1)
    return False


def clear_stale_recovery(home: Path) -> bool:
    """Remove FreeCAD's cache (lock files + crash-recovery documents) from a worker sandbox.

    If a previous FreeCAD in this home did not exit cleanly, the next start shows a modal "Document
    Recovery" dialog; nobody can answer it on a server and the RPC dispatch then never runs.
    Only ever touches `<home>/.cache/FreeCAD`.  Returns True if something was removed.
    """
    cache = Path(home) / ".cache" / "FreeCAD"
    if not cache.is_dir() or not cache.resolve().is_relative_to(Path(home).resolve()):
        return False
    shutil.rmtree(cache, ignore_errors=True)
    return True


# ------------------------------------------------------------------ worker
class FreeCADWorker:
    """One FreeCAD GUI process + virtual display + addon RPC server.

    Parameters
    ----------
    port        RPC port (default: a free one)
    token       auth token (default: a fresh random one); always set, even on loopback
    home        sandbox directory (default: a fresh temp dir, removed on stop)
    display     ':N' to use an existing X server; default starts a private Xvfb
    remote      bind 0.0.0.0 and accept `allowed_ips` (needs the token, which is always set)
    """

    def __init__(
        self,
        port: int | None = None,
        token: str | None = None,
        home: str | Path | None = None,
        display: str | None = None,
        freecad: list[str] | None = None,
        remote: bool = False,
        allowed_ips: str = "127.0.0.1",
        startup_timeout: float = 120.0,
    ):
        self.port = port or free_port()
        self.token = token or secrets.token_urlsafe(32)
        self._own_home = home is None
        self.home = Path(home) if home else Path(tempfile.mkdtemp(prefix="cemsim-fc-"))
        self.display = display
        self.freecad = freecad or find_freecad()
        self.remote, self.allowed_ips = remote, allowed_ips
        self.startup_timeout = startup_timeout
        self.proc: subprocess.Popen | None = None
        self._xvfb: subprocess.Popen | None = None
        self._log = None
        self.version = ""
        self.user_data: Path | None = None

    # -- paths ---------------------------------------------------------
    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def log_path(self) -> Path:
        return self.home / "freecad.log"

    @property
    def token_path(self) -> Path:
        return self.home / "rpc_token"

    def client(self) -> FreeCADClient:
        return FreeCADClient(self.host, self.port, self.token)

    def log_tail(self, n: int = 25) -> str:
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-n:])
        except OSError:
            return ""

    # -- lifecycle -----------------------------------------------------
    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
        run = self.home / "run"
        run.mkdir(mode=0o700, exist_ok=True)
        env.update(HOME=str(self.home), LIBGL_ALWAYS_SOFTWARE="1", XDG_RUNTIME_DIR=str(run))
        env.pop("FREECAD_MCP_TOKEN", None)  # the worker is configured via its settings file only
        return env

    def _probe_userdata(self, env: dict[str, str]) -> None:
        """Ask the console build for its version and per-user data dir (differs per release)."""
        code = "import FreeCAD; print('CEMSIM_PROBE', '.'.join(FreeCAD.Version()[:3]), FreeCAD.getUserAppDataDir())"
        r = subprocess.run(  # noqa: S603 - argv from find_freecad()
            [*_console_variant(self.freecad), "-c", code],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        for ln in (r.stdout + r.stderr).splitlines():
            if ln.startswith("CEMSIM_PROBE"):
                _, ver, ud = ln.split(" ", 2)
                self.version, self.user_data = ver, Path(ud.strip())
                return
        raise WorkerError(f"could not probe FreeCAD ({self.freecad}): {(r.stderr or r.stdout)[-300:]}")

    def start(self) -> FreeCADWorker:
        if self.proc is not None:
            return self
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex((self.host, self.port)) == 0:
                # not only unfriendly: _wait_ready would otherwise be answered by *that* process and
                # report a worker that never bound its own RPC server as healthy
                raise WorkerError(f"port {self.port} is already in use by another process; stop it or choose a port")
        if not self.freecad:
            raise WorkerError(
                f"FreeCAD not found. Install FreeCAD >= 1.0 (AppImage) and set {ENV_ROOT}=<extracted dir> "
                "- see docs/FREECAD_INTEGRATION.md"
            )
        if not ADDON_SRC.is_dir():
            raise WorkerError(f"vendored addon missing: {ADDON_SRC}")
        self.home.mkdir(parents=True, exist_ok=True)
        env = self._env()
        self._probe_userdata(env)
        assert self.user_data is not None
        mod = self.user_data / "Mod"
        mod.mkdir(parents=True, exist_ok=True)
        dst = mod / "FreeCADMCP"
        if dst.exists():
            if not dst.resolve().is_relative_to(self.home.resolve()):  # never delete outside the sandbox
                raise WorkerError(f"refusing to replace addon outside the sandbox: {dst}")
            shutil.rmtree(dst)
        shutil.copytree(ADDON_SRC, dst, ignore=shutil.ignore_patterns("__pycache__"))

        settings = {
            "remote_enabled": bool(self.remote),
            "allowed_ips": self.allowed_ips,
            "auto_start_rpc": False,  # our macro starts it on the requested port
            "auth_token": self.token,
        }
        sp = self.user_data / "freecad_mcp_settings.json"
        sp.write_text(json.dumps(settings))
        sp.chmod(0o600)
        self.token_path.write_text(self.token)
        self.token_path.chmod(0o600)
        macro = self.home / "start_rpc.py"
        macro.write_text(START_MACRO % {"port": self.port})
        clear_stale_recovery(self.home)

        self._log = open(self.log_path, "ab")  # noqa: SIM115 - lives as long as the process
        own_display = self.display is None
        try:
            for attempt in (1, 2, 3):
                # A display we own can die between "reported its number" and FreeCAD connecting (seen when a
                # worker is restarted while the previous Xvfb is still shutting down): make a fresh one.
                if own_display and (self._xvfb is None or self._xvfb.poll() is not None):
                    self._xvfb, self.display = _start_xvfb(self._log)
                assert self.display is not None
                env["DISPLAY"] = self.display
                if not _wait_x(self.display, server=self._xvfb if own_display else None):
                    if attempt == 3:
                        raise WorkerError(f"the X server on {self.display} never accepted connections")
                    if own_display and self._xvfb is not None:
                        self._xvfb.kill()
                        self._xvfb.wait(timeout=5)
                        self._xvfb = None
                    continue
                self.proc = subprocess.Popen(  # noqa: S603 - argv from find_freecad()
                    [*self.freecad, str(macro)],
                    env=env,
                    stdout=self._log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,  # own process group: stop() reaps FreeCAD's children too
                )
                try:
                    self._wait_ready()
                    break
                except WorkerError as e:
                    died_early = self.proc.poll() is not None
                    if attempt == 3 or not died_early:
                        raise
                    log.warning(
                        "FreeCAD exited during start-up (attempt %d), retrying: %s", attempt, str(e).splitlines()[0]
                    )
                    self.proc = None
        except BaseException:
            self.stop()
            raise
        return self

    def _wait_ready(self) -> None:
        c = self.client()
        deadline = time.monotonic() + self.startup_timeout
        last = ""
        while time.monotonic() < deadline:
            if self.proc is None or self.proc.poll() is not None:
                raise WorkerError(
                    f"FreeCAD exited during start-up (code {self.proc and self.proc.returncode})\n" + self.log_tail()
                )
            try:
                if c.ping():
                    st = c.rpc_status()
                    if (st.get("gui_dispatch") or {}).get("state") == "healthy":
                        self._check_identity(c)
                        return
                    last = f"gui_dispatch={st.get('gui_dispatch')}"
            except Exception as e:  # not listening yet / GUI still loading
                last = f"{type(e).__name__}: {e}"
            time.sleep(0.5)
        raise WorkerError(
            f"FreeCAD RPC server not healthy after {self.startup_timeout:.0f}s ({last})\n" + self.log_tail()
        )

    def _check_identity(self, c: FreeCADClient) -> None:
        """The RPC server must belong to the FreeCAD we started (it is that process or its child:
        AppRun is a wrapper script), not a stale instance that happened to hold the port."""
        assert self.proc is not None
        out = c.execute("import os; print(os.getpid(), os.getppid())").split()
        pid, ppid = int(out[0]), int(out[1])
        if self.proc.pid not in (pid, ppid):
            raise WorkerError(
                f"port {self.port} is served by a different FreeCAD (pid {pid}), not the one just started "
                f"(pid {self.proc.pid}): a previous worker is still running"
            )

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self) -> None:
        """Hard-kill FreeCAD only (fault injection: the Xvfb and sandbox stay)."""
        if self.proc is not None and self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            self.proc.wait(timeout=10)

    def restart_freecad(self) -> None:
        """Start a fresh FreeCAD in the same sandbox/display/port (after kill())."""
        self.kill()
        assert self.display is not None
        env = self._env()
        env["DISPLAY"] = self.display
        self.proc = subprocess.Popen(  # noqa: S603 - argv from find_freecad()
            [*self.freecad, str(self.home / "start_rpc.py")],
            env=env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._wait_ready()

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                self.proc.wait(timeout=10)
        self.proc = None
        if self._xvfb is not None:
            self._xvfb.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._xvfb.wait(timeout=5)
            self._xvfb = None
        if self._log is not None:
            self._log.close()
            self._log = None
        if self._own_home:
            shutil.rmtree(self.home, ignore_errors=True)

    def __enter__(self) -> FreeCADWorker:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


# --------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cemsim.freecad_worker", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("find", help="print the FreeCAD command that would be used")
    st = sub.add_parser("start", help="start a worker in the foreground")
    st.add_argument("--port", type=int, default=9875)
    st.add_argument("--home", default="", help="sandbox dir (default: .cemsim/freecad-worker/<port> in the repo)")
    st.add_argument("--token-file", default="", help="read the auth token from this file (default: generate)")
    st.add_argument("--display", default="", help="use this X display instead of starting Xvfb")
    st.add_argument("--remote", action="store_true", help="listen on 0.0.0.0 (token + --allow required)")
    st.add_argument("--allow", default="127.0.0.1", help="comma-separated IPs/CIDRs allowed with --remote")
    st.add_argument("--print-token", action="store_true", help="print the token (default: only its file path)")
    a = ap.parse_args(argv)

    if a.cmd == "find":
        cmd = find_freecad()
        print(" ".join(cmd) if cmd else "FreeCAD not found", file=sys.stderr if not cmd else sys.stdout)
        return 0 if cmd else 1

    if a.remote and not a.token_file:
        print(
            "error: --remote requires --token-file (a network-reachable addon without a token is remote code execution)",
            file=sys.stderr,
        )
        return 2
    token = Path(a.token_file).read_text().strip() if a.token_file else None
    if a.remote and not token:
        print("error: the token file is empty", file=sys.stderr)
        return 2
    home = Path(a.home) if a.home else REPO / ".cemsim" / "freecad-worker" / str(a.port)
    w = FreeCADWorker(
        port=a.port, token=token, home=home, display=a.display or None, remote=a.remote, allowed_ips=a.allow
    )
    try:
        w.start()
    except WorkerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    info = {
        "url": f"http://{w.host}:{w.port}",
        "freecad": w.version,
        "display": w.display,
        "token_file": str(w.token_path),
    }
    if a.print_token:
        info["token"] = w.token
    print(json.dumps(info), flush=True)

    stop = {"now": False}
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.update(now=True))
    while not stop["now"] and w.alive():
        time.sleep(0.5)
    code = 0 if stop["now"] else 2
    if code:
        print("FreeCAD exited unexpectedly:\n" + w.log_tail(), file=sys.stderr)
    w.stop()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
