"""The sign-time tripwire for a Mach-O hidden inside a compressed member.

`collect_all_machos` reads magic bytes, so an executable stored compressed is
invisible to it AND to the pre-sign ad-hoc signature strip -- nothing signs it.
The Apple notary service, however, decompresses archive members and scans what
is inside, so such a payload fails the WHOLE submission ~30 minutes later with
an opaque `Invalid`. That is how #6746 broke the macOS release lane (Apple
submission 3dbd3c7d, three `error` issues against
`.../binaries/ffmpeg-macos-aarch64-v7.1.gz/ffmpeg-macos-aarch64-v7.1`).

These tests pin the tripwire that turns that into a sign-time failure with a
bisectable trail.
"""

from __future__ import annotations

import gzip
import importlib.util
import pathlib
import struct
import tarfile
import zipfile

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "packaging" / "signing" / "generate-manifest.py"

# Thin arm64 Mach-O magic (MH_MAGIC_64) plus a plausible cputype word.
MACHO_HEAD = struct.pack(">I", 0xFEEDFACF) + struct.pack(">I", 0x0100000C)


@pytest.fixture(scope="module")
def generator():
    spec = importlib.util.spec_from_file_location("generate_manifest", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _bundle(tmp_path: pathlib.Path) -> pathlib.Path:
    binaries = tmp_path / "KiroCrew.app" / "Contents" / "Resources" / "binaries"
    binaries.mkdir(parents=True)
    return binaries


def test_a_clean_bundle_reports_nothing(generator, tmp_path):
    binaries = _bundle(tmp_path)
    # A plain Mach-O is the SHIPPING shape: enumerated and signed elsewhere, so
    # this tripwire must stay silent about it.
    (binaries / "ffmpeg-macos-aarch64-v7.1").write_bytes(MACHO_HEAD + b"\x00" * 64)
    (binaries / "notes.txt.gz").write_bytes(gzip.compress(b"just text", mtime=0))

    assert generator.find_archived_machos(str(tmp_path / "KiroCrew.app")) == []


def test_gzip_sealed_macho_is_reported(generator, tmp_path):
    """The exact #6746 shape."""
    binaries = _bundle(tmp_path)
    (binaries / "ffmpeg-macos-aarch64-v7.1.gz").write_bytes(
        gzip.compress(MACHO_HEAD + b"\x00" * 64, mtime=0)
    )

    errors = generator.find_archived_machos(str(tmp_path / "KiroCrew.app"))

    assert len(errors) == 1
    assert "ffmpeg-macos-aarch64-v7.1.gz" in errors[0]
    assert "Mach-O inside a compressed member" in errors[0]
    # The message must say what to DO, not just that something is wrong.
    assert "UNCOMPRESSED" in errors[0]


def test_zip_and_tar_members_are_inspected_too(generator, tmp_path):
    binaries = _bundle(tmp_path)
    payload = binaries / "payload"
    payload.write_bytes(MACHO_HEAD + b"\x00" * 64)
    with zipfile.ZipFile(binaries / "sealed.zip", "w") as archive:
        archive.write(payload, arcname="inner-binary")
    with tarfile.open(binaries / "sealed.tar.gz", "w:gz") as archive:
        archive.add(payload, arcname="tarred-binary")
    payload.unlink()

    reported = " ".join(generator.find_archived_machos(str(tmp_path / "KiroCrew.app")))

    assert "inner-binary" in reported
    assert "tarred-binary" in reported


def test_unreadable_container_is_not_treated_as_a_finding(generator, tmp_path):
    """A malformed container is not evidence of a hidden binary, and must not
    fail a sign that would otherwise notarize."""
    binaries = _bundle(tmp_path)
    (binaries / "truncated.gz").write_bytes(b"\x1f\x8b\x08truncated-garbage")

    assert generator.find_archived_machos(str(tmp_path / "KiroCrew.app")) == []


def test_the_generator_wires_the_tripwire_into_its_fail_closed_path(generator):
    """`main` must merge these errors with the layout errors it already aborts
    on -- a check nothing calls is not a check."""
    source = GENERATOR.read_text(encoding="utf-8")
    assert "layout_errors.extend(find_archived_machos(app_path))" in source
