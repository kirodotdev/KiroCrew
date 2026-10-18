"""Crew agent spec -> KAS ``ClientCustomAgent`` projection.

Each assertion here pins a constraint read off KAS's own zod schema
(``resolve-client-agents.ts``), not a preference: getting ``tools`` or ``prompt``
wrong produces an agent that registers successfully and then behaves nothing like
the one the operator configured.

Sibling files: ``test_kas_agents_mcp_servers.py`` covers the agent's MCP servers:
what the ``mcpServers`` block forwards or withholds, the stubbed names the runtime
supplies, the bound port and session key managed servers carry, and their hoist
into the session-level array. ``kas_agents_helpers.py`` holds the ``_spec``
builder both test files import.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest
from kas_agents_helpers import _spec

from kiro_crew import config as kiro_crew_config
from kiro_crew.acp import kas_agents
from kiro_crew.acp.kas_agents import (
    _KAS_FALLBACK_PROMPT,
    KAS_MAX_CUSTOM_AGENTS,
    KasAgentTranslationError,
    build_kas_custom_agents,
    load_agent_spec,
    resolve_prompt,
    to_client_custom_agent,
)
from kiro_crew.agent_discovery import (
    WELCOME_MESSAGE_MAX_CHARS,
    spec_welcome_message,
)
from kiro_crew.agent_files import KAS_RESERVED_AGENT_IDS, kas_wire_agent_id
from kiro_crew.platform.tool_names import (
    KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME,
    expand_kiro_cli_tool_names,
)


def _rule(policy, capability):
    """The single rule for ``capability`` in a projected policy."""
    return next(r for r in policy["rules"] if r["capability"] == capability)


class TestRequiredFields:
    """``id`` and ``prompt`` are the schema's only required members."""

    def test_id_and_prompt_are_emitted(self):
        out = to_client_custom_agent("kirocrew", _spec(), "You are Kiro.")
        assert out["id"] == "kirocrew"
        assert out["prompt"] == "You are Kiro."

    def test_empty_id_is_refused(self):
        with pytest.raises(KasAgentTranslationError):
            to_client_custom_agent("", _spec(), "p")

    def test_empty_prompt_is_refused(self):
        with pytest.raises(KasAgentTranslationError):
            to_client_custom_agent("kirocrew", _spec(), "   ")


class TestReservedIds:
    """An id the engine keeps for itself goes out under a wire alias.

    kiro-cli 2.23.0 accepts a ``customAgents`` batch carrying any of these ids
    and then does one of two things, neither of them an error: ``default`` is
    dropped from ``availableModes`` outright, and a built-in mode id (``vibe``,
    ``spec``, ``quick-spec``, ``bug-fix``, ``plan``, ``autonomous``) keeps the
    ENGINE's definition, so ``set_mode`` would run the built-in with the
    crewmate's name on it. A crewmate bound to a private copy named after
    itself hits the first case (the seeded crewmate is ``default``), which is
    exactly the setup a kiro-cli user carries over when switching to KAS. So
    the projection keeps the crewmate's id and only the wire envelope renames
    it (see ``test_kas_mode_binding``).
    """

    RESERVED = ("default", "vibe", "spec", "quick-spec", "bug-fix", "plan", "autonomous")

    def test_the_reserved_set_is_the_measured_one(self):
        assert KAS_RESERVED_AGENT_IDS == frozenset(self.RESERVED)

    @pytest.mark.parametrize("agent_id", RESERVED)
    def test_a_reserved_id_is_projected_under_its_own_name(self, agent_id):
        out = to_client_custom_agent(agent_id, _spec(name=agent_id), "You are Kiro.")
        assert out["id"] == agent_id

    @pytest.mark.parametrize("agent_id", RESERVED)
    def test_a_reserved_id_gets_a_wire_alias_off_the_reserved_set(self, agent_id):
        wire = kas_wire_agent_id(agent_id)
        assert wire == f"{agent_id}-crewmate"
        assert wire not in KAS_RESERVED_AGENT_IDS

    def test_the_batch_builder_accepts_a_reserved_id(self, tmp_path):
        batch = build_kas_custom_agents(tmp_path, "default", _spec(name="default"))
        assert [a["id"] for a in batch] == ["default"]

    @pytest.mark.parametrize(
        "agent_id",
        ["Default", "DEFAULT", "Vibe", "Spec", "PLAN", "default-2", "kiro_default", "kirocrew"],
    )
    def test_the_match_is_exact_and_case_sensitive(self, agent_id):
        # Measured alongside the reserved ids in one session/new: each of these
        # registered as an ordinary client agent (origin: client, the injected
        # description), so widening the match would refuse working ids.
        out = to_client_custom_agent(agent_id, _spec(name=agent_id), "You are Kiro.")
        assert out["id"] == agent_id
        assert kas_wire_agent_id(agent_id) == agent_id


class TestToolsFailClosed:
    """``tools`` absent means NO tools on KAS (``agent.tools ?? []``).

    So the list must always be emitted, and a spec that does not state one must
    not be widened into an allowlist nobody wrote.
    """

    def test_list_is_passed_through(self):
        """The exact list, in the spec's order: each kiro-cli name followed by its
        KAS family, the MCP ref untouched, and nothing else. An exact pin is what
        catches an entry the projection starts injecting that nobody wrote."""
        out = to_client_custom_agent("a", _spec(), "p")
        assert out["tools"] == [
            "fs_read",
            "read_file",
            "list_directory",
            "fs_write",
            "str_replace",
            "fs_append",
            "@kirocrew-core",
        ]

    def test_mcp_server_shorthand_survives(self):
        """KAS tags every MCP tool ``@<server>``, so Crew's existing syntax works."""
        out = to_client_custom_agent("a", _spec(tools=["@kirocrew-cron"]), "p")
        assert out["tools"] == ["@kirocrew-cron"]

    def test_star_becomes_the_all_tools_literal(self):
        """``"*"`` is a distinct type in the schema, not a list member."""
        assert to_client_custom_agent("a", _spec(tools=["*"]), "p")["tools"] == "*"
        assert to_client_custom_agent("a", _spec(tools="*"), "p")["tools"] == "*"

    @pytest.mark.parametrize("bad", [None, {}, 7, "fs_read"])
    def test_absent_or_malformed_yields_an_empty_allowlist(self, bad):
        spec = _spec()
        spec["tools"] = bad
        if bad is None:
            del spec["tools"]
        assert to_client_custom_agent("a", spec, "p")["tools"] == []

    def test_non_string_entries_are_discarded(self):
        out = to_client_custom_agent("a", _spec(tools=["fs_read", 3, "", None]), "p")
        assert out["tools"] == ["fs_read", "read_file", "list_directory"]


class TestTheSpecIsSpelledInKasToolIds:
    """The defect: a spec written for kiro-cli mounts nothing it names on KAS.

    ``matchesPattern`` (``tools/tool-filter.ts``) compares an allowlist entry to
    a tool's exact id or a tag and translates nothing; on KAS ``fs_read`` is a
    policy capability, not a tool, and ``grep`` / ``glob`` are no tool at all. So
    the shipped spec reached the engine holding ``execute_bash``, ``fs_write``,
    ``web_fetch`` and its MCP refs -- and no way to read, list or search a file.
    """

    def test_each_kiro_cli_read_tool_carries_its_kas_family(self):
        out = to_client_custom_agent("a", _spec(tools=["fs_read", "grep", "glob"]), "p")
        assert out["tools"] == [
            "fs_read",
            "read_file",
            "list_directory",
            "grep",
            "grep_search",
            "glob",
            "file_search",
        ]

    def test_the_write_family_is_bounded_by_what_kiro_cli_s_fs_write_can_do(self):
        """kiro-cli's ``fs_write`` creates, replaces, inserts and appends; it
        cannot delete. KAS files ``delete_file`` under the same ``fs_write``
        capability, but a spec that mounted ``fs_write`` never granted deletion,
        and under a trust mode that skips the prompt a mounted tool RUNS -- so
        the family stops at what the kiro-cli tool does. Every member is still
        refused an auto-approve rule by ``WITHHELD_FROM_AUTO_APPROVE``."""
        out = to_client_custom_agent("a", _spec(tools=["fs_write"]), "p")
        assert out["tools"] == ["fs_write", "str_replace", "fs_append"]
        assert "delete_file" not in out["tools"]
        assert "permissions" not in out or not any(
            r["capability"] == "fs_write" and r["effect"] == "allow"
            for r in out["permissions"]["rules"]
        )

    def test_delete_file_mounts_only_when_the_spec_names_it(self):
        out = to_client_custom_agent("a", _spec(tools=["fs_write", "delete_file"]), "p")
        assert out["tools"] == ["fs_write", "str_replace", "fs_append", "delete_file"]

    def test_no_family_widens_past_its_kiro_cli_tool_s_own_verbs(self):
        """The deletion rule, stated for the table as a whole: no kiro-cli name
        expands to a KAS tool with a verb the kiro-cli tool lacks."""
        assert "delete_file" not in {
            t for fam in KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME.values() for t in fam
        }

    def test_the_spec_s_own_name_is_kept_beside_the_family(self):
        """Adds, never substitutes: a name both engines know keeps matching."""
        out = to_client_custom_agent("a", _spec(tools=["fs_read"]), "p")
        assert out["tools"][0] == "fs_read"

    def test_names_both_engines_share_and_mcp_refs_pass_through_unchanged(self):
        shared = ["execute_bash", "code", "web_fetch", "tool_search", "@kirocrew-core"]
        out = to_client_custom_agent("a", _spec(tools=shared), "p")
        assert out["tools"] == shared

    def test_a_spec_already_in_kas_vocabulary_is_not_duplicated(self):
        out = to_client_custom_agent(
            "a", _spec(tools=["read_file", "fs_read", "str_replace", "fs_write"]), "p"
        )
        assert out["tools"] == [
            "read_file",
            "fs_read",
            "list_directory",
            "str_replace",
            "fs_write",
            "fs_append",
        ]

    def test_excluded_tools_are_widened_the_same_way(self):
        """The restricting direction: excluding ``fs_read`` for kiro-cli must not
        leave ``read_file`` mounted on KAS."""
        out = to_client_custom_agent("a", _spec(excludedTools=["fs_read", "@x/y"]), "p")
        assert out["excludedTools"] == ["fs_read", "read_file", "list_directory", "@x/y"]

    def test_excluding_fs_write_also_excludes_delete_file(self):
        """Opposite polarity from mounting: a mount family stops at the kiro-cli
        tool's own verbs, an exclusion reaches everything governed under the
        name -- or excluding ``fs_write`` leaves ``delete_file`` mounted."""
        out = to_client_custom_agent(
            "a", _spec(tools=["fs_write", "delete_file"], excludedTools=["fs_write"]), "p"
        )
        assert out["excludedTools"] == ["fs_write", "str_replace", "fs_append", "delete_file"]

    def test_the_table_names_only_ids_kas_registers_on_a_standalone_session(self):
        """Pins the vocabulary to the engine's, via the recorded registry
        ``test/fixtures/kas_builtin_tool_ids.json``: every family member is an
        id the engine registers, and every key is a kiro-cli built-in the shipped
        spec can name. A tag (``read``, ``@builtin``) would also match but grants
        by class rather than by tool, so none appears here."""
        registry = Path(__file__).resolve().parent / "fixtures" / "kas_builtin_tool_ids.json"
        registered = set(json.loads(registry.read_text(encoding="utf-8"))["registered"])
        for kiro_cli_name, family in KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME.items():
            assert kiro_cli_name in {"fs_read", "fs_write", "grep", "glob"}
            assert set(family) <= registered, kiro_cli_name
            assert kiro_cli_name not in family, "a family lists the OTHER engine's ids"

    def test_expand_is_pure_and_order_preserving(self):
        entries = ["@a", "fs_read", "@b", "fs_read"]
        assert expand_kiro_cli_tool_names(entries) == [
            "@a",
            "fs_read",
            "read_file",
            "list_directory",
            "@b",
        ]
        assert entries == ["@a", "fs_read", "@b", "fs_read"], "input untouched"
        assert expand_kiro_cli_tool_names([]) == []


class TestDeliberateOmissions:
    """Fields left out on purpose; each would misbehave if projected.

    ``model`` would compete with the dedicated model verb. ``permissions`` is NOT
    in this list — see :class:`TestPermissionsProjection`; it is absent only when
    the spec gives nothing to derive it from. ``mcpServers`` is not in this
    list either: omitting it left a KAS session with ``@server`` refs naming
    nothing — see :class:`TestMcpServersProjection`.

    ``effortLevel`` and ``dispatchKind`` are absent for a different reason again:
    ``ClientCustomAgentSchema`` has no slot for either, so there is nothing to
    project into.
    """

    @pytest.mark.parametrize("key", ["model", "effortLevel", "dispatchKind"])
    def test_key_is_not_projected(self, key):
        assert key not in to_client_custom_agent("a", _spec(**{key: "x"}), "p")


class TestWelcomeMessageProjection:
    """``welcomeMessage`` is a wire field, read by the dashboard's own function.

    The hint is authored in a user-writable, tool-shared directory, so the wire
    must not become a second, unbounded path for it: the assertions below pin
    that the projected value is the SAME reading the transcript renders — capped
    at ``WELCOME_MESSAGE_MAX_CHARS``, whitespace-stripped, non-string treated as
    absent — rather than the raw spec value.
    """

    def test_projected_when_present(self):
        out = to_client_custom_agent("a", _spec(welcomeMessage="Ask me for slides."), "p")
        assert out["welcomeMessage"] == "Ask me for slides."

    def test_absent_when_the_spec_has_none(self):
        assert "welcomeMessage" not in to_client_custom_agent("a", _spec(), "p")

    @pytest.mark.parametrize("value", ["", "   \n\t ", 17, None, {"a": 1}, ["x"]])
    def test_blank_and_non_string_read_as_absent(self, value):
        out = to_client_custom_agent("a", _spec(welcomeMessage=value), "p")
        assert "welcomeMessage" not in out

    def test_surrounding_whitespace_is_stripped(self):
        out = to_client_custom_agent("a", _spec(welcomeMessage="\n  hi  \n"), "p")
        assert out["welcomeMessage"] == "hi"

    def test_capped_at_the_transcript_ceiling(self):
        long_hint = "y" * (WELCOME_MESSAGE_MAX_CHARS + 500)
        out = to_client_custom_agent("a", _spec(welcomeMessage=long_hint), "p")
        assert len(out["welcomeMessage"]) == WELCOME_MESSAGE_MAX_CHARS
        assert out["welcomeMessage"].endswith("\u2026")

    def test_the_wire_value_equals_what_the_transcript_would_render(self):
        """One reader for both surfaces, so a hint cannot differ between them."""
        for hint in ["plain", "  padded  ", "z" * (WELCOME_MESSAGE_MAX_CHARS + 1)]:
            spec = _spec(welcomeMessage=hint)
            out = to_client_custom_agent("a", spec, "p")
            assert out.get("welcomeMessage", "") == spec_welcome_message(spec)


class TestInclusionFlagProjection:
    """``includeMcpJson`` / ``includePowers``: forwarded, never defaulted.

    An ABSENT flag is deliberately left absent rather than given a default,
    because absence does not mean the same thing on the two hosts Crew writes
    specs for: kiro-cli reads an absent ``includeMcpJson`` as ``True``, KAS's own
    disk schema defaults it to ``False``. Crew picking either would ship one
    host's answer to the other. KAS loses nothing by the silence — its wire
    schema has no default and its tool filter resolves an absent flag to
    ``false``, which is already its disk default.
    """

    @pytest.mark.parametrize("flag", ["includeMcpJson", "includePowers"])
    @pytest.mark.parametrize("value", [True, False])
    def test_a_bool_is_forwarded_verbatim(self, flag, value):
        out = to_client_custom_agent("a", _spec(**{flag: value}), "p")
        assert out[flag] is value

    @pytest.mark.parametrize("flag", ["includeMcpJson", "includePowers"])
    def test_absent_stays_absent_rather_than_defaulted(self, flag):
        spec = _spec()
        spec.pop(flag, None)
        assert flag not in to_client_custom_agent("a", spec, "p")

    @pytest.mark.parametrize("flag", ["includeMcpJson", "includePowers"])
    @pytest.mark.parametrize("value", ["true", "no", 1, 0, None, [], {}])
    def test_a_non_bool_is_dropped_rather_than_coerced(self, flag, value):
        """``z.boolean()`` rejects it, and a failing agent is dropped WHOLE."""
        out = to_client_custom_agent("a", _spec(**{flag: value}), "p")
        assert flag not in out

    def test_neither_flag_widens_the_permissions_policy(self):
        """They reveal tools; they do not auto-approve them."""
        bare = to_client_custom_agent("a", _spec(allowedTools=["web_fetch"]), "p")
        widened = to_client_custom_agent(
            "a",
            _spec(allowedTools=["web_fetch"], includeMcpJson=True, includePowers=True),
            "p",
        )
        assert widened["permissions"] == bare["permissions"]
        assert widened["tools"] == bare["tools"]


class TestOptionalPassThrough:
    def test_description_when_present(self):
        assert to_client_custom_agent("a", _spec(), "p")["description"] == "the crew agent"

    def test_description_omitted_when_blank(self):
        assert "description" not in to_client_custom_agent("a", _spec(description=""), "p")

    def test_include_mcp_json_is_a_bool_passthrough(self):
        assert to_client_custom_agent("a", _spec(), "p")["includeMcpJson"] is False
        assert "includeMcpJson" not in to_client_custom_agent("a", _spec(includeMcpJson="no"), "p")

    def test_resources_and_excluded_tools_when_non_empty(self):
        out = to_client_custom_agent(
            "a", _spec(resources=["file:///x.md"], excludedTools=["execute_bash"]), "p"
        )
        assert out["resources"] == ["file:///x.md"]
        assert out["excludedTools"] == ["execute_bash"]

    def test_empty_lists_are_omitted_rather_than_sent(self):
        out = to_client_custom_agent("a", _spec(resources=[], excludedTools=[]), "p")
        assert "resources" not in out
        assert "excludedTools" not in out


class TestPermissionsProjection:
    """``allowedTools`` has no slot on the wire; its MEANING travels as a policy.

    Omitting the field is not neutral: with no policy KAS resolves every request
    to ``ask``, so an injected agent would prompt for the whole list its kiro-cli
    twin auto-approves. The translation itself is pinned in
    ``test_kas_permissions.py``; here we pin only that it is wired in, and that an
    authored block reaches the same ceiling rather than being dropped or obeyed.
    """

    def test_the_allowlist_is_translated_rather_than_dropped(self):
        out = to_client_custom_agent("a", _spec(allowedTools=["web_fetch"]), "p")
        assert out["permissions"] == {"rules": [{"capability": "web_fetch", "effect": "allow"}]}

    def test_the_cli_only_key_itself_never_goes_on_the_wire(self):
        out = to_client_custom_agent("a", _spec(allowedTools=["web_fetch"]), "p")
        assert "allowedTools" not in out

    def test_a_spec_with_nothing_to_derive_omits_the_field(self):
        """Absent says "this spec never described auto-approval", which is true."""
        assert "permissions" not in to_client_custom_agent("a", _spec(), "p")

    def test_an_unclassifiable_allowlist_omits_the_field(self):
        out = to_client_custom_agent("a", _spec(allowedTools=["introspect"]), "p")
        assert "permissions" not in out

    def test_an_authored_block_is_intersected_with_the_ceiling_not_dropped(self):
        """The author's block is a second input, and it is wired to the same
        ceiling. The merge itself is pinned in ``test_kas_permissions.py``; here we
        pin only that the projection consults it. A scoped ``allow`` is used because
        a bare one is the ``allowedTools`` list's own to make."""
        mine = {
            "rules": [{"capability": "web_search", "match": ["example.com"], "effect": "allow"}]
        }
        out = to_client_custom_agent("a", _spec(allowedTools=["web_fetch"], permissions=mine), "p")
        assert out["permissions"]["rules"] == [
            {"capability": "web_search", "match": ["example.com"], "effect": "allow"},
            {"capability": "web_fetch", "effect": "allow"},
        ]

    def test_a_stale_allow_the_allowlist_dropped_is_not_put_back(self):
        """Crew's seeder preserves the block it wrote, so it can lag the list. The
        list is re-derived every projection and owns a grant of that shape."""
        stale = {"rules": [{"capability": "web_search", "effect": "allow"}]}
        out = to_client_custom_agent("a", _spec(allowedTools=["web_fetch"], permissions=stale), "p")
        assert out["permissions"]["rules"] == [{"capability": "web_fetch", "effect": "allow"}]

    def test_a_pure_kas_agent_reaches_the_wire_with_its_own_policy(self):
        """``permissions`` authored, no ``allowedTools``: nothing to derive from, so
        an omitted field would resolve every request to ``ask`` and prompt for each
        of the calls the author had just written a policy for."""
        mine = {"rules": [{"capability": "mcp", "match": ["srv/*"], "effect": "allow"}]}
        out = to_client_custom_agent("a", _spec(permissions=mine), "p")
        assert out["permissions"] == mine

    def test_an_authored_block_cannot_smuggle_a_shell_grant_past_the_allowlist(self):
        """The sharp case: the block grants the one family the allowlist refuses."""
        mine = {"rules": [{"capability": "shell", "effect": "allow"}]}
        out = to_client_custom_agent("a", _spec(allowedTools=[], permissions=mine), "p")
        assert "permissions" not in out

    def test_an_authored_allow_is_put_to_the_projection_time_ceiling(self, monkeypatch):
        """The same re-ask the derived rules get, for the same reason: projection
        reads a file, and the file can predate the ceiling that now governs it."""
        monkeypatch.setattr(kas_agents, "may_skip_gate_now", lambda ref: ref != "@denied-srv")
        mine = {
            "rules": [
                {"capability": "mcp", "match": ["denied-srv/*"], "effect": "allow"},
                {"capability": "mcp", "match": ["ok-srv/*"], "effect": "allow"},
            ]
        }
        out = to_client_custom_agent("a", _spec(permissions=mine), "p")
        assert out["permissions"]["rules"] == [
            {"capability": "mcp", "match": ["ok-srv/*"], "effect": "allow"}
        ]

    def test_a_withheld_authored_allow_is_recorded_in_the_security_event_log(self, monkeypatch):
        """The same trail the derived path emits, through the same writer, because
        it is the same decision about the same ceiling."""
        monkeypatch.setattr(kas_agents, "may_skip_gate_now", lambda ref: False)
        events: list[dict] = []
        monkeypatch.setattr(
            kas_agents,
            "sel",
            lambda: types.SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
        )
        mine = {"rules": [{"capability": "mcp", "match": ["denied-srv/*"], "effect": "allow"}]}
        assert "permissions" not in to_client_custom_agent("a", _spec(permissions=mine), "p")
        assert [e["operation"] for e in events] == ["mcp_auto_approve_withheld"]
        assert "@denied-srv" in events[0]["resources"]
        assert "governance ceiling" in events[0]["resources"]

    def test_a_relayed_authored_grant_is_recorded_under_its_own_operation(self, monkeypatch):
        """A relay must never be counted as a withhold: it is the opposite decision,
        and it is the half a log of refusals cannot answer."""
        events: list[dict] = []
        monkeypatch.setattr(
            kas_agents,
            "sel",
            lambda: types.SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
        )
        mine = {"rules": [{"capability": "mcp", "match": ["ok-srv/*"], "effect": "allow"}]}
        to_client_custom_agent("a", _spec(permissions=mine), "p")
        assert [e["operation"] for e in events] == ["kas_authored_permissions_relayed"]
        assert "projected with auto-approve" in events[0]["resources"]

    def test_an_unparseable_block_does_not_abort_the_session(self):
        """`effect: ["allow"]` is unhashable, and nothing between the projection and
        session creation catches a ``TypeError``."""
        out = to_client_custom_agent(
            "a",
            _spec(
                allowedTools=["web_fetch"],
                permissions={"rules": [{"capability": "shell", "effect": ["allow"]}]},
            ),
            "p",
        )
        assert out["permissions"]["rules"] == [{"capability": "web_fetch", "effect": "allow"}]

    def test_an_authored_deny_travels_even_where_the_ceiling_withholds(self, monkeypatch):
        monkeypatch.setattr(kas_agents, "may_skip_gate_now", lambda ref: False)
        mine = {"rules": [{"capability": "shell", "match": ["rm *"], "effect": "deny"}]}
        out = to_client_custom_agent("a", _spec(allowedTools=["web_fetch"], permissions=mine), "p")
        assert out["permissions"]["rules"] == mine["rules"]

    def test_a_malformed_block_leaves_the_derivation_standing(self):
        out = to_client_custom_agent(
            "a",
            _spec(allowedTools=["web_fetch"], permissions={"rules": "all of them"}),
            "p",
        )
        assert out["permissions"]["rules"] == [{"capability": "web_fetch", "effect": "allow"}]


class TestTheCeilingIsReAskedAtProjectionTime:
    """The write-time ceiling check is not enough, because projection READS a file.

    The five writers of an ``allowedTools`` list consult the ceiling when they
    write, so a freshly rebuilt spec is already clean. A spec written on an
    ungoverned host, restored from a backup, or edited by hand is not — and
    projection is the last place to notice before the grant reaches the backend.
    """

    def test_a_withheld_entry_is_dropped_from_the_projected_policy(self, monkeypatch):
        monkeypatch.setattr(kas_agents, "may_skip_gate_now", lambda ref: ref != "@denied-srv")
        out = to_client_custom_agent("a", _spec(allowedTools=["@denied-srv", "@ok-srv"]), "p")
        assert _rule(out["permissions"], "mcp")["match"] == ["ok-srv/*"]

    def test_withholding_everything_omits_the_field(self, monkeypatch):
        monkeypatch.setattr(kas_agents, "may_skip_gate_now", lambda ref: False)
        out = to_client_custom_agent("a", _spec(allowedTools=["web_fetch"]), "p")
        assert "permissions" not in out

    def test_the_withholding_is_reported_so_a_missing_grant_is_explainable(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(kas_agents, "may_skip_gate_now", lambda ref: False)
        with caplog.at_level("INFO", logger="kiro_crew.acp.kas_agents"):
            to_client_custom_agent("kirocrew", _spec(allowedTools=["web_fetch"]), "p")
        assert "withholds auto-approval for web_fetch" in caplog.text

    def test_an_ungoverned_host_keeps_every_grant(self, monkeypatch):
        """``may_skip_gate_now`` answers True with no ceiling installed."""
        monkeypatch.setattr(kas_agents, "may_skip_gate_now", lambda ref: True)
        out = to_client_custom_agent("a", _spec(allowedTools=["web_fetch"]), "p")
        assert out["permissions"]["rules"] == [{"capability": "web_fetch", "effect": "allow"}]

    def test_the_withhold_is_recorded_in_the_security_event_log(self, monkeypatch):
        """A permission decision, so it belongs in SEL and not only in a log line.

        The other three writers that produce this state (app-agent
        materialization, the host shared-MCP sync, doctor's auto-fix) all emit the
        same ``mcp_auto_approve_withheld`` event. Projection is the one whose
        input is a file it did not write, so a stale grant is likeliest to be
        withheld here — the path that most needs the trail must not be the one
        without it.
        """
        monkeypatch.setattr(kas_agents, "may_skip_gate_now", lambda ref: False)
        events: list[dict] = []
        monkeypatch.setattr(
            kas_agents,
            "sel",
            lambda: types.SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
        )

        to_client_custom_agent("kirocrew", _spec(allowedTools=["@denied-srv"]), "p")

        assert len(events) == 1
        assert events[0]["operation"] == "mcp_auto_approve_withheld"
        assert events[0]["source"] == "kas_agent_projection"
        assert "@denied-srv" in events[0]["resources"]
        assert "kirocrew" in events[0]["resources"]

    def test_nothing_withheld_emits_no_event(self, monkeypatch):
        monkeypatch.setattr(kas_agents, "may_skip_gate_now", lambda ref: True)
        events: list[dict] = []
        monkeypatch.setattr(
            kas_agents,
            "sel",
            lambda: types.SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
        )

        to_client_custom_agent("a", _spec(allowedTools=["web_fetch"]), "p")

        assert events == []

    def test_an_audit_failure_does_not_undo_the_withhold(self, monkeypatch):
        """The withhold has already happened and is the safe direction.

        Failing the projection because the audit sink is unavailable would turn a
        missing log line into a session that cannot start.
        """
        monkeypatch.setattr(kas_agents, "may_skip_gate_now", lambda ref: False)

        def _broken():
            raise RuntimeError("no sink")

        monkeypatch.setattr(kas_agents, "sel", _broken)

        out = to_client_custom_agent("a", _spec(allowedTools=["web_fetch"]), "p")
        assert "permissions" not in out


class TestKeysTheWireCannotCarry:
    """A key with no slot in the schema is reported — and only reported once.

    The wording matters as much as the level: the previous message said "no KAS
    equivalent", which reads as "KAS cannot do this" and sends a reader looking
    for a missing feature. ``hooks`` in particular IS a KAS feature; what is
    missing is a way to deliver it on an agent injected over the wire.

    Every ``at_level`` here names the logger. Left to the root logger it passes
    alone and fails in the full suite (something else has raised the package
    level by then), and the negative assertions would pass VACUOUSLY.
    """

    def test_the_keys_are_named(self, caplog):
        spec = _spec(hooks={"postToolUse": []}, toolsSettings={"x": 1})
        with caplog.at_level("DEBUG", logger="kiro_crew.acp.kas_agents"):
            to_client_custom_agent("kirocrew", spec, "p")
        assert "toolsSettings" in caplog.text

    def test_it_does_not_warn_on_every_session(self, caplog):
        """Constant payload on a per-session path: at WARNING it is pure noise."""
        with caplog.at_level("WARNING"):
            to_client_custom_agent("kirocrew", _spec(toolsSettings={"x": 1}), "p")
        assert caplog.text.strip() == ""

    def test_the_translated_key_is_not_reported_as_lost(self, caplog):
        with caplog.at_level("DEBUG", logger="kiro_crew.acp.kas_agents"):
            to_client_custom_agent("kirocrew", _spec(allowedTools=["web_fetch"]), "p")
        assert "allowedTools" not in caplog.text

    def test_nothing_logged_when_the_spec_has_none(self, caplog):
        with caplog.at_level("DEBUG", logger="kiro_crew.acp.kas_agents"):
            to_client_custom_agent("kirocrew", _spec(), "p")
        assert "cannot carry" not in caplog.text


class TestPromptResolution:
    """KAS requires resolved content; a ``file://`` prompt is ours to read."""

    def test_inline_prompt_is_returned_as_is(self, tmp_path):
        assert resolve_prompt({"prompt": "hello"}, agent_id="a", agents_dir=tmp_path) == "hello"

    def test_file_uri_is_inlined(self, tmp_path):
        p = tmp_path / "prompt.md"
        p.write_text("from disk", encoding="utf-8")
        assert (
            resolve_prompt({"prompt": f"file://{p}"}, agent_id="a", agents_dir=tmp_path)
            == "from disk"
        )

    def test_missing_file_is_an_error_not_a_silent_empty_prompt(self, tmp_path):
        with pytest.raises(KasAgentTranslationError):
            resolve_prompt(
                {"prompt": f"file://{tmp_path / 'nope.md'}"}, agent_id="a", agents_dir=tmp_path
            )

    def test_empty_file_is_refused(self, tmp_path):
        p = tmp_path / "empty.md"
        p.write_text("   ", encoding="utf-8")
        with pytest.raises(KasAgentTranslationError):
            resolve_prompt({"prompt": f"file://{p}"}, agent_id="a", agents_dir=tmp_path)

    def test_sensitive_prompt_path_is_refused_before_any_read(self, tmp_path):
        # A spec whose prompt points at a credential file must NOT be inlined and
        # shipped to KAS. The guard fires on the path, before read_text, so it
        # holds even if the file does not exist.
        for target in ("file://~/.aws/credentials", "file://~/.ssh/id_rsa"):
            with pytest.raises(KasAgentTranslationError, match="not an allowed location"):
                resolve_prompt({"prompt": target}, agent_id="a", agents_dir=tmp_path)

    def test_proc_environ_prompt_is_refused(self, tmp_path):
        # /proc/self/environ would leak the gateway's own environment. It must be
        # refused either way: on POSIX it is absolute and caught by the pseudo-fs
        # denylist ("not an allowed location"); on Windows it is not absolute (no
        # drive letter), so it is treated as a relative prompt and rejected for
        # escaping the agent directory. Both are fail-closed rejections.
        with pytest.raises(
            KasAgentTranslationError, match="not an allowed location|escapes the agent directory"
        ):
            resolve_prompt(
                {"prompt": "file:///proc/self/environ"}, agent_id="a", agents_dir=tmp_path
            )

    def test_relative_prompt_anchors_to_agents_dir_not_cwd(self, tmp_path):
        sub = tmp_path / "prompts"
        sub.mkdir()
        (sub / "expert.md").write_text("expert prompt", encoding="utf-8")
        out = resolve_prompt(
            {"prompt": "file://./prompts/expert.md"}, agent_id="a", agents_dir=tmp_path
        )
        assert out == "expert prompt"

    def test_relative_prompt_escaping_the_agents_dir_is_refused(self, tmp_path):
        with pytest.raises(KasAgentTranslationError, match="escapes"):
            resolve_prompt({"prompt": "file://../../etc/passwd"}, agent_id="a", agents_dir=tmp_path)

    @pytest.mark.parametrize("bad", [None, "", "   "])
    def test_empty_prompt_falls_back_to_the_kas_constant(self, bad, tmp_path, caplog):
        # KAS requires a non-empty prompt; a missing or blank string is an
        # intentionally prompt-less agent (e.g. kirocrew-lite ships "prompt": ""),
        # so the projection substitutes the small inline fallback constant
        # instead of crashing the session.
        out = resolve_prompt({"prompt": bad}, agent_id="kirocrew-lite", agents_dir=tmp_path)
        assert out == _KAS_FALLBACK_PROMPT
        assert "falling back to the lightweight KAS prompt" in caplog.text

    @pytest.mark.parametrize("bad", [7, 3.14, True, [], {}, ["x"]])
    def test_non_string_prompt_is_refused_not_defaulted(self, bad, tmp_path):
        # A non-string prompt is a malformed spec, not a prompt-less one — it
        # must fail loud rather than silently run with the fallback text.
        with pytest.raises(KasAgentTranslationError, match="must be a string"):
            resolve_prompt({"prompt": bad}, agent_id="a", agents_dir=tmp_path)

    def test_a_real_prompt_wins_over_the_fallback(self, tmp_path):
        # The fallback only fires for an empty spec.
        assert resolve_prompt({"prompt": "own"}, agent_id="a", agents_dir=tmp_path) == "own"

    def test_non_utf8_file_prompt_is_refused_not_crashing(self, tmp_path):
        # A non-UTF-8 agent-supplied file:// prompt must fail loud as
        # "unreadable", never raise a raw UnicodeDecodeError out of KAS session
        # creation.
        p = tmp_path / "prompt.md"
        p.write_bytes(b"\xff\xfe not utf-8")
        with pytest.raises(KasAgentTranslationError, match="unreadable"):
            resolve_prompt({"prompt": f"file://{p}"}, agent_id="a", agents_dir=tmp_path)

    def test_build_projects_a_prompt_less_spec_with_the_fallback(self, tmp_path):
        (tmp_path / "kirocrew-lite.json").write_text(
            json.dumps({"name": "kirocrew-lite", "tools": [], "prompt": ""}), encoding="utf-8"
        )
        spec = load_agent_spec(tmp_path, "kirocrew-lite")
        agents = build_kas_custom_agents(tmp_path, "kirocrew-lite", spec)
        assert agents[0]["prompt"] == _KAS_FALLBACK_PROMPT
        # Tool restriction is preserved — the fallback only supplies a prompt.
        assert agents[0]["tools"] == []


def test_the_batch_cap_matches_the_schema():
    """KAS declares ``customAgents: z.array(z.unknown()).max(50)``."""
    assert KAS_MAX_CUSTOM_AGENTS == 50


class TestAgainstTheRealBundledSpec:
    """Translate the spec Crew actually ships, not a hand-written stand-in.

    The fixtures above encode what the schema allows; this one catches the case
    where the real spec's shape has drifted away from them.
    """

    @staticmethod
    def _bundled() -> dict:
        path = Path(kiro_crew_config.__file__).resolve().parent / "defaults.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_the_crew_agent_projects_with_its_tools_intact(self):
        spec = self._bundled()
        out = to_client_custom_agent(spec["name"], spec, "resolved prompt text")

        assert out["id"] == "kirocrew"
        assert out["prompt"] == "resolved prompt text"
        # The MCP shorthand is most of Crew's tool surface; losing it would leave
        # the agent nominally configured but unable to reach its own tools.
        assert any(t.startswith("@") for t in out["tools"])
        assert "fs_read" in out["tools"]
        # The shipped spec is written for kiro-cli; on KAS the same list must
        # name the ids the engine actually registers, or the agent runs
        # read-blind (the defect this projection now covers). Pinned EXACTLY --
        # the spec's own list with each kiro-cli name followed by its family and
        # nothing else -- so an entry the projection starts injecting that the
        # spec never wrote fails here rather than riding a subset check.
        assert out["tools"] == expand_kiro_cli_tool_names(spec["tools"])
        assert {"read_file", "list_directory", "grep_search", "file_search"} <= set(out["tools"])
        assert "delete_file" not in out["tools"]

    def test_the_real_spec_carries_keys_KAS_cannot_take(self):
        """Guards the drop path against the actual spec, not a synthetic one."""
        spec = self._bundled()
        assert spec.get("allowedTools"), "expected the real spec to still carry allowedTools"
        out = to_client_custom_agent(spec["name"], spec, "p")
        assert "allowedTools" not in out

    def test_the_bundled_template_carries_refs_but_declares_no_servers(self):
        """Why the ref list alone cannot be the fixture for the test below.

        ``defaults.json`` ships ``@kirocrew-*`` refs with NO ``mcpServers`` key:
        the entries are written at rebuild time by ``agent.build_agent_config``.
        So the shipped template is not a spec any session ever runs, and asserting
        ref/declaration parity against it would be asserting the wrong thing.
        """
        spec = self._bundled()
        assert any(t.startswith("@") for t in spec["tools"])
        assert "mcpServers" not in spec

    def test_every_mcp_ref_resolves_to_a_declaration_on_a_materialized_spec(self):
        """The defect this change fixes, on the real ref list.

        Takes the shipped template's own ``@`` refs and adds the ``mcpServers``
        block ``rebuild_agent_config`` writes for them, which is the shape a live
        session actually loads. A ``@server`` ref with no matching entry mounts
        nothing, so before this projection a stock KAS session advertised Crew's
        whole tool surface and could reach none of it.
        """
        spec = self._bundled()
        refs = [t[1:] for t in spec["tools"] if t.startswith("@")]
        assert refs, "expected the real template to still carry @server refs"
        spec["mcpServers"] = {
            name: {"command": "/opt/kirocrew", "args": [f"mcp-{name.split('-')[-1]}"]}
            for name in refs
        }

        out = to_client_custom_agent(spec["name"], spec, "p")

        declared = set(out.get("mcpServers") or {})
        assert set(refs) <= declared, f"refs naming nothing: {sorted(set(refs) - declared)}"


class TestSpecLookup:
    """Which file on disk the projection reads for an ``agent_id``.

    A spec's filename and its declared ``name`` are allowed to differ, and a
    package manager that installs several agents namespaces them as
    ``<package>-<name>.json``. ``kiro_crew.agent.agent_spec_path`` already
    resolves those by declared name, so a filename-only lookup here fails the
    projection on agents the config, the CLI and the dashboard all resolve.
    """

    @staticmethod
    def _write(agents_dir: Path, filename: str, **over) -> Path:
        path = agents_dir / filename
        path.write_text(json.dumps(_spec(**over)), encoding="utf-8")
        return path

    def test_the_filename_match_is_read(self, tmp_path):
        self._write(tmp_path, "kirocrew.json", description="direct")

        assert load_agent_spec(tmp_path, "kirocrew")["description"] == "direct"

    def test_a_namespaced_filename_resolves_by_declared_name(self, tmp_path):
        self._write(tmp_path, "SomePackage-kirocrew.json", description="namespaced")

        assert load_agent_spec(tmp_path, "kirocrew")["description"] == "namespaced"

    def test_a_declared_name_outranks_a_misnamed_direct_file(self, tmp_path):
        """`kirocrew.json` declaring some OTHER agent must not be projected as
        `kirocrew` while the spec that declares `kirocrew` sits beside it: that
        would run the other agent's tools and prompt under this name. Declared
        name first is the order `agent_spec_path` uses for the same reason."""
        self._write(tmp_path, "kirocrew.json", name="other", description="misnamed")
        self._write(tmp_path, "SomePackage-kirocrew.json", description="namespaced")

        assert load_agent_spec(tmp_path, "kirocrew")["description"] == "namespaced"

    def test_a_direct_file_declaring_another_name_is_the_fallback(self, tmp_path):
        """With nothing declaring the id, `<agent_id>.json` still resolves even
        when its declared name differs -- the filename-stem fallback
        `agent_spec_path` and config.md rung 2 describe."""
        self._write(tmp_path, "kirocrew.json", name="other", description="stem fallback")

        assert load_agent_spec(tmp_path, "kirocrew")["description"] == "stem fallback"

    def test_no_match_still_names_the_direct_path(self, tmp_path):
        """The scan must not blur the error: the operator is told which file to
        create, not which of the dir's specs failed to match."""
        self._write(tmp_path, "SomePackage-other.json", name="other")

        with pytest.raises(KasAgentTranslationError) as exc:
            load_agent_spec(tmp_path, "kirocrew")

        assert str(tmp_path / "kirocrew.json") in str(exc.value)

    def test_an_unparseable_sibling_does_not_break_the_scan(self, tmp_path):
        """The agents dir is user-writable and shared, so a stray file is normal;
        the hardened reader skips it and the real match is still found."""
        (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
        (tmp_path / "list.json").write_text("[]", encoding="utf-8")
        self._write(tmp_path, "SomePackage-kirocrew.json", description="namespaced")

        assert load_agent_spec(tmp_path, "kirocrew")["description"] == "namespaced"

    def test_a_missing_agents_dir_is_a_translation_error(self, tmp_path):
        with pytest.raises(KasAgentTranslationError):
            load_agent_spec(tmp_path / "absent", "kirocrew")

    def test_the_scanned_spec_is_used_without_a_second_read(self, tmp_path, monkeypatch):
        """The hardened reader resolves the symlink and vets the target it lands
        on. Reopening that path afterwards would read whatever it points at by
        then, so the vetted parse itself has to be what the projection uses."""
        self._write(tmp_path, "SomePackage-kirocrew.json", description="on disk")
        monkeypatch.setattr(
            kas_agents,
            "spec_by_declared_name",
            lambda *_a, **_k: _spec(description="what the reader vetted"),
        )

        spec = load_agent_spec(tmp_path, "kirocrew")

        assert spec["description"] == "what the reader vetted"

    def test_two_differing_specs_declaring_one_name_are_refused(self, tmp_path):
        """`agent_spec_path` refuses this ambiguity because which spec is live is
        undefined. Picking one here would project an agent the operator did not
        name, with its tools and its prompt, and say nothing about it.
        """
        self._write(tmp_path, "AlphaPackage-kirocrew.json", description="alpha")
        self._write(tmp_path, "BetaPackage-kirocrew.json", description="beta")

        with pytest.raises(KasAgentTranslationError) as exc:
            load_agent_spec(tmp_path, "kirocrew")

        message = str(exc.value)
        assert "AlphaPackage-kirocrew.json" in message
        assert "BetaPackage-kirocrew.json" in message

    def test_two_identical_specs_declaring_one_name_are_projected(self, tmp_path):
        """A package installer that flattens dependencies writes the same spec
        under two package prefixes. Both parse to one dict, so whichever the
        backend runs, the projection is the same, and the session starts."""
        self._write(tmp_path, "AlphaPackage-kirocrew.json", description="shared", tools=["read"])
        self._write(tmp_path, "BetaPackage-kirocrew.json", description="shared", tools=["read"])

        spec = load_agent_spec(tmp_path, "kirocrew")

        assert spec["description"] == "shared"
        assert spec["tools"] == ["read"]

    def test_a_deeply_nested_declared_spec_is_handed_back_without_a_recursion_error(self, tmp_path):
        """The reader admits a spec nested hundreds of levels deep, and the
        copy handed to the caller must not recurse through it: a
        ``RecursionError`` here aborts session start with neither a projection
        nor the typed refusal. Twins or a single file alike."""
        deep = "leaf"
        for _ in range(600):
            deep = {"m": [deep]}
        self._write(tmp_path, "AlphaPackage-kirocrew.json", tools=["read"], metadata=deep)
        self._write(tmp_path, "BetaPackage-kirocrew.json", tools=["read"], metadata=deep)

        spec = load_agent_spec(tmp_path, "kirocrew")

        assert spec["tools"] == ["read"]
        assert "metadata" in spec
        # Still the caller's own parse: a mutation does not reach the memo.
        spec["tools"].append("shell")
        assert load_agent_spec(tmp_path, "kirocrew")["tools"] == ["read"]

    def test_a_direct_file_does_not_settle_a_duplicate_declared_name(self, tmp_path):
        """Two specs declaring the id are refused even when `<agent_id>.json`
        exists: which of the two is live is undefined, and a misnamed direct
        file is not a tie-breaker between them. `agent_spec_path` refuses the
        same input."""
        self._write(tmp_path, "kirocrew.json", name="other", description="misnamed")
        self._write(tmp_path, "AlphaPackage-kirocrew.json", description="alpha")
        self._write(tmp_path, "BetaPackage-kirocrew.json", description="beta")

        with pytest.raises(KasAgentTranslationError) as exc:
            load_agent_spec(tmp_path, "kirocrew")

        assert "AlphaPackage-kirocrew.json" in str(exc.value)
        assert "BetaPackage-kirocrew.json" in str(exc.value)

    def test_an_unsearchable_dir_is_a_translation_error_at_the_direct_read(
        self, tmp_path, monkeypatch
    ):
        """The strict reader resolves the file before opening it, and on every
        supported version that propagates a permission error, so an agents dir
        the process cannot search reaches the fallback read as an ``OSError``.
        Callers of this module handle ``KasAgentTranslationError``, so an
        ``OSError`` escaping here aborts session startup instead of failing the
        projection.
        """

        def _denied(_self, *_a, **_k):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "resolve", _denied)

        with pytest.raises(KasAgentTranslationError) as exc:
            load_agent_spec(tmp_path, "kirocrew")

        assert str(tmp_path / "kirocrew.json") in str(exc.value)

    def test_an_unsearchable_dir_is_a_translation_error_during_the_scan(
        self, tmp_path, monkeypatch
    ):
        """On Python 3.12 ``Path.glob`` probes the directory with ``is_dir``
        before walking it and propagates that error, so the scan raises even
        though it suppresses per-entry ``scandir`` failures.
        """

        def _denied(_self, _pattern):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "glob", _denied)

        with pytest.raises(KasAgentTranslationError) as exc:
            load_agent_spec(tmp_path, "kirocrew")

        assert str(tmp_path / "kirocrew.json") in str(exc.value)
