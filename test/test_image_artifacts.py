"""Tests for first-class IMAGE artifact support.

Covers three layers:

* :class:`kiro_crew.artifacts.ArtifactStore` — ``create_image`` round-trip,
  ``read_image_bytes``, dimension sniffing, allowlist / oversize rejection,
  sidecar cleanup on delete, and tolerant meta serialization.
* the dashboard ``_serialize`` shape the frontend consumes.
* :mod:`kiro_crew.image_artifacts` — auto-registration of local markdown images
  from finalized chat text (stable slug, idempotent, remote-skipping), and the
  restricted-session gate in the chat_runner scheduler.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import types
from pathlib import Path

import pytest

from kiro_crew import image_artifacts
from kiro_crew.artifacts import (
    MAX_CONTENT_BYTES,
    ArtifactAlreadyExistsError,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactStore,
    ArtifactValidationError,
    ImageMetadata,
)

# ── Fixtures / helpers ────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ArtifactStore:
    """A tmp-rooted store installed as image_artifacts' default."""
    s = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(image_artifacts, "get_default_store", lambda: s)
    return s


def _png_bytes(width: int = 3, height: int = 2) -> bytes:
    """Minimal PNG whose IHDR carries ``width``/``height`` (enough for the sniff)."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_body = (
        b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"  # bit depth / color type / compression / filter / interlace
    )
    ihdr = (13).to_bytes(4, "big") + ihdr_body + b"\x00\x00\x00\x00"  # len + body + (fake) crc
    iend = (0).to_bytes(4, "big") + b"IEND" + b"\xae\x42\x60\x82"
    return sig + ihdr + iend


def _gif_bytes(width: int = 7, height: int = 5) -> bytes:
    return b"GIF89a" + width.to_bytes(2, "little") + height.to_bytes(2, "little") + b"\x00" * 4


def _bmp_bytes(width: int = 9, height: int = 5) -> bytes:
    """Minimal BMP whose BITMAPINFOHEADER carries width/height (signed LE int32)."""
    header = bytearray(b"BM" + b"\x00" * 24)
    header[18:22] = width.to_bytes(4, "little", signed=True)
    header[22:26] = height.to_bytes(4, "little", signed=True)
    return bytes(header)


# ── Store: create_image / read_image_bytes ─────────────────────────────────────


class TestCreateImage:
    def test_round_trips_bytes_and_metadata(self, store: ArtifactStore) -> None:
        data = _png_bytes(3, 2)
        art = store.create_image(
            name="Diagram",
            image_bytes=data,
            mime="image/png",
            alt="a diagram",
            original_filename="diagram.png",
        )
        assert art.kind == "image"
        assert art.version == 1
        assert art.content == ""  # image body lives in the sidecar, not current.html
        assert art.image is not None
        assert art.image.mime == "image/png"
        assert art.image.ext == "png"
        assert art.image.size_bytes == len(data)
        assert art.image.width == 3 and art.image.height == 2
        assert art.image.sha256 == hashlib.sha256(data).hexdigest()
        assert art.image.alt == "a diagram"
        assert art.image.original_filename == "diagram.png"

        # Bytes come back verbatim with the right mime.
        got_bytes, got_mime = store.read_image_bytes(art.slug)
        assert got_bytes == data
        assert got_mime == "image/png"

    def test_asset_sidecar_written_next_to_meta(self, store: ArtifactStore) -> None:
        art = store.create_image(name="I", image_bytes=_png_bytes(), mime="image/png")
        asset = store.root / art.slug / "asset.png"
        assert asset.exists()
        assert asset.read_bytes() == _png_bytes()
        # current.html is present but empty (uniform three-file directory shape).
        assert (store.root / art.slug / "current.html").read_text() == ""

    def test_get_and_meta_reload_include_image(self, store: ArtifactStore) -> None:
        slug = store.create_image(name="I", image_bytes=_gif_bytes(7, 5), mime="image/gif").slug
        # Reload through get() (which goes through _load_meta -> tolerant parse).
        reloaded = store.get(slug)
        assert reloaded.image is not None
        assert reloaded.image.mime == "image/gif"
        assert reloaded.image.width == 7 and reloaded.image.height == 5
        # to_dict serializes the nested block for the API/frontend.
        d = reloaded.to_dict()
        assert isinstance(d["image"], dict)
        assert d["image"]["mime"] == "image/gif"
        assert d["kind"] == "image"

    def test_delete_removes_sidecar(self, store: ArtifactStore) -> None:
        slug = store.create_image(name="I", image_bytes=_png_bytes(), mime="image/png").slug
        adir = store.root / slug
        assert (adir / "asset.png").exists()
        store.delete(slug)
        assert not adir.exists()  # whole directory (incl. sidecar) gone

    def test_jpeg_dimension_sniff(self, store: ArtifactStore) -> None:
        # SOI + SOF0 segment declaring 20x10, then EOI.
        sof0 = b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08" + (10).to_bytes(2, "big") + (
            20
        ).to_bytes(2, "big") + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
        data = b"\xff\xd8" + sof0 + b"\xff\xd9"
        art = store.create_image(name="J", image_bytes=data, mime="image/jpeg")
        assert art.image is not None
        assert art.image.width == 20 and art.image.height == 10

    def test_unmeasurable_image_stores_with_none_dims(self, store: ArtifactStore) -> None:
        # Valid mime, but bytes are not a real PNG — sniff must yield None, not raise.
        art = store.create_image(name="X", image_bytes=b"not a real png", mime="image/png")
        assert art.image is not None
        assert art.image.width is None and art.image.height is None

    def test_rejects_disallowed_mime(self, store: ArtifactStore) -> None:
        # SVG is markup, not a raster — explicitly rejected.
        with pytest.raises(ArtifactValidationError, match="unsupported image mime"):
            store.create_image(name="S", image_bytes=b"<svg/>", mime="image/svg+xml")

    def test_rejects_empty_bytes(self, store: ArtifactStore) -> None:
        with pytest.raises(ArtifactValidationError, match="empty"):
            store.create_image(name="E", image_bytes=b"", mime="image/png")

    def test_rejects_oversize(self, store: ArtifactStore) -> None:
        big = b"\x00" * (MAX_CONTENT_BYTES + 1)
        with pytest.raises(ArtifactValidationError, match="exceeds"):
            store.create_image(name="Big", image_bytes=big, mime="image/png")

    def test_explicit_slug_collision_raises(self, store: ArtifactStore) -> None:
        store.create_image(name="I", image_bytes=_png_bytes(), mime="image/png", slug="pic")
        with pytest.raises(ArtifactAlreadyExistsError):
            store.create_image(name="I2", image_bytes=_png_bytes(), mime="image/png", slug="pic")

    def test_read_image_bytes_rejects_non_image(self, store: ArtifactStore) -> None:
        slug = store.create(name="Doc", content="# hi", kind="markdown").slug
        with pytest.raises(Exception):  # ArtifactNotFoundError — no image asset
            store.read_image_bytes(slug)


class TestImageMetadataTolerantLoad:
    def test_parse_handles_partial_block(self, store: ArtifactStore) -> None:
        parsed = ArtifactStore._parse_image_metadata({"mime": "image/png", "width": "bad"})
        assert isinstance(parsed, ImageMetadata)
        assert parsed.mime == "image/png"
        assert parsed.width is None  # wrong-typed → default, not a crash
        assert parsed.size_bytes == 0

    def test_parse_none_for_absent(self) -> None:
        assert ArtifactStore._parse_image_metadata(None) is None
        assert ArtifactStore._parse_image_metadata("nope") is None


# ── Dashboard serialize shape ───────────────────────────────────────────────


class TestSerializeShape:
    def test_serialize_redacts_llm_derived_image_metadata(
        self, store: ArtifactStore
    ) -> None:
        """``alt`` / ``original_filename`` must pass the same gate as ``name``.

        Both come from markdown the agent wrote. The dashboard prefers
        ``image.alt`` over ``name`` for the accessible description, so leaving
        the image block raw would route unredacted text onto the very surface
        ``name``'s redaction protects.
        """
        from kiro_crew.dashboard.handlers.artifacts import _serialize

        leak = "AKIAIOSFODNN7EXAMPLE"
        art = store.create_image(
            name=f"pic {leak}",
            image_bytes=_png_bytes(4, 4),
            mime="image/png",
            alt=f"see {leak}",
            original_filename=f"{leak}.png",
        )
        out = _serialize(art, include_content=True)
        # Baseline: the sibling field is already gated, so this test fails loudly
        # if the redactor itself stops recognising the pattern.
        assert leak not in out["name"]
        assert leak not in out["image"]["alt"]
        assert leak not in out["image"]["original_filename"]
        # Structural leaves are store-computed and must survive untouched.
        assert out["image"]["mime"] == "image/png"
        assert out["image"]["width"] == 4

    def test_serialize_includes_image_block(self, store: ArtifactStore) -> None:
        from kiro_crew.dashboard.handlers.artifacts import _serialize

        art = store.create_image(
            name="Pic", image_bytes=_png_bytes(4, 4), mime="image/png", alt="alt"
        )
        out = _serialize(art, include_content=True)
        assert out["kind"] == "image"
        assert out["image"]["mime"] == "image/png"
        assert out["image"]["width"] == 4
        assert out["image"]["alt"] == "alt"


# ── image_artifacts: auto-registration ──────────────────────────────────────


class TestMarkdownDestinationParsing:
    """Parser-level cases, asserted without touching the filesystem.

    These run identically on every OS — which matters, because treating every
    backslash as an escape stripped Windows path separators and silently
    disabled image registration on Windows with no failing POSIX test.
    """

    def test_windows_separators_survive(self) -> None:
        got = image_artifacts.md_destination(r"C:\Users\me\shot.png)")
        assert got == r"C:\Users\me\shot.png"

    def test_balanced_parens_are_kept(self) -> None:
        assert image_artifacts.md_destination("/tmp/screenshot(1).png)") == (
            "/tmp/screenshot(1).png"
        )

    def test_markdown_escaped_paren_is_unescaped(self) -> None:
        assert image_artifacts.md_destination(r"/tmp/a\(b.png)") == "/tmp/a(b.png"

    def test_title_suffix_is_split_off(self) -> None:
        assert image_artifacts.md_destination('/tmp/a.png "a title")') == "/tmp/a.png"

    def test_angle_wrapped_path_with_spaces_survives(self) -> None:
        got = image_artifacts.md_destination("</tmp/generated images/chart.png>)")
        assert got == "/tmp/generated images/chart.png"

    def test_angle_wrapped_path_with_title_suffix(self) -> None:
        got = image_artifacts.md_destination('</tmp/a b/c.png> "a title")')
        assert got == "/tmp/a b/c.png"

    def test_unterminated_angle_wrap_is_none(self) -> None:
        assert image_artifacts.md_destination("</tmp/a b/c.png)") is None

    def test_unclosed_destination_is_none(self) -> None:
        assert image_artifacts.md_destination("/tmp/a.png") is None


class TestRegisterImages:
    def test_per_message_image_count_is_capped(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        """One message cannot register unbounded images.

        Pruning runs only after the loop, so without a ceiling a message with a
        thousand references copies a thousand sidecars first.
        """
        img = tmp_path / "one.png"
        img.write_bytes(_png_bytes())
        text = "\n".join(f"![a{i}]({img})" for i in range(40))
        slugs = image_artifacts.register_images(text, "ts-many", "chat-1")
        assert len(slugs) == image_artifacts.MAX_IMAGES_PER_MESSAGE
        assert len(store.list()) == image_artifacts.MAX_IMAGES_PER_MESSAGE

    def test_per_message_byte_budget_stops_copying(
        self, store: ArtifactStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The byte budget is checked before the copy, so it bounds bytes written."""
        monkeypatch.setattr(image_artifacts, "MAX_IMAGE_BYTES_PER_MESSAGE", 100)
        big = tmp_path / "big.png"
        big.write_bytes(_png_bytes())
        monkeypatch.setattr(
            image_artifacts, "safe_read_file_bytes_nolink", lambda *a, **k: b"x" * 60
        )
        # First image fits (60 <= 100); the second would exceed it.
        slugs = image_artifacts.register_images(
            f"![a]({big})\n![b]({big})", "ts-budget", "chat-1"
        )
        assert len(slugs) == 1

    def test_two_images_on_one_line_are_both_registered(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        """Both same-line references must register.

        A destination pattern that runs to end-of-line makes `finditer` consume
        every later image on that line into a single match, so the second one is
        never seen.
        """
        a = tmp_path / "a.png"
        b = tmp_path / "b.png"
        a.write_bytes(_png_bytes(2, 2))
        b.write_bytes(_png_bytes(3, 3))
        slugs = image_artifacts.register_images(f"![a]({a}) ![b]({b})", "ts-line", "c")
        assert len(slugs) == 2
        widths = sorted(store.get(s).image.width for s in slugs)  # type: ignore[union-attr]
        assert widths == [2, 3]

    def test_replay_cannot_walk_past_the_per_message_cap(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        """Re-finalizing an over-cap message must not store the next batch.

        Counting only successful creations meant an already-registered duplicate
        never advanced the counter, so each replay stored the following slice.
        """
        img = tmp_path / "one.png"
        img.write_bytes(_png_bytes())
        text = "\n".join(f"![a{i}]({img})" for i in range(40))
        first = image_artifacts.register_images(text, "ts-replay", "chat-1")
        assert len(first) == image_artifacts.MAX_IMAGES_PER_MESSAGE
        # Replay: every eligible image is a duplicate, so nothing new is stored.
        again = image_artifacts.register_images(text, "ts-replay", "chat-1")
        assert again == []
        assert len(store.list()) == image_artifacts.MAX_IMAGES_PER_MESSAGE

    def test_escaped_brackets_in_alt_text_still_register(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        """`![Revenue \\[Q1\\]](p.png)` is legal markdown and must be captured.

        A plain `[^\\]]*` alt capture stops at the escaped `]`, the whole pattern
        then fails, and the image is never registered at all — the source file is
        deleted later and the only copy is gone.
        """
        img = tmp_path / "chart.png"
        img.write_bytes(_png_bytes(6, 3))
        slugs = image_artifacts.register_images(
            rf"![Revenue \[Q1\]]({img})", "ts-esc", "chat-1"
        )
        assert len(slugs) == 1
        art = store.get(slugs[0])
        # The escapes are unwrapped, so the caption reads as authored.
        assert art.name == "Revenue [Q1]"
        assert art.image is not None and art.image.alt == "Revenue [Q1]"

    def test_registration_runs_off_the_loop_without_the_subprocess_executor(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        """Dispatch must not queue behind the shared subprocess executor.

        A wedged worker there would hold registration until after the temp source
        file is gone, turning a late copy into a lost one.
        """
        img = tmp_path / "x.png"
        img.write_bytes(_png_bytes())
        slugs = asyncio.run(
            image_artifacts.register_images_off_loop(f"![a]({img})", "ts-thread", "c")
        )
        assert len(slugs) == 1
        assert not hasattr(image_artifacts, "subprocess_executor")

    def test_parenthesised_filename_is_not_truncated(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        """``screenshot(1).png`` is an ordinary filename, not a parse edge case.

        A lazy `[^)\\s]+` destination capture stops at the inner ``)``, leaving a
        path that does not exist — so the image is skipped and its only copy is
        lost to temp cleanup. Markdown permits unescaped parens while balanced.
        """
        img = tmp_path / "screenshot(1).png"
        img.write_bytes(_png_bytes(6, 3))
        slugs = image_artifacts.register_images(f"![shot]({img})", "ts-paren", "chat-1")
        assert len(slugs) == 1
        art = store.get(slugs[0])
        assert art.image is not None
        assert art.image.original_filename == "screenshot(1).png"
        assert art.image.width == 6

    def test_title_suffix_still_does_not_leak_into_the_path(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        """`![a](p "t")` — the quoted title is not part of the destination."""
        img = tmp_path / "titled.png"
        img.write_bytes(_png_bytes())
        slugs = image_artifacts.register_images(
            f'![a]({img} "a title")', "ts-title", "chat-1"
        )
        assert len(slugs) == 1
        assert store.get(slugs[0]).image.original_filename == "titled.png"  # type: ignore[union-attr]

    def test_unclosed_destination_is_skipped(self, store: ArtifactStore) -> None:
        """An unbalanced destination registers nothing instead of guessing."""
        assert image_artifacts.register_images("![a](/tmp/nope.png", "ts-open", "c") == []

    def test_bmp_is_registered_with_sniffed_dimensions(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        """BMP is a raster the chat surface renders, so it must be durable too."""
        img = tmp_path / "diagram.bmp"
        img.write_bytes(_bmp_bytes(9, 5))
        slugs = image_artifacts.register_images(f"![d]({img})", "ts-bmp", "chat-1")
        assert len(slugs) == 1
        art = store.get(slugs[0])
        assert art.image is not None
        assert art.image.mime == "image/bmp"
        assert (art.image.width, art.image.height) == (9, 5)
        assert store.read_image_bytes(slugs[0])[1] == "image/bmp"

    def test_registers_local_image_with_stable_slug(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        img = tmp_path / "shot.png"
        img.write_bytes(_png_bytes(5, 4))
        text = f"Here is a screenshot:\n![my shot]({img})"
        slugs = image_artifacts.register_images(text, "ts-1", "chat-1")
        assert slugs == [image_artifacts._derive_image_slug("ts-1", 0)]
        art = store.get(slugs[0])
        assert art.kind == "image"
        assert art.name == "my shot"
        assert art.auto_registered is True
        assert art.pinned is False
        assert art.session_key == "chat-1"
        assert art.image is not None and art.image.width == 5
        # Bytes were copied, not referenced.
        assert store.read_image_bytes(slugs[0])[0] == _png_bytes(5, 4)

    def test_second_finalize_does_not_duplicate(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        img = tmp_path / "s.png"
        img.write_bytes(_png_bytes())
        text = f"![a]({img})"
        first = image_artifacts.register_images(text, "ts-dup", "chat-1")
        assert first
        again = image_artifacts.register_images(text, "ts-dup", "chat-1")
        assert again == []  # same slug already exists → skipped
        assert len(store.list()) == 1

    def test_replay_does_not_overwrite_edits(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        img = tmp_path / "s.png"
        img.write_bytes(_png_bytes())
        text = f"![a]({img})"
        slug = image_artifacts.register_images(text, "ts-e", "chat-1")[0]
        store.update(slug, name="renamed by user")
        image_artifacts.register_images(text, "ts-e", "chat-1")
        assert store.get(slug).name == "renamed by user"

    def test_remote_urls_are_skipped(self, store: ArtifactStore) -> None:
        text = (
            "![a](https://example.com/x.png)\n"
            "![b](http://example.com/y.jpg)\n"
            "![c](data:image/png;base64,AAAA)"
        )
        assert image_artifacts.register_images(text, "ts-r", "chat-1") == []
        assert store.list() == []

    def test_missing_and_relative_files_skipped(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        text = "![gone](/nonexistent/abs/path.png)\n![rel](relative/path.png)"
        assert image_artifacts.register_images(text, "ts-m", "chat-1") == []

    def test_non_raster_extension_skipped(self, store: ArtifactStore, tmp_path: Path) -> None:
        svg = tmp_path / "vec.svg"
        svg.write_text("<svg/>")
        assert image_artifacts.register_images(f"![v]({svg})", "ts-svg", "chat-1") == []

    def test_missing_message_ts_registers_nothing(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        img = tmp_path / "s.png"
        img.write_bytes(_png_bytes())
        assert image_artifacts.register_images(f"![a]({img})", "", "chat-1") == []

    def test_image_and_widget_slug_do_not_collide(self) -> None:
        from kiro_crew.widget_slug import derive_widget_slug

        assert image_artifacts._derive_image_slug("ts", 0) != derive_widget_slug("ts", 0)

    def test_off_loop_wrapper(self, store: ArtifactStore, tmp_path: Path) -> None:
        img = tmp_path / "s.png"
        img.write_bytes(_png_bytes())

        async def _run() -> list[str]:
            return await image_artifacts.register_images_off_loop(
                f"![a]({img})", "ts-async", "chat-1"
            )

        slugs = asyncio.run(_run())
        assert slugs == [image_artifacts._derive_image_slug("ts-async", 0)]


# ── chat_runner scheduler gate ──────────────────────────────────────────────


class TestSchedulerGate:
    """The restricted-session gate lives in the chat_runner scheduler."""

    def _fake_state(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(_background_tasks=set())

    def test_restricted_session_registers_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.dashboard import chat_runner

        called = False

        async def _fake(text, ts, key):  # noqa: ANN001
            nonlocal called
            called = True
            return []

        monkeypatch.setattr(chat_runner, "register_images_off_loop", _fake)
        monkeypatch.setattr(chat_runner, "register_widgets_off_loop", _fake)
        slot = types.SimpleNamespace(key="s1", is_restricted=True)

        async def _run() -> None:
            chat_runner._schedule_widget_registration(
                self._fake_state(), slot, "![a](/abs/x.png)", "ts"
            )
            await asyncio.sleep(0)

        asyncio.run(_run())
        assert called is False

    def test_local_image_schedules_registration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.dashboard import chat_runner

        calls: list = []

        async def _fake_images(text, ts, key):  # noqa: ANN001
            calls.append((text, ts, key))
            return []

        async def _noop(text, ts, key):  # noqa: ANN001
            return []

        monkeypatch.setattr(chat_runner, "register_images_off_loop", _fake_images)
        monkeypatch.setattr(chat_runner, "register_widgets_off_loop", _noop)
        state = self._fake_state()
        slot = types.SimpleNamespace(key="s1", is_restricted=False)

        async def _run() -> None:
            chat_runner._schedule_widget_registration(state, slot, "![a](/abs/x.png)", "ts-x")
            await asyncio.sleep(0)
            for t in list(state._background_tasks):
                await t

        asyncio.run(_run())
        assert calls == [("![a](/abs/x.png)", "ts-x", "s1")]


# ── Hardening regressions (review findings) ─────────────────────────────────


class TestAssetReadHardening:
    """The asset path must stay bounded, off-loop, and privately cached.

    Each assertion here stands in for a review finding: reverting any one of
    them reintroduces an availability or disclosure bug, not a style nit.
    """

    def test_oversize_image_is_never_fully_read_into_memory(
        self, store: ArtifactStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Registration must not allocate the whole file before the size check.

        A finalized message can name an arbitrarily large local file, so the
        read is bounded up front. ``Path.read_bytes`` is barred outright: it
        reads everything before any cap applies.
        """
        img = tmp_path / "huge.png"
        img.write_bytes(_png_bytes())

        def _explode(self: Path) -> bytes:  # pragma: no cover - must not run
            raise AssertionError("unbounded read_bytes() on a chat-referenced file")

        monkeypatch.setattr(Path, "read_bytes", _explode)
        captured: dict[str, object] = {}

        def _fake_read(raw: str, *, max_bytes: int | None = None, **kw: object) -> bytes:
            captured["max_bytes"] = max_bytes
            return _png_bytes()

        monkeypatch.setattr(image_artifacts, "safe_read_file_bytes_nolink", _fake_read)
        assert image_artifacts.register_images(f"![a]({img})", "ts-cap", "chat-1")
        # The cap handed to the reader is the store's own limit.
        assert captured["max_bytes"] == MAX_CONTENT_BYTES

    def test_rejected_read_is_skipped_not_registered(
        self, store: ArtifactStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refused read (hardlink / non-regular / sensitive) registers nothing."""
        img = tmp_path / "swapped.png"
        img.write_bytes(_png_bytes())
        monkeypatch.setattr(
            image_artifacts, "safe_read_file_bytes_nolink", lambda *a, **k: None
        )
        assert image_artifacts.register_images(f"![a]({img})", "ts-rej", "chat-1") == []
        assert store.list() == []

    def test_oversize_error_is_skipped_without_failing_the_turn(
        self, store: ArtifactStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An over-cap file is skipped; a lost thumbnail must not break the turn."""
        from kiro_crew.hooks import FileTooLargeError

        img = tmp_path / "big.png"
        img.write_bytes(_png_bytes())

        def _too_large(*a: object, **k: object) -> bytes:
            raise FileTooLargeError("too big")

        monkeypatch.setattr(image_artifacts, "safe_read_file_bytes_nolink", _too_large)
        assert image_artifacts.register_images(f"![a]({img})", "ts-big", "chat-1") == []
        assert store.list() == []

    def test_read_image_bytes_does_not_hold_the_store_lock(
        self, store: ArtifactStore
    ) -> None:
        """The byte read happens after the lock is released.

        Holding the store-wide lock across a multi-MiB read serializes every
        other artifact operation behind it.
        """
        art = store.create_image(
            name="Pic", image_bytes=_png_bytes(3, 3), mime="image/png"
        )
        seen: dict[str, bool] = {}
        original = store._read_image_asset_bytes

        def _spy(path: Path) -> bytes:
            # The lock must be free at read time: acquiring it here would
            # deadlock if it were still held by the caller.
            acquired = store._lock.acquire(blocking=False)
            seen["lock_free"] = acquired
            if acquired:
                store._lock.release()
            return original(path)

        store._read_image_asset_bytes = _spy  # type: ignore[method-assign]
        try:
            data, mime = store.read_image_bytes(art.slug)
        finally:
            store._read_image_asset_bytes = original  # type: ignore[method-assign]
        assert data == _png_bytes(3, 3)
        assert mime == "image/png"
        assert seen["lock_free"] is True

    @pytest.mark.skipif(
        sys.platform == "win32", reason="symlink creation needs elevation on Windows"
    )
    def test_swapped_sidecar_symlink_cannot_redirect_the_read(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        """A sidecar replaced by a link must not return the link target's bytes.

        The asset endpoint is reachable with nothing but a slug, so a
        resolve-then-open read would let a swapped sidecar hand back an
        arbitrary file. The read is descriptor-pinned and root-contained, so the
        swap is refused instead of followed.
        """
        art = store.create_image(
            name="Pic", image_bytes=_png_bytes(3, 3), mime="image/png"
        )
        secret = tmp_path / "outside-the-store.txt"
        secret.write_bytes(b"private-bytes")
        asset = store._artifact_dir(art.slug) / "asset.png"
        asset.unlink()
        asset.symlink_to(secret)

        with pytest.raises((ArtifactNotFoundError, ArtifactError)):
            store.read_image_bytes(art.slug)

    def test_failed_write_releases_the_slug_for_retry(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transient write failure must not permanently reserve the slug.

        Image slugs are deterministic, so a half-written directory would make
        every retry raise ArtifactAlreadyExistsError and lose the image for good.
        """
        boom = OSError("disk full")

        def _fail(*a: object, **k: object) -> None:
            raise boom

        monkeypatch.setattr(store, "_write_image_artifact", _fail)
        with pytest.raises(OSError):
            store.create_image(
                name="Pic", image_bytes=_png_bytes(), mime="image/png", slug="retry-me"
            )
        # Nothing left behind, so the same slug is usable again.
        monkeypatch.undo()
        art = store.create_image(
            name="Pic", image_bytes=_png_bytes(4, 4), mime="image/png", slug="retry-me"
        )
        assert art.slug == "retry-me"
        assert store.read_image_bytes("retry-me")[0] == _png_bytes(4, 4)

    def test_poisoned_mime_is_refused_on_read(
        self, store: ArtifactStore
    ) -> None:
        """A non-image mime in meta.json must never be served.

        ``create_image`` validates the mime, but meta.json is a file: anything
        that can write it could name ``text/html`` and turn the authenticated
        asset URL into same-origin script execution. The read path re-validates
        and derives the extension from the allowlist, so the stored ``ext`` is
        never trusted either.
        """
        import json

        art = store.create_image(
            name="Pic", image_bytes=_png_bytes(2, 2), mime="image/png"
        )
        meta_path = store._artifact_dir(art.slug) / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta["image"]["mime"] = "text/html"
        meta["image"]["ext"] = "html"
        meta_path.write_text(json.dumps(meta))
        (store._artifact_dir(art.slug) / "asset.html").write_bytes(
            b"<script>alert(1)</script>"
        )

        with pytest.raises(ArtifactNotFoundError):
            store.read_image_bytes(art.slug)

    def test_asset_response_is_private_and_read_off_the_loop(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Authenticated bytes are ``private``, and the read is offloaded.

        ``public`` would let a shared caching proxy hand these bytes to an
        unauthenticated requester, and a synchronous read would stall the
        gateway's single event loop for the whole file.
        """
        from kiro_crew.dashboard.handlers import artifacts as handlers

        art = store.create_image(
            name="Pic", image_bytes=_png_bytes(2, 2), mime="image/png"
        )
        monkeypatch.setattr(handlers, "get_default_store", lambda: store)
        offloaded: dict[str, bool] = {"used": False}
        real_to_thread = asyncio.to_thread

        async def _spy_to_thread(fn, /, *args, **kw):  # type: ignore[no-untyped-def]
            offloaded["used"] = True
            return await real_to_thread(fn, *args, **kw)

        monkeypatch.setattr(handlers.asyncio, "to_thread", _spy_to_thread)
        request = types.SimpleNamespace(match_info={"slug": art.slug})
        resp = asyncio.run(handlers.api_artifact_asset(request))  # type: ignore[arg-type]

        assert resp.status == 200
        assert resp.body == _png_bytes(2, 2)
        cache = resp.headers["Cache-Control"]
        assert "private" in cache
        assert "public" not in cache
        assert offloaded["used"] is True


class TestLinkedAncestorGate:
    """On Windows, a registration destination beneath a linked ANCESTOR must
    be refused BEFORE the first filesystem probe -- both is_file() and
    is_sensitive_path's resolved candidate forms traverse every ancestor, so
    the probe itself would open the SMB connection the lexical UNC screen in
    local_destination exists to prevent (#5962). Mirrors the guard on the
    upload-side consumer (_inspect in outbound_files)."""

    def _windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Patch ONLY the module's view of os -- patching the global os.name
        # would make pathlib dispatch WindowsPath on a POSIX test host.
        import os as _os

        monkeypatch.setattr(
            image_artifacts, "os", types.SimpleNamespace(name="nt", path=_os.path)
        )

    def test_linked_ancestor_is_refused_before_any_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ordering IS the property: both downstream probes are wired to
        explode, so a regression that probes first fails loudly."""
        f = tmp_path / "shot.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        self._windows(monkeypatch)
        monkeypatch.setattr(
            image_artifacts, "first_linked_ancestor", lambda _p: str(tmp_path)
        )

        def _boom_is_file(self: Path) -> bool:  # pragma: no cover
            raise AssertionError("is_file ran before the ancestor walk")

        def _boom_sensitive(_p: str) -> bool:  # pragma: no cover
            raise AssertionError("is_sensitive_path ran before the ancestor walk")

        monkeypatch.setattr(Path, "is_file", _boom_is_file)
        monkeypatch.setattr(image_artifacts, "is_sensitive_path", _boom_sensitive)
        assert image_artifacts._local_file(str(f)) is None

    def test_bypassing_the_guard_restores_the_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mutation check: with the walk reporting no link, the same file
        resolves again -- the refusal above is attributable to the guard."""
        f = tmp_path / "shot.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        self._windows(monkeypatch)
        monkeypatch.setattr(image_artifacts, "first_linked_ancestor", lambda _p: None)
        assert image_artifacts._local_file(str(f)) == f

    def test_the_walk_is_not_consulted_on_posix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POSIX behavior is pinned unchanged: the walk must not even run."""
        import os as _os

        if _os.name == "nt":
            pytest.skip("gate is active on Windows by design")

        def _boom(_p: object) -> None:  # pragma: no cover
            raise AssertionError("ancestor walk ran on POSIX")

        monkeypatch.setattr(image_artifacts, "first_linked_ancestor", _boom)
        f = tmp_path / "shot.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        assert image_artifacts._local_file(str(f)) == f

    def test_a_leaf_link_is_refused_before_any_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The walk deliberately excludes the leaf, so the leaf gets its own
        junction-aware check -- is_file() FOLLOWS a final-component link."""
        f = tmp_path / "shot.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        self._windows(monkeypatch)
        monkeypatch.setattr(image_artifacts, "first_linked_ancestor", lambda _p: None)
        monkeypatch.setattr(image_artifacts, "is_link_or_junction", lambda _p: True)

        def _boom_is_file(self: Path) -> bool:  # pragma: no cover
            raise AssertionError("is_file ran before the leaf link check")

        monkeypatch.setattr(Path, "is_file", _boom_is_file)
        assert image_artifacts._local_file(str(f)) is None
