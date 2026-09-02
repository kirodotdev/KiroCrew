#!/usr/bin/env python3
"""Check a Windows installer's architecture and signature metadata before publish.

Run by the Windows publish lane on the exact bytes it is about to make
immutable. It exists because electron-updater's ``NsisUpdater`` verifies
Authenticode **fail-closed**: an installer that is unsigned, or signed by
someone other than the pinned publisher, does not degrade updates. It breaks
every update for every client reading the feed, and the mutable ``latest/``
alias means it breaks them simultaneously.

WHAT THIS PROVES, AND WHAT IT DOES NOT
--------------------------------------
It answers "was this signed, by us, in a form the client will accept" -- a
**misconfiguration** check. The state it exists to catch is real and routine:
``build-windows.yml`` skips signing cleanly when its signing secret is absent,
so "a working installer with no signature" is a normal output of a
misconfigured run, not a hypothetical.

It does NOT recompute the Authenticode digest over the PE, so it is not a
tamper check. That is deliberate rather than overlooked. The bytes it inspects
come from the same workflow run's own build job via ``download-artifact``, and
byte identity from there to the CDN is already established twice over: the
versioned key is written with ``--if-none-match`` and a 412 is tolerated only
after a sha256 comparison against the published object, and the feed step
re-reads the object through the CDN and refuses to advertise a digest the
served bytes do not have. An actor able to substitute the artifact is already
inside CI and could edit this workflow instead.

Two things are checked, each unrecoverable once clients cache the feed:

* the certificate table carries a PKCS#7 signature at all, and
* the SIGNER certificate is the expected publisher, countersigned by a
  timestamp authority.

There is deliberately NO architecture check, and the analogy to
publish-linux.yml's ELF-machine check does not hold. An AppImage IS its
payload, so its ELF header describes what the user will run. An NSIS installer
is a stub that unpacks a payload, and NSIS ships only a 32-bit stub: a real
electron-builder x64 installer reports COFF machine 0x014c
(IMAGE_FILE_MACHINE_I386) with a PE32 optional header, measured on the signed
nightly installer. So the header carries no information about the packaged
architecture, and asserting 0x8664 rejects every genuine installer while
asserting 0x014c only restates that NSIS built it. What actually binds the
architecture is artifact identity: the lane accepts arch x64 alone and consumes
the artifact the x64 build job uploaded by name. The machine is reported for
debugging and never gated on.

The timestamp matters beyond provenance. The signing certificate is reissued
annually, so a signature with no RFC3161 countersignature stops verifying when
that certificate expires, which would strand every installer already published
under it.

Stdlib only, plus ``openssl`` for certificate parsing.
"""

from __future__ import annotations

import argparse
import re
import struct
import subprocess
import sys
from pathlib import Path

# The Security data directory is index 4, and it is the one directory whose
# VirtualAddress is a raw file offset rather than an RVA, so no section-table
# walk is needed to find it.
_SECURITY_DIRECTORY_INDEX = 4
_PE32_MAGIC = 0x10B
_PE32_PLUS_MAGIC = 0x20B
_PE32_DIRECTORY_OFFSET = 96
_PE32_PLUS_DIRECTORY_OFFSET = 112
_WIN_CERT_TYPE_PKCS_SIGNED_DATA = 0x0002

# Microsoft's RFC3161 timestamp attribute, as an unsigned attribute on the
# SignerInfo. Matched as raw DER bytes because the token is nested inside an
# OCTET STRING that `openssl asn1parse` will not descend into.
_MS_RFC3161_TIMESTAMP_OID = bytes.fromhex("2b060104018237030301")

# RFC2253 escapes a comma inside an attribute value as "\,", which is what makes
# splitting on commas safe. The expected publisher CN contains one
# ("Amazon Web Services, Inc."), so a parser that treats every comma as an RDN
# separator truncates it and rejects the genuine installer.
_RFC2253_CN_RE = re.compile(r"(?:^|,)CN=((?:[^,\\]|\\.)*)")


class VerificationError(Exception):
    """The installer must not be published."""


def _read_pe_security_blob(installer: Path) -> tuple[int, bytes]:
    """Return the COFF machine and the installer's PKCS#7 signature blob."""
    data = installer.read_bytes()
    if data[:2] != b"MZ":
        raise VerificationError(f"{installer} is not a PE image (no MZ header)")

    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e_lfanew : e_lfanew + 4] != b"PE\0\0":
        raise VerificationError(f"{installer} is not a PE image (no PE signature)")

    machine = struct.unpack_from("<H", data, e_lfanew + 4)[0]

    optional_header = e_lfanew + 24
    magic = struct.unpack_from("<H", data, optional_header)[0]
    if magic == _PE32_MAGIC:
        directories = optional_header + _PE32_DIRECTORY_OFFSET
    elif magic == _PE32_PLUS_MAGIC:
        directories = optional_header + _PE32_PLUS_DIRECTORY_OFFSET
    else:
        raise VerificationError(f"{installer} has an unknown optional header magic {magic:#x}")

    offset, size = struct.unpack_from("<II", data, directories + _SECURITY_DIRECTORY_INDEX * 8)
    if offset == 0 or size == 0:
        raise VerificationError(
            f"{installer} carries no Authenticode signature (the certificate table is "
            "empty). Publishing it would make every client's fail-closed update check "
            "reject the installer."
        )

    position, end = offset, offset + size
    while position < end:
        length, _revision, cert_type = struct.unpack_from("<IHH", data, position)
        if length < 8:
            break
        if cert_type == _WIN_CERT_TYPE_PKCS_SIGNED_DATA:
            return machine, data[position + 8 : position + length]
        # Entries are padded to an 8-byte boundary.
        position += (length + 7) & ~7

    raise VerificationError(f"{installer} has a certificate table with no PKCS#7 signed-data entry")


def _openssl(args: list[str], stdin: bytes | None = None) -> str:
    try:
        completed = subprocess.run(["openssl", *args], input=stdin, capture_output=True, check=True)
    except FileNotFoundError as exc:  # pragma: no cover - environment gap
        raise VerificationError("openssl is required to verify the signature") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", "replace").strip()
        raise VerificationError(f"openssl {args[0]} failed: {detail}") from exc
    return completed.stdout.decode("utf-8", "replace")


def _certificate_pems(blob: bytes) -> list[str]:
    # Fed on stdin rather than through a temp file. `NamedTemporaryFile` holds an
    # open handle, and on Windows a second process cannot then read the path --
    # openssl fails with "Permission denied", which surfaced as eight failures on
    # the Windows backend shard. openssl reads DER from stdin when `-in` is
    # omitted, so dropping the file removes the failure mode instead of working
    # around it.
    try:
        pem = _openssl(
            ["pkcs7", "-inform", "DER", "-print_certs", "-outform", "PEM"],
            stdin=blob,
        )
    except VerificationError as exc:
        raise VerificationError(f"the signature blob is not parseable as PKCS#7: {exc}") from exc

    begin = "-----BEGIN CERTIFICATE-----"
    end = "-----END CERTIFICATE-----"
    return [
        f"{begin}{body.split(end, 1)[0]}{end}\n" for body in pem.split(begin)[1:] if end in body
    ]


def _common_name(distinguished_name: str) -> str | None:
    match = _RFC2253_CN_RE.search(distinguished_name)
    if match is None:
        return None
    return match.group(1).replace("\\,", ",").replace("\\", "")


def _signer_common_name(blob: bytes) -> tuple[str | None, int]:
    """The CN of the END-ENTITY certificate, plus the chain length.

    The runtime check this mirrors compares ``publisherName`` against the SIGNER
    certificate's subject alone, so matching any certificate in the chain would
    accept a build whose leaf is wrong but whose intermediate happens to carry
    the expected name. That build passes here and is then rejected fail-closed by
    every client, which is the fleet-wide breakage this guard exists to prevent.

    Subjects and issuers are read in RFC2253 form, where a comma inside a value
    is escaped, so splitting on commas is safe. The expected publisher CN
    contains one ("Amazon Web Services, Inc."), and a parser that treats every
    comma as an RDN separator truncates it and refuses the genuine installer.

    The end-entity certificate is the one that issued nothing else in the bag.
    """
    subjects: list[str] = []
    issuers: list[str] = []
    for pem in _certificate_pems(blob):
        text = _openssl(
            ["x509", "-noout", "-subject", "-issuer", "-nameopt", "rfc2253"],
            stdin=pem.encode("ascii"),
        )
        subject = issuer = ""
        for line in text.splitlines():
            if line.startswith("subject="):
                subject = line.removeprefix("subject=").strip()
            elif line.startswith("issuer="):
                issuer = line.removeprefix("issuer=").strip()
        subjects.append(subject)
        issuers.append(issuer)

    leaves = [
        subject
        for index, subject in enumerate(subjects)
        if not any(issuer == subject for j, issuer in enumerate(issuers) if j != index)
    ]
    if len(leaves) != 1:
        raise VerificationError(
            f"the signature carries {len(subjects)} certificate(s) with {len(leaves)} "
            "end-entity candidate(s); the signer cannot be identified unambiguously, "
            "so the publisher cannot be checked against what the client will demand."
        )
    return _common_name(leaves[0]), len(subjects)


def verify(installer: Path, expect_subject_cn: str) -> list[str]:
    """Verify the installer, returning the human-readable findings on success."""
    machine, blob = _read_pe_security_blob(installer)

    signer_cn, chain_length = _signer_common_name(blob)
    if signer_cn != expect_subject_cn:
        raise VerificationError(
            f"{installer} is signed, but its SIGNER certificate carries CN "
            f"{signer_cn!r} rather than the expected publisher {expect_subject_cn!r}. "
            "The client compares only the signer, so a mismatch fails every client's "
            "signature check."
        )

    if _MS_RFC3161_TIMESTAMP_OID not in blob:
        raise VerificationError(
            f"{installer} has no RFC3161 timestamp countersignature. The signing "
            "certificate is reissued annually, so an untimestamped signature stops "
            "verifying once it expires and strands every installer published under it."
        )

    return [
        # Reported, deliberately NOT asserted -- see the module docstring. An
        # NSIS installer stub is a 32-bit PE whatever architecture it installs,
        # so this number identifies the stub, not the payload.
        f"nsis stub machine {machine:#06x}",
        f"signer {expect_subject_cn!r}",
        f"{chain_length} certificate(s) in the chain",
        "RFC3161 timestamp present",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--expect-subject-cn", required=True)
    args = parser.parse_args(argv)

    try:
        findings = verify(args.installer, args.expect_subject_cn)
    except VerificationError as exc:
        print(f"::error::{exc}")
        return 1

    for finding in findings:
        print(f"verified: {finding}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
