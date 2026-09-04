"""``allowedTools`` -> KAS ``permissions``, and the two places it has to land.

The translation is not a preference: each assertion here pins a fact read off
KAS's own policy engine, and getting one wrong is silent in both directions.

* Its evaluator resolves an unmatched request to ``ask``. A capability this
  module fails to emit therefore keeps prompting (safe), and one it emits too
  broadly stops prompting (not safe) — so the vocabulary is pinned, and an
  unclassifiable entry must produce NO rule rather than a guess.
* ``match`` omitted means every resource. That is what a tool-name allowlist
  entry means, so the omission is load-bearing rather than an oversight, and a
  test that expected ``match: ["**"]`` would be pinning the wrong thing.
* An MCP tool is addressed as ``<server>/<tool>``, which is what makes
  per-server and per-action grants expressible at all.
* And the field's PRESENCE decides whether KAS will load the on-disk profile,
  which is why the disk writer keeps an empty policy where the wire projection
  drops the key.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp.kas_agents import to_client_custom_agent
from kiro_crew.acp.kas_permissions import (
    CAPABILITY_BY_TOOL,
    WITHHELD_FROM_AUTO_APPROVE,
    allowed_tools_to_permissions,
)
from kiro_crew.agent import _seed_kas_permissions


def _rule(policy: dict, capability: str) -> dict:
    """The single rule for *capability*, asserting there is exactly one."""
    found = [r for r in policy["rules"] if r["capability"] == capability]
    assert len(found) == 1, f"expected one {capability} rule, got {found}"
    return found[0]


class TestBuiltinToolsBecomeCapabilities:
    def test_a_named_tool_maps_to_its_capability(self):
        policy = allowed_tools_to_permissions(["web_fetch"])
        assert policy == {"rules": [{"capability": "web_fetch", "effect": "allow"}]}

    def test_match_is_omitted_because_a_tool_entry_carries_no_resource_scope(self):
        """KAS reads a missing ``match`` as every resource — the intended meaning."""
        assert "match" not in _rule(allowed_tools_to_permissions(["web_search"]), "web_search")

    def test_rules_are_ordered_deterministically(self):
        """Two rebuilds of the same list must produce byte-identical output."""
        entries = ["web_search", "invoke_sub_agent", "web_fetch"]
        first = allowed_tools_to_permissions(entries)
        second = allowed_tools_to_permissions(list(reversed(entries)))
        assert first == second


class TestTheShellAndFilesystemFamiliesAreRefused:
    """The one place this module declines to translate something it could.

    Auto-approval is not "one fewer prompt", it is the ABSENCE of a permission
    request — and Crew's deny floor and sensitive-path check run on that request.
    A rule derived from a tool-name allowlist is also unscoped, because the
    allowlist carries no resource pattern, so the grant would be "any command" /
    "any path". Refusing means no rule, and no rule means prompt.
    """

    @pytest.mark.parametrize(
        "tool",
        sorted(WITHHELD_FROM_AUTO_APPROVE),
    )
    def test_a_withheld_tool_produces_no_rule(self, tool):
        assert allowed_tools_to_permissions([tool]) is None

    def test_a_withheld_tool_does_not_suppress_the_rest_of_the_list(self):
        policy = allowed_tools_to_permissions(["execute_bash", "web_fetch"])
        assert policy["rules"] == [{"capability": "web_fetch", "effect": "allow"}]

    def test_the_refusal_is_reported_at_info_because_it_reverses_the_spec(self, caplog):
        """Distinct from an unmappable entry: this one the spec explicitly asked for."""
        with caplog.at_level("INFO", logger="kiro_crew.acp.kas_permissions"):
            allowed_tools_to_permissions(["execute_bash"], agent_id="kirocrew")
        assert "not auto-approving execute_bash" in caplog.text

    def test_no_withheld_tool_is_also_in_the_capability_table(self):
        """A tool in both places would translate anyway; the two must not overlap."""
        assert WITHHELD_FROM_AUTO_APPROVE.isdisjoint(CAPABILITY_BY_TOOL)


class TestMcpEntries:
    def test_a_bare_server_becomes_a_one_level_glob(self):
        policy = allowed_tools_to_permissions(["@kirocrew-core"])
        assert _rule(policy, "mcp")["match"] == ["kirocrew-core/*"]

    def test_a_named_action_stays_exact(self):
        policy = allowed_tools_to_permissions(["@kirocrew-cron/cron_list"])
        assert _rule(policy, "mcp")["match"] == ["kirocrew-cron/cron_list"]

    def test_all_servers_share_one_rule(self):
        policy = allowed_tools_to_permissions(["@a", "@b"])
        assert _rule(policy, "mcp")["match"] == ["a/*", "b/*"]

    def test_a_server_wildcard_absorbs_its_own_per_tool_entries(self):
        """The real spec carries both; emitting both is misleading to read.

        A reviewer comparing policy against allowlist should not have to work out
        that one line already subsumes another.
        """
        policy = allowed_tools_to_permissions(
            ["@kirocrew-cron/cron_list", "@kirocrew-cron/cron_pause", "@kirocrew-cron"]
        )
        assert _rule(policy, "mcp")["match"] == ["kirocrew-cron/*"]

    def test_another_servers_per_tool_entry_is_not_absorbed(self):
        policy = allowed_tools_to_permissions(["@a", "@b/one"])
        assert _rule(policy, "mcp")["match"] == ["a/*", "b/one"]

    @pytest.mark.parametrize("bad", ["@", "@/tool"])
    def test_an_entry_naming_no_server_grants_nothing(self, bad):
        """Better to prompt than to emit a pattern that could match anything."""
        assert allowed_tools_to_permissions([bad]) is None


class TestTranslationNeverWidensAGrant:
    """A glob in the source list must not become a glob in the projected policy.

    Crew's own auto-approve check compares ``allowedTools`` entries literally, so
    ``@*`` there grants a server named ``*`` — nothing at all. Translated naively
    it becomes the pattern ``*/*``, which KAS resolves as every tool on every
    server: one line of text meaning "no grant" on one backend and "grant
    everything" on the other. The widening is what is under test, not the syntax.
    """

    @pytest.mark.parametrize(
        "entry",
        [
            "@*",
            "@*/*",
            "@kirocrew-*",
            "@kirocrew-core/*",
            "@kirocrew-core/cron_?",
            "@kirocrew-core/[abc]",
            "@{a,b}",
            "@!kirocrew-core",
        ],
    )
    def test_a_glob_reference_yields_no_rule(self, entry):
        assert allowed_tools_to_permissions([entry]) is None

    def test_a_glob_does_not_suppress_the_literal_entries_beside_it(self):
        policy = allowed_tools_to_permissions(["@*", "@kirocrew-core", "web_fetch"])
        assert _rule(policy, "mcp")["match"] == ["kirocrew-core/*"]
        assert _rule(policy, "web_fetch")["effect"] == "allow"

    def test_a_rejected_glob_is_explainable_from_the_log(self, caplog):
        with caplog.at_level("DEBUG", logger="kiro_crew.acp.kas_permissions"):
            allowed_tools_to_permissions(["@*"], agent_id="a")
        assert "@*" in caplog.text


class TestUnclassifiableEntriesFailClosed:
    @pytest.mark.parametrize("entry", ["introspect", "session", "report", "tool_search"])
    def test_a_tool_with_no_kas_capability_emits_no_rule(self, entry):
        """It keeps prompting, which is the same as having no policy for it."""
        assert allowed_tools_to_permissions([entry]) is None

    def test_it_does_not_suppress_the_entries_that_do_map(self):
        policy = allowed_tools_to_permissions(["introspect", "web_fetch"])
        assert policy["rules"] == [{"capability": "web_fetch", "effect": "allow"}]

    def test_the_names_are_reported_so_a_missing_grant_is_explainable(self, caplog):
        # Names the logger: left to the root logger this passes alone and fails in
        # the full suite, once something else has raised the package level.
        with caplog.at_level("DEBUG", logger="kiro_crew.acp.kas_permissions"):
            allowed_tools_to_permissions(["introspect"], agent_id="kirocrew")
        assert "introspect" in caplog.text

    @pytest.mark.parametrize("bad", [None, "fs_read", 42, {}])
    def test_a_non_list_is_not_coerced(self, bad):
        assert allowed_tools_to_permissions(bad) is None

    @pytest.mark.parametrize("junk", [[""], ["   "], [None, 7], []])
    def test_no_usable_entries_yields_no_policy_rather_than_an_empty_one(self, junk):
        """Absent and empty are different claims; only the caller knows which fits."""
        assert allowed_tools_to_permissions(junk) is None


class TestTheRealAllowlist:
    """The spec Crew actually ships, so a drift in it shows up here.

    Pinned as behaviour rather than a literal: what matters is which capabilities
    end up auto-approved and — more importantly — which do not.
    """

    ALLOWED = [
        "web_fetch",
        "web_search",
        "introspect",
        "session",
        "report",
        "@kirocrew-cron/cron_list",
        "@kirocrew-cron/cron_pause",
        "@kirocrew-core",
        "@notes-mcp",
        "@tickets-mcp",
        "@kirocrew-cron",
        "@weather-mcp",
    ]

    def test_the_excluded_tools_gain_no_grant(self):
        """``execute_bash``/``fs_write``/``code`` are held back on purpose.

        They are in ``tools`` but NOT ``allowedTools``, and with no rule KAS
        resolves them to ``ask`` — which is the whole point of the exclusion.
        """
        policy = allowed_tools_to_permissions(self.ALLOWED)
        granted = {r["capability"] for r in policy["rules"]}
        assert granted == {"mcp", "web_fetch", "web_search"}
        assert "shell" not in granted
        assert "fs_write" not in granted
        assert "fs_read" not in granted

    def test_the_computer_use_server_is_not_auto_approved(self):
        """It is absent from the allowlist, and it can drive a logged-in app."""
        policy = allowed_tools_to_permissions(self.ALLOWED)
        assert not any("computer" in p for p in _rule(policy, "mcp")["match"])


class TestTheDiskWriter:
    """What the agent spec on disk must say, which is NOT what the wire says.

    KAS classifies a JSON agent profile that carries kiro-cli-only fields and no
    KAS field as written for the other runtime, and skips it outright. So on disk
    the presence of ``permissions`` is what keeps the agent loadable at all —
    independently of whether it grants anything.
    """

    @staticmethod
    def _config(**over) -> dict:
        base: dict = {"name": "kirocrew", "tools": ["fs_read"], "allowedTools": ["web_fetch"]}
        base.update(over)
        return base

    def test_the_policy_is_derived_from_the_allowlist(self):
        config = self._config()
        _seed_kas_permissions(config)
        assert config["permissions"] == {"rules": [{"capability": "web_fetch", "effect": "allow"}]}

    def test_an_empty_policy_is_still_written_so_the_profile_stays_loadable(self):
        """Dropping the key would silently un-register the agent on KAS.

        ``{"rules": []}`` is both true (nothing is pre-approved) and enough to
        keep the file from being classified as kiro-cli-only. This is the one
        place the disk and wire behaviours deliberately differ.
        """
        config = self._config(allowedTools=[])
        _seed_kas_permissions(config)
        assert config["permissions"] == {"rules": []}

    def test_an_allowlist_of_only_unmappable_tools_still_marks_the_profile(self):
        config = self._config(allowedTools=["introspect", "session"])
        _seed_kas_permissions(config)
        assert config["permissions"] == {"rules": []}

    @pytest.mark.parametrize(
        "existing",
        [
            {"rules": [{"capability": "shell", "match": ["rm -rf *"], "effect": "deny"}]},
            {"rules": [{"capability": "shell", "effect": "allow"}]},
            {"rules": [], "policies": ["team-base"]},
            {"rules": []},
            {},
        ],
        ids=["deny", "blanket-allow", "policy-bundle", "empty-rules", "empty-block"],
    )
    def test_an_existing_block_is_never_touched_whatever_its_shape(self, existing):
        """Once the key exists it belongs to whoever edits the file.

        Recognising Crew's own output by shape and regenerating that was the
        first design and is gone: a blanket ``allow`` is exactly what a user
        writes too, so the rule that keeps a derived policy current is the same
        rule that silently destroys a hand-written one.
        """
        config = self._config(allowedTools=["@srv"], permissions=dict(existing))
        _seed_kas_permissions(config)
        assert config["permissions"] == existing

    def test_a_stale_block_is_the_accepted_cost_and_the_wire_covers_it(self):
        """Seeding-not-refreshing means the file can lag ``allowedTools``.

        Bounded on purpose: the wire projection derives afresh every session and
        outranks the file, so the block on disk is what applies when Crew is not
        injecting an agent at all.
        """
        config = self._config(
            allowedTools=["@srv"],
            permissions={"rules": [{"capability": "web_fetch", "effect": "allow"}]},
        )
        _seed_kas_permissions(config)
        assert config["permissions"]["rules"] == [{"capability": "web_fetch", "effect": "allow"}]
        assert to_client_custom_agent("kirocrew", {**config, "prompt": "p"}, "p")[
            "permissions"
        ] == {"rules": [{"capability": "mcp", "match": ["srv/*"], "effect": "allow"}]}

    def test_the_cli_only_key_is_kept_so_kiro_cli_still_works(self):
        """The spec has to serve both runtimes: KAS ignores what it does not use."""
        config = self._config()
        _seed_kas_permissions(config)
        assert config["allowedTools"] == ["web_fetch"]

    def test_it_is_idempotent(self):
        config = self._config()
        _seed_kas_permissions(config)
        once = dict(config["permissions"])
        _seed_kas_permissions(config)
        assert config["permissions"] == once
