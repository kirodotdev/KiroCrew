"""Per-job project directory for agent cron jobs (``CronJob.project_dir``).

Without a project directory an LLM-message cron runs in the gateway's default
working directory -- a Kiro Crew-internal scratch path with no
``.kiro/steering/`` of its own -- so a job doing real repository work never
loads that repository's steering (kiro-cli resolves an agent's steering
resources relative to its cwd). This module pins the field end to end:

* the store validates and persists a RESOLVED path, refuses what a session
  could not be rooted at, and refuses the field on script/command jobs;
* the gateway roots the job's session at that path (``cwd=`` on
  ``get_or_create``), resets a live persistent session whose cwd moved, and
  FAILS a run whose directory is gone rather than silently running unscoped;
* the MCP ``cron_add``/``cron_update`` tools and ``kirocrew cron add``/``update``
  carry the field to that one store.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronJob, CronSchedule, CronService, validate_cron_project_dir
from kiro_crew.session import ConversationRoot

# ── Store ──


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path / "store")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    d = tmp_path / "repo"
    (d / ".kiro" / "steering").mkdir(parents=True)
    return d


class TestValidator:
    def test_empty_and_none_mean_unset(self):
        assert validate_cron_project_dir("") == ""
        assert validate_cron_project_dir("   ") == ""
        assert validate_cron_project_dir(None) == ""

    def test_returns_realpath(self, repo: Path, tmp_path: Path):
        link = tmp_path / "link"
        os.symlink(repo, link, target_is_directory=True)
        if os.name == "nt":
            # On Windows a reparse point is never resolved (its target may be a
            # share, and resolving it is an outbound authentication): the
            # linked spelling is refused and the caller names the real path.
            with pytest.raises(ValueError, match="reparse point"):
                validate_cron_project_dir(str(link))
            assert validate_cron_project_dir(str(repo)) == os.path.realpath(repo)
            return
        assert validate_cron_project_dir(str(link)) == os.path.realpath(repo)

    def test_expands_tilde(self, repo: Path, monkeypatch):
        monkeypatch.setenv("HOME", str(repo.parent))
        monkeypatch.setenv("USERPROFILE", str(repo.parent))
        assert validate_cron_project_dir("~/repo") == os.path.realpath(repo)

    def test_relative_path_refused(self):
        with pytest.raises(ValueError, match="absolute"):
            validate_cron_project_dir("repo")

    def test_missing_directory_refused(self, tmp_path: Path):
        with pytest.raises(ValueError, match="existing directory"):
            validate_cron_project_dir(str(tmp_path / "gone"))

    def test_canonical_value_is_capped_too(self, repo: Path, monkeypatch):
        # The cap bounds what is RETAINED. Canonicalisation can lengthen the
        # input (``~`` expands; a Windows 8.3 short-name spelling expands), so
        # an input under the cap whose canonical form is over it is refused.
        from kiro_crew.cron import MAX_PROJECT_DIR_LEN

        long_canonical = os.path.realpath(repo) + os.sep + ("x" * MAX_PROJECT_DIR_LEN)
        monkeypatch.setattr("kiro_crew.cron.resolve_project_dir", lambda *a, **k: long_canonical)
        with pytest.raises(ValueError, match="exceeds"):
            validate_cron_project_dir(str(repo))

    def test_file_refused(self, tmp_path: Path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        with pytest.raises(ValueError, match="existing directory"):
            validate_cron_project_dir(str(f))

    def test_sensitive_path_refused(self, repo: Path, monkeypatch):
        monkeypatch.setattr("kiro_crew.security.is_sensitive_canonical_path", lambda p: True)
        with pytest.raises(ValueError, match="sensitive"):
            validate_cron_project_dir(str(repo))

    def test_uses_the_canonical_path_gate_not_the_bounded_resolver(self, repo: Path, monkeypatch):
        # The validator computed the realpath itself. Re-submitting it to the
        # bounded mc-pathres pool via is_sensitive_path would fail CLOSED on a
        # budget miss (simultaneous fires), refusing a healthy directory.
        def _bounded_must_not_run(*a, **k):
            raise AssertionError("is_sensitive_path (bounded resolver) must not be consulted")

        seen: list[str] = []

        def _canonical(p):
            seen.append(p)
            return False

        monkeypatch.setattr("kiro_crew.security.is_sensitive_path", _bounded_must_not_run)
        monkeypatch.setattr("kiro_crew.security.is_sensitive_canonical_path", _canonical)
        assert validate_cron_project_dir(str(repo)) == os.path.realpath(repo)
        if os.name == "nt":
            # The pinned branch asks the gate twice: the lexical spelling before
            # anything is opened, then the handle's answer.
            assert seen == [os.path.normpath(str(repo)), os.path.realpath(repo)]
        else:
            assert seen == [os.path.realpath(repo)]

    def test_sensitive_path_refusal_is_audited_with_the_calling_surface(
        self, repo: Path, monkeypatch
    ):
        # A security decision, not input validation: the refusal lands in the
        # SEL like the sibling slot-project and chat-folder validators' do,
        # attributed to whoever asked. Redaction of the path is asserted by
        # handing the redactor a marker.
        monkeypatch.setattr("kiro_crew.security.is_sensitive_canonical_path", lambda p: True)
        events: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: events.append(kw)
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: fake_sel)
        with pytest.raises(ValueError, match="sensitive"):
            validate_cron_project_dir(str(repo), audit_caller="cli")
        assert len(events) == 1
        ev = events[0]
        assert ev["caller"] == "cli"
        assert ev["operation"] == "cron.project_dir"
        assert ev["outcome"] == "denied"
        assert ev["error"] == "sensitive path"
        assert os.path.realpath(repo) in ev["resources"]

    def test_sensitive_path_refusal_defaults_the_caller_to_the_store(self, repo: Path, monkeypatch):
        monkeypatch.setattr("kiro_crew.security.is_sensitive_canonical_path", lambda p: True)
        events: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: events.append(kw)
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: fake_sel)
        with pytest.raises(ValueError):
            validate_cron_project_dir(str(repo))
        assert events[0]["caller"] == "cron_store"

    def test_audit_failure_never_masks_the_refusal(self, repo: Path, monkeypatch):
        monkeypatch.setattr("kiro_crew.security.is_sensitive_canonical_path", lambda p: True)

        def _boom():
            raise RuntimeError("sel down")

        monkeypatch.setattr("kiro_crew.sel.sel", _boom)
        with pytest.raises(ValueError, match="sensitive"):
            validate_cron_project_dir(str(repo))

    def test_ordinary_refusals_are_not_audited(self, tmp_path: Path, monkeypatch):
        # Input validation (a missing directory) is not a permission decision.
        events: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: events.append(kw)
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: fake_sel)
        with pytest.raises(ValueError):
            validate_cron_project_dir(str(tmp_path / "gone"))
        assert events == []

    @pytest.mark.parametrize("unc", [r"\\server\share", "//server/share", r"/\server\share"])
    def test_unc_refused_before_resolution(self, unc: str):
        with pytest.raises(ValueError, match="UNC"):
            validate_cron_project_dir(unc)

    def test_non_string_refused(self):
        with pytest.raises(ValueError, match="must be a string"):
            validate_cron_project_dir(42)  # type: ignore[arg-type]

    def test_oversize_refused(self):
        with pytest.raises(ValueError, match="exceeds"):
            validate_cron_project_dir("/" + "a" * 5000)


class TestStore:
    def test_add_persists_resolved_path_and_round_trips(self, tmp_path: Path, repo: Path):
        svc = CronService(base_dir=tmp_path / "store")
        link = tmp_path / "link"
        os.symlink(repo, link, target_is_directory=True)
        # Windows never resolves a reparse point (see ``test_returns_realpath``);
        # the store round-trip is what this test pins, so submit the real path.
        submitted = str(repo) if os.name == "nt" else str(link)
        job = svc.add_job(name="j", message="m", every_secs=3600, project_dir=submitted)
        assert job.project_dir == os.path.realpath(repo)
        reloaded = CronService(base_dir=tmp_path / "store").list_jobs()[0]
        assert reloaded.project_dir == os.path.realpath(repo)

    def test_default_is_unset(self, tmp_path: Path):
        svc = CronService(base_dir=tmp_path / "store")
        job = svc.add_job(name="j", message="m", every_secs=3600)
        assert job.project_dir == ""

    def test_legacy_record_without_field_reads_unset(self, tmp_path: Path):
        svc = CronService(base_dir=tmp_path / "store")
        svc.add_job(name="j", message="m", every_secs=3600)
        # Drop the key as an older build's store would lack it.
        import json

        store_file = tmp_path / "store" / "crons.json"
        data = json.loads(store_file.read_text())
        data["jobs"][0].pop("project_dir", None)
        store_file.write_text(json.dumps(data))
        assert CronService(base_dir=tmp_path / "store").list_jobs()[0].project_dir == ""

    def test_add_refuses_missing_dir_and_writes_nothing(self, tmp_path: Path):
        svc = CronService(base_dir=tmp_path / "store")
        with pytest.raises(ValueError, match="existing directory"):
            svc.add_job(name="j", message="m", every_secs=3600, project_dir=str(tmp_path / "x"))
        assert svc.list_jobs() == []

    @pytest.mark.parametrize("kind", ["command", "script"])
    def test_add_refuses_project_dir_on_zero_token_job(self, tmp_path: Path, repo: Path, kind: str):
        svc = CronService(base_dir=tmp_path / "store")
        extra = {"command": "true"} if kind == "command" else {"script": "x.py:run"}
        with pytest.raises(ValueError, match="agent .*job"):
            svc.add_job(name="j", message="", every_secs=3600, project_dir=str(repo), **extra)
        assert svc.list_jobs() == []

    def test_update_sets_and_clears(self, tmp_path: Path, repo: Path):
        svc = CronService(base_dir=tmp_path / "store")
        job = svc.add_job(name="j", message="m", every_secs=3600)
        updated = svc.update_job(job.id, project_dir=str(repo))
        assert updated is not None and updated.project_dir == os.path.realpath(repo)
        cleared = svc.update_job(job.id, project_dir="")
        assert cleared is not None and cleared.project_dir == ""
        assert CronService(base_dir=tmp_path / "store").list_jobs()[0].project_dir == ""

    # -- the rescope marker: how the gateway tells "cleared" from "never set" --

    def test_setting_a_project_on_a_never_scoped_job_leaves_no_marker(
        self, tmp_path: Path, repo: Path
    ):
        svc = CronService(base_dir=tmp_path / "store")
        job = svc.add_job(name="j", message="m", every_secs=3600)
        assert job.project_dir_was == ""
        updated = svc.update_job(job.id, project_dir=str(repo))
        assert updated is not None and updated.project_dir_was == ""

    def test_clearing_a_project_records_where_the_conversation_began(
        self, tmp_path: Path, repo: Path
    ):
        svc = CronService(base_dir=tmp_path / "store")
        job = svc.add_job(name="j", message="m", every_secs=3600, project_dir=str(repo))
        cleared = svc.update_job(job.id, project_dir="")
        assert cleared is not None
        assert cleared.project_dir == ""
        assert cleared.project_dir_was == os.path.realpath(repo)
        # Durable: the gateway reads it after a restart too.
        reloaded = CronService(base_dir=tmp_path / "store").list_jobs()[0]
        assert reloaded.project_dir_was == os.path.realpath(repo)

    def test_changing_a_project_records_the_old_one(self, tmp_path: Path, repo: Path):
        other = tmp_path / "other"
        other.mkdir()
        svc = CronService(base_dir=tmp_path / "store")
        job = svc.add_job(name="j", message="m", every_secs=3600, project_dir=str(repo))
        moved = svc.update_job(job.id, project_dir=str(other))
        assert moved is not None
        assert moved.project_dir == os.path.realpath(other)
        assert moved.project_dir_was == os.path.realpath(repo)

    def test_a_second_rescope_before_a_wake_keeps_the_first_old_project(
        self, tmp_path: Path, repo: Path
    ):
        # A -> B -> C without a wake in between: a stored conversation may still
        # sit in A, so A is what the marker keeps until the gateway settles it.
        other = tmp_path / "other"
        other.mkdir()
        third = tmp_path / "third"
        third.mkdir()
        svc = CronService(base_dir=tmp_path / "store")
        job = svc.add_job(name="j", message="m", every_secs=3600, project_dir=str(repo))
        svc.update_job(job.id, project_dir=str(other))
        again = svc.update_job(job.id, project_dir=str(third))
        assert again is not None and again.project_dir_was == os.path.realpath(repo)

    def test_re_setting_the_same_project_is_not_a_rescope(self, tmp_path: Path, repo: Path):
        svc = CronService(base_dir=tmp_path / "store")
        job = svc.add_job(name="j", message="m", every_secs=3600, project_dir=str(repo))
        same = svc.update_job(job.id, project_dir=str(repo))
        assert same is not None and same.project_dir_was == ""

    def test_the_gateway_clears_the_marker_once_settled(self, tmp_path: Path, repo: Path):
        svc = CronService(base_dir=tmp_path / "store")
        job = svc.add_job(name="j", message="m", every_secs=3600, project_dir=str(repo))
        svc.update_job(job.id, project_dir="")
        settled = svc.update_job(
            job.id, project_dir_was="", expect_project_scope=("", os.path.realpath(repo))
        )
        assert settled is not None and settled.project_dir_was == ""
        assert CronService(base_dir=tmp_path / "store").list_jobs()[0].project_dir_was == ""

    def test_a_rescope_that_landed_during_the_wake_survives_the_clear(
        self, tmp_path: Path, repo: Path
    ):
        # The GPT scenario: A -> B (marker A). A wake reads (B, A) and runs.
        # Meanwhile B -> "" lands; the store keeps the FIRST marker, so the job
        # is now ("", A). The wake's clear, conditioned on what it read, is
        # refused and the marker stays -- the next wake retires the conversation
        # still rooted in B. Without the condition it would be erased and the
        # job would run in B for good, unpoliced.
        from kiro_crew.cron import CronPendingMismatch

        other = tmp_path / "other"
        other.mkdir()
        svc = CronService(base_dir=tmp_path / "store")
        job = svc.add_job(name="j", message="m", every_secs=3600, project_dir=str(repo))
        svc.update_job(job.id, project_dir=str(other))
        wake_read = (os.path.realpath(other), os.path.realpath(repo))
        svc.update_job(job.id, project_dir="")  # lands during the wake
        with pytest.raises(CronPendingMismatch, match="project scope"):
            svc.update_job(job.id, project_dir_was="", expect_project_scope=wake_read)
        reloaded = CronService(base_dir=tmp_path / "store").list_jobs()[0]
        assert reloaded.project_dir == ""
        assert reloaded.project_dir_was == os.path.realpath(repo)

    def test_the_marker_cannot_be_written_only_cleared(self, tmp_path: Path, repo: Path):
        # Its content is always a project_dir this store already validated; a
        # caller planting one would make an unscoped job policed on the next
        # wake against a directory nobody validated.
        svc = CronService(base_dir=tmp_path / "store")
        job = svc.add_job(name="j", message="m", every_secs=3600)
        with pytest.raises(ValueError, match="store-managed"):
            svc.update_job(job.id, project_dir_was=str(repo))
        assert CronService(base_dir=tmp_path / "store").list_jobs()[0].project_dir_was == ""

    def test_legacy_record_without_the_marker_reads_unset(self, tmp_path: Path):
        svc = CronService(base_dir=tmp_path / "store")
        svc.add_job(name="j", message="m", every_secs=3600)
        store_file = tmp_path / "store" / "crons.json"
        data = json.loads(store_file.read_text())
        data["jobs"][0].pop("project_dir_was", None)
        store_file.write_text(json.dumps(data))
        assert CronService(base_dir=tmp_path / "store").list_jobs()[0].project_dir_was == ""

    def test_update_refusal_strands_no_other_field(self, tmp_path: Path):
        svc = CronService(base_dir=tmp_path / "store")
        job = svc.add_job(name="j", message="m", every_secs=3600)
        with pytest.raises(ValueError):
            svc.update_job(job.id, name="renamed", project_dir="relative/path")
        reloaded = CronService(base_dir=tmp_path / "store").list_jobs()[0]
        assert reloaded.name == "j"
        assert reloaded.project_dir == ""

    def test_update_refuses_project_dir_on_command_job(self, tmp_path: Path, repo: Path):
        svc = CronService(base_dir=tmp_path / "store")
        job = svc.add_job(name="j", message="", every_secs=3600, command="true")
        with pytest.raises(ValueError, match="agent .*job"):
            svc.update_job(job.id, project_dir=str(repo))

    def test_update_non_string_refused(self, tmp_path: Path):
        svc = CronService(base_dir=tmp_path / "store")
        job = svc.add_job(name="j", message="m", every_secs=3600)
        with pytest.raises(ValueError, match="must be a string"):
            svc.update_job(job.id, project_dir=["/x"])

    @staticmethod
    def _capture_sel(monkeypatch) -> list[dict]:
        events: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: events.append(kw)
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: fake_sel)
        return events

    def test_add_attributes_the_denial_to_the_caller_it_was_given(
        self, tmp_path: Path, repo: Path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.security.is_sensitive_canonical_path", lambda p: True)
        events = self._capture_sel(monkeypatch)
        svc = CronService(base_dir=tmp_path / "store")
        with pytest.raises(ValueError, match="sensitive"):
            svc.add_job(
                name="j", message="m", every_secs=3600, project_dir=str(repo), audit_caller="cli"
            )
        assert [e["caller"] for e in events] == ["cli"]
        assert svc.list_jobs() == []

    def test_update_attributes_the_denial_and_never_persists_it(
        self, tmp_path: Path, repo: Path, monkeypatch
    ):
        svc = CronService(base_dir=tmp_path / "store")
        job = svc.add_job(name="j", message="m", every_secs=3600)
        monkeypatch.setattr("kiro_crew.security.is_sensitive_canonical_path", lambda p: True)
        events = self._capture_sel(monkeypatch)
        with pytest.raises(ValueError, match="sensitive"):
            svc.update_job(job.id, project_dir=str(repo), audit_caller="dashboard:alice")
        assert [e["caller"] for e in events] == ["dashboard:alice"]
        reloaded = CronService(base_dir=tmp_path / "store").list_jobs()[0]
        assert reloaded.project_dir == ""
        # audit_caller is a call parameter, never a stored field.
        assert not hasattr(reloaded, "audit_caller")

    def test_async_add_builds_off_the_event_loop(self, tmp_path: Path, repo: Path, monkeypatch):
        # validate_cron_project_dir stats the filesystem; on a network-mounted
        # path that stalls, so the async create must not run it on the loop.
        import threading

        seen: list[int] = []
        real = validate_cron_project_dir

        def probe(raw, **kw):
            seen.append(threading.get_ident())
            return real(raw, **kw)

        monkeypatch.setattr("kiro_crew.cron.validate_cron_project_dir", probe)

        async def go():
            svc = CronService(base_dir=tmp_path / "store")
            job = await svc.add_job_async(
                "j", "m", every_secs=3600, project_dir=str(repo), audit_caller="dashboard"
            )
            return job, threading.get_ident()

        job, loop_thread = asyncio.run(go())
        assert job.project_dir == os.path.realpath(repo)
        assert seen and all(t != loop_thread for t in seen)


# ── Gateway ──


#: Stand-in for the ACP runtime's own default work dir -- the second entry the
#: session manager's ``default_conversation_roots`` returns after the pool cwd.
#: A default (cwd-less) session that could not resolve a pool cwd lands here.
_RUNTIME_DEFAULT = "/tmp/kirocrew-runtime-default-workspace"


def _run_callback(
    job: CronJob,
    *,
    live_provider=None,
    stored: tuple[str, str] | None = None,
    pool_cwd: str = "",
    reset_ok: bool = True,
    replaced_by=None,
    acquire_raises: Exception | None = None,
    settle_raises: Exception | None = None,
    discard_misses: list[tuple[str, str] | None] | None = None,
) -> dict:
    """Drive the real ``_cron_callback`` closure; capture the session calls made.

    Returns ``{"get_or_create": kwargs | None, "reset": calls, "discarded":
    keys, "settled": store-update kwargs, "error": exc | None}``. ``stored``
    seeds the durable conversation record for ``cron:<id>`` as ``(sid, cwd)``
    -- what a resume would restore with no live provider; ``pool_cwd`` is the
    gateway default; ``reset_ok`` is what ``sessions.reset`` answers to the
    cwd-driven reset when it is aimed at the session that is live.
    ``replaced_by`` models a session that REPLACES ``live_provider`` between
    the gateway's snapshot and its reset: the reset aimed at the snapshot
    declines (identity mismatch) and later reads see the replacement. Mirrors
    the harness in ``test_cron_approval_mode.py``. ``discard_misses`` scripts
    the compare-and-clear: each entry is consumed by one discard call, which
    then answers ``False`` (a successor conversation was published on the key
    after the wake looked) and installs the entry as the new stored record
    (``None`` = the pointer is simply gone); once the list is exhausted the
    discard answers ``True`` as the real facade does on a match.
    """
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.ctx_builder = MagicMock()
    gw.slack = MagicMock()
    gw.conv_log = None
    gw.dashboard_state = None
    gw._owner_id = "U000"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._no_crons = False

    captured: dict = {
        "get_or_create": None,
        "get_or_create_calls": [],
        "reset": [],
        "discarded": [],
        "settled": [],
        "error": None,
    }
    live_state = {"provider": live_provider}
    gp_calls = {"n": 0}

    async def fake_get_or_create(key, **kwargs):
        captured["get_or_create_calls"].append(kwargs)
        if acquire_raises is not None:
            raise acquire_raises
        captured["get_or_create"] = kwargs
        return MagicMock(), True, False

    async def fake_reset(key, **kwargs):
        # The callback also resets the session at run END (an unrelated,
        # pre-existing path). Only the cwd-driven reset carries skip_if_busy,
        # and it must land BEFORE the session is acquired.
        if kwargs.get("skip_if_busy"):
            captured["reset"].append((key, kwargs, captured["get_or_create"] is None))
            # The replacement lands in the gap between the gateway's snapshot
            # and this reset -- exactly once, on the FIRST reset attempt.
            if replaced_by is not None and live_state["provider"] is live_provider:
                live_state["provider"] = replaced_by
            # The real facade resets only the session whose provider the
            # caller observed; anything else is a no-op ``False``.
            if kwargs.get("expect_provider") is not live_state["provider"]:
                return False
            if reset_ok:
                live_state["provider"] = None  # torn down
            return reset_ok
        return True

    def _get_provider(_k):
        gp_calls["n"] += 1
        return live_state["provider"]

    key = f"cron:{job.id}"
    # The public conversation-root surface the gateway consumes. ``stored`` is
    # the durable record a resume would restore when nothing is live.
    default_roots = [d for d in ([pool_cwd] if pool_cwd else []) + [_RUNTIME_DEFAULT] if d]

    gw.sessions = MagicMock()
    stored_state = {"record": stored}
    misses = list(discard_misses or [])

    def _conversation_root(k):
        rec = stored_state["record"]
        if rec and k == key:
            return ConversationRoot(sid=rec[0], cwd=rec[1])
        return ConversationRoot(sid="", cwd="")

    def _discard(k, *, observed):
        captured["discarded"].append((k, observed.sid, observed.cwd))
        if misses:
            stored_state["record"] = misses.pop(0)
            return False
        stored_state["record"] = None
        return True

    gw.sessions.conversation_root = MagicMock(side_effect=_conversation_root)
    gw.sessions.discard_conversation_root = MagicMock(side_effect=_discard)
    gw.sessions.default_conversation_roots = MagicMock(return_value=default_roots)
    gw.sessions.get_pid = MagicMock(return_value=None)
    gw.sessions.get_or_create = fake_get_or_create
    gw.sessions.get_provider = MagicMock(side_effect=_get_provider)
    gw.sessions.release = MagicMock()
    gw.sessions.reset = fake_reset
    gw.sessions.cancel_current = AsyncMock()
    gw.ctx_builder.build_message = MagicMock(return_value=("msg", None))
    captured["build_message"] = gw.ctx_builder.build_message
    gw.ctx_builder.hooks = MagicMock()
    gw._interactive_approval = MagicMock(return_value="interactive_cb")

    captured_cb = None

    with (
        patch("kiro_crew.slack.gateway.stream_and_collect", AsyncMock(return_value="done")),
        patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
    ):

        def capture_cron(on_job=None, **kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = MagicMock()
            svc.start = AsyncMock()

            async def _update(job_id, **kwargs):
                captured["settled"].append((job_id, kwargs))
                if settle_raises is not None:
                    raise settle_raises
                return job

            svc.update_job_async = _update
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

        async def _init_and_run():
            await gw._init_cron()
            assert captured_cb is not None
            try:
                await captured_cb(job)
            except Exception as exc:  # the run-failure path; recorded, not raised
                captured["error"] = exc

        asyncio.run(_init_and_run())

    return captured


def _job(project_dir: str = "", *, was: str = "") -> CronJob:
    """A persistent agent job; ``was`` is the store's rescope marker."""
    return CronJob(
        id="g1",
        name="scoped",
        message="go",
        schedule=CronSchedule(kind="every", every_secs=300),
        project_dir=project_dir,
        project_dir_was=was,
    )


def _ending_reset(expect) -> dict:
    """The reset that ENDS the mis-rooted conversation: aimed at exactly the
    observed session, sid dropped so get_or_create cannot resume it at the
    stored cwd, and its sub-agent runs end with it."""
    return {
        "expect_provider": expect,
        "skip_if_busy": True,
        "clear_conversation": True,
        "ends_conversation": True,
    }


def _settled(project_dir: str, was: str) -> tuple:
    """The store clear a re-rooted wake issues: the marker goes to ``""`` only
    if the job is still scoped exactly as the wake read it (compare-and-swap
    against ``(project_dir, project_dir_was)``)."""
    return ("g1", {"project_dir_was": "", "expect_project_scope": (project_dir, was)})


class TestGatewayCwd:
    def test_the_wake_names_the_project_to_the_context_builder(self, repo: Path):
        # The [PROJECT] context line is emitted only when ``project`` is set,
        # and the spec, the MCP description and the issue all promise it for a
        # rooted job: the wake hands the job's project over, exactly as an
        # interactive chat scoped to the project does.
        captured = _run_callback(_job(str(repo)))
        assert captured["error"] is None, captured["error"]
        kwargs = captured["build_message"].call_args.kwargs
        assert kwargs["project"] == os.path.realpath(repo)

    def test_an_unset_project_stays_unset_for_the_context_builder(self, tmp_path: Path):
        captured = _run_callback(_job(""))
        assert captured["build_message"].call_args.kwargs["project"] is None

    def test_session_is_rooted_at_project_dir(self, repo: Path):
        captured = _run_callback(_job(str(repo)))
        assert captured["error"] is None
        assert captured["get_or_create"]["cwd"] == os.path.realpath(repo)
        # The claim itself refuses a differently-rooted live session under the
        # registry lock, closing the gap between the retire and the claim.
        assert captured["get_or_create"]["require_cwd"] is True

    def test_unset_project_dir_keeps_default_cwd(self):
        captured = _run_callback(_job(""))
        assert captured["error"] is None
        assert captured["get_or_create"]["cwd"] is None
        assert captured["get_or_create"]["require_cwd"] is False
        assert captured["reset"] == []
        assert captured["discarded"] == []
        assert captured["settled"] == []

    def test_claim_time_cwd_mismatch_fails_the_wake_not_the_model(self, tmp_path: Path):
        # The allocation layer refused the claim because a session rooted
        # elsewhere was installed on the key after the retire (the residual
        # window). That refusal is a wake failure, and never a model failure --
        # even with a pinned model and "model" in every path.
        from kiro_crew.session import SessionCwdMismatch

        proj = tmp_path / "model-zoo"
        proj.mkdir()
        job = _job(str(proj))
        job.model = "claude-sonnet"
        captured = _run_callback(
            job,
            acquire_raises=SessionCwdMismatch(
                "cron:g1", os.path.realpath(proj), "/model-registry/elsewhere"
            ),
        )
        assert isinstance(captured["error"], RuntimeError)
        assert "project_dir" in str(captured["error"])
        assert "claimed by another surface" in str(captured["error"])
        # Exactly one claim: the mismatch must not route into the model
        # fallback's second get_or_create.
        assert len(captured["get_or_create_calls"]) == 1
        assert captured["get_or_create_calls"][0]["require_cwd"] is True

    def test_vanished_directory_fails_the_run_before_dispatch(self, tmp_path: Path):
        gone = tmp_path / "gone"
        gone.mkdir()
        job = _job(str(gone))
        gone.rmdir()  # authored while present, vanished before the wake
        captured = _run_callback(job)
        assert captured["get_or_create"] is None, "must not dispatch unscoped"
        assert isinstance(captured["error"], RuntimeError)
        assert "project_dir" in str(captured["error"])

    def test_sensitive_directory_at_fire_time_fails_the_run(self, repo: Path, monkeypatch):
        monkeypatch.setattr("kiro_crew.security.is_sensitive_canonical_path", lambda p: True)
        captured = _run_callback(_job(str(repo)))
        assert captured["get_or_create"] is None
        assert isinstance(captured["error"], RuntimeError)

    # -- live provider --

    def test_live_session_elsewhere_ends_the_conversation_before_the_cold_start(
        self, repo: Path, tmp_path: Path
    ):
        live = MagicMock()
        live.cwd = str(tmp_path / "elsewhere")
        captured = _run_callback(_job(str(repo)), live_provider=live)
        assert captured["error"] is None
        assert len(captured["reset"]) == 1
        key, kwargs, before_acquire = captured["reset"][0]
        assert key == "cron:g1"
        assert kwargs == _ending_reset(
            live
        ), "a plain recycle keeps the sid and resumes the old cwd"
        assert before_acquire, "the reset must precede the cold start it exists for"
        assert captured["get_or_create"]["cwd"] == os.path.realpath(repo)
        # The reset clears the sid itself; nothing is discarded separately, so
        # a declined reset never leaves the other surface's pointer dropped.
        assert captured["discarded"] == []

    def test_live_session_already_at_project_is_kept(self, repo: Path, tmp_path: Path):
        live = MagicMock()
        link = tmp_path / "link"
        os.symlink(repo, link, target_is_directory=True)
        live.cwd = str(link)  # same directory under another spelling
        captured = _run_callback(_job(str(repo)), live_provider=live)
        if os.name == "nt":
            # The Windows rule compares canonical strings and never resolves a
            # spelling by name, so a link spelling is a different root there.
            assert len(captured["reset"]) == 1
            return
        assert captured["reset"] == []

    def test_the_wake_never_resolves_a_named_root_by_name_on_the_windows_rule(
        self, repo: Path, monkeypatch
    ):
        # Both sides of the wake's root compare are canonical strings on
        # Windows (the project came from an opened handle; a live cwd is what
        # that spawn was rooted at). Resolving either by name would open a
        # component swapped for a junction, so the compare is string-only there.
        import kiro_crew.project_dir as pd
        from kiro_crew import platform_compat

        monkeypatch.setattr(platform_compat, "_COMPARE_KEY_RESOLVES", False)
        # The wake re-validates the project through the rule, whose Windows
        # branch canonicalises through a handle rather than by name.
        monkeypatch.setattr(pd, "_WINDOWS", True)
        monkeypatch.setattr(platform_compat, "path_volume_is_remote", lambda p: False)
        real_realpath = os.path.realpath
        project = real_realpath(repo)

        def _guard(p, *a, **k):
            if os.fspath(p).startswith(project):
                pytest.fail(f"realpath reached for the named root {p!r}")
            return real_realpath(p, *a, **k)

        monkeypatch.setattr(os.path, "realpath", _guard)
        live = MagicMock()
        live.cwd = project
        captured = _run_callback(_job(project), live_provider=live)
        assert captured["error"] is None
        assert captured["reset"] == []

    def test_a_link_spelling_is_rooted_elsewhere_on_the_windows_rule(
        self, repo: Path, tmp_path: Path, monkeypatch
    ):
        # The other edge of the same rule: a spelling that differs from the
        # canonical one is a different root, so the conversation is ended and
        # re-rooted at the canonical project rather than kept.
        from kiro_crew import platform_compat

        monkeypatch.setattr(platform_compat, "_COMPARE_KEY_RESOLVES", False)
        live = MagicMock()
        link = tmp_path / "link"
        os.symlink(repo, link, target_is_directory=True)
        live.cwd = str(link)
        captured = _run_callback(_job(str(repo)), live_provider=live)
        assert len(captured["reset"]) == 1

    def test_declined_reset_fails_the_wake_instead_of_running_in_the_old_repo(
        self, repo: Path, tmp_path: Path
    ):
        live = MagicMock()
        live.cwd = str(tmp_path / "elsewhere")
        captured = _run_callback(_job(str(repo)), live_provider=live, reset_ok=False)
        assert len(captured["reset"]) == 1
        assert captured["get_or_create"] is None, "must not run on the mis-rooted session"
        assert isinstance(captured["error"], RuntimeError)
        assert "project_dir" in str(captured["error"])
        # The busy surface keeps its resume pointer: nothing was retired.
        assert captured["discarded"] == []

    def test_declined_reset_is_not_reclassified_as_a_model_failure(self, tmp_path: Path):
        # The model-fallback handler classifies by substring ("model" in the
        # message). The declined-reset error embeds project paths, so a repo
        # path containing "model" plus a pinned model must still FAIL the wake,
        # not fall back onto the busy mis-rooted session.
        proj = tmp_path / "model-zoo"
        proj.mkdir()
        live = MagicMock()
        live.cwd = str(tmp_path / "elsewhere")
        job = _job(str(proj))
        job.model = "claude-sonnet"
        captured = _run_callback(job, live_provider=live, reset_ok=False)
        assert captured["get_or_create"] is None, "fallback must not reuse the old session"
        assert isinstance(captured["error"], RuntimeError)
        assert "project_dir" in str(captured["error"])

    def test_a_correctly_rooted_replacement_is_judged_afresh_not_torn_down(
        self, repo: Path, tmp_path: Path
    ):
        # The snapshot saw a session elsewhere; during the awaits it was
        # replaced by one already at the project (with, say, live child work).
        # The reset aimed at the snapshot declines on identity, the replacement
        # is re-read and found at the target, and NOTHING is ended.
        stale = MagicMock()
        stale.cwd = str(tmp_path / "elsewhere")
        fresh = MagicMock()
        fresh.cwd = str(repo)
        captured = _run_callback(_job(str(repo)), live_provider=stale, replaced_by=fresh)
        assert captured["error"] is None
        assert [r[1]["expect_provider"] for r in captured["reset"]] == [stale]
        assert captured["get_or_create"]["cwd"] == os.path.realpath(repo)

    def test_a_mis_rooted_replacement_is_retired_on_its_own_reading(
        self, repo: Path, tmp_path: Path
    ):
        # Replaced by another session that is ALSO elsewhere: the second pass
        # judges the replacement itself and ends that one.
        stale = MagicMock()
        stale.cwd = str(tmp_path / "elsewhere")
        other = MagicMock()
        other.cwd = str(tmp_path / "another")
        captured = _run_callback(_job(str(repo)), live_provider=stale, replaced_by=other)
        assert captured["error"] is None
        assert [r[1]["expect_provider"] for r in captured["reset"]] == [stale, other]
        assert captured["get_or_create"]["cwd"] == os.path.realpath(repo)

    # -- never-scoped jobs are never policed --

    def test_never_scoped_job_rooted_elsewhere_is_left_alone(self, repo: Path, tmp_path: Path):
        # project_dir was never set. Its tab's own project control (or the MCP
        # set_project tool) rooted the session at a repo on purpose; a legacy
        # job must not lose its conversation to a guard it never opted into.
        default = tmp_path / "default"
        default.mkdir()
        live = MagicMock()
        live.cwd = str(repo)
        captured = _run_callback(_job(""), live_provider=live, pool_cwd=str(default))
        assert captured["error"] is None
        assert captured["reset"] == []
        assert captured["discarded"] == []
        assert captured["get_or_create"]["cwd"] is None
        assert captured["get_or_create"]["require_cwd"] is False

    def test_never_scoped_job_with_a_stored_conversation_elsewhere_resumes_it(
        self, repo: Path, tmp_path: Path
    ):
        captured = _run_callback(_job(""), live_provider=None, stored=("sid-a", str(repo)))
        assert captured["reset"] == []
        assert captured["discarded"] == []
        assert captured["get_or_create"]["cwd"] is None

    # -- a rescoped job (the store's marker) is policed until re-rooted --

    def test_cleared_project_dir_ends_the_live_conversation_rooted_at_the_old_repo(
        self, repo: Path, tmp_path: Path
    ):
        # A -> "": the provider still sits in A while the job now names no
        # project. A plain reset would keep the sid and get_or_create(cwd=None)
        # would resume at the stored A; the conversation has to END, and the
        # claim must refuse a session installed at A in the gap.
        default = tmp_path / "default"
        default.mkdir()
        live = MagicMock()
        live.cwd = str(repo)
        captured = _run_callback(_job("", was=str(repo)), live_provider=live, pool_cwd=str(default))
        assert captured["error"] is None
        assert len(captured["reset"]) == 1
        assert captured["reset"][0][1] == _ending_reset(live)
        assert captured["reset"][0][2], "reset precedes the cold start"
        assert captured["get_or_create"]["cwd"] is None
        assert captured["get_or_create"]["require_cwd"] is True
        # Re-rooted: the marker is cleared so the job is not policed again.
        assert captured["settled"] == [_settled("", str(repo))]

    def test_cleared_project_after_restart_is_discarded(self, repo: Path, tmp_path: Path):
        # THE case an in-memory record cannot see: A -> "" then a gateway
        # restart. The stored conversation is rooted in A and get_or_create
        # (cwd=None) restores exactly that cwd from the map, so the sid must go.
        default = tmp_path / "default"
        default.mkdir()
        captured = _run_callback(
            _job("", was=str(repo)),
            live_provider=None,
            stored=("sid-a", str(repo)),
            pool_cwd=str(default),
        )
        # Compare-and-clear on the root this wake judged (the old repo).
        assert captured["discarded"] == [("cron:g1", "sid-a", str(repo))]
        assert captured["reset"] == []
        assert captured["error"] is None
        assert captured["get_or_create"]["cwd"] is None
        assert captured["get_or_create"]["require_cwd"] is True
        assert captured["settled"] == [_settled("", str(repo))]

    def test_rescoped_job_already_at_the_pool_default_is_confirmed_and_settled(
        self, repo: Path, tmp_path: Path
    ):
        default = tmp_path / "default"
        default.mkdir()
        live = MagicMock()
        live.cwd = str(default)
        captured = _run_callback(_job("", was=str(repo)), live_provider=live, pool_cwd=str(default))
        assert captured["reset"] == []
        assert captured["settled"] == [_settled("", str(repo))]

    def test_rescoped_job_at_the_runtime_default_workspace_is_confirmed(self, repo: Path):
        # With no resolvable pool cwd, a default (cwd-less) session lands on the
        # ACP runtime's own work dir, which default_conversation_roots also
        # returns; that root is "the default" too, not a drift to correct.
        live = MagicMock()
        live.cwd = _RUNTIME_DEFAULT
        captured = _run_callback(_job("", was=str(repo)), live_provider=live, pool_cwd="")
        assert captured["reset"] == []

    def test_rescoped_job_that_fails_busy_keeps_the_marker(self, repo: Path, tmp_path: Path):
        # Not re-rooted this wake: the marker survives so the next wake tries
        # again instead of the job silently going unpoliced.
        default = tmp_path / "default"
        default.mkdir()
        live = MagicMock()
        live.cwd = str(repo)
        captured = _run_callback(
            _job("", was=str(repo)), live_provider=live, pool_cwd=str(default), reset_ok=False
        )
        assert isinstance(captured["error"], RuntimeError)
        assert captured["settled"] == []

    def test_the_settle_is_conditioned_on_the_scope_the_wake_read(self, repo: Path, tmp_path: Path):
        # A -> B before the wake (marker A, project B): the clear names BOTH
        # values it read, so a rescope landing during the wake fails the
        # compare-and-swap in the store instead of being erased.
        other = tmp_path / "other"
        other.mkdir()
        live = MagicMock()
        live.cwd = str(repo)
        captured = _run_callback(_job(str(other), was=str(repo)), live_provider=live)
        assert captured["error"] is None
        assert captured["settled"] == [_settled(os.path.realpath(other), str(repo))]

    def test_a_rescope_landing_during_the_wake_keeps_the_marker_and_the_run(
        self, repo: Path, tmp_path: Path
    ):
        # The store refused the clear (scope changed concurrently). The run
        # this wake did is still valid -- it re-rooted for the scope it read --
        # so it succeeds; the marker stays for the next wake to act on.
        from kiro_crew.cron import CronPendingMismatch

        other = tmp_path / "other"
        other.mkdir()
        live = MagicMock()
        live.cwd = str(repo)
        captured = _run_callback(
            _job(str(other), was=str(repo)),
            live_provider=live,
            settle_raises=CronPendingMismatch("project scope changed concurrently"),
        )
        assert captured["error"] is None
        assert captured["get_or_create"]["cwd"] == os.path.realpath(other)
        assert len(captured["settled"]) == 1

    # -- no live provider: the durable record is what a resume would restore --

    def test_no_live_session_and_no_stored_conversation_just_cold_starts(self, repo: Path):
        # Project set but nothing live and nothing stored: the retire path runs
        # (a concurrent acquisition could appear) but finds nothing to end or
        # discard, and the cold start roots at the project. A session
        # installed in the gap is the claim's ``require_cwd`` to refuse.
        captured = _run_callback(_job(str(repo)), live_provider=None, stored=None)
        assert captured["error"] is None
        assert captured["discarded"] == []  # nothing recorded, nothing to compare against
        assert captured["reset"] == []
        assert captured["get_or_create"]["cwd"] == os.path.realpath(repo)
        assert captured["get_or_create"]["require_cwd"] is True

    def test_stored_conversation_elsewhere_is_discarded(self, repo: Path, tmp_path: Path):
        # Gateway restarted (or the process was evicted) after project_dir moved
        # A -> B. Nothing is live, but the map still points at a conversation
        # rooted in A that get_or_create(cwd=None) would restore. Dropping the
        # sid makes the record inert.
        other = tmp_path / "other"
        other.mkdir()
        captured = _run_callback(_job(str(repo)), live_provider=None, stored=("sid-a", str(other)))
        assert captured["discarded"] == [("cron:g1", "sid-a", str(other))]
        assert captured["error"] is None
        assert captured["get_or_create"]["cwd"] == os.path.realpath(repo)

    def test_a_missed_discard_re_judges_the_successor_it_found(self, repo: Path, tmp_path: Path):
        # Between the wake's read and its compare-and-clear, another surface
        # published a successor conversation on the key and closed it. The
        # clear misses (a different sid is recorded). The wake must not treat
        # the key as clear: a cwd-less claim would RESUME that successor at
        # whatever cwd it stored. It re-reads, finds the successor rooted at
        # the project, and proceeds without discarding it.
        other = tmp_path / "old"
        other.mkdir()
        captured = _run_callback(
            _job(str(repo)),
            live_provider=None,
            stored=("sid-old", str(other)),
            discard_misses=[("sid-new", str(repo))],
        )
        assert captured["error"] is None
        assert captured["discarded"] == [("cron:g1", "sid-old", str(other))]
        assert captured["get_or_create"]["cwd"] == os.path.realpath(repo)

    def test_a_missed_discard_discards_a_successor_still_rooted_elsewhere(
        self, repo: Path, tmp_path: Path
    ):
        # The successor the miss revealed is itself rooted at the old repo (a
        # cwd-less cold start adopted the stored cwd, then closed): it is
        # judged on its own reading and its pointer dropped in turn.
        other = tmp_path / "old"
        other.mkdir()
        captured = _run_callback(
            _job(str(repo)),
            live_provider=None,
            stored=("sid-old", str(other)),
            discard_misses=[("sid-new", str(other))],
        )
        assert captured["error"] is None
        assert captured["discarded"] == [
            ("cron:g1", "sid-old", str(other)),
            ("cron:g1", "sid-new", str(other)),
        ]
        assert captured["get_or_create"]["cwd"] == os.path.realpath(repo)

    def test_a_key_that_keeps_changing_fails_the_wake(self, repo: Path, tmp_path: Path):
        # Three misses in a row: the wake gives up rather than run in an
        # unverified directory, and dispatches nothing.
        other = tmp_path / "old"
        other.mkdir()
        captured = _run_callback(
            _job(str(repo)),
            live_provider=None,
            stored=("sid-0", str(other)),
            discard_misses=[("sid-1", str(other)), ("sid-2", str(other)), ("sid-3", str(other))],
        )
        assert isinstance(captured["error"], RuntimeError)
        assert "kept changing" in str(captured["error"])
        assert len(captured["discarded"]) == 3
        assert captured["get_or_create"] is None

    def test_stored_conversation_already_at_project_is_resumed(self, repo: Path, tmp_path: Path):
        link = tmp_path / "link"
        os.symlink(repo, link, target_is_directory=True)
        captured = _run_callback(_job(str(repo)), live_provider=None, stored=("sid-a", str(link)))
        assert captured["reset"] == []
        if os.name == "nt":
            # Windows rule: a link spelling is a different root, so the stored
            # pointer is discarded and the cold start roots at the project.
            assert captured["discarded"] == [("cron:g1", "sid-a", str(link))]
            return
        assert captured["discarded"] == []

    def test_live_provider_outranks_the_stored_record(self, repo: Path, tmp_path: Path):
        # The map may lag a live provider (sid promotion is deferred while a
        # replay is pending); the provider's own cwd is the truth when present.
        live = MagicMock()
        live.cwd = str(repo)
        captured = _run_callback(
            _job(str(repo)), live_provider=live, stored=("sid-a", str(tmp_path / "stale"))
        )
        assert captured["reset"] == []
        assert captured["discarded"] == []


# ── MCP tools ──


from kiro_crew.mcp_cron import _agent_cwd_allowed_roots as _real_allowed_roots  # noqa: E402


def _allow_roots(monkeypatch, roots: list[str]) -> None:
    """Stand in for the config the MCP scope check reads its allowlist from."""
    import kiro_crew.mcp_cron as mcp_cron

    monkeypatch.setattr(mcp_cron, "_agent_cwd_allowed_roots", lambda: list(roots))


class TestStoreScopedRoots:
    """The agent allowlist is judged by the store on the value it persists."""

    def test_operator_surfaces_pass_no_roots_and_are_not_confined(self, repo: Path):
        from kiro_crew.cron import validate_cron_project_dir

        assert validate_cron_project_dir(str(repo)) == os.path.realpath(repo)
        assert validate_cron_project_dir(str(repo), allowed_roots=None) == os.path.realpath(repo)

    def test_a_root_outside_the_allowlist_is_a_typed_refusal_carrying_the_canonical(
        self, repo: Path, tmp_path: Path
    ):
        from kiro_crew.cron import (
            PROJECT_DIR_OUT_OF_SCOPE,
            CronProjectDirOutOfScope,
            validate_cron_project_dir,
        )

        with pytest.raises(CronProjectDirOutOfScope) as info:
            validate_cron_project_dir(str(repo), allowed_roots=[str(tmp_path / "allowed")])
        assert str(info.value) == PROJECT_DIR_OUT_OF_SCOPE
        assert info.value.canonical == os.path.realpath(repo)
        assert isinstance(info.value, ValueError)

    def test_an_empty_allowlist_admits_nothing(self, repo: Path):
        from kiro_crew.cron import CronProjectDirOutOfScope, validate_cron_project_dir

        with pytest.raises(CronProjectDirOutOfScope):
            validate_cron_project_dir(str(repo), allowed_roots=[])

    def test_containment_is_judged_on_the_one_resolution_the_store_persists(
        self, tmp_path: Path, monkeypatch
    ):
        # The store resolves the raw string ONCE and judges that very value; a
        # caller-side pre-check on its own resolution would be a second pass a
        # link retargeted in between could slip past. Count the resolutions.
        import kiro_crew.cron as cron_mod

        allowed = tmp_path / "allowed"
        allowed.mkdir()
        inside = allowed / "repo"
        inside.mkdir()
        calls: list[str] = []
        real = cron_mod.resolve_project_dir

        def _counting(raw, **kw):
            calls.append(raw)
            return real(raw, **kw)

        monkeypatch.setattr(cron_mod, "resolve_project_dir", _counting)
        svc = CronService(base_dir=tmp_path / "home")
        job = svc.add_job(
            name="x",
            message="go",
            every_secs=300,
            project_dir=str(inside),
            project_dir_allowed_roots=[str(allowed)],
        )
        assert job.project_dir == os.path.realpath(inside)
        assert calls == [str(inside)]

    def test_the_store_refuses_and_persists_nothing_for_an_out_of_scope_add(
        self, repo: Path, tmp_path: Path
    ):
        from kiro_crew.cron import CronProjectDirOutOfScope

        svc = CronService(base_dir=tmp_path / "home")
        with pytest.raises(CronProjectDirOutOfScope):
            svc.add_job(
                name="x",
                message="go",
                every_secs=300,
                project_dir=str(repo),
                project_dir_allowed_roots=[str(tmp_path / "allowed")],
            )
        assert svc.list_jobs() == []

    def test_the_store_refuses_an_out_of_scope_update_and_keeps_the_old_root(
        self, repo: Path, tmp_path: Path
    ):
        from kiro_crew.cron import CronProjectDirOutOfScope

        svc = CronService(base_dir=tmp_path / "home")
        job = svc.add_job(name="x", message="go", every_secs=300, project_dir=str(repo))
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        with pytest.raises(CronProjectDirOutOfScope):
            svc.update_job(job.id, project_dir=str(outside), project_dir_allowed_roots=[str(repo)])
        assert svc.get_job(job.id).project_dir == os.path.realpath(repo)


class TestMcpTools:
    @pytest.fixture(autouse=True)
    def _named(self, named_cron_caller, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        # An agent's choice is confined to the caller's scope; these tests name
        # directories under tmp_path, so that is the configured allowlist here.
        _allow_roots(monkeypatch, [str(tmp_path)])

    def test_cron_add_stores_project_dir(self, tmp_path: Path, repo: Path):
        from kiro_crew.mcp_cron import _call_tool_inner

        name = f"pd-{uuid.uuid4().hex[:6]}"
        result = _call_tool_inner(
            "cron_add",
            {"name": name, "message": "go", "every": 300, "project_dir": str(repo)},
        )
        assert "Error" not in result
        svc = CronService(base_dir=tmp_path / "home")
        job = next(j for j in svc.list_jobs() if j.name == name)
        assert job.project_dir == os.path.realpath(repo)

    def test_cron_add_refuses_missing_directory(self, tmp_path: Path):
        from kiro_crew.mcp_cron import _call_tool_inner

        result = _call_tool_inner(
            "cron_add",
            {"name": "x", "message": "go", "every": 300, "project_dir": str(tmp_path / "nope")},
        )
        assert result.startswith("Error")
        assert CronService(base_dir=tmp_path / "home").list_jobs() == []

    def test_cron_add_refuses_project_dir_with_command(self, tmp_path: Path, repo: Path):
        from kiro_crew.mcp_cron import _call_tool_inner

        result = _call_tool_inner(
            "cron_add",
            {"name": "x", "message": "", "every": 300, "command": "true", "project_dir": str(repo)},
        )
        assert result.startswith("Error")
        assert CronService(base_dir=tmp_path / "home").list_jobs() == []

    def test_cron_update_sets_then_clears(self, tmp_path: Path, repo: Path):
        from kiro_crew.mcp_cron import _call_tool_inner

        name = f"pd-{uuid.uuid4().hex[:6]}"
        _call_tool_inner("cron_add", {"name": name, "message": "go", "every": 300})
        svc = CronService(base_dir=tmp_path / "home")
        job = next(j for j in svc.list_jobs() if j.name == name)

        result = _call_tool_inner("cron_update", {"job_id": job.id, "project_dir": str(repo)})
        assert "Error" not in result
        assert (
            next(j for j in CronService(base_dir=tmp_path / "home").list_jobs() if j.id == job.id)
        ).project_dir == os.path.realpath(repo)

        result = _call_tool_inner("cron_update", {"job_id": job.id, "project_dir": ""})
        assert "Error" not in result
        assert (
            next(j for j in CronService(base_dir=tmp_path / "home").list_jobs() if j.id == job.id)
        ).project_dir == ""

    def test_cron_add_refuses_a_root_outside_the_callers_scope(self, tmp_path: Path, monkeypatch):
        # The store admits any existing, non-sensitive directory -- the right
        # rule for an operator. An agent calling this auto-approved tool is
        # confined to the allowlist spawn_run's cwd answers to; a directory
        # outside it is refused and no job is left behind.
        from kiro_crew.cron import PROJECT_DIR_OUT_OF_SCOPE as _PROJECT_DIR_SCOPE_MESSAGE
        from kiro_crew.mcp_cron import _call_tool_inner

        outside = tmp_path / "elsewhere"
        outside.mkdir()
        _allow_roots(monkeypatch, [str(tmp_path / "allowed")])
        monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path / "caller"))
        result = _call_tool_inner(
            "cron_add",
            {"name": "x", "message": "go", "every": 300, "project_dir": str(outside)},
        )
        assert result == f"Error: {_PROJECT_DIR_SCOPE_MESSAGE}"
        assert CronService(base_dir=tmp_path / "home").list_jobs() == []

    def test_cron_update_refuses_a_root_outside_the_callers_scope(
        self, tmp_path: Path, repo: Path, monkeypatch
    ):
        from kiro_crew.cron import PROJECT_DIR_OUT_OF_SCOPE as _PROJECT_DIR_SCOPE_MESSAGE
        from kiro_crew.mcp_cron import _call_tool_inner

        name = f"pd-{uuid.uuid4().hex[:6]}"
        _call_tool_inner(
            "cron_add", {"name": name, "message": "go", "every": 300, "project_dir": str(repo)}
        )
        svc = CronService(base_dir=tmp_path / "home")
        job = next(j for j in svc.list_jobs() if j.name == name)
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        _allow_roots(monkeypatch, [str(repo)])
        monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path / "caller"))
        result = _call_tool_inner("cron_update", {"job_id": job.id, "project_dir": str(outside)})
        assert result == f"Error: {_PROJECT_DIR_SCOPE_MESSAGE}"
        assert (
            next(j for j in CronService(base_dir=tmp_path / "home").list_jobs() if j.id == job.id)
        ).project_dir == os.path.realpath(repo)

    @pytest.mark.parametrize("tool", ["cron_add", "cron_update"])
    def test_a_scope_refusal_is_an_audited_authz_denial(
        self, tmp_path: Path, repo: Path, monkeypatch, tool: str
    ):
        # The generic tool-call row redacts every argument and carries no denial
        # class, so the scope refusal records itself: a ``denied`` authz event
        # under the tool's name, naming the (redacted) canonical path.
        import kiro_crew.mcp_cron as mcp_cron_mod
        from kiro_crew.cron import PROJECT_DIR_OUT_OF_SCOPE as _PROJECT_DIR_SCOPE_MESSAGE
        from kiro_crew.mcp_cron import _call_tool_inner

        events: list[dict] = []

        class _FakeSel:
            def log_tool_invocation(self, **kw):
                events.append(kw)

            def log_api_access(self, **kw):
                pass

        monkeypatch.setattr(mcp_cron_mod, "sel", lambda: _FakeSel())
        name = f"pd-{uuid.uuid4().hex[:6]}"
        _call_tool_inner("cron_add", {"name": name, "message": "go", "every": 300})
        job = next(j for j in CronService(base_dir=tmp_path / "home").list_jobs() if j.name == name)
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        _allow_roots(monkeypatch, [str(repo)])
        events.clear()
        if tool == "cron_add":
            args = {"name": "x", "message": "go", "every": 300, "project_dir": str(outside)}
        else:
            args = {"job_id": job.id, "project_dir": str(outside)}
        assert _call_tool_inner(tool, args) == f"Error: {_PROJECT_DIR_SCOPE_MESSAGE}"
        denials = [e for e in events if e.get("outcome") == "denied"]
        assert len(denials) == 1
        assert denials[0]["tool_name"] == tool
        assert denials[0]["tool_kind"] == "authz"
        assert "project_dir outside the allowed roots" in denials[0]["error"]
        assert os.path.realpath(outside) in denials[0]["error"]

    def test_the_host_processs_working_directory_is_not_a_root(self, tmp_path: Path, monkeypatch):
        # The tool may run inside the gateway (in-process dispatch) or a pooled
        # backend, whose cwd is the operator's, not the caller's; the directory
        # this process runs in admits nothing.
        from kiro_crew.cron import PROJECT_DIR_OUT_OF_SCOPE as _PROJECT_DIR_SCOPE_MESSAGE
        from kiro_crew.mcp_cron import _call_tool_inner

        host = tmp_path / "host-cwd"
        sub = host / "sub"
        sub.mkdir(parents=True)
        _allow_roots(monkeypatch, [])
        monkeypatch.setattr(os, "getcwd", lambda: str(host))
        for target in (host, sub):
            result = _call_tool_inner(
                "cron_add",
                {"name": "x", "message": "go", "every": 300, "project_dir": str(target)},
            )
            assert result == f"Error: {_PROJECT_DIR_SCOPE_MESSAGE}"
        assert CronService(base_dir=tmp_path / "home").list_jobs() == []

    def test_a_sibling_sharing_the_roots_prefix_is_outside_it(self, tmp_path: Path, monkeypatch):
        # Containment is by path component, not by string prefix.
        from kiro_crew.cron import PROJECT_DIR_OUT_OF_SCOPE as _PROJECT_DIR_SCOPE_MESSAGE
        from kiro_crew.mcp_cron import _call_tool_inner

        (tmp_path / "allowed").mkdir()
        sibling = tmp_path / "allowed-not"
        sibling.mkdir()
        _allow_roots(monkeypatch, [str(tmp_path / "allowed")])
        monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path / "caller"))
        result = _call_tool_inner(
            "cron_add", {"name": "x", "message": "go", "every": 300, "project_dir": str(sibling)}
        )
        assert result == f"Error: {_PROJECT_DIR_SCOPE_MESSAGE}"

    def test_an_unreadable_config_fails_closed_to_an_empty_allowlist(
        self, tmp_path: Path, monkeypatch
    ):
        import kiro_crew.mcp_cron as mcp_cron
        from kiro_crew.cron import PROJECT_DIR_OUT_OF_SCOPE as _PROJECT_DIR_SCOPE_MESSAGE
        from kiro_crew.mcp_cron import _call_tool_inner

        def _boom():
            raise RuntimeError("config unreadable")

        # The reader itself fails closed to an empty allowlist ...
        with monkeypatch.context() as inner:
            inner.setattr(mcp_cron.KiroCrewConfig, "load", staticmethod(_boom))
            assert _real_allowed_roots() == []
        # ... and an empty allowlist admits nothing.
        _allow_roots(monkeypatch, [])
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path / "caller"))
        result = _call_tool_inner(
            "cron_add", {"name": "x", "message": "go", "every": 300, "project_dir": str(outside)}
        )
        assert result == f"Error: {_PROJECT_DIR_SCOPE_MESSAGE}"

    def test_the_scope_check_judges_the_rules_canonical_answer(self, tmp_path: Path, monkeypatch):
        # A link spelling inside the allowlist that resolves outside it is
        # judged where it resolves; the store would have canonicalised it the
        # same way, so the two rules cannot disagree about the root.
        from kiro_crew.cron import PROJECT_DIR_OUT_OF_SCOPE as _PROJECT_DIR_SCOPE_MESSAGE
        from kiro_crew.mcp_cron import _call_tool_inner

        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        os.symlink(outside, allowed / "link", target_is_directory=True)
        _allow_roots(monkeypatch, [str(allowed)])
        monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path / "caller"))
        result = _call_tool_inner(
            "cron_add",
            {"name": "x", "message": "go", "every": 300, "project_dir": str(allowed / "link")},
        )
        if os.name == "nt":
            # The Windows rule never resolves a spelling by name; the link is a
            # reparse point and the rule refuses it outright.
            assert result.startswith("Error")
            return
        assert result == f"Error: {_PROJECT_DIR_SCOPE_MESSAGE}"

    def test_cron_list_shows_project_dir(self, tmp_path: Path, repo: Path):
        from kiro_crew.mcp_cron import _call_tool_inner

        name = f"pd-{uuid.uuid4().hex[:6]}"
        _call_tool_inner(
            "cron_add",
            {"name": name, "message": "go", "every": 300, "project_dir": str(repo)},
        )
        assert os.path.realpath(repo) in _call_tool_inner("cron_list", {})

    def test_cron_list_json_redacts_a_credential_shaped_project_path(self, tmp_path: Path):
        # A pathname is operator/LLM-controlled text like every other field on
        # the record; JSON mode must route it through the same redaction the
        # compact render and the sibling fields get, not emit it raw.
        import json

        from kiro_crew.mcp_cron import _call_tool_inner

        secret = "ghp_" + "A" * 36
        repo = tmp_path / secret
        repo.mkdir()
        name = f"pd-{uuid.uuid4().hex[:6]}"
        res = _call_tool_inner(
            "cron_add",
            {"name": name, "message": "go", "every": 300, "project_dir": str(repo)},
        )
        assert "Error" not in res
        out = _call_tool_inner("cron_list", {"json": True})
        assert secret not in out
        payload = json.loads(out)
        record = next(r for r in payload["jobs"] if r["name"] == name)
        assert record["project_dir"]
        assert secret not in record["project_dir"]


# ── Dashboard REST ──


def _dash_app(handler, route: str, **store) -> web.Application:
    """Mirror of the harness in test_cron_persistent_session_api.py."""
    app = web.Application()
    app["state"] = SimpleNamespace(
        crons=SimpleNamespace(**store),
        push_refresh=MagicMock(),
        ack_notification=AsyncMock(),
        has_slot=MagicMock(return_value=False),
    )
    app.router.add_route("*", route, handler)
    return app


def _dash_job(**over) -> CronJob:
    fields = {"id": "j1", "name": "poller", "message": "go"}
    fields.update(over)
    return CronJob(**fields)


class TestDashboardRest:
    def test_create_forwards_project_dir_to_the_store(self, repo: Path):
        from kiro_crew.dashboard.handlers.cron import api_crons_create

        add = AsyncMock(return_value=_dash_job(project_dir=str(repo)))
        app = _dash_app(api_crons_create, "/api/crons", add_job_async=add)

        async def go():
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/crons",
                    json={
                        "name": "poller",
                        "message": "go",
                        "every": 3600,
                        "project_dir": str(repo),
                    },
                )
                return resp.status

        assert asyncio.run(go()) == 200
        assert add.await_args.kwargs["project_dir"] == str(repo)
        assert add.await_args.kwargs["audit_caller"] == "dashboard"

    def test_create_omits_the_field_when_absent(self):
        from kiro_crew.dashboard.handlers.cron import api_crons_create

        add = AsyncMock(return_value=_dash_job())
        app = _dash_app(api_crons_create, "/api/crons", add_job_async=add)

        async def go():
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/crons", json={"name": "poller", "message": "go", "every": 3600}
                )
                return resp.status

        assert asyncio.run(go()) == 200
        assert "project_dir" not in add.await_args.kwargs

    def test_create_non_string_is_a_400_before_the_store(self):
        from kiro_crew.dashboard.handlers.cron import api_crons_create

        add = AsyncMock()
        app = _dash_app(api_crons_create, "/api/crons", add_job_async=add)

        async def go():
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/crons",
                    json={"name": "poller", "message": "go", "every": 3600, "project_dir": 7},
                )
                return resp.status, await resp.json()

        status, body = asyncio.run(go())
        assert status == 400
        assert body["code"] == "invalid_project_dir"
        add.assert_not_awaited()

    def test_create_store_refusal_is_a_400(self, tmp_path: Path):
        from kiro_crew.dashboard.handlers.cron import api_crons_create

        add = AsyncMock(side_effect=ValueError("project_dir must be an existing directory"))
        app = _dash_app(api_crons_create, "/api/crons", add_job_async=add)

        async def go():
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/crons",
                    json={
                        "name": "poller",
                        "message": "go",
                        "every": 3600,
                        "project_dir": str(tmp_path / "gone"),
                    },
                )
                return resp.status, await resp.json()

        status, body = asyncio.run(go())
        assert status == 400
        assert "existing directory" in body["error"]

    def test_update_forwards_set_and_clear(self, repo: Path):
        from kiro_crew.dashboard.handlers.cron import api_cron_update

        update = AsyncMock(return_value=_dash_job(project_dir=str(repo)))
        app = _dash_app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)

        async def go():
            async with TestClient(TestServer(app)) as client:
                r1 = await client.patch("/api/crons/j1", json={"project_dir": str(repo)})
                r2 = await client.patch("/api/crons/j1", json={"project_dir": ""})
                return r1.status, r2.status

        assert asyncio.run(go()) == (200, 200)
        first, second = update.await_args_list
        assert first.kwargs["project_dir"] == str(repo)
        assert first.kwargs["audit_caller"] == "dashboard"
        assert second.kwargs["project_dir"] == ""

    def test_update_unrelated_patch_leaves_the_field_alone(self):
        from kiro_crew.dashboard.handlers.cron import api_cron_update

        update = AsyncMock(return_value=_dash_job())
        app = _dash_app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)

        async def go():
            async with TestClient(TestServer(app)) as client:
                return (await client.patch("/api/crons/j1", json={"name": "renamed"})).status

        assert asyncio.run(go()) == 200
        assert "project_dir" not in update.await_args.kwargs

    def test_update_non_string_is_a_400(self):
        from kiro_crew.dashboard.handlers.cron import api_cron_update

        update = AsyncMock()
        app = _dash_app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)

        async def go():
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch("/api/crons/j1", json={"project_dir": ["/x"]})
                return resp.status, await resp.json()

        status, body = asyncio.run(go())
        assert status == 400
        assert body["code"] == "invalid_project_dir"
        update.assert_not_awaited()

    @staticmethod
    def _list_request(job: CronJob):
        state = MagicMock()
        state.has_slot.return_value = False
        state.crons.list_jobs.return_value = [job]
        state.crons.list_jobs_async = AsyncMock(return_value=[job])
        state.crons.is_running.return_value = False
        state.crons.running_since.return_value = None
        request = MagicMock()
        request.app = {"state": state}
        return request

    def test_list_serializes_the_field_redacted(self, tmp_path: Path):
        from kiro_crew.dashboard.handlers.cron import api_crons

        secret = "ghp_" + "B" * 36
        job = _dash_job(project_dir=str(tmp_path / secret))
        resp = asyncio.run(api_crons(self._list_request(job)))
        record = json.loads(resp.body)["jobs"][0]
        assert record["project_dir"]
        assert secret not in record["project_dir"]

    def test_list_reports_an_unset_field_as_empty(self):
        from kiro_crew.dashboard.handlers.cron import api_crons

        resp = asyncio.run(api_crons(self._list_request(_dash_job())))
        assert json.loads(resp.body)["jobs"][0]["project_dir"] == ""


# ── CLI ──


def _ns(**overrides) -> argparse.Namespace:
    base = dict(
        cron_action="add",
        name="job",
        message="do the thing",
        every=None,
        cron_expr=None,
        at=None,
        timezone="",
        channel=None,
        agent="",
        script="",
        shell_command="",
        timeout=None,
        timeout_secs=None,
        model="",
        project_dir="",
        persistent_session=True,
        minimal_context=False,
        hide_in_chat=False,
        silent=False,
        folder="",
        approval_mode="",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class TestCli:
    @staticmethod
    def _main_with(argv: list[str], monkeypatch) -> argparse.Namespace | None:
        import sys

        from kiro_crew import cli

        seen: dict[str, argparse.Namespace] = {}
        monkeypatch.setattr("kiro_crew.cli_commands._cron", lambda args: seen.update(args=args))
        monkeypatch.setattr(sys, "argv", ["kirocrew", "cron", *argv])
        cli.main()
        return seen.get("args")

    def test_parser_exposes_flag_on_add_and_update(self, monkeypatch):
        ns = self._main_with(
            ["add", "n", "m", "--every", "600", "--project-dir", "/srv/repo"], monkeypatch
        )
        assert ns is not None and ns.project_dir == "/srv/repo"
        ns = self._main_with(["update", "abc", "--project-dir", ""], monkeypatch)
        assert ns is not None and ns.project_dir == ""
        ns = self._main_with(["update", "abc", "--name", "x"], monkeypatch)
        assert ns is not None and ns.project_dir is None

    def test_add_forwards_project_dir_into_the_single_add_job(self, repo: Path, capsys):
        from kiro_crew.cli_commands import _cron

        with (
            patch("kiro_crew.cli_commands.CronService") as svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            svc = svc_cls.return_value
            job = MagicMock()
            job.id, job.name, job.timezone = "p1", "job", ""
            job.project_dir = os.path.realpath(repo)
            job.schedule.kind, job.schedule.every_secs = "every", 300
            job.schedule.cron_expr = job.schedule.at_ts = None
            svc.add_job.return_value = job
            _cron(_ns(every=300, project_dir=str(repo)))
            assert svc.add_job.call_args.kwargs["project_dir"] == str(repo)
            svc._save.assert_not_called()
        out = capsys.readouterr().out
        assert out.startswith("Added job: p1 ")
        assert os.path.realpath(repo) in out

    def test_add_refuses_project_dir_with_script_before_the_store(self, repo: Path, capsys):
        from kiro_crew.cli_commands import _cron

        with patch("kiro_crew.cli_commands.CronService") as svc_cls:
            with pytest.raises(SystemExit) as exc:
                _cron(_ns(message="", every=300, script="x.py:run", project_dir=str(repo)))
            assert exc.value.code == 1
            svc_cls.return_value.add_job.assert_not_called()
        out, err = capsys.readouterr()
        assert out == ""
        assert "--project-dir" in err

    def test_update_forwards_and_clears(self, repo: Path):
        from kiro_crew.cli_commands import _cron

        with (
            patch("kiro_crew.cli_commands.CronService") as svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            svc = svc_cls.return_value
            svc.update_job.return_value = MagicMock()
            ns = argparse.Namespace(cron_action="update", job_id="abc", project_dir=str(repo))
            _cron(ns)
            assert svc.update_job.call_args.kwargs == {
                "project_dir": str(repo),
                "audit_caller": "cli",
            }
            ns = argparse.Namespace(cron_action="update", job_id="abc", project_dir="")
            _cron(ns)
            assert svc.update_job.call_args.kwargs == {"project_dir": "", "audit_caller": "cli"}
