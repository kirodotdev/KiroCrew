"""A Bot's conversation starters: the prompts its empty chat offers as buttons.

A starter is the owner's own text and a press sends it, so the rule is *clean it as a
display name, bound it, and drop what does not pass with a warning*. These tests pin
that rule where a value enters: :func:`clean_starters` itself, the hand-editable
``config.json``, ``PUT /api/agents/{name}`` and the ``GET /api/members`` row.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.paths import config_dir
from kiro_crew.member_starters import STARTER_MAX_CHARS, STARTER_MAX_COUNT, clean_starters
from kiro_crew.members import clean_display_name
from kiro_crew.testing.synthetic_credentials import github_classic

#: A secret the redactor removes (an AWS key ID is an identifier it keeps visible),
#: built at run time so no source line carries a credential.
SECRET = github_classic()


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


class TestCleanStarters:
    def test_absent_is_no_starters_and_no_warning(self):
        assert clean_starters(None) == ([], [], [])
        assert clean_starters([]) == ([], [], [])

    @pytest.mark.parametrize("raw", ["  What  is\nnew\ttoday?  ", "Plan\u200b my\x00 week\u202e"])
    def test_each_is_cleaned_exactly_as_a_display_name_is(self, raw):
        assert clean_starters(["Summarize my inbox", raw]) == (
            ["Summarize my inbox", clean_display_name(raw)],
            [],
            [],
        )

    def test_each_value_left_out_is_named_by_its_place_and_reason_and_never_quoted(self):
        raw = ["ok", 7, "  ", "x" * (STARTER_MAX_CHARS + 1), f"use key {SECRET}", " ok "]
        kept, warnings, left_out = clean_starters(raw + [f"p{i}" for i in range(STARTER_MAX_COUNT)])
        assert kept == ["ok", "p0", "p1", "p2"]
        # Each by its place and a reason code the editor words itself.
        assert left_out == [
            (2, "not_text"),
            (3, "empty"),
            (4, "too_long"),
            (5, "credential"),
            (6, "repeated"),
            (10, "over_limit"),
        ]
        assert warnings == [
            "starter 2 is not text and was left out",
            "starter 3 is empty and was left out",
            f"starter 4 is longer than {STARTER_MAX_CHARS} characters and was left out",
            "starter 5 looks like a credential and was left out",
            "starter 6 repeats an earlier starter and was left out",
            f"starter 10 is past the first {STARTER_MAX_COUNT} and was left out",
        ]
        assert clean_starters(["x" * STARTER_MAX_CHARS]) == (["x" * STARTER_MAX_CHARS], [], [])

    @pytest.mark.parametrize("raw", ["a string", 5, {"a": 1}, ("a", "b")])
    def test_a_value_that_is_not_a_list_is_left_out_whole(self, raw):
        assert clean_starters(raw) == ([], ["starters must be a list of text; none were kept"], [])

    def test_without_the_credential_check_it_never_asks_the_redactor(self):
        """The config load runs before boot, where the platform redactor refuses."""
        with patch(
            "kiro_crew.member_starters.external_text_requires_redaction",
            side_effect=AssertionError("the redactor was asked"),
        ):
            assert clean_starters(["Hi", "Hi", ""], check_credentials=False) == (
                ["Hi"],
                [
                    "starter 2 repeats an earlier starter and was left out",
                    "starter 3 is empty and was left out",
                ],
                [(2, "repeated"), (3, "empty")],
            )


def _hand_edit(name: str, value: object) -> None:
    cfg = KiroCrewConfig.load()
    cfg.agents[name] = KiroCrewAgentConfig(kiro_agent="kirocrew", starters=["kept?"])
    cfg.save()
    path = config_dir() / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["agents"][name]["starters"] = value
    path.write_text(json.dumps(data), encoding="utf-8")


class TestConfigField:
    def test_round_trips_and_defaults_to_none(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["guide"] = KiroCrewAgentConfig(kiro_agent="kirocrew", starters=["Hi", "Help"])
        cfg.agents["plain"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
        again = KiroCrewConfig.load()
        assert again.agents["guide"].starters == ["Hi", "Help"]
        assert again.agents["plain"].starters == []

    def test_a_record_without_the_key_has_none(self):
        """Every Bot that exists today has no key; none of them gains starters."""
        _hand_edit("old", None)
        path = config_dir() / "config.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["agents"]["old"].pop("starters")
        path.write_text(json.dumps(data), encoding="utf-8")
        assert KiroCrewConfig.load().agents["old"].starters == []

    def test_a_hand_edited_value_is_cleaned_and_named_on_each_disk_read(self, caplog):
        bad = ["fine", 7, "x" * (STARTER_MAX_CHARS + 1), "fine"]
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            _hand_edit("weird", bad)
            caplog.clear()
            first = KiroCrewConfig.load().agents["weird"].starters
            cached = KiroCrewConfig.load().agents["weird"].starters
            warned_once = [r for r in caplog.records if "left out" in r.getMessage()]
            _hand_edit("weird", bad + ["new"])
            caplog.clear()
            KiroCrewConfig.load()
            warned_again = [r for r in caplog.records if "left out" in r.getMessage()]
        assert first == cached == ["fine"]
        assert len(warned_once) == 3
        assert len(warned_again) == 3

    def test_the_load_needs_no_platform_context_for_a_saved_starter(self):
        """An enterprise boot loads the config before it composes the platform.

        With no context installed and a non-standalone profile, ``current_context``
        refuses with ``PlatformCompositionError``; a saved starter must not reach it.
        """
        from kiro_crew.platform.context import PlatformCompositionError

        _hand_edit("guide", ["Plan my week", f"use key {SECRET}"])

        def _unbooted():
            raise PlatformCompositionError("no installed context")

        with patch("kiro_crew.platform.context.current_context", side_effect=_unbooted):
            loaded = KiroCrewConfig.load().agents["guide"].starters
        assert loaded == ["Plan my week", f"use key {SECRET}"]

    @pytest.mark.parametrize("junk", ["one prompt", 4, {"a": "b"}, None])
    def test_a_hand_edited_non_list_reads_as_none_and_never_fails_the_load(self, junk):
        _hand_edit("weird", junk)
        assert KiroCrewConfig.load().agents["weird"].starters == []


def _crud_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

    app = web.Application()
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    return app


@pytest.fixture()
def seeded_agent():
    cfg = KiroCrewConfig.load()
    cfg.agents["existing"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", workspace="default", memory_store="default"
    )
    cfg.save()
    return "existing"


async def _put(client, name: str, body: dict) -> dict:
    resp = await client.put(f"/api/agents/{name}", json=body)
    assert resp.status == 200
    return await resp.json()


class TestUpdateEndpoint:
    @pytest.mark.asyncio
    async def test_an_update_without_starters_answers_exactly_as_before(self, seeded_agent):
        """Passes on main: the answer for a save that never names starters is unchanged."""
        async with TestClient(TestServer(_crud_app())) as client:
            result = await _put(client, seeded_agent, {"starred": True})
        assert result == {"ok": True, "name": seeded_agent, "memory_store": "default"}

    @pytest.mark.asyncio
    async def test_a_save_stores_what_passes_and_names_what_does_not(self, seeded_agent):
        body = {"starters": ["Summarize my inbox", "x" * (STARTER_MAX_CHARS + 1), 3, "Plan"]}
        async with TestClient(TestServer(_crud_app())) as client:
            result = await _put(client, seeded_agent, body)
        assert KiroCrewConfig.load().agents[seeded_agent].starters == ["Summarize my inbox", "Plan"]
        assert result["ok"] is True
        assert len(result["warnings"]) == 2
        # What was kept and where each value left out sat, for the editor to show.
        assert result["starters"] == ["Summarize my inbox", "Plan"]
        assert result["starters_left_out"] == [
            {"place": 2, "reason": "too_long"},
            {"place": 3, "reason": "not_text"},
        ]

    @pytest.mark.asyncio
    async def test_an_empty_list_clears_them_and_an_unrelated_save_keeps_them(self, seeded_agent):
        async with TestClient(TestServer(_crud_app())) as client:
            await _put(client, seeded_agent, {"starters": ["Keep me"]})
            assert "warnings" not in await _put(client, seeded_agent, {"starred": True})
            assert KiroCrewConfig.load().agents[seeded_agent].starters == ["Keep me"]
            assert "warnings" not in await _put(client, seeded_agent, {"starters": []})
        assert KiroCrewConfig.load().agents[seeded_agent].starters == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("junk", ["one prompt", 4, {"a": "b"}, None])
    async def test_a_value_that_is_not_a_list_keeps_the_saved_starters(self, seeded_agent, junk):
        """Main's outcome is kept: no refusal, the other fields still save, the old starters stay."""
        async with TestClient(TestServer(_crud_app())) as client:
            await _put(client, seeded_agent, {"starters": ["Keep me"]})
            result = await _put(client, seeded_agent, {"description": "new", "starters": junk})
        assert any("saved starters were kept" in w for w in result["warnings"])
        stored = KiroCrewConfig.load().agents[seeded_agent]
        assert (stored.starters, stored.description) == (["Keep me"], "new")


def _members_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers.members import api_members

    @web.middleware
    async def _auth(request: web.Request, handler):
        request.setdefault("app", "")
        request.setdefault("user", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/members", api_members)
    return app


@pytest.mark.asyncio
async def test_roster_rows_carry_the_starters_minus_anything_it_would_mask(tmp_path):
    guide = KiroCrewAgentConfig(kiro_agent="guide", starters=["Hello there"])
    # Stored by code, not the loader, so the roster's own guard is what leaves it out.
    guide.starters.append(f"use key {SECRET}")
    fake = SimpleNamespace(
        agents={"guide": guide, "plain": KiroCrewAgentConfig(kiro_agent="plain")},
        default_agent="plain",
        memory_stores={},
        degraded_sections=frozenset(),
    )
    with patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=fake):
        async with TestClient(TestServer(_members_app(_make_state(tmp_path)))) as client:
            resp = await client.get("/api/members")
            assert resp.status == 200
            text = await resp.text()
    rows = {r["name"]: r for r in json.loads(text)["members"]}
    assert rows["guide"]["starters"] == ["Hello there"]
    assert rows["plain"]["starters"] == []
    assert SECRET not in text
