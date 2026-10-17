"""Unit tests for kiro_crew.tips: the curated pool and the doc / doc_link split.

Curated, action-first tips are the primary user-facing pool. These pin what ships in
it, how its actions and highlight anchors are sanitised, that ``doc`` (a tip's
dismissal identity) and ``doc_link`` (a rendering-only hint) never share one field, and
the one-shot ``relink_migrated`` repair of state written before that split.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

from kiro_crew.tips import (
    TipsState,
    _is_eligible,
    _load_state,
    _parse_tips,
    _save_state,
    _validate_tip_fields,
)


class TestCuratedTips:
    """Curated, action-first tips are the primary user-facing pool."""

    def test_curated_tips_ship_and_validate(self) -> None:
        from kiro_crew.tips import _TIP_ALLOWED_FIELDS, _load_curated_tips

        tips = _load_curated_tips()
        assert len(tips) >= 8
        ids = [t["id"] for t in tips]
        assert len(ids) == len(set(ids)), "curated tip ids must be unique"
        # A few known Kiro Crew-native features must be present.
        for expected in ("split-view", "command-palette", "warm-pool", "secrets-vault"):
            assert expected in ids
        # The secrets-vault tip routes to the Secrets settings tab.
        by_id = {t["id"]: t for t in tips}
        assert by_id["secrets-vault"]["action"] == {
            "kind": "route",
            "label": "Open Secrets settings",
            "route": "/settings/secrets",
        }
        for t in tips:
            for k in _TIP_ALLOWED_FIELDS:
                assert isinstance(t.get(k), str), f"{t.get('id')}.{k} must be a string"
            for k in ("id", "title", "body"):
                assert t[k].strip(), f"{t.get('id')}.{k} must be non-empty"

    def test_curated_tips_missing_file_returns_empty(self, tmp_path: Path) -> None:
        from unittest.mock import patch as mpatch

        from kiro_crew import tips as tips_mod

        missing = tmp_path / "nope.json"
        with mpatch.object(tips_mod, "_CURATED_FILE", missing):
            assert tips_mod._load_curated_tips() == []

    def test_curated_tips_malformed_returns_empty(self, tmp_path: Path) -> None:
        from unittest.mock import patch as mpatch

        from kiro_crew import tips as tips_mod

        bad = tmp_path / "bad.json"
        bad.write_text("{ not json", encoding="utf-8")
        with mpatch.object(tips_mod, "_CURATED_FILE", bad):
            assert tips_mod._load_curated_tips() == []
        # A JSON list root (not a dict) also degrades to empty.
        bad.write_text("[]", encoding="utf-8")
        with mpatch.object(tips_mod, "_CURATED_FILE", bad):
            assert tips_mod._load_curated_tips() == []

    def test_curated_tips_skip_invalid_entries(self, tmp_path: Path) -> None:
        from unittest.mock import patch as mpatch

        from kiro_crew import tips as tips_mod

        f = tmp_path / "c.json"
        good = {
            "id": "ok",
            "feature": "F",
            "title": "T",
            "body": "Do the thing.",
            "why": "",
            "doc": "",
            "cta_prompt": "",
        }
        f.write_text(json.dumps({"tips": [good, {"id": "bad"}, "nope", 3]}), encoding="utf-8")
        with mpatch.object(tips_mod, "_CURATED_FILE", f):
            out = tips_mod._load_curated_tips()
        assert [t["id"] for t in out] == ["ok"]

    @pytest.mark.asyncio
    async def test_curated_tip_served_when_generated_empty(self, tmp_path: Path) -> None:
        import types
        from unittest.mock import MagicMock
        from unittest.mock import patch as mpatch

        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.tips import TipsCache, api_tips_next

        curated = {
            "id": "split-view",
            "feature": "Split View",
            "title": "Work two sessions side by side",
            "body": "Turn on Settings > Chat > Split View, then press Cmd+D.",
            "why": "",
            "doc": "",
            "cta_prompt": "",
        }
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cache = TipsCache()
            cache.curated = [curated]
            cache.state = TipsState(tips=[])
            state = types.SimpleNamespace(_tips_cache=cache)
            cfg = MagicMock()
            cfg.dashboard.tips_enabled = True
            cfg.dashboard.tips_cadence_hours = 0.0
            cfg.dashboard.tips_snooze_hours = 48.0
            cfg.dashboard.tips_recency_decay = 0.6
            cfg.dashboard.tips_explore_ratio = 0.0

            async def noop_refresh(*a: object, **k: object) -> None:
                return None

            with (
                mpatch("kiro_crew.tips.KiroCrewConfig") as mock_cfg_cls,
                mpatch("kiro_crew.tips.maybe_refresh", noop_refresh),
            ):
                mock_cfg_cls.load.return_value = cfg
                req = make_mocked_request("GET", "/api/tips/next")
                req.app["state"] = state
                resp = await api_tips_next(req)
                assert resp.status == 200
                body = json.loads(resp.body)
                assert body["tip"] is not None
                assert body["tip"]["id"] == "split-view"

    @pytest.mark.asyncio
    async def test_empty_curated_preserves_generated(self, tmp_path: Path) -> None:
        import types
        from unittest.mock import MagicMock
        from unittest.mock import patch as mpatch

        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.tips import TipsCache, api_tips_next

        gen = {
            "id": "gen-1",
            "feature": "F",
            "title": "T",
            "body": "B",
            "why": "W",
            "doc": "",
            "cta_prompt": "",
        }
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cache = TipsCache()  # curated defaults to []
            cache.state = TipsState(tips=[gen])
            state = types.SimpleNamespace(_tips_cache=cache)
            cfg = MagicMock()
            cfg.dashboard.tips_enabled = True
            cfg.dashboard.tips_cadence_hours = 0.0
            cfg.dashboard.tips_snooze_hours = 48.0
            cfg.dashboard.tips_recency_decay = 0.6
            cfg.dashboard.tips_explore_ratio = 0.0

            async def noop_refresh(*a: object, **k: object) -> None:
                return None

            with (
                mpatch("kiro_crew.tips.KiroCrewConfig") as mock_cfg_cls,
                mpatch("kiro_crew.tips.maybe_refresh", noop_refresh),
            ):
                mock_cfg_cls.load.return_value = cfg
                req = make_mocked_request("GET", "/api/tips/next")
                req.app["state"] = state
                resp = await api_tips_next(req)
                assert resp.status == 200
                body = json.loads(resp.body)
                assert body["tip"] is not None
                assert body["tip"]["id"] == "gen-1"

    def test_sanitize_tip_action_valid_and_invalid(self) -> None:
        from kiro_crew.tips import _sanitize_tip_action

        # Valid internal route (with query + highlight) is projected as-is.
        ok = _sanitize_tip_action(
            {"kind": "route", "label": "Open X", "route": "/settings?tab=chat&highlight=x"}
        )
        assert ok == {
            "kind": "route",
            "label": "Open X",
            "route": "/settings?tab=chat&highlight=x",
        }
        # Rejected: not a dict / unknown kind / empty or oversized label.
        assert _sanitize_tip_action(None) is None
        assert _sanitize_tip_action("nope") is None
        assert _sanitize_tip_action({"kind": "message", "label": "L", "route": "/x"}) is None
        assert _sanitize_tip_action({"kind": "route", "label": "  ", "route": "/x"}) is None
        assert _sanitize_tip_action({"kind": "route", "label": "L" * 41, "route": "/x"}) is None
        # Rejected: off-origin / open-redirect / non-path routes.
        for bad in ("//evil.com", "https://evil.com", "http://x", "settings", "", "mailto:a@b"):
            assert _sanitize_tip_action({"kind": "route", "label": "L", "route": bad}) is None, bad
        # Rejected: missing route or non-string route.
        assert _sanitize_tip_action({"kind": "route", "label": "L"}) is None
        assert _sanitize_tip_action({"kind": "route", "label": "L", "route": 5}) is None

    def test_persisted_tip_attaches_valid_action_drops_invalid(self) -> None:
        from kiro_crew.tips import _sanitize_persisted_tip

        base = {
            "id": "x",
            "feature": "F",
            "title": "T",
            "body": "B",
            "why": "",
            "doc": "",
            "cta_prompt": "",
        }
        # Valid action is attached to the sanitized tip.
        good = _sanitize_persisted_tip(
            {**base, "action": {"kind": "route", "label": "Go", "route": "/apps"}}
        )
        assert good is not None
        assert good["action"] == {"kind": "route", "label": "Go", "route": "/apps"}
        # Invalid action is silently dropped; the tip itself still survives (no button).
        bad = _sanitize_persisted_tip(
            {**base, "action": {"kind": "route", "label": "Go", "route": "https://evil"}}
        )
        assert bad is not None
        assert "action" not in bad
        # No action key at all -> no action attached.
        none = _sanitize_persisted_tip(base)
        assert none is not None
        assert "action" not in none

    def test_shipped_curated_actions_match_capable_features(self) -> None:
        from kiro_crew.tips import _load_curated_tips, _sanitize_tip_action

        tips = {t["id"]: t for t in _load_curated_tips()}
        # Features with a navigable destination carry a valid, internal route action.
        for tid in (
            "split-view",
            "interface-cli-mode",
            "warm-pool",
            "mcp-gateway",
            "subagent-parallelism",
            "zero-token-cron",
            "dev-fleet",
            "app-store",
            "command-palette",
        ):
            action = tips[tid].get("action")
            assert action is not None, f"{tid} should have an action"
            assert _sanitize_tip_action(action) == action, f"{tid} action must be valid"
            assert action["route"].startswith("/")
        # Features with no in-dashboard destination render body only (no button).
        for tid in ("steer-or-queue", "local-telemetry"):
            assert "action" not in tips[tid], f"{tid} must not carry an action"

    def test_curated_tips_do_not_claim_a_catalog_doc(self) -> None:
        """A curated tip must not carry a `doc` the docs catalog also owns.

        Dismissing a tip appends its `doc` to `dismissed_docs`, and
        `_is_eligible` then refuses EVERY tip carrying that doc. So a curated
        tip that names an allowlisted doc silently takes the catalog's tip for
        the same doc down with it — one dismissal, two features permanently
        unmentionable. Doc-level dismissal exists to keep identity stable for
        LLM-generated tips whose ids churn between regenerations; a curated tip
        has a hand-authored, permanent id and does not need it.
        """
        from kiro_crew.tips import _load_curated_tips
        from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST

        collisions = {
            t["id"]: t["doc"] for t in _load_curated_tips() if t.get("doc") in TIP_DOC_ALLOWLIST
        }
        assert not collisions, (
            "curated tips claiming a catalog doc (dismissing one would also "
            f"suppress the catalog tip for that doc): {collisions}"
        )

    def test_curated_highlight_anchors_exist_in_settings_registry(self) -> None:
        """A tip's `highlight=` names a control the settings registry owns.

        Two forms reach the same highlight. `key:<configKey>` finds the control
        by a `data-setting-key` attribute, so it survives translation;
        `<tab>.<kebab-label>` resolves the registry's ENGLISH label and matches
        it against the rendered DOM label, which cannot match once the
        dashboard is translated (the button still opens the right tab, and the
        highlight is simply skipped). Prefer `key:` wherever the registry
        carries a configKey; only 6 of 97 entries do today.

        Either way the registry is generated from the panels, so a renamed or
        deleted control silently turns the button into a navigation that
        highlights nothing. Pin the two together.
        """
        from kiro_crew.tips import _load_curated_tips

        registry_file = (
            Path(__file__).resolve().parents[1]
            / "website/src/components/commandPalette/settingsRegistry.gen.ts"
        )
        if not registry_file.is_file():  # packaged install carries no website/ tree
            pytest.skip("settings registry not present in this tree")
        source = registry_file.read_text(encoding="utf-8")
        entries = json.loads(source[source.index("[\n  {") : source.rindex("]") + 1])
        by_id = {e["id"]: e for e in entries}
        by_config_key = {e["configKey"]: e for e in entries if e.get("configKey")}

        for tip in _load_curated_tips():
            route = tip.get("action", {}).get("route", "")
            query = parse_qs(urlparse(route).query)
            if "highlight" not in query:
                continue
            anchor = query["highlight"][0]
            if anchor.startswith("key:"):
                entry = by_config_key.get(anchor[len("key:") :])
                assert entry is not None, f"{tip['id']} highlights unknown config key {anchor!r}"
            else:
                entry = by_id.get(anchor)
                assert entry is not None, f"{tip['id']} highlights unknown control {anchor!r}"
                # An id-form anchor whose control HAS a configKey is a missed
                # chance at the translation-proof form.
                assert not entry.get("configKey"), (
                    f"{tip['id']} uses the label-derived id {anchor!r} while "
                    f"the control exposes configKey {entry['configKey']!r} — "
                    f"use highlight=key:{entry['configKey']} so the highlight "
                    f"survives a translated dashboard"
                )
            # Navigation state lives in the path now (/settings/<tab>), not in
            # a ?tab= query param — read the tab from the first segment.
            route_path = urlparse(route).path
            assert route_path.startswith(
                "/settings/"
            ), f"{tip['id']} action route {route!r} is not a settings path URL"
            route_tab = route_path.split("/")[2]
            assert route_tab == entry["tab"], (
                f"{tip['id']} highlights {anchor!r}, which lives on the "
                f"{entry['tab']!r} tab, not {route_tab!r}"
            )
            # A control on a sub-navigated tab (Settings -> Chat pages) is only
            # mounted when the route carries the page segment; without it the
            # tab opens on its default page and the highlight misses.
            sub = (entry.get("params") or {}).get("sub")
            if sub:
                segments = route_path.split("/")
                route_sub = segments[3] if len(segments) > 3 else None
                assert route_sub == sub, (
                    f"{tip['id']} highlights {anchor!r}, which lives on the "
                    f"{sub!r} page of the {entry['tab']!r} tab, but its route "
                    f"{route!r} opens {route_sub!r}"
                )

    @pytest.mark.asyncio
    async def test_curated_action_survives_serve(self, tmp_path: Path) -> None:
        import types
        from unittest.mock import MagicMock
        from unittest.mock import patch as mpatch

        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.tips import TipsCache, api_tips_next

        curated = {
            "id": "split-view",
            "feature": "Split View",
            "title": "Two sessions",
            "body": "Turn on split view.",
            "why": "",
            "doc": "",
            "cta_prompt": "",
            "action": {
                "kind": "route",
                "label": "Open Split View setting",
                "route": "/settings?tab=chat&highlight=chat.split-view-session-grid",
            },
        }
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cache = TipsCache()
            cache.curated = [curated]
            cache.state = TipsState(tips=[])
            state = types.SimpleNamespace(_tips_cache=cache)
            cfg = MagicMock()
            cfg.dashboard.tips_enabled = True
            cfg.dashboard.tips_cadence_hours = 0.0
            cfg.dashboard.tips_snooze_hours = 48.0
            cfg.dashboard.tips_recency_decay = 0.6
            cfg.dashboard.tips_explore_ratio = 0.0

            async def noop_refresh(*a: object, **k: object) -> None:
                return None

            with (
                mpatch("kiro_crew.tips.KiroCrewConfig") as mock_cfg_cls,
                mpatch("kiro_crew.tips.maybe_refresh", noop_refresh),
            ):
                mock_cfg_cls.load.return_value = cfg
                req = make_mocked_request("GET", "/api/tips/next")
                req.app["state"] = state
                resp = await api_tips_next(req)
                assert resp.status == 200
                body = json.loads(resp.body)
                assert body["tip"]["action"]["kind"] == "route"
                assert body["tip"]["action"]["route"].startswith("/settings?tab=chat")


class TestDocLinkSplit:
    """ "doc" is a tip's dismissal identity; "doc_link" is a rendering-only
    "learn more" hint. The two must never share one field: a curated tip that
    set `doc` to a catalog-owned doc silently took the catalog's tip for that
    doc down with it on a single dismissal.

    The companion ratchet (a curated tip must never claim a catalog doc as its
    dismissal identity) lives in TestCuratedTips.
    test_curated_tips_do_not_claim_a_catalog_doc and guards `doc` ONLY —
    `doc_link` is rendering-only and may freely name catalog docs.
    """

    @pytest.mark.asyncio
    async def test_doc_link_does_not_suppress_other_tips(self, tmp_path: Path) -> None:
        """Dismissing a tip with doc="" and a non-empty doc_link — through the
        REAL feedback handler — must not suppress any other tip sharing that
        doc_link value."""
        import types
        from unittest.mock import Mock

        from aiohttp import streams
        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.tips import TipsCache, api_tips_feedback

        curated = {
            "id": "curated-a",
            "feature": "F",
            "title": "T",
            "body": "B",
            "why": "",
            "doc": "",
            "doc_link": "skills.md",
            "cta_prompt": "",
        }
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cache = TipsCache()
            cache.curated = [curated]
            cache.state = TipsState(offered=dict(curated))
            state = types.SimpleNamespace(_tips_cache=cache)

            payload = streams.StreamReader(
                Mock(_reading_paused=False), 2**16, loop=asyncio.get_event_loop()
            )
            payload.feed_data(json.dumps({"id": "curated-a", "action": "dismiss"}).encode())
            payload.feed_eof()
            req = make_mocked_request("POST", "/api/tips/feedback", payload=payload)
            req.app["state"] = state
            resp = await api_tips_feedback(req)
            assert resp.status == 200

            st = cache.state
            assert "curated-a" in st.dismissed
            # The rendering-only doc_link must not become a dismissal identity.
            assert st.dismissed_docs == []
            # Another tip sharing the doc_link stays eligible…
            # flake-ok: a dismiss writes no snooze stamp, so neither _is_eligible call compares this instant; moved verbatim, tracked for a de-flake pass
            now = time.time()
            sibling = {"id": "catalog-b", "doc": "skills.md", "doc_link": "skills.md"}
            assert _is_eligible(sibling, st, now, 48.0)
            # …and so does a tip whose ONLY overlap is the doc_link value.
            link_only = {"id": "curated-c", "doc": "", "doc_link": "skills.md"}
            assert _is_eligible(link_only, st, now, 48.0)

    def test_is_eligible_ignores_doc_link_in_dismissed_docs(self) -> None:
        """Even a doc-level dismissal already on record keys on `doc` only."""
        now = 1000000.0
        st = TipsState(dismissed_docs=["skills.md"])
        # Same value in doc_link only → still eligible.
        assert _is_eligible({"id": "a", "doc": "", "doc_link": "skills.md"}, st, now, 48.0)
        # Same value in doc → suppressed (existing behavior preserved).
        assert not _is_eligible({"id": "b", "doc": "skills.md"}, st, now, 48.0)
        # doc_link is not consulted for snooze either.
        st2 = TipsState(snoozed_docs={"skills.md": now - 100})
        assert _is_eligible({"id": "c", "doc": "", "doc_link": "skills.md"}, st2, now, 48.0)

    def test_restored_curated_tips_carry_doc_link(self) -> None:
        """The tips whose learn-more link was lost when their colliding `doc`
        was split off get it back through doc_link."""
        from kiro_crew.tips import _load_curated_tips

        tips = {t["id"]: t for t in _load_curated_tips()}
        expected = {
            "auto-skills": "skills.md",
            "subagent-parallelism": "dynamic-subagent-sizing.md",
            "zero-token-cron": "cron-and-scheduling.md",
        }
        for tid, doc_link in expected.items():
            assert tips[tid]["doc_link"] == doc_link
            assert tips[tid]["doc"] == ""

    def test_parse_defaults_missing_doc_link(self) -> None:
        """LLM output authored without doc_link still parses; the projection
        carries doc_link as "" so downstream key access never raises."""
        raw = json.dumps(
            [
                {
                    "id": "t1",
                    "feature": "F",
                    "title": "T",
                    "body": "B",
                    "why": "W",
                    "doc": "d.md",
                    "cta_prompt": "C",
                }
            ]
        )
        tips = _parse_tips(raw)
        assert len(tips) == 1
        assert tips[0]["doc_link"] == ""

    def test_parse_sanitizes_invalid_doc_link(self) -> None:
        """doc_link goes through the same shape sanitization as doc: an
        LLM-invented non-URL, non-.md value is cleared, not served."""
        raw = json.dumps(
            [
                {
                    "id": "t1",
                    "feature": "F",
                    "title": "T",
                    "body": "B",
                    "why": "W",
                    "doc": "",
                    "doc_link": "javascript:alert(1)",
                    "cta_prompt": "C",
                },
                {
                    "id": "t2",
                    "feature": "F",
                    "title": "T",
                    "body": "B",
                    "why": "W",
                    "doc": "",
                    "doc_link": "skills.md",
                    "cta_prompt": "C",
                },
            ]
        )
        tips = _parse_tips(raw)
        assert tips[0]["doc_link"] == ""
        assert tips[1]["doc_link"] == "skills.md"

    def test_persisted_tip_without_doc_link_still_loads(self, tmp_path: Path) -> None:
        """State files written before doc_link existed keep loading; the
        sanitizer defaults the missing field to ""."""
        legacy = {
            "id": "t1",
            "feature": "F",
            "title": "T",
            "body": "B",
            "why": "W",
            "doc": "d.md",
            "cta_prompt": "C",
        }
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            (tmp_path / "tips_state.json").write_text(
                json.dumps({"tips": [legacy], "offered": legacy})
            )
            st = _load_state()
            assert st.tips and st.tips[0]["doc_link"] == ""
            assert st.offered is not None and st.offered["doc_link"] == ""

    def test_non_string_doc_link_rejects_tip(self) -> None:
        """Absence defaults to "", but a PRESENT non-string value still rejects."""
        t = {
            "id": "t1",
            "feature": "F",
            "title": "T",
            "body": "B",
            "why": "W",
            "doc": "",
            "doc_link": ["skills.md"],
            "cta_prompt": "C",
        }
        assert not _validate_tip_fields(t)


class TestRelinkedStateMigration:
    """State written before the doc -> doc_link split holds the old tip shape,
    whose dismissal would RE-RECORD the catalog doc. _load_state repairs the
    re-recording paths once (held-over tip shapes, stale shown_docs entries),
    gated by the relink_migrated marker. dismissed_docs itself is never
    touched: its entries carry no provenance, so lifting one risks reverting
    an intentional generated-tip dismissal."""

    _TID = "subagent-parallelism"
    _DOC = "dynamic-subagent-sizing.md"

    def _load(self, tmp_path: Path, data: dict) -> TipsState:  # type: ignore[type-arg]
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            (tmp_path / "tips_state.json").write_text(json.dumps(data))
            return _load_state()

    def test_collateral_doc_dismissal_not_lifted(self, tmp_path: Path) -> None:
        """A pre-split doc-level entry is preserved even when it looks like
        curated collateral: dismissed_docs carries no provenance, so the entry
        may equally record an intentional generated-tip dismissal, and
        reverting one of those is the harm class this split fixes."""
        st = self._load(tmp_path, {"dismissed": [self._TID], "dismissed_docs": [self._DOC]})
        assert self._TID in st.dismissed
        assert self._DOC in st.dismissed_docs

    def test_catalog_tips_own_dismissal_preserved(self, tmp_path: Path) -> None:
        """dismissed_docs is NEVER touched by the migration — including when
        the catalog's own tip id is also dismissed."""
        catalog_tid = self._DOC.replace(".md", "-tip")
        st = self._load(
            tmp_path,
            {
                "dismissed": [self._TID, catalog_tid],
                "dismissed_docs": [self._DOC],
            },
        )
        assert self._DOC in st.dismissed_docs

    def test_doc_entry_without_curated_dismissal_preserved(self, tmp_path: Path) -> None:
        """A doc-level entry the curated tip did not cause (e.g. an LLM tip
        about the doc was dismissed under an invented id) is left alone."""
        st = self._load(tmp_path, {"dismissed": ["llm-invented-7"], "dismissed_docs": [self._DOC]})
        assert self._DOC in st.dismissed_docs

    def test_fresh_state_never_migrates(self, tmp_path: Path) -> None:
        """A fresh install (no state file) has nothing predating the split, so
        it must start with relink_migrated=True — otherwise its first
        curated+generated dismissal pair sharing a doc would be lifted on the
        next load, silently reverting a brand-new user's dismissal."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            st = _load_state()  # no file on disk
            assert st.relink_migrated is True
            # Simulate the user dismissing the curated tip AND a generated tip
            # that legitimately claims the doc, post-split.
            st.dismissed = [self._TID, "llm-invented-7"]
            st.dismissed_docs = [self._DOC]
            _save_state(st)
            st2 = _load_state()
            assert self._DOC in st2.dismissed_docs  # NOT lifted

    def test_migration_is_one_shot(self, tmp_path: Path) -> None:
        """Once the marker is persisted the repair never re-runs: a held-over
        old-shape tip in state is left alone when relink_migrated is True."""
        old_shape = {
            "id": self._TID,
            "feature": "F",
            "title": "T",
            "body": "B",
            "why": "",
            "doc": self._DOC,
            "cta_prompt": "",
        }
        st = self._load(
            tmp_path,
            {
                "relink_migrated": True,
                "tips": [dict(old_shape)],
            },
        )
        assert st.tips[0]["doc"] == self._DOC  # NOT rewritten
        assert st.tips[0]["doc_link"] == ""

    def test_first_load_sets_and_persists_marker(self, tmp_path: Path) -> None:
        """The repair runs once, flips the marker, and the marker round-trips
        through _save_state so the next load skips the repair entirely."""
        old_shape = {
            "id": self._TID,
            "feature": "F",
            "title": "T",
            "body": "B",
            "why": "",
            "doc": self._DOC,
            "cta_prompt": "",
        }
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            (tmp_path / "tips_state.json").write_text(
                json.dumps(
                    {
                        "tips": [old_shape],
                    }
                )
            )
            st = _load_state()
            assert st.tips[0]["doc"] == ""  # pre-split shape repaired
            assert st.tips[0]["doc_link"] == self._DOC
            assert st.relink_migrated is True
            _save_state(st)
            on_disk = json.loads((tmp_path / "tips_state.json").read_text())
            assert on_disk["relink_migrated"] is True
            st2 = _load_state()
            assert st2.relink_migrated is True

    def test_heldover_old_shape_relinked(self, tmp_path: Path) -> None:
        """A persisted copy of the old tip shape (offered slot / cached pool)
        is rewritten doc -> doc_link so dismissing it after the upgrade cannot
        re-record the catalog doc."""
        old_shape = {
            "id": self._TID,
            "feature": "F",
            "title": "T",
            "body": "B",
            "why": "",
            "doc": self._DOC,
            "cta_prompt": "",
        }
        st = self._load(tmp_path, {"tips": [dict(old_shape)], "offered": dict(old_shape)})
        for t in [st.tips[0], st.offered]:
            assert t is not None
            assert t["doc"] == ""
            assert t["doc_link"] == self._DOC

    def test_stale_shown_docs_entry_dropped(self, tmp_path: Path) -> None:
        """A shown_docs mapping from the curated id to the catalog doc is the
        other re-recording path; loading drops it. Unrelated entries stay."""
        st = self._load(
            tmp_path,
            {
                "shown_docs": {self._TID: self._DOC, "other-tip": "subagents.md"},
            },
        )
        assert self._TID not in st.shown_docs
        assert st.shown_docs["other-tip"] == "subagents.md"

    def test_unrelated_tip_with_same_doc_untouched(self, tmp_path: Path) -> None:
        """Only the two relinked curated ids migrate: an LLM tip legitimately
        carrying the catalog doc keeps it as its dismissal identity."""
        llm_tip = {
            "id": "llm-invented-9",
            "feature": "F",
            "title": "T",
            "body": "B",
            "why": "",
            "doc": self._DOC,
            "cta_prompt": "",
        }
        st = self._load(tmp_path, {"tips": [llm_tip]})
        assert st.tips[0]["doc"] == self._DOC
        assert st.tips[0]["doc_link"] == ""

    def test_persisted_tip_link_shape_sanitized(self, tmp_path: Path) -> None:
        """Persisted tips go through the same link-shape sanitization as
        generated ones: a non-URL, non-.md value is cleared on load."""
        bad = {
            "id": "x",
            "feature": "F",
            "title": "T",
            "body": "B",
            "why": "",
            "doc": "javascript:alert(1)",
            "doc_link": "also not a doc",
            "cta_prompt": "",
        }
        st = self._load(tmp_path, {"tips": [bad]})
        assert st.tips[0]["doc"] == ""
        assert st.tips[0]["doc_link"] == ""
