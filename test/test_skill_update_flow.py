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
        supersede_slug=None,
        supersede_created_at=None,
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
                "supersede_slug": supersede_slug,
                "supersede_created_at": supersede_created_at,
            }
        )
        return f"auto/{slug}"

    # new-path fallbacks (unused in these tests but referenced by the code)
    def create_auto_skill(self, *a, **k):
        return None

    def run_skill_lifecycle(self, *a, **k):
        return None


def _mk(loader, **kw):
    return HistoryConsolidator(
        log=MagicMock(),
        memory=MagicMock(),
        skills_loader=loader,
        auto_skills_enabled=True,
        approval_required=True,
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
    assert st["slug"] == "deploy-helper-update"
    assert st["kind"] == "update"
    assert st["target"] == "auto/deploy-helper"
    assert st["base_version"] == 7
    # The merge boundary sanitizer strips surrounding whitespace (the SKILL.md
    # builder strips the body anyway), so compare against the stripped form.
    assert st["procedure_md"] == merged.strip()
    ev = [r for r in recorded if r.get("outcome") == "staged_update"]
    assert ev and ev[0]["metadata"] == {
        "name": "auto/deploy-helper-update",
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


def test_process_routes_update(monkeypatch):
    loader = FakeLoader()
    c = _mk(loader)
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
    assert staged_slug.endswith("-update")
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


def _real_loader_with_live(tmp_path):
    from kiro_crew.skills import AutoSkillProvenance, SkillsLoader

    loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
    assert (
        loader.create_auto_skill(
            "deploy-helper",
            description="deploy the service",
            triggers="deploy",
            procedure_md="## Steps\n1. live step\n",
            provenance=AutoSkillProvenance(
                session_key="s0", created_at=AutoSkillProvenance.now_iso()
            ),
        )
        == "auto/deploy-helper"
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


@pytest.mark.asyncio
async def test_two_sessions_updating_one_skill_leave_one_pending_item(tmp_path):
    """Two sessions proposing an update to the same live skill must leave ONE
    pending update, and the second merge must start from the first proposal so
    neither requirement is lost."""
    loader = _real_loader_with_live(tmp_path)
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()
    merge_bases: list[str] = []

    async def fake_merge(base, d, t, p):
        merge_bases.append(base)
        return f"{base}\n{p}"

    c._merge_skill_update = fake_merge  # type: ignore[assignment]

    await asyncio.to_thread(_stage_update_from, c, "sess-a", "2. first requirement", "rollback")
    await asyncio.to_thread(_stage_update_from, c, "sess-b", "3. second requirement", "canary")

    pending = loader.list_pending_skills()
    assert len(pending) == 1, [p["slug"] for p in pending]
    only = pending[0]
    assert only["kind"] == "update" and only["target"] == "auto/deploy-helper"
    # The second merge was built on the first proposal, not on the live body.
    assert "first requirement" in merge_bases[1]
    body = loader.get_pending_skill(only["slug"])["content"]
    assert "first requirement" in body and "second requirement" in body
    for trig in ("deploy", "rollback", "canary"):
        assert trig in only["triggers"]


@pytest.mark.asyncio
async def test_queued_update_on_an_older_live_version_is_not_the_merge_base(tmp_path):
    """A queued update built on an older live version is refused as stale at
    approval, so the next update merges over the live body and replaces it."""
    loader = _real_loader_with_live(tmp_path)
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()
    merge_bases: list[str] = []

    async def fake_merge(base, d, t, p):
        merge_bases.append(base)
        return f"{base}\n{p}"

    c._merge_skill_update = fake_merge  # type: ignore[assignment]
    await asyncio.to_thread(_stage_update_from, c, "sess-a", "2. first requirement", "rollback")
    real_version = loader.get_auto_skill_version("auto/deploy-helper")
    loader.get_auto_skill_version = lambda key: real_version + 1  # type: ignore[method-assign]
    await asyncio.to_thread(_stage_update_from, c, "sess-b", "3. second requirement", "canary")

    assert "first requirement" not in merge_bases[1]
    # The stale one was not the merge base, so it is not superseded: it stays
    # beside the new candidate for approval to refuse.
    pending = loader.list_pending_skills()
    assert len(pending) == 2, [p["slug"] for p in pending]


@pytest.mark.asyncio
async def test_failed_merge_over_a_queued_update_supersedes_nothing(tmp_path):
    """When the merge over a queued update fails, the new candidate carries
    only its own procedure, so the queued requirement must stay queued."""
    loader = _real_loader_with_live(tmp_path)
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()
    calls: list[str] = []

    async def merge_then_fail(base, d, t, p):
        calls.append(base)
        if len(calls) == 1:
            return f"{base}\n{p}"
        raise RuntimeError("merge unavailable")

    c._merge_skill_update = merge_then_fail  # type: ignore[assignment]
    await asyncio.to_thread(_stage_update_from, c, "sess-a", "2. first requirement", "rollback")
    await asyncio.to_thread(_stage_update_from, c, "sess-b", "3. second requirement", "canary")

    pending = loader.list_pending_skills()
    assert len(pending) == 2, [p["slug"] for p in pending]
    bodies = [loader.get_pending_skill(p["slug"])["content"] for p in pending]
    assert any("first requirement" in b for b in bodies)
    assert any("second requirement" in b for b in bodies)


@pytest.mark.asyncio
async def test_merge_supersedes_only_the_queued_update_it_used(tmp_path):
    """With two queued updates for one skill, a merge built from one of them
    removes that one only; the other sibling keeps its requirement queued."""
    from kiro_crew.skills import AutoSkillProvenance

    loader = _real_loader_with_live(tmp_path)
    version = loader.get_auto_skill_version("auto/deploy-helper")
    for step in ("2. first requirement", "2. sibling requirement"):
        loader.stage_skill_candidate(
            "deploy-helper-update",
            description="deploy the service",
            triggers="deploy",
            procedure_md=f"## Steps\n1. live step\n{step}\n",
            provenance=AutoSkillProvenance(
                session_key="s", created_at=AutoSkillProvenance.now_iso()
            ),
            kind="update",
            target="auto/deploy-helper",
            base_version=version,
        )
    assert sorted(p["slug"] for p in loader.list_pending_skills()) == [
        "deploy-helper-update",
        "deploy-helper-update-2",
    ]
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()
    merge_bases: list[str] = []

    async def fake_merge(base, d, t, p):
        merge_bases.append(base)
        return f"{base}\n{p}"

    c._merge_skill_update = fake_merge  # type: ignore[assignment]
    await asyncio.to_thread(_stage_update_from, c, "sess-b", "3. second requirement", "canary")

    slugs = sorted(p["slug"] for p in loader.list_pending_skills())
    assert "first requirement" in merge_bases[0]
    assert "deploy-helper-update" not in slugs
    assert "deploy-helper-update-2" in slugs
    assert len(slugs) == 2, slugs


def test_stage_update_supersedes_only_the_same_target(tmp_path, monkeypatch):
    """Staging an update replaces the named queued update for the SAME target
    only; a queued update for another skill and a queued new candidate stay,
    and a named slug for another target is left alone."""
    from kiro_crew.skills import AutoSkillProvenance, SkillsLoader

    loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

    def stage(slug, **kw):
        return loader.stage_skill_candidate(
            slug,
            description=f"d {slug}",
            triggers=slug,
            procedure_md="## Steps\n1. x",
            provenance=AutoSkillProvenance(
                session_key="s", created_at=AutoSkillProvenance.now_iso()
            ),
            **kw,
        )

    assert stage("a-update", kind="update", target="auto/a", base_version=1) == "auto/a-update"
    assert stage("b-update", kind="update", target="auto/b", base_version=1) == "auto/b-update"
    assert stage("a-new") == "auto/a-new"
    consumed: list[dict] = []
    from kiro_crew import skills_auto_model

    monkeypatch.setattr(skills_auto_model, "_PENDING_CONSUMED_HOOK", consumed.append)
    # Naming a candidate that is not an update for this target removes nothing.
    stage("a-update", kind="update", target="auto/a", base_version=1, supersede_slug="b-update")
    stage("a-update", kind="update", target="auto/a", base_version=1, supersede_slug="a-new")
    assert [e for e in consumed if e["outcome"] == "superseded"] == []
    for p in loader.list_pending_skills():
        if p["slug"] not in ("a-update", "a-new", "b-update"):
            loader.dismiss_pending_skill(p["slug"])
    a_created = next(
        p["created_at"] for p in loader.list_pending_skills() if p["slug"] == "a-update"
    )
    # A different created_at means a different candidate under the same slug.
    stray = stage(
        "a-update",
        kind="update",
        target="auto/a",
        base_version=1,
        supersede_slug="a-update",
        supersede_created_at="1999-01-01T00:00:00+00:00",
    )
    assert [e for e in consumed if e["outcome"] == "superseded"] == []
    loader.dismiss_pending_skill(stray.split("/", 1)[1])
    second = stage(
        "a-update",
        kind="update",
        target="auto/a",
        base_version=1,
        supersede_slug="a-update",
        supersede_created_at=a_created,
    )

    slugs = sorted(p["slug"] for p in loader.list_pending_skills())
    assert slugs == sorted(["a-new", "b-update", second.split("/", 1)[1]])
    assert len([p for p in loader.list_pending_skills() if p.get("target") == "auto/a"]) == 1
    assert [e["slug"] for e in consumed if e["outcome"] == "superseded"] == ["a-update"]


@pytest.mark.asyncio
async def test_update_verdict_on_a_pending_update_targets_its_live_skill(monkeypatch):
    """The judge can name the queued UPDATE candidate itself. That is an update
    to the candidate's live skill, not a new skill to queue beside it."""
    loader = FakeLoader()
    loader.list_pending_skills = lambda: [  # type: ignore[method-assign]
        {"slug": "deploy-helper-update", "kind": "update", "target": "auto/deploy-helper"}
    ]
    loader.get_pending_skill = lambda slug: None  # type: ignore[attr-defined]
    live = {"auto/deploy-helper": _LIVE_BODY}
    loader.read_auto_skill_body = lambda key: live.get(key)  # type: ignore[method-assign]
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()
    c._dedupe_candidate = lambda s, d, t: (H.VERDICT_UPDATE, "auto/deploy-helper-update")  # type: ignore[assignment]

    async def fake_merge(*a):
        return "## Steps\n1. m\n"

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


def _queue_update_with_scripts(loader, scripts):
    from kiro_crew.skills import AutoSkillProvenance

    name = loader.stage_skill_candidate(
        "deploy-helper-update",
        description="deploy the service",
        triggers="deploy",
        procedure_md="## Steps\n1. live step\n2. first requirement, run check.sh\n",
        provenance=AutoSkillProvenance(session_key="s", created_at=AutoSkillProvenance.now_iso()),
        scripts=scripts,
        kind="update",
        target="auto/deploy-helper",
        base_version=loader.get_auto_skill_version("auto/deploy-helper"),
    )
    assert name == "auto/deploy-helper-update"


async def _merge_second_update(loader, monkeypatch, scripts):
    from kiro_crew import skills_auto_model

    consumed: list[dict] = []
    monkeypatch.setattr(skills_auto_model, "_PENDING_CONSUMED_HOOK", consumed.append)
    c = _mk(loader)
    c._event_loop = asyncio.get_running_loop()
    merge_bases: list[str] = []

    async def fake_merge(base, d, t, p):
        merge_bases.append(base)
        return f"{base}\n{p}"

    c._merge_skill_update = fake_merge  # type: ignore[assignment]

    def stage():
        ctx = _sel_recorder([])
        try:
            c._stage_skill_update(
                key="sess-b",
                target_key="auto/deploy-helper",
                description="",
                triggers="canary",
                procedure_md="3. second requirement",
                scripts=scripts,
            )
        finally:
            ctx.stop()

    await asyncio.to_thread(stage)
    assert len(merge_bases) == 1
    return consumed, merge_bases[0]


_CHECK_SH = {"filename": "check.sh", "content": "#!/bin/sh\necho ok\n"}


@pytest.mark.asyncio
async def test_queued_update_with_a_script_the_new_one_lacks_is_not_the_merge_base(
    tmp_path, monkeypatch
):
    """Approval copies only the approved candidate's scripts, so a body merged
    from a queued update shipping a script the new candidate does not ship would
    cite a missing script. The merge runs from the live body instead, and both
    candidates stay queued."""
    loader = _real_loader_with_live(tmp_path)
    _queue_update_with_scripts(loader, [_CHECK_SH])
    consumed, base = await _merge_second_update(loader, monkeypatch, scripts=None)

    assert "live step" in base
    assert "first requirement" not in base and "check.sh" not in base
    slugs = sorted(p["slug"] for p in loader.list_pending_skills())
    assert slugs == ["deploy-helper-update", "deploy-helper-update-2"], slugs
    assert (loader._pending_root() / "deploy-helper-update" / "scripts" / "check.sh").is_file()
    assert [e for e in consumed if e["outcome"] == "superseded"] == []
    new_body = loader.get_pending_skill("deploy-helper-update-2")["content"]
    assert "check.sh" not in new_body


@pytest.mark.asyncio
async def test_queued_update_whose_scripts_the_new_one_ships_is_superseded(tmp_path, monkeypatch):
    loader = _real_loader_with_live(tmp_path)
    _queue_update_with_scripts(loader, [_CHECK_SH])
    consumed, base = await _merge_second_update(
        loader, monkeypatch, scripts=[{"filename": "check.sh", "content": "#!/bin/sh\necho v2\n"}]
    )

    assert "first requirement" in base
    slugs = [p["slug"] for p in loader.list_pending_skills()]
    assert slugs == ["deploy-helper-update-2"], slugs
    assert [e["slug"] for e in consumed if e["outcome"] == "superseded"] == ["deploy-helper-update"]


@pytest.mark.asyncio
async def test_queued_update_with_unreadable_script_list_is_not_the_merge_base(
    tmp_path, monkeypatch
):
    """A queued update whose ``scripts`` metadata cannot be read may ship any
    script, so it is not used as the merge base."""
    loader = _real_loader_with_live(tmp_path)
    _queue_update_with_scripts(loader, None)
    meta_path = loader._pending_root() / "deploy-helper-update" / ".meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["scripts"] = "check.sh"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    consumed, base = await _merge_second_update(loader, monkeypatch, scripts=None)

    assert "first requirement" not in base
    assert len(loader.list_pending_skills()) == 2
    assert [e for e in consumed if e["outcome"] == "superseded"] == []


@pytest.mark.asyncio
async def test_queued_update_without_scripts_is_superseded(tmp_path, monkeypatch):
    loader = _real_loader_with_live(tmp_path)
    _queue_update_with_scripts(loader, None)
    consumed, base = await _merge_second_update(loader, monkeypatch, scripts=None)

    assert "first requirement" in base
    slugs = [p["slug"] for p in loader.list_pending_skills()]
    assert slugs == ["deploy-helper-update-2"], slugs
    assert [e["slug"] for e in consumed if e["outcome"] == "superseded"] == ["deploy-helper-update"]
