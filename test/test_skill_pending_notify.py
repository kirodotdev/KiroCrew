"""Tests for the pending-candidate staged + consumed notification hooks.

The hooks are the Stage-5 notification seam: ``stage_skill_candidate`` fires a
MODULE-level observer (not a per-instance callback) so a candidate staged by ANY
loader — consolidation's ContextBuilder loader or a per-request dashboard one —
surfaces to the user. The gateway registers a hook that raises a bell-feed
notification + broadcasts ``skills.pending_changed``.

The consumed hook is the counterpart for candidates LEAVING the queue
(approved, dismissed, or TTL-pruned): the gateway registers a hook that
retires the candidate's bell notification so the badge doesn't stay lit for a
review that can no longer be acted on.
"""

from __future__ import annotations

import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kiro_crew import skills as S
from kiro_crew.skills import AutoSkillProvenance, SkillsLoader


@pytest.fixture()
def loader(tmp_path):
    return SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)


@pytest.fixture(autouse=True)
def _clear_hook():
    """Never leak a hook across tests (they are module-level global state)."""
    S.set_pending_staged_hook(None)
    S.set_pending_consumed_hook(None)
    yield
    S.set_pending_staged_hook(None)
    S.set_pending_consumed_hook(None)


def _prov() -> AutoSkillProvenance:
    return AutoSkillProvenance(
        session_key="sess-1",
        created_at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    )


def _stage(loader, slug, **kw):
    return loader.stage_skill_candidate(
        slug,
        description=f"desc {slug}",
        triggers=slug,
        procedure_md="## Steps\n\nrun it",
        provenance=_prov(),
        **kw,
    )


def test_hook_fires_for_new_candidate(loader):
    seen: list[dict] = []
    S.set_pending_staged_hook(seen.append)
    assert _stage(loader, "cand-new") == "auto/cand-new"
    assert len(seen) == 1
    assert seen[0]["name"] == "auto/cand-new"
    assert seen[0]["slug"] == "cand-new"
    assert seen[0]["kind"] == "new"
    assert seen[0]["target"] is None
    assert seen[0]["has_scripts"] is False


def test_hook_fires_for_update_candidate_with_target(loader):
    seen: list[dict] = []
    S.set_pending_staged_hook(seen.append)
    _stage(
        loader,
        "helper-update",
        kind="update",
        target="auto/helper",
        base_version=3,
    )
    assert len(seen) == 1
    assert seen[0]["kind"] == "update"
    assert seen[0]["target"] == "auto/helper"


def test_hook_reports_scripts_flag(loader):
    seen: list[dict] = []
    S.set_pending_staged_hook(seen.append)
    _stage(
        loader,
        "with-script",
        scripts=[{"filename": "go.py", "content": "print('hi')\n"}],
    )
    assert seen and seen[0]["has_scripts"] is True


def test_hook_carries_description_and_triggers(loader):
    # The notification body is built from these: without them it can only say
    # THAT a skill was generated, never what it does -- the one fact a reviewer
    # needs to decide whether to open the queue at all.
    seen: list[dict] = []
    S.set_pending_staged_hook(seen.append)
    _stage(loader, "cand-detail")
    assert seen[0]["description"] == "desc cand-detail"
    assert seen[0]["triggers"] == "cand-detail"


def test_no_hook_registered_is_a_silent_noop(loader):
    # CLI processes register nothing — staging must still succeed.
    assert _stage(loader, "cand-silent") == "auto/cand-silent"
    assert [p["slug"] for p in loader.list_pending_skills()] == ["cand-silent"]


def test_hook_failure_never_breaks_staging(loader):
    def boom(_info: dict) -> None:
        raise RuntimeError("observer exploded")

    S.set_pending_staged_hook(boom)
    # Staging already succeeded on disk before the hook runs; a broken observer
    # must not turn that into a failure.
    assert _stage(loader, "cand-boom") == "auto/cand-boom"
    assert [p["slug"] for p in loader.list_pending_skills()] == ["cand-boom"]


def test_hook_not_fired_when_staging_rejected(loader):
    seen: list[dict] = []
    S.set_pending_staged_hook(seen.append)
    # Slug fails validation → no candidate, so no notification.
    assert _stage(loader, "x") is None
    assert seen == []


def test_set_hook_replaces_rather_than_stacks(loader):
    first: list[dict] = []
    second: list[dict] = []
    S.set_pending_staged_hook(first.append)
    S.set_pending_staged_hook(second.append)
    _stage(loader, "cand-replace")
    assert first == []
    assert len(second) == 1


# ── consumed hook (approve / dismiss / prune) ──


def _payloads_sans_stamp(seen):
    """Strip (and validate) the consumed_at stamp so payloads compare exactly."""
    out = []
    for info in seen:
        info = dict(info)
        stamp = info.pop("consumed_at")
        assert isinstance(stamp, str) and stamp  # ISO-8601 UTC, always present
        out.append(info)
    return out


def test_consumed_hook_fires_on_approve(loader):
    _stage(loader, "cand-approve")
    seen: list[dict] = []
    S.set_pending_consumed_hook(seen.append)
    assert loader.approve_pending_skill("cand-approve") == "auto/cand-approve"
    assert _payloads_sans_stamp(seen) == [
        {"slug": "cand-approve", "outcome": "approved", "name": "auto/cand-approve"}
    ]


def test_consumed_hook_fires_on_update_approve(loader):
    _stage(loader, "cand-upd")
    assert loader.approve_pending_skill("cand-upd") == "auto/cand-upd"
    _stage(loader, "cand-upd", kind="update", target="auto/cand-upd", base_version=1)
    seen: list[dict] = []
    S.set_pending_consumed_hook(seen.append)
    assert loader.approve_pending_update("cand-upd") == "auto/cand-upd"
    assert _payloads_sans_stamp(seen) == [{"slug": "cand-upd", "outcome": "approved", "name": "auto/cand-upd"}]


def test_consumed_hook_not_fired_when_update_cleanup_leaves_candidate(loader, monkeypatch):
    # approve_pending_update deletes the candidate with ignore_errors=True
    # (a Windows file lock can silently defeat it). A surviving candidate is
    # still an actionable review in the pending queue, so its notification
    # must NOT be retired.
    _stage(loader, "cand-locked")
    assert loader.approve_pending_skill("cand-locked") == "auto/cand-locked"
    _stage(loader, "cand-locked", kind="update", target="auto/cand-locked", base_version=1)
    pending_dir = loader._pending_root() / "cand-locked"
    real_rmtree = shutil.rmtree

    def _locked_rmtree(path, *args, **kwargs):
        if Path(path) == pending_dir and kwargs.get("ignore_errors"):
            return  # swallow, like rmtree with a locked file inside
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("kiro_crew.skills.shutil.rmtree", _locked_rmtree)
    seen: list[dict] = []
    S.set_pending_consumed_hook(seen.append)
    assert loader.approve_pending_update("cand-locked") == "auto/cand-locked"
    assert pending_dir.is_dir()  # cleanup really was defeated
    assert seen == []


def test_consumed_hook_fires_on_dismiss(loader):
    _stage(loader, "cand-dismiss")
    seen: list[dict] = []
    S.set_pending_consumed_hook(seen.append)
    assert loader.dismiss_pending_skill("cand-dismiss") is True
    assert _payloads_sans_stamp(seen) == [{"slug": "cand-dismiss", "outcome": "dismissed"}]


def test_consumed_hook_fires_on_ttl_prune(loader):
    # Pruning routes through dismiss_pending_skill, so the hook covers the
    # silent-expiry path too — a pruned candidate's notification must not
    # outlive the candidate any more than a dismissed one's.
    _stage(loader, "cand-prune")
    seen: list[dict] = []
    S.set_pending_consumed_hook(seen.append)
    assert loader.prune_pending(0, now=time.time() + 60) == 1
    assert _payloads_sans_stamp(seen) == [{"slug": "cand-prune", "outcome": "dismissed"}]


def test_consumed_hook_not_fired_when_nothing_consumed(loader):
    seen: list[dict] = []
    S.set_pending_consumed_hook(seen.append)
    assert loader.approve_pending_skill("no-such-slug") is None
    assert loader.dismiss_pending_skill("no-such-slug") is False
    assert seen == []


def test_consumed_no_hook_registered_is_a_silent_noop(loader):
    # CLI processes register nothing — consumption must still succeed.
    _stage(loader, "cand-cli")
    assert loader.dismiss_pending_skill("cand-cli") is True
    assert loader.list_pending_skills() == []


def test_consumed_hook_failure_never_breaks_consumption(loader):
    def boom(_info: dict) -> None:
        raise RuntimeError("observer exploded")

    S.set_pending_consumed_hook(boom)
    _stage(loader, "cand-boom2")
    # The candidate is gone from disk before the hook runs; a broken observer
    # must not turn that into a failure.
    assert loader.approve_pending_skill("cand-boom2") == "auto/cand-boom2"
    assert loader.list_pending_skills() == []
