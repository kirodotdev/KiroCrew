"""Two sessions on one install each see only their own folder, under the real sandbox.

An edition keeps state in one folder per session under a shared root. Its
``SandboxPolicy.session_masks`` hides the root and hands each session its own folder
back read-only. Each test resolves that answer through the launch tail's resolver, wraps
a child with the real backend (``wrap_argv`` picks it), and runs one child per session.
The test process writes a file into each session's folder only after the child is
running, so the read-only view is proved LIVE rather than a copy taken at spawn.

Linux needs unprivileged user and mount namespaces, which the namespace-sandbox CI job
provides; macOS needs a process that can apply a Seatbelt profile, which a process
already inside one cannot. Elsewhere these tests skip with that reason.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import sandbox
from kiro_crew.platform import context as context_mod

pytestmark = [
    pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="no sandbox applies masks"),
    pytest.mark.xdist_group("session_masks_real_sandbox"),
]

_SESSIONS = ("alpha", "beta")

# Reports it started, waits for the late write, then reads and writes every path.
_CHILD = r"""
import json, os, sys
print("READY", flush=True)
sys.stdin.readline()
own, other = sys.argv[1], sys.argv[2]
seen = {}
for name, path in (
    ("own_secret", os.path.join(own, "secret")),
    ("own_late", os.path.join(own, "late")),
    ("other_secret", os.path.join(other, "secret")),
    ("other_late", os.path.join(other, "late")),
):
    try:
        seen[name] = {"read": open(path, encoding="utf-8").read()}
    except OSError as exc:
        seen[name] = {"errno": exc.errno}
for name, path in (("write_own", os.path.join(own, "planted")), ("rewrite_own", os.path.join(own, "secret"))):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("x")
        seen[name] = {"wrote": True}
    except OSError as exc:
        seen[name] = {"errno": exc.errno}
print(json.dumps(seen), flush=True)
"""


def _require_backend() -> None:
    if sys.platform == "linux":
        if not sandbox.userns_available():
            pytest.skip("this host cannot build the namespace sandbox")
        return
    probe = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("already inside a sandbox: sandbox_apply is refused")


@pytest.fixture
def state_root(tmp_path, monkeypatch) -> Path:
    home = (tmp_path / "home").resolve()
    crew = home / ".kiro" / "crew"
    crew.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    sandbox.reset_backend()
    root = crew / "edition-sessions"
    for session in _SESSIONS:
        (root / session).mkdir(parents=True)
        (root / session / "secret").write_text(f"secret-{session}", encoding="utf-8")
    yield root
    sandbox.reset_backend()


def _install_policy(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    from kiro_crew.platform.interfaces import SessionMasks

    class _PerSessionFolders:
        def session_masks(self, identity):
            return SessionMasks(
                hidden_dirs=(str(root),), readonly_dirs=(str(root / identity.session_key),)
            )

    monkeypatch.setattr(
        context_mod, "current_context", lambda: SimpleNamespace(sandbox=_PerSessionFolders())
    )


def _run_session(root: Path, session: str, other: str) -> dict:
    from kiro_crew.acp.launch import resolve_session_masks
    from kiro_crew.platform.interfaces import SpawnIdentity

    hidden, readonly = resolve_session_masks(SpawnIdentity(session_key=session), "standard")
    argv, cleanup = sandbox.wrap_argv(
        [sys.executable, "-c", _CHILD, str(root / session), str(root / other)],
        "standard",
        extra_hidden_dirs=hidden,
        extra_readonly_windows=readonly,
    )
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
    proc = subprocess.Popen(
        argv,
        cwd=root.parent,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert proc.stdout is not None and proc.stdin is not None
        line = proc.stdout.readline()
        assert line.strip() == "READY", (line, proc.stderr.read() if proc.stderr else "")
        # Written by the host AFTER the child started: a copy taken at spawn misses it.
        for name in _SESSIONS:
            (root / name / "late").write_text(f"late-{name}", encoding="utf-8")
        proc.stdin.write("go\n")
        proc.stdin.flush()
        report = json.loads(proc.stdout.readline())
    finally:
        if proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
        proc.wait(timeout=30)
        for name in _SESSIONS:
            (root / name / "late").unlink(missing_ok=True)
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)
    return report


def test_two_sessions_each_read_only_their_own_live_folder(state_root, monkeypatch) -> None:
    _require_backend()
    _install_policy(monkeypatch, state_root)

    for session, other in (("alpha", "beta"), ("beta", "alpha")):
        report = _run_session(state_root, session, other)
        assert report["own_secret"] == {"read": f"secret-{session}"}, report
        assert report["own_late"] == {"read": f"late-{session}"}, report
        assert "read" not in report["other_secret"], report
        assert "read" not in report["other_late"], report
        assert "wrote" not in report["write_own"], report
        assert "wrote" not in report["rewrite_own"], report
        # Nothing the child tried landed on the host.
        assert not (state_root / session / "planted").exists()
        assert (state_root / session / "secret").read_text(encoding="utf-8") == f"secret-{session}"
