"""The CD workflow's shell steps, executed for real with fake secrets and a fake `ssh`.

A deploy that fails with ssh's usage text because a secret was empty is how this test came to exist:
the configuration check must fail early with a clear message, and the ssh step must build exactly the
command the server's restricted key expects.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

CD = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "cd.yml"
SHA = "a" * 40
GOOD = {
    "DEPLOY_HOST": "203.0.113.7",
    "DEPLOY_USER": "root",
    "DEPLOY_SSH_KEY": "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----",
    "DEPLOY_KNOWN_HOSTS": "203.0.113.7 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItest",
}

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="runs the steps with bash on Linux")


def step(name: str) -> dict:
    wf = yaml.safe_load(CD.read_text())
    return next(s for s in wf["jobs"]["deploy"]["steps"] if s["name"].startswith(name))


def run(script: str, env: dict, tmp: Path, **extra):
    return subprocess.run(
        ["bash", "-c", script],
        env={"PATH": os.environ["PATH"], "HOME": str(tmp), **env, **extra},
        capture_output=True, text=True, check=False,
    )  # fmt: skip


def test_configuration_check_passes_with_a_complete_environment(tmp_path):
    r = run(step("Check the deployment configuration")["run"], GOOD, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("empty", ["DEPLOY_HOST", "DEPLOY_USER", "DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS"])
def test_a_missing_secret_is_named_and_explained_not_a_cryptic_ssh_error(tmp_path, empty):
    r = run(step("Check the deployment configuration")["run"], {**GOOD, empty: ""}, tmp_path)
    assert r.returncode == 1
    out = r.stdout + r.stderr
    assert empty in out and "environment named exactly 'production'" in out
    assert "usage: ssh" not in out


def test_the_exact_failure_seen_in_production_every_secret_empty(tmp_path):
    """All four empty = the job ran in an environment that has none of the secrets."""
    r = run(step("Check the deployment configuration")["run"], dict.fromkeys(GOOD, ""), tmp_path)
    assert r.returncode == 1
    for name in GOOD:
        assert name in r.stdout


def test_shape_mistakes_are_caught(tmp_path):
    check = step("Check the deployment configuration")["run"]
    pub = run(check, {**GOOD, "DEPLOY_SSH_KEY": "ssh-ed25519 AAAA cemsim-cd"}, tmp_path)
    assert pub.returncode == 1 and "not a private key" in pub.stdout
    wrong = run(check, {**GOOD, "DEPLOY_KNOWN_HOSTS": "198.51.100.1 ssh-ed25519 AAAA"}, tmp_path)
    assert wrong.returncode == 1 and "does not mention DEPLOY_HOST" in wrong.stdout


def test_the_ssh_step_builds_the_command_the_restricted_key_expects(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    argv_log = tmp_path / "argv"
    (bindir / "ssh").write_text(textwrap.dedent(f"""\
        #!/bin/bash
        printf '%s\\n' "$@" > {argv_log}
        cp "${{HOME}}/.ssh/deploy_key" {tmp_path}/key_seen
        cp "${{HOME}}/.ssh/known_hosts" {tmp_path}/hosts_seen
        stat -c %a "${{HOME}}/.ssh/deploy_key" > {tmp_path}/mode_seen
    """))  # fmt: skip
    (bindir / "ssh").chmod(0o755)
    env = {**GOOD, "SHA": SHA, "DEPLOY_PORT": "22", "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    r = run(step("Deploy over SSH")["run"], env, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    argv = argv_log.read_text().split("\n")
    assert "root@203.0.113.7" in argv, argv
    assert argv[-2] == f"deploy {SHA}", "the forced command receives `deploy <sha>`"
    assert "StrictHostKeyChecking=yes" in argv and "BatchMode=yes" in argv and "IdentitiesOnly=yes" in argv
    assert (tmp_path / "mode_seen").read_text().strip() == "600"
    assert (tmp_path / "key_seen").read_text().startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert (tmp_path / "hosts_seen").read_text().startswith("203.0.113.7 ssh-ed25519")
