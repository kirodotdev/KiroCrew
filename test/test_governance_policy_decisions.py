"""What the resolver and the gate decide for an unnamed capability or an alias group.

An unnamed capability is ungoverned and therefore permitted, ``kirocrew policy
validate`` reports a partially governed ``capabilities`` block, and
``gate_decision`` judges a tool reached under two spellings of one identity as one
question. The archetypes, the loader and the resolution engine are in
``test_governance_policy.py``; the shared policy-body builder is
``governance_policy_helpers.py``.
"""

from __future__ import annotations

import logging

import pytest
from governance_policy_helpers import _policy_body

from kiro_crew.platform import governance
from kiro_crew.platform.governance import (
    CAPABILITY,
    MODE_ALLOW,
    MODE_DENY,
    SCOPE_CATALOG,
    GovernanceCeiling,
    Profile,
    ScopedRuleset,
    compose_profiles,
    parse_policy,
    parse_profile,
    resolve,
)


# ──────────────────────────────────────────────────────────────────────────
# Capability omission — the settled contract
# ──────────────────────────────────────────────────────────────────────────
def _capability_scopes() -> list[str]:
    return [name for name, spec in SCOPE_CATALOG.items() if spec.kind == CAPABILITY]


class TestCapabilityOmissionIsUngoverned:
    """An unnamed capability is UNGOVERNED and therefore PERMITTED. On purpose.

    This class exists because the opposite is an inviting mistake. Several
    ``SCOPE_CATALOG`` comments once described ``capability_default`` as applying
    when a policy "governs ``capabilities.*`` but omits" a row, which does not
    match the code. The tempting fix is to make the code match the comments by
    filling unnamed rows with their registered defaults.

    That fix was implemented, measured, and rejected. Three reasons, recorded here
    so the next person does not repeat it:

    1. It breaks the model's central invariant. Omission means
       ungoverned-and-permitted for `mcp`, `tools`, `commands`, `filesystem.*` and
       `network.egress`; making `capabilities` the one archetype that infers a
       value is a per-control special case in an evaluator whose stated contract
       is that it dispatches on archetype and never on scope name.
    2. It requires a namespace-specific branch in ``_parse_controls``, whose
       documented contract is that a newly ``register_scope``'d family parses with
       NO loader edit. A companion's own capability family would not get the same
       treatment, so the behaviour would not even be uniform.
    3. It destroys an audit signal. ``Decision.layer == "default"`` means "nothing
       governed this", which is what operators are told to alert on to find
       missing controls. A row filled from a catalog default resolves at
       ``layer="policy"`` and so becomes indistinguishable from one a human
       actually wrote.

    The real defect was documentation, and the protection is
    ``kirocrew policy validate`` reporting a partially-governed block.
    """

    def test_unnamed_capability_is_permitted_and_reads_as_ungoverned(self):
        ceiling = parse_policy(_policy_body(capabilities={"script_hooks": {"enabled": False}}))
        for scope in _capability_scopes():
            if scope == "capabilities.script_hooks":
                continue
            decision = resolve(ceiling, None, scope, "")
            assert decision.permitted, f"{scope} must be permitted by omission"
            # Load-bearing: `default` is the audit signal for "nobody governed
            # this". A filled-in row would report `policy` and hide the gap.
            assert decision.layer == "default", f"{scope} must read as ungoverned"

    def test_named_row_is_still_governed(self):
        ceiling = parse_policy(_policy_body(capabilities={"script_hooks": {"enabled": False}}))
        decision = resolve(ceiling, None, "capabilities.script_hooks", "")
        assert not decision.permitted
        assert decision.layer == "policy"

    def test_omission_behaves_identically_across_archetypes(self):
        """The consistency argument, asserted rather than assumed."""
        ceiling = parse_policy(_policy_body(capabilities={"cron": {"enabled": False}}))
        for scope, item in (
            ("mcp", "@anything/tool"),
            ("tools", "anything"),
            ("commands", "echo hi"),
            ("filesystem.read", "/etc/hosts"),
            ("network.egress", "example.com"),
            ("capabilities.messaging", ""),
        ):
            decision = resolve(ceiling, None, scope, item)
            assert decision.permitted, f"unnamed {scope} must permit"
            assert decision.layer == "default", f"unnamed {scope} must read as ungoverned"

    def test_no_capabilities_block_leaves_every_capability_ungoverned(self):
        ceiling = parse_policy(_policy_body(commands={"mode": MODE_DENY, "deny": ["nc *"]}))
        for scope in _capability_scopes():
            assert resolve(ceiling, None, scope, "").permitted

    def test_present_key_without_enabled_uses_the_registered_default(self):
        """The case ``capability_default`` DOES cover — and the only one.

        Naming a capability to configure its inner scopes, without saying whether
        it is on, resolves to the registered default: off for the exfil surfaces,
        on for the benign ones.
        """
        ceiling = parse_policy(
            _policy_body(
                capabilities={
                    "publish": {"scopes": {"destinations": {"mode": MODE_ALLOW, "allow": ["x"]}}},
                    "spawn": {"scopes": {"agents": {"mode": MODE_ALLOW, "allow": ["a"]}}},
                }
            )
        )
        assert not resolve(ceiling, None, "capabilities.publish", "").permitted
        assert SCOPE_CATALOG["capabilities.publish"].capability_default is False
        assert resolve(ceiling, None, "capabilities.spawn", "").permitted
        assert SCOPE_CATALOG["capabilities.spawn"].capability_default is True


class TestValidateReportsUngovernedCapabilities:
    """`kirocrew policy validate` must surface a partially-governed block.

    Since omission cannot deny, the only protection against an author believing
    otherwise is telling them which rows they left open.
    """

    def _validate(self, capsys, ceiling):
        import argparse
        from unittest.mock import patch

        from kiro_crew import cli_commands

        args = argparse.Namespace(policy_action="validate")
        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=type("Ctx", (), {"governance": ceiling})(),
        ):
            cli_commands._policy(args)
        return capsys.readouterr().out

    def test_partial_block_lists_the_ungoverned_rows(self, capsys):
        ceiling = parse_policy(_policy_body(capabilities={"script_hooks": {"enabled": False}}))
        out = self._validate(capsys, ceiling)
        assert "UNGOVERNED" in out
        assert "Omission does not deny" in out
        assert "capabilities.cron" in out
        # The row the author DID name must not be reported as a gap.
        assert "capabilities.script_hooks\n" not in out.split("UNGOVERNED", 1)[1]

    def test_fully_enumerated_block_reports_no_gap(self, capsys):
        body = {scope.split(".", 1)[1]: {"enabled": True} for scope in _capability_scopes()}
        # agentcore requires a known inner posture when enabled.
        body["agentcore"] = {"enabled": True, "posture": "workload"}
        out = self._validate(capsys, parse_policy(_policy_body(capabilities=body)))
        assert "UNGOVERNED" not in out
        assert "✅ valid" in out

    def test_policy_that_never_mentions_capabilities_reports_no_gap(self, capsys):
        """Silence about capabilities entirely is not a partial statement."""
        ceiling = parse_policy(_policy_body(commands={"mode": MODE_DENY, "deny": ["nc *"]}))
        out = self._validate(capsys, ceiling)
        assert "UNGOVERNED" not in out

    def test_validate_run_surfaces_a_url_shaped_network_egress_deny_entry(self, capsys, caplog):
        """The operator's own `policy validate` run must surface the dead entry.

        `validate` reports on the ceiling boot ALREADY resolved, so the finding
        reaches that run as the parse-time warning, not as stdout: validate
        re-reads a parsed object and cannot see a pattern that never matched.
        The line matters because this policy reports OK while denying nothing.
        """
        with caplog.at_level(logging.WARNING, logger="kiro_crew.platform.governance"):
            ceiling = parse_policy(
                _policy_body(
                    network={"egress": {"mode": MODE_DENY, "deny": ["https://skills.sh/api"]}}
                )
            )
            out = self._validate(capsys, ceiling)
        assert "governed scopes" in out
        # The entry the operator wrote to block skills.sh does not block it.
        assert resolve(ceiling, None, "network.egress", "skills.sh").permitted
        line = next(r.getMessage() for r in caplog.records if "matcher=host" in r.getMessage())
        assert "'network.egress'" in line
        assert "deny[0]" in line
        assert "https://skills.sh/api" not in line


class TestGateDecisionAliasGroups:
    """A KAS built-in reaches the gate under two spellings of ONE identity: its
    raw id (``str_replace``) and the kiro-cli name a rule about it is written
    under (``fs_write``). Profiles are written in kiro-cli vocabulary. Asked as
    two separate items, an allow-mode profile of ``{fs_write}`` refuses the raw
    id it never listed; asked as the alias alone, a deny-mode ``deny:
    [str_replace]`` stops binding. An alias group is one question: an explicit
    deny on any spelling binds, otherwise one permitted spelling admits."""

    def _profile(self, control) -> Profile:
        return Profile(name="p", controls={"tools": control})

    def test_allow_mode_written_in_kiro_cli_names_admits_the_kas_write(self):
        p = self._profile(ScopedRuleset(MODE_ALLOW, ("Update the changelog", "fs_write")))
        d = governance.gate_decision(
            None,
            p,
            "Update the changelog",
            alias_groups=(("str_replace", "fs_write"),),
        )
        assert d.permitted, d.reason

    def test_allow_mode_written_in_kas_ids_admits_the_kas_write_too(self):
        p = self._profile(ScopedRuleset(MODE_ALLOW, ("Update the changelog", "str_replace")))
        d = governance.gate_decision(
            None,
            p,
            "Update the changelog",
            alias_groups=(("str_replace", "fs_write"),),
        )
        assert d.permitted, d.reason

    def test_allow_mode_listing_neither_spelling_refuses_and_names_the_alias(self):
        p = self._profile(ScopedRuleset(MODE_ALLOW, ("Update the changelog",)))
        d = governance.gate_decision(
            None,
            p,
            "Update the changelog",
            alias_groups=(("str_replace", "fs_write"),),
        )
        assert not d.permitted
        assert d.item == "fs_write"

    @pytest.mark.parametrize("denied", ["str_replace", "fs_write"])
    def test_deny_mode_on_either_spelling_binds(self, denied):
        p = self._profile(ScopedRuleset(MODE_DENY, deny=(denied,)))
        d = governance.gate_decision(
            None,
            p,
            "Update the changelog",
            alias_groups=(("str_replace", "fs_write"),),
        )
        assert not d.permitted
        assert d.item == denied
        assert d.rule == "rule1-deny"

    def test_a_ceiling_deny_on_the_raw_id_is_final(self):
        c = GovernanceCeiling(
            version=1,
            boot=parse_policy(_policy_body()).boot,
            controls={"tools": ScopedRuleset(MODE_DENY, deny=("str_replace",))},
        )
        p = self._profile(ScopedRuleset(MODE_ALLOW, ("Update the changelog", "fs_write")))
        d = governance.gate_decision(
            c, p, "Update the changelog", alias_groups=(("str_replace", "fs_write"),)
        )
        assert not d.permitted and d.layer == "policy"

    def test_a_title_equal_to_a_group_member_is_covered_by_the_group(self):
        """KAS titles the call by its id in some shapes; the title must not become
        a lone allow-set requirement beside the group that already carries it."""
        p = self._profile(ScopedRuleset(MODE_ALLOW, ("fs_write",)))
        d = governance.gate_decision(
            None, p, "str_replace", alias_groups=(("str_replace", "fs_write"),)
        )
        assert d.permitted, d.reason

    def test_an_empty_group_is_ignored(self):
        p = self._profile(ScopedRuleset(MODE_ALLOW, ("t",)))
        assert governance.gate_decision(None, p, "t", alias_groups=(("", ""),)).permitted

    def test_each_level_granting_a_different_spelling_admits_the_one_tool(self):
        """A ceiling written in KAS ids grants ``str_replace``; a profile written
        in kiro-cli names grants ``fs_write``. Both levels granted the one tool,
        each in its own vocabulary, so the intersection admits it. Resolving each
        spelling through the intersection first would refuse: neither spelling is
        permitted by BOTH levels."""
        c = GovernanceCeiling(
            version=1,
            boot=parse_policy(_policy_body()).boot,
            controls={"tools": ScopedRuleset(MODE_ALLOW, ("Update the changelog", "str_replace"))},
        )
        p = self._profile(ScopedRuleset(MODE_ALLOW, ("Update the changelog", "fs_write")))
        d = governance.gate_decision(
            c, p, "Update the changelog", alias_groups=(("str_replace", "fs_write"),)
        )
        assert d.permitted, d.reason
        # The same two rulesets the other way round answer the same.
        c2 = GovernanceCeiling(
            version=1,
            boot=parse_policy(_policy_body()).boot,
            controls={"tools": ScopedRuleset(MODE_ALLOW, ("Update the changelog", "fs_write"))},
        )
        p2 = self._profile(ScopedRuleset(MODE_ALLOW, ("Update the changelog", "str_replace")))
        assert governance.gate_decision(
            c2, p2, "Update the changelog", alias_groups=(("str_replace", "fs_write"),)
        ).permitted

    def test_a_level_granting_neither_spelling_still_refuses(self):
        """The OR is within a level only: a profile that lists neither spelling
        refuses even though the ceiling grants one."""
        c = GovernanceCeiling(
            version=1,
            boot=parse_policy(_policy_body()).boot,
            controls={"tools": ScopedRuleset(MODE_ALLOW, ("Update the changelog", "str_replace"))},
        )
        p = self._profile(ScopedRuleset(MODE_ALLOW, ("Update the changelog",)))
        d = governance.gate_decision(
            c, p, "Update the changelog", alias_groups=(("str_replace", "fs_write"),)
        )
        assert not d.permitted
        assert d.layer == "profile"
        assert d.item == "fs_write"

    def test_a_profile_deny_is_reported_over_a_ceiling_allow_miss(self):
        """Every level is walked before a refusal is answered: a ceiling allow-set
        that lists neither spelling and a profile that explicitly denies one --
        the deny is the answer, with its provenance, not the miss."""
        c = GovernanceCeiling(
            version=1,
            boot=parse_policy(_policy_body()).boot,
            controls={"tools": ScopedRuleset(MODE_ALLOW, ("Update the changelog",))},
        )
        p = self._profile(ScopedRuleset(MODE_DENY, deny=("str_replace",)))
        d = governance.gate_decision(
            c, p, "Update the changelog", alias_groups=(("str_replace", "fs_write"),)
        )
        assert not d.permitted
        assert d.explicit_deny is True
        assert d.item == "str_replace"
        assert d.layer == "profile"

    def test_a_subordinate_deny_folded_under_an_allow_ceiling_still_binds(self):
        """A composed control (central allow-mode ``{fs_write}`` folded with a
        subordinate deny-mode ``deny: [str_replace]``) relabels its refusals
        ``rule2-intersect``. The alias group must read the deny's PROVENANCE, not
        the label, or the fold would hide the subordinate deny behind the
        authority's allow and the denied write would proceed."""
        central = ScopedRuleset(MODE_ALLOW, ("Update the changelog", "fs_write"))
        subordinate = ScopedRuleset(MODE_DENY, deny=("str_replace",))
        composed = central.compose(subordinate)
        assert isinstance(composed, governance._AndRuleset)
        c = GovernanceCeiling(
            version=1,
            boot=parse_policy(_policy_body()).boot,
            controls={"tools": composed},
        )
        d = governance.gate_decision(
            c, None, "Update the changelog", alias_groups=(("str_replace", "fs_write"),)
        )
        assert not d.permitted
        assert d.item == "str_replace"
        assert d.explicit_deny is True
        assert d.rule == "rule2-intersect"  # the label the fold gives it

    @pytest.mark.parametrize("outer_first", [True, False])
    def test_a_central_delete_deny_folded_with_a_local_allow_still_binds(self, outer_first):
        """Either fold order: a central ``tools: {deny: [delete_file]}`` and a
        local allow-mode set naming ``fs_write``. A KAS ``delete_file`` arrives on
        its own name with ``fs_write`` as a deny-only alias; the fold may only
        tighten, so the denied id is final whichever tier the allow sits in."""
        central = ScopedRuleset(MODE_DENY, deny=("delete_file",))
        local = ScopedRuleset(MODE_ALLOW, ("Delete File", "fs_write"))
        composed = central.compose(local) if outer_first else local.compose(central)
        c = GovernanceCeiling(
            version=1, boot=parse_policy(_policy_body()).boot, controls={"tools": composed}
        )
        d = governance.gate_decision(
            c,
            None,
            "Delete File",
            extra_titles=("delete_file",),
            deny_aliases=("fs_write",),
        )
        assert not d.permitted
        assert d.item == "delete_file"
        assert d.explicit_deny is True
        # The same ceiling with only one tier governing denies the call too --
        # the fold must not answer differently.
        c1 = GovernanceCeiling(
            version=1, boot=parse_policy(_policy_body()).boot, controls={"tools": central}
        )
        assert not governance.gate_decision(
            c1, None, "Delete File", extra_titles=("delete_file",), deny_aliases=("fs_write",)
        ).permitted

    def test_an_allow_mode_fs_write_grant_does_not_admit_a_kas_delete(self):
        """``delete_file`` reads under ``fs_write`` for DENIES only. kiro-cli's
        ``fs_write`` cannot delete, so an allow-mode ``tools`` set written for
        kiro-cli that names ``fs_write`` grants the write family and nothing more;
        the deletion is refused on its own name, which the set never listed."""
        p = self._profile(ScopedRuleset(MODE_ALLOW, ("Delete File", "fs_write")))
        d = governance.gate_decision(
            None,
            p,
            "Delete File",
            extra_titles=("delete_file",),
            deny_aliases=("fs_write",),
        )
        assert not d.permitted
        assert d.item == "delete_file"
        assert d.explicit_deny is False  # an allow-set miss, not a written deny

    def test_an_allow_mode_set_naming_the_delete_admits_it_without_naming_fs_write(self):
        """The deny-only alias is not a second required entry: an operator who
        wrote ``delete_file`` into an allow set granted the deletion, and the
        set's silence on ``fs_write`` is not a refusal."""
        p = self._profile(ScopedRuleset(MODE_ALLOW, ("Delete File", "delete_file")))
        d = governance.gate_decision(
            None,
            p,
            "Delete File",
            extra_titles=("delete_file",),
            deny_aliases=("fs_write",),
        )
        assert d.permitted, d.reason

    def test_a_deny_written_under_the_deny_only_alias_binds(self):
        p = self._profile(ScopedRuleset(MODE_DENY, deny=("fs_write",)))
        d = governance.gate_decision(
            None,
            p,
            "Delete File",
            extra_titles=("delete_file",),
            deny_aliases=("fs_write",),
        )
        assert not d.permitted
        assert d.item == "fs_write"
        assert d.explicit_deny is True

    def test_a_deny_on_a_sibling_write_verb_does_not_reach_the_delete(self):
        """``deny: [str_replace]`` denies that verb, not the ``fs_write`` name the
        delete reads under, so the delete is not refused by it."""
        p = self._profile(ScopedRuleset(MODE_DENY, deny=("str_replace",)))
        d = governance.gate_decision(
            None,
            p,
            "Delete File",
            extra_titles=("delete_file",),
            deny_aliases=("fs_write",),
        )
        assert d.permitted, d.reason

    def test_a_deny_only_alias_never_admits_even_when_permitted_by_an_allow_set(self):
        """Mutation guard for the deny-only channel: an allow-mode set that
        PERMITS ``fs_write`` but not the delete still refuses the delete -- the
        alias's permit is not consulted, only its deny."""
        p = self._profile(ScopedRuleset(MODE_ALLOW, ("fs_write",)))
        d = governance.gate_decision(None, p, "delete_file", deny_aliases=("fs_write",))
        assert not d.permitted
        assert d.item == "delete_file"

    def test_deny_provenance_survives_every_fold_and_an_allow_miss_never_gains_it(self):
        deny = ScopedRuleset(MODE_DENY, deny=("x",))
        allow = ScopedRuleset(MODE_ALLOW, ("y",))
        assert deny.permits("x").explicit_deny is True
        assert allow.permits("x").explicit_deny is False  # not listed, not denied
        assert allow.permits("y").explicit_deny is False  # a permit never carries it
        for composed in (allow.compose(deny), deny.compose(allow)):
            assert composed.permits("x").explicit_deny is True
        nested = allow.compose(allow.compose(deny))
        assert nested.permits("x").explicit_deny is True
        assert nested.permits("z").explicit_deny is False  # allow-mode miss only
        c = GovernanceCeiling(
            version=1, boot=parse_policy(_policy_body()).boot, controls={"tools": deny}
        )
        assert resolve(c, None, "tools", "x").explicit_deny is True
        p = Profile(name="p", controls={"tools": allow})
        assert resolve(None, p, "tools", "x").explicit_deny is False

    # ── The read pair: a deny on ``fs_read`` beside an allow on ``read_file`` ──
    # A KAS ``read_file`` is one identity with two spellings (``read_file`` and
    # its kiro-cli policy name ``fs_read``). Within one ``ScopedRuleset`` the
    # live half is the mode's (Rule 1: allow-mode ignores ``deny``, deny-mode
    # ignores ``allow``), so a layer that names ``deny: [fs_read]`` alongside
    # ``allow: [read_file]`` in deny mode must refuse the KAS read, at every
    # layer and through every fold.

    _READ_GROUP = (("read_file", "fs_read"),)

    def _read_pair(self) -> ScopedRuleset:
        return ScopedRuleset(MODE_DENY, ("Read File", "read_file"), deny=("fs_read",))

    def test_read_pair_in_one_profile_layer_denies_the_kas_read(self):
        d = governance.gate_decision(
            None, self._profile(self._read_pair()), "Read File", alias_groups=self._READ_GROUP
        )
        assert not d.permitted
        assert d.item == "fs_read"
        assert d.explicit_deny is True
        assert d.layer == "profile"

    def test_read_pair_in_one_ceiling_layer_denies_the_kas_read(self):
        c = GovernanceCeiling(
            version=1,
            boot=parse_policy(_policy_body()).boot,
            controls={"tools": self._read_pair()},
        )
        d = governance.gate_decision(c, None, "Read File", alias_groups=self._READ_GROUP)
        assert not d.permitted
        assert d.item == "fs_read"
        assert d.explicit_deny is True
        assert d.layer == "policy"

    @pytest.mark.parametrize("outer_first", [True, False])
    def test_read_pair_split_across_a_fold_still_denies_the_kas_read(self, outer_first):
        """An allow on ``read_file`` in one tier, a deny on ``fs_read`` in the
        other, composed in either order: the deny binds."""
        allow = ScopedRuleset(MODE_ALLOW, ("Read File", "read_file"))
        deny = ScopedRuleset(MODE_DENY, deny=("fs_read",))
        composed = allow.compose(deny) if outer_first else deny.compose(allow)
        c = GovernanceCeiling(
            version=1, boot=parse_policy(_policy_body()).boot, controls={"tools": composed}
        )
        d = governance.gate_decision(c, None, "Read File", alias_groups=self._READ_GROUP)
        assert not d.permitted
        assert d.item == "fs_read"
        assert d.explicit_deny is True

    @pytest.mark.parametrize(
        ("parent_tools", "child_tools"),
        [
            (
                {"mode": "deny", "deny": ["fs_read"]},
                {"mode": "allow", "allow": ["Read File", "read_file"]},
            ),
            (
                {"mode": "allow", "allow": ["Read File", "read_file"]},
                {"mode": "deny", "deny": ["fs_read"]},
            ),
        ],
        ids=["parent-denies", "child-denies"],
    )
    def test_a_profile_extends_fold_keeps_a_kiro_cli_deny_on_the_kas_read(
        self, parent_tools, child_tools
    ):
        """``compose_profiles`` (the ``extends`` fold) puts both links in ONE
        composed control. A deny on ``fs_read`` in either link refuses the KAS
        ``read_file`` the other link allows."""
        parent = parse_profile({"name": "base", "tools": parent_tools})
        child = parse_profile({"name": "child", "extends": "base", "tools": child_tools})
        merged = compose_profiles(parent, child)
        d = governance.gate_decision(None, merged, "Read File", alias_groups=self._READ_GROUP)
        assert not d.permitted
        assert d.item == "fs_read"
        assert d.explicit_deny is True

    def test_a_profile_extends_fold_keeps_a_kiro_cli_write_deny_on_the_kas_write(self):
        parent = parse_profile({"name": "base", "tools": {"mode": "allow", "allow": ["fs_write"]}})
        child = parse_profile(
            {"name": "child", "extends": "base", "tools": {"mode": "deny", "deny": ["str_replace"]}}
        )
        merged = compose_profiles(parent, child)
        d = governance.gate_decision(
            None, merged, "str_replace", alias_groups=(("str_replace", "fs_write"),)
        )
        assert not d.permitted
        assert d.item == "str_replace"
        assert d.explicit_deny is True
