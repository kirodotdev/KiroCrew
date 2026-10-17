"""Codex's spec projection as unit behaviour: the agent spec -> a ``session/new`` array.

The unit half of ``test_codex_session_mcp.py``, which states the two rules
``providers/mirrors/codex.py`` adds and keeps their measurement against an installed
``codex-acp``. Here: the transport filter, the identity carriage on Crew's own servers,
codex's name folding, the mirror's declared rulings, the session array seam, the
runtime path that builds that array through the mirror, and the dashboard grant.
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
    """The filter reads THIS session's advertisement, not a remembered one.

    Keyed on ``initialize``'s ``mcpCapabilities`` rather than on a constant: an
    adapter release that gains ``sse`` or drops ``http`` would make a hardcoded set
    silently wrong. Being wrong here is invisible rather than loud: codex-acp
    ACCEPTS an element whose transport it declares unsupported, answering
    ``session/new`` with an ordinary ``sessionId`` and never wiring that server, so
    this filter is the only thing keeping the array Crew sends equal to the array
    the adapter honours.
    """

    def test_an_unadvertised_transport_is_dropped_before_it_is_sent(self):
        """The element leaves the array here, which is the only place it can.

        codex-acp does NOT refuse an ``sse`` element it declares unsupported: it
        answers ``session/new`` normally and leaves that server unwired, so nothing
        downstream reports the mismatch. Dropping it here is what keeps the array
        Crew sends equal to the array the adapter honours, and the advertised
        elements beside it are untouched.
        """
        kept = drop_unadvertised_transports(
            [
                {"name": "remote", "type": "sse", "url": "https://x/sse", "headers": []},
                {"name": "local", "type": "stdio", "command": "/bin/x", "args": [], "env": []},
            ],
            _CODEX_1_11_CAPS,
        )
        assert [e["name"] for e in kept] == ["local"]

    def test_a_MEANINGLESS_transport_type_is_dropped_TOO(self):
        """The adapter validates the ``type`` field at all, so the filter must.

        A deliberately meaningless ``{"type": "nonsense-type"}`` element is accepted
        by codex-acp exactly as an unadvertised ``sse`` one is: ``session/new``
        answers with a ``sessionId`` and the server is never wired. Keeping only
        transports that were POSITIVELY advertised is what covers both, where a
        denylist of known-bad spellings would cover neither.
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

        Anything else with no positive claim behind it risks being accepted and
        left unwired, so a session whose handshake has not been read yet keeps the
        one transport that cannot be refused.
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


# ── the session array seam ──────────────────────────────────────────────────


class TestTheSessionArraySeam:
    """What a codex ``session/new`` is handed, and who decides each part of it.

    The array is the MIRROR's -- both halves of it, the spec translation and the
    pooled broker stubs -- and the transport narrowing that follows is the
    HARNESS's, reading the handshake this session captured. These drive that pair,
    plus the source-level pins that keep the split where it is.
    """

    def test_codex_is_in_both_the_array_set_and_member_dispatch(self):
        """The two sets this session's array depends on.

        Without the array set the session gets ``[]`` however good the mirror is.
        Member dispatch is mounted onto that same array, but by the RUNTIME rather
        than by this projection -- ``AcpRuntime.create_session`` appends the entry
        after the mirror has run, because the dashboard server is identity-bound and
        ``codex_withheld_servers`` therefore keeps the SPEC-described spelling of it
        out of the translation below. Which sessions get that append is decided by
        ``AcpProvider._member_session_key``, pinned in ``test_member_dispatch_mount``.
        """
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_SESSION_MCP_ARRAY
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_MEMBER_DISPATCH

    def test_the_spec_can_never_supply_the_dashboard_server_itself(self, agents_dir):
        """Membership adds no way for the agent file to mount session control.

        An agent spec that names ``@kirocrew-dashboard`` still gets it withheld: a
        spec-described element carries no session identity and would answer
        ``identity_unattested`` to every verb. So the only dashboard entry a codex
        session can hold is the one the runtime builds with this session's key, and
        adding codex to the dispatch set does not un-withhold the other kind.
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
        """The fail-open consequence, pinned where the guard could be lost.

        An element whose transport the adapter declares unsupported produces a
        NORMAL session: ``session/new`` answers with a ``sessionId`` and the server
        is simply never wired, so there is no error for a later stage to notice and
        no later stage removes the element either. This asserts both halves -- the
        spec parse carries the ``sse`` server through untouched, and the narrowing
        at the ``session/new`` site is the single place it leaves the array. Losing
        that call does not break a session; it buys one that silently holds a server
        with no tools.
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
        """The same spec, two advertisements, two answers.

        This is what a hardcoded unsupported-transport set could not do, and the
        reason the WATCH on that constant was legitimate: the code follows the
        adapter instead of following one measurement of it.
        """
        _write_spec(
            agents_dir,
            servers={"remote": {"url": "https://x/sse", "type": "sse"}},
            tools=["@remote"],
        )
        assert "remote" not in _by_name(_codex_session_array(caps=_CODEX_1_11_CAPS)[1])
        advertised = {"acp": False, "http": True, "sse": True}
        assert "remote" in _by_name(_codex_session_array(caps=advertised)[1])

    def test_a_codex_session_needs_no_claude_settings_file(self, agents_dir):
        """Codex's array is not conditional on a file no codex session has.

        Claude withholds its whole array unless Crew authored
        ``settings.local.json``, because a ``permissions.allow`` in a file Crew does
        not own pre-approves a call and Crew's gate never fires. Copying that
        condition here would withhold every Crew tool from every codex session on
        the strength of something that does not describe the backend: codex's
        asking is asserted per session and enforced, so there is no file to own.

        ``False`` is the value a real session is built with -- the runtime authors no
        native permission file and says so to every mirror -- and claude's answer to
        the same argument is asserted beside it, so this is a decision rather than a
        default nobody exercises.
        """
        _write_spec(agents_dir, servers={}, tools=["@kirocrew-core"])
        codex = CodexMirror().session_projection("kirocrew", permission_surface_owned=False)
        assert "kirocrew-core" in _by_name(codex.params["mcpServers"])
        claude = mirror_for(ACP_BACKEND_CLAUDE)
        assert claude is not None
        withheld = claude.session_projection("kirocrew", permission_surface_owned=False)
        assert withheld.params["mcpServers"] == []

    def test_a_server_narrowed_per_tool_is_withheld_end_to_end(self, agents_dir):
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
        names = set(_by_name(_codex_session_array(caps=_CODEX_1_11_CAPS)[1]))
        assert "kirocrew-work" not in names
        assert "kirocrew-core" in names

    def test_the_control_plane_is_never_withheld_by_its_own_disabled_tools(self, agents_dir):
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
        assert "kirocrew-core" in _by_name(_codex_session_array(caps=_CODEX_1_11_CAPS)[1])

    def test_the_pooled_resolution_cannot_run_beside_the_projection(self):
        """A stub wraps the SAME name, so an unnarrowed append un-withholds it.

        The stub is the UNRESTRICTED server, which is the worse of the two, so the
        pooled half of the array goes through the same withholding rules -- which is
        why the projection places the stubs itself (asserted just below, and again on
        the runtime path). What has to hold beside that is that the RAW pooled
        resolution cannot also run: a session that took the projection and then
        appended ``pooled_session_servers`` would re-add every name the projection
        withheld.

        Structural, because the two are one ``if``/``else`` on the shared session
        construction path and a behavioural test only ever observes the branch it
        took. Read from the tree rather than the text, so a mention in a comment or a
        docstring is not mistaken for a call.
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

        With one parse there is no second read to disagree with, so the projection
        either withholds the server (it saw the narrowing) or translates it from
        bytes that did not narrow it -- never the mismatch where the withhold set
        comes from one revision of the file and the array from another.
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
        """H13: the shared ``session/new`` site must not put a disk read on the loop.

        The blocking half is the agent-spec parse and the overlay read inside the
        projection, plus the spec snapshot the unresolved-ref guard judges against,
        and all of it runs in ONE thread: the mirrored path hands a single function
        to ``asyncio.to_thread`` and every ``session_projection`` /
        ``_ref_spec_snapshot`` call in the method sits inside that function's body.
        Read from the tree, so a call that migrated out of the hop -- inline on the
        loop, or into a second hop of its own -- is seen wherever it lands. The
        narrowing that follows reads this session's captured handshake and nothing
        else, so it stays synchronous and pure.

        Pinned at the source because both sites sit inside an async construction path
        with no unit-level seam, and one process hosts every session: a read landing
        on the loop stalls all of them.
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

    This is what replaced ``_refuse_unprojected_pooled_servers``. That gate made the
    unprojected state unreachable by refusing the session outright, which cost the
    preview every pooled server; these tests assert the state the gate stood in for is
    unreachable because the projection now runs here too.
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
        """Condition 5(d): the allowlist that filtered the translated half filters stubs.

        The overlay is written per agent from the GLOBAL settings file too, so it can
        carry a stub for a server this agent's ``tools`` never names. On the old
        runtime path that stub went straight to ``session/new``, and codex approves
        its own tools -- a live tool surface Crew never granted.
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
        """Condition 5(c)'s input: the pairs the runtime hands the session handle.

        Derived on the SAME parse as the array, so it cannot name a tool on a spec
        revision the array never saw.
        """
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
        caller runs the pooled path it always ran -- no new conditional and no new
        failure mode on the shared construction path (H13)."""
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
