"""The stub's path resolution must produce ABSOLUTE paths on every platform.

Both the default socket path and the fallback audit log derive from one data
home. That home used to fall back to ``os.environ["HOME"]``, which is normally
unset on Windows (it uses ``USERPROFILE``), so the expression evaluated to
``Path("")`` and every derived path became relative to the stub's cwd.

That is not cosmetic. The Windows pipe name is a hash of the socket path, so a
daemon and a stub started from different working directories would hash to two
different pipe names and never meet -- silently, with a gateway nothing can
reach. The fallback log landing in an arbitrary cwd also quietly destroys the
main signal for "did pooling actually engage".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.mcp_gateway.stub import (
    _crew_home,
    _default_socket_path,
    _fallback_log_path,
)


def _clear_home_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduce a Windows environment: no KIROCREW_HOME and no HOME."""
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)


def test_kirocrew_home_wins_when_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    assert _crew_home() == tmp_path
    assert _default_socket_path() == str(tmp_path / "kirocrew-mcp-gateway.sock")
    assert _fallback_log_path() == tmp_path / "logs" / "stub_fallback.jsonl"


def test_home_is_absolute_without_any_home_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression. With HOME unset the old expression yielded ``Path("")``,
    making every derived path relative to whatever cwd the stub inherited."""
    _clear_home_env(monkeypatch)
    assert _crew_home().is_absolute(), "data home must never be cwd-relative"


def test_socket_default_is_absolute_without_any_home_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Load-bearing on Windows: the pipe name is a hash of this path, so a
    cwd-relative value lets two processes derive different pipe names."""
    _clear_home_env(monkeypatch)
    assert Path(_default_socket_path()).is_absolute()


def test_fallback_log_is_absolute_without_any_home_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_home_env(monkeypatch)
    assert _fallback_log_path().is_absolute()


def test_unresolvable_home_degrades_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Path.home()`` raises RuntimeError when no home can be resolved at all,
    and neither caller may raise: the socket value is an argparse default (a
    raise there kills the stub before it can degrade to a per-session exec) and
    the log path is used by ``log_fallback``, whose handler catches ``OSError``
    only -- so a RuntimeError would escape the one function documented as
    never allowed to break the exec that keeps kiro-cli working.
    """
    _clear_home_env(monkeypatch)
    monkeypatch.delenv("USERPROFILE", raising=False)

    def _no_home() -> Path:
        raise RuntimeError("could not determine home directory")

    monkeypatch.setattr(Path, "home", staticmethod(_no_home))

    # Must not raise, and must still yield a usable path.
    assert _crew_home().parts, "a degraded home must still be a usable path"
    assert _default_socket_path().endswith("kirocrew-mcp-gateway.sock")
    assert _fallback_log_path().name == "stub_fallback.jsonl"
