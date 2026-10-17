"""The rewritten MCP stub argv keeps the session cwd off ``sys.path``.

kiro-cli launches the overlay's stub entry with the agent's SESSION cwd, which
is a project checkout Kiro Crew does not control. ``python -m`` puts that cwd
at ``sys.path[0]``, so a stdlib-named file there (``json.py``, ``socket.py``)
would be imported in place of the standard library by the stub. ``-P``
(``PYTHONSAFEPATH``) drops that entry; the stub imports only from its install.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.mcp_gateway import rewriter

# A stdlib module the stub imports and the bare interpreter startup does not.
_SHADOWED = "json"


def _entry(tmp_path: Path) -> dict:
    return rewriter._build_stub_entry(
        stubs_dir=tmp_path / "stubs",
        server_name="probe",
        agent_name="probe-agent",
        original={"command": sys.executable, "args": [], "autoApprove": []},
        env_pairs={},
        target_command=sys.executable,
        socket_path=tmp_path / "absent.sock",
        work_dir=tmp_path,
        sandbox_mode="standard",
        approval_mode="interactive",
        identity_keys=(),
    )


def _interpreter_options(args: list[str]) -> list[str]:
    """The interpreter options ahead of ``-m <stub module>``."""
    idx = args.index("-m")
    assert args[idx + 1] == rewriter._STUB_MODULE
    return args[:idx]


@pytest.mark.parametrize("user_site", [True, False])
def test_stub_argv_passes_safe_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user_site: bool
) -> None:
    monkeypatch.setattr(rewriter.platform_compat, "is_bundled_interpreter", lambda: False)
    monkeypatch.setattr(rewriter.platform_compat.site, "ENABLE_USER_SITE", user_site)
    options = _interpreter_options(_entry(tmp_path)["args"])
    assert "-P" in options
    assert ("-s" in options) is (not user_site)


def _run_from(cwd: Path, argv: list[str]) -> None:
    try:
        subprocess.run(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # The real stub may sit waiting on stdin or a broker; the planted
        # module, if imported, already left its marker before that.
        pass


def test_stdlib_named_file_in_session_cwd_is_not_imported(tmp_path: Path) -> None:
    """Launch the overlay entry exactly as kiro-cli does, from a session cwd
    holding a planted ``json.py``. The plant must never run."""
    session_cwd = tmp_path / "session"
    session_cwd.mkdir()
    marker = tmp_path / "shadow-ran"
    (session_cwd / f"{_SHADOWED}.py").write_text(
        f"open({str(marker)!r}, 'w').close()\nimport os\nos._exit(0)\n",
        encoding="utf-8",
    )
    entry = _entry(tmp_path)
    argv = [entry["command"], *entry["args"]]

    # Control: the same launch without the safe-path flag DOES import the
    # plant, so the probe really reaches the shadowing path.
    _run_from(session_cwd, [a for a in argv if a != "-P"])
    assert marker.exists(), "fixture did not exercise the cwd import path"
    marker.unlink()

    _run_from(session_cwd, argv)
    assert not marker.exists(), "a stdlib-named file in the session cwd shadowed the stdlib"


@pytest.mark.parametrize("user_site", [True, False])
def test_fingerprint_records_the_written_interpreter_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user_site: bool
) -> None:
    """Overlays written before the flag existed must be rewritten on upgrade,
    so the cache key records exactly the interpreter options the overlay
    carries -- ``-P`` included. A drift between the two would serve a stale
    overlay from cache."""
    monkeypatch.setattr(rewriter.platform_compat, "is_bundled_interpreter", lambda: False)
    monkeypatch.setattr(rewriter.platform_compat.site, "ENABLE_USER_SITE", user_site)
    snapshot = rewriter._rewrite_inputs_fingerprint(
        source_dir=tmp_path / "agents",
        settings_path=tmp_path / "settings.json",
        overlay_dir=tmp_path / "overlay",
        socket_path=tmp_path / "absent.sock",
        work_dir=tmp_path,
        sandbox_mode="standard",
        approval_mode="interactive",
        stub_set=frozenset(),
        pooling_enabled=True,
        forward_env=False,
        identity_keys=(),
        read_buffer_limit=1 << 20,
    )
    recorded = json.loads(json.dumps(snapshot))["python_isolation_flags"]
    assert "-P" in recorded
    assert recorded == _interpreter_options(_entry(tmp_path)["args"])
