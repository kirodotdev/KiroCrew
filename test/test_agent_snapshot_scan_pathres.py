"""The agent snapshot scan judges each spec on the canonical sensitive-path gate.

``refresh_materialized_agents`` reads every spec in both agents directories with
``hooks.safe_read_file(path, canonical_gate=True)``. Off the event loop that gate
answers inline, for the resolved name before the open and for the opened
descriptor's kernel path after it, so a refresh submits nothing to the two-worker
``mc-pathres`` pool. On the event loop the same keyword takes the bounded gate. A
link to a fenced file is still refused per entry, and the default read is unchanged.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import hooks, security
from kiro_crew.config import loader, loader_snapshots


def _spec(directory: Path, filename: str, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(json.dumps({"name": name}), encoding="utf-8")
    return path


def _pool_recorder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every path handed to the ``mc-pathres`` pool, passed through to it."""
    seen: list[str] = []
    real = security.paths._run_resolution_bounded

    def recording(expanded, *args, **kwargs):
        seen.append(expanded)
        return real(expanded, *args, **kwargs)

    monkeypatch.setattr(security.paths, "_run_resolution_bounded", recording)
    return seen


def _crew_secret(text: str) -> Path:
    """The crew home's ``.env``: ``KIROCREW_HOME`` is a per-test tmp dir here."""
    crew_home = Path(os.environ["KIROCREW_HOME"])
    crew_home.mkdir(parents=True, exist_ok=True)
    secret = crew_home / ".env"
    secret.write_text(text, encoding="utf-8")
    return secret


@pytest.fixture
def agents_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Both directories the refresh scans, under tmp_path."""
    user_dir = tmp_path / "agents"
    mate_dir = tmp_path / "mates" / "agents"
    user_dir.mkdir()
    monkeypatch.setattr(loader, "kiro_agents_dir", lambda: user_dir)
    monkeypatch.setattr(loader, "mate_agents_dir", lambda: mate_dir)
    return user_dir, mate_dir


class TestTheScanStaysOffThePool:
    """A refresh off the loop reads every spec without a resolver round trip."""

    def test_a_refresh_submits_nothing_to_the_resolver_pool(
        self, agents_dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_dir, mate_dir = agents_dirs
        for i in range(3):
            _spec(user_dir, f"agent-{i}.json", f"agent-{i}")
        _spec(mate_dir, "mate-1.json", "mate-1")
        # Cold target cache: the anchors must be rebuilt inline, not via the pool.
        monkeypatch.setattr(security.paths, "_home_targets_cache", {})
        submitted = _pool_recorder(monkeypatch)

        assert loader.refresh_materialized_agents() is True

        assert submitted == []
        assert loader.materialized_snapshot_complete() is True
        assert {"agent-0", "agent-1", "agent-2", "mate-1"} <= loader_snapshots._MATERIALIZED_AGENTS

    def test_the_default_read_still_takes_the_bounded_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The keyword is opt-in: every other safe_read_file caller keeps the gate
        # it had, and only the keyword moves a read off the pool.
        spec = _spec(tmp_path / "agents", "a.json", "a")
        submitted = _pool_recorder(monkeypatch)

        assert json.loads(hooks.safe_read_file(str(spec))) == {"name": "a"}
        assert submitted, "the default read never reached the bounded gate"

        submitted.clear()
        assert json.loads(hooks.safe_read_file(str(spec), canonical_gate=True)) == {"name": "a"}
        assert submitted == []


class TestTheScanRefusesAFencedLinkPerEntry:
    """Moving the gate off the pool did not move what it refuses."""

    @requires_symlinks
    def test_a_link_to_a_crew_secret_is_skipped_and_a_benign_link_is_read(
        self, agents_dirs: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``evil.json -> <credential file>`` shape, real gates only."""
        user_dir, _mate_dir = agents_dirs
        secret = _crew_secret(json.dumps({"name": "leaked"}))
        (user_dir / "evil.json").symlink_to(secret)
        benign_target = _spec(tmp_path / "elsewhere", "benign.json", "benign-target")
        (user_dir / "benign.json").symlink_to(benign_target)
        _spec(user_dir, "good.json", "good")
        submitted = _pool_recorder(monkeypatch)

        assert loader.refresh_materialized_agents() is True

        names = loader_snapshots._MATERIALIZED_AGENTS
        assert "leaked" not in names
        assert "evil" not in names
        assert {"benign-target", "good"} <= names
        # The refused entry is a skipped spec, so the scan reports itself partial.
        assert loader.materialized_snapshot_complete() is False
        assert submitted == []


class TestTheOpenedDescriptorIsJudgedOnTheCanonicalGate:
    """The second check, on the descriptor's kernel path, also answers inline."""

    def test_a_fenced_file_the_name_check_passed_is_refused_at_the_descriptor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret = _crew_secret("TOKEN=leaked\n")
        # Stands in for a name that passed its check before a swap: only the
        # opened descriptor's own verdict is left to refuse the read.
        monkeypatch.setattr(hooks, "canonical_path_refusal", lambda _resolved: None)
        submitted = _pool_recorder(monkeypatch)

        with pytest.raises(PermissionError, match="no longer matches safe path"):
            hooks.safe_read_file(str(secret), canonical_gate=True)
        assert submitted == []


class TestTheKeywordFollowsTheThread:
    """On the loop the bounded gate answers; on a worker the inline gate does."""

    @pytest.mark.asyncio
    async def test_on_the_event_loop_the_keyword_takes_the_bounded_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The inline gate resolves its anchors on the calling thread, which on the
        # loop is the blocking call the pool exists to keep off it.
        spec = _spec(tmp_path / "agents", "a.json", "a")
        submitted = _pool_recorder(monkeypatch)
        monkeypatch.setattr(
            security.paths,
            "is_sensitive_resolved_path",
            lambda *a, **k: pytest.fail("the inline gate ran on the event loop"),
        )

        assert json.loads(hooks.safe_read_file(str(spec), canonical_gate=True)) == {"name": "a"}
        assert submitted, "the read on the loop never reached the bounded gate"

    @pytest.mark.asyncio
    async def test_off_the_event_loop_the_keyword_answers_inline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = _spec(tmp_path / "agents", "a.json", "a")
        submitted = _pool_recorder(monkeypatch)

        text = await asyncio.to_thread(hooks.safe_read_file, str(spec), canonical_gate=True)

        assert json.loads(text) == {"name": "a"}
        assert submitted == []
