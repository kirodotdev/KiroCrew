"""Phase-1 tests: pending-approval + pin dashboard API handlers."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import pinned_fs
from kiro_crew import skills as skills_mod
from kiro_crew.dashboard.handlers import prompts as H
from kiro_crew.skills import AutoSkillProvenance, PendingApprovalRefused, SkillsLoader

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


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    """Run as the dashboard owner: these tests cover handler business logic.

    The owner gate on the mutating skill handlers has its own dedicated
    coverage in ``test_skill_write_guard.py``; here it would only stand
    between the test and the behavior under test.
    """
    monkeypatch.setattr(H, "is_owner_dashboard_request", lambda _request: True, raising=False)


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
async def test_pending_detail_carries_script_validation_verdict(
    loader, flagged_loader, monkeypatch
):
    """The detail payload contract, exercised on EVERY platform.

    On a host with the pinned machinery the real verdict rides the response.
    On a host without it the field must be OMITTED — and the payload plumbing
    is still covered by mocking the capability-dependent verdict, so a
    non-pinned CI host keeps these contract assertions in collection instead
    of skipping them (the ratchet GPT flagged).
    """
    if pinned_fs.supports_pinned_walk() and pinned_fs.supports_pinned_tree_walk():
        clean = await H.api_skill_pending_detail(_Req(loader, match={"slug": "deploy-helper"}))
        clean_sv = _payload(clean)["script_validation"]
        assert clean_sv == {"ok": True, "report": {}}
        flagged = await H.api_skill_pending_detail(
            _Req(flagged_loader, match={"slug": "evil-helper"})
        )
        flagged_sv = _payload(flagged)["script_validation"]
        assert flagged_sv["ok"] is False
        assert any("eval" in f for f in flagged_sv["report"]["evil.py"])
        return
    # Negative contract on this host: omitted, never false.
    detail = await H.api_skill_pending_detail(_Req(flagged_loader, match={"slug": "evil-helper"}))
    assert "script_validation" not in _payload(detail)
    # Positive payload plumbing, capability-dependent walk mocked.
    monkeypatch.setattr(
        type(flagged_loader),
        "_pending_scripts_verdict",
        lambda self, pdir: (False, {"evil.py": ["banned call: eval"]}),
    )
    mocked = await H.api_skill_pending_detail(_Req(flagged_loader, match={"slug": "evil-helper"}))
    sv = _payload(mocked)["script_validation"]
    assert sv["ok"] is False
    assert any("eval" in f for f in sv["report"]["evil.py"])


@pytest.mark.asyncio
async def test_pending_list_carries_script_validation_verdict(flagged_loader, monkeypatch):
    """The list payload contract, exercised on EVERY platform (see detail twin)."""
    if pinned_fs.supports_pinned_walk() and pinned_fs.supports_pinned_tree_walk():
        resp = await H.api_skills_pending(_Req(flagged_loader))
        (entry,) = _payload(resp)["pending"]
        assert entry["script_validation"]["ok"] is False
        assert "evil.py" in entry["script_validation"]["report"]
        return
    resp = await H.api_skills_pending(_Req(flagged_loader))
    (entry,) = _payload(resp)["pending"]
    assert "script_validation" not in entry
    monkeypatch.setattr(
        type(flagged_loader),
        "_pending_scripts_verdict",
        lambda self, pdir: (False, {"evil.py": ["banned call: eval"]}),
    )
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
@pytest.mark.skipif(not pinned_fs.supports_pinned_walk(), reason="no pinned layout verdict")
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
@pytest.mark.skipif(not pinned_fs.supports_pinned_walk(), reason="no pinned layout verdict")
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
@pytest.mark.skipif(not pinned_fs.supports_pinned_walk(), reason="no pinned layout verdict")
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
@pytest.mark.parametrize("root_is_junction", [False, True], ids=["swappable-root", "junction"])
async def test_pending_list_non_pinned_omits_verdict_without_scanning(
    loader, monkeypatch, root_is_junction
):
    """No root scan, even when a pre-scan link check would report a real directory.

    A negative link check cannot prevent a subsequent junction swap. Decline
    the verdict without scanning, rather than reporting target entry names.
    """
    import os

    pdir = loader._pending_root() / "deploy-helper"
    (pdir / "sekret-junction-target.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
    real_is_link_or_junction = skills_mod.is_link_or_junction
    monkeypatch.setattr(
        skills_mod,
        "is_link_or_junction",
        lambda p: (
            root_is_junction if os.fspath(p) == os.fspath(pdir) else real_is_link_or_junction(p)
        ),
    )
    assert not os.path.islink(pdir), "the simulated junction must be invisible to islink"
    real_scandir = os.scandir
    scans = []

    def track_scandir(path):
        if not isinstance(path, int) and os.fspath(path) == os.fspath(pdir):
            scans.append(path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", track_scandir)
    verdict = loader._pending_scripts_verdict(pdir)
    assert not scans, "non-pinned verdict scanned the candidate root by name"
    assert verdict is None
    resp = await H.api_skills_pending(_Req(loader))
    (entry,) = _payload(resp)["pending"]
    assert not scans, "pending-list handler scanned the candidate root by name"
    assert "script_validation" not in entry
    assert "sekret-junction-target" not in json.dumps(entry)


@pytest.mark.parametrize("candidate_layout", ["no-scripts", "scripts", "invalid-layout"])
def test_non_pinned_verdict_returns_before_any_candidate_inspection(
    loader, monkeypatch, candidate_layout
):
    """Windows loses the badge entirely, not only for already-detected links."""
    import os

    pdir = loader._pending_root() / "deploy-helper"
    if candidate_layout == "scripts":
        (pdir / "scripts").mkdir()
        (pdir / "scripts" / "clean.py").write_text("x = 1\n", encoding="utf-8")
    elif candidate_layout == "invalid-layout":
        (pdir / "extra.txt").write_text("planted\n", encoding="utf-8")
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)

    def unexpected_inspection(*args, **kwargs):
        pytest.fail("non-pinned verdict attempted candidate inspection")

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "scandir", unexpected_inspection)
        scoped.setattr(os, "lstat", unexpected_inspection)
        scoped.setattr(skills_mod, "is_link_or_junction", unexpected_inspection)
        assert loader._pending_scripts_verdict(pdir) is None


@pytest.mark.asyncio
@pytest.mark.skipif(not pinned_fs.supports_pinned_walk(), reason="no pinned layout verdict")
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
@pytest.mark.skipif(not pinned_fs.supports_pinned_walk(), reason="no pinned layout verdict")
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


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(),
    reason="the verdict is declined without pinned opens, so the field is absent",
)
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


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(),
    reason="the verdict is declined without pinned opens, so the field is absent",
)
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


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(),
    reason="the verdict is declined without pinned opens, so the field is absent",
)
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


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(),
    reason="the verdict is declined without pinned opens, so the field is absent",
)
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


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(),
    reason="the verdict is declined without pinned opens, so the field is absent",
)
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
#
# The mutation list below is hand-written, so on its own it can only cover
# the refusal classes someone remembered to list. The vocabulary pin
# (``test_approve_refusal_vocabulary_is_fully_classified``) closes that seam:
# it reads the reason vocabulary out of the SOURCE (every literal the approve
# path passes to ``PendingApprovalRefused``) and fails on any reason that is
# neither exercised by a mutation here nor explicitly classified as not
# predictable from a poll. A new refusal class therefore cannot ship an
# under-predicting badge silently — it fails until someone classifies it.
# ---------------------------------------------------------------------------


def _predicts(reason):
    """Tag a parity mutation with the approve refusal reason it provokes."""

    def _tag(fn):
        fn.predicts = reason
        return fn

    return _tag


@_predicts("invalid_layout")
def _mutate_stray_top_level(pdir, tmp_path):
    (pdir / "extra.txt").write_text("stray\n", encoding="utf-8")


@_predicts("invalid_layout")
def _mutate_symlinked_skill_md(pdir, tmp_path):
    import os

    outside = tmp_path / "outside.md"
    outside.write_text("# outside\n", encoding="utf-8")
    (pdir / "SKILL.md").unlink()
    os.symlink(str(outside), str(pdir / "SKILL.md"))


@_predicts("invalid_layout")
def _mutate_scripts_regular_file(pdir, tmp_path):
    (pdir / "scripts").write_text("not a dir\n", encoding="utf-8")


@_predicts("invalid_layout")
def _mutate_symlinked_scripts_dir(pdir, tmp_path):
    import os

    outside = tmp_path / "outside-scripts"
    outside.mkdir()
    (outside / "x.py").write_text("x = 1\n", encoding="utf-8")
    os.symlink(str(outside), str(pdir / "scripts"))


@_predicts("script_validation_failed")
def _mutate_failing_script(pdir, tmp_path):
    (pdir / "scripts").mkdir(exist_ok=True)
    (pdir / "scripts" / "evil.py").write_text(
        "import sys\nprint(eval(sys.argv[1]))\n", encoding="utf-8"
    )


_PARITY_MUTATIONS = [
    _mutate_stray_top_level,
    _mutate_symlinked_skill_md,
    _mutate_scripts_regular_file,
    _mutate_symlinked_scripts_dir,
    _mutate_failing_script,
]

# Approve refusal reasons a poll-time verdict CANNOT predict, each with the
# reason it cannot. Every key must be a reason the approve path actually
# raises (the vocabulary pin fails on a stale or invented key), and a reason
# may not be both exercised above and listed here. Adding a reason to the
# approve path without a mutation above forces an entry here — with a
# justification — before the suite goes green again.
_UNPREDICTABLE_APPROVE_REFUSALS: dict[str, str] = {
    "not_found": (
        "no candidate to inspect: the slug is unsafe, has no SKILL.md, or is the "
        "wrong kind for the approve variant called. A listed row always exists "
        "at poll time, so this only arises from a poll→click race or a wrong "
        "variant — not from anything in the candidate's own layout or scripts."
    ),
    "live_exists": (
        "a live skill already holds the name. That is the state of the LIVE "
        "namespace at click time, not a property of the candidate's layout or "
        "scripts, which is all the verdict inspects."
    ),
    "kind_mismatch": (
        "an update candidate reached the new-skill approve variant. Which "
        "variant a caller invokes is a request-routing fact, not a candidate "
        "property: the candidate is perfectly approvable via its own path, so "
        "an ok:false verdict would be a false refusal prediction for the "
        "click the badge actually fronts."
    ),
    "target_missing": (
        "update path only: the candidate's .meta.json names no live target, or "
        "the live target auto-skill is gone. A live-namespace lookup, not a "
        "candidate scripts finding."
    ),
    "stale_base": (
        "update path only: the live target has moved past the version the "
        "candidate was merged against. A live-vs-candidate version comparison "
        "made at click time, not a candidate scripts finding."
    ),
    "redaction_failed": (
        "the in-place redaction of SKILL.md / a script could not read or write "
        "the file. The write half is click-time I/O a read-only poll cannot "
        "observe; the unreadable-script half IS already surfaced by the verdict "
        "as a finding, and SKILL.md content is outside the scripts verdict's "
        "remit."
    ),
    "promotion_failed": (
        "an OS-level read/write/move failure AFTER every check passed. "
        "Click-time I/O that a read-only poll cannot observe."
    ),
}


def _approve_refusal_reasons_from_source() -> set[str]:
    """Every reason the approve path can raise, read from the SOURCE.

    Walks the AST of every module in the ``kiro_crew`` package that mentions
    ``PendingApprovalRefused(`` and collects the ``reason`` literal of each
    construction. This is the ground truth the classification is checked
    against — not a second hand-written list that could drift the same way
    the mutation list can. A construction whose reason is not a string
    literal is a hard failure: it cannot be classified, so the pin cannot
    vouch for it.
    """
    pkg_root = Path(skills_mod.__file__).resolve().parent
    reasons: set[str] = set()
    for py in sorted(pkg_root.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        if "PendingApprovalRefused(" not in text:
            continue
        for node in ast.walk(ast.parse(text, filename=str(py))):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            callee = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if callee != "PendingApprovalRefused":
                continue
            arg = None
            if node.args:
                arg = node.args[0]
            else:
                for kw in node.keywords:
                    if kw.arg == "reason":
                        arg = kw.value
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                pytest.fail(
                    f"{py.relative_to(pkg_root)}:{node.lineno}: PendingApprovalRefused "
                    "constructed with a non-literal reason — the parity pin cannot "
                    "classify it; pass a string literal"
                )
            reasons.add(arg.value)
    return reasons


def test_approve_refusal_vocabulary_is_fully_classified():
    """Drift pin: a new approve refusal reason fails until it is classified.

    The source vocabulary must equal ``exercised ∪ unpredictable`` exactly —
    an unlisted new reason fails (it would ship an under-predicting badge
    silently), a classified reason the approve path does not raise fails
    (stale classification), and a reason may not sit on both sides.
    """
    source_reasons = _approve_refusal_reasons_from_source()
    exercised = {m.predicts for m in _PARITY_MUTATIONS}
    unpredictable = set(_UNPREDICTABLE_APPROVE_REFUSALS)
    # Coherence check on the scan itself: it must at least have found the
    # classes the parametrized parity test demonstrably provokes.
    assert exercised <= source_reasons, sorted(exercised - source_reasons)
    both = exercised & unpredictable
    assert not both, f"classified as unpredictable but exercised by a mutation: {sorted(both)}"
    unclassified = source_reasons - exercised - unpredictable
    assert not unclassified, (
        f"approve path can raise {sorted(unclassified)} but no parity mutation exercises "
        "them and they are not classified as unpredictable — add a _PARITY_MUTATIONS "
        "case, or an entry in _UNPREDICTABLE_APPROVE_REFUSALS stating WHY a poll cannot "
        "predict it"
    )
    stale = unpredictable - source_reasons
    assert not stale, f"classified reasons the approve path does not raise: {sorted(stale)}"
    for reason, why in _UNPREDICTABLE_APPROVE_REFUSALS.items():
        assert why.strip(), f"{reason}: unpredictable classification must state why"
    # The exception's docstring is the user-facing vocabulary; keep it honest.
    doc = PendingApprovalRefused.__doc__ or ""
    for reason in sorted(source_reasons):
        assert f"``{reason}``" in doc, f"PendingApprovalRefused docstring omits {reason!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    _PARITY_MUTATIONS,
    ids=[
        "stray-top-level",
        "symlinked-skill-md",
        "scripts-regular-file",
        "symlinked-scripts-dir",
        "failing-script",
    ],
)
@pytest.mark.parametrize("without_pinned_walk", [False, True], ids=["native", "non-pinned"])
async def test_verdict_predicts_every_approve_refusal_class(
    loader, tmp_path, monkeypatch, mutate, without_pinned_walk
):
    if without_pinned_walk:
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
    pdir = loader._pending_root() / "deploy-helper"
    mutate(pdir, tmp_path)
    expected_reason = mutate.predicts
    # First PROVE the approve path refuses this candidate (not assumed): the
    # parity claim is only meaningful against a demonstrated refusal.
    with pytest.raises(PendingApprovalRefused) as exc:
        loader.approve_pending_skill_checked("deploy-helper")
    assert exc.value.reason == expected_reason
    # ... then the badge must have predicted it: the same candidate's list
    # verdict reads ok: false.
    resp = await H.api_skills_pending(_Req(loader))
    (entry,) = _payload(resp)["pending"]
    if not pinned_fs.supports_pinned_walk():
        # Windows has no trustworthy layout OR content verdict. The pre-click
        # badge disappears entirely; approve's refusal above is unchanged.
        assert "script_validation" not in entry
        return
    if "script_validation" not in entry:
        # With pinned opens but no tree walk, only content-stage refusals may
        # be omitted. Pinned layout refusals must still be predicted.
        assert (
            not pinned_fs.supports_pinned_tree_walk()
        ), "verdict missing on a platform that supports the pinned walk"
        assert (
            expected_reason == "script_validation_failed"
        ), f"layout refusal {expected_reason!r} must carry a verdict with pinned opens"
        return
    sv = entry["script_validation"]
    assert (
        sv["ok"] is False
    ), f"verdict read clean for a candidate approve refuses ({expected_reason})"


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(),
    reason="the verdict is declined without pinned opens, so the field is absent",
)
@pytest.mark.asyncio
@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="list-path verdict reads contents only via a descriptor-pinned walk",
)
async def test_verdict_refuses_entry_swapped_after_stat(loader, monkeypatch):
    """The FIFO-swap TOCTOU: a name that stat described as a regular file but
    that OPENS as something else (swapped between stat and open) must fail the
    verdict closed via the fstat identity check — and must not hang, which is
    what O_NONBLOCK guarantees for a writerless FIFO. The lying-stat
    monkeypatch simulates the winnable race deterministically."""
    import os as _os

    pdir = loader._pending_root() / "deploy-helper"
    sdir = pdir / "scripts"
    sdir.mkdir(parents=True, exist_ok=True)
    _os.mkfifo(str(sdir / "evil.py"))
    # A stat that LIES: report the FIFO as the regular SKILL.md inode, the way
    # the racing adversary's pre-swap file would have been described.
    real_stat = _os.stat
    skill_st = _os.lstat(pdir / "SKILL.md")

    def lying_stat(path, *args, **kwargs):
        if kwargs.get("dir_fd") is not None and path == "evil.py":
            return skill_st
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(_os, "stat", lying_stat)
    # Replacing os.stat removes it from os.supports_dir_fd, which would make
    # the capability probe honestly report "cannot pin" and omit the verdict —
    # the capability is real on this host, so pin the probe, not the answer.
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: True)
    resp = await H.api_skills_pending(_Req(loader))
    (entry,) = _payload(resp)["pending"]
    sv = entry["script_validation"]
    assert sv["ok"] is False
    assert any("changed during scan" in f for fs in sv["report"].values() for f in fs)


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(),
    reason="the verdict is declined without pinned opens, so the field is absent",
)
@pytest.mark.asyncio
@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="list-path verdict reads contents only via a descriptor-pinned walk",
)
async def test_verdict_flags_redaction_breaking_script(loader, monkeypatch):
    """Approve validates twice — raw, then the redacted result — and refuses a
    script whose redaction breaks its syntax. The verdict must predict that
    refusal instead of reading clean on the raw pass alone."""
    pdir = loader._pending_root() / "deploy-helper"
    sdir = pdir / "scripts"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "run.py").write_text("import json\nprint(json.dumps({'a': 1}))\n", encoding="utf-8")

    orig = loader._redact_text

    def corrupting_redact(text):
        if isinstance(text, str) and "json.dumps" in text:
            return "def (:\n"  # redaction broke the syntax
        return orig(text)

    monkeypatch.setattr(loader, "_redact_text", corrupting_redact)
    resp = await H.api_skills_pending(_Req(loader))
    (entry,) = _payload(resp)["pending"]
    sv = entry["script_validation"]
    assert sv["ok"] is False


def _truncation_message(entries=0, findings=0, chars=0):
    return [
        "too large: validation report truncated; "
        f"omitted {entries} script entries, "
        f"{findings} findings from retained entries, "
        f"{chars} characters from retained strings"
    ]


def test_validation_report_entry_bound(loader):
    cap = skills_mod._PENDING_SCRIPT_MAX_ENTRIES
    # The healthy boundary retains ALL entries without a truncation warning.
    healthy = {f"s{i}.py": ["invalid layout: example"] for i in range(cap)}
    assert loader._redact_validation_report(healthy) == healthy
    crowded = {**healthy, **{f"overflow{i}.py": ["omitted"] for i in range(7)}}
    bounded = loader._redact_validation_report(crowded)
    assert len(bounded) == cap + 1  # script population + one fixed summary
    assert {k: v for k, v in bounded.items() if k != "<truncated>"} == healthy
    assert bounded["<truncated>"] == _truncation_message(entries=7)


@pytest.mark.parametrize("field", ["filename", "finding"])
def test_validation_report_string_bound(loader, field):
    cap = skills_mod._VALIDATION_REPORT_MAX_STRING_CHARS
    for excess in (0, 13):
        text = "z" * (cap + excess)
        report = {text: ["invalid layout: example"]} if field == "filename" else {"a.py": [text]}
        bounded = loader._redact_validation_report(report)
        expected = (
            {"z" * cap: ["invalid layout: example"]}
            if field == "filename"
            else {"a.py": ["z" * cap]}
        )
        if excess:
            expected["<truncated>"] = _truncation_message(chars=excess)
        assert bounded == expected
        assert all(len(k) <= cap for k in bounded)
        assert all(len(v) <= cap for findings in bounded.values() for v in findings)


def test_validation_report_finding_bound(loader):
    cap = skills_mod._VALIDATION_REPORT_MAX_FINDINGS
    healthy = {"a.py": [f"invalid layout: issue {i}" for i in range(cap)]}
    assert loader._redact_validation_report(healthy) == healthy
    bounded = loader._redact_validation_report({"a.py": healthy["a.py"] + ["extra"] * 9})
    assert bounded["a.py"] == healthy["a.py"]
    assert len(bounded["a.py"]) == cap
    assert bounded["<truncated>"] == _truncation_message(findings=9)


def test_validation_report_counts_all_truncation_once(loader):
    entries = skills_mod._PENDING_SCRIPT_MAX_ENTRIES
    findings = skills_mod._VALIDATION_REPORT_MAX_FINDINGS
    chars = skills_mod._VALIDATION_REPORT_MAX_STRING_CHARS
    # Two shortened names collide, and a third name tries to steal the fixed
    # summary slot. Neither may overwrite retained findings or hide an omission.
    report = {
        "z" * (chars + 2): ["x" * (chars + 3)] * (findings + 5),
        "z" * (chars + 4): ["must not overwrite"],
        "<truncated>": ["must not replace the summary"],
        **{f"s{i}.py": ["invalid layout: example"] for i in range(entries)},
    }
    bounded = loader._redact_validation_report(report)
    assert bounded["z" * chars] == ["x" * chars] * findings
    assert bounded["<truncated>"] == _truncation_message(
        entries=5, findings=5, chars=2 + 3 * findings
    )
    assert sum("validation report truncated;" in f for fs in bounded.values() for f in fs) == 1
    assert len(bounded) == entries - 1  # two collisions + one summary


def test_validation_report_redacts_before_shortening(loader):
    cap = skills_mod._VALIDATION_REPORT_MAX_STRING_CHARS
    # Put a credential across the cut: shortening first leaks its prefix.
    text = "z" * (cap - 5) + " AKIAIOSFODNN7EXAMPLE"
    bounded = loader._redact_validation_report({text: [text]})
    assert "AKIA" not in json.dumps(bounded)
    key = next(iter(bounded))
    assert key == loader._redact_text(text)[:cap]
    assert bounded[key] == [key]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["approve", "detail", "list"])
async def test_validation_report_bounded_on_every_http_surface(
    flagged_loader, monkeypatch, surface
):
    cap = skills_mod._PENDING_SCRIPT_MAX_ENTRIES
    report = {f"s{i}.py": ["invalid layout: example"] for i in range(cap + 7)}
    monkeypatch.setattr(skills_mod, "validate_scripts", lambda _scripts: (False, report))
    monkeypatch.setattr(flagged_loader, "_pending_scripts_verdict", lambda _pdir: (False, report))
    request = _Req(flagged_loader, match={"slug": "evil-helper"})
    if surface == "approve":
        response = await H.api_skill_pending_approve(request)
        assert response.status == 422
        actual = _payload(response)["report"]
    elif surface == "detail":
        response = await H.api_skill_pending_detail(request)
        assert response.status == 200
        actual = _payload(response)["script_validation"]["report"]
    else:
        response = await H.api_skills_pending(request)
        assert response.status == 200
        actual = _payload(response)["pending"][0]["script_validation"]["report"]
    assert len(actual) == cap + 1
    assert actual["<truncated>"] == _truncation_message(entries=7)


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="verdict omitted without a descriptor-pinned walk",
)
def test_validation_report_and_verdict_share_entry_budget(loader, monkeypatch):
    # Change the ONE population budget, not either consumer. Both must follow
    # it, with no independent literal drifting back into the verdict walk.
    monkeypatch.setattr(skills_mod, "_PENDING_SCRIPT_MAX_ENTRIES", 3)
    pdir = loader._pending_root() / "deploy-helper"
    sdir = pdir / "scripts"
    sdir.mkdir()
    for i in range(3):
        (sdir / f"s{i}.py").write_text("x = 1\n", encoding="utf-8")
    assert loader._pending_scripts_verdict(pdir) == (True, {})
    (sdir / "extra.py").write_text("x = 1\n", encoding="utf-8")
    assert loader._pending_scripts_verdict(pdir) is None
    bounded = loader._redact_validation_report({f"s{i}.py": ["finding"] for i in range(4)})
    assert len(bounded) == 4
    assert bounded["<truncated>"] == _truncation_message(entries=1)


def test_new_path_refuses_update_candidate_kind_mismatch(loader):
    """The new-skill path must refuse a candidate whose metadata marks it an update.

    The HTTP handler routes on the candidate detail's ``kind``; a raising
    detail read drops that to ``None`` and defaults to THIS path, which would
    promote the update fresh under ``auto/<candidate-slug>`` while its live
    target stays unchanged. The guard lives at the consumption point, so the
    handler's default can never mis-route regardless of why the read failed.
    """
    pdir = loader._pending_root() / "deploy-helper"
    (pdir / ".meta.json").write_text(
        json.dumps({"kind": "update", "target": "auto/deploy-helper", "base_version": 1}),
        encoding="utf-8",
    )
    with pytest.raises(PendingApprovalRefused) as exc:
        loader.approve_pending_skill_checked("deploy-helper")
    assert exc.value.reason == "kind_mismatch"
    # Refused, not consumed: the candidate is still pending, nothing went live.
    assert (pdir / "SKILL.md").exists()
    assert not (loader._dir / "auto" / "deploy-helper").exists()


def test_approve_refuses_when_the_badge_verdict_fails(loader, monkeypatch):
    """One verdict, two surfaces: approve consults the verdict the badge serves.

    A computable ``ok: false`` verdict is a refusal by construction — badge
    and click cannot disagree. Deleting the consult makes this test fail: the
    candidate is clean on disk, so only the consult can carry the mocked
    verdict's findings into the refusal.
    """
    monkeypatch.setattr(
        type(loader),
        "_pending_scripts_verdict",
        lambda self, pdir: (False, {"scripts/x.py": ["banned call: eval"]}),
    )
    with pytest.raises(PendingApprovalRefused) as exc:
        loader.approve_pending_skill_checked("deploy-helper")
    assert exc.value.reason == "script_validation_failed"
    assert "scripts/x.py" in (exc.value.report or {})
    # The refusal left the candidate pending and untouched.
    assert (loader._pending_root() / "deploy-helper" / "SKILL.md").exists()
    assert not (loader._dir / "auto" / "deploy-helper").exists()


def test_approve_keeps_its_own_authority_without_a_verdict(loader, monkeypatch):
    """A ``None`` verdict (no pinned opens, or a breached budget) never vetoes.

    The authority split the verdict's contract documents: where no verdict is
    computable the click path's own machinery decides alone — a platform
    without pinned opens still approves a clean candidate.
    """
    monkeypatch.setattr(type(loader), "_pending_scripts_verdict", lambda self, pdir: None)
    assert loader.approve_pending_skill_checked("deploy-helper") == "auto/deploy-helper"


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(),
    reason="the single-resolution invariant only exists where pinned opens do",
)
def test_verdict_resolves_the_candidate_root_exactly_once(loader, monkeypatch):
    """The whole verdict hangs off ONE pinned resolution of the candidate root.

    The layout scan and the scripts walk must share that retained descriptor —
    a second `open_dir_pinned` call (the old shape re-resolved `pdir/scripts`
    by path) reopens the TOCTOU where a real directory renamed over the
    candidate root between the two resolutions redirects the walk into
    another tree. `O_NOFOLLOW` cannot catch that swap: a real directory is
    not a symlink. So the pin is structural: exactly one path resolution per
    verdict, addressed at the candidate root, never at anything below it.
    """
    calls: list[str] = []
    real_open = pinned_fs.open_dir_pinned

    def _recording_open(path, *a, **k):
        calls.append(str(path))
        return real_open(path, *a, **k)

    monkeypatch.setattr(pinned_fs, "open_dir_pinned", _recording_open)
    pdir = loader._pending_root() / "deploy-helper"
    verdict = loader._pending_scripts_verdict(pdir)
    assert verdict is not None and verdict[0] is True
    assert calls == [str(pdir)], (
        f"expected exactly one pinned resolution of the candidate root, got {calls} — "
        "anything below the root must be opened dir_fd-relative to the retained descriptor"
    )
