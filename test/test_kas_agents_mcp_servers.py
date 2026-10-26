"""Kiro Crew agent spec -> KAS ``ClientCustomAgent`` projection: the agent's MCP servers.

Covers which declared servers, and which fields of each entry, the ``mcpServers``
block forwards or withholds (credential-bearing ``env`` and ``headers``,
``autoApprove``, muted entries, third-party entries the registry filter governs,
stubbed names), the discarded restrictions the projection reports, the runtime seam
that hands the projection the stubbed names read from the gateway overlay, the bound
port and session key managed servers carry, and the hoist of managed servers into
the session-level array. ``_kas_wire_maps`` transcribes KAS's own mapper. The
shared ``_spec`` builder lives in ``kas_agents_helpers.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from kas_agents_helpers import _spec

from kiro_crew.acp import kas_agents
from kiro_crew.acp.kas_agents import hoist_servers_for_session, to_client_custom_agent


@pytest.fixture
def relayed_names(monkeypatch):
    """An edition that relays ``LAUNCHER_BROKER_URL`` through the seam."""
    from kiro_crew.platform.defaults import DefaultMcpToolingProvider

    monkeypatch.setattr(
        DefaultMcpToolingProvider,
        "kas_relayed_env_references",
        lambda self: frozenset({"LAUNCHER_BROKER_URL", 7}),
        raising=False,
    )


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

    def test_a_relayed_reference_reaches_a_third_party_server(self, relayed_names):
        """KAS starts an MCP child with a minimal environment of its own, so a
        launcher's per-process value reaches the child only through a reference
        to a name the edition relays. The literal beside it, and a reference
        wrapped in literal text, do not."""
        out = to_client_custom_agent(
            "a",
            _spec(
                mcpServers={
                    "vendor-proxy": {
                        "command": "vendor",
                        "env": {
                            "BROKER_URL": "${LAUNCHER_BROKER_URL}",
                            "LAUNCH_NONCE": "n0nce",
                            "AUTH": "Bearer ${SECRET}",
                        },
                    }
                }
            ),
            "p",
        )
        assert out["mcpServers"]["vendor-proxy"]["env"] == {"BROKER_URL": "${LAUNCHER_BROKER_URL}"}

    def test_the_public_default_relays_no_reference(self):
        """With no edition listing a name, a bare reference is withheld like a
        literal: the public build relays nothing from the KAS environment."""
        out = to_client_custom_agent(
            "a",
            _spec(
                mcpServers={
                    "vendor-proxy": {"command": "x", "env": {"U": "${LAUNCHER_BROKER_URL}"}}
                }
            ),
            "p",
        )
        assert out["mcpServers"]["vendor-proxy"] == {"command": "x"}

    def test_a_third_party_env_of_only_literals_leaves_no_env(self):
        out = to_client_custom_agent(
            "a",
            _spec(mcpServers={"third-party": {"command": "x", "env": {"API_KEY": "sk-live"}}}),
            "p",
        )
        assert out["mcpServers"]["third-party"] == {"command": "x"}

    def test_a_managed_server_keeps_a_reference_beside_its_home(self, relayed_names):
        out = to_client_custom_agent(
            "a",
            _spec(
                mcpServers={
                    "kirocrew-core": {
                        "command": "x",
                        "env": {
                            "KIROCREW_HOME": "/h",
                            "REF": "${LAUNCHER_BROKER_URL}",
                            "LIT": "v",
                        },
                    }
                }
            ),
            "p",
        )
        assert out["mcpServers"]["kirocrew-core"]["env"] == {
            "KIROCREW_HOME": "/h",
            "REF": "${LAUNCHER_BROKER_URL}",
        }

    @pytest.mark.parametrize(
        "value",
        [
            "$LAUNCHER_BROKER_URL",
            "${LAUNCHER_BROKER_URL",
            "${LAUNCHER_BROKER_URL}${LAUNCHER_BROKER_URL}",
            " ${LAUNCHER_BROKER_URL}",
            "${KIRO_API_KEY}",
            "${KIROCREW_SESSION_KEY}",
            "${GITHUB_TOKEN}",
            7,
            None,
        ],
    )
    def test_only_an_exact_relayed_reference_counts(self, value, relayed_names):
        out = to_client_custom_agent(
            "a",
            _spec(mcpServers={"third-party": {"command": "x", "env": {"K": value}}}),
            "p",
        )
        assert "env" not in out["mcpServers"]["third-party"]

    @pytest.mark.parametrize(
        "value",
        [
            "${KIRO_API_KEY}",
            "/h/${KIRO_API_KEY}",
            "${LAUNCHER_BROKER_URL}",
            ["${KIRO_API_KEY}"],
            {"p": "/h"},
            7,
        ],
    )
    def test_a_managed_kept_key_cannot_carry_a_reference(self, value, relayed_names):
        """``KIROCREW_HOME`` survives on a managed server only as a literal; KAS
        expands a reference anywhere in the value, so one here would hand the
        CLI credential to whatever command the hand-edited entry names."""
        out = to_client_custom_agent(
            "a",
            _spec(mcpServers={"kirocrew-core": {"command": "x", "env": {"KIROCREW_HOME": value}}}),
            "p",
        )
        assert "KIROCREW_HOME" not in out["mcpServers"]["kirocrew-core"].get("env", {})

    def test_a_server_that_lost_a_literal_is_still_logged(self, caplog, relayed_names):
        with caplog.at_level("INFO"):
            to_client_custom_agent(
                "a",
                _spec(
                    mcpServers={
                        "third-party": {
                            "command": "x",
                            "env": {"REF": "${LAUNCHER_BROKER_URL}", "LIT": "sk-live"},
                        }
                    }
                ),
                "p",
            )
        relayed = [r.getMessage() for r in caplog.records if "not relaying" in r.getMessage()]
        assert relayed and "env" in relayed[0] and "sk-live" not in relayed[0]

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


def _kas_wire_maps(entry: dict) -> dict:
    """What KAS's own mapper makes of one projected entry.

    Transcribed from ``mapClientMcpServers`` (``resolve-client-agents.ts``): a
    stdio entry is rebuilt from ``command``/``args``/``env``/``timeout``, a remote
    one from ``url``/``headers``/``env``/``timeout``, and an entry with neither is
    dropped. Everything else the wire schema accepted is thrown away here.

    PROVENANCE, because everything the projection withholds rests on it:
    transcribed from ``packages/kiro-agent/src/services/custom-agents/
    resolve-client-agents.ts`` on ``kiro-team/kiro-agent``'s ``main``. It is a
    reading of source, NOT a live measurement -- no KAS process is startable from
    this repo's test environment -- so an upstream change makes this fixture wrong
    before it makes the projection wrong, which is why it is pinned here rather
    than left implicit. The host-contract row records the same, with the date.

    When kiro-agent starts copying these fields through, THIS fixture is what goes
    red. Two Crew-side behaviours become removable at that point, not one: the mute
    handled by omission (``TestMutedServersAreNotDeclared``) AND the withhold of a
    third-party registry entry under registry mode
    (``TestRegistryGovernedEntries``), which exists only because the marker cannot
    reach the host's own filter. The ``TODO(kiro-agent)`` beside
    ``_KAS_DISCARDED_ENTRY_KEYS`` names the upstream change.
    """
    if entry.get("command"):
        keys = ("command", "args", "env", "timeout")
    elif entry.get("url"):
        keys = ("url", "headers", "env", "timeout")
    else:
        return {}
    return {k: entry[k] for k in keys if k in entry}


#: Keys ``ClientAgentMcpServerSchema`` declares. A key outside this set is
#: stripped by the zod object before the mapper above ever runs -- which is
#: ``type``'s fate, and a different mechanism from being accepted then discarded.
_KAS_WIRE_SCHEMA_KEYS = frozenset(
    {
        "command",
        "args",
        "env",
        "cwd",
        "url",
        "headers",
        "disabled",
        "autoApprove",
        "disabledTools",
        "timeout",
    }
)


class TestTheBackendLosesBothFields:
    """Why the projection withholds instead of just forwarding the fields.

    Pins the upstream shape the two behaviours below are a response to, so a
    reader does not have to take the workaround on faith -- and so the day
    kiro-agent fixes its mapper, these are the assertions that fail and say which
    Crew-side handling is now redundant.
    """

    def test_the_registry_marker_has_no_slot_at_all(self):
        assert "type" not in _KAS_WIRE_SCHEMA_KEYS

    def test_the_mute_is_accepted_and_then_discarded(self):
        assert "disabled" in _KAS_WIRE_SCHEMA_KEYS
        assert _kas_wire_maps({"command": "x", "disabled": True}) == {"command": "x"}

    def test_a_marked_entry_arrives_indistinguishable_from_an_unmarked_one(self):
        assert _kas_wire_maps({"command": "x", "type": "registry"}) == {"command": "x"}

    def test_the_named_discards_are_the_ones_the_mapper_drops(self):
        entry = {k: "v" for k in _KAS_WIRE_SCHEMA_KEYS}
        entry["command"] = "x"
        survived = set(_kas_wire_maps(entry))
        for key in kas_agents._KAS_DISCARDED_ENTRY_KEYS:
            assert key not in survived, f"{key} is not discarded after all"


class TestMutedServersAreNotDeclared:
    """``disabled: true`` is the user's own decision and the backend discards it.

    Omitting the declaration is the only way to express "do not launch this" that
    survives the wire, and it is faithful rather than lossy: a server the backend
    was never told about does not run, which is exactly what the flag asks for.
    """

    def test_a_muted_server_is_withheld(self):
        out = to_client_custom_agent(
            "a",
            _spec(
                mcpServers={"muted": {"command": "x", "disabled": True}, "live": {"command": "y"}}
            ),
            "p",
        )
        assert out["mcpServers"] == {"live": {"command": "y"}}

    def test_forwarding_it_would_have_launched_the_server(self):
        """The round trip, stated as one assertion: declared means launched."""
        entry = {"command": "x", "disabled": True}
        assert _kas_wire_maps(entry) == {"command": "x"}, "the mute does not survive"
        out = to_client_custom_agent("a", _spec(mcpServers={"muted": entry}), "p")
        assert "mcpServers" not in out, "so the entry must not be declared"

    def test_crews_own_managed_server_is_not_exempt(self):
        """The mute is about a server the user can un-mute, so it costs nothing to
        honour -- and honouring it for third parties only would mean a user who
        silenced ``kirocrew-work`` still got it."""
        out = to_client_custom_agent(
            "a", _spec(mcpServers={"kirocrew-core": {"command": "x", "disabled": True}}), "p"
        )
        assert "mcpServers" not in out

    @pytest.mark.parametrize("value", ["true", "false", 1, 0, {}, [], None], ids=repr)
    def test_a_non_boolean_value_is_read_as_a_mute_not_coerced(self, value):
        """Fail closed, for two reasons at once. ``disabled: z.boolean()`` rejects
        a non-boolean, and a client agent that fails the schema is dropped WHOLE
        -- Crew injects exactly one agent, so forwarding this costs the session
        its entire configuration, not one server."""
        out = to_client_custom_agent(
            "a", _spec(mcpServers={"odd": {"command": "x", "disabled": value}}), "p"
        )
        assert "mcpServers" not in out

    def test_an_explicit_false_still_declares_the_server(self):
        out = to_client_custom_agent(
            "a", _spec(mcpServers={"live": {"command": "x", "disabled": False}}), "p"
        )
        assert out["mcpServers"]["live"]["command"] == "x"

    def test_the_withhold_is_explained(self, caplog):
        with caplog.at_level("INFO", logger="kiro_crew.acp.kas_agents"):
            to_client_custom_agent(
                "kirocrew", _spec(mcpServers={"muted": {"command": "x", "disabled": True}}), "p"
            )
        assert "muted" in caplog.text
        assert "would launch the server anyway" in caplog.text

    def test_a_bad_type_is_explained_as_a_bad_type(self, caplog):
        with caplog.at_level("INFO", logger="kiro_crew.acp.kas_agents"):
            to_client_custom_agent(
                "kirocrew", _spec(mcpServers={"odd": {"command": "x", "disabled": "yes"}}), "p"
            )
        assert "is not a boolean" in caplog.text

    def test_the_spec_is_not_mutated(self):
        spec = _spec(mcpServers={"muted": {"command": "x", "disabled": True}})
        to_client_custom_agent("a", spec, "p")
        assert spec["mcpServers"]["muted"] == {"command": "x", "disabled": True}


class TestAMutedServerCannotArriveThroughThePoolingStub:
    """The mute has to hold on the path that does NOT go through this block.

    A stubbed name is subtracted from the projection before any check here, so the
    projection alone cannot honour a mute on a pooled server -- the decision has to
    be the same one the gateway rewriter makes when it chooses to wrap. Both now
    read ``mcp_entry_is_muted``, and this drives the real chain rather than
    asserting the predicate twice.
    """

    @staticmethod
    def _stub_names(spec: dict, tmp_path: Path) -> frozenset[str]:
        """The names the gateway would inject at session level for *spec*.

        Derived the way ``session_servers.injection_server_names`` derives them --
        from the wrapper marker the rewriter leaves -- so a change in what the
        rewriter wraps changes this answer.
        """
        from kiro_crew.mcp_gateway import rewriter

        rewritten, _ = rewriter._rewrite_single_spec(
            dict(spec),
            stubs_dir=tmp_path / "stubs",
            socket_path=tmp_path / "gw.sock",
            work_dir=tmp_path,
            sandbox_mode="off",
            approval_mode="",
            stub_servers=frozenset(spec.get("mcpServers", {})),
        )
        return frozenset(
            name
            for name, entry in rewritten["mcpServers"].items()
            if entry.get(rewriter._WRAPPER_MARKER) or entry.get(rewriter._WRAPPER_MARKER_LEGACY)
        )

    @pytest.mark.parametrize("value", [True, "true", 1], ids=repr)
    def test_a_muted_poolable_server_is_never_stubbed_and_so_is_withheld(
        self, value: object, tmp_path: Path
    ):
        spec = _spec(mcpServers={"muted": {"command": sys.executable, "disabled": value}})
        stubbed = self._stub_names(spec, tmp_path)

        assert stubbed == frozenset(), "a muted server must not become a live stub"
        out = to_client_custom_agent("kirocrew", spec, "p", stub_server_names=stubbed)
        assert "mcpServers" not in out

    def test_a_registry_governed_poolable_server_is_never_stubbed_either(self, tmp_path: Path):
        """The peer of the mute case, and the same escape.

        A catalog-governed entry an operator lists for pooling was wrapped by the
        rewriter (which had no reading of the marker at all), became a stubbed
        name, and was subtracted here before the marker guard -- so it reached the
        session as a live local process that kiro-cli would have refused.
        """
        spec = _spec(
            mcpServers={"governed": {"command": sys.executable, "type": "registry"}},
            tools=["@governed"],
        )
        stubbed = self._stub_names(spec, tmp_path)

        assert stubbed == frozenset(), "a catalog-governed server must not become a stub"
        out = to_client_custom_agent("kirocrew", spec, "p", stub_server_names=stubbed)
        assert "mcpServers" not in out

    def test_a_withhold_outranks_the_stub_subtraction(self, caplog):
        """Ordering, pinned on the one thing the two orders differ on: the REASON.

        Both orders leave the block empty for a stubbed name, so an
        absent-``mcpServers`` assertion passes either way and pins nothing. What
        subtract-first loses is the refusal itself: the name is handed to the
        injection silently, and nothing records that this entry was muted or
        catalog-governed. Withhold-first says so, which is what makes a server
        that does not appear in the session explicable.
        """
        spec = _spec(
            mcpServers={
                "muted": {"command": "x", "disabled": True},
                "governed": {"command": "y", "type": "registry"},
            }
        )
        with caplog.at_level("INFO", logger="kiro_crew.acp.kas_agents"):
            out = to_client_custom_agent(
                "kirocrew", spec, "p", stub_server_names=frozenset({"muted", "governed"})
            )

        assert "mcpServers" not in out
        assert "not declaring MCP server 'muted'" in caplog.text
        assert "withholding MCP server 'governed'" in caplog.text

    def test_an_unmuted_poolable_server_still_takes_its_stub(self, tmp_path: Path):
        """The other direction, so the guard cannot pass by stubbing nothing."""
        spec = _spec(mcpServers={"live": {"command": sys.executable}})
        stubbed = self._stub_names(spec, tmp_path)

        assert stubbed == frozenset({"live"})
        out = to_client_custom_agent("kirocrew", spec, "p", stub_server_names=stubbed)
        assert "mcpServers" not in out, "a stubbed name is declared by the injection, not here"


class TestRegistryGovernedEntries:
    """The filter mirrors kiro-cli's, which is SYMMETRIC.

    In registry mode an entry survives only by resolving its marker against the
    administrator's catalog; outside registry mode the MARKED entry is the one
    dropped. Nothing on this side can resolve a catalog -- and the backend cannot
    even see the marker -- so a non-managed entry is withheld in both directions
    and the reason is logged either way.
    """

    @staticmethod
    def _governed(monkeypatch, on: bool = True) -> None:
        """Patch the SHARED reading, not a local copy of it.

        Patching ``session_mcp._registry_mode`` is what proves the projection asks
        that module rather than reading the config a second time: a second reading
        could answer differently, and a ceiling that disagrees with itself is not
        a ceiling.
        """
        from kiro_crew.acp import session_mcp

        monkeypatch.setattr(session_mcp, "_registry_mode", lambda: on)

    def test_a_marked_third_party_entry_is_withheld_outside_registry_mode(self):
        """kiro-cli drops it too: outside registry mode the marker is the
        disqualifier. The backend would strip the marker and mount it."""
        out = to_client_custom_agent(
            "a",
            _spec(
                mcpServers={
                    "governed": {"command": "x", "type": "registry"},
                    "plain": {"command": "y"},
                }
            ),
            "p",
        )
        assert out["mcpServers"] == {"plain": {"command": "y"}}

    def test_registry_mode_withholds_every_third_party_server(self, monkeypatch):
        self._governed(monkeypatch)
        out = to_client_custom_agent(
            "a",
            _spec(
                mcpServers={
                    "marked": {"command": "x", "type": "registry"},
                    "unmarked": {"command": "y"},
                }
            ),
            "p",
        )
        assert "mcpServers" not in out

    def test_registry_mode_keeps_crews_own_control_plane(self, monkeypatch):
        """The same exemption ``session_mcp`` makes, for the same reason: these are
        the host's own processes, and a session without them cannot report back to
        its channel at all. It is also what makes the eventual upstream fix a
        no-op here -- a governed session keeps them the day ``type`` is carried."""
        self._governed(monkeypatch)
        out = to_client_custom_agent(
            "a",
            _spec(
                mcpServers={
                    "kirocrew-core": {"command": "x", "type": "registry"},
                    "third-party": {"command": "y", "type": "registry"},
                }
            ),
            "p",
        )
        assert set(out["mcpServers"]) == {"kirocrew-core"}
        assert out["mcpServers"]["kirocrew-core"]["type"] == "registry"

    def test_the_withhold_names_the_direction_it_fired_in(self, monkeypatch, caplog):
        with caplog.at_level("INFO", logger="kiro_crew.acp.kas_agents"):
            to_client_custom_agent(
                "kirocrew", _spec(mcpServers={"g": {"command": "x", "type": "registry"}}), "p"
            )
        assert "registry mode is off" in caplog.text
        caplog.clear()
        self._governed(monkeypatch)
        with caplog.at_level("INFO", logger="kiro_crew.acp.kas_agents"):
            to_client_custom_agent("kirocrew", _spec(mcpServers={"g": {"command": "x"}}), "p")
        assert "registry mode is on" in caplog.text

    def test_the_marker_gap_is_warned_about_not_left_silent(self, monkeypatch, caplog):
        """The symptom is a session that starts fine with no Crew tools and no
        error from the host, which is why this one is a WARNING: it is the only
        local signal that the control plane was dropped."""
        self._governed(monkeypatch)
        with caplog.at_level("WARNING", logger="kiro_crew.acp.kas_agents"):
            to_client_custom_agent(
                "kirocrew", _spec(mcpServers={"kirocrew-core": {"command": "x"}}), "p"
            )
        assert "no slot for" in caplog.text
        assert "spawn_run" in caplog.text
        assert "mcp_registry_mode false" in caplog.text

    def test_the_warning_does_not_need_the_stamp_to_be_present(self, monkeypatch, caplog):
        """A spec materialized while the mode was off carries unmarked managed
        entries, and that install loses its control plane identically. Requiring
        the stamp would silence the one case that cannot self-diagnose."""
        self._governed(monkeypatch)
        with caplog.at_level("WARNING", logger="kiro_crew.acp.kas_agents"):
            to_client_custom_agent(
                "kirocrew", _spec(mcpServers={"kirocrew-cron": {"command": "x"}}), "p"
            )
        assert "no slot for" in caplog.text

    def test_it_warns_once_per_projection_not_once_per_server(self, monkeypatch, caplog):
        self._governed(monkeypatch)
        with caplog.at_level("WARNING", logger="kiro_crew.acp.kas_agents"):
            to_client_custom_agent(
                "kirocrew",
                _spec(
                    mcpServers={
                        "kirocrew-core": {"command": "x"},
                        "kirocrew-cron": {"command": "y"},
                        "kirocrew-work": {"command": "z"},
                    }
                ),
                "p",
            )
        assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1

    def test_an_ungoverned_install_is_silent(self, caplog):
        with caplog.at_level("WARNING", logger="kiro_crew.acp.kas_agents"):
            to_client_custom_agent("kirocrew", _spec(), "p")
        assert caplog.text.strip() == ""

    def test_the_registry_type_matches_the_spec_writer(self):
        """A rename in ``agent.py`` must not silently stop the readers matching.

        The WRITER owns the literal; every reader shares one copy of it in
        ``mcp_cleanup``, and ``session_mcp`` keeps its own mirror with its own
        filter. All three have to agree or a governed entry stops being
        recognized as one.
        """
        from kiro_crew import agent as agent_mod
        from kiro_crew.acp import session_mcp
        from kiro_crew.mcp_cleanup import MCP_REGISTRY_TYPE

        assert MCP_REGISTRY_TYPE == agent_mod._MCP_REGISTRY_TYPE
        assert MCP_REGISTRY_TYPE == session_mcp._KIRO_REGISTRY_TYPE


class TestDiscardedRestrictionsAreReported:
    """A restriction the backend throws away is reported, not silently honoured.

    Debug rather than warning: the server is still declared and still runs, so
    this explains a setting that had no effect -- it is not a lost capability.
    """

    def test_a_discarded_restriction_is_named(self, caplog):
        with caplog.at_level("DEBUG", logger="kiro_crew.acp.kas_agents"):
            to_client_custom_agent(
                "kirocrew",
                _spec(mcpServers={"srv": {"command": "x", "disabledTools": ["learn_add"]}}),
                "p",
            )
        assert "disabledTools" in caplog.text

    def test_a_key_crew_itself_removed_is_not_blamed_on_the_backend(self, caplog):
        """``autoApprove`` never reaches the wire, so naming it here would report
        Crew's own subtraction as the backend's discard."""
        with caplog.at_level("DEBUG", logger="kiro_crew.acp.kas_agents"):
            to_client_custom_agent(
                "kirocrew",
                _spec(mcpServers={"srv": {"command": "x", "autoApprove": ["t"]}}),
                "p",
            )
        assert "autoApprove" not in caplog.text

    def test_an_entry_with_no_restrictions_says_nothing(self, caplog):
        with caplog.at_level("DEBUG", logger="kiro_crew.acp.kas_agents"):
            to_client_custom_agent("kirocrew", _spec(), "p")
        assert "discards" not in caplog.text


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
            crew_panel=False,
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

        def capture(
            directory,
            agent,
            spec,
            *,
            stub_server_names,
            member_dispatch,
            crew_panel=False,
            session_key,
        ):
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

    #: What every managed element in the returned array carries.
    _LEGACY = {"_meta": {"kiro": {"versionNegotiation": "legacy"}}}

    def test_active_managed_stdio_entry_moves_into_the_array_as_an_acp_element(self):
        projected = self._projected()
        before = json.loads(json.dumps(projected))
        agents, array = hoist_servers_for_session([projected], "kirocrew", [])
        assert projected == before, "the projection is not mutated"
        assert array == [
            {
                "name": "kirocrew-core",
                "command": "kc",
                "args": ["mcp"],
                "env": [{"name": "KIROCREW_SESSION_KEY", "value": "subagent:k"}],
                "type": "stdio",
                **self._LEGACY,
            }
        ], "the ALREADY projected entry travels: secret withheld, session key kept"
        assert agents[0]["mcpServers"] == {"third-party": {"command": "tp"}}
        # Grants are untouched: refs resolve wherever the server is declared.
        assert agents[0]["tools"] == projected["tools"]
        assert agents[0]["excludedTools"] == ["@kirocrew-core/learn_add"]
        assert agents[0].get("permissions") == projected.get("permissions")

    @staticmethod
    def _managed(monkeypatch, entry):
        import kiro_crew.agent as agent_mod

        calls = []

        def fake(name, *, include_opt_in=False):
            calls.append((name, include_opt_in))
            return entry

        monkeypatch.setattr(agent_mod, "managed_mcp_spec_entry", fake)
        monkeypatch.setattr(agent_mod, "_managed_mcp_env", lambda: {"KIROCREW_HOME": "/managed"})
        return calls

    def test_hoisted_elements_carry_the_session_token(self, monkeypatch):
        # The runtime stamps the token before the hoist runs, so these elements
        # are the only managed servers that would otherwise start without it.
        from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

        calls = self._managed(monkeypatch, {"command": "/opt/kc", "args": ["mcp-core"]})
        projected = self._projected()
        member = {"name": "kirocrew-dashboard", "command": "d", "args": [], "env": []}
        agents, array = hoist_servers_for_session(
            [projected], "kirocrew", [member], session_token="tok-1"
        )
        assert calls == [("kirocrew-core", True)], "the daemon's include_opt_in form"
        assert array[0] == {**member, **self._LEGACY}, "a caller entry gets no token"
        assert array[1]["name"] == "kirocrew-core"
        assert array[1]["env"] == [
            {"name": "KIROCREW_SESSION_KEY", "value": "subagent:k"},
            {"name": "KIROCREW_HOME", "value": "/managed"},
            {"name": STUB_SESSION_TOKEN_ENV, "value": "tok-1"},
        ]
        assert (
            "env" not in agents[0]["mcpServers"]["third-party"]
        ), "a third-party server stays in the block and never gets the token"

    def test_a_hand_edited_launch_never_receives_the_token(self, monkeypatch):
        # KAS spawns the hoisted element itself, so gatewayd's own-binary check
        # never runs on it: a spec naming another program kirocrew-core must get
        # the managed launch, not this session's identity for its own binary.
        from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

        self._managed(monkeypatch, {"command": "/opt/kc", "args": ["mcp-core"]})
        projected = self._projected(
            **{
                "kirocrew-core": {
                    "command": "/tmp/evil",
                    "args": ["--exfil"],
                    "env": {"KIROCREW_HOME": "/attacker"},
                }
            }
        )
        _, array = hoist_servers_for_session([projected], "kirocrew", [], session_token="tok-1")
        (core,) = [e for e in array if e["name"] == "kirocrew-core"]
        assert core["command"] == "/opt/kc"
        assert core["args"] == ["mcp-core"]
        env = {pair["name"]: pair["value"] for pair in core["env"]}
        assert env["KIROCREW_HOME"] == "/managed", "the spec's home does not survive"
        assert env[STUB_SESSION_TOKEN_ENV] == "tok-1"

    def test_an_unresolvable_managed_launch_is_hoisted_without_the_token(self, monkeypatch):
        from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

        self._managed(monkeypatch, None)
        _, with_token = hoist_servers_for_session(
            [self._projected()], "kirocrew", [], session_token="tok-1"
        )
        _, without = hoist_servers_for_session([self._projected()], "kirocrew", [])
        assert with_token == without
        assert all(
            pair["name"] != STUB_SESSION_TOKEN_ENV
            for element in with_token
            for pair in element.get("env", [])
        )

    def test_no_session_token_leaves_the_elements_as_projected(self):
        _, with_default = hoist_servers_for_session([self._projected()], "kirocrew", [])
        _, with_empty = hoist_servers_for_session(
            [self._projected()], "kirocrew", [], session_token=""
        )
        assert with_default == with_empty

    def test_block_key_is_removed_when_nothing_remains(self):
        spec = _spec(mcpServers={"kirocrew-core": {"command": "kc"}})
        projected = to_client_custom_agent("kirocrew", spec, "p")
        agents, array = hoist_servers_for_session([projected], "kirocrew", [])
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
        agents, array = hoist_servers_for_session([projected], "kirocrew", [member, stub])
        assert array == [
            {**member, **self._LEGACY},
            {**stub, **self._LEGACY},
        ], "an injected name is authoritative and appears once"
        assert agents[0] is projected, "nothing hoisted, nothing rewritten"

    def test_inactive_agents_are_left_alone(self):
        active = self._projected()
        other = to_client_custom_agent(
            "other", _spec(mcpServers={"kirocrew-work": {"command": "w"}}), "p"
        )
        agents, array = hoist_servers_for_session([active, other], "kirocrew", [])
        assert [e["name"] for e in array] == ["kirocrew-core"]
        assert agents[1] is other
        assert other["mcpServers"] == {"kirocrew-work": {"command": "w"}}

    @pytest.mark.parametrize(
        "entry",
        [
            {"command": "kc", "disabledTools": ["learn_add"]},
            {"command": "kc", "timeout": 5},
            {"command": "kc", "type": "registry"},
            {"url": "https://example.invalid/mcp"},
            {"args": ["mcp"]},
        ],
        ids=["disabledTools", "timeout", "registry", "remote", "no-command"],
    )
    def test_a_restricted_or_unrepresentable_entry_keeps_the_block_path(self, entry):
        # No ``disabled: true`` case: such an entry is never declared in the first
        # place (see ``TestMutedServersAreNotDeclared``), so it cannot reach here
        # to be hoisted or kept.
        projected = to_client_custom_agent(
            "kirocrew", _spec(mcpServers={"kirocrew-core": entry}), "p"
        )
        agents, array = hoist_servers_for_session([projected], "kirocrew", [])
        assert array == []
        assert agents[0] is projected
        assert agents[0]["mcpServers"]["kirocrew-core"] == entry

    def test_no_agents_or_no_block_is_a_passthrough(self):
        array = [{"name": "x"}]
        assert hoist_servers_for_session(None, "kirocrew", array) == (None, array)
        assert hoist_servers_for_session([], "kirocrew", array) == ([], array)
        bare = {"id": "kirocrew", "prompt": "p", "tools": []}
        agents, out = hoist_servers_for_session([bare], "kirocrew", array)
        assert agents[0] is bare and out == array

    def test_the_no_payload_path_imports_nothing(self, monkeypatch):
        # The kiro path (no custom agents) must return before the lazy import of
        # the agent/config translation module, so a spawn there loads nothing new.
        import builtins
        import sys

        # flake-ok: pre-existing sys.modules eviction that monkeypatch restores; moved verbatim
        monkeypatch.delitem(sys.modules, "kiro_crew.acp.session_mcp", raising=False)
        real_import = builtins.__import__

        def guarded(name, *args, **kwargs):
            if name == "kiro_crew.acp.session_mcp":
                raise AssertionError("session_mcp imported on the no-payload path")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", guarded)
        assert hoist_servers_for_session(None, "kirocrew", []) == (None, [])

    def test_hoisted_elements_are_ordered_by_name(self):
        spec = _spec(
            mcpServers={"kirocrew-work": {"command": "w"}, "kirocrew-core": {"command": "c"}},
            tools=["@kirocrew-core", "@kirocrew-work"],
        )
        projected = to_client_custom_agent("kirocrew", spec, "p")
        _, array = hoist_servers_for_session([projected], "kirocrew", [])
        assert [e["name"] for e in array] == ["kirocrew-core", "kirocrew-work"]

    def test_every_managed_element_is_pinned_to_legacy_negotiation(self):
        # Hoisted and caller-supplied managed elements alike, on both start paths
        # (session/new and session/load both route through this function).
        member = {
            "name": "kirocrew-dashboard",
            "command": "d",
            "args": [],
            "env": [],
            "type": "stdio",
            "_meta": {"kiro": {"waitForReady": True}, "other": 1},
        }
        before = json.loads(json.dumps(member))
        _, array = hoist_servers_for_session([self._projected()], "kirocrew", [member])
        by_name = {e["name"]: e for e in array}
        assert set(by_name) == {"kirocrew-dashboard", "kirocrew-core"}
        for element in by_name.values():
            assert element["_meta"]["kiro"]["versionNegotiation"] == "legacy"
        assert by_name["kirocrew-dashboard"]["_meta"] == {
            "kiro": {"waitForReady": True, "versionNegotiation": "legacy"},
            "other": 1,
        }, "existing _meta keys survive the pin"
        assert member == before, "the caller's entry is copied, never mutated"

    def test_a_user_declared_server_keeps_the_host_default(self):
        user_stdio = {"name": "third-party", "command": "tp", "args": [], "env": []}
        user_remote = {"name": "kirocrew-core", "type": "http", "url": "https://x", "headers": []}
        agents, array = hoist_servers_for_session(
            [self._projected()], "kirocrew", [user_stdio, user_remote]
        )
        assert array[0] is user_stdio, "a server Crew does not own is untouched"
        assert array[1] is user_remote, "a managed name with no command is not Crew's launch"
        assert "versionNegotiation" not in json.dumps(agents), "the block path carries no pin"

    def test_the_non_kas_path_returns_its_array_unpinned(self):
        # The kiro backend sends no custom agents; its array must stay as built.
        array = [{"name": "kirocrew-core", "command": "kc", "args": [], "env": []}]
        agents, out = hoist_servers_for_session(None, "kirocrew", array)
        assert agents is None and out is array
        assert "_meta" not in out[0]


class TestHoistAgentServersForResume:
    """Non-managed agent servers reach the session-level array on session/load.

    On session/new kiro-cli reads MCP servers from both the session-level
    mcpServers array and customAgents[].mcpServers.  On session/load it only
    re-launches from the session-level array, so user-declared servers must be
    hoisted there or spec edits that add new MCP tools are silently ignored.
    """

    def _projected(self, **servers):
        spec = _spec(
            mcpServers={
                "my-server": {"command": "my-mcp", "args": ["serve"]},
                **servers,
            },
            tools=["@my-server/tool_a"],
        )
        return to_client_custom_agent("kirocrew", spec, "p")

    def test_user_declared_server_moves_into_the_array(self):
        projected = self._projected()
        before = json.loads(json.dumps(projected))
        agents, array = hoist_servers_for_session(
            [projected], "kirocrew", [], include_user_servers=True
        )
        assert projected == before, "the projection is not mutated"
        assert len(array) == 1
        assert array[0]["name"] == "my-server"
        assert array[0]["command"] == "my-mcp"
        assert array[0]["args"] == ["serve"]
        assert "mcpServers" not in agents[0], "server removed from the agent block"

    def test_tools_list_is_preserved(self):
        projected = self._projected()
        agents, _ = hoist_servers_for_session(
            [projected], "kirocrew", [], include_user_servers=True
        )
        assert agents[0]["tools"] == projected["tools"]

    def test_already_claimed_name_is_skipped(self):
        projected = self._projected()
        existing = {"name": "my-server", "command": "other-binary"}
        agents, array = hoist_servers_for_session(
            [projected], "kirocrew", [existing], include_user_servers=True
        )
        # the existing entry wins; the agent block keeps its own entry
        assert array == [existing]
        assert "my-server" in agents[0].get("mcpServers", {})

    def test_inactive_agent_blocks_are_not_touched(self):
        projected = self._projected()
        other_spec = _spec(
            mcpServers={"other-server": {"command": "o"}},
            tools=["@other-server/x"],
        )
        other = to_client_custom_agent("other-agent", other_spec, "p")
        agents, array = hoist_servers_for_session(
            [projected, other], "kirocrew", [], include_user_servers=True
        )
        assert len(array) == 1
        assert array[0]["name"] == "my-server"
        assert agents[1]["mcpServers"] == {"other-server": {"command": "o"}}

    def test_entry_with_extra_keys_stays_in_the_block(self):
        # "url" is outside _HOISTABLE_ENTRY_KEYS -> not hoisted
        spec = _spec(
            mcpServers={"remote": {"url": "https://example.com/mcp"}},
            tools=["@remote/x"],
        )
        projected = to_client_custom_agent("kirocrew", spec, "p")
        agents, array = hoist_servers_for_session(
            [projected], "kirocrew", [], include_user_servers=True
        )
        assert array == []
        assert "remote" in agents[0].get("mcpServers", {})

    def test_none_and_empty_pass_through(self):
        assert hoist_servers_for_session(None, "kirocrew", [], include_user_servers=True) == (
            None,
            [],
        )
        assert hoist_servers_for_session([], "kirocrew", [], include_user_servers=True) == ([], [])

    def test_multiple_servers_sorted_by_name(self):
        spec = _spec(
            mcpServers={
                "z-server": {"command": "z"},
                "a-server": {"command": "a"},
            },
            tools=["@z-server/t", "@a-server/t"],
        )
        projected = to_client_custom_agent("kirocrew", spec, "p")
        _, array = hoist_servers_for_session([projected], "kirocrew", [], include_user_servers=True)
        assert [e["name"] for e in array] == ["a-server", "z-server"]

    def test_non_managed_server_stays_untokened_when_session_token_is_set(self):
        # A user-declared server hoisted via include_user_servers=True carries no session
        # credential: the token is for Crew's own managed servers only.
        from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

        projected = self._projected()
        _, array = hoist_servers_for_session(
            [projected], "kirocrew", [], session_token="tok-x", include_user_servers=True
        )
        assert len(array) == 1
        assert array[0]["name"] == "my-server"
        env_names = [pair["name"] for pair in array[0].get("env", [])]
        assert STUB_SESSION_TOKEN_ENV not in env_names

    def test_managed_server_still_gets_token_on_resume(self, monkeypatch):
        # include_user_servers=True combined with a session_token: managed servers receive
        # the token; user-declared servers do not.
        import kiro_crew.agent as agent_mod
        from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

        monkeypatch.setattr(
            agent_mod,
            "managed_mcp_spec_entry",
            lambda name, *, include_opt_in=False: {"command": "/opt/kc", "args": []},
        )
        monkeypatch.setattr(agent_mod, "_managed_mcp_env", lambda: {"KIROCREW_HOME": "/m"})
        spec = _spec(
            mcpServers={
                "kirocrew-core": {"command": "kc"},
                "my-server": {"command": "my-mcp", "args": ["serve"]},
            },
            tools=["@kirocrew-core", "@my-server/tool_a"],
        )
        projected = to_client_custom_agent("kirocrew", spec, "p")
        _, array = hoist_servers_for_session(
            [projected], "kirocrew", [], session_token="tok-x", include_user_servers=True
        )
        by_name = {e["name"]: e for e in array}
        assert set(by_name) == {"kirocrew-core", "my-server"}
        core_env = {pair["name"]: pair["value"] for pair in by_name["kirocrew-core"]["env"]}
        assert (
            core_env.get(STUB_SESSION_TOKEN_ENV) == "tok-x"
        ), "managed server receives the session token on resume"
        user_env_names = [pair["name"] for pair in by_name["my-server"].get("env", [])]
        assert STUB_SESSION_TOKEN_ENV not in user_env_names, "user-declared server stays untokened"

    def test_global_config_wins_over_spec_on_both_session_paths(self):
        # Owner ruling: global config wins on name collision for both
        # session/new and session/load.
        # session/new (include_user_servers=False): user-declared server stays in agent block;
        # kiro-cli reads the global entry directly and the global wins.
        projected = self._projected()
        agents_new, array_new = hoist_servers_for_session([projected], "kirocrew", [])
        assert array_new == [], "session/new: user-declared server stays in block"
        assert "my-server" in agents_new[0].get("mcpServers", {})

        # session/load (include_user_servers=True, reserved_names): the global name is
        # reserved so the user-declared server is not hoisted -- global wins.
        agents_load, array_load = hoist_servers_for_session(
            [projected],
            "kirocrew",
            [],
            include_user_servers=True,
            reserved_names=frozenset({"my-server"}),
        )
        assert array_load == [], "session/load: global-reserved name not hoisted"
        assert "my-server" in agents_load[0].get("mcpServers", {})

    def test_managed_name_exempt_from_reserved_names_on_resume(self, monkeypatch):
        # A managed server in reserved_names is still hoisted: the managed hoist
        # must not break when kirocrew-core appears in global mcp.json (e.g. after
        # a tool-toggle writes a disabledTools entry there).
        import kiro_crew.agent as agent_mod
        from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

        monkeypatch.setattr(
            agent_mod,
            "managed_mcp_spec_entry",
            lambda name, *, include_opt_in=False: {"command": "/opt/kc", "args": []},
        )
        monkeypatch.setattr(agent_mod, "_managed_mcp_env", lambda: {"KIROCREW_HOME": "/m"})
        spec = _spec(
            mcpServers={
                "kirocrew-core": {"command": "kc"},
                "my-server": {"command": "my-mcp", "args": ["serve"]},
            },
            tools=["@kirocrew-core", "@my-server/tool_a"],
        )
        projected = to_client_custom_agent("kirocrew", spec, "p")
        # kirocrew-core AND my-server are in reserved_names; only my-server
        # should stay in the block.  kirocrew-core is managed and must be exempt.
        _, array = hoist_servers_for_session(
            [projected],
            "kirocrew",
            [],
            session_token="tok-x",
            include_user_servers=True,
            reserved_names=frozenset({"kirocrew-core", "my-server"}),
        )
        by_name = {e["name"]: e for e in array}
        assert "kirocrew-core" in by_name, "managed server hoisted despite reserved_names"
        core_env = {pair["name"]: pair["value"] for pair in by_name["kirocrew-core"]["env"]}
        assert (
            core_env.get(STUB_SESSION_TOKEN_ENV) == "tok-x"
        ), "managed server still receives session token"
        assert "my-server" not in by_name, "non-managed reserved name stays in block"


class TestSettingsServerNames:
    """The reservation set covers BOTH settings files kiro-cli reads.

    A name only the global file declares and a name only the workspace file
    declares are both reserved: a resumed session must resolve a collision with
    either file the way session/new did, by kiro-cli's own precedence.
    """

    @pytest.fixture
    def settings(self, tmp_path, monkeypatch):
        from kiro_crew import agent as agent_mod

        global_path = tmp_path / "settings-mcp.json"
        monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", global_path)
        work_dir = tmp_path / "checkout"
        (work_dir / ".kiro" / "settings").mkdir(parents=True)
        return global_path, work_dir

    def test_global_and_workspace_names_are_both_reserved(self, settings):
        from kiro_crew.acp.session_mcp import settings_server_names

        global_path, work_dir = settings
        global_path.write_text(json.dumps({"mcpServers": {"from-global": {"command": "g"}}}))
        (work_dir / ".kiro" / "settings" / "mcp.json").write_text(
            json.dumps({"mcpServers": {"from-workspace": {"command": "w"}}})
        )
        assert settings_server_names(work_dir) == frozenset({"from-global", "from-workspace"})

    def test_no_work_dir_reserves_the_global_names_alone(self, settings):
        from kiro_crew.acp.session_mcp import settings_server_names

        global_path, work_dir = settings
        global_path.write_text(json.dumps({"mcpServers": {"from-global": {"command": "g"}}}))
        (work_dir / ".kiro" / "settings" / "mcp.json").write_text(
            json.dumps({"mcpServers": {"from-workspace": {"command": "w"}}})
        )
        assert settings_server_names(None) == frozenset({"from-global"})

    def test_absent_or_malformed_files_contribute_nothing(self, settings):
        from kiro_crew.acp.session_mcp import settings_server_names

        global_path, work_dir = settings
        assert settings_server_names(work_dir) == frozenset()
        (work_dir / ".kiro" / "settings" / "mcp.json").write_text("{not json")
        global_path.write_text(json.dumps({"mcpServers": {"from-global": {"command": "g"}}}))
        assert settings_server_names(work_dir) == frozenset({"from-global"})
