"""Tests for _sync_mcp_to_agent and _sync_mcp_to_agent_batch in mcp.py."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.handlers.agents import (
    api_capability_mcp_install,
    api_capability_mcp_uninstall,
)
from kiro_crew.mcp_provenance import without_marker

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def mcp_env(tmp_path: Path):
    """Set up agent config and global mcp.json in tmp_path."""
    agent_cfg = tmp_path / "kirocrew.json"
    mcp_json = tmp_path / "mcp.json"

    agent_cfg.write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "mcpServers": {"builder-mcp": {"command": "builder-mcp"}},
                "tools": ["@builder-mcp"],
                "allowedTools": ["@builder-mcp"],
            }
        )
    )
    mcp_json.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "builder-mcp": {"command": "builder-mcp"},
                    "slack-mcp": {"command": "slack-mcp", "args": []},
                    "outlook-mcp": {"command": "outlook-mcp", "env": {"WRITES": "true"}},
                }
            }
        )
    )

    with (
        patch("kiro_crew.dashboard.handlers.mcp._GLOBAL_MCP_JSON", mcp_json),
        patch(
            "kiro_crew.dashboard.handlers.agents._installed_agent_config", return_value=agent_cfg
        ),
    ):
        yield agent_cfg, mcp_json


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


async def _sync_one_remote(remote, mcp_env, *, owned: bool = True, marked: bool = True) -> dict:
    """Run api_mcp_sync over one remote server and return its written global entry.

    ``owned`` marks the name as managed by Kiro Crew, which is the regime every caller
    here exercises: writes to the kiro-global mcp.json are gated on ownership, so
    an unowned name is deliberately left untouched (see
    TestGlobalWritesAreOwnershipGated).

    ``marked`` stamps the authorship marker onto whatever this server already has
    in the global file, so the default regime is "re-syncing an entry we wrote" --
    the case the sync behaviour tests are about. Pass ``marked=False`` to control
    the marker from the test body, which the collision and reclamation tests do
    because the marker's presence is the thing under test there.
    """
    from kiro_crew.dashboard.handlers.mcp import api_mcp_sync
    from kiro_crew.mcp_discovery import SCOPE_KIROCREW
    from kiro_crew.mcp_provenance import stamp

    _, mcp_json = mcp_env
    if marked:
        data = _load(mcp_json)
        current = data.get("mcpServers", {}).get(remote.name)
        if isinstance(current, dict):
            data["mcpServers"][remote.name] = stamp(current)
            mcp_json.write_text(json.dumps(data))

    req = MagicMock()
    req.app = {"state": MagicMock()}
    _store = {remote.name: {"url": "https://store"}} if owned else {}
    with (
        patch("kiro_crew.mcp_discovery.discover_servers_to_sync", return_value=[remote]),
        patch("kiro_crew.mcp_discovery.sync_to_agent_config", return_value=True),
        patch("kiro_crew.mcp_discovery.register_servers_for_cc"),
        patch(
            "kiro_crew.mcp_discovery._load_mcp_json_by_source",
            return_value={SCOPE_KIROCREW: _store},
        ),
        patch("kiro_crew.dashboard.handlers.mcp._get_mcp_lock") as mock_lock,
        patch("kiro_crew.dashboard.handlers.mcp._write_mcp_json") as mock_write,
        patch("kiro_crew.dashboard.handlers.mcp._sync_mcp_to_agent_batch"),
        patch(
            "kiro_crew.dashboard.handlers.sessions._reset_all_sessions",
            new_callable=AsyncMock,
            return_value=1,
        ),
    ):
        mock_lock.return_value = AsyncMock()
        resp = await api_mcp_sync(req)

    assert resp.status == 200
    return mock_write.call_args.args[0]["mcpServers"][remote.name]


class TestGlobalWritesAreOwnershipGated:
    """Every write to a config surface we do NOT own is gated on ownership.

    Two such surfaces are touched here: the kiro-global ``mcp.json`` (through
    ``api_mcp_sync``) and the Claude Code ``~/.mcp.json`` sidecar (through
    ``register_servers_for_cc``). Both are fed by the SAME source --
    ``discover_servers_to_sync``, which merges every scope -- so a name the user
    configured only in their own global file arrives exactly like a managed one.

    Parametrized on purpose: the gate was previously reasoned about one write site
    at a time, so a per-site test lets the next site ship ungated. This asserts
    the property across all of them at once.
    """

    @staticmethod
    def _own(names: set[str]):
        """Patch the store scope so ``kirocrew_managed_names`` sees exactly ``names``."""
        from kiro_crew.mcp_discovery import SCOPE_KIROCREW

        return patch(
            "kiro_crew.mcp_discovery._load_mcp_json_by_source",
            return_value={SCOPE_KIROCREW: {n: {"url": "https://store"} for n in names}},
        )

    async def _kiro_global(self, *, managed: bool, mcp_env, marked: bool | None = None) -> dict:
        """Sync a remote whose url differs from the user's global entry.

        ``marked`` defaults to ``managed``: the managed case represents an entry
        THIS emitter wrote, so it carries the authorship marker, while the
        unmanaged case represents one the user typed and carries none. Pass it
        explicitly to build the third state -- a managed name whose global entry
        we cannot prove we wrote.
        """
        from kiro_crew.mcp_discovery import McpServerInfo
        from kiro_crew.mcp_provenance import stamp

        _, mcp_json = mcp_env
        data = _load(mcp_json)
        entry = {
            "url": "https://user.example.com/mcp",
            "headers": {"Authorization": "Bearer user-typed"},
            "oauthScopes": ["user:scope"],
            "oauth": {"clientId": "user-client", "issuer": "https://user-issuer"},
        }
        data["mcpServers"]["handmade"] = (
            stamp(entry) if (managed if marked is None else marked) else entry
        )
        mcp_json.write_text(json.dumps(data))
        remote = McpServerInfo(
            name="handmade",
            url="https://kirocrew.example.com/mcp",
            scopes=["kirocrew:scope"],
            client_id="kirocrew-client",
            source="discovered",
        )
        return await _sync_one_remote(remote, mcp_env, owned=managed, marked=False)

    def _cc_sidecar(self, *, managed: bool, tmp_path) -> dict:
        """Register a remote into the CC sidecar."""
        from kiro_crew.mcp_discovery import McpServerInfo, register_servers_for_cc

        sidecar = tmp_path / "cc.json"
        remote = McpServerInfo(
            name="handmade",
            url="https://kirocrew.example.com/mcp",
            scopes=["kirocrew:scope"],
            client_id="kirocrew-client",
            source="discovered",
        )
        with self._own({"handmade"} if managed else set()):
            register_servers_for_cc([remote], mcp_json_path=sidecar)
        return json.loads(sidecar.read_text())["mcpServers"]["handmade"]

    @pytest.mark.asyncio
    async def test_an_unmanaged_name_is_never_modified_at_any_global_write_site(
        self, mcp_env, tmp_path
    ):
        """The user's own global config is not ours to rewrite."""
        # Site 1 -- kiro-global mcp.json: the entry must come back byte-identical.
        entry = await self._kiro_global(managed=False, mcp_env=mcp_env)
        assert entry == {
            "url": "https://user.example.com/mcp",
            "headers": {"Authorization": "Bearer user-typed"},
            "oauthScopes": ["user:scope"],
            "oauth": {"clientId": "user-client", "issuer": "https://user-issuer"},
        }

        # Site 2 -- CC sidecar: our OAuth hints are not written for a name we do
        # not own. (The wholesale rebuild of the entry itself is pre-existing
        # behaviour on the base ref, unchanged here.)
        cc = self._cc_sidecar(managed=False, tmp_path=tmp_path)
        assert "scopes" not in cc
        assert "clientId" not in cc

    @pytest.mark.asyncio
    async def test_a_managed_name_still_syncs_at_every_global_write_site(
        self, mcp_env, tmp_path
    ):
        """The gate must not disable the re-sync this change exists to deliver."""
        entry = await self._kiro_global(managed=True, mcp_env=mcp_env)
        assert entry["url"] == "https://kirocrew.example.com/mcp"
        assert entry["oauthScopes"] == ["kirocrew:scope"]
        assert entry["oauth"]["clientId"] == "kirocrew-client"
        assert entry["oauth"]["issuer"] == "https://user-issuer", "sub-key still survives"

        cc = self._cc_sidecar(managed=True, tmp_path=tmp_path)
        assert cc["scopes"] == ["kirocrew:scope"]
        assert cc["clientId"] == "kirocrew-client"

    @pytest.mark.asyncio
    async def test_a_managed_name_with_an_unmarked_entry_is_left_to_the_user(self, mcp_env):
        """The third state: the name is ours, the ENTRY is not provably ours.

        Managing a name is necessary but not sufficient. Here the sync would
        change the entry and nothing on it says we wrote it, so it reads as
        hand-authored and survives untouched -- including the credential header,
        which the name-only gate would have dropped on the url change.
        """
        entry = await self._kiro_global(managed=True, mcp_env=mcp_env, marked=False)
        assert entry == {
            "url": "https://user.example.com/mcp",
            "headers": {"Authorization": "Bearer user-typed"},
            "oauthScopes": ["user:scope"],
            "oauth": {"clientId": "user-client", "issuer": "https://user-issuer"},
        }

    def test_a_malformed_store_value_does_not_make_a_name_managed(self, tmp_path):
        """Same discriminator as the agent-spec path: a non-dict is not ownership."""
        from kiro_crew.mcp_discovery import SCOPE_KIROCREW, kirocrew_managed_names

        with patch(
            "kiro_crew.mcp_discovery._load_mcp_json_by_source",
            return_value={SCOPE_KIROCREW: {"good": {"url": "https://x"}, "bad": "not-a-dict"}},
        ):
            assert kirocrew_managed_names() == {"good"}


class TestExactNameCollisionIsDecidedByTheMarker:
    """The marker separates "ours, moved" from "the user's, colliding".

    Both of these used to arrive at the write as the SAME input -- a global entry
    at url A, that name in the store, and a discovered url B:

    * LEGITIMATE: a managed server whose store url moved A -> B. The global entry
      at A is our own earlier emit and MUST be rewritten, or the re-sync never
      propagates and kiro-cli keeps running a url the dashboard no longer shows.
    * HARMFUL: the user hand-authored a global server at A whose name collides
      with a managed one. Rewriting destroys config we did not author.

    No CONTENT test separates them -- the minimal entry ``{"url": ...}`` is
    exactly what both produce. The difference is authorship, so authorship is now
    recorded rather than inferred: our writes carry ``x-kirocrew``, and an entry
    without it is the user's. These tests pin both directions, and that stripping
    the marker reclaims an entry for good.
    """

    @staticmethod
    async def _sync_over_global(
        entry: object, mcp_env, *, url: str = "https://b.example.net/mcp"
    ) -> Any:
        """Sync a managed remote at ``url`` over an existing global ``entry``.

        ``entry`` is deliberately untyped: a hand-edited file can hold a string
        or ``null`` under a server name, and that shape has to reach the write.
        """
        from kiro_crew.mcp_discovery import McpServerInfo

        _, mcp_json = mcp_env
        data = _load(mcp_json)
        data["mcpServers"]["collide"] = entry
        mcp_json.write_text(json.dumps(data))
        remote = McpServerInfo(name="collide", url=url, source="discovered")
        return await _sync_one_remote(remote, mcp_env, owned=True, marked=False)

    @pytest.mark.asyncio
    async def test_a_managed_url_move_propagates(self, mcp_env):
        """Direction (b): the case that MUST keep working (round-16 contract).

        The entry carries our marker, so the move is provably ours to make.
        """
        from kiro_crew.mcp_provenance import stamp

        written = await self._sync_over_global(
            stamp({"url": "https://a.example.com/mcp"}), mcp_env
        )
        assert written["url"] == "https://b.example.net/mcp", (
            "a managed server's url legitimately moves; refusing to write it "
            "would strand kiro-cli on the old transport"
        )

    @pytest.mark.asyncio
    async def test_a_colliding_hand_authored_global_entry_is_preserved(self, mcp_env):
        """Direction (a): the harm, now prevented.

        Reachability was already bounded -- ``POST /api/mcp/custom`` refuses a
        name that ``_find_server_spec_anywhere`` finds in ANY scope, the kiro
        global file included -- but the residual case, a user editing their own
        global file AFTER the managed entry exists, is what this closes.
        """
        entry = {
            "url": "https://a.example.com/mcp",
            "oauthScopes": ["user:scope"],
            "oauth": {"clientId": "user-client", "issuer": "https://user-issuer"},
        }
        written = await self._sync_over_global(dict(entry), mcp_env)
        assert written == entry, "an unmarked entry is the user's, verbatim"

    @pytest.mark.asyncio
    async def test_a_non_hint_oauth_subkey_is_not_authorship_evidence(self, mcp_env):
        """Why "preserve an entry carrying ``issuer``" is still not the fix.

        A non-hint sub-key looks like a fingerprint -- no writer here emits one.
        It is still not authorship: a MANAGED entry legitimately carries
        ``issuer`` once a user adds it, which is why ``apply_kiro_oauth_hints``
        edits ``oauth`` surgically instead of replacing it. Preserving on that
        signal would strand every managed server whose ``oauth`` a user has ever
        touched, so a MARKED entry carrying one must still sync. The marker, not
        the sub-key, is what decides.
        """
        from kiro_crew.mcp_provenance import stamp

        written = await self._sync_over_global(
            stamp({"url": "https://a.example.com/mcp", "oauth": {"issuer": "https://i"}}),
            mcp_env,
        )
        assert written["url"] == "https://b.example.net/mcp"
        assert written["oauth"] == {"issuer": "https://i"}

    @pytest.mark.asyncio
    async def test_an_unmarked_bare_url_collision_is_preserved(self, mcp_env):
        """The residual case from the base ref, now decided.

        A minimal ``{"url": ...}`` entry is both what this emitter writes for a
        managed server carrying no OAuth hints and the smallest thing a user can
        hand-type. On the base ref the managed url propagated over it, because
        nothing on disk recorded authorship. The marker records it, so the same
        bytes now read as the user's and are left alone -- while
        ``test_a_managed_url_move_propagates`` shows the marked spelling of the
        same shape still propagating.
        """
        written = await self._sync_over_global({"url": "https://a.example.com/mcp"}, mcp_env)
        assert written == {"url": "https://a.example.com/mcp"}

    @pytest.mark.asyncio
    async def test_stripping_the_marker_reclaims_the_entry_for_good(self, mcp_env):
        """Reclamation is durable across syncs, which is what forbids migration.

        The marker promises a user can strip it and we stop rewriting. Stripping
        it off one of our own entries leaves exactly our emit behind, so the
        obvious migration for pre-marker entries -- stamp an unmarked entry the
        sync would not change -- reads this reclaimed entry as legacy and takes it
        back, after which a later url move rewrites it. The two states are
        indistinguishable on disk, so no unmarked entry is written at all: legacy
        entries stay unmanaged (re-established with Disconnect then Connect, which
        creates stamped) and reclamation holds.
        """
        from kiro_crew.mcp_provenance import is_marked, stamp, without_marker

        settled = await self._sync_over_global(
            stamp({"url": "https://b.example.net/mcp"}), mcp_env
        )
        assert is_marked(settled), "precondition: an entry we wrote and re-synced"

        reclaimed = without_marker(settled)
        after = await self._sync_over_global(dict(reclaimed), mcp_env)
        assert not is_marked(after), "the marker must not come back on its own"
        assert after == reclaimed, "and nothing else may change either"

        # The step that makes re-stamping harmful: a stamped entry is rewritable,
        # so had the previous sync taken it back, this url move would land on it.
        moved = await self._sync_over_global(dict(after), mcp_env, url="https://c.example.org/mcp")
        assert moved == reclaimed, "a reclaimed entry outlives a later store-side move"

    @pytest.mark.asyncio
    async def test_a_marked_entry_does_not_re_write_on_every_sync(self, mcp_env):
        """The marker must not make a settled entry look permanently divergent.

        The rendered agent spec carries no marker while the shared file does, so a
        divergence check that saw the key would report this server as changed on
        every pass and re-write the file forever. ``McpServerInfo`` has explicit
        fields, so an unknown key never reaches it -- this pins that, because the
        failure mode is silent write amplification rather than a wrong value.
        """
        from kiro_crew.mcp_provenance import is_marked, stamp

        first = await self._sync_over_global(stamp({"url": "https://b.example.net/mcp"}), mcp_env)
        assert is_marked(first)
        second = await self._sync_over_global(first, mcp_env)
        assert second == first, "a settled marked entry is byte-stable across syncs"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("site", ["kiro_global", "cc_sidecar"])
    async def test_a_marked_managed_url_move_reaches_both_surfaces(
        self, site, mcp_env, tmp_path
    ):
        """Both global surfaces re-sync a marked entry, and are asserted together.

        The base ref answered the exact-name shape differently at each: the
        kiro-global file re-synced, the Claude Code sidecar stayed add-only for
        remotes because re-syncing needed a record of which entries we wrote.
        That record now exists, so both surfaces propagate. Pinning them together
        means a change to either one has to say so.
        """
        from kiro_crew.mcp_provenance import stamp

        if site == "kiro_global":
            written = await self._sync_over_global(
                stamp({"url": "https://a.example.com/mcp"}), mcp_env
            )
        else:
            from kiro_crew.mcp_discovery import McpServerInfo, register_servers_for_cc

            sidecar = tmp_path / "cc.json"
            sidecar.write_text(
                json.dumps(
                    {"mcpServers": {"collide": stamp({"url": "https://a.example.com/mcp"})}}
                )
            )
            remote = McpServerInfo(
                name="collide", url="https://b.example.net/mcp", source="discovered"
            )
            with TestGlobalWritesAreOwnershipGated._own({"collide"}):
                register_servers_for_cc([remote], mcp_json_path=sidecar)
            written = json.loads(sidecar.read_text())["mcpServers"]["collide"]
        assert written["url"] == "https://b.example.net/mcp"

    def test_an_unmanaged_remote_keeps_its_sidecar_entry(self, tmp_path, monkeypatch):
        """The sidecar is add-only for remotes, so a present entry is untouched.

        ``register_servers_for_cc`` builds each remote entry from scratch, so
        anything it rewrote would lose the fields it does not reconstruct. It
        rewrites nothing that is already there: the user's own server, credential
        header and all, survives a sync verbatim -- even though the entry does
        diverge from the discovered source and does enter the sync set for the
        surfaces that can act on it.
        """
        from kiro_crew import mcp_discovery as md

        agents = tmp_path / "agents"
        agents.mkdir()
        # The agent config holds an OLDER url, so divergence genuinely exists.
        (agents / "defaults.json").write_text(
            json.dumps({"mcpServers": {"handmade": {"url": "https://old.example/mcp"}}})
        )
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        user_entry = {
            "url": "https://user.example.com/mcp",
            "headers": {"Authorization": "Bearer user-typed"},
        }
        source = tmp_path / "user_mcp.json"
        source.write_text(json.dumps({"mcpServers": {"handmade": user_entry}}))
        monkeypatch.setattr(md, "_MCP_JSON_PATHS", (source,))
        # Present in the user's kiro-global scope only — never in the store.
        monkeypatch.setattr(
            md,
            "_load_mcp_json_by_source",
            lambda: {
                md.SCOPE_KIROCREW: {},
                md.SCOPE_KIRO_GLOBAL: {"handmade": user_entry},
                md.SCOPE_CC_GLOBAL: {},
            },
        )

        to_sync = md.discover_servers_to_sync()

        sidecar = tmp_path / "cc.json"
        sidecar.write_text(json.dumps({"mcpServers": {"handmade": user_entry}}))
        md.register_servers_for_cc(to_sync, mcp_json_path=sidecar)
        written = json.loads(sidecar.read_text())["mcpServers"]["handmade"]
        assert written == user_entry, "url and credential header survive the sync"

    def test_a_managed_remote_re_syncs_to_the_sidecar_only_when_marked(
        self, tmp_path, monkeypatch
    ):
        """A marked entry re-syncs here; an unmarked one still does not.

        The base ref declined both, because ownership was name-only: "our managed
        server moved" and "a different server the user named the same" were the
        same input, and this writer replaces an entry rather than merging into it.
        The marker separates them, so the divergence that already had to reach the
        agent config and the kiro-global file now reaches this surface too --
        unless the entry carries no marker, in which case add-only still holds.
        """
        from kiro_crew import mcp_discovery as md
        from kiro_crew.mcp_provenance import stamp, without_marker

        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "defaults.json").write_text(
            json.dumps({"mcpServers": {"owned": {"url": "https://old.example/mcp"}}})
        )
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        store_entry = {"url": "https://new.example.net/mcp"}
        source = tmp_path / "store_mcp.json"
        source.write_text(json.dumps({"mcpServers": {"owned": store_entry}}))
        monkeypatch.setattr(md, "_MCP_JSON_PATHS", (source,))
        monkeypatch.setattr(
            md,
            "_load_mcp_json_by_source",
            lambda: {
                md.SCOPE_KIROCREW: {"owned": store_entry},
                md.SCOPE_KIRO_GLOBAL: {},
                md.SCOPE_CC_GLOBAL: {},
            },
        )

        to_sync = md.discover_servers_to_sync()

        assert [s.name for s in to_sync] == ["owned"], (
            "the divergence must still reach the agent config and kiro-global"
        )
        assert to_sync[0].url == "https://new.example.net/mcp"

        stale = {"url": "https://old.example/mcp", "disabledTools": ["danger"]}
        sidecar = tmp_path / "cc.json"

        # Unmarked: nothing proves we wrote it, so add-only still applies.
        sidecar.write_text(json.dumps({"mcpServers": {"owned": dict(stale)}}))
        assert md.register_servers_for_cc(to_sync, mcp_json_path=sidecar) is False
        assert json.loads(sidecar.read_text())["mcpServers"]["owned"] == stale

        # Marked: ours, so the moved url propagates. ``disabledTools`` is NOT
        # merged back -- this writer rebuilds a remote entry from scratch, which is
        # exactly why it may only ever touch an entry it wrote.
        sidecar.write_text(json.dumps({"mcpServers": {"owned": stamp(dict(stale))}}))
        assert md.register_servers_for_cc(to_sync, mcp_json_path=sidecar) is True
        written = json.loads(sidecar.read_text())["mcpServers"]["owned"]
        assert without_marker(written) == {"url": "https://new.example.net/mcp"}

    def test_a_remote_with_no_sidecar_entry_is_still_registered(self, tmp_path):
        """Add-only is not no-op: a name absent here is registered as base does.

        The hints ride along on this path only, so a first registration still
        carries the scopes the card asked for -- and the entry is stamped, because
        we are the ones authoring it. That stamp is what lets the next sync tell
        this entry apart from one the user typed.
        """
        from kiro_crew.mcp_discovery import McpServerInfo, register_servers_for_cc
        from kiro_crew.mcp_provenance import is_marked, without_marker

        sidecar = tmp_path / "cc.json"
        sidecar.write_text(json.dumps({"mcpServers": {}}))
        remote = McpServerInfo(
            name="fresh",
            url="https://fresh.example.net/mcp",
            scopes=["read:me"],
            client_id="cid",
            source="discovered",
        )
        with TestGlobalWritesAreOwnershipGated._own({"fresh"}):
            assert register_servers_for_cc([remote], mcp_json_path=sidecar) is True
        written = json.loads(sidecar.read_text())["mcpServers"]["fresh"]
        assert is_marked(written)
        assert without_marker(written) == {
            "url": "https://fresh.example.net/mcp",
            "scopes": ["read:me"],
            "clientId": "cid",
        }


class TestAPresentButUnreadableEntryIsNotACreate:
    """A malformed entry occupies the name, so writing over it is not a create.

    Both shared files are hand-edited, so a name can hold a string, a ``null`` or
    a list. Such a value cannot carry the marker, so by the invariant it is
    unmarked -- the user's -- and the create branch must not claim it. Absence
    gets its own signal (``ABSENT``) precisely because ``None`` is a value the
    user can type.
    """

    @pytest.mark.asyncio
    async def test_a_malformed_kiro_global_entry_survives_a_managed_sync(self, mcp_env):
        written = await TestExactNameCollisionIsDecidedByTheMarker._sync_over_global(
            "not-a-dict", mcp_env
        )
        assert written == "not-a-dict"

    def test_a_malformed_sidecar_entry_survives_a_managed_sync(self, tmp_path):
        from kiro_crew.mcp_discovery import McpServerInfo, register_servers_for_cc

        sidecar = tmp_path / "cc.json"
        sidecar.write_text(json.dumps({"mcpServers": {"collide": "not-a-dict"}}))
        remote = McpServerInfo(
            name="collide", url="https://b.example.net/mcp", source="discovered"
        )
        with TestGlobalWritesAreOwnershipGated._own({"collide"}):
            assert register_servers_for_cc([remote], mcp_json_path=sidecar) is False
        assert json.loads(sidecar.read_text())["mcpServers"]["collide"] == "not-a-dict"

    def test_a_null_sidecar_entry_is_not_mistaken_for_absence(self, tmp_path):
        """The shape that made ``None`` unusable as the absence signal."""
        from kiro_crew.mcp_discovery import McpServerInfo, register_servers_for_cc

        sidecar = tmp_path / "cc.json"
        sidecar.write_text(json.dumps({"mcpServers": {"collide": None}}))
        remote = McpServerInfo(
            name="collide", url="https://b.example.net/mcp", source="discovered"
        )
        with TestGlobalWritesAreOwnershipGated._own({"collide"}):
            assert register_servers_for_cc([remote], mcp_json_path=sidecar) is False
        assert json.loads(sidecar.read_text())["mcpServers"]["collide"] is None


class TestSidecarStdioWritesAreGatedToo:
    """The gate is per ENTRY, not per transport.

    ``register_servers_for_cc`` rewrites a diverging stdio entry in place, so the
    same collision the marker exists to prevent for remotes -- a user's own server
    sharing a managed name -- reaches this branch as well. Nothing about a
    ``command`` makes authorship knowable, so it resolves the same way.
    """

    @staticmethod
    def _sync_stdio(sidecar: Path, on_disk: object, managed: bool = True) -> bool:
        from kiro_crew.mcp_discovery import McpServerInfo, register_servers_for_cc

        sidecar.write_text(json.dumps({"mcpServers": {"collide": on_disk}}))
        local = McpServerInfo(name="collide", command="/opt/ours", source="discovered")
        with TestGlobalWritesAreOwnershipGated._own({"collide"} if managed else set()):
            return register_servers_for_cc([local], mcp_json_path=sidecar)

    def test_a_colliding_hand_authored_stdio_entry_is_preserved(self, tmp_path, caplog):
        theirs = {"command": "/usr/local/bin/theirs", "args": ["--their-flag"]}
        sidecar = tmp_path / "cc.json"
        with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_provenance"):
            assert self._sync_stdio(sidecar, dict(theirs)) is False
        assert json.loads(sidecar.read_text())["mcpServers"]["collide"] == theirs
        assert "collide" in caplog.text

    def test_a_marked_stdio_entry_still_re_syncs(self, tmp_path):
        """The propagation the gate must not break."""
        from kiro_crew.mcp_provenance import stamp, without_marker

        sidecar = tmp_path / "cc.json"
        assert self._sync_stdio(sidecar, stamp({"command": "/opt/old"})) is True
        written = json.loads(sidecar.read_text())["mcpServers"]["collide"]
        assert without_marker(written) == {"command": "/opt/ours", "args": [], "type": "stdio"}

    def test_an_absent_stdio_name_is_created_stamped(self, tmp_path):
        from kiro_crew.mcp_provenance import is_marked

        sidecar = tmp_path / "cc.json"
        sidecar.write_text(json.dumps({"mcpServers": {}}))
        from kiro_crew.mcp_discovery import McpServerInfo, register_servers_for_cc

        local = McpServerInfo(name="fresh", command="/opt/ours", source="discovered")
        with TestGlobalWritesAreOwnershipGated._own({"fresh"}):
            assert register_servers_for_cc([local], mcp_json_path=sidecar) is True
        assert is_marked(json.loads(sidecar.read_text())["mcpServers"]["fresh"])

    def test_an_unmanaged_stdio_name_is_never_stamped(self, tmp_path):
        """Add-only for a name we do not manage, and no marker claiming it."""
        from kiro_crew.mcp_provenance import is_marked

        sidecar = tmp_path / "cc.json"
        sidecar.write_text(json.dumps({"mcpServers": {}}))
        from kiro_crew.mcp_discovery import McpServerInfo, register_servers_for_cc

        local = McpServerInfo(name="theirs", command="/opt/ours", source="discovered")
        with TestGlobalWritesAreOwnershipGated._own(set()):
            assert register_servers_for_cc([local], mcp_json_path=sidecar) is True
        written = json.loads(sidecar.read_text())["mcpServers"]["theirs"]
        assert not is_marked(written)
        assert written == {"command": "/opt/ours", "args": [], "type": "stdio"}


class TestSyncMcpToAgent:
    def test_enable_adds_server_and_tool_refs(self, mcp_env):
        agent_cfg, _ = mcp_env
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent

        _sync_mcp_to_agent("slack-mcp", enabled=True)
        cfg = _load(agent_cfg)
        assert "slack-mcp" in cfg["mcpServers"]
        assert "@slack-mcp" in cfg["tools"]
        assert "@slack-mcp" in cfg["allowedTools"]

    def test_enable_preserves_existing_server(self, mcp_env):
        agent_cfg, _ = mcp_env
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent

        _sync_mcp_to_agent("builder-mcp", enabled=True)
        cfg = _load(agent_cfg)
        assert cfg["mcpServers"]["builder-mcp"] == {"command": "builder-mcp"}

    def test_enable_strips_disabled_key(self, mcp_env):
        agent_cfg, mcp_json = mcp_env
        d = json.loads(mcp_json.read_text(encoding="utf-8"))
        d["mcpServers"]["slack-mcp"]["disabled"] = True
        mcp_json.write_text(json.dumps(d))
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent

        _sync_mcp_to_agent("slack-mcp", enabled=True)
        cfg = _load(agent_cfg)
        assert "disabled" not in cfg["mcpServers"]["slack-mcp"]

    def test_enable_noop_when_already_present(self, mcp_env):
        agent_cfg, _ = mcp_env
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent

        _sync_mcp_to_agent("builder-mcp", enabled=True)
        cfg = _load(agent_cfg)
        assert cfg["tools"].count("@builder-mcp") == 1

    def test_disable_removes_tool_refs(self, mcp_env):
        agent_cfg, _ = mcp_env
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent

        _sync_mcp_to_agent("builder-mcp", enabled=False)
        cfg = _load(agent_cfg)
        assert "@builder-mcp" not in cfg["tools"]
        assert "@builder-mcp" not in cfg["allowedTools"]

    def test_remove_deletes_server_entry(self, mcp_env):
        agent_cfg, _ = mcp_env
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent

        _sync_mcp_to_agent("builder-mcp", enabled=False, remove=True)
        cfg = _load(agent_cfg)
        assert "builder-mcp" not in cfg["mcpServers"]

    def test_enable_returns_early_on_missing_mcp_json(self, mcp_env):
        agent_cfg, mcp_json = mcp_env
        mcp_json.unlink()
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent

        _sync_mcp_to_agent("slack-mcp", enabled=True)
        cfg = _load(agent_cfg)
        assert "slack-mcp" not in cfg.get("mcpServers", {})


class TestSyncMcpToAgentBatch:
    def test_enable_adds_multiple_servers(self, mcp_env):
        agent_cfg, _ = mcp_env
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent_batch

        _sync_mcp_to_agent_batch(["slack-mcp", "outlook-mcp"], enabled=True)
        cfg = _load(agent_cfg)
        assert "slack-mcp" in cfg["mcpServers"]
        assert "outlook-mcp" in cfg["mcpServers"]
        assert "@slack-mcp" in cfg["tools"]
        assert "@outlook-mcp" in cfg["allowedTools"]

    def test_disable_removes_multiple_tool_refs(self, mcp_env):
        agent_cfg, _ = mcp_env
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent_batch

        _sync_mcp_to_agent_batch(["builder-mcp"], enabled=False)
        cfg = _load(agent_cfg)
        assert "@builder-mcp" not in cfg["tools"]

    def test_enable_with_missing_mcp_json_still_adds_tool_refs(self, mcp_env):
        """Post #15 fix: existing servers get tool refs even when mcp.json missing."""
        agent_cfg, mcp_json = mcp_env
        mcp_json.unlink()
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent_batch

        _sync_mcp_to_agent_batch(["builder-mcp"], enabled=True)
        cfg = _load(agent_cfg)
        # builder-mcp already in mcpServers, should still get tool ref
        assert "@builder-mcp" in cfg["tools"]

    def test_enable_skips_invalid_spec(self, mcp_env):
        agent_cfg, mcp_json = mcp_env
        d = json.loads(mcp_json.read_text(encoding="utf-8"))
        d["mcpServers"]["bad-server"] = "not-a-dict"
        mcp_json.write_text(json.dumps(d))
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent_batch

        _sync_mcp_to_agent_batch(["bad-server"], enabled=True)
        cfg = _load(agent_cfg)
        assert "bad-server" not in cfg["mcpServers"]

    def test_noop_returns_without_write(self, mcp_env):
        agent_cfg, _ = mcp_env
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent_batch

        _sync_mcp_to_agent_batch(["builder-mcp"], enabled=True)
        cfg = _load(agent_cfg)
        assert "@builder-mcp" in cfg["tools"]


class TestAimMcpInstallSync:

    @staticmethod
    def _mgr(ok: bool):
        from kiro_crew.platform.interfaces import CapabilityResult

        m = MagicMock()
        m.available.return_value = True
        m.install_mcp = AsyncMock(
            return_value=CapabilityResult(ok=ok, message="" if ok else "install failed")
        )
        m.uninstall_mcp = AsyncMock(
            return_value=CapabilityResult(ok=ok, message="" if ok else "uninstall failed")
        )
        return m

    @pytest.mark.asyncio
    async def test_install_calls_sync(self):
        req = MagicMock()
        req.json = AsyncMock(return_value={"server_id": "meetings-mcp"})
        req.app = {"state": MagicMock()}
        with (
            patch(
                "kiro_crew.dashboard.handlers.agents._capability_manager",
                return_value=self._mgr(ok=True),
            ),
            patch("kiro_crew.dashboard.handlers.mcp._sync_mcp_to_agent") as mock_sync,
        ):
            resp = await api_capability_mcp_install(req)

        assert resp.status == 200
        mock_sync.assert_called_once_with("meetings-mcp", True)

    @pytest.mark.asyncio
    async def test_install_no_sync_on_aim_failure(self):
        req = MagicMock()
        req.json = AsyncMock(return_value={"server_id": "bad-mcp"})
        req.app = {"state": MagicMock()}
        with (
            patch(
                "kiro_crew.dashboard.handlers.agents._capability_manager",
                return_value=self._mgr(ok=False),
            ),
            patch("kiro_crew.dashboard.handlers.mcp._sync_mcp_to_agent") as mock_sync,
        ):
            resp = await api_capability_mcp_install(req)

        assert resp.status == 500
        mock_sync.assert_not_called()

    @pytest.mark.asyncio
    async def test_uninstall_calls_sync_with_remove(self):
        req = MagicMock()
        req.json = AsyncMock(return_value={"server_id": "meetings-mcp"})
        req.app = {"state": MagicMock()}
        with (
            patch(
                "kiro_crew.dashboard.handlers.agents._capability_manager",
                return_value=self._mgr(ok=True),
            ),
            patch("kiro_crew.dashboard.handlers.mcp._sync_mcp_to_agent") as mock_sync,
        ):
            resp = await api_capability_mcp_uninstall(req)

        assert resp.status == 200
        mock_sync.assert_called_once_with("meetings-mcp", False, remove=True)

    @pytest.mark.asyncio
    async def test_uninstall_no_sync_on_aim_failure(self):
        req = MagicMock()
        req.json = AsyncMock(return_value={"server_id": "bad-mcp"})
        req.app = {"state": MagicMock()}
        with (
            patch(
                "kiro_crew.dashboard.handlers.agents._capability_manager",
                return_value=self._mgr(ok=False),
            ),
            patch("kiro_crew.dashboard.handlers.mcp._sync_mcp_to_agent") as mock_sync,
        ):
            resp = await api_capability_mcp_uninstall(req)

        assert resp.status == 500
        mock_sync.assert_not_called()


class TestApiMcpSyncToolsUpdate:

    @pytest.mark.asyncio
    async def test_sync_adds_tools_for_discovered_servers(self, mcp_env):
        """api_mcp_sync should call _sync_mcp_to_agent_batch for new servers."""
        from kiro_crew.dashboard.handlers.mcp import api_mcp_sync

        agent_cfg, _ = mcp_env

        mock_server = MagicMock()
        mock_server.name = "aws-outlook-mcp"
        mock_server.command = "aws-outlook-mcp"
        mock_server.args = []
        mock_server.env = {}
        mock_server.is_remote = False

        req = MagicMock()
        req.app = {"state": MagicMock()}

        with (
            patch(
                "kiro_crew.mcp_discovery.discover_servers_to_sync",
                return_value=[mock_server],
            ),
            patch(
                "kiro_crew.mcp_discovery.sync_to_agent_config",
                return_value=True,
            ),
            patch("kiro_crew.mcp_discovery.register_servers_for_cc"),
            patch("kiro_crew.dashboard.handlers.mcp._get_mcp_lock") as mock_lock,
            patch("kiro_crew.dashboard.handlers.mcp._write_mcp_json"),
            patch(
                "kiro_crew.dashboard.handlers.mcp._sync_mcp_to_agent_batch",
            ) as mock_batch,
            patch(
                "kiro_crew.dashboard.handlers.sessions._reset_all_sessions",
                new_callable=AsyncMock,
                return_value=1,
            ),
        ):
            mock_lock.return_value = AsyncMock()
            resp = await api_mcp_sync(req)

        assert resp.status == 200
        mock_batch.assert_called_once_with(["aws-outlook-mcp"], enabled=True)

    @pytest.mark.asyncio
    async def test_sync_survives_a_malformed_discovered_header_map(self, mcp_env):
        """A hand-edited non-dict ``headers`` must not abort the whole sync.

        Discovery coerces only a FALSY headers value, so a truthy non-dict (a
        string, a list) reaches the writer as-is. Such a value carries no usable
        credential, so it is read as "discovery said nothing" and the on-disk map
        is preserved -- the same ranking the empty-map case uses. Rejecting the
        sync instead would let one typo in one entry block every other server's
        legitimate re-sync.
        """
        from kiro_crew.mcp_discovery import McpServerInfo

        _, mcp_json = mcp_env
        data = _load(mcp_json)
        data["mcpServers"]["remote"] = {
            "url": "https://mcp.example.com/v1",
            "headers": {"Authorization": "Bearer user-typed"},
        }
        mcp_json.write_text(json.dumps(data))

        for bad in ("not-a-dict", ["Authorization", "Bearer x"]):
            remote = McpServerInfo(
                name="remote",
                url="https://mcp.example.com/v1",
                headers=bad,  # type: ignore[arg-type]
                source="discovered",
            )
            written = await _sync_one_remote(remote, mcp_env)
            assert written["headers"] == {"Authorization": "Bearer user-typed"}, (
                f"a malformed {type(bad).__name__} must preserve the on-disk map"
            )

    def test_discovery_can_deliver_a_non_dict_header_map(self, tmp_path: Path):
        """The writer's guard is reachable: discovery does not coerce a truthy non-dict."""
        from kiro_crew.mcp_discovery import discover_servers_to_sync

        with (
            patch(
                "kiro_crew.mcp_discovery._load_mcp_json",
                return_value={"remote": {"url": "https://mcp.example.com/v1", "headers": "bad"}},
            ),
            patch("kiro_crew.mcp_discovery._load_agent_config", return_value={"mcpServers": {}}),
        ):
            out = discover_servers_to_sync()
        assert [s.headers for s in out] == ["bad"], "a truthy non-dict passes through unchanged"

    @pytest.mark.asyncio
    async def test_sync_preserves_a_global_only_header(self, mcp_env):
        """A credential the user typed into their own global file must survive.

        Discovery reads the MERGED view, so the store's header map is what arrives
        here; a header present only in the kiro-global file is absent from it.
        Replacing the map wholesale would delete that credential outright.
        """
        from kiro_crew.mcp_discovery import McpServerInfo

        _, mcp_json = mcp_env
        data = _load(mcp_json)
        data["mcpServers"]["remote"] = {
            "url": "https://mcp.example.com/v1",
            "headers": {"Authorization": "Bearer user-typed"},
        }
        mcp_json.write_text(json.dumps(data))
        remote = McpServerInfo(
            name="remote",
            url="https://mcp.example.com/v1",
            headers={"X-Tenant": "acme"},
            source="discovered",
        )
        written = await _sync_one_remote(remote, mcp_env)
        assert written["headers"] == {
            "Authorization": "Bearer user-typed",
            "X-Tenant": "acme",
        }, "discovered headers overlay, they do not replace"

    @pytest.mark.asyncio
    async def test_sync_does_not_forward_a_global_only_header_to_a_new_host(self, mcp_env):
        """A header map is scoped to the host it was typed for.

        The overlay above deliberately carries a credential the user typed into
        their own global file across a sync. That preservation may not survive a
        change of ``url``: the bearer was issued by -- and for -- the OLD origin,
        so writing it beside a NEW one turns "we kept your credential" into "we
        sent your credential somewhere else". Dropping it costs a re-auth, which
        is recoverable; forwarding it is not.
        """
        from kiro_crew.mcp_discovery import McpServerInfo

        _, mcp_json = mcp_env
        data = _load(mcp_json)
        data["mcpServers"]["remote"] = {
            "url": "https://old.example.com/v1",
            "headers": {"Authorization": "Bearer user-typed"},
        }
        mcp_json.write_text(json.dumps(data))
        remote = McpServerInfo(
            name="remote",
            url="https://new.example.net/v1",
            headers={"X-Tenant": "acme"},
            source="discovered",
        )
        written = await _sync_one_remote(remote, mcp_env)
        assert written["url"] == "https://new.example.net/v1"
        assert written["headers"] == {"X-Tenant": "acme"}, (
            "the bearer was typed for old.example.com -- carrying it onto "
            "new.example.net hands the credential to a different origin"
        )

    @pytest.mark.asyncio
    async def test_a_url_move_drops_prior_headers_even_when_discovery_is_silent(self, mcp_env):
        """The leak also runs through the header-less branch.

        With no discovered headers the overlay never executes, so the prior map
        simply rides along in the copied entry -- same forwarded credential, via
        a path the overlay's guard never sees. The url test has to sit ahead of
        both branches, not inside one.
        """
        from kiro_crew.mcp_discovery import McpServerInfo

        _, mcp_json = mcp_env
        data = _load(mcp_json)
        data["mcpServers"]["remote"] = {
            "url": "https://old.example.com/v1",
            "headers": {"Authorization": "Bearer user-typed"},
        }
        mcp_json.write_text(json.dumps(data))
        remote = McpServerInfo(
            name="remote", url="https://new.example.net/v1", source="discovered"
        )
        written = await _sync_one_remote(remote, mcp_env)
        assert written["url"] == "https://new.example.net/v1"
        assert "headers" not in written, (
            "a credential with no proven relationship to the destination host "
            "must not be written beside it"
        )

    @pytest.mark.asyncio
    async def test_sync_writes_remote_url_and_headers_to_global_config(self, mcp_env):
        """A remote sync must never be serialized as an empty stdio command."""
        from kiro_crew.mcp_discovery import McpServerInfo

        _, mcp_json = mcp_env
        data = _load(mcp_json)
        data["mcpServers"]["remote"] = {
            "url": "https://mcp.example.com/v1",
            "headers": {"Authorization": "Bearer old"},
            "disabled": True,
            "disabledTools": ["write"],
        }
        mcp_json.write_text(json.dumps(data))
        remote = McpServerInfo(
            name="remote",
            url="https://mcp.example.com/v2",
            headers={"Authorization": "Bearer current"},
            source="discovered",
        )
        written = await _sync_one_remote(remote, mcp_env)
        assert without_marker(written) == {
            "url": "https://mcp.example.com/v2",
            "headers": {"Authorization": "Bearer current"},
            "disabled": True,
            "disabledTools": ["write"],
        }
        assert "command" not in written

    @pytest.mark.asyncio
    async def test_sync_writes_remote_oauth_hints_to_global_config(self, mcp_env):
        """Hints reach kiro-cli in the WIRE spellings, or the grant is never requested.

        kiro-cli only deserializes ``oauthScopes`` and ``oauth.clientId`` and drops
        unknown keys silently, so asserting the internal ``scopes``/``clientId``
        spellings here would guard the bug instead of the fix.
        """
        from kiro_crew.mcp_discovery import McpServerInfo

        remote = McpServerInfo(
            name="remote",
            url="https://api.githubcopilot.com/mcp/",
            scopes=["read:user", "read:org"],
            client_id="public-client-id",
            source="discovered",
        )
        written = await _sync_one_remote(remote, mcp_env)
        assert without_marker(written) == {
            "url": "https://api.githubcopilot.com/mcp/",
            "oauthScopes": ["read:user", "read:org"],
            "oauth": {"clientId": "public-client-id"},
        }

    @pytest.mark.asyncio
    async def test_sync_removes_oauth_hints_dropped_upstream(self, mcp_env):
        """Absent upstream means REMOVED, so narrowing a scope actually narrows it."""
        from kiro_crew.mcp_discovery import McpServerInfo

        _, mcp_json = mcp_env
        data = _load(mcp_json)
        data["mcpServers"]["remote"] = {
            "url": "https://mcp.example.com/v1",
            "scopes": ["read", "write"],
            "clientId": "stale-id",
            "disabledTools": ["write"],
        }
        mcp_json.write_text(json.dumps(data))
        remote = McpServerInfo(
            name="remote", url="https://mcp.example.com/v1", source="discovered"
        )
        written = await _sync_one_remote(remote, mcp_env)
        assert without_marker(written) == {
            "url": "https://mcp.example.com/v1",
            "disabledTools": ["write"],
        }

    @pytest.mark.asyncio
    async def test_sync_preserves_a_header_discovery_cannot_see(self, mcp_env):
        """A header-less discovered remote must NOT erase a configured header.

        Discovery reads the dashboard's own mcp.json. A server of the same name
        carrying an Authorization header in the user's global
        ~/.kiro/settings/mcp.json therefore arrives here header-less, and popping
        on that would destroy the only copy of a credential the user typed --
        silently, with nothing to restore it from.
        """
        _, mcp_json = mcp_env
        data = _load(mcp_json)
        data["mcpServers"]["remote"] = {
            "url": "https://mcp.example.com/v1",
            "headers": {"Authorization": "Bearer user-typed"},
        }
        mcp_json.write_text(json.dumps(data))
        from kiro_crew.mcp_discovery import McpServerInfo

        remote = McpServerInfo(
            name="remote", url="https://mcp.example.com/v1", source="discovered"
        )
        written = await _sync_one_remote(remote, mcp_env)
        assert written["headers"] == {"Authorization": "Bearer user-typed"}

    @pytest.mark.asyncio
    async def test_sync_keeps_unrelated_oauth_subkeys_while_rewriting_client_id(self, mcp_env):
        """Only ``clientId`` under ``oauth`` is ours; ``issuer`` is the user's.

        Deleting the whole mapping to rewrite our one sub-key destroys hand-set
        configuration a sync has no business touching.
        """
        _, mcp_json = mcp_env
        data = _load(mcp_json)
        data["mcpServers"]["remote"] = {
            "url": "https://mcp.example.com/v1",
            "oauthScopes": ["read"],
            "oauth": {"issuer": "https://issuer.example.com", "clientId": "old-id"},
        }
        mcp_json.write_text(json.dumps(data))
        from kiro_crew.mcp_discovery import McpServerInfo

        remote = McpServerInfo(
            name="remote",
            url="https://mcp.example.com/v1",
            scopes=["read", "write"],
            client_id="new-id",
            source="discovered",
        )
        written = await _sync_one_remote(remote, mcp_env)
        assert written["oauthScopes"] == ["read", "write"]
        assert written["oauth"] == {"issuer": "https://issuer.example.com", "clientId": "new-id"}

    @pytest.mark.asyncio
    async def test_sync_omits_a_malformed_scope_list_instead_of_truncating_it(self, mcp_env):
        """Row 6 on the sync path: one validation contract, emit and readback.

        A partially-valid list must never be silently narrowed into a different
        grant. Reading ``["read", 7]`` as ``["read"]`` while the emit path omits
        the field entirely would make the synced file request access the source
        never asked for, and the agent spec request none -- two different answers
        from one line of config.
        """
        _, mcp_json = mcp_env
        data = _load(mcp_json)
        data["mcpServers"]["remote"] = {
            "url": "https://mcp.example.com/v1",
            "scopes": ["read", 7],
        }
        mcp_json.write_text(json.dumps(data))
        from kiro_crew.mcp_discovery import _spec_scopes

        assert _spec_scopes(data["mcpServers"]["remote"]) == [], "readback must omit, not truncate"

        from kiro_crew.mcp_discovery import McpServerInfo

        remote = McpServerInfo(
            name="remote",
            url="https://mcp.example.com/v1",
            scopes=_spec_scopes(data["mcpServers"]["remote"]),
            source="discovered",
        )
        written = await _sync_one_remote(remote, mcp_env)
        assert "oauthScopes" not in written
        assert "scopes" not in written

    @pytest.mark.asyncio
    async def test_sync_no_tools_update_when_nothing_discovered(self, mcp_env):
        """api_mcp_sync should not call _sync_mcp_to_agent_batch when empty."""
        from kiro_crew.dashboard.handlers.mcp import api_mcp_sync

        req = MagicMock()
        req.app = {"state": MagicMock()}

        with (
            patch(
                "kiro_crew.mcp_discovery.discover_servers_to_sync",
                return_value=[],
            ),
            patch(
                "kiro_crew.dashboard.handlers.mcp._sync_mcp_to_agent_batch",
            ) as mock_batch,
            patch(
                "kiro_crew.dashboard.handlers.sessions._reset_all_sessions",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            resp = await api_mcp_sync(req)

        assert resp.status == 200
        mock_batch.assert_not_called()


class TestOffloadedSyncHoldsTheConfigLock:
    """`_sync_mcp_to_agent*` does a read-modify-write of kirocrew.json. Offloading
    it to a worker thread means two concurrent MCP requests can interleave, so
    every offloaded call must run inside `_get_config_lock()` — the event loop no
    longer serializes them for free.
    """

    def test_every_offloaded_sync_is_under_the_config_lock(self) -> None:

        lines = (
            (_REPO_ROOT / "src/kiro_crew/dashboard/handlers/mcp.py").read_text(encoding="utf-8").splitlines()
        )
        offenders: list[str] = []
        for i, ln in enumerate(lines):
            if "asyncio.to_thread(" in ln and "_sync_mcp_to_agent" in ln:
                window = "\n".join(lines[max(0, i - 4) : i + 1])
                if "_get_config_lock()" not in window:
                    offenders.append(f"line {i + 1}: {ln.strip()[:70]}")
        assert offenders == [], "offloaded sync without config lock: " + "; ".join(offenders)


class TestSyncSharesTheFileLockWithBridges:
    """The dashboard sync and bridges' app-MCP registration both RMW kirocrew.json.
    They must share ONE file lock; the dashboard's in-process _get_config_lock does
    not coordinate with bridges' cross-process _mcp_lock, so the dashboard paths
    now acquire _mcp_lock too."""

    def test_both_sync_funcs_hold_the_mcp_file_lock(self) -> None:
        import inspect

        from kiro_crew.dashboard.handlers import mcp

        for fn in (mcp._sync_mcp_to_agent, mcp._sync_mcp_to_agent_batch):
            src = inspect.getsource(fn)
            assert "_mcp_lock(target=_installed_agent_config())" in src, fn.__name__


class TestSyncStripsGovernedAutoApprove:
    """Copying a global MCP server into kirocrew.json must not carry a governed
    `autoApprove`: kiro-cli honours it on the copy and auto-approves the server
    without ever reaching the PreToolUse gate."""

    def test_single_sync_strips_autoapprove_when_governed(self) -> None:
        import inspect

        from kiro_crew.dashboard.handlers import mcp

        src = inspect.getsource(mcp._sync_mcp_to_agent_unlocked)
        assert 'entry.pop("autoApprove", None)' in src
        assert "not may_skip_gate_now(tool_ref)" in src

    def test_batch_sync_strips_autoapprove_when_governed(self) -> None:
        import inspect

        from kiro_crew.dashboard.handlers import mcp

        src = inspect.getsource(mcp._sync_mcp_to_agent_batch_unlocked)
        assert '_entry.pop("autoApprove", None)' in src


class TestSyncStripsPreExistingGovernedAutoApprove:
    """A governed autoApprove must be stripped even when the alias ALREADY exists
    in kirocrew.json (re-enable, or a spec written before the ceiling): the copy
    branch only runs for a brand-new alias."""

    def test_existing_entry_autoapprove_is_stripped(self, mcp_env, monkeypatch):
        agent_cfg, _ = mcp_env
        import kiro_crew.dashboard.handlers.mcp as mcp
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent, mcp_server_alias

        alias = mcp_server_alias("slack-mcp")
        cfg = _load(agent_cfg)
        cfg.setdefault("mcpServers", {})[alias] = {"command": "x", "autoApprove": ["danger"]}
        agent_cfg.write_text(json.dumps(cfg))
        monkeypatch.setattr(mcp, "may_skip_gate_now", lambda ref: False)  # governed

        _sync_mcp_to_agent("slack-mcp", enabled=True)
        out = _load(agent_cfg)
        assert "autoApprove" not in out["mcpServers"][alias], "governed grant must be stripped"


class TestGovernedSyncAuditsWithheld:
    """A governed enable withholds auto-approve (mounts in `tools`, keeps the
    ref OUT of allowedTools). The SEL audit must record that WITHHELD decision,
    not a grant — logging mcp_tools_added there falsely reports the opposite.
    """

    class _SelRec:
        def __init__(self) -> None:
            self.events: list[dict] = []

        def log_api_access(self, **kw) -> None:
            self.events.append(kw)

        def ops(self) -> set[str]:
            return {e.get("operation") for e in self.events}

    def test_single_governed_emits_withheld_not_added(self, mcp_env, monkeypatch):
        agent_cfg, _ = mcp_env
        import kiro_crew.dashboard.handlers.mcp as mcp
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent, mcp_server_alias

        rec = self._SelRec()
        monkeypatch.setattr(mcp, "sel", lambda: rec)
        monkeypatch.setattr(mcp, "may_skip_gate_now", lambda ref: False)  # governed

        _sync_mcp_to_agent("slack-mcp", enabled=True)

        ref = f"@{mcp_server_alias('slack-mcp')}"
        out = _load(agent_cfg)
        assert ref in out.get("tools", [])  # mounted
        assert ref not in out.get("allowedTools", [])  # auto-approve withheld
        assert "mcp_auto_approve_withheld" in rec.ops()
        assert "mcp_tools_added" not in rec.ops()

    def test_single_ungoverned_still_emits_added(self, mcp_env, monkeypatch):
        agent_cfg, _ = mcp_env
        import kiro_crew.dashboard.handlers.mcp as mcp
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent, mcp_server_alias

        rec = self._SelRec()
        monkeypatch.setattr(mcp, "sel", lambda: rec)
        monkeypatch.setattr(mcp, "may_skip_gate_now", lambda ref: True)  # ungoverned

        _sync_mcp_to_agent("slack-mcp", enabled=True)

        ref = f"@{mcp_server_alias('slack-mcp')}"
        out = _load(agent_cfg)
        assert ref in out.get("allowedTools", [])
        assert "mcp_tools_added" in rec.ops()
        assert "mcp_auto_approve_withheld" not in rec.ops()

    def test_batch_governed_emits_withheld_not_added(self, mcp_env, monkeypatch):
        agent_cfg, _ = mcp_env
        import kiro_crew.dashboard.handlers.mcp as mcp
        from kiro_crew.dashboard.handlers.mcp import (
            _sync_mcp_to_agent_batch,
            mcp_server_alias,
        )

        rec = self._SelRec()
        monkeypatch.setattr(mcp, "sel", lambda: rec)
        monkeypatch.setattr(mcp, "may_skip_gate_now", lambda ref: False)  # governed

        _sync_mcp_to_agent_batch(["slack-mcp", "outlook-mcp"], enabled=True)

        out = _load(agent_cfg)
        for name in ("slack-mcp", "outlook-mcp"):
            ref = f"@{mcp_server_alias(name)}"
            assert ref in out.get("tools", [])
            assert ref not in out.get("allowedTools", [])
        assert "mcp_auto_approve_withheld" in rec.ops()
        assert "mcp_tools_added" not in rec.ops()


class TestCapabilityInstallOffloadsTheLockedSync:
    """_sync_mcp_to_agent takes bridges' synchronous _mcp_lock for a full
    kirocrew.json RMW. Called directly on the event loop it freezes the gateway
    when a concurrent app registration holds that lock, so every async caller
    MUST offload it to a worker thread."""

    def _assert_offloaded(self, fn: object) -> None:
        import inspect

        src = inspect.getsource(fn)  # type: ignore[arg-type]
        # After removing the offloaded call forms, no bare _sync_mcp_to_agent(
        # call may remain (the from-import has no "(" so it is not matched).
        stripped = src.replace("to_thread(_sync_mcp_to_agent", "").replace(
            "to_thread(lambda: _sync_mcp_to_agent", ""
        )
        assert (
            "_sync_mcp_to_agent(" not in stripped
        ), f"{fn.__name__} calls _sync_mcp_to_agent on the event loop; offload it"

    def test_capability_install_uninstall_offload(self):
        from kiro_crew.dashboard.handlers import agents

        self._assert_offloaded(agents.api_capability_mcp_install)
        self._assert_offloaded(agents.api_capability_mcp_uninstall)

    def test_discover_capability_install_offloads(self):
        from kiro_crew.dashboard.handlers import mcp_discover

        self._assert_offloaded(mcp_discover._install_via_capability)


class TestCcSidecarEnvEmission:
    """The sidecar is a consumed surface — a declared PATH must be complete."""

    def test_stdio_env_path_is_expanded_on_write(self, tmp_path, monkeypatch):
        from kiro_crew import mcp_discovery as md

        monkeypatch.setenv("PATH", "/usr/bin")
        srv = md.McpServerInfo(
            name="tooling",
            command="/opt/bin/tooling",
            args=["--stdio"],
            env={"PATH": "/opt/shims", "TOKEN": "t"},
            source="discovered",
        )
        sidecar = tmp_path / "cc.json"
        assert md.register_servers_for_cc([srv], mcp_json_path=sidecar) is True

        written = json.loads(sidecar.read_text())["mcpServers"]["tooling"]
        entries = written["env"]["PATH"].split(os.pathsep)
        assert entries[0] == "/opt/shims", "spec-authored entries stay first"
        assert "/usr/bin" in entries, "inherited PATH must survive the override"
        assert written["env"]["TOKEN"] == "t"

    def test_source_env_object_is_not_mutated(self, tmp_path, monkeypatch):
        from kiro_crew import mcp_discovery as md

        monkeypatch.setenv("PATH", "/usr/bin")
        source_env = {"PATH": "/opt/shims"}
        srv = md.McpServerInfo(
            name="tooling", command="/opt/bin/tooling", env=source_env, source="discovered"
        )
        md.register_servers_for_cc([srv], mcp_json_path=tmp_path / "cc.json")
        assert source_env == {"PATH": "/opt/shims"}, "emit must not write back into the source"


class TestSyncDiscoveredServers:
    """The one serialized discover→write entry point both handlers share."""

    def test_runs_full_sequence_when_servers_found(self):
        from unittest.mock import patch

        from kiro_crew import mcp_discovery as md

        fake = md.McpServerInfo(name="srv", command="x")
        with (
            patch.object(md, "discover_servers_to_sync", return_value=[fake]) as disc,
            patch.object(md, "sync_to_agent_config") as sync,
            patch.object(md, "register_servers_for_cc") as cc,
        ):
            out = md.sync_discovered_servers()
        assert out == [fake]
        disc.assert_called_once()
        sync.assert_called_once_with([fake])
        cc.assert_called_once_with([fake])

    def test_empty_delta_still_reconciles(self):
        """No new/diverged servers is NOT "nothing to do": a source entry
        gaining ``disabled: true`` yields an empty delta but must still remove
        the server from the agent config — install_agent() is the idempotent
        reconciler, so it runs unconditionally. The additive-only sidecar
        write is the one thing skipped."""
        from unittest.mock import patch

        from kiro_crew import mcp_discovery as md

        with (
            patch.object(md, "discover_servers_to_sync", return_value=[]),
            patch.object(md, "sync_to_agent_config") as sync,
            patch.object(md, "register_servers_for_cc") as cc,
        ):
            out = md.sync_discovered_servers()
        assert out == []
        sync.assert_called_once_with([])
        cc.assert_not_called()

    def test_concurrent_callers_serialize(self):
        """Two threads running the sequence must not interleave — the mutex is
        the fix for the two-handler read-modify-write race."""
        import threading
        from unittest.mock import patch

        from kiro_crew import mcp_discovery as md

        active = []
        overlap = []
        gate = threading.Barrier(2, timeout=5)

        def slow_discover():
            if active:
                overlap.append(True)
            active.append(1)
            import time as _t

            _t.sleep(0.05)
            active.pop()
            return []

        def run():
            gate.wait()
            md.sync_discovered_servers()

        with patch.object(md, "discover_servers_to_sync", side_effect=slow_discover):
            t1, t2 = threading.Thread(target=run), threading.Thread(target=run)
            t1.start(), t2.start()
            t1.join(), t2.join()
        assert not overlap, "the sync mutex must serialize concurrent callers"
