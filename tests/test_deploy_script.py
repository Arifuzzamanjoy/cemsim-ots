"""scripts/deploy.sh against a throw-away git origin, with stubbed systemctl/sudo and a local health URL.

Nothing here touches a real service: the point is to prove the safety properties of the deploy path
(input validation, only-main, dirty tree, test gate, automatic rollback) before they are trusted
on a server.
"""

import http.server
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}

# scripts/deploy.sh is a Linux-server script (bash, flock, systemd conventions); macOS has no flock and
# Windows has no usable /bin/sh for the stubs, so these tests run only where the script can run.
pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or any(shutil.which(t) is None for t in ("git", "curl", "flock", "bash")),
    reason="deploy.sh needs Linux with git, curl, flock and bash",
)


def git(cwd, *args, check=True):
    r = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env={**os.environ, **GIT_ENV}, check=False
    )
    if check and r.returncode:
        raise AssertionError(f"git {args}: {r.stderr}")
    return r.stdout.strip()


class Rig:
    """`dev` authors commits and pushes to `origin`; `work` is the server checkout that only fetches."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.origin, self.dev, self.work = tmp / "origin.git", tmp / "dev", tmp / "work"
        git(tmp, "init", "--bare", "-b", "main", str(self.origin))
        git(tmp, "clone", "-q", str(self.origin), str(self.dev))
        self.commit("A", {"VERSION": "A", "scripts/deploy.sh": SCRIPT.read_text()})
        git(self.dev, "push", "-q", "-u", "origin", "main")
        git(tmp, "clone", "-q", str(self.origin), str(self.work))
        # stubs: systemctl logs its calls; sudo just runs its arguments (the script uses sudo -n when not root)
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.log = tmp / "systemctl.log"
        (self.bin / "systemctl").write_text(f'#!/bin/sh\necho "$@" >> {self.log}\nexit 0\n')
        (self.bin / "sudo").write_text('#!/bin/sh\n[ "$1" = "-n" ] && shift\nexec "$@"\n')
        for f in self.bin.iterdir():
            f.chmod(0o755)

        rig = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                bad = (rig.work / "BAD").exists()
                self.send_response(503 if bad else 200)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/health"

    def commit(self, msg, files: dict[str, str], push=False) -> str:
        for name, content in files.items():
            p = self.dev / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-q", "-m", msg)
        sha = git(self.dev, "rev-parse", "HEAD")
        if push:
            git(self.dev, "push", "-q", "origin", "HEAD:main")
        return sha

    def run(self, request: str, **env):
        e = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "DUOSIM_DEPLOY_SYNC_CMD": "true",
            "DUOSIM_DEPLOY_TEST_CMD": "test ! -e TESTFAIL",
            "DUOSIM_UNITS": "worker.service app.service",
            "DUOSIM_HEALTH_URLS": self.url,
            "DUOSIM_HEALTH_TIMEOUT": "4",
            **env,
        }
        r = subprocess.run(
            ["bash", str(self.work / "scripts" / "deploy.sh"), *([request] if request else [])],
            cwd=self.work, capture_output=True, text=True, env=e, check=False,
        )  # fmt: skip
        return r

    def head(self):
        return git(self.work, "rev-parse", "HEAD")

    def restarts(self):
        return (
            [ln.split()[-1] for ln in self.log.read_text().splitlines() if ln.startswith("restart")]
            if self.log.exists()
            else []
        )

    def history(self):
        p = self.work / ".duosim" / "deploy" / "history.log"
        return p.read_text() if p.exists() else ""


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.srv.shutdown()


def test_deploys_a_commit_restarts_in_order_and_records_it(rig):
    a = rig.head()
    b = rig.commit("B", {"VERSION": "B"}, push=True)
    r = rig.run(f"deploy {b}")
    assert r.returncode == 0, r.stdout + r.stderr
    assert rig.head() == b and (rig.work / "VERSION").read_text() == "B"
    assert rig.restarts() == ["worker.service", "app.service"], "the FreeCAD worker restarts before the app"
    assert "OK" in rig.history()
    assert (rig.work / ".duosim/deploy/previous").read_text().strip() == a


@pytest.mark.parametrize(
    "request_text",
    [
        "",
        "deploy",
        "deploy abc",
        "deploy " + "g" * 40,
        "deploy $(touch /tmp/pwned)",
        "deploy; id",
        "rm -rf /",
        "deploy " + "a" * 40 + " extra",
    ],
)
def test_malformed_or_hostile_requests_do_nothing(rig, request_text):
    before = rig.head()
    r = rig.run(request_text)
    assert r.returncode != 0
    assert rig.head() == before and rig.restarts() == []
    assert not Path("/tmp/pwned").exists()


def test_the_forced_command_form_works_and_ignores_argv(rig):
    b = rig.commit("B", {"VERSION": "B"}, push=True)
    r = rig.run("", SSH_ORIGINAL_COMMAND=f"deploy {b}")
    assert r.returncode == 0 and rig.head() == b


def test_only_commits_on_main_can_be_deployed(rig):
    git(rig.dev, "checkout", "-q", "-b", "feature")
    x = rig.commit("X", {"VERSION": "X"})
    git(rig.dev, "push", "-q", "origin", "feature")
    before = rig.head()
    r = rig.run(f"deploy {x}")
    assert r.returncode == 7 and "not on origin/main" in r.stderr + r.stdout
    assert rig.head() == before and rig.restarts() == []


def test_a_dirty_working_tree_is_never_overwritten(rig):
    b = rig.commit("B", {"VERSION": "B"}, push=True)
    (rig.work / "VERSION").write_text("local edit")
    r = rig.run(f"deploy {b}")
    assert r.returncode == 4 and (rig.work / "VERSION").read_text() == "local edit"
    assert rig.restarts() == []


def test_a_failing_test_gate_stops_before_any_restart(rig):
    good = rig.head()
    bad = rig.commit("D", {"TESTFAIL": "x"}, push=True)
    r = rig.run(f"deploy {bad}")
    assert r.returncode == 9
    assert rig.head() == good, "back on the previous commit"
    assert rig.restarts() == [], "no service was touched"
    assert "TESTS-FAILED" in rig.history()


def test_an_unhealthy_deploy_is_rolled_back_automatically(rig):
    b = rig.commit("B", {"VERSION": "B"}, push=True)
    assert rig.run(f"deploy {b}").returncode == 0
    rig.log.write_text("")
    c = rig.commit("C", {"BAD": "this version cannot serve"}, push=True)
    r = rig.run(f"deploy {c}")
    assert r.returncode == 1 and "rolled back" in r.stderr + r.stdout
    assert rig.head() == b and not (rig.work / "BAD").exists()
    assert rig.restarts() == ["worker.service", "app.service"] * 2, (
        "restarted for the deploy and again for the rollback"
    )
    assert "ROLLED-BACK" in rig.history()


def test_manual_rollback_returns_to_the_previous_commit(rig):
    a = rig.head()
    b = rig.commit("B", {"VERSION": "B"}, push=True)
    assert rig.run(f"deploy {b}").returncode == 0
    r = rig.run("rollback")
    assert r.returncode == 0 and rig.head() == a
    assert rig.run("rollback").returncode == 0 and rig.head() == b, "rollback toggles between the last two"


def test_missing_units_are_skipped_not_fatal(rig, tmp_path):
    (rig.bin / "systemctl").write_text(f'#!/bin/sh\n[ "$1" = "cat" ] && exit 1\necho "$@" >> {rig.log}\nexit 0\n')
    b = rig.commit("B", {"VERSION": "B"}, push=True)
    r = rig.run(f"deploy {b}")
    assert r.returncode == 0 and rig.restarts() == []
    assert "not installed here" in r.stdout + r.stderr
