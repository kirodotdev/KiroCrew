"""Phase-1 tests: pending-approval + pin dashboard API handlers."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew import pinned_fs
from kiro_crew.dashboard.handlers import prompts as H
from kiro_crew.skills import AutoSkillProvenance, SkillsLoader

_OMITTED = object()


class _Req:
    """Minimal aiohttp-request stand-in for handler unit tests."""

    def __init__(self, loader, *, match=None, body=_OMITTED, query=None):
        state = SimpleNamespace(context_builder=SimpleNamespace(skills=loader))
        self.app = {"state": state}
        self.match_info = match or {}
        # `body or {}` would have turned a falsy-but-valid JSON body (`[]`, `0`,
        # `null`) into a dict inside the double — hiding exactly the non-object
        # bodies a handler has to survive. Only an OMITTED body defaults.
        self._body = {} if body is _OMITTED else body
        self.query = query or {}

    async def json(self):
        return self._body


def _payload(resp):
    return json.loads(resp.body.decode())


@pytest.fixture()
def loader(tmp_path):
    ld = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
    ld.stage_skill_candidate(
        "deploy-helper",
        description="deploy helper",
        triggers="deploy",
        procedure_md="## Steps\n1. go\n",
        provenance=AutoSkillProvenance(session_key="s", created_at=AutoSkillProvenance.now_iso()),
    )
    return ld


@pytest.mark.asyncio
async def test_list_pending(loader):
    resp = await H.api_skills_pending(_Req(loader))
    data = _payload(resp)
    assert [p["slug"] for p in data["pending"]] == ["deploy-helper"]


@pytest.mark.asyncio
async def test_detail(loader):
    resp = await H.api_skill_pending_detail(_Req(loader, match={"slug": "deploy-helper"}))
    data = _payload(resp)
    assert data["name"] == "auto/deploy-helper"
    assert "go" in data["content"]


@pytest.mark.asyncio
async def test_detail_invalid_slug(loader):
    resp = await H.api_skill_pending_detail(_Req(loader, match={"slug": "../etc"}))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_pin_executor_failure_audits_and_500s(loader, monkeypatch):
    """A set_pinned executor failure must emit a SEL error event and return a
    controlled 500, not bypass auditing."""

    def _boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(loader, "set_pinned", _boom)
    events: list[dict] = []
    monkeypatch.setattr(
        H,
        "_sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
    )
    resp = await H.api_skill_pin(_Req(loader, body={"name": "auto/deploy-helper", "pinned": True}))
    assert resp.status == 500
    assert any(e.get("outcome") == "error" for e in events)


@pytest.mark.asyncio
async def test_detail_executor_failure_audits_and_500s(loader, monkeypatch):
    """A filesystem/executor failure must emit a SEL error event and return a
    controlled 500 — not bypass mandatory auditing with an unhandled crash."""

    def _boom(_slug):
        raise OSError("disk gone")

    monkeypatch.setattr(loader, "get_pending_skill", _boom)
    events: list[dict] = []
    monkeypatch.setattr(
        H,
        "_sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
    )
    resp = await H.api_skill_pending_detail(_Req(loader, match={"slug": "deploy-helper"}))
    assert resp.status == 500
    assert any(e.get("outcome") == "error" for e in events)


@pytest.mark.asyncio
async def test_approve_promotes(loader):
    resp = await H.api_skill_pending_approve(_Req(loader, match={"slug": "deploy-helper"}))
    assert resp.status == 200
    assert _payload(resp)["approved"] == "auto/deploy-helper"
    assert [s["key"] for s in loader.list_auto_skills()] == ["auto/deploy-helper"]
    assert loader.list_pending_skills() == []


@pytest.mark.asyncio
async def test_approve_missing_returns_404_coded(loader):
    resp = await H.api_skill_pending_approve(_Req(loader, match={"slug": "nope"}))
    assert resp.status == 404
    data = _payload(resp)
    assert data["code"] == "pending_skill_not_found"


@pytest.mark.asyncio
async def test_dismiss(loader):
    resp = await H.api_skill_pending_dismiss(_Req(loader, match={"slug": "deploy-helper"}))
    assert resp.status == 200
    assert loader.list_pending_skills() == []
    resp2 = await H.api_skill_pending_dismiss(_Req(loader, match={"slug": "deploy-helper"}))
    assert resp2.status == 404
    # Coded, like the approve path: the dashboard keys its recovery
    # (refetch + catalog message) on this code.
    assert _payload(resp2)["code"] == "pending_skill_not_found"


@pytest.mark.asyncio
async def test_pin_roundtrip(loader):
    name = loader.approve_pending_skill("deploy-helper")
    assert name == "auto/deploy-helper"
    resp = await H.api_skill_pin(_Req(loader, body={"name": name, "pinned": True}))
    assert resp.status == 200 and _payload(resp)["pinned"] is True
    resp2 = await H.api_skill_pin(_Req(loader, body={"name": "does/not-exist", "pinned": True}))
    assert resp2.status == 400


@pytest.mark.asyncio
async def test_pin_rejects_non_bool_pinned(loader):
    name = loader.approve_pending_skill("deploy-helper")
    assert name == "auto/deploy-helper"
    # JSON string "false" must be rejected, not coerced to truthy (which would
    # pin instead of unpin) — GPT MEDIUM.
    resp = await H.api_skill_pin(_Req(loader, body={"name": name, "pinned": "false"}))
    assert resp.status == 400
    resp2 = await H.api_skill_pin(_Req(loader, body={"name": name, "pinned": 1}))
    assert resp2.status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [[], "name", 7, None, True])
async def test_inject_on_trigger_rejects_a_non_object_body(loader, body):
    """`[]` and `"x"` are valid JSON, so `request.json()` can hand back a
    non-dict. Calling `.get` on it would raise AttributeError and surface as a
    500 — a validation answer is the correct outcome."""
    resp = await H.api_skill_inject_on_trigger(_Req(loader, body=body))
    assert resp.status == 400
    assert _payload(resp)["code"] == "inject_not_bool"


# ── Part C: pending-update fields + approve/detail routing ──
#
# These monkeypatch the loader so they do NOT depend on part B landing the
# kind/target/base_version support in skills.py.


@pytest.mark.asyncio
async def test_list_pending_passes_update_fields(loader, monkeypatch):
    """kind/target/base_version flow through the list handler untouched."""
    monkeypatch.setattr(
        loader,
        "list_pending_skills",
        lambda: [
            {
                "slug": "deploy-helper-update",
                "name": "auto/deploy-helper-update",
                "description": "d",
                "triggers": "t",
                "has_scripts": False,
                "created_at": "",
                "source": "consolidation",
                "kind": "update",
                "target": "auto/deploy-helper",
                "base_version": 3,
            }
        ],
    )
    resp = await H.api_skills_pending(_Req(loader))
    p = _payload(resp)["pending"][0]
    assert p["kind"] == "update"
    assert p["target"] == "auto/deploy-helper"
    assert p["base_version"] == 3


@pytest.mark.asyncio
async def test_update_detail_includes_live_body(loader, monkeypatch):
    """An update candidate's detail carries the target's current live body."""
    monkeypatch.setattr(
        loader,
        "get_pending_skill",
        lambda slug: {
            "slug": slug,
            "name": "auto/deploy-helper-update",
            "meta": {"kind": "update", "target": "auto/deploy-helper"},
            "content": "## Steps\nnew\n",
            "scripts": [],
        },
    )
    monkeypatch.setattr(
        loader,
        "preview_pending_update",
        lambda slug: {
            "live_body": "## Steps\nOLD BODY\n",
            "proposed_body": "## Steps\nNEW BODY\n",
            "diff": "--- a\n+++ b\n@@ -1 +1 @@\n-OLD BODY\n+NEW BODY\n",
            "from_version": 2,
            "to_version": 3,
            "base_version": 2,
            "stale_base": False,
        },
        raising=False,
    )
    resp = await H.api_skill_pending_detail(_Req(loader, match={"slug": "deploy-helper-update"}))
    data = _payload(resp)
    assert data["live_body"] == "## Steps\nOLD BODY\n"
    assert data["proposed_body"] == "## Steps\nNEW BODY\n"
    assert "+NEW BODY" in data["diff"]
    assert (data["from_version"], data["to_version"]) == (2, 3)
    assert data["stale_base"] is False


@pytest.mark.asyncio
async def test_update_detail_live_body_null_when_target_gone(loader, monkeypatch):
    """If the target skill was removed, live_body is null (not an error)."""
    monkeypatch.setattr(
        loader,
        "get_pending_skill",
        lambda slug: {
            "slug": slug,
            "name": "auto/deploy-helper-update",
            "meta": {"kind": "update", "target": "auto/deploy-helper"},
            "content": "## Steps\nnew\n",
            "scripts": [],
        },
    )
    monkeypatch.setattr(
        loader,
        "preview_pending_update",
        lambda slug: None,
        raising=False,
    )
    resp = await H.api_skill_pending_detail(_Req(loader, match={"slug": "deploy-helper-update"}))
    data = _payload(resp)
    assert data["live_body"] is None
    assert data["diff"] is None
    assert data["stale_base"] is False


@pytest.mark.asyncio
async def test_new_detail_has_no_live_body(loader):
    """A plain (new) candidate detail does not gain a live_body field."""
    resp = await H.api_skill_pending_detail(_Req(loader, match={"slug": "deploy-helper"}))
    data = _payload(resp)
    assert "live_body" not in data


@pytest.mark.asyncio
async def test_approve_routes_update_to_approve_pending_update(loader, monkeypatch):
    """kind=='update' → approve_pending_update; approve_pending_skill untouched."""
    monkeypatch.setattr(
        loader,
        "get_pending_skill",
        lambda slug: {"slug": slug, "meta": {"kind": "update", "target": "auto/deploy-helper"}},
    )
    called: dict = {}

    def _upd(slug):
        called["update"] = slug
        return "auto/deploy-helper"

    def _new(slug):
        called["new"] = slug
        return "auto/should-not-run"

    monkeypatch.setattr(loader, "approve_pending_update_checked", _upd, raising=False)
    monkeypatch.setattr(loader, "approve_pending_skill_checked", _new)
    monkeypatch.setattr(loader, "run_skill_lifecycle", lambda **k: None)
    resp = await H.api_skill_pending_approve(_Req(loader, match={"slug": "deploy-helper-update"}))
    assert resp.status == 200
    assert _payload(resp)["approved"] == "auto/deploy-helper"
    assert called.get("update") == "deploy-helper-update"
    assert "new" not in called


@pytest.mark.asyncio
async def test_approve_routes_new_to_approve_pending_skill(loader, monkeypatch):
    """A candidate without kind=='update' promotes via approve_pending_skill."""
    called: dict = {}

    def _upd(slug):
        called["update"] = slug
        return "auto/should-not-run"

    monkeypatch.setattr(loader, "approve_pending_update_checked", _upd, raising=False)
    # get_pending_skill + approve_pending_skill_checked remain the real impls.
    resp = await H.api_skill_pending_approve(_Req(loader, match={"slug": "deploy-helper"}))
    assert resp.status == 200
    assert _payload(resp)["approved"] == "auto/deploy-helper"
    assert "update" not in called
    assert loader.list_pending_skills() == []


@pytest.mark.asyncio
async def test_dismiss_routes_update_candidate_by_slug(loader, monkeypatch):
    """Dismiss is kind-agnostic — it deletes the pending dir by slug."""
    seen: dict = {}

    def _dismiss(slug):
        seen["slug"] = slug
        return True

    monkeypatch.setattr(loader, "dismiss_pending_skill", _dismiss)
    resp = await H.api_skill_pending_dismiss(_Req(loader, match={"slug": "deploy-helper-update"}))
    assert resp.status == 200
    assert seen["slug"] == "deploy-helper-update"


# ── Approve refusals carry a machine-readable reason ────────────────────────
# The dashboard needs to tell "not found", "live skill exists", and "script
# validation failed" apart, so each maps to its own coded response and the
# validator's findings ride the refusal. These pin the distinct coded
# responses, the pre-approval verdict on the pending payloads, and the SEL
# outcome accuracy.

_EVIL_SCRIPT = "x = eval('1+1')\n"  # trips the validator's dynamic-exec rule


@pytest.fixture()
def flagged_loader(tmp_path):
    """A loader with one candidate whose script fails validation."""
    ld = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
    ld.stage_skill_candidate(
        "evil-helper",
        description="does bad things",
        triggers="evil",
        procedure_md="## Steps\n1. go\n",
        provenance=AutoSkillProvenance(session_key="s", created_at=AutoSkillProvenance.now_iso()),
        scripts=[{"filename": "evil.py", "content": _EVIL_SCRIPT}],
    )
    return ld


@pytest.mark.asyncio
async def test_approve_validation_failure_returns_coded_422_with_report(flagged_loader):
    resp = await H.api_skill_pending_approve(_Req(flagged_loader, match={"slug": "evil-helper"}))
    assert resp.status == 422
    data = _payload(resp)
    assert data["code"] == "script_validation_failed"
    assert "evil.py" in data["report"]
    assert any("eval" in f for f in data["report"]["evil.py"])
    # The refusal left the candidate reviewable in the queue.
    assert [p["slug"] for p in flagged_loader.list_pending_skills()] == ["evil-helper"]


@pytest.mark.asyncio
async def test_approve_live_exists_returns_coded_409(loader):
    live = loader._dir / "auto" / "deploy-helper"
    live.mkdir(parents=True)
    (live / "SKILL.md").write_text("---\nname: auto/deploy-helper\n---\nbody\n", encoding="utf-8")
    resp = await H.api_skill_pending_approve(_Req(loader, match={"slug": "deploy-helper"}))
    assert resp.status == 409
    assert _payload(resp)["code"] == "live_skill_exists"


@pytest.mark.asyncio
async def test_approve_validation_refusal_audits_rejected_not_not_found(
    flagged_loader, monkeypatch
):
    events: list[dict] = []
    monkeypatch.setattr(
        H,
        "_sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
    )
    resp = await H.api_skill_pending_approve(_Req(flagged_loader, match={"slug": "evil-helper"}))
    assert resp.status == 422
    assert events, "refusal must be SEL-audited"
    assert events[-1]["outcome"] == "rejected"
    assert events[-1]["metadata"]["reason"] == "script_validation_failed"


@pytest.mark.asyncio
@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="verdict is omitted on platforms without a descriptor-pinned walk",
)
async def test_pending_detail_carries_script_validation_verdict(loader, flagged_loader):
    clean = await H.api_skill_pending_detail(_Req(loader, match={"slug": "deploy-helper"}))
    clean_sv = _payload(clean)["script_validation"]
    assert clean_sv == {"ok": True, "report": {}}
    flagged = await H.api_skill_pending_detail(_Req(flagged_loader, match={"slug": "evil-helper"}))
    flagged_sv = _payload(flagged)["script_validation"]
    assert flagged_sv["ok"] is False
    assert any("eval" in f for f in flagged_sv["report"]["evil.py"])


@pytest.mark.asyncio
@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="verdict is omitted on platforms without a descriptor-pinned walk",
)
async def test_pending_list_carries_script_validation_verdict(flagged_loader):
    resp = await H.api_skills_pending(_Req(flagged_loader))
    (entry,) = _payload(resp)["pending"]
    assert entry["script_validation"]["ok"] is False
    assert "evil.py" in entry["script_validation"]["report"]


def test_none_wrapper_contract_preserved(flagged_loader):
    """Existing callers of the un-checked approve still get None, no raise."""
    assert flagged_loader.approve_pending_skill("evil-helper") is None
    assert flagged_loader.approve_pending_skill("does-not-exist") is None
    assert flagged_loader.approve_pending_update("does-not-exist") is None
    # The candidate is still pending and its script bytes are untouched.
    pdir = flagged_loader._pending_root() / "evil-helper"
    assert (pdir / "scripts" / "evil.py").read_text(encoding="utf-8") == _EVIL_SCRIPT


@pytest.mark.asyncio
async def test_pending_list_stray_top_level_entry_fails_verdict(loader):
    """A candidate with an unexpected top-level file must not read ``ok: true``:
    approve refuses it (_candidate_layout_ok), so the list verdict flags the
    layout — same predict-the-refusal contract as the scripts findings."""
    pdir = loader._pending_root() / "deploy-helper"
    (pdir / "extra.txt").write_text("planted\n", encoding="utf-8")
    resp = await H.api_skills_pending(_Req(loader))
    (entry,) = _payload(resp)["pending"]
    sv = entry["script_validation"]
    assert sv["ok"] is False
    assert any("unexpected candidate entry" in f for fs in sv["report"].values() for f in fs)


@pytest.mark.asyncio
async def test_pending_list_symlinked_skill_md_fails_verdict(loader, tmp_path):
    """A symlinked SKILL.md passes the exists() listing gate (it follows the
    link) but approve refuses the candidate — the verdict must flag the layout
    WITHOUT reading the link target."""
    import os

    outside = tmp_path / "outside.md"
    outside.write_text("# sekret-target\n", encoding="utf-8")
    pdir = loader._pending_root() / "deploy-helper"
    (pdir / "SKILL.md").unlink()
    os.symlink(str(outside), str(pdir / "SKILL.md"))
    resp = await H.api_skills_pending(_Req(loader))
    (entry,) = _payload(resp)["pending"]
    sv = entry["script_validation"]
    assert sv["ok"] is False
    assert "sekret-target" not in json.dumps(sv["report"])
    assert any("is a symlink" in f for fs in sv["report"].values() for f in fs)


@pytest.mark.asyncio
async def test_pending_list_symlinked_candidate_root_fails_verdict(loader, tmp_path):
    """The approve path refuses a candidate whose ROOT is a symlink
    (_candidate_has_symlink checks pdir itself); the verdict must predict that
    refusal WITHOUT scandir following the link into the target tree."""
    import os
    import shutil

    outside = tmp_path / "outside-candidate"
    pdir = loader._pending_root() / "deploy-helper"
    shutil.move(str(pdir), str(outside))
    (outside / "sekret-marker.py").write_text("x = 1\n", encoding="utf-8")
    os.symlink(str(outside), str(pdir))
    resp = await H.api_skills_pending(_Req(loader))
    (entry,) = _payload(resp)["pending"]
    sv = entry["script_validation"]
    assert sv["ok"] is False
    # The verdict names the layout problem, never the link target's contents.
    assert "sekret-marker" not in json.dumps(sv["report"])
    assert any("candidate root is" in f for fs in sv["report"].values() for f in fs)


@pytest.mark.asyncio
async def test_pending_list_directory_valued_skill_md_fails_verdict(loader):
    """Approval reads SKILL.md's bytes, so a DIRECTORY named SKILL.md is
    refused at approve time — the verdict must not read ok:true for it."""
    pdir = loader._pending_root() / "deploy-helper"
    (pdir / "SKILL.md").unlink()
    (pdir / "SKILL.md").mkdir()
    resp = await H.api_skills_pending(_Req(loader))
    (entry,) = _payload(resp)["pending"]
    sv = entry["script_validation"]
    assert sv["ok"] is False
    assert any("not a regular file" in f for fs in sv["report"].values() for f in fs)


@pytest.mark.asyncio
async def test_pending_list_symlinked_candidate_flags_layout_without_reading(loader, tmp_path):
    """A candidate-planted ``scripts`` symlink must not be traversed by the
    list path (same guard as get_pending_skill): the verdict reports the
    layout as failing WITHOUT reading the link target."""
    import os

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sekret-file.py").write_text("x = eval('1')\n", encoding="utf-8")
    pdir = loader._pending_root() / "deploy-helper"
    os.symlink(str(outside), str(pdir / "scripts"))
    resp = await H.api_skills_pending(_Req(loader))
    (entry,) = _payload(resp)["pending"]
    sv = entry["script_validation"]
    assert sv["ok"] is False
    # The verdict names the layout problem, never the link target's contents.
    assert "sekret-file.py" not in json.dumps(sv["report"])
    assert any("invalid layout" in f for fs in sv["report"].values() for f in fs)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="list-path verdict reads contents only via a descriptor-pinned walk",
)
async def test_pending_list_survives_undecodable_script(flagged_loader):
    """One non-UTF-8 script must not blank the whole pending panel: the
    candidate fails CLOSED with an unreadable-script finding (approve would
    refuse it at redaction), and the list itself keeps serving."""
    sdir = flagged_loader._pending_root() / "evil-helper" / "scripts"
    (sdir / "binary.py").write_bytes(b"\xff\xfe\x00 not utf8")
    resp = await H.api_skills_pending(_Req(flagged_loader))
    (entry,) = _payload(resp)["pending"]
    assert entry["slug"] == "evil-helper"
    sv = entry["script_validation"]
    assert sv["ok"] is False
    assert any("not valid UTF-8" in f for fs in sv["report"].values() for f in fs)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="list-path verdict reads contents only via a descriptor-pinned walk",
)
async def test_pending_list_oversized_script_flagged_from_stat_alone(loader):
    """A script over MAX_SCRIPT_BYTES is flagged from its size (stat) without
    loading its bytes on the poll path — same verdict the validator reaches."""
    from kiro_crew.skills_script_validator import MAX_SCRIPT_BYTES

    pdir = loader._pending_root() / "deploy-helper"
    sdir = pdir / "scripts"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "big.py").write_text("# pad\n" * (MAX_SCRIPT_BYTES // 6 + 10), encoding="utf-8")
    resp = await H.api_skills_pending(_Req(loader))
    (entry,) = _payload(resp)["pending"]
    sv = entry["script_validation"]
    assert sv["ok"] is False
    assert any("too large" in f for f in sv["report"]["big.py"])


@pytest.mark.asyncio
async def test_pending_list_verdict_refuses_symlink_without_trusting_precheck(
    loader, tmp_path, monkeypatch
):
    """The verdict walk must not TRUST the earlier candidate-wide symlink
    check: even when that check reports clean (simulating a swap racing in
    after it), the walk's own no-follow discipline refuses a symlinked
    ``scripts`` root and the link target is never read."""
    import os

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sekret.py").write_text("x = 1\n", encoding="utf-8")
    pdir = loader._pending_root() / "deploy-helper"
    os.symlink(str(outside), str(pdir / "scripts"))
    monkeypatch.setattr(loader, "_candidate_has_symlink", lambda p: False)
    resp = await H.api_skills_pending(_Req(loader))
    (entry,) = _payload(resp)["pending"]
    sv = entry["script_validation"]
    assert sv["ok"] is False
    assert "sekret.py" not in json.dumps(sv["report"])


@pytest.mark.asyncio
@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="list-path verdict reads contents only via a descriptor-pinned walk",
)
async def test_pending_list_verdict_bounds_file_count(loader):
    """Many small planted files must not accumulate without limit: the walk
    stops at its budget. The breach yields NO verdict (field omitted), not a
    failing one — approve's collector is unbudgeted, so a 65-clean-script
    candidate approves fine and `ok: false` would over-promise a refusal."""
    pdir = loader._pending_root() / "deploy-helper"
    sdir = pdir / "scripts"
    sdir.mkdir(parents=True, exist_ok=True)
    for i in range(70):  # over the 64-file budget, each file tiny
        (sdir / f"s{i:03d}.py").write_text("x = 1\n", encoding="utf-8")
    resp = await H.api_skills_pending(_Req(loader))
    (entry,) = _payload(resp)["pending"]
    assert "script_validation" not in entry or entry["script_validation"] is None


@pytest.mark.asyncio
@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="list-path verdict reads contents only via a descriptor-pinned walk",
)
async def test_pending_list_verdict_caps_tree_depth(loader):
    """A deeply nested planted tree must not recurse without limit (or raise
    RecursionError into the caller's degraded fallback): the walk stops at its
    depth cap. Like the entry budget, the breach yields NO verdict, not a
    false refusal claim."""
    pdir = loader._pending_root() / "deploy-helper"
    sdir = pdir / "scripts"
    deep = sdir
    for i in range(12):  # over the 8-level depth cap
        deep = deep / f"d{i}"
    deep.mkdir(parents=True)
    (deep / "leaf.py").write_text("x = 1\n", encoding="utf-8")
    resp = await H.api_skills_pending(_Req(loader))
    (entry,) = _payload(resp)["pending"]
    assert "script_validation" not in entry or entry["script_validation"] is None


# ---------------------------------------------------------------------------
# Parity property: the badge's predict-the-refusal contract.
#
# The list verdict exists to predict what the approve path will refuse. The
# example tests above each pin one shape; THIS test pins the contract itself:
# for every layout / validation refusal class the approve path can raise, the
# SAME candidate's list verdict must read ok: false. A refusal rule added to
# one side without the other fails here, instead of silently desynchronizing
# the badge from the server (a clean-badged candidate that approve refuses,
# or a flagged one that approves).
# ---------------------------------------------------------------------------


def _mutate_stray_top_level(pdir, tmp_path):
    (pdir / "extra.txt").write_text("stray\n", encoding="utf-8")
    return "invalid_layout"


def _mutate_symlinked_skill_md(pdir, tmp_path):
    import os

    outside = tmp_path / "outside.md"
    outside.write_text("# outside\n", encoding="utf-8")
    (pdir / "SKILL.md").unlink()
    os.symlink(str(outside), str(pdir / "SKILL.md"))
    return "invalid_layout"


def _mutate_scripts_regular_file(pdir, tmp_path):
    (pdir / "scripts").write_text("not a dir\n", encoding="utf-8")
    return "invalid_layout"


def _mutate_symlinked_scripts_dir(pdir, tmp_path):
    import os

    outside = tmp_path / "outside-scripts"
    outside.mkdir()
    (outside / "x.py").write_text("x = 1\n", encoding="utf-8")
    os.symlink(str(outside), str(pdir / "scripts"))
    return "invalid_layout"


def _mutate_failing_script(pdir, tmp_path):
    (pdir / "scripts").mkdir(exist_ok=True)
    (pdir / "scripts" / "evil.py").write_text(
        "import sys\nprint(eval(sys.argv[1]))\n", encoding="utf-8"
    )
    return "script_validation_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        _mutate_stray_top_level,
        _mutate_symlinked_skill_md,
        _mutate_scripts_regular_file,
        _mutate_symlinked_scripts_dir,
        _mutate_failing_script,
    ],
    ids=[
        "stray-top-level",
        "symlinked-skill-md",
        "scripts-regular-file",
        "symlinked-scripts-dir",
        "failing-script",
    ],
)
async def test_verdict_predicts_every_approve_refusal_class(loader, tmp_path, mutate):
    from kiro_crew.skills import PendingApprovalRefused

    pdir = loader._pending_root() / "deploy-helper"
    expected_reason = mutate(pdir, tmp_path)
    # First PROVE the approve path refuses this candidate (not assumed): the
    # parity claim is only meaningful against a demonstrated refusal.
    with pytest.raises(PendingApprovalRefused) as exc:
        loader.approve_pending_skill_checked("deploy-helper")
    assert exc.value.reason == expected_reason
    # ... then the badge must have predicted it: the same candidate's list
    # verdict reads ok: false.
    resp = await H.api_skills_pending(_Req(loader))
    (entry,) = _payload(resp)["pending"]
    sv = entry["script_validation"]
    assert (
        sv["ok"] is False
    ), f"verdict read clean for a candidate approve refuses ({expected_reason})"
