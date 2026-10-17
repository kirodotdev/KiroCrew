"""WhatsApp outbound upload planning: what gets sent, and what the user is told."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from kiro_crew.messaging.outbound_files import (
    REASON_NOT_RASTER,
    REASON_OVER_FILE_BYTES,
    REASON_OVER_FILE_CAP,
    REASON_SENSITIVE,
    Rejection,
)
from kiro_crew.whatsapp import files as wa_files
from kiro_crew.whatsapp.files import (
    REASON_OVER_PIXEL_BUDGET,
    REASON_UNDECODABLE,
    WHATSAPP_MAX_FILE_BYTES,
    WHATSAPP_MAX_REJECTION_LINES,
    WHATSAPP_MAX_TOTAL_UPLOAD_BYTES,
    WHATSAPP_MAX_UPLOAD_FILES,
    plan_uploads,
    plan_uploads_off_loop,
    rejection_note,
    whatsapp_limits,
)

#: A real 2x2 PNG: the plan decodes what it accepts, so a magic-bytes-only stub
#: would be refused and prove nothing.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg=="
)

#: PNG magic with nothing decodable behind it: what a truncated write looks like.
_TRUNCATED_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _write(directory: Path, name: str, body: bytes = _PNG) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


class TestLimits:
    def test_the_owning_constants_are_the_ones_handed_to_extraction(self) -> None:
        limits = whatsapp_limits()
        assert limits.max_files == WHATSAPP_MAX_UPLOAD_FILES
        assert limits.max_file_bytes == WHATSAPP_MAX_FILE_BYTES
        assert limits.max_total_bytes == WHATSAPP_MAX_TOTAL_UPLOAD_BYTES

    def test_the_total_budget_binds_before_the_per_file_ceiling_times_the_cap(self) -> None:
        """Otherwise the aggregate bound is decorative and the real peak is 4x8 MiB."""
        assert WHATSAPP_MAX_TOTAL_UPLOAD_BYTES < (
            WHATSAPP_MAX_UPLOAD_FILES * WHATSAPP_MAX_FILE_BYTES
        )

    def test_a_patched_ceiling_reaches_the_limits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wa_files, "WHATSAPP_MAX_FILE_BYTES", 64)
        assert whatsapp_limits().max_file_bytes == 64


class TestPlanning:
    def test_a_raster_is_planned_and_its_markup_leaves_the_text(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "chart.png")
        plan = plan_uploads(f"Here it is:\n\n![the chart]({path})\n", within_root=str(tmp_path))
        assert [file.path for file in plan.files] == [str(path)]
        assert plan.files[0].data == _PNG
        assert plan.files[0].mime == "image/png"
        assert plan.files[0].alt == "the chart"
        assert str(path) not in plan.text
        assert plan.text.strip() == "Here it is:"
        assert plan.rejections == []

    def test_text_with_no_references_comes_back_byte_identical(self, tmp_path: Path) -> None:
        text = "no pictures here, just *prose* and a `/tmp/path.png` mention\n\n"
        plan = plan_uploads(text, within_root=str(tmp_path))
        assert plan.text == text
        assert plan.files == []
        assert plan.rejections == []

    def test_a_script_named_png_is_refused_by_its_leading_bytes(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "chart.png", b"#!/bin/sh\necho pwned\n")
        plan = plan_uploads(f"![c]({path})", within_root=str(tmp_path))
        assert plan.files == []
        assert [item.reason for item in plan.rejections] == [REASON_NOT_RASTER]
        # A refused reference keeps its markup, so the path stays readable.
        assert str(path) in plan.text

    def test_an_oversize_file_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wa_files, "WHATSAPP_MAX_FILE_BYTES", len(_PNG) - 1)
        path = _write(tmp_path, "chart.png")
        plan = plan_uploads(f"![c]({path})", within_root=str(tmp_path))
        assert plan.files == []
        assert [item.reason for item in plan.rejections] == [REASON_OVER_FILE_BYTES]

    def test_a_path_outside_the_approved_root_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        outside = _write(tmp_path / "elsewhere", "chart.png")
        plan = plan_uploads(f"![c]({outside})", within_root=str(root))
        assert plan.files == []
        assert [item.reason for item in plan.rejections] == [REASON_SENSITIVE]

    def test_a_relative_reference_is_refused_and_not_resolved(self, tmp_path: Path) -> None:
        plan = plan_uploads("![c](chart.png)", within_root=str(tmp_path))
        assert plan.files == []
        assert len(plan.rejections) == 1

    def test_a_reference_inside_a_code_fence_is_documentation(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "chart.png")
        text = f"```\n![c]({path})\n```"
        plan = plan_uploads(text, within_root=str(tmp_path))
        assert plan.files == []
        assert plan.rejections == []
        assert plan.text == text

    def test_references_past_the_file_cap_are_reported_not_dropped_silently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wa_files, "WHATSAPP_MAX_UPLOAD_FILES", 1)
        first = _write(tmp_path, "a.png")
        second = _write(tmp_path, "b.png")
        plan = plan_uploads(
            f"![a]({first})\n\n![b]({second})",
            within_root=str(tmp_path),
        )
        assert [file.path for file in plan.files] == [str(first)]
        assert [item.reason for item in plan.rejections] == [REASON_OVER_FILE_CAP]

    def test_planning_never_raises_and_degrades_to_the_text_as_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("filesystem gone")

        monkeypatch.setattr(wa_files, "extract_local_refs", boom)
        plan = plan_uploads("![c](/tmp/chart.png)", within_root=str(tmp_path))
        assert plan == wa_files.UploadPlan(text="![c](/tmp/chart.png)")


class TestDecodeScreen:
    def test_a_raster_the_transport_cannot_decode_is_refused(self, tmp_path: Path) -> None:
        """neonize opens the bytes with Pillow to build a thumbnail before uploading."""
        path = _write(tmp_path, "chart.png", _TRUNCATED_PNG)
        plan = plan_uploads(f"![c]({path})", within_root=str(tmp_path))
        assert plan.files == []
        assert [item.reason for item in plan.rejections] == [REASON_UNDECODABLE]
        assert plan.rejections[0].dest == str(path)

    def test_an_image_past_the_pixel_ceiling_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wa_files, "WHATSAPP_MAX_IMAGE_PIXELS", 3)  # the fixture is 2x2
        path = _write(tmp_path, "chart.png")
        plan = plan_uploads(f"![c]({path})", within_root=str(tmp_path))
        assert plan.files == []
        assert [item.reason for item in plan.rejections] == [REASON_OVER_PIXEL_BUDGET]
        assert "2x2" in plan.rejections[0].detail

    def test_the_screen_passes_when_pillow_cannot_be_loaded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is verifiable then, and refusing every picture helps nobody."""
        monkeypatch.setattr(wa_files, "pil_available", lambda: False)
        path = _write(tmp_path, "chart.png", _TRUNCATED_PNG)
        plan = plan_uploads(f"![c]({path})", within_root=str(tmp_path))
        assert [file.path for file in plan.files] == [str(path)]
        assert plan.rejections == []


class TestOffLoop:
    @pytest.mark.asyncio
    async def test_the_async_form_plans_the_same_thing(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "chart.png")
        text = f"look\n\n![c]({path})"
        assert await plan_uploads_off_loop(text, within_root=str(tmp_path)) == plan_uploads(
            text, within_root=str(tmp_path)
        )

    @pytest.mark.asyncio
    async def test_the_async_form_also_degrades_instead_of_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(*args: object, **kwargs: object) -> None:
            raise OSError("filesystem gone")

        monkeypatch.setattr(wa_files, "extract_local_refs_off_loop", boom)
        plan = await plan_uploads_off_loop("![c](/tmp/chart.png)", within_root=str(tmp_path))
        assert plan.files == []
        assert plan.text == "![c](/tmp/chart.png)"


class TestRejectionNote:
    def test_no_rejections_is_no_note(self) -> None:
        assert rejection_note([]) == ""

    def test_every_refusal_is_named_with_its_reason(self) -> None:
        rejections = [
            Rejection("/tmp/a.png", REASON_NOT_RASTER, "not a PNG, JPEG, GIF, WebP or BMP image"),
            Rejection("/tmp/b.png", REASON_OVER_FILE_BYTES, "larger than the per-file limit"),
            Rejection("/tmp/c.png", REASON_SENSITIVE, "outside the approved workspace"),
        ]
        note = rejection_note(rejections)
        assert len(rejections) == WHATSAPP_MAX_REJECTION_LINES
        for rejection in rejections:
            assert rejection.dest in note
            assert rejection.detail in note

    def test_a_message_level_refusal_needs_no_path(self) -> None:
        note = rejection_note([Rejection("", REASON_OVER_FILE_CAP, "2 more file(s) not sent")])
        assert "2 more file(s) not sent" in note
        assert "`" not in note

    def test_the_note_stays_short_and_counts_the_rest(self) -> None:
        rejections = [
            Rejection(f"/tmp/{index}.png", REASON_NOT_RASTER, "not an image") for index in range(9)
        ]
        note = rejection_note(rejections)
        assert "/tmp/8.png" not in note
        assert f"and {9 - WHATSAPP_MAX_REJECTION_LINES} more" in note

    def test_a_path_is_wrapped_so_the_dialect_leaves_it_alone(self) -> None:
        note = rejection_note([Rejection("/tmp/my_chart_1.png", REASON_NOT_RASTER, "not an image")])
        assert "`/tmp/my_chart_1.png`" in note

    def test_a_backtick_in_a_path_cannot_break_out_of_the_code_span(self) -> None:
        note = rejection_note([Rejection("/tmp/a`b.png", REASON_NOT_RASTER, "not an image")])
        assert note.count("`") == 2

    def test_a_long_path_keeps_the_filename_end(self) -> None:
        dest = f"/tmp/{'d' * 200}/chart-with-a-long-name.png"
        note = rejection_note([Rejection(dest, REASON_NOT_RASTER, "not an image")])
        assert "chart-with-a-long-name.png" in note
        assert len(max(note.split("\n"), key=len)) < 120


def test_upload_failed_is_a_named_reason():
    """A failed upload must surface as a rejection, not just a log line.

    Extraction removes the markdown reference from the delivered text before the
    upload runs, so a silent wire failure leaves the reader with neither the
    picture nor the path nor any hint one existed.
    """
    from kiro_crew.messaging.outbound_files import Rejection
    from kiro_crew.whatsapp.files import REASON_UPLOAD_FAILED, rejection_note

    note = rejection_note([Rejection("/tmp/chart.png", REASON_UPLOAD_FAILED, "")])
    assert note, "a failed upload must produce a user-visible note"
    assert "chart.png" in note
