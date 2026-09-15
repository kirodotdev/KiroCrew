"""Tests for the edition tip-pool seam (``PlatformContext.tips``).

The seam exists because a tip is an unprompted claim that a feature exists and
is worth using. On a build that does not have (or deliberately does not expose)
that feature, a public tip advertises a capability the user cannot reach — the
"WeChat integration" class of leak. So the seam REPLACES the pool instead of
unioning into it, and these tests pin the three ways a public tip could still
get out: through the curated file, through the docs-scan catalog, and through
generated tips cached in state from BEFORE the pool was composed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.platform import (
    PROFILE_ENTERPRISE,
    PROFILE_STANDALONE,
    PlatformCompositionError,
    build_default_context,
    reset_context,
    set_context,
)
from kiro_crew.platform.defaults import DefaultTipsProvider
from kiro_crew.tips import (
    TipsState,
    _reconcile_pool,
    _resolve_pool,
    _save_state,
    get_tips_cache,
)
from kiro_crew.tips_pool import (
    PUBLIC_POOL_ID,
    WITHHELD_POOL_ID,
    CatalogEntry,
    TipsPool,
)

_EDITION_TIP = {
    "id": "edition-only-tip",
    "feature": "Edition Feature",
    "title": "An edition-only feature",
    "body": "Only this build has it.",
    "why": "",
    "doc": "",
    "doc_link": "",
    "cta_prompt": "",
}


def _pool(pool_id: str = "edition-v1", **kw: object) -> TipsPool:
    return TipsPool(
        pool_id=pool_id,
        curated=kw.get("curated", (dict(_EDITION_TIP),)),  # type: ignore[arg-type]
        catalog=kw.get(  # type: ignore[arg-type]
            "catalog",
            (CatalogEntry(feature="Edition Feature", summary="s", doc="edition.md", mtime=9.0),),
        ),
    )


class _PoolProvider:
    """Minimal edition adapter — the shape a companion composition root supplies."""

    def __init__(self, pool: TipsPool | None) -> None:
        self._pool = pool

    def tips_pool(self) -> TipsPool | None:
        return self._pool


def _install(provider: object, *, profile: str = PROFILE_STANDALONE) -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    ctx = build_default_context(KiroCrewConfig.load(), profile=profile)
    set_context(dataclasses.replace(ctx, tips=provider))


class TestPoolIdentity:
    def test_public_default_supplies_no_pool(self) -> None:
        assert DefaultTipsProvider().tips_pool() is None

    def test_default_context_composes_the_default_provider(self) -> None:
        from kiro_crew.config.loader import KiroCrewConfig

        ctx = build_default_context(KiroCrewConfig.load())
        assert isinstance(ctx.tips, DefaultTipsProvider)
        assert ctx.tips.tips_pool() is None

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_empty_pool_id_is_refused(self, bad: str) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            TipsPool(pool_id=bad)

    def test_pool_may_not_impersonate_the_public_pool(self) -> None:
        # Reusing the public id would make a switch undetectable, so publicly
        # generated tips would survive into the edition build.
        with pytest.raises(ValueError, match="undetectable"):
            TipsPool(pool_id=PUBLIC_POOL_ID)

    def test_empty_pool_is_legal(self) -> None:
        pool = TipsPool(pool_id="edition-empty")
        assert pool.curated == ()
        assert pool.catalog == ()

    @pytest.mark.parametrize("bad", [None, 17, "tips", {"a": 1}])
    @pytest.mark.parametrize("field_name", ["curated", "catalog"])
    def test_non_sequence_container_is_refused(self, field_name: str, bad: object) -> None:
        # A packaged pool file with `"curated": null` would otherwise reach
        # iteration in _sanitize_pool and 500 every tips endpoint.
        with pytest.raises(TypeError, match=f"TipsPool.{field_name}"):
            TipsPool(pool_id="edition-v1", **{field_name: bad})  # type: ignore[arg-type]

    def test_list_container_is_frozen_to_a_tuple(self) -> None:
        # A JSON loader hands back lists; rejecting them would make the common
        # correct case a trap.
        pool = TipsPool(
            pool_id="edition-v1",
            curated=[dict(_EDITION_TIP)],  # type: ignore[arg-type]
            catalog=[CatalogEntry(feature="F", summary="s", doc="d.md")],  # type: ignore[arg-type]
        )
        assert isinstance(pool.curated, tuple)
        assert isinstance(pool.catalog, tuple)


class TestResolution:
    def test_no_context_resolves_to_the_public_pool(self) -> None:
        reset_context()
        assert _resolve_pool() is None

    def test_edition_pool_is_resolved(self) -> None:
        pool = _pool()
        _install(_PoolProvider(pool))
        assert _resolve_pool() is pool

    def test_composition_error_is_fail_closed(self) -> None:
        class _Broken:
            def tips_pool(self) -> TipsPool | None:
                raise PlatformCompositionError("companion did not compose")

        _install(_Broken())
        # Must NOT degrade to the public pool: that is the leak this seam closes.
        with pytest.raises(PlatformCompositionError):
            _resolve_pool()

    def test_transient_adapter_error_degrades_to_public_on_standalone(self) -> None:
        class _Flaky:
            def tips_pool(self) -> TipsPool | None:
                raise RuntimeError("transient")

        _install(_Flaky())
        assert _resolve_pool() is None

    def test_transient_adapter_error_withholds_on_an_edition_build(self) -> None:
        """A broken adapter on an edition host must not fall back to public tips.

        That fallback would serve exactly the tips the seam withholds, so a merely
        flaky adapter would reintroduce the leak. Withhold instead.
        """

        class _Flaky:
            def tips_pool(self) -> TipsPool | None:
                raise RuntimeError("transient")

        _install(_Flaky(), profile=PROFILE_ENTERPRISE)
        pool = _resolve_pool()
        assert pool is not None
        assert pool.pool_id == WITHHELD_POOL_ID
        assert pool.curated == () and pool.catalog == ()

    def test_wrong_return_type_degrades_to_public_on_standalone(self) -> None:
        _install(_PoolProvider("not-a-pool"))  # type: ignore[arg-type]
        assert _resolve_pool() is None

    def test_wrong_return_type_withholds_on_an_edition_build(self) -> None:
        _install(_PoolProvider("not-a-pool"), profile=PROFILE_ENTERPRISE)  # type: ignore[arg-type]
        pool = _resolve_pool()
        assert pool is not None
        assert pool.pool_id == WITHHELD_POOL_ID

    @pytest.mark.asyncio
    async def test_withheld_pool_serves_no_tips(self, tmp_path: Path) -> None:
        class _Flaky:
            def tips_pool(self) -> TipsPool | None:
                raise RuntimeError("transient")

        _install(_Flaky(), profile=PROFILE_ENTERPRISE)
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cache = await get_tips_cache(types.SimpleNamespace())
        assert cache.curated == []
        assert cache.catalog == []
        assert cache.state.pool_id == WITHHELD_POOL_ID


class TestCacheReplacement:
    @pytest.mark.asyncio
    async def test_edition_pool_replaces_curated_and_catalog(self, tmp_path: Path) -> None:
        _install(_PoolProvider(_pool()))
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cache = await get_tips_cache(types.SimpleNamespace())
        assert [t["id"] for t in cache.curated] == ["edition-only-tip"]
        assert [e.doc for e in cache.catalog] == ["edition.md"]
        assert cache.state.pool_id == "edition-v1"

    @pytest.mark.asyncio
    async def test_no_public_tip_survives_the_override(self, tmp_path: Path) -> None:
        # The concrete leak: a public curated tip (e.g. the WeChat integration
        # one) must not be reachable through ANY of the three pool sources.
        from kiro_crew.tips import _load_curated_tips

        public_ids = {t["id"] for t in _load_curated_tips()}
        assert public_ids, "public curated pool is empty; test would pass vacuously"

        _install(_PoolProvider(_pool()))
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cache = await get_tips_cache(types.SimpleNamespace())

        reachable = (
            {t.get("id", "") for t in cache.curated}
            | {t.get("id", "") for t in cache.state.tips}
            | {e.doc.replace(".md", "-tip") for e in cache.catalog}
        )
        assert not (reachable & public_ids)

    @pytest.mark.asyncio
    async def test_empty_edition_pool_does_not_fall_back_to_public(self, tmp_path: Path) -> None:
        _install(_PoolProvider(TipsPool(pool_id="edition-empty")))
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cache = await get_tips_cache(types.SimpleNamespace())
        assert cache.curated == []
        assert cache.catalog == []

    @pytest.mark.asyncio
    async def test_public_build_is_unchanged(self, tmp_path: Path) -> None:
        _install(DefaultTipsProvider())
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cache = await get_tips_cache(types.SimpleNamespace())
        assert cache.curated, "public build must still load the bundled curated tips"
        assert cache.catalog, "public build must still load the docs catalog"
        assert cache.state.pool_id == PUBLIC_POOL_ID


class TestCrossPoolReconciliation:
    def test_same_pool_keeps_cached_tips(self) -> None:
        st = TipsState(pool_id="edition-v1", tips=[dict(_EDITION_TIP)], last_generated=123.0)
        assert _reconcile_pool(st, "edition-v1") is False
        assert st.tips and st.last_generated == 123.0

    def test_switch_discards_generated_tips_and_offer(self) -> None:
        public_tip = {
            "id": "wechat-tip",
            "feature": "WeChat",
            "title": "WeChat integration",
            "body": "b",
            "why": "",
            "doc": "",
            "doc_link": "",
            "cta_prompt": "",
        }
        st = TipsState(
            pool_id=PUBLIC_POOL_ID,
            tips=[public_tip],
            offered=dict(public_tip),
            last_generated=123.0,
        )
        assert _reconcile_pool(st, "edition-v1") is True
        assert st.tips == []
        assert st.offered is None
        assert st.last_generated == 0.0
        assert st.pool_id == "edition-v1"

    def test_switch_preserves_user_dismissals(self) -> None:
        st = TipsState(
            pool_id=PUBLIC_POOL_ID,
            dismissed=["wechat-tip"],
            dismissed_docs=["channels.md"],
            snoozed={"x-tip": 5.0},
            snoozed_docs={"cron-and-scheduling.md": 5.0},
            opted_out=True,
        )
        _reconcile_pool(st, "edition-v1")
        assert st.dismissed == ["wechat-tip"]
        assert st.dismissed_docs == ["channels.md"]
        assert st.snoozed == {"x-tip": 5.0}
        assert st.snoozed_docs == {"cron-and-scheduling.md": 5.0}
        assert st.opted_out is True

    def test_switch_clears_shown_docs(self) -> None:
        """shown_docs is CACHE, not intent, and carrying it across corrupts.

        It maps tip id -> doc, and tip ids are author-chosen slugs, so two pools
        can name the same id. A stale entry lets a docless tip in the NEW pool
        resolve through a colliding id to the OLD pool's doc.
        """
        st = TipsState(pool_id=PUBLIC_POOL_ID, shown_docs={"shared-id": "channels.md"})
        _reconcile_pool(st, "edition-v1")
        assert st.shown_docs == {}

    @pytest.mark.asyncio
    async def test_colliding_id_cannot_dismiss_the_previous_pools_doc(self, tmp_path: Path) -> None:
        """Dismissing an edition tip must not suppress a public doc.

        The corruption path: a public tip is shown (recording id -> doc), the pool
        switches, the edition pool reuses that id for a DOCLESS tip, and dismissing
        it resolves the doc through the stale map and writes a permanent
        dismissal for a feature the user never refused.
        """
        from unittest.mock import MagicMock, Mock
        from unittest.mock import patch as mpatch

        from aiohttp import streams
        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.tips import api_tips_feedback

        shared_id = "shared-id"
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            # State as the public pool left it: id -> public doc.
            _save_state(TipsState(pool_id=PUBLIC_POOL_ID, shown_docs={shared_id: "channels.md"}))
            # The edition pool reuses the id for a tip with NO doc of its own.
            _install(
                _PoolProvider(
                    TipsPool(
                        pool_id="edition-v1",
                        curated=({**_EDITION_TIP, "id": shared_id, "doc": ""},),
                    )
                )
            )
            cfg = MagicMock()
            cfg.dashboard.tips_enabled = True
            cfg.dashboard.tips_cadence_hours = 0.0
            cfg.dashboard.tips_snooze_hours = 48.0
            cfg.dashboard.tips_recency_decay = 0.6
            cfg.dashboard.tips_explore_ratio = 0.0

            state = types.SimpleNamespace()
            with mpatch("kiro_crew.tips.KiroCrewConfig") as mock_cfg:
                mock_cfg.load.return_value = cfg
                cache = await get_tips_cache(state)
                payload = streams.StreamReader(
                    Mock(_reading_paused=False), 2**16, loop=asyncio.get_event_loop()
                )
                payload.feed_data(json.dumps({"id": shared_id, "action": "dismiss"}).encode())
                payload.feed_eof()
                req = make_mocked_request("POST", "/api/tips/feedback", payload=payload)
                req.app["state"] = state
                resp = await api_tips_feedback(req)

        assert resp.status == 200
        # The id is dismissed; the previous pool's doc is NOT dragged in with it.
        from kiro_crew.tips import _scoped

        assert cache.state.dismissed == [_scoped(cache.state, shared_id)]
        assert cache.state.dismissed_docs == []

    @pytest.mark.asyncio
    async def test_stale_public_tips_are_dropped_on_first_load(self, tmp_path: Path) -> None:
        """A host that gains the companion must not re-serve public tips."""
        stale = {
            "id": "wechat-tip",
            "feature": "WeChat",
            "title": "WeChat integration",
            "body": "b",
            "why": "",
            "doc": "",
            "doc_link": "",
            "cta_prompt": "",
        }
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _save_state(TipsState(pool_id=PUBLIC_POOL_ID, tips=[stale], offered=dict(stale)))
            _install(_PoolProvider(_pool()))
            cache = await get_tips_cache(types.SimpleNamespace())
            assert cache.state.tips == []
            assert cache.state.offered is None
            # ...and the new stamp is persisted, so a restart does not re-run it.
            from kiro_crew.tips import _state_path

            on_disk = json.loads(_state_path().read_text(encoding="utf-8"))
            assert on_disk["pool_id"] == "edition-v1"

    def test_pool_id_round_trips_through_disk(self, tmp_path: Path) -> None:
        from kiro_crew.tips import _load_state

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _save_state(TipsState(pool_id="edition-v1"))
            assert _load_state().pool_id == "edition-v1"

    def test_missing_or_bad_pool_id_loads_as_public(self, tmp_path: Path) -> None:
        from kiro_crew.tips import _load_state, _state_path

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _save_state(TipsState())
            path = _state_path()
            data = json.loads(path.read_text(encoding="utf-8"))
            del data["pool_id"]
            path.write_text(json.dumps(data), encoding="utf-8")
            assert _load_state().pool_id == PUBLIC_POOL_ID

            data["pool_id"] = 17
            path.write_text(json.dumps(data), encoding="utf-8")
            assert _load_state().pool_id == PUBLIC_POOL_ID

    @pytest.mark.asyncio
    async def test_unwritable_state_does_not_break_the_endpoints(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pool switch on a read-only data home must not 500 every tips call.

        The in-memory reconciliation is the part that matters; a failed write only
        costs re-running it next boot. Before this seam get_tips_cache wrote
        nothing, so a raising write would be a NEW way to take the feature down.
        """
        import kiro_crew.tips as tips_mod

        def _boom(_st: TipsState) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(tips_mod, "_save_state", _boom)
        _install(_PoolProvider(_pool()))
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cache = await get_tips_cache(types.SimpleNamespace())
        # Reconciled in memory despite the failed persist.
        assert cache.state.pool_id == "edition-v1"
        assert cache.state.tips == []
        assert [t["id"] for t in cache.curated] == ["edition-only-tip"]


class TestGeneratedTipContainment:
    """A restricted catalog stops a public feature being OFFERED to the model.

    It does not constrain what the model writes back, so a hallucinated public
    feature would return as a shape-valid tip and be served — on an edition build
    that is the unreachable-feature card this whole PR removes. Parse-time
    anchoring closes it.
    """

    @staticmethod
    def _payload(*docs: str) -> str:
        return json.dumps(
            [
                {
                    "id": f"tip-{i}",
                    "feature": f"F{i}",
                    "title": f"T{i}",
                    "body": "b",
                    "why": "w",
                    "doc": d,
                    "cta_prompt": "",
                }
                for i, d in enumerate(docs)
            ]
        )

    def test_no_catalog_means_no_anchoring(self) -> None:
        from kiro_crew.tips import _parse_tips

        # Backwards-compatible default: callers with no catalog keep every tip.
        tips = _parse_tips(self._payload("edition.md", "channels.md"))
        assert [t["doc"] for t in tips] == ["edition.md", "channels.md"]

    def test_tip_outside_the_active_catalog_is_dropped(self) -> None:
        from kiro_crew.tips import _parse_tips

        tips = _parse_tips(self._payload("edition.md", "channels.md"), allowed_docs={"edition.md"})
        assert [t["doc"] for t in tips] == ["edition.md"]

    def test_docless_generated_tip_is_dropped(self) -> None:
        from kiro_crew.tips import _parse_tips

        # No doc means no verifiable provenance, and the prompt asks for one.
        assert _parse_tips(self._payload(""), allowed_docs={"edition.md"}) == []

    def test_anchoring_runs_after_link_sanitization(self) -> None:
        from kiro_crew.tips import _clean_catalog_entry, _parse_tips

        # A doc the link sanitizer blanks is anchored as "" and so cannot match a
        # real catalog: _clean_catalog_entry refuses an empty doc, so "" is never
        # in allowed_docs. Order matters — anchoring before sanitization would
        # judge the pre-blanked value instead.
        assert _clean_catalog_entry("F", "s", "", 1.0) is None
        assert _parse_tips(self._payload("javascript:alert(1)"), allowed_docs={"edition.md"}) == []

    @pytest.mark.asyncio
    async def test_generation_anchors_against_the_edition_catalog(self, tmp_path: Path) -> None:
        """End to end: a hallucinated public doc does not survive generation."""
        from unittest.mock import AsyncMock, MagicMock
        from unittest.mock import patch as mpatch

        from kiro_crew.tips import generate_tips

        _install(_PoolProvider(_pool()))  # catalog = edition.md only
        payload = self._payload("edition.md", "channels.md")

        cfg = MagicMock()
        cfg.dashboard.tips_model = "auto"
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            state = types.SimpleNamespace(sessions=MagicMock())
            with (
                mpatch("kiro_crew.tips.KiroCrewConfig") as mock_cfg,
                mpatch("kiro_crew.tips.run_bg_oneliner", AsyncMock(return_value=payload)),
                mpatch("kiro_crew.tips._build_context", return_value=""),
            ):
                mock_cfg.load.return_value = cfg
                tips = await generate_tips(state)

        assert [t["doc"] for t in tips] == ["edition.md"]


class TestScopedDismissalIdentity:
    """Dismissal state is global and persistent; tip ids and docs are not unique.

    Two pools can independently name `cron-tip` or `channels.md`, so a flat
    namespace lets one pool's dismissal suppress the other's unrelated tip --
    which the user never saw and never refused. Dismissals must SURVIVE a switch
    (intent, honoured again on switching back) but must not be READ across pools.
    """

    def test_public_keys_stay_bare(self) -> None:
        from kiro_crew.tips import _scoped

        # Free migration: every pre-existing state file was the public pool's, so
        # its unprefixed entries are already correctly scoped.
        st = TipsState(pool_id=PUBLIC_POOL_ID)
        assert _scoped(st, "cron-tip") == "cron-tip"

    def test_edition_keys_are_namespaced(self) -> None:
        from kiro_crew.tips import _scoped

        st = TipsState(pool_id="edition-v1")
        assert _scoped(st, "cron-tip") != "cron-tip"
        assert "cron-tip" in _scoped(st, "cron-tip")

    def test_public_dismissal_does_not_suppress_an_edition_tip(self) -> None:
        from kiro_crew.tips import _is_eligible

        st = TipsState(
            pool_id="edition-v1",
            dismissed=["cron-tip"],
            dismissed_docs=["channels.md"],
            snoozed={"cron-tip": 0.0},
            snoozed_docs={"channels.md": 0.0},
        )
        # Same id as the dismissed public tip, but a different pool's tip.
        assert _is_eligible({"id": "cron-tip", "doc": ""}, st, 0.0, 48.0)
        # Same doc filename as the dismissed public doc.
        assert _is_eligible({"id": "other", "doc": "channels.md"}, st, 0.0, 48.0)

    def test_edition_dismissal_still_suppresses_its_own_tip(self) -> None:
        from kiro_crew.tips import _is_eligible, _scoped

        st = TipsState(pool_id="edition-v1")
        st.dismissed.append(_scoped(st, "cron-tip"))
        st.dismissed_docs.append(_scoped(st, "internal.md"))
        assert not _is_eligible({"id": "cron-tip", "doc": ""}, st, 0.0, 48.0)
        assert not _is_eligible({"id": "other", "doc": "internal.md"}, st, 0.0, 48.0)

    def test_public_dismissal_still_suppresses_its_own_tip(self) -> None:
        from kiro_crew.tips import _is_eligible

        st = TipsState(pool_id=PUBLIC_POOL_ID, dismissed=["cron-tip"])
        assert not _is_eligible({"id": "cron-tip", "doc": ""}, st, 0.0, 48.0)

    def test_dismissal_is_honoured_again_after_switching_back(self) -> None:
        from kiro_crew.tips import _is_eligible, _reconcile_pool

        st = TipsState(pool_id=PUBLIC_POOL_ID, dismissed=["cron-tip"])
        tip = {"id": "cron-tip", "doc": ""}
        _reconcile_pool(st, "edition-v1")
        assert _is_eligible(tip, st, 0.0, 48.0)  # invisible to the edition pool
        _reconcile_pool(st, PUBLIC_POOL_ID)
        assert not _is_eligible(tip, st, 0.0, 48.0)  # intent survived the round trip

    def test_prompt_sees_only_the_active_pools_dismissals_unscoped(self) -> None:
        from kiro_crew.tips import _active_pool_dismissals, _scoped

        st = TipsState(pool_id="edition-v1", dismissed=["public-tip"])
        st.dismissed.append(_scoped(st, "edition-tip"))
        assert _active_pool_dismissals(st) == ["edition-tip"]
        st.pool_id = PUBLIC_POOL_ID
        assert _active_pool_dismissals(st) == ["public-tip"]

    @pytest.mark.asyncio
    async def test_dismissing_an_edition_tip_scopes_what_it_writes(self, tmp_path: Path) -> None:
        """End to end: the recorded key must not collide with the public pool."""
        from unittest.mock import MagicMock, Mock
        from unittest.mock import patch as mpatch

        from aiohttp import streams
        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.tips import _is_eligible, api_tips_feedback

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _install(_PoolProvider(_pool(curated=({**_EDITION_TIP, "id": "cron-tip"},))))
            cfg = MagicMock()
            cfg.dashboard.tips_enabled = True
            cfg.dashboard.tips_cadence_hours = 0.0
            cfg.dashboard.tips_snooze_hours = 48.0
            cfg.dashboard.tips_recency_decay = 0.6
            cfg.dashboard.tips_explore_ratio = 0.0

            state = types.SimpleNamespace()
            with mpatch("kiro_crew.tips.KiroCrewConfig") as mock_cfg:
                mock_cfg.load.return_value = cfg
                cache = await get_tips_cache(state)
                payload = streams.StreamReader(
                    Mock(_reading_paused=False), 2**16, loop=asyncio.get_event_loop()
                )
                payload.feed_data(json.dumps({"id": "cron-tip", "action": "dismiss"}).encode())
                payload.feed_eof()
                req = make_mocked_request("POST", "/api/tips/feedback", payload=payload)
                req.app["state"] = state
                assert (await api_tips_feedback(req)).status == 200

        # Recorded, but not as the bare id a public tip would match.
        assert cache.state.dismissed != ["cron-tip"]
        assert len(cache.state.dismissed) == 1
        # The edition tip is suppressed; a public tip of the same name is not.
        assert not _is_eligible({"id": "cron-tip", "doc": ""}, cache.state, 0.0, 48.0)
        public_st = TipsState(pool_id=PUBLIC_POOL_ID, dismissed=list(cache.state.dismissed))
        assert _is_eligible({"id": "cron-tip", "doc": ""}, public_st, 0.0, 48.0)


class TestPoolEntryValidation:
    """A pool is trusted for intent, not for shape.

    An edition may load its pool from a packaged file, so it can carry the same
    malformed entries a bundled file can. An unhashable ``id`` reaching
    ``_is_eligible`` raises inside the snooze lookup and 500s every tips call.
    """

    def test_unhashable_id_would_crash_eligibility(self) -> None:
        # Pins the mechanism the sanitizer protects, so this class cannot pass
        # vacuously if _is_eligible later tolerates the bad value.
        from kiro_crew.tips import _is_eligible

        with pytest.raises(TypeError):
            _is_eligible({"id": []}, TipsState(snoozed={"x": 1.0}), 0.0, 48.0)

    @pytest.mark.asyncio
    async def test_malformed_curated_entries_are_dropped(self, tmp_path: Path) -> None:
        good = dict(_EDITION_TIP)
        pool = TipsPool(
            pool_id="edition-v1",
            curated=(
                good,
                {**_EDITION_TIP, "id": []},  # unhashable id
                {**_EDITION_TIP, "id": "no-body", "body": ""},  # empty required field
                {"id": "incomplete"},  # missing required fields
                "not-a-dict",  # type: ignore[arg-type]
            ),
        )
        _install(_PoolProvider(pool))
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cache = await get_tips_cache(types.SimpleNamespace())
        assert [t["id"] for t in cache.curated] == ["edition-only-tip"]

    @pytest.mark.asyncio
    async def test_malformed_catalog_entries_are_dropped(self, tmp_path: Path) -> None:
        pool = TipsPool(
            pool_id="edition-v1",
            curated=(),
            catalog=(
                CatalogEntry(feature="Good", summary="s", doc="good.md", mtime=1.0),
                CatalogEntry(feature="", summary="s", doc="empty.md", mtime=1.0),
                CatalogEntry(feature="NoDoc", summary="s", doc="", mtime=1.0),
                CatalogEntry(feature="BadMtime", summary="s", doc="nan.md", mtime=float("nan")),
                CatalogEntry(feature=17, summary="s", doc="x.md"),  # type: ignore[arg-type]
            ),
        )
        _install(_PoolProvider(pool))
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cache = await get_tips_cache(types.SimpleNamespace())
        assert [e.doc for e in cache.catalog] == ["good.md", "nan.md"]
        # A non-finite mtime is coerced, not carried into _select_tip's arithmetic.
        nan_entry = next(e for e in cache.catalog if e.doc == "nan.md")
        assert nan_entry.mtime == 0.0

    @pytest.mark.asyncio
    async def test_sanitized_pool_serves_without_error(self, tmp_path: Path) -> None:
        """End to end: a pool carrying a poisoned entry still serves a tip."""
        from unittest.mock import MagicMock
        from unittest.mock import patch as mpatch

        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.tips import api_tips_next

        _install(
            _PoolProvider(
                TipsPool(
                    pool_id="edition-v1",
                    curated=(dict(_EDITION_TIP), {**_EDITION_TIP, "id": []}),
                )
            )
        )
        cfg = MagicMock()
        cfg.dashboard.tips_enabled = True
        cfg.dashboard.tips_cadence_hours = 0.0
        cfg.dashboard.tips_snooze_hours = 48.0
        cfg.dashboard.tips_recency_decay = 0.6
        cfg.dashboard.tips_explore_ratio = 0.0

        async def _noop(*a: object, **k: object) -> None:
            return None

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            state = types.SimpleNamespace()
            with (
                mpatch("kiro_crew.tips.KiroCrewConfig") as mock_cfg,
                mpatch("kiro_crew.tips.maybe_refresh", _noop),
            ):
                mock_cfg.load.return_value = cfg
                req = make_mocked_request("GET", "/api/tips/next")
                req.app["state"] = state
                resp = await api_tips_next(req)
        assert resp.status == 200
        assert json.loads(resp.body)["tip"]["id"] == "edition-only-tip"
