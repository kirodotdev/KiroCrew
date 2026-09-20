"""Every taskrunner spec read goes through the descriptor gate.

``start_background`` validates the spec path with ``hooks.validate_file_path`` and
then hands the validated NAME to ``_read_spec_prefix``. A read that re-opens that
name is not a read of what was validated: a hardlink alias shares its target's
inode but carries its own innocent name, so ``realpath`` yields the alias,
``is_symlink()`` is False, every name-based check passes — and the bytes belong
to whatever it aliases. ``st_nlink`` is the only signal, and it is readable only
on an open descriptor, so the read goes through
``hooks.safe_read_file_bytes_nolink`` (open first, validate the descriptor, read
that same descriptor) with the spec's own directory as ``within_root``.

``_read_spec_text`` is that one gated read. ``_read_spec_prefix`` asks it for the
bounded planning prefix, and ``run`` / ``plan(source="file")`` ask it for the
whole spec — the text that becomes the LLM prompt, the persisted run and the
review context. A whole-spec read the gate refuses fails the run, because a run
that proceeds on a refused spec is what publishes the aliased target's bytes.

The bounded caller's failure shape stays quiet: an unreadable spec yields an empty
prefix, never an error that would tell a caller whether a path is protected.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import requires_symlinks
from kiro_crew import hooks
from kiro_crew.hooks import FileTooLargeError
from kiro_crew.taskrunner import (
    Step,
    TaskRunner,
    _read_spec_prefix,
    _read_spec_text,
)

# A wait on state that MUST arrive but whose latency the host owns (a thread hop,
# an fsync). It returns as soon as the state is observed.
_GENEROUS_DEADLINE = 30.0

_SECRET_MARKER = "SHOULD-NOT-APPEAR"


def _plant_alias(secret: Path, alias: Path) -> None:
    try:
        os.link(secret, alias)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - host capability
        pytest.skip(f"filesystem does not support hardlinks: {exc}")
    if alias.stat().st_nlink < 2:  # pragma: no cover - host capability
        pytest.skip("filesystem did not create a second link")


def _protected_secret(tmp_path: Path, monkeypatch) -> Path:
    # Path.home() reads USERPROFILE on Windows and never HOME; pin both.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from kiro_crew.security import is_sensitive_path

    secret = tmp_path / ".aws" / "credentials"
    secret.parent.mkdir()
    secret.write_text("aws_secret_access_key = SHOULD-NOT-APPEAR\n", encoding="utf-8")
    assert is_sensitive_path(str(secret)), "precondition: the target is protected"
    return secret


class TestReadSpecPrefix:
    def test_reads_a_plain_spec_prefix(self, tmp_path: Path):
        spec = tmp_path / "task.md"
        spec.write_text("  # Build the thing\n\nStep one.\n  ", encoding="utf-8")
        assert _read_spec_prefix(str(spec), 4000) == "# Build the thing\n\nStep one."

    def test_bounds_the_prefix_by_characters_not_bytes(self, tmp_path: Path):
        # Multi-byte UTF-8: the bound is in characters, as the text-mode read
        # it replaces counted, and a cut inside a code point is never published.
        spec = tmp_path / "task.md"
        spec.write_text("こんにちは" * 1000, encoding="utf-8")
        out = _read_spec_prefix(str(spec), 7)
        assert out == "こんにちはこん"

    def test_normalizes_newlines_like_the_text_mode_read_it_replaces(self, tmp_path: Path):
        spec = tmp_path / "task.md"
        spec.write_bytes(b"# Title\r\n\r\nStep one.\rStep two.\r\n")
        assert _read_spec_prefix(str(spec), 4000) == "# Title\n\nStep one.\nStep two."

    def test_invalid_utf8_still_raises_for_the_caller_to_map(self, tmp_path: Path):
        # The caller maps any error to an empty prefix; keep that contract strict
        # rather than silently publishing replacement characters.
        spec = tmp_path / "task.md"
        spec.write_bytes(b"# Title\xff\xfe\n")
        with pytest.raises(UnicodeDecodeError):
            _read_spec_prefix(str(spec), 4000)

    def test_an_incomplete_sequence_at_eof_still_raises(self, tmp_path: Path):
        # Only a byte-cap cut may leave a dangling code point; a file that ENDS
        # mid code point is malformed and must not become a shorter prefix.
        spec = tmp_path / "task.md"
        spec.write_bytes("# Title こ".encode("utf-8")[:-1])
        with pytest.raises(UnicodeDecodeError):
            _read_spec_prefix(str(spec), 4000)

    def test_a_byte_cap_cut_mid_code_point_is_not_an_error(self, tmp_path: Path):
        # 3-byte code points against a 4-bytes-per-char cap: the cut lands
        # inside a code point, and the bound still yields exactly max_chars.
        spec = tmp_path / "task.md"
        spec.write_text("こ" * 100, encoding="utf-8")
        assert _read_spec_prefix(str(spec), 5) == "こ" * 5

    def test_a_missing_spec_yields_an_empty_prefix(self, tmp_path: Path):
        assert _read_spec_prefix(str(tmp_path / "ghost.md"), 4000) == ""

    def test_a_directory_yields_an_empty_prefix(self, tmp_path: Path):
        assert _read_spec_prefix(str(tmp_path), 4000) == ""

    def test_a_protected_name_yields_an_empty_prefix(self, tmp_path: Path, monkeypatch):
        secret = _protected_secret(tmp_path, monkeypatch)
        assert _read_spec_prefix(str(secret), 4000) == ""

    def test_withholds_a_hardlink_alias_of_a_protected_file(self, tmp_path: Path, monkeypatch):
        """The alias validates as an innocent spec name; the inode is the secret."""
        secret = _protected_secret(tmp_path, monkeypatch)
        alias = tmp_path / "specs" / "task.md"
        alias.parent.mkdir()
        _plant_alias(secret, alias)

        assert _read_spec_prefix(str(alias), 4000) == ""

    @requires_symlinks
    def test_withholds_a_symlink_to_a_protected_file(self, tmp_path: Path, monkeypatch):
        secret = _protected_secret(tmp_path, monkeypatch)
        link = tmp_path / "specs" / "task.md"
        link.parent.mkdir()
        link.symlink_to(secret)

        assert _read_spec_prefix(str(link), 4000) == ""


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions._lock = asyncio.Lock()
    sessions._sessions = {}
    sessions.get_or_create = AsyncMock()

    async def _open_task_session(_pk, session_key, *, agent=None, cwd=None, approval_policy=""):
        return await sessions.get_or_create(session_key, agent=agent, cwd=cwd)

    sessions.open_task_session = _open_task_session
    sessions.release_subagent_runtime = AsyncMock()
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.record_failure = AsyncMock()
    sessions.check_context_usage = MagicMock()
    return sessions


def _runner(work_dir: Path) -> TaskRunner:
    work_dir.mkdir(parents=True, exist_ok=True)
    return TaskRunner(sessions=_mock_sessions(), auto_test=False, work_dir=work_dir)


def _no_run_carries_the_secret(runner: TaskRunner) -> bool:
    return all(_SECRET_MARKER not in (run.spec_content or "") for run in runner._runs.values())


class TestReadSpecText:
    def test_reads_the_whole_spec_when_unbounded(self, tmp_path: Path):
        spec = tmp_path / "task.md"
        body = "# Title\n" + ("line\n" * 4000)
        spec.write_text(body, encoding="utf-8")
        assert _read_spec_text(str(spec), None) == body.strip()

    def test_refuses_a_spec_past_the_gate_cap_instead_of_cutting_it(
        self, tmp_path: Path, monkeypatch
    ):
        # The unbounded read is capped by the gate's own ``MAX_FILE_BYTES``; a
        # spec past that cap is refused, never handed back as a silent prefix.
        monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 32)
        spec = tmp_path / "task.md"
        spec.write_text("x" * 64, encoding="utf-8")
        with pytest.raises(FileTooLargeError):
            _read_spec_text(str(spec), None)

    def test_withholds_a_hardlink_alias_of_a_protected_file(self, tmp_path: Path, monkeypatch):
        secret = _protected_secret(tmp_path, monkeypatch)
        alias = tmp_path / "specs" / "task.md"
        alias.parent.mkdir()
        _plant_alias(secret, alias)

        assert _read_spec_text(str(alias), None) is None

    def test_accepts_a_path_the_caller_spells_with_a_home_tilde(self, tmp_path: Path, monkeypatch):
        # ``within_root`` is taken from the canonical path, so the spelling the
        # caller hands over does not decide whether its own spec is in root.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "task.md").write_text("# Build the thing", encoding="utf-8")

        assert _read_spec_text(os.path.join("~", "specs", "task.md"), None) == "# Build the thing"


@pytest.mark.parametrize("max_chars", [4000, None], ids=["prefix", "whole"])
class TestSpecReadAudit:
    @pytest.fixture
    def audit_log(self, monkeypatch):
        audit = MagicMock()
        monkeypatch.setattr("kiro_crew.taskrunner.sel", lambda: audit)
        return audit.log_tool_invocation

    def test_records_allowed_without_content(self, tmp_path: Path, audit_log, max_chars):
        spec = tmp_path / "spec.md"
        spec.write_text(_SECRET_MARKER, encoding="utf-8")

        assert _read_spec_text(str(spec), max_chars) == _SECRET_MARKER

        audit_log.assert_called_once_with(
            session_key="taskrunner",
            source="taskrunner",
            tool_name="spec_read_validate",
            outcome="allowed",
            metadata={
                "raw": str(spec),
                "resolved": str(spec.resolve()),
                "bounded": max_chars is not None,
            },
        )
        assert _SECRET_MARKER not in repr(audit_log.call_args_list)

    @pytest.mark.parametrize("alias", [False, True], ids=["protected-name", "hardlink"])
    def test_records_denied_without_content(
        self, tmp_path: Path, monkeypatch, audit_log, max_chars, alias
    ):
        secret = _protected_secret(tmp_path, monkeypatch)
        spec = secret
        metadata = {
            "raw": str(secret),
            "reason": "name_validation_rejected",
            "bounded": max_chars is not None,
        }
        if alias:
            spec = tmp_path / "spec.md"
            _plant_alias(secret, spec)
            metadata.update(
                raw=str(spec), resolved=str(spec.resolve()), reason="descriptor_gate_rejected"
            )

        assert _read_spec_text(str(spec), max_chars) is None

        audit_log.assert_called_once_with(
            session_key="taskrunner",
            source="taskrunner",
            tool_name="spec_read_validate",
            outcome="denied",
            metadata=metadata,
        )
        assert _SECRET_MARKER not in repr(audit_log.call_args_list)

    @pytest.mark.parametrize("decision", ["allowed", "name", "descriptor"])
    def test_audit_failure_preserves_read_decision(
        self, tmp_path: Path, monkeypatch, audit_log, max_chars, decision
    ):
        audit_log.side_effect = RuntimeError("audit unavailable")
        spec = tmp_path / "spec.md"
        if decision == "allowed":
            spec.write_text("# Task", encoding="utf-8")
        else:
            secret = _protected_secret(tmp_path, monkeypatch)
            if decision == "name":
                spec = secret
            else:
                _plant_alias(secret, spec)

        expected = "# Task" if decision == "allowed" else None
        assert _read_spec_text(str(spec), max_chars) == expected
        audit_log.assert_called_once()


class TestRunReadsTheSpecThroughTheGate:
    @pytest.mark.asyncio
    async def test_a_plain_spec_reaches_the_run_in_full(self, tmp_path: Path):
        # Longer than the planning prefix bound: the run carries the whole spec,
        # so the gated read must not inherit the prefix's cut.
        spec = tmp_path / "spec.md"
        body = "# Task\n## Steps\n" + ("1. Do the thing\n" * 500)
        spec.write_text(body, encoding="utf-8")
        runner = _runner(tmp_path / "work")
        runner._decompose = AsyncMock(return_value=[Step(index=1, title="s", description="d")])

        with patch.object(runner, "_execute_tasks", new_callable=AsyncMock, return_value=True):
            run = await runner.run(spec)

        assert run.spec_content == body.strip()
        assert len(run.spec_content) > 4000

    @pytest.mark.asyncio
    async def test_refuses_a_hardlink_alias_of_a_protected_file(self, tmp_path: Path, monkeypatch):
        secret = _protected_secret(tmp_path, monkeypatch)
        alias = tmp_path / "specs" / "spec.md"
        alias.parent.mkdir()
        _plant_alias(secret, alias)
        runner = _runner(tmp_path / "work")

        with pytest.raises(PermissionError):
            await runner.run(alias)

        assert _no_run_carries_the_secret(runner)

    @requires_symlinks
    @pytest.mark.asyncio
    async def test_refuses_a_symlink_to_a_protected_file(self, tmp_path: Path, monkeypatch):
        secret = _protected_secret(tmp_path, monkeypatch)
        link = tmp_path / "specs" / "spec.md"
        link.parent.mkdir()
        link.symlink_to(secret)
        runner = _runner(tmp_path / "work")

        with pytest.raises(PermissionError):
            await runner.run(link)

        assert _no_run_carries_the_secret(runner)

    @pytest.mark.asyncio
    async def test_plan_from_file_refuses_a_hardlink_alias(self, tmp_path: Path, monkeypatch):
        secret = _protected_secret(tmp_path, monkeypatch)
        alias = tmp_path / "specs" / "spec.md"
        alias.parent.mkdir()
        _plant_alias(secret, alias)
        runner = _runner(tmp_path / "work")

        with pytest.raises(PermissionError):
            await runner.plan("", source="file", spec_path=str(alias))

        assert _no_run_carries_the_secret(runner)


class TestStartBackgroundPublishesNothingFromARefusedSpec:
    @pytest.mark.asyncio
    async def test_a_hardlink_alias_produces_a_failed_run_with_no_target_content(
        self, tmp_path: Path, monkeypatch
    ):
        secret = _protected_secret(tmp_path, monkeypatch)
        alias = tmp_path / "specs" / "spec.md"
        alias.parent.mkdir()
        _plant_alias(secret, alias)
        runner = _runner(tmp_path / "work")

        task_id = await runner.start_background(alias)
        worker = runner._tasks.get(task_id)
        assert worker is not None
        await asyncio.wait_for(worker, timeout=_GENEROUS_DEADLINE)

        placeholder = runner._runs[task_id]
        assert placeholder.status == "failed"
        assert placeholder.spec_content == ""
        assert _no_run_carries_the_secret(runner)
