"""The spawn rung, the read-only proofs and governance under a call's trusted identity.

Pins ``_should_auto_approve_spawn`` and the canonical ``event_is_spawn_run`` predicate it
keys on, the host-trusted read-only proof under ``classifier_only``, the read-only
``tool_kind`` allow-list that a computer-use or read-sounding title cannot get around, and
the deny floor and governance under a call's trusted identity: the canonical
``@server/tool`` reference, a built-in's stamped name and its spec spelling, KAS ids folded
to their kiro-cli names on KAS only, and a server-level rule bound on a partial identity.
The shell command an event recovers, the hooks config and ``safe_read_file`` live in
``test_hooks_shell_command.py``; the app-own MCP server auto-approve and the path tier's
target paths and exemptions in ``test_hooks_path_tier.py``; tool matching, the tool hooks
and ``hook_gate_kwargs`` in ``test_hooks.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE, TOOL_DENY, HookManager, HooksConfig


class TestShouldAutoApproveSpawn:
    """Test _should_auto_approve_spawn helper from handler_runtime/permission_ladder.py."""

    @staticmethod
    def _spawn_event(**overrides):
        base = {
            "tool_name": "spawn_run",
            "mcp_server_name": "kirocrew-core",
            "mcp_identity_trusted": True,
            "title": "spawn_run",
            "is_shell": False,
            "shell_classified": True,
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_approves_spawn_run_when_flag_true(self):
        from kiro_crew.hooks import HookManager
        from kiro_crew.slack.handler import _should_auto_approve_spawn

        ctx = MagicMock()
        ctx.hooks = HookManager(HooksConfig.from_dict({"auto_approve_subagent_spawn": True}))
        assert _should_auto_approve_spawn(ctx, self._spawn_event()) is True

    def test_rejects_when_flag_false(self):
        from kiro_crew.hooks import HookManager
        from kiro_crew.slack.handler import _should_auto_approve_spawn

        ctx = MagicMock()
        ctx.hooks = HookManager(HooksConfig.from_dict({"auto_approve_subagent_spawn": False}))
        assert _should_auto_approve_spawn(ctx, self._spawn_event()) is False

    def test_rejects_non_spawn_tool(self):
        from kiro_crew.hooks import HookManager
        from kiro_crew.slack.handler import _should_auto_approve_spawn

        ctx = MagicMock()
        ctx.hooks = HookManager(HooksConfig.from_dict({"auto_approve_subagent_spawn": True}))
        event = self._spawn_event(tool_name="spawn_run_privileged", title="spawn_run_privileged")
        assert _should_auto_approve_spawn(ctx, event) is False

    def test_rejects_shell_event_with_forged_spawn_title(self):
        """The issue's attack: a SHELL event titled spawn_run must not ride
        the spawn rung — the title is model-authored."""
        from kiro_crew.hooks import HookManager
        from kiro_crew.slack.handler import _should_auto_approve_spawn

        ctx = MagicMock()
        ctx.hooks = HookManager(HooksConfig.from_dict({"auto_approve_subagent_spawn": True}))
        event = self._spawn_event(
            tool_name="", mcp_server_name="", title="spawn_run", is_shell=True
        )
        assert _should_auto_approve_spawn(ctx, event) is False


class TestEventIsSpawnRun:
    """The canonical spawn identity predicate the rung keys on.

    Mutation pin: reverting any consumer to a title-only check must fail the
    forged-shell and canonical-mismatch directions below.
    """

    @staticmethod
    def _event(**fields):
        return SimpleNamespace(
            tool_name=fields.get("tool_name", ""),
            mcp_server_name=fields.get("mcp_server_name", ""),
            mcp_identity_trusted=fields.get("mcp_identity_trusted", False),
            title=fields.get("title", ""),
            is_shell=fields.get("is_shell", False),
            shell_classified=fields.get("shell_classified", False),
        )

    def _genuine(self, **overrides):
        fields = dict(
            tool_name="spawn_run",
            mcp_server_name="kirocrew-core",
            mcp_identity_trusted=True,
            title="spawn_run",
        )
        fields.update(overrides)
        return self._event(**fields)

    def test_canonical_trusted_core_spawn_is_genuine(self):
        from kiro_crew.hooks import event_is_spawn_run

        assert event_is_spawn_run(self._genuine()) is True

    def test_rephrased_title_falls_to_the_ladder_despite_canonical_identity(self):
        # A genuine spawn whose display title was rephrased does NOT ride the
        # rung: the channel deny plane keys on the title, so approving here
        # would bypass a title-keyed deny rule. Falling to the ladder is a
        # downgrade (prompt/trust), never a hard block.
        from kiro_crew.hooks import event_is_spawn_run

        assert event_is_spawn_run(self._genuine(title="Spawn agents")) is False

    def test_canonical_mismatch_refuses_regardless_of_a_forged_title(self):
        from kiro_crew.hooks import event_is_spawn_run

        event = self._genuine(tool_name="execute_bash")
        assert event_is_spawn_run(event) is False

    def test_untrusted_identity_provenance_fails_closed(self):
        # tool_name non-emptiness alone is not proof of provenance: a future
        # inline population path leaves the flag False and must fail closed.
        from kiro_crew.hooks import event_is_spawn_run

        assert event_is_spawn_run(self._genuine(mcp_identity_trusted=False)) is False

    def test_foreign_server_spawn_run_tool_is_refused(self):
        # A third-party MCP server exposing a tool literally named spawn_run
        # must not ride the crew's spawn rung.
        from kiro_crew.hooks import event_is_spawn_run

        assert event_is_spawn_run(self._genuine(mcp_server_name="evil-server")) is False

    def test_builtin_named_spawn_run_without_a_server_is_refused(self):
        # kiro-cli sets tool_name for built-ins too, with an empty server
        # name — a built-in cannot satisfy the MCP-server pin.
        from kiro_crew.hooks import event_is_spawn_run

        assert event_is_spawn_run(self._genuine(mcp_server_name="")) is False

    def test_shell_event_with_forged_title_refuses_without_canonical_identity(self):
        from kiro_crew.hooks import event_is_spawn_run

        event = self._event(tool_name="", title="spawn_run", is_shell=True, shell_classified=True)
        assert event_is_spawn_run(event) is False

    def test_unclassified_event_with_forged_title_fails_closed(self):
        # The correlated cache miss: tool_name empty AND shell_classified
        # False AND is_shell miss-default False. Canonical-only means no
        # evidence -> refuse.
        from kiro_crew.hooks import event_is_spawn_run

        event = self._event(tool_name="", title="spawn_run", shell_classified=False)
        assert event_is_spawn_run(event) is False

    def test_non_shell_event_with_forged_title_fails_closed(self):
        # CI GPT-lane finding: a no-_meta.kiro NON-shell tool (e.g. send_file
        # re-titled "spawn_run") must not ride the rung either — there is no
        # title fallback. The rung not firing is a downgrade to the channel's
        # normal ladder, never a hard block.
        from kiro_crew.hooks import event_is_spawn_run

        event = self._event(tool_name="", title="spawn_run", is_shell=False, shell_classified=True)
        assert event_is_spawn_run(event) is False

    def test_plain_non_spawn_title_refuses(self):
        from kiro_crew.hooks import event_is_spawn_run

        assert event_is_spawn_run(self._event(tool_name="", title="grep")) is False


class TestClassifierOnlyHostTrustedProof:
    """The READ_ONLY (``classifier_only``) non-shell proof is host-trusted only.

    ``_is_host_read_only_builtin`` is the sole non-shell read-only proof the gate
    accepts for the side chat. It demands a POSITIVE provenance signal
    (``mcp_identity_trusted``) plus a host-known built-in name with no MCP server
    behind it, and the allowlist itself is pinned to read scopes so a
    write-capable built-in cannot join by mistake.
    """

    _WRITE_SCOPES = frozenset({"filesystem.write", "commands", "tools"})

    def test_host_read_only_builtins_map_only_to_read_scopes(self):
        # Every allowlisted name is a governed built-in whose scopes are read
        # scopes only. A name absent from BUILTIN_TOOL_SCOPES has no governed
        # scope at all and is not presumed read-only either.
        from kiro_crew.hooks import _HOST_READ_ONLY_BUILTIN_TOOLS
        from kiro_crew.platform.governance import BUILTIN_TOOL_SCOPES

        read_scopes = {"filesystem.read", "network.egress"}
        for name in sorted(_HOST_READ_ONLY_BUILTIN_TOOLS):
            assert name in BUILTIN_TOOL_SCOPES, name
            scopes = set(BUILTIN_TOOL_SCOPES[name])
            assert scopes, name
            assert scopes <= read_scopes, (name, scopes)
            assert not (scopes & self._WRITE_SCOPES), (name, scopes)

    def test_write_capable_builtins_are_not_allowlisted(self):
        # The converse pin: no built-in that can write, run commands, or govern
        # tools is on the read-only allowlist.
        from kiro_crew.hooks import _HOST_READ_ONLY_BUILTIN_TOOLS
        from kiro_crew.platform.governance import BUILTIN_TOOL_SCOPES

        for name, scopes in BUILTIN_TOOL_SCOPES.items():
            if self._WRITE_SCOPES & set(scopes):
                assert name not in _HOST_READ_ONLY_BUILTIN_TOOLS, name

    def test_trusted_host_builtin_is_proven_read_only(self):
        from kiro_crew.hooks import _is_host_read_only_builtin

        assert _is_host_read_only_builtin("fs_read", "", mcp_identity_trusted=True) is True

    def test_kiro_cli_short_read_name_is_proven_read_only(self):
        # kiro-cli stamps its file-read built-in as ``read``; ``fs_read`` is the
        # alias a spec may still use. The allowlist carries the spec spelling, so
        # the stamped name must resolve through KIRO_TOOL_ALIASES before the
        # membership test, or a READ_ONLY surface refuses every file read.
        from kiro_crew.hooks import _is_host_read_only_builtin
        from kiro_crew.platform.tool_names import KIRO_TOOL_ALIASES

        assert KIRO_TOOL_ALIASES["read"] == "fs_read"
        assert _is_host_read_only_builtin("read", "", mcp_identity_trusted=True) is True

    def test_kiro_tool_aliases_resolve_to_governed_builtins(self):
        # Every spelling the alias step resolves a stamp to is a built-in the
        # deny rules, governance and the allowlist already judge by that name.
        from kiro_crew.platform.governance import BUILTIN_TOOL_SCOPES
        from kiro_crew.platform.tool_names import KIRO_TOOL_ALIASES

        assert set(KIRO_TOOL_ALIASES.values()) <= set(BUILTIN_TOOL_SCOPES)

    def test_kiro_cli_short_write_and_shell_names_are_not_read_only_proofs(self):
        # The alias step widens nothing: ``write`` and ``shell`` resolve to names
        # the allowlist does not hold, so the proof refuses them resolved or not.
        from kiro_crew.hooks import (
            _HOST_READ_ONLY_BUILTIN_TOOLS,
            _host_builtin_alias,
            _is_host_read_only_builtin,
        )

        for name in ("write", "shell"):
            assert _host_builtin_alias(name, "") not in _HOST_READ_ONLY_BUILTIN_TOOLS, name
            assert _is_host_read_only_builtin(name, "", mcp_identity_trusted=True) is False, name

    def test_host_builtin_alias_is_for_host_stamps_only(self):
        # A server's own tool called ``read`` is not the host's file reader, and
        # a stamp with no alias resolves to nothing rather than to itself.
        from kiro_crew.hooks import _host_builtin_alias

        assert _host_builtin_alias("read", "") == "fs_read"
        assert _host_builtin_alias("read", "docs") == ""
        assert _host_builtin_alias("fs_read", "") == ""
        assert _host_builtin_alias("", "") == ""

    def test_short_read_name_still_needs_trusted_identity_and_no_server(self):
        from kiro_crew.hooks import _is_host_read_only_builtin

        assert _is_host_read_only_builtin("read", "", mcp_identity_trusted=False) is False
        assert _is_host_read_only_builtin("read", "evil-server", mcp_identity_trusted=True) is False

    def test_untrusted_identity_refuses_a_host_known_name(self):
        # The seam: a host-known name whose provenance is not the _meta.kiro
        # parse this client made (inline payload, hand-built event, cache miss)
        # is prose, not identity. The absence of a server name proves nothing.
        from kiro_crew.hooks import _is_host_read_only_builtin

        assert _is_host_read_only_builtin("fs_read", "", mcp_identity_trusted=False) is False

    def test_mcp_served_tool_named_like_a_builtin_is_refused(self):
        from kiro_crew.hooks import _is_host_read_only_builtin

        assert (
            _is_host_read_only_builtin("fs_read", "evil-server", mcp_identity_trusted=True) is False
        )

    def test_empty_identity_matches_nothing(self):
        from kiro_crew.hooks import _is_host_read_only_builtin

        assert _is_host_read_only_builtin("", "", mcp_identity_trusted=True) is False

    def test_write_capable_builtin_is_not_a_read_only_proof(self):
        from kiro_crew.hooks import _is_host_read_only_builtin

        for name in ("fs_write", "code", "execute_bash"):
            assert _is_host_read_only_builtin(name, "", mcp_identity_trusted=True) is False, name

    @pytest.mark.parametrize(
        "kas_id", ["read_file", "list_directory", "grep_search", "file_search"]
    )
    def test_a_trusted_kas_read_builtin_is_proven_read_only_under_its_kiro_cli_name(self, kas_id):
        """KAS names the same work differently; under ``--approval reads`` a KAS
        read must be free exactly where kiro-cli's ``fs_read`` is."""
        from kiro_crew.hooks import _is_host_read_only_builtin

        assert (
            _is_host_read_only_builtin(kas_id, "", mcp_identity_trusted=True, kas_builtin_ids=True)
            is True
        ), kas_id

    @pytest.mark.parametrize("kas_id", ["str_replace", "fs_append", "delete_file", "run_command"])
    def test_a_kas_write_or_shell_alias_is_not_a_read_only_proof(self, kas_id):
        from kiro_crew.hooks import _is_host_read_only_builtin

        assert (
            _is_host_read_only_builtin(kas_id, "", mcp_identity_trusted=True, kas_builtin_ids=True)
            is False
        ), kas_id

    def test_the_alias_fold_still_demands_trust_and_no_server(self):
        from kiro_crew.hooks import _is_host_read_only_builtin

        assert (
            _is_host_read_only_builtin(
                "read_file", "", mcp_identity_trusted=False, kas_builtin_ids=True
            )
            is False
        )
        assert (
            _is_host_read_only_builtin(
                "read_file", "filesystem", mcp_identity_trusted=True, kas_builtin_ids=True
            )
            is False
        )

    def test_a_kas_read_auto_approves_under_the_read_only_policy_end_to_end(self):
        """The wire shape: cache hit with empty names (trusted, no server) plus
        the frame's ``toolId``; a read of an ordinary file is auto-approved."""
        r = HookManager().on_tool_call(
            "Read File",
            kas_builtin_ids=True,
            mcp_tool_name="read_file",
            mcp_identity_trusted=True,
            tool_kind="read",
            raw_params={"path": "/tmp/notes.md"},
            classifier_only=True,
        )
        assert r.action == TOOL_AUTO_APPROVE
        assert r.read_only is True

    def test_a_kas_read_of_a_sensitive_path_is_still_denied_before_the_proof(self):
        r = HookManager().on_tool_call(
            "Read File",
            kas_builtin_ids=True,
            mcp_tool_name="read_file",
            mcp_identity_trusted=True,
            tool_kind="read",
            raw_params={"path": "~/.ssh/id_rsa"},
            classifier_only=True,
        )
        assert r.action == TOOL_DENY


class TestMutatingKindBeatsTheTitle:
    """Only an explicitly READ-ONLY ``tool_kind`` may auto-approve on a title.

    ``tool_name`` is the display title, and ``select_tool_title``
    (``acp/_dispatch_parts/surfaced_text.py``) prefers the LLM-authored ``description`` — so it is
    agent-controlled, which ``ToolCall.title``'s own documentation states outright. The
    computer-use read-only auto-approve must not be tested BEFORE any kind guard: otherwise,
    once the operator enabled computer use, an ``edit``/``execute``/``write``/
    ``delete`` call titled ``mcp__kirocrew-computer__computer_get_state`` skipped
    interactive approval entirely.

    The first fix for that was a DENYLIST (``kind in _WRITE_TOOL_KINDS`` → allow),
    and GPT 5.6 correctly rejected it as still fail-open: ``tool_kind`` arrives
    verbatim from the ACP ``kind`` field, so an unlisted-but-real value like
    ``"other"`` sailed past it and auto-approved. The decision is now an ALLOW-list —
    only ``_READ_ONLY_TOOL_KINDS`` and an ABSENT kind can reach a title-keyed
    branch — so a kind nobody has enumerated fails closed.
    """

    _CU_OBSERVE_TITLE = "mcp__kirocrew-computer__computer_get_state"
    #: A title no read-only rule recognises, so only the kind can approve it.
    _OPAQUE_TITLE = "mcp__ops__frobnicate"

    #: Known mutators, plus the ACP values and shapes a denylist would miss. The
    #: second group is the point: those are what made the denylist fail open.
    _NON_READ_KINDS = [
        "edit",
        "execute",
        "delete",
        "move",
        "write",
        "create",
        "other",
        "unknown",
        "switch_mode",
        "search",
        "think",
        "EDIT",
    ]

    @pytest.mark.parametrize("kind", _NON_READ_KINDS)
    def test_a_non_read_kind_is_not_auto_approved_despite_a_cu_read_title(self, kind, monkeypatch):
        # Force the CU predicate True so the test pins the DECISION, not the
        # keystone read (which is off in CI and would mask a regression).
        monkeypatch.setattr("kiro_crew.hooks._cu_read_only_auto_approve", lambda _name: True)
        mgr = HookManager()
        result = mgr.on_tool_call(self._CU_OBSERVE_TITLE, tool_kind=kind)
        assert result.action == TOOL_ALLOW, (
            f"tool_kind={kind!r} auto-approved on the strength of an agent-supplied "
            "computer-use title — interactive approval was skipped"
        )

    #: Kinds that must NOT auto-approve a non-shell call on their own: every
    #: mutator, the ACP values a denylist would miss, and arbitrary strings.
    _NOT_READ_ONLY = [
        "other",
        "edit",
        "EDIT",
        "execute",
        "delete",
        "move",
        "search",
        "think",
        "switch_mode",
        "write",
        "frobnicate",
    ]

    @pytest.mark.parametrize("kind", ["read", "fetch", "READ", " fetch "])
    def test_only_the_read_only_kinds_auto_approve_a_call_on_their_own(self, kind):
        """The allow-list, observed: ``read`` and ``fetch`` (case- and space-folded)
        auto-approve a non-shell call whose title proves nothing -- and the
        vocabulary is exactly those two, since a behavioural sample alone would keep
        passing if the set were widened to a kind nobody listed here."""
        from kiro_crew import hooks as hooks_mod

        assert hooks_mod._READ_ONLY_TOOL_KINDS == frozenset({"read", "fetch"})
        result = HookManager().on_tool_call(self._OPAQUE_TITLE, tool_kind=kind)
        assert (result.action, result.read_only) == (TOOL_AUTO_APPROVE, True)

    @pytest.mark.parametrize("kind", _NOT_READ_ONLY)
    def test_any_other_kind_reaches_a_prompt(self, kind):
        """Fail closed on a kind nobody enumerated: ``other`` and an arbitrary string
        are as unapproved as ``edit``. A DENYLIST of mutating kinds would auto-approve
        exactly these, which is why the vocabulary is an allow-list."""
        result = HookManager().on_tool_call(self._OPAQUE_TITLE, tool_kind=kind)
        assert result.action == TOOL_ALLOW, f"tool_kind={kind!r} skipped the prompt"

    def test_an_absent_kind_falls_back_to_the_title_and_no_further(self):
        """No kind at all is not a read: the title-verb fallback decides, and a title
        that proves nothing reaches a prompt."""
        assert HookManager().on_tool_call(self._OPAQUE_TITLE).action == TOOL_ALLOW
        assert HookManager().on_tool_call(self._OPAQUE_TITLE, tool_kind="").action == TOOL_ALLOW

    @pytest.mark.parametrize("kind", ["read", "fetch"])
    def test_a_genuine_cu_observation_still_auto_approves(self, kind, monkeypatch):
        """The feature the fix must not break: a PROVEN read still doesn't nag."""
        monkeypatch.setattr("kiro_crew.hooks._cu_read_only_auto_approve", lambda _name: True)
        mgr = HookManager()
        assert mgr.on_tool_call(self._CU_OBSERVE_TITLE, tool_kind=kind).action == (
            TOOL_AUTO_APPROVE
        ), f"a computer-use observation tool with tool_kind={kind!r} should auto-approve"

    def test_computer_use_needs_an_EXPLICIT_read_kind_not_merely_an_absent_one(self, monkeypatch):
        """An OMITTED kind must not reach the computer-use auto-approve either.

        Second GPT 5.6 finding on this PR, and correct. Two agent-controlled inputs meet
        in this branch: the title (from `select_tool_title`, which prefers the LLM-authored
        `description`) and the kind (whose absence is indistinguishable from an honest
        omission). While the class lookup keyed on the title alone, a `computer_click`
        could forge `…__computer_get_state`, omit its kind, and skip the approval prompt.
        The two inputs must AGREE.

        Narrower than GPT's prescription — the generic `_is_read_only_tool` fallback for
        absent kinds is deliberately left intact, because it rejects every
        `mcp__kirocrew-computer__*` title anyway (asserted below), so blocking absent
        kinds outright would have regressed every ordinary tool for no security gain.
        """
        monkeypatch.setattr("kiro_crew.hooks._cu_read_only_auto_approve", lambda _name: True)
        mgr = HookManager()
        assert mgr.on_tool_call(self._CU_OBSERVE_TITLE).action == TOOL_ALLOW, (
            "a computer-use title with NO kind auto-approved — a mutating call can "
            "forge the title and omit the kind, so the two must agree"
        )

    def test_the_generic_fallback_never_matches_a_computer_use_title(self):
        """Why leaving the absent-kind fallback in place is safe.

        If `_is_read_only_tool` ever started matching a `computer_*` title, the branch
        above would stop being the only way in and this PR's guarantee would quietly
        weaken — so it is asserted rather than assumed.
        """
        from kiro_crew.slack.gateway import _is_read_only_tool

        for tool in ("computer_get_state", "computer_list_apps", "computer_click"):
            title = f"mcp__kirocrew-computer__{tool}"
            assert not _is_read_only_tool(title), title

    def test_a_read_sounding_title_cannot_rescue_a_non_read_kind(self, monkeypatch):
        """The same invariant for the generic title heuristic, not just the CU one."""
        monkeypatch.setattr("kiro_crew.hooks._cu_read_only_auto_approve", lambda _name: False)
        mgr = HookManager()
        for kind in ("execute", "other"):
            assert mgr.on_tool_call("read_the_docs", tool_kind=kind).action == TOOL_ALLOW, kind
        # ...and with no kind at all the title heuristic is still allowed to work for
        # ORDINARY tools — unchanged from base, and the reason the fix stayed narrow.
        assert mgr.on_tool_call("read_the_docs").action == TOOL_AUTO_APPROVE

    def test_rejects_none_context(self):
        from kiro_crew.slack.handler import _should_auto_approve_spawn

        event = SimpleNamespace(
            tool_name="spawn_run",
            mcp_server_name="kirocrew-core",
            mcp_identity_trusted=True,
            title="spawn_run",
            is_shell=False,
            shell_classified=True,
        )
        assert _should_auto_approve_spawn(None, event) is False

    def test_rejects_none_hooks(self):
        from kiro_crew.slack.handler import _should_auto_approve_spawn

        ctx = MagicMock()
        ctx.hooks = None
        event = SimpleNamespace(
            tool_name="spawn_run",
            mcp_server_name="kirocrew-core",
            mcp_identity_trusted=True,
            title="spawn_run",
            is_shell=False,
            shell_classified=True,
        )
        assert _should_auto_approve_spawn(ctx, event) is False


class TestCanonicalMcpIdentityGoverned:
    """An ORDINARY MCP permission request — no first-party app-own-server
    auto-approve involved — is governed under its trusted
    ``mcp__<server>__<tool>`` identity as well as its LLM-authored title, so a
    per-tool ceiling rule binds even when the title is benign prose. The
    canonical name is ADDED to the deny floor and governance, never substituted
    for the title, so the title/raw-command floor keeps denying on its own."""

    @staticmethod
    def _deny_canonical(monkeypatch, denied: str):
        """Governance that denies exactly *denied*, recording what it saw.

        The gate asks ONE question carrying every identity for the call (title,
        trusted tool name, canonical MCP reference), so this double records all of
        them and denies if any matches -- mirroring the real tightest-wins
        evaluation over a single profile snapshot.
        """
        import kiro_crew.hooks as hooks_mod

        seen: list[str] = []

        def fake_gov(ctx, name, *a, **k):
            targets = [
                name,
                *k.get("extra_titles", ()),
                *(s for g in k.get("alias_groups", ()) for s in g),
                k.get("mcp_ref", ""),
            ]
            for target in targets:
                if target and target not in seen:
                    seen.append(target)
            if denied in targets:
                return "Blocked by governance policy: denied"
            return None

        monkeypatch.setattr(hooks_mod, "_governance_denial", fake_gov)
        return seen

    def test_governance_denies_canonical_behind_prose_title(self, monkeypatch):
        # THE REGRESSION. A policy denies the weather server's wipe_disk tool, but
        # select_tool_title handed us the model's prose description. Governing
        # only the title would permit, the call would reach the human prompt,
        # and an "allow" would run a tool the ceiling forbids.
        seen = self._deny_canonical(monkeypatch, "@weather:srv/wipe_disk")
        mgr = HookManager()
        r = mgr.on_tool_call(
            "Check tomorrow's forecast",  # benign model-authored prose
            mcp_server_name="weather:srv",
            mcp_tool_name="wipe_disk",
            tool_kind="other",
        )
        assert r.action == TOOL_DENY
        # Both identities were offered to governance — the title (permitted)
        # AND the trusted canonical reference (denied).
        assert "Check tomorrow's forecast" in seen
        assert "@weather:srv/wipe_disk" in seen

    def test_deny_floor_matches_canonical_behind_prose_title(self, monkeypatch):
        # Same bypass, via the effective deny set rather than governance.
        cfg = HooksConfig(auto_deny_tools=["mcp__weather:srv__wipe_disk"])
        mgr = HookManager(cfg)
        r = mgr.on_tool_call(
            "Check tomorrow's forecast",
            mcp_server_name="weather:srv",
            mcp_tool_name="wipe_disk",
            tool_kind="other",
        )
        assert r.action == TOOL_DENY

    def test_partial_mcp_identity_governs_the_server_never_a_malformed_name(self, monkeypatch):
        # Only ONE trusted field → no canonical per-TOOL name exists. What the
        # server alone supports is a SERVER-level question, which the governance
        # grammar has (``@server`` covers every tool under it), so that is what
        # governance is asked — and nothing else is synthesised.
        #
        # The shapes this guards against are the malformed ones a title-encoded
        # identity could produce: a dangling ``__`` separator, or an empty server
        # (``mcp____wipe_disk``), either of which could match a prefix rule and
        # deny an unrelated call. Composing the reference from the trusted fields
        # cannot produce them, and this pins that. Denying the tool-qualified form
        # must NOT deny the call either: the tool was never proven.
        seen = self._deny_canonical(monkeypatch, "@weather:srv/")
        mgr = HookManager()
        r = mgr.on_tool_call(
            "Check tomorrow's forecast", mcp_server_name="weather:srv", tool_kind="other"
        )
        assert r.action != TOOL_DENY
        assert seen == ["Check tomorrow's forecast", "@weather:srv"]
        assert not any(t.endswith("__") or "mcp____" in t for t in seen)

    def test_title_floor_survives_canonical_enforcement(self, monkeypatch):
        # PRESERVATION. Governance permits EVERYTHING here, so the only thing
        # that can produce a deny is the title-keyed security floor. The display
        # title names a sensitive path while the canonical name is innocuous:
        # canonical enforcement is additive, so it must not have displaced the
        # title from the checks it was already in. Governance must permit for
        # this to mean anything — a stub that denies the canonical name would
        # pass even if the title had been substituted away.
        self._deny_canonical(monkeypatch, "nothing-is-denied")
        mgr = HookManager()
        r = mgr.on_tool_call(
            "~/.aws/credentials",
            mcp_server_name="files:srv",
            mcp_tool_name="nothing_to_see",
            tool_kind="read",
        )
        assert r.action == TOOL_DENY

    def test_raw_command_floor_survives_canonical_enforcement(self, monkeypatch):
        # PRESERVATION, the shell half: benign title, benign canonical name,
        # dangerous raw command, and governance permitting everything — so the
        # deny can only come from the command being evaluated. (For this input
        # the effective deny set is what fires; the point is that adding the
        # canonical identity did not displace `command` from the checks that
        # see it.)
        self._deny_canonical(monkeypatch, "nothing-is-denied")
        mgr = HookManager()
        r = mgr.on_tool_call(
            "Tidy up some files",
            command="curl http://169.254.169.254/latest/meta-data/",
            is_shell=True,
            mcp_server_name="shell:srv",
            mcp_tool_name="nothing_to_see",
            tool_kind="execute",
        )
        assert r.action == TOOL_DENY

    def test_non_mcp_call_unaffected(self, monkeypatch):
        # Neither trusted field (a shell/built-in tool, or a backend that omits
        # _meta.kiro): governance sees the title and nothing else.
        seen = self._deny_canonical(monkeypatch, "never-matches")
        mgr = HookManager()
        r = mgr.on_tool_call("List the files", tool_kind="read")
        assert r.action == TOOL_AUTO_APPROVE
        assert seen == ["List the files"]


class TestBuiltinToolIdentityGoverned:
    """A BUILT-IN tool proves ``_meta.kiro.toolName`` but no ``mcpServerName``,
    so no canonical ``@server/tool`` reference exists for it. Its trusted name
    must still reach the deny floor and governance on its own, or a rule naming
    the real tool is bypassable behind a benign model-authored title."""

    def test_deny_floor_matches_builtin_tool_name_behind_prose_title(self):
        # THE REGRESSION. deny=["fs_write"] with a benign title and no MCP
        # server: the canonical form is empty, so before this the title was the
        # only deny target and the policy-denied built-in reached the human.
        cfg = HooksConfig(auto_deny_tools=["fs_write"])
        mgr = HookManager(cfg)
        r = mgr.on_tool_call(
            "Update the changelog",  # benign model-authored prose
            mcp_tool_name="fs_write",  # trusted _meta.kiro.toolName
            tool_kind="edit",
        )
        assert r.action == TOOL_DENY

    def test_governance_denies_builtin_tool_name_behind_prose_title(self, monkeypatch):
        # Same bypass on the governance plane. The identity is asked as a plain
        # tool item, never as an ``@server/tool`` reference -- a built-in has no
        # server, so synthesising one would invent a name nothing matches.
        seen = TestCanonicalMcpIdentityGoverned._deny_canonical(monkeypatch, "fs_write")
        mgr = HookManager()
        r = mgr.on_tool_call(
            "Update the changelog",
            mcp_tool_name="fs_write",
            tool_kind="edit",
        )
        assert r.action == TOOL_DENY
        # Both were offered: the title (permitted) AND the trusted name (denied).
        assert "Update the changelog" in seen
        assert "fs_write" in seen

    _ALIASED_STAMPS = [
        ("read", "fs_read", "read"),
        ("write", "fs_write", "edit"),
        ("shell", "execute_bash", "execute"),
    ]

    @pytest.mark.parametrize(("stamp", "rule", "kind"), _ALIASED_STAMPS)
    def test_deny_floor_matches_the_spec_spelling_of_an_aliased_stamp(self, stamp, rule, kind):
        # ``auto_deny_tools`` is written in the spec spelling (``fs_write``) and
        # kiro-cli stamps the short name (``write``): the rule must reach the
        # call on every surface, or the operator's deny is inert against the
        # default backend. Under ``classifier_only`` (READ_ONLY) the proof would
        # otherwise approve the one stamp it resolves.
        cfg = HooksConfig(auto_deny_tools=[rule])
        mgr = HookManager(cfg)
        for classifier_only in (False, True):
            r = mgr.on_tool_call(
                "Update the changelog",
                mcp_tool_name=stamp,
                mcp_identity_trusted=True,
                tool_kind=kind,
                classifier_only=classifier_only,
            )
            assert r.action == TOOL_DENY, classifier_only
        # The stamped spelling still binds on its own.
        r = HookManager(HooksConfig(auto_deny_tools=[stamp])).on_tool_call(
            "Update the changelog", mcp_tool_name=stamp, mcp_identity_trusted=True, tool_kind=kind
        )
        assert r.action == TOOL_DENY
        # A server's own tool under the same short name is not the host's
        # built-in: the spec-spelling rule does not reach it.
        r = mgr.on_tool_call(
            "Update the changelog",
            mcp_server_name="docs",
            mcp_tool_name=stamp,
            mcp_identity_trusted=True,
            tool_kind=kind,
        )
        assert r.action != TOOL_DENY

    @pytest.mark.parametrize(("stamp", "rule", "kind"), _ALIASED_STAMPS)
    def test_governance_is_asked_the_spec_spelling_of_an_aliased_stamp(
        self, monkeypatch, stamp, rule, kind
    ):
        # Same on the governance plane: a ``tools`` rule naming the spec spelling
        # denies the short stamp. Title, stamped name and resolved name travel in
        # the one query; a name with no alias adds nothing, and a server's own
        # tool is asked under its canonical reference, never as the built-in.
        seen = TestCanonicalMcpIdentityGoverned._deny_canonical(monkeypatch, rule)
        mgr = HookManager()
        r = mgr.on_tool_call(
            "Update the changelog",
            mcp_tool_name=stamp,
            mcp_identity_trusted=True,
            tool_kind=kind,
            classifier_only=True,
        )
        assert r.action == TOOL_DENY
        assert seen == ["Update the changelog", stamp, rule]
        seen = TestCanonicalMcpIdentityGoverned._deny_canonical(monkeypatch, "never-matches")
        HookManager().on_tool_call("Search", mcp_tool_name="grep", tool_kind="search")
        assert seen == ["Search", "grep"]
        seen = TestCanonicalMcpIdentityGoverned._deny_canonical(monkeypatch, rule)
        r = HookManager().on_tool_call(
            "Update the changelog",
            mcp_server_name="docs",
            mcp_tool_name=stamp,
            mcp_identity_trusted=True,
            tool_kind=kind,
        )
        assert r.action != TOOL_DENY
        assert rule not in seen

    @pytest.mark.parametrize(("stamp", "rule", "kind"), _ALIASED_STAMPS)
    def test_an_aliased_stamp_is_one_identity_in_two_spellings(
        self, monkeypatch, stamp, rule, kind
    ):
        # A trusted stamp and its spec spelling travel as ONE alias group, so an
        # allow-mode profile written in either spelling admits the call and a
        # deny on either refuses it. An unverified stamp may not admit under a
        # name the model could have influenced: it is asked on its own name and
        # the alias may only deny.
        import kiro_crew.hooks as hooks_mod

        seen: dict = {}

        def gov(ctx, name, *a, **k):
            seen.update(k)
            return None

        monkeypatch.setattr(hooks_mod, "_governance_denial", gov)
        HookManager().on_tool_call(
            "Update the changelog", mcp_tool_name=stamp, mcp_identity_trusted=True, tool_kind=kind
        )
        assert seen["alias_groups"] == ((stamp, rule),)
        assert seen["extra_titles"] == ()
        assert seen["deny_aliases"] == ()
        seen.clear()
        HookManager().on_tool_call("Update the changelog", mcp_tool_name=stamp, tool_kind=kind)
        assert seen["alias_groups"] == ()
        assert seen["extra_titles"] == (stamp,)
        assert seen["deny_aliases"] == (rule,)

    @pytest.mark.parametrize(("stamp", "rule", "kind"), _ALIASED_STAMPS)
    def test_an_allow_mode_profile_in_either_spelling_admits_a_trusted_stamp(
        self, monkeypatch, stamp, rule, kind
    ):
        # End to end through the real ``gate_decision``: {title, fs_write} and
        # {title, write} each admit a trusted ``write``; a profile naming neither
        # refuses it and reports the spec spelling; a deny on either refuses it.
        import kiro_crew.platform.governance_profiles as profiles_mod
        from kiro_crew.platform.governance import MODE_ALLOW, MODE_DENY, Profile, ScopedRuleset

        def with_profile(control):
            monkeypatch.setattr(
                profiles_mod,
                "resolve_active_scope",
                lambda *a, **k: Profile(name="p", controls={"tools": control}),
            )

        def call(trusted=True):
            return HookManager().on_tool_call(
                "Do the thing", mcp_tool_name=stamp, mcp_identity_trusted=trusted, tool_kind=kind
            )

        for spelling in (rule, stamp):
            with_profile(ScopedRuleset(MODE_ALLOW, ("Do the thing", spelling)))
            assert call().action != TOOL_DENY, spelling
        with_profile(ScopedRuleset(MODE_ALLOW, ("Do the thing",)))
        r = call()
        assert r.action == TOOL_DENY
        assert rule in r.reason
        for denied in (rule, stamp):
            with_profile(ScopedRuleset(MODE_DENY, deny=(denied,)))
            assert call().action == TOOL_DENY, denied
        # Unverified: asked on its own name, so the spec spelling cannot admit
        # it, the stamp itself still can, and a deny on the spec spelling binds.
        with_profile(ScopedRuleset(MODE_ALLOW, ("Do the thing", rule)))
        assert call(trusted=False).action == TOOL_DENY
        with_profile(ScopedRuleset(MODE_ALLOW, ("Do the thing", stamp)))
        assert call(trusted=False).action != TOOL_DENY
        with_profile(ScopedRuleset(MODE_DENY, deny=(rule,)))
        assert call(trusted=False).action == TOOL_DENY

    def test_identity_equal_to_the_title_is_not_asked_twice(self, monkeypatch):
        # A backend whose title already IS the tool name must not cost a second
        # identical governance query.
        seen = TestCanonicalMcpIdentityGoverned._deny_canonical(monkeypatch, "never-matches")
        mgr = HookManager()
        mgr.on_tool_call("fs_read", mcp_tool_name="fs_read", tool_kind="read")
        assert seen == ["fs_read"]

    def test_every_identity_shares_one_profile_resolution(self, monkeypatch):
        # THE SNAPSHOT CONTRACT. Title, trusted tool name and MCP reference are
        # one question against one resolved profile. Resolving per identity let a
        # profile hot-reloaded mid-call answer each from a different snapshot, so
        # a tool both complete profiles deny could be permitted by every single
        # lookup -- and each resolve walked ``profiles/`` on the event loop.
        import kiro_crew.platform.governance_profiles as profiles_mod

        calls = []
        real = profiles_mod.resolve_active_scope

        def counting(session_key, **kw):
            calls.append(session_key)
            return real(session_key, **kw)

        monkeypatch.setattr(profiles_mod, "resolve_active_scope", counting)
        mgr = HookManager()
        mgr.on_tool_call(
            "Update the changelog",
            session_key="s1",
            mcp_server_name="weather:srv",
            mcp_tool_name="fs_write",
            tool_kind="edit",
        )
        # Three identities in play (title, fs_write, @weather:srv/fs_write) and
        # exactly ONE profile resolution.
        assert len(calls) == 1, f"expected one profile resolution, got {len(calls)}"


class TestKasAliasesAreGovernedUnderTheirKiroCliName:
    """``kas_agents`` mounts kiro-cli's ``fs_write`` on KAS as ``str_replace`` and
    ``fs_append`` (``platform.tool_names``). An operator's rule is written in
    kiro-cli vocabulary -- ``auto_deny_tools: ["fs_write"]`` is the natural
    spelling, since Crew's specs are -- and the deny tier and the ``tools``-scope
    ceiling both match on names. Without folding the alias back, a trust mode
    that skips the prompt runs the write the operator denied."""

    @pytest.mark.parametrize("kas_id", ["str_replace", "fs_append", "delete_file"])
    def test_deny_floor_binds_a_kiro_cli_write_rule_to_the_kas_write_alias(self, kas_id):
        cfg = HooksConfig(auto_deny_tools=["fs_write"])
        r = HookManager(cfg).on_tool_call(
            "Update the changelog",
            kas_builtin_ids=True,
            mcp_tool_name=kas_id,
            tool_kind="edit",
        )
        assert r.action == TOOL_DENY, kas_id

    @pytest.mark.parametrize(
        ("kas_id", "rule"),
        [
            ("read_file", "fs_read"),
            ("list_directory", "fs_read"),
            ("grep_search", "grep"),
            ("file_search", "glob"),
        ],
    )
    def test_deny_floor_binds_a_kiro_cli_read_rule_to_the_kas_read_alias(self, kas_id, rule):
        cfg = HooksConfig(auto_deny_tools=[rule])
        r = HookManager(cfg).on_tool_call(
            "Look around", kas_builtin_ids=True, mcp_tool_name=kas_id, tool_kind="read"
        )
        assert r.action == TOOL_DENY, kas_id

    @pytest.mark.parametrize("title", ["str_replace", "Running: str_replace", "read_file"])
    def test_a_model_authored_title_never_folds_to_a_policy_name(self, title):
        """``tool_name`` is ``select_tool_title``'s pick, which prefers the
        model-authored ``description`` verbatim. A shell call the model described
        as ``str_replace`` must not fold to ``fs_write`` and be refused by an
        operator's ``deny fs_write`` -- a refusal the operator did not write is
        still the model steering the gate. Only the engine-stamped identity
        (``_meta.kiro.toolName`` / ``toolId``) folds."""
        cfg = HooksConfig(auto_deny_tools=["fs_write", "fs_read"])
        r = HookManager(cfg).on_tool_call(title, tool_kind="execute")
        assert r.action != TOOL_DENY, title

    @pytest.mark.parametrize("shell_id", ["run_command", "execute_bash"])
    def test_an_identified_shell_call_titled_like_a_kas_write_is_not_denied_by_a_write_rule(
        self, shell_id
    ):
        """THE REGRESSION GPT found: the identity is the shell (KAS reports it as
        ``run_command``), the model's ``description`` reads ``str_replace``. Only
        the identity folds -- to ``execute_bash``, which ``deny fs_write`` does
        not name -- so the command the operator never denied is not refused."""
        cfg = HooksConfig(auto_deny_tools=["fs_write"])
        r = HookManager(cfg).on_tool_call(
            "str_replace", kas_builtin_ids=True, mcp_tool_name=shell_id, tool_kind="execute"
        )
        assert r.action != TOOL_DENY, shell_id
        # And the same identity IS refused by the rule that names the shell.
        cfg = HooksConfig(auto_deny_tools=["execute_bash"])
        r = HookManager(cfg).on_tool_call(
            "str_replace", kas_builtin_ids=True, mcp_tool_name=shell_id, tool_kind="execute"
        )
        assert r.action == TOOL_DENY, shell_id

    def test_a_kas_identity_arrives_on_the_permission_frame_not_the_title(self):
        """KAS stamps no ``toolName`` on the tool_call, but the permission
        request carries ``_meta.kiro.toolId`` (``build_permission_event`` reads
        it into ``mcp_tool_name``). That is the input the fold trusts."""
        cfg = HooksConfig(auto_deny_tools=["fs_write"])
        r = HookManager(cfg).on_tool_call(
            "Edit File", kas_builtin_ids=True, mcp_tool_name="str_replace", tool_kind="edit"
        )
        assert r.action == TOOL_DENY

    def test_governance_tools_scope_sees_the_kiro_cli_name_for_a_kas_alias(self, monkeypatch):
        seen = TestCanonicalMcpIdentityGoverned._deny_canonical(monkeypatch, "fs_write")
        r = HookManager().on_tool_call(
            "Update the changelog",
            kas_builtin_ids=True,
            mcp_tool_name="str_replace",
            tool_kind="edit",
        )
        assert r.action == TOOL_DENY
        assert "fs_write" in seen

    def test_governance_is_asked_the_kas_id_and_its_alias_as_one_group(self, monkeypatch):
        """Governance profiles are written in kiro-cli vocabulary, and an ALLOW-mode
        profile requires every queried item to match its allow set. So the raw
        ``str_replace`` and its ``fs_write`` alias travel as ONE alias group
        (``gate_decision``'s ``alias_groups``), never as two separate items and
        never as the alias alone: a deny on either spelling binds, one permitted
        spelling admits. The deny tier still reads both spellings."""
        import kiro_crew.hooks as hooks_mod

        groups: list = []

        def gov(ctx, name, *a, **k):
            groups.extend(k.get("alias_groups", ()))
            assert "str_replace" not in k.get("extra_titles", ())
            assert "fs_write" not in k.get("extra_titles", ())
            return None

        monkeypatch.setattr(hooks_mod, "_governance_denial", gov)
        HookManager().on_tool_call(
            "Update the changelog",
            kas_builtin_ids=True,
            mcp_tool_name="str_replace",
            tool_kind="edit",
        )
        assert groups == [("str_replace", "fs_write")]
        # Title equal to the raw id: the group still carries both spellings.
        groups.clear()
        HookManager().on_tool_call(
            "str_replace", kas_builtin_ids=True, mcp_tool_name="str_replace", tool_kind="edit"
        )
        assert groups == [("str_replace", "fs_write")]

    def test_a_kas_delete_is_asked_on_its_own_name_with_fs_write_deny_only(self, monkeypatch):
        """``delete_file``'s alias may only deny: kiro-cli's ``fs_write`` cannot
        delete, so the id travels in ``extra_titles`` (it has to be permitted on
        its own name) and ``fs_write`` in ``deny_aliases``, never as an alias
        group whose permitted spelling would admit the deletion."""
        import kiro_crew.hooks as hooks_mod

        seen: dict = {}

        def gov(ctx, name, *a, **k):
            seen.update(k)
            return None

        monkeypatch.setattr(hooks_mod, "_governance_denial", gov)
        HookManager().on_tool_call(
            "Delete File", kas_builtin_ids=True, mcp_tool_name="delete_file", tool_kind="delete"
        )
        assert seen["alias_groups"] == ()
        assert seen["extra_titles"] == ("delete_file",)
        assert seen["deny_aliases"] == ("fs_write",)
        # Title equal to the raw id: the id is the title, so it is not repeated.
        seen.clear()
        HookManager().on_tool_call(
            "delete_file", kas_builtin_ids=True, mcp_tool_name="delete_file", tool_kind="delete"
        )
        assert seen["extra_titles"] == ()
        assert seen["deny_aliases"] == ("fs_write",)

    def test_an_allow_mode_fs_write_grant_does_not_admit_a_kas_delete(self, monkeypatch):
        """End to end through the real ``gate_decision``: an allow set of {title,
        fs_write} written for kiro-cli admits a KAS ``str_replace`` and REFUSES a
        KAS ``delete_file``; naming ``delete_file`` admits it; a deny under
        ``fs_write`` still refuses it."""
        import kiro_crew.platform.governance_profiles as profiles_mod
        from kiro_crew.platform.governance import MODE_ALLOW, MODE_DENY, Profile, ScopedRuleset

        def with_profile(control):
            monkeypatch.setattr(
                profiles_mod,
                "resolve_active_scope",
                lambda *a, **k: Profile(name="p", controls={"tools": control}),
            )

        with_profile(ScopedRuleset(MODE_ALLOW, ("Delete File", "fs_write")))
        r = HookManager().on_tool_call(
            "Delete File", kas_builtin_ids=True, mcp_tool_name="delete_file", tool_kind="delete"
        )
        assert r.action == TOOL_DENY
        assert "delete_file" in r.reason
        r = HookManager().on_tool_call(
            "Delete File", kas_builtin_ids=True, mcp_tool_name="str_replace", tool_kind="edit"
        )
        assert r.action != TOOL_DENY, r.reason

        with_profile(ScopedRuleset(MODE_ALLOW, ("Delete File", "delete_file")))
        r = HookManager().on_tool_call(
            "Delete File", kas_builtin_ids=True, mcp_tool_name="delete_file", tool_kind="delete"
        )
        assert r.action != TOOL_DENY, r.reason

        with_profile(ScopedRuleset(MODE_DENY, deny=("fs_write",)))
        r = HookManager().on_tool_call(
            "Delete File", kas_builtin_ids=True, mcp_tool_name="delete_file", tool_kind="delete"
        )
        assert r.action == TOOL_DENY

    def test_an_allow_mode_profile_written_in_kiro_cli_names_permits_the_kas_write(
        self, monkeypatch
    ):
        """End to end through the real ``gate_decision``: an allow set of {title,
        fs_write} admits a KAS ``str_replace``; a deny-mode profile denying the
        raw id still refuses it; and the deny TIER's raw-id rule still binds."""
        import kiro_crew.platform.governance_profiles as profiles_mod
        from kiro_crew.platform.governance import MODE_ALLOW, MODE_DENY, Profile, ScopedRuleset

        def with_profile(control):
            monkeypatch.setattr(
                profiles_mod,
                "resolve_active_scope",
                lambda *a, **k: Profile(name="p", controls={"tools": control}),
            )

        with_profile(ScopedRuleset(MODE_ALLOW, ("Update the changelog", "fs_write")))
        r = HookManager().on_tool_call(
            "Update the changelog",
            kas_builtin_ids=True,
            mcp_tool_name="str_replace",
            tool_kind="edit",
        )
        assert r.action != TOOL_DENY, r.reason

        with_profile(ScopedRuleset(MODE_DENY, deny=("str_replace",)))
        r = HookManager().on_tool_call(
            "Update the changelog",
            kas_builtin_ids=True,
            mcp_tool_name="str_replace",
            tool_kind="edit",
        )
        assert r.action == TOOL_DENY

        with_profile(ScopedRuleset(MODE_DENY, deny=("fs_write",)))
        r = HookManager().on_tool_call(
            "Update the changelog",
            kas_builtin_ids=True,
            mcp_tool_name="str_replace",
            tool_kind="edit",
        )
        assert r.action == TOOL_DENY

        # And the deny tier is unaffected: a raw-id deny still binds.
        r = HookManager(HooksConfig(auto_deny_tools=["str_replace"])).on_tool_call(
            "Update the changelog",
            kas_builtin_ids=True,
            mcp_tool_name="str_replace",
            tool_kind="edit",
        )
        assert r.action == TOOL_DENY

    def test_a_name_both_engines_share_gains_no_alias(self, monkeypatch):
        """``fs_write`` IS the policy spelling; nothing is added, nothing asked twice."""
        seen = TestCanonicalMcpIdentityGoverned._deny_canonical(monkeypatch, "never-matches")
        HookManager().on_tool_call(
            "fs_write", kas_builtin_ids=True, mcp_tool_name="fs_write", tool_kind="edit"
        )
        assert seen == ["fs_write"]

    def test_an_unrelated_tool_is_not_denied_by_a_write_rule(self):
        cfg = HooksConfig(auto_deny_tools=["fs_write"])
        r = HookManager(cfg).on_tool_call(
            "Look around", kas_builtin_ids=True, mcp_tool_name="read_file", tool_kind="read"
        )
        assert r.action != TOOL_DENY

    def test_an_mcp_served_tool_named_like_a_kas_builtin_gains_no_alias_at_the_deny_tier(self):
        """The reference MCP filesystem server calls its tools ``read_file`` and
        ``list_directory``. They are not kiro-cli's ``fs_read``; a built-in deny
        must not fence a server the operator never named. ``mcpServerName`` is
        stamped on every MCP-served call, so its presence is the discriminator."""
        cfg = HooksConfig(auto_deny_tools=["fs_read"])
        r = HookManager(cfg).on_tool_call(
            "Read the notes",
            mcp_server_name="filesystem",
            kas_builtin_ids=True,
            mcp_tool_name="read_file",
            tool_kind="read",
        )
        assert r.action != TOOL_DENY

    def test_an_mcp_served_tool_named_like_a_kas_builtin_gains_no_alias_at_governance(
        self, monkeypatch
    ):
        seen = TestCanonicalMcpIdentityGoverned._deny_canonical(monkeypatch, "fs_write")
        r = HookManager().on_tool_call(
            "Edit the notes",
            mcp_server_name="filesystem",
            kas_builtin_ids=True,
            mcp_tool_name="str_replace",
            tool_kind="edit",
        )
        assert r.action != TOOL_DENY
        assert "fs_write" not in seen

    def test_a_per_server_rule_on_the_mcp_tool_itself_still_binds(self):
        """Scoping the alias out of MCP calls removes nothing the operator wrote
        for that server: the canonical identity is still a deny target."""
        cfg = HooksConfig(auto_deny_tools=["@filesystem/read_file"])
        r = HookManager(cfg).on_tool_call(
            "Read the notes",
            mcp_server_name="filesystem",
            kas_builtin_ids=True,
            mcp_tool_name="read_file",
            tool_kind="read",
        )
        assert r.action == TOOL_DENY


class TestKasIdFoldIsKasOnly:
    """The kiro-cli <-> KAS name table speaks about KAS's built-in ids. A harness
    outside ``ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL`` that stamps the same
    bare id (``read_file``, ``run_command``, ``delete_file``) is judged under that
    id alone, exactly as on main: no fold into the deny or governance planes and
    no read-only proof under ``--approval reads``."""

    @pytest.mark.parametrize(
        ("bare_id", "rule", "kind"),
        [
            ("read_file", "fs_read", "read"),
            ("run_command", "execute_bash", "execute"),
            ("delete_file", "fs_write", "delete"),
        ],
    )
    def test_a_kiro_cli_rule_does_not_reach_a_bare_id_off_kas(self, bare_id, rule, kind):
        cfg = HooksConfig(auto_deny_tools=[rule])
        off_kas = HookManager(cfg).on_tool_call(
            "Do the thing", mcp_tool_name=bare_id, tool_kind=kind
        )
        assert off_kas.action != TOOL_DENY, bare_id
        # Control: the same frame on KAS folds and the rule binds.
        on_kas = HookManager(cfg).on_tool_call(
            "Do the thing", mcp_tool_name=bare_id, tool_kind=kind, kas_builtin_ids=True
        )
        assert on_kas.action == TOOL_DENY, bare_id

    @pytest.mark.parametrize("bare_id", ["read_file", "run_command", "delete_file"])
    def test_governance_gets_no_alias_group_off_kas(self, bare_id, monkeypatch):
        import kiro_crew.hooks as hooks_mod

        seen: dict = {}

        def gov(ctx, name, *a, **k):
            seen.update(k)
            return None

        monkeypatch.setattr(hooks_mod, "_governance_denial", gov)
        HookManager().on_tool_call("Do the thing", mcp_tool_name=bare_id, tool_kind="other")
        assert seen.get("alias_groups", ()) == ()
        assert seen.get("deny_aliases", ()) == ()
        assert seen.get("extra_titles", ()) == (bare_id,)

    @pytest.mark.parametrize("bare_id", ["read_file", "list_directory", "grep_search"])
    def test_a_bare_read_id_off_kas_is_no_read_only_proof(self, bare_id):
        from kiro_crew.hooks import _is_host_read_only_builtin

        assert _is_host_read_only_builtin(bare_id, "", mcp_identity_trusted=True) is False
        assert (
            _is_host_read_only_builtin(bare_id, "", mcp_identity_trusted=True, kas_builtin_ids=True)
            is True
        )

    def test_a_bare_read_id_off_kas_is_not_auto_approved_under_read_only(self):
        kwargs = dict(
            mcp_tool_name="read_file",
            mcp_identity_trusted=True,
            tool_kind="read",
            raw_params={"path": "/tmp/notes.md"},
            classifier_only=True,
        )
        off_kas = HookManager().on_tool_call("Read File", **kwargs)
        assert off_kas.action != TOOL_AUTO_APPROVE
        on_kas = HookManager().on_tool_call("Read File", kas_builtin_ids=True, **kwargs)
        assert on_kas.action == TOOL_AUTO_APPROVE

    def test_a_kiro_cli_read_alias_still_proves_read_only_on_its_own_harness(self):
        """kiro-cli's own ``read`` alias is unchanged by the KAS gating."""
        from kiro_crew.hooks import _is_host_read_only_builtin

        assert _is_host_read_only_builtin("read", "", mcp_identity_trusted=True) is True
        assert _is_host_read_only_builtin("fs_read", "", mcp_identity_trusted=True) is True


class TestServerLevelGovernanceBindsOnPartialIdentity:
    """A trusted SERVER identity with no trusted tool name is still a complete
    question for governance, whose grammar has a server level: ``@server`` covers
    every tool under it. Exercised through the REAL policy engine rather than a
    stubbed ``_governance_denial``, because what is at stake is precisely whether
    the raw target this gate emits is one the ``mcp`` matcher can bind a
    server-level rule to."""

    @staticmethod
    def _ceiling_denying(server_ref: str):
        """A real ceiling whose ``mcp`` scope denies *server_ref*."""
        from kiro_crew.platform.governance import (
            MODE_DENY,
            GovernanceCeiling,
            ScopedRuleset,
            parse_policy,
        )

        return GovernanceCeiling(
            version=1,
            boot=parse_policy({"version": 1, "boot": {"fail_closed": True}}).boot,
            controls={"mcp": ScopedRuleset(mode=MODE_DENY, deny=(server_ref,), matcher="mcp")},
        )

    @staticmethod
    def _install(monkeypatch, ceiling):
        """Run the gate against *ceiling* with the real security authority."""
        import kiro_crew.hooks as hooks_mod
        from kiro_crew.platform import current_context

        real = current_context()

        class _Ctx:
            security = real.security
            governance = ceiling

        monkeypatch.setattr(hooks_mod, "current_context", lambda: _Ctx())

    def test_server_level_deny_binds_without_a_trusted_tool_name(self, monkeypatch):
        # THE REGRESSION. kiro-cli supplied `_meta.kiro.mcpServerName` but no
        # `toolName` — an uncached permission event, or a backend that omits it.
        # The title is the model's own prose, so governing on the title alone
        # leaves a `deny @github` ceiling with nothing to bind to, and the call
        # walks past the ceiling to a human who can approve it.
        self._install(monkeypatch, self._ceiling_denying("@github"))
        mgr = HookManager()
        r = mgr.on_tool_call(
            "Read a public README",  # benign model-authored prose
            mcp_server_name="github",
            mcp_tool_name="",  # not proven
            tool_kind="other",
        )
        assert r.action == TOOL_DENY

    def test_server_level_deny_still_binds_on_a_full_identity(self, monkeypatch):
        # PRESERVATION: `@server` covers every tool under it, so the complete
        # identity is denied by the same rule.
        self._install(monkeypatch, self._ceiling_denying("@github"))
        mgr = HookManager()
        r = mgr.on_tool_call(
            "Read a public README",
            mcp_server_name="github",
            mcp_tool_name="get_file",
            tool_kind="other",
        )
        assert r.action == TOOL_DENY

    def test_tool_level_deny_does_not_widen_to_the_whole_server(self, monkeypatch):
        # The partial target must not be a blunter instrument than the grammar
        # allows: `@github/delete_repo` denies that tool, and a call that proves
        # only the server is NOT that tool, so it is not denied by this rule.
        # Without this, "govern the server when the tool is unknown" would
        # quietly become "deny the server whenever any of its tools is denied".
        self._install(monkeypatch, self._ceiling_denying("@github/delete_repo"))
        mgr = HookManager()
        r = mgr.on_tool_call(
            "Read a public README",
            mcp_server_name="github",
            mcp_tool_name="",
            tool_kind="other",
        )
        assert r.action != TOOL_DENY

    def test_an_ungoverned_server_is_not_denied(self, monkeypatch):
        # The control that keeps the test above honest: same partial identity,
        # a rule naming a DIFFERENT server, so the deny must not fire.
        self._install(monkeypatch, self._ceiling_denying("@gitlab"))
        mgr = HookManager()
        r = mgr.on_tool_call(
            "Read a public README",
            mcp_server_name="github",
            mcp_tool_name="",
            tool_kind="other",
        )
        assert r.action != TOOL_DENY

    def test_a_server_name_containing_the_separator_still_binds(self, monkeypatch):
        # A ``__``-bearing server name must reach governance intact. Encoded into
        # an ``mcp__<server>`` title it re-parses as server ``npm`` + tool
        # ``playwright_mcp`` and the ceiling denying the real server never binds --
        # a human would be asked to approve a server the policy forbids.
        self._install(monkeypatch, self._ceiling_denying("@npm__playwright_mcp"))
        mgr = HookManager()
        r = mgr.on_tool_call(
            "Open a page",
            mcp_server_name="npm__playwright_mcp",
            mcp_tool_name="",
            tool_kind="other",
        )
        assert r.action == TOOL_DENY

    def test_a_tool_name_containing_the_separator_still_binds(self, monkeypatch):
        # The sibling, and the one no title spelling can fix: the title form is
        # read by splitting on the LAST ``__``, so ``@github`` + ``repo__delete``
        # encodes to ``mcp__github__repo__delete`` and reads back as server
        # ``github__repo`` with tool ``delete``. A ``deny @github/repo__delete``
        # ceiling would then never bind and approval could run the denied tool.
        self._install(monkeypatch, self._ceiling_denying("@github/repo__delete"))
        mgr = HookManager()
        r = mgr.on_tool_call(
            "Read a public README",
            mcp_server_name="github",
            mcp_tool_name="repo__delete",
            tool_kind="other",
        )
        assert r.action == TOOL_DENY

    def test_both_segments_containing_the_separator_still_bind(self, monkeypatch):
        # Both halves ambiguous at once — the branch a per-segment patch on either
        # side alone would still leave open.
        self._install(monkeypatch, self._ceiling_denying("@npm__gh/repo__delete"))
        mgr = HookManager()
        r = mgr.on_tool_call(
            "Read a public README",
            mcp_server_name="npm__gh",
            mcp_tool_name="repo__delete",
            tool_kind="other",
        )
        assert r.action == TOOL_DENY

    def test_a_specific_tool_rule_does_not_deny_the_server_only_question(self, monkeypatch):
        # The other half of the server-level question: an unproven tool must not be
        # denied by a rule naming a specific one, or closing the missed denies
        # above would trade them for a false one.
        self._install(monkeypatch, self._ceiling_denying("@npm__playwright_mcp/wipe"))
        mgr = HookManager()
        r = mgr.on_tool_call(
            "Open a page",
            mcp_server_name="npm__playwright_mcp",
            mcp_tool_name="",
            tool_kind="other",
        )
        assert r.action != TOOL_DENY

    def test_partial_identity_never_reaches_the_raw_deny_regex_plane(self, monkeypatch):
        # The server-only target belongs to the governance grammar, where
        # `mcp__<server>` means `@server`. The deny plane matches raw text and
        # operator regexes, where it is simply a different string — so a rule
        # written against the canonical identity must NOT start matching calls
        # whose tool was never proven.
        cfg = HooksConfig(auto_deny_tools=["mcp__github__delete_repo"])
        mgr = HookManager(cfg)
        r = mgr.on_tool_call(
            "Read a public README",
            mcp_server_name="github",
            mcp_tool_name="",
            tool_kind="other",
        )
        assert r.action != TOOL_DENY
