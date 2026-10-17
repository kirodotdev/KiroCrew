"""Wire-contract tests for the MCP probe and measurement handlers.

``/api/mcp/probe`` and its cache, and the measurement readout's progress payload.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from handlers_mcp_coverage_helpers import sandbox_fixture  # noqa: F401
from handlers_mcp_coverage_helpers import _payload, _read_global, _request, _State, _write_global

from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.mcp_discovery import McpServerInfo
from kiro_crew.testing.wait import await_until

# ── /api/mcp/probe (POST live, GET cached) ──────────────────────────────


def _probed(name: str, **extra: Any) -> MagicMock:
    srv = MagicMock()
    srv.name = name
    srv.to_dict.return_value = {"name": name, "status": "ok", **extra}
    return srv


class TestProbe:
    @pytest.mark.asyncio
    async def test_live_probe_overlays_enabled_and_disabled_tools(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.mcp_discovery as disc

        _write_global(
            sandbox,
            {
                "off": {"disabled": True},
                "on": {"command": "x", "disabledTools": ["ReadFile"]},
            },
        )
        monkeypatch.setattr(
            disc, "probe_all", AsyncMock(return_value=[_probed("off"), _probed("on")])
        )
        monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])
        monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", 0.0)

        resp = await mcp_mod.api_mcp_probe(_request({}))
        assert resp.status == 200
        rows = {r["name"]: r for r in _payload(resp)}
        assert rows["off"]["enabled"] is False
        assert rows["on"]["enabled"] is True
        assert rows["on"]["disabledTools"] == ["ReadFile"]
        assert "disabledTools" not in rows["off"]
        # The live result becomes the handler cache.
        assert [r["name"] for r in mcp_mod._mcp_probe_cache] == ["off", "on"]
        assert mcp_mod._mcp_probe_ts > 0.0

    @pytest.mark.asyncio
    async def test_live_probe_tolerates_a_missing_global_config(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.mcp_discovery as disc

        sandbox.global_json.unlink(missing_ok=True)
        monkeypatch.setattr(disc, "probe_all", AsyncMock(return_value=[_probed("on")]))
        monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])
        resp = await mcp_mod.api_mcp_probe(_request({}))
        assert _payload(resp)[0]["enabled"] is True

    @pytest.mark.asyncio
    async def test_live_probe_rows_carry_the_same_config_state_as_the_list(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The probe response REPLACES the table's list on the client,
        so its rows must say what ``GET /api/mcp`` says. A store-disabled
        (consent) row and a row ``list_servers`` flagged disabled from another
        scope both read disabled, and ``kirocrewManaged`` rides along so the
        Edit action does not vanish between a probe and the next GET."""
        import kiro_crew.mcp_discovery as disc

        _write_global(sandbox, {"on": {"command": "x"}})
        store = mcp_mod._kirocrew_mcp_json()
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "on": {"command": "x"},
                        "consent": {"command": "y", "disabled": True},
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            disc,
            "probe_all",
            AsyncMock(
                return_value=[
                    _probed("on"),
                    _probed("consent", status="disabled", disabled=True),
                    # Disabled in a scope neither file above holds: only the
                    # row's own aggregate flag says so.
                    _probed("shared-off", status="disabled", disabled=True),
                ]
            ),
        )
        monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])
        monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", 0.0)

        rows = {r["name"]: r for r in _payload(await mcp_mod.api_mcp_probe(_request({})))}
        assert rows["on"]["enabled"] is True
        assert rows["on"]["kirocrewManaged"] is True
        assert rows["on"]["disabledIn"] is None
        assert rows["consent"]["enabled"] is False
        assert rows["consent"]["status"] == "disabled"
        assert rows["consent"]["kirocrewManaged"] is True
        # The store's own flag: the consent step in the table lifts it.
        assert rows["consent"]["disabledIn"] == "kirocrew"
        assert rows["consent"]["disabledInFile"] is None
        assert rows["shared-off"]["enabled"] is False
        assert rows["shared-off"]["status"] == "disabled"
        assert rows["shared-off"]["kirocrewManaged"] is False
        # Only the aggregate flag says so: inert here, and no file to name.
        assert rows["shared-off"]["disabledIn"] == "shared"
        assert rows["shared-off"]["disabledInFile"] is None

    @pytest.mark.asyncio
    async def test_live_probe_survives_a_non_mapping_store(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hand-edited store whose ``mcpServers`` is a list must not turn the
        probe response into a 500 after the fan-out already ran: it reads as an
        empty map, so the row keeps the state the other sources give it."""
        import kiro_crew.mcp_discovery as disc

        _write_global(sandbox, {"on": {"command": "x"}})
        store = mcp_mod._kirocrew_mcp_json()
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps({"mcpServers": []}), encoding="utf-8")
        monkeypatch.setattr(disc, "probe_all", AsyncMock(return_value=[_probed("on")]))
        monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])

        resp = await mcp_mod.api_mcp_probe(_request({}))
        assert resp.status == 200
        row = _payload(resp)[0]
        assert row["enabled"] is True
        assert row["kirocrewManaged"] is False

    def test_stamp_config_state_tolerates_non_mapping_inputs(self) -> None:
        d: dict[str, Any] = {"name": "srv", "status": "ok"}
        mcp_mod._stamp_config_state(d, [], "not a map")
        assert d["enabled"] is True
        assert d["kirocrewManaged"] is False
        assert d["status"] == "ok"
        assert d["disabledIn"] is None
        assert d["disabledInFile"] is None

    def test_stamp_config_state_names_the_scope_that_disabled_the_row(
        self, sandbox: SimpleNamespace
    ) -> None:
        """``disabledIn`` is the backend's own answer to "which config switched
        this off", so the table never infers it from ``enabled`` +
        ``kirocrewManaged``. The shared Kiro-global flag names its file; the
        store's flag is the consent state the table can lift itself."""
        shared: dict[str, Any] = {"name": "figma", "status": "ok"}
        mcp_mod._stamp_config_state(shared, {"figma": {"command": "x", "disabled": True}}, {})
        assert shared["enabled"] is False
        assert shared["status"] == "disabled"
        assert shared["disabledIn"] == "shared"
        assert shared["disabledInFile"] == mcp_mod._KIRO_GLOBAL_SURFACE
        assert shared["disabledInFile"].endswith("mcp.json")

        consent: dict[str, Any] = {"name": "weather", "status": "ok"}
        mcp_mod._stamp_config_state(consent, {}, {"weather": {"command": "y", "disabled": True}})
        assert consent["enabled"] is False
        assert consent["kirocrewManaged"] is True
        assert consent["disabledIn"] == "kirocrew"
        assert consent["disabledInFile"] is None

    def test_stamp_config_state_dual_scope_disable_reads_shared(
        self, sandbox: SimpleNamespace
    ) -> None:
        """A store-managed server that the SHARED config disables is inert, not a
        consent row: lifting the store's flag (the consent step) would leave the
        shared flag standing, so the table must not offer it. Both flags set
        reads the same way, for the same reason."""
        managed_shared_off: dict[str, Any] = {"name": "figma", "status": "ok"}
        mcp_mod._stamp_config_state(
            managed_shared_off,
            {"figma": {"command": "x", "disabled": True}},
            {"figma": {"command": "x"}},
        )
        assert managed_shared_off["kirocrewManaged"] is True
        assert managed_shared_off["enabled"] is False
        assert managed_shared_off["disabledIn"] == "shared"
        assert managed_shared_off["disabledInFile"] == mcp_mod._KIRO_GLOBAL_SURFACE

        both_off: dict[str, Any] = {"name": "figma", "status": "ok"}
        mcp_mod._stamp_config_state(
            both_off,
            {"figma": {"command": "x", "disabled": True}},
            {"figma": {"command": "x", "disabled": True}},
        )
        assert both_off["disabledIn"] == "shared"

    def test_stamp_config_state_provider_global_plus_store_disable_reads_shared(
        self, sandbox: SimpleNamespace
    ) -> None:
        """A server disabled in a PROVIDER global (a scope this helper never
        reads) and in the store is inert, not a consent row: ``list_servers``
        records the provider-global mute on the row (``disabledInShared``), and
        that verdict outranks the store flag the two maps in hand explain. Red
        under ``store_off and not shared_off -> "kirocrew"``, which offered a
        consent step that lifted the store flag and left the provider-global one
        standing. The row's marker is consumed, not forwarded."""
        row: dict[str, Any] = {
            "name": "figma",
            "status": "ok",
            "disabled": True,
            "disabledInShared": True,
        }
        mcp_mod._stamp_config_state(row, {}, {"figma": {"command": "x", "disabled": True}})
        assert row["kirocrewManaged"] is True
        assert row["enabled"] is False
        assert row["disabledIn"] == "shared"
        # Not the Kiro-global file: the helper cannot name a provider global.
        assert row["disabledInFile"] is None
        assert row["disabledReason"] is None
        assert "disabledInShared" not in row
        # The same store flag WITHOUT the row's verdict is the consent row.
        consent: dict[str, Any] = {"name": "figma", "status": "ok", "disabled": True}
        mcp_mod._stamp_config_state(consent, {}, {"figma": {"command": "x", "disabled": True}})
        assert consent["disabledIn"] == "kirocrew"

    def test_stamp_config_state_resolves_a_slash_named_entry_by_its_alias(
        self, sandbox: SimpleNamespace
    ) -> None:
        """The row is canonical (``playwright-mcp``), the files keep the key as
        written (``npm:@playwright/mcp``). Both maps are read by alias too, so
        the shared disable is seen, the file is named, and the store entry
        counts as managed. Red under ``global_mcps.get(name)``: the row stamped
        from nothing -- ``enabled: true`` beside a launch gate that refused it,
        and no Edit action for a store it does own."""
        row: dict[str, Any] = {"name": "playwright-mcp", "status": "ok", "disabled": True}
        mcp_mod._stamp_config_state(
            row,
            {"npm:@playwright/mcp": {"command": "npx", "disabled": True}},
            {"npm:@playwright/mcp": {"command": "npx"}},
        )
        assert row["kirocrewManaged"] is True
        assert row["enabled"] is False
        assert row["disabledIn"] == "shared"
        assert row["disabledInFile"] == mcp_mod._KIRO_GLOBAL_SURFACE
        # Both spellings in one file: the EXACT key is the row's source (it is the
        # entry step 3b keeps as the representative), so the muted raw key beside
        # it is not read as this row's entry. The disable still reaches the row
        # through the verdict ``list_servers`` stamped over every raw key.
        both: dict[str, Any] = {
            "name": "playwright-mcp",
            "status": "ok",
            "disabled": True,
            "disabledInShared": True,
            "disabledReason": "invalid",
        }
        mcp_mod._stamp_config_state(
            both,
            {
                "playwright-mcp": {"command": "npx"},
                "npm:@playwright/mcp": {"command": "npx", "disabled": "false"},
            },
            {},
        )
        assert both["enabled"] is False
        assert both["disabledIn"] == "shared"
        assert both["disabledInFile"] is None
        assert both["disabledReason"] == "invalid"

    def test_stamp_config_state_never_touches_the_filesystem(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stamping runs on the event loop for every row of every listing and
        probe response, so it may not stat anything: ``disabledInFile`` is the
        constant display form of the Kiro-global file, not a resolution of it.
        Every resolver a path display could reach raises here. Red under
        ``display_path(_GLOBAL_MCP_JSON)``, which resolved the home directory
        (``Path.home().resolve()``) per row -- a slow home mount stalled the
        gateway and its heartbeat."""

        def boom(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("filesystem touched on the event-loop stamp path")

        shared: dict[str, Any] = {"name": "figma-mcp", "status": "ok"}
        consent: dict[str, Any] = {"name": "weather", "status": "ok", "disabled": True}
        with monkeypatch.context() as m:
            m.setattr(Path, "resolve", boom)
            m.setattr(Path, "home", classmethod(lambda cls: boom()))
            m.setattr(Path, "stat", boom)
            m.setattr(os, "stat", boom)
            m.setattr(os, "lstat", boom)
            mcp_mod._stamp_config_state(
                shared,
                {"npm:@figma/mcp": {"command": "x", "disabled": "false"}},
                {"figma-mcp": {"command": "x"}},
            )
            mcp_mod._stamp_config_state(
                consent, {}, {"weather": {"command": "y", "disabled": True}}
            )
        assert shared["disabledIn"] == "shared"
        assert shared["disabledInFile"] == "~/.kiro/settings/mcp.json"
        assert shared["disabledReason"] == "invalid"
        assert shared["kirocrewManaged"] is True
        assert consent["disabledIn"] == "kirocrew"
        assert consent["disabledInFile"] is None

    def test_config_key_for_resolves_to_one_key_or_refuses(self) -> None:
        """Exact key first; else the single raw key whose alias is the name;
        two raw keys and no exact key are a refusal, never a set. ``team/foo``
        and ``team-foo`` are two servers: the row ``team-foo`` resolves to its
        own key and leaves ``team/foo`` alone."""
        servers = {
            "team-foo": {"command": "a"},
            "team/foo": {"command": "b"},
            "npm:@playwright/mcp": {"command": "npx"},
            "plain": {"command": "p"},
        }
        assert mcp_mod._config_key_for(servers, "team-foo") == "team-foo"
        assert mcp_mod._config_key_for(servers, "playwright-mcp") == "npm:@playwright/mcp"
        assert mcp_mod._config_key_for(servers, "plain") == "plain"
        assert mcp_mod._config_key_for(servers, "absent") is None
        assert mcp_mod._config_key_for("not a map", "plain") is None
        collide = {"team/foo": {"command": "b"}, "other:team/foo": {"command": "c"}}
        with pytest.raises(mcp_mod.AmbiguousServerKey) as excinfo:
            mcp_mod._config_key_for(collide, "team-foo")
        assert excinfo.value.count == 2
        assert "edit the file directly" in str(excinfo.value)
        # The READ side treats the refusal as "no entry in this map".
        assert mcp_mod._config_entry_for(collide, "team-foo") is None
        assert mcp_mod._config_entry_for(servers, "team-foo") == {"command": "a"}

    def test_stamp_config_state_reads_a_non_boolean_disabled_fail_closed(
        self, sandbox: SimpleNamespace
    ) -> None:
        """``"disabled": "false"`` (a string) is a config error read FAIL-CLOSED
        by the launch predicate the stamping shares with the sessions: the row is
        off in both the shared and the store map, and says WHY --
        ``disabledReason == "invalid"`` -- so the table can name the value to
        repair rather than a switch to flip. A literal ``True`` is a real switch
        and carries no reason. Red under the ``is True`` read this replaced,
        which listed the row enabled beside a gate that refused to start it."""
        for value in ("false", "true", 1, "yes", None):
            shared: dict[str, Any] = {"name": "srv", "status": "ok"}
            mcp_mod._stamp_config_state(shared, {"srv": {"command": "x", "disabled": value}}, {})
            assert shared["enabled"] is False, value
            assert shared["status"] == "disabled", value
            assert shared["disabledIn"] == "shared", value
            assert shared["disabledInFile"] == mcp_mod._KIRO_GLOBAL_SURFACE
            assert shared["disabledReason"] == "invalid", value
            store: dict[str, Any] = {"name": "srv", "status": "ok"}
            mcp_mod._stamp_config_state(store, {}, {"srv": {"command": "x", "disabled": value}})
            assert store["enabled"] is False, value
            assert store["kirocrewManaged"] is True, value
            assert store["disabledIn"] == "kirocrew", value
            assert store["disabledReason"] == "invalid", value
        # A real switch: off, and no reason to add. An enabled row carries the
        # key too, as ``None``, so a client never reads absence as a reason.
        real: dict[str, Any] = {"name": "srv", "status": "ok"}
        mcp_mod._stamp_config_state(real, {"srv": {"command": "x", "disabled": True}}, {})
        assert real["enabled"] is False
        assert real["disabledReason"] is None
        on: dict[str, Any] = {"name": "srv", "status": "ok"}
        mcp_mod._stamp_config_state(on, {"srv": {"command": "x", "disabled": False}}, {})
        assert on["enabled"] is True
        assert on["disabledReason"] is None
        # The aggregate flag ``list_servers`` stamps is the one other disable
        # source; neither map in hand explains it, so its reason is the row's
        # own verdict -- carried when ``list_servers`` set it, ``None`` otherwise.
        aggregate: dict[str, Any] = {"name": "srv", "status": "ok", "disabled": True}
        mcp_mod._stamp_config_state(aggregate, {}, {})
        assert aggregate["enabled"] is False
        assert aggregate["disabledIn"] == "shared"
        assert aggregate["disabledInFile"] is None
        assert aggregate["disabledReason"] is None
        aggregate_invalid: dict[str, Any] = {
            "name": "srv",
            "status": "ok",
            "disabled": True,
            "disabledReason": "invalid",
        }
        mcp_mod._stamp_config_state(aggregate_invalid, {}, {})
        assert aggregate_invalid["disabledIn"] == "shared"
        assert aggregate_invalid["disabledReason"] == "invalid"

    def test_enable_lifts_a_non_boolean_disabled_in_the_store(
        self, sandbox: SimpleNamespace
    ) -> None:
        """The consent step must be able to land on a row the fail-closed read
        shows as Disabled: enabling lifts WHATEVER mutes the store entry, not
        only a literal ``true`` -- under the ``is True`` test the string stayed
        put as a "noop" and the row could never be switched on from here. The
        disable direction still writes the boolean over an odd value (a repair),
        and a spec edit keeps a muted entry muted, as the boolean."""
        store = mcp_mod._kirocrew_mcp_json()
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(
            json.dumps({"mcpServers": {"srv": {"command": "x", "disabled": "false"}}}),
            encoding="utf-8",
        )
        assert mcp_mod._set_kirocrew_entry("srv", enabled=True) == "enabled"
        assert "disabled" not in json.loads(store.read_text())["mcpServers"]["srv"]
        # Disable over an odd value: written as the boolean, not a no-op.
        store.write_text(
            json.dumps({"mcpServers": {"srv": {"command": "x", "disabled": "yes"}}}),
            encoding="utf-8",
        )
        assert mcp_mod._set_kirocrew_entry("srv", enabled=False) == "disabled"
        assert json.loads(store.read_text())["mcpServers"]["srv"]["disabled"] is True
        assert mcp_mod._set_kirocrew_entry("srv", enabled=False) == "noop"
        # A spec edit is not consent: the mute survives, as the boolean.
        store.write_text(
            json.dumps({"mcpServers": {"srv": {"command": "x", "disabled": "false"}}}),
            encoding="utf-8",
        )
        assert mcp_mod._replace_kirocrew_spec("srv", {"command": "y", "disabled": "false"})
        assert json.loads(store.read_text())["mcpServers"]["srv"] == {
            "command": "y",
            "disabled": True,
        }
        # The same lift for a provider-global file.
        shared = sandbox.global_json
        shared.write_text(
            json.dumps({"mcpServers": {"srv": {"command": "x", "disabled": None}}}),
            encoding="utf-8",
        )
        assert mcp_mod._set_scope_entry(shared, "srv", enabled=True) == "enabled"
        assert "disabled" not in json.loads(shared.read_text())["mcpServers"]["srv"]

    def test_edit_reaches_a_raw_keyed_store_entry_through_its_canonical_row(
        self, sandbox: SimpleNamespace
    ) -> None:
        """The table stamps ``kirocrewManaged`` from the store entry the row's
        canonical name resolves to (``npm:@playwright/mcp`` for ``playwright-mcp``)
        and advertises Edit on it; the editor's GET and PUT must resolve the same
        way, or the action it offers answers 404. The PUT replaces the entry under
        its OWN raw key -- one entry, not a canonical twin beside it -- and keeps
        a mute as the boolean. An ambiguous name reads as absent, as the row's
        stamping does. Red under the exact-key reads: ``None`` / ``False``."""
        store = mcp_mod._kirocrew_mcp_json()
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(
            json.dumps(
                {"mcpServers": {"npm:@playwright/mcp": {"command": "npx", "disabled": "false"}}}
            ),
            encoding="utf-8",
        )
        assert mcp_mod._get_kirocrew_entry("playwright-mcp") == {
            "command": "npx",
            "disabled": "false",
        }
        assert mcp_mod._replace_kirocrew_spec("playwright-mcp", {"command": "npx", "args": ["-y"]})
        assert json.loads(store.read_text())["mcpServers"] == {
            "npm:@playwright/mcp": {"command": "npx", "args": ["-y"], "disabled": True}
        }
        # Two raw keys, no exact one: nothing to read or edit.
        store.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "npm:@team/foo": {"command": "a"},
                        "other:team/foo": {"command": "b"},
                    }
                }
            ),
            encoding="utf-8",
        )
        assert mcp_mod._get_kirocrew_entry("team-foo") is None
        assert mcp_mod._replace_kirocrew_spec("team-foo", {"command": "c"}) is False
        assert set(json.loads(store.read_text())["mcpServers"]) == {
            "npm:@team/foo",
            "other:team/foo",
        }

    def test_consent_lifts_a_raw_keyed_store_entry_in_place(self, sandbox: SimpleNamespace) -> None:
        """The table offers the consent step (Kiro Crew badge + Apply) on a
        ``disabledIn: "kirocrew"`` row stamped from a raw-keyed store entry; the
        writer must reach that same entry. Red under ``servers.get(name)``:
        without a spec it answered ``noop`` (row stuck Disabled), and with the
        preserved spec the apply passes it wrote a canonical twin beside the muted
        raw entry -- the row stayed Disabled and its provenance read ``shared``."""
        store = mcp_mod._kirocrew_mcp_json()
        store.parent.mkdir(parents=True, exist_ok=True)
        muted = {"mcpServers": {"npm:@playwright/mcp": {"command": "npx", "disabled": True}}}
        store.write_text(json.dumps(muted), encoding="utf-8")
        assert mcp_mod._scope_has_entry("playwright-mcp", store) is True
        assert mcp_mod._set_kirocrew_entry("playwright-mcp", enabled=True) == "enabled"
        assert json.loads(store.read_text())["mcpServers"] == {
            "npm:@playwright/mcp": {"command": "npx"}
        }
        # With the preserved spec the apply passes: still the one entry, lifted.
        store.write_text(json.dumps(muted), encoding="utf-8")
        assert (
            mcp_mod._set_kirocrew_entry("playwright-mcp", enabled=True, spec={"command": "npx"})
            == "enabled"
        )
        assert set(json.loads(store.read_text())["mcpServers"]) == {"npm:@playwright/mcp"}
        # Disable lands on the same entry, and an ambiguous name writes nothing.
        assert mcp_mod._set_kirocrew_entry("playwright-mcp", enabled=False) == "disabled"
        assert (
            json.loads(store.read_text())["mcpServers"]["npm:@playwright/mcp"]["disabled"] is True
        )
        store.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "npm:@team/foo": {"command": "a"},
                        "other:team/foo": {"command": "b"},
                    }
                }
            ),
            encoding="utf-8",
        )
        before = store.read_bytes()
        assert (
            mcp_mod._set_kirocrew_entry("team-foo", enabled=True, spec={"command": "c"})
            == "ambiguous"
        )
        assert mcp_mod._scope_has_entry("team-foo", store) is False
        assert store.read_bytes() == before

    def test_uninstall_by_alias_removes_the_raw_slash_named_entry_everywhere(
        self, sandbox: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Uninstall carries the row's canonical name; the store and the shared
        file hold the raw ``npm:@...`` key. The one key that names the server
        goes, in every scope and in the rendered agent file. Red under
        ``name not in servers``: both scopes answered ``noop`` and the
        definitions stayed while the table reported the row removed."""
        import kiro_crew.dashboard.handlers.mcp as handler

        store = mcp_mod._kirocrew_mcp_json()
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(
            json.dumps({"mcpServers": {"npm:@playwright/mcp": {"command": "npx"}}}),
            encoding="utf-8",
        )
        _write_global(
            sandbox,
            {
                "npm:@playwright/mcp": {"command": "npx", "disabled": True},
                "other": {"command": "y"},
            },
        )
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        rendered = agents_dir / "kirocrew.json"
        rendered.write_text(
            json.dumps({"mcpServers": {"playwright-mcp": {"command": "npx"}}}), encoding="utf-8"
        )
        monkeypatch.setattr(handler, "kiro_agents_dir", lambda: agents_dir)
        actions = mcp_mod._purge_server_config("playwright-mcp")
        assert actions == {"kirocrew": "removed", "kiroGlobal": "removed"}
        assert json.loads(store.read_text())["mcpServers"] == {}
        assert _read_global(sandbox) == {"other": {"command": "y"}}
        assert json.loads(rendered.read_text())["mcpServers"] == {}
        # Idempotent: a second purge finds nothing.
        assert mcp_mod._purge_server_config("playwright-mcp") == {
            "kirocrew": "noop",
            "kiroGlobal": "noop",
        }

    def test_uninstall_leaves_a_distinct_server_whose_alias_collides(
        self, sandbox: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``team/foo`` and ``team-foo`` both alias to ``team-foo`` and are two
        servers. Uninstalling the row ``team-foo`` removes ITS key and leaves
        ``team/foo`` byte for byte; the row the survivor then stands for is
        uninstalled by its own, second, Uninstall. Red under the collision set
        ``_config_keys_for`` returned: one Uninstall deleted both configurations."""
        import kiro_crew.dashboard.handlers.mcp as handler

        survivor = {"command": "uvx", "args": ["team-foo-server", "--port", "9"], "env": {"A": "1"}}
        store = mcp_mod._kirocrew_mcp_json()
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps({"mcpServers": {"team-foo": {"command": "npx"}}}))
        _write_global(
            sandbox,
            {"team/foo": survivor, "team-foo": {"command": "npx", "disabled": True}},
        )
        survivor_bytes = json.dumps(_read_global(sandbox)["team/foo"], sort_keys=True)
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "kirocrew.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        monkeypatch.setattr(handler, "kiro_agents_dir", lambda: agents_dir)

        actions = mcp_mod._purge_server_config("team-foo")
        assert actions == {"kirocrew": "removed", "kiroGlobal": "removed"}
        assert json.loads(store.read_text())["mcpServers"] == {}
        remaining = _read_global(sandbox)
        assert set(remaining) == {"team/foo"}
        assert json.dumps(remaining["team/foo"], sort_keys=True) == survivor_bytes
        # With ``team-foo`` gone, ``team/foo`` is the single key the alias names.
        assert mcp_mod._purge_server_config("team-foo")["kiroGlobal"] == "removed"
        assert _read_global(sandbox) == {}

    def test_uninstall_refuses_an_ambiguous_alias_and_changes_nothing(
        self, sandbox: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two raw keys alias to ``team-foo`` and neither is keyed ``team-foo``:
        nothing says which the row is, so the scope reports ``ambiguous`` and the
        file is untouched byte for byte -- a guess either way edits or deletes a
        server the user did not name."""
        import kiro_crew.dashboard.handlers.mcp as handler

        _write_global(
            sandbox,
            {"team/foo": {"command": "a"}, "other:team/foo": {"command": "b"}},
        )
        before = sandbox.global_json.read_bytes()
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        monkeypatch.setattr(handler, "kiro_agents_dir", lambda: agents_dir)
        actions = mcp_mod._purge_server_config("team-foo")
        assert actions == {"kirocrew": "noop", "kiroGlobal": "ambiguous"}
        assert sandbox.global_json.read_bytes() == before
        assert (
            mcp_mod._set_scope_entry(sandbox.global_json, "team-foo", enabled=True) == "ambiguous"
        )
        assert sandbox.global_json.read_bytes() == before

    def test_uninstall_is_one_read_and_one_write_per_store_under_the_lock(
        self, sandbox: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Structural pin of the uninstall's shape: under the MCP lock, every
        config store is read exactly once and written exactly once, and the key
        removed is computed from that same read (``_rmw_remove_entry``). The only
        other read of a store is the read-only preflight, which runs BEFORE the
        lock and decides nothing about presence. With reads and writes adjacent
        under one lock, nothing a dashboard writer does can land between them."""
        import kiro_crew.dashboard.handlers.mcp as handler

        store = mcp_mod._kirocrew_mcp_json()
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps({"mcpServers": {"npm:@team/foo": {"command": "s"}}}))
        _write_global(sandbox, {"npm:@team/foo": {"command": "a"}, "other": {"command": "o"}})
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        rendered = agents_dir / "kirocrew.json"
        rendered.write_text(json.dumps({"mcpServers": {"team-foo": {"command": "s"}}}))
        monkeypatch.setattr(handler, "kiro_agents_dir", lambda: agents_dir)

        held = {"lock": False}
        reads: list[tuple[Path, bool]] = []
        writes: list[tuple[Path, bool]] = []
        real_load, real_write = mcp_mod._load_json_or_empty, mcp_mod._atomic_write

        def spy_load(path: Path) -> dict:
            reads.append((Path(path), held["lock"]))
            return real_load(path)

        def spy_write(path: Path, data: Any) -> None:
            writes.append((Path(path), held["lock"]))
            real_write(path, data)

        monkeypatch.setattr(mcp_mod, "_load_json_or_empty", spy_load)
        monkeypatch.setattr(mcp_mod, "_atomic_write", spy_write)

        class _FlagLock:
            async def __aenter__(self):
                held["lock"] = True

            async def __aexit__(self, *a):
                held["lock"] = False

        class _NoSync:
            def __enter__(self):
                return None

            def __exit__(self, *a):
                return None

        monkeypatch.setattr(mcp_mod, "_get_mcp_lock", lambda: _FlagLock())
        monkeypatch.setattr(mcp_mod, "rebuild_agent_config_reporting", lambda: (Path(), True))
        monkeypatch.setattr(mcp_mod, "_get_mcp_lock_sync", lambda: _NoSync())

        async def run() -> None:
            resp = await mcp_mod.api_mcp_apply(
                _request({"changes": [{"name": "team-foo", "uninstall": True}]})
            )
            assert resp.status == 200, resp.text

        asyncio.run(run())

        stores = [store, sandbox.global_json, rendered]
        for path in stores:
            locked_reads = [p for p, h in reads if p == path and h]
            locked_writes = [p for p, h in writes if p == path and h]
            assert len(locked_reads) == 1, (path, reads)
            assert len(locked_writes) == 1, (path, writes)
        # Every write happened under the lock; the only unlocked reads are the
        # preflight's, one per store.
        assert all(h for _, h in writes), writes
        unlocked_reads = [p for p, h in reads if not h and p in stores]
        assert sorted(map(str, unlocked_reads)) == sorted(map(str, stores)), unlocked_reads
        assert json.loads(store.read_text())["mcpServers"] == {}
        assert _read_global(sandbox) == {"other": {"command": "o"}}
        assert json.loads(rendered.read_text())["mcpServers"] == {}

    def test_an_entry_re_aliased_between_preflight_and_purge_is_still_removed(
        self, sandbox: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The preflight saw ``team-foo`` under its exact key; before the purge a
        writer re-keyed that same server as ``npm:@team/foo``. The purge's own
        read resolves the alias and removes it. Red under the pinned design: the
        pinned key ``team-foo`` is absent from the file, so the store answers
        ``noop`` and the server the user uninstalled stays configured."""
        import kiro_crew.dashboard.handlers.mcp as handler

        _write_global(sandbox, {"team-foo": {"command": "a"}})
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        monkeypatch.setattr(handler, "kiro_agents_dir", lambda: agents_dir)
        keys, ambiguous = mcp_mod._preflight_change_keys(["team-foo"])
        assert ambiguous == {} and keys["team-foo"]["kiroGlobal"] == "team-foo"
        # The concurrent re-alias.
        _write_global(sandbox, {"npm:@team/foo": {"command": "a"}})
        actions = mcp_mod._purge_server_config("team-foo", preferred=keys["team-foo"])
        assert actions["kiroGlobal"] == "removed"
        assert _read_global(sandbox) == {}

    def test_keys_added_between_preflight_and_purge_survive(
        self, sandbox: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whatever lands between the preflight and the purge goes back exactly as
        the purge's read found it: an unrelated server added meanwhile survives,
        and a colliding sibling (the r10 race) survives while the entry the row
        stood for -- the preflight's key, the one tie the read cannot break alone
        -- is the one removed. A read that stays ambiguous with no usable
        tiebreaker writes nothing to that store."""
        import kiro_crew.dashboard.handlers.mcp as handler

        _write_global(sandbox, {"npm:@team/foo": {"command": "a"}})
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        monkeypatch.setattr(handler, "kiro_agents_dir", lambda: agents_dir)
        keys, _ = mcp_mod._preflight_change_keys(["team-foo"])
        assert keys["team-foo"]["kiroGlobal"] == "npm:@team/foo"
        # Concurrent additions: an unrelated server and a colliding sibling.
        _write_global(
            sandbox,
            {
                "npm:@team/foo": {"command": "a"},
                "other:team/foo": {"command": "b"},
                "newcomer": {"command": "n"},
            },
        )
        actions = mcp_mod._purge_server_config("team-foo", preferred=keys["team-foo"])
        assert actions["kiroGlobal"] == "removed"
        assert _read_global(sandbox) == {
            "other:team/foo": {"command": "b"},
            "newcomer": {"command": "n"},
        }
        # No usable tiebreaker (the preferred key itself is gone): nothing written.
        _write_global(sandbox, {"x:team/foo": {"command": "c"}, "y:team/foo": {"command": "d"}})
        before = sandbox.global_json.read_bytes()
        assert (
            mcp_mod._purge_server_config("team-foo", preferred=keys["team-foo"])["kiroGlobal"]
            == "ambiguous"
        )
        assert sandbox.global_json.read_bytes() == before

    @pytest.mark.asyncio
    async def test_cached_probe_returns_the_warm_cache_without_reprobing(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time

        cache = [{"name": "on", "status": "ok"}]
        monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", list(cache))
        monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", time.time())
        monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", False)
        bg = AsyncMock()
        monkeypatch.setattr(mcp_mod, "_bg_mcp_probe", bg)

        state = _State()
        resp = await mcp_mod.api_mcp_probe_cached(_request(state=state))
        assert resp.status == 200
        assert _payload(resp) == cache
        assert not state._background_tasks
        bg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cached_probe_arms_a_background_reprobe_when_stale(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])
        monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", 0.0)  # never probed
        monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", False)
        bg = AsyncMock()
        monkeypatch.setattr(mcp_mod, "_bg_mcp_probe", bg)

        state = _State()
        resp = await mcp_mod.api_mcp_probe_cached(_request(state=state))
        assert resp.status == 200
        assert _payload(resp) == []
        assert mcp_mod._mcp_probe_in_progress is True
        assert len(state._background_tasks) == 1
        await asyncio.gather(*state._background_tasks)
        bg.assert_awaited_once()
        # The done-callback deregisters the task from the state registry.
        assert not state._background_tasks

    @pytest.mark.asyncio
    async def test_cached_probe_does_not_stack_reprobes(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An in-flight probe suppresses a second one even on a cold cache."""
        monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])
        monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", 0.0)
        monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", True)
        bg = AsyncMock()
        monkeypatch.setattr(mcp_mod, "_bg_mcp_probe", bg)

        state = _State()
        await mcp_mod.api_mcp_probe_cached(_request(state=state))
        assert not state._background_tasks
        bg.assert_not_awaited()


# ── GET/POST /api/mcp/measure ───────────────────────────────────────────


class TestMeasureProgressPayload:
    """The measurement readout's wire contract.

    ``done`` and ``measured`` are separate fields because a pass can attempt a
    server and produce no verdict: a pre-flight that could not run leaves the row
    unmeasured on purpose. The dashboard advances its progress line on ``done``
    and builds its closing claim from ``measured``, so collapsing them is what let
    a pass that measured nothing report that it measured everything it tried.
    """

    @pytest.mark.asyncio
    async def test_the_status_payload_carries_both_counts(self) -> None:
        body = _payload(await mcp_mod.api_mcp_measure_progress(_request(method="GET")))
        assert body["ok"] is True
        # Named individually: a reader of this test should see that the two counts
        # are both on the wire, which is the whole change.
        assert "done" in body, body
        assert "measured" in body, body
        assert "total" in body, body

    @pytest.mark.asyncio
    async def test_starting_a_pass_clears_the_previous_pass_counts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale ``measured`` would render as this pass's result.

        The progress dict outlives the pass that wrote it, and the closing line is
        built from ``measured``, so a new pass that has not measured anything yet
        would inherit the previous pass's number and close on it.
        """
        # Stand in for a finished earlier pass. Restored by monkeypatch, so this
        # cannot leak into another test through the module global.
        monkeypatch.setitem(mcp_mod._measure_progress, "running", False)
        monkeypatch.setitem(mcp_mod._measure_progress, "done", 7)
        monkeypatch.setitem(mcp_mod._measure_progress, "measured", 7)
        monkeypatch.setitem(mcp_mod._measure_progress, "total", 7)
        monkeypatch.setitem(mcp_mod._measure_progress, "error", "RuntimeError")

        # The pass itself spawns processes; this test is about the reset, so the
        # background body is replaced rather than run.
        started = asyncio.Event()

        async def _noop() -> None:
            started.set()

        monkeypatch.setattr(mcp_mod, "_bg_measure_all", _noop)

        state = _State()
        body = _payload(await mcp_mod.api_mcp_measure_start(_request(method="POST", state=state)))
        assert body["ok"] is True and body["running"] is True
        assert body["measured"] == 0, body
        assert body["done"] == 0, body
        assert body["error"] == "", body

        # Let the stubbed task run so it does not outlive the test.
        await await_until(started, what="the stubbed measure-all body starting")
        await asyncio.gather(*state._background_tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_a_second_start_while_running_is_reported_not_queued(self) -> None:
        """Refusing is what keeps a second press from doubling the spawn load."""
        import contextlib

        @contextlib.contextmanager
        def _running():
            prior = dict(mcp_mod._measure_progress)
            mcp_mod._measure_progress.update(running=True, done=1, measured=1, total=4)
            try:
                yield
            finally:
                mcp_mod._measure_progress.clear()
                mcp_mod._measure_progress.update(prior)

        with _running():
            body = _payload(await mcp_mod.api_mcp_measure_start(_request(method="POST")))
        assert body["ok"] is False and body["running"] is True
        # The in-flight pass's own numbers, not a reset: the operator pressing a
        # second time is still watching the first pass.
        assert (body["measured"], body["done"], body["total"]) == (1, 1, 4), body

    @pytest.mark.asyncio
    async def test_measure_all_survives_a_disabled_row_with_a_malformed_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One disabled row whose config is malformed must not take the pass down.

        The probe response carries a disabled server as an unprobed placeholder so
        the table can list it, and Measure All feeds that response straight to the
        evaluator. A disabled row with a dict for ``command`` made the evaluator
        raise before its per-server boundary, so the readout closed on
        ``AttributeError`` with nothing measured. The healthy row is measured and
        the readout closes clean; the disabled row is never spawned.
        """
        import kiro_crew.mcp_discovery as disc
        import kiro_crew.mcp_gateway.evaluate as ev

        runtime = tmp_path / "runtime"
        runtime.mkdir()
        monkeypatch.setattr(mcp_mod, "records_dir", lambda _socket: runtime)
        rows = [
            McpServerInfo(name="good-mcp", command="/bin/true", status="ok"),
            McpServerInfo(
                name="off-mcp", command={"not": "a string"}, disabled=True, status="disabled"
            ),
        ]

        async def _probe_all(**_kw):
            return list(rows)

        monkeypatch.setattr(disc, "probe_all", _probe_all)
        spawned: list[str] = []

        async def _preflight(server):
            spawned.append(server.name)
            return SimpleNamespace(ran=True, caller_sensitive=False, reasons=())

        monkeypatch.setattr(ev, "preflight", _preflight)
        for key, value in (("running", True), ("done", 0), ("measured", 0), ("error", "")):
            monkeypatch.setitem(mcp_mod._measure_progress, key, value)

        await mcp_mod._bg_measure_all()

        progress = dict(mcp_mod._measure_progress)
        assert progress["error"] == "", progress
        assert (progress["measured"], progress["done"]) == (1, 1), progress
        assert progress["running"] is False
        assert spawned == ["good-mcp"]
