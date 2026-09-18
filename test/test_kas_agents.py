"""Crew agent spec -> KAS ``ClientCustomAgent`` projection.

Each assertion here pins a constraint read off KAS's own zod schema
(``resolve-client-agents.ts``), not a preference: getting ``tools`` or ``prompt``
wrong produces an agent that registers successfully and then behaves nothing like
the one the operator configured.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from kiro_crew import config as kiro_crew_config
from kiro_crew.acp import kas_agents
from kiro_crew.acp.kas_agents import (
    _KAS_FALLBACK_PROMPT,
    KAS_MAX_CUSTOM_AGENTS,
    KasAgentTranslationError,
    build_kas_custom_agents,
    hoist_managed_servers,
    load_agent_spec,
    resolve_prompt,
    to_client_custom_agent,
)


def _rule(policy, capability):
    """The single rule for ``capability`` in a projected policy."""
    return next(r for r in policy["rules"] if r["capability"] == capability)


def _spec(**over):
    base = {
        "name": "kirocrew",
        "description": "the crew agent",
        "prompt": "You are Kiro.",
        "tools": ["fs_read", "fs_write", "@kirocrew-core"],
        "mcpServers": {"kirocrew-core": {"command": "x"}},
        "model": "auto",
        "includeMcpJson": False,
    }
    base.update(over)
    return base


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


class TestToolsFailClosed:
    """``tools`` absent means NO tools on KAS (``agent.tools ?? []``).

    So the list must always be emitted, and a spec that does not state one must
    not be widened into an allowlist nobody wrote.
    """

    def test_list_is_passed_through(self):
        out = to_client_custom_agent("a", _spec(), "p")
        assert out["tools"] == ["fs_read", "fs_write", "@kirocrew-core"]

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
        assert out["tools"] == ["fs_read"]


class TestDeliberateOmissions:
    """Fields left out on purpose; each would misbehave if projected.

    ``model`` would compete with the dedicated model verb. ``permissions`` is NOT
    in this list — see :class:`TestPermissionsProjection`; it is absent only when
    the spec gives nothing to derive it from. ``mcpServers`` is not in this
    list either: omitting it left a KAS session with ``@server`` refs naming
    nothing — see :class:`TestMcpServersProjection`.
    """

    @pytest.mark.parametrize("key", ["model", "welcomeMessage"])
    def test_key_is_not_projected(self, key):
        assert key not in to_client_custom_agent("a", _spec(), "p")


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
    ``test_kas_permissions.py``; here we pin only that it is wired in, and that a
    hand-written block outranks it.
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

    def test_a_hand_written_policy_is_not_relayed(self):
        """The wire carries only what passed Crew's governance ceiling.

        Forwarding an author block would be one line and it is already in KAS's
        vocabulary — which is the trap. ``allowedTools`` is the only auto-approve
        input the ceiling (``_may_auto_approve``) has seen, so relaying a block
        from the file would hand any editor of it a grant the ceiling never
        reviewed. An auto-approved call never reaches Crew's permission callback,
        so the deny-list and the audit trail would be skipped with it.
        """
        mine = {"rules": [{"capability": "shell", "effect": "allow"}]}
        out = to_client_custom_agent("a", _spec(allowedTools=["web_fetch"], permissions=mine), "p")
        assert out["permissions"] == {"rules": [{"capability": "web_fetch", "effect": "allow"}]}

    def test_a_hand_written_policy_cannot_smuggle_a_grant_past_the_allowlist(self):
        """The sharp case: the block grants a capability the allowlist withholds."""
        mine = {"rules": [{"capability": "shell", "effect": "allow"}]}
        out = to_client_custom_agent("a", _spec(allowedTools=[], permissions=mine), "p")
        assert "permissions" not in out

    def test_the_derivation_tracks_the_allowlist_not_the_stored_block(self):
        """So a block that has gone stale on disk cannot resurrect an old grant."""
        out = to_client_custom_agent(
            "a",
            _spec(
                allowedTools=["web_fetch"],
                permissions={"rules": [{"capability": "web_search", "effect": "allow"}]},
            ),
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


class TestMcpServersProjection:
    """``mcpServers`` reaches KAS, minus three things.

    KAS honouring an agent-declared block is not an assumption here: it was
    verified with an A/B probe against a live session using a uniquely-named
    witness server, twice per arm. Without the block a stock KAS session gets
    ``tools: ["@kirocrew-core", ...]`` and no definition of what that names.
    """

    def test_declared_server_is_projected(self):
        out = to_client_custom_agent("a", _spec(), "p")
        assert out["mcpServers"] == {"kirocrew-core": {"command": "x"}}

    def test_absent_or_malformed_block_emits_nothing(self):
        assert "mcpServers" not in to_client_custom_agent("a", _spec(mcpServers={}), "p")
        assert "mcpServers" not in to_client_custom_agent("a", _spec(mcpServers=None), "p")
        assert "mcpServers" not in to_client_custom_agent("a", _spec(mcpServers=[]), "p")

    def test_malformed_entries_are_skipped_not_fatal(self):
        out = to_client_custom_agent(
            "a",
            _spec(mcpServers={"good": {"command": "x"}, "bad": "nope", "": {"command": "y"}}),
            "p",
        )
        assert out["mcpServers"] == {"good": {"command": "x"}}

    def test_stubbed_names_are_withheld(self):
        """A stubbed server arrives as the session-level param, which outranks an
        agent-declared entry — declaring both is the double registration this
        block was originally omitted to avoid."""
        out = to_client_custom_agent(
            "a",
            _spec(mcpServers={"kirocrew-core": {"command": "x"}, "other": {"command": "y"}}),
            "p",
            stub_server_names=frozenset({"kirocrew-core"}),
        )
        assert out["mcpServers"] == {"other": {"command": "y"}}

    def test_all_names_stubbed_emits_nothing(self):
        out = to_client_custom_agent(
            "a", _spec(), "p", stub_server_names=frozenset({"kirocrew-core"})
        )
        assert "mcpServers" not in out

    def test_auto_approve_is_never_relayed(self):
        """An autoApproved MCP tool is approved by the host and emits no permission
        request, so Crew's deny floor / sensitive-path check / governance ceiling
        never run for it. Auto-approve reaches KAS only as ``permissions``."""
        out = to_client_custom_agent(
            "a",
            _spec(mcpServers={"third-party": {"command": "x", "autoApprove": ["dangerous"]}}),
            "p",
        )
        assert out["mcpServers"] == {"third-party": {"command": "x"}}

    def test_auto_approve_is_stripped_from_a_managed_server_too(self):
        out = to_client_custom_agent(
            "a",
            _spec(mcpServers={"kirocrew-core": {"command": "x", "autoApprove": ["t"]}}),
            "p",
        )
        assert "autoApprove" not in out["mcpServers"]["kirocrew-core"]

    def test_managed_server_keeps_the_one_env_key_it_needs(self):
        """Crew's own env pins KIROCREW_HOME; dropping it would have the shims
        read a different data home than the gateway."""
        out = to_client_custom_agent(
            "a",
            _spec(mcpServers={"kirocrew-core": {"command": "x", "env": {"KIROCREW_HOME": "/h"}}}),
            "p",
        )
        assert out["mcpServers"]["kirocrew-core"]["env"] == {"KIROCREW_HOME": "/h"}

    def test_a_hand_added_env_key_under_a_managed_name_is_withheld(self):
        """A managed entry still lives in a user-editable agent file, so being
        managed cannot mean "every key now in this env is Crew's". Only
        KIROCREW_HOME survives; the neighbouring secret does not reach the wire.
        """
        out = to_client_custom_agent(
            "a",
            _spec(
                mcpServers={
                    "kirocrew-core": {
                        "command": "x",
                        "env": {"KIROCREW_HOME": "/h", "OPENAI_API_KEY": "sk-live"},
                    }
                }
            ),
            "p",
        )
        assert out["mcpServers"]["kirocrew-core"]["env"] == {"KIROCREW_HOME": "/h"}

    def test_a_managed_env_of_only_secrets_leaves_no_env_at_all(self):
        out = to_client_custom_agent(
            "a",
            _spec(mcpServers={"kirocrew-cron": {"command": "x", "env": {"T": "s"}}}),
            "p",
        )
        entry = out["mcpServers"]["kirocrew-cron"]
        assert "env" not in entry
        assert entry == {"command": "x"}

    def test_headers_are_withheld_from_a_managed_server_too(self):
        """All four managed servers are local stdio processes with no legitimate
        headers, so retaining the field would only forward a hand edit."""
        out = to_client_custom_agent(
            "a",
            _spec(
                mcpServers={
                    "kirocrew-core": {
                        "command": "x",
                        "headers": {"Authorization": "Bearer live"},
                    }
                }
            ),
            "p",
        )
        assert out["mcpServers"]["kirocrew-core"] == {"command": "x"}

    def test_a_malformed_managed_env_is_dropped_not_filtered(self):
        """A non-dict env cannot be filtered key-by-key, so it fails toward
        withholding rather than forwarding an unknown shape."""
        out = to_client_custom_agent(
            "a",
            _spec(mcpServers={"kirocrew-core": {"command": "x", "env": "TOKEN=s"}}),
            "p",
        )
        assert out["mcpServers"]["kirocrew-core"] == {"command": "x"}

    def test_a_withheld_managed_key_is_not_named_in_the_log(self, caplog):
        with caplog.at_level("INFO"):
            to_client_custom_agent(
                "a",
                _spec(
                    mcpServers={
                        "kirocrew-core": {
                            "command": "x",
                            "env": {"KIROCREW_HOME": "/h", "SECRET_TOKEN": "sekrit"},
                        }
                    }
                ),
                "p",
            )
        assert "sekrit" not in caplog.text
        assert "SECRET_TOKEN" not in caplog.text

    @pytest.mark.parametrize("field", ["env", "headers"])
    def test_credential_bearing_field_is_withheld_from_an_unmanaged_server(self, field):
        out = to_client_custom_agent(
            "a",
            _spec(mcpServers={"third-party": {"command": "x", field: {"TOKEN": "secret"}}}),
            "p",
        )
        entry = out["mcpServers"]["third-party"]
        assert field not in entry
        # Withheld, not dropped: the server is still declared and still mounts.
        assert entry == {"command": "x"}

    def test_withheld_credential_is_not_logged_by_value(self, caplog):
        with caplog.at_level("INFO"):
            to_client_custom_agent(
                "a",
                _spec(mcpServers={"third-party": {"command": "x", "env": {"K": "sekrit"}}}),
                "p",
            )
        assert "third-party" in caplog.text
        assert "sekrit" not in caplog.text

    def test_wrapper_marker_never_reaches_the_wire(self):
        """Crew-internal bookkeeping on a rewritten entry. An unknown field can
        fail a strict schema and means nothing to the backend."""
        out = to_client_custom_agent(
            "a",
            _spec(
                mcpServers={
                    "srv": {"command": "x", "_kirocrew_mcp_gateway_wrapped": True},
                }
            ),
            "p",
        )
        assert out["mcpServers"]["srv"] == {"command": "x"}

    def test_the_spec_is_not_mutated(self):
        spec = _spec(mcpServers={"third-party": {"command": "x", "autoApprove": ["t"]}})
        to_client_custom_agent("a", spec, "p")
        assert spec["mcpServers"]["third-party"]["autoApprove"] == ["t"]

    def test_the_managed_name_set_is_the_shared_one(self):
        """Not a third spelling of the four names: this is the set
        ``mcp_cleanup`` already ratchet-pins to ``agent._MANAGED_MCP_SERVERS``."""
        from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS

        assert kas_agents.MANAGED_MCP_SERVER_NAMES == frozenset(KIROCREW_BIN_MCP_SERVERS)


class TestRuntimeSuppliesTheStubbedSet:
    """The seam between the overlay and the projection.

    ``_project_mcp_servers`` can be perfect and the feature still wrong if the
    runtime never tells it which names are stubbed: every stubbed server would be
    declared twice. Only the runtime holds the overlay, so this is the one place
    that can get it right, and nothing else asserts it.
    """

    @staticmethod
    def _overlay_with_a_stub(root: Path, agent: str) -> Path:
        """A user-level overlay holding one broker stub for *agent*."""
        from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER

        overlay = root / "overlay"
        overlay.mkdir(parents=True, exist_ok=True)
        (overlay / f"{agent}.json").write_text(
            json.dumps(
                {
                    "name": agent,
                    "mcpServers": {
                        "pooled": {
                            _WRAPPER_MARKER: True,
                            "command": "/stub",
                            "args": ["--target-command=user-level-cmd"],
                            "env": {},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return overlay

    def test_a_checkout_does_not_scope_the_kas_overlay_lookup(self, tmp_path):
        """KAS projects the USER-LEVEL spec, so its stub set must stay name-keyed.

        ``load_agent_spec`` is handed ``paths.kiro_agents_dir()`` and reads nothing
        else, so a checkout declaring the same name does not change which agent this
        session runs. Scoping the lookup here would collapse the set to empty, and
        the projection would then declare the user-level servers un-subtracted: they
        would run outside the pool, outside caller-identity attribution and outside
        broker governance, with the operator believing the gateway applies.
        """
        import kiro_crew.mcp_gateway.session_servers as session_servers_mod

        overlay = self._overlay_with_a_stub(tmp_path, "kirocrew")
        # A checkout that DOES declare the same name, which is the trigger.
        agents = tmp_path / "checkout" / ".kiro" / "agents"
        agents.mkdir(parents=True)
        (agents / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "mcpServers": {}}), encoding="utf-8"
        )

        assert session_servers_mod.injection_server_names(overlay, "kirocrew") == frozenset(
            {"pooled"}
        )
        # And the scoped answer is the one the KAS path must NOT take.
        assert (
            session_servers_mod.injection_server_names(
                overlay, "kirocrew", work_dir=tmp_path / "checkout"
            )
            == frozenset()
        )

    def test_the_scope_decider_answers_none_for_a_user_level_only_host(self):
        """The injection half of the same rule, read off the seam rather than the name.

        ONE function answers for every caller -- ``AcpClient`` and ``AcpRuntime``
        alike -- so a host that reads the user level alone joins the set and both
        paths change together. A second spelling on one path is how the next such
        host gets mis-scoped, which is the round-1 defect repeated.

        It answers in KEYWORDS because the checkout is only half the scope: which
        spec FORMATS this session's agent resolution honours is the other half, and
        a site taking the checkout without that rule would either un-broker a
        mirrored host's real servers or leave a project markdown agent running the
        user-level stub's command. Splatting one mapping makes the two inseparable.
        """
        from kiro_crew.acp.types import (
            ACP_BACKEND_CODEX,
            ACP_BACKEND_KAS,
            ACP_BACKEND_KIRO,
            overlay_project_scope,
        )

        # A user-level-only host passes NOTHING, so the lookup keeps its by-name
        # defaults rather than being handed a checkout to ignore.
        assert overlay_project_scope(ACP_BACKEND_KAS, "/checkout") == {}
        # kiro-cli resolves ``--agent`` from the checkout itself and discovers JSON
        # only, so a project markdown spec is not the agent it runs.
        assert overlay_project_scope(ACP_BACKEND_KIRO, "/checkout") == {
            "work_dir": "/checkout",
            "markdown_specs": False,
            "dispatchable_only": True,
        }
        # A mirrored host's array is composed by Crew from a spec it resolved
        # itself, and that resolution honours the markdown form.
        assert overlay_project_scope(ACP_BACKEND_CODEX, "/checkout") == {
            "work_dir": "/checkout",
            "markdown_specs": True,
            "dispatchable_only": False,
        }
        # A host with no opinion recorded keeps the checkout rather than silently
        # losing its project scope, and takes the JSON-only answer: with no mirror
        # it resolves no project spec through Crew at all.
        assert overlay_project_scope("some-future-host", "/checkout") == {
            "work_dir": "/checkout",
            "markdown_specs": False,
            "dispatchable_only": True,
        }

    def test_the_format_half_follows_the_projection_that_consumes_it(self):
        """Every mirrored host gets ``markdown_specs=True``, read off the registry.

        Hand-listing the mirrored hosts here would go stale the moment one is
        added, and the failure would be silent: that host's sessions would keep a
        user-level stub in place for a project markdown agent its own projection
        honours, mounting that stub's command under the checkout's agent. Deriving
        the expectation from ``has_mirror`` is what makes a new mirror correct by
        construction rather than by someone remembering this list.
        """
        from kiro_crew.acp.types import (
            ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY,
            overlay_project_scope,
        )
        from kiro_crew.agent_sdk import backends as backends_mod
        from kiro_crew.providers.mirrors.registry import has_mirror

        candidates = {
            value
            for name, value in vars(backends_mod).items()
            if name.startswith("ACP_BACKEND_") and isinstance(value, str)
        }
        assert len(candidates) >= 8, f"the enumeration found only {candidates}"
        mirrored_seen = 0
        for backend in sorted(candidates):
            scope = overlay_project_scope(backend, "/checkout")
            if backend in ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY:
                assert scope == {}, backend
                continue
            assert scope["work_dir"] == "/checkout", backend
            assert scope["markdown_specs"] == has_mirror(backend), backend
            # The two keys are facets of ONE question -- which resolver decides this
            # session's spec -- so a host is never told "both forms" and "must parse".
            # Crew's projection matches on the filename fallback; kiro-cli does not.
            assert scope["dispatchable_only"] is not scope["markdown_specs"], backend
            mirrored_seen += bool(has_mirror(backend))
        assert mirrored_seen >= 1, "no mirrored backend reached the assertion above"

    @pytest.mark.asyncio
    async def test_the_harness_still_subtracts_the_stub_under_a_shadowing_checkout(
        self, monkeypatch, tmp_path
    ):
        """The real harness call site, with the real lookup and a real overlay.

        The runtime hands its own work dir down to ``session_extras``, so a checkout
        that happens to declare this agent's name must not change the answer: KAS
        runs the user-level agent either way. If the harness scoped the lookup, the
        set would arrive empty, the projection would declare the user-level servers
        un-subtracted, and they would run un-brokered.
        """
        overlay = self._overlay_with_a_stub(tmp_path, "kirocrew")
        checkout = tmp_path / "checkout"
        agents = checkout / ".kiro" / "agents"
        agents.mkdir(parents=True)
        (agents / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "mcpServers": {}}), encoding="utf-8"
        )
        seen: list[frozenset] = []
        rt = self._runtime(monkeypatch, str(overlay), seen)
        rt._work_dir = checkout

        await rt._kas_custom_agents("kirocrew")

        assert seen == [frozenset({"pooled"})]

    @staticmethod
    def _runtime(monkeypatch, overlay, seen):
        from kiro_crew.acp import runtime as runtime_mod

        rt = object.__new__(runtime_mod.AcpRuntime)
        rt._acp_backend = runtime_mod.ACP_BACKEND_KAS
        rt._mcp_gateway_overlay = overlay

        import kiro_crew.acp.kas_agents as kas_agents_mod
        import kiro_crew.agent as agent_mod
        import kiro_crew.config.paths as paths_mod

        monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _a: None)
        monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: Path("/agents"))
        # The projection is handed the spec the harness read under the gate, so the read
        # is the harness's own and is stubbed here rather than inside the builder.
        monkeypatch.setattr(
            kas_agents_mod, "load_agent_spec", lambda _dir, agent: {"name": agent, "prompt": "p"}
        )

        def _capture(
            _dir,
            agent,
            _spec,
            *,
            stub_server_names=frozenset(),
            member_dispatch=False,
            session_key="",
        ):
            seen.append(stub_server_names)
            return [{"id": agent}]

        monkeypatch.setattr(kas_agents_mod, "build_kas_custom_agents", _capture)
        return rt

    @pytest.mark.asyncio
    async def test_runtime_carries_each_callers_identity_through_the_harness(self, monkeypatch):
        rt = self._runtime(monkeypatch, None, [])
        import kiro_crew.acp.kas_agents as kas_agents_mod

        seen = []

        def capture(directory, agent, spec, *, stub_server_names, member_dispatch, session_key):
            seen.append((session_key, member_dispatch))
            return [{"id": agent}]

        monkeypatch.setattr(kas_agents_mod, "build_kas_custom_agents", capture)
        await rt._kas_custom_agents("worker", session_key="subagent:first")
        await rt._kas_custom_agents("worker", session_key="subagent:second")
        await rt._kas_custom_agents("worker")
        assert seen == [("subagent:first", False), ("subagent:second", False), ("", False)]

    @pytest.mark.asyncio
    async def test_the_overlay_set_is_forwarded(self, monkeypatch):
        seen: list[frozenset] = []
        rt = self._runtime(monkeypatch, "/overlay", seen)
        import kiro_crew.mcp_gateway.session_servers as session_servers_mod

        monkeypatch.setattr(
            session_servers_mod,
            "injection_server_names",
            # ``**_kw``: the real signature takes the session's checkout as
            # ``work_dir``, and a double that refuses it sends the caller down its
            # except branch instead of exercising the forwarding under test.
            lambda _o, _a, **_kw: frozenset({"kirocrew-core"}),
        )

        await rt._kas_custom_agents("kirocrew")

        assert seen == [frozenset({"kirocrew-core"})]

    @pytest.mark.asyncio
    async def test_no_overlay_forwards_an_empty_set(self, monkeypatch):
        """The default install: nothing stubbed, so nothing is subtracted."""
        seen: list[frozenset] = []
        rt = self._runtime(monkeypatch, None, seen)
        import kiro_crew.mcp_gateway.session_servers as session_servers_mod

        monkeypatch.setattr(
            session_servers_mod, "injection_server_names", lambda _o, _a, **_kw: frozenset()
        )

        await rt._kas_custom_agents("kirocrew")

        assert seen == [frozenset()]

    @pytest.mark.asyncio
    async def test_an_unreadable_overlay_still_yields_an_agent(self, monkeypatch):
        """Fail toward declaring too much, never toward an agent with no servers:
        a double declaration is harmless (the injection outranks it), while
        withholding a server nothing else supplies is the bug being fixed."""
        seen: list[frozenset] = []
        rt = self._runtime(monkeypatch, "/overlay", seen)

        def _boom(_o, _a, **_kw):
            raise OSError("overlay unreadable")

        import kiro_crew.mcp_gateway.session_servers as session_servers_mod

        monkeypatch.setattr(session_servers_mod, "injection_server_names", _boom)

        out = await rt._kas_custom_agents("kirocrew")

        assert seen == [frozenset()]
        assert out.custom_agents == [{"id": "kirocrew"}]


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

    def test_two_specs_declaring_one_name_are_refused(self, tmp_path):
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


class TestNativeManagedMcpIdentity:
    def test_managed_callback_targets_live_gateway_without_relaying_spec_env(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "61234")
        monkeypatch.setenv("KIROCREW_PORT", "5476")
        out = to_client_custom_agent(
            "a",
            _spec(
                mcpServers={
                    "kirocrew-core": {
                        "command": "x",
                        "env": {
                            "KIROCREW_HOME": "/h",
                            "KIROCREW_PORT": "secret-in-editable-spec",
                            "SECRET_TOKEN": "secret",
                        },
                    },
                    "third-party": {"command": "y"},
                }
            ),
            "p",
        )
        assert out["mcpServers"]["kirocrew-core"]["env"] == {
            "KIROCREW_HOME": "/h",
            "KIROCREW_PORT": "61234",
        }
        assert "env" not in out["mcpServers"]["third-party"]
        assert "secret" not in json.dumps(out)

    @pytest.mark.parametrize("port", ["", "auto", "secret", "0", "-1", "65536", "１２３"])
    def test_invalid_bound_port_never_reaches_managed_spec(self, monkeypatch, port):
        monkeypatch.setenv("KIROCREW_BOUND_PORT", port)
        out = to_client_custom_agent("a", _spec(), "p")
        assert "KIROCREW_PORT" not in out["mcpServers"]["kirocrew-core"].get("env", {})

    def test_worker_identity_is_runtime_scoped_without_member_control_tools(self):
        spec = _spec(
            tools=["@kirocrew-work"],
            mcpServers={
                "kirocrew-work": {
                    "command": "worker",
                    "env": {"KIROCREW_SESSION_KEY": "forged-parent"},
                },
                "third-party": {"command": "other"},
            },
        )
        worker = to_client_custom_agent("worker", spec, "p", session_key="subagent:abc12345")
        assert worker["mcpServers"]["kirocrew-work"]["env"] == {
            "KIROCREW_SESSION_KEY": "subagent:abc12345",
        }
        assert "env" not in worker["mcpServers"]["third-party"]
        assert "kirocrew-dashboard" not in json.dumps(worker)
        assert "forged-parent" not in json.dumps(worker)
        unrelated = to_client_custom_agent("worker", spec, "p")
        assert "env" not in unrelated["mcpServers"]["kirocrew-work"]


class TestHoistManagedServers:
    """Managed declarations travel in the session-level array, restrictions intact.

    Captured released kiro-cli 2.18.0 honours a session-level entry over a
    same-named global or workspace ``mcp.json`` server on new and load
    (``bugfix-repair58b-fable/2.18.0-payload-probe.json`` sends this very
    payload) while an agent-block declaration loses to the global one
    (``bugfix-repair58-fable/2.18.0-newload-global+agent.json``), and stamps no
    provenance -- so the array is the one declaration site whose report is
    positively the session's own.
    """

    def _projected(self, **servers):
        spec = _spec(
            mcpServers={
                "kirocrew-core": {"command": "kc", "args": ["mcp"], "env": {"SECRET": "s"}},
                "third-party": {"command": "tp", "env": {"TOKEN": "t"}},
                **servers,
            },
            tools=["@kirocrew-core", "@third-party"],
            excludedTools=["@kirocrew-core/learn_add"],
        )
        return to_client_custom_agent("kirocrew", spec, "p", session_key="subagent:k")

    def test_active_managed_stdio_entry_moves_into_the_array_as_an_acp_element(self):
        projected = self._projected()
        before = json.loads(json.dumps(projected))
        agents, array = hoist_managed_servers([projected], "kirocrew", [])
        assert projected == before, "the projection is not mutated"
        assert array == [
            {
                "name": "kirocrew-core",
                "command": "kc",
                "args": ["mcp"],
                "env": [{"name": "KIROCREW_SESSION_KEY", "value": "subagent:k"}],
                "type": "stdio",
            }
        ], "the ALREADY projected entry travels: secret withheld, session key kept"
        assert agents[0]["mcpServers"] == {"third-party": {"command": "tp"}}
        # Grants are untouched: refs resolve wherever the server is declared.
        assert agents[0]["tools"] == projected["tools"]
        assert agents[0]["excludedTools"] == ["@kirocrew-core/learn_add"]
        assert agents[0].get("permissions") == projected.get("permissions")

    def test_block_key_is_removed_when_nothing_remains(self):
        spec = _spec(mcpServers={"kirocrew-core": {"command": "kc"}})
        projected = to_client_custom_agent("kirocrew", spec, "p")
        agents, array = hoist_managed_servers([projected], "kirocrew", [])
        assert "mcpServers" not in agents[0]
        assert [e["name"] for e in array] == ["kirocrew-core"]
        assert array[0]["env"] == []

    def test_caller_entries_stay_first_and_are_never_duplicated(self):
        projected = self._projected()
        member = {
            "name": "kirocrew-dashboard",
            "command": "d",
            "args": [],
            "env": [],
            "type": "stdio",
        }
        stub = {
            "name": "kirocrew-core",
            "command": "broker",
            "args": [],
            "env": [],
            "type": "stdio",
        }
        agents, array = hoist_managed_servers([projected], "kirocrew", [member, stub])
        assert array == [member, stub], "an injected name is authoritative and appears once"
        assert agents[0] is projected, "nothing hoisted, nothing rewritten"

    def test_inactive_agents_are_left_alone(self):
        active = self._projected()
        other = to_client_custom_agent(
            "other", _spec(mcpServers={"kirocrew-work": {"command": "w"}}), "p"
        )
        agents, array = hoist_managed_servers([active, other], "kirocrew", [])
        assert [e["name"] for e in array] == ["kirocrew-core"]
        assert agents[1] is other
        assert other["mcpServers"] == {"kirocrew-work": {"command": "w"}}

    @pytest.mark.parametrize(
        "entry",
        [
            {"command": "kc", "disabled": True},
            {"command": "kc", "disabledTools": ["learn_add"]},
            {"command": "kc", "timeout": 5},
            {"command": "kc", "type": "registry"},
            {"url": "https://example.invalid/mcp"},
            {"args": ["mcp"]},
        ],
        ids=["disabled", "disabledTools", "timeout", "registry", "remote", "no-command"],
    )
    def test_a_restricted_or_unrepresentable_entry_keeps_the_block_path(self, entry):
        projected = to_client_custom_agent(
            "kirocrew", _spec(mcpServers={"kirocrew-core": entry}), "p"
        )
        agents, array = hoist_managed_servers([projected], "kirocrew", [])
        assert array == []
        assert agents[0] is projected
        assert agents[0]["mcpServers"]["kirocrew-core"] == entry

    def test_no_agents_or_no_block_is_a_passthrough(self):
        array = [{"name": "x"}]
        assert hoist_managed_servers(None, "kirocrew", array) == (None, array)
        assert hoist_managed_servers([], "kirocrew", array) == ([], array)
        bare = {"id": "kirocrew", "prompt": "p", "tools": []}
        agents, out = hoist_managed_servers([bare], "kirocrew", array)
        assert agents[0] is bare and out is array

    def test_the_no_payload_path_imports_nothing(self, monkeypatch):
        # The kiro path (no custom agents) must return before the lazy import of
        # the agent/config translation module, so a spawn there loads nothing new.
        import builtins
        import sys

        monkeypatch.delitem(sys.modules, "kiro_crew.acp.session_mcp", raising=False)
        real_import = builtins.__import__

        def guarded(name, *args, **kwargs):
            if name == "kiro_crew.acp.session_mcp":
                raise AssertionError("session_mcp imported on the no-payload path")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", guarded)
        assert hoist_managed_servers(None, "kirocrew", []) == (None, [])

    def test_hoisted_elements_are_ordered_by_name(self):
        spec = _spec(
            mcpServers={"kirocrew-work": {"command": "w"}, "kirocrew-core": {"command": "c"}},
            tools=["@kirocrew-core", "@kirocrew-work"],
        )
        projected = to_client_custom_agent("kirocrew", spec, "p")
        _, array = hoist_managed_servers([projected], "kirocrew", [])
        assert [e["name"] for e in array] == ["kirocrew-core", "kirocrew-work"]
