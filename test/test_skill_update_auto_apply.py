"""Tests: auto-apply of prose-only skill UPDATE candidates when approval is off.

When ``skills.approval_required`` is false, prose-only NEW candidates already
go live without review — these tests cover the matching UPDATE behavior: a
prose-only update candidate is staged and immediately promoted through
``approve_pending_update`` (same guards, version snapshot, pruning), audited as
``auto_applied_update`` and announced via the informational auto-applied hook
instead of a review request. Script-bearing updates and approval-on instances
keep the staging behavior, and any promotion failure fails SAFE (candidate
stays pending, review notification fires).
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import flock_compat
from kiro_crew import skills as S
from kiro_crew.history import VERDICT_UPDATE, HistoryConsolidator
from kiro_crew.skills import AutoSkillProvenance, SkillsLoader

_NEW_STEPS = "## Steps\n\n1. the new way\n"


@pytest.fixture()
def loader(tmp_path):
    return SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)


@pytest.fixture(autouse=True)
def _clear_hooks():
    """Never leak a hook across tests (module-level global state)."""
    S.set_pending_staged_hook(None)
    S.set_update_auto_applied_hook(None)
    yield
    S.set_pending_staged_hook(None)
    S.set_update_auto_applied_hook(None)


def _write_live(loader, slug, *, version=1, body="original body"):
    """Write a live auto-skill directly (frontmatter version included)."""
    live = loader._dir / "auto" / slug
    live.mkdir(parents=True, exist_ok=True)
    content = (
        "---\n"
        f"name: auto/{slug}\n"
        "description: live desc\n"
        "triggers: t\n"
        "source: auto\n"
        "created_at: 2020-01-01T00:00:00+00:00\n"
        f"version: {version}\n"
        "---\n\n"
        f"# {slug}\n\n{body}\n"
    )
    (live / "SKILL.md").write_text(content, encoding="utf-8")
    loader._invalidate_iter_cache()
    return live


def _prov() -> AutoSkillProvenance:
    return AutoSkillProvenance(
        session_key="s",
        created_at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    )


def _mk(loader, *, approval_required, auto_refine_enabled=False):
    c = HistoryConsolidator(
        log=MagicMock(),
        memory=MagicMock(),
        skills_loader=loader,
        auto_skills_enabled=True,
        approval_required=approval_required,
        auto_refine_enabled=auto_refine_enabled,
    )
    c._event_loop = None  # skip the LLM merge; candidate body is used as-is
    return c


def _sel_recorder(recorded):
    ctx = patch("kiro_crew.history.sel")
    mock = ctx.start()
    mock.return_value.log_tool_invocation = lambda **k: recorded.append(k)
    return ctx


def _stage_update(c, *, scripts=None, scripts_supplied=False):
    c._stage_skill_update(
        key="sess",
        target_key="auto/deploy-helper",
        description="new desc",
        triggers="new trigger",
        procedure_md=_NEW_STEPS,
        scripts=scripts,
        scripts_supplied=scripts_supplied,
    )


def _pending_slugs(loader):
    return [p["slug"] for p in loader.list_pending_skills()]


# ── (a) prose-only update auto-applies when approval is off ──


def test_prose_only_update_auto_applies_when_approval_off(loader):
    _write_live(loader, "deploy-helper", version=1)
    staged_seen: list[dict] = []
    applied_seen: list[dict] = []
    S.set_pending_staged_hook(staged_seen.append)
    S.set_update_auto_applied_hook(applied_seen.append)

    c = _mk(loader, approval_required=False)
    recorded: list[dict] = []
    ctx = _sel_recorder(recorded)
    try:
        _stage_update(c)
    finally:
        ctx.stop()

    # Live skill carries the update and its version incremented.
    body = loader.read_auto_skill_body("auto/deploy-helper")
    assert body is not None and "the new way" in body
    assert loader.get_auto_skill_version("auto/deploy-helper") == 2
    # The prior version was snapshotted for rollback.
    snapshot = loader._dir / "auto" / "deploy-helper" / ".versions" / "v1-SKILL.md"
    assert snapshot.exists()
    assert "original body" in snapshot.read_text(encoding="utf-8")
    # The candidate did not stay in the queue.
    assert _pending_slugs(loader) == []
    # Audited as an auto-applied update.
    applied = [r for r in recorded if r.get("outcome") == "auto_applied_update"]
    assert applied and applied[0]["metadata"]["new_version"] == 2
    assert applied[0]["metadata"]["target"] == "auto/deploy-helper"
    # Informational notification fired; the review request did NOT.
    assert len(applied_seen) == 1
    assert applied_seen[0]["target"] == "auto/deploy-helper"
    assert applied_seen[0]["new_version"] == 2
    assert staged_seen == []


# ── (b) script-bearing update still stages when approval is off ──


def test_script_bearing_update_still_stages_when_approval_off(loader):
    _write_live(loader, "deploy-helper", version=1)
    staged_seen: list[dict] = []
    applied_seen: list[dict] = []
    S.set_pending_staged_hook(staged_seen.append)
    S.set_update_auto_applied_hook(applied_seen.append)

    c = _mk(loader, approval_required=False)
    recorded: list[dict] = []
    ctx = _sel_recorder(recorded)
    try:
        _stage_update(
            c,
            scripts=[{"filename": "go.py", "content": "print('hi')\n"}],
            scripts_supplied=True,
        )
    finally:
        ctx.stop()

    # Candidate is queued for review; live is untouched.
    assert _pending_slugs(loader) == ["deploy-helper-update"]
    assert loader.get_auto_skill_version("auto/deploy-helper") == 1
    body = loader.read_auto_skill_body("auto/deploy-helper")
    assert body is not None and "original body" in body
    # Review request fired; no auto-apply happened.
    assert len(staged_seen) == 1 and staged_seen[0]["has_scripts"] is True
    assert applied_seen == []
    assert not [r for r in recorded if r.get("outcome") == "auto_applied_update"]


# ── (c) prose update still stages when approval is on ──


def test_prose_update_still_stages_when_approval_on(loader):
    _write_live(loader, "deploy-helper", version=1)
    staged_seen: list[dict] = []
    applied_seen: list[dict] = []
    S.set_pending_staged_hook(staged_seen.append)
    S.set_update_auto_applied_hook(applied_seen.append)

    c = _mk(loader, approval_required=True)
    recorded: list[dict] = []
    ctx = _sel_recorder(recorded)
    try:
        _stage_update(c)
    finally:
        ctx.stop()

    assert _pending_slugs(loader) == ["deploy-helper-update"]
    assert loader.get_auto_skill_version("auto/deploy-helper") == 1
    assert len(staged_seen) == 1
    assert applied_seen == []
    assert not [r for r in recorded if r.get("outcome") == "auto_applied_update"]


# ── (c2) same-result refine of the target vetoes auto-apply ──


def test_update_stays_staged_when_same_result_refines_target(loader, monkeypatch):
    """A ``new_skill`` deduped as an UPDATE of a target that the same result
    ALSO refines must not auto-apply: the refine path overwrites live through
    ``update_auto_skill`` — no version snapshot, body derived from the
    pre-update skill — so an immediate promotion would be silently destroyed.
    The update stays staged for review against the refined skill."""
    _write_live(loader, "deploy-helper", version=1)
    staged_seen: list[dict] = []
    applied_seen: list[dict] = []
    S.set_pending_staged_hook(staged_seen.append)
    S.set_update_auto_applied_hook(applied_seen.append)

    c = _mk(loader, approval_required=False, auto_refine_enabled=True)
    monkeypatch.setattr(
        c, "_dedupe_candidate", lambda s, d, t: (VERDICT_UPDATE, "auto/deploy-helper")
    )
    recorded: list[dict] = []
    ctx = _sel_recorder(recorded)
    try:
        c._process_auto_skills(
            {
                "new_skill": {
                    "slug": "deploy-helper-2",
                    "description": "new desc",
                    "triggers": "new trigger",
                    "procedure_md": _NEW_STEPS,
                },
                "refined_skill": {
                    "name": "auto/deploy-helper",
                    "description": "refined desc",
                    "triggers": "t",
                    "procedure_md": "## Steps\n\n1. the refined way\n",
                },
            },
            "sess",
        )
    finally:
        ctx.stop()

    # The update candidate stayed in the queue and its body survives for review.
    assert _pending_slugs(loader) == ["deploy-helper-update"]
    pend = loader.get_pending_skill("deploy-helper-update")
    assert pend is not None and "the new way" in pend["content"]
    # No promotion happened; the refine result owns the live body.
    assert not [r for r in recorded if r.get("outcome") == "auto_applied_update"]
    assert applied_seen == []
    body = loader.read_auto_skill_body("auto/deploy-helper")
    assert body is not None and "the refined way" in body
    assert "the new way" not in body
    # The review request fired (staging did not suppress it).
    assert len(staged_seen) == 1
    assert staged_seen[0]["slug"] == "deploy-helper-update"


def test_refine_enabled_stages_update_even_for_other_target(loader, monkeypatch):
    """The guard is config-scoped, not result-scoped: with auto-refine enabled,
    a refine of THIS update's target can arrive from any concurrent session —
    invisible to this result — so auto-apply is disabled even when this
    result's own ``refined_skill`` names a different skill. The update stages
    for review; the unrelated refine still lands on its own target."""
    _write_live(loader, "deploy-helper", version=1)
    _write_live(loader, "other-skill", version=1)
    staged_seen: list[dict] = []
    applied_seen: list[dict] = []
    S.set_pending_staged_hook(staged_seen.append)
    S.set_update_auto_applied_hook(applied_seen.append)

    c = _mk(loader, approval_required=False, auto_refine_enabled=True)
    monkeypatch.setattr(
        c, "_dedupe_candidate", lambda s, d, t: (VERDICT_UPDATE, "auto/deploy-helper")
    )
    recorded: list[dict] = []
    ctx = _sel_recorder(recorded)
    try:
        c._process_auto_skills(
            {
                "new_skill": {
                    "slug": "deploy-helper-2",
                    "description": "new desc",
                    "triggers": "new trigger",
                    "procedure_md": _NEW_STEPS,
                },
                "refined_skill": {
                    "name": "auto/other-skill",
                    "description": "refined desc",
                    "triggers": "t",
                    "procedure_md": "## Steps\n\n1. refined other\n",
                },
            },
            "sess",
        )
    finally:
        ctx.stop()

    # No unattended promotion while refine is enabled; the update is staged.
    assert _pending_slugs(loader) == ["deploy-helper-update"]
    assert not [r for r in recorded if r.get("outcome") == "auto_applied_update"]
    assert applied_seen == []
    assert loader.get_auto_skill_version("auto/deploy-helper") == 1
    # The unrelated refine landed on its own target.
    other = loader.read_auto_skill_body("auto/other-skill")
    assert other is not None and "refined other" in other
    # The review request fired for the staged update.
    assert len(staged_seen) == 1
    assert staged_seen[0]["slug"] == "deploy-helper-update"


def test_concurrent_session_refine_does_not_lose_either_write(loader, monkeypatch):
    """Regression (GPT round 9): with auto-refine enabled, a DIFFERENT
    session's refine of the same target races the unattended promotion.
    ``update_auto_skill`` is unlocked and leaves the version frontmatter
    unchanged, so the promotion's stale-base check passes and last-write-wins
    silently discards one side. With auto-apply disabled under refine, the
    concurrent refine's write survives live and this session's update stays
    staged — neither write is lost."""
    _write_live(loader, "deploy-helper", version=1)
    staged_seen: list[dict] = []
    applied_seen: list[dict] = []
    S.set_pending_staged_hook(staged_seen.append)
    S.set_update_auto_applied_hook(applied_seen.append)

    c = _mk(loader, approval_required=False, auto_refine_enabled=True)
    monkeypatch.setattr(
        c, "_dedupe_candidate", lambda s, d, t: (VERDICT_UPDATE, "auto/deploy-helper")
    )

    # Interleave the other session's refine right after this session stages —
    # inside the window where the pre-guard code promoted next. On the old
    # code the promotion then overwrote live (base_version 1 == live version
    # 1, staleness check passes) and the refine's write was discarded.
    real_stage = loader.stage_skill_candidate

    def _stage_then_concurrent_refine(*args, **kwargs):
        name = real_stage(*args, **kwargs)
        assert loader.update_auto_skill(
            "auto/deploy-helper",
            description="refined by other session",
            triggers="t",
            procedure_md="## Steps\n\n1. the other session way\n",
            provenance=_prov(),
        )
        return name

    monkeypatch.setattr(loader, "stage_skill_candidate", _stage_then_concurrent_refine)

    recorded: list[dict] = []
    ctx = _sel_recorder(recorded)
    try:
        c._process_auto_skills(
            {
                "new_skill": {
                    "slug": "deploy-helper-2",
                    "description": "new desc",
                    "triggers": "new trigger",
                    "procedure_md": _NEW_STEPS,
                }
            },
            "sess",
        )
    finally:
        ctx.stop()

    # No unattended promotion raced the refine.
    assert not [r for r in recorded if r.get("outcome") == "auto_applied_update"]
    assert applied_seen == []
    # The concurrent session's refine survives live...
    body = loader.read_auto_skill_body("auto/deploy-helper")
    assert body is not None and "the other session way" in body
    assert "the new way" not in body
    # ...and this session's update was not lost: it is staged for review.
    assert _pending_slugs(loader) == ["deploy-helper-update"]
    pend = loader.get_pending_skill("deploy-helper-update")
    assert pend is not None and "the new way" in pend["content"]
    assert len(staged_seen) == 1


# ── (d) auto-approve failure leaves the candidate staged ──


def test_auto_apply_failure_leaves_candidate_staged(loader, monkeypatch):
    _write_live(loader, "deploy-helper", version=1)
    staged_seen: list[dict] = []
    applied_seen: list[dict] = []
    S.set_pending_staged_hook(staged_seen.append)
    S.set_update_auto_applied_hook(applied_seen.append)
    # Promotion refuses (any approve_pending_update guard tripping).
    monkeypatch.setattr(loader, "approve_pending_update", lambda slug: None)

    c = _mk(loader, approval_required=False)
    recorded: list[dict] = []
    ctx = _sel_recorder(recorded)
    try:
        _stage_update(c)
    finally:
        ctx.stop()

    # Fail SAFE: the candidate stays pending, live untouched.
    assert _pending_slugs(loader) == ["deploy-helper-update"]
    assert loader.get_auto_skill_version("auto/deploy-helper") == 1
    # The review request suppressed at staging time was re-fired, so the
    # still-pending candidate does not sit invisible in the queue.
    assert len(staged_seen) == 1
    assert staged_seen[0]["slug"] == "deploy-helper-update"
    assert staged_seen[0]["kind"] == "update"
    assert applied_seen == []
    assert not [r for r in recorded if r.get("outcome") == "auto_applied_update"]
    # The staging itself is still audited.
    assert [r for r in recorded if r.get("outcome") == "staged_update"]


def test_auto_apply_exception_leaves_candidate_staged(loader, monkeypatch):
    _write_live(loader, "deploy-helper", version=1)
    staged_seen: list[dict] = []
    S.set_pending_staged_hook(staged_seen.append)

    def boom(slug):
        raise RuntimeError("disk went away")

    monkeypatch.setattr(loader, "auto_apply_pending_update", boom)

    c = _mk(loader, approval_required=False)
    ctx = _sel_recorder([])
    try:
        _stage_update(c)
    finally:
        ctx.stop()

    assert _pending_slugs(loader) == ["deploy-helper-update"]
    assert loader.get_auto_skill_version("auto/deploy-helper") == 1
    assert len(staged_seen) == 1


# ── (e) a candidate that SUPPLIED scripts never auto-applies, even when the
#        validator rejected every one of them ──


def test_rejected_scripts_candidate_still_stages_when_approval_off(loader):
    _write_live(loader, "deploy-helper", version=1)
    applied_seen: list[dict] = []
    S.set_update_auto_applied_hook(applied_seen.append)

    c = _mk(loader, approval_required=False)
    ctx = _sel_recorder([])
    try:
        # scripts=None (all rejected by the validator) but scripts_supplied=True:
        # the candidate wanted scripts, so it must never auto-publish.
        _stage_update(c, scripts=None, scripts_supplied=True)
    finally:
        ctx.stop()

    assert _pending_slugs(loader) == ["deploy-helper-update"]
    assert loader.get_auto_skill_version("auto/deploy-helper") == 1
    assert applied_seen == []


# ── loader-level: auto_apply_pending_update guards ──


def _stage_candidate(loader, *, scripts=None, base_version=1, notify=True):
    return loader.stage_skill_candidate(
        "deploy-helper-update",
        description="updated",
        triggers="t",
        procedure_md=_NEW_STEPS,
        provenance=_prov(),
        scripts=scripts,
        kind="update",
        target="auto/deploy-helper",
        base_version=base_version,
        notify=notify,
    )


def test_loader_auto_apply_promotes_and_emits(loader):
    _write_live(loader, "deploy-helper", version=1)
    applied_seen: list[dict] = []
    S.set_update_auto_applied_hook(applied_seen.append)
    assert _stage_candidate(loader, notify=False) == "auto/deploy-helper-update"

    result = loader.auto_apply_pending_update("deploy-helper-update")

    assert result == ("auto/deploy-helper", 2)
    assert loader.get_auto_skill_version("auto/deploy-helper") == 2
    assert _pending_slugs(loader) == []
    assert len(applied_seen) == 1
    assert applied_seen[0]["target"] == "auto/deploy-helper"
    assert applied_seen[0]["new_version"] == 2


def test_loader_auto_apply_refuses_script_bearing_candidate(loader):
    _write_live(loader, "deploy-helper", version=1)
    applied_seen: list[dict] = []
    S.set_update_auto_applied_hook(applied_seen.append)
    _stage_candidate(
        loader, scripts=[{"filename": "go.py", "content": "print('hi')\n"}], notify=False
    )

    assert loader.auto_apply_pending_update("deploy-helper-update") is None

    # Candidate untouched, live untouched, no notification.
    assert _pending_slugs(loader) == ["deploy-helper-update"]
    assert loader.get_auto_skill_version("auto/deploy-helper") == 1
    assert applied_seen == []


def test_loader_auto_apply_refuses_physical_scripts_dir_without_meta(loader):
    """Defense in depth: a direct-write candidate whose ``.meta.json`` is
    missing or lies about ``has_scripts`` must still be refused when a physical
    ``scripts/`` dir exists — otherwise scripts would go live unreviewed."""
    _write_live(loader, "deploy-helper", version=1)
    _stage_candidate(loader, notify=False)
    pend = loader._pending_root() / "deploy-helper-update"
    (pend / "scripts").mkdir()
    (pend / "scripts" / "sneaky.py").write_text("print('hi')\n", encoding="utf-8")

    assert loader.auto_apply_pending_update("deploy-helper-update") is None
    assert _pending_slugs(loader) == ["deploy-helper-update"]
    assert loader.get_auto_skill_version("auto/deploy-helper") == 1


def test_loader_auto_apply_toctou_scripts_injected_mid_promotion(loader, monkeypatch):
    """TOCTOU closure: scripts injected AFTER the entry precondition but before
    the copy step must abort the promotion (live SKILL.md rolled back, snapshot
    removed, candidate left pending) — never ship an unreviewed script live."""
    _write_live(loader, "deploy-helper", version=1)
    applied_seen: list[dict] = []
    S.set_update_auto_applied_hook(applied_seen.append)
    _stage_candidate(loader, notify=False)
    pend = loader._pending_root() / "deploy-helper-update"
    live_before = (
        loader._dir / "auto" / "deploy-helper" / "SKILL.md"
    ).read_text(encoding="utf-8")

    # Simulate the concurrent writer deterministically: inject scripts/ as a
    # side effect of the redaction step, which runs after the entry
    # precondition and before the copy step.
    real_redact = loader._validate_and_redact_candidate

    def _inject_then_redact(src, target_name):
        (pend / "scripts").mkdir(exist_ok=True)
        (pend / "scripts" / "sneaky.py").write_text("print('hi')\n", encoding="utf-8")
        return real_redact(src, target_name)

    monkeypatch.setattr(loader, "_validate_and_redact_candidate", _inject_then_redact)

    assert loader.auto_apply_pending_update("deploy-helper-update") is None

    # Live rolled back byte-identical, no version bump, no snapshot left over.
    live_after = (
        loader._dir / "auto" / "deploy-helper" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert live_after == live_before
    assert loader.get_auto_skill_version("auto/deploy-helper") == 1
    versions_dir = loader._dir / "auto" / "deploy-helper" / ".versions"
    assert not versions_dir.is_dir() or not any(versions_dir.iterdir())
    # Candidate still pending, injected script NOT live, no notification.
    assert _pending_slugs(loader) == ["deploy-helper-update"]
    assert not (loader._dir / "auto" / "deploy-helper" / "scripts" / "sneaky.py").exists()
    assert applied_seen == []


def test_loader_auto_apply_stale_base_refused_and_stays_pending(loader):
    """The staleness guard is inherited from approve_pending_update: a
    candidate merged against an older live version is refused, not applied."""
    _write_live(loader, "deploy-helper", version=2)  # live moved past base 1
    applied_seen: list[dict] = []
    S.set_update_auto_applied_hook(applied_seen.append)
    _stage_candidate(loader, base_version=1, notify=False)

    assert loader.auto_apply_pending_update("deploy-helper-update") is None
    assert _pending_slugs(loader) == ["deploy-helper-update"]
    assert loader.get_auto_skill_version("auto/deploy-helper") == 2
    assert applied_seen == []


# ── notify=False staging + emit_pending_staged re-fire ──


def test_stage_notify_false_suppresses_review_notification(loader):
    _write_live(loader, "deploy-helper", version=1)
    staged_seen: list[dict] = []
    S.set_pending_staged_hook(staged_seen.append)
    _stage_candidate(loader, notify=False)
    assert staged_seen == []


def test_emit_pending_staged_refires_from_meta(loader):
    _write_live(loader, "deploy-helper", version=1)
    staged_seen: list[dict] = []
    S.set_pending_staged_hook(staged_seen.append)
    _stage_candidate(loader, notify=False)

    loader.emit_pending_staged("deploy-helper-update")

    assert len(staged_seen) == 1
    payload = staged_seen[0]
    assert payload["name"] == "auto/deploy-helper-update"
    assert payload["slug"] == "deploy-helper-update"
    assert payload["kind"] == "update"
    assert payload["target"] == "auto/deploy-helper"
    assert payload["has_scripts"] is False
    assert payload["description"] == "updated"


def test_emit_pending_staged_noop_for_missing_candidate(loader):
    staged_seen: list[dict] = []
    S.set_pending_staged_hook(staged_seen.append)
    loader.emit_pending_staged("never-staged")
    assert staged_seen == []


# ── per-target promotion lock (cross-process serialization) ──


def _stage_named(loader, slug, *, procedure_md=_NEW_STEPS, base_version=1):
    return loader.stage_skill_candidate(
        slug,
        description="updated",
        triggers="t",
        procedure_md=procedure_md,
        provenance=_prov(),
        scripts=None,
        kind="update",
        target="auto/deploy-helper",
        base_version=base_version,
        notify=False,
    )


needs_flock = pytest.mark.skipif(
    not flock_compat.HAVE_FCNTL, reason="real flock required (no-op on Windows)"
)


@needs_flock
def test_concurrent_same_target_promotions_serialize(loader, monkeypatch):
    """Two promoters targeting the same live skill from the same base must
    serialize under the per-target lock: the first wins, the second re-reads
    the live version under the lock and is refused as stale — never a silent
    last-write-wins with both candidates consumed."""
    _write_live(loader, "deploy-helper", version=1)
    loader_b = SkillsLoader(skills_path=loader._dir, install_builtins=False)
    assert _stage_named(loader, "upd-a") == "auto/upd-a"
    assert _stage_named(loader_b, "upd-b", procedure_md="## Steps\n\n1. the B way\n") == (
        "auto/upd-b"
    )
    assert sorted(_pending_slugs(loader)) == ["upd-a", "upd-b"]

    a_in_lock = threading.Event()
    release_a = threading.Event()
    real_redact = loader._validate_and_redact_candidate

    def _hold_inside_lock(src, target_name):
        # Runs INSIDE promoter A's critical section: park here so promoter B
        # demonstrably contends on the lock while A is mid-promotion.
        a_in_lock.set()
        assert release_a.wait(timeout=10)
        return real_redact(src, target_name)

    monkeypatch.setattr(loader, "_validate_and_redact_candidate", _hold_inside_lock)

    results: dict = {}
    t_a = threading.Thread(
        target=lambda: results.__setitem__("a", loader.approve_pending_update("upd-a"))
    )
    t_b = threading.Thread(
        target=lambda: results.__setitem__("b", loader_b.approve_pending_update("upd-b"))
    )
    t_a.start()
    assert a_in_lock.wait(timeout=10)
    t_b.start()
    # B is now polling the held lock (poll interval is 0.05s).
    time.sleep(0.4)
    assert "b" not in results  # B must not have promoted while A holds the lock
    release_a.set()
    t_a.join(timeout=30)
    t_b.join(timeout=30)

    assert results["a"] == "auto/deploy-helper"
    assert results["b"] is None  # stale base under the lock -> refused
    assert loader.get_auto_skill_version("auto/deploy-helper") == 2  # ONE bump
    live_body = (loader._dir / "auto" / "deploy-helper" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "the new way" in live_body and "the B way" not in live_body
    # Loser's candidate survives for review; winner's was consumed.
    assert _pending_slugs(loader) == ["upd-b"]


def test_stage_token_mismatch_inside_lock_refuses_promotion(loader):
    """A candidate swapped between the caller's pre-lock ownership check and
    the locked promotion (concurrent dismiss + same-slug re-stage) must be
    refused INSIDE the lock: the pending meta's ``stage_token`` no longer
    matches the staging flow's token, so the unreviewed replacement stays
    pending instead of going live."""
    import secrets

    _write_live(loader, "deploy-helper", version=1)
    # Production-shaped tokens (secrets.token_hex(16)): pins that the pending
    # meta redaction pass does NOT scrub the token, which would make every
    # unattended promotion self-refuse.
    tok_original = secrets.token_hex(16)
    tok_impostor = secrets.token_hex(16)
    assert (
        loader.stage_skill_candidate(
            "upd-swap",
            description="updated",
            triggers="t",
            procedure_md=_NEW_STEPS,
            provenance=_prov(),
            scripts=None,
            kind="update",
            target="auto/deploy-helper",
            base_version=1,
            notify=False,
            stage_token=tok_original,
        )
        == "auto/upd-swap"
    )
    # Simulate the swap: the original candidate is dismissed and a different
    # one is re-staged under the SAME slug with its own token.
    loader.dismiss_pending_skill("upd-swap")
    assert (
        loader.stage_skill_candidate(
            "upd-swap",
            description="updated",
            triggers="t",
            procedure_md="## Steps\n\n1. the impostor way\n",
            provenance=_prov(),
            scripts=None,
            kind="update",
            target="auto/deploy-helper",
            base_version=1,
            notify=False,
            stage_token=tok_impostor,
        )
        == "auto/upd-swap"
    )
    # The staging flow still holds the ORIGINAL token — promotion must refuse.
    assert (
        loader.approve_pending_update(
            "upd-swap", refuse_scripts=True, expected_stage_token=tok_original
        )
        is None
    )
    assert loader.get_auto_skill_version("auto/deploy-helper") == 1  # untouched
    assert _pending_slugs(loader) == ["upd-swap"]  # impostor stays reviewable
    # Matching token (the impostor's own flow) still promotes normally.
    assert (
        loader.approve_pending_update(
            "upd-swap", refuse_scripts=True, expected_stage_token=tok_impostor
        )
        == "auto/deploy-helper"
    )
    assert loader.get_auto_skill_version("auto/deploy-helper") == 2


@needs_flock
def test_script_injection_after_lock_acquisition_refused(loader, monkeypatch):
    """A scripts/ dir injected AFTER the promotion lock is acquired (and after
    the entry precondition) must still be refused: the re-check runs inside
    the lock, and under ``refuse_scripts`` the copy path is unreachable."""
    _write_live(loader, "deploy-helper", version=1)
    _stage_candidate(loader, notify=False)
    pend = loader._pending_root() / "deploy-helper-update"
    lock_states: list[str] = []
    real_redact = loader._validate_and_redact_candidate

    def _probe_then_inject(src, target_name):
        # Prove the per-target lock is already held at this point: a
        # non-blocking flock on a second fd must fail with EWOULDBLOCK.
        lock_path = loader._locks_root() / "deploy-helper.lock"
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                flock_compat.flock(fd, flock_compat.LOCK_EX | flock_compat.LOCK_NB)
                lock_states.append("unlocked")
                flock_compat.flock(fd, flock_compat.LOCK_UN)
            except OSError:
                lock_states.append("locked")
        finally:
            os.close(fd)
        (pend / "scripts").mkdir(exist_ok=True)
        (pend / "scripts" / "sneaky.py").write_text("print('hi')\n", encoding="utf-8")
        return real_redact(src, target_name)

    monkeypatch.setattr(loader, "_validate_and_redact_candidate", _probe_then_inject)

    assert loader.auto_apply_pending_update("deploy-helper-update") is None

    assert lock_states == ["locked"]
    assert loader.get_auto_skill_version("auto/deploy-helper") == 1
    assert not (loader._dir / "auto" / "deploy-helper" / "scripts" / "sneaky.py").exists()
    assert _pending_slugs(loader) == ["deploy-helper-update"]


@needs_flock
def test_promotion_refused_while_lock_held_elsewhere(loader, monkeypatch):
    """A held per-target lock (e.g. a promotion in another process) makes a
    contending promotion FAIL SAFE after the bounded poll: refused, candidate
    left pending, live untouched. A crashed holder cannot cause this state to
    persist — flock is released by the kernel on process death."""
    _write_live(loader, "deploy-helper", version=1)
    _stage_candidate(loader, notify=False)
    monkeypatch.setattr(S, "_PROMOTE_LOCK_TIMEOUT_S", 0.2)
    lock_path = loader._locks_root() / "deploy-helper.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    flock_compat.flock(fd, flock_compat.LOCK_EX)
    try:
        assert loader.approve_pending_update("deploy-helper-update") is None
    finally:
        flock_compat.flock(fd, flock_compat.LOCK_UN)
        os.close(fd)
    assert _pending_slugs(loader) == ["deploy-helper-update"]
    assert loader.get_auto_skill_version("auto/deploy-helper") == 1


# ── pending-lookup failure re-fires the suppressed staging notification ──


def test_pending_lookup_failure_refires_staged_notification(loader, monkeypatch):
    """A transient ``get_pending_skill`` failure during the ownership check
    must not leave the just-staged candidate invisible: the review request
    that staging suppressed (notify=False) is re-fired, and the refusal is
    audited as ``pending_lookup_failed`` (distinct from ownership_mismatch)."""
    _write_live(loader, "deploy-helper", version=1)
    staged_seen: list[dict] = []
    S.set_pending_staged_hook(staged_seen.append)
    c = _mk(loader, approval_required=False)

    def _boom(slug):
        raise RuntimeError("transient pending-store failure")

    monkeypatch.setattr(loader, "get_pending_skill", _boom)
    recorded: list[dict] = []
    ctx = _sel_recorder(recorded)
    try:
        _stage_update(c)
    finally:
        ctx.stop()

    # Candidate still pending, live untouched.
    pending = _pending_slugs(loader)
    assert len(pending) == 1
    assert loader.get_auto_skill_version("auto/deploy-helper") == 1
    # The suppressed staging notification fired exactly once, for our slug.
    assert [p["slug"] for p in staged_seen] == pending
    # Audited with the lookup-failure reason.
    reasons = [
        r["metadata"].get("reason")
        for r in recorded
        if r.get("outcome") == "auto_apply_failed"
    ]
    assert reasons == ["pending_lookup_failed"]
