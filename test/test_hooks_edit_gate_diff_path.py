"""The hook edit gate judges the diff content block's path too.

Two gates judge a file edit. The always-enforced tier
(``llm_helpers._edit_target_denial``) judges the UNION of the params' path
spellings and the path the tool_call's ``{"type": "diff"}`` content block named
(``event.diff_path``), and denies an empty union. The parallel gate in
``hooks.on_tool_call`` — the ``tool_kind == "edit"`` branch — must judge the
SAME union: an edit whose target is named only in the diff block would
otherwise be invisible to it, and an empty target set would pass unjudged.
These tests pin that parity: both gates consume
``platform.tool_paths.edit_target_candidates``, the hook denies an empty union
within the edit branch, a relative (unanchored) diff-block path is denied as
unverifiable in both tiers, and the deliberately unmirrored empty-kind read
allowance (hooks.py's write-only tier comment) stays allowed.
"""

from __future__ import annotations

import pytest

from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig

#: In the write-only tier: reads pass the sensitive-path keystone, edits are
#: denied by the write-protected branch. This is what makes the read-allowance
#: regression guard meaningful — a read+write floor path would be denied by the
#: keystone regardless of the branch under test.
_WRITE_ONLY = "~/.kiro/crew/config.json"


def _gate() -> HookManager:
    return HookManager(HooksConfig.from_dict({}))


def _call(
    *,
    tool_kind: str = "edit",
    raw_params: dict | None = None,
    diff_path: str = "",
):
    return _gate().on_tool_call(
        "Editing the notes",
        session_key="cli_chat",
        tool_kind=tool_kind,
        raw_params=raw_params,
        diff_path=diff_path,
    )


class TestTheDiffBlockPathIsAHookTarget:
    def test_protected_path_named_only_by_the_diff_block_is_denied(self) -> None:
        # The fs_write shape a backend may stream: trusted params that carry no
        # path key at all, the file named only in the diff content block.
        decision = _call(
            raw_params={"command": "create", "fileText": "x"},
            diff_path=_WRITE_ONLY,
        )
        assert decision.action == TOOL_DENY, (
            "an edit naming its write-protected target only in the diff content "
            "block passed the hook gate unjudged"
        )
        assert "config.json" in decision.reason

    def test_both_sources_are_judged(self) -> None:
        decision = _call(
            raw_params={"path": "/tmp/ok.md"},
            diff_path="~/.kiro/agents/pwn.json",
        )
        assert decision.action == TOOL_DENY
        assert "pwn.json" in decision.reason

    def test_diff_block_only_edit_with_no_params_is_still_judged(self) -> None:
        # raw_params can be absent entirely on the permission path; the diff
        # block alone must still reach the write-protected tier.
        decision = _call(raw_params=None, diff_path=_WRITE_ONLY)
        assert decision.action == TOOL_DENY

    def test_safe_diff_block_path_is_not_denied(self) -> None:
        decision = _call(raw_params=None, diff_path="/tmp/notes.md")
        assert decision.action != TOOL_DENY

    def test_ordinary_params_named_edit_still_passes(self) -> None:
        decision = _call(raw_params={"path": "/tmp/notes.md"})
        assert decision.action != TOOL_DENY


class TestAnEmptyUnionIsDenied:
    def test_edit_naming_no_target_is_denied(self) -> None:
        # The always-enforced tier's precedent: a declared file edit whose
        # params and content block together name no target has no proven
        # target to judge, and is denied rather than approved blind.
        decision = _call(raw_params={"command": "create", "fileText": "x"})
        assert decision.action == TOOL_DENY
        assert "no target path" in decision.reason

    def test_an_empty_params_dict_is_denied_not_skipped(self) -> None:
        # {} is present but falsy: a truthiness guard would skip the gate
        # entirely (the falsy-guard fail-open class), while the always-enforced
        # tier selects ANY dict via isinstance and denies its empty union. The
        # hook branch must enter on `is not None`, not truthiness.
        decision = _call(raw_params={})
        assert decision.action == TOOL_DENY, (
            "an edit-kind call with raw_params={} skipped the write-protected "
            "tier instead of being denied on its empty union"
        )
        assert "no target path" in decision.reason

    def test_an_edit_with_neither_params_nor_diff_block_falls_through(self) -> None:
        # Mirrors llm_helpers: an edit with no params at all never reaches
        # _edit_target_denial and keeps the document scan. The hook branch has
        # nothing to judge and must not hard-deny what other tiers still cover
        # (the permission path commonly carries raw_tool_params=None).
        decision = _call(raw_params=None, diff_path="")
        assert decision.action != TOOL_DENY


class TestTheReadAllowanceIsNotRegressed:
    """hooks.py's empty/unknown ``tool_kind`` case is DELIBERATELY not mirrored
    (the write-only tier exists so config READS stay allowed). Regression guard
    for the comment block above the edit branch."""

    def test_empty_kind_read_of_config_stays_allowed(self) -> None:
        decision = _call(tool_kind="", raw_params={"path": _WRITE_ONLY})
        assert decision.action != TOOL_DENY, (
            "the empty-kind read allowance regressed: a config READ arriving "
            "without a kind was denied by the write-only tier"
        )

    def test_read_kind_of_config_stays_allowed(self) -> None:
        decision = _call(tool_kind="read", raw_params={"path": _WRITE_ONLY})
        assert decision.action != TOOL_DENY


class TestADiffBlockRoutesOntoTheWritePlane:
    """The diff content block is the edit's target of record: its PRESENCE is
    what routes a call onto the write plane, because the diff_path cache is
    written only when a tool_call frame declares a file change — no legitimate
    non-edit call carries one. The spec-optional, agent-influenced ``kind``
    field is one of two edit signals, never the gate: a kindless (or
    read-labelled) call carrying a diff block is judged as an edit, while the
    read allowance stays keyed on the ABSENCE of a diff block."""

    def test_a_kindless_call_carrying_a_diff_block_is_judged_as_an_edit(self) -> None:
        decision = _call(tool_kind="", raw_params=None, diff_path=_WRITE_ONLY)
        assert decision.action == TOOL_DENY, (
            "a kindless call whose diff block names write-protected config "
            "skipped the write tier — the kind field gated the write plane"
        )
        assert "config.json" in decision.reason

    def test_a_read_labelled_call_carrying_a_diff_block_is_judged_as_an_edit(self) -> None:
        decision = _call(
            tool_kind="read",
            raw_params={"command": "create", "fileText": "x"},
            diff_path=_WRITE_ONLY,
        )
        assert decision.action == TOOL_DENY

    def test_a_kindless_diff_block_call_with_a_safe_target_passes(self) -> None:
        decision = _call(tool_kind="", raw_params=None, diff_path="/tmp/notes.md")
        assert decision.action != TOOL_DENY

    def test_the_always_enforced_tier_routes_on_the_diff_block_too(self) -> None:
        from kiro_crew.platform.tool_paths import is_edit_call

        assert is_edit_call("", _WRITE_ONLY) is True
        assert is_edit_call("read", _WRITE_ONLY) is True
        assert is_edit_call("edit", "") is True
        assert is_edit_call("", "") is False
        assert is_edit_call("read", "") is False

    def test_governance_classifies_a_kindless_diff_block_call_as_a_write(self) -> None:
        from kiro_crew.platform.governance import classify_tool_args

        pairs = classify_tool_args("", None, diff_path="/tmp/outside.md")
        assert ("filesystem.write", "/tmp/outside.md") in pairs
        # The kindless shape-inference fallback applied BOTH ceilings; routing
        # by the diff block keeps the read pairs so no call loses one.
        assert ("filesystem.read", "/tmp/outside.md") in pairs


class TestTheClientEventDrivesTheHookGate:
    """End to end through ``acp._dispatch``, the way
    ``test_llm_helpers_edit_gate.py::TestTheClientCarriesTheDiffPathOntoThePermissionEvent``
    pins the always-enforced tier: a tool_call frame whose diff block names a
    protected path, followed by the permission frame, produces an event whose
    fields — handed to ``hooks.on_tool_call`` exactly as the channel dispatchers
    hand them — are denied by the hook gate."""

    def _event(self, diff_path: str):
        from kiro_crew.acp import _dispatch
        from kiro_crew.acp.types import JsonRpcMessage

        caches = dict(
            tool_input_cache={},
            shell_cache={},
            raw_params_cache={},
            diff_path_cache={},
            cache_scope="sess-1",
        )
        _dispatch.parse_session_update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tc-1",
                "title": "Editing",
                "kind": "edit",
                "rawInput": {"command": "create", "fileText": "x"},
                "content": [
                    {
                        "type": "diff",
                        "path": diff_path,
                        "oldText": None,
                        "newText": "x",
                    }
                ],
            },
            **caches,
        )
        msg = JsonRpcMessage(
            id="req-1",
            method="session/request_permission",
            params={
                "sessionId": "sess-1",
                "toolCall": {"toolCallId": "tc-1", "title": "Editing", "kind": "edit"},
                "options": [{"optionId": "allow_once", "kind": "allow_once"}],
            },
        )
        event, _ = _dispatch.build_permission_event(msg, **caches)
        return event

    @pytest.mark.parametrize("path", ["~/.kiro/agents/pwn.json", "~/.kiro/crew/config.json"])
    def test_diff_block_protected_path_reaches_the_hook_deny(self, path: str) -> None:
        event = self._event(path)
        assert event.diff_path == path
        decision = _gate().on_tool_call(
            event.title,
            session_key="cli_chat",
            tool_kind=event.tool_kind,
            raw_params=event.raw_tool_params,
            diff_path=event.diff_path,
        )
        assert decision.action == TOOL_DENY

    def test_safe_diff_block_path_reaches_the_hook_unharmed(self) -> None:
        event = self._event("/tmp/proj/a.md")
        decision = _gate().on_tool_call(
            event.title,
            session_key="cli_chat",
            tool_kind=event.tool_kind,
            raw_params=event.raw_tool_params,
            diff_path=event.diff_path,
        )
        assert decision.action != TOOL_DENY


class TestTheTwoGatesJudgeTheSameSet:
    """The defect was drift between the two edit gates; the repair is one shared
    candidate computation. Pin that both consumers call it, so the union cannot
    silently fork again."""

    def test_edit_target_candidates_union(self) -> None:
        from kiro_crew.platform.tool_paths import edit_target_candidates

        assert list(edit_target_candidates({"path": "/a"}, "/b")) == ["/a", "/b"]
        assert list(edit_target_candidates({"path": "/a"}, "/a")) == ["/a"], "deduped"
        assert list(edit_target_candidates(None, "/b")) == ["/b"]
        assert list(edit_target_candidates({"command": "create"}, "")) == []
        assert edit_target_candidates(None, "") == []

    def test_truncated_flag_survives_the_union(self) -> None:
        from kiro_crew.platform.tool_paths import (
            _TARGET_PATH_MAX_PATHS,
            edit_target_candidates,
        )

        flood = {"operations": [{"path": f"/tmp/f{i}"} for i in range(_TARGET_PATH_MAX_PATHS + 5)]}
        candidates = edit_target_candidates(flood, "/tmp/extra")
        assert candidates.truncated is True
        # The work caps bound the returned set: a truncated walk must not grow
        # past the cap by the diff-path append (the append is skipped — the
        # union is already unverifiable and both consumers deny on the flag).
        assert "/tmp/extra" not in candidates
        assert len(candidates) <= _TARGET_PATH_MAX_PATHS

    def test_llm_helpers_denial_uses_the_shared_helper(self) -> None:
        from kiro_crew import llm_helpers
        from kiro_crew.platform import tool_paths

        assert (
            getattr(llm_helpers, "edit_target_candidates", None)
            is tool_paths.edit_target_candidates
        )


class TestTheReorderGuardStaysArmed:
    """The edit branch's own truncated deny is unreachable while the keystone
    above denies a truncated walk first (its comment says so) — but it exists
    precisely so a reorder or narrowing of the keystone cannot silently turn a
    partial scan into a pass. Simulate that drift: neutralize the keystone's
    walk and assert the edit branch still fails closed on its own reading."""

    def test_edit_branch_denies_truncation_without_the_keystone(self, monkeypatch) -> None:
        import kiro_crew.hooks as hooks_mod
        from kiro_crew.platform.tool_paths import _TARGET_PATH_MAX_PATHS, TargetPaths

        # The keystone reads hooks.target_paths; the edit branch reads
        # hooks.edit_target_candidates (which calls platform.tool_paths' own
        # reference). Blinding only the keystone's name models the guarded-for
        # drift while leaving the edit branch's real computation intact.
        monkeypatch.setattr(hooks_mod, "target_paths", lambda _p: TargetPaths())
        flood = {"operations": [{"path": f"/tmp/f{i}"} for i in range(_TARGET_PATH_MAX_PATHS + 5)]}
        decision = _call(raw_params=flood)
        assert decision.action == TOOL_DENY, (
            "with the keystone blinded, the edit branch trusted a truncated "
            "walk as complete — the reorder guard is dead"
        )
        assert "too large to verify" in decision.reason


class TestAnUnanchoredDiffBlockPathIsDenied:
    """The diff block's path is a verbatim backend field. A relative one
    resolves against the gateway process CWD, not the agent workspace, so a
    workspace symlink can point it at a protected file no gate recognizes
    under its unanchored spelling. Both tiers deny it as unverifiable, the
    same fail-closed shape as a truncated walk."""

    def test_relative_diff_path_is_denied_by_the_hook_gate(self) -> None:
        decision = _call(raw_params=None, diff_path="notes/plan.md")
        assert decision.action == TOOL_DENY
        assert "relative target path" in decision.reason

    def test_relative_diff_path_is_denied_even_with_safe_params(self) -> None:
        # The relative path must not ride along unjudged while an absolute
        # params target passes the sensitivity loop.
        decision = _call(raw_params={"path": "/tmp/ok.md"}, diff_path="sub/x.md")
        assert decision.action == TOOL_DENY
        assert "relative target path" in decision.reason

    def test_relative_diff_path_is_denied_by_the_always_enforced_tier(self) -> None:
        from kiro_crew.llm_helpers import _edit_target_denial

        hit = _edit_target_denial({"command": "create", "fileText": "x"}, "sub/x.md")
        assert hit is not None
        assert "relative target path" in hit[1]

    def test_tilde_diff_path_is_anchored_not_denied_as_relative(self) -> None:
        # ~ and $HOME expand deterministically (no CWD involved), so a
        # home-relative spelling is verifiable: judged on its merits, not
        # refused as unanchored.
        decision = _call(raw_params=None, diff_path="~/projects/notes.md")
        assert decision.action != TOOL_DENY

    def test_unanchored_flag_is_set_and_path_withheld(self) -> None:
        from kiro_crew.platform.tool_paths import edit_target_candidates

        candidates = edit_target_candidates({"path": "/tmp/ok.md"}, "sub/x.md")
        assert candidates.unanchored is True
        assert "sub/x.md" not in candidates
        anchored = edit_target_candidates(None, "/abs/x.md")
        assert anchored.unanchored is False
        assert list(anchored) == ["/abs/x.md"]


class TestGovernanceClassifiesTheDiffBlockPath:
    """The governance plane's filesystem.write classification must judge the
    same params-union-diff-block target set the edit gates judge — a diff-only
    edit otherwise reaches an operator ALLOW-mode write confinement pathless,
    and the confinement never binds."""

    def test_diff_only_edit_is_classified_for_filesystem_write(self) -> None:
        from kiro_crew.platform.governance import classify_tool_args

        pairs = classify_tool_args(
            "edit", {"command": "create", "fileText": "x"}, diff_path="/tmp/outside.md"
        )
        assert ("filesystem.write", "/tmp/outside.md") in pairs

    def test_diff_only_edit_with_no_params_is_classified(self) -> None:
        from kiro_crew.platform.governance import classify_tool_args

        pairs = classify_tool_args("edit", None, diff_path="/tmp/outside.md")
        assert ("filesystem.write", "/tmp/outside.md") in pairs

    def test_params_and_diff_paths_are_both_classified(self) -> None:
        from kiro_crew.platform.governance import classify_tool_args

        pairs = classify_tool_args("edit", {"path": "/a"}, diff_path="/b")
        assert ("filesystem.write", "/a") in pairs
        assert ("filesystem.write", "/b") in pairs

    def test_unanchored_diff_path_emits_the_never_permittable_marker(self) -> None:
        from kiro_crew.platform.governance import (
            _UNANCHORED_TARGET_ITEM,
            classify_tool_args,
        )

        pairs = classify_tool_args("edit", None, diff_path="sub/x.md")
        assert ("filesystem.write", _UNANCHORED_TARGET_ITEM) in pairs
        assert ("filesystem.write", "sub/x.md") not in pairs

    def test_truncated_walk_still_emits_the_truncation_marker(self) -> None:
        from kiro_crew.platform.governance import _TRUNCATED_SCAN_ITEM, classify_tool_args
        from kiro_crew.platform.tool_paths import _TARGET_PATH_MAX_PATHS

        flood = {"operations": [{"path": f"/tmp/f{i}"} for i in range(_TARGET_PATH_MAX_PATHS + 5)]}
        pairs = classify_tool_args("edit", flood, diff_path="/tmp/extra")
        assert ("filesystem.write", _TRUNCATED_SCAN_ITEM) in pairs

    def test_non_edit_kinds_are_unchanged(self) -> None:
        from kiro_crew.platform.governance import classify_tool_args

        assert classify_tool_args("read", {"path": "/a"}) == (("filesystem.read", "/a"),)
        assert classify_tool_args("read", None) == ()


class TestEveryEnforcingCallSiteThreadsTheDiffPath:
    """Tripwire for a recurring drift class: the diff-block path must reach
    every judgement tier alongside the params it belongs to. Any production
    call of on_tool_call that passes raw_params= must pass diff_path= too — a
    caller that omits it judges edits params-only AND hard-denies benign
    diff-only edits under the empty-union rule."""

    def test_raw_params_callers_all_pass_diff_path(self) -> None:
        import ast
        from pathlib import Path

        src_root = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
        offenders: list[str] = []
        for py in sorted(src_root.rglob("*.py")):
            if "tests" in py.parts:
                continue  # in-package test fixtures are not enforcement surfaces
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover - unparseable file is CI's problem
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                # Direct call: gate.on_tool_call(...). Indirect call: the bound
                # method passed as a callable argument with the kwargs applied
                # by the wrapper — asyncio.to_thread(hooks.on_tool_call, ...)
                # is how cli_chat.py invokes it, and is the exact shape the
                # missed call site had.
                indirect = any(
                    isinstance(arg, ast.Attribute) and arg.attr == "on_tool_call"
                    for arg in node.args
                )
                if name != "on_tool_call" and not indirect:
                    continue
                kwargs = {kw.arg for kw in node.keywords if kw.arg}
                if "raw_params" in kwargs and "diff_path" not in kwargs:
                    offenders.append(f"{py.relative_to(src_root.parent.parent)}:{node.lineno}")
        assert not offenders, (
            "on_tool_call call site(s) pass raw_params without diff_path — the "
            "hook edit gate there judges edits params-only and denies benign "
            f"diff-only edits: {offenders}"
        )
