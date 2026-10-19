"""The gate's path tier and the auto-approve of a first-party app's own MCP server.

Pins the app-own MCP server auto-approve (keyed on the trusted server name, after the deny
floor and governance, never for a third-party app), the target paths the sensitive-path
keystone reads (every spelling, paths nested in arguments, work caps that fail closed), the
refusal of a shell call with no recoverable command that no override admits, what a
refusal says under a resolver stall (``safe_read_file`` included), and the strings spared
the path tier: a sandboxed shell tool's recovered command and a title equal to the call's
verified ``@server/tool`` identity. The shell command an event recovers and
``safe_read_file`` itself live in ``test_hooks_shell_command.py``; the spawn rung, the
read-only proofs and identity governance in ``test_hooks_trusted_identity.py``; tool
matching, the tool hooks and the shell-title tiers in ``test_hooks.py``.
"""

from __future__ import annotations

import pytest

from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE, TOOL_DENY, HookManager, HooksConfig


class TestAppOwnMcpServerAutoApprove:
    """A FIRST-PARTY (builtin) app agent calling its OWN app-scoped MCP server
    (identified by the trusted, non-model-authored ``mcp_server_name`` =
    ``<app>:<server>``) is auto-approved intra-app, without re-widening any host
    grant, and only AFTER the always-on deny floor + governance (which still
    win). The decision keys on ``mcp_server_name``, NEVER the LLM-authored title,
    so a forged ``mcp__…`` title on a shell/host tool cannot win auto-approval. A
    THIRD-PARTY app's own server is NOT auto-approved."""

    @pytest.fixture
    def _builtin(self, monkeypatch):
        """Treat the test app names as first-party builtins whose shipped
        manifest declares the ``myapp:srv`` MCP server."""
        import kiro_crew.hooks as hooks_mod

        monkeypatch.setattr(
            hooks_mod,
            "_is_first_party_app",
            lambda app: app.casefold() in {"myapp"},
        )
        monkeypatch.setattr(hooks_mod, "_BUILTIN_APP_MCP_SERVERS", frozenset({"myapp:srv"}))

    def test_own_server_tool_auto_approved(self, _builtin):
        mgr = HookManager()
        r = mgr.on_tool_call(
            "mcp__myapp:srv__do_thing",
            app="myapp",
            mcp_server_name="myapp:srv",
            mcp_tool_name="do_thing",
            tool_kind="other",
        )
        assert r.action == TOOL_AUTO_APPROVE

    @pytest.fixture
    def _agent_owned(self, monkeypatch):
        """``myagent`` is declared by builtin ``myapp``'s shipped manifest."""
        import kiro_crew.hooks as hooks_mod

        monkeypatch.setattr(hooks_mod, "_BUILTIN_APP_AGENTS", {"myagent": "myapp"})

    def test_own_server_auto_approved_when_slot_app_empty(self, _builtin, _agent_owned):
        # A builtin whose UI is not an app iframe (e.g. an Electron window using
        # the dashboard session cookie) binds its slot with NO authenticated app
        # scope, so Slot._app is empty and every app-keyed condition would fail —
        # the app could not call its OWN server. The owner is recovered from the
        # non-model-authored agent via shipped-manifest provenance.
        mgr = HookManager()
        r = mgr.on_tool_call(
            "mcp__myapp:srv__do_thing",
            app="",
            agent="myalias",
            resolved_agent="myagent",
            mcp_server_name="myapp:srv",
            mcp_tool_name="do_thing",
            tool_kind="other",
        )
        assert r.action == TOOL_AUTO_APPROVE

    def test_unmapped_agent_with_empty_app_not_auto_approved(self, _builtin, _agent_owned):
        # No shipped manifest declares this agent, so no app identity can be
        # proven: fail closed to interactive approval rather than guessing.
        mgr = HookManager()
        r = mgr.on_tool_call(
            "mcp__myapp:srv__do_thing",
            app="",
            resolved_agent="stranger",
            mcp_server_name="myapp:srv",
            mcp_tool_name="do_thing",
            tool_kind="other",
        )
        assert r.action == TOOL_ALLOW

    def test_slot_agent_alias_cannot_impersonate_app_agent(self, _builtin, _agent_owned):
        # The slot's `agent` is an ALIAS that resolve_agent_bindings maps to a
        # concrete kiro agent before dispatch, so an alias NAMED after a builtin's
        # agent must NOT lend that app's identity to whatever actually ran. Only
        # the resolved identity decides ownership.
        mgr = HookManager()
        r = mgr.on_tool_call(
            "mcp__myapp:srv__do_thing",
            app="",
            agent="myagent",  # alias spelled like the builtin's agent
            resolved_agent="kirocrew",  # …but a different agent served the turn
            mcp_server_name="myapp:srv",
            mcp_tool_name="do_thing",
            tool_kind="other",
        )
        assert r.action == TOOL_ALLOW

    def test_missing_resolved_agent_not_auto_approved(self, _builtin, _agent_owned):
        # Without a resolved identity we cannot prove WHICH agent ran, so the
        # alias is never used as a substitute — fail closed.
        mgr = HookManager()
        r = mgr.on_tool_call(
            "mcp__myapp:srv__do_thing",
            app="",
            agent="myagent",
            resolved_agent="",
            mcp_server_name="myapp:srv",
            mcp_tool_name="do_thing",
            tool_kind="other",
        )
        assert r.action == TOOL_ALLOW

    def test_derived_owner_cannot_reach_another_apps_server(self, _agent_owned, monkeypatch):
        # The derived identity is still only ITS OWN server: it must not
        # auto-approve a different app's app-scoped server. Both apps are
        # first-party and both servers declared, so ONLY the ownership check can
        # be what rejects this.
        import kiro_crew.hooks as hooks_mod

        monkeypatch.setattr(
            hooks_mod, "_is_first_party_app", lambda app: app.casefold() in {"myapp", "otherapp"}
        )
        monkeypatch.setattr(
            hooks_mod, "_BUILTIN_APP_MCP_SERVERS", frozenset({"myapp:srv", "otherapp:srv"})
        )
        mgr = HookManager()
        r = mgr.on_tool_call(
            "mcp__otherapp:srv__do_thing",
            app="",
            agent="myalias",
            resolved_agent="myagent",
            mcp_server_name="otherapp:srv",
            mcp_tool_name="do_thing",
            tool_kind="other",
        )
        assert r.action == TOOL_ALLOW

    def test_slot_app_takes_precedence_over_derived_owner(self, _builtin, _agent_owned):
        # An app-scoped session keeps its AUTHENTICATED identity: the agent map is
        # a fallback for an empty _app, never an override that could lend one app
        # another's identity.
        mgr = HookManager()
        r = mgr.on_tool_call(
            "mcp__myapp:srv__do_thing",
            app="otherapp",
            agent="myalias",
            resolved_agent="myagent",
            mcp_server_name="myapp:srv",
            mcp_tool_name="do_thing",
            tool_kind="other",
        )
        assert r.action == TOOL_ALLOW

    def test_own_server_without_trusted_tool_name_not_auto_approved(self, _builtin):
        # No trusted _meta.kiro.toolName → we cannot identify WHICH tool this is
        # to govern it, so the app-own-server auto-approve does NOT fire; the
        # call falls through to interactive approval (fail-closed), never a
        # silent execute. select_tool_title prefers the model's prose
        # description, so a real MCP call can arrive with a non-canonical title.
        mgr = HookManager()
        r = mgr.on_tool_call(
            "Do a thing",
            app="myapp",
            mcp_server_name="myapp:srv",
            mcp_tool_name="",
            tool_kind="other",
        )
        assert r.action == TOOL_ALLOW

    def test_own_server_governs_canonical_tool_not_prose_title(self, monkeypatch):
        # The auto-approve must govern the REAL tool (trusted mcp_server_name +
        # mcp_tool_name → canonical ``@server/tool``), NOT the LLM prose title.
        # A per-tool policy denying ``@myapp:srv/danger`` must block the call
        # even though the prose title never matches that policy — closing the
        # bypass where an own-server auto-approve skipped per-tool governance.
        import kiro_crew.hooks as hooks_mod

        monkeypatch.setattr(hooks_mod, "_is_first_party_app", lambda app: True)
        monkeypatch.setattr(hooks_mod, "_is_declared_builtin_mcp_server", lambda name: True)
        seen: list[str] = []

        def fake_gov(ctx, name, *a, **k):
            # One question carries every identity for the call, so match against
            # all of them rather than a single target.
            targets = [name, *k.get("extra_titles", ()), k.get("mcp_ref", "")]
            for target in targets:
                if target and target not in seen:
                    seen.append(target)
            if "@myapp:srv/danger" in targets:
                return "Blocked by governance policy: denied"
            return None

        monkeypatch.setattr(hooks_mod, "_governance_denial", fake_gov)
        mgr = HookManager()
        r = mgr.on_tool_call(
            "Do a risky thing",  # prose title → upstream governance permits it
            app="myapp",
            mcp_server_name="myapp:srv",
            mcp_tool_name="danger",
            tool_kind="other",
        )
        assert r.action == TOOL_DENY
        # BOTH identities were offered in the one query: the prose title
        # (permitted) AND the canonical reference (denied).
        assert "Do a risky thing" in seen
        assert "@myapp:srv/danger" in seen

    def test_own_server_honors_canonical_deny_rule(self, monkeypatch):
        # The always-on deny floor (auto_deny_tools / denied regexes) must apply
        # to the canonical mcp__server__tool, not just the prose title. A deny
        # rule keyed on the canonical name blocks the own-server auto-approve
        # even when the LLM title is non-canonical prose that never matched it.
        import kiro_crew.hooks as hooks_mod

        monkeypatch.setattr(hooks_mod, "_is_first_party_app", lambda app: True)
        monkeypatch.setattr(hooks_mod, "_is_declared_builtin_mcp_server", lambda name: True)
        cfg = HooksConfig(auto_deny_tools=["mcp__myapp:srv__danger"])
        mgr = HookManager(cfg)
        r = mgr.on_tool_call(
            "Do a risky thing",  # prose title: the top-of-method deny check misses it
            app="myapp",
            mcp_server_name="myapp:srv",
            mcp_tool_name="danger",
            tool_kind="other",
        )
        assert r.action == TOOL_DENY

    def test_undeclared_own_prefix_server_not_auto_approved(self, _builtin):
        # A ``<app>:``-prefixed server the app's SHIPPED manifest does NOT declare
        # (e.g. injected into the mutable global MCP config as ``myapp:evil``)
        # must NOT auto-approve, even though the prefix matches a first-party app
        # and a trusted tool name is present. Only manifest-declared servers
        # (here ``myapp:srv``) are trusted → this falls through to interactive
        # approval (fail-closed).
        mgr = HookManager()
        r = mgr.on_tool_call(
            "mcp__myapp:evil__x",
            app="myapp",
            mcp_server_name="myapp:evil",
            mcp_tool_name="x",
            tool_kind="other",
        )
        assert r.action == TOOL_ALLOW

    def test_set_builtin_app_mcp_servers_populates_gate(self, monkeypatch):
        # Boot pushes the shipped-manifest-declared <app>:<server> names in; the
        # gate then does a pure in-memory, case-insensitive membership test.
        import kiro_crew.hooks as hooks_mod

        monkeypatch.setattr(hooks_mod, "_BUILTIN_APP_MCP_SERVERS", frozenset())
        hooks_mod.set_builtin_app_mcp_servers(["MyApp:Srv", "other:s", "", None])  # junk ignored
        assert hooks_mod._is_declared_builtin_mcp_server("myapp:srv") is True  # casefolded
        assert hooks_mod._is_declared_builtin_mcp_server("OTHER:S") is True
        assert hooks_mod._is_declared_builtin_mcp_server("myapp:nope") is False
        assert hooks_mod._is_declared_builtin_mcp_server("") is False  # fail-closed

    def test_forged_title_on_shell_tool_not_approved(self, _builtin):
        # GPT's attack: a prompt-injected builtin agent titles a Bash call
        # `mcp__myapp:srv__x`, but a genuine shell tool carries NO server name
        # (kiro-cli only sets mcp_server_name for MCP-served calls). Keying on the
        # trusted server name (empty here) means the forged title never wins —
        # the real command falls through to interactive approval.
        mgr = HookManager()
        r = mgr.on_tool_call(
            "mcp__myapp:srv__x",
            app="myapp",
            mcp_server_name="",
            command="curl http://example.com/data",
            is_shell=True,
            tool_kind="execute",
        )
        assert r.action == TOOL_ALLOW

    def test_forged_title_without_server_name_not_approved(self, _builtin):
        # Same principle for a non-shell host tool: a forged MCP-looking title
        # with an empty trusted server name does not auto-approve.
        mgr = HookManager()
        r = mgr.on_tool_call(
            "mcp__myapp:srv__x", app="myapp", mcp_server_name="", tool_kind="other"
        )
        assert r.action == TOOL_ALLOW

    def test_third_party_own_server_not_auto_approved(self, monkeypatch):
        # A third-party app (no builtin provenance) is NOT blanket-trusted.
        import kiro_crew.hooks as hooks_mod

        monkeypatch.setattr(hooks_mod, "_is_first_party_app", lambda app: False)
        mgr = HookManager()
        r = mgr.on_tool_call(
            "mcp__myapp:srv__do_thing",
            app="myapp",
            mcp_server_name="myapp:srv",
            tool_kind="other",
        )
        assert r.action == TOOL_ALLOW

    def test_other_apps_server_not_auto_approved(self, _builtin):
        mgr = HookManager()
        r = mgr.on_tool_call(
            "mcp__myapp:srv__do_thing",
            app="otherapp",
            mcp_server_name="myapp:srv",
            tool_kind="other",
        )
        assert r.action == TOOL_ALLOW

    def test_blank_app_cannot_match(self, _builtin):
        mgr = HookManager()
        r = mgr.on_tool_call(
            "mcp__myapp:srv__do_thing", app="", mcp_server_name="myapp:srv", tool_kind="other"
        )
        assert r.action == TOOL_ALLOW

    def test_host_managed_server_not_matched(self, _builtin):
        # Host/managed servers (kirocrew-cron) are not `<app>:`-namespaced.
        mgr = HookManager()
        r = mgr.on_tool_call(
            "mcp__kirocrew-cron__cron_add",
            app="myapp",
            mcp_server_name="kirocrew-cron",
            tool_kind="other",
        )
        assert r.action == TOOL_ALLOW

    def test_owning_app_match_is_case_insensitive(self, _builtin):
        mgr = HookManager()
        r = mgr.on_tool_call(
            "mcp__MyApp:srv__do_thing",
            app="myapp",
            mcp_server_name="MyApp:srv",
            mcp_tool_name="do_thing",
            tool_kind="other",
        )
        assert r.action == TOOL_AUTO_APPROVE

    def test_governance_deny_wins_over_own_server(self, monkeypatch):
        # The app-own-server branch is placed AFTER `_governance_denial`, so a
        # ceiling/profile that denies the app's own server still blocks it.
        import kiro_crew.hooks as hooks_mod

        monkeypatch.setattr(hooks_mod, "_is_first_party_app", lambda app: True)
        monkeypatch.setattr(
            hooks_mod,
            "_governance_denial",
            lambda *a, **k: "Blocked by governance policy: denied",
        )
        mgr = HookManager()
        r = mgr.on_tool_call(
            "mcp__myapp:srv__do_thing",
            app="myapp",
            mcp_server_name="myapp:srv",
            tool_kind="other",
        )
        assert r.action == TOOL_DENY

    def test_ownership_helper_direct(self):
        from kiro_crew.hooks import _app_owns_mcp_server

        assert _app_owns_mcp_server("myapp:srv", "myapp") is True
        assert _app_owns_mcp_server("MyApp:srv", "myapp") is True  # case-insensitive
        assert _app_owns_mcp_server("other:srv", "myapp") is False
        assert _app_owns_mcp_server("myapp:srv", "") is False
        assert _app_owns_mcp_server("", "myapp") is False  # no server → fail closed
        assert _app_owns_mcp_server("kirocrew-cron", "myapp") is False  # no colon

    def test_set_builtin_app_names_populates_gate(self, monkeypatch):
        # Boot pushes the builtin names in; the gate then does a pure in-memory,
        # case-insensitive membership test (no filesystem I/O on the event loop).
        import kiro_crew.hooks as hooks_mod

        monkeypatch.setattr(hooks_mod, "_BUILTIN_APP_NAMES", frozenset())
        hooks_mod.set_builtin_app_names(["MyApp", "other", "", None])  # junk ignored
        assert hooks_mod._is_first_party_app("myapp") is True  # casefolded on ingest
        assert hooks_mod._is_first_party_app("OTHER") is True
        assert hooks_mod._is_first_party_app("nope") is False

    def test_unwarmed_builtin_set_fails_closed(self, monkeypatch):
        # Before boot warms the set, provenance is unknown → treated as
        # third-party (own-server calls prompt, never wrongly auto-approved).
        import kiro_crew.hooks as hooks_mod

        monkeypatch.setattr(hooks_mod, "_BUILTIN_APP_NAMES", frozenset())
        assert hooks_mod._is_first_party_app("myapp") is False


class TestTargetPathSpellings:
    """The sensitive-path keystone read ``path`` and ``file_path`` but not
    ``filePath`` -- the camel-case form ``_SEARCH_DENY_ARG_KEYS`` has accepted for
    the search plane all along. A backend sending that spelling reached NEITHER
    read, so a write to a sensitive path under it was never gated.

    ``cli_chat`` shares ``target_paths`` with the gate, so a prompt cannot
    disclose a path the keystone did not inspect.
    """

    @staticmethod
    def _gate():
        from kiro_crew.hooks import HookManager, HooksConfig

        return HookManager(HooksConfig.from_dict({}))

    @pytest.mark.parametrize("key", ["path", "file_path", "filePath", "targetFile"])
    def test_a_sensitive_path_is_denied_under_every_spelling(self, key):
        from kiro_crew.hooks import TOOL_DENY

        decision = self._gate().on_tool_call(
            "Tidy up the notes",
            session_key="cli_chat",
            tool_kind="edit",
            raw_params={key: "~/.ssh/id_rsa"},
        )
        assert decision.action == TOOL_DENY, (
            f"a sensitive path under {key!r} reached no check; the human would be "
            "asked to approve what the keystone must refuse outright"
        )

    def test_a_kas_delete_of_a_sensitive_path_is_denied(self):
        """``delete_file`` (KAS) carries its path as ``targetFile`` under kind
        ``delete``; the keystone is not kind-gated, so the spelling alone decides
        whether the delete is seen."""
        from kiro_crew.hooks import TOOL_DENY

        decision = self._gate().on_tool_call(
            "Delete the key",
            session_key="cli_chat",
            tool_kind="delete",
            raw_params={"targetFile": "~/.aws/credentials", "explanation": "x"},
        )
        assert decision.action == TOOL_DENY

    def test_a_second_innocent_alias_cannot_shadow_a_sensitive_one(self):
        """Every value present is checked, so a deny cannot be dodged by adding a
        benign alias. This is why the fix does not 'normalize onto one key and
        reject conflicts' -- a conflict rule has to pick a winner, and picking
        wrong is exactly how the sensitive value slips past.

        Uses a READ kind deliberately. An ``edit`` would also be caught by the
        write-protected-config gate further down, so the deny would prove nothing
        about the sensitive-path keystone this test exists to pin -- a mutation
        limiting the keystone to the FIRST path still passed while the kind was
        ``edit``.
        """
        from kiro_crew.hooks import TOOL_DENY

        decision = self._gate().on_tool_call(
            "Read the notes",
            session_key="cli_chat",
            tool_kind="read",
            raw_params={"path": "/tmp/harmless.txt", "filePath": "~/.aws/credentials"},
        )
        assert decision.action == TOOL_DENY

    def test_an_ordinary_path_is_still_allowed_under_every_spelling(self):
        """The absence direction: widening the spellings must not start denying
        ordinary files, or the gate would refuse most real edits."""
        from kiro_crew.hooks import TOOL_DENY

        for key in ("path", "file_path", "filePath", "targetFile"):
            decision = self._gate().on_tool_call(
                "Tidy up the notes",
                session_key="cli_chat",
                tool_kind="edit",
                raw_params={key: "/tmp/notes.md"},
            )
            assert decision.action != TOOL_DENY, f"{key!r} wrongly denied an ordinary file"

    def test_target_paths_ignores_non_string_and_blank_values(self):
        from kiro_crew.hooks import target_paths

        assert target_paths({"path": {"nested": 1}, "filePath": "   "}) == []
        assert target_paths(None) == []
        assert target_paths({"path": "/a", "file_path": "/a"}) == ["/a"], "deduped"


class TestTargetPathNestedExtraction:
    """``target_paths`` read only the TOP-LEVEL ``TARGET_PATH_KEYS`` string
    values, so a tool that nests its path inside an array argument — the batch
    read shape ``{"operations": [{"mode": "Line", "path": …}]}`` — yielded ``[]``
    and the sensitive-path keystone in ``on_tool_call`` never saw the target.
    Worse than a missed deny: a read-kind call with the nested spelling was
    AUTO-APPROVED while the flat spelling of the same path is denied.

    The fix recurses into dict/list values (depth-bounded) and stays
    extract-only; ``cli_chat``'s consent prompt shares the helper, so the
    disclosure and the gate widen together.
    """

    @staticmethod
    def _gate():
        from kiro_crew.hooks import HookManager, HooksConfig

        return HookManager(HooksConfig.from_dict({}))

    def test_a_sensitive_path_nested_in_an_array_argument_is_denied(self):
        """The reported fence miss, end to end through the gate."""
        from kiro_crew.hooks import TOOL_DENY

        decision = self._gate().on_tool_call(
            "Read the notes",
            session_key="cli_chat",
            tool_kind="read",
            raw_params={"operations": [{"mode": "Line", "path": "~/.ssh/id_rsa"}]},
        )
        assert decision.action == TOOL_DENY, (
            "a sensitive path nested in an array argument reached no check; "
            "the call would be auto-approved"
        )

    def test_single_op_array_extraction(self):
        from kiro_crew.hooks import target_paths

        params = {"operations": [{"mode": "Line", "path": "~/.ssh/id_rsa"}]}
        assert target_paths(params) == ["~/.ssh/id_rsa"]

    def test_two_op_array_extraction_preserves_order_and_dedups(self):
        from kiro_crew.hooks import target_paths

        params = {
            "operations": [
                {"mode": "Line", "path": "/tmp/a.txt"},
                {"mode": "Line", "file_path": "~/.aws/credentials"},
                {"mode": "Line", "path": "/tmp/a.txt"},
            ]
        }
        assert target_paths(params) == ["/tmp/a.txt", "~/.aws/credentials"]

    def test_negative_control_pattern_only_yields_nothing(self):
        """A search-shaped call with no path key emits no candidates — the
        widening must not start inventing targets from unrelated arguments."""
        from kiro_crew.hooks import target_paths

        assert target_paths({"pattern": "x"}) == []
        assert target_paths({"operations": [{"mode": "Directory", "depth": 2}]}) == []

    def test_list_of_strings_directly_under_a_path_key_is_collected(self):
        from kiro_crew.hooks import target_paths

        assert target_paths({"path": ["/tmp/a", "~/.ssh/id_rsa"]}) == [
            "/tmp/a",
            "~/.ssh/id_rsa",
        ]

    def test_an_ordinary_nested_path_is_still_allowed(self):
        """The absence direction: recursing must not start denying ordinary
        batch reads, or the gate would refuse most real multi-file calls."""
        from kiro_crew.hooks import TOOL_DENY

        decision = self._gate().on_tool_call(
            "Read the notes",
            session_key="cli_chat",
            tool_kind="read",
            raw_params={"operations": [{"mode": "Line", "path": "/tmp/notes.md"}]},
        )
        assert decision.action != TOOL_DENY, "ordinary nested path wrongly denied"

    def test_a_deeply_nested_path_is_still_found_without_raising(self):
        """The walk is iterative and exhaustive: depth alone cannot hide a
        path (no RecursionError, no fail-open depth bound)."""
        from kiro_crew.hooks import target_paths

        deep: dict = {"path": "~/.ssh/id_rsa"}
        for _ in range(1000):
            deep = {"wrapper": [deep]}
        result = target_paths(deep)
        assert result == ["~/.ssh/id_rsa"]
        assert result.truncated is False

    def test_work_cap_fails_closed_as_truncated_and_the_gate_denies(self):
        """An attacker-shaped payload that exhausts the work caps must mark the
        result truncated, and the gate must DENY the unverifiable call rather
        than trust a partial scan — deny-by-default, never silent fail-open."""
        from kiro_crew.hooks import (
            _TARGET_PATH_MAX_PATHS,
            TOOL_DENY,
            target_paths,
        )

        # Path-count cap: more distinct candidates than the cap.
        flood = {"path": [f"/n/a/{i}" for i in range(_TARGET_PATH_MAX_PATHS + 10)]}
        result = target_paths(flood)
        assert result.truncated is True
        assert len(result) == _TARGET_PATH_MAX_PATHS

        # Node-budget cap: a huge container structure with no paths at all is
        # still unverifiable — the walk stopped before proving absence.
        wide: dict = {"blobs": [{"x": i} for i in range(20_000)]}
        assert target_paths(wide).truncated is True

        decision = self._gate().on_tool_call(
            "Read the notes",
            session_key="cli_chat",
            tool_kind="read",
            raw_params=flood,
        )
        assert decision.action == TOOL_DENY, (
            "a truncated (unverifiable) argument scan must deny, or the cap "
            "becomes a bypass: park a sensitive path beyond it"
        )

    def test_ordinary_calls_are_nowhere_near_the_work_caps(self):
        """The caps must not start refusing real batch reads."""
        from kiro_crew.hooks import target_paths

        params = {"operations": [{"mode": "Line", "path": f"/tmp/f{i}.txt"} for i in range(40)]}
        result = target_paths(params)
        assert len(result) == 40
        assert result.truncated is False


class TestUnrecoverableShellIsRefusedUnconditionally:
    """The deny-by-default refusal on a shell call with no recoverable command has
    no operator override, and must not acquire one.

    An override was implemented and removed on the PR that added the
    ``DeniedRuleProvider`` seam. ``ToolHookResult`` carries only
    ``allow`` / ``auto_approve`` / ``deny``, so a suppressed call can at best
    return ``allow`` — and ``allow`` falls through to
    patterns / trust-reads / trust / YOLO / interactive in the dashboard runner.
    Under YOLO the unverified command would then execute with no human ever
    seeing it, so barring the hook-level auto-approve branches is not sufficient:
    the decision is re-made downstream. Reinstating a switch here needs a fourth
    action meaning "force the interactive prompt, and let no downstream tier
    auto-grant it".
    """

    _REASON = "could not be verified for security policy"

    def test_the_refusal_fires(self):
        res = HookManager(HooksConfig()).on_tool_call(
            "Running a command", is_shell=True, command=None
        )
        assert res.action == TOOL_DENY
        assert self._REASON in (res.reason or "")

    def test_a_stale_override_key_cannot_resurrect_the_suppression(self):
        """A build that briefly shipped the switch may have left
        ``allow_unverified_shell: true`` in the keystone state. It is now an
        unknown key and must be inert, not honoured."""
        cfg = HooksConfig.from_dict(
            {"denied_commands": {"allow_unverified_shell": True}},
        )
        assert not hasattr(cfg, "denied_commands_allow_unverified_shell")
        res = HookManager(cfg).on_tool_call("Running a command", is_shell=True, command=None)
        assert res.action == TOOL_DENY
        assert self._REASON in (res.reason or "")

    def test_an_auto_approve_pattern_cannot_admit_it(self):
        """The ``auto_approve_tools`` loop matches only the agent-authored TITLE.
        It is safe ONLY because this refusal returns before the loop is reached —
        so a pattern as broad as ``*`` must still not admit an unverified call."""
        cfg = HooksConfig(auto_approve_tools=["*", "Running: *"])
        res = HookManager(cfg).on_tool_call("Running: something", is_shell=True, command=None)
        assert res.action == TOOL_DENY

        # Control: the same config DOES auto-approve once the command is present,
        # so the assertion above pins the refusal and not a broken fixture.
        ok = HookManager(cfg).on_tool_call("Running: ls", is_shell=True, command="ls")
        assert ok.action == TOOL_AUTO_APPROVE


class TestPathTierUnderAResolverStall:
    """What the gate SAYS when the resolver cannot answer in time.

    The decision is the same either way -- unverifiable is refused, fail-closed --
    but a refusal reading ``access to sensitive path: <ordinary file>`` sends an
    agent hunting for a credential in a project file, or concluding the session
    has been locked down. The gate applies ONE path-tier check,
    ``security.sensitive_path_refusal``; the verdict it is built on is stubbed on
    the owning module, which is the global the real function reads.
    """

    def test_a_stalled_file_read_is_refused_as_unverifiable_not_as_sensitive(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew import security

        target = tmp_path / "ws" / "README.md"
        target.parent.mkdir()
        target.write_text("x")

        def stalled(*args, **kwargs):
            raise security.PathResolutionStalled(str(target), "/x")

        monkeypatch.setattr(security.paths, "_path_in_home_dirs", stalled)
        result = HookManager().on_tool_call(
            f"Reading {target}", tool_kind="read", raw_params={"path": str(target)}
        )
        assert result.action == TOOL_DENY
        assert result.security_deny is True, "still a hard refusal, not policy state"
        assert "access to sensitive path" not in result.reason
        assert security.is_unverifiable_path_refusal(result.reason)
        assert "NOT a match" in result.reason
        assert repr(str(target)) in result.reason  # quoted: the unverifiable wording uses the repr

    def test_a_sensitive_read_still_says_sensitive(self, monkeypatch):
        from kiro_crew import security

        monkeypatch.setattr(security.paths, "_path_in_home_dirs", lambda *a, **k: True)
        result = HookManager().on_tool_call("~/.aws/credentials", tool_kind="read")
        assert result.action == TOOL_DENY
        assert result.reason == "Blocked: access to sensitive path: ~/.aws/credentials"

    def test_safe_read_file_keeps_the_repr_quoted_match_wording(self, monkeypatch, tmp_path):
        from kiro_crew import hooks, security

        target = tmp_path / "plain.txt"
        target.write_text("x")
        monkeypatch.setattr(security.paths, "_path_in_home_dirs", lambda *a, **k: True)
        with pytest.raises(PermissionError) as info:
            hooks.safe_read_file(str(target))
        assert str(info.value) == f"Blocked: access to sensitive path: {str(target)!r}"

    def test_safe_read_file_passes_the_unverifiable_wording_through(self, monkeypatch, tmp_path):
        from kiro_crew import hooks, security

        target = tmp_path / "plain.txt"
        target.write_text("x")

        def stalled(*args, **kwargs):
            raise security.PathResolutionStalled(str(target), "/x")

        monkeypatch.setattr(security.paths, "_path_in_home_dirs", stalled)
        with pytest.raises(PermissionError) as info:
            hooks.safe_read_file(str(target))
        assert security.is_unverifiable_path_refusal(str(info.value))
        assert "access to sensitive path" not in str(info.value)

    def test_safe_read_file_escapes_a_matched_path_spelled_like_the_stall_wording(
        self, monkeypatch, tmp_path
    ):
        """The stall is recognised by a fixed prefix the path cannot reach, so a
        matched path that carries the stall wording -- and a forged newline -- is
        still re-spelled with the repr (the log-forgery guard)."""
        import os

        from kiro_crew import hooks, security

        forged = str(tmp_path / f"{security.UNVERIFIABLE_PATH_PREFIX}\nWARNING forged")
        resolved = os.path.realpath(forged)
        monkeypatch.setattr(security.paths, "_path_in_home_dirs", lambda *a, **k: True)
        with pytest.raises(PermissionError) as info:
            hooks.safe_read_file(forged)
        message = str(info.value)
        assert message == f"Blocked: access to sensitive path: {resolved!r}"
        assert "\n" not in message


class TestShellCommandTextLeavesThePathTier:
    """A sandboxed shell tool's recovered COMMAND is command text, and the gate does
    not match paths in command text (the sandbox holds the credential stores away
    from the shell). Resolving ``cd /x && grep ...`` as a filename matches
    nothing, spends a resolver round-trip per call, and under a resolver stall
    yields ``access to sensitive path: cd /x && grep ...`` -- a refusal naming a
    non-path as a credential. So exactly that string (and the title when it IS
    that string) is spared the path tier; every other target still pays it.
    """

    CMD = "cd /workplace/me/proj && grep -rn verified_reverse_launch_into src"

    def test_the_recovered_command_never_enters_the_path_tier(self, monkeypatch):
        from kiro_crew import hooks

        asked: list[str] = []

        def recording(p, base_dir=None):
            asked.append(p)
            return "Blocked: sensitive path could not be verified"

        monkeypatch.setattr(hooks, "sensitive_path_refusal", recording)
        result = HookManager().on_tool_call(f"Running: {self.CMD}", command=self.CMD, is_shell=True)
        assert asked == [], "no path resolution on the recovered command text"
        assert result.action != TOOL_DENY or "sensitive path" not in result.reason

    def test_a_shell_command_that_is_a_bare_path_is_left_to_the_sandbox(self, monkeypatch):
        """The claude-agent-acp adapter's Bash title is the bare command. A command
        that happens to be a bare path is command text all the same: the path tier
        is not consulted, and the sandbox is what stands between the shell and the
        credential store (see the spec's "does not match PATHS in command text")."""
        from kiro_crew import hooks

        asked: list[str] = []
        monkeypatch.setattr(
            hooks,
            "sensitive_path_refusal",
            lambda p, base_dir=None: asked.append(p) or "Blocked: sensitive path",
        )
        HookManager().on_tool_call(
            "~/.aws/credentials", command="~/.aws/credentials", is_shell=True
        )
        assert asked == []

    def test_a_shell_title_that_is_not_the_command_still_pays_the_path_tier(self, monkeypatch):
        """An LLM-authored title on a shell frame is not command text; it is gated as
        before (the resolver sees it, and a match on it is refused)."""
        from kiro_crew import hooks

        asked: list[str] = []
        monkeypatch.setattr(
            hooks, "sensitive_path_refusal", lambda p, base_dir=None: asked.append(p) or None
        )
        HookManager().on_tool_call("list the repo", command=self.CMD, is_shell=True)
        assert asked == ["list the repo"]

    def test_a_non_shell_title_is_still_path_gated(self, monkeypatch):
        """The Claude Code adapter's file-read title is the bare path with no prefix
        and no raw_params; the title tier is what fences it, and it must keep doing so."""
        from kiro_crew import hooks

        asked: list[str] = []

        def recording(p, base_dir=None):
            asked.append(p)
            return f"Blocked: access to sensitive path: {p}"

        monkeypatch.setattr(hooks, "sensitive_path_refusal", recording)
        result = HookManager().on_tool_call("~/.ssh/id_rsa")
        assert asked == ["~/.ssh/id_rsa"]
        assert result.action == TOOL_DENY
        assert result.reason == "Blocked: access to sensitive path: ~/.ssh/id_rsa"

    def test_a_non_shell_read_with_raw_params_is_still_path_gated(self, monkeypatch):
        from kiro_crew import hooks

        monkeypatch.setattr(
            hooks,
            "sensitive_path_refusal",
            lambda p, base_dir=None: f"Blocked: access to sensitive path: {p}",
        )
        result = HookManager().on_tool_call(
            "Reading ~/.ssh/id_rsa", tool_kind="read", raw_params={"path": "~/.ssh/id_rsa"}
        )
        assert result.action == TOOL_DENY
        assert "sensitive path" in result.reason

    def test_an_mcp_served_shell_frame_keeps_the_path_tier(self, monkeypatch):
        """kiro-cli can classify an execute-kind frame as shell while naming an MCP
        server. An MCP-served tool runs outside the sandbox the exemption leans
        on, so its command stays path-gated."""
        from kiro_crew import hooks

        asked: list[str] = []

        def recording(p, base_dir=None):
            asked.append(p)
            return f"Blocked: access to sensitive path: {p}"

        monkeypatch.setattr(hooks, "sensitive_path_refusal", recording)
        result = HookManager().on_tool_call(
            "~/.aws/credentials",
            command="~/.aws/credentials",
            is_shell=True,
            mcp_server_name="local-files",
            mcp_tool_name="read",
        )
        assert asked, "an MCP-served frame must reach the path tier"
        assert result.action == TOOL_DENY
        assert result.reason == "Blocked: access to sensitive path: ~/.aws/credentials"

    def test_raw_params_paths_on_a_shell_frame_are_still_refused(self, monkeypatch):
        """The raw_params path tier is independent of the command exemption: a shell-
        classified frame carrying a path parameter is still refused on it."""
        from kiro_crew import hooks

        monkeypatch.setattr(
            hooks,
            "sensitive_path_refusal",
            lambda p, base_dir=None: f"Blocked: access to sensitive path: {p}",
        )
        result = HookManager().on_tool_call(
            "Running: read",
            command="read",
            is_shell=True,
            tool_kind="read",
            raw_params={"path": "~/.ssh/id_rsa"},
        )
        assert result.action == TOOL_DENY
        assert "sensitive path" in result.reason

    def test_a_verified_tool_identity_title_leaves_the_path_tier(self, monkeypatch):
        """A title that is exactly the call's own verified ``@server/tool``
        identity names a tool, not a file -- a resolver stall cannot refuse it."""
        from kiro_crew import hooks

        asked: list[str] = []

        def stalled(p, base_dir=None):
            asked.append(p)
            return f"{hooks.security.UNVERIFIABLE_PATH_PREFIX}, so it is refused. Path: {p!r}"

        monkeypatch.setattr(hooks, "sensitive_path_refusal", stalled)
        result = HookManager().on_tool_call(
            "@kirocrew-core/wait",
            mcp_server_name="kirocrew-core",
            mcp_tool_name="wait",
            mcp_identity_trusted=True,
        )
        assert "@kirocrew-core/wait" not in asked
        assert result.action != TOOL_DENY or "Path:" not in result.reason

    def test_a_bare_server_title_with_a_known_tool_is_not_exempt(self, monkeypatch):
        """When the tool is proven the call's own identity is ``@server/tool``; a
        bare ``@server`` title names a broader thing than the specific tool that
        ran, so it is NOT this call's identity and must stay path-gated."""
        from kiro_crew import hooks

        asked: list[str] = []

        def recording(p, base_dir=None):
            asked.append(p)
            return f"Blocked: access to sensitive path: {p}"

        monkeypatch.setattr(hooks, "sensitive_path_refusal", recording)
        result = HookManager().on_tool_call(
            "@kirocrew-core",
            mcp_server_name="kirocrew-core",
            mcp_tool_name="wait",
            mcp_identity_trusted=True,
        )
        assert "@kirocrew-core" in asked
        assert result.action == TOOL_DENY

    @pytest.mark.parametrize(
        ("title", "trusted"),
        [
            ("@kirocrew-core/wait", False),
            ("~/.ssh/id_rsa", True),
            ("@kirocrew-core/wait/x", True),
            ("@kirocrew-core/other", True),
        ],
    )
    def test_any_other_title_of_an_mcp_call_is_still_path_gated(self, monkeypatch, title, trusted):
        from kiro_crew import hooks

        asked: list[str] = []

        def recording(p, base_dir=None):
            asked.append(p)
            return f"Blocked: access to sensitive path: {p}"

        monkeypatch.setattr(hooks, "sensitive_path_refusal", recording)
        result = HookManager().on_tool_call(
            title,
            mcp_server_name="kirocrew-core",
            mcp_tool_name="wait",
            mcp_identity_trusted=trusted,
        )
        assert title in asked
        assert result.action == TOOL_DENY
