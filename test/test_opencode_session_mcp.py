"""The opencode session MCP array: its projection, and the adapter facts it rests on.

Two halves, deliberately in one file. The unit half drives
:mod:`kiro_crew.providers.mirrors.opencode` over a temp agent spec. The
:func:`test_real_opencode_acp_accepts_the_crew_stdio_element` half drives a real
``opencode acp`` and is the anti-drift guard for the whole projection: this backend
had NO projection at all because an adapter fact was inferred from its
``initialize`` result rather than measured, and the inference was wrong. A
measurement that lives beside the code it licenses is the only thing that stops
that happening a second time in the other direction.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.acp import session_mcp
from kiro_crew.acp_backends import (
    ACP_BACKEND_OPENCODE,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
)
from kiro_crew.providers.mirrors import Concern, Disposition, mirror_for
from kiro_crew.providers.mirrors.identity import identity_bound_crew_servers
from kiro_crew.providers.mirrors.opencode import (
    OpenCodeMirror,
    narrowed_control_plane,
    opencode_elements,
    opencode_projection,
    without_stdio_tag,
)

#: What opencode 1.18.30's ``initialize`` actually answered. Two fields, because
#: ACP's ``McpCapabilities`` has exactly two -- which is the whole point: there is
#: no stdio field for a conforming agent to set, so this IS full support.
_OPENCODE_1_18_CAPS = {"http": True, "sse": True}

_CORE = {"command": "/opt/kirocrew", "args": ["mcp-core"]}
_CRON = {"command": "/opt/kirocrew", "args": ["mcp-cron"]}


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """Point the agent-spec resolver at a temp agents directory.

    Same seam as ``test_codex_session_mcp.py``: materialization would rebuild the
    managed default from bundled defaults, and these tests supply the spec.
    """
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
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


# ── the channel this backend was declared not to have ───────────────────────


class TestTheChannelExists:
    def test_opencode_is_in_the_session_array_set_and_has_a_mirror(self):
        """The two registrations that together make the channel real.

        Membership decides whether the translation runs at all
        (``_session_mcp_servers`` returns ``[]`` for a non-member before it ever
        consults the cache); the mirror decides what the array contains. Either one
        alone leaves the session with no Crew tools, which is the state this backend
        shipped in.
        """
        assert ACP_BACKEND_OPENCODE in ACP_BACKENDS_SESSION_MCP_ARRAY
        assert mirror_for(ACP_BACKEND_OPENCODE) is not None

    def test_the_mcp_ruling_is_delivered_and_says_what_refuted_the_old_reason(self):
        """The reason is load-bearing, not decoration.

        The previous declaration read an ABSENT stdio flag as a refusal. If the
        ruling merely said "delivered", the next author meeting the same
        ``{"http": true, "sse": true}`` answer has nothing to stop them re-deriving
        the old conclusion.
        """
        ruling = OpenCodeMirror().rulings()[Concern.MCP_SERVERS]
        assert ruling.disposition is Disposition.DELIVERED
        assert "McpCapabilities" in ruling.reason
        assert "cannot advertise stdio" in ruling.reason

    def test_the_control_plane_reaches_a_session_end_to_end(self, agents_dir):
        """The defect, inverted: ``spawn_run`` and friends have to be THERE."""
        _write_spec(agents_dir, servers={}, tools=["@kirocrew-core", "@kirocrew-cron"])
        names = [e["name"] for e in opencode_projection("kirocrew").params["mcpServers"]]
        assert names == ["kirocrew-core", "kirocrew-cron"]


# ── no transport filter, and why ────────────────────────────────────────────


class TestEveryTransportSurvives:
    """codex needs :func:`drop_unadvertised_transports`; this backend must not have it.

    Measured: ``opencode acp`` ACCEPTS an ``http`` element and an ``sse`` element.
    Dropping them would remove servers the harness would have mounted, with no error
    to explain the absence -- the same class of mistake as delivering one it refuses,
    in the other direction.
    """

    def test_an_sse_server_is_kept(self, agents_dir):
        _write_spec(
            agents_dir,
            servers={"remote": {"url": "https://example.invalid/sse", "type": "sse"}},
            tools=["@remote"],
        )
        element = _by_name(opencode_projection("kirocrew").params["mcpServers"])["remote"]
        assert element["type"] == "sse"
        assert element["url"] == "https://example.invalid/sse"

    def test_an_http_server_is_kept(self, agents_dir):
        _write_spec(
            agents_dir,
            servers={"remote": {"url": "https://example.invalid/mcp"}},
            tools=["@remote"],
        )
        element = _by_name(opencode_projection("kirocrew").params["mcpServers"])["remote"]
        assert element["type"] == "http"

    def test_the_stdio_type_tag_is_removed_rather_than_relied_on(self, agents_dir):
        """The tag is dropped, and the reason is a dependence rather than tidiness.

        The adapter tolerates it only because zod strips unknown keys before its
        union matches the untagged stdio member -- and its mapping then branches on
        whether ``type`` is PRESENT, so a tag that survived would be routed as a
        remote server and throw on its absent ``headers``. Keeping it would make
        every session start depend on that stripping behaviour, and on this harness
        the penalty is the whole ``session/new`` failing with ``-32602``. Both
        shapes are measured working, so the safe one is free.
        """
        _write_spec(agents_dir, servers={"local": {"command": "/bin/x"}}, tools=["@local"])
        element = _by_name(opencode_projection("kirocrew").params["mcpServers"])["local"]
        assert "type" not in element
        # The rest of the element is untouched: this is a removal, not a reshape.
        assert element["command"] == "/bin/x"
        assert element["args"] == [] and element["env"] == []

    def test_a_remote_transport_KEEPS_its_tag(self, agents_dir):
        """Only the stdio tag carries no information.

        For ``http`` and ``sse`` the tag is what the adapter's union matches on, so
        dropping it there would route a remote server down the stdio branch and fail
        it for having no ``command`` -- the very failure the stdio drop avoids.
        """
        _write_spec(
            agents_dir,
            servers={"remote": {"url": "https://example.invalid/sse", "type": "sse"}},
            tools=["@remote"],
        )
        element = _by_name(opencode_projection("kirocrew").params["mcpServers"])["remote"]
        assert element["type"] == "sse"


# ── one bad element costs the array ─────────────────────────────────────────


class TestAMalformedEntryIsSkippedNotSent:
    """The failure mode here is the OPPOSITE of codex's, and it is the worse one.

    codex-acp drops a malformed stdio element and ``session/new`` succeeds.
    ``opencode acp`` answers ``-32602`` for the WHOLE request -- measured on an
    element with neither ``command`` nor ``url``, and on an ``env`` that is a mapping
    rather than an array of pairs. So the shared translator's skip discipline is what
    keeps one hand-edited spec line from costing every other server in the session.
    """

    def test_an_entry_with_no_command_and_no_url_never_reaches_the_array(self, agents_dir):
        _write_spec(
            agents_dir,
            servers={
                "broken": {"args": ["--x"]},
                "fine": {"command": "/bin/x"},
            },
            tools=["@broken", "@fine"],
        )
        names = [e["name"] for e in opencode_projection("kirocrew").params["mcpServers"]]
        assert names == ["fine"]

    def test_a_non_list_args_is_launched_with_none_rather_than_exploded(self, agents_dir):
        """A hand-editable spec writes ``"args": "--flag"``; iterating it per character
        would send one argument per letter, and iterating a number would raise out
        through the projection and fail the whole request."""
        _write_spec(
            agents_dir, servers={"local": {"command": "/bin/x", "args": 8080}}, tools=["@local"]
        )
        element = _by_name(opencode_projection("kirocrew").params["mcpServers"])["local"]
        assert element["args"] == []

    def test_every_element_carries_the_two_arrays_the_schema_requires(self, agents_dir):
        """``env`` and ``args`` are REQUIRED arrays in the adapter's stdio member.

        An element missing either fails the whole ``session/new``, so this asserts
        the shape rather than trusting that the translator happens to fill them.
        """
        _write_spec(
            agents_dir,
            servers={"local": {"command": "/bin/x"}},
            tools=["@local", "@kirocrew-core"],
        )
        for element in opencode_projection("kirocrew", session_key="k").params["mcpServers"]:
            assert isinstance(element.get("args"), list), element
            assert isinstance(element.get("env"), list), element
            assert all(
                isinstance(p, dict) and "name" in p and "value" in p for p in element["env"]
            ), element

    def test_a_non_dict_array_member_is_dropped_by_the_element_pass(self):
        """Defence in depth at this module's own boundary, not just upstream."""
        assert opencode_elements([{"name": "ok", "env": []}, "nope", None]) == [  # type: ignore[list-item]
            {"name": "ok", "env": []}
        ]


# ── identity carriage ───────────────────────────────────────────────────────


class TestIdentityRidesOnlyTheControlPlane:
    """``KIROCREW_SESSION_KEY`` is a credential, so its placement is a security rule.

    The REASON differs from codex's and the difference decides the failure mode.
    codex-rs ``env_clear()``s its stdio children, so without the element env codex's
    control plane has no key at all. This harness's children inherit the ambient
    environment, so dropping the rule here MIS-SCOPES -- the child picks up the
    gateway's key rather than this session's -- which is the harder failure to see.
    """

    def test_the_control_plane_carries_the_session_key(self, agents_dir):
        _write_spec(agents_dir, servers={}, tools=["@kirocrew-core", "@kirocrew-cron"])
        elements = _by_name(
            opencode_projection("kirocrew", session_key="sk-1", channel_id="ch-1").params[
                "mcpServers"
            ]
        )
        for name in ("kirocrew-core", "kirocrew-cron"):
            env = _env(elements[name])
            assert env["KIROCREW_SESSION_KEY"] == "sk-1"
            assert env["KIROCREW_CHANNEL_ID"] == "ch-1"
            assert "KIROCREW_BOUND_PORT" in env

    def test_a_spec_described_server_gets_no_crew_identity(self, agents_dir):
        """An element the spec describes is one whose command, args and env the spec
        chose, so handing it this session's credential would let a hand-edited line
        drive the session it was mounted into."""
        _write_spec(
            agents_dir,
            servers={"third-party": {"command": "/bin/x", "env": {"A": "1"}}},
            tools=["@third-party", "@kirocrew-core"],
        )
        elements = _by_name(
            opencode_projection("kirocrew", session_key="sk-1").params["mcpServers"]
        )
        assert _env(elements["third-party"]) == {"A": "1"}
        assert "KIROCREW_SESSION_KEY" in _env(elements["kirocrew-core"])

    def test_a_third_party_server_NAMED_like_the_control_plane_cannot_steal_the_key(
        self, agents_dir
    ):
        """Provenance, not the name. The two control-plane entries are the ones the
        shared translation REPLACES from ``managed_mcp_spec_entry``, so a spec line
        spelling ``kirocrew-core`` with its own command never survives to receive
        anything -- the managed entry overwrites it."""
        _write_spec(
            agents_dir,
            servers={"kirocrew-core": {"command": "/tmp/evil", "args": ["--x"]}},
            tools=["@kirocrew-core"],
        )
        element = _by_name(
            opencode_projection("kirocrew", session_key="sk-1").params["mcpServers"]
        )["kirocrew-core"]
        assert element["command"] == _CORE["command"]
        assert element["args"] == _CORE["args"]
        assert _env(element)["KIROCREW_SESSION_KEY"] == "sk-1"

    def test_no_identity_is_added_when_the_caller_supplies_none(self, agents_dir):
        """A session with no key must not get an empty-valued one, which would look
        like a credential and authenticate nothing."""
        _write_spec(agents_dir, servers={}, tools=["@kirocrew-core"])
        element = _by_name(opencode_projection("kirocrew").params["mcpServers"])["kirocrew-core"]
        assert "KIROCREW_SESSION_KEY" not in _env(element)

    def test_a_stale_key_on_an_entry_is_replaced_rather_than_duplicated(self):
        """Later wins, and only once: an ACP env array with two pairs of one name is
        a shape whose winner is the adapter's business, not Crew's."""
        element = opencode_elements(
            [
                {
                    "name": "kirocrew-core",
                    "command": "/x",
                    "args": [],
                    "env": [{"name": "KIROCREW_SESSION_KEY", "value": "stale"}],
                }
            ],
            session_key="fresh",
        )[0]
        keys = [p["name"] for p in element["env"]]
        assert keys.count("KIROCREW_SESSION_KEY") == 1
        assert _env(element)["KIROCREW_SESSION_KEY"] == "fresh"


class TestNoNameFolding:
    """codex folds whitespace in a registered name; opencode does not.

    ``mcp.add({directory, name, config})`` takes the name as given. What it does
    sanitise is the TOOL id shown to the model (``sanitize(server) + "_" +
    sanitize(tool)``), a display spelling this projection never compares against. So
    there is no fold to reproduce -- and reproducing one would rename servers the
    harness registers under their original names, making Crew's own session report
    name servers that do not exist.
    """

    def test_a_name_with_whitespace_is_passed_through_unchanged(self, agents_dir):
        _write_spec(agents_dir, servers={"my server": {"command": "/bin/x"}}, tools=["@my server"])
        names = [e["name"] for e in opencode_projection("kirocrew").params["mcpServers"]]
        assert "my server" in names


# ── withholding ─────────────────────────────────────────────────────────────


class TestWhatIsWithheld:
    def test_a_narrowed_third_party_server_is_withheld_whole(self, agents_dir):
        """The codex precedent: this transport has no per-tool deny slot, so the
        faithful options are withhold the server or widen the session behind the
        user's back. An availability cost is the honest price; reachability of a tool
        the user switched off is not."""
        _write_spec(
            agents_dir,
            servers={"narrowed": {"command": "/bin/x", "disabledTools": ["danger"]}},
            tools=["@narrowed"],
        )
        names = [e["name"] for e in opencode_projection("kirocrew").params["mcpServers"]]
        assert "narrowed" not in names

    def test_a_third_party_server_narrowed_only_in_the_global_file_is_withheld(
        self, agents_dir, tmp_path, monkeypatch
    ):
        """The dashboard's ordinary tool-off action writes to the global settings
        file, not to the spec, so a withhold set read from the spec alone would let
        that server mount un-narrowed."""
        settings = tmp_path / "settings-mcp.json"
        settings.write_text(
            json.dumps({"mcpServers": {"narrowed": {"disabledTools": ["danger"]}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", settings)
        _write_spec(agents_dir, servers={"narrowed": {"command": "/bin/x"}}, tools=["@narrowed"])
        names = [e["name"] for e in opencode_projection("kirocrew").params["mcpServers"]]
        assert "narrowed" not in names

    def test_a_narrowed_control_plane_server_is_withheld_too(self, agents_dir):
        """Where this backend parts company with codex, and why.

        codex exempts the control plane from the withholding -- but the exemption was
        never about the wire. ``managed_mcp_spec_entry`` emits only
        command/args/env, so the narrowing reaches no element on either backend; what
        makes keeping the server safe on codex is its SECOND channel, refusing the
        call at the permission request. This harness emits no
        ``rawInput.server``/``tool``, so that channel does not exist -- and carrying
        the exemption across without the mechanism would leave a tool the operator
        switched off on the dashboard REACHABLE, on an ordinary path, with nothing
        saying so.
        """
        _write_spec(
            agents_dir,
            servers={"kirocrew-core": {"command": "/x", "disabledTools": ["spawn_run"]}},
            tools=["@kirocrew-core", "@kirocrew-cron"],
        )
        names = [e["name"] for e in opencode_projection("kirocrew").params["mcpServers"]]
        assert "kirocrew-core" not in names
        # Only the NARROWED one. A blanket "the spec narrows something, drop the whole
        # control plane" would cost a session its channel for an unrelated setting.
        assert "kirocrew-cron" in names

    def test_a_control_plane_narrowed_only_in_the_global_file_is_withheld(
        self, agents_dir, tmp_path, monkeypatch
    ):
        """The path that matters, since it is the one the dashboard writes.

        A tool-off action writes ``disabledTools`` to the global MCP settings file and
        not to the agent spec, so a rule reading the spec alone would miss exactly the
        ordinary case and leave the switched-off tool reachable.
        """
        settings = tmp_path / "settings-mcp.json"
        settings.write_text(
            json.dumps({"mcpServers": {"kirocrew-core": {"disabledTools": ["spawn_run"]}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", settings)
        _write_spec(agents_dir, servers={}, tools=["@kirocrew-core", "@kirocrew-cron"])
        names = [e["name"] for e in opencode_projection("kirocrew").params["mcpServers"]]
        assert "kirocrew-core" not in names
        assert "kirocrew-cron" in names

    def test_an_unnarrowed_control_plane_pays_nothing(self, agents_dir):
        """The cost lands only on an operator who narrowed it deliberately.

        A default install narrows nothing, so the whole control plane is there. Without
        this the withhold rule could quietly become "opencode has no control plane".
        """
        _write_spec(agents_dir, servers={}, tools=["@kirocrew-core", "@kirocrew-cron"])
        names = [e["name"] for e in opencode_projection("kirocrew").params["mcpServers"]]
        assert names == ["kirocrew-core", "kirocrew-cron"]

    def test_the_narrowed_set_is_derived_from_the_pairs_not_the_spec(self, agents_dir):
        """Same parse for both decisions, and control-plane names only.

        ``session_mcp_restricted_servers`` SUBTRACTS the control plane, so this rule
        cannot be folded into it -- and it must not widen to a third-party name that
        set already owns, or the two would disagree about who withheld what.
        """
        assert narrowed_control_plane([("kirocrew-core", "spawn_run")]) == frozenset(
            {"kirocrew-core"}
        )
        assert narrowed_control_plane([("third-party", "x")]) == frozenset()
        assert narrowed_control_plane([]) == frozenset()
        # And the pairs it reads are the ones the restricted set drops.
        assert not session_mcp.session_mcp_restricted_servers(
            [("kirocrew-core", "spawn_run")]
        ) & set(session_mcp.CONTROL_PLANE_SERVERS)

    def test_denied_tools_is_empty_because_this_transport_cannot_match_a_pair(self, agents_dir):
        """A DECISION, pinned so it cannot become an accident.

        The client's per-call refusal identifies an MCP call from codex-acp's
        ``rawInput = {server, tool}``, which this harness does not emit -- it sends a
        fused ``<server>_<tool>`` title and no ``_meta.kiro``. Returning pairs would
        read as an enforced restriction that never matches, which is worse than an
        empty set; the restriction is honoured by withholding instead.
        """
        _write_spec(
            agents_dir,
            servers={"kirocrew-core": {"command": "/x", "disabledTools": ["spawn_run"]}},
            tools=["@kirocrew-core"],
        )
        assert opencode_projection("kirocrew").denied_tools == frozenset()
        reason = OpenCodeMirror().rulings()[Concern.DENIED_TOOLS].reason
        assert "narrowed_control_plane" in reason

    def test_crews_other_managed_servers_are_withheld_rather_than_mounted_unusable(
        self, agents_dir, monkeypatch
    ):
        """A managed Crew server that is not the control plane reaches the array from
        the spec unreplaced, so it would come up bound to no session and answer
        ``not_bound`` to every call -- the present-but-unusable shape this folder
        exists to remove."""
        bound = sorted(identity_bound_crew_servers())
        assert bound, "the identity-bound set is empty, so this test proves nothing"
        name = bound[0]
        _write_spec(agents_dir, servers={name: {"command": "/bin/x"}}, tools=[f"@{name}"])
        names = [e["name"] for e in opencode_projection("kirocrew").params["mcpServers"]]
        assert name not in names

    def test_the_identity_bound_set_never_includes_the_control_plane(self):
        assert not identity_bound_crew_servers() & set(session_mcp.CONTROL_PLANE_SERVERS)


# ── pooled broker stubs ─────────────────────────────────────────────────────


class TestPooledStubs:
    """Once opencode is in ``MIRRORS``, ``_pooled_mcp_servers`` returns ``[]`` for it.

    So this projection IS the stubs' only route in, and the withhold rule has to run
    over both halves here -- a stub carries the SAME name as the entry it rewrites,
    so a name withheld from the translation and re-added as a stub is un-withheld,
    and the stub is the UNRESTRICTED server.
    """

    def test_a_granted_stub_is_appended(self, agents_dir):
        _write_spec(agents_dir, servers={"pooled": {"command": "/bin/x"}}, tools=["@pooled"])
        stub = {"name": "pooled", "command": "/opt/stub", "args": [], "env": []}
        elements = opencode_projection(
            "kirocrew", stub_server_names=("pooled",), stub_elements=[stub]
        ).params["mcpServers"]
        assert _by_name(elements)["pooled"]["command"] == "/opt/stub"

    def test_a_stub_the_allowlist_does_not_grant_is_not_mounted(self, agents_dir):
        """The overlay is written per agent from the GLOBAL settings file too, so it
        can carry a stub for a server this agent's ``tools`` never references."""
        _write_spec(agents_dir, servers={"pooled": {"command": "/bin/x"}}, tools=[])
        stub = {"name": "pooled", "command": "/opt/stub", "args": [], "env": []}
        elements = opencode_projection(
            "kirocrew", stub_server_names=("pooled",), stub_elements=[stub]
        ).params["mcpServers"]
        assert "pooled" not in _by_name(elements)

    def test_a_stub_of_a_withheld_server_does_not_re_add_it(self, agents_dir):
        """The withhold-and-re-add hazard, driven: the stub is the unrestricted
        server, so re-adding it is strictly worse than the mount the projection
        already refused."""
        _write_spec(
            agents_dir,
            servers={"narrowed": {"command": "/bin/x", "disabledTools": ["danger"]}},
            tools=["@narrowed"],
        )
        stub = {"name": "narrowed", "command": "/opt/stub", "args": [], "env": []}
        elements = opencode_projection(
            "kirocrew", stub_server_names=("narrowed",), stub_elements=[stub]
        ).params["mcpServers"]
        assert "narrowed" not in _by_name(elements)

    def test_a_stub_gets_no_crew_identity(self, agents_dir):
        """Stubs are gateway-authored and their env is the broker's own; a
        session-strict server is never pooled, so none of them is the control
        plane."""
        _write_spec(agents_dir, servers={"pooled": {"command": "/bin/x"}}, tools=["@pooled"])
        stub = {"name": "kirocrew-core", "command": "/opt/stub", "args": [], "env": []}
        elements = opencode_projection(
            "kirocrew",
            stub_server_names=("pooled",),
            stub_elements=[stub],
            session_key="sk-1",
        ).params["mcpServers"]
        # Not granted by this spec's `tools`, so it is not mounted at all -- and the
        # assertion that matters is that no stub path adds a credential.
        for element in elements:
            if element.get("command") == "/opt/stub":
                assert "KIROCREW_SESSION_KEY" not in _env(element)

    def test_a_stub_goes_through_the_same_stdio_tag_strip(self, agents_dir):
        """The array is ONE array, so a rule true of half of it is false.

        Broker stubs arrive from ``mcp_gateway.session_servers`` rather than from
        ``acp_server_element``, and today they carry no ``type`` key -- so this
        asserts a property no shipped stub exercises. That is the point: the module
        states that this projection removes the stdio tag, and a stub appended
        verbatim would make that statement true only of the half the mirror builds
        itself, the moment the gateway's entry shape gained one.
        """
        _write_spec(agents_dir, servers={"pooled": {"command": "/bin/x"}}, tools=["@pooled"])
        stub = {
            "name": "pooled",
            "type": "stdio",
            "command": "/opt/stub",
            "args": [],
            "env": [],
        }
        element = _by_name(
            opencode_projection(
                "kirocrew", stub_server_names=("pooled",), stub_elements=[stub]
            ).params["mcpServers"]
        )["pooled"]
        assert element["command"] == "/opt/stub"
        assert "type" not in element
        # The caller's dict is not mutated: the projection copies, so a stub the
        # client reuses for a second session still looks the way it built it.
        assert stub["type"] == "stdio"

    def test_a_remote_stub_keeps_its_tag(self):
        """Same boundary as the translated half: only the stdio tag is meaningless."""
        kept = without_stdio_tag({"name": "r", "type": "sse", "url": "https://x/sse"})
        assert kept["type"] == "sse"

    def test_the_client_hands_the_stubs_down_rather_than_appending_them(self):
        """Structural: the shared append is inert for a mirrored backend, so a mirror
        that ignored ``stub_elements`` would ship a backend the gateway cannot pool
        onto. Pinned at the source because the call site is inside an async setup
        path with no unit-level seam."""
        import inspect

        from kiro_crew.acp import client as client_mod
        from kiro_crew.providers.mirrors import MIRRORS

        assert ACP_BACKEND_OPENCODE in MIRRORS
        pooled = inspect.getsource(client_mod.AcpClient._pooled_mcp_servers)
        assert "self.backend in MIRRORS" in pooled
        resolve = inspect.getsource(client_mod.AcpClient._resolve_session_mcp_servers)
        assert "stub_elements=self._pooled_broker_stubs()" in resolve


# ── the allowlist ───────────────────────────────────────────────────────────


class TestTheToolsAllowlistApplies:
    def test_a_declared_but_unreferenced_server_is_not_mounted(self, agents_dir):
        _write_spec(
            agents_dir,
            servers={"used": {"command": "/bin/x"}, "unused": {"command": "/bin/y"}},
            tools=["@used"],
        )
        names = [e["name"] for e in opencode_projection("kirocrew").params["mcpServers"]]
        assert names == ["used"]

    def test_a_spec_that_drops_the_control_plane_reference_drops_the_server(self, agents_dir):
        """kiro-cli parity: it mounts a server only when ``tools`` names it, and this
        backend must not re-grant what kiro-cli would drop."""
        _write_spec(agents_dir, servers={}, tools=["fs_read"])
        assert opencode_projection("kirocrew").params["mcpServers"] == []


# ── the two faces cannot drift ──────────────────────────────────────────────


class TestTheMirrorFaces:
    def test_the_wire_face_is_the_projections_params(self, agents_dir):
        _write_spec(agents_dir, servers={}, tools=["@kirocrew-core"])
        mirror = OpenCodeMirror()
        assert mirror.session_params("kirocrew") == mirror.session_projection("kirocrew").params

    def test_the_wire_face_does_NOT_fail_closed_on_claudes_precondition(self, agents_dir):
        """``permission_surface_owned`` describes claude's settings file, not this
        harness. Failing closed on it would withhold every Crew tool from every
        opencode session on the strength of a condition that does not apply."""
        _write_spec(agents_dir, servers={}, tools=["@kirocrew-core"])
        params = OpenCodeMirror().session_params("kirocrew", permission_surface_owned=False)
        assert [e["name"] for e in params["mcpServers"]] == ["kirocrew-core"]

    def test_the_mirror_writes_no_files(self, tmp_path):
        """Crew authors no opencode MCP config, and that is a decision rather than an
        omission: ``OPENCODE_CONFIG_CONTENT`` MERGES with the project and user
        config, and declaring one server in both that block and the array
        DOUBLE-MOUNTS it -- both children spawn, because the array does not go
        through config resolution at all."""
        before = sorted(p.name for p in tmp_path.iterdir())
        OpenCodeMirror().write_files("kirocrew", work_dir=tmp_path)
        assert sorted(p.name for p in tmp_path.iterdir()) == before

    def test_the_routing_seed_carries_no_mcp_block(self):
        """The other half of "pick one channel", asserted against the code that
        writes the config Crew DOES seed."""
        import inspect

        from kiro_crew.acp import client as client_mod

        body = inspect.getsource(client_mod.AcpClient._opencode_routing_config)
        assert '"mcp"' not in body
        assert "mcpServers" not in body


# ── the real adapter ────────────────────────────────────────────────────────

# The driver runs OUT OF PROCESS on purpose: it spawns a real harness binary, and a
# stalled readline in the test process would surface as a pytest timeout kill rather
# than the clean assertion failures below.
_DRIVER = r"""
import json, os, queue, subprocess, sys, threading, time

root, opencode_bin, stub = sys.argv[1:4]
report = os.path.join(root, "report.json")


def reap(p):
    # Every exit runs this, including an exception and the outer timeout's SIGTERM:
    # a bare readline() on a harness that has stopped writing blocks forever, and a
    # harness left running keeps its own MCP child alive after this driver is gone.
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


def drive(case, element, watch_report):
    # A private HOME and XDG tree per case, so nothing reads or writes the
    # operator's own opencode configuration.
    work = os.path.join(root, case, "work")
    home = os.path.join(root, case, "home")
    tmp = os.path.join(root, case, "tmp")
    for d in (work, home, tmp):
        os.makedirs(d, exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = home
    env["XDG_CONFIG_HOME"] = os.path.join(home, ".config")
    env["XDG_DATA_HOME"] = os.path.join(home, ".local", "share")
    env["XDG_CACHE_HOME"] = os.path.join(home, ".cache")
    env["NO_BROWSER"] = "1"
    # The harness makes a scratch directory of its own, and it does not remove it.
    # Pointed inside this case's tree so the test's OWN temp root carries it away --
    # otherwise it lands in the shared scratch dir and the suite reports residue
    # against whichever test happened to run last on this worker.
    for name in ("TMPDIR", "TEMP", "TMP"):
        env[name] = tmp
    if watch_report and os.path.exists(report):
        os.unlink(report)
    p = subprocess.Popen(
        [opencode_bin, "acp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, cwd=work, env=env, text=True, bufsize=1,
    )
    try:
        q = queue.Queue()
        threading.Thread(target=pump, args=(p.stdout, q), daemon=True).start()

        def send(o):
            p.stdin.write(json.dumps(o) + "\n")
            p.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": 1, "clientCapabilities": {"fs": {}}}})
        send({"jsonrpc": "2.0", "id": 2, "method": "session/new",
              "params": {"cwd": work, "mcpServers": [element]}})
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
        if watch_report and 2 in got and "result" in got[2]:
            for _ in range(120):
                if os.path.exists(report):
                    break
                time.sleep(0.25)
        return got.get(1) or {}, got.get(2) or {}
    finally:
        reap(p)


# 1 + 2 + 3: the element Crew already emits, its child, and the env it carried.
stdio_el = {
    "name": "kirocrew-core", "type": "stdio", "command": sys.executable,
    "args": [stub],
    "env": [{"name": "STUB_MCP_REPORT", "value": report},
            {"name": "KIROCREW_SESSION_KEY", "value": "probe-session-key"}],
}
init, new = drive("stdio", stdio_el, True)
out = {
    "mcp_capabilities": (init.get("result") or {}).get("agentCapabilities", {}).get(
        "mcpCapabilities"),
    "agent_version": ((init.get("result") or {}).get("agentInfo") or {}).get("version"),
    "stdio_error": new.get("error"),
    "stdio_ok": bool(new.get("result")),
    "child": json.load(open(report)) if os.path.exists(report) else None,
}
if os.path.exists(report):
    os.unlink(report)

# 4: the same element with NO type key -- the ACP untagged stdio member. Measured
# equivalent, which is what licenses leaving the tag on.
untyped = {k: v for k, v in stdio_el.items() if k != "type"}
_, new_untyped = drive("untyped", untyped, True)
out["untyped_ok"] = bool(new_untyped.get("result"))
out["untyped_child"] = json.load(open(report)) if os.path.exists(report) else None
if os.path.exists(report):
    os.unlink(report)

# 5: the remote transports this harness ACCEPTS, so the projection needs no filter.
_, http_new = drive("http", {"name": "remote-http", "type": "http",
                             "url": "http://127.0.0.1:1/mcp", "headers": []}, False)
out["http_ok"] = bool(http_new.get("result"))
_, sse_new = drive("sse", {"name": "remote-sse", "type": "sse",
                           "url": "http://127.0.0.1:1/sse", "headers": []}, False)
out["sse_ok"] = bool(sse_new.get("result"))

# 6: and the shape that costs the WHOLE request, which is why skipping is required.
_, bad = drive("malformed", {"name": "no-command", "args": [], "env": []}, False)
out["malformed_error"] = bad.get("error")
out["malformed_ok"] = bool(bad.get("result"))
_, bad_env = drive("bad-env", {"name": "map-env", "command": sys.executable,
                               "args": [stub], "env": {"A": "1"}}, False)
out["bad_env_ok"] = bool(bad_env.get("result"))
out["bad_env_error"] = bad_env.get("error")
print(json.dumps(out))
"""

# A stdio MCP server small enough to read: it records the environment it was
# LAUNCHED with (which is the measurement) and answers the two methods it is sent.
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
"""


def _run_driver_reaping_group(
    argv: list[str], *, timeout: int, cwd: str
) -> subprocess.CompletedProcess:
    """Run the driver in *cwd* and kill its whole PROCESS GROUP on every exit.

    *cwd* is required rather than defaulted, and it is a temp directory in both
    callers. Every path the driver writes is already absolute under the root it is
    handed, so this changes no file it creates -- what it removes is the standing
    invitation for the next edit to write one relative path and land it in the
    repository checkout, which is where an inherited CWD would have put it.

    Three generations deep: this runner spawns the driver, the driver spawns
    ``opencode acp``, and the harness spawns the MCP child the element names. On the
    timeout path the driver's own ``finally`` never runs, so two generations of
    descendants would outlive the test with nothing on the machine that knows to end
    them. ``start_new_session`` makes the driver a group leader, so ONE tree kill
    reaches every descendant; the kill runs on every exit because a clean reap leaves
    an empty group and makes it a no-op. Routed through
    ``platform_compat.kill_process_tree`` so it is not POSIX-only and keeps the
    broadcast guard. Copied in shape from ``test_codex_session_mcp.py``, whose
    sibling test has the identical three-generation problem.
    """
    from kiro_crew import platform_compat

    proc = subprocess.Popen(
        argv,
        cwd=cwd,
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
            pass

    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            reap()
            try:
                out, err = proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                out, err = "", ""
    finally:
        reap()
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _opencode_bin() -> str | None:
    """The installed harness, through the SPAWN's own resolver.

    Asking ``_resolve_opencode_bin`` rather than ``shutil.which`` is deliberate:
    what this test must exercise is the binary a real session would spawn, on the
    same ladder (``OPENCODE_BIN``, mise, PATH).
    """
    from kiro_crew.acp.client import _resolve_opencode_bin

    resolved, _search = _resolve_opencode_bin()
    return resolved or None


_BIN = _opencode_bin()


@pytest.mark.skipif(_BIN is None, reason="opencode not installed")
def test_real_opencode_acp_accepts_the_crew_stdio_element():
    """ANTI-DRIFT GUARD, and the measurement this whole projection rests on.

    Six facts, none documented by the harness and all of which the mirror depends
    on:

    1. The element Crew already emits -- ``{"name", "command", "args", "env",
       "type": "stdio"}``, the claude shape unchanged -- is ACCEPTED. This is the
       fact whose absence was INFERRED, from an ``initialize`` result advertising
       ``mcpCapabilities`` of ``http`` and ``sse``. ACP's ``McpCapabilities`` has
       exactly those two boolean fields and no ``stdio`` field, so no conforming
       agent can advertise stdio and the absence was never evidence.
    2. The server is really LAUNCHED and its tools listed: the child answers
       ``initialize`` and then ``tools/list``. Acceptance alone would leave a
       session holding a mounted server with no usable tool.
    3. The element's ``env`` REACHES the child, which is what makes
       ``KIROCREW_SESSION_KEY`` on the element the thing that scopes Crew's control
       plane to this session rather than to the gateway.
    4. Dropping the ``type`` key is EQUIVALENT, which is what licenses leaving the
       tag on rather than adding a second element shape to the shared translator.
       The tag is tolerated and discarded, not honoured -- so nothing may read
       meaning into it.
    5. ``http`` and ``sse`` elements are BOTH accepted, which is why this backend
       needs no analogue of codex's transport filter. A filter here would remove
       servers the harness would have mounted.
    6. A MALFORMED element fails the WHOLE ``session/new``, in BOTH the shapes a
       hand-edited spec produces -- no ``command``, and an ``env`` that is a mapping
       rather than an array of pairs. This is the opposite of codex, which drops the
       element and succeeds, and it is why ``acp_server_element``'s skip discipline
       is load-bearing rather than tidy.

    No credential and no model are needed: ``initialize`` and ``session/new`` make
    no model call, so nothing is sent anywhere. Every case runs under its own
    ``HOME`` and ``XDG_*`` tree, so the operator's own opencode configuration is
    neither read nor written.

    Skips cleanly where the harness is absent, which is CI. Its companion
    :func:`test_the_real_adapter_guard_is_reachable_at_all` is what keeps the skip
    from becoming permanent silence.
    """
    assert _BIN is not None
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as w:
        root = Path(w)
        stub = root / "stub_mcp.py"
        stub.write_text(_STUB_MCP, encoding="utf-8")
        driver = root / "drive.py"
        driver.write_text(_DRIVER, encoding="utf-8")
        result = _run_driver_reaping_group(
            [sys.executable, str(driver), str(root), _BIN, str(stub)],
            # A BACKSTOP, not the control: the driver bounds each of its six harness
            # runs itself and reaps in a finally, so its own worst case is well
            # inside this. Reaching it means the driver was killed before it could
            # reap -- which is why the runner kills the whole process group.
            timeout=900,
            cwd=str(root),
        )
        context = (
            f"driver exit: {result.returncode}\n"
            f"stdout: {result.stdout[-3000:]}\nstderr: {result.stderr[-3000:]}"
        )
        try:
            measured = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            pytest.fail("the opencode acp driver produced no measurement\n" + context)

    # 1 + 2 + 3
    assert measured["stdio_ok"], (
        "opencode acp rejected the mcpServers element Crew emits, so the opencode "
        "projection cannot be delivered in this shape at all -- and the mirror's "
        "whole premise is gone rather than one server being missing\n" + context
    )
    child = measured.get("child")
    assert child, "the stdio MCP server was never launched\n" + context
    assert "tools/list" in child["methods"], (
        "opencode acp launched the server but never listed its tools, so the session "
        "would hold a mounted server with no usable tool\n" + context
    )
    assert child["env"].get("KIROCREW_SESSION_KEY") == "probe-session-key", (
        "the element's env did not reach the child, so nothing scopes Crew's control "
        "plane to this session\n" + context
    )

    # 4
    assert measured["untyped_ok"], "the ACP untagged stdio member was refused\n" + context
    assert (measured.get("untyped_child") or {}).get("methods"), (
        "the untyped element was accepted but launched nothing, so the two shapes are "
        "NOT equivalent and the projection must stop assuming they are\n" + context
    )

    # 5
    assert measured["http_ok"] and measured["sse_ok"], (
        "opencode acp now REFUSES an http or sse element. The absent transport filter "
        "in the opencode mirror is no longer safe: one such entry would cost the "
        "session every other server, so codex's drop_unadvertised_transports has to "
        "be applied here after all.\n" + context
    )

    # 6
    assert not measured["malformed_ok"], (
        "a malformed stdio element no longer fails the whole session/new on opencode. "
        "That is a relaxation, not a break -- but the skip discipline is justified in "
        "the mirror BY this fact, so the docstring must be re-derived rather than "
        "left claiming a consequence the harness no longer has.\n" + context
    )
    assert measured["malformed_error"], "a malformed element failed with no error\n" + context
    assert measured["malformed_error"].get("code") == -32602, (
        "the malformed element failed with a code other than -32602; the mirror's "
        f"prose names that code\nerror: {measured['malformed_error']}\n" + context
    )
    assert not measured["bad_env_ok"], (
        "an env given as a mapping rather than an ACP array of pairs is now accepted. "
        "acp_server_element always emits the array form, so this is the shape a "
        "future translator change would break the whole session with.\n" + context
    )

    # The advertisement, pinned. A future opencode that starts rejecting stdio would
    # most likely also change what it advertises, and this is the frame the old
    # no-channel claim was read out of -- so it is asserted rather than trusted.
    caps = measured["mcp_capabilities"]
    assert caps == _OPENCODE_1_18_CAPS, (
        "opencode's initialize mcpCapabilities has changed shape. The projection does "
        "not consult it (there is no transport filter here), but the no-channel claim "
        "this mirror removed WAS read out of this frame -- so a change is a signal to "
        f"re-measure rather than to re-infer.\nadvertised: {caps}\n" + context
    )
    assert "stdio" not in caps, (
        "opencode now advertises a stdio capability, which ACP's McpCapabilities has "
        "no field for -- re-read the schema before trusting either answer"
    )


def test_the_real_adapter_guard_is_reachable_at_all():
    """A skip-only guard is a guard nobody notices has stopped running.

    This does not assert the harness is installed -- CI has no opencode. It asserts
    the RESOLVER the guard skips on is the spawn's own, so a rename there turns the
    guard permanently green without anyone seeing it.
    """
    from kiro_crew.acp.client import _resolve_opencode_bin

    resolved, search = _resolve_opencode_bin()
    assert resolved is None or isinstance(resolved, str)
    assert isinstance(search, str)
    assert os.environ.get("OPENCODE_BIN") is None or _BIN is not None


@pytest.mark.skipif(not hasattr(os, "getpgid"), reason="POSIX process groups only")
def test_the_driver_runner_reaps_descendants_on_the_timeout_path():
    """The leak the outer bound exists to prevent, driven end to end.

    ``subprocess.run(timeout=...)`` kills the driver alone. The real driver spawns
    ``opencode acp``, which spawns the MCP child -- so on the timeout path a plain
    ``run()`` leaves a harness process and a Python MCP server alive with nothing on
    the machine that knows to end them. Stood in for here by a parent that spawns a
    sleeping grandchild and then hangs: the shape is the same and it costs seconds
    instead of a node install.

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
        result = _run_driver_reaping_group([sys.executable, str(script)], timeout=5, cwd=w)
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
            "driver but not its process group, so a real run leaks opencode acp and "
            "the MCP child opencode itself spawned"
        )


def test_the_driver_and_stub_are_syntactically_valid_python():
    """The two embedded programs are strings, so nothing else compiles them.

    A typo in either reaches CI as a driver that "produced no measurement", which
    reads as an absent harness and skips. This is the cheap half of that guard and it
    runs everywhere.
    """
    import ast

    ast.parse(_DRIVER)
    ast.parse(_STUB_MCP)
