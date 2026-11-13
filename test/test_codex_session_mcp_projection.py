"""Codex's spec projection as unit behaviour: the agent spec -> a ``session/new`` array.

The unit half of ``test_codex_session_mcp.py``, which measures against an installed
``codex-acp``.
"""

from __future__ import annotations

import asyncio
import re
import sys
from unittest.mock import patch

import codex_session_mcp_helpers
import pytest
from codex_session_mcp_helpers import (
    _CODEX_1_11_CAPS,
    _CORE,
    _by_name,
    _codex_session_array,
    _write_spec,
)

from kiro_crew.acp import runtime as acp_runtime
from kiro_crew.acp import session_mcp
from kiro_crew.acp.harness.codex import CodexHarness
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_MEMBER_DISPATCH,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
)
from kiro_crew.members import MEMBER_DISPATCH_SERVER
from kiro_crew.providers.mirrors import Concern, Disposition, mirror_for
from kiro_crew.providers.mirrors.codex import (
    CodexMirror,
    _identity_bound_crew_servers,
    codex_elements,
    codex_name,
    codex_projection,
    codex_withheld_servers,
    drop_unadvertised_transports,
)

# The shared fixtures, bound here by name so pytest registers them for this module.
agents_dir = codex_session_mcp_helpers.agents_dir


def _env(element: dict) -> dict[str, str]:
    return {p["name"]: p["value"] for p in element.get("env") or []}


def _element(name: str, **over) -> dict:
    """A translated stdio element for *name*, as ``acp_server_element`` emits one."""
    element = {
        "name": name,
        "type": "stdio",
        "command": "/opt/kirocrew",
        "args": ["mcp-core"],
        "env": [],
    }
    element.update(over)
    return element


# ── the transport filter ────────────────────────────────────────────────────


class TestTransportFilter:
    """The filter reads THIS session's ``initialize`` ``mcpCapabilities``, not a constant.

    codex-acp ACCEPTS an element whose transport it declares unsupported and never
    wires that server, so this filter is the only guard and a mistake here is silent.
    """

    def test_an_unadvertised_transport_is_dropped_before_it_is_sent(self):
        """An ``sse`` element leaves the array; the advertised one beside it stays."""
        kept = drop_unadvertised_transports(
            [
                {"name": "remote", "type": "sse", "url": "https://x/sse", "headers": []},
                {"name": "local", "type": "stdio", "command": "/bin/x", "args": [], "env": []},
            ],
            _CODEX_1_11_CAPS,
        )
        assert [e["name"] for e in kept] == ["local"]

    def test_a_MEANINGLESS_transport_type_is_dropped_TOO(self):
        """Only POSITIVELY advertised transports are kept, so a junk ``type`` is dropped.

        Up to codex-acp 2.2.2 such an element was accepted and never wired; since 2.3.0
        it fails the whole ``session/new`` with ``-32602``. A denylist covers neither.
        """
        kept = drop_unadvertised_transports(
            [
                {"name": "junk", "type": "nonsense-type", "url": "https://x/j"},
                {"name": "local", "type": "stdio", "command": "/bin/x", "args": [], "env": []},
            ],
            _CODEX_1_11_CAPS,
        )
        assert [e["name"] for e in kept] == ["local"]

    def test_an_ADVERTISED_remote_transport_is_KEPT(self):
        """Measured: codex-acp 1.11.0 advertises ``http: true``, so "stdio only" is wrong.

        Dropping a server the adapter would mount removes tools with no error.
        """
        kept = drop_unadvertised_transports(
            [{"name": "r", "type": "http", "url": "https://x/mcp", "headers": []}],
            _CODEX_1_11_CAPS,
        )
        assert [e["name"] for e in kept] == ["r"]

    def test_a_transport_the_agent_LATER_advertises_is_kept_with_no_code_change(self):
        """A release that starts advertising ``sse`` is served by the same code."""
        kept = drop_unadvertised_transports(
            [{"name": "remote", "type": "sse", "url": "https://x/sse", "headers": []}],
            {"acp": False, "http": True, "sse": True},
        )
        assert [e["name"] for e in kept] == ["remote"]

    def test_an_unknown_advertisement_keeps_stdio_only(self):
        """Fail-safe: ACP requires every agent to support stdio, so stdio cannot be refused."""
        elements = [
            {"name": "local", "type": "stdio", "command": "/bin/x", "args": [], "env": []},
            {"name": "r", "type": "http", "url": "https://x/mcp", "headers": []},
        ]
        assert [e["name"] for e in drop_unadvertised_transports(elements, {})] == ["local"]
        assert [e["name"] for e in drop_unadvertised_transports(elements, None)] == ["local"]

    def test_an_element_with_no_type_is_stdio(self):
        """ACP v1 makes the stdio variant the UNTAGGED fallback, so absent = stdio."""
        kept = drop_unadvertised_transports(
            [{"name": "local", "command": "/bin/x", "args": [], "env": []}], {}
        )
        assert [e["name"] for e in kept] == ["local"]

    def test_a_stdio_element_keeps_its_type_tag_unchanged(self):
        """A real adapter accepts ``type: "stdio"``, so the element is not rewritten.

        ACP v1 spells ``McpServer`` as ``serde(tag = "type")`` with stdio as the UNTAGGED
        fallback, so the tag matches no named variant and falls through to it.
        """
        el = codex_elements(
            [{"name": "local", "type": "stdio", "command": "/bin/x", "args": [], "env": []}]
        )[0]
        assert el["type"] == "stdio"
        assert drop_unadvertised_transports([el], _CODEX_1_11_CAPS) == [el]


# ── identity carriage and name folding ───────────────────────────────


class TestIdentityEnvCarriage:
    """Crew's identity env goes only to the servers the translation REPLACES.

    ``codex-rs``'s stdio launcher runs ``Command::env_clear()`` and re-adds only
    ``DEFAULT_ENV_VARS`` plus the entry's ``env``, so without ``KIROCREW_SESSION_KEY``
    the control plane cannot name its session.
    """

    def test_the_control_plane_carries_the_session_key(self):
        el = codex_elements([_element("kirocrew-core")], session_key="chat-7-123")[0]
        assert _env(el)["KIROCREW_SESSION_KEY"] == "chat-7-123"

    def test_the_control_plane_carries_the_bound_port(self):
        """Without the port the child falls back to the run marker and the default port.

        That check needs ``lsof``, which sees no listener inside a sandbox's user namespace.
        """
        el = codex_elements([_element("kirocrew-cron")], session_key="chat-7-123")[0]
        assert _env(el)["KIROCREW_BOUND_PORT"].isdigit()

    def test_the_channel_id_rides_along_when_there_is_one(self):
        """``mcp_cron`` reads ``KIROCREW_CHANNEL_ID`` to place a cron's output."""
        el = codex_elements(
            [_element("kirocrew-cron")], session_key="chat-7-123", channel_id="C123"
        )[0]
        assert _env(el)["KIROCREW_CHANNEL_ID"] == "C123"

    def test_a_THIRD_PARTY_server_gets_no_crew_identity(self):
        """``KIROCREW_SESSION_KEY`` is a credential, so a spec-described server never gets it.

        Handing it over would let any server a spec names drive the session.
        """
        el = codex_elements([_element("somebody-else")], session_key="chat-7-123")[0]
        assert _env(el) == {}

    def test_an_OPT_IN_crew_server_gets_no_identity_EITHER(self):
        """Opt-in crew servers carry no identity: the spec chose their command and env.

        ``codex_withheld_servers`` drops them upstream; this pins that one which still
        arrives gets no key.
        """
        el = codex_elements(
            [_element("kirocrew-work", args=["mcp-work"])], session_key="chat-7-123"
        )[0]
        assert _env(el) == {}

    def test_a_stale_spec_value_is_replaced_not_duplicated(self):
        """Resolved-live beats spec-declared, and the array holds one pair per name."""
        el = codex_elements(
            [_element("kirocrew-core", env=[{"name": "KIROCREW_SESSION_KEY", "value": "stale"}])],
            session_key="fresh",
        )[0]
        names = [pair["name"] for pair in el["env"]]
        assert names.count("KIROCREW_SESSION_KEY") == 1
        assert _env(el)["KIROCREW_SESSION_KEY"] == "fresh"

    def test_no_session_key_means_no_env_work_at_all(self):
        """A keyless client (a worker pool, a probe) must not gain a bogus identity."""
        assert _env(codex_elements([_element("kirocrew-core")])[0]) == {}


class TestNameFolding:
    """The fold must be codex's fold, character for character.

    codex-acp runs ``name.replace(|c: char| c.is_whitespace(), "_")``. A fold that
    collapses runs or strips ends can map an impostor onto the control plane.
    """

    def test_the_fold_matches_codex_character_for_character(self):
        for raw in ("my server", "a  b", " lead", "trail ", "x\ty", "plain", "kirocrew-core"):
            assert codex_name(raw) == re.sub(r"\s", "_", raw), raw

    def test_no_spec_name_can_fold_onto_the_control_plane(self):
        """The only string that folds onto ``kirocrew-core`` is itself.

        Folding only replaces whitespace with ``_`` and the name contains no ``_``.
        """
        for impostor in ("kirocrew-core ", " kirocrew-core", "kirocrew core", "kirocrew_core"):
            assert codex_name(impostor) != "kirocrew-core", impostor
        assert codex_name("kirocrew-core") == "kirocrew-core"

    def test_a_whitespace_variant_reaches_codex_under_its_own_distinct_name(self):
        """The impostor mounts under its own folded name, with no Crew identity."""
        kept = codex_elements(
            [
                _element("kirocrew-core"),
                _element("kirocrew-core ", command="/tmp/not-crews-binary"),
            ],
            session_key="chat-7-123",
        )
        by_name = {e["name"]: e for e in kept}
        assert set(by_name) == {"kirocrew-core", "kirocrew-core_"}
        assert _env(by_name["kirocrew-core"])["KIROCREW_SESSION_KEY"] == "chat-7-123"
        assert _env(by_name["kirocrew-core_"]) == {}

    def test_a_genuine_fold_collision_keeps_the_first_writer(self):
        """Two spec names CAN fold together; codex would take the last, Crew keeps the first."""
        kept = codex_elements(
            [_element("my server", command="/bin/a"), _element("my_server", command="/bin/b")]
        )
        assert [e["name"] for e in kept] == ["my_server"]
        assert kept[0]["command"] == "/bin/a"

    def test_the_input_elements_are_not_mutated(self):
        """The caller's list is the translator's output, cached per spawn."""
        src = [_element("a b")]
        codex_elements(src, session_key="sk")
        assert src[0]["name"] == "a b"


# ── the mirror's declared rulings ───────────────────────────────────────────


class TestCodexRulings:
    def test_the_mcp_ruling_names_both_codex_specific_rules(self):
        """The folder is the inventory, so the rules have to be readable there.

        A ruling that said only "delivered" would let the next backend copy the
        delivery and drop the two conditions that make it work at all.
        """
        ruling = mirror_for(ACP_BACKEND_CODEX).rulings()[Concern.MCP_SERVERS]
        assert ruling.disposition is Disposition.DELIVERED
        assert "drop_unadvertised_transports" in ruling.reason
        assert "KIROCREW_SESSION_KEY" in ruling.reason

    def test_disabled_tools_is_honoured_by_withholding_the_server(self):
        """With no deny channel, the only faithful option is to withhold the server.

        codex-acp hardcodes ``disabled_tools=None``, and claude's ``permissions.deny``
        needs a settings file codex does not have.
        """
        ruling = mirror_for(ACP_BACKEND_CODEX).rulings()[Concern.DENIED_TOOLS]
        assert ruling.disposition is Disposition.TRANSLATED
        assert "session_mcp_restricted_servers" in ruling.reason

    def test_auto_approve_is_withheld_because_of_the_gate(self):
        ruling = mirror_for(ACP_BACKEND_CODEX).rulings()[Concern.AUTO_APPROVE]
        assert ruling.disposition is Disposition.WITHHELD
        assert "gate" in ruling.reason

    def test_hooks_is_the_second_open_gap_and_it_is_addressed(self):
        ruling = mirror_for(ACP_BACKEND_CODEX).rulings()[Concern.HOOKS]
        assert ruling.disposition is Disposition.NO_CHANNEL
        assert ruling.channel.strip()

    def test_the_wire_face_does_not_fail_closed_on_claudes_precondition(self, tmp_path, agents_dir):
        """Copying claude's gate here would have withheld every codex tool.

        ``permission_surface_owned`` describes claude's ``settings.local.json``, a
        file no codex session has. Codex's routing is ``SESSION_CONFIG`` -- the one
        mechanism in ``ENFORCED_ROUTINGS`` -- so a session that cannot arm
        ``mode=read-only`` is refused rather than run, and there is no file to own.
        """
        _write_spec(agents_dir, servers={}, tools=["@kirocrew-core"])
        params = CodexMirror().session_params("kirocrew", permission_surface_owned=False)
        assert [e["name"] for e in params["mcpServers"]] == ["kirocrew-core"]


# ── the session array seam ──────────────────────────────────────────────────


class TestTheSessionArraySeam:
    """What a codex ``session/new`` is handed, and who decides each part of it.

    The array (spec translation and pooled broker stubs) is the MIRROR's; the
    transport narrowing after it is the HARNESS's, reading this session's handshake.
    """

    def test_codex_is_in_both_the_array_set_and_member_dispatch(self):
        """Without the array set the session gets ``[]`` however good the mirror is.

        Member dispatch is appended by ``AcpRuntime.create_session`` after the mirror runs,
        since ``codex_withheld_servers`` keeps the spec's spelling of it out.
        """
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_SESSION_MCP_ARRAY
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_MEMBER_DISPATCH

    def test_the_spec_can_never_supply_the_dashboard_server_itself(self, agents_dir):
        """A spec naming ``@kirocrew-dashboard`` still gets it withheld.

        A spec-described element carries no session identity and would answer
        ``identity_unattested`` to every verb; only the runtime's entry has the key.
        """
        _write_spec(
            agents_dir,
            servers={MEMBER_DISPATCH_SERVER: {"command": "/opt/kirocrew"}},
            tools=[f"@{MEMBER_DISPATCH_SERVER}", "@kirocrew-core"],
        )
        projection = codex_projection("kirocrew")
        names = [e["name"] for e in projection.params["mcpServers"]]
        assert MEMBER_DISPATCH_SERVER not in names
        assert MEMBER_DISPATCH_SERVER in codex_withheld_servers(frozenset())

    def test_the_session_array_carries_the_spec_and_the_control_plane(self, agents_dir):
        """The one assertion the whole mirror exists to make true.

        An empty array on a selectable backend is a session with no Crew tools and
        no error, so this is the seam's contract rather than a detail of it.
        """
        _write_spec(
            agents_dir,
            servers={"foo": {"command": "/bin/foo"}},
            # The control plane is NOT exempt from the allowlist -- kiro-cli drops
            # kirocrew-core from a spec whose `tools` stops naming it, and this
            # backend must not re-grant what kiro-cli would drop -- so a spec that
            # wants both has to name both.
            tools=["@foo", "@kirocrew-core"],
        )
        names = set(_by_name(_codex_session_array(caps=_CODEX_1_11_CAPS)[1]))
        assert "foo" in names
        assert "kirocrew-core" in names

    def test_an_sse_spec_entry_never_reaches_a_codex_session(self, agents_dir):
        """End to end through both owners, against the advertisement a session holds."""
        _write_spec(
            agents_dir,
            servers={
                "remote": {"url": "https://x/sse", "type": "sse"},
                "local": {"command": "/bin/foo"},
            },
            tools=["@remote", "@local"],
        )
        names = set(_by_name(_codex_session_array(caps=_CODEX_1_11_CAPS)[1]))
        assert "remote" not in names
        assert "local" in names

    def test_the_spec_parse_does_not_narrow_so_this_filter_is_the_only_guard(self, agents_dir):
        """The spec parse keeps the ``sse`` server; only the narrowing removes it.

        An unsupported transport yields a NORMAL session with the server never wired,
        so losing the narrowing call fails silently.
        """
        _write_spec(
            agents_dir,
            servers={
                "remote": {"url": "https://x/sse", "type": "sse"},
                "local": {"command": "/bin/foo"},
            },
            tools=["@remote", "@local"],
        )
        unnarrowed = codex_projection("kirocrew").params["mcpServers"]
        assert "remote" in _by_name(unnarrowed)
        kept = _by_name(drop_unadvertised_transports(unnarrowed, _CODEX_1_11_CAPS))
        assert "remote" not in kept
        assert "local" in kept

    def test_the_narrowing_reads_the_advertisement_rather_than_a_constant(self, agents_dir):
        """The same spec, two advertisements, two answers."""
        _write_spec(
            agents_dir,
            servers={"remote": {"url": "https://x/sse", "type": "sse"}},
            tools=["@remote"],
        )
        assert "remote" not in _by_name(_codex_session_array(caps=_CODEX_1_11_CAPS)[1])
        advertised = {"acp": False, "http": True, "sse": True}
        assert "remote" in _by_name(_codex_session_array(caps=advertised)[1])

    def test_a_codex_session_needs_no_claude_settings_file(self, agents_dir):
        """Codex's array is not conditional on claude's ``settings.local.json``.

        ``False`` is what a real session is built with; claude's answer to it (an empty
        array) is asserted beside codex's.
        """
        _write_spec(agents_dir, servers={}, tools=["@kirocrew-core"])
        codex = CodexMirror().session_projection("kirocrew", permission_surface_owned=False)
        assert "kirocrew-core" in _by_name(codex.params["mcpServers"])
        claude = mirror_for(ACP_BACKEND_CLAUDE)
        assert claude is not None
        withheld = claude.session_projection("kirocrew", permission_surface_owned=False)
        assert withheld.params["mcpServers"] == []

    def test_a_server_narrowed_per_tool_is_withheld_end_to_end(self, agents_dir):
        """A ``disabledTools`` server is omitted; its un-narrowed sibling is kept.

        ``acp_server_element`` strips ``disabledTools``, so the mirror reads it from the
        spec (``session_mcp_restricted_servers``).
        """
        _write_spec(
            agents_dir,
            servers={
                "narrowed": {"command": "/bin/foo", "disabledTools": ["dangerous_tool"]},
                "open": {"command": "/bin/bar"},
            },
            tools=["@narrowed", "@open"],
        )
        names = set(_by_name(_codex_session_array(caps=_CODEX_1_11_CAPS)[1]))
        assert "narrowed" not in names
        assert "open" in names

    def test_an_empty_or_malformed_disabled_tools_withholds_nothing(self, agents_dir):
        """The dashboard writes an empty list when the last tool is re-enabled.

        Reading that as a restriction would unmount a server the user just turned
        back on — the availability half of the same mistake. A non-list value is a
        hand-edit rather than a restriction anyone declared, so it is not one either.
        """
        _write_spec(
            agents_dir,
            servers={
                "empty": {"command": "/bin/a", "disabledTools": []},
                "bogus": {"command": "/bin/b", "disabledTools": "nope"},
            },
            tools=["@empty", "@bogus"],
        )
        assert session_mcp.session_mcp_projection("kirocrew").restricted == frozenset()
        names = set(_by_name(_codex_session_array(caps=_CODEX_1_11_CAPS)[1]))
        assert {"empty", "bogus"} <= names

    def test_an_identity_bound_crew_server_is_not_mounted_at_all(self, agents_dir):
        """A ``kirocrew-work`` with no session identity answers ``not_bound``, so it is withheld.

        The control plane is re-derived, so it keeps its identity and stays.
        """
        _write_spec(
            agents_dir,
            servers={"kirocrew-work": {"command": "/opt/kirocrew", "args": ["mcp-work"]}},
            tools=["@kirocrew-work", "@kirocrew-core"],
        )
        names = set(_by_name(_codex_session_array(caps=_CODEX_1_11_CAPS)[1]))
        assert "kirocrew-work" not in names
        assert "kirocrew-core" in names

    def test_the_control_plane_is_never_withheld_by_its_own_disabled_tools(self, agents_dir):
        """The control plane stays mounted; its ``disabledTools`` is enforced at approval.

        Dropping it would leave the session unable to report back. See
        ``TestSpecDisabledToolRefusal``.
        """
        _write_spec(
            agents_dir,
            servers={
                "kirocrew-core": {
                    "command": "/opt/kirocrew",
                    "args": ["mcp-core"],
                    "disabledTools": ["spawn_run"],
                }
            },
            tools=["@kirocrew-core"],
        )
        assert "kirocrew-core" not in session_mcp.session_mcp_projection("kirocrew").restricted
        assert "kirocrew-core" in _by_name(_codex_session_array(caps=_CODEX_1_11_CAPS)[1])

    def test_the_pooled_resolution_cannot_run_beside_the_projection(self):
        """The raw ``pooled_session_servers`` must not run beside the projection.

        A stub is the UNRESTRICTED server under the SAME name, so appending it would
        un-withhold that name. Read from the AST: one ``if``/``else`` where a behavioural
        test sees only the branch it took, and a docstring mention is not a call.
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(AcpRuntime.create_session)))
        branches = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If) and "mirrored is not None" in ast.unparse(node.test)
        ]
        assert len(branches) == 1, "the mirrored-array branch has moved or been duplicated"
        taken = "".join(ast.unparse(stmt) for stmt in branches[0].body)
        untaken = "".join(ast.unparse(stmt) for stmt in branches[0].orelse)
        assert "pooled_session_servers" not in taken, (
            "the raw pooled resolution runs on the branch that ALREADY has the "
            "projection's array, so an unprojected broker stub reaches session/new "
            "beside it and un-withholds the name the projection dropped"
        )
        assert "pooled_session_servers" in untaken, (
            "the pooled resolution has left the else branch, so a host with no "
            "mirror may reach session/new with no servers at all"
        )

    def test_the_mirror_places_the_pooled_stubs_itself(self, agents_dir):
        """Both halves of the array are the MIRROR's, so one withhold rule covers both.

        A stub appended after the mirror withheld its name would un-withhold it, as the
        UNRESTRICTED server.
        """
        _write_spec(
            agents_dir,
            servers={"narrowed": {"command": "/bin/foo", "disabledTools": ["x"]}},
            tools=["@narrowed", "@unrelated"],
        )
        stubs = [
            {"name": "narrowed", "command": "/stub", "args": [], "env": [], "type": "stdio"},
            {"name": "unrelated", "command": "/stub", "args": [], "env": [], "type": "stdio"},
        ]
        projection = codex_projection(
            "kirocrew", stub_server_names=("narrowed", "unrelated"), stub_elements=stubs
        )
        names = [e["name"] for e in projection.params["mcpServers"]]
        assert "narrowed" not in names
        assert "unrelated" in names
        # The wire face IS the projection's params, pinned so the two cannot drift.
        assert (
            CodexMirror().session_params(
                "kirocrew", stub_server_names=("narrowed", "unrelated"), stub_elements=stubs
            )
            == projection.params
        )

    def test_a_pooled_stub_is_held_to_the_same_tools_allowlist(self, agents_dir):
        """A stub the spec's ``tools`` never references does not mount.

        The overlay also reads the GLOBAL settings file, so it can carry a stub for a
        server this agent never referenced. ``*`` grants all; no ``tools`` list grants none.
        """
        stubs = [
            {"name": "granted", "command": "/stub", "args": [], "env": [], "type": "stdio"},
            {"name": "unreferenced", "command": "/stub", "args": [], "env": [], "type": "stdio"},
        ]
        _write_spec(agents_dir, servers={"granted": {"command": "/bin/g"}}, tools=["@granted"])
        names = [
            e["name"]
            for e in codex_projection(
                "kirocrew", stub_server_names=("granted", "unreferenced"), stub_elements=stubs
            ).params["mcpServers"]
        ]
        assert "granted" in names
        assert "unreferenced" not in names

        _write_spec(agents_dir, servers={"granted": {"command": "/bin/g"}}, tools=["*"])
        names = [
            e["name"]
            for e in codex_projection(
                "kirocrew", stub_server_names=("granted", "unreferenced"), stub_elements=stubs
            ).params["mcpServers"]
        ]
        assert {"granted", "unreferenced"} <= set(names)

        _write_spec(agents_dir, servers={"granted": {"command": "/bin/g"}}, tools=None)
        names = [
            e["name"]
            for e in codex_projection(
                "kirocrew", stub_server_names=("granted", "unreferenced"), stub_elements=stubs
            ).params["mcpServers"]
        ]
        assert "granted" not in names and "unreferenced" not in names

    def test_the_client_no_longer_filters_the_array_itself(self):
        """The client holds the overlay and the mirror holds the rule.

        The seam is the base contract's ``session_projection``, not a codex import, so the
        next mirror author has no codex-specific branch to copy."""
        import inspect

        from kiro_crew.acp import client as client_mod

        source = "".join(
            inspect.getsource(sys.modules[name])
            for name in (client_mod.__name__, *client_mod._CLIENT_OWNERS)
        )
        assert "codex_withheld_servers" not in source
        assert "codex_projection" not in inspect.getsource(
            client_mod.AcpClient._resolve_session_mcp_servers
        )
        assert "mirror.session_projection(" in source
        assert "stub_elements=self._pooled_broker_stubs()" in source

    def test_the_base_projection_is_the_wire_face_with_nothing_off_wire(self, agents_dir):
        """Every mirror answers the client with ONE shape.

        A mirror with no client obligation answers with its wire params and an
        empty deny set -- claude here -- so the client needs no per-backend branch
        to read the seam, and a codex-only shape does not leak into the client.
        """
        from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE
        from kiro_crew.providers.mirrors import SessionProjection

        _write_spec(
            agents_dir,
            servers={"kirocrew-core": {"command": "/x", "disabledTools": ["spawn_run"]}},
            tools=["@kirocrew-core"],
        )
        claude = mirror_for(ACP_BACKEND_CLAUDE)
        assert claude is not None
        projection = claude.session_projection(
            "kirocrew", permission_surface_owned=True, stub_elements=[{"name": "ignored"}]
        )
        assert isinstance(projection, SessionProjection)
        assert projection.denied_tools == frozenset()
        assert projection.params == claude.session_params("kirocrew", permission_surface_owned=True)
        # Codex's answer is the same shape, carrying its obligation.
        assert ("kirocrew-core", "spawn_run") in CodexMirror().session_projection(
            "kirocrew"
        ).denied_tools

    def test_the_withheld_set_takes_the_restriction_half_from_its_caller(self):
        """No self-resolving default: a second parse is the window the projection
        closes, so the only caller supplies the half it already read."""
        import inspect

        assert list(inspect.signature(codex_withheld_servers).parameters) == ["restricted"]
        with pytest.raises(TypeError):
            codex_withheld_servers()  # type: ignore[call-arg]

    def test_the_withheld_set_is_one_owner_for_both_halves(self, agents_dir):
        """Two consumers, one rule: the projection and the pooled append."""
        _write_spec(
            agents_dir,
            servers={"narrowed": {"command": "/bin/foo", "disabledTools": ["x"]}},
            tools=["@narrowed"],
        )
        withheld = codex_withheld_servers(session_mcp.session_mcp_projection("kirocrew").restricted)
        assert "narrowed" in withheld
        assert _identity_bound_crew_servers() <= withheld
        assert not _identity_bound_crew_servers() & set(session_mcp.CONTROL_PLANE_SERVERS)

    def test_the_identity_bound_set_is_derived_from_the_managed_set(self):
        """DERIVED from the managed set minus the control plane, never enumerated.

        A hand-copied list would miss a newly managed server, which would mount and
        answer ``not_bound``. The literal check reads the AST so docstring mentions pass.
        """
        import ast
        import inspect

        from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS
        from kiro_crew.providers.mirrors import codex as codex_mod

        derived = _identity_bound_crew_servers()
        assert derived == frozenset(KIROCREW_BIN_MCP_SERVERS) - frozenset(
            session_mcp.CONTROL_PLANE_SERVERS
        )
        # Non-empty on both sides, or the equality above passes vacuously.
        assert derived
        assert frozenset(session_mcp.CONTROL_PLANE_SERVERS) <= frozenset(KIROCREW_BIN_MCP_SERVERS)

        tree = ast.parse(inspect.getsource(codex_mod))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                first = node.body[0] if node.body else None
                if ast.get_docstring(node, clean=False) is not None and isinstance(first, ast.Expr):
                    docstrings.add(id(first.value))
        spelled = sorted(
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and n.value in derived
            and id(n) not in docstrings
        )
        assert not spelled, (
            f"{spelled} are spelled literally in the codex mirror's CODE. The withhold "
            "set must be derived from mcp_cleanup's managed set, which a ratchet test "
            "already pins to agent._MANAGED_MCP_SERVERS"
        )

    def test_the_projection_reads_the_spec_once(self, agents_dir, monkeypatch):
        """One parse for the translation AND the restriction set.

        Two reads of a USER-WRITABLE file open a window where a ``disabledTools`` added
        between them lets the narrowed server mount UN-narrowed. Counted at the parse seam.
        """
        _write_spec(
            agents_dir,
            servers={"narrowed": {"command": "/bin/foo", "disabledTools": ["x"]}},
            tools=["@narrowed"],
        )
        real = session_mcp._agent_spec_and_snapshot_for
        calls: list[object] = []

        def counting(agent, work_dir=None, **kwargs):
            calls.append(agent)
            return real(agent, work_dir, **kwargs)

        monkeypatch.setattr(session_mcp, "_agent_spec_and_snapshot_for", counting)
        params = CodexMirror().session_params("kirocrew", session_key="k", channel_id="c")

        assert len(calls) == 1, f"the codex projection parsed the agent spec {len(calls)} times"
        assert "narrowed" not in _by_name(params["mcpServers"])

    def test_a_restriction_arriving_between_two_reads_cannot_be_lost(self, agents_dir, monkeypatch):
        """The window itself, driven: a SECOND read would see the narrowed spec.

        With one parse, the withhold set and the array always come from one revision.
        """
        _write_spec(agents_dir, servers={"narrowed": {"command": "/bin/foo"}}, tools=["@narrowed"])
        real = session_mcp._agent_spec_and_snapshot_for
        seen = {"n": 0}

        def drifting(agent, work_dir=None, **kwargs):
            spec, snapshot = real(agent, work_dir, **kwargs)
            seen["n"] += 1
            if seen["n"] >= 2 and isinstance(spec, dict):
                spec["mcpServers"]["narrowed"]["disabledTools"] = ["x"]
            return spec, snapshot

        monkeypatch.setattr(session_mcp, "_agent_spec_and_snapshot_for", drifting)
        params = CodexMirror().session_params("kirocrew", session_key="k", channel_id="c")

        assert seen["n"] == 1
        # Both halves saw the un-narrowed revision, so the server mounts and carries
        # no restriction it was never given. The forbidden outcome is the other one:
        # a mount whose own spec narrowed it.
        assert "narrowed" in _by_name(params["mcpServers"])

    def test_the_session_new_site_stays_a_pure_in_memory_read(self):
        """The shared ``session/new`` site must not put a disk read on the loop.

        Every ``session_projection`` / ``_ref_spec_snapshot`` call must sit inside the ONE
        function handed to ``asyncio.to_thread``; one process hosts every session, so a
        read on the loop stalls all of them. Read from the AST: there is no unit seam.
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(AcpRuntime._mirrored_session_mcp)))
        method = tree.body[0]
        assert isinstance(method, ast.AsyncFunctionDef)

        def _is_call_to(node: ast.AST, name: str) -> bool:
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, (ast.Attribute, ast.Name))
                and (node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id)
                == name
            )

        nested = {n.name: n for n in ast.walk(method) if isinstance(n, ast.FunctionDef)}
        hops = [n for n in ast.walk(method) if _is_call_to(n, "to_thread")]
        handed = [ast.unparse(h.args[0]) for h in hops]
        assert "pooled_session_servers" in handed, "the stub hop has left the mirrored path"
        # The one hop handed a nested def is the projection hop; the others hand a
        # module function by reference, and a second nested hop would be the extra
        # scheduling point this pin exists to refuse.
        hop_fns = [name for name in handed if name in nested]
        assert len(hop_fns) == 1, f"expected one nested-def hop on the mirrored path, got {hop_fns}"
        inside = {id(n) for n in ast.walk(nested[hop_fns[0]])}
        for blocking in ("session_projection", "_ref_spec_snapshot"):
            calls = [n for n in ast.walk(method) if _is_call_to(n, blocking)]
            assert calls, f"{blocking} is no longer read on the mirrored path"
            for call in calls:
                assert id(call) in inside, (
                    f"{blocking} is called outside the off-loop hop; it parses the agent "
                    "spec and reads the gateway overlay, so it belongs in that thread"
                )

        narrowing = inspect.getsource(CodexHarness.session_mcp_servers)
        for blocking in ("async def", "await ", "to_thread", "open(", "read_text"):
            assert blocking not in narrowing, (
                f"{blocking!r} appears in the session/new narrowing, which runs on the "
                "loop for every session this process hosts"
            )


# ── the runtime path ────────────────────────────────────────────────────────


def _codex_runtime(work_dir) -> AcpRuntime:
    """An initialized codex ``AcpRuntime`` with no child process.

    Nothing here spawns: the two methods under test resolve an array and answer a
    permission frame, and neither reads the pipe.
    """
    rt = AcpRuntime(work_dir=str(work_dir), acp_backend=ACP_BACKEND_CODEX)
    rt._initialized = True
    # Truthy so the pooled resolution is reached at all; the stub source is patched,
    # so no overlay is written and no gatewayd socket is opened.
    rt._mcp_gateway_overlay = str(work_dir)
    return rt


def _stub(name: str) -> dict:
    """A broker stub as ``pooled_session_servers`` shapes one: the SAME name as the
    agent-spec entry it rewrites, which is what makes an unnarrowed append dangerous."""
    return {"name": name, "type": "stdio", "command": "/stub", "args": [], "env": []}


class TestTheRuntimePathAppliesTheProjection:
    """``AcpRuntime`` builds a mirrored host's array through the mirror, not around it.

    The unprojected state is unreachable because the projection runs here too, so the
    session keeps its pooled servers instead of being refused.
    """

    @staticmethod
    def _projected(rt, stubs: list[dict], *, referenced: frozenset[str]):
        with (
            patch.object(acp_runtime, "pooled_session_servers", lambda *a, **k: list(stubs)),
            patch.object(acp_runtime, "injection_server_names", lambda *a, **k: referenced),
        ):
            return asyncio.run(
                rt._mirrored_session_mcp(
                    "kirocrew", work_dir=rt._work_dir, session_key="s-owner", channel_id="c-1"
                )
            )

    def test_a_pooled_stub_the_spec_never_references_does_not_reach_session_new(
        self, tmp_path, agents_dir
    ):
        """The allowlist that filters the translated half also filters stubs.

        The overlay also reads the GLOBAL settings file, and codex approves its own
        tools, so an unfiltered stub is a live tool surface Crew never granted.
        """
        _write_spec(
            agents_dir,
            servers={"kirocrew-core": dict(_CORE)},
            tools=["@kirocrew-core"],
        )
        rt = _codex_runtime(tmp_path)
        rt._agent_capabilities = {"mcpCapabilities": dict(_CODEX_1_11_CAPS)}
        out = self._projected(
            rt,
            [_stub("kirocrew-core"), _stub("never-referenced")],
            referenced=frozenset({"kirocrew-core"}),
        )
        assert out is not None
        names = set(_by_name(out.servers))
        assert "never-referenced" not in names
        # And the referenced one IS mounted: the rule withholds an ungranted server,
        # it does not un-pool the session.
        assert "kirocrew-core" in names

    def test_a_stub_wrapping_a_withheld_server_cannot_re_add_it(self, tmp_path, agents_dir):
        """One owner for both halves. A stub carries the name of the entry it rewrites,
        so an append after the projection withheld that name re-adds the server as the
        UNRESTRICTED one of the two."""
        _write_spec(
            agents_dir,
            servers={"narrowed": {"command": "/bin/foo", "disabledTools": ["dangerous_tool"]}},
            tools=["@narrowed"],
        )
        rt = _codex_runtime(tmp_path)
        rt._agent_capabilities = {"mcpCapabilities": dict(_CODEX_1_11_CAPS)}
        out = self._projected(rt, [_stub("narrowed")], referenced=frozenset({"narrowed"}))
        assert out is not None
        assert "narrowed" not in set(_by_name(out.servers))

    def test_the_deny_set_comes_back_with_the_array(self, tmp_path, agents_dir):
        """The deny pairs the runtime hands the session handle, from the array's own parse."""
        _write_spec(
            agents_dir,
            servers={"kirocrew-core": {**_CORE, "disabledTools": ["spawn_run"]}},
            tools=["@kirocrew-core"],
        )
        rt = _codex_runtime(tmp_path)
        rt._agent_capabilities = {"mcpCapabilities": dict(_CODEX_1_11_CAPS)}
        out = self._projected(rt, [], referenced=frozenset())
        assert out is not None
        assert ("kirocrew-core", "spawn_run") in out.denied_tools
        # The server itself still mounts: the restriction narrows a tool, it does not
        # cost the session its control plane.
        assert "kirocrew-core" in set(_by_name(out.servers))

    def test_the_session_identity_rides_the_element(self, tmp_path, agents_dir):
        """A codex stdio child starts from ``env_clear()`` plus an allowlist, so the
        session key reaches Crew's control plane only if the ELEMENT carries it."""
        _write_spec(
            agents_dir,
            servers={"kirocrew-core": dict(_CORE)},
            tools=["@kirocrew-core"],
        )
        rt = _codex_runtime(tmp_path)
        rt._agent_capabilities = {"mcpCapabilities": dict(_CODEX_1_11_CAPS)}
        out = self._projected(rt, [], referenced=frozenset())
        assert out is not None
        env = _env(_by_name(out.servers)["kirocrew-core"])
        assert env["KIROCREW_SESSION_KEY"] == "s-owner"
        assert env["KIROCREW_CHANNEL_ID"] == "c-1"

    def test_a_host_with_no_mirror_takes_no_projection(self, tmp_path):
        """kiro and KAS reach their servers natively, so this answers None and the
        caller runs the plain pooled path, with no new failure mode on the shared
        construction path."""
        for backend in (ACP_BACKEND_KIRO, ACP_BACKEND_KAS):
            assert mirror_for(backend) is None
            rt = AcpRuntime(work_dir=str(tmp_path), acp_backend=backend)
            rt._initialized = True
            rt._mcp_gateway_overlay = str(tmp_path)
            assert (
                asyncio.run(
                    rt._mirrored_session_mcp(
                        "kirocrew", work_dir=str(tmp_path), session_key="s", channel_id="c"
                    )
                )
                is None
            )


def test_granted_dashboard_is_rebuilt_and_bound(agents_dir, monkeypatch):
    _write_spec(
        agents_dir,
        servers={"kirocrew-dashboard": {"command": "/untrusted", "args": []}},
        tools=["@kirocrew-dashboard"],
    )
    original = session_mcp.managed_mcp_spec_entry

    def managed(name, **kwargs):
        if name == "kirocrew-dashboard":
            return {"command": "/opt/kirocrew", "args": ["mcp-dashboard"]}
        return original(name)

    monkeypatch.setattr(session_mcp, "managed_mcp_spec_entry", managed)
    projection = codex_projection(
        "kirocrew", session_key="dashboard:owner", session_token="issued-token"
    )
    dashboard = _by_name(projection.params["mcpServers"])["kirocrew-dashboard"]
    assert dashboard["command"] == "/opt/kirocrew"
    assert dashboard["args"] == ["mcp-dashboard"]
    assert _env(dashboard)["KIROCREW_SESSION_KEY"] == "dashboard:owner"
    assert "issued-token" in _env(dashboard).values()


@pytest.mark.parametrize("restricted", [False, True])
def test_dashboard_broker_mount_keeps_spec_restrictions(agents_dir, restricted):
    entry = {"command": "/unused"}
    if restricted:
        entry["disabledTools"] = ["session_send"]
    _write_spec(agents_dir, servers={"kirocrew-dashboard": entry}, tools=["@kirocrew-dashboard"])
    projected = codex_projection(
        "kirocrew",
        stub_server_names=("kirocrew-dashboard",),
        stub_elements=[_stub("kirocrew-dashboard")],
    )
    assert ("kirocrew-dashboard" in _by_name(projected.params["mcpServers"])) is not restricted


@pytest.mark.parametrize(
    "tools,entry",
    [
        ([], {"command": "/unused"}),
        (["@kirocrew-dashboard"], {"command": "/unused", "disabled": True}),
    ],
)
def test_dashboard_grant_is_not_created_by_identity(agents_dir, tools, entry):
    _write_spec(agents_dir, servers={"kirocrew-dashboard": entry}, tools=tools)
    projected = codex_projection(
        "kirocrew",
        session_key="dashboard:owner",
        session_token="owner-token",
        stub_server_names=("kirocrew-dashboard",),
        stub_elements=[_stub("kirocrew-dashboard")],
    )
    assert "kirocrew-dashboard" not in _by_name(projected.params["mcpServers"])


@pytest.mark.parametrize("ambient", [None, "false"])
def test_codex_session_mount_outranks_unbound_global_config(ambient):
    env = {} if ambient is None else {"DISABLE_MCP_CONFIG_FILTERING": ambient}
    CodexHarness().apply_spawn_env(env)
    assert env["DISABLE_MCP_CONFIG_FILTERING"] == "true"
