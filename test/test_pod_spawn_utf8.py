"""The pod backends decode journalctl and launchctl output as UTF-8.

``runtime.recent_journal`` (``journalctl``, the boot-failure tail) and
``launchd.launchctl`` (every launchd call) used ``text=True`` with no
``encoding=``, so the gateway decoded the child's output with
``locale.getpreferredencoding(False)``. Both tools write UTF-8: the journal stores
UTF-8 and journalctl prints it raw, and launchctl prints labels and plist paths as
UTF-8. Under a non-UTF-8 gateway locale a pod path such as ``/opt/projet-José/...``
came back as mojibake, or raised ``UnicodeDecodeError`` out of the very call that
explains why a pod failed to boot.

The spy tests pin the decode on each spawn; the real-pipe test proves the pinned
kwargs decode a UTF-8 stream correctly on any host locale.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from kiro_crew.pod import launchd
from kiro_crew.pod import runtime as rt
from kiro_crew.pod.config import PodConfig

# The real spawn primitive, captured before any test patches
# ``subprocess.run`` (the stubs patch the shared module attribute).
_REAL_RUN = subprocess.run

# What a failed boot logs for a worktree under a non-ASCII directory.
_NON_ASCII = "FATAL: no built dist under /opt/projet-José/wörk/ü"


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> PodConfig:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
    monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods-env"))
    monkeypatch.setenv("KIROCREW_POD_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    return PodConfig.load()


def _assert_utf8_decode(kwargs: dict) -> None:
    # Report only the decode keys: the journalctl spawn's kwargs carry env=.
    decode = {k: kwargs.get(k) for k in ("text", "encoding", "errors")}
    assert decode["encoding"] == "utf-8", decode
    assert decode["errors"] == "replace", decode


def _journalctl_kwargs(cfg: PodConfig, monkeypatch) -> dict:
    seen: list[dict] = []

    def fake_run(argv, **kwargs):
        assert argv[0] == "journalctl"
        seen.append(kwargs)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=_NON_ASCII + "\n")

    with monkeypatch.context() as mp:
        mp.setattr(rt, "IS_MACOS", False)
        mp.setattr(rt, "IS_WINDOWS", False)
        mp.setattr(rt, "require_systemd", lambda: None)
        mp.setattr(rt.subprocess, "run", fake_run)
        assert rt.recent_journal(cfg, "smoke", 30) == _NON_ASCII + "\n"
    assert len(seen) == 1
    return seen[0]


def _launchctl_kwargs(monkeypatch) -> dict:
    seen: list[dict] = []

    def fake_run(argv, **kwargs):
        assert argv[0] == "launchctl"
        seen.append(kwargs)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    with monkeypatch.context() as mp:
        mp.setattr(launchd, "require_backend", lambda: None)
        mp.setattr(launchd.subprocess, "run", fake_run)
        launchd.launchctl("print", "gui/501")
    assert len(seen) == 1
    return seen[0]


def test_journalctl_tail_decodes_utf8(cfg, monkeypatch):
    _assert_utf8_decode(_journalctl_kwargs(cfg, monkeypatch))


def test_launchctl_decodes_utf8(monkeypatch):
    _assert_utf8_decode(_launchctl_kwargs(monkeypatch))


@pytest.mark.parametrize("spawn", ["journalctl", "launchctl"])
def test_pinned_kwargs_decode_a_real_utf8_stdout(spawn, cfg, monkeypatch):
    """Replay each spawn's own decode kwargs against a real child writing UTF-8 bytes.

    The child writes raw bytes, so what reaches the pipe does not depend on its
    locale either; the decoded text depends only on the kwargs under test. The
    helpers scope their stubs, so ``subprocess.run`` is real again by now.
    """
    kwargs = (
        _journalctl_kwargs(cfg, monkeypatch)
        if spawn == "journalctl"
        else _launchctl_kwargs(monkeypatch)
    )
    decode = {k: kwargs[k] for k in ("text", "encoding", "errors") if k in kwargs}
    # One trailing byte that is not valid UTF-8 at all: it must degrade to
    # U+FFFD, not raise out of ``subprocess.run``.
    payload = _NON_ASCII.encode("utf-8") + b" \xff"
    child = (
        "import sys; sys.stdout.buffer.write(bytes.fromhex(%r)); sys.stdout.flush()" % payload.hex()
    )
    result = _REAL_RUN([sys.executable, "-c", child], capture_output=True, check=False, **decode)
    assert result.stdout == _NON_ASCII + " \ufffd"
