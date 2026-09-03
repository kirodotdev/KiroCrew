"""Images pasted into a meeting note.

This is the app's first binary upload, so the tests are organised around the two
things that make it safe rather than around the happy path:

* **Nothing the client sends becomes a path.** The extension comes from sniffing
  the bytes and the name is a fresh uuid, so there is no traversal case — the tests
  assert that property directly rather than trying to defeat a sanitizer.
* **An unrecognised signature is refused.** SVG is the one that matters: it has no
  binary signature and it is a document that can carry script, not an image.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from aiohttp import FormData
from meetings_helpers import (  # noqa: F401 — fixtures are used by name
    app_fixture,
    client_for,
    enabled_fixture,
    reset_module_state_fixture,
    root_fixture,
)

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.domain import images

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF87 = b"GIF87a" + b"\x00" * 32
GIF89 = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF" + struct.pack("<I", 40) + b"WEBP" + b"\x00" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'

#: The shape the server generates: 32 lowercase hex characters plus a sniffed
#: extension. Anything else is refused by `safe_note_image_name`.
GENERATED_NAME = "0123456789abcdef0123456789abcdef.png"


async def _upload(client, body: bytes, *, meeting: str = "m1", field: str = "file"):
    form = {field: body}
    return await client.post(f"{k.API_BASE}/meetings/{meeting}/note/images", data=form)


# ---------------------------------------------------------------------------
# Sniffing
# ---------------------------------------------------------------------------


class TestSniffImageExt:
    @pytest.mark.parametrize(
        "data,expected",
        [
            (PNG, ".png"),
            (JPEG, ".jpg"),
            (GIF87, ".gif"),
            (GIF89, ".gif"),
            (WEBP, ".webp"),
        ],
    )
    def test_recognises_the_raster_formats_a_screenshot_can_be(self, data, expected):
        assert images.sniff_image_ext(data) == expected

    def test_refuses_svg(self):
        # The one that matters. An SVG has no binary signature, so a
        # verify-the-claimed-extension design fails OPEN for it — and an SVG is a
        # document that can carry <script>, not an image.
        assert images.sniff_image_ext(SVG) is None

    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"\x89PN",  # truncated PNG header
            b"%PDF-1.7 rest",
            b"plain text pretending to be a screenshot",
            b"BM" + b"\x00" * 32,  # BMP: deliberately not accepted
            b"RIFF" + struct.pack("<I", 40) + b"AVI " + b"\x00" * 32,  # RIFF, not WebP
        ],
    )
    def test_refusal_is_the_default(self, data):
        assert images.sniff_image_ext(data) is None

    def test_sniffing_needs_no_more_than_the_advertised_prefix(self):
        # So a caller can decide from a header rather than buffering a whole file.
        assert images.sniff_image_ext(PNG[: images.MIN_SNIFF_BYTES]) == ".png"
        assert images.sniff_image_ext(WEBP[: images.MIN_SNIFF_BYTES]) == ".webp"


class TestFormatElapsed:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "0:00"),
            (9, "0:09"),
            (61, "1:01"),
            (623, "10:23"),
            (3599, "59:59"),
            (3600, "1:00:00"),
            (3661, "1:01:01"),
        ],
    )
    def test_formats_as_a_reader_would_expect(self, seconds, expected):
        assert images.format_elapsed(seconds) == expected

    @pytest.mark.parametrize("bad", [-5, float("nan"), float("inf"), None, "x"])
    def test_nonsense_collapses_to_zero_rather_than_rendering_a_minus(self, bad):
        # A clock that moved backwards must not put "-1:-30" into a note.
        assert images.format_elapsed(bad) == "0:00"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TestStore:
    def test_images_live_below_the_meeting_directory(self, root: Path):
        # One level down, so an image can never share a name with a flat agent
        # output file whatever a future widget extension turns out to be.
        directory = store.note_images_dir("m1", root)
        assert directory.parent == store.meetings_root(root).resolve() / "m1"
        assert directory.name == k.NOTE_IMAGES_DIR

    def test_write_then_read_back(self, root: Path):
        path = store.write_note_image("m1", GENERATED_NAME, PNG, root)
        assert path.read_bytes() == PNG

    def test_paths_are_contained(self, root: Path):
        resolved = store.note_image_path("m1", GENERATED_NAME, root)
        assert resolved.is_relative_to(store.data_dir(root).resolve())

    @pytest.mark.parametrize(
        "escape",
        [
            "../escape.png",
            "../../etc/passwd",
            "a/b.png",
            # The one that shows why `contain` alone is not enough: this resolves to
            # ANOTHER MEETING's agent output file and is still inside the data root.
            "../m2/note-taker.md",
            "abc.png",  # right shape, wrong length — not a generated name
            "ABCDEF0123456789abcdef0123456789.png",  # uppercase hex
            "0123456789abcdef0123456789abcdef.svg",  # generated shape, refused type
            "0123456789abcdef0123456789abcdef.png.svg",
            "",
        ],
    )
    def test_only_a_generated_name_is_accepted(self, root: Path, escape: str):
        # Nothing hands this a client string today, but "nothing currently does" is
        # not a barrier. The name check is.
        with pytest.raises(store.MeetingsPathError):
            store.note_image_path("m1", escape, root)

    def test_a_generated_name_is_accepted(self, root: Path):
        for ext in ("png", "jpg", "gif", "webp"):
            name = f"{'a' * 32}.{ext}"
            assert store.note_image_path("m1", name, root).name == name

    def test_an_unsafe_meeting_id_is_refused(self, root: Path):
        with pytest.raises(store.MeetingsPathError):
            store.note_images_dir("../escape", root)


# ---------------------------------------------------------------------------
# Upload route
# ---------------------------------------------------------------------------


class TestUpload:
    @pytest.mark.asyncio
    async def test_stores_a_png_and_returns_a_relative_src(self, app, root: Path):
        async with client_for(app) as client:
            resp = await _upload(client, PNG)
            assert resp.status == 200
            body = await resp.json()

        assert body["ok"] is True
        # Relative, so the dashboard's markdown renderer resolves it against the
        # note's own location — which is what avoids a second serving route here.
        assert body["src"] == f"{k.NOTE_IMAGES_DIR}/{body['filename']}"
        assert body["content_type"] == "image/png"
        assert store.note_image_path("m1", body["filename"], root).read_bytes() == PNG

    @pytest.mark.asyncio
    async def test_the_filename_is_server_generated_hex_plus_a_sniffed_extension(self, app):
        # The property that removes the whole traversal question: no client string
        # reaches a path.
        async with client_for(app) as client:
            body = await (await _upload(client, JPEG)).json()
        stem, _, ext = body["filename"].rpartition(".")
        assert ext == "jpg"
        assert len(stem) == 32
        assert all(c in "0123456789abcdef" for c in stem)

    @pytest.mark.asyncio
    async def test_a_hostile_client_filename_is_ignored_entirely(self, app, root: Path):
        # The part's filename is attacker-controlled and the handler never reads it,
        # so it cannot appear anywhere on disk.
        form = FormData()
        form.add_field("file", PNG, filename="../../../etc/passwd.png")
        async with client_for(app) as client:
            resp = await client.post(f"{k.API_BASE}/meetings/m1/note/images", data=form)
            assert resp.status == 200
            body = await resp.json()
        assert "passwd" not in body["filename"]
        assert ".." not in body["filename"]
        stored = list(store.note_images_dir("m1", root).iterdir())
        assert [p.name for p in stored] == [body["filename"]]

    @pytest.mark.asyncio
    async def test_refuses_an_svg(self, app, root: Path):
        async with client_for(app) as client:
            resp = await _upload(client, SVG)
            assert resp.status == 400
        assert not store.note_images_dir("m1", root).exists() or not list(
            store.note_images_dir("m1", root).iterdir()
        )

    @pytest.mark.asyncio
    async def test_refuses_a_text_file_renamed_to_png(self, app):
        async with client_for(app) as client:
            resp = await client.post(
                f"{k.API_BASE}/meetings/m1/note/images",
                data={"file": ("shot.png", b"not an image at all")},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_refuses_an_oversized_image_with_413(self, app):
        oversized = PNG + b"\x00" * (k.MAX_NOTE_IMAGE_BYTES + 1)
        async with client_for(app) as client:
            resp = await _upload(client, oversized)
            assert resp.status == 413
            assert (await resp.json())["code"] == "image_too_large"

    @pytest.mark.asyncio
    async def test_refuses_a_non_multipart_body(self, app):
        async with client_for(app) as client:
            resp = await client.post(f"{k.API_BASE}/meetings/m1/note/images", json={"file": "nope"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_refuses_a_body_with_no_file_field(self, app):
        async with client_for(app) as client:
            resp = await _upload(client, PNG, field="screenshot")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_caps_the_number_of_images_per_note(self, app, root: Path, monkeypatch):
        monkeypatch.setattr(k, "MAX_NOTE_IMAGES", 2)
        async with client_for(app) as client:
            assert (await _upload(client, PNG)).status == 200
            assert (await _upload(client, PNG)).status == 200
            assert (await _upload(client, PNG)).status == 400

    @pytest.mark.asyncio
    async def test_images_are_per_meeting(self, app, root: Path):
        async with client_for(app) as client:
            first = await (await _upload(client, PNG, meeting="m1")).json()
            second = await (await _upload(client, PNG, meeting="m2")).json()
        assert first["filename"] != second["filename"]
        assert store.note_image_path("m1", first["filename"], root).is_file()
        assert store.note_image_path("m2", second["filename"], root).is_file()

    @pytest.mark.asyncio
    async def test_an_unsafe_meeting_id_is_refused(self, app):
        async with client_for(app) as client:
            resp = await client.post(
                f"{k.API_BASE}/meetings/..%2F..%2Fetc/note/images", data={"file": PNG}
            )
            assert resp.status in (400, 403, 404)


class TestAltText:
    @pytest.mark.asyncio
    async def test_alt_is_empty_when_the_meeting_has_not_started(self, app):
        # An honest `![](images/…)` beats inventing a timestamp.
        async with client_for(app) as client:
            body = await (await _upload(client, PNG)).json()
        assert body["alt"] == ""

    @pytest.mark.asyncio
    async def test_alt_is_the_elapsed_time_once_a_meeting_is_running(self, app, root: Path):
        # The whole point of the alt text: it is how a reader lines the image up
        # against the transcript later.
        meta = store.new_meeting_meta("m1", "Weekly")
        meta["started_at"] = "2026-08-04T10:00:00Z"
        store.write_meeting_meta("m1", meta, root)

        import kiro_crew.apps.builtins.meetings.backend.routes.meeting_lifecycle as lifecycle

        # 10:23 into the meeting.
        elapsed = lifecycle.images.format_elapsed(623)
        assert elapsed == "10:23"

        async with client_for(app) as client:
            body = await (await _upload(client, PNG)).json()
        # The real clock is used, so assert on SHAPE rather than a fixed value.
        assert body["alt"]
        assert ":" in body["alt"]

    @pytest.mark.asyncio
    async def test_a_malformed_started_at_yields_no_alt_rather_than_an_error(self, app, root: Path):
        meta = store.new_meeting_meta("m1", "Weekly")
        meta["started_at"] = "not a timestamp"
        store.write_meeting_meta("m1", meta, root)
        async with client_for(app) as client:
            resp = await _upload(client, PNG)
            assert resp.status == 200
            assert (await resp.json())["alt"] == ""
