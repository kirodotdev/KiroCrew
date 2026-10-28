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
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronJob, CronSchedule, CronService, validate_cron_project_dir
from kiro_crew.testing.links import make_dir_link

# ── Store ──


@pytest.fixture(autouse=True)
def _isolate_cron_store(_floor_monkeypatch, tmp_path):
    _floor_monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path / "store")


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
        make_dir_link(link, repo)
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
        monkeypatch.setattr(os.path, "realpath", lambda p, *a, **k: long_canonical)
        with pytest.raises(ValueError, match="exceeds"):
            validate_cron_project_dir(str(repo))

    @pytest.mark.skipif(os.name == "nt", reason="Windows strips trailing spaces from names")
    def test_revalidation_judges_the_persisted_spelling_untrimmed(self, tmp_path: Path):
        # The store persisted a canonical path whose leaf ends in a space, and a
        # same-named sibling without the space also exists. Authoring input is
        # trimmed; the fire-time revalidation must NOT be, or it would resolve
        # the sibling -- a directory the authoring-time confinement never saw.
        spaced = tmp_path / "repo "
        spaced.mkdir()
        (tmp_path / "repo").mkdir()
        persisted = os.path.realpath(spaced)
        assert validate_cron_project_dir(persisted, revalidate=True) == persisted
        # Authoring keeps trimming: the same raw string names the sibling.
        assert validate_cron_project_dir(persisted) == os.path.realpath(tmp_path / "repo")

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

    def test_a_local_spelling_that_canonicalises_to_a_share_is_refused_at_authoring(
        self, repo: Path, monkeypatch
    ):
        # A mapped drive or ``subst`` letter passes the pre-resolution check but
        # can realpath to its share; persisting that would fail every wake's
        # re-check, so authoring refuses it.
        monkeypatch.setattr(
            "kiro_crew.cron_service.fields.os.path.realpath",
            lambda p: r"\\server\share\repo",
        )
        with pytest.raises(ValueError, match="UNC"):
            validate_cron_project_dir(str(repo))

    def test_on_windows_links_are_screened_before_resolution(self, repo: Path, monkeypatch):
        # ``realpath`` follows every link on the way, so a local spelling whose
        # ancestor or leaf links to a UNC share would make the resolution the
        # SMB probe. The shared Windows link screen runs first: a refused
        # target never reaches ``realpath`` and is audited as a network-path
        # denial; an accepted one is rewritten, and THAT path is resolved.
        calls: list[str] = []
        events: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: events.append(kw)
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: fake_sel)
        monkeypatch.setattr("kiro_crew.cron_service.fields._IS_WINDOWS", True)
        real_realpath = os.path.realpath

        def _spy_realpath(p):
            calls.append(("realpath", p))
            return real_realpath(p)

        monkeypatch.setattr("kiro_crew.cron_service.fields.os.path.realpath", _spy_realpath)
        monkeypatch.setattr(
            "kiro_crew.cron_service.fields._screen_windows_links",
            lambda p: calls.append(("screen", p)) or None,
        )
        with pytest.raises(ValueError, match="local path"):
            validate_cron_project_dir(str(repo), audit_caller="cron:t1")
        assert calls == [("screen", str(repo))], "refused before any realpath"
        assert [ev["error"] for ev in events] == ["network path"]

        calls.clear()
        rewritten = str(repo.parent / "rewritten")
        (repo.parent / "rewritten").mkdir()
        monkeypatch.setattr(
            "kiro_crew.cron_service.fields._screen_windows_links",
            lambda p: calls.append(("screen", p)) or rewritten,
        )
        assert validate_cron_project_dir(str(repo)) == real_realpath(rewritten)
        assert calls[0] == ("screen", str(repo))
        first_realpath = next(c for c in calls if c[0] == "realpath")
        assert first_realpath == ("realpath", rewritten), "the screened target is what resolves"

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
        make_dir_link(link, repo)
        job = svc.add_job(name="j", message="m", every_secs=3600, project_dir=str(link))
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

    def test_a_refused_marker_write_leaves_the_other_fields_unapplied(
        self, tmp_path: Path, repo: Path
    ):
        # The refusal must land BEFORE any field is assigned: the store rolls
        # back only a failed save, so a message written and then refused would
        # sit on the in-memory job and persist with the next successful update.
        svc = CronService(base_dir=tmp_path / "store")
        job = svc.add_job(name="j", message="m", every_secs=3600)
        with pytest.raises(ValueError, match="store-managed"):
            svc.update_job(job.id, message="smuggled", project_dir_was=str(repo))
        assert svc.get_job(job.id).message == "m"
        svc.update_job(job.id, name="renamed")
        reloaded = CronService(base_dir=tmp_path / "store").list_jobs()[0]
        assert reloaded.message == "m"
        assert reloaded.name == "renamed"

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


def _run_callback(
    job: CronJob,
    *,
    live_provider=None,
    claimed_cwd: str | list[str | None] | None = None,
    busy_keys: frozenset[str] = frozenset(),
    acquire_raises: Exception | None = None,
    settle_raises: Exception | None = None,
) -> dict:
    """Drive the real ``_cron_callback`` closure; capture the session calls made.

    Returns ``{"get_or_create": kwargs | None, "discarded": keys, "released":
    keys, "settled": store-update kwargs, "error": exc | None}``.
    ``live_provider`` is what ``get_provider`` answers for the job's STABLE
    key. ``claimed_cwd`` is the cwd of the session each claim hands back (a
    list is consumed one entry per claim); ``None`` means it is rooted exactly
    where the claim asked (the cold-start case). ``busy_keys`` are the keys
    whose ``discard_conversation`` declines (``skip_if_busy``: a turn is in
    flight on another surface). Mirrors the harness in
    ``test_cron_approval_mode.py``.
    """
    from kiro_crew.slack.gateway import GatewayOrchestrator

    claimed_cwds = list(claimed_cwd) if isinstance(claimed_cwd, list) else []
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
        "discarded": [],
        "released": [],
        "settled": [],
        "error": None,
        "order": [],
    }
    key = f"cron:{job.id}"

    async def fake_get_or_create(k, **kwargs):
        captured["get_or_create_calls"].append(kwargs)
        captured["order"].append(("claim", k))
        if acquire_raises is not None:
            raise acquire_raises
        captured["get_or_create"] = kwargs
        client = MagicMock()
        if isinstance(claimed_cwd, list):
            handed = claimed_cwds.pop(0) if claimed_cwds else None
        else:
            handed = claimed_cwd
        client.cwd = handed if handed is not None else kwargs.get("cwd")
        return client, True, False

    async def fake_discard(k, **kwargs):
        assert kwargs == {"skip_if_busy": True}, kwargs
        captured["discarded"].append(k)
        captured["order"].append(("discard", k))
        return k not in busy_keys

    gw.sessions = MagicMock()
    gw.sessions.get_pid = MagicMock(return_value=None)
    gw.sessions.get_or_create = fake_get_or_create
    gw.sessions.discard_conversation = fake_discard
    gw.sessions.get_provider = MagicMock(side_effect=lambda k: live_provider if k == key else None)
    gw.sessions.release = MagicMock(side_effect=lambda k: captured["released"].append(k))
    gw.sessions.reset = AsyncMock(return_value=True)
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


def _job(project_dir: str = "", *, was: str = "", persistent: bool = True) -> CronJob:
    """An agent job; ``was`` is the store's rescope marker."""
    return CronJob(
        id="g1",
        name="scoped",
        message="go",
        schedule=CronSchedule(kind="every", every_secs=300),
        project_dir=project_dir,
        project_dir_was=was,
        persistent_session=persistent,
    )


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

    def test_unset_project_dir_keeps_default_cwd(self):
        captured = _run_callback(_job(""))
        assert captured["error"] is None
        assert captured["get_or_create"]["cwd"] is None
        assert captured["discarded"] == []
        assert captured["settled"] == []

    def test_a_scoped_job_that_was_not_rescoped_keeps_its_conversation(self, repo: Path):
        # Persistence is the point of a persistent job: only a CHANGED project
        # ends the conversation, a wake at an unchanged one resumes it.
        captured = _run_callback(_job(str(repo)))
        assert captured["discarded"] == []
        assert captured["settled"] == []

    def test_vanished_directory_fails_the_run_before_dispatch(self, tmp_path: Path):
        gone = tmp_path / "gone"
        gone.mkdir()
        job = _job(str(gone))
        gone.rmdir()  # authored while present, vanished before the wake
        captured = _run_callback(job)
        assert captured["get_or_create"] is None, "must not dispatch unscoped"
        assert isinstance(captured["error"], RuntimeError)
        assert "project_dir" in str(captured["error"])

    def test_a_refused_project_is_alerted_and_charged_like_any_failed_run(
        self, tmp_path: Path, monkeypatch
    ):
        # The refusal leaves the capture before the run paths whose failure
        # handler alerts and counts. Routed through that same handler, a
        # deleted or relinked project pages the operator and reaches
        # auto-pause instead of failing silently on every tick.
        gone = tmp_path / "gone"
        gone.mkdir()
        job = _job(str(gone))
        gone.rmdir()
        delivered: list[tuple] = []

        async def _deliver(self_gw, j, **kw):
            delivered.append((j.id, kw["plain"]))
            return True, True, False

        from kiro_crew.slack.gateway import GatewayOrchestrator

        monkeypatch.setattr(GatewayOrchestrator, "_deliver_failure_alert", _deliver)
        assert job.consecutive_failures == 0
        captured = _run_callback(job)
        assert isinstance(captured["error"], RuntimeError)
        assert job.consecutive_failures == 1
        assert len(delivered) == 1 and "project_dir" in delivered[0][1]

    def test_sensitive_directory_at_fire_time_fails_the_run(self, repo: Path, monkeypatch):
        monkeypatch.setattr("kiro_crew.security.is_sensitive_canonical_path", lambda p: True)
        captured = _run_callback(_job(str(repo)))
        assert captured["get_or_create"] is None
        assert isinstance(captured["error"], RuntimeError)

    def test_a_component_relinked_since_authoring_fails_the_run_instead_of_being_followed(
        self, tmp_path: Path, monkeypatch
    ):
        # The store persisted the canonical directory the agent-scope check
        # judged. Replacing a component of it with a link afterwards must not
        # let the wake root the session wherever the link points: the fresh
        # resolution is compared with the persisted spelling and refused --
        # and audited -- when it differs.
        authored = tmp_path / "allowed" / "repo"
        authored.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        job = _job(str(authored))  # persisted as the canonical path
        assert job.project_dir == os.path.realpath(authored)
        shutil.rmtree(authored)
        make_dir_link(authored, outside)
        events: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: events.append(kw)
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: fake_sel)
        captured = _run_callback(job)
        assert captured["get_or_create"] is None, "must not run at the link's target"
        assert isinstance(captured["error"], RuntimeError)
        assert "no longer resolves" in str(captured["error"])
        denials = [ev for ev in events if ev.get("error") == "moved"]
        assert len(denials) == 1, "a followed relink is a security refusal and is audited"
        assert denials[0]["caller"] == "cron:g1"
        assert denials[0]["operation"] == "cron.project_dir"
        assert denials[0]["outcome"] == "denied"

    # -- a changed project ends the conversation, then the wake re-roots --

    def test_a_rescoped_wake_ends_the_old_conversation_before_the_claim(
        self, repo: Path, tmp_path: Path
    ):
        old = tmp_path / "old"
        old.mkdir()
        captured = _run_callback(_job(str(repo), was=str(old)))
        assert captured["error"] is None
        assert captured["discarded"] == ["cron:g1"]
        assert captured["order"].index(("discard", "cron:g1")) < captured["order"].index(
            ("claim", "cron:g1")
        )
        assert captured["get_or_create"]["cwd"] == os.path.realpath(repo)
        assert captured["settled"] == [_settled(os.path.realpath(repo), str(old))]

    def test_a_cleared_project_ends_the_conversation_and_cold_starts_at_the_default(
        self, repo: Path
    ):
        # project_dir cleared: the conversation rooted in the old repository is
        # ended, and the claim passes no cwd, so the fresh one roots where any
        # unscoped job roots.
        captured = _run_callback(_job("", was=str(repo)))
        assert captured["error"] is None
        assert captured["discarded"] == ["cron:g1"]
        assert captured["get_or_create"]["cwd"] is None
        assert captured["settled"] == [_settled("", str(repo))]

    def test_a_declined_end_fails_the_wake_and_keeps_the_marker(self, repo: Path, tmp_path: Path):
        # The session is busy on another surface (a dashboard turn in the old
        # repository): a repository-writing prompt must not run there, so the
        # wake is skipped and the marker stays for the next one.
        old = tmp_path / "old"
        old.mkdir()
        live = MagicMock()
        live.cwd = str(old)
        captured = _run_callback(
            _job(str(repo), was=str(old)), live_provider=live, busy_keys=frozenset({"cron:g1"})
        )
        assert isinstance(captured["error"], RuntimeError)
        assert "busy on another surface" in str(captured["error"])
        assert captured["get_or_create"] is None
        assert captured["settled"] == []

    def test_a_declined_end_is_not_reclassified_as_a_model_failure(self, tmp_path: Path):
        # The end runs outside the model-fallback try: with a pinned model and
        # "model" in the job name, the refusal is still a wake failure and
        # never a second claim on the default model.
        proj = tmp_path / "model-zoo"
        proj.mkdir()
        old = tmp_path / "old"
        old.mkdir()
        job = _job(str(proj), was=str(old))
        job.name = "model sweep"
        job.model = "claude-sonnet"
        captured = _run_callback(job, busy_keys=frozenset({"cron:g1"}))
        assert isinstance(captured["error"], RuntimeError)
        assert "busy on another surface" in str(captured["error"])
        assert captured["get_or_create_calls"] == []

    # -- the claimed session is judged on where it actually is --

    def test_a_first_project_on_a_job_with_a_conversation_at_the_default_ends_it_once(
        self, repo: Path, tmp_path: Path
    ):
        # The common adoption path: a persistent job that already has a
        # conversation (live, or stored and resumed) at the gateway default
        # is given its first project. Setting it from unset writes no rescope
        # marker, so the claim hands that conversation back; it is ended once
        # and the second claim cold-starts at the project.
        default = tmp_path / "workspace"
        default.mkdir()
        captured = _run_callback(_job(str(repo)), claimed_cwd=[str(default), None])
        assert captured["error"] is None, captured["error"]
        assert captured["discarded"] == ["cron:g1"]
        assert len(captured["get_or_create_calls"]) == 2
        assert captured["order"] == [
            ("claim", "cron:g1"),
            ("discard", "cron:g1"),
            ("claim", "cron:g1"),
        ]
        assert captured["build_message"].call_count == 1

    def test_a_second_mis_rooted_hand_back_is_released_and_fails_the_wake(
        self, repo: Path, tmp_path: Path
    ):
        # A dashboard follow-up keeps installing a session in another directory
        # on the key (cwd is ignored for a live session). Ended once, claimed
        # again, still elsewhere: released, not run.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        captured = _run_callback(_job(str(repo)), claimed_cwd=str(elsewhere))
        assert isinstance(captured["error"], RuntimeError)
        assert "rooted at" in str(captured["error"])
        assert captured["discarded"] == ["cron:g1"]
        assert len(captured["get_or_create_calls"]) == 2
        assert captured["released"] == ["cron:g1", "cron:g1"]
        assert captured["build_message"].call_count == 0

    def test_a_failure_during_the_cwd_check_releases_the_lease(self, repo: Path):
        # The claim handed the lease over, and a failure (or the wake's
        # deadline) lands in the off-loop cwd check before the caller records
        # the session as acquired: the lease must not stay held on the key.
        def _deadline(*_a, **_k):
            raise RuntimeError("wake deadline")

        with patch("kiro_crew.slack.gateway._rooted_elsewhere", _deadline):
            captured = _run_callback(_job(str(repo)))
        assert isinstance(captured["error"], RuntimeError)
        assert "wake deadline" in str(captured["error"])
        assert captured["released"] == ["cron:g1"]
        assert captured["build_message"].call_count == 0

    def test_a_mis_rooted_session_busy_elsewhere_fails_the_wake(self, repo: Path, tmp_path: Path):
        # The wrong-directory session cannot be ended (a turn is in flight on
        # another surface): no second claim, the wake is skipped.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        captured = _run_callback(
            _job(str(repo)), claimed_cwd=str(elsewhere), busy_keys=frozenset({"cron:g1"})
        )
        assert isinstance(captured["error"], RuntimeError)
        assert len(captured["get_or_create_calls"]) == 1
        assert captured["released"] == ["cron:g1"]

    def test_an_equivalent_spelling_of_the_project_is_not_refused(self, tmp_path: Path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        make_dir_link(link, real)
        captured = _run_callback(_job(str(real)), claimed_cwd=str(link))
        assert captured["error"] is None
        assert captured["discarded"] == []
        assert captured["build_message"].call_count == 1

    def test_the_project_swapped_for_a_link_after_the_capture_is_not_accepted(self, tmp_path: Path):
        # The capture validated and confined the project; between that check
        # and the claim an agent replaces the directory with a link to an
        # unadmitted one, and the session opens through the link. Resolving
        # the EXPECTATION again would make both sides name the link's target
        # and accept it: the expectation is the captured canonical spelling,
        # compared as a string, so the swapped session is refused and ended.
        repo = tmp_path / "allowed" / "repo"
        repo.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        job = _job(str(repo))
        from kiro_crew.slack import gateway as _gw

        original = _gw._rooted_elsewhere
        swapped = {"done": False}

        def _swap_then_check(client, **kw):
            if not swapped["done"]:
                swapped["done"] = True  # tracked: is_symlink misses a junction
                repo.rmdir()
                make_dir_link(repo, outside)
            return original(client, **kw)

        with patch.object(_gw, "_rooted_elsewhere", _swap_then_check):
            captured = _run_callback(job, claimed_cwd=str(repo))
        assert isinstance(captured["error"], RuntimeError)
        assert "rooted at" in str(captured["error"])
        assert captured["build_message"].call_count == 0

    def test_a_project_set_back_to_its_earlier_value_is_at_the_project(self, repo: Path):
        # A -> B -> A before a wake: the store keeps the first marker (A), so
        # the wake's "previous" IS the project. Being at the project wins; the
        # fresh session claimed at A is run and the marker is settled.
        captured = _run_callback(_job(str(repo), was=str(repo)))
        assert captured["error"] is None, captured["error"]
        assert captured["get_or_create"]["cwd"] == os.path.realpath(repo)
        assert captured["released"] == ["cron:g1"]  # the ordinary run-end release
        assert captured["settled"] == [_settled(os.path.realpath(repo), str(repo))]

    def test_a_cleared_wake_still_in_the_previous_project_is_ended_and_reclaimed(
        self, repo: Path, tmp_path: Path
    ):
        # Cleared project, but the claim came back in the previous repository
        # anyway (a conversation fork that restored the stored cwd): ended
        # once; the second claim roots at the default and runs.
        default = tmp_path / "workspace"
        default.mkdir()
        captured = _run_callback(_job("", was=str(repo)), claimed_cwd=[str(repo), str(default)])
        assert captured["error"] is None, captured["error"]
        assert captured["discarded"] == ["cron:g1", "cron:g1"]
        assert captured["build_message"].call_count == 1
        assert captured["settled"] == [_settled("", str(repo))]

    def test_a_cleared_wake_anywhere_else_is_fine(self, repo: Path, tmp_path: Path):
        default = tmp_path / "workspace"
        default.mkdir()
        captured = _run_callback(_job("", was=str(repo)), claimed_cwd=str(default))
        assert captured["error"] is None
        assert captured["build_message"].call_count == 1
        assert captured["settled"] == [_settled("", str(repo))]

    def test_a_mis_rooted_claim_is_not_reclassified_as_a_model_failure(self, tmp_path: Path):
        proj = tmp_path / "model-zoo"
        proj.mkdir()
        elsewhere = tmp_path / "model-registry"
        elsewhere.mkdir()
        job = _job(str(proj))
        job.model = "claude-sonnet"
        captured = _run_callback(job, claimed_cwd=str(elsewhere))
        assert isinstance(captured["error"], RuntimeError)
        assert "rooted at" in str(captured["error"])
        # Two claims (the end-once retry), none of them the model fallback's
        # second get_or_create: every claim carried the pinned model.
        assert [c.get("model") for c in captured["get_or_create_calls"]] == [
            "claude-sonnet",
            "claude-sonnet",
        ]

    # -- the stable key of a stateless job --

    def test_a_stateless_wake_ends_the_stable_keys_conversation_before_settling(
        self, repo: Path, tmp_path: Path
    ):
        # persistent_session off: the wake runs on a throwaway key, but the
        # STABLE key may still hold a conversation rooted in the old repository
        # that re-enabling persistence would resume. It is ended too, before
        # the marker is cleared.
        old = tmp_path / "old"
        old.mkdir()
        captured = _run_callback(_job(str(repo), was=str(old), persistent=False))
        assert captured["error"] is None
        assert "cron:g1" in captured["discarded"]
        assert captured["settled"] == [_settled(os.path.realpath(repo), str(old))]

    def test_a_stateless_wake_fails_before_its_turn_when_the_stable_key_is_busy(
        self, repo: Path, tmp_path: Path
    ):
        # The stable key's conversation cannot be ended (busy on another
        # surface), so the marker must stay set -- and a turn run under a kept
        # marker is work the next wake's end would cancel. The wake fails
        # before the turn; the next wake judges the stable key again.
        old = tmp_path / "old"
        old.mkdir()
        live = MagicMock()
        live.cwd = str(old)
        captured = _run_callback(
            _job(str(repo), was=str(old), persistent=False),
            live_provider=live,
            busy_keys=frozenset({"cron:g1"}),
        )
        assert isinstance(captured["error"], RuntimeError)
        assert "cron:g1" in captured["discarded"]
        assert captured["settled"] == []
        assert captured["build_message"].call_count == 0

    # -- the marker is cleared only for the scope this wake acted on --

    def test_the_settle_is_conditioned_on_the_scope_the_wake_read(self, repo: Path, tmp_path: Path):
        # A -> B before the wake (marker A, project B): the clear names BOTH
        # values it read, so a rescope landing during the wake fails the
        # compare-and-swap in the store instead of being erased.
        other = tmp_path / "other"
        other.mkdir()
        captured = _run_callback(_job(str(other), was=str(repo)))
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
        captured = _run_callback(
            _job(str(other), was=str(repo)),
            settle_raises=CronPendingMismatch("project scope changed concurrently"),
        )
        assert captured["error"] is None
        assert captured["get_or_create"]["cwd"] == os.path.realpath(other)
        assert len(captured["settled"]) == 1

    def test_a_store_that_cannot_clear_the_marker_fails_the_wake_before_the_turn(
        self, repo: Path, tmp_path: Path
    ):
        # A kept marker makes the NEXT wake end this key's conversation again,
        # cancelling whatever this wake's turn left running in the new
        # project. So when the store cannot take the clear, the wake fails
        # here, before the turn: nothing new has started for that end to lose.
        from kiro_crew.cron import CronStoreBusy

        other = tmp_path / "other"
        other.mkdir()
        captured = _run_callback(
            _job(str(other), was=str(repo)),
            settle_raises=CronStoreBusy("cron store lock held"),
        )
        assert isinstance(captured["error"], RuntimeError)
        assert "rescope marker" in str(captured["error"])
        assert len(captured["settled"]) == 1
        assert captured["build_message"].call_count == 0

    def test_a_clear_landing_during_validation_keeps_the_marker(self, repo: Path, tmp_path: Path):
        # The scope pair is read BEFORE the validation await, and the validated
        # project is the one that pair names. A dashboard clear landing during
        # the await must not pair the old validated cwd with the new ("",
        # old) reading: that wake re-roots nothing, so it must not clear the
        # marker either -- the next wake ends the conversation.
        job = _job(str(repo))

        def _clear_during_validation(project_dir, **_kw):
            job.project_dir, job.project_dir_was = "", os.path.realpath(repo)
            return os.path.realpath(project_dir)

        with patch("kiro_crew.slack.gateway.validate_cron_project_dir", _clear_during_validation):
            captured = _run_callback(job)
        assert captured["error"] is None
        assert captured["get_or_create"]["cwd"] == os.path.realpath(repo)
        assert captured["discarded"] == []
        assert captured["settled"] == [], "a wake that did not re-root clears no marker"


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
        real = cron_mod.validate_cron_project_dir

        def _counting(raw, **kw):
            calls.append(raw)
            return real(raw, **kw)

        monkeypatch.setattr(cron_mod, "validate_cron_project_dir", _counting)
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

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink swap")
    def test_containment_judges_the_persisted_spelling_not_a_second_resolution(
        self, tmp_path: Path
    ):
        # The candidate reaches the containment check already canonical, and
        # that string is what the store persists. A directory swapped for a
        # link INTO the allowed root between the resolution and the check
        # must not be admitted: the persisted spelling, restored afterwards,
        # would name the outside directory and every wake would run there.
        from kiro_crew.cron_service.fields import _project_dir_within

        allowed = tmp_path / "allowed"
        allowed.mkdir()
        (allowed / "repo").mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        canonical = os.path.realpath(outside / "repo")
        (outside / "repo").mkdir()
        # The swap: the canonical spelling now resolves inside the root.
        shutil.rmtree(outside / "repo")
        make_dir_link(outside / "repo", allowed / "repo")
        assert _project_dir_within(canonical, [str(allowed)]) is False
        assert _project_dir_within(os.path.realpath(allowed / "repo"), [str(allowed)]) is True


class TestMcpTools:
    @pytest.fixture(autouse=True)
    def _named(self, named_cron_caller, _floor_monkeypatch, tmp_path):
        _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        _floor_monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        # An agent's choice is confined to the caller's scope; these tests name
        # directories under tmp_path, so that is the configured allowlist here.
        _allow_roots(_floor_monkeypatch, [str(tmp_path)])

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
        make_dir_link(allowed / "link", outside)
        _allow_roots(monkeypatch, [str(allowed)])
        monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path / "caller"))
        result = _call_tool_inner(
            "cron_add",
            {"name": "x", "message": "go", "every": 300, "project_dir": str(allowed / "link")},
        )
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
            with patch("kiro_crew.cli_commands._agent_cwd_allowed_roots", return_value=["/r"]):
                _cron(ns)
            assert svc.update_job.call_args.kwargs == {
                "project_dir": str(repo),
                "project_dir_allowed_roots": ["/r"],
                "audit_caller": "cli",
            }
            ns = argparse.Namespace(cron_action="update", job_id="abc", project_dir="")
            _cron(ns)
            assert svc.update_job.call_args.kwargs == {"project_dir": "", "audit_caller": "cli"}

    def test_add_and_update_confine_project_dir_to_the_allowed_roots(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        # An agent with shell access reaches the CLI as easily as cron_add, so
        # a directory outside agent.subagent_cwd_allowed_roots is refused here too.
        from kiro_crew.cli_commands import _cron

        allowed, outside = tmp_path / "allowed", tmp_path / "outside"
        allowed.mkdir()
        outside.mkdir()
        svc = CronService(base_dir=tmp_path / "store")
        monkeypatch.setattr(
            "kiro_crew.cli_commands._agent_cwd_allowed_roots", lambda: [str(allowed)]
        )
        with (
            patch("kiro_crew.cli_commands.CronService", return_value=svc),
            patch("kiro_crew.cli_commands.sel") as sel_,
        ):
            with pytest.raises(SystemExit):
                _cron(_ns(every=300, project_dir=str(outside)))
            assert svc.list_jobs() == []
            _cron(_ns(every=300, project_dir=str(allowed)))
            (job,) = svc.list_jobs()
            ns = argparse.Namespace(cron_action="update", job_id=job.id, project_dir=str(outside))
            with pytest.raises(SystemExit):
                _cron(ns)
        denials = [
            c.kwargs["operation"]
            for c in sel_.return_value.log_api_access.call_args_list
            if c.kwargs.get("outcome") == "denied"
        ]
        assert denials == ["cron.add", "cron.update"]
        assert svc.get_job(job.id).project_dir == os.path.realpath(allowed)
        assert "allowed_roots" in capsys.readouterr().err
