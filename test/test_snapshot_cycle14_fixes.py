"""Decoding as UTF-8 is necessary evidence of text, not sufficient.

NUL is a legal code point, so a NUL-padded binary whose other bytes are ASCII decodes
cleanly -- a tar of text files is exactly that shape. Replacing a credential is a
variable-length edit, so rewriting one moves every following byte and the operator restores
something that is no longer a valid archive.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from kiro_crew import snapshot_redact as redact

KEY = "AKIAIOSFODNN7EXAMPLE"


def _stage(tmp_path: Path) -> Path:
    stage = tmp_path / "bundle"
    stage.mkdir()
    (stage / "MANIFEST.json").write_text(json.dumps({"components": {}}), encoding="utf-8")
    return stage


def _tar_of_text(payload: str) -> bytes:
    """A real tar carrying one text member: mostly ASCII, NUL-padded, UTF-8 decodable."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        data = payload.encode("utf-8")
        info = tarfile.TarInfo("notes.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestTheFilterKeywordHasAFallbackAtEverySite:
    """`filter=` landed in 3.11.4, and this repo supports 3.10."""

    def test_the_redaction_extract_falls_back_like_the_restore_extract(self) -> None:
        import ast

        src = Path(__import__("kiro_crew.snapshot", fromlist=["x"]).__file__)
        tree = ast.parse(src.read_text(encoding="utf-8"))
        unguarded: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "extractall"):
                continue
            if not any(kw.arg == "filter" for kw in node.keywords):
                continue
            # Walk up is not available on an ast node, so re-scan: the call must sit inside
            # a Try whose handlers name TypeError.
            guarded = False
            for outer in ast.walk(tree):
                if not isinstance(outer, ast.Try):
                    continue
                if node not in list(ast.walk(outer)):
                    continue
                for handler in outer.handlers:
                    names = (
                        [handler.type.id]
                        if isinstance(handler.type, ast.Name)
                        else [
                            e.id
                            for e in getattr(handler.type, "elts", [])
                            if isinstance(e, ast.Name)
                        ]
                    )
                    if "TypeError" in names:
                        guarded = True
            if not guarded:
                unguarded.append(node.lineno)
        assert unguarded == [], (
            f"extractall(filter=...) at line(s) {unguarded} has no TypeError fallback -- "
            f"an uncaught TypeError on Python < 3.11.4"
        )


class TestANulBearingFileIsNotRewritten:
    def test_the_fixture_really_is_utf8_decodable_and_nul_padded(self) -> None:
        """Otherwise the tests below would pass for the wrong reason."""
        raw = _tar_of_text(f"token={KEY}")
        assert b"\x00" in raw
        raw.decode("utf-8")  # must not raise

    def test_a_credential_inside_it_refuses_the_upload_instead_of_corrupting_it(
        self, tmp_path: Path
    ) -> None:
        stage = _stage(tmp_path)
        archive = stage / "workspace" / "export.tar"
        archive.parent.mkdir(parents=True)
        raw = _tar_of_text(f"token={KEY}")
        archive.write_bytes(raw)

        with pytest.raises(redact.OpaqueFilesPresent):
            redact.redact_bundle_for_egress(stage)

        assert archive.read_bytes() == raw, "the archive was rewritten in place"

    def test_it_is_still_a_readable_tar_afterwards(self, tmp_path: Path) -> None:
        """The corruption stated directly, rather than as a byte comparison."""
        stage = _stage(tmp_path)
        archive = stage / "workspace" / "export.tar"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(_tar_of_text(f"token={KEY}"))

        with pytest.raises(redact.OpaqueFilesPresent):
            redact.redact_bundle_for_egress(stage)

        with tarfile.open(archive, mode="r") as tf:
            assert [m.name for m in tf.getmembers()] == ["notes.txt"]

    def test_a_nul_bearing_file_with_no_credential_still_rides(self, tmp_path: Path) -> None:
        """The hazard is on the WRITE branch, so refusing this one would cost an upload."""
        stage = _stage(tmp_path)
        archive = stage / "workspace" / "clean.tar"
        archive.parent.mkdir(parents=True)
        raw = _tar_of_text("nothing secret in here")
        archive.write_bytes(raw)

        redact.redact_bundle_for_egress(stage)

        assert archive.read_bytes() == raw

    def test_ordinary_text_is_still_redacted_in_place(self, tmp_path: Path) -> None:
        stage = _stage(tmp_path)
        note = stage / "workspace" / "note.md"
        note.parent.mkdir(parents=True)
        note.write_text(f"token={KEY}\n", encoding="utf-8")

        redact.redact_bundle_for_egress(stage)

        assert KEY not in note.read_text(encoding="utf-8")
