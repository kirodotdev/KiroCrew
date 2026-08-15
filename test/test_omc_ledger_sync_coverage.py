"""Unit coverage for Ops Mission Control's git-native ledger sync.

The app already ships ``tests/test_ledger_sync_git.py``, which drives this module against
a REAL git repo and a real bare remote. That file is the right place for the "does git
actually move the text" contract -- and it is DESELECTED in CI, so on a pull request none
of it runs and the module's error and refusal branches are measured as unreached.

This file is the complement, not a duplicate: no subprocess, no network, no real git. The
whole transport is stubbed at one seam -- ``_git`` is replaced by a table-driven fake that
records every argv it was handed -- so the tests can assert on the two things a mocked
run CAN prove:

* the argv a step BUILDS (``branch -m --``, the two tracking ``config`` keys, the
  refspecs), and
* what each non-zero return code is TURNED INTO -- the fallbacks, the refusals, the
  operator-facing strings ``status()`` puts on the Settings card.

Weighted deliberately toward what a happy path never sees: the branch-name fallback, every
``status()`` detail branch, ``_align_branch``'s three refusals, ``_ensure_repo``'s four
failure returns, the conflict detectors' unreadable-file paths, the credential pre-push
gate, and ``sync_safely``'s transient-fault retry.

Every write lands under ``tmp_path``: ``ledger.app_data_dir`` is redirected, which moves
the ledger file, the ledger lock and the repo root together.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync as ls
from kiro_crew.apps.builtins.ops_mission_control.backend import providers
from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

#: The real coroutine, captured before any fixture swaps it for the fake. The handful of
#: tests that cover ``_git``'s OWN body call this rather than the module attribute.
_REAL_GIT = ls._git

_REMOTE = "git@example.com:team/ops-ledger.git"


def _write(path: Path, text: str) -> None:
    """Write text with LF endings on every platform.

    ``Path.write_text`` translates ``\\n`` to ``\\r\\n`` on Windows while the readers under
    test split on the untranslated bytes, so a conflict marker written the ordinary way is
    still detected but a byte-for-byte comparison is not reproducible across platforms.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


# ── the transport seam ───────────────────────────────────────────────────────


class FakeGit:
    """Stand-in for ``ledger_sync._git``: answers from an argv-prefix table.

    Entries are matched in registration order and the FIRST match wins, so a specific
    prefix has to be registered before a broader one. Anything unmatched succeeds
    silently, which keeps a test that only cares about one step from having to spell out
    the other eight.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._table: list[tuple[tuple[str, ...], tuple[int, str, str]]] = []
        self.default: tuple[int, str, str] = (0, "", "")

    def when(self, *prefix: str, rc: int = 0, out: str = "", err: str = "") -> FakeGit:
        self._table.append((tuple(prefix), (rc, out, err)))
        return self

    def aligned(self) -> FakeGit:
        """Make ``_align_branch`` see an ordinary repo already on ``main``."""
        return self.when("symbolic-ref", out="main\n")

    async def __call__(self, *args: str) -> tuple[int, str, str]:
        self.calls.append(tuple(args))
        for prefix, result in self._table:
            if args[: len(prefix)] == prefix:
                return result
        return self.default

    def ran(self, *prefix: str) -> bool:
        return any(call[: len(prefix)] == prefix for call in self.calls)

    def argv_for(self, *prefix: str) -> tuple[str, ...]:
        for call in self.calls:
            if call[: len(prefix)] == prefix:
                return call
        raise AssertionError(f"git {' '.join(prefix)} was never run; ran {self.calls}")


@pytest.fixture
def omc(tmp_path, monkeypatch):
    """Point the whole module at ``tmp_path`` and stub every outside edge.

    Redirecting ``ledger.app_data_dir`` (rather than ``ledger_path`` alone) is what keeps
    the ledger lock inside the temp tree too -- ``_LedgerLock`` resolves its own path, so
    patching only the file would have put the lock in the caller's real data home.
    """
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(ledger, "app_data_dir", lambda *_a, **_k: data)

    policy: dict[str, Any] = {}
    monkeypatch.setattr(ls.policy_store, "get", lambda key, default=None: policy.get(key, default))
    monkeypatch.setattr(ls.policy_store, "put", lambda key, value: policy.__setitem__(key, value))

    config: dict[str, Any] = {}
    monkeypatch.setattr(ls, "read_config", lambda: dict(config))
    monkeypatch.setattr(providers, "set_top_level", lambda key, value: config.__setitem__(key, value))

    # The audit sink is an assertion target here, never a real write.
    audit = MagicMock()
    monkeypatch.setattr(ls, "sel", lambda: audit)
    # Module-level and mutated by ``_ensure_repo``; pinned so test order cannot leak.
    monkeypatch.setattr(ls, "_align_refusal", "")

    git = FakeGit()
    monkeypatch.setattr(ls, "_git", git)

    def enable(*, remote_url: str = _REMOTE, branch_name: str | None = None) -> None:
        policy["ledger_sync_enabled"] = True
        policy["ledger_sync_remote"] = remote_url
        if branch_name is not None:
            config["ledger_sync_branch"] = branch_name

    def init_repo(head: str = "ref: refs/heads/main\n") -> None:
        _write(data / ".git" / "HEAD", head)

    def ledger_lines(*entries: LedgerEntry, extra: str = "") -> None:
        body = "".join(json.dumps(e.to_dict()) + "\n" for e in entries)
        _write(ledger.ledger_path(), body + extra)

    return SimpleNamespace(
        root=data,
        policy=policy,
        config=config,
        git=git,
        audit=audit,
        enable=enable,
        init_repo=init_repo,
        ledger_lines=ledger_lines,
        schedule=data / "rotation.yaml",
    )


def _entry(pattern: str, fix: str = "restart the consumer") -> LedgerEntry:
    return LedgerEntry.create(pattern=pattern, fix=fix)


_CONFLICTED_SCHEDULE = (
    "shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n"
    "<<<<<<< HEAD\n    who: alice\n=======\n    who: bob\n>>>>>>> origin/main\n"
)
_CLEAN_SCHEDULE = "shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: alice\n"


# ── configuration accessors ──────────────────────────────────────────────────


def test_configured_needs_both_the_toggle_and_a_remote(omc):
    assert ls.configured() is False
    omc.policy["ledger_sync_enabled"] = True
    assert ls.configured() is False, "a toggle with no remote is not configured"
    omc.policy["ledger_sync_remote"] = _REMOTE
    assert ls.configured() is True
    omc.policy["ledger_sync_enabled"] = False
    assert ls.configured() is False


def test_remote_is_read_from_the_fenced_store_and_stripped(omc):
    assert ls.remote() == ""
    omc.policy["ledger_sync_remote"] = f"  {_REMOTE}\n"
    assert ls.remote() == _REMOTE


def test_remote_survives_a_none_in_the_store(omc):
    """A hand-edited store can hold ``null``; the accessor must not return "None"."""
    omc.policy["ledger_sync_remote"] = None
    assert ls.remote() == ""


def test_branch_defaults_when_unset_or_blank(omc):
    assert ls.branch() == ls.DEFAULT_BRANCH
    omc.config["ledger_sync_branch"] = "   "
    assert ls.branch() == ls.DEFAULT_BRANCH


def test_branch_accepts_an_ordinary_ref_name(omc):
    omc.config["ledger_sync_branch"] = "team/ops-ledger.v2"
    assert ls.branch() == "team/ops-ledger.v2"


@pytest.mark.parametrize(
    "bad",
    [
        "--upload-pack=evil",
        "-x",
        "main evil",
        "with\nnewline",
        "",
        "a" * 200,
    ],
)
def test_an_option_like_branch_falls_back_and_says_so(omc, caplog, bad):
    """``set_settings`` and a hand-edited config both bypass the route's validation.

    Verified hazards: ``git init -b '-x'`` creates ``refs/heads/-x``, and
    ``git symbolic-ref HEAD refs/heads/--upload-pack=evil`` succeeds with no validation at
    all -- so an option-like value must never reach a git argv.
    """
    omc.config["ledger_sync_branch"] = bad
    with caplog.at_level(logging.WARNING, logger=ls.logger.name):
        assert ls.branch() == ls.DEFAULT_BRANCH
    if bad.strip():
        assert "not a usable git ref" in caplog.text


def test_set_settings_routes_each_key_to_its_own_store(omc):
    """Destination keys are fenced; the branch is plain config written under the lock."""
    ls.set_settings(enabled=True, remote_url=f" {_REMOTE} ", branch_name=" team-ledger ")
    assert omc.policy["ledger_sync_enabled"] is True
    assert omc.policy["ledger_sync_remote"] == _REMOTE
    assert omc.config["ledger_sync_branch"] == "team-ledger"


def test_set_settings_leaves_unnamed_keys_alone(omc):
    ls.set_settings(enabled=False)
    assert omc.policy == {"ledger_sync_enabled": False}
    assert omc.config == {}


def test_set_settings_can_move_the_branch_without_touching_the_fenced_keys(omc):
    """Changing the branch must not write the destination keys an agent cannot reach."""
    ls.set_settings(branch_name="team-ledger")
    assert omc.policy == {}
    assert omc.config == {"ledger_sync_branch": "team-ledger"}


# ── _head_branch ─────────────────────────────────────────────────────────────


def test_head_branch_reads_the_ref_out_of_dot_git_head(omc):
    omc.init_repo("ref: refs/heads/team-ledger\n")
    assert ls._head_branch() == "team-ledger"


def test_head_branch_is_empty_when_there_is_no_repo(omc):
    assert ls._head_branch() == ""


def test_head_branch_is_empty_for_a_detached_head(omc):
    omc.init_repo("9f1c0a4b2d3e4f5061728394a5b6c7d8e9f00112\n")
    assert ls._head_branch() == ""


def test_head_branch_is_empty_for_a_ref_outside_refs_heads(omc):
    omc.init_repo("ref: refs/remotes/origin/main\n")
    assert ls._head_branch() == ""


def test_head_branch_is_empty_when_dot_git_is_a_file(omc):
    """A ``.git`` FILE is a worktree or submodule gitdir pointer, not a repo."""
    _write(omc.root / ".git", "gitdir: /elsewhere/.git/worktrees/ops\n")
    assert ls._head_branch() == ""


# ── status(): every detail branch the Settings card can show ─────────────────


def test_status_off(omc):
    state = ls.status()
    assert state["enabled"] is False
    assert state["ready"] is False
    assert state["detail"].startswith("Off.")


def test_status_enabled_without_a_remote(omc):
    omc.policy["ledger_sync_enabled"] = True
    state = ls.status()
    assert state["ready"] is False
    assert "No remote set" in state["detail"]


def test_status_ready_before_the_first_sync(omc):
    omc.enable()
    state = ls.status()
    assert state["ready"] is True
    assert state["initialized"] is False
    assert state["branch_matches"] is True, "nothing exists yet to disagree with"
    assert state["local_branch"] == ""
    assert state["detail"] == f"Ready. The repo is created on the first sync ({_REMOTE})."


def test_status_reports_the_refusal_push_actually_makes(omc):
    """A conflicted schedule is the one state that used to LIE on this card.

    ``push`` refuses outright while ``rotation.yaml`` holds markers, and that refusal
    reached only the log and a SEL line -- so the card kept claiming "Syncing".
    """
    omc.enable()
    omc.init_repo()
    _write(omc.schedule, _CONFLICTED_SCHEDULE)
    state = ls.status()
    assert state["schedule_conflict"] is True
    assert "refused" in state["detail"]
    assert "Syncing" not in state["detail"]


def test_status_calls_a_ledger_conflict_reconcilable_not_refused(omc):
    """The ledger conflict wording must not send the operator hand-editing a fixed file."""
    omc.enable()
    omc.init_repo()
    omc.ledger_lines(_entry("dlq fills"), extra="<<<<<<< HEAD\n")
    state = ls.status()
    assert state["conflict"] is True
    assert state["schedule_conflict"] is False
    assert "refused" not in state["detail"]
    assert f"syncing {_REMOTE} on branch main" in state["detail"]


def test_a_conflicted_ledger_does_not_hide_a_branch_mismatch(omc):
    """The ledger sentence OUTRANKS the mismatch one, so it must not overstate.

    ``_where``'s second arm exists for exactly this: claiming "on branch main" here would
    make a conflicted ledger swallow the fact that HEAD is somewhere else entirely.
    """
    omc.enable()
    omc.init_repo("ref: refs/heads/legacy\n")
    omc.ledger_lines(_entry("dlq fills"), extra=">>>>>>> origin/main\n")
    state = ls.status()
    assert state["conflict"] is True
    assert state["branch_matches"] is False
    assert "publishing to" in state["detail"]
    assert "syncing" not in state["detail"]


def test_status_names_a_detached_head_and_says_it_is_deliberate(omc):
    omc.enable()
    omc.init_repo("9f1c0a4b2d3e4f5061728394a5b6c7d8e9f00112\n")
    state = ls.status()
    assert state["detached"] is True
    assert state["local_branch"] == ""
    assert state["branch_matches"] is False
    assert "detached" in state["detail"]


def test_status_promises_the_next_sync_will_fix_a_plain_mismatch(omc):
    omc.enable()
    omc.init_repo("ref: refs/heads/legacy\n")
    state = ls.status()
    assert state["branch"] == "main"
    assert state["local_branch"] == "legacy"
    assert state["branch_matches"] is False
    assert "The next sync moves it onto main" in state["detail"]


def test_status_surfaces_an_alignment_refusal_instead_of_that_promise(omc, monkeypatch):
    """Alignment can REFUSE, and then "the next sync fixes it" is a lie.

    That reason is stashed in a module global rather than threaded through
    ``_ensure_repo``'s return, precisely so it can reach this card without turning a
    usability refusal into a sync failure.
    """
    omc.enable()
    omc.init_repo("ref: refs/heads/legacy\n")
    monkeypatch.setattr(ls, "_align_refusal", "A different local branch named main exists.")
    state = ls.status()
    assert "A different local branch named main exists." in state["detail"]
    assert "The next sync moves it onto" not in state["detail"]


def test_status_of_a_healthy_repo(omc):
    omc.enable()
    omc.init_repo()
    _write(omc.schedule, _CLEAN_SCHEDULE)
    omc.ledger_lines(_entry("dlq fills"))
    state = ls.status()
    assert state == {
        "enabled": True,
        "remote": _REMOTE,
        "branch": "main",
        "local_branch": "main",
        "branch_matches": True,
        "detached": False,
        "initialized": True,
        "ready": True,
        "conflict": False,
        "schedule_conflict": False,
        "detail": f"Syncing {_REMOTE} on branch main.",
    }


# ── conflict detectors ───────────────────────────────────────────────────────


def test_has_conflict_is_false_without_a_ledger(omc):
    assert ls.has_conflict() is False


def test_has_conflict_is_false_on_a_clean_ledger(omc):
    omc.ledger_lines(_entry("dlq fills"))
    assert ls.has_conflict() is False


@pytest.mark.parametrize("marker", ls.CONFLICT_MARKERS)
def test_has_conflict_sees_each_marker_git_writes(omc, marker):
    omc.ledger_lines(_entry("dlq fills"), extra=f"{marker} HEAD\n")
    assert ls.has_conflict() is True


def test_has_conflict_treats_an_unreadable_ledger_as_clean(omc, monkeypatch):
    """Raising here would turn a read fault into an unexplained sync failure."""
    omc.ledger_lines(_entry("dlq fills"))
    monkeypatch.setattr(Path, "read_text", _raise_oserror)
    assert ls.has_conflict() is False


def test_schedule_has_conflict_is_false_without_a_schedule(omc):
    assert ls.schedule_has_conflict() is False


def test_schedule_has_conflict_is_scoped_to_the_schedule(omc):
    """Sharing one detector with the ledger would block every push on a ledger conflict."""
    _write(omc.schedule, _CLEAN_SCHEDULE)
    omc.ledger_lines(_entry("dlq fills"), extra="<<<<<<< HEAD\n")
    assert ls.schedule_has_conflict() is False
    _write(omc.schedule, _CONFLICTED_SCHEDULE)
    assert ls.schedule_has_conflict() is True


def test_schedule_has_conflict_treats_an_unreadable_file_as_clean(omc, monkeypatch):
    _write(omc.schedule, _CONFLICTED_SCHEDULE)
    monkeypatch.setattr(Path, "read_text", _raise_oserror)
    assert ls.schedule_has_conflict() is False


def _raise_oserror(*_args: Any, **_kwargs: Any) -> str:
    raise OSError("simulated read fault")


# ── the pre-push credential gate ─────────────────────────────────────────────


def test_credential_scan_is_quiet_on_ordinary_ops_prose(omc):
    """A scan that fires on a normal ``fix`` would make team sync unusable."""
    omc.ledger_lines(
        _entry("checkout p99 breach", "drain the stuck SQS consumer, then scale the ASG to 6")
    )
    assert ls._credential_bearing_lines() == []


def test_credential_scan_reports_line_numbers_for_a_core_pattern(omc):
    """The AKIA shape comes from ``security.get_credential_patterns``."""
    omc.ledger_lines(
        _entry("clean lesson"),
        _entry("assume-role denied", "aws sts assume-role --access-key AKIAIOSFODNN7EXAMPLE"),
    )
    assert ls._credential_bearing_lines() == [2]


def test_credential_scan_also_catches_a_provider_shape_the_core_does_not_know(omc):
    """The union of two detectors, because neither is a superset of the other.

    Measured, the gap runs BOTH ways: the core patterns carry AKIA but not a prefixed
    Datadog application key, while this app's ``redact_tokens`` knows the provider shapes
    and not AKIA.
    """
    omc.ledger_lines(
        _entry("datadog probe", "ddapp_0123456789abcdef0123456789abcdef01234567"),
    )
    assert ls._credential_bearing_lines() == [1]


def test_credential_scan_reports_a_missing_ledger_as_clean(omc):
    assert ls._credential_bearing_lines() == []


# ── _git: the sandboxed spawn seam itself ────────────────────────────────────


class _FakeProc:
    """Minimal ``asyncio`` subprocess stand-in for ``_git``'s three exits."""

    def __init__(
        self,
        *,
        rc: int | None = 0,
        out: bytes = b"",
        err: bytes = b"",
        hang: bool = False,
    ):
        self.returncode = rc
        self._out = out
        self._err = err
        self._hang = hang
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            await asyncio.sleep(30)
        return self._out, self._err

    def kill(self) -> None:
        self.killed = True


def _install_spawn(monkeypatch, proc: Any, *, cleanup: str | None) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def _argv(argv: list[str], *_a: Any, **_k: Any):
        return list(argv), {"PATH": "/usr/bin"}, cleanup

    async def _spawn(*args: str, **kwargs: Any):
        seen.append({"argv": list(args), "kwargs": kwargs})
        if isinstance(proc, BaseException):
            raise proc
        return proc

    monkeypatch.setattr(ls, "sandboxed_spawn_argv", _argv)
    monkeypatch.setattr(ls, "create_subprocess_limited", _spawn)
    return seen


@pytest.mark.asyncio
async def test_git_decodes_both_streams_and_unlinks_the_temp_profile(omc, monkeypatch, tmp_path):
    profile = tmp_path / "sandbox-profile"
    _write(profile, "(version 1)\n")
    proc = _FakeProc(rc=0, out=b"main\n", err=b"hint\n")
    seen = _install_spawn(monkeypatch, proc, cleanup=str(profile))

    rc, out, err = await _REAL_GIT("symbolic-ref", "--short", "HEAD")

    assert (rc, out, err) == (0, "main\n", "hint\n")
    assert seen[0]["argv"] == ["git", "symbolic-ref", "--short", "HEAD"]
    assert seen[0]["kwargs"]["cwd"] == str(ls._repo_root())
    assert seen[0]["kwargs"]["env"] == {"PATH": "/usr/bin"}
    assert not profile.exists(), "the caller owns unlinking the temp profile"


@pytest.mark.asyncio
async def test_git_normalizes_a_none_returncode_to_zero(omc, monkeypatch):
    """``Popen.returncode`` can still be ``None`` right after ``communicate``."""
    _install_spawn(monkeypatch, _FakeProc(rc=None), cleanup=None)
    rc, _, _ = await _REAL_GIT("status", "--porcelain")
    assert rc == 0


@pytest.mark.asyncio
async def test_git_undecodable_output_does_not_raise(omc, monkeypatch):
    _install_spawn(monkeypatch, _FakeProc(out=b"\xff\xfe not utf8"), cleanup=None)
    rc, out, _ = await _REAL_GIT("log")
    assert rc == 0
    assert "not utf8" in out


@pytest.mark.asyncio
async def test_git_kills_and_reports_a_hung_invocation(omc, monkeypatch):
    """A hung fetch must not stall the dispatch heartbeat, which is the caller."""
    proc = _FakeProc(hang=True)
    _install_spawn(monkeypatch, proc, cleanup=None)
    monkeypatch.setattr(ls, "GIT_TIMEOUT_SECS", 0.01)

    rc, out, err = await _REAL_GIT("fetch", "--quiet", "origin", "main")

    assert rc == 124
    assert out == ""
    assert "timed out after 0.01s" in err
    assert proc.killed is True


@pytest.mark.asyncio
async def test_git_reports_a_missing_binary_instead_of_raising(omc, monkeypatch):
    _install_spawn(monkeypatch, OSError("No such file or directory: 'git'"), cleanup=None)
    rc, out, err = await _REAL_GIT("init", "-q")
    assert (rc, out) == (127, "")
    assert err.startswith("could not run git:")


# ── _align_branch ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_align_writes_tracking_when_head_is_already_right(omc):
    """The two ``config`` keys, written explicitly and by hand.

    ``git branch --set-upstream-to`` fails in BOTH ordinary first-sync states (no
    ``origin/<b>`` fetched yet, and an unborn local branch), and an empty remote is how a
    team normally starts -- so the tracking keys are written directly.
    """
    omc.enable()
    omc.git.aligned()
    assert await ls._align_branch() == ""
    assert omc.git.ran("config", "--", "branch.main.remote", "origin")
    assert omc.git.ran("config", "--", "branch.main.merge", "refs/heads/main")
    assert not omc.git.ran("branch", "-m")


@pytest.mark.asyncio
async def test_align_renames_through_branch_m_with_a_double_dash(omc):
    """``git branch -m --`` is the primitive: it keeps the sha and never touches the tree.

    The ``--`` matters on its own -- ``git symbolic-ref HEAD refs/heads/--upload-pack=evil``
    succeeds with no validation, while ``git branch -m --`` refuses it outright.
    """
    omc.enable(branch_name="team-ledger")
    omc.git.aligned().when("show-ref", rc=1)

    assert await ls._align_branch() == ""

    assert omc.git.argv_for("branch") == ("branch", "-m", "--", "team-ledger")
    assert omc.git.ran("config", "--", "branch.team-ledger.merge", "refs/heads/team-ledger")


@pytest.mark.asyncio
async def test_align_refuses_a_detached_head_and_names_the_recovery(omc):
    omc.enable()
    omc.git.when("symbolic-ref", rc=1, err="fatal: ref HEAD is not a symbolic ref")
    reason = await ls._align_branch()
    assert "detached" in reason
    assert "git switch main" in reason
    assert not omc.git.ran("branch", "-m")


@pytest.mark.asyncio
async def test_align_refuses_when_a_divergent_branch_of_that_name_exists(omc, caplog):
    """``git branch -M`` would DELETE it, collapsing two lines of ledger work into one."""
    omc.enable(branch_name="other-work")
    omc.git.aligned().when("show-ref", rc=0)

    with caplog.at_level(logging.WARNING, logger=ls.logger.name):
        reason = await ls._align_branch()

    assert "already exists" in reason
    assert "git switch other-work && git merge main" in reason
    assert "not aligned" in caplog.text
    assert not omc.git.ran("branch", "-m")
    assert not omc.git.ran("config"), "tracking must not be written for a branch we did not move"


@pytest.mark.asyncio
async def test_align_reports_a_failed_rename_without_claiming_sync_broke(omc, caplog):
    omc.enable(branch_name="team-ledger")
    omc.git.aligned().when("show-ref", rc=1).when("branch", rc=128, err="fatal: not a valid ref\n")

    with caplog.at_level(logging.WARNING, logger=ls.logger.name):
        reason = await ls._align_branch()

    assert "Could not move this repo onto team-ledger" in reason
    assert "fatal: not a valid ref" in reason
    assert "sync still publishes through an explicit refspec" in reason


@pytest.mark.asyncio
async def test_align_uses_the_validated_branch_not_the_raw_config_value(omc):
    """An option-like config value must fall back BEFORE it reaches a git argv."""
    omc.enable(branch_name="--upload-pack=evil")
    omc.git.aligned()
    assert await ls._align_branch() == ""
    assert omc.git.ran("config", "--", "branch.main.remote", "origin")
    assert not any("--upload-pack=evil" in arg for call in omc.git.calls for arg in call)


# ── _ensure_repo ─────────────────────────────────────────────────────────────


_WANTED_GITIGNORE = "*\n!.gitignore\n!ledger.jsonl\n!rotation.yaml\n"


@pytest.mark.asyncio
async def test_ensure_repo_writes_a_gitignore_that_tracks_only_shared_files(omc):
    """The dispatch index lives in this directory and must NEVER be committed."""
    omc.enable()
    omc.git.aligned()
    ok, err = await ls._ensure_repo()
    assert (ok, err) == (True, "")
    assert (omc.root / ".gitignore").read_text(encoding="utf-8") == _WANTED_GITIGNORE


@pytest.mark.asyncio
async def test_ensure_repo_repairs_a_drifted_gitignore(omc):
    omc.enable()
    omc.git.aligned()
    _write(omc.root / ".gitignore", "*\n!ledger.jsonl\n")
    await ls._ensure_repo()
    assert (omc.root / ".gitignore").read_text(encoding="utf-8") == _WANTED_GITIGNORE


@pytest.mark.asyncio
async def test_ensure_repo_leaves_a_correct_gitignore_alone(omc):
    omc.enable()
    omc.git.aligned()
    path = omc.root / ".gitignore"
    _write(path, _WANTED_GITIGNORE)
    stamp = path.stat().st_mtime_ns
    await ls._ensure_repo()
    assert path.stat().st_mtime_ns == stamp


@pytest.mark.asyncio
async def test_ensure_repo_initializes_only_when_dot_git_is_missing(omc):
    omc.enable()
    omc.git.aligned()
    omc.init_repo()
    await ls._ensure_repo()
    assert not omc.git.ran("init")


@pytest.mark.asyncio
async def test_ensure_repo_reports_a_failed_init(omc):
    omc.enable()
    omc.git.when("init", rc=1, err="fatal: cannot mkdir\n")
    ok, err = await ls._ensure_repo()
    assert ok is False
    assert err == "git init failed: fatal: cannot mkdir"


@pytest.mark.asyncio
async def test_ensure_repo_adds_the_remote_when_there_is_none(omc):
    omc.enable()
    omc.git.aligned().when("remote", "get-url", rc=2)
    ok, _ = await ls._ensure_repo()
    assert ok is True
    assert omc.git.argv_for("remote", "add") == ("remote", "add", "origin", _REMOTE)


@pytest.mark.asyncio
async def test_ensure_repo_reports_a_failed_remote_add(omc):
    omc.enable()
    omc.git.when("remote", "get-url", rc=2).when("remote", "add", rc=3, err="bad url\n")
    ok, err = await ls._ensure_repo()
    assert (ok, err) == (False, "git remote add failed: bad url")


@pytest.mark.asyncio
async def test_ensure_repo_follows_the_operator_changing_the_remote(omc):
    omc.enable()
    omc.git.aligned().when("remote", "get-url", out="git@example.com:old/repo.git\n")
    ok, _ = await ls._ensure_repo()
    assert ok is True
    assert omc.git.argv_for("remote", "set-url") == ("remote", "set-url", "origin", _REMOTE)


@pytest.mark.asyncio
async def test_ensure_repo_reports_a_failed_remote_set_url(omc):
    omc.enable()
    omc.git.when("remote", "get-url", out="git@example.com:old/repo.git\n").when(
        "remote", "set-url", rc=4, err="cannot write config\n"
    )
    ok, err = await ls._ensure_repo()
    assert (ok, err) == (False, "git remote set-url failed: cannot write config")


@pytest.mark.asyncio
async def test_ensure_repo_leaves_an_unchanged_remote_alone(omc):
    omc.enable()
    omc.git.aligned().when("remote", "get-url", out=f"{_REMOTE}\n")
    await ls._ensure_repo()
    assert not omc.git.ran("remote", "set-url")
    assert not omc.git.ran("remote", "add")


@pytest.mark.asyncio
async def test_an_alignment_refusal_is_stashed_not_returned_as_a_failure(omc):
    """This fix must not be able to make the app worse than it already was.

    Publishing has always worked through explicit refspecs, so a refusal to align cannot
    be allowed to stop it -- the reason goes to ``status()`` instead.
    """
    omc.enable()
    omc.git.when("symbolic-ref", rc=1)
    ok, err = await ls._ensure_repo()
    assert (ok, err) == (True, "")
    assert "detached" in ls._align_refusal


# ── resolve_conflict ─────────────────────────────────────────────────────────


def test_resolve_conflict_rewrites_the_file_from_the_reconciled_entries(omc):
    """The reconciled view already exists on READ; this makes it durable for git."""
    first, second = _entry("dlq fills"), _entry("throttled writes", "raise the concurrency")
    body = json.dumps(first.to_dict()) + "\n"
    body += "<<<<<<< HEAD\n"
    body += json.dumps(second.to_dict()) + "\n"
    body += "=======\n>>>>>>> origin/main\n"
    _write(ledger.ledger_path(), body)
    assert ls.has_conflict() is True

    kept = ls.resolve_conflict()

    assert kept == 2
    assert ls.has_conflict() is False
    patterns = sorted(e.pattern for e in ledger.read_entries())
    assert patterns == ["dlq fills", "throttled writes"]
    assert omc.audit.log_api_access.call_args.kwargs["operation"] == "ledger_sync_resolve"


def test_resolve_conflict_is_safe_with_nothing_to_resolve(omc):
    omc.ledger_lines(_entry("dlq fills"))
    assert ls.resolve_conflict() == 1


# ── _resolve_schedule_conflict ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schedule_resolution_is_a_noop_without_a_conflict(omc):
    _write(omc.schedule, _CLEAN_SCHEDULE)
    assert await ls._resolve_schedule_conflict() is False
    assert not omc.git.ran("checkout")


@pytest.mark.asyncio
async def test_schedule_resolution_takes_theirs_and_audits_it(omc, caplog):
    """A shift is a single-owner fact, so one edit has to lose -- and it must be the local one.

    Converging on the remote keeps every instance's view of who is on call identical,
    which is the property that makes the file usable as a lock.
    """
    _write(omc.schedule, _CONFLICTED_SCHEDULE)
    with caplog.at_level(logging.WARNING, logger=ls.logger.name):
        assert await ls._resolve_schedule_conflict() is True

    assert omc.git.argv_for("checkout") == ("checkout", "--theirs", "--", "rotation.yaml")
    assert omc.git.ran("add", "--", "rotation.yaml")
    assert omc.audit.log_api_access.call_args.kwargs["resources"] == "resolution=theirs"
    assert "re-apply and push it" in caplog.text


@pytest.mark.asyncio
async def test_schedule_resolution_reports_a_failed_checkout(omc, caplog):
    _write(omc.schedule, _CONFLICTED_SCHEDULE)
    omc.git.when("checkout", rc=1, err="error: path not in the index\n")
    with caplog.at_level(logging.WARNING, logger=ls.logger.name):
        assert await ls._resolve_schedule_conflict() is False
    assert "could not take the remote schedule" in caplog.text
    assert not omc.git.ran("add")


# ── _stage_and_commit ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stage_and_commit_stages_the_whole_tracked_set(omc):
    """Staging only the ledger left ``rotation.yaml`` committed nowhere -- it is un-ignored
    specifically so it can sync, so a ledger-only ``git add`` silently stranded it."""
    omc.ledger_lines(_entry("dlq fills"))
    _write(omc.schedule, _CLEAN_SCHEDULE)
    _write(omc.root / ".gitignore", _WANTED_GITIGNORE)
    omc.git.when("status", out=" M ledger.jsonl\n")

    assert await ls._stage_and_commit("update ops ledger") is True

    for name in ls.TRACKED_FILES:
        assert omc.git.ran("add", "--", name)
    assert omc.git.argv_for("commit") == (
        "commit",
        "--no-edit",
        "-q",
        "-m",
        "update ops ledger",
    )


@pytest.mark.asyncio
async def test_stage_and_commit_skips_files_that_do_not_exist(omc):
    omc.ledger_lines(_entry("dlq fills"))
    omc.git.when("status", out=" M ledger.jsonl\n")
    await ls._stage_and_commit("update ops ledger")
    assert omc.git.ran("add", "--", "ledger.jsonl")
    assert not omc.git.ran("add", "--", "rotation.yaml")


@pytest.mark.asyncio
async def test_stage_and_commit_returns_false_on_a_clean_tree(omc):
    omc.git.when("status", out="")
    assert await ls._stage_and_commit("update ops ledger") is False
    assert not omc.git.ran("commit")


@pytest.mark.asyncio
async def test_stage_and_commit_commits_anyway_when_the_caller_needs_a_merge_recorded(omc):
    omc.git.when("status", out="")
    assert await ls._stage_and_commit("merge team ledger", allow_empty_message_only=True) is True
    assert omc.git.ran("commit")


@pytest.mark.asyncio
async def test_stage_and_commit_swallows_nothing_to_commit(omc, caplog):
    omc.git.when("status", out=" M ledger.jsonl\n").when(
        "commit", rc=1, err="nothing to commit, working tree clean\n"
    )
    with caplog.at_level(logging.DEBUG, logger=ls.logger.name):
        assert await ls._stage_and_commit("update ops ledger") is False
    assert "commit skipped" not in caplog.text, "the ordinary no-op must stay silent"


@pytest.mark.asyncio
async def test_stage_and_commit_logs_a_real_commit_failure(omc, caplog):
    omc.git.when("status", out=" M ledger.jsonl\n").when(
        "commit", rc=1, err="fatal: could not read Username\n"
    )
    with caplog.at_level(logging.DEBUG, logger=ls.logger.name):
        assert await ls._stage_and_commit("update ops ledger") is False
    assert "commit skipped" in caplog.text


# ── pull ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pull_refuses_when_sync_is_not_configured(omc):
    assert await ls.pull() == (False, "ledger sync is not configured")
    assert omc.git.calls == []


@pytest.mark.asyncio
async def test_pull_propagates_an_ensure_repo_failure(omc):
    omc.enable()
    omc.git.when("init", rc=1, err="fatal: cannot mkdir\n")
    ok, detail = await ls.pull()
    assert ok is False
    assert detail.startswith("git init failed:")


@pytest.mark.asyncio
async def test_pull_merges_and_reports_success(omc):
    omc.enable()
    omc.git.aligned()
    assert await ls.pull() == (True, "pulled")
    assert omc.git.argv_for("fetch") == ("fetch", "--quiet", "origin", "main")
    assert omc.git.argv_for("merge") == (
        "merge",
        "--no-edit",
        "--allow-unrelated-histories",
        "origin/main",
    )


@pytest.mark.asyncio
async def test_pull_treats_an_empty_remote_as_the_normal_first_use(omc):
    omc.enable()
    omc.git.aligned().when(
        "fetch", rc=128, err="fatal: couldn't find remote ref main\n"
    )
    ok, detail = await ls.pull()
    assert ok is True
    assert detail == "remote has no ledger branch yet (first sync will create it)"
    assert not omc.git.ran("merge")


@pytest.mark.asyncio
async def test_pull_reports_an_unreachable_remote(omc):
    omc.enable()
    omc.git.aligned().when("fetch", rc=128, err="fatal: Could not resolve hostname\n")
    ok, detail = await ls.pull()
    assert ok is False
    assert detail == "fetch failed: fatal: Could not resolve hostname"


@pytest.mark.asyncio
async def test_pull_commits_local_work_before_merging(omc):
    """Without this an instance that recorded ONE lesson could never pull, permanently:
    git refuses a merge that would overwrite an untracked working-tree file."""
    omc.enable()
    omc.ledger_lines(_entry("dlq fills"))
    omc.git.aligned().when("status", out=" M ledger.jsonl\n")

    await ls.pull()

    order = [call[0] for call in omc.git.calls]
    assert order.index("commit") < order.index("merge")


@pytest.mark.asyncio
async def test_pull_reconciles_a_conflicted_ledger_instead_of_failing(omc):
    omc.enable()
    first, second = _entry("dlq fills"), _entry("throttled writes", "raise the concurrency")
    body = json.dumps(first.to_dict()) + "\n<<<<<<< HEAD\n"
    body += json.dumps(second.to_dict()) + "\n=======\n>>>>>>> origin/main\n"
    _write(ledger.ledger_path(), body)
    omc.git.aligned().when("merge", rc=1, err="CONFLICT (add/add): ledger.jsonl\n")

    ok, detail = await ls.pull()

    assert ok is True
    assert detail == "merged with conflict, reconciled to 2 entries"
    assert ls.has_conflict() is False


@pytest.mark.asyncio
async def test_pull_reports_both_conflicts_when_both_happen(omc):
    omc.enable()
    omc.ledger_lines(_entry("dlq fills"), extra="<<<<<<< HEAD\n")
    _write(omc.schedule, _CONFLICTED_SCHEDULE)
    omc.git.aligned().when("merge", rc=1, err="CONFLICT\n")

    ok, detail = await ls.pull()

    assert ok is True
    assert "reconciled to 1 entries" in detail
    assert "schedule conflict resolved to the remote's version" in detail


@pytest.mark.asyncio
async def test_pull_resolves_a_schedule_only_conflict(omc):
    omc.enable()
    omc.ledger_lines(_entry("dlq fills"))
    _write(omc.schedule, _CONFLICTED_SCHEDULE)
    omc.git.aligned().when("merge", rc=1, err="CONFLICT (content): rotation.yaml\n")

    ok, detail = await ls.pull()

    assert (ok, detail) == (True, "schedule conflict resolved to the remote's version")


@pytest.mark.asyncio
async def test_pull_reports_a_merge_failure_it_cannot_reconcile(omc):
    omc.enable()
    omc.git.aligned().when("merge", rc=1, err="fatal: Not possible to fast-forward\n")
    ok, detail = await ls.pull()
    assert ok is False
    assert detail == "merge failed: fatal: Not possible to fast-forward"


# ── push ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_push_refuses_when_sync_is_not_configured(omc):
    assert await ls.push() == (False, "ledger sync is not configured")
    assert omc.git.calls == []


@pytest.mark.asyncio
async def test_push_propagates_an_ensure_repo_failure(omc):
    omc.enable()
    omc.git.when("init", rc=1, err="fatal: cannot mkdir\n")
    ok, detail = await ls.push()
    assert ok is False
    assert detail.startswith("git init failed:")


@pytest.mark.asyncio
async def test_push_publishes_through_an_explicit_refspec(omc):
    omc.enable(branch_name="team-ledger")
    omc.ledger_lines(_entry("dlq fills"))
    omc.git.aligned().when("show-ref", rc=1).when("status", out=" M ledger.jsonl\n")

    assert await ls.push() == (True, "pushed")

    assert omc.git.argv_for("push") == ("push", "--quiet", "origin", "HEAD:team-ledger")
    kwargs = omc.audit.log_api_access.call_args.kwargs
    assert kwargs["operation"] == "ledger_sync_push"
    assert kwargs["outcome"] == "success"
    assert kwargs["resources"] == f"remote={_REMOTE} branch=team-ledger"


@pytest.mark.asyncio
async def test_push_reconciles_a_conflicted_ledger_before_publishing(omc):
    """Teammates must never receive a file holding markers."""
    omc.enable()
    omc.ledger_lines(_entry("dlq fills"), extra="<<<<<<< HEAD\n=======\n")
    omc.git.aligned().when("status", out=" M ledger.jsonl\n")

    assert await ls.push() == (True, "pushed")
    assert ls.has_conflict() is False


@pytest.mark.asyncio
async def test_push_refuses_a_conflicted_schedule(omc, caplog):
    """One bad push costs the whole team its on-call gating, and "theirs" is already corrupt.

    Unlike the ledger there is no union to compute, so this refuses rather than guesses.
    """
    omc.enable()
    omc.ledger_lines(_entry("dlq fills"))
    _write(omc.schedule, _CONFLICTED_SCHEDULE)

    with caplog.at_level(logging.ERROR, logger=ls.logger.name):
        ok, detail = await ls.push()

    assert ok is False
    assert detail.startswith("refused: rotation.yaml holds conflict markers")
    assert "resolve it first" in detail
    assert "refusing to push" in caplog.text
    assert omc.audit.log_api_access.call_args.kwargs["outcome"] == "refused"
    assert not omc.git.ran("push")


@pytest.mark.asyncio
async def test_push_refuses_credential_material_and_never_echoes_it(omc, caplog):
    """The pre-push half of the redaction defence, for rows the write path cannot reach.

    Recovery from a published secret is a history rewrite across every teammate's clone,
    so refusing -- which costs one operator a manual fix -- is the cheaper side.
    """
    omc.enable()
    secret = "AKIAIOSFODNN7EXAMPLE"
    omc.ledger_lines(_entry("assume-role denied", f"aws sts assume-role --access-key {secret}"))
    omc.git.aligned()

    with caplog.at_level(logging.ERROR, logger=ls.logger.name):
        ok, detail = await ls.push()

    assert ok is False
    assert "line(s) 1" in detail
    assert secret not in detail, "the refusal must not copy the secret into the console"
    assert secret not in caplog.text, "nor into the log"
    resources = omc.audit.log_api_access.call_args.kwargs["resources"]
    assert resources == "reason=credential_in_ledger.jsonl lines=[1]"
    assert not omc.git.ran("push")


@pytest.mark.asyncio
async def test_push_is_a_noop_when_the_tree_is_clean_and_nothing_is_unpushed(omc):
    omc.enable()
    omc.git.aligned().when("status", out="").when("rev-list", out="0\n")
    assert await ls.push() == (True, "nothing to push")
    assert not omc.git.ran("push")


@pytest.mark.asyncio
async def test_push_does_not_strand_a_commit_a_previous_run_failed_to_send(omc):
    """A clean tree is not proof everything is shared."""
    omc.enable()
    omc.git.aligned().when("status", out="").when("rev-list", out="2\n")
    assert await ls.push() == (True, "pushed")
    assert omc.git.ran("push")


@pytest.mark.asyncio
async def test_push_reports_a_rejected_push(omc):
    omc.enable()
    omc.ledger_lines(_entry("dlq fills"))
    omc.git.aligned().when("status", out=" M ledger.jsonl\n").when(
        "push", rc=1, err="! [rejected] main -> main (fetch first)\n"
    )
    ok, detail = await ls.push()
    assert ok is False
    assert detail == "push failed: ! [rejected] main -> main (fetch first)"


# ── _has_unpushed ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_has_unpushed_counts_commits_the_remote_lacks(omc):
    omc.enable()
    omc.git.when("rev-list", out="3\n")
    assert await ls._has_unpushed() is True
    assert omc.git.argv_for("rev-list") == ("rev-list", "--count", "origin/main..HEAD")


@pytest.mark.asyncio
async def test_has_unpushed_is_false_when_everything_is_shared(omc):
    omc.enable()
    omc.git.when("rev-list", out="0\n")
    assert await ls._has_unpushed() is False


@pytest.mark.asyncio
async def test_has_unpushed_is_false_on_empty_output(omc):
    omc.enable()
    omc.git.when("rev-list", out="\n")
    assert await ls._has_unpushed() is False


@pytest.mark.asyncio
async def test_has_unpushed_assumes_yes_when_the_ref_is_unknown(omc):
    """A redundant push is cheap; skipping a needed one strands a lesson forever."""
    omc.enable()
    omc.git.when("rev-list", rc=128, err="fatal: bad revision\n")
    assert await ls._has_unpushed() is True


@pytest.mark.asyncio
async def test_has_unpushed_assumes_yes_on_unparseable_output(omc):
    omc.enable()
    omc.git.when("rev-list", out="not a number\n")
    assert await ls._has_unpushed() is True


# ── sync_safely ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_safely_is_a_quiet_noop_when_unconfigured(omc):
    assert await ls.sync_safely() == ""


@pytest.mark.asyncio
async def test_sync_safely_returns_the_pull_detail(omc, monkeypatch):
    omc.enable()

    async def _pull():
        return True, "pulled"

    monkeypatch.setattr(ls, "pull", _pull)
    assert await ls.sync_safely(direction="pull") == "pulled"


@pytest.mark.asyncio
async def test_sync_safely_routes_push_to_push(omc, monkeypatch):
    omc.enable()
    seen: list[str] = []

    async def _push():
        seen.append("push")
        return True, "pushed"

    async def _pull():  # pragma: no cover - must not be reached
        raise AssertionError("direction=push must not pull")

    monkeypatch.setattr(ls, "push", _push)
    monkeypatch.setattr(ls, "pull", _pull)
    assert await ls.sync_safely(direction="push") == "pushed"
    assert seen == ["push"]


@pytest.mark.asyncio
async def test_sync_safely_warns_but_still_returns_a_failure_detail(omc, monkeypatch, caplog):
    omc.enable()

    async def _pull():
        return False, "fetch failed: no route to host"

    monkeypatch.setattr(ls, "pull", _pull)
    with caplog.at_level(logging.WARNING, logger=ls.logger.name):
        assert await ls.sync_safely() == "fetch failed: no route to host"
    assert "no route to host" in caplog.text


@pytest.mark.asyncio
async def test_sync_safely_retries_once_through_a_transient_spawn_fault(omc, monkeypatch):
    """The sandbox backend probe raises a self-described TRANSIENT error on a cold cache.

    A real roundtrip hit this on EVERY first push in a fresh process, so the whole first
    sync failed for a condition that resolves in milliseconds.
    """
    omc.enable()
    attempts: list[int] = []

    async def _pull():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("sandbox backend cache warming, please retry")
        return True, "pulled"

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(ls, "pull", _pull)
    monkeypatch.setattr(ls.asyncio, "sleep", _no_sleep)

    assert await ls.sync_safely() == "pulled"
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_sync_safely_swallows_a_permanent_fault(omc, monkeypatch, caplog):
    """Shared memory is worth having; it is never worth failing a dispatch cycle over."""
    omc.enable()

    async def _pull():
        raise RuntimeError("permission denied (publickey)")

    monkeypatch.setattr(ls, "pull", _pull)
    with caplog.at_level(logging.ERROR, logger=ls.logger.name):
        assert await ls.sync_safely() == "pull errored"
    assert "ledger pull failed" in caplog.text


@pytest.mark.asyncio
async def test_sync_safely_gives_up_after_the_single_bounded_retry(omc, monkeypatch, caplog):
    """The retry is bounded at one so it cannot mask a genuine, repeating fault."""
    omc.enable()
    attempts: list[int] = []

    async def _push():
        attempts.append(1)
        raise RuntimeError("transient backend fault")

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(ls, "push", _push)
    monkeypatch.setattr(ls.asyncio, "sleep", _no_sleep)
    with caplog.at_level(logging.ERROR, logger=ls.logger.name):
        assert await ls.sync_safely(direction="push") == "push errored"
    assert len(attempts) == 2
