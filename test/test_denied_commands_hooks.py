"""Tests for the user-configurable denied-commands hooks rework (Task 4).

Covers ``UserDeniedPattern``, the three nested ``HooksConfig`` opt-out fields,
``HooksConfig.from_dict``/``to_dict`` round-trip, and ``HookManager`` gate
ordering: the effective built-in deny set (with disable-all / per-id opt-out /
governance-pin force-re-add) is enforced BEFORE the new read-only auto-approve
step, which is itself the last branch before ``allow()``.
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew.hooks import (
    _BUNDLED_AUTO_APPROVE_TOOLS,
    _READ_ONLY_TOOL_KINDS,
    TOOL_ALLOW,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    HookManager,
    HooksConfig,
    UserDeniedPattern,
    resolve_denied_notes,
)


@pytest.fixture
def restore_context():
    """Reset the platform context after a test installs a custom one."""
    from kiro_crew.platform import reset_context

    yield
    reset_context()


def _install_ceiling_with_command_deny(patterns):
    """Install a platform context whose governance ceiling pins ``patterns``."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import governance as gov
    from kiro_crew.platform import set_context
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.profile import PROFILE_STANDALONE

    ceiling = gov.parse_policy(
        {"version": 1, "boot": {}, "commands": {"mode": "deny", "deny": list(patterns)}}
    )
    ctx = build_default_context(KiroCrewConfig(), profile=PROFILE_STANDALONE)
    set_context(dataclasses.replace(ctx, governance=ceiling))
    return ceiling


class TestUserDeniedPattern:
    def test_from_dict_generates_id_when_blank(self):
        p = UserDeniedPattern.from_dict({"pattern": "rm -rf /tmp/mine"})
        assert p.pattern == "rm -rf /tmp/mine"
        assert p.enabled is True
        assert p.id  # auto-generated
        assert len(p.id) == 12

    def test_from_dict_preserves_id(self):
        p = UserDeniedPattern.from_dict(
            {"id": "user-abc123", "pattern": "danger", "enabled": False}
        )
        assert p.id == "user-abc123"
        assert p.enabled is False

    def test_to_dict_roundtrip(self):
        p = UserDeniedPattern(id="x1", pattern="foo", enabled=True)
        # ``note`` joined the serialized shape; an un-annotated rule reports "".
        assert p.to_dict() == {"id": "x1", "pattern": "foo", "enabled": True, "note": ""}

    def test_note_roundtrips_through_from_dict_and_to_dict(self):
        raw = {"id": "u1", "pattern": "danger", "enabled": True, "note": "use the safe wrapper"}
        p = UserDeniedPattern.from_dict(raw)
        assert p.note == "use the safe wrapper"
        assert p.to_dict() == raw

    @pytest.mark.parametrize("junk", [None, {}, [], 0, False, ""])
    def test_falsy_malformed_note_degrades_to_empty_string(self, junk):
        # The note is cosmetic, so junk must degrade rather than raise:
        # from_dict runs at gateway boot, and a TypeError here would both block
        # startup and take down the rule the note was annotating. Every FALSY
        # junk value (``... or ""``) lands on the empty string.
        p = UserDeniedPattern.from_dict({"id": "u1", "pattern": "danger", "note": junk})
        assert p.note == ""
        assert p.pattern == "danger"  # the rule itself is untouched
        assert p.enabled is True

    @pytest.mark.parametrize(
        "junk,coerced", [(1, "1"), (3.5, "3.5"), ({"a": 1}, "{'a': 1}"), (["x"], "['x']")]
    )
    def test_truthy_malformed_note_is_str_coerced_not_dropped(self, junk, coerced):
        # ``str(data.get("note", "") or "")`` STRINGIFIES truthy junk rather than
        # discarding it, so an int/dict note becomes its repr. Pinned because it
        # is the observable behavior, and because what matters is the pair of
        # invariants below: it never raises, and the rule it annotates is intact.
        p = UserDeniedPattern.from_dict({"id": "u1", "pattern": "danger", "note": junk})
        assert p.note == coerced
        assert isinstance(p.note, str)
        assert p.pattern == "danger"
        assert p.enabled is True

    def test_note_absent_is_empty_string(self):
        p = UserDeniedPattern.from_dict({"id": "u1", "pattern": "danger"})
        assert p.note == ""

    def test_positional_construction_still_works(self):
        # ``note`` is declared LAST precisely so pre-existing positional
        # construction keeps compiling — this is the compatibility guard.
        p = UserDeniedPattern("i", "p", True)
        assert (p.id, p.pattern, p.enabled, p.note) == ("i", "p", True, "")


class TestHooksConfigDeniedCommands:
    def test_from_dict_reads_nested_denied_commands(self):
        cfg = HooksConfig.from_dict(
            {
                "denied_commands": {
                    "disable_all": True,
                    "disabled_ids": ["local-destructive-rm-rf-root"],
                    "user_added": [
                        {"id": "user-1", "pattern": "rm -rf /tmp/x", "enabled": True},
                        {"pattern": "", "enabled": True},  # blank pattern skipped
                        "not-a-dict",  # ignored
                    ],
                }
            }
        )
        assert cfg.denied_commands_disable_all is True
        assert cfg.denied_commands_disabled_ids == ["local-destructive-rm-rf-root"]
        assert len(cfg.denied_commands_user_added) == 1
        assert cfg.denied_commands_user_added[0].pattern == "rm -rf /tmp/x"

    def test_from_dict_defaults_when_absent(self):
        cfg = HooksConfig.from_dict({})
        assert cfg.denied_commands_disable_all is False
        assert cfg.denied_commands_disabled_ids == []
        assert cfg.denied_commands_user_added == []

    def test_from_dict_tolerates_scalar_user_added_and_disabled_ids(self):
        # config.json is operator-editable: a malformed scalar in either nested
        # list field must NOT raise (a TypeError here crashes gateway startup,
        # which calls from_dict at boot). It degrades to "no opt-out".
        cfg = HooksConfig.from_dict(
            {
                "denied_commands": {
                    "user_added": 1,  # scalar, not a list
                    "disabled_ids": 7,  # scalar, not a list
                    "disable_all": True,
                }
            }
        )
        assert cfg.denied_commands_user_added == []
        assert cfg.denied_commands_disabled_ids == []
        assert cfg.denied_commands_disable_all is True

    def test_from_dict_filters_non_string_disabled_ids(self):
        # A list with junk entries keeps only the non-empty string ids.
        cfg = HooksConfig.from_dict(
            {"denied_commands": {"disabled_ids": ["keep-me", 5, None, "", "also-keep"]}}
        )
        assert cfg.denied_commands_disabled_ids == ["keep-me", "also-keep"]

    def test_from_dict_tolerates_malformed_sibling_hook_fields(self):
        # Every operator-editable hooks field must survive malformed junk without
        # raising — from_dict runs at gateway boot, so a TypeError/AttributeError
        # here would prevent the service from starting. Each degrades to default.
        cfg = HooksConfig.from_dict(
            {
                "auto_replies": 1,  # scalar, not a list
                "transforms": "nope",  # string, not a list
                "context_rules": [1, None, "x"],  # list of non-dicts
                "auto_approve_tools": 7,  # scalar
                "auto_approve_sources": {"a": 1},  # dict, not a list
                "auto_deny_tools": "tool",  # string, not a list
            }
        )
        assert cfg.auto_replies == []
        assert cfg.transforms == []
        assert cfg.context_rules == []
        # Bundled auto-approve tools are still injected even when the user value
        # is junk (they are the always-on defaults).
        assert all(isinstance(t, str) for t in cfg.auto_approve_tools)
        assert cfg.auto_approve_sources == []
        assert cfg.auto_deny_tools == []

    def test_from_dict_tolerates_non_dict_top_level(self):
        # A top-level hooks value that is not a dict must not raise.
        assert HooksConfig.from_dict([]).denied_commands_user_added == []  # type: ignore[arg-type]

    def test_disable_all_string_false_is_not_truthy(self):
        # plain bool("false") is True — a hand-edited '"disable_all": "false"'
        # must NOT silently disable every built-in protection. Fail safe: the
        # string "false" (and any junk) resolves to False (denies stay on).
        assert (
            HooksConfig.from_dict(
                {"denied_commands": {"disable_all": "false"}}
            ).denied_commands_disable_all
            is False
        )
        assert (
            HooksConfig.from_dict(
                {"denied_commands": {"disable_all": "off"}}
            ).denied_commands_disable_all
            is False
        )
        assert (
            HooksConfig.from_dict(
                {"denied_commands": {"disable_all": 0}}
            ).denied_commands_disable_all
            is False
        )
        # Recognized truthy spellings still enable it.
        assert (
            HooksConfig.from_dict(
                {"denied_commands": {"disable_all": "true"}}
            ).denied_commands_disable_all
            is True
        )
        assert (
            HooksConfig.from_dict(
                {"denied_commands": {"disable_all": True}}
            ).denied_commands_disable_all
            is True
        )

    def test_auto_approve_flags_string_false_not_truthy(self):
        # A malformed auto-approve flag must not silently WIDEN approval.
        cfg = HooksConfig.from_dict(
            {
                "auto_approve_subagent_spawn": "false",
                "auto_approve_subagent_tools": "no",
            }
        )
        assert cfg.auto_approve_subagent_spawn is False
        assert cfg.auto_approve_subagent_tools is False

    def test_user_rule_enabled_string_false_respected(self):
        # A user rule's ``enabled: "false"`` must disable it, not stay truthy.
        cfg = HooksConfig.from_dict(
            {
                "denied_commands": {
                    "user_added": [{"id": "u1", "pattern": "danger", "enabled": "false"}]
                }
            }
        )
        assert cfg.denied_commands_user_added[0].enabled is False

    def test_to_dict_roundtrip_no_bundled_leak(self):
        cfg = HooksConfig.from_dict(
            {
                "denied_commands": {
                    "disable_all": False,
                    "disabled_ids": ["some-id"],
                    "user_added": [{"id": "u1", "pattern": "danger", "enabled": False}],
                }
            }
        )
        out = cfg.to_dict()
        # Bundled auto-approve tools must not leak back into persisted config.
        for bundled in _BUNDLED_AUTO_APPROVE_TOOLS:
            assert bundled not in out["auto_approve_tools"]
        dc = out["denied_commands"]
        assert dc["disable_all"] is False
        assert dc["disabled_ids"] == ["some-id"]
        assert dc["user_added"] == [
            {"id": "u1", "pattern": "danger", "enabled": False, "note": ""}
        ]
        # A full round-trip preserves the opt-out state.
        cfg2 = HooksConfig.from_dict(out)
        assert cfg2.denied_commands_disabled_ids == ["some-id"]
        assert cfg2.denied_commands_user_added[0].pattern == "danger"


class TestEffectiveDenied:
    def test_builtin_denied_by_default(self):
        mgr = HookManager(HooksConfig())
        result = mgr.on_tool_call("rm -rf /tmp/foo", command="rm -rf /tmp/foo", is_shell=True)
        assert result.action == TOOL_DENY

    def test_disabled_builtin_falls_through(self):
        cfg = HooksConfig(denied_commands_disabled_ids=["local-destructive-rm-rf-root"])
        mgr = HookManager(cfg)
        result = mgr.on_tool_call("rm -rf /tmp/foo", command="rm -rf /tmp/foo", is_shell=True)
        # No longer denied — the built-in rule was individually opted out.
        assert result.action != TOOL_DENY

    def test_disable_all_falls_through(self):
        cfg = HooksConfig(denied_commands_disable_all=True)
        mgr = HookManager(cfg)
        result = mgr.on_tool_call("rm -rf /tmp/foo", command="rm -rf /tmp/foo", is_shell=True)
        assert result.action != TOOL_DENY

    def test_user_added_pattern_denies(self):
        cfg = HooksConfig(
            denied_commands_user_added=[UserDeniedPattern(id="u1", pattern="frobnicate.*")]
        )
        mgr = HookManager(cfg)
        result = mgr.on_tool_call("frobnicate the box", command="frobnicate the box", is_shell=True)
        assert result.action == TOOL_DENY

    def test_disabled_user_added_pattern_ignored(self):
        cfg = HooksConfig(
            denied_commands_user_added=[
                UserDeniedPattern(id="u1", pattern="frobnicate.*", enabled=False)
            ]
        )
        mgr = HookManager(cfg)
        result = mgr.on_tool_call("frobnicate the box", command="frobnicate the box", is_shell=True)
        assert result.action != TOOL_DENY


class TestResolveDeniedNotes:
    """``resolve_denied_notes`` maps annotated, ENABLED user patterns to notes."""

    def test_includes_only_enabled_and_annotated(self):
        cfg = HooksConfig(
            denied_commands_user_added=[
                UserDeniedPattern(id="u1", pattern="frobnicate.*", note="use --dry-run"),
                UserDeniedPattern(id="u2", pattern="quux.*"),  # enabled, no note
            ]
        )
        assert resolve_denied_notes(cfg) == {"frobnicate.*": "use --dry-run"}

    def test_excludes_disabled_rule(self):
        # A disabled rule cannot fire, so surfacing its note would attach
        # remediation to a refusal that never happens.
        cfg = HooksConfig(
            denied_commands_user_added=[
                UserDeniedPattern(id="u1", pattern="frobnicate.*", note="nope", enabled=False)
            ]
        )
        assert resolve_denied_notes(cfg) == {}

    @pytest.mark.parametrize("blank", ["", "   ", "\t", "\n", " \n\t "])
    def test_excludes_blank_and_whitespace_only_notes(self, blank):
        # A whitespace-only note would append an empty second line to the
        # refusal — worse than no note at all.
        cfg = HooksConfig(
            denied_commands_user_added=[
                UserDeniedPattern(id="u1", pattern="frobnicate.*", note=blank)
            ]
        )
        assert resolve_denied_notes(cfg) == {}

    def test_excludes_note_that_would_forge_a_second_reason_line(self):
        # The note is emitted on its own line and RecoveryCard.tsx parses refusals
        # with a GLOBAL per-line regex, so a note carrying the refusal prefix would
        # be read as a second, FABRICATED deny pattern. The add endpoint rejects
        # this, but the keystone file is operator-editable by hand, so the read
        # path is what actually holds the invariant.
        from kiro_crew.security import DENY_REASON_PREFIX

        cfg = HooksConfig(
            denied_commands_user_added=[
                UserDeniedPattern(
                    id="u1",
                    pattern="frobnicate.*",
                    note=f"{DENY_REASON_PREFIX}rm -rf /",
                ),
                UserDeniedPattern(id="u2", pattern="quux.*", note="this one is fine"),
            ]
        )
        # Fail-safe: the forging note is dropped, the honest one survives.
        assert resolve_denied_notes(cfg) == {"quux.*": "this one is fine"}

    @pytest.mark.parametrize(
        "forged",
        [
            "Blocked by security policy: real-looking",
            # No space after the colon: RecoveryCard's regex is `policy:\s*`, so
            # this still parses as a refusal line even though it does NOT contain
            # the emitted prefix (which carries a trailing space). Guarding on the
            # emitted form left exactly this bypass.
            "Blocked by security policy:no-space",
            "Blocked by security policy:\ttab-separated",
            "why: see runbook. Blocked by security policy:appended",
        ],
    )
    def test_drops_every_whitespace_variant_that_parses_as_a_refusal_line(self, forged):
        cfg = HooksConfig(
            denied_commands_user_added=[
                UserDeniedPattern(id="u1", pattern="frobnicate.*", note=forged)
            ]
        )
        assert resolve_denied_notes(cfg) == {}

    def test_forging_note_cannot_produce_two_parseable_reason_lines(self):
        """End-to-end: resolve -> is_denied must never emit two parseable lines.

        Guards the composition, not just the filter -- a future refactor that
        routes notes into ``is_denied`` around ``resolve_denied_notes`` would pass
        the unit test above and still hand RecoveryCard a fabricated pattern.
        """
        from kiro_crew.security import DENY_REASON_PREFIX, is_denied

        pattern = "frobnicate.*"
        cfg = HooksConfig(
            denied_commands_user_added=[
                UserDeniedPattern(
                    id="u1", pattern=pattern, note=f"{DENY_REASON_PREFIX}rm -rf /"
                )
            ]
        )
        reason = is_denied(
            "frobnicate the box",
            denied_regexes=[pattern],
            reason_notes=resolve_denied_notes(cfg),
        )
        assert reason is not None
        # Exactly ONE line is parseable as a deny pattern -- the real one.
        assert reason.count(DENY_REASON_PREFIX) == 1
        assert reason == f"{DENY_REASON_PREFIX}{pattern}"

    def test_excludes_blank_pattern(self):
        # A rule with no pattern matches nothing; keying the map on "" would
        # annotate whatever else happened to arrive as an empty match key.
        cfg = HooksConfig(
            denied_commands_user_added=[UserDeniedPattern(id="u1", pattern="", note="orphan")]
        )
        assert resolve_denied_notes(cfg) == {}

    def test_strips_surrounding_whitespace(self):
        cfg = HooksConfig(
            denied_commands_user_added=[
                UserDeniedPattern(id="u1", pattern="frobnicate.*", note="  use --dry-run\t")
            ]
        )
        assert resolve_denied_notes(cfg) == {"frobnicate.*": "use --dry-run"}

    def test_empty_when_nothing_configured(self):
        assert resolve_denied_notes(HooksConfig()) == {}

    def test_manager_exposes_the_same_map(self):
        cfg = HooksConfig(
            denied_commands_user_added=[
                UserDeniedPattern(id="u1", pattern="frobnicate.*", note="use --dry-run")
            ]
        )
        assert HookManager(cfg)._denied_notes() == {"frobnicate.*": "use --dry-run"}

    def test_note_reaches_the_refusal_on_a_second_line(self):
        # End-to-end through the gate: the note the operator wrote is what the
        # agent reads, and it lands on its OWN line so the first line stays the
        # machine-parsed contract.
        cfg = HooksConfig(
            denied_commands_user_added=[
                UserDeniedPattern(id="u1", pattern="frobnicate.*", note="use --dry-run")
            ]
        )
        mgr = HookManager(cfg)
        result = mgr.on_tool_call("frobnicate the box", command="frobnicate the box", is_shell=True)
        assert result.action == TOOL_DENY
        lines = result.reason.splitlines()
        assert lines[0] == "Blocked by security policy: frobnicate.*"
        assert lines[1] == "use --dry-run"

    def test_unannotated_rule_refusal_is_single_line(self):
        cfg = HooksConfig(
            denied_commands_user_added=[UserDeniedPattern(id="u1", pattern="frobnicate.*")]
        )
        mgr = HookManager(cfg)
        result = mgr.on_tool_call("frobnicate the box", command="frobnicate the box", is_shell=True)
        assert result.action == TOOL_DENY
        assert result.reason == "Blocked by security policy: frobnicate.*"

    def test_config_authored_forging_note_is_dropped_not_forwarded(self):
        # This was pinned as a KNOWN GAP and is now CLOSED. The POST endpoint
        # collapses whitespace precisely because "newlines would forge extra lines
        # in the refusal", but the config-file path (from_dict ->
        # resolve_denied_notes) applies only .strip() -- so a note hand-written
        # into the keystone denied_commands.json used to reach the refusal with its
        # newlines intact, and RecoveryCard's per-line POLICY_RE read the forged
        # line as a second pattern. Reach is operator-only (the keystone is not
        # agent-writable), but pasting a refusal you just saw into a note is a
        # natural mistake, so the read path now drops any note carrying the prefix.
        forged = "why: ask a human\nBlocked by security policy: not-a-real-rule"
        cfg = HooksConfig.from_dict(
            {
                "denied_commands": {
                    "user_added": [
                        {"id": "u1", "pattern": "frobnicate.*", "enabled": True, "note": forged}
                    ]
                }
            }
        )
        # Dropped at the resolver, so nothing forged can reach the refusal.
        assert resolve_denied_notes(cfg) == {}
        mgr = HookManager(cfg)
        result = mgr.on_tool_call("frobnicate the box", command="frobnicate the box", is_shell=True)
        assert result.action == TOOL_DENY
        # Fail-safe: the pattern still denies, with exactly the historical text.
        assert result.reason == "Blocked by security policy: frobnicate.*"
        assert "not-a-real-rule" not in result.reason

    def test_config_authored_multiline_note_without_the_prefix_is_kept(self):
        # The guard is scoped to the FORGING case. A multiline note that cannot be
        # mistaken for a deny pattern is merely untidy, so it is preserved rather
        # than silently discarded -- dropping it would lose operator intent for no
        # correctness gain.
        note = "why: this is slow\nprefer: rg with a depth cap"
        cfg = HooksConfig.from_dict(
            {
                "denied_commands": {
                    "user_added": [
                        {"id": "u1", "pattern": "frobnicate.*", "enabled": True, "note": note}
                    ]
                }
            }
        )
        assert resolve_denied_notes(cfg) == {"frobnicate.*": note}


class TestGovernancePins:
    def test_pinned_builtin_still_denied_when_disabled(self, restore_context):
        _install_ceiling_with_command_deny(["rm -rf /.*"])
        cfg = HooksConfig(
            denied_commands_disabled_ids=["local-destructive-rm-rf-root"],
            denied_commands_disable_all=True,
        )
        mgr = HookManager(cfg)
        # Governance pinned the rule — user opt-out (and disable-all) cannot
        # weaken it (tightest-wins).
        result = mgr.on_tool_call("rm -rf /tmp/foo", command="rm -rf /tmp/foo", is_shell=True)
        assert result.action == TOOL_DENY

    def test_governance_pinned_ids_helper_failsoft(self):
        # Standalone default context governs nothing → no pins, no raise.
        from kiro_crew.hooks import _governance_pinned_command_ids
        from kiro_crew.platform import current_context

        assert _governance_pinned_command_ids(current_context()) == set()


class TestReadOnlyAutoApprove:
    def test_readonly_shell_autoapproved(self):
        mgr = HookManager(HooksConfig())
        result = mgr.on_tool_call("list files", command="ls -la /workplace", is_shell=True)
        assert result.action == TOOL_AUTO_APPROVE

    def test_non_readonly_shell_falls_through(self):
        # A benign but non-read-only command is neither denied nor auto-approved.
        mgr = HookManager(HooksConfig())
        result = mgr.on_tool_call("touch a file", command="touch newfile", is_shell=True)
        assert result.action == TOOL_ALLOW

    def test_readonly_tool_kind_autoapproved(self):
        mgr = HookManager(HooksConfig())
        result = mgr.on_tool_call("SomeReader", tool_kind="read")
        assert result.action == TOOL_AUTO_APPROVE
        assert "fetch" in _READ_ONLY_TOOL_KINDS

    def test_readonly_title_autoapproved(self):
        mgr = HookManager(HooksConfig())
        result = mgr.on_tool_call("get_user_profile")
        assert result.action == TOOL_AUTO_APPROVE

    def test_write_kind_with_readonly_title_not_autoapproved(self):
        # A mutating tool_kind must NEVER auto-approve on a read-sounding
        # (agent-supplied, spoofable) title — the title heuristic only applies
        # when the kind is absent. Guards against the "edit tool titled 'Read
        # project status'" bypass.
        mgr = HookManager(HooksConfig())
        result = mgr.on_tool_call("Read project status", tool_kind="edit")
        assert result.action != TOOL_AUTO_APPROVE

    def test_deny_runs_before_readonly(self):
        # A denied command that is ALSO read-only-shaped stays DENIED — the
        # read-only auto-approve is the last branch and never re-admits.
        cfg = HooksConfig(
            denied_commands_user_added=[UserDeniedPattern(id="u1", pattern="cat .*secret.*")]
        )
        mgr = HookManager(cfg)
        result = mgr.on_tool_call("read secret", command="cat mysecret.txt", is_shell=True)
        assert result.action == TOOL_DENY

    def test_readonly_autoapprove_after_deny(self):
        # Built-in deny still wins over the read-only classifier: a governance
        # pinned + read-only-shaped command stays denied.
        cfg = HooksConfig(
            denied_commands_user_added=[UserDeniedPattern(id="u1", pattern="grep .*forbidden.*")]
        )
        mgr = HookManager(cfg)
        result = mgr.on_tool_call("search logs", command="grep forbidden /var/log", is_shell=True)
        assert result.action == TOOL_DENY
