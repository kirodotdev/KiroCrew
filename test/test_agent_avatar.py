"""Tests for the per-crew avatar override on KiroCrewAgentConfig.

Covers:
- _safe_avatar validation (shape guards, trait coercion, tile hex pinning)
- The field's defaults and asdict serialization
- Round-trip through the agents-section from-dict parse
"""

import dataclasses
import json
import tempfile
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
)
from kiro_crew.config.sections import _safe_avatar


def _load_from_dict(data: dict) -> KiroCrewConfig:
    """Write *data* to a temp config file and load via KiroCrewConfig.load()."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        tmp = Path(f.name)
    try:
        with unittest.mock.patch(
            "kiro_crew.config.loader.config_path",
            return_value=tmp,
        ):
            return KiroCrewConfig.load()
    finally:
        tmp.unlink(missing_ok=True)


_GHOST = {
    "kind": "ghost",
    "traits": {
        "eyes": "wink",
        "brows": "none",
        "mouth": "smile",
        "accessory": "halo",
        "prop": "none",
        "blush": True,
        "flip": False,
        "tile": "#21a5de",
    },
}


class TestSafeAvatar:
    """Unit tests for the _safe_avatar coercer."""

    def test_valid_ghost_round_trips(self):
        assert _safe_avatar(_GHOST) == _GHOST

    def test_non_dict_collapses(self):
        assert _safe_avatar("ghost") == {}
        assert _safe_avatar(None) == {}
        assert _safe_avatar(["ghost"]) == {}
        assert _safe_avatar(42) == {}

    def test_unknown_kind_collapses(self):
        assert _safe_avatar({"kind": "hologram", "traits": {}}) == {}

    def test_image_kind_accepted(self):
        """The upload tier's marker: kind=image, unknown keys dropped."""
        assert _safe_avatar({"kind": "image", "file": "x.png"}) == {"kind": "image"}

    def test_image_kind_keeps_cache_stamp(self):
        assert _safe_avatar({"kind": "image", "v": 1700000000}) == {
            "kind": "image",
            "v": 1700000000,
        }

    def test_image_cache_stamp_rejects_non_int(self):
        """bool is an int subclass; junk stamps drop rather than store."""
        assert _safe_avatar({"kind": "image", "v": True}) == {"kind": "image"}
        assert _safe_avatar({"kind": "image", "v": "123"}) == {"kind": "image"}
        assert _safe_avatar({"kind": "image", "v": -5}) == {"kind": "image"}

    def test_image_file_pin_kept_only_when_valid(self):
        """The file pin is a <digest16>.<ext> suffix; junk drops."""
        pin = "0" * 16 + ".webp"
        assert _safe_avatar({"kind": "image", "file": pin}) == {"kind": "image", "file": pin}
        assert _safe_avatar({"kind": "image", "file": "x.png"}) == {"kind": "image"}
        assert _safe_avatar({"kind": "image", "file": "0" * 16 + ".exe"}) == {"kind": "image"}
        assert _safe_avatar({"kind": "image", "file": 3}) == {"kind": "image"}

    def test_missing_traits_collapses(self):
        assert _safe_avatar({"kind": "ghost"}) == {}

    def test_non_dict_traits_collapses(self):
        assert _safe_avatar({"kind": "ghost", "traits": "canon"}) == {}

    def test_all_empty_traits_collapse_to_reset(self):
        """An all-absent trait set is the reset spelling, not a third state.

        The builder cannot produce it (Apply always carries the seeded
        defaults), so it only arrives hand-written — storing it would render
        a featureless ghost distinct from both the name-derived face and any
        pinned one.
        """
        assert _safe_avatar({"kind": "ghost", "traits": {}}) == {}

    def test_non_string_trait_collapses_to_empty(self):
        out = _safe_avatar({"kind": "ghost", "traits": {"eyes": 7, "mouth": "smile"}})
        assert out["traits"]["eyes"] == ""
        assert out["traits"]["mouth"] == "smile"

    def test_unknown_trait_keys_dropped(self):
        out = _safe_avatar({"kind": "ghost", "traits": {"hat": "tall", "eyes": "canon"}})
        assert "hat" not in out["traits"]
        assert out["traits"]["eyes"] == "canon"

    def test_overlong_trait_value_truncated(self):
        out = _safe_avatar({"kind": "ghost", "traits": {"eyes": "x" * 500}})
        assert len(out["traits"]["eyes"]) == 32

    def test_bools_require_real_booleans(self):
        """bool("false") is True, so string-typed values must NOT coerce on."""
        out = _safe_avatar(
            {"kind": "ghost", "traits": {"blush": 1, "flip": "true", "eyes": "canon"}}
        )
        assert out["traits"]["blush"] is False
        assert out["traits"]["flip"] is False
        on = _safe_avatar({"kind": "ghost", "traits": {"blush": True}})
        assert on["traits"]["blush"] is True

    def test_tile_pinned_to_hex(self):
        """tile is interpolated into SVG, so junk must not survive."""
        bad = dict(_GHOST, traits=dict(_GHOST["traits"], tile='"><script>'))
        assert _safe_avatar(bad)["traits"]["tile"] == ""

    def test_tile_normalized_lowercase(self):
        raw = dict(_GHOST, traits=dict(_GHOST["traits"], tile="#21A5DE"))
        assert _safe_avatar(raw)["traits"]["tile"] == "#21a5de"


class TestKiroCrewAgentConfigAvatar:
    """avatar field on KiroCrewAgentConfig."""

    def test_default_empty(self):
        assert KiroCrewAgentConfig().avatar == {}

    def test_default_is_not_shared_between_instances(self):
        a, b = KiroCrewAgentConfig(), KiroCrewAgentConfig()
        a.avatar["kind"] = "ghost"
        assert b.avatar == {}

    def test_serializes_in_asdict(self):
        d = dataclasses.asdict(KiroCrewAgentConfig(avatar=_GHOST))
        assert d["avatar"] == _GHOST

    def test_empty_serializes(self):
        d = dataclasses.asdict(KiroCrewAgentConfig())
        assert d["avatar"] == {}


class TestAvatarLoadRoundTrip:
    """The agents-section parse keeps a stored avatar and drops junk."""

    def test_round_trips_through_to_dict(self):
        cfg = KiroCrewConfig()
        cfg.agents["radar"] = KiroCrewAgentConfig(avatar=_GHOST)
        assert cfg.to_dict()["agents"]["radar"]["avatar"] == _GHOST

    def test_loads_from_agents_section(self):
        cfg = _load_from_dict({"agents": {"radar": {"kiro_agent": "kirocrew", "avatar": _GHOST}}})
        assert cfg.agents["radar"].avatar == _GHOST

    def test_junk_avatar_collapses_on_load(self):
        cfg = _load_from_dict({"agents": {"radar": {"kiro_agent": "kirocrew", "avatar": "ghost"}}})
        assert cfg.agents["radar"].avatar == {}


class TestAvatarEndpoints:
    """Create/update refuse junk with a code; valid overrides persist."""

    @staticmethod
    def _app():
        from aiohttp import web

        from kiro_crew.dashboard.handlers import (
            api_kirocrew_agent_update,
            api_kirocrew_agents_create,
        )

        app = web.Application()
        app.router.add_post("/api/agents", api_kirocrew_agents_create)
        app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
        return app

    @pytest.fixture(autouse=True)
    def _owner_caller(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )

    @pytest.fixture()
    def seeded_agent(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["existing"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
        return "existing"

    @pytest.mark.asyncio
    async def test_update_persists_a_valid_override(self, seeded_agent):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            resp = await client.put(f"/api/agents/{seeded_agent}", json={"avatar": _GHOST})
            assert resp.status == 200
        assert KiroCrewConfig.load().agents[seeded_agent].avatar == _GHOST

    @pytest.mark.asyncio
    async def test_update_refuses_junk_with_a_code(self, seeded_agent):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            resp = await client.put(f"/api/agents/{seeded_agent}", json={"avatar": "ghost"})
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_avatar"
        assert KiroCrewConfig.load().agents[seeded_agent].avatar == {}

    @pytest.mark.asyncio
    async def test_update_empty_resets(self, seeded_agent):
        from aiohttp.test_utils import TestClient, TestServer

        cfg = KiroCrewConfig.load()
        cfg.agents[seeded_agent].avatar = dict(_GHOST)
        cfg.save()
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.put(f"/api/agents/{seeded_agent}", json={"avatar": {}})
            assert resp.status == 200
        assert KiroCrewConfig.load().agents[seeded_agent].avatar == {}

    @pytest.mark.asyncio
    async def test_create_accepts_an_override(self):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            resp = await client.post(
                "/api/agents",
                json={"name": "radar2", "kiro_agent": "kirocrew", "avatar": _GHOST},
            )
            assert resp.status == 200
        assert KiroCrewConfig.load().agents["radar2"].avatar == _GHOST

    @pytest.mark.asyncio
    async def test_create_refuses_junk_with_a_code(self):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            resp = await client.post(
                "/api/agents",
                json={"name": "radar3", "kiro_agent": "kirocrew", "avatar": ["x"]},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_avatar"
        assert "radar3" not in KiroCrewConfig.load().agents


# Minimal structurally complete images. The endpoint sniffs the magic bytes and
# then checks the container is CLOSED (PNG IEND chunk, JPEG EOI marker, RIFF
# length) — it never decodes pixels — so a header, filler, and the terminator
# is a complete test image.
_PNG_IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64 + _PNG_IEND
_JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
_WEBP_BODY = b"WEBP" + b"\x00" * 64
_WEBP = b"RIFF" + len(_WEBP_BODY).to_bytes(4, "little") + _WEBP_BODY


class TestImageBodyComplete:
    """Structural completeness catches what magic-byte sniffing cannot."""

    def test_complete_fixtures_pass(self):
        from kiro_crew.dashboard.handlers.agents import _image_body_complete

        assert _image_body_complete("png", _PNG)
        assert _image_body_complete("jpg", _JPG)
        assert _image_body_complete("webp", _WEBP)

    @pytest.mark.parametrize(
        "ext, body",
        [
            ("png", _PNG[:-1]),
            ("png", _PNG[: -len(_PNG_IEND)]),
            ("jpg", _JPG[:-1]),
            ("jpg", _JPG[:-2]),
            ("webp", _WEBP[:-1]),
            ("webp", _WEBP[:12]),
            ("webp", b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 64),
        ],
    )
    def test_truncated_bodies_fail(self, ext, body):
        from kiro_crew.dashboard.handlers.agents import _image_body_complete

        assert not _image_body_complete(ext, body)

    def test_webp_odd_payload_pad_byte_is_legal(self):
        from kiro_crew.dashboard.handlers.agents import _image_body_complete

        payload = b"WEBP" + b"\x00" * 63  # odd length -> one RIFF pad byte
        body = b"RIFF" + len(payload).to_bytes(4, "little") + payload + b"\x00"
        assert _image_body_complete("webp", body)

    def test_unknown_ext_fails(self):
        from kiro_crew.dashboard.handlers.agents import _image_body_complete

        assert not _image_body_complete("gif", b"GIF89a")


class TestUploadedAvatarEndpoints:
    """The image tier: upload stages a sniffed file, saving the field commits
    it, GET serves it, and clearing the field or deleting the crew cleans it
    up. The config field moves only through the ordinary update path."""

    @pytest.mark.asyncio
    async def test_upload_rejects_truncated_image(self, seeded_agent):
        """Valid magic bytes on a body cut off mid-stream must not stage.

        Otherwise the commit would reap the crew's saved picture and serve a
        file no browser can decode in its place.
        """
        from aiohttp import FormData
        from aiohttp.test_utils import TestClient, TestServer

        form = FormData()
        form.add_field("file", _PNG[:-3], filename="x.png", content_type="image/png")
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.post(f"/api/agents/{seeded_agent}/avatar", data=form)
            assert resp.status == 400
            assert (await resp.json())["code"] == "avatar_bad_format"
        assert self._stored(seeded_agent) is None
        from kiro_crew.dashboard.handlers.agents import _pending_avatar_path

        assert _pending_avatar_path(seeded_agent) is None

    @staticmethod
    def _app():
        from aiohttp import web

        from kiro_crew.dashboard.handlers import (
            api_kirocrew_agent_avatar_get,
            api_kirocrew_agent_avatar_upload,
            api_kirocrew_agent_delete,
            api_kirocrew_agent_update,
        )

        app = web.Application()
        app.router.add_get("/api/agents/{name}/avatar", api_kirocrew_agent_avatar_get)
        app.router.add_post("/api/agents/{name}/avatar", api_kirocrew_agent_avatar_upload)
        app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
        app.router.add_delete("/api/agents/{name}", api_kirocrew_agent_delete)
        return app

    @pytest.fixture(autouse=True)
    def _owner_caller(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )

    @pytest.fixture()
    def seeded_agent(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["existing"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        # A second agent so crew-delete has a surviving default.
        cfg.agents["other"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.default_agent = "other"
        cfg.save()
        return "existing"

    @staticmethod
    def _form(data: bytes):
        from aiohttp import FormData

        form = FormData()
        form.add_field("file", data, filename="face.bin", content_type="application/octet-stream")
        return form

    @staticmethod
    def _stored(name: str):
        """Any installed (non-pending) variant on disk for ``name``, or None.

        A test-side view of the file store: production resolves ONLY through
        the record's ``file`` pin (:func:`_live_avatar_file`); this helper is
        how a test asserts what physically exists regardless of the pin.
        """
        from kiro_crew.dashboard.handlers.agents import _avatar_variant_paths

        variants = _avatar_variant_paths(name)
        return variants[0] if variants else None

    @classmethod
    async def _commit(cls, client, name: str, data: bytes):
        """Stage the picture, then PUT the committing override with its token."""
        up = await client.post(f"/api/agents/{name}/avatar", data=cls._form(data))
        tok = (await up.json())["token"]
        return await client.put(
            f"/api/agents/{name}",
            json={"avatar": {"kind": "image", "promote": True, "token": tok}},
        )

    @pytest.mark.asyncio
    async def test_upload_then_commit_then_get_roundtrip(self, seeded_agent):
        """Upload stages; the PUT is the commit; GET serves the promoted file."""
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            up = await client.post(f"/api/agents/{seeded_agent}/avatar", data=self._form(_PNG))
            assert up.status == 200
            body = await up.json()
            assert body["staged"] is True and isinstance(body["token"], str)
            # Staged only: nothing to serve until the field commits it.
            assert (await client.get(f"/api/agents/{seeded_agent}/avatar")).status == 404
            put = await client.put(
                f"/api/agents/{seeded_agent}",
                json={"avatar": {"kind": "image", "promote": True, "token": body["token"]}},
            )
            assert put.status == 200
            stored = KiroCrewConfig.load().agents[seeded_agent].avatar
            assert stored["kind"] == "image" and isinstance(stored["v"], int)
            got = await client.get(f"/api/agents/{seeded_agent}/avatar")
            assert got.status == 200
            assert got.headers["Content-Type"] == "image/png"
            assert await got.read() == _PNG
            etag = got.headers["ETag"]
            again = await client.get(
                f"/api/agents/{seeded_agent}/avatar", headers={"If-None-Match": etag}
            )
            assert again.status == 304

    @pytest.mark.asyncio
    async def test_failed_or_abandoned_save_keeps_the_live_picture(self, seeded_agent):
        """A staged upload never touches what the roster serves."""
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            await self._commit(client, seeded_agent, _PNG)
            # Second upload staged but its Save never happens (abandoned).
            await client.post(f"/api/agents/{seeded_agent}/avatar", data=self._form(_JPG))
            got = await client.get(f"/api/agents/{seeded_agent}/avatar")
            assert got.headers["Content-Type"] == "image/png"
            assert await got.read() == _PNG

    @pytest.mark.asyncio
    async def test_commit_without_upload_400(self, seeded_agent):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded_agent}", json={"avatar": {"kind": "image", "promote": True}}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "avatar_file_missing"
        assert KiroCrewConfig.load().agents[seeded_agent].avatar == {}

    @pytest.mark.asyncio
    async def test_leaving_image_kind_removes_the_file(self, seeded_agent):
        """PUTting a ghost/reset override over a stored picture cleans up."""
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            await self._commit(client, seeded_agent, _PNG)
            assert self._stored(seeded_agent) is not None
            resp = await client.put(f"/api/agents/{seeded_agent}", json={"avatar": {}})
            assert resp.status == 200
        assert self._stored(seeded_agent) is None

    @pytest.mark.asyncio
    async def test_upload_sniffs_magic_not_content_type(self, seeded_agent):
        """A .png filename and image/png header lie; the bytes decide."""
        from aiohttp import FormData
        from aiohttp.test_utils import TestClient, TestServer

        form = FormData()
        form.add_field("file", b"GIF89a" + b"\x00" * 32, filename="x.png", content_type="image/png")
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.post(f"/api/agents/{seeded_agent}/avatar", data=form)
            assert resp.status == 400
            assert (await resp.json())["code"] == "avatar_bad_format"
        assert self._stored(seeded_agent) is None

    @pytest.mark.asyncio
    async def test_upload_caps_size(self, seeded_agent, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer

        monkeypatch.setattr("kiro_crew.dashboard.handlers.agents._AVATAR_MAX_BYTES", 128)
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.post(
                f"/api/agents/{seeded_agent}/avatar",
                data=self._form(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096),
            )
            assert resp.status == 413
            assert (await resp.json())["code"] == "avatar_too_large"

    @pytest.mark.asyncio
    async def test_upload_unknown_crew_404(self):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            resp = await client.post("/api/agents/ghost-crew/avatar", data=self._form(_PNG))
            assert resp.status == 404
            # The code (not the English copy) is what a frontend may branch on.
            assert (await resp.json())["code"] == "agent_not_found"

    @pytest.mark.asyncio
    async def test_malformed_multipart_is_400_not_500(self, seeded_agent):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            resp = await client.post(
                f"/api/agents/{seeded_agent}/avatar",
                data=b"not multipart at all",
                headers={"Content-Type": "multipart/form-data; boundary=xyz"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_multipart"

    @pytest.mark.asyncio
    async def test_format_change_replaces_stale_extension(self, seeded_agent):
        """png -> webp must not leave the old .png as a resolvable sibling."""
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            await self._commit(client, seeded_agent, _PNG)
            await self._commit(client, seeded_agent, _WEBP)
            got = await client.get(f"/api/agents/{seeded_agent}/avatar")
            assert got.headers["Content-Type"] == "image/webp"
        stored = self._stored(seeded_agent)
        assert stored is not None and stored.suffix == ".webp"

    @pytest.mark.asyncio
    async def test_replacement_changes_etag(self, seeded_agent):
        """Same-size replacement must still invalidate caches (content ETag)."""
        from aiohttp.test_utils import TestClient, TestServer

        other = b"\x89PNG\r\n\x1a\n" + b"\x01" * 64 + _PNG_IEND  # same length as _PNG
        async with TestClient(TestServer(self._app())) as client:
            await self._commit(client, seeded_agent, _PNG)
            first = await client.get(f"/api/agents/{seeded_agent}/avatar")
            await self._commit(client, seeded_agent, other)
            second = await client.get(
                f"/api/agents/{seeded_agent}/avatar",
                headers={"If-None-Match": first.headers["ETag"]},
            )
            assert second.status == 200
            assert second.headers["ETag"] != first.headers["ETag"]

    @pytest.mark.asyncio
    async def test_crew_delete_removes_avatar_files(self, seeded_agent):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.handlers.agents import _pending_avatar_path

        async with TestClient(TestServer(self._app())) as client:
            await self._commit(client, seeded_agent, _PNG)
            # Stage a second picture too: crew delete must reap BOTH tiers.
            await client.post(f"/api/agents/{seeded_agent}/avatar", data=self._form(_JPG))
            resp = await client.delete(f"/api/agents/{seeded_agent}")
            assert resp.status == 200
        assert self._stored(seeded_agent) is None
        assert _pending_avatar_path(seeded_agent) is None

    @pytest.mark.asyncio
    async def test_get_requires_config_to_select_the_image(self, seeded_agent):
        """A leftover file with a non-image field must not stay retrievable."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.handlers.agents import _avatar_stem, _avatars_dir

        d = _avatars_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{_avatar_stem(seeded_agent)}.png").write_bytes(_PNG)
        async with TestClient(TestServer(self._app())) as client:
            got = await client.get(f"/api/agents/{seeded_agent}/avatar")
            assert got.status == 404

    @pytest.mark.asyncio
    async def test_pinless_image_record_serves_nothing(self, seeded_agent):
        """A hand-edited ``{"kind": "image"}`` without a ``file`` pin selects no file.

        Every writer stamps the pin at the commit, so a pinless record never
        names a committed picture; falling back to "any stored variant" would
        serve an orphaned install left by a crash between the install and the
        config save. The GET 404s, and a picture-keeping save refuses rather
        than adopting the unknown file.
        """
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.handlers.agents import _avatar_stem, _avatars_dir

        d = _avatars_dir()
        d.mkdir(parents=True, exist_ok=True)
        orphan = d / f"{_avatar_stem(seeded_agent)}.{'a' * 16}.png"
        orphan.write_bytes(_PNG)
        cfg = KiroCrewConfig.load()
        cfg.agents[seeded_agent].avatar = {"kind": "image", "v": 7}
        cfg.save()
        async with TestClient(TestServer(self._app())) as client:
            got = await client.get(f"/api/agents/{seeded_agent}/avatar")
            assert got.status == 404
            resp = await client.put(
                f"/api/agents/{seeded_agent}", json={"avatar": {"kind": "image"}}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "avatar_file_missing"
        # The orphan is neither served nor adopted — and not silently deleted either.
        assert orphan.is_file()

    @pytest.mark.asyncio
    async def test_update_all_empty_ghost_is_reset_not_400(self, seeded_agent):
        """The validator's all-empty→reset collapse is not caller junk."""
        from aiohttp.test_utils import TestClient, TestServer

        cfg = KiroCrewConfig.load()
        cfg.agents[seeded_agent].avatar = dict(_GHOST)
        cfg.save()
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded_agent}",
                json={"avatar": {"kind": "ghost", "traits": {}}},
            )
            assert resp.status == 200
        assert KiroCrewConfig.load().agents[seeded_agent].avatar == {}

    @pytest.mark.asyncio
    async def test_mistyped_ghost_traits_never_delete_the_saved_picture(self, seeded_agent):
        """A wrong-TYPE trait value is caller junk, not a reset.

        The validator coerces ``{"eyes": 7}`` to absent, so without the
        type check at the 400 gate the payload would collapse to reset and
        silently delete the crew's uploaded picture. It must 400 instead,
        and the picture must remain both selected and served.
        """
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            await self._commit(client, seeded_agent, _PNG)
            resp = await client.put(
                f"/api/agents/{seeded_agent}",
                json={"avatar": {"kind": "ghost", "traits": {"eyes": 7}}},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_avatar"
            got = await client.get(f"/api/agents/{seeded_agent}/avatar")
            assert got.status == 200
            assert await got.read() == _PNG
        assert KiroCrewConfig.load().agents[seeded_agent].avatar.get("kind") == "image"

    @pytest.mark.asyncio
    async def test_unknown_trait_keys_and_junk_tile_never_reset_the_picture(self, seeded_agent):
        """An unknown axis name or junk tile color is caller junk, not a reset."""
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            await self._commit(client, seeded_agent, _PNG)
            for payload in (
                {"kind": "ghost", "traits": {"hat": "tall"}},
                {"kind": "ghost", "traits": {"tile": "not-a-color"}},
            ):
                resp = await client.put(f"/api/agents/{seeded_agent}", json={"avatar": payload})
                assert resp.status == 400, payload
                assert (await resp.json())["code"] == "invalid_avatar"
            got = await client.get(f"/api/agents/{seeded_agent}/avatar")
            assert got.status == 200
            assert await got.read() == _PNG

    @pytest.mark.asyncio
    async def test_promote_without_token_fails_instead_of_keeping_silently(self, seeded_agent):
        """`promote: true` with no token must not slide into the keep branch."""
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            await self._commit(client, seeded_agent, _PNG)
            up = await client.post(f"/api/agents/{seeded_agent}/avatar", data=self._form(_JPG))
            assert up.status == 200
            resp = await client.put(
                f"/api/agents/{seeded_agent}",
                json={"avatar": {"kind": "image", "promote": True}},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "avatar_file_missing"
            got = await client.get(f"/api/agents/{seeded_agent}/avatar")
            assert got.status == 200
            assert await got.read() == _PNG

    @pytest.mark.asyncio
    async def test_get_without_upload_404(self, seeded_agent):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            got = await client.get(f"/api/agents/{seeded_agent}/avatar")
            assert got.status == 404
            assert (await got.json())["code"] == "avatar_not_found"

    @pytest.mark.asyncio
    async def test_plain_image_put_discards_a_stale_staging(self, seeded_agent):
        """An abandoned staging must not ride into an unrelated later save.

        Scenario: a save staged a picture but its PUT never landed; a LATER
        edit (touching only other fields) PUTs ``{"kind":"image"}`` without
        ``promote`` — the crew must keep its current picture and the stale
        staging must be discarded, not silently committed.
        """
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.handlers.agents import _pending_avatar_path

        async with TestClient(TestServer(self._app())) as client:
            await self._commit(client, seeded_agent, _PNG)
            # Abandoned staging from a save whose PUT never happened.
            await client.post(f"/api/agents/{seeded_agent}/avatar", data=self._form(_JPG))
            resp = await client.put(
                f"/api/agents/{seeded_agent}", json={"avatar": {"kind": "image"}}
            )
            assert resp.status == 200
            got = await client.get(f"/api/agents/{seeded_agent}/avatar")
            assert got.headers["Content-Type"] == "image/png"
            assert await got.read() == _PNG
        assert _pending_avatar_path(seeded_agent) is None

    @pytest.mark.asyncio
    async def test_stale_token_does_not_promote_newer_staging(self, seeded_agent):
        """Save A's token must not commit save B's bytes.

        Sequence: A stages PNG (token A); B stages JPG over the same slot;
        A's PUT arrives with token A — the staged bytes no longer match, so
        nothing is promoted and A's commit fails avatar_file_missing rather
        than installing B's picture under A's intent.
        """
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            up_a = await client.post(f"/api/agents/{seeded_agent}/avatar", data=self._form(_PNG))
            tok_a = (await up_a.json())["token"]
            await client.post(f"/api/agents/{seeded_agent}/avatar", data=self._form(_JPG))
            resp = await client.put(
                f"/api/agents/{seeded_agent}",
                json={"avatar": {"kind": "image", "promote": True, "token": tok_a}},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "avatar_file_missing"
            assert (await client.get(f"/api/agents/{seeded_agent}/avatar")).status == 404

    @pytest.mark.asyncio
    async def test_stale_token_fails_even_when_a_live_picture_exists(self, seeded_agent):
        """A failed promotion must not fall back to reporting success.

        With a picture already live, a stale-token promote could quietly
        keep the old file and return 200 — the save the user just made
        would claim success while their selected replacement was dropped.
        It must fail, and the previously saved picture must stay intact.
        """
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            await self._commit(client, seeded_agent, _PNG)
            up_a = await client.post(f"/api/agents/{seeded_agent}/avatar", data=self._form(_JPG))
            tok_a = (await up_a.json())["token"]
            await client.post(f"/api/agents/{seeded_agent}/avatar", data=self._form(_WEBP))
            resp = await client.put(
                f"/api/agents/{seeded_agent}",
                json={"avatar": {"kind": "image", "promote": True, "token": tok_a}},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "avatar_file_missing"
            got = await client.get(f"/api/agents/{seeded_agent}/avatar")
            assert got.status == 200
            assert await got.read() == _PNG

    @pytest.mark.asyncio
    async def test_install_never_touches_the_committed_file(self, seeded_agent, monkeypatch):
        """A process kill mid-promotion must never strand the saved picture.

        Installs are content-addressed: the pin is that at the moment the
        staged replacement is installed, the committed file is still on disk
        at its own path — nothing overwrites or moves it before the config
        save commits the replacement. Only the successful commit reaps it.
        """
        from aiohttp.test_utils import TestClient, TestServer

        import kiro_crew.dashboard.handlers.agents as handlers_agents

        async with TestClient(TestServer(self._app())) as client:
            await self._commit(client, seeded_agent, _PNG)
            committed = self._stored(seeded_agent)
            assert committed is not None and committed.suffix == ".png"
            real = handlers_agents.replace_with_retry
            committed_at_install: list[object] = []

            def spying(src, dst):
                # Only the INSTALL step is under test. The staging write also
                # lands at a `.jpg` path (`<stem>.pending.jpg`) through the
                # same atomic replace, so it is filtered out here rather than
                # counted as a second install.
                if dst.suffix == ".jpg" and ".pending." not in dst.name:
                    committed_at_install.append(committed.is_file())
                    committed_at_install.append(dst != committed)
                return real(src, dst)

            monkeypatch.setattr(handlers_agents, "replace_with_retry", spying)
            resp = await self._commit(client, seeded_agent, _JPG)
            assert resp.status == 200
            assert committed_at_install == [True, True]
            # And the successful commit reaped the previous variant.
            assert not committed.is_file()

    @pytest.mark.asyncio
    async def test_failed_install_leaves_the_old_picture_untouched(self, seeded_agent, monkeypatch):
        """An install failure (ENOSPC/EIO) must not damage the saved picture.

        The install is the only filesystem step of a promotion and lands at
        a content-addressed path, so its failure leaves the committed file
        byte-identical and still selected — the save fails, nothing rolls
        over the old picture.
        """
        from aiohttp.test_utils import TestClient, TestServer

        import kiro_crew.dashboard.handlers.agents as handlers_agents

        def _boom(src, dst):
            raise OSError("disk full")

        async with TestClient(TestServer(self._app())) as client:
            await self._commit(client, seeded_agent, _PNG)
            up = await client.post(f"/api/agents/{seeded_agent}/avatar", data=self._form(_JPG))
            tok = (await up.json())["token"]
            monkeypatch.setattr(handlers_agents, "replace_with_retry", _boom)
            resp = await client.put(
                f"/api/agents/{seeded_agent}",
                json={"avatar": {"kind": "image", "promote": True, "token": tok}},
            )
            assert resp.status == 500
            got = await client.get(f"/api/agents/{seeded_agent}/avatar")
            assert got.status == 200
            assert await got.read() == _PNG
        assert KiroCrewConfig.load().agents[seeded_agent].avatar.get("kind") == "image"

    @pytest.mark.asyncio
    async def test_get_serves_only_the_committed_file(self, seeded_agent):
        """The config's file pin decides what GET serves.

        A stray variant — what an install that never reached its config save
        leaves behind — must not be served in the committed picture's place
        after a restart.
        """
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.handlers.agents import _avatar_stem, _avatars_dir

        async with TestClient(TestServer(self._app())) as client:
            await self._commit(client, seeded_agent, _WEBP)
            # Simulate the uncommitted install: the file landed, the config
            # save never did.
            stray = _avatars_dir() / f"{_avatar_stem(seeded_agent)}.{'0' * 16}.png"
            stray.write_bytes(_PNG)
            got = await client.get(f"/api/agents/{seeded_agent}/avatar")
            assert got.status == 200
            assert got.headers["Content-Type"] == "image/webp"
            assert await got.read() == _WEBP

    @pytest.mark.asyncio
    async def test_get_refuses_planted_non_regular_or_oversized_files(
        self, seeded_agent, monkeypatch, tmp_path
    ):
        """Stored avatar files are not trusted just for being in place.

        Defense in depth under the ``run/`` fence: a planted oversized blob
        must not be slurped unbounded into memory, and a planted symlink
        must not let the authenticated GET read an arbitrary file. Both
        serve as 404.
        """
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            await self._commit(client, seeded_agent, _PNG)
            committed = self._stored(seeded_agent)
            assert committed is not None
            monkeypatch.setattr("kiro_crew.dashboard.handlers.agents._AVATAR_MAX_BYTES", 128)
            committed.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 256)
            got = await client.get(f"/api/agents/{seeded_agent}/avatar")
            assert got.status == 404

            target = tmp_path / "outside.png"
            target.write_bytes(_PNG)
            committed.unlink()
            committed.symlink_to(target)
            got = await client.get(f"/api/agents/{seeded_agent}/avatar")
            assert got.status == 404

    @pytest.mark.asyncio
    async def test_failed_save_rolls_back_without_touching_the_committed_file(
        self, seeded_agent, monkeypatch
    ):
        """A config-save failure removes only the orphaned install.

        The committed file lives at its own content-addressed path and is
        never unlinked, moved, or overwritten by a failed save — the old
        bytes stay selected and served.
        """
        from aiohttp.test_utils import TestClient, TestServer

        import kiro_crew.dashboard.handlers.agents as handlers_agents

        other_png = b"\x89PNG\r\n\x1a\n" + b"\x01" * 64 + _PNG_IEND

        async with TestClient(TestServer(self._app())) as client:
            await self._commit(client, seeded_agent, _PNG)
            committed = self._stored(seeded_agent)
            assert committed is not None
            up = await client.post(f"/api/agents/{seeded_agent}/avatar", data=self._form(other_png))
            tok = (await up.json())["token"]

            unlinked: list[str] = []
            real_unlink = handlers_agents.Path.unlink

            def spy_unlink(self, *a, **kw):
                if self == committed:
                    unlinked.append(self.name)
                return real_unlink(self, *a, **kw)

            monkeypatch.setattr(handlers_agents.Path, "unlink", spy_unlink)
            real_save = handlers_agents.KiroCrewConfig.save
            monkeypatch.setattr(
                handlers_agents.KiroCrewConfig,
                "save",
                lambda self_cfg: (_ for _ in ()).throw(OSError("disk full")),
            )
            resp = await client.put(
                f"/api/agents/{seeded_agent}",
                json={"avatar": {"kind": "image", "promote": True, "token": tok}},
            )
            assert resp.status == 500
            # The committed path was never unlinked, and the orphaned
            # install was removed by the rollback.
            assert unlinked == []
            monkeypatch.setattr(handlers_agents.KiroCrewConfig, "save", real_save)
            got = await client.get(f"/api/agents/{seeded_agent}/avatar")
            assert got.status == 200
            assert await got.read() == _PNG

    @pytest.mark.asyncio
    async def test_promote_flag_never_persists(self, seeded_agent):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            await self._commit(client, seeded_agent, _PNG)
        stored = KiroCrewConfig.load().agents[seeded_agent].avatar
        assert "promote" not in stored and "token" not in stored
        assert stored["kind"] == "image"
