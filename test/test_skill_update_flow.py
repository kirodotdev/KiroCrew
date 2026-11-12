"""Part C tests: pending skill-update flow.

Covers the tri-state dedupe verdict mapping in ``_dedupe_candidate``, the
``_stage_skill_update`` staging path (merge + fallback + metadata), and the
``_process_auto_skills`` routing (update stages, dup rejects, new stages).

All tests drive a FAKE loader + monkeypatched LLM so they do NOT depend on part
B landing the ``kind``/``target``/``base_version`` support in ``skills.py`` — the
fake ``stage_skill_candidate`` simply accepts and records those keyword args.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import history as H
from kiro_crew.history import HistoryConsolidator

_LIVE_BODY = "## When to use\nold\n## Steps\n1. old\n## Gotchas\nnone\n"


class FakeLoader:
    """Minimal SkillsLoader stand-in capturing stage_skill_candidate calls."""

    def __init__(self, *, existing=None, live_body=_LIVE_BODY, version=3, find=None):
        self._existing = existing or [
            {"key": "auto/deploy-helper", "description": "deploy the service", "triggers": "deploy"}
        ]
        self._live_body = live_body
        self._version = version
        self._find = find
        self.staged: list[dict] = []

    # dedupe inputs
    def list_auto_skills(self):
        return list(self._existing)

    def list_pending_skills(self):
        return []

    def find_similar(self, description, threshold=0.85):
        return self._find

    # update inputs
    def read_auto_skill_body(self, key):
        return self._live_body

    def get_auto_skill_version(self, key):
        return self._version

    def stage_skill_candidate(
        self,
        slug,
        *,
        description,
        triggers,
        procedure_md,
        provenance,
        kind="new",
        target=None,
        base_version=None,
        scripts=None,
        source="consolidation",
        refusal=None,
        missing_scripts=None,
        rejected_scripts=None,
    ):
        self.staged.append(
            {
                "slug": slug,
                "description": description,
                "triggers": triggers,
                "procedure_md": procedure_md,
                "kind": kind,
                "target": target,
                "base_version": base_version,
                "scripts": scripts,
                "missing_scripts": missing_scripts,
                "rejected_scripts": rejected_scripts,
            }
        )
        return f"auto/{slug}"

    # new-path fallbacks (unused in these tests but referenced by the code)
    def create_auto_skill(self, *a, **k):
        return None

    def run_skill_lifecycle(self, *a, **k):
        return None


def _mk(loader, approval_required=True, **kw):
    return HistoryConsolidator(
        log=MagicMock(),
        memory=MagicMock(),
        skills_loader=loader,
        auto_skills_enabled=True,
        approval_required=approval_required,
        **kw,
    )


def _sel_recorder(recorded):
    ctx = patch("kiro_crew.history.sel")
    mock = ctx.start()
    mock.return_value.log_tool_invocation = lambda **k: recorded.append(k)
    return ctx


# ── verdict mapping ──


@pytest.mark.asyncio
async def test_dedupe_maps_update_verdict(monkeypatch):
    loader = FakeLoader()
    c = _mk(loader, judge_model="claude-haiku-4.5")
    c._event_loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        H,
        "metadata_dedupe_verdict",
        lambda cand, existing, judge: (H.VERDICT_UPDATE, "auto/deploy-helper"),
    )
    assert c._dedupe_candidate("deploy-helper-2", "d", "t") == (
        H.VERDICT_UPDATE,
        "auto/deploy-helper",
    )


@pytest.mark.asyncio
async def test_dedupe_maps_dup_verdict(monkeypatch):
    loader = FakeLoader()
    c = _mk(loader, judge_model="claude-haiku-4.5")
    c._event_loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        H,
        "metadata_dedupe_verdict",
        lambda *a: (H.VERDICT_DUP, "auto/deploy-helper"),
    )
    assert c._dedupe_candidate("x", "d", "t") == (H.VERDICT_DUP, "auto/deploy-helper")


@pytest.mark.asyncio
async def test_dedupe_new_verdict_falls_back_to_lexical_hit(monkeypatch):
    loader = FakeLoader(find="auto/deploy-helper")
    c = _mk(loader, judge_model="claude-haiku-4.5")
    c._event_loop = asyncio.get_running_loop()
    monkeypatch.setattr(H, "metadata_dedupe_verdict", lambda *a: (H.VERDICT_NEW, None))
    # judge says new, but lexical safety-net finds a near-duplicate → DUP.
    assert c._dedupe_candidate("x", "d", "t") == (H.VERDICT_DUP, "auto/deploy-helper")


@pytest.mark.asyncio
async def test_dedupe_new_verdict_lexical_miss_is_new(monkeypatch):
    loader = FakeLoader(find=None)
    c = _mk(loader, judge_model="claude-haiku-4.5")
    c._event_loop = asyncio.get_running_loop()
    monkeypatch.setattr(H, "metadata_dedupe_verdict", lambda *a: (H.VERDICT_NEW, None))
    assert c._dedupe_candidate("x", "d", "t") == (H.VERDICT_NEW, None)


def test_dedupe_no_judge_uses_lexical_only():
    hit = FakeLoader(find="auto/deploy-helper")
    c_hit = _mk(hit, judge_model="")
    assert c_hit._dedupe_candidate("x", "d", "t") == (H.VERDICT_DUP, "auto/deploy-helper")

    miss = FakeLoader(find=None)
    c_miss = _mk(miss, judge_model="")
    assert c_miss._dedupe_candidate("x", "d", "t") == (H.VERDICT_NEW, None)


# ── _stage_skill_update ──


@pytest.mark.asyncio
async def test_stage_update_uses_merged_body():
    loader = FakeLoader(version=7)
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()

    merged = "## When to use\nMERGED\n## Steps\n1. new\n## Gotchas\nz\n"

    async def fake_merge(live, d, t, p):
        return merged

    c._merge_skill_update = fake_merge  # type: ignore[assignment]
    recorded: list[dict] = []
    ctx = _sel_recorder(recorded)
    try:
        await asyncio.to_thread(
            c._stage_skill_update,
            key="sess",
            target_key="auto/deploy-helper",
            description="new desc",
            triggers="new",
            procedure_md="## Steps\n1. cand\n",
        )
    finally:
        ctx.stop()

    assert len(loader.staged) == 1
    st = loader.staged[0]
    assert re.fullmatch(r"deploy-helper-update-[0-9a-f]{8}", st["slug"])
    assert st["kind"] == "update"
    assert st["target"] == "auto/deploy-helper"
    assert st["base_version"] == 7
    # The merge boundary sanitizer strips surrounding whitespace (the SKILL.md
    # builder strips the body anyway), so compare against the stripped form.
    assert st["procedure_md"] == merged.strip()
    ev = [r for r in recorded if r.get("outcome") == "staged_update"]
    assert ev and ev[0]["metadata"] == {
        "name": f"auto/{st['slug']}",
        "target": "auto/deploy-helper",
        "base_version": 7,
        "merged": True,
    }


@pytest.mark.asyncio
async def test_stage_update_merge_failure_falls_back_to_candidate():
    loader = FakeLoader()
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()

    async def fail_merge(*a):
        return None

    c._merge_skill_update = fail_merge  # type: ignore[assignment]
    recorded: list[dict] = []
    ctx = _sel_recorder(recorded)
    try:
        await asyncio.to_thread(
            c._stage_skill_update,
            key="sess",
            target_key="auto/deploy-helper",
            description="d",
            triggers="t",
            procedure_md="## Steps\n1. cand\n",
        )
    finally:
        ctx.stop()

    st = loader.staged[0]
    assert st["procedure_md"] == "## Steps\n1. cand\n"
    ev = [r for r in recorded if r.get("outcome") == "staged_update"]
    assert ev and ev[0]["metadata"]["merged"] is False


@pytest.mark.asyncio
async def test_stage_update_oversize_merge_falls_back():
    loader = FakeLoader()
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()

    async def big_merge(*a):
        return "x" * (H.AUTO_SKILL_MAX_PROCEDURE_CHARS + 10)

    c._merge_skill_update = big_merge  # type: ignore[assignment]
    recorded: list[dict] = []
    ctx = _sel_recorder(recorded)
    try:
        await asyncio.to_thread(
            c._stage_skill_update,
            key="sess",
            target_key="auto/deploy-helper",
            description="d",
            triggers="t",
            procedure_md="## Steps\n1. cand\n",
        )
    finally:
        ctx.stop()

    st = loader.staged[0]
    assert st["procedure_md"] == "## Steps\n1. cand\n"
    ev = [r for r in recorded if r.get("outcome") == "staged_update"]
    assert ev and ev[0]["metadata"]["merged"] is False


def test_stage_update_no_loop_skips_merge():
    """With no captured event loop, the merge is skipped and the candidate body
    is used (merged=False) — no crash."""
    loader = FakeLoader(version=2)
    c = _mk(loader)
    c._event_loop = None
    recorded: list[dict] = []
    ctx = _sel_recorder(recorded)
    try:
        c._stage_skill_update(
            key="sess",
            target_key="auto/deploy-helper",
            description="d",
            triggers="t",
            procedure_md="## Steps\n1. cand\n",
        )
    finally:
        ctx.stop()
    st = loader.staged[0]
    assert st["kind"] == "update"
    assert st["base_version"] == 2
    assert st["procedure_md"] == "## Steps\n1. cand\n"
    ev = [r for r in recorded if r.get("outcome") == "staged_update"]
    assert ev and ev[0]["metadata"]["merged"] is False


# ── _process_auto_skills routing ──


# An update to a live skill stages even with approval off: the toggle governs
# new candidates only, as its help text and the Skills doc say.
@pytest.mark.parametrize("approval_required", [True, False])
def test_process_routes_update(monkeypatch, approval_required):
    loader = FakeLoader()
    c = _mk(loader, approval_required=approval_required)
    monkeypatch.setattr(
        c, "_dedupe_candidate", lambda s, d, t: (H.VERDICT_UPDATE, "auto/deploy-helper")
    )
    calls: list[dict] = []
    monkeypatch.setattr(c, "_stage_skill_update", lambda **k: calls.append(k))
    recorded: list[dict] = []
    ctx = _sel_recorder(recorded)
    try:
        c._process_auto_skills(
            {
                "new_skill": {
                    "slug": "deploy-helper-2",
                    "description": "d",
                    "triggers": "t",
                    "procedure_md": "## Steps\n1. go\n",
                }
            },
            "sess",
        )
    finally:
        ctx.stop()
    assert len(calls) == 1
    assert calls[0]["target_key"] == "auto/deploy-helper"
    assert calls[0]["procedure_md"] == "## Steps\n1. go\n"
    # An update does NOT go through the new-candidate staging branch.
    assert loader.staged == []


def test_process_dup_still_rejects(monkeypatch):
    loader = FakeLoader()
    c = _mk(loader)
    monkeypatch.setattr(
        c, "_dedupe_candidate", lambda s, d, t: (H.VERDICT_DUP, "auto/deploy-helper")
    )
    staged_update: list = []
    monkeypatch.setattr(c, "_stage_skill_update", lambda **k: staged_update.append(k))
    recorded: list[dict] = []
    ctx = _sel_recorder(recorded)
    try:
        c._process_auto_skills(
            {
                "new_skill": {
                    "slug": "dup-skill",
                    "description": "d",
                    "triggers": "t",
                    "procedure_md": "## Steps\n1. go\n",
                }
            },
            "sess",
        )
    finally:
        ctx.stop()
    assert staged_update == []
    assert loader.staged == []
    rej = [
        r
        for r in recorded
        if r.get("outcome") == "rejected"
        and r.get("metadata", {}).get("reason") == "similar_exists"
    ]
    assert rej and rej[0]["metadata"]["existing"] == "auto/deploy-helper"


def test_process_no_candidate_emits_skipped_audit(monkeypatch):
    """When the model returns no new_skill, emit a 'skipped' audit event.

    Regression test for the observability gap: an eligible session that ran the
    skill-gen prompt but got no candidate would leave NO SEL event, making
    'asked, model declined' indistinguishable from 'never asked' in the audit
    log. The else-branch in _process_auto_skills now records it.
    """
    loader = FakeLoader()
    c = _mk(loader)
    # _dedupe_candidate must never be consulted — there is no candidate.
    monkeypatch.setattr(
        c,
        "_dedupe_candidate",
        lambda *a, **k: pytest.fail("dedupe called for a null candidate"),
    )
    recorded: list[dict] = []
    ctx = _sel_recorder(recorded)
    try:
        # result carries a history_entry but new_skill is absent (model declined).
        c._process_auto_skills({"history_entry": "did some work"}, "sess")
    finally:
        ctx.stop()
    assert loader.staged == []
    skipped = [
        r
        for r in recorded
        if r.get("tool_name") == "auto_skill_create"
        and r.get("outcome") == "skipped"
        and r.get("metadata", {}).get("reason") == "no_candidate_proposed"
    ]
    assert len(skipped) == 1
    assert skipped[0]["session_key"] == "sess"


def test_process_null_new_skill_emits_skipped_audit(monkeypatch):
    """A literal ``new_skill: null`` is treated the same as an absent key."""
    loader = FakeLoader()
    c = _mk(loader)
    recorded: list[dict] = []
    ctx = _sel_recorder(recorded)
    try:
        c._process_auto_skills({"new_skill": None}, "sess")
    finally:
        ctx.stop()
    assert loader.staged == []
    assert any(
        r.get("outcome") == "skipped"
        and r.get("metadata", {}).get("reason") == "no_candidate_proposed"
        for r in recorded
    )


def test_process_new_stages_as_new(monkeypatch):
    loader = FakeLoader()
    c = _mk(loader)  # approval_required=True → stages
    monkeypatch.setattr(c, "_dedupe_candidate", lambda s, d, t: (H.VERDICT_NEW, None))
    recorded: list[dict] = []
    ctx = _sel_recorder(recorded)
    try:
        c._process_auto_skills(
            {
                "new_skill": {
                    "slug": "brand-new",
                    "description": "d",
                    "triggers": "t",
                    "procedure_md": "## Steps\n1. go\n",
                }
            },
            "sess",
        )
    finally:
        ctx.stop()
    assert len(loader.staged) == 1
    assert loader.staged[0]["slug"] == "brand-new"
    assert loader.staged[0]["kind"] == "new"
    staged = [r for r in recorded if r.get("outcome") == "staged"]
    assert staged


# ── Frontmatter / fence hygiene on the merge boundary ──────────────────────


def test_strip_skill_frontmatter_and_fence_helpers():
    """The merge boundary must never carry frontmatter or fences."""
    full = "---\nname: auto/x\nversion: 2\n---\n\n## Steps\n1. go\n"
    assert H._strip_skill_frontmatter(full) == "## Steps\n1. go"
    # No frontmatter → unchanged (stripped).
    assert H._strip_skill_frontmatter("## Steps\n1. go\n") == "## Steps\n1. go"
    assert H._strip_skill_frontmatter(None) == ""
    assert H._strip_code_fence("```markdown\n## Steps\n1. go\n```") == "## Steps\n1. go"
    assert H._strip_code_fence("## Steps\n1. go") == "## Steps\n1. go"


@pytest.mark.asyncio
async def test_merge_receives_prose_only_not_frontmatter():
    """The live SKILL.md header must be stripped before the merge turn, else the
    model echoes it back and a second ``---`` block nests in the procedure."""
    loader = FakeLoader(live_body="---\nname: auto/deploy-helper\nversion: 4\n---\n\n" + _LIVE_BODY)
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()
    seen: dict = {}

    async def capture_merge(live, d, t, p):
        seen["live"] = live
        return "## When to use\nM\n## Steps\n1. m\n## Gotchas\nz\n"

    c._merge_skill_update = capture_merge  # type: ignore[assignment]
    ctx = _sel_recorder([])
    try:
        await asyncio.to_thread(
            c._stage_skill_update,
            key="sess",
            target_key="auto/deploy-helper",
            description="d",
            triggers="t",
            procedure_md="## Steps\n1. cand\n",
        )
    finally:
        ctx.stop()

    assert "---" not in seen["live"]
    assert "name: auto/deploy-helper" not in seen["live"]
    assert seen["live"].startswith("## When to use")


@pytest.mark.asyncio
async def test_staged_update_body_is_sanitized():
    """A model reply wrapped in a fence AND carrying frontmatter is sanitized to
    pure prose before staging."""
    loader = FakeLoader()
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()

    async def dirty_merge(*a):
        return (
            "```markdown\n---\nname: auto/deploy-helper\nversion: 9\n---\n\n"
            "## When to use\nCLEAN\n## Steps\n1. m\n## Gotchas\nz\n```"
        )

    c._merge_skill_update = dirty_merge  # type: ignore[assignment]
    ctx = _sel_recorder([])
    try:
        await asyncio.to_thread(
            c._stage_skill_update,
            key="sess",
            target_key="auto/deploy-helper",
            description="d",
            triggers="t",
            procedure_md="## Steps\n1. cand\n",
        )
    finally:
        ctx.stop()

    body = loader.staged[0]["procedure_md"]
    assert body.startswith("## When to use")
    assert "---" not in body
    assert "```" not in body
    assert "version: 9" not in body


# ── Regression: validated scripts must survive the UPDATE route ──────────────


@pytest.mark.asyncio
async def test_stage_update_carries_validated_scripts():
    """A candidate that generated a valid script and got an UPDATE verdict must
    keep the script. Dropping it here loses the helper permanently, because
    consolidation advances its message offset regardless of candidate outcome."""
    loader = FakeLoader()
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()

    async def fake_merge(*a):
        return "## When to use\nM\n## Steps\n1. m\n## Gotchas\nz\n"

    c._merge_skill_update = fake_merge  # type: ignore[assignment]
    scripts = [{"filename": "go.py", "content": "print('hi')\n"}]
    ctx = _sel_recorder([])
    try:
        await asyncio.to_thread(
            c._stage_skill_update,
            key="sess",
            target_key="auto/deploy-helper",
            description="d",
            triggers="t",
            procedure_md="## Steps\n1. cand\n",
            scripts=scripts,
        )
    finally:
        ctx.stop()

    assert loader.staged[0]["scripts"] == scripts


@pytest.mark.asyncio
async def test_process_auto_skills_update_route_forwards_scripts():
    """The _process_auto_skills UPDATE branch must forward valid_scripts."""
    loader = FakeLoader()
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()
    c._generate_scripts = True
    monkey: dict = {}

    def capture(**kw):
        monkey.update(kw)

    c._dedupe_candidate = lambda s, d, t: (H.VERDICT_UPDATE, "auto/deploy-helper")  # type: ignore[assignment]
    c._stage_skill_update = capture  # type: ignore[assignment]
    result = {
        "new_skill": {
            "slug": "deploy-helper-2",
            "description": "d",
            "triggers": "t",
            "procedure_md": "## Steps\n1. x",
            "scripts": [{"filename": "go.py", "content": "print('hi')\n"}],
        }
    }
    ctx = _sel_recorder([])
    try:
        await asyncio.to_thread(c._process_auto_skills, result, "sess")
    finally:
        ctx.stop()

    assert monkey.get("scripts") == [{"filename": "go.py", "content": "print('hi')\n"}]


@pytest.mark.asyncio
async def test_process_auto_skills_update_route_forwards_rejected_script_findings():
    """A script the validator refused reaches the UPDATE candidate with its findings."""
    loader = FakeLoader()
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()
    c._generate_scripts = True
    monkey: dict = {}
    c._dedupe_candidate = lambda s, d, t: (H.VERDICT_UPDATE, "auto/deploy-helper")  # type: ignore[assignment]
    c._stage_skill_update = lambda **kw: monkey.update(kw)  # type: ignore[assignment]
    result = {
        "new_skill": {
            "slug": "deploy-helper-2",
            "description": "d",
            "triggers": "t",
            "procedure_md": "## Steps\n1. python3 scripts/wipe.py",
            "scripts": [{"filename": "wipe.py", "content": "import os\nos.system('rm -rf /')\n"}],
        }
    }
    ctx = _sel_recorder([])
    try:
        await asyncio.to_thread(c._process_auto_skills, result, "sess")
    finally:
        ctx.stop()

    assert monkey.get("scripts") is None
    assert list(monkey.get("rejected_scripts") or {}) == ["wipe.py"]


@pytest.mark.asyncio
async def test_stage_update_skips_non_live_target():
    """The judge sees PENDING candidates in `existing`, so it can answer
    `UPDATE auto/<pending-slug>` — a target that is not live. Staging that would
    queue a candidate approve_pending_update_checked rejects forever, so drop it."""
    loader = FakeLoader(live_body=None)
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()
    recorded: list[dict] = []
    ctx = _sel_recorder(recorded)
    try:
        await asyncio.to_thread(
            c._stage_skill_update,
            key="sess",
            target_key="auto/not-live",
            description="d",
            triggers="t",
            procedure_md="## Steps\n1. cand\n",
        )
    finally:
        ctx.stop()

    assert loader.staged == []
    rej = [r for r in recorded if r.get("outcome") == "rejected"]
    assert rej and rej[0]["metadata"]["reason"] == "target_not_live"


@pytest.mark.asyncio
async def test_stage_update_truncates_long_target_slug():
    """A 60-char target slug is permitted by the generation prompt, but
    `<slug>-update` would exceed the 64-char slug cap and staging would REJECT it
    — silently dropping the learning, since consolidation advances its offset
    regardless. The slug must be truncated so the suffix fits."""
    from kiro_crew.skills import _AUTO_NAME_PATTERN

    long_slug = "d" * 60
    assert _AUTO_NAME_PATTERN.match(long_slug)  # the target itself is legal
    assert not _AUTO_NAME_PATTERN.match(f"{long_slug}-update")  # naive suffix is not

    loader = FakeLoader()
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()

    async def fake_merge(*a):
        return "## When to use\nM\n## Steps\n1. m\n## Gotchas\nz\n"

    c._merge_skill_update = fake_merge  # type: ignore[assignment]
    ctx = _sel_recorder([])
    try:
        await asyncio.to_thread(
            c._stage_skill_update,
            key="sess",
            target_key=f"auto/{long_slug}",
            description="d",
            triggers="t",
            procedure_md="## Steps\n1. cand\n",
        )
    finally:
        ctx.stop()

    assert len(loader.staged) == 1
    staged_slug = loader.staged[0]["slug"]
    assert re.search(r"-update-[0-9a-f]{8}$", staged_slug)
    # Legal on its own AND with a "-50" collision suffix appended.
    assert _AUTO_NAME_PATTERN.match(staged_slug)
    assert _AUTO_NAME_PATTERN.match(f"{staged_slug}-50")
    # The target is still recorded in full, so approval resolves correctly.
    assert loader.staged[0]["target"] == f"auto/{long_slug}"


@pytest.mark.asyncio
async def test_update_verdict_on_pending_target_stages_a_new_candidate():
    """The judge sees PENDING candidates too, so it can answer
    `UPDATE auto/<pending-slug>`. That target cannot be updated, but the
    requirement is new relative to the LIVE set and consolidation advances its
    offset regardless — so it must be staged as a NEW candidate, not dropped."""
    loader = FakeLoader(live_body=None)  # target is not live
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()
    c._dedupe_candidate = lambda s, d, t: (H.VERDICT_UPDATE, "auto/still-pending")  # type: ignore[assignment]
    result = {
        "new_skill": {
            "slug": "fresh-learning",
            "description": "d",
            "triggers": "t",
            "procedure_md": "## Steps\n1. x",
        }
    }
    recorded: list[dict] = []
    ctx = _sel_recorder(recorded)
    try:
        await asyncio.to_thread(c._process_auto_skills, result, "sess")
    finally:
        ctx.stop()

    # Staged as a NEW candidate under its own slug — the learning survives.
    assert len(loader.staged) == 1
    st = loader.staged[0]
    assert st["slug"] == "fresh-learning"
    assert st.get("kind", "new") == "new"
    assert st.get("target") is None
    # And it was NOT audited as a dropped/rejected candidate.
    assert not [
        r
        for r in recorded
        if r.get("outcome") == "rejected"
        and r.get("metadata", {}).get("reason") in ("target_not_live", "similar_exists")
    ]


@pytest.mark.asyncio
async def test_update_verdict_on_live_target_still_stages_an_update():
    """The downgrade must only apply when the target is not live."""
    loader = FakeLoader()  # live_body present
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()
    c._dedupe_candidate = lambda s, d, t: (H.VERDICT_UPDATE, "auto/deploy-helper")  # type: ignore[assignment]

    async def fake_merge(*a):
        return "## When to use\nM\n## Steps\n1. m\n## Gotchas\nz\n"

    c._merge_skill_update = fake_merge  # type: ignore[assignment]
    result = {
        "new_skill": {
            "slug": "another-take",
            "description": "d",
            "triggers": "t",
            "procedure_md": "## Steps\n1. x",
        }
    }
    ctx = _sel_recorder([])
    try:
        await asyncio.to_thread(c._process_auto_skills, result, "sess")
    finally:
        ctx.stop()

    assert len(loader.staged) == 1
    assert loader.staged[0]["kind"] == "update"
    assert loader.staged[0]["target"] == "auto/deploy-helper"


@pytest.mark.asyncio
async def test_merge_prompt_never_receives_live_body_credentials():
    """A credential typed straight into a skill body via the dashboard editor
    lives legitimately in the skills tree, so no path guard catches it. It must
    be redacted BEFORE the merge prompt — redacting the merge output is too late
    to protect what was already sent to the model."""
    loader = FakeLoader(
        live_body=(
            "## When to use\nrun the deploy\n"
            "## Steps\n1. export AWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE\n"
            "2. aws s3 sync ./out s3://bucket\n"
        )
    )
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()
    seen: dict = {}

    async def capture_merge(live, d, t, p):
        seen["live"] = live
        return "## When to use\nM\n## Steps\n1. m\n## Gotchas\nz\n"

    c._merge_skill_update = capture_merge  # type: ignore[assignment]
    ctx = _sel_recorder([])
    try:
        await asyncio.to_thread(
            c._stage_skill_update,
            key="sess",
            target_key="auto/deploy-helper",
            description="d",
            triggers="t",
            procedure_md="## Steps\n1. cand\n",
        )
    finally:
        ctx.stop()

    # The credential-shaped token must not appear in what the model received.
    assert "AKIAIOSFODNN7EXAMPLE" not in seen["live"]
    # The surrounding prose is still intact, so the merge stays useful.
    assert "run the deploy" in seen["live"]
    assert "aws s3 sync" in seen["live"]


def test_merge_trigger_lists_unions_dedupes_and_caps():
    assert H._merge_trigger_lists("deploy failed, retry", "rollback") == (
        "deploy failed, retry, rollback"
    )
    # Case-insensitive dedupe, live order preserved.
    assert H._merge_trigger_lists("Deploy Failed", "deploy failed, rollback") == (
        "Deploy Failed, rollback"
    )
    # Whitespace normalised, empties dropped.
    assert H._merge_trigger_lists("a ,, b", "  c\n") == "a, b, c"
    # Capped so repeated updates can't grow the list without bound.
    many = ", ".join(f"t{i}" for i in range(20))
    assert len(H._merge_trigger_lists(many, "extra").split(", ")) == 12
    # Either side empty is fine.
    assert H._merge_trigger_lists("", "only") == "only"
    assert H._merge_trigger_lists("only", "") == "only"


def test_frontmatter_value_reads_fields_and_tolerates_missing():
    body = "---\nname: auto/x\ndescription: Retry a deploy\ntriggers: a, b\n---\n\n## Steps\n"
    assert H._frontmatter_value(body, "description") == "Retry a deploy"
    assert H._frontmatter_value(body, "triggers") == "a, b"
    assert H._frontmatter_value(body, "nope") == ""
    assert H._frontmatter_value("no frontmatter here", "description") == ""
    assert H._frontmatter_value(None, "description") == ""


def test_frontmatter_value_resolves_block_scalars():
    # A live skill authored with block-scalar frontmatter must round-trip
    # through the update path: the staged candidate overwrites the live skill
    # on approval, so reading the indicator verbatim would collapse the
    # description to ">" (resolved to "" by the loader) and inject a bogus
    # ">" entry into the merged trigger list.
    body = (
        "---\n"
        "name: auto/x\n"
        "description: >\n"
        "  Retry a deploy\n"
        "  after checking the logs.\n"
        "triggers: |-\n"
        "  a, b\n"
        "---\n\n## Steps\n"
    )
    assert H._frontmatter_value(body, "description") == "Retry a deploy after checking the logs.\n"
    # `triggers` uses `|-`, whose strip chomping drops the trailing break, so it is
    # unaffected by the strip chomping -- the pair keeps the two modes visibly distinct here.
    assert H._frontmatter_value(body, "triggers") == "a, b"
    # An empty block resolves to "" rather than the indicator character.
    assert H._frontmatter_value("---\ndescription: >\nname: x\n---\nbody", "description") == ""
    # An indented occurrence of the key is prose inside a block, not a field.
    nested = "---\ndescription: >\n  triggers: not real\ntriggers: real\n---\nbody"
    assert H._frontmatter_value(nested, "triggers") == "real"


@pytest.mark.asyncio
async def test_stage_update_merges_live_triggers_not_replaces_them():
    """Approval writes the candidate's frontmatter over live, so the candidate
    must carry MERGED triggers. Otherwise an update that adds a rollback step
    narrows the activation surface and the skill stops firing on the phrasings it
    already answered."""
    loader = FakeLoader(
        live_body=(
            "---\nname: auto/deploy-helper\n"
            "description: Retry a failed deployment\n"
            "triggers: deploy failed, retry deployment, pipeline error\n"
            "---\n\n## Steps\n1. old\n"
        )
    )
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()

    async def fake_merge(*a):
        return "## When to use\nM\n## Steps\n1. m\n## Gotchas\nz\n"

    c._merge_skill_update = fake_merge  # type: ignore[assignment]
    ctx = _sel_recorder([])
    try:
        await asyncio.to_thread(
            c._stage_skill_update,
            key="sess",
            target_key="auto/deploy-helper",
            description="Retry a failed deployment, rolling back if needed",
            triggers="rollback",
            procedure_md="## Steps\n1. cand\n",
        )
    finally:
        ctx.stop()

    staged = loader.staged[0]["triggers"]
    # Every live trigger survives...
    for t in ("deploy failed", "retry deployment", "pipeline error"):
        assert t in staged
    # ...and the new one is added.
    assert "rollback" in staged


@pytest.mark.asyncio
async def test_stage_update_falls_back_to_live_description_when_candidate_empty():
    loader = FakeLoader(
        live_body=(
            "---\nname: auto/deploy-helper\ndescription: Live description\n"
            "triggers: a\n---\n\n## Steps\n1. old\n"
        )
    )
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()

    async def fake_merge(*a):
        return "## Steps\n1. m\n"

    c._merge_skill_update = fake_merge  # type: ignore[assignment]
    ctx = _sel_recorder([])
    try:
        await asyncio.to_thread(
            c._stage_skill_update,
            key="sess",
            target_key="auto/deploy-helper",
            description="",
            triggers="b",
            procedure_md="## Steps\n1. cand\n",
        )
    finally:
        ctx.stop()

    assert loader.staged[0]["description"] == "Live description"


@pytest.mark.asyncio
async def test_base_version_is_captured_before_the_body_it_describes():
    """base_version must describe the body the merge was computed from.

    If live advances while the (up to 90s) merge turn runs, sampling the version
    afterwards records the NEW version against a body merged from the OLD one —
    and approve_pending_update_checked's staleness guard, seeing base == current, would
    let that stale body overwrite the intervening update. The guard is only as
    good as the version it is handed.
    """
    loader = FakeLoader(
        live_body="---\nname: auto/drift\ndescription: d\ntriggers: t\n---\n\n## Steps\n1. v1\n",
        version=1,  # live is v1 when the body is read
    )
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()

    async def fake_merge(*a):
        # An approval lands mid-merge and advances live to v2.
        loader._version = 2
        return "## Steps\n1. merged from v1\n"

    c._merge_skill_update = fake_merge  # type: ignore[assignment]
    ctx = _sel_recorder([])
    try:
        await asyncio.to_thread(
            c._stage_skill_update,
            key="sess",
            target_key="auto/drift",
            description="d",
            triggers="t",
            procedure_md="## Steps\n1. cand\n",
        )
    finally:
        ctx.stop()

    # Recorded base is the version the body actually came from (1), NOT the
    # version live drifted to during the merge (2). The staleness guard will then
    # correctly refuse this candidate instead of silently applying it over v2.
    assert loader.staged[0]["base_version"] == 1


# ── one pending update per live skill ──


def _prov(session_key="s"):
    from kiro_crew.skills import AutoSkillProvenance

    return AutoSkillProvenance(session_key=session_key, created_at=AutoSkillProvenance.now_iso())


def _real_loader_with_live(tmp_path, slug="deploy-helper"):
    from kiro_crew.skills import SkillsLoader

    loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
    assert (
        loader.create_auto_skill(
            slug,
            description="deploy the service",
            triggers="deploy",
            procedure_md="## Steps\n1. live step\n",
            provenance=_prov("s0"),
        )
        == f"auto/{slug}"
    )
    return loader


def _stage_update_from(c, session_key, procedure_md, triggers):
    ctx = _sel_recorder([])
    try:
        c._stage_skill_update(
            key=session_key,
            target_key="auto/deploy-helper",
            description="",
            triggers=triggers,
            procedure_md=procedure_md,
        )
    finally:
        ctx.stop()


def _stage_direct(
    loader, slug, *, target="auto/deploy-helper", kind="update", scripts=None, base_version=1
):
    return loader.stage_skill_candidate(
        slug,
        description=f"d {slug}",
        triggers=slug,
        procedure_md=f"## Steps\n1. {slug}\n",
        provenance=_prov(),
        scripts=scripts,
        kind=kind,
        target=target if kind == "update" else None,
        base_version=base_version if kind == "update" else None,
    )


@pytest.fixture()
def consumed(monkeypatch):
    from kiro_crew import skills_auto_model

    events: list[dict] = []
    monkeypatch.setattr(skills_auto_model, "_PENDING_CONSUMED_HOOK", events.append)
    return events


@pytest.mark.asyncio
async def test_two_sessions_leave_one_pending_update_merged_from_live(tmp_path, consumed):
    """Two sessions proposing an update to one live skill leave ONE pending update.
    Each merge starts from the live body, so the survivor holds the live procedure
    and the second session's requirement, never the first session's."""
    loader = _real_loader_with_live(tmp_path)
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()
    merge_bases: list[str] = []

    async def fake_merge(base, d, t, p):
        merge_bases.append(base)
        return f"{base}\n{p}"

    c._merge_skill_update = fake_merge  # type: ignore[assignment]

    await asyncio.to_thread(_stage_update_from, c, "sess-a", "2. first requirement", "rollback")
    (first,) = [p["slug"] for p in loader.list_pending_skills()]
    await asyncio.to_thread(_stage_update_from, c, "sess-b", "3. second requirement", "canary")

    pending = loader.list_pending_skills()
    assert len(pending) == 1, [p["slug"] for p in pending]
    only = pending[0]
    assert only["slug"] != first
    assert only["kind"] == "update" and only["target"] == "auto/deploy-helper"
    assert all("live step" in b and "first requirement" not in b for b in merge_bases)
    body = loader.get_pending_skill(only["slug"])["content"]
    assert "live step" in body and "second requirement" in body
    assert "first requirement" not in body
    meta = loader.get_pending_skill(only["slug"])["meta"]
    assert "merged" not in meta
    assert [(e["slug"], e["outcome"]) for e in consumed] == [(first, "superseded")]


def test_staged_update_records_the_digest_of_its_files(tmp_path):
    from kiro_crew.skill_runtime.auto_skills import _staged_digest
    from kiro_crew.skills_review import _candidate_digest

    loader = _real_loader_with_live(tmp_path)
    name = _stage_direct(loader, "u-one", scripts=[{"filename": "run.sh", "content": "echo hi\n"}])
    pdir = loader._pending_root() / name.split("/", 1)[1]
    meta = loader._read_pending_meta(pdir.name)
    # An untouched candidate recomputes to its recorded digest, which is the
    # dismiss/approve reader and digest with the metadata left out.
    body, _meta, scripts = loader._read_candidate_pinned(pdir)
    assert meta["staged_sha256"] == _candidate_digest(body, {}, scripts)
    assert meta["staged_sha256"] == _staged_digest(loader, pdir)
    # The digest covers SKILL.md and scripts, never the metadata file.
    (pdir / ".meta.json").write_text(json.dumps({**meta, "note": "x"}), encoding="utf-8")
    assert _staged_digest(loader, pdir) == meta["staged_sha256"]
    (pdir / "scripts" / "run.sh").write_text("echo bye\n", encoding="utf-8")
    assert _staged_digest(loader, pdir) != meta["staged_sha256"]


@pytest.mark.parametrize("base_version", [1, None], ids=["matching-base", "no-base"])
def test_unreadable_new_update_keeps_a_readable_queued_update(tmp_path, consumed, base_version):
    from kiro_crew.skills_candidate_tree import _PENDING_SCRIPT_MAX_ENTRIES

    loader = _real_loader_with_live(tmp_path)
    assert _stage_direct(loader, "u-readable") == "auto/u-readable"
    first = loader.get_pending_skill("u-readable")
    assert first is not None
    assert first["meta"]["staged_sha256"]

    scripts = [
        {"filename": f"step-{n}.py", "content": "print('step')\n"}
        for n in range(_PENDING_SCRIPT_MAX_ENTRIES + 1)
    ]
    assert (
        _stage_direct(loader, "u-unreadable", scripts=scripts, base_version=base_version)
        == "auto/u-unreadable"
    )

    assert {p["slug"] for p in loader.list_pending_skills()} == {"u-readable", "u-unreadable"}
    assert loader.get_pending_skill("u-readable") == first
    assert (loader._pending_root() / "u-unreadable").is_dir()
    assert "staged_sha256" not in loader._read_pending_meta("u-unreadable")
    assert loader.get_pending_skill("u-unreadable") is None
    assert consumed == []


def test_a_new_candidate_records_no_digest(tmp_path):
    loader = _real_loader_with_live(tmp_path)
    assert _stage_direct(loader, "fresh-skill", kind="new") == "auto/fresh-skill"
    assert "staged_sha256" not in loader._read_pending_meta("fresh-skill")


def _fifo_as_skill_md(pdir):
    (pdir / "SKILL.md").unlink()
    os.mkfifo(pdir / "SKILL.md")


def _fifo_as_script(pdir):
    (pdir / "scripts" / "run.sh").unlink()
    os.mkfifo(pdir / "scripts" / "run.sh")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
@pytest.mark.parametrize("plant", [_fifo_as_skill_md, _fifo_as_script], ids=["skill-md", "script"])
def test_a_fifo_in_a_candidate_has_no_digest_and_does_not_block(tmp_path, plant):
    from kiro_crew.skill_runtime.auto_skills import _staged_digest

    loader = _real_loader_with_live(tmp_path)
    name = _stage_direct(loader, "u-fifo", scripts=[{"filename": "run.sh", "content": "echo hi\n"}])
    pdir = loader._pending_root() / name.split("/", 1)[1]
    plant(pdir)
    result = []
    worker = threading.Thread(
        target=lambda: result.append(_staged_digest(loader, pdir)), daemon=True
    )
    worker.start()
    # Lost-run bound: a blocking open of the FIFO never returns, so name the hang.
    worker.join(timeout=30)
    assert not worker.is_alive(), "_staged_digest blocked opening a FIFO"
    assert result == [None]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
@pytest.mark.parametrize("plant", [_fifo_as_skill_md, _fifo_as_script], ids=["skill-md", "script"])
def test_replacement_keeps_a_queued_update_holding_a_fifo(tmp_path, consumed, plant):
    loader = _real_loader_with_live(tmp_path)
    first = _stage_direct(
        loader, "u-fifo", scripts=[{"filename": "run.sh", "content": "echo hi\n"}]
    ).split("/", 1)[1]
    plant(loader._pending_root() / first)
    result = []
    worker = threading.Thread(
        target=lambda: result.append(_stage_direct(loader, "u-two")), daemon=True
    )
    worker.start()
    # Lost-run bound: replacement reads the queued candidate under the claim lock.
    worker.join(timeout=30)
    assert not worker.is_alive(), "replacement blocked opening a FIFO"
    assert result == ["auto/u-two"]
    assert (loader._pending_root() / first).exists()
    assert [e for e in consumed if e["outcome"] == "superseded"] == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
def test_replacement_keeps_a_queued_update_with_fifo_metadata(tmp_path, consumed):
    loader = _real_loader_with_live(tmp_path)
    first = _stage_direct(loader, "u-fifo").split("/", 1)[1]
    pdir = loader._pending_root() / first
    mf = pdir / ".meta.json"
    mf.unlink()
    os.mkfifo(mf)
    result = []
    worker = threading.Thread(
        target=lambda: result.append(_stage_direct(loader, "u-two")), daemon=True
    )
    worker.start()
    # Lost-run bound: metadata is read while replacement holds the claim lock.
    worker.join(timeout=30)
    assert not worker.is_alive(), "replacement blocked opening FIFO metadata"
    assert result == ["auto/u-two"]
    assert pdir.is_dir()
    assert mf.exists()
    assert (loader._pending_root() / "u-two" / "SKILL.md").is_file()
    assert [e for e in consumed if e["outcome"] == "superseded"] == []


def test_replacement_removes_an_untouched_update_with_redacted_metadata(tmp_path, consumed):
    from kiro_crew.skills_review import _candidate_digest

    loader = _real_loader_with_live(tmp_path)
    first = _stage_direct(
        loader, "u-one", scripts=[{"filename": "run.sh", "content": "echo hi\n"}]
    ).split("/", 1)[1]
    pdir = loader._pending_root() / first
    body, meta, scripts = loader._read_candidate_pinned(pdir)
    assert meta == loader._read_pending_meta(first)
    assert meta["kind"] == "update"
    assert meta["target"] == "auto/deploy-helper"
    assert meta["staged_sha256"] == _candidate_digest(body, {}, scripts)

    assert _stage_direct(loader, "u-two") == "auto/u-two"
    assert not pdir.exists()
    assert (loader._pending_root() / "u-two" / "SKILL.md").is_file()
    assert [(e["slug"], e["outcome"]) for e in consumed] == [(first, "superseded")]


def test_replacement_keeps_an_oversized_queued_update_without_reading_it(
    tmp_path, consumed, monkeypatch
):
    from kiro_crew.skills_auto_model import _PENDING_DOCUMENT_MAX_BYTES

    loader = _real_loader_with_live(tmp_path)
    first = _stage_direct(loader, "u-big").split("/", 1)[1]
    md = loader._pending_root() / first / "SKILL.md"
    # Sparse: one byte past the pinned reader's document cap costs no disk.
    with open(md, "r+b") as fh:
        fh.truncate(_PENDING_DOCUMENT_MAX_BYTES + 1)
    read_total = [0]
    real_read = os.read

    def _counting_read(fd, n):
        data = real_read(fd, n)
        read_total[0] += len(data)
        return data

    with monkeypatch.context() as mp:
        mp.setattr(os, "read", _counting_read)
        assert _stage_direct(loader, "u-two") == "auto/u-two"

    assert read_total[0] < _PENDING_DOCUMENT_MAX_BYTES
    assert (loader._pending_root() / first).is_dir()
    assert (loader._pending_root() / "u-two").is_dir()
    assert [e for e in consumed if e["outcome"] == "superseded"] == []


def _edit_skill_md(pdir):
    md = pdir / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8") + "\n2. hand edit\n", encoding="utf-8")


def _edit_script(pdir):
    (pdir / "scripts" / "run.sh").write_text("echo edited\n", encoding="utf-8")


def _add_script(pdir):
    (pdir / "scripts" / "extra.sh").write_text("echo extra\n", encoding="utf-8")


def _rewrite_meta(pdir, change):
    mf = pdir / ".meta.json"
    meta = json.loads(mf.read_text(encoding="utf-8"))
    change(meta)
    mf.write_text(json.dumps(meta), encoding="utf-8")


def _link_script(pdir):
    import os

    target = pdir.parent.parent / "outside.sh"
    target.write_text("echo hi\n", encoding="utf-8")
    run = pdir / "scripts" / "run.sh"
    run.unlink()
    os.symlink(target, run)


def _nest_script(pdir):
    (pdir / "scripts" / "sub").mkdir()
    (pdir / "scripts" / "sub" / "deep.sh").write_text("echo deep\n", encoding="utf-8")


@pytest.mark.parametrize(
    "tamper",
    [
        _edit_skill_md,
        _edit_script,
        _add_script,
        lambda p: _rewrite_meta(p, lambda m: m.pop("staged_sha256")),
        lambda p: _rewrite_meta(p, lambda m: m.update(staged_sha256=7)),
        lambda p: _rewrite_meta(p, lambda m: m.update(staged_sha256="")),
        _link_script,
        _nest_script,
        lambda p: (p / "SKILL.md").unlink(),
        # Both unprovable at once: an absent digest must not match an unreadable tree.
        lambda p: (_rewrite_meta(p, lambda m: m.pop("staged_sha256")), _link_script(p)),
    ],
    ids=[
        "skill-md-edited",
        "script-edited",
        "script-added",
        "no-digest",
        "non-string-digest",
        "empty-digest",
        "linked-script",
        "nested-script-added",
        "skill-md-missing",
        "no-digest-and-linked-script",
    ],
)
def test_replacement_keeps_a_queued_update_it_cannot_prove_unchanged(tmp_path, consumed, tamper):
    loader = _real_loader_with_live(tmp_path)
    first = _stage_direct(
        loader, "u-one", scripts=[{"filename": "run.sh", "content": "echo hi\n"}]
    ).split("/", 1)[1]
    tamper(loader._pending_root() / first)

    second = _stage_direct(loader, "u-two").split("/", 1)[1]

    assert (loader._pending_root() / first).is_dir()
    assert (loader._pending_root() / second).is_dir()
    assert [e for e in consumed if e["outcome"] == "superseded"] == []


def test_replacement_removes_only_unchanged_updates_for_the_same_target(tmp_path, consumed):
    loader = _real_loader_with_live(tmp_path)
    same = _stage_direct(loader, "a-one", scripts=[{"filename": "run.sh", "content": "x\n"}])
    other = _stage_direct(loader, "b-one", target="auto/other")
    new = _stage_direct(loader, "new-one", kind="new")
    (loader._pending_root() / "not-canonical_").mkdir()

    kept = _stage_direct(loader, "a-two")

    slugs = {p["slug"] for p in loader.list_pending_skills()}
    assert slugs == {s.split("/", 1)[1] for s in (other, new, kept)}
    assert [(e["slug"], e["outcome"]) for e in consumed] == [("a-one", "superseded")]
    assert same == "auto/a-one"


@pytest.mark.parametrize("base_version", [1, 2, None], ids=["stale", "matching", "no-base"])
def test_replacement_checks_candidate_base_against_live_version(tmp_path, consumed, base_version):
    loader = _real_loader_with_live(tmp_path)
    assert _stage_direct(loader, "u-promote") == "auto/u-promote"
    assert loader.approve_pending_update_checked("u-promote") == "auto/deploy-helper"
    assert loader.get_auto_skill_version("auto/deploy-helper") == 2
    consumed.clear()
    assert _stage_direct(loader, "u-current", base_version=2) == "auto/u-current"

    assert _stage_direct(loader, "u-later", base_version=base_version) == "auto/u-later"

    slugs = {p["slug"] for p in loader.list_pending_skills()}
    if base_version == 1:
        assert slugs == {"u-current", "u-later"}
        assert consumed == []
    else:
        assert slugs == {"u-later"}
        assert [(e["slug"], e["outcome"]) for e in consumed] == [("u-current", "superseded")]


def test_replacement_keeps_queued_updates_when_live_version_is_unreadable(tmp_path, consumed):
    loader = _real_loader_with_live(tmp_path)
    assert _stage_direct(loader, "u-one") == "auto/u-one"

    with patch.object(
        loader, "get_auto_skill_version", side_effect=PermissionError
    ) as read_version:
        assert _stage_direct(loader, "u-two") == "auto/u-two"

    read_version.assert_called_once_with("auto/deploy-helper")
    assert {p["slug"] for p in loader.list_pending_skills()} == {"u-one", "u-two"}
    assert consumed == []


def test_a_failed_removal_keeps_the_queued_update(tmp_path, consumed, monkeypatch):
    from kiro_crew.skill_runtime import auto_skills

    loader = _real_loader_with_live(tmp_path)
    first = _stage_direct(loader, "u-one").split("/", 1)[1]
    real_rmtree = auto_skills.shutil.rmtree

    def _rmtree(path, *a, **kw):
        if str(path).endswith(first):
            raise OSError("busy")
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(auto_skills.shutil, "rmtree", _rmtree)
    _stage_direct(loader, "u-two")

    assert (loader._pending_root() / first).is_dir()
    assert consumed == []


@pytest.mark.asyncio
async def test_update_slugs_are_never_reused_after_a_replacement(tmp_path, consumed):
    loader = _real_loader_with_live(tmp_path)
    c = _mk(loader)
    c._event_loop = None
    seen: list[str] = []
    for n in range(4):
        await asyncio.to_thread(_stage_update_from, c, f"sess-{n}", f"{n}. step", "t")
        (current,) = [p["slug"] for p in loader.list_pending_skills()]
        seen.append(current)
    assert len(set(seen)) == len(seen)
    assert [e["slug"] for e in consumed] == seen[:-1]


# ── dismiss names the bytes the reviewer read ──


def test_dismiss_with_the_read_digest_deletes(tmp_path, consumed):
    loader = _real_loader_with_live(tmp_path)
    slug = _stage_direct(loader, "u-one").split("/", 1)[1]
    seen = loader.get_pending_skill(slug)["content_sha256"]

    assert loader.dismiss_pending_skill(slug, seen) is True
    assert not (loader._pending_root() / slug).exists()
    assert [(e["slug"], e["outcome"]) for e in consumed] == [(slug, "dismissed")]


def test_dismiss_with_a_stale_digest_refuses_and_keeps(tmp_path, consumed):
    from kiro_crew.skills import PendingDismissRefused

    loader = _real_loader_with_live(tmp_path)
    slug = _stage_direct(loader, "u-one").split("/", 1)[1]
    seen = loader.get_pending_skill(slug)["content_sha256"]
    _edit_skill_md(loader._pending_root() / slug)

    with pytest.raises(PendingDismissRefused) as refused:
        loader.dismiss_pending_skill(slug, seen)
    assert refused.value.reason == "content_changed"
    assert (loader._pending_root() / slug).is_dir()
    assert consumed == []


def test_dismiss_with_a_digest_refuses_an_unreadable_candidate(tmp_path, monkeypatch):
    from kiro_crew.skills import PendingDismissRefused

    loader = _real_loader_with_live(tmp_path)
    slug = _stage_direct(loader, "u-one").split("/", 1)[1]
    seen = loader.get_pending_skill(slug)["content_sha256"]
    monkeypatch.setattr(loader, "_read_candidate_pinned", lambda _pdir: None)

    with pytest.raises(PendingDismissRefused) as refused:
        loader.dismiss_pending_skill(slug, seen)
    assert refused.value.reason == "content_changed"
    assert (loader._pending_root() / slug).is_dir()


def test_dismiss_with_a_digest_refuses_without_the_lock(tmp_path, monkeypatch):
    import contextlib

    from kiro_crew.skills import PendingDismissRefused

    loader = _real_loader_with_live(tmp_path)
    slug = _stage_direct(loader, "u-one").split("/", 1)[1]
    seen = loader.get_pending_skill(slug)["content_sha256"]

    @contextlib.contextmanager
    def _unacquired():
        yield False

    monkeypatch.setattr(loader, "_auto_slug_claim_lock", _unacquired)
    with pytest.raises(PendingDismissRefused) as refused:
        loader.dismiss_pending_skill(slug, seen)
    assert refused.value.reason == "lock_unavailable"
    assert (loader._pending_root() / slug).is_dir()


def test_dismiss_with_a_digest_of_a_candidate_gone_under_the_lock(tmp_path, monkeypatch):
    import contextlib
    import shutil

    loader = _real_loader_with_live(tmp_path)
    slug = _stage_direct(loader, "u-one").split("/", 1)[1]
    seen = loader.get_pending_skill(slug)["content_sha256"]
    real_lock = loader._auto_slug_claim_lock

    @contextlib.contextmanager
    def _lock_after_removal():
        shutil.rmtree(loader._pending_root() / slug)
        with real_lock() as locked:
            yield locked

    monkeypatch.setattr(loader, "_auto_slug_claim_lock", _lock_after_removal)
    assert loader.dismiss_pending_skill(slug, seen) is False


def test_dismiss_without_a_digest_deletes_as_found(tmp_path):
    loader = _real_loader_with_live(tmp_path)
    slug = _stage_direct(loader, "u-one").split("/", 1)[1]
    _edit_skill_md(loader._pending_root() / slug)

    assert loader.dismiss_pending_skill(slug) is True
    assert not (loader._pending_root() / slug).exists()


# ── an UPDATE verdict naming a queued update ──


def _run_update_verdict(c, verdict_target):
    c._dedupe_candidate = lambda s, d, t: (H.VERDICT_UPDATE, verdict_target)  # type: ignore[assignment]
    result = {
        "new_skill": {
            "slug": "another-take",
            "description": "d",
            "triggers": "t",
            "procedure_md": "## Steps\n1. x",
        }
    }
    ctx = _sel_recorder([])
    try:
        c._process_auto_skills(result, "sess")
    finally:
        ctx.stop()


def _loader_with_queue(pending, live):
    loader = FakeLoader()
    loader.list_pending_skills = lambda: pending  # type: ignore[method-assign]
    loader.read_auto_skill_body = lambda key: live.get(key)  # type: ignore[method-assign]
    return loader


def test_update_verdict_on_a_pending_update_targets_its_live_skill():
    loader = _loader_with_queue(
        [
            {
                "slug": "deploy-helper-update-1a2b3c4d",
                "kind": "update",
                "target": "auto/deploy-helper",
            }
        ],
        {"auto/deploy-helper": _LIVE_BODY},
    )
    c = _mk(loader)
    _run_update_verdict(c, "auto/deploy-helper-update-1a2b3c4d")

    assert len(loader.staged) == 1
    assert loader.staged[0]["kind"] == "update"
    assert loader.staged[0]["target"] == "auto/deploy-helper"


def test_a_live_skill_named_like_an_update_is_not_redirected():
    """Liveness comes first: a live ``auto/x-update`` is updated itself, even
    when a queued update of the same slug targets another skill."""
    loader = _loader_with_queue(
        [{"slug": "x-update", "kind": "update", "target": "auto/x"}],
        {"auto/x": _LIVE_BODY, "auto/x-update": _LIVE_BODY},
    )
    c = _mk(loader)
    _run_update_verdict(c, "auto/x-update")

    assert len(loader.staged) == 1
    assert loader.staged[0]["kind"] == "update"
    assert loader.staged[0]["target"] == "auto/x-update"


@pytest.mark.parametrize(
    "pending",
    [
        [{"slug": "x-update", "kind": "update", "target": "auto/gone"}],
        [{"slug": "x-update", "kind": "update"}],
        [{"slug": "x-update", "kind": "new"}],
        [],
    ],
    ids=["target-not-live", "no-target", "new-candidate", "not-queued"],
)
def test_an_unresolvable_pending_target_is_staged_as_new(pending):
    loader = _loader_with_queue(pending, {"auto/x": _LIVE_BODY})
    c = _mk(loader)
    _run_update_verdict(c, "auto/x-update")

    assert len(loader.staged) == 1
    assert loader.staged[0]["kind"] == "new"


def test_an_unreadable_queue_or_live_read_is_staged_as_new():
    loader = _loader_with_queue([], {})

    def _boom():
        raise OSError("unreadable")

    loader.list_pending_skills = _boom  # type: ignore[method-assign]
    c = _mk(loader)
    _run_update_verdict(c, "auto/x-update")
    assert loader.staged[-1]["kind"] == "new"

    loader2 = _loader_with_queue([{"slug": "x-update", "kind": "update", "target": "auto/x"}], {})

    def _live_boom(key):
        raise OSError("unreadable")

    loader2.read_auto_skill_body = _live_boom  # type: ignore[method-assign]
    c2 = _mk(loader2)
    _run_update_verdict(c2, "auto/x-update")
    assert loader2.staged[-1]["kind"] == "new"
