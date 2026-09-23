"""Every dashboard ``config.json`` saver merges under the sidecar lock.

The savers under test each read ``config.json`` to validate a request, then
persist a few keys of their own section. The property pinned here is that the
persisted keys are merged into the file **as re-read inside
``update_config_locked``'s advisory lock**, not written back from the snapshot
taken for validation. A writer in another process (``kirocrew config set``)
that lands between those two reads is therefore preserved, where a snapshot
write-back would silently revert it.

The interleave is injected at the one point that distinguishes the two shapes:
the first ``update_config_locked`` call a save makes for its config path is
preceded by a foreign write to the same file. A saver that never reaches
``update_config_locked`` fails the ``injected`` assertion; a saver that reaches
it but writes its snapshot loses the foreign key.

``TestTheAtomicJsonWriteConfigFamilyIsRatcheted`` in
``test_config_rmw_preserves_settings.py`` is the static half of the same
guarantee: it scans for the unlocked spelling. This file is the behavioural
half, one save per converted handler.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

import kiro_crew.config.loader as loader

FOREIGN_KEY = "set_by_another_process"


# ── The transaction wrapper every saver runs under ─────────────────────────


class TestRunToCompletion:
    def test_defers_repeated_cancellation_until_the_work_is_done(self) -> None:
        from kiro_crew.dashboard.chat_utils import run_to_completion

        phases: list[str] = []
        gate = asyncio.Event()

        async def _transaction() -> str:
            phases.append("config")
            await gate.wait()
            phases.append("env")
            return "ok"

        async def _run() -> bool:
            task = asyncio.ensure_future(run_to_completion(_transaction()))
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()  # a second cancel (shutdown escalating) must not break out
            await asyncio.sleep(0)
            assert phases == ["config"] and not task.done()
            gate.set()
            try:
                await task
            except asyncio.CancelledError:
                return True
            return False

        assert asyncio.run(_run()) is True
        assert phases == ["config", "env"]

    def test_returns_the_result_and_propagates_an_exception(self) -> None:
        from kiro_crew.dashboard.chat_utils import run_to_completion

        async def _ok() -> int:
            return 7

        async def _boom() -> None:
            raise OSError("disk full")

        assert asyncio.run(run_to_completion(_ok())) == 7
        with pytest.raises(OSError):
            asyncio.run(run_to_completion(_boom()))


# ── The write helper the channel savers share ──────────────────────────────


class TestLockedSectionWrite:
    """``messaging._LockedSectionWrite`` merges into the fresh document and undoes
    only what it wrote."""

    @staticmethod
    def _cls():
        from kiro_crew.dashboard.handlers.messaging import _LockedSectionWrite

        return _LockedSectionWrite

    def test_apply_merges_into_the_document_it_is_handed(self, tmp_path: Path) -> None:
        w = self._cls()(tmp_path / "c.json", "slack", {"a": 1}, drop_keys=("legacy",))
        fresh = {"slack": {"legacy": "x", "keep": 2}, "other": {"k": 3}}
        out = w.apply(fresh)
        assert out is fresh
        assert out["slack"] == {"keep": 2, "a": 1}
        assert out["other"] == {"k": 3}

    def test_apply_creates_a_missing_or_non_object_section(self) -> None:
        w = self._cls()(Path("c.json"), "discord", {"enabled": True})
        assert w.apply({})["discord"] == {"enabled": True}
        assert w.apply({"discord": "junk"})["discord"] == {"enabled": True}

    def test_apply_seeds_a_missing_section_from_a_copy_of_the_legacy_one(self) -> None:
        w = self._cls()(Path("c.json"), "wecom", {"enabled": True}, seed_from="wechat")
        legacy = {"allowed_user_ids": ["zhangsan"], "soft_threshold_pct": 70}
        out = w.apply({"wechat": legacy})
        assert out["wecom"] == {
            "allowed_user_ids": ["zhangsan"],
            "soft_threshold_pct": 70,
            "enabled": True,
        }
        assert out["wechat"] == legacy  # copied, never mutated in place
        assert out["wechat"] is legacy
        # A present target section wins over the legacy one.
        out = w.apply({"wechat": legacy, "wecom": {"soft_threshold_pct": 90}})
        assert out["wecom"] == {"soft_threshold_pct": 90, "enabled": True}

    def test_restore_reverts_only_the_keys_still_holding_our_value(self) -> None:
        w = self._cls()(Path("c.json"), "teams", {"enabled": True, "allow": True})
        doc = w.apply({"teams": {"enabled": False, "allow": False}})
        # Someone else changed ``allow`` after our write landed.
        doc["teams"]["allow"] = "theirs"
        out = w.restore(doc)
        assert out["teams"] == {"enabled": False, "allow": "theirs"}

    def test_restore_removes_a_key_we_introduced(self) -> None:
        w = self._cls()(Path("c.json"), "webex", {"session_folder": "ops"})
        doc = w.apply({"webex": {"enabled": True}})
        assert w.restore(doc)["webex"] == {"enabled": True}

    def test_restore_puts_back_a_key_we_dropped_unless_someone_re_set_it(self) -> None:
        w = self._cls()(Path("c.json"), "telegram", {}, drop_keys=("bot_token",))
        doc = w.apply({"telegram": {"bot_token": "legacy"}})
        assert "bot_token" not in doc["telegram"]
        assert w.restore(doc)["telegram"] == {"bot_token": "legacy"}
        doc = w.apply({"telegram": {"bot_token": "legacy"}})
        doc["telegram"]["bot_token"] = "re-set"
        assert w.restore(doc)["telegram"] == {"bot_token": "re-set"}

    def test_restore_drops_a_section_only_we_created(self) -> None:
        w = self._cls()(Path("c.json"), "wecom", {"enabled": True})
        doc = w.apply({})
        assert w.restore(doc) == {}

    def test_restore_drops_a_seeded_section_so_it_cannot_shadow_the_legacy_one(self) -> None:
        legacy = {"allowed_user_ids": ["zhangsan"]}
        w = self._cls()(Path("c.json"), "wecom", {"enabled": True}, seed_from="wechat")
        doc = w.apply({"wechat": legacy})
        assert w.restore(doc) == {"wechat": legacy}
        # A key someone else added to the new section since makes it theirs.
        doc = w.apply({"wechat": legacy})
        doc["wecom"]["ws_url"] = "wss://theirs"
        assert w.restore(doc)["wecom"] == {**legacy, "ws_url": "wss://theirs"}

    def test_restore_of_a_seeded_section_puts_an_overwritten_legacy_value_back(self) -> None:
        """A key written OVER a seeded legacy value goes back to that value, so
        the undone section again equals its seed and is dropped -- a partial
        section left behind would shadow ``wechat`` and reset the field."""
        legacy = {"allowed_user_ids": ["zhangsan"], "soft_threshold_pct": 70}
        w = self._cls()(
            Path("c.json"),
            "wecom",
            {"allowed_user_ids": ["zhangsan", "lisi"], "enabled": True},
            seed_from="wechat",
        )
        doc = w.apply({"wechat": legacy})
        assert doc["wecom"]["allowed_user_ids"] == ["zhangsan", "lisi"]
        assert w.restore(doc) == {"wechat": legacy}

    def test_restore_puts_back_a_pre_existing_non_object_value(self) -> None:
        """A present ``null`` (or other junk) under the key is not the same as an
        absent key: the rollback restores it rather than deleting it."""
        for junk in (None, "junk", 3, ["x"]):
            w = self._cls()(Path("c.json"), "teams", {"enabled": True})
            doc = w.apply({"teams": junk, "other": 1})
            assert doc["teams"] == {"enabled": True}
            assert w.restore(doc) == {"teams": junk, "other": 1}
        # ...but a key someone else added since makes the section theirs.
        w = self._cls()(Path("c.json"), "teams", {"enabled": True})
        doc = w.apply({"teams": None})
        doc["teams"]["ws_url"] = "wss://theirs"
        assert w.restore(doc) == {"teams": {"ws_url": "wss://theirs"}}

    def test_finalize_sees_the_merged_section_and_its_adjustment_is_what_restore_undoes(
        self,
    ) -> None:
        def _pull_soft_down(section: dict) -> None:
            if section["soft_threshold_pct"] > section["hard_threshold_pct"]:
                section["soft_threshold_pct"] = section["hard_threshold_pct"]

        w = self._cls()(
            Path("c.json"), "webex", {"soft_threshold_pct": 90}, finalize=_pull_soft_down
        )
        # The counterpart in the FRESH document (hard=50) is what the rule sees.
        doc = w.apply({"webex": {"soft_threshold_pct": 80, "hard_threshold_pct": 50}})
        assert doc["webex"] == {"soft_threshold_pct": 50, "hard_threshold_pct": 50}
        # Rollback recognises the ADJUSTED value as ours and puts the prior back.
        assert w.restore(doc)["webex"] == {"soft_threshold_pct": 80, "hard_threshold_pct": 50}

    def test_restore_undoes_a_key_finalize_moved_that_the_request_never_named(self) -> None:
        """Lowering ``hard`` below the stored ``soft`` makes ``finalize`` pull soft
        down too; a rollback must put BOTH back, not only the requested key."""

        def _pull_soft_down(section: dict) -> None:
            if section["soft_threshold_pct"] > section["hard_threshold_pct"]:
                section["soft_threshold_pct"] = section["hard_threshold_pct"]

        w = self._cls()(
            Path("c.json"), "webex", {"hard_threshold_pct": 70}, finalize=_pull_soft_down
        )
        doc = w.apply({"webex": {"soft_threshold_pct": 80, "hard_threshold_pct": 95}})
        assert doc["webex"] == {"soft_threshold_pct": 70, "hard_threshold_pct": 70}
        assert w.restore(doc)["webex"] == {"soft_threshold_pct": 80, "hard_threshold_pct": 95}

    def test_finalize_raising_aborts_the_write(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"teams": {"hard_threshold_pct": 50}}), encoding="utf-8")

        class _Inverted(Exception):
            pass

        def _refuse(section: dict) -> None:
            raise _Inverted()

        w = self._cls()(cfg, "teams", {"soft_threshold_pct": 90}, finalize=_refuse)
        with pytest.raises(_Inverted):
            asyncio.run(w.commit())
        assert json.loads(cfg.read_text(encoding="utf-8")) == {"teams": {"hard_threshold_pct": 50}}

    def test_blank_keys_blank_a_present_key_and_restore_puts_it_back(self) -> None:
        w = self._cls()(Path("c.json"), "webex", {"enabled": True}, blank_keys=("bot_token",))
        doc = w.apply({"webex": {"bot_token": "legacy-tok", "enabled": False}})
        assert doc["webex"] == {"bot_token": "", "enabled": True}
        assert w.restore(doc)["webex"] == {"bot_token": "legacy-tok", "enabled": False}
        # Absent key: nothing is added.
        assert w.apply({"webex": {"enabled": False}})["webex"] == {"enabled": True}

    def test_apply_skips_the_write_when_nothing_would_change(self, tmp_path: Path) -> None:
        w = self._cls()(Path("c.json"), "slack", {"a": 1}, drop_keys=("gone",))
        assert w.apply({"slack": {"a": 1, "b": 2}}) is None
        assert w.apply({"slack": {"a": 0}}) is not None
        # A section this write creates is always written.
        assert w.apply({}) is not None

    def test_restore_leaves_a_section_someone_replaced_wholesale(self) -> None:
        w = self._cls()(Path("c.json"), "wecom", {"enabled": True})
        w.apply({"wecom": {}})
        assert w.restore({"wecom": "replaced"}) == {"wecom": "replaced"}

    def test_commit_holds_the_sidecar_lock_off_the_loop(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"slack": {"a": 0}}), encoding="utf-8")
        threads: list[int] = []
        real = loader.write_config_atomically

        def _rec(path, data, **kw):
            threads.append(threading.get_ident())
            real(path, data, **kw)

        w = self._cls()(cfg, "slack", {"a": 1})

        async def _run(monkeypatch_write: Callable[[], None]) -> None:
            monkeypatch_write()
            await w.commit()

        with pytest.MonkeyPatch.context() as mp:
            asyncio.run(_run(lambda: mp.setattr(loader, "write_config_atomically", _rec)))
        assert threads and all(t != threading.get_ident() for t in threads)
        assert json.loads(cfg.read_text(encoding="utf-8"))["slack"] == {"a": 1}

    def test_commit_survives_a_cancelled_caller(self, tmp_path: Path) -> None:
        """A thread cannot be cancelled, so the write always lands; the caller
        must not unwind before it does.

        The sidecar lock is held from another thread so the commit is parked
        inside ``update_config_locked``; the awaiting task is cancelled while it
        waits. The write must be on disk when ``CancelledError`` reaches the
        caller -- a caller that unwound early would release ``_get_config_lock()``
        and skip its paired ``.env`` write with the file still being rewritten.
        """
        import os

        from kiro_crew.platform_compat import file_lock

        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"teams": {"enabled": False}}), encoding="utf-8")
        holding = threading.Event()
        release = threading.Event()

        def _hold_the_sidecar() -> None:
            fd = os.open(str(cfg) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                with file_lock(fd):
                    holding.set()
                    release.wait(timeout=10)
            finally:
                os.close(fd)

        w = self._cls()(cfg, "teams", {"enabled": True})

        async def _run() -> tuple[bool, bool]:
            worker = threading.Thread(target=_hold_the_sidecar, daemon=True)
            worker.start()
            try:
                assert holding.wait(timeout=10)
                task = asyncio.ensure_future(w.commit())
                await asyncio.sleep(0.2)
                assert not task.done(), "the commit did not wait for the sidecar lock"
                task.cancel()
                await asyncio.sleep(0.2)
                # Cancelled, lock still held: the caller must still be parked.
                still_parked = not task.done()
                release.set()
                try:
                    await task
                except asyncio.CancelledError:
                    cancelled = True
                else:
                    cancelled = False
            finally:
                # The lock holder must not outlive the test on any exit path: a
                # failed assertion above would otherwise leave the sidecar held.
                release.set()
                worker.join(timeout=10)
                assert not worker.is_alive(), "the lock-holder thread did not terminate"
            return still_parked, cancelled

        still_parked, cancelled = asyncio.run(_run())
        assert still_parked, "the caller unwound while the worker was still waiting to write"
        assert cancelled, "the cancellation must still be delivered once the write is done"
        assert json.loads(cfg.read_text(encoding="utf-8"))["teams"]["enabled"] is True


# ── One save per converted handler ─────────────────────────────────────────


def _intercept_first_locked_write(monkeypatch, cfg: Path) -> list[bool]:
    """Land a foreign key in *cfg* just before the save's first locked write.

    Returns the ``injected`` list; empty afterwards means the save never reached
    ``update_config_locked`` for its config path.
    """
    real_update = loader.update_config_locked
    gate = threading.Lock()
    injected: list[bool] = []

    def _foreign(data: dict) -> dict:
        data[FOREIGN_KEY] = True
        return data

    def _interleave(*a: Any, **kw: Any):
        target = a[0] if a else kw.get("path")
        with gate:
            first = not injected and target == cfg
            if first:
                injected.append(True)
        if first:
            real_update(cfg, mutate=_foreign)
        return real_update(*a, **kw)

    monkeypatch.setattr(loader, "update_config_locked", _interleave)
    return injected


def _seed(monkeypatch, tmp_path: Path, doc: dict) -> Path:
    """Write *doc* as the config, pointing the loader and env at *tmp_path*.

    ``KiroCrewConfig.load()`` persists its one-time migrations on the first
    load of a bare document; landing them now keeps that write out of the
    interception below, which must fire on the SAVE's locked write only.
    """
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(doc), encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.setattr(loader, "env_path", lambda: env)
    loader.KiroCrewConfig.load()
    assert FOREIGN_KEY not in json.loads(cfg.read_text(encoding="utf-8"))
    return cfg


def _messaging_put(mod, route: str, handler, body: dict) -> int:
    from aiohttp.test_utils import TestClient, TestServer

    async def _run() -> int:
        app = web.Application()
        app["state"] = SimpleNamespace()
        app.router.add_put(route, handler)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(route, json=body)
            return resp.status

    return asyncio.run(_run())


@pytest.mark.parametrize(
    ("channel", "handler_name", "body", "expect"),
    [
        (
            "slack",
            "api_slack_config_save",
            {"reactions_enabled": True},
            ("reactions_enabled", True),
        ),
        ("discord", "api_discord_config_save", {"enabled": True}, ("enabled", True)),
        ("telegram", "api_telegram_config_save", {"enabled": True}, ("enabled", True)),
        ("teams", "api_teams_config_save", {"enabled": True}, ("enabled", True)),
        ("webex", "api_webex_config_save", {"enabled": True}, ("enabled", True)),
        ("wecom", "api_wecom_config_save", {"enabled": True}, ("enabled", True)),
    ],
)
def test_channel_save_keeps_a_write_that_landed_after_its_snapshot(
    tmp_path: Path, monkeypatch, channel: str, handler_name: str, body: dict, expect
) -> None:
    import kiro_crew.dashboard.handlers.messaging as mod

    cfg = _seed(monkeypatch, tmp_path, {channel: {"enabled": False}, "other": {"keep": 1}})
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    injected = _intercept_first_locked_write(monkeypatch, cfg)

    status = _messaging_put(mod, f"/api/{channel}/config", getattr(mod, handler_name), body)

    assert status == 200, status
    assert injected, "the save never reached update_config_locked for config.json"
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    key, value = expect
    assert saved[channel][key] == value  # ours landed
    assert saved[FOREIGN_KEY] is True  # theirs survived
    assert saved["other"] == {"keep": 1}  # untouched section untouched


def test_wecom_save_keeps_the_legacy_wechat_settings_on_first_save(
    tmp_path: Path, monkeypatch
) -> None:
    """An install still on the renamed ``wechat`` key keeps its allow-list.

    The saver seeds ``wecom`` from a copy of ``wechat`` when the new section is
    absent -- inside the locked mutation, so the seed comes from the document as
    read under the lock, not from the validation snapshot.
    """
    import kiro_crew.dashboard.handlers.messaging as mod

    legacy = {"allowed_user_ids": ["zhangsan"], "soft_threshold_pct": 70}
    cfg = _seed(monkeypatch, tmp_path, {"wechat": legacy})
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    status = _messaging_put(mod, "/api/wecom/config", mod.api_wecom_config_save, {"enabled": True})

    assert status == 200, status
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["wecom"] == {**legacy, "enabled": True}
    assert saved["wechat"] == legacy  # the legacy block is left as it was


def test_stt_put_keeps_a_write_that_landed_after_its_snapshot(tmp_path: Path, monkeypatch) -> None:
    import kiro_crew.dashboard.handlers.core as core

    cfg = _seed(monkeypatch, tmp_path, {"stt": {"language_code": "en-US"}})
    monkeypatch.setattr(core, "config_path", lambda: cfg)
    # The GET tail probes the host (ffmpeg, optional extras); not the subject here.
    monkeypatch.setattr(core, "_stt_prereq_commands", lambda provider: {})
    monkeypatch.setattr(core, "is_available", lambda stt: False)
    injected = _intercept_first_locked_write(monkeypatch, cfg)

    req = MagicMock(spec=web.Request)
    req.method = "PUT"
    req.json = AsyncMock(return_value={"language_code": "it-IT"})
    resp = asyncio.run(core.api_stt_config(req))

    assert resp.status == 200
    assert injected, "the PUT never reached update_config_locked for config.json"
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["stt"]["language_code"] == "it-IT"
    assert saved[FOREIGN_KEY] is True


def test_mcp_gateway_toggle_keeps_a_write_that_landed_after_its_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    from body_stream_helpers import BodyStreamPayload
    from dashboard_owner_helpers import owner_claims

    import kiro_crew.dashboard.handlers.mcp as mcp_mod

    cfg = _seed(monkeypatch, tmp_path, {"mcp_gateway": {"enabled": True, "stub_servers": ["x"]}})
    monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
    injected = _intercept_first_locked_write(monkeypatch, cfg)

    body = {"enabled": False}
    raw = json.dumps(body).encode()
    req = MagicMock(spec=web.Request)
    req.json = AsyncMock(return_value=body)
    req.content = BodyStreamPayload(raw)
    req.content_length = len(raw)
    req.charset = None
    req.can_read_body = True
    req.method = "POST"
    req.query = {}
    req.match_info = {}
    req.app = {
        "state": SimpleNamespace(
            owner_id="",
            _mcp_gateway_apply=AsyncMock(return_value={"running": False, "ping_ok": False}),
        )
    }
    resp = asyncio.run(mcp_mod.api_mcp_gateway_enable(owner_claims(req)))

    assert resp.status == 200, resp.text
    assert injected, "the toggle never reached update_config_locked for config.json"
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["mcp_gateway"]["enabled"] is False
    assert saved["mcp_gateway"]["stub_servers"] == ["x"]
    assert saved[FOREIGN_KEY] is True


# ── Coupled threshold pairs are decided against the document the write lands on ──


def _intercept_first_locked_write_with(monkeypatch, cfg: Path, foreign) -> list[bool]:
    """Like ``_intercept_first_locked_write`` but with a caller-supplied mutation."""
    real_update = loader.update_config_locked
    gate = threading.Lock()
    injected: list[bool] = []

    def _interleave(*a: Any, **kw: Any):
        target = a[0] if a else kw.get("path")
        with gate:
            first = not injected and target == cfg
            if first:
                injected.append(True)
        if first:
            real_update(cfg, mutate=foreign)
        return real_update(*a, **kw)

    monkeypatch.setattr(loader, "update_config_locked", _interleave)
    return injected


def test_teams_refuses_a_threshold_pair_inverted_only_in_the_merged_document(
    tmp_path: Path, monkeypatch
) -> None:
    """Dashboard sets soft=90 while another process lowers hard to 50: the pair is
    valid against the snapshot and inverted against the fresh document. The save
    is refused with the same code the pre-lock check uses, and nothing is written."""
    import kiro_crew.dashboard.handlers.messaging as mod

    cfg = _seed(
        monkeypatch, tmp_path, {"teams": {"soft_threshold_pct": 80, "hard_threshold_pct": 95}}
    )
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    def _lower_hard(data: dict) -> dict:
        data["teams"]["hard_threshold_pct"] = 50
        return data

    injected = _intercept_first_locked_write_with(monkeypatch, cfg, _lower_hard)
    status = _messaging_put(
        mod, "/api/teams/config", mod.api_teams_config_save, {"soft_threshold_pct": 90}
    )

    assert injected
    assert status == 400, status
    saved = json.loads(cfg.read_text(encoding="utf-8"))["teams"]
    assert saved["soft_threshold_pct"] == 80  # ours never landed
    assert saved["hard_threshold_pct"] == 50  # theirs kept


def test_webex_normalizes_the_threshold_pair_against_the_merged_document(
    tmp_path: Path, monkeypatch
) -> None:
    """Same race on Webex, which normalizes rather than refuses: the stored pair is
    the one the loader would compute (soft pulled down to the concurrent hard)."""
    import kiro_crew.dashboard.handlers.messaging as mod

    cfg = _seed(
        monkeypatch, tmp_path, {"webex": {"soft_threshold_pct": 80, "hard_threshold_pct": 95}}
    )
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    def _lower_hard(data: dict) -> dict:
        data["webex"]["hard_threshold_pct"] = 50
        return data

    injected = _intercept_first_locked_write_with(monkeypatch, cfg, _lower_hard)
    status = _messaging_put(
        mod, "/api/webex/config", mod.api_webex_config_save, {"soft_threshold_pct": 90}
    )

    assert injected
    assert status == 200, status
    saved = json.loads(cfg.read_text(encoding="utf-8"))["webex"]
    assert (saved["soft_threshold_pct"], saved["hard_threshold_pct"]) == (50, 50)


def test_webex_rollback_restores_the_soft_threshold_the_normalizer_lowered(
    tmp_path: Path, monkeypatch
) -> None:
    """hard 95->70 with stored soft 80 and a token that fails to write: the
    normalizer lowered soft to 70 inside the lock; the rollback puts both back."""
    import kiro_crew.dashboard.handlers.messaging as mod

    cfg = _seed(
        monkeypatch, tmp_path, {"webex": {"soft_threshold_pct": 80, "hard_threshold_pct": 95}}
    )
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    monkeypatch.setattr(mod, "_validate_webex_token", lambda tok: None)

    async def _boom(updates):
        raise OSError("read-only file system")

    monkeypatch.setattr(mod, "_write_env_off_loop", _boom)
    status = _messaging_put(
        mod,
        "/api/webex/config",
        mod.api_webex_config_save,
        {"hard_threshold_pct": 70, "bot_token": "x" * 40},
    )

    assert status >= 500, status
    saved = json.loads(cfg.read_text(encoding="utf-8"))["webex"]
    assert (saved["soft_threshold_pct"], saved["hard_threshold_pct"]) == (80, 95)


def test_slack_refused_config_write_leaves_the_credential_unwritten(
    tmp_path: Path, monkeypatch
) -> None:
    """Config commits first: when the locked reread finds the file corrupt, the
    save answers 500 and .env has NOT been touched -- no half-applied save."""
    import kiro_crew.dashboard.handlers.messaging as mod

    cfg = _seed(monkeypatch, tmp_path, {"slack": {}})
    env = tmp_path / ".env"
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    async def _accept(key, tok):
        return None

    monkeypatch.setattr(mod, "_validate_slack_token", _accept)

    real_update = loader.update_config_locked

    def _interleave(*a: Any, **kw: Any):
        if (a[0] if a else kw.get("path")) == cfg:
            cfg.write_text("{not json", encoding="utf-8")
        return real_update(*a, **kw)

    monkeypatch.setattr(loader, "update_config_locked", _interleave)
    status = _messaging_put(
        mod,
        "/api/slack/config",
        mod.api_slack_config_save,
        {"reactions_enabled": True, "bot_token": "xoxb-" + "a" * 30},
    )

    assert status == 500, status
    assert "SLACK_BOT_TOKEN" not in env.read_text(encoding="utf-8")


def test_slack_failed_credential_write_rolls_the_config_back(tmp_path: Path, monkeypatch) -> None:
    import kiro_crew.dashboard.handlers.messaging as mod

    cfg = _seed(monkeypatch, tmp_path, {"slack": {"reactions_enabled": False}})
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    async def _accept(key, tok):
        return None

    async def _boom(updates):
        raise OSError("read-only file system")

    monkeypatch.setattr(mod, "_validate_slack_token", _accept)
    monkeypatch.setattr(mod, "_write_env_off_loop", _boom)
    status = _messaging_put(
        mod,
        "/api/slack/config",
        mod.api_slack_config_save,
        {"reactions_enabled": True, "bot_token": "xoxb-" + "a" * 30},
    )

    assert status >= 500, status
    assert json.loads(cfg.read_text(encoding="utf-8"))["slack"]["reactions_enabled"] is False


def test_slack_config_and_env_writes_run_under_the_live_config_hold(
    tmp_path: Path, monkeypatch
) -> None:
    """Nothing between the config commit and the .env commit may be applied to the
    running gateway: a widened allow-list must not go live on a save that then
    fails and rolls back."""
    import kiro_crew.dashboard.handlers.messaging as mod
    from kiro_crew.config import live

    _seed(monkeypatch, tmp_path, {"slack": {"reactions_enabled": False}})
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    async def _accept(key, tok):
        return None

    monkeypatch.setattr(mod, "_validate_slack_token", _accept)
    watcher = live.watch()
    held_at: list[str] = []
    real_write = loader.write_config_atomically
    real_env = mod._write_env_off_loop

    def _cfg(path, data, **kw):
        held_at.append(f"config:{watcher._hold_depth}")
        real_write(path, data, **kw)

    async def _env(updates):
        held_at.append(f"env:{watcher._hold_depth}")
        await real_env(updates)

    monkeypatch.setattr(loader, "write_config_atomically", _cfg)
    monkeypatch.setattr(mod, "_write_env_off_loop", _env)
    status = _messaging_put(
        mod,
        "/api/slack/config",
        mod.api_slack_config_save,
        {"reactions_enabled": True, "bot_token": "xoxb-" + "a" * 30},
    )
    assert status == 200, status
    assert held_at == ["config:1", "env:1"], held_at
    assert watcher._hold_depth == 0


def test_teams_refuses_a_credential_tuple_changed_only_in_the_merged_document(
    tmp_path: Path, monkeypatch
) -> None:
    """Azure verified (app_id=A, password, tenant); another process changes
    ``teams.app_id`` after the snapshot. The write must not store the new
    password under an app id Azure never saw."""
    import kiro_crew.dashboard.handlers.messaging as mod

    app_id = "a" * 36
    cfg = _seed(monkeypatch, tmp_path, {"teams": {"app_id": app_id, "tenant_id": "t" * 36}})
    env = tmp_path / ".env"
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    for var in ("MICROSOFT_APP_ID", "MICROSOFT_APP_PASSWORD", "MICROSOFT_APP_TENANT_ID"):
        monkeypatch.delenv(var, raising=False)

    async def _no_network(*a: Any, **kw: Any):
        return None

    monkeypatch.setattr(mod, "_validate_teams_app_credentials", _no_network)

    def _swap_app_id(data: dict) -> dict:
        data["teams"]["app_id"] = "b" * 36
        return data

    injected = _intercept_first_locked_write_with(monkeypatch, cfg, _swap_app_id)
    status = _messaging_put(
        mod,
        "/api/teams/config",
        mod.api_teams_config_save,
        {"app_password": "new-pass", "app_id": app_id, "enabled": True},
    )

    assert injected
    assert status == 400, status
    saved = json.loads(cfg.read_text(encoding="utf-8"))["teams"]
    assert saved["app_id"] == "b" * 36  # theirs kept
    assert saved.get("enabled") is not True  # ours never landed
    assert "MICROSOFT_APP_PASSWORD=new-pass" not in env.read_text(encoding="utf-8")


# ── A legacy plaintext token is purged from the document the write lands on ──


@pytest.mark.parametrize(
    ("channel", "handler_name", "body", "expect_absent"),
    [
        ("discord", "api_discord_config_save", {"bot_token_clear": True}, True),
        ("telegram", "api_telegram_config_save", {"bot_token_clear": True}, True),
        ("webex", "api_webex_config_save", {"bot_token_clear": True}, False),
    ],
)
def test_clearing_the_credential_purges_a_legacy_token_landed_after_the_snapshot(
    tmp_path: Path, monkeypatch, channel: str, handler_name: str, body: dict, expect_absent: bool
) -> None:
    """The snapshot has no legacy ``bot_token``; another process writes one after
    the snapshot and before the locked write; the clear must still leave no
    usable fallback in ``config.json``."""
    import kiro_crew.dashboard.handlers.messaging as mod

    cfg = _seed(monkeypatch, tmp_path, {channel: {"enabled": False}})
    env = tmp_path / ".env"
    env.write_text(
        {
            "discord": "DISCORD_BOT_TOKEN",
            "telegram": "TELEGRAM_BOT_TOKEN",
            "webex": "WEBEX_BOT_TOKEN",
        }[channel]
        + "=live\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    def _plant_legacy(data: dict) -> dict:
        data[channel]["bot_token"] = "planted-after-snapshot"
        return data

    injected = _intercept_first_locked_write_with(monkeypatch, cfg, _plant_legacy)
    status = _messaging_put(mod, f"/api/{channel}/config", getattr(mod, handler_name), body)

    assert status == 200, status
    assert injected, "the save never reached update_config_locked for config.json"
    saved = json.loads(cfg.read_text(encoding="utf-8"))[channel]
    if expect_absent:
        assert "bot_token" not in saved
    else:
        assert saved["bot_token"] == ""
    assert "=live" not in env.read_text(encoding="utf-8")


def test_teams_save_blanks_a_legacy_password_landed_after_the_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    import kiro_crew.dashboard.handlers.messaging as mod

    cfg = _seed(monkeypatch, tmp_path, {"teams": {"enabled": False}})
    env = tmp_path / ".env"
    env.write_text("MICROSOFT_APP_PASSWORD=live\n", encoding="utf-8")
    monkeypatch.setenv("MICROSOFT_APP_PASSWORD", "live")
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    def _plant_legacy(data: dict) -> dict:
        data["teams"]["app_password"] = "planted-after-snapshot"
        return data

    injected = _intercept_first_locked_write_with(monkeypatch, cfg, _plant_legacy)
    status = _messaging_put(mod, "/api/teams/config", mod.api_teams_config_save, {"enabled": True})

    assert status == 200, status
    assert injected
    saved = json.loads(cfg.read_text(encoding="utf-8"))["teams"]
    assert saved["app_password"] == ""
    assert saved["enabled"] is True


# ── A cancelled request cannot split the config write from the .env write ──


def _hold_sidecar(cfg: Path, holding: threading.Event, release: threading.Event) -> None:
    import os

    from kiro_crew.platform_compat import file_lock

    fd = os.open(str(cfg) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with file_lock(fd):
            holding.set()
            release.wait(timeout=10)
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    ("channel", "handler_name", "body", "cred_line"),
    [
        (
            "teams",
            "api_teams_config_save",
            {"enabled": True, "app_password": "new-pass", "app_id": "a" * 36},
            "MICROSOFT_APP_PASSWORD=new-pass",
        ),
        (
            "webex",
            "api_webex_config_save",
            {"enabled": True, "bot_token": "x" * 40},
            "WEBEX_BOT_TOKEN=" + "x" * 40,
        ),
        (
            "wecom",
            "api_wecom_config_save",
            {"enabled": True, "bot_id": "wxb-1234567890abcdef", "bot_token": "s" * 40},
            "WECOM_SECRET=" + "s" * 40,
        ),
    ],
)
def test_save_cancelled_during_the_config_commit_still_writes_the_credential(
    tmp_path: Path, monkeypatch, channel: str, handler_name: str, body: dict, cred_line: str
) -> None:
    """The config write and the .env write are one transaction.

    The commit is parked on the sidecar lock (held from another thread) when the
    request task is cancelled. The write lands regardless -- a thread cannot be
    cancelled -- so a handler that unwound at that point would leave the NEW
    metadata on disk paired with the OLD credential. The saver runs to
    completion instead: the credential lands, and only then does the caller see
    ``CancelledError``.
    """
    import kiro_crew.dashboard.handlers.messaging as mod

    cfg = _seed(monkeypatch, tmp_path, {channel: {"enabled": False}})
    env = tmp_path / ".env"
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    monkeypatch.delenv("WECOM_SECRET", raising=False)
    monkeypatch.delenv("WECOM_BOT_ID", raising=False)

    async def _no_network(*a: Any, **kw: Any):
        return None

    monkeypatch.setattr(mod, "_validate_teams_app_credentials", _no_network)
    monkeypatch.setattr(mod, "_validate_webex_token", lambda tok: None)

    holding = threading.Event()
    release = threading.Event()

    async def _run() -> bool:
        req = MagicMock(spec=web.Request)
        req.method = "PUT"
        req.json = AsyncMock(return_value=body)
        req.get = lambda key, default=None: default
        req.app = {"state": SimpleNamespace()}
        worker = threading.Thread(target=_hold_sidecar, args=(cfg, holding, release), daemon=True)
        worker.start()
        try:
            assert holding.wait(timeout=10)
            task = asyncio.ensure_future(getattr(mod, handler_name)(req))
            # Let the save validate and park on the held sidecar lock.
            for _ in range(50):
                await asyncio.sleep(0.02)
                if json.loads(cfg.read_text(encoding="utf-8"))[channel]["enabled"]:
                    break
            assert not task.done(), "the save did not wait for the sidecar lock"
            task.cancel()
            await asyncio.sleep(0.1)
            release.set()
            try:
                await task
            except asyncio.CancelledError:
                cancelled = True
            else:
                cancelled = False
        finally:
            # The lock holder must not outlive the test on any exit path: a
            # failed assertion above would otherwise leave the sidecar held.
            release.set()
            worker.join(timeout=10)
            assert not worker.is_alive(), "the lock-holder thread did not terminate"
        return cancelled

    cancelled = asyncio.run(_run())
    assert cancelled, "the cancellation must still reach the caller once the save is done"
    assert json.loads(cfg.read_text(encoding="utf-8"))[channel]["enabled"] is True
    assert cred_line in env.read_text(encoding="utf-8"), env.read_text(encoding="utf-8")


# ── Rollback after a failed .env write undoes only our keys ────────────────


@pytest.mark.parametrize(
    ("channel", "handler_name", "body", "our_key"),
    [
        (
            "teams",
            "api_teams_config_save",
            {"enabled": True, "app_password": "new-pass", "app_id": "a" * 36},
            "enabled",
        ),
        (
            "webex",
            "api_webex_config_save",
            {"enabled": True, "bot_token": "x" * 40},
            "enabled",
        ),
        (
            "wecom",
            "api_wecom_config_save",
            {"enabled": True, "bot_id": "wxb-1234567890abcdef", "bot_token": "s" * 40},
            "enabled",
        ),
    ],
)
def test_rollback_keeps_a_concurrent_edit_it_does_not_own(
    tmp_path: Path, monkeypatch, channel: str, handler_name: str, body: dict, our_key: str
) -> None:
    """Mirrors the Feishu saver's contract for the three savers that roll back.

    Sequence: our save writes ``enabled``; another process then sets a sibling
    key; our .env write fails. The rollback compares per key, so ``enabled``
    reverts and the sibling keeps THEIR value -- a whole-file or whole-section
    restore would discard it.
    """
    import kiro_crew.dashboard.handlers.messaging as mod

    cfg = _seed(monkeypatch, tmp_path, {channel: {"enabled": False, "sibling": "before"}})
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    monkeypatch.delenv("WECOM_SECRET", raising=False)
    monkeypatch.delenv("WECOM_BOT_ID", raising=False)

    async def _no_network(*a: Any, **kw: Any):
        return None

    monkeypatch.setattr(mod, "_validate_teams_app_credentials", _no_network)
    monkeypatch.setattr(mod, "_validate_webex_token", lambda tok: None)

    real_update = loader.update_config_locked
    gate = threading.Lock()
    injected: list[bool] = []

    def _foreign(data: dict) -> dict:
        data[channel]["sibling"] = "set-by-someone-else"
        return data

    def _after_our_write(*a: Any, **kw: Any):
        result = real_update(*a, **kw)
        section = result.get(channel) if isinstance(result, dict) else None
        with gate:
            fire = (
                not injected
                and (a[0] if a else kw.get("path")) == cfg
                and isinstance(section, dict)
                and section.get(our_key) is True
            )
            if fire:
                injected.append(True)
        if fire:
            real_update(cfg, mutate=_foreign)
        return result

    async def _boom(updates):
        raise OSError("read-only file system")

    monkeypatch.setattr(loader, "update_config_locked", _after_our_write)
    monkeypatch.setattr(mod, "_write_env_off_loop", _boom)

    status = _messaging_put(mod, f"/api/{channel}/config", getattr(mod, handler_name), body)

    assert status >= 500, status
    assert injected, "the foreign concurrent edit was never injected"
    out = json.loads(cfg.read_text(encoding="utf-8"))[channel]
    assert out[our_key] is False  # ours: reverted
    assert out["sibling"] == "set-by-someone-else"  # theirs: untouched
