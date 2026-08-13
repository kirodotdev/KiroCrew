"""Tests for the Mochi builtin's backend routes.

Follows the issue-radar route-test pattern: handlers invoked directly with
mocked requests, the enabled-gate and runtime presence stubbed per test.
"""

from __future__ import annotations

import contextlib
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.mochi import hooks
from kiro_crew.apps.builtins.mochi.backend import routes

_REPO_ROOT = Path(__file__).resolve().parents[1]


class _Ctx:
    def __init__(self, tmp_path) -> None:
        self.name = "mochi"
        self.data_dir = tmp_path
        self.events = None
        self.config: dict[str, Any] = {}


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(routes, "is_app_enabled", lambda name: True)
    hooks._runtime = None
    yield
    hooks._runtime = None


@contextlib.asynccontextmanager
async def _live_runtime(tmp_path):
    """Start the runtime inside the test's own loop, stop it on exit.

    An async CONTEXT MANAGER rather than an ``@pytest_asyncio.fixture``: the
    suite pins pytest-asyncio 0.20.3, whose async-fixture wrapper reads the
    ``fixturedef.unittest`` attribute pytest 8.1 removed — on CI every
    async-generator fixture errors at setup. The repo avoids the decorator by
    convention (see test_denied_commands_api.py's module docstring).
    """
    await hooks.on_startup(_Ctx(tmp_path))
    try:
        yield hooks._runtime
    finally:
        await hooks.on_shutdown(None)


def _json_request(method: str, path: str, body: dict | None = None):
    payload = json.dumps(body or {}).encode()
    req = make_mocked_request(
        method, path, headers={"Content-Type": "application/json"}, payload=None
    )

    async def _json():
        if body is None:
            raise ValueError("no body")
        return body

    req.json = _json  # type: ignore[method-assign]
    del payload
    return req


class TestGates:
    @pytest.mark.asyncio
    async def test_disabled_app_403s(self, monkeypatch):
        monkeypatch.setattr(routes, "is_app_enabled", lambda name: False)
        handler = routes._require_enabled(routes._handle_stats_get)
        resp = await handler(make_mocked_request("GET", "/api/apps/mochi/stats"))
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_no_runtime_503s(self):
        handler = routes._require_enabled(routes._handle_stats_get)
        resp = await handler(make_mocked_request("GET", "/api/apps/mochi/stats"))
        assert resp.status == 503


class TestWatchlistRoutes:
    @pytest.mark.asyncio
    async def test_get_empty_watchlist(self, tmp_path):
        async with _live_runtime(tmp_path):
            resp = await routes._handle_watchlist_get(
                make_mocked_request("GET", "/api/apps/mochi/watchlist")
            )
            assert resp.status == 200
            assert json.loads(resp.body) == {"items": []}

    @pytest.mark.asyncio
    async def test_add_then_get_roundtrip(self, tmp_path):
        async with _live_runtime(tmp_path):
            resp = await routes._handle_watchlist_update(
                _json_request(
                    "POST",
                    "/api/apps/mochi/watchlist/update",
                    {"add": [{"label": "Site", "kind": "url", "target": "https://example.com"}]},
                )
            )
            assert resp.status == 200
            data = json.loads(resp.body)
            assert data["updated"] is True
            assert len(data["items"]) == 1
            assert data["items"][0]["label"] == "Site"
            assert data["items"][0]["status"] == "watching"

            resp2 = await routes._handle_watchlist_get(
                make_mocked_request("GET", "/api/apps/mochi/watchlist")
            )
            assert len(json.loads(resp2.body)["items"]) == 1

    @pytest.mark.asyncio
    async def test_agent_authored_credential_is_redacted_in_both_responses(self, tmp_path):
        """An MCP-authored watchlist item carrying a credential must not reach the
        browser verbatim through either the update echo or the GET payload."""
        secret = "AKIAIOSFODNN7EXAMPLE"
        async with _live_runtime(tmp_path):
            upd = await routes._handle_watchlist_update(
                _json_request(
                    "POST",
                    "/api/apps/mochi/watchlist/update",
                    {"add": [{"label": secret, "kind": "custom", "target": "t"}]},
                )
            )
            assert upd.status == 200
            assert secret not in upd.body.decode()

            got = await routes._handle_watchlist_get(
                make_mocked_request("GET", "/api/apps/mochi/watchlist")
            )
            assert got.status == 200
            assert secret not in got.body.decode()

    @pytest.mark.asyncio
    async def test_cancel_via_update(self, tmp_path):
        async with _live_runtime(tmp_path):
            resp = await routes._handle_watchlist_update(
                _json_request(
                    "POST",
                    "/api/apps/mochi/watchlist/update",
                    {"add": [{"label": "x", "kind": "custom", "target": "t"}]},
                )
            )
            item_id = json.loads(resp.body)["items"][0]["id"]
            resp2 = await routes._handle_watchlist_update(
                _json_request("POST", "/api/apps/mochi/watchlist/update", {"cancel": [item_id]})
            )
            items = json.loads(resp2.body)["items"]
            assert items[0]["status"] == "cancelled"
            assert items[0]["completionReason"] == "cancelled"

    @pytest.mark.asyncio
    async def test_remove_via_update(self, tmp_path):
        # The panel Delete button posts {remove: [id]}; the route guard used to
        # reject it (400 invalid_watchlist_op) even though the backend + MCP
        # schema support remove, so the row reappeared on the next refresh.
        async with _live_runtime(tmp_path):
            resp = await routes._handle_watchlist_update(
                _json_request(
                    "POST",
                    "/api/apps/mochi/watchlist/update",
                    {"add": [{"label": "x", "kind": "custom", "target": "t"}]},
                )
            )
            item_id = json.loads(resp.body)["items"][0]["id"]
            resp2 = await routes._handle_watchlist_update(
                _json_request("POST", "/api/apps/mochi/watchlist/update", {"remove": [item_id]})
            )
            assert resp2.status == 200, "remove op must be accepted, not 400"
            assert json.loads(resp2.body)["items"] == [], "removed item must be gone"

    @pytest.mark.asyncio
    async def test_clear_completed_archives_then_removes(self, tmp_path):
        async with _live_runtime(tmp_path):
            # One active + one done item; clear moves the done one to the archive.
            await routes._handle_watchlist_update(
                _json_request(
                    "POST",
                    "/api/apps/mochi/watchlist/update",
                    {
                        "add": [
                            {"label": "live", "kind": "url", "target": "t"},
                            {"label": "finished", "kind": "url", "target": "t"},
                        ]
                    },
                )
            )
            wl = json.loads(
                (
                    await routes._handle_watchlist_get(
                        make_mocked_request("GET", "/api/apps/mochi/watchlist")
                    )
                ).body
            )
            done_id = next(i["id"] for i in wl["items"] if i["label"] == "finished")
            await routes._handle_watchlist_update(
                _json_request(
                    "POST",
                    "/api/apps/mochi/watchlist/update",
                    {"update": [{"id": done_id, "status": "done"}]},
                )
            )

            resp = await routes._handle_watchlist_clear_completed(
                _json_request("POST", "/api/apps/mochi/watchlist/clear-completed", {})
            )
            assert json.loads(resp.body) == {"cleared": 1}

            remaining = json.loads(
                (
                    await routes._handle_watchlist_get(
                        make_mocked_request("GET", "/api/apps/mochi/watchlist")
                    )
                ).body
            )["items"]
            assert [i["label"] for i in remaining] == ["live"]
            archive = json.loads((tmp_path / "mochi-watchlist-archive.json").read_text())
            assert [i["label"] for i in archive["items"]] == ["finished"]
            assert "archivedAt" in archive["items"][0]

    @pytest.mark.asyncio
    async def test_malformed_body_400s(self, tmp_path):
        async with _live_runtime(tmp_path):
            resp = await routes._handle_watchlist_update(
                _json_request("POST", "/api/apps/mochi/watchlist/update", None)
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_empty_op_body_400s(self, tmp_path):
        async with _live_runtime(tmp_path):
            resp = await routes._handle_watchlist_update(
                _json_request("POST", "/api/apps/mochi/watchlist/update", {"nope": 1})
            )
            assert resp.status == 400


class TestStatsAndSoulRoutes:
    @pytest.mark.asyncio
    async def test_stats_shape(self, tmp_path):
        async with _live_runtime(tmp_path):
            resp = await routes._handle_stats_get(
                make_mocked_request("GET", "/api/apps/mochi/stats")
            )
            data = json.loads(resp.body)
            assert data["streak"] == 1
            assert "celebratedMilestones" in data

    @pytest.mark.asyncio
    async def test_soul_default(self, tmp_path):
        async with _live_runtime(tmp_path):
            resp = await routes._handle_soul_get(make_mocked_request("GET", "/api/apps/mochi/soul"))
            data = json.loads(resp.body)
            assert data["petName"] == "Mochi"
            assert data["isDefault"] is True
            assert "companion" in data["soul"]

    @pytest.mark.asyncio
    async def test_pet_state_shape(self, tmp_path):
        async with _live_runtime(tmp_path):
            resp = await routes._handle_pet_state_get(
                make_mocked_request("GET", "/api/apps/mochi/pet-state")
            )
            data = json.loads(resp.body)
            # Runtime start applies 'connect' (offline → idle); mood seeds neutral.
            assert data["state"] == "idle"
            assert data["mood"] == "neutral"
            assert data["silentUntil"] == 0


class TestQuietRoute:
    @pytest.mark.asyncio
    async def test_quiet_round_trip(self, tmp_path):
        async with _live_runtime(tmp_path):
            resp = await routes._handle_quiet(
                _json_request("POST", "/api/apps/mochi/quiet", {"minutes": 60})
            )
            data = json.loads(resp.body)
            assert data["ok"] is True
            assert data["silentUntil"] > 0
            # The state pull the menu makes on open sees the same expiry.
            state = json.loads(
                (
                    await routes._handle_pet_state_get(
                        make_mocked_request("GET", "/api/apps/mochi/pet-state")
                    )
                ).body
            )
            assert state["silentUntil"] == data["silentUntil"]
            # Resume clears it.
            resp = await routes._handle_quiet(
                _json_request("POST", "/api/apps/mochi/quiet", {"minutes": 0})
            )
            assert json.loads(resp.body)["silentUntil"] == 0

    @pytest.mark.asyncio
    async def test_quiet_rejects_bad_minutes(self, tmp_path):
        async with _live_runtime(tmp_path):
            for bad in (-1, 1441, "60", True, None, 1.5):
                resp = await routes._handle_quiet(
                    _json_request("POST", "/api/apps/mochi/quiet", {"minutes": bad})
                )
                assert resp.status == 400, f"minutes={bad!r} should be rejected"

    @pytest.mark.asyncio
    async def test_quiet_holds_then_resume_delivers(self, tmp_path):
        """End to end through the runtime: quiet buffers a gate push, resume
        flushes it into notify_user's sinks (observable via the activity log)."""
        async with _live_runtime(tmp_path) as runtime:
            await routes._handle_quiet(
                _json_request("POST", "/api/apps/mochi/quiet", {"minutes": 60})
            )
            runtime.notify_gate.push(
                {"action": "notify", "summary": "held while quiet"}, hooks._now_ms()
            )
            assert runtime.notify_gate.pending_count == 1
            await routes._handle_quiet(
                _json_request("POST", "/api/apps/mochi/quiet", {"minutes": 0})
            )
            assert runtime.notify_gate.pending_count == 0


class TestPinnedRoutes:
    @pytest.mark.asyncio
    async def test_pinned_lifecycle(self, tmp_path):
        async with _live_runtime(tmp_path) as runtime:
            target = tmp_path / "watched.txt"
            target.write_text("x")
            assert runtime.pinned.add_pin(str(target), now_ms=1_000)

            resp = await routes._handle_pinned_get(
                make_mocked_request("GET", "/api/apps/mochi/pinned")
            )
            pins = json.loads(resp.body)["pins"]
            assert len(pins) == 1

            resp2 = await routes._handle_pinned_mark_seen(
                _json_request("POST", "/api/apps/mochi/pinned/mark-seen", {"path": str(target)})
            )
            assert json.loads(resp2.body) == {"ok": True}

            resp3 = await routes._handle_pinned_unpin(
                _json_request("POST", "/api/apps/mochi/pinned/unpin", {"path": str(target)})
            )
            assert json.loads(resp3.body) == {"ok": True}
            resp4 = await routes._handle_pinned_get(
                make_mocked_request("GET", "/api/apps/mochi/pinned")
            )
            assert json.loads(resp4.body)["pins"] == []

    @pytest.mark.asyncio
    async def test_pinned_label_credential_is_redacted(self, tmp_path):
        """Pin labels are agent-authored (pin_file); a credential in one must be
        scrubbed before the pinned route hands it to the browser."""
        async with _live_runtime(tmp_path) as runtime:
            target = tmp_path / "watched.txt"
            target.write_text("x")
            # Fake AWS key, split to avoid a CodeQL clear-text-storage FP on a
            # redaction-control test (runtime value is a full AKIA key).
            planted = "AKIA" + "IOSFODNN7EXAMPLE"
            assert runtime.pinned.add_pin(str(target), label=f"see {planted}", now_ms=1_000)
            resp = await routes._handle_pinned_get(
                make_mocked_request("GET", "/api/apps/mochi/pinned")
            )
            raw = resp.body.decode()
            assert planted not in raw
            assert "[REDACTED" in json.loads(resp.body)["pins"][0]["label"]

    @pytest.mark.asyncio
    async def test_settings_round_trip(self, tmp_path):
        async with _live_runtime(tmp_path):
            resp = await routes._handle_settings_get(
                make_mocked_request("GET", "/api/apps/mochi/settings")
            )
            assert json.loads(resp.body)["petInstance"] == "self"

            resp = await routes._handle_settings_update(
                _json_request("POST", "/api/apps/mochi/settings", {"petInstance": "inst-3"})
            )
            assert json.loads(resp.body)["petInstance"] == "inst-3"

            resp = await routes._handle_settings_get(
                make_mocked_request("GET", "/api/apps/mochi/settings")
            )
            assert json.loads(resp.body)["petInstance"] == "inst-3"

    @pytest.mark.asyncio
    async def test_settings_rejects_bad_payloads(self, tmp_path):
        async with _live_runtime(tmp_path):
            resp = await routes._handle_settings_update(
                _json_request("POST", "/api/apps/mochi/settings", {"petInstance": 5})
            )
            assert resp.status == 400

            bad = make_mocked_request("POST", "/api/apps/mochi/settings")
            resp = await routes._handle_settings_update(bad)
            assert resp.status == 400


class TestAppearanceBroadcasts:
    """Appearance changes must reach ALREADY-OPEN windows.

    The original emitted ``gallery:packs-changed`` / ``color-map-changed`` on
    every save, delete and recolour, and re-installed the agent prompt with the
    new appearance description. Emitting nothing is what forced the pet to be
    closed and reopened before a new look showed up.
    """

    @staticmethod
    def _capture(runtime, monkeypatch) -> list[tuple[str, tuple]]:
        seen: list[tuple[str, tuple]] = []
        monkeypatch.setattr(
            runtime, "_broadcast", lambda channel, *args: seen.append((channel, args))
        )
        return seen

    @pytest.mark.asyncio
    async def test_appearance_setting_broadcasts_and_reapplies_persona(self, tmp_path, monkeypatch):
        async with _live_runtime(tmp_path) as runtime:
            seen = self._capture(runtime, monkeypatch)
            applied: list[tuple] = []
            monkeypatch.setattr(runtime.soul, "set_appearance", lambda *a: applied.append(a))

            resp = await routes._handle_settings_update(
                _json_request(
                    "POST", "/api/apps/mochi/settings", {"activeAppearance": "kiro-ghost"}
                )
            )
            assert resp.status == 200
            assert [c for c, _ in seen] == ["mochi:color-map-changed"]
            # The persona is otherwise chosen once at runtime construction, so without
            # this the pet would keep describing itself as its previous character. It
            # is re-resolved from the RUNTIME (not from this payload) because a custom
            # pack's persona comes from the pack's own description.
            assert applied == [("kiro-ghost", None)]

    @pytest.mark.asyncio
    async def test_non_appearance_setting_broadcasts_nothing(self, tmp_path, monkeypatch):
        async with _live_runtime(tmp_path) as runtime:
            seen = self._capture(runtime, monkeypatch)
            resp = await routes._handle_settings_update(
                _json_request("POST", "/api/apps/mochi/settings", {"petInstance": "inst-9"})
            )
            assert resp.status == 200
            assert seen == []

    @pytest.mark.asyncio
    async def test_persona_failure_does_not_fail_the_save(self, tmp_path, monkeypatch):
        async with _live_runtime(tmp_path) as runtime:
            seen = self._capture(runtime, monkeypatch)

            def _boom(*_a):
                raise RuntimeError("persona blew up")

            monkeypatch.setattr(runtime.soul, "set_appearance", _boom)
            resp = await routes._handle_settings_update(
                _json_request(
                    "POST", "/api/apps/mochi/settings", {"activeAppearance": "kiro-ghost"}
                )
            )
            # The user's setting IS saved; a persona refresh is best-effort.
            assert resp.status == 200
            assert [c for c, _ in seen] == ["mochi:color-map-changed"]


class TestMovementReports:
    """Upstream these were ipcMain handlers in the pet's own main process.

    They exist for the reasons they did there: pet STATE and the stats file are
    backend-owned, and a walk that never reports completion leaves the state
    machine stuck in "walking" — a failure with no error attached.
    """

    @pytest.mark.asyncio
    async def test_walk_done_restores_after_an_instant_walk(self, tmp_path) -> None:
        async with _live_runtime(tmp_path) as runtime:
            # A walk shorter than INSTANT_WALK_THRESHOLD_MS restores immediately; a
            # longer one DEFERS via a timer so a chained walk does not flash idle.
            # Both paths matter, and only the route delegating at all makes either
            # reachable — without it the pet stays "walking" forever.
            runtime.state_manager.start_walking(routes._now())
            assert runtime.state_manager.current == "walking"
            res = await routes._handle_walk_done(_json_request("POST", "/walk-done", {}))
            assert res.status == 200
            assert runtime.state_manager.current != "walking"

    @pytest.mark.asyncio
    async def test_walk_done_defers_restore_after_a_long_walk(self, tmp_path) -> None:
        async with _live_runtime(tmp_path) as runtime:
            runtime.state_manager.start_walking(routes._now() - 60_000)
            await routes._handle_walk_done(_json_request("POST", "/walk-done", {}))
            # Still walking: the restore is scheduled, not immediate.
            assert runtime.state_manager.current == "walking"

    @pytest.mark.asyncio
    async def test_walk_distance_converts_pixels_to_steps(self, tmp_path) -> None:
        async with _live_runtime(tmp_path):
            res = await routes._handle_walk_distance(
                _json_request("POST", "/walk-distance", {"pixels": 640})
            )
            assert json.loads(res.text)["steps"] == 10  # 64px per step, as upstream

    @pytest.mark.asyncio
    async def test_walk_distance_rejects_non_positive(self, tmp_path) -> None:
        async with _live_runtime(tmp_path):
            for bad in (0, -5, "far", None, float("nan"), float("inf"), float("-inf")):
                res = await routes._handle_walk_distance(
                    _json_request("POST", "/walk-distance", {"pixels": bad})
                )
                assert res.status == 400, bad

    @pytest.mark.asyncio
    async def test_peeking_is_rebroadcast(self, tmp_path, monkeypatch) -> None:
        async with _live_runtime(tmp_path) as runtime:
            published: list[tuple[str, dict]] = []
            monkeypatch.setattr(runtime, "publish", lambda t, d: published.append((t, d)))
            res = await routes._handle_peeking(_json_request("POST", "/peeking", {"peeking": True}))
            assert res.status == 200
            # Every Mochi surface must agree the pet is tucked away, not just the pet.
            assert ("mochi:peeking", {"peeking": True}) in published

    @pytest.mark.asyncio
    async def test_peeking_requires_a_boolean(self, tmp_path) -> None:
        async with _live_runtime(tmp_path):
            res = await routes._handle_peeking(
                _json_request("POST", "/peeking", {"peeking": "yes"})
            )
            assert res.status == 400

    @pytest.mark.asyncio
    async def test_displays_are_cached_for_the_query_action(self, tmp_path) -> None:
        async with _live_runtime(tmp_path):
            res = await routes._handle_displays(
                _json_request(
                    "POST",
                    "/displays",
                    {
                        "displays": [
                            {
                                "id": 7,
                                "index": 2,
                                "primary": False,
                                "x": 1512,
                                "y": 0,
                                "width": 2560,
                                "height": 1440,
                                "workArea": {"x": 1512, "y": 0, "width": 2560, "height": 1400},
                            }
                        ],
                        "activeId": 7,
                    },
                )
            )
            assert res.status == 200
            cached = json.loads((tmp_path / routes.DISPLAYS_FILE).read_text())
            # The MCP `query` action reads exactly this file; only the shell can see
            # the monitors, so without the cache the tool cannot answer at all.
            assert cached["activeId"] == 7
            entry = cached["displays"][0]
            # The GEOMETRY has to survive. The projection used to keep only
            # {id, width, height}, which left the pet with no way to say WHICH screen
            # it was on — so it guessed, and told the user "display 1" while standing
            # on display 2.
            assert entry["index"] == 2
            assert entry["primary"] is False
            assert entry["active"] is True
            assert (entry["x"], entry["y"]) == (1512, 0)
            assert entry["workArea"]["height"] == 1400

    @pytest.mark.asyncio
    async def test_a_non_active_display_is_not_marked_active(self, tmp_path) -> None:
        async with _live_runtime(tmp_path):
            await routes._handle_displays(
                _json_request(
                    "POST",
                    "/displays",
                    {
                        "displays": [{"id": 1, "index": 1}, {"id": 2, "index": 2}],
                        "activeId": 2,
                    },
                )
            )
            cached = json.loads((tmp_path / routes.DISPLAYS_FILE).read_text())
            assert [d["active"] for d in cached["displays"]] == [False, True]

    @pytest.mark.asyncio
    async def test_displays_rejects_a_non_list(self, tmp_path) -> None:
        async with _live_runtime(tmp_path):
            res = await routes._handle_displays(
                _json_request("POST", "/displays", {"displays": {}})
            )
            assert res.status == 400


class TestPackFileServing:
    """A pack is user-imported. An SVG served as image/svg+xml runs any inline
    <script> under the dashboard origin when opened as a document (direct
    navigation / iframe), reaching authenticated APIs — so it is served as
    text/plain with X-Content-Type-Options: nosniff. The renderer fetches the
    bytes and reads res.text(), so text/plain is transparent to legitimate use."""

    @pytest.mark.asyncio
    async def test_svg_pack_file_served_as_text_plain_nosniff(self, tmp_path, monkeypatch):
        # Patch on `routes`, not on appearance_store: routes imports the symbol at
        # module scope, so that is the namespace the lookup actually goes through.
        monkeypatch.setattr(
            routes,
            "read_pack_file",
            lambda data_dir, pack_id, filename: b"<svg><script>steal()</script></svg>",
        )
        async with _live_runtime(tmp_path):
            req = make_mocked_request(
                "GET",
                "/api/apps/mochi/packs/p1/evil.svg",
                match_info={"pack_id": "p1", "filename": "evil.svg"},
            )
            resp = await routes._handle_pack_file(req)
        assert resp.status == 200
        # Not image/svg+xml — a document render must not execute the script.
        assert resp.content_type == "text/plain"
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"

    @pytest.mark.asyncio
    async def test_png_pack_file_keeps_image_type_with_nosniff(self, tmp_path, monkeypatch):
        # Patch on `routes`, not on appearance_store: routes imports the symbol at
        # module scope, so that is the namespace the lookup actually goes through.
        monkeypatch.setattr(
            routes,
            "read_pack_file",
            lambda data_dir, pack_id, filename: b"\x89PNG fake-bytes",
        )
        async with _live_runtime(tmp_path):
            req = make_mocked_request(
                "GET",
                "/api/apps/mochi/packs/p1/idle.png",
                match_info={"pack_id": "p1", "filename": "idle.png"},
            )
            resp = await routes._handle_pack_file(req)
        assert resp.content_type == "image/png"
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"


class TestReset:
    """Reset returns Mochi to a fresh state: no memory, defaults, the cat again."""

    @pytest.mark.asyncio
    async def test_clears_state_files_and_restores_defaults(self, tmp_path) -> None:
        async with _live_runtime(tmp_path):
            from kiro_crew.apps.builtins.mochi.activity_log import LOG_FILE
            from kiro_crew.apps.builtins.mochi.pinned_files_service import DATA_FILE_NAME
            from kiro_crew.apps.builtins.mochi.settings import (
                PACK_GHOST,
                PACK_MOCHI,
                save_settings,
            )
            from kiro_crew.apps.builtins.mochi.stats_service import STATS_FILE_NAME

            # Seed via the OWNING modules' constants. The previous version of this
            # test wrote "mochi-stats.json" — a name no module uses — and then
            # asserted it was gone, so it validated the typo it was meant to catch
            # and the real stats file survived every reset.
            (tmp_path / LOG_FILE).write_text("[]")
            (tmp_path / STATS_FILE_NAME).write_text("{}")
            (tmp_path / DATA_FILE_NAME).write_text("{}")
            save_settings(tmp_path, {"activeAppearance": PACK_GHOST, "petName": "Spooky"})

            res = await routes._handle_reset(_json_request("POST", "/reset", {}))
            assert res.status == 200
            assert not (tmp_path / LOG_FILE).exists()
            assert not (tmp_path / STATS_FILE_NAME).exists()
            assert not (tmp_path / DATA_FILE_NAME).exists()
            body = json.loads(res.text)
            # Back to the orange cat with no custom name — back to the starting state.
            assert body["settings"]["activeAppearance"] == PACK_MOCHI
            assert body["settings"]["petName"] == ""

    @pytest.mark.asyncio
    async def test_reset_stats_serializes_with_tick_and_clears_pending_flush(self, tmp_path) -> None:
        """Stats reset runs off the loop (asyncio.to_thread) under the same lock
        as tick(), and the dirty flag + pending flush deadline are cleared, so a
        due flush in another worker thread cannot rewrite a stale snapshot back
        over the wipe. (A lock-free off-thread unlink used to race a flush and
        'restore' pre-reset counters.)
        """
        async with _live_runtime(tmp_path) as rt:
            from kiro_crew.apps.builtins.mochi.stats_service import STATS_FILE_NAME

            # tick(), reset(), the recorders and get_stats() share ONE reentrant
            # lock so they cannot interleave across threads; RLock (not Lock) so
            # the same-thread tick() -> flush() nesting does not self-deadlock.
            assert isinstance(rt.stats._lock, type(threading.RLock()))

            rt.stats.mark_dirty(1_000)
            rt.stats.flush(1_000)  # persist a snapshot to disk
            assert (tmp_path / STATS_FILE_NAME).exists()
            rt.stats.mark_dirty(2_000)  # dirty again, with a pending flush deadline

            res = await routes._handle_reset(_json_request("POST", "/reset", {}))
            assert res.status == 200
            # The reset removed the file and reported it in `removed`.
            assert STATS_FILE_NAME in json.loads(res.text)["removed"]
            assert not (tmp_path / STATS_FILE_NAME).exists()
            # The anti-resurrection mechanism: no dirty state, no pending flush.
            assert rt.stats._dirty is False
            assert rt.stats._flush_deadline is None

    @pytest.mark.asyncio
    async def test_recorders_and_get_stats_go_through_the_lock(self, tmp_path) -> None:
        """Recorders and get_stats() must hold the shared lock, or a `/stat`
        write can interleave a threaded tick()/flush() (torn json.dumps / lost
        count). Deterministic check: a tracking wrapper counts lock entries."""
        async with _live_runtime(tmp_path) as rt:
            real = rt.stats._lock
            count = {"n": 0}

            class Tracking:
                def __enter__(self):
                    count["n"] += 1
                    return real.__enter__()

                def __exit__(self, *a):
                    return real.__exit__(*a)

            rt.stats._lock = Tracking()
            try:
                rt.stats.record_message_sent(1_000)
                rt.stats.get_stats()
                assert count["n"] >= 2, "recorder and get_stats must both take the lock"
            finally:
                # _runtime is a module global reused across tests — restore the
                # real RLock so a later test's isinstance(_lock, RLock) holds.
                rt.stats._lock = real

    @pytest.mark.asyncio
    async def test_reset_reseeds_pinned_files_not_just_stats(self, tmp_path) -> None:
        """A cached service must not survive the reset that deleted its file.

        Reset reloaded `stats` only, so `pinned` kept its in-memory list: the pins
        stayed visible after a "clean slate", and the next pin/unpin wrote the list
        back out — recreating the file reset had just removed. The reset silently
        un-did itself.
        """
        async with _live_runtime(tmp_path) as rt:
            from kiro_crew.apps.builtins.mochi.pinned_files_service import DATA_FILE_NAME

            target = tmp_path / "watched.txt"
            target.write_text("hello")
            assert rt.pinned.add_pin(str(target), now_ms=1_000)
            assert rt.pinned.get_pins(), "precondition: a pin exists in memory"
            assert (tmp_path / DATA_FILE_NAME).exists()

            await routes._handle_reset(_json_request("POST", "/reset", {}))

            assert rt.pinned.get_pins() == [], "pins survived the reset in memory"
            assert not (tmp_path / DATA_FILE_NAME).exists()

    @pytest.mark.asyncio
    async def test_reset_reloads_pinned_while_holding_its_lock(self, tmp_path) -> None:
        """The pinned wipe+reload must be atomic under pins_mutation, or an
        owner-loop pin write can flush its cached (pre-reset) list back over the
        file the reset just cleared. Assert the service reload runs WHILE the
        lock is held, not after it is released."""
        import contextlib

        depth = {"n": 0}
        real = routes.pins_mutation

        @contextlib.contextmanager
        def _spy_lock(fp):
            depth["n"] += 1
            try:
                with real(fp):
                    yield
            finally:
                depth["n"] -= 1

        # Patched on `routes`: the wipe reaches pins_mutation through routes'
        # module-scope import, so patching pinned_files_service would not be seen.
        import unittest.mock as _mock

        with _mock.patch.object(routes, "pins_mutation", _spy_lock):
            async with _live_runtime(tmp_path) as rt:
                held = {"v": False}
                orig = rt.pinned.load

                def _load_spy(now_ms: int) -> None:
                    held["v"] = depth["n"] > 0
                    orig(now_ms)

                rt.pinned.load = _load_spy  # type: ignore[method-assign]
                await routes._handle_reset(_json_request("POST", "/reset", {}))
                assert held["v"], "pinned reload must run while pins_mutation is held"

    @pytest.mark.asyncio
    async def test_every_service_with_a_load_is_reseeded_by_reset(self, tmp_path) -> None:
        """Drift guard: adding a caching service must not silently skip the reset.

        The forward check above only covers the two services that exist today. This
        walks the runtime for anything exposing `load(now)` and asserts the reset
        actually calls it, so a third cached service fails here rather than in a
        user's "why are my pins back" report.
        """
        async with _live_runtime(tmp_path) as rt:
            cached = {
                name: obj
                for name in dir(rt)
                if not name.startswith("_")
                for obj in [getattr(rt, name, None)]
                if callable(getattr(obj, "load", None))
            }
            assert cached, "no services with load() found — the walk is broken"

            called: set[str] = set()
            for name, obj in cached.items():
                original = obj.load

                def _spy(now_ms: int, _n: str = name, _o=original) -> None:
                    called.add(_n)
                    _o(now_ms)

                obj.load = _spy  # type: ignore[method-assign]

            await routes._handle_reset(_json_request("POST", "/reset", {}))
            assert called == set(cached), (
                "reset did not re-seed every cached service — missing: "
                f"{sorted(set(cached) - called)}"
            )

    def test_every_reset_target_is_a_real_filename(self) -> None:
        """No hand-spelled names: each entry must be some module's own constant.

        A duplicated literal list is how three of these drifted out of step with
        the files they were supposed to delete, and the unlink loop reads a
        missing file as "already gone" — so reset reported success while keeping
        the state it promised to clear.
        """
        from kiro_crew.apps.builtins.mochi import (
            activity_log,
            mcp_server,
            pinned_files_service,
            stats_service,
            watchlist_service,
        )

        owned = {
            activity_log.LOG_FILE,
            activity_log.YESTERDAY_LOG_FILE,
            stats_service.STATS_FILE_NAME,
            pinned_files_service.DATA_FILE_NAME,
            mcp_server._QUEUE_FILE,
            watchlist_service._WATCHLIST_FILE,
            watchlist_service._ARCHIVE_FILE,
            routes.DISPLAYS_FILE,
        }
        assert set(routes._reset_files()) <= owned
        # And the reverse: a file the app owns and clearly counts as memory must
        # not be quietly dropped from the reset.
        assert stats_service.STATS_FILE_NAME in routes._reset_files()
        assert pinned_files_service.DATA_FILE_NAME in routes._reset_files()

    @pytest.mark.asyncio
    async def test_keeps_user_imported_appearance_packs(self, tmp_path) -> None:
        async with _live_runtime(tmp_path):
            from kiro_crew.apps.builtins.mochi.appearance_store import save_pack

            save_pack(tmp_path, {"id": "mine"}, {"idle": "<svg/>"})
            await routes._handle_reset(_json_request("POST", "/reset", {}))
            # A pack is the user's OWN ART. Upstream's dialog promised history,
            # activity logs, screenshots and preferences — never the artwork.
            assert (tmp_path / "appearances" / "mine" / "manifest.json").is_file()

    @pytest.mark.asyncio
    async def test_is_idempotent_on_a_fresh_install(self, tmp_path) -> None:
        async with _live_runtime(tmp_path):
            # Nothing to delete must not be an error, or a second reset would fail.
            first = await routes._handle_reset(_json_request("POST", "/reset", {}))
            second = await routes._handle_reset(_json_request("POST", "/reset", {}))
            assert first.status == 200 and second.status == 200


class TestNoLockingCallRunsOnTheEventLoop:
    """A cross-process lock must never be awaited on the loop.

    Several of Mochi's services take a file lock so the gateway and the MCP
    server (a separate process) cannot interleave a read-modify-write. That makes
    the call a BLOCKING wait whenever the other side holds it — and on the event
    loop a blocking wait stalls chat streaming, the heartbeat, and every other
    request until it clears. Each instance looked local and correct; only the
    pairing with the lock makes it a stall, which is why this is a sweep rather
    than a per-call review.
    """

    def test_every_locking_call_in_async_code_is_offloaded(self) -> None:
        import ast

        locking = {
            "add_pin",
            "remove_pin",
            "mark_seen",
            "save_pack",
            "delete_pack",
            "import_bundle",
            "save_sprite_pack",
            "get_due",
            "get_missed",
            "add_watch_item",
            "update_watch_item",
            "cancel_watch_item",
            "remove_watch_item",
        }
        root = _REPO_ROOT / "src/kiro_crew/apps/builtins/mochi"
        offenders: list[str] = []
        scanned = 0
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]:
                scanned += 1
                wrapped = {
                    id(arg)
                    for n in ast.walk(fn)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "to_thread"
                    for arg in n.args
                }
                # Free functions (bare-name calls) that re-render the prompt or
                # re-materialize the agent config — disk scans + atomic writes,
                # blocking on the loop just like the locked service methods.
                disk_free_funcs = {"apply_policy", "_appearance_changed"}
                for n in ast.walk(fn):
                    if not isinstance(n, ast.Call) or id(n.func) in wrapped:
                        continue
                    if isinstance(n.func, ast.Attribute) and n.func.attr in locking:
                        offenders.append(f"{path.name}:{n.lineno} in {fn.name}() -> {n.func.attr}")
                    elif isinstance(n.func, ast.Name) and n.func.id in disk_free_funcs:
                        offenders.append(f"{path.name}:{n.lineno} in {fn.name}() -> {n.func.id}")
        assert scanned > 0, "no async functions scanned — did the layout move?"
        assert offenders == [], "wrap these in asyncio.to_thread: " + "; ".join(offenders)


class TestTheOwnerLoopNeverBlocksOnDisk:
    """The 1s owner-loop fan-out must offload every disk-bound service tick.

    The routes guard above scans by METHOD NAME, so it missed the owner loop's
    `self.pinned.poll_file_changes(now)` / `self.pinned.tick(now)` /
    `self.stats.tick(now)` — `tick` is a name a dozen pure in-memory services also
    use, so it can't go in a name-only denylist. This guard instead matches by the
    (receiver, method) PAIR, which is exact: only the pinned/stats/watchlist ticks
    do disk work, and every one of them must be wrapped in `asyncio.to_thread`.
    """

    # (receiver-attribute, method) pairs whose implementation reaches the
    # filesystem (os.stat per path, or an atomic_write when a debounce fires).
    DISK_BOUND = {
        ("pinned", "poll_file_changes"),
        ("pinned", "tick"),
        ("stats", "tick"),
    }
    # self-methods on the owner that stat/read a file each cycle.
    DISK_BOUND_SELF = {"_poll_watchlist_file"}

    def test_every_disk_bound_tick_is_offloaded(self) -> None:
        import ast

        tree = ast.parse(
            (_REPO_ROOT / "src/kiro_crew/apps/builtins/mochi/hooks.py").read_text(encoding="utf-8")
        )

        def receiver(call: ast.Call) -> tuple[str, str] | None:
            fn = call.func
            if not isinstance(fn, ast.Attribute):
                return None
            base = fn.value
            if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
                if base.value.id == "self":
                    return (base.attr, fn.attr)  # self.<recv>.<method>
            if isinstance(base, ast.Name) and base.id == "self":
                return ("self", fn.attr)  # self.<method>
            return None

        offenders: list[str] = []
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]:
            wrapped = {
                id(arg)
                for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "to_thread"
                for arg in n.args
            }
            for n in ast.walk(fn):
                if not isinstance(n, ast.Call):
                    continue
                recv = receiver(n)
                if recv is None:
                    continue
                is_disk = recv in self.DISK_BOUND or (
                    recv[0] == "self" and recv[1] in self.DISK_BOUND_SELF
                )
                # a to_thread wrapper passes the callable, not the call, so an
                # offloaded site appears as `func` referenced (id in wrapped) OR
                # the method is simply named as a bare attribute arg.
                if is_disk and id(n.func) not in wrapped:
                    offenders.append(f"{fn.name}:{n.lineno} -> {recv[0]}.{recv[1]}")
        assert offenders == [], "wrap in asyncio.to_thread: " + "; ".join(offenders)


class TestSettingsAndDisplayWritesAreOffloaded:
    """save_settings and the displays-cache write are blocking atomic writes; on
    an async route they must be offloaded or the gateway heartbeat/chat stall."""

    def test_no_bare_blocking_write_on_an_async_route(self) -> None:
        import re

        lines = (
            (_REPO_ROOT / "src/kiro_crew/apps/builtins/mochi/backend/routes.py")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        offenders = []
        for i, ln in enumerate(lines):
            if re.search(r"^\s+(updated = )?save_settings\(|^\s+_write_displays_cache\(", ln):
                offenders.append(f"line {i + 1}: {ln.strip()[:60]}")
        assert offenders == [], "these blocking writes must be awaited via to_thread: " + "; ".join(
            offenders
        )


class TestResetSerializesUnlinks:
    """The reset must unlink each lock-guarded store file UNDER that store's
    mutation lock, so a concurrent writer cannot recreate the file from
    pre-reset state right after the reset succeeds.
    """

    @pytest.mark.asyncio
    async def test_reset_enters_each_store_lock(self, tmp_path, monkeypatch):
        entered: list[str] = []

        def _spy(mod, name: str) -> None:
            real = getattr(mod, name)

            @contextlib.contextmanager
            def spy(*args, **kwargs):
                entered.append(name)
                with real(*args, **kwargs):
                    yield

            monkeypatch.setattr(mod, name, spy)

        async with _live_runtime(tmp_path):
            # Wrap AFTER startup so only the reset's own acquisitions are counted.
            # `routes` is the namespace, not each source module: routes imports
            # these at module scope, so patching the source no longer intercepts.
            _spy(routes, "queue_mutation")
            _spy(routes, "watchlist_mutation")
            _spy(routes, "pins_mutation")
            _spy(routes, "activity_mutation")

            resp = await routes._handle_reset(make_mocked_request("POST", "/api/apps/mochi/reset"))
            assert json.loads(resp.body)["ok"] is True

        # Every lock-guarded store must have been entered during the wipe; a
        # bare unlink outside these locks (the pre-fix behaviour) would leave
        # one or more of these absent.
        assert set(entered) >= {
            "queue_mutation",
            "watchlist_mutation",
            "pins_mutation",
            "activity_mutation",
        }


class TestNonObjectBodyRejected:
    """A JSON array/scalar body must be rejected with 400, not crash the
    handler with an AttributeError (HTTP 500) when it calls ``body.get()``.
    """

    @pytest.mark.asyncio
    async def test_walk_stat_peeking_displays_reject_array_body(self, tmp_path):
        async with _live_runtime(tmp_path):
            cases = [
                (routes._handle_walk_distance, "/api/apps/mochi/walk-distance"),
                (routes._handle_stat, "/api/apps/mochi/stat"),
                (routes._handle_peeking, "/api/apps/mochi/peeking"),
                (routes._handle_displays, "/api/apps/mochi/displays"),
            ]
            for handler, path in cases:
                resp = await handler(_json_request("POST", path, [1, 2, 3]))
                assert resp.status == 400, f"{path} must 400 on a non-object body"


class TestMalformedWatchlistOpsRejected:
    """A watchlist operation with the right KEY but the wrong VALUE type must be
    a 400, not a 500 — and must never mutate the list.

    The route only asserted that one of ``add``/``cancel``/``update``/``remove``
    was present, so a wrong type reached the per-item code: ``{"add": "x"}``
    sliced the string and called ``create_watch_item`` on each character
    (``AttributeError`` -> 500), and ``{"remove": "abc"}`` built a set of
    CHARACTERS and deleted every item whose id was one of them — silent data
    loss that returned 200.
    """

    MALFORMED = [
        {"add": "x"},  # string, not a list
        {"add": ["x"]},  # list of strings, not objects
        {"add": 5},  # not even a sequence
        {"update": "x"},
        {"update": ["x"]},
        {"cancel": "abc"},  # would delete ids 'a', 'b', 'c'
        {"remove": "abc"},
        {"add": [{"checkIntervalMins": "abc"}]},  # arithmetic on a string
        {"add": [{"checkIntervalMins": True}]},  # bool passes isinstance(_, int)
    ]

    @pytest.mark.asyncio
    async def test_every_malformed_op_is_a_client_error(self, tmp_path):
        async with _live_runtime(tmp_path):
            for body in self.MALFORMED:
                resp = await routes._handle_watchlist_update(
                    _json_request("POST", "/api/apps/mochi/watchlist/update", body)
                )
                assert resp.status == 400, f"{body} must 400, got {resp.status}"

    @pytest.mark.asyncio
    async def test_malformed_op_leaves_existing_items_untouched(self, tmp_path):
        async with _live_runtime(tmp_path):
            added = await routes._handle_watchlist_update(
                _json_request(
                    "POST",
                    "/api/apps/mochi/watchlist/update",
                    {"add": [{"label": "Keep me", "kind": "url", "target": "https://e.com"}]},
                )
            )
            assert added.status == 200
            item_id = json.loads(added.body)["items"][0]["id"]

            # A single-character id would be caught by set("abc"); a real id is
            # long, so assert on the count instead of relying on that.
            for body in ({"remove": "abc"}, {"cancel": "abc"}, {"add": "x"}):
                resp = await routes._handle_watchlist_update(
                    _json_request("POST", "/api/apps/mochi/watchlist/update", body)
                )
                assert resp.status == 400

            got = await routes._handle_watchlist_get(
                make_mocked_request("GET", "/api/apps/mochi/watchlist")
            )
            items = json.loads(got.body)["items"]
            assert [i["id"] for i in items] == [item_id]

    @pytest.mark.asyncio
    async def test_well_formed_ops_still_apply(self, tmp_path):
        """The guard must not narrow the accepted shapes: empty lists and
        explicit nulls were valid no-ops before and stay valid."""
        async with _live_runtime(tmp_path):
            for body in (
                {"add": [{"kind": "url", "target": "https://e.com"}]},
                {"add": [{"kind": "url", "target": "https://e.com", "checkIntervalMins": 30}]},
                {"add": [{"kind": "url", "target": "https://e.com", "checkIntervalMins": 7.5}]},
                {"add": [], "cancel": []},
                {"add": None, "cancel": []},
            ):
                resp = await routes._handle_watchlist_update(
                    _json_request("POST", "/api/apps/mochi/watchlist/update", body)
                )
                assert resp.status == 200, f"{body} must be accepted, got {resp.status}"


class TestQueueFilenameHasOneDefinition:
    """The queue filename is defined ONCE, in the module that owns queue files.

    `hooks` and `mcp_server` each used to define their own copy of the literal,
    and `routes` imported it from whichever was convenient per call site.
    Renaming the file in one module would have left the others reading a path
    nothing writes — a reset that clears nothing, with no error anywhere.
    """

    def test_every_reader_resolves_to_the_owner(self):
        from kiro_crew.apps.builtins.mochi import hooks, mcp_server, queue_file
        from kiro_crew.apps.builtins.mochi.backend import routes

        assert hooks._QUEUE_FILE is queue_file.QUEUE_FILE
        assert mcp_server._QUEUE_FILE is queue_file.QUEUE_FILE
        assert routes._QUEUE_FILE is queue_file.QUEUE_FILE

    def test_no_module_redefines_the_literal(self):
        """A future copy-paste must fail here rather than drift silently."""

        from kiro_crew.apps.builtins.mochi import queue_file

        pkg = Path(queue_file.__file__).parent
        owner = Path(queue_file.__file__).name
        literal = f'= "{queue_file.QUEUE_FILE}"'
        offenders = [
            py.name
            for py in sorted(pkg.rglob("*.py"))
            if py.name != owner and literal in py.read_text(encoding="utf-8")
        ]
        assert offenders == [], f"{offenders} redefine the queue filename instead of importing it"


class TestMcpToolsRoute:
    """GET /api/apps/mochi/mcp-tools/{name} — the settings panel's discover action.

    The panel used to call core's ``/api/mcp/servers/{name}``, which only has
    PUT/DELETE registered, so every discover took a 405 and both the api helper
    and the click handler swallowed it.
    """

    @staticmethod
    def _server(name="srv", disabled=False, tools=()):
        from kiro_crew.mcp_discovery import McpServerInfo

        s = McpServerInfo(name=name, command="node")
        s.tools = list(tools)
        s.disabled = disabled
        return s

    def _patch(self, monkeypatch, servers, probe=None, scopes=None):
        # Patch the names bound INTO routes, not mcp_discovery's own attributes:
        # routes imports them at module scope, so rebinding the source module
        # would leave these handlers holding the real functions and the test
        # would silently exercise a live probe.
        from kiro_crew.apps.builtins.mochi.backend import routes as r

        monkeypatch.setattr(r, "list_servers", lambda: list(servers))
        if probe is not None:
            monkeypatch.setattr(r, "probe_server", probe)
        # The effective-disabled check scans every MCP scope on disk. Stub it so
        # the suite never depends on the developer's own mcp.json (and never
        # reads it): ``scopes`` is {scope: {name: spec}}, same shape as
        # mcp_discovery._load_mcp_json_by_source.
        monkeypatch.setattr(
            r, "_mcp_scope_specs_strict", lambda: [dict(m) for m in (scopes or {}).values()]
        )

    @pytest.mark.asyncio
    async def test_route_is_registered_for_get(self):
        """Guards the actual defect: the path existed but not for this method."""
        from aiohttp import web

        from kiro_crew.apps.builtins.mochi.backend import routes

        app = web.Application()
        routes.register_routes(app)
        registered = {
            (res.method, str(res.resource.canonical))
            for res in app.router.routes()
            if res.resource is not None
        }
        assert ("POST", "/api/apps/mochi/mcp-tools/{name}") in registered

    @pytest.mark.asyncio
    async def test_returns_tools_as_objects(self, monkeypatch):
        from kiro_crew.apps.builtins.mochi.backend import routes

        srv = self._server(tools=["alpha", "beta"])

        async def _probe(server):
            server.status = "ok"
            return server

        self._patch(monkeypatch, [srv], _probe)
        req = make_mocked_request("POST", "/api/apps/mochi/mcp-tools/srv", match_info={"name": "srv"})
        resp = await routes._handle_mcp_tools_probe(req)
        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["tools"] == [{"name": "alpha"}, {"name": "beta"}]
        assert body["cached"] is False

    @pytest.mark.asyncio
    async def test_non_string_tool_names_are_dropped(self, monkeypatch):
        """A server answering a non-string ``name`` must not reach the panel.

        Both extraction paths in ``mcp_discovery`` bind the name on truthiness
        alone (``isinstance(t, dict) and (name := t.get("name", ""))``), so a
        server returning ``{"name": {"x": 1}}`` puts a dict in ``server.tools``.
        The panel renders each name as a React child and a non-primitive child
        throws, blanking the settings tree — so this boundary narrows the shape
        instead of trusting upstream.
        """
        from kiro_crew.apps.builtins.mochi.backend import routes

        srv = self._server(tools=[{"x": 1}, "alpha", ["a"], "", 7, None, "beta"])

        async def _probe(server):
            server.status = "ok"
            return server

        self._patch(monkeypatch, [srv], _probe)
        req = make_mocked_request("POST", "/api/apps/mochi/mcp-tools/srv", match_info={"name": "srv"})
        resp = await routes._handle_mcp_tools_probe(req)
        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["tools"] == [{"name": "alpha"}, {"name": "beta"}]
        for entry in body["tools"]:
            assert isinstance(entry["name"], str) and entry["name"]

    @pytest.mark.asyncio
    async def test_probe_error_prose_is_not_returned(self, monkeypatch):
        """A server's own error text can carry a credential and this response
        reaches the dashboard, so the prose must not be on the wire at all."""
        from kiro_crew.apps.builtins.mochi.backend import routes

        srv = self._server()

        async def _probe(server):
            server.status = "error"
            server.error = "connect failed: https://example.test?token=SECRETVALUE"
            return server

        self._patch(monkeypatch, [srv], _probe)
        req = make_mocked_request("POST", "/api/apps/mochi/mcp-tools/srv", match_info={"name": "srv"})
        resp = await routes._handle_mcp_tools_probe(req)
        assert resp.status == 200
        assert "SECRETVALUE" not in resp.text
        body = json.loads(resp.text)
        assert "error" not in body, "error prose must not be returned"
        assert body["status"] == "error", "status is how the panel learns the probe failed"

    @pytest.mark.asyncio
    async def test_unknown_server_is_404(self, monkeypatch):
        from kiro_crew.apps.builtins.mochi.backend import routes

        self._patch(monkeypatch, [self._server(name="other")])
        req = make_mocked_request("POST", "/api/apps/mochi/mcp-tools/srv", match_info={"name": "srv"})
        resp = await routes._handle_mcp_tools_probe(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_disabled_server_is_never_probed(self, monkeypatch):
        """Probing SPAWNS the server. ``probe_all`` filters consent-disabled rows
        before calling ``probe_server``; ``probe_server`` does not enforce it, so
        this per-server entry point must, or it bypasses the consent gate."""
        from kiro_crew.apps.builtins.mochi.backend import routes

        called = []

        async def _probe(server):
            called.append(server.name)
            return server

        self._patch(monkeypatch, [self._server(disabled=True)], _probe)
        req = make_mocked_request("POST", "/api/apps/mochi/mcp-tools/srv", match_info={"name": "srv"})
        resp = await routes._handle_mcp_tools_probe(req)
        assert resp.status == 409
        assert json.loads(resp.text)["code"] == "server_disabled"
        assert called == [], "a disabled server must not be spawned"

    @pytest.mark.asyncio
    async def test_toggle_disabled_in_kiro_global_scope_is_never_probed(self, monkeypatch):
        """The bypass GPT caught: /api/mcp/toggle writes ``disabled: true`` into
        the KIRO-GLOBAL mcp.json, but ``list_servers`` only sets
        ``McpServerInfo.disabled`` from the Kiro Crew scope. A row introduced by a
        retained agent entry therefore arrives with ``disabled = False`` even
        though the user switched the server off in the dashboard, so a check that
        reads only the row would spawn it. Row says enabled, scope says disabled
        -> must refuse.
        """
        from kiro_crew.apps.builtins.mochi.backend import routes

        called = []

        async def _probe(server):
            called.append(server.name)
            return server

        self._patch(
            monkeypatch,
            [self._server(disabled=False)],
            _probe,
            scopes={"kiroGlobal": {"srv": {"command": "node", "disabled": True}}},
        )
        req = make_mocked_request(
            "POST", "/api/apps/mochi/mcp-tools/srv", match_info={"name": "srv"}
        )
        resp = await routes._handle_mcp_tools_probe(req)
        assert resp.status == 409
        assert json.loads(resp.text)["code"] == "server_disabled"
        assert called == [], "a server disabled in any scope must not be spawned"

    @pytest.mark.asyncio
    async def test_scope_scan_failure_fails_closed(self, monkeypatch):
        """A scan that raises must refuse the probe, not fall through to it."""
        from kiro_crew.apps.builtins.mochi.backend import routes

        called = []

        async def _probe(server):
            called.append(server.name)
            return server

        self._patch(monkeypatch, [self._server(disabled=False)], _probe)

        def _boom():
            raise OSError("unreadable")

        monkeypatch.setattr(routes, "_mcp_scope_specs_strict", _boom)
        req = make_mocked_request(
            "POST", "/api/apps/mochi/mcp-tools/srv", match_info={"name": "srv"}
        )
        resp = await routes._handle_mcp_tools_probe(req)
        assert resp.status == 409
        assert called == [], "a failed consent scan must not spawn the server"

    @pytest.mark.asyncio
    async def test_raw_scoped_key_disable_blocks_the_canonical_row(self, monkeypatch):
        """Second bypass GPT caught: ``list_servers`` CANONICALIZES row names, so
        a server configured as ``npm:@playwright/mcp`` is reported as
        ``playwright-mcp``. The scope dict is still keyed by the RAW name, so an
        exact lookup finds no ``disabled: true`` — and the canonical row can be
        retained from the agent config, which is what makes it probeable.
        """
        from kiro_crew.apps.builtins.mochi.backend import routes

        called = []

        async def _probe(server):
            called.append(server.name)
            return server

        self._patch(
            monkeypatch,
            [self._server(name="playwright-mcp", disabled=False)],
            _probe,
            scopes={
                "kiroGlobal": {
                    "npm:@playwright/mcp": {"command": "npx", "disabled": True}
                }
            },
        )
        req = make_mocked_request(
            "POST",
            "/api/apps/mochi/mcp-tools/playwright-mcp",
            match_info={"name": "playwright-mcp"},
        )
        resp = await routes._handle_mcp_tools_probe(req)
        assert resp.status == 409
        assert json.loads(resp.text)["code"] == "server_disabled"
        assert called == [], "a raw-keyed disable must block the canonical row"

    @pytest.mark.asyncio
    async def test_unreadable_scope_refuses_instead_of_probing(self, monkeypatch, tmp_path):
        """An unusable scope means the consent state is UNKNOWN, not absent.

        ``mcp_discovery._load_mcp_json_by_source`` logs and skips a file it
        cannot parse, so a scope holding the ``disabled: true`` simply vanishes
        and the row looks enabled — the fail-OPEN GPT caught. This drives the
        real reader against a malformed file on disk (no stub) to prove the
        propagate-and-refuse path, rather than asserting on a mocked raise.
        """
        from kiro_crew.apps.builtins.mochi.backend import routes

        called = []

        async def _probe(server):
            called.append(server.name)
            return server

        bad = tmp_path / "broken-mcp.json"
        bad.write_text("{ this is not json", encoding="utf-8")
        monkeypatch.setattr(
            routes.mcp_discovery, "_mcp_json_paths", lambda: (bad,)
        )
        monkeypatch.setattr(
            routes.mcp_discovery, "_extra_scope_sources", lambda: []
        )
        from kiro_crew.apps.builtins.mochi.backend import routes as r

        monkeypatch.setattr(r, "list_servers", lambda: [self._server(disabled=False)])
        monkeypatch.setattr(r, "probe_server", _probe)

        req = make_mocked_request(
            "POST", "/api/apps/mochi/mcp-tools/srv", match_info={"name": "srv"}
        )
        resp = await routes._handle_mcp_tools_probe(req)
        assert resp.status == 409
        assert called == [], "an unreadable scope must not fall through to a probe"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "doc",
        ['{"mcpServers": []}', '{"mcpServers": "nope"}', "[]", '"a string"'],
        ids=["servers-list", "servers-string", "doc-list", "doc-string"],
    )
    async def test_malformed_scope_shape_refuses_instead_of_probing(
        self, monkeypatch, tmp_path, doc
    ):
        """Parseable-but-wrong SHAPE is unreadable too.

        The earlier fix propagated read/parse errors but still SKIPPED a scope
        whose shape was wrong, so ``{"mcpServers": []}`` dropped a scope that may
        hold the ``disabled: true`` and the consent check went back to
        fail-OPEN — the same defect one layer down.
        """
        from kiro_crew.apps.builtins.mochi.backend import routes as r

        called = []

        async def _probe(server):
            called.append(server.name)
            return server

        bad = tmp_path / "shape-mcp.json"
        bad.write_text(doc, encoding="utf-8")
        monkeypatch.setattr(r.mcp_discovery, "_mcp_json_paths", lambda: (bad,))
        monkeypatch.setattr(r.mcp_discovery, "_extra_scope_sources", lambda: [])
        monkeypatch.setattr(r, "list_servers", lambda: [self._server(disabled=False)])
        monkeypatch.setattr(r, "probe_server", _probe)

        req = make_mocked_request(
            "POST", "/api/apps/mochi/mcp-tools/srv", match_info={"name": "srv"}
        )
        resp = await r._handle_mcp_tools_probe(req)
        assert resp.status == 409
        assert called == [], f"malformed scope {doc!r} must not fall through to a probe"

    def test_absent_mcp_servers_key_is_legitimately_empty(self, monkeypatch, tmp_path):
        """Guard the fix against over-reaching: a config with NO ``mcpServers``
        key is a valid empty scope, not a malformed one, and must not raise."""
        from kiro_crew.apps.builtins.mochi.backend import routes as r

        ok = tmp_path / "empty-mcp.json"
        ok.write_text('{"someOtherKey": 1}', encoding="utf-8")
        monkeypatch.setattr(r.mcp_discovery, "_mcp_json_paths", lambda: (ok,))
        monkeypatch.setattr(r.mcp_discovery, "_extra_scope_sources", lambda: [])
        assert r._mcp_scope_specs_strict() == []

    @pytest.mark.asyncio
    async def test_blank_name_is_400(self, monkeypatch):
        from kiro_crew.apps.builtins.mochi.backend import routes

        self._patch(monkeypatch, [])
        req = make_mocked_request("POST", "/api/apps/mochi/mcp-tools/ ", match_info={"name": "  "})
        resp = await routes._handle_mcp_tools_probe(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_concurrent_probe_is_rejected(self, monkeypatch):
        """Click-spam must not spawn one process per click."""
        from kiro_crew.apps.builtins.mochi.backend import routes

        async def _probe(server):
            return server

        self._patch(monkeypatch, [self._server()], _probe)
        routes._mcp_probe_inflight.add("srv")
        try:
            req = make_mocked_request(
                "POST", "/api/apps/mochi/mcp-tools/srv", match_info={"name": "srv"}
            )
            resp = await routes._handle_mcp_tools_probe(req)
        finally:
            routes._mcp_probe_inflight.discard("srv")
        assert resp.status == 409
        assert json.loads(resp.text)["code"] == "probe_in_progress"

    @pytest.mark.asyncio
    async def test_inflight_cleared_when_probe_raises(self, monkeypatch):
        from kiro_crew.apps.builtins.mochi.backend import routes

        async def _probe(server):
            raise RuntimeError("boom")

        self._patch(monkeypatch, [self._server()], _probe)
        req = make_mocked_request("POST", "/api/apps/mochi/mcp-tools/srv", match_info={"name": "srv"})
        with pytest.raises(RuntimeError):
            await routes._handle_mcp_tools_probe(req)
        assert "srv" not in routes._mcp_probe_inflight
