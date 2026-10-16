"""The STT config the streaming socket reads.

The ``streaming`` default and its opt-out, the STT config PUT handler's round trip, and
the language list that drives the Chat Settings STT picker. The socket itself is tested
in ``test_stt_stream.py`` and its ``_defensive_guards`` and ``_local_session`` siblings.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import KiroCrewConfig, SttConfig


class TestConfig:
    def test_streaming_defaults_true(self):
        """Streaming is on out of the box, on every provider.

        Recognition is local and free by default, so there is no per-word cost to
        opt into, and words appearing while the user is still speaking is what makes
        dictation feel like dictation. Turning it off is the opt-in now: it buys less
        CPU on the on-device providers and fewer API calls on ``transcribe``.
        """
        assert SttConfig().streaming is True

    def test_streaming_respects_an_explicit_opt_out(self):
        """The field is honoured, not just defaulted.

        Pinned at ``False``, the value that DIFFERS from the default: asserting the
        default's own value here would hold even if the field stopped being read.
        """
        assert SttConfig(streaming=False).streaming is False


class TestConfigPutRoundTrip:
    """Verify the STT config PUT handler persists the streaming field."""

    @pytest.mark.asyncio
    async def test_put_persists_streaming(self, tmp_path, monkeypatch):
        """A saved ``streaming`` choice reaches disk and survives a fresh load.

        Written as an opt-OUT, the value that differs from the default: PUTting
        ``True`` would leave every assertion here true even if the handler dropped
        the field entirely, since a fresh load would answer ``True`` on its own.
        """
        # KIROCREW_HOME redirects both config_dir() and config_path() in a
        # way that survives the `from ... import config_path` idiom used by
        # the handler, unlike monkeypatching a module-level name.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.dashboard import handlers

        app = web.Application()
        # The config endpoint resolves owner identity from `app["state"]`.
        app["state"] = SimpleNamespace(owner_id="owner-1")
        app.router.add_get("/api/config/stt", handlers.api_stt_config)
        app.router.add_put("/api/config/stt", handlers.api_stt_config)

        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/config/stt", json={"streaming": False, "provider": "transcribe"}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["streaming"] is False
            assert data["provider"] == "transcribe"
            # Assert it persisted to disk (survives a fresh load).
            cfg_file = tmp_path / "config.json"
            assert cfg_file.exists()
            import json as _json

            on_disk = _json.loads(cfg_file.read_text(encoding="utf-8"))
            assert on_disk["stt"]["streaming"] is False
            # Assert KiroCrewConfig.load() correctly deserializes — guards
            # against field-name mismatches that would silently break at runtime.
            reloaded = KiroCrewConfig.load()
            assert reloaded.stt.streaming is False

    @pytest.mark.asyncio
    async def test_put_rejects_non_boolean_streaming(self, tmp_path, monkeypatch):
        """Non-boolean ``streaming`` values must be ignored, not coerced.

        ``bool()`` of a JSON string is the trap: ``bool("false")`` is ``True`` and
        ``bool("")`` is ``False``, so a coercing handler moves the setting on input
        that expressed no choice. With streaming on by default the damage runs both
        ways (a falsy non-bool would silently turn dictation's live text off, and a
        truthy one would turn it back on under a user who opted out), so both
        directions are pinned here. ``isinstance(body["streaming"], bool)`` is what
        keeps the stored value where the user left it.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.dashboard import handlers

        app = web.Application()
        # The config endpoint resolves owner identity from `app["state"]`.
        app["state"] = SimpleNamespace(owner_id="owner-1")
        app.router.add_put("/api/config/stt", handlers.api_stt_config)

        async with TestClient(TestServer(app)) as client:
            # Falsy non-bools, against the default-on setting: none may switch it off.
            for value in ("false", "", 0):
                resp = await client.put(
                    "/api/config/stt",
                    json={"streaming": value, "provider": "transcribe"},
                )
                assert resp.status == 200
                assert (await resp.json())["streaming"] is True, value

            # A real opt-out, so the truthy non-bools below have something to undo.
            resp = await client.put(
                "/api/config/stt",
                json={"streaming": False, "provider": "transcribe"},
            )
            assert resp.status == 200
            assert (await resp.json())["streaming"] is False

            # Truthy non-bools must not switch it back on.
            for value in ("true", 1, "yes"):
                resp = await client.put(
                    "/api/config/stt",
                    json={"streaming": value, "provider": "transcribe"},
                )
                assert resp.status == 200
                assert (await resp.json())["streaming"] is False, value

    @pytest.mark.asyncio
    async def test_get_exposes_transcribe_fields_for_ui(self, tmp_path, monkeypatch):
        """GET response must include transcribe_region, transcribe_profile,
        language_code, and language_codes so the Chat Settings STT section
        can render the current values and a language dropdown.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.dashboard import handlers

        app = web.Application()
        # The config endpoint resolves owner identity from `app["state"]`.
        app["state"] = SimpleNamespace(owner_id="owner-1")
        app.router.add_get("/api/config/stt", handlers.api_stt_config)
        app.router.add_put("/api/config/stt", handlers.api_stt_config)

        async with TestClient(TestServer(app)) as client:
            # PUT all three transcribe-specific fields and the provider.
            resp = await client.put(
                "/api/config/stt",
                json={
                    "provider": "transcribe",
                    "transcribe_region": "eu-west-1",
                    "transcribe_profile": "dev-account",
                    "language_code": "fr-FR",
                },
            )
            assert resp.status == 200

            # GET must reflect the persisted values and expose the
            # language-code list the UI picker uses.
            resp = await client.get("/api/config/stt")
            assert resp.status == 200
            data = await resp.json()
            assert data["provider"] == "transcribe"
            assert data["transcribe_region"] == "eu-west-1"
            assert data["transcribe_profile"] == "dev-account"
            assert data["language_code"] == "fr-FR"
            assert isinstance(data["language_codes"], list)
            assert "en-US" in data["language_codes"]
            assert "fr-FR" in data["language_codes"]


class TestSttLanguageCodes:
    """The language list that drives the Chat Settings STT picker.

    The picker is the only supported way to choose a recognition language, so a
    language missing from this list is unreachable without hand-editing
    config.json — even when the provider (AWS Transcribe) supports it.
    """

    def test_korean_is_offered(self):
        """Korean must be selectable in the picker.

        Regression: `ko-KR` was absent while ja-JP and zh-CN were present, so
        Korean speakers could not choose their language from the dashboard.
        """
        from kiro_crew.dashboard.handlers.core import _STT_LANGUAGE_CODES

        assert "ko-KR" in _STT_LANGUAGE_CODES

    def test_codes_are_unique_and_well_formed(self):
        """Every entry is a distinct `ll-CC` BCP-47 tag.

        A duplicate renders twice in the dropdown, and a malformed tag is
        rejected by Transcribe at stream-start rather than at selection time.
        """
        from kiro_crew.dashboard.handlers.core import _STT_LANGUAGE_CODES

        assert len(set(_STT_LANGUAGE_CODES)) == len(_STT_LANGUAGE_CODES)
        for code in _STT_LANGUAGE_CODES:
            language, _, region = code.partition("-")
            assert language.isalpha() and language.islower() and len(language) == 2, code
            assert region.isalpha() and region.isupper() and len(region) == 2, code

    @pytest.mark.asyncio
    async def test_korean_round_trips_through_the_config_api(self, tmp_path, monkeypatch):
        """Selecting Korean persists and is served back to the UI."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.dashboard import handlers

        app = web.Application()
        # The config endpoint resolves owner identity from `app["state"]`.
        app["state"] = SimpleNamespace(owner_id="owner-1")
        app.router.add_get("/api/config/stt", handlers.api_stt_config)
        app.router.add_put("/api/config/stt", handlers.api_stt_config)

        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/config/stt",
                json={"provider": "transcribe", "language_code": "ko-KR"},
            )
            assert resp.status == 200

            resp = await client.get("/api/config/stt")
            assert resp.status == 200
            data = await resp.json()
            assert data["language_code"] == "ko-KR"
            assert "ko-KR" in data["language_codes"]
