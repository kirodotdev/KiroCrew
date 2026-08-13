"""Tests for the Windows installer publish guard.

The guard runs on the exact bytes the publish lane is about to make immutable,
and every branch it has is a refusal, so each refusal is exercised here against
a synthetic PE. The signature branches use a real ``openssl``-generated PKCS#7
rather than a canned string: the parsing is delegated to ``openssl``, so a
hand-written fixture would test the fixture and not the delegation.
"""

from __future__ import annotations

import importlib.util
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_windows_installer.py"

_spec = importlib.util.spec_from_file_location("verify_windows_installer", SCRIPT)
assert _spec is not None and _spec.loader is not None
verifier = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verifier)

# The real NSIS stub: measured on the signed nightly installer.
MACHINE_I386 = 0x014C
MACHINE_AMD64 = 0x8664
MACHINE_ARM64 = 0xAA64
PUBLISHER = "Amazon Web Services, Inc."

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl is required to build a PKCS#7 fixture"
)


def _pe_image(machine: int, certificate: bytes | None, magic: int = 0x20B) -> bytes:
    """Build a minimal PE whose certificate table holds ``certificate``."""
    e_lfanew = 0x80
    # Data directories sit at optional_header + 112 for PE32+; the security
    # directory is index 4, so the header must extend at least 5 entries.
    optional_header = e_lfanew + 24
    directories = optional_header + (112 if magic == 0x20B else 96)
    security_entry = directories + 4 * 8
    header_size = security_entry + 8

    image = bytearray(header_size)
    image[0:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, e_lfanew)
    image[e_lfanew : e_lfanew + 4] = b"PE\0\0"
    struct.pack_into("<H", image, e_lfanew + 4, machine)
    struct.pack_into("<H", image, optional_header, magic)

    if certificate is None:
        struct.pack_into("<II", image, security_entry, 0, 0)
        return bytes(image)

    # WIN_CERTIFICATE: dwLength covers the 8-byte header plus the blob.
    entry = struct.pack("<IHH", len(certificate) + 8, 0x0200, 0x0002) + certificate
    struct.pack_into("<II", image, security_entry, header_size, len(entry))
    return bytes(image) + entry


def _pkcs7(tmp_path: Path, common_name: str) -> bytes:
    """A certs-only DER PKCS#7 carrying one self-signed certificate."""
    key = tmp_path / "key.pem"
    cert = tmp_path / "cert.pem"
    bundle = tmp_path / "bundle.p7b"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            f"/C=US/O=Test/CN={common_name}",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl",
            "crl2pkcs7",
            "-nocrl",
            "-certfile",
            str(cert),
            "-outform",
            "DER",
            "-out",
            str(bundle),
        ],
        check=True,
        capture_output=True,
    )
    return bundle.read_bytes()


def _write(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "installer.exe"
    path.write_bytes(data)
    return path


def test_a_non_pe_file_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, b"not an executable")
    with pytest.raises(verifier.VerificationError, match="no MZ header"):
        verifier.verify(path, PUBLISHER)


def test_a_truncated_pe_signature_is_refused(tmp_path: Path) -> None:
    image = bytearray(_pe_image(MACHINE_AMD64, None))
    image[0x80:0x84] = b"XX\0\0"
    with pytest.raises(verifier.VerificationError, match="no PE signature"):
        verifier.verify(_write(tmp_path, bytes(image)), PUBLISHER)


def test_an_unknown_optional_header_magic_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, _pe_image(MACHINE_AMD64, None, magic=0x1234))
    with pytest.raises(verifier.VerificationError, match="unknown optional header magic"):
        verifier.verify(path, PUBLISHER)


def test_an_unsigned_installer_is_refused(tmp_path: Path) -> None:
    # The case build-windows.yml actually produces when its signing secret is
    # absent: a working installer that no client will accept an update from.
    path = _write(tmp_path, _pe_image(MACHINE_AMD64, None))
    with pytest.raises(verifier.VerificationError, match="certificate table is empty"):
        verifier.verify(path, PUBLISHER)


def test_a_certificate_table_without_signed_data_is_refused(tmp_path: Path) -> None:
    entry = struct.pack("<IHH", 12, 0x0200, 0x0001) + b"junk"
    image = bytearray(_pe_image(MACHINE_AMD64, None))
    security_entry = 0x80 + 24 + 112 + 4 * 8
    struct.pack_into("<II", image, security_entry, len(image), len(entry))
    path = _write(tmp_path, bytes(image) + entry)
    with pytest.raises(verifier.VerificationError, match="no PKCS#7 signed-data entry"):
        verifier.verify(path, PUBLISHER)


def test_a_32_bit_nsis_stub_is_accepted(tmp_path: Path, monkeypatch) -> None:
    """A genuine x64 installer is a 32-bit PE, and must not be refused for it.

    Measured on the signed nightly installer: COFF machine 0x014c
    (IMAGE_FILE_MACHINE_I386), PE32 optional header. NSIS ships only a 32-bit
    stub, so this is what EVERY electron-builder Windows installer looks like
    regardless of the architecture it installs.

    An earlier version of this guard demanded 0x8664 and therefore rejected
    every real installer, which would have failed the publish job on every run
    and shipped nothing. Pinning the i386 case keeps that from coming back --
    including via the tempting-looking "mirror publish-linux.yml's ELF-machine
    check", which does not transfer because an AppImage IS its payload while an
    NSIS stub is not.
    """
    monkeypatch.setattr(verifier, "_MS_RFC3161_TIMESTAMP_OID", b"\x30")
    # magic=0x10B is PE32, matching the real stub; the arm64 machine below is
    # equally accepted, because architecture is bound by artifact identity and
    # deliberately not by this header.
    stub = _pe_image(MACHINE_I386, _pkcs7(tmp_path, PUBLISHER), magic=0x10B)
    findings = verifier.verify(_write(tmp_path, stub), PUBLISHER)
    assert "0x014c" in findings[0]
    assert PUBLISHER in findings[1]


def test_the_signature_blob_never_travels_through_a_file(monkeypatch) -> None:
    """openssl must receive the DER on stdin, not as a path.

    A `NamedTemporaryFile` keeps its handle open, and on Windows a second process
    cannot then read that path: openssl fails with "Permission denied". That is
    invisible to a POSIX-only run, so the Windows backend shard was the first
    thing to notice, with eight failures at once.

    The invariant is therefore pinned behaviourally rather than by re-reading the
    source: the blob is passed in-band, and no `-in` path is handed to openssl. A
    Linux run cannot reproduce the failure itself, so this is the only test that
    can hold the line here.
    """
    seen: dict[str, object] = {}

    def fake_openssl(args, stdin=None):
        seen["args"] = list(args)
        seen["stdin"] = stdin
        return ""

    monkeypatch.setattr(verifier, "_openssl", fake_openssl)
    verifier._certificate_pems(b"\x30\x03der")

    assert seen["stdin"] == b"\x30\x03der", "the blob must be fed in-band"
    assert "-in" not in seen["args"], (
        "passing a path makes this fail on Windows, where the open temp-file "
        "handle blocks openssl from reading it"
    )


def test_the_machine_is_reported_but_never_gated_on(tmp_path: Path, monkeypatch) -> None:
    """Every architecture passes, so the report cannot be mistaken for a check."""
    monkeypatch.setattr(verifier, "_MS_RFC3161_TIMESTAMP_OID", b"\x30")
    for machine in (MACHINE_I386, MACHINE_AMD64, MACHINE_ARM64):
        image = _pe_image(machine, _pkcs7(tmp_path, PUBLISHER))
        findings = verifier.verify(_write(tmp_path, image), PUBLISHER)
        assert f"{machine:#06x}" in findings[0]


def test_an_unparseable_signature_blob_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, _pe_image(MACHINE_AMD64, b"\x30\x03not-der"))
    with pytest.raises(verifier.VerificationError, match="not parseable as PKCS#7"):
        verifier.verify(path, PUBLISHER)


def test_the_wrong_publisher_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, _pe_image(MACHINE_AMD64, _pkcs7(tmp_path, "Someone Else")))
    with pytest.raises(verifier.VerificationError, match="expected publisher"):
        verifier.verify(path, PUBLISHER)


def test_a_missing_timestamp_is_refused(tmp_path: Path) -> None:
    # The signing certificate is reissued annually, so an untimestamped
    # signature strands every installer published under it once it expires.
    path = _write(tmp_path, _pe_image(MACHINE_AMD64, _pkcs7(tmp_path, PUBLISHER)))
    with pytest.raises(verifier.VerificationError, match="no RFC3161 timestamp"):
        verifier.verify(path, PUBLISHER)


def test_a_correctly_signed_installer_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blob = _pkcs7(tmp_path, PUBLISHER)
    # openssl cannot mint an RFC3161 countersignature without a timestamp
    # authority, so the OID this fixture cannot carry is redirected to a byte
    # sequence every DER structure has. That isolates the success branch without
    # weakening the production check.
    monkeypatch.setattr(verifier, "_MS_RFC3161_TIMESTAMP_OID", b"\x30")
    findings = verifier.verify(_write(tmp_path, _pe_image(MACHINE_AMD64, blob)), PUBLISHER)
    assert "machine 0x8664" in findings[0]
    assert PUBLISHER in findings[1]
    assert "RFC3161 timestamp present" in findings


def _chain_pkcs7(tmp_path: Path, ca_cn: str, leaf_cn: str) -> bytes:
    """A DER PKCS#7 holding a CA cert plus a leaf it issued.

    Used to prove the guard matches the SIGNER rather than any certificate in
    the bag: the CA carries the expected publisher name and the leaf does not.
    """
    ca_key, ca_cert = tmp_path / "ca.key", tmp_path / "ca.pem"
    leaf_key, leaf_csr, leaf_cert = (
        tmp_path / "leaf.key",
        tmp_path / "leaf.csr",
        tmp_path / "leaf.pem",
    )
    bundle = tmp_path / "chain.p7b"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_cert),
            "-days",
            "1",
            "-subj",
            f"/C=US/O=Test/CN={ca_cn}",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl",
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(leaf_key),
            "-out",
            str(leaf_csr),
            "-subj",
            f"/C=US/O=Test/CN={leaf_cn}",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(leaf_csr),
            "-CA",
            str(ca_cert),
            "-CAkey",
            str(ca_key),
            "-set_serial",
            "2",
            "-days",
            "1",
            "-out",
            str(leaf_cert),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl",
            "crl2pkcs7",
            "-nocrl",
            "-certfile",
            str(leaf_cert),
            "-certfile",
            str(ca_cert),
            "-outform",
            "DER",
            "-out",
            str(bundle),
        ],
        check=True,
        capture_output=True,
    )
    return bundle.read_bytes()


def test_the_expected_name_on_a_non_signer_certificate_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # electron-updater compares publisherName against the SIGNER certificate's
    # subject alone. Accepting a match anywhere in the chain would let a build
    # whose leaf is wrong, but whose issuer happens to carry our name, pass this
    # guard and then be refused fail-closed by every client -- the exact
    # fleet-wide breakage the guard exists to prevent.
    blob = _chain_pkcs7(tmp_path, ca_cn=PUBLISHER, leaf_cn="Somebody Else")
    monkeypatch.setattr(verifier, "_MS_RFC3161_TIMESTAMP_OID", b"\x30")
    with pytest.raises(verifier.VerificationError, match="SIGNER certificate"):
        verifier.verify(_write(tmp_path, _pe_image(MACHINE_AMD64, blob)), PUBLISHER)


def test_the_signer_is_matched_through_a_real_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blob = _chain_pkcs7(tmp_path, ca_cn="Some Intermediate CA", leaf_cn=PUBLISHER)
    monkeypatch.setattr(verifier, "_MS_RFC3161_TIMESTAMP_OID", b"\x30")
    findings = verifier.verify(_write(tmp_path, _pe_image(MACHINE_AMD64, blob)), PUBLISHER)
    assert "2 certificate(s) in the chain" in findings


def test_a_common_name_containing_a_comma_is_matched_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real publisher CN is "Amazon Web Services, Inc.". A parser that treats
    # every comma as an RDN separator reads it as "Amazon Web Services" and
    # refuses the genuine installer, which is a publish-blocking false negative
    # rather than a cosmetic bug.
    blob = _pkcs7(tmp_path, PUBLISHER)
    monkeypatch.setattr(verifier, "_MS_RFC3161_TIMESTAMP_OID", b"\x30")
    signer_cn, _ = verifier._signer_common_name(blob)
    assert signer_cn == PUBLISHER, f"the comma-bearing CN was truncated: {signer_cn!r}"


def test_main_returns_one_on_a_refusal(tmp_path: Path) -> None:
    path = _write(tmp_path, _pe_image(MACHINE_AMD64, None))
    assert verifier.main(["--installer", str(path), "--expect-subject-cn", PUBLISHER]) == 1


def test_main_returns_zero_on_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blob = _pkcs7(tmp_path, PUBLISHER)
    monkeypatch.setattr(verifier, "_MS_RFC3161_TIMESTAMP_OID", b"\x30")
    path = _write(tmp_path, _pe_image(MACHINE_AMD64, blob))
    assert verifier.main(["--installer", str(path), "--expect-subject-cn", PUBLISHER]) == 0
