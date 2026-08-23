"""A secret-bearing file must be locked down BEFORE it is published.

Applying the owner-only lockdown after ``atomic_write`` has already renamed the
payload into place leaves a window in which the file exists at its final path
under whatever permissions it inherited. Two writers did that:

* ``mcp_gateway/rewriter.py`` — the per-agent and settings MCP overlays, which
  carry passed-through ``env`` blocks (tokens / API keys). Its lockdown was also
  guarded by ``if not platform_compat.IS_POSIX``, so on POSIX the overlay relied
  entirely on ``atomic_write``'s mode and on Windows it got the DACL only after
  publication.
* ``service/live_target.py`` — the live-target pointer, which is a
  code-execution input read at every startup.

``atomic_write(restrict_to_owner=True)`` applies the lockdown to the temp file
before any content reaches it and before the rename, so the payload never exists
unprotected at its final path and a lockdown failure publishes nothing at all.

The assertions here are about ORDER and OUTCOME rather than about which platform
is running: each records the state of the FINAL path at the moment the lockdown
runs, which is a property both platforms must satisfy.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from test_live_target import _make_valid_checkout

# ── live-target pointer ──────────────────────────────────────────────────────


@pytest.fixture()
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


def test_the_pointer_is_locked_down_before_it_is_published(_home) -> None:
    """The lockdown must not see a file already at the pointer's final path."""
    from kiro_crew.service import live_target

    seen_at_call: list[bool] = []
    real = None
    from kiro_crew import platform_compat

    real = platform_compat.restrict_to_owner

    def _recording(path):
        seen_at_call.append(live_target.pointer_path().exists())
        return real(path)

    with patch("kiro_crew.platform_compat.restrict_to_owner", side_effect=_recording):
        live_target.write_target(_make_valid_checkout(_home))

    assert seen_at_call, "the pointer was written without any owner-only lockdown at all"
    assert not any(seen_at_call), (
        "the lockdown ran while the pointer was already published at its final "
        "path: the file existed there unprotected until that call returned"
    )


def test_a_pointer_that_cannot_be_locked_down_is_not_published(_home) -> None:
    """Fail-closed: a failed lockdown must leave the PREVIOUS pointer in place."""
    from kiro_crew.service import live_target

    live_target.write_target(_make_valid_checkout(_home))
    before = live_target.snapshot()
    assert before, "scaffold: the first pointer was never written"

    second_root = _home / "second"
    second_root.mkdir()
    other = _make_valid_checkout(second_root)

    with patch(
        "kiro_crew.platform_compat.restrict_to_owner",
        side_effect=OSError("icacls: transient failure"),
    ):
        with pytest.raises(OSError):
            live_target.write_target(other)

    assert live_target.snapshot() == before, (
        "a pointer whose lockdown failed replaced the previous one anyway; the "
        "gateway would exec a target that was never protected"
    )


def test_restore_still_reports_failure_instead_of_raising(_home) -> None:
    """Preservation: ``restore`` is best-effort by contract and returns False."""
    from kiro_crew.service import live_target

    live_target.write_target(_make_valid_checkout(_home))
    prior = live_target.snapshot()

    with patch(
        "kiro_crew.platform_compat.restrict_to_owner",
        side_effect=OSError("icacls: transient failure"),
    ):
        assert live_target.restore(prior) is False, (
            "restore raised instead of reporting failure, which would lose the "
            "original cutover error it was unwinding"
        )


# ── MCP overlays ─────────────────────────────────────────────────────────────


def _agent_source(tmp_path: Path) -> Path:
    source_dir = tmp_path / "agents"
    source_dir.mkdir()
    spec = {
        "name": "test-agent",
        "mcpServers": {
            "myserver": {
                "command": "echo",
                "args": ["hello"],
                "env": {"SECRET_TOKEN": "s3cr3t"},
                "poolable": True,
            }
        },
    }
    (source_dir / "test-agent.json").write_text(json.dumps(spec), encoding="utf-8")
    return source_dir


def test_the_agent_overlay_is_locked_down_before_publication(tmp_path: Path) -> None:
    """On EVERY platform, and before the overlay reaches its final path.

    The previous spelling ran the lockdown only when ``IS_POSIX`` was false, so
    on POSIX this assertion fails because nothing locked the overlay down at
    all, and on Windows it fails because the overlay was already published when
    the lockdown ran. Both are the same defect seen from different platforms.
    """
    from kiro_crew import platform_compat
    from kiro_crew.mcp_gateway.rewriter import rewrite_agents

    source_dir = _agent_source(tmp_path)
    overlay_dir = tmp_path / "overlay"
    locked: list[Path] = []
    real = platform_compat.restrict_to_owner

    def _recording(path):
        p = Path(path)
        # Scoped to the overlay directory. The rewriter also persists a
        # fingerprint file there, whose own temp is locked down after the
        # overlay is published -- correct for that file, and not what this test
        # is about, so the assertions below name the overlay spec explicitly
        # rather than timing every call in the directory.
        if p.parent == overlay_dir:
            locked.append(p)
        return real(path)

    with patch("kiro_crew.platform_compat.restrict_to_owner", side_effect=_recording):
        rewrite_agents(
            source_dir=source_dir,
            overlay_dir=overlay_dir,
            socket_path=tmp_path / "gw.sock",
            work_dir=tmp_path / "wd",
            sandbox_mode="auto",
            approval_mode="interactive",
            stub_servers=frozenset(["myserver"]),
        )

    published = overlay_dir / "test-agent.json"
    assert locked, (
        "no owner-only lockdown was applied in the overlay directory on this "
        "platform; the overlay carries passed-through env blocks (tokens / API "
        "keys) and must not depend on POSIX mode bits alone"
    )
    assert published not in locked, (
        "the lockdown was applied to the overlay AFTER it was published at its "
        f"final path, leaving it briefly readable: {locked}"
    )
    assert any(
        p.name.endswith(".tmp") for p in locked
    ), f"the overlay was never locked down before publication: {locked}"
    assert (overlay_dir / "test-agent.json").is_file(), (
        "the overlay was not published at all — the lockdown change must not "
        "stop the rewriter producing its output"
    )
