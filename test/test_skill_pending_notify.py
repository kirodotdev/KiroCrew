"""Tests for the pending-candidate staged notification hook.

The hook is the Stage-5 notification seam: ``stage_skill_candidate`` fires a
MODULE-level observer (not a per-instance callback) so a candidate staged by ANY
loader — consolidation's ContextBuilder loader or a per-request dashboard one —
surfaces to the user. The gateway registers a hook that raises a bell-feed
notification + broadcasts ``skills.pending_changed``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kiro_crew import skills as S
from kiro_crew.skills import AutoSkillProvenance, SkillsLoader


@pytest.fixture()
def loader(tmp_path):
    return SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)


@pytest.fixture(autouse=True)
def _clear_hook():
    """Never leak a hook across tests (it is module-level global state)."""
    S.set_pending_staged_hook(None)
    yield
    S.set_pending_staged_hook(None)


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
