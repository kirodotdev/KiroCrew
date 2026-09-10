"""Codex's spec projection: the agent spec -> a codex ``session/new`` array.

``providers/mirrors/codex.py`` reuses claude's translation and adds two rules of
its own. Both are properties of the ADAPTER rather than of Crew, so both are
measured against a real ``codex-acp`` here and asserted as unit behaviour above:

* an ``sse`` element is dropped, because codex-acp fails the WHOLE ``session/new``
  on one -- so forwarding it costs the session every other server;
* Crew's OWN servers carry ``KIROCREW_SESSION_KEY`` on the element, because
  ``codex-rs`` launches a stdio MCP server with ``env_clear()`` plus a fixed
  allowlist and inherits nothing else -- and which servers those are is decided by
  the resolved invocation, never by the name, since that key authenticates a
  session-directive claim and the agent spec is hand-editable.

``test_real_codex_acp_accepts_the_crew_stdio_element`` is the anti-drift guard for
both: an adapter fact a projection depends on is measured against the adapter, and
this is where the measurement lives.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.acp import session_mcp
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKENDS_MEMBER_DISPATCH,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
)
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

#: What codex-acp 1.11.0's ``initialize`` actually answered. Used as the live
#: advertisement in these tests so they exercise the same input the client feeds
#: the filter, rather than a shape no adapter returns.
_CODEX_1_11_CAPS = {"acp": False, "http": True, "sse": False}

_CORE = {"command": "/opt/kirocrew", "args": ["mcp-core"]}
_CRON = {"command": "/opt/kirocrew", "args": ["mcp-cron"]}


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """Point the agent-spec resolver at a temp agents directory.

    Same seam as ``test_acp_session_mcp.py``: materialization would rebuild the
    managed default from bundled defaults, and these tests supply the spec.
    """
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
    # The global settings file is a second source of per-tool restrictions;
    # these tests supply it (or leave it absent) rather than read the machine's.
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "settings-mcp.json")
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
    managed = {"kirocrew-core": dict(_CORE), "kirocrew-cron": dict(_CRON)}
    monkeypatch.setattr(
        session_mcp,
        "managed_mcp_spec_entry",
        lambda name: dict(managed[name]) if name in managed else None,
    )
    monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
    return d


def _write_spec(agents_dir: Path, *, servers: dict, tools: list | None) -> None:
    spec: dict = {"name": "kirocrew", "mcpServers": servers}
    if tools is not None:
        spec["tools"] = tools
    (agents_dir / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")


def _by_name(elements: list[dict]) -> dict[str, dict]:
    return {e["name"]: e for e in elements}


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
    """The filter reads THIS session's advertisement, not a remembered one.

    Keyed on ``initialize``'s ``mcpCapabilities`` rather than on a constant: an
    adapter release that gains ``sse`` or drops ``http`` would make a hardcoded set
    silently wrong, and being wrong here is not "one server missing" -- codex
    answers ``-32600`` for the whole ``session/new``.
    """

    def test_an_unadvertised_transport_is_dropped_and_the_rest_survive(self):
        """One bad entry must not be allowed to cost the whole session.

        codex-acp answers ``session/new`` with ``-32600`` for the entire request
        when it meets an ``sse`` element, so dropping it is what keeps the other
        servers. Withholding the whole array instead would be the same loss by
        another route.
        """
        kept = drop_unadvertised_transports(
            [
                {"name": "remote", "type": "sse", "url": "https://x/sse", "headers": []},
                {"name": "local", "type": "stdio", "command": "/bin/x", "args": [], "env": []},
            ],
            _CODEX_1_11_CAPS,
        )
        assert [e["name"] for e in kept] == ["local"]

    def test_an_ADVERTISED_remote_transport_is_KEPT(self):
        """Measured: codex-acp 1.11.0 advertises ``http: true``.

        "stdio only" would have been the easy rule and the wrong one -- dropping a
        remote server the adapter would have mounted removes capability from the
        session with no error to explain it, which is the same class of mistake as
        delivering one it refuses.
        """
        kept = drop_unadvertised_transports(
            [{"name": "r", "type": "http", "url": "https://x/mcp", "headers": []}],
            _CODEX_1_11_CAPS,
        )
        assert [e["name"] for e in kept] == ["r"]

    def test_a_transport_the_agent_LATER_advertises_is_kept_with_no_code_change(self):
        """The point of reading the fact: a future adapter needs no edit here.

        A release that starts advertising ``sse`` is served by the same code, which
        is precisely what a hardcoded unsupported-transport set could not do.
        """
        kept = drop_unadvertised_transports(
            [{"name": "remote", "type": "sse", "url": "https://x/sse", "headers": []}],
            {"acp": False, "http": True, "sse": True},
        )
        assert [e["name"] for e in kept] == ["remote"]

    def test_an_unknown_advertisement_keeps_stdio_only(self):
        """Fail-safe, and not arbitrary: ACP requires every agent to support stdio.

        Anything else with no positive claim behind it risks the whole request, so
        a session whose handshake has not been read yet keeps the one transport that
        cannot be refused.
        """
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
        """The tag is measured-good, so nothing is adapted away.

        ACP v1 spells ``McpServer`` as ``serde(tag = "type")`` with the stdio
        variant as the UNTAGGED fallback, so ``type: "stdio"`` matches no named
        variant and falls through to it -- and a real adapter accepts it. Rewriting
        the element for codex would have been a fix for a problem that is not there,
        and would have put two element shapes into one translator.
        """
        el = codex_elements(
            [{"name": "local", "type": "stdio", "command": "/bin/x", "args": [], "env": []}]
        )[0]
        assert el["type"] == "stdio"
        assert drop_unadvertised_transports([el], _CODEX_1_11_CAPS) == [el]


# ── identity carriage and name folding ───────────────────────────────


class TestIdentityEnvCarriage:
    """Why any of this is here: ``codex-rs``'s stdio launcher runs
    ``Command::env_clear()`` and then re-adds only ``DEFAULT_ENV_VARS``
    (``HOME``/``PATH``/``SHELL``/``USER``/``LANG``/...) plus the entry's own ``env``
    map. So the process inheritance claude's MCP children rely on does not exist
    here, and an entry without ``KIROCREW_SESSION_KEY`` yields a control plane that
    cannot name the session it belongs to — which is also what the out-of-band
    session-directive path claims against.

    The carriage is deliberately narrow: it reaches the two servers the shared
    translation REPLACES from the managed source, and nothing the spec describes.
    """

    def test_the_control_plane_carries_the_session_key(self):
        el = codex_elements([_element("kirocrew-core")], session_key="chat-7-123")[0]
        assert _env(el)["KIROCREW_SESSION_KEY"] == "chat-7-123"

    def test_the_control_plane_carries_the_bound_port(self):
        """``members.member_dispatch_session_server``'s reason, same mechanism.

        Without the port the child falls through to the run marker, whose check
        needs ``lsof``, which sees no listener from inside a sandbox's user
        namespace — so the child dials the default port and every call is a
        connection refused on a gateway bound anywhere else.
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
        """The security half of the carriage, and the reason it is a filter.

        ``KIROCREW_SESSION_KEY`` is the credential Crew's internal API
        authenticates a session-directive claim with. A claude MCP child sees it
        only because it inherits the adapter's whole environment — an inheritance
        nobody chose. Re-creating that deliberately for a spec-described server
        would be choosing it, and would let any server a spec happens to name drive
        the session it was mounted into.
        """
        el = codex_elements([_element("somebody-else")], session_key="chat-7-123")[0]
        assert _env(el) == {}

    def test_an_OPT_IN_crew_server_gets_no_identity_EITHER(self):
        """The deliberate line, and the one that shrank this PR.

        ``kirocrew-work`` and ``kirocrew-dashboard`` are Crew's own binaries, but
        they are ``opt_in`` sets that arrive from the agent spec UNREPLACED — so the
        spec chose their command, args and env. Earlier revisions tried to hand them
        the key behind a provenance check, and the reviewer found two ways through it
        in two rounds (a borrowed name, then a spec-supplied ``PYTHONPATH`` beside a
        genuine command). The credential now rides only what Crew derives itself.

        Reaching this function at all would be a bug — ``codex_withheld_servers``
        drops them upstream — so this pins the second half of the rule rather than
        the first: even if one arrives, it carries no identity.
        """
        el = codex_elements(
            [_element("kirocrew-work", args=["mcp-work"])], session_key="chat-7-123"
        )[0]
        assert _env(el) == {}

    def test_a_stale_spec_value_is_replaced_not_duplicated(self):
        """Resolved-live beats spec-declared, and the array holds one pair per name.

        Two pairs with one name is a shape whose winner is the consumer's choice,
        which is not a thing to leave to the consumer for an identity value.
        """
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

    codex-acp runs ``name.replace(|c: char| c.is_whitespace(), "_")``. An earlier
    revision used ``"_".join(name.split())``, which also collapses runs and strips
    the ends — and that difference MANUFACTURED a collision with the control plane
    that codex's own rule cannot produce.
    """

    def test_the_fold_matches_codex_character_for_character(self):
        for raw in ("my server", "a  b", " lead", "trail ", "x\ty", "plain", "kirocrew-core"):
            assert codex_name(raw) == re.sub(r"\s", "_", raw), raw

    def test_no_spec_name_can_fold_onto_the_control_plane(self):
        """The structural reason the identity carriage is safe by name.

        Folding only ever replaces a whitespace character with ``_``; it never
        removes one and never produces a ``-``. ``kirocrew-core`` contains no ``_``,
        so the only string that folds onto it is itself — and that name the shared
        translation replaces from the managed source. The collapsing fold broke
        exactly this: it mapped ``"kirocrew-core "`` onto the real name.
        """
        for impostor in ("kirocrew-core ", " kirocrew-core", "kirocrew core", "kirocrew_core"):
            assert codex_name(impostor) != "kirocrew-core", impostor
        assert codex_name("kirocrew-core") == "kirocrew-core"

    def test_a_whitespace_variant_reaches_codex_under_its_own_distinct_name(self):
        """End to end: the impostor mounts, under a name that is not the real one.

        It gets no Crew identity (it is not the control plane) and it cannot take the
        control plane's slot (its folded name differs), so it is an ordinary
        third-party server with an odd name — which is all it ever was.
        """
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
        """Two spec names CAN legitimately fold together; codex would take the last.

        No credential is at stake on either, so this is a naming clash rather than a
        privilege question — but a session whose roster does not match what
        registered is still worth being deterministic about.
        """
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
        """A dropped RESTRICTION is not an addressed gap, whatever it is labelled.

        An earlier revision ruled this ``no-channel`` and forwarded the server
        anyway: codex-acp hardcodes ``disabled_tools=None`` so the element has no
        slot, and claude's answer (``permissions.deny``) needs a settings file codex
        does not have. But a session that can call a tool the user switched off is
        the defect, not the label on it. On a transport with no deny channel the only
        faithful option is to withhold the server.
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


# ── the client seam ─────────────────────────────────────────────────────────


class TestClientSeam:
    def test_codex_is_in_the_array_set_and_NOT_in_member_dispatch(self):
        """One set this projection needs, and one it deliberately stays out of.

        Without the array set the hook returns ``[]`` however good the mirror is.
        Member dispatch is a different capability -- session control in a DM thread
        -- and this PR does not add it, so the set is pinned in both directions.
        """
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_SESSION_MCP_ARRAY
        assert ACP_BACKEND_CODEX not in ACP_BACKENDS_MEMBER_DISPATCH

    def test_the_codex_hook_returns_the_projection(self, tmp_path, agents_dir):
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
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CODEX)
        names = set(_by_name(client._codex_session_mcp_servers()))
        assert "foo" in names
        assert "kirocrew-core" in names

    def test_the_hook_carries_this_clients_session_key(self, tmp_path, agents_dir):
        """The mirror cannot discover it; the client passes it down."""
        _write_spec(agents_dir, servers={}, tools=["@kirocrew-core"])
        client = AcpClient(
            work_dir=tmp_path,
            agent="kirocrew",
            acp_backend=ACP_BACKEND_CODEX,
            session_key="chat-9-42",
        )
        core = _by_name(client._codex_session_mcp_servers())["kirocrew-core"]
        assert _env(core)["KIROCREW_SESSION_KEY"] == "chat-9-42"

    def test_an_sse_spec_entry_never_reaches_a_codex_session(self, tmp_path, agents_dir):
        """End to end through the client, against the advertisement it captured."""
        _write_spec(
            agents_dir,
            servers={
                "remote": {"url": "https://x/sse", "type": "sse"},
                "local": {"command": "/bin/foo"},
            },
            tools=["@remote", "@local"],
        )
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CODEX)
        client._agent_mcp_capabilities = dict(_CODEX_1_11_CAPS)
        names = set(_by_name(client._codex_session_mcp_servers()))
        assert "remote" not in names
        assert "local" in names

    def test_the_hook_reads_the_advertisement_rather_than_a_constant(self, tmp_path, agents_dir):
        """The same client, the same spec, two advertisements, two answers.

        This is what a hardcoded unsupported-transport set could not do, and the
        reason the WATCH on that constant was legitimate: the code now follows the
        adapter instead of following one measurement of it.
        """
        _write_spec(
            agents_dir,
            servers={"remote": {"url": "https://x/sse", "type": "sse"}},
            tools=["@remote"],
        )
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CODEX)
        client._agent_mcp_capabilities = dict(_CODEX_1_11_CAPS)
        assert "remote" not in _by_name(client._codex_session_mcp_servers())
        client._agent_mcp_capabilities = {"acp": False, "http": True, "sse": True}
        assert "remote" in _by_name(client._codex_session_mcp_servers())

    def test_a_codex_session_needs_no_claude_settings_file(self, tmp_path, agents_dir):
        """Codex's array is not conditional on a file no codex session has.

        Claude withholds its whole array unless Crew authored
        ``settings.local.json``, because a ``permissions.allow`` in a file Crew does
        not own pre-approves a call and Crew's gate never fires. Copying that
        condition here would withhold every Crew tool from every codex session on
        the strength of something that does not describe the backend: codex's
        asking is asserted per session and enforced, so there is no file to own.
        """
        _write_spec(agents_dir, servers={}, tools=["@kirocrew-core"])
        codex = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CODEX)
        assert codex._claude_settings_authored is False
        assert "kirocrew-core" in _by_name(codex._codex_session_mcp_servers())

    def test_a_server_narrowed_per_tool_is_withheld_end_to_end(self, tmp_path, agents_dir):
        """The restriction reaches the array as an omission, through the real seam.

        ``disabledTools`` is stripped by ``acp_server_element`` like every other
        kiro-cli-only key, so the mirror reads it from the spec itself
        (``session_mcp_restricted_servers``) rather than from the element it never
        appears in. The un-narrowed sibling is kept, so this is a withhold and not a
        blanket refusal.
        """
        _write_spec(
            agents_dir,
            servers={
                "narrowed": {"command": "/bin/foo", "disabledTools": ["dangerous_tool"]},
                "open": {"command": "/bin/bar"},
            },
            tools=["@narrowed", "@open"],
        )
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CODEX)
        client._agent_mcp_capabilities = dict(_CODEX_1_11_CAPS)
        names = set(_by_name(client._codex_session_mcp_servers()))
        assert "narrowed" not in names
        assert "open" in names

    def test_an_empty_or_malformed_disabled_tools_withholds_nothing(self, tmp_path, agents_dir):
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
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CODEX)
        client._agent_mcp_capabilities = dict(_CODEX_1_11_CAPS)
        assert {"empty", "bogus"} <= set(_by_name(client._codex_session_mcp_servers()))

    def test_an_identity_bound_crew_server_is_not_mounted_at_all(self, tmp_path, agents_dir):
        """Present-but-unusable is the defect this folder exists to kill.

        A ``kirocrew-work`` that mounts with no session identity answers
        ``not_bound`` to every call — tools the model can see and cannot use, which
        costs it turns and tells it nothing. It is withheld instead, and the absence
        is logged. The control plane is unaffected: it IS re-derived, so it keeps its
        identity and stays.
        """
        _write_spec(
            agents_dir,
            servers={"kirocrew-work": {"command": "/opt/kirocrew", "args": ["mcp-work"]}},
            tools=["@kirocrew-work", "@kirocrew-core"],
        )
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CODEX)
        client._agent_mcp_capabilities = dict(_CODEX_1_11_CAPS)
        names = set(_by_name(client._codex_session_mcp_servers()))
        assert "kirocrew-work" not in names
        assert "kirocrew-core" in names

    def test_the_control_plane_is_never_withheld_by_its_own_disabled_tools(
        self, tmp_path, agents_dir
    ):
        """Withholding the control plane would BE the defect, not a safe default.

        ``managed_mcp_spec_entry`` emits only command/args/env, so a ``disabledTools``
        on the spec's ``kirocrew-core`` entry narrows nothing on the wire — and
        dropping the server over it would leave the session unable to report back to
        its channel at all. The server stays mounted; the restriction is honoured at
        the approval request instead (``TestSpecDisabledToolRefusal``).
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
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CODEX)
        client._agent_mcp_capabilities = dict(_CODEX_1_11_CAPS)
        assert "kirocrew-core" in _by_name(client._codex_session_mcp_servers())

    def test_a_pooled_stub_cannot_re_add_a_withheld_server(self, tmp_path, agents_dir):
        """A stub wraps the SAME name, so an unnarrowed append un-withholds it.

        The stub is the UNRESTRICTED server, which is the worse of the two, so the
        pooled half of the array goes through the same withholding rules. The shared
        append returns ``[]`` for codex precisely so it cannot bypass them.
        """
        _write_spec(
            agents_dir,
            servers={"narrowed": {"command": "/bin/foo", "disabledTools": ["dangerous_tool"]}},
            tools=["@narrowed", "@unrelated"],
        )
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CODEX)
        client._agent_mcp_capabilities = dict(_CODEX_1_11_CAPS)
        client._pooled_broker_stubs = lambda: [  # type: ignore[method-assign]
            {"name": "narrowed", "command": "/stub", "args": [], "env": [], "type": "stdio"},
            {"name": "unrelated", "command": "/stub", "args": [], "env": [], "type": "stdio"},
        ]
        names = set(_by_name(client._codex_session_mcp_servers()))
        assert "narrowed" not in names
        assert "unrelated" in names
        # And the shared append is inert for codex, so nothing re-adds it later.
        assert client._pooled_mcp_servers() == []

    def test_the_mirror_places_the_pooled_stubs_itself(self, agents_dir):
        """Both halves of the array are the MIRROR's, so one withhold rule covers both.

        A stub carries the same name as the entry it rewrites, so a stub appended
        by the client after the mirror withheld that name would un-withhold it —
        and as the UNRESTRICTED server. The elements come in beside the names the
        translation already yields to, and are placed here.
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

        The overlay is written per agent from the GLOBAL settings file as well as
        the agent's own spec, so it can carry a stub for a server this agent never
        referenced. The translated half is filtered by ``tools``; a stub is the
        same server under the same name, so it is held to the same allowlist --
        from the same parse -- or an unreferenced server mounts anyway, which is
        the exact thing that filter exists to prevent. ``*`` still grants all, and
        a spec with no ``tools`` list grants nothing.
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
        """The design point, pinned: the client holds the overlay and the mirror
        holds the rule. A second copy of the withhold filter in ``client.py`` is
        exactly the split the next mirror author would copy -- and so is a
        ``self._is_codex`` branch around the projection call, which is why the seam
        is the base contract's ``session_projection`` and not a codex import."""
        import inspect

        from kiro_crew.acp import client as client_mod

        source = inspect.getsource(client_mod)
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
        """DERIVED, not enumerated -- the drift direction here is the bad one.

        An earlier revision spelled the three names out under a comment saying they
        were the managed set minus the control plane. A server added to the managed
        set later would have missed the hand-copy, mounted, and answered
        ``not_bound`` to every call -- the present-but-unusable defect this folder
        exists to remove, reintroduced by omission. So the subtraction is asserted
        against its two SOURCES, and the module is pinned to hold no enumeration
        that could drift from them again.

        The enumeration check reads the AST rather than the text, so a name a
        DOCSTRING mentions (several explain the control plane by name) is not
        mistaken for one the code depends on.
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

        Two reads of a USER-WRITABLE file is a consistency window, and the two
        halves here are not independent: the restriction set says which servers to
        withhold, and the translation is what they would be withheld from. A spec
        that gains ``disabledTools`` between the reads yields a withhold set from
        the old bytes applied to a translation of the new ones -- and the narrowed
        server mounts UN-narrowed, which is the outcome the withholding exists to
        prevent.

        Counted at the parse seam rather than asserted on the source, because the
        property is "how many times the bytes were read", not "which helper the
        projection happens to call".
        """
        _write_spec(
            agents_dir,
            servers={"narrowed": {"command": "/bin/foo", "disabledTools": ["x"]}},
            tools=["@narrowed"],
        )
        real = session_mcp._agent_spec_for
        calls: list[object] = []

        def counting(agent, work_dir=None):
            calls.append(agent)
            return real(agent, work_dir)

        monkeypatch.setattr(session_mcp, "_agent_spec_for", counting)
        params = CodexMirror().session_params("kirocrew", session_key="k", channel_id="c")

        assert len(calls) == 1, f"the codex projection parsed the agent spec {len(calls)} times"
        assert "narrowed" not in _by_name(params["mcpServers"])

    def test_a_restriction_arriving_between_two_reads_cannot_be_lost(self, agents_dir, monkeypatch):
        """The window itself, driven: a SECOND read would see the narrowed spec.

        With one parse there is no second read to disagree with, so the projection
        either withholds the server (it saw the narrowing) or translates it from
        bytes that did not narrow it -- never the mismatch where the withhold set
        comes from one revision of the file and the array from another.
        """
        _write_spec(agents_dir, servers={"narrowed": {"command": "/bin/foo"}}, tools=["@narrowed"])
        real = session_mcp._agent_spec_for
        seen = {"n": 0}

        def drifting(agent, work_dir=None):
            spec = real(agent, work_dir)
            seen["n"] += 1
            if seen["n"] >= 2 and isinstance(spec, dict):
                spec["mcpServers"]["narrowed"]["disabledTools"] = ["x"]
            return spec

        monkeypatch.setattr(session_mcp, "_agent_spec_for", drifting)
        params = CodexMirror().session_params("kirocrew", session_key="k", channel_id="c")

        assert seen["n"] == 1
        # Both halves saw the un-narrowed revision, so the server mounts and carries
        # no restriction it was never given. The forbidden outcome is the other one:
        # a mount whose own spec narrowed it.
        assert "narrowed" in _by_name(params["mcpServers"])

    def test_the_spawn_path_warms_the_cache_off_the_loop(self):
        """H13: the shared ``session/new`` site must stay a pure in-memory read.

        Pinned at the source, in this file's neighbour's idiom, because the warm
        sits inside an async spawn path with no unit-level seam. The claude arm
        already does this; a codex arm that skipped it would move the disk read
        onto the loop for every codex session.
        """
        import inspect

        source = inspect.getsource(AcpClient._spawn)
        codex_arm = source.split("elif self._is_codex:", 1)
        assert len(codex_arm) == 2, "the codex spawn arm has moved"
        assert "self._session_mcp_cache = await asyncio.to_thread" in codex_arm[1]


# ── the per-tool restriction on the control plane ────────────────────────────


def _codex_mcp_tool_call(call_id: str, server: str, tool: str) -> JsonRpcMessage:
    """The ``tool_call`` frame codex-acp emits for an MCP call (``createMcpToolCallUpdate``)."""
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "s-1",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": call_id,
                "kind": "execute",
                "title": f"mcp.{server}.{tool}",
                "status": "pending",
                "rawInput": {"server": server, "tool": tool, "arguments": {"a": 1}},
                "_meta": {"is_mcp_tool_call": True},
            },
        },
    )


def _codex_mcp_approval(request_id: int, call_id: str) -> JsonRpcMessage:
    """The correlated ``session/request_permission`` (``buildMcpPermissionRequest``):
    no rawInput of its own, the adapter's three options, ``cancel`` as the reject."""
    return JsonRpcMessage(
        id=request_id,
        method="session/request_permission",
        params={
            "sessionId": "s-1",
            "toolCall": {"toolCallId": call_id, "kind": "execute", "status": "pending"},
            "_meta": {"is_mcp_tool_approval": True},
            "options": [
                {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                {
                    "optionId": "allow_session",
                    "name": "Allow for this session",
                    "kind": "allow_always",
                },
                {"optionId": "cancel", "name": "Cancel", "kind": "reject_once"},
            ],
        },
    )


class TestSpecDisabledToolRefusal:
    """``disabledTools`` on the control plane is HONOURED on codex, not dropped.

    kiro-cli enforces it itself (``_MANAGED_MCP_ENTRY_KEYS`` admits the key on a
    managed entry) and claude gets it as ``permissions.deny``; codex has no wire
    channel for it, and withholding ``kirocrew-core`` whole would leave the session
    unable to report back. So the server mounts and the CALL is refused where codex
    asks: every un-annotated MCP call prompts under ``mode=read-only``
    (codex-rs ``requires_mcp_tool_approval`` with ``AppToolApproval::Auto``), Crew's
    servers declare no annotations, and the client answers the prompt for a
    switched-off tool with the adapter's reject option.
    """

    def _client(self, tmp_path, agents_dir, *, disabled: list[str]) -> AcpClient:
        _write_spec(
            agents_dir,
            servers={
                "kirocrew-core": {
                    "command": "/opt/kirocrew",
                    "args": ["mcp-core"],
                    "disabledTools": disabled,
                }
            },
            tools=["@kirocrew-core"],
        )
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CODEX)
        client._agent_mcp_capabilities = dict(_CODEX_1_11_CAPS)
        # The spawn path's warm, run inline: this is where the deny set is derived.
        client._session_mcp_cache = client._resolve_session_mcp_servers()
        return client

    @staticmethod
    def _capture(client: AcpClient) -> list[tuple]:
        sent: list[tuple] = []

        async def _send(request_id, payload):
            sent.append((request_id, payload))

        client._send_response = _send  # type: ignore[method-assign]
        return sent

    def test_the_deny_set_comes_out_of_the_projection(self, tmp_path, agents_dir):
        client = self._client(tmp_path, agents_dir, disabled=["spawn_run"])
        assert ("kirocrew-core", "spawn_run") in client._spec_denied_tools
        # And the server itself is still mounted: the restriction narrows a tool,
        # it does not cost the session its control plane.
        assert "kirocrew-core" in _by_name(client._codex_session_mcp_servers())

    @pytest.mark.asyncio
    async def test_a_switched_off_tool_is_refused_at_the_approval_request(
        self, tmp_path, agents_dir, monkeypatch
    ):
        client = self._client(tmp_path, agents_dir, disabled=["spawn_run"])
        sent = self._capture(client)
        audited: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                audited.append(kw)

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())

        # The tool_call frame arrives first and is cached by toolCallId...
        assert client._extract_tool_event(_codex_mcp_tool_call("c1", "kirocrew-core", "spawn_run"))
        # ...then codex asks. The event is built exactly as the dispatch loop builds it.
        event = client._build_permission_event(_codex_mcp_approval(7, "c1"))
        assert event.raw_params_trusted
        assert await client._deny_spec_disabled_tool(event) is True

        # Answered with the adapter's OWN reject option, never `cancelled`: a
        # cancelled outcome is the turn-scoped fallback, this is one call.
        assert sent == [(7, {"outcome": {"outcome": "selected", "optionId": "cancel"}})]
        assert audited and audited[0]["outcome"] == "denied"
        assert audited[0]["tool_name"] == "mcp__kirocrew-core__spawn_run"
        assert audited[0]["metadata"]["reason"] == "spec_disabled_tool"

    @pytest.mark.asyncio
    async def test_a_tool_the_spec_left_on_goes_to_the_ordinary_gate(self, tmp_path, agents_dir):
        client = self._client(tmp_path, agents_dir, disabled=["spawn_run"])
        sent = self._capture(client)
        client._extract_tool_event(_codex_mcp_tool_call("c2", "kirocrew-core", "send_message"))
        event = client._build_permission_event(_codex_mcp_approval(8, "c2"))
        assert await client._deny_spec_disabled_tool(event) is False
        assert sent == []

    @pytest.mark.asyncio
    async def test_the_permission_payloads_own_fields_are_never_the_identity(
        self, tmp_path, agents_dir
    ):
        """Identity comes from the cached tool_call frame or not at all.

        An uncorrelated approval carries ``rawInput = {serverName, description,
        schema}`` on the permission frame itself; a forged one could carry anything.
        With no cached frame the params are not trusted and the request goes on to
        the human -- toward ASKING, never toward running.
        """
        client = self._client(tmp_path, agents_dir, disabled=["spawn_run"])
        sent = self._capture(client)
        msg = _codex_mcp_approval(9, "never-seen")
        msg.params["toolCall"]["rawInput"] = {"server": "kirocrew-core", "tool": "spawn_run"}
        event = client._build_permission_event(msg)
        assert not event.raw_params_trusted
        assert await client._deny_spec_disabled_tool(event) is False
        assert sent == []

    @pytest.mark.asyncio
    async def test_a_non_codex_client_is_untouched(self, tmp_path, agents_dir):
        _write_spec(
            agents_dir,
            servers={"kirocrew-core": {"command": "/x", "disabledTools": ["spawn_run"]}},
            tools=["@kirocrew-core"],
        )
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        client._session_mcp_cache = client._resolve_session_mcp_servers()
        assert client._spec_denied_tools == frozenset()
        client._extract_tool_event(_codex_mcp_tool_call("c3", "kirocrew-core", "spawn_run"))
        event = client._build_permission_event(_codex_mcp_approval(10, "c3"))
        assert await client._deny_spec_disabled_tool(event) is False

    @pytest.mark.asyncio
    async def test_the_auto_approve_site_refuses_a_switched_off_tool(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """``_handle_permission`` answers with no consumer's gate in between, so the
        refusal must run there too -- and it does, before the approve."""
        client = self._client(tmp_path, agents_dir, disabled=["spawn_run"])
        sent = self._capture(client)
        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(
            sel_mod, "sel", lambda: type("S", (), {"log_tool_invocation": lambda *a, **k: None})()
        )
        client._extract_tool_event(_codex_mcp_tool_call("c4", "kirocrew-core", "spawn_run"))
        await client._handle_permission(_codex_mcp_approval(11, "c4"))
        assert sent == [(11, {"outcome": {"outcome": "selected", "optionId": "cancel"}})]

    @pytest.mark.asyncio
    async def test_the_auto_approve_site_refuses_an_MCP_approval_it_cannot_identify(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """No human at this site, so "unidentified" cannot fall toward asking.

        An MCP tool approval (``_meta.is_mcp_tool_approval``) with no cached
        ``tool_call`` frame -- a loop that never populated the provenance caches, a
        standalone approval, an overflowed cache -- would otherwise be approved
        blind on a session whose spec switches tools off. It is refused instead.
        A shell approval on the same path is untouched: the check is scoped to the
        one shape it can reason about.
        """
        client = self._client(tmp_path, agents_dir, disabled=["spawn_run"])
        sent = self._capture(client)
        audited: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                audited.append(kw)

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        await client._handle_permission(_codex_mcp_approval(12, "never-seen"))
        assert sent == [(12, {"outcome": {"outcome": "selected", "optionId": "cancel"}})]
        # A permission decision, so it reaches the SEL like the identified refusal.
        assert audited and audited[0]["outcome"] == "denied"
        assert audited[0]["metadata"]["reason"] == "spec_disabled_tool_unidentified_call"

        shell = JsonRpcMessage(
            id=13,
            method="session/request_permission",
            params={
                "sessionId": "s-1",
                "toolCall": {"toolCallId": "sh1", "kind": "execute", "title": "ls"},
                "options": [
                    {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "cancel", "name": "Cancel", "kind": "reject_once"},
                ],
            },
        )
        await client._handle_permission(shell)
        assert sent[-1] == (13, {"outcome": {"outcome": "selected", "optionId": "allow_once"}})

    @pytest.mark.asyncio
    async def test_the_auto_approve_site_is_untouched_without_a_deny_set(
        self, tmp_path, agents_dir
    ):
        """With nothing switched off, the site behaves exactly as before: no event
        is built, no option ids are recorded, the canonical allow id is sent."""
        client = self._client(tmp_path, agents_dir, disabled=[])
        assert client._spec_denied_tools == frozenset()
        sent = self._capture(client)
        await client._handle_permission(_codex_mcp_approval(14, "never-seen"))
        assert sent == [(14, {"outcome": {"outcome": "selected", "optionId": "allow_once"}})]
        assert 14 not in client._permission_options

    def test_the_streaming_loop_populates_the_provenance_the_refusal_reads(self):
        """``send_message_stream`` answers permissions through the auto-approve site,
        so it must run the FULL tool_call extractor (which caches raw params by
        toolCallId), not the stats-only tracker. Pinned on the source: the loop has
        no unit seam, and the failure mode is a switched-off tool running."""
        import inspect

        from kiro_crew.acp import client as client_mod

        body = inspect.getsource(client_mod.AcpClient.send_message_stream)
        assert "if self._spec_denied_tools:" in body
        assert "self._extract_tool_event(msg)" in body
        # ...and with no deny set, the stats-only tracker every other backend had.
        assert "self._track_tool_call(msg)" in body

    def test_a_dashboard_toggle_on_the_control_plane_is_honoured(self, tmp_path, agents_dir):
        """The ordinary path: the dashboard writes the restriction to the GLOBAL
        settings file only, the spec never carries it for a managed server, and
        the deny set must still name it."""
        _write_spec(
            agents_dir,
            servers={"kirocrew-core": {"command": "/opt/kirocrew", "args": ["mcp-core"]}},
            tools=["@kirocrew-core"],
        )
        (tmp_path / "settings-mcp.json").write_text(
            json.dumps({"mcpServers": {"kirocrew-core": {"disabledTools": ["spawn_run"]}}}),
            encoding="utf-8",
        )
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CODEX)
        client._agent_mcp_capabilities = dict(_CODEX_1_11_CAPS)
        client._session_mcp_cache = client._resolve_session_mcp_servers()
        assert ("kirocrew-core", "spawn_run") in client._spec_denied_tools
        assert "kirocrew-core" in _by_name(client._codex_session_mcp_servers())

    def test_a_third_party_server_narrowed_only_in_the_global_file_is_withheld(
        self, tmp_path, agents_dir
    ):
        """The withhold set reads the same two sources as the deny set.

        A third-party tool switched off in the dashboard lands in the global file
        only. The per-call refusal cannot reach a third-party server (an annotated
        tool is approved inside codex without asking), so the server must be
        withheld -- and it is, from the same unioned pairs, while the control plane
        stays mounted and takes the per-call path.
        """
        _write_spec(
            agents_dir,
            servers={
                "kirocrew-core": {"command": "/opt/kirocrew", "args": ["mcp-core"]},
                "third": {"command": "/bin/third"},
            },
            tools=["@kirocrew-core", "@third"],
        )
        (tmp_path / "settings-mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "third": {"command": "/bin/third", "disabledTools": ["risky"]},
                        "kirocrew-core": {"disabledTools": ["spawn_run"]},
                    }
                }
            ),
            encoding="utf-8",
        )
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CODEX)
        client._agent_mcp_capabilities = dict(_CODEX_1_11_CAPS)
        client._session_mcp_cache = client._resolve_session_mcp_servers()
        names = _by_name(client._codex_session_mcp_servers())
        assert "third" not in names
        assert "kirocrew-core" in names
        assert ("kirocrew-core", "spawn_run") in client._spec_denied_tools

    def test_a_switched_off_tool_that_ran_anyway_trips_the_wire(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """Independent of the adapter's prompting: if a denied call COMPLETES, the
        result frame says so and Crew makes it loud. Not enforcement -- the call
        ran -- but the difference between a silent drift and a red line."""
        client = self._client(tmp_path, agents_dir, disabled=["spawn_run"])
        audited: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                audited.append(kw)

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        client._extract_tool_event(_codex_mcp_tool_call("c9", "kirocrew-core", "spawn_run"))
        done = JsonRpcMessage(
            method="session/update",
            params={
                "sessionId": "s-1",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "c9",
                    "status": "completed",
                    "rawOutput": {"result": {"ok": True}, "error": None},
                },
            },
        )
        result = client._extract_tool_call_update(done)
        assert result is not None and result.tool_final
        client._tripwire_spec_disabled_tool(result)
        assert audited and audited[0]["outcome"] == "ran_despite_spec_disable"
        assert audited[0]["tool_name"] == "mcp__kirocrew-core__spawn_run"

        # A permitted tool completing, or a denied one that did NOT complete, is silent.
        audited.clear()
        client._extract_tool_event(_codex_mcp_tool_call("c10", "kirocrew-core", "send_message"))
        done.params["update"]["toolCallId"] = "c10"
        client._tripwire_spec_disabled_tool(client._extract_tool_call_update(done))
        done.params["update"]["toolCallId"] = "c9"
        done.params["update"]["status"] = "failed"
        failed = client._extract_tool_call_update(done)
        if failed is not None:
            client._tripwire_spec_disabled_tool(failed)
        assert audited == []

    def test_every_result_site_runs_the_tripwire(self):
        import inspect

        from kiro_crew.acp import client as client_mod

        for site in ("_dispatch_events", "_read_prompt_response"):
            body = inspect.getsource(getattr(client_mod.AcpClient, site))
            assert "_extract_tool_call_update(msg)" in body
            assert "_tripwire_spec_disabled_tool(" in body, site

    def test_the_control_plane_declares_no_tool_annotations(self):
        """The load-bearing premise, pinned rather than stated.

        The refusal is complete for the control plane ONLY because codex prompts
        for every one of its calls, and codex prompts (mode ``Auto``,
        ``requires_mcp_tool_approval``) only for a tool that carries no
        annotations -- a ``readOnlyHint: true`` tool is approved inside codex and
        never reaches Crew. So a Crew tool gaining an annotation for another
        backend's UX would silently reopen the dropped-restriction defect on codex.
        Both control-plane servers' ``tools/list`` descriptors are asserted
        annotation-free here; the day one needs an annotation, this is the test that
        says the codex refusal must then fire on the ``tool_call`` frame instead.
        """
        from kiro_crew import mcp_core, mcp_cron

        for server, tools in (
            ("kirocrew-core", mcp_core._list_tools()),
            ("kirocrew-cron", mcp_cron._list_tools()),
        ):
            assert tools, server
            annotated = [t["name"] for t in tools if t.get("annotations")]
            assert not annotated, (
                f"{server} tools {annotated} declare MCP annotations; codex approves an "
                "annotated read-only tool internally without asking, so the spec's "
                "disabledTools refusal at the permission request cannot reach it"
            )

    def test_a_reset_drops_the_deny_set_with_the_array(self, tmp_path, agents_dir):
        """Per-spawn freshness, same rule as the array: an edited spec is what the
        NEXT session enforces, not this one's snapshot."""
        client = self._client(tmp_path, agents_dir, disabled=["spawn_run"])
        assert client._spec_denied_tools
        client._reset_state()
        assert client._spec_denied_tools == frozenset()
        assert client._session_mcp_cache is None

    def test_the_server_is_spelled_as_codex_registers_it(self, tmp_path, agents_dir):
        """``rawInput.server`` is the REGISTERED name, so the deny set must be too."""
        _write_spec(
            agents_dir,
            servers={"my tool": {"command": "/bin/foo", "disabledTools": ["x"]}},
            tools=["@my tool"],
        )
        projection = codex_projection("kirocrew")
        assert ("my_tool", "x") in projection.denied_tools
        assert ("my tool", "x") not in projection.denied_tools

    def test_every_site_that_answers_a_permission_request_runs_the_refusal(self):
        """Structural: the refusal is paired with EVERY ``_build_permission_event``.

        Three sites answer a ``session/request_permission`` -- the event-yielding
        dispatch loop and the two auto-approve paths through ``_handle_permission``
        -- and a restriction that holds on two of them is not a restriction. Pinned
        on the source in this file's neighbour's idiom, because the sites have no
        unit-level seam of their own.
        """
        import inspect

        from kiro_crew.acp import client as client_mod

        source = inspect.getsource(client_mod)
        builds = source.count("self._build_permission_event(")
        # One per answering site; `_build_permission_event` is defined once more.
        assert builds == 2, "a site that answers a permission request was added or removed"
        for site in ("_dispatch_events", "_handle_permission"):
            body = inspect.getsource(getattr(client_mod.AcpClient, site))
            assert "_build_permission_event(" in body and "_deny_spec_disabled_tool(" in body, site
        # And the two other loops answer ONLY through _handle_permission.
        assert source.count("await self._handle_permission(msg)") == 2


# ── the real adapter ────────────────────────────────────────────────────────

# The driver runs OUT OF PROCESS on purpose: it spawns a real Node adapter, and a
# stalled readline in the test process would surface as a pytest timeout kill
# rather than the clean assertion failures below.
_DRIVER = r"""
import json, os, queue, subprocess, sys, threading, time

root, entry, stub, node = sys.argv[1:5]
report = os.path.join(root, "report.json")
env = dict(os.environ)
env["CODEX_HOME"] = os.path.join(root, "codex_home")
env["NO_BROWSER"] = "1"


def reap(p):
    # Every exit from drive() runs this, including an exception and the outer
    # timeout's SIGTERM: a bare readline() on an adapter that has stopped writing
    # blocks forever, and an adapter left running keeps its own MCP child alive
    # after this driver is gone. Nothing else on the machine knows to kill them.
    for step in (p.terminate, p.kill):
        try:
            step()
            p.communicate(timeout=15)
            return
        except subprocess.TimeoutExpired:
            continue
        except Exception:
            return


def pump(stream, q):
    for line in stream:
        q.put(line)
    q.put(None)


def drive(element):
    p = subprocess.Popen(
        [node, entry], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, cwd=os.path.join(root, "work"), env=env,
        text=True, bufsize=1,
    )
    try:
        # Read on a daemon thread so the deadline below is real. The main loop
        # never blocks on the adapter, so a stalled one costs 90s, not the turn.
        q = queue.Queue()
        threading.Thread(target=pump, args=(p.stdout, q), daemon=True).start()

        def send(o):
            p.stdin.write(json.dumps(o) + "\n")
            p.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": 1, "clientCapabilities": {"fs": {}}}})
        send({"jsonrpc": "2.0", "id": 2, "method": "session/new",
              "params": {"cwd": os.path.join(root, "work"), "mcpServers": [element]}})
        got, deadline = {}, time.time() + 90
        while 2 not in got:
            budget = deadline - time.time()
            if budget <= 0:
                break
            try:
                line = q.get(timeout=budget)
            except queue.Empty:
                break
            if line is None:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg.get("id"), int):
                got[msg["id"]] = msg
        if 2 in got and "result" in got[2]:
            # The child MCP server is launched and queried after session/new answers.
            for _ in range(120):
                if os.path.exists(report):
                    break
                time.sleep(0.25)
        return got.get(1) or {}, got.get(2) or {}
    finally:
        reap(p)


stdio_el = {
    "name": "kirocrew-core", "type": "stdio", "command": sys.executable,
    "args": [stub],
    "env": [{"name": "STUB_MCP_REPORT", "value": report},
            {"name": "KIROCREW_SESSION_KEY", "value": "probe-session-key"}],
}
init, new = drive(stdio_el)
out = {
    "mcp_capabilities": (init.get("result") or {}).get("agentCapabilities", {}).get(
        "mcpCapabilities"),
    "stdio_error": new.get("error"),
    "stdio_ok": bool(new.get("result")),
    "child": json.load(open(report)) if os.path.exists(report) else None,
}
if os.path.exists(report):
    os.unlink(report)
_, sse = drive({"name": "remote", "type": "sse", "url": "http://127.0.0.1:1/sse",
                "headers": []})
out["sse_error"] = sse.get("error")
out["sse_ok"] = bool(sse.get("result"))
_, bad = drive({"name": "no-command", "args": [], "env": []})
out["malformed_error"] = bad.get("error")
out["malformed_ok"] = bool(bad.get("result"))

# FOLD MEASUREMENT. codex registers by name with `insert`, so a collision is
# observable as a child that never launches. "probe  one" (TWO spaces) folds to
# "probe__one" under codex's per-character rule and to "probe_one" under a
# collapsing one -- so pairing it with a literal "probe__one" discriminates them:
# per-character means one slot and ONE report file, collapsing means two.
fold_a = os.path.join(root, "fold-a.json")
fold_b = os.path.join(root, "fold-b.json")
for path in (fold_a, fold_b):
    if os.path.exists(path):
        os.unlink(path)


def stub_named(name, report):
    return {
        "name": name, "type": "stdio", "command": sys.executable, "args": [stub],
        "env": [{"name": "STUB_MCP_REPORT", "value": report}],
    }


p_fold = subprocess.Popen(
    [node, entry], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL, cwd=os.path.join(root, "work"), env=env, text=True, bufsize=1,
)
try:
    def send_fold(o):
        p_fold.stdin.write(json.dumps(o) + "\n")
        p_fold.stdin.flush()

    send_fold({"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": 1, "clientCapabilities": {"fs": {}}}})
    send_fold({"jsonrpc": "2.0", "id": 2, "method": "session/new",
               "params": {"cwd": os.path.join(root, "work"), "mcpServers": [
                   stub_named("probe  one", fold_a), stub_named("probe__one", fold_b)]}})
    got_fold, deadline = {}, time.time() + 90
    q_fold = queue.Queue()
    threading.Thread(target=pump, args=(p_fold.stdout, q_fold), daemon=True).start()
    while 2 not in got_fold:
        budget = deadline - time.time()
        if budget <= 0:
            break
        try:
            line = q_fold.get(timeout=budget)
        except queue.Empty:
            break
        if line is None:
            break
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg.get("id"), int):
            got_fold[msg["id"]] = msg
    for _ in range(60):
        if os.path.exists(fold_a) or os.path.exists(fold_b):
            break
        time.sleep(0.25)
    time.sleep(4)
finally:
    reap(p_fold)
out["fold_session_ok"] = bool((got_fold.get(2) or {}).get("result"))
out["fold_children"] = sorted(
    n for n, pth in (("probe  one", fold_a), ("probe__one", fold_b)) if os.path.exists(pth)
)
print(json.dumps(out))
"""

# A stdio MCP server small enough to read: it records the environment it was
# LAUNCHED with (which is the measurement) and answers the two methods codex sends.
_STUB_MCP = r"""
import json, os, sys

REPORT = os.environ["STUB_MCP_REPORT"]
seen = []


def dump():
    tmp = REPORT + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"methods": seen, "env": dict(os.environ)}, fh)
    os.replace(tmp, REPORT)


def send(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    seen.append(msg.get("method") or "")
    dump()
    if "id" not in msg:
        continue
    if msg.get("method") == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
            "serverInfo": {"name": "stub", "version": "0"}}})
    elif msg.get("method") == "tools/list":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [
            {"name": "stub_echo", "description": "echo",
             "inputSchema": {"type": "object", "properties": {}}}]}})
    else:
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
"""


def _run_driver_reaping_group(
    argv: list[str], *, timeout: float
) -> "subprocess.CompletedProcess[str]":
    """Run the out-of-process driver in its OWN process group and reap the group.

    ``subprocess.run(timeout=...)`` kills the driver and nothing else. The driver
    spawns codex-acp, and codex-acp spawns the MCP child the element names, so on
    the timeout path the driver's own ``finally`` never runs and two generations of
    descendants outlive the test with nothing on the machine that knows to end them
    -- a leaked Node adapter holding a port and a Python MCP server holding a temp
    directory, degrading whatever runs next.

    ``start_new_session`` makes the driver a session/group leader, so ONE tree kill
    reaches every descendant that has not left the group. The kill runs on every
    exit rather than only after a timeout: a driver that reaped cleanly leaves an
    empty group and the kill is a no-op, which is cheaper and more reliable than
    deciding case by case whether it was needed.

    Routed through ``platform_compat.kill_process_tree`` -- ``killpg`` on POSIX,
    ``taskkill /T`` on Windows, with the broadcast guard that keeps a reserved pgid
    from signalling every process this uid owns. A raw ``os.killpg`` here would be
    POSIX-only and unguarded.
    """
    from kiro_crew import platform_compat

    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
    )

    def reap() -> None:
        try:
            platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
        except Exception:
            # Already gone is the expected case on the happy path.
            pass

    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            reap()
            # Bounded: the group is SIGKILLed, so the only thing left to do is
            # drain pipes no live writer holds. A second stall would be a kernel
            # problem, and swallowing the output beats hanging the suite.
            try:
                out, err = proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                out, err = "", ""
    finally:
        reap()
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _codex_acp_entry() -> Path | None:
    """The installed adapter's entry script, through the SPAWN's own resolver.

    Asking ``_resolve_codex_acp_bin`` rather than ``shutil.which`` is deliberate:
    what this test must exercise is the adapter a real session would spawn, on the
    same ladder (``CODEX_ACP_BIN``, project ``node_modules``, mise, PATH).
    """
    from kiro_crew.acp.client import _resolve_codex_acp_bin

    argv, _search = _resolve_codex_acp_bin()
    if not argv:
        return None
    return Path(argv[-1])


_ENTRY = _codex_acp_entry()


@pytest.mark.skipif(not hasattr(os, "getpgid"), reason="POSIX process groups only")
def test_the_driver_runner_reaps_descendants_on_the_timeout_path():
    """The leak the outer bound exists to prevent, driven end to end.

    ``subprocess.run(timeout=...)`` kills the driver alone. The real driver spawns
    codex-acp, which spawns the MCP child -- so on the timeout path a plain
    ``run()`` leaves a Node adapter and a Python MCP server alive with nothing on
    the machine that knows to end them. Stood in for here by a parent that spawns a
    sleeping grandchild and then hangs: the shape is the same and it costs seconds
    instead of a Node install.

    Asserted on the GRANDCHILD, because a parent-only kill is the bug -- the parent
    dies either way.
    """
    parent = r"""
import subprocess, sys, time
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
print(g.pid, flush=True)
time.sleep(300)
"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as w:
        script = Path(w) / "parent.py"
        script.write_text(parent, encoding="utf-8")
        started = time.monotonic()
        result = _run_driver_reaping_group([sys.executable, str(script)], timeout=5)
        # The bound is the control here, and the reap must not add a long second wait.
        assert time.monotonic() - started < 90
        grandchild = int((result.stdout or "").strip().splitlines()[0])

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except OSError:
            break
        time.sleep(0.2)
    else:  # pragma: no cover - the failure this test exists to catch
        try:
            os.kill(grandchild, 9)
        except OSError:
            pass
        pytest.fail(
            f"pid {grandchild} outlived the driver's timeout: the runner killed the "
            "driver but not its process group, so a real run leaks codex-acp and "
            "the MCP child codex-acp itself spawned"
        )


@pytest.mark.skipif(_ENTRY is None, reason="codex-acp not installed")
@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_real_codex_acp_accepts_the_crew_stdio_element():
    """ANTI-DRIFT GUARD, and the measurement the old docstring lacked.

    Four facts, all of which the projection depends on and none of which is
    documented by the adapter:

    1. The element Crew already emits -- ``{"name", "command", "args", "env",
       "type": "stdio"}``, the claude shape unchanged -- is ACCEPTED. ACP v1 spells
       ``McpServer`` as ``serde(tag = "type")`` with stdio as the untagged
       fallback, so nothing guaranteed a ``"stdio"`` tag would fall through to it.
    2. The server is really LAUNCHED and its tools listed: the child answers
       ``initialize`` and then ``tools/list``.
    3. The child inherits ALMOST NOTHING. ``codex-rs`` runs ``env_clear()`` and
       re-adds an allowlist, so ``KIROCREW_SESSION_KEY`` arrives only because the
       element carried it -- which is why ``codex_elements`` carries it.
    4. ``sse`` fails the WHOLE ``session/new``, while a MALFORMED stdio element
       does not. Both halves are load-bearing: the first is why ``codex_elements``
       filters, and the second is why it filters rather than withholding the whole
       array -- a wide reading (``-32602`` for anything unadvertised) argues for
       projecting nothing, and the adapter answers ``-32600``, only for ``sse``.

    A fabricated API key in a throwaway ``CODEX_HOME`` is what gets past the
    adapter's auth check, which fires BEFORE it looks at ``mcpServers`` (verified:
    without it every shape above answers ``-32000 Authentication required``
    identically, so the run would prove nothing). ``session/new`` performs no
    model call, so nothing is sent anywhere and the key never leaves the temp
    directory.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as w:
        root = Path(w)
        (root / "work").mkdir()
        (root / "codex_home").mkdir()
        (root / "codex_home" / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": "sk-not-a-real-key-" + "0" * 24}), encoding="utf-8"
        )
        stub = root / "stub_mcp.py"
        stub.write_text(_STUB_MCP, encoding="utf-8")
        driver = root / "drive.py"
        driver.write_text(_DRIVER, encoding="utf-8")
        result = _run_driver_reaping_group(
            [
                sys.executable,
                str(driver),
                str(root),
                str(_ENTRY),
                str(stub),
                shutil.which("node") or "node",
            ],
            # A BACKSTOP, not the control. The driver bounds each of its three
            # adapter runs itself and reaps in a finally, so its own worst case is
            # well inside this. Reaching it means the driver was killed before it
            # could reap -- which is why the runner kills the whole process group
            # rather than the driver alone.
            timeout=420,
        )
        context = (
            f"driver exit: {result.returncode}\n"
            f"stdout: {result.stdout[-3000:]}\nstderr: {result.stderr[-3000:]}"
        )
        try:
            measured = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            pytest.fail("the codex-acp driver produced no measurement\n" + context)

        if (measured.get("stdio_error") or {}).get("code") == -32000:
            pytest.skip("codex-acp refused the fabricated credential; nothing to measure")

        # 1 + 2: the shape is accepted and the server really runs.
        assert measured["stdio_ok"], (
            "codex-acp rejected the mcpServers element Crew emits, so the codex "
            "projection cannot be delivered in this shape at all\n" + context
        )
        child = measured.get("child")
        assert child, "the stdio MCP server was never launched\n" + context
        assert "tools/list" in child["methods"], (
            "codex-acp launched the server but never listed its tools, so the "
            "session would hold a mounted server with no usable tool\n" + context
        )

        # 3: the env allowlist, which is the whole reason for the carriage rule.
        assert child["env"].get("KIROCREW_SESSION_KEY") == "probe-session-key"
        assert "PATH" in child["env"]
        assert "STUB_MCP_REPORT" in child["env"]

        # 4: what actually costs a session, and what does not.
        assert not measured["sse_ok"], (
            "codex-acp now ACCEPTS an sse element. The drop in codex_elements is "
            "no longer required and should be reconsidered rather than kept as "
            "folklore.\n" + context
        )
        assert measured["sse_error"], "an sse element failed with no error\n" + context
        assert measured["malformed_ok"], (
            "a malformed stdio element now fails the whole session/new. The "
            "translator degrades on a bad spec entry rather than raising, so this "
            "would turn one hand-edited spec line into a dead session.\n" + context
        )
        # The advertisement the client captures and the filter consumes, so this is
        # also the assertion that the two agree on a real adapter.
        assert measured["mcp_capabilities"]["sse"] is False
        assert measured["mcp_capabilities"]["http"] is True

        # 5: the FOLD, which `codex_name` reproduces and the projection's
        # control-plane safety rests on. Two elements were sent, `"probe  one"`
        # (two spaces) and `"probe__one"`. Under codex's per-character rule both
        # register as `probe__one`, so `insert` leaves ONE slot and only one child
        # launches; under a collapsing fold they would be two distinct names and
        # both would. Exactly one child is therefore the measurement that the
        # per-character rule holds -- and that a collapsing fold (which is what
        # manufactured the control-plane collision) is NOT what the adapter does.
        assert measured["fold_session_ok"], "the fold probe's session/new failed\n" + context
        assert len(measured["fold_children"]) == 1, (
            "codex-acp no longer folds whitespace per character: two names that this "
            "rule maps together launched separately, so `codex_name` and the "
            "control-plane collision argument both need re-deriving.\n"
            f"children launched: {measured['fold_children']}\n" + context
        )
        assert codex_name("probe  one") == codex_name("probe__one") == "probe__one"
        assert (
            drop_unadvertised_transports(
                [{"name": "remote", "type": "sse", "url": "http://x/sse", "headers": []}],
                measured["mcp_capabilities"],
            )
            == []
        )


@pytest.mark.skipif(_ENTRY is None, reason="codex-acp not installed")
def test_the_installed_adapter_still_builds_the_frames_the_refusal_reads():
    """The frame VOCABULARY the deny channel keys on, pinned against the adapter.

    Three things identify a codex MCP call to Crew: the ``tool_call`` frame's
    ``rawInput = {server, tool, arguments}`` (``createMcpRawInput``), its
    ``_meta.is_mcp_tool_call`` marker, and the approval request's
    ``_meta.is_mcp_tool_approval`` marker with ``cancel`` as its reject option
    (``buildMcpPermissionRequest`` / ``McpApprovalOptionId``). All three come from
    one builder in codex-acp, so a release that reshapes them blinds the per-call
    refusal, the unidentified-approval refusal and the tripwire together -- the
    coordinated drift the design review names. Observing them on the wire needs a
    model to call a tool; observing them in the adapter's own shipped source does
    not, and the entry the spawn resolves IS that source. Skips where the adapter
    is absent, like its sibling; where it is present, a drift goes red here.
    """
    assert _ENTRY is not None
    source = _ENTRY.read_text(encoding="utf-8", errors="replace")
    for needle in (
        "function createMcpRawInput(server, tool, argumentsValue)",
        "is_mcp_tool_call: true",
        "is_mcp_tool_approval: true",
        'Cancel: "cancel"',
    ):
        assert needle in source, (
            f"codex-acp at {_ENTRY} no longer contains {needle!r}: the frame shape the "
            "spec-restriction refusal identifies an MCP call by has drifted"
        )


def test_the_real_adapter_guard_is_reachable_at_all():
    """A skip-only guard is a guard nobody notices has stopped running.

    This does not assert the adapter is installed -- CI has no codex-acp. It
    asserts the RESOLVER the guard skips on is the spawn's own, so a rename there
    turns the guard permanently green without anyone seeing it.
    """
    from kiro_crew.acp.client import _resolve_codex_acp_bin

    argv, search = _resolve_codex_acp_bin()
    assert argv is None or isinstance(argv, list)
    assert isinstance(search, str)
    assert os.environ.get("CODEX_ACP_BIN") is None or _ENTRY is not None
