"""Tests for repairing kiro-cli transcripts wedged by an oversized image.

The module rewrites stored image blocks in a ``.jsonl`` transcript so no image
exceeds the inline-image edge cap. These tests generate REAL PNG bytes with
Pillow (the fixtures must decode), build records at both observed nesting depths
(a ``Prompt`` attachment directly under ``content`` and a ``ToolResults`` image
one ``toolResult`` level deeper), and assert the structural invariants a repair
must preserve. Pillow is a hard dependency here, as it is in the neighbouring
``test_web_verify_downscale`` / ``test_image_primitives`` suites, so it is
imported at module level with no skipif guard.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest
from PIL import Image

from kiro_crew import session_image_repair as sir


def _png_bytes(size: tuple[int, int], colour: str = "red") -> list[int]:
    """A real, decodable PNG at *size*, as the int list a transcript stores."""
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="PNG")
    return list(buf.getvalue())


def _image_block(size: tuple[int, int]) -> dict:
    """The stored image-block shape verified against real transcripts."""
    return {
        "kind": "image",
        "data": {"format": "png", "source": {"kind": "bytes", "data": _png_bytes(size)}},
    }


def _prompt_record(size: tuple[int, int]) -> dict:
    """A Prompt record: the image block sits in the record's own content array.

    Path verified against a real transcript: ``.data.content[i]``.
    """
    return {
        "version": 1,
        "kind": "Prompt",
        "data": {
            "message_id": "m1",
            "content": [{"kind": "text", "data": "hi"}, _image_block(size)],
        },
    }


def _tool_result_record(size: tuple[int, int]) -> dict:
    """A ToolResults record: the image nests one ``toolResult`` level deeper.

    Path verified against a real transcript:
    ``.data.content[i].data.content[j]``, where the outer block carries
    ``kind: "toolResult"`` and its ``data`` holds ``toolUseId`` / ``content`` /
    ``status``.
    """
    return {
        "version": 1,
        "kind": "ToolResults",
        "data": {
            "message_id": "m2",
            "content": [
                {
                    "kind": "toolResult",
                    "data": {
                        "toolUseId": "toolu_x",
                        "content": [_image_block(size)],
                        "status": "success",
                    },
                }
            ],
        },
    }


def _compaction_record(size: tuple[int, int]) -> dict:
    """A Compaction record: blocks hang off ``content`` on a snapshot message.

    Path verified against real transcripts:
    ``.data.messages_snapshot[i].content[j]``. Note the block array is at a
    top-level ``content`` here, NOT under ``data`` -- a traversal that only
    knows the ``data.content`` spelling misses every one of these, and
    Compaction snapshots hold 42% of all stored image blocks.
    """
    return {
        "version": 1,
        "kind": "Compaction",
        "data": {
            "messages_snapshot": [
                {"content": [{"kind": "text", "data": "summary"}, _image_block(size)]}
            ]
        },
    }


def _compaction_record_with_nested_tool_result(size: tuple[int, int]) -> dict:
    """The deepest observed shape: a tool result inside a compaction snapshot.

    Path verified against real transcripts:
    ``.data.messages_snapshot[i].content[j].data.content[k]``.
    """
    return {
        "version": 1,
        "kind": "Compaction",
        "data": {
            "messages_snapshot": [
                {
                    "content": [
                        {
                            "kind": "toolResult",
                            "data": {
                                "toolUseId": "toolu_z",
                                "content": [_image_block(size)],
                                "status": "success",
                            },
                        }
                    ]
                }
            ]
        },
    }


def _text_block_carrying_an_image_shaped_payload(size: tuple[int, int]) -> dict:
    """A ToolResults record whose TEXT block payload merely looks like an image.

    This is application data a tool happened to return, not transcript media. A
    traversal that walks every dict would rewrite the bytes inside it; the
    anchored traversal must leave the record untouched.
    """
    return {
        "version": 1,
        "kind": "ToolResults",
        "data": {
            "message_id": "m3",
            "content": [
                {
                    "kind": "toolResult",
                    "data": {
                        "toolUseId": "toolu_y",
                        "content": [{"kind": "text", "data": _image_block(size)}],
                        "status": "success",
                    },
                }
            ],
        },
    }


def _write_transcript(path: Path, records: list) -> None:
    """Write one JSON record per line, matching kiro-cli's compact spelling.

    ``newline=""`` is required, not cosmetic: without it Python translates each
    ``\\n`` to ``\\r\\n`` on Windows, so these fixtures would assert
    platform-dependent bytes and the byte-exactness tests would fail there while
    passing on POSIX. Line-ending BEHAVIOUR is exercised deliberately by the
    CRLF test instead of leaking in through the helper.

    Members that are already strings are written verbatim (used for the
    invalid-JSON and non-image passthrough lines).
    """
    with path.open("w", encoding="utf-8", newline="") as handle:
        for rec in records:
            if isinstance(rec, str):
                handle.write(rec)
            else:
                handle.write(json.dumps(rec, separators=(",", ":")))
            handle.write("\n")


class TestDriftTripwire:
    """A format change must surface as a warning, never as a clean report."""

    def test_an_unknown_nesting_is_WARNED_about_and_not_repaired(self, tmp_path):
        """The failure mode this PR nearly shipped: a shape the anchor cannot see.

        Simulates a future kiro-cli record whose blocks hang somewhere the
        anchored traversal does not know. The image must NOT be rewritten (the
        anchor is what decides that) but the report must refuse to look clean.
        """
        p = tmp_path / "s.jsonl"
        unknown = {
            "version": 1,
            "kind": "SomeFutureRecord",
            "data": {"turns": [{"blocks": [_image_block((3000, 1200))]}]},
        }
        _write_transcript(p, [unknown])
        before = p.read_bytes()

        report, lines = sir.scan_file(p)

        assert report.images == 0  # the anchor found nothing, correctly
        assert report.findings == []
        assert lines is None  # and nothing is rewritten on an unknown shape
        assert report.unanchored_images == 1  # but the divergence is recorded
        assert report.unanchored_lines == [0]
        assert p.read_bytes() == before

        rendered = sir._format_report(report, applied=False)
        assert "all within cap" in rendered
        assert "WARNING" in rendered  # the clean line alone would be a lie
        assert "NOT repaired" in rendered

    def test_the_warning_also_fires_on_a_harmless_lookalike(self, tmp_path):
        """A tool payload that looks like a block warns too, and that is fine.

        The check is deliberately allowed to be imprecise in this direction only:
        a false alarm costs a line of output, a silent miss costs the session.
        """
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_text_block_carrying_an_image_shaped_payload((3000, 1200))])

        report, lines = sir.scan_file(p)

        assert report.images == 0
        assert lines is None  # still never rewritten
        assert report.unanchored_images == 1

    def test_a_spaced_serialization_is_still_found_and_repaired(self, tmp_path):
        """A serializer that emits `"kind": "image"` must not slip the prefilter.

        The prefilter gates on the loose token for exactly this: the strict
        `"kind":"image"` spelling would skip the whole line, and the transcript
        would be reported clean while an oversized image sat in it.
        """
        p = tmp_path / "s.jsonl"
        spaced = json.dumps(_prompt_record((3000, 1200)), indent=None, separators=(", ", ": "))
        p.write_bytes(spaced.encode("utf-8") + b"\n")

        report, lines = sir.scan_file(p)

        assert report.images == 1
        assert [f.action for f in report.findings] == ["resize"]
        assert lines is not None
        assert report.unanchored_images == 0  # anchored reached it; no drift

    def test_a_deeply_nested_tool_payload_does_not_crash_the_repair(self, tmp_path):
        """An arbitrarily nested tool payload must not abort the whole repair.

        The tripwire walk descends into TOOL PAYLOADS, whose nesting is not this
        module's data and has no natural bound, so trusting the interpreter's
        recursion limit would let one odd record kill the repair of a transcript
        the operator needs repaired. Depth-bounded instead -- and the truncation
        is REPORTED, because an unverified subtree must not read as clean.
        """
        p = tmp_path / "s.jsonl"
        # Must carry the QUOTED "image" token, or the byte prefilter skips the
        # line and it is never parsed or walked at all -- which is itself the
        # first bound on how reachable this is. A tool payload with a
        # {"type": "image"} field is the plausible shape.
        deep: dict = {"kind": "text", "data": {"type": "image", "note": "x"}}
        for _ in range(sir._MAX_WALK_DEPTH + 60):
            deep = {"nested": deep}
        record = {
            "version": 1,
            "kind": "ToolResults",
            "data": {
                "content": [
                    {
                        "kind": "toolResult",
                        "data": {"toolUseId": "t", "content": [deep], "status": "ok"},
                    }
                ]
            },
        }
        _write_transcript(p, [record, _prompt_record((3000, 1200))])

        report, lines = sir.scan_file(p)  # must not raise RecursionError

        assert report.walk_truncated is True
        rendered = sir._format_report(report, applied=False)
        assert "could NOT be fully checked" in rendered
        assert "incomplete" in rendered
        # The real repair on the OTHER line still happened.
        assert lines is not None
        assert report.rewritten_lines == 1

    def test_unparseable_deeply_nested_json_is_passed_through_not_fatal(self, tmp_path):
        """json.loads itself raises RecursionError on deep input, not just the walk.

        The parse sits on another tool's file, so the nesting is not ours to
        bound; uncaught, one line would abort the repair. Such a line is treated
        like any other unparseable line: passed through byte-for-byte.
        """
        p = tmp_path / "s.jsonl"
        blob = b'{"image":' + b"[" * 6000 + b"]" * 6000 + b"}"
        _write_transcript(p, [_prompt_record((3000, 1200))])
        with p.open("ab") as handle:
            handle.write(blob + b"\n")

        report, lines = sir.scan_file(p)  # must not raise

        assert lines is not None
        assert blob in lines  # byte-for-byte passthrough
        assert report.rewritten_lines == 1  # the real repair still landed

    def test_a_known_transcript_produces_no_warning(self, tmp_path):
        """All four pinned shapes together must not trip the tripwire."""
        p = tmp_path / "s.jsonl"
        _write_transcript(
            p,
            [
                _prompt_record((3000, 1200)),
                _tool_result_record((3000, 1200)),
                _compaction_record((3000, 1200)),
                _compaction_record_with_nested_tool_result((3000, 1200)),
            ],
        )

        report, _lines = sir.scan_file(p)

        assert report.images == 4
        assert report.unanchored_images == 0
        assert "WARNING" not in sir._format_report(report, applied=False)


class TestCLI:
    """The CLI is the whole user-facing surface, so its contract is pinned here.

    Exercised through ``main(argv)`` rather than a subprocess so exit codes and
    printed output are asserted directly.
    """

    def test_a_bare_invocation_is_a_dry_run_that_reports_and_changes_nothing(
        self, tmp_path, capsys
    ):
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])
        before = p.read_bytes()

        code = sir.main([str(p)])

        assert code == 0
        out = capsys.readouterr().out
        assert "1 image(s), 1 over cap, would repair 1 record(s)" in out
        assert "3000x1200 -> 2000x800" in out
        assert p.read_bytes() == before  # no --apply, no write

    def test_apply_writes_and_reports_the_backup(self, tmp_path, capsys):
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])

        code = sir.main(["--apply", str(p)])

        assert code == 0
        out = capsys.readouterr().out
        assert "backup at s.jsonl.pre-image-repair.bak" in out
        assert "repaired 1 record(s)" in out
        assert p.with_suffix(".jsonl.pre-image-repair.bak").exists()
        report_after, _ = sir.scan_file(p)
        assert report_after.oversized == []

    def test_a_within_cap_transcript_reports_clean_and_is_not_rewritten(self, tmp_path, capsys):
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((100, 80))])
        before = p.read_bytes()

        assert sir.main(["--apply", str(p)]) == 0

        assert "1 image(s), all within cap" in capsys.readouterr().out
        assert p.read_bytes() == before

    def test_an_unreadable_path_reports_ERROR_and_exits_nonzero(self, tmp_path, capsys):
        assert sir.main([str(tmp_path / "missing.jsonl")]) == 1
        assert "ERROR" in capsys.readouterr().out

    def test_a_live_transcript_is_REFUSED_with_a_nonzero_exit(self, tmp_path, capsys, monkeypatch):
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])
        p.with_suffix(".lock").write_text(json.dumps({"pid": 4242}), encoding="utf-8")
        monkeypatch.setattr(sir.platform_compat, "pid_exists", lambda pid: True)
        before = p.read_bytes()

        code = sir.main(["--apply", str(p)])

        assert code == 1
        assert "REFUSED" in capsys.readouterr().err
        assert p.read_bytes() == before

    def test_allow_live_lets_the_repair_through(self, tmp_path, monkeypatch):
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])
        p.with_suffix(".lock").write_text(json.dumps({"pid": 4242}), encoding="utf-8")
        monkeypatch.setattr(sir.platform_compat, "pid_exists", lambda pid: True)

        assert sir.main(["--apply", "--allow-live", str(p)]) == 0

        report_after, _ = sir.scan_file(p)
        assert report_after.oversized == []

    def test_a_moved_transcript_is_REFUSED_with_a_nonzero_exit(self, tmp_path, capsys, monkeypatch):
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])

        real_scan = sir.scan_file

        def scan_then_append(path, **kw):
            result = real_scan(path, **kw)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"version": 1, "kind": "Prompt", "data": {}}) + "\n")
            return result

        monkeypatch.setattr(sir, "scan_file", scan_then_append)

        assert sir.main(["--apply", str(p)]) == 1
        assert "REFUSED" in capsys.readouterr().err

    def test_several_paths_are_each_reported(self, tmp_path, capsys):
        a = tmp_path / "a.jsonl"
        b = tmp_path / "b.jsonl"
        _write_transcript(a, [_prompt_record((3000, 1200))])
        _write_transcript(b, [_prompt_record((100, 80))])

        assert sir.main([str(a), str(b)]) == 0

        out = capsys.readouterr().out
        assert "a.jsonl: 1 image(s), 1 over cap" in out
        assert "b.jsonl: 1 image(s), all within cap" in out

    def test_a_dropped_block_is_reported_as_DROPPED(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(sir, "downscale_image_block", lambda *a, **k: None)
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])

        assert sir.main([str(p)]) == 0

        assert "3000x1200 -> DROPPED" in capsys.readouterr().out


class TestScanFile:
    def test_within_cap_image_is_kept_byte_identical(self, tmp_path):
        """(1) An image already within the cap is reported keep and untouched."""
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((100, 80))])
        before = p.read_bytes()

        report, lines = sir.scan_file(p)

        assert report.images == 1
        assert [f.action for f in report.findings] == ["keep"]
        assert lines is None  # nothing to rewrite
        assert p.read_bytes() == before

    def test_oversized_image_is_resized_within_cap_keeping_aspect(self, tmp_path):
        """(2) An oversized image is resized so both edges <= cap, aspect kept."""
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1500))])

        report, lines = sir.scan_file(p)

        assert lines is not None
        finding = report.findings[0]
        assert finding.action == "resize"
        assert finding.new_size is not None
        new_w, new_h = finding.new_size
        assert max(new_w, new_h) <= sir.MAX_IMAGE_EDGE_PX
        # 2:1 source aspect preserved (2000x1000).
        assert new_w == 2000 and new_h == 1000
        # And the rewritten bytes actually decode at the reported size.
        rec = json.loads(lines[0])
        block = next(sir._iter_image_blocks(rec))
        with Image.open(io.BytesIO(bytes(block["data"]["source"]["data"]))) as img:
            assert img.size == (new_w, new_h)

    def test_repair_preserves_record_kinds_and_line_count(self, tmp_path):
        """(3) Record-kind sequence and line count are unchanged by a repair."""
        p = tmp_path / "s.jsonl"
        records = [
            _prompt_record((100, 100)),
            _tool_result_record((3000, 1200)),
            {"kind": "Response", "content": [{"kind": "text", "data": "ok"}]},
        ]
        _write_transcript(p, records)

        _report, lines = sir.scan_file(p)

        assert lines is not None
        assert len(lines) == len(records)
        assert [json.loads(x)["kind"] for x in lines] == ["Prompt", "ToolResults", "Response"]

    def test_non_image_and_invalid_json_pass_through_untouched(self, tmp_path):
        """(4) A non-image record and an invalid-JSON line are left verbatim."""
        p = tmp_path / "s.jsonl"
        non_image = json.dumps(
            {"kind": "Response", "content": [{"kind": "text", "data": "hello"}]},
            separators=(",", ":"),
        )
        broken = '{"kind":"image", this is not valid json'
        _write_transcript(p, [non_image, broken, _prompt_record((3000, 1200))])

        _report, lines = sir.scan_file(p)

        assert lines is not None  # the third line is dirty
        assert lines[0] == non_image.encode("utf-8")
        assert lines[1] == broken.encode("utf-8")

    def test_invalid_utf8_in_an_unrelated_line_survives_a_repair(self, tmp_path):
        """A malformed byte sequence must not be rewritten into U+FFFD.

        Every line is written back on apply, so a text-mode read with
        errors="replace" would irreversibly destroy any byte sequence that is not
        valid UTF-8 -- in a line this tool has no business touching. Decoding
        strictly instead would refuse the whole transcript and leave the session
        dead. Reading bytes and only decoding the lines actually parsed keeps
        both the malformed line and the repair.
        """
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])
        bad = b'{"kind":"text","data":"\xff\xfe not utf-8"}'
        with p.open("ab") as handle:
            handle.write(bad + b"\n")

        report, lines = sir.scan_file(p)

        assert lines is not None
        assert bad in lines  # byte-for-byte, no replacement character
        assert report.rewritten_lines == 1  # and the real repair still happened

        sir.apply_repair(p, lines, backup=False, expect=report.source_stat)

        assert bad in p.read_bytes()
        assert b"\xef\xbf\xbd" not in p.read_bytes()  # no U+FFFD anywhere

    def test_a_CRLF_transcript_keeps_CRLF_on_the_repaired_line_too(self, tmp_path):
        """Line endings must survive, including on the line that was rewritten.

        Surfaced by a Windows CI shard, which is the only place it could be:
        kiro-cli may write CRLF there, and reading bytes while stripping only
        ``\\n`` leaves the ``\\r`` attached. Passthrough lines were already
        byte-exact, but a rewritten record would have been emitted with a bare
        ``\\n`` -- leaving the repaired line as the single odd one out in an
        otherwise CRLF file.
        """
        p = tmp_path / "s.jsonl"
        keep = json.dumps({"kind": "Response", "data": {}}, separators=(",", ":"))
        record = json.dumps(_prompt_record((3000, 1200)), separators=(",", ":"))
        p.write_bytes(keep.encode() + b"\r\n" + record.encode() + b"\r\n")

        report, lines = sir.scan_file(p)

        assert lines is not None
        assert report.rewritten_lines == 1
        assert all(line.endswith(b"\r") for line in lines)

        sir.apply_repair(p, lines, backup=False, expect=report.source_stat)

        written = p.read_bytes()
        assert written.count(b"\r\n") == 2
        assert b"}\n" not in written.replace(b"\r\n", b"")  # no bare-LF line

    def test_non_bytes_source_kind_is_skipped(self, tmp_path):
        """(5) A source whose kind is not 'bytes' is skipped, not coerced.

        The payload is a real oversized-PNG byte list so that the ONLY thing
        stopping it from being decoded and resized is the source-kind guard: if
        that guard were dropped, ``_block_bytes`` would happily coerce the list.
        """
        p = tmp_path / "s.jsonl"
        rec = {
            "kind": "Prompt",
            "content": [
                {
                    "kind": "image",
                    "data": {
                        "format": "png",
                        "source": {"kind": "path", "data": _png_bytes((3000, 1200))},
                    },
                }
            ],
        }
        _write_transcript(p, [rec])
        before = p.read_bytes()

        report, lines = sir.scan_file(p)

        assert report.images == 0  # not counted as a decodable image payload
        assert report.findings == []
        assert lines is None
        assert p.read_bytes() == before

    def test_uncappable_block_becomes_dropped_text_placeholder(self, tmp_path, monkeypatch):
        """(6) When downscale returns None the block becomes a DROPPED text block."""
        monkeypatch.setattr(sir, "downscale_image_block", lambda *a, **k: None)
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])

        report, lines = sir.scan_file(p)

        assert lines is not None
        assert [f.action for f in report.findings] == ["drop"]
        rec = json.loads(lines[0])
        block = rec["data"]["content"][1]
        assert block["kind"] == "text"
        marker = sir.DROPPED_PLACEHOLDER.format(max_edge=sir.MAX_IMAGE_EDGE_PX)
        assert block["data"] == marker


class TestAnchoredTraversal:
    """The walk must not wander out of the block arrays into tool payloads."""

    def test_image_shaped_payload_inside_a_text_block_is_left_alone(self, tmp_path):
        """Application data that merely looks like an image block is not media.

        A tool can legitimately return a JSON document containing a dict shaped
        exactly like a stored image block. Rewriting its bytes would corrupt
        another tool's data file while reporting a successful repair, so the
        record must come back untouched even though the payload is over cap.
        """
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_text_block_carrying_an_image_shaped_payload((3000, 1200))])
        before = p.read_bytes()

        report, lines = sir.scan_file(p)

        assert report.images == 0
        assert report.findings == []
        assert lines is None
        assert p.read_bytes() == before

    def test_a_real_nested_tool_result_image_is_still_repaired(self, tmp_path):
        """The anchored walk must still reach the depth real transcripts use.

        Guards the other direction of the same change: narrowing the traversal
        must not stop it finding an image one ``toolResult`` level down, which
        is where 18 of 20 real oversized images were found.
        """
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_tool_result_record((3000, 1200))])

        report, lines = sir.scan_file(p)

        assert report.images == 1
        assert [f.action for f in report.findings] == ["resize"]
        assert lines is not None

    @pytest.mark.parametrize(
        "builder",
        [_compaction_record, _compaction_record_with_nested_tool_result],
        ids=["snapshot-content", "snapshot-nested-toolresult"],
    )
    def test_compaction_snapshot_images_are_found(self, tmp_path, builder):
        """Compaction snapshots carry 42% of stored images and must be reached.

        These are the shapes that first exposed the traversal as too narrow: the
        block array sits at a top-level ``content`` on each snapshot message
        rather than under ``data``, so a data.content-only walk silently reported
        a clean transcript while an over-cap image stayed live -- success
        returned, nothing changed, session still wedged.
        """
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [builder((3000, 1200))])

        report, lines = sir.scan_file(p)

        assert report.images == 1
        assert [f.action for f in report.findings] == ["resize"]
        assert lines is not None

    def test_all_four_observed_shapes_are_covered_in_one_transcript(self, tmp_path):
        """The complete shape set from the 16253-transcript survey, together.

        Pinned as one transcript so a future narrowing of the traversal cannot
        pass by covering three of four.
        """
        p = tmp_path / "s.jsonl"
        _write_transcript(
            p,
            [
                _prompt_record((3000, 1200)),
                _tool_result_record((3000, 1200)),
                _compaction_record((3000, 1200)),
                _compaction_record_with_nested_tool_result((3000, 1200)),
            ],
        )

        report, _lines = sir.scan_file(p)

        assert report.images == 4
        assert [f.action for f in report.findings] == ["resize"] * 4


class TestLostAppendRefusal:
    def test_a_transcript_that_moved_since_the_scan_is_REFUSED(self, tmp_path):
        """An append landing between scan and write must not be overwritten.

        The rewrite is built from lines read earlier, so replacing the file
        would drop the appended turn silently. The write refuses instead.
        """
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])
        report, lines = sir.scan_file(p)
        assert lines is not None
        assert report.source_stat is not None

        # kiro-cli appends a turn while the operator was reading the dry run.
        with p.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"version": 1, "kind": "Prompt", "data": {}}) + "\n")
        after_append = p.read_bytes()

        with pytest.raises(sir.TranscriptMovedError):
            sir.apply_repair(p, lines, backup=False, expect=report.source_stat)

        assert p.read_bytes() == after_append  # the appended turn survived

    def test_an_append_landing_DURING_the_write_is_still_refused(self, tmp_path, monkeypatch):
        """The early check alone is not enough: the write itself takes time.

        Between the up-front stat and ``os.replace`` sit a whole-file backup copy
        and a full temp-file rewrite plus fsync -- seconds on a real transcript.
        An append arriving inside that window passes the early check and would
        still be clobbered, so the stat is re-taken immediately before the
        rename. Simulated by appending from inside ``os.fsync``, the last call
        before the rename, which is precisely the window the early check misses.
        """
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])
        report, lines = sir.scan_file(p)
        assert lines is not None

        real_fsync = os.fsync
        appended = {"done": False}

        def fsync_then_append(fd):
            real_fsync(fd)
            if not appended["done"]:
                appended["done"] = True
                with p.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"version": 1, "kind": "Prompt", "data": {}}) + "\n")

        monkeypatch.setattr(sir.os, "fsync", fsync_then_append)

        with pytest.raises(sir.TranscriptMovedError):
            sir.apply_repair(p, lines, backup=False, expect=report.source_stat)

        assert appended["done"]  # the window was actually exercised
        assert len(p.read_text(encoding="utf-8").strip().splitlines()) == 2
        assert not list(tmp_path.glob("*.tmp"))  # the abandoned temp is cleaned up

    def test_an_unmoved_transcript_is_written(self, tmp_path):
        """The guard must not refuse the normal case it is wrapped around."""
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])
        report, lines = sir.scan_file(p)
        assert lines is not None

        sir.apply_repair(p, lines, backup=False, expect=report.source_stat)

        report_after, lines_after = sir.scan_file(p)
        assert report_after.oversized == []
        assert lines_after is None


class TestLiveWriterRefusal:
    """A running kiro-cli is the condition under which the narrow race bites."""

    def _lock(self, transcript: Path, pid: int) -> Path:
        lock = transcript.with_suffix(".lock")
        lock.write_text(json.dumps({"pid": pid, "started_at": "now"}), encoding="utf-8")
        return lock

    def test_apply_is_REFUSED_while_a_live_pid_holds_the_transcript(self, tmp_path, monkeypatch):
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])
        self._lock(p, 4242)
        monkeypatch.setattr(sir.platform_compat, "pid_exists", lambda pid: pid == 4242)
        before = p.read_bytes()

        report, lines = sir.scan_file(p)
        assert lines is not None
        with pytest.raises(sir.TranscriptLiveError):
            sir.apply_repair(p, lines, backup=False, expect=report.source_stat)

        assert p.read_bytes() == before
        assert not list(tmp_path.glob("*.tmp"))

    def test_a_STALE_lock_does_not_block_a_repair(self, tmp_path, monkeypatch):
        """Existence is not the signal -- stale locks accumulate without bound.

        6471 were present on one real machine, the oldest months old. Treating
        the file's presence as liveness would make the tool refuse essentially
        every transcript it exists to fix.
        """
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])
        self._lock(p, 999999)
        monkeypatch.setattr(sir.platform_compat, "pid_exists", lambda pid: False)

        report, lines = sir.scan_file(p)
        assert lines is not None
        sir.apply_repair(p, lines, backup=False, expect=report.source_stat)

        report_after, _ = sir.scan_file(p)
        assert report_after.oversized == []

    def test_allow_live_overrides_the_refusal(self, tmp_path, monkeypatch):
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])
        self._lock(p, 4242)
        monkeypatch.setattr(sir.platform_compat, "pid_exists", lambda pid: True)

        report, lines = sir.scan_file(p)
        assert lines is not None
        sir.apply_repair(p, lines, backup=False, expect=report.source_stat, allow_live=True)

        report_after, _ = sir.scan_file(p)
        assert report_after.oversized == []

    def test_a_missing_or_malformed_lock_is_not_liveness(self, tmp_path):
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((100, 80))])
        assert sir._live_writer_pid(p) is None  # no lock at all

        p.with_suffix(".lock").write_text("not json", encoding="utf-8")
        assert sir._live_writer_pid(p) is None


class TestReplacementTranscriptIsLockedDown:
    """os.replace carries the TEMP's permissions onto the transcript."""

    def test_the_temp_is_locked_down_before_any_content_is_written(self, tmp_path, monkeypatch):
        """A repair must not WIDEN access to the conversation it rewrites.

        mkstemp is 0o600 on POSIX, but on Windows the temp inherits the session
        directory's DACL, and ``os.replace`` then publishes those permissions
        onto the transcript -- so a repair would hand the whole conversation to
        other local accounts. Asserted as an ordered trace because the ordering
        is the property: locking down after the payload leaves the content
        readable for the duration of a multi-megabyte write.
        """
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])
        report, lines = sir.scan_file(p)
        assert lines is not None

        order: list[str] = []
        real_restrict = sir.platform_compat.restrict_to_owner
        real_fdopen = os.fdopen

        def traced_restrict(target):
            order.append(f"restrict:{'tmp' if str(target).endswith('.tmp') else 'other'}")
            return real_restrict(target)

        def traced_fdopen(fd, *a, **kw):
            order.append("fdopen")
            return real_fdopen(fd, *a, **kw)

        monkeypatch.setattr(sir.platform_compat, "restrict_to_owner", traced_restrict)
        monkeypatch.setattr(os, "fdopen", traced_fdopen)

        sir.apply_repair(p, lines, backup=False, expect=report.source_stat)
        monkeypatch.undo()

        # The temp is restricted, and restricted before it is opened for writing.
        assert "restrict:tmp" in order
        assert order.index("restrict:tmp") < order.index("fdopen")

    def test_a_failed_temp_lockdown_leaves_no_temp_and_no_leak(self, tmp_path, monkeypatch):
        """The lockdown is fail-loud, so its failure must not strand the temp.

        Same descriptor-ownership hazard as the backup path: the raw fd is ours
        until fdopen takes it, and Windows refuses to unlink a file with an open
        handle -- which would leave a .tmp behind on every failed repair.
        """
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])
        report, lines = sir.scan_file(p)
        assert lines is not None
        before = p.read_bytes()

        def boom(target):
            raise OSError("icacls failed")

        monkeypatch.setattr(sir.platform_compat, "restrict_to_owner", boom)
        fds_before = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None

        with pytest.raises(OSError, match="icacls failed"):
            sir.apply_repair(p, lines, backup=False, expect=report.source_stat)

        assert p.read_bytes() == before  # transcript untouched
        assert not list(tmp_path.glob("*.tmp"))
        if fds_before is not None:
            assert len(os.listdir("/proc/self/fd")) <= fds_before


class TestBackupIsNeverOverwritten:
    """The first sidecar is the only pre-repair copy, so it is never replaced."""

    def test_a_second_apply_is_REFUSED_rather_than_clobbering_the_backup(self, tmp_path):
        """The plain trigger: run --apply twice on a transcript still needing work.

        Without this the second run replaces the true pre-repair sidecar with
        already-repaired content. If the first run DROPPED an uncappable block,
        those bytes then exist in no file at all.
        """
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])
        sidecar = p.with_suffix(".jsonl.pre-image-repair.bak")
        sidecar.write_bytes(b'{"kind":"Prompt","data":{}}\n')  # an earlier run's copy
        original_backup = sidecar.read_bytes()

        report, lines = sir.scan_file(p)
        assert lines is not None
        with pytest.raises(sir.BackupExistsError):
            sir.apply_repair(p, lines, backup=True, expect=report.source_stat)

        assert sidecar.read_bytes() == original_backup  # untouched
        assert not list(tmp_path.glob("*.tmp"))

    def test_the_sidecar_NAME_is_claimed_before_the_content_is_read(self, tmp_path, monkeypatch):
        """This ordering is what makes the concurrency guarantee hold.

        A check-then-write guard reads the transcript and then writes, so two
        concurrent runs can both pass the check and the later read can capture
        ALREADY-REPAIRED content, replacing the genuine pre-repair copy. Exclusive
        create closes that only because the name is claimed FIRST: a rival running
        this same code gets EEXIST at its own open and never reaches a read.

        Asserted as a sequence rather than by racing two processes, which would be
        a flaky test of the same fact.
        """
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])
        report, lines = sir.scan_file(p)
        assert lines is not None

        order: list[str] = []
        real_open = os.open
        real_read_bytes = Path.read_bytes

        def traced_open(target, flags, *a, **kw):
            if str(target).endswith(".pre-image-repair.bak"):
                order.append(f"open(exclusive={bool(flags & os.O_EXCL)})")
            return real_open(target, flags, *a, **kw)

        def traced_read(self):
            if self == p:
                order.append("read")
            return real_read_bytes(self)

        monkeypatch.setattr(os, "open", traced_open)
        monkeypatch.setattr(Path, "read_bytes", traced_read)

        sir.apply_repair(p, lines, backup=True, expect=report.source_stat)
        monkeypatch.undo()

        assert order == ["open(exclusive=True)", "read"]

    def test_a_failed_lockdown_strands_no_sidecar_and_leaks_no_descriptor(
        self, tmp_path, monkeypatch
    ):
        """A fail-loud lockdown must not wedge every future retry.

        The lockdown runs while the sidecar is still empty, so its failure is the
        one point where a descriptor is open on a file the cleanup then has to
        remove. If the fd outlived that, Windows would refuse the unlink (open
        handle) and the stranded empty sidecar would make every later --apply
        raise BackupExistsError -- a permanent block created by a failure that
        changed nothing. Asserted by count of open descriptors as well as by the
        absent file, because a leak is invisible from the filesystem alone.
        """
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])
        report, lines = sir.scan_file(p)
        assert lines is not None

        def boom(_path):
            raise OSError("icacls failed")

        monkeypatch.setattr(sir.platform_compat, "restrict_to_owner", boom)

        fds_before = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None

        with pytest.raises(OSError, match="icacls failed"):
            sir.apply_repair(p, lines, backup=True, expect=report.source_stat)

        sidecar = p.with_suffix(".jsonl.pre-image-repair.bak")
        assert not sidecar.exists()  # nothing stranded to block the retry
        assert not list(tmp_path.glob("*.tmp"))
        if fds_before is not None:
            assert len(os.listdir("/proc/self/fd")) <= fds_before  # no leak

        # And the retry it would have blocked now works.
        monkeypatch.undo()
        report2, lines2 = sir.scan_file(p)
        assert lines2 is not None
        sir.apply_repair(p, lines2, backup=True, expect=report2.source_stat)
        assert sidecar.exists()

    def test_a_dangling_symlink_sidecar_is_also_refused(self, tmp_path):
        """exists() is False for a dangling link, so is_symlink is checked too."""
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])
        sidecar = p.with_suffix(".jsonl.pre-image-repair.bak")
        sidecar.symlink_to(tmp_path / "does-not-exist")

        report, lines = sir.scan_file(p)
        assert lines is not None
        with pytest.raises(sir.BackupExistsError):
            sir.apply_repair(p, lines, backup=True, expect=report.source_stat)

        assert sidecar.is_symlink()  # left exactly as found

    def test_the_CLI_reports_the_refusal_and_exits_nonzero(self, tmp_path, capsys):
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])
        p.with_suffix(".jsonl.pre-image-repair.bak").write_bytes(b"earlier\n")
        before = p.read_bytes()

        assert sir.main(["--apply", str(p)]) == 1

        assert "REFUSED" in capsys.readouterr().err
        assert p.read_bytes() == before  # the transcript is untouched too


class TestBackupDoesNotFollowAPlantedLink:
    def test_a_symlink_at_the_backup_path_is_REFUSED_and_its_target_untouched(self, tmp_path):
        """The backup name is predictable, so it is a plantable target.

        Anything able to write the session directory can pre-create
        ``<transcript>.pre-image-repair.bak`` as a symlink to a file it could not
        write directly, and a copy that opens the destination for writing would
        push the transcript through the link into that file -- the write performed
        by a trusted operator command rather than by the planter.

        Two independent defences now stand in front of that, and this asserts the
        outer one: the sidecar-exists refusal declines before any write happens.
        The inner one remains as defence in depth -- the backup goes through
        ``atomic_write``, which renames a fresh temp file into place and so
        replaces a link rather than following it, should the refusal ever be
        bypassed.
        """
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])
        victim = tmp_path / "victim.txt"
        victim.write_text("do not overwrite me", encoding="utf-8")
        planted = p.with_suffix(".jsonl.pre-image-repair.bak")
        planted.symlink_to(victim)

        report, lines = sir.scan_file(p)
        assert lines is not None
        with pytest.raises(sir.BackupExistsError):
            sir.apply_repair(p, lines, backup=True, expect=report.source_stat)

        assert victim.read_text(encoding="utf-8") == "do not overwrite me"
        assert planted.is_symlink()  # left as found, not replaced
        assert not list(tmp_path.glob("*.tmp"))


class TestApplyRepair:
    def test_dry_run_writes_nothing(self, tmp_path):
        """(7) scan_file (dry run, no apply) never writes to disk."""
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])
        before = p.read_bytes()
        listing_before = sorted(os.listdir(tmp_path))

        report, lines = sir.scan_file(p)

        assert lines is not None and report.changed  # a repair IS warranted...
        assert p.read_bytes() == before  # ...but scan_file did not perform it
        assert sorted(os.listdir(tmp_path)) == listing_before

    def test_apply_creates_backup_sidecar(self, tmp_path):
        """(8a) apply with backup=True writes the .pre-image-repair.bak sidecar."""
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])
        original = p.read_bytes()

        _report, lines = sir.scan_file(p)
        assert lines is not None
        backup = sir.apply_repair(p, lines, backup=True)

        assert backup is not None
        assert backup.name == "s.jsonl.pre-image-repair.bak"
        assert backup.exists()
        assert backup.read_bytes() == original  # backup is the pre-repair content
        assert p.read_bytes() != original  # live file was rewritten

    def test_apply_no_backup_writes_no_sidecar(self, tmp_path):
        """(8b) apply with backup=False makes no sidecar."""
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])

        _report, lines = sir.scan_file(p)
        assert lines is not None
        backup = sir.apply_repair(p, lines, backup=False)

        assert backup is None
        sidecar = p.with_suffix(p.suffix + ".pre-image-repair.bak")
        assert not sidecar.exists()

    def test_apply_leaves_no_tmp_files(self, tmp_path):
        """(9) apply is atomic: no leftover .tmp files in the directory after."""
        p = tmp_path / "s.jsonl"
        _write_transcript(p, [_prompt_record((3000, 1200))])

        _report, lines = sir.scan_file(p)
        assert lines is not None
        sir.apply_repair(p, lines, backup=False)

        leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
        assert leftovers == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
