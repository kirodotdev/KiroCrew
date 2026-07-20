#!/usr/bin/env python3
"""Generate the CDSigner signing manifest with full nested Mach-O coverage.

Why this exists: Apple notarization requires EVERY nested Mach-O binary to be
Developer-ID signed with hardened runtime + secure timestamp. CDSigner only
auto-detects frameworks/dylibs under Contents/Frameworks; everything under
Contents/Resources (our embedded Python backend: the interpreter, every .so
C-extension, every vendored .dylib) plus Squirrel's ShipIt helper must be
listed explicitly in `embedded_requirements`, or notarization returns
`Invalid` (confirmed: submission 3fefb424, 72 rejected binaries).

The backend's binary set changes whenever a Python dependency changes, so the
list is generated at sign time from the actual .app rather than maintained by
hand (same pattern as other Amazon Electron/CDSigner pipelines).

Usage:
    generate-manifest.py <manifest-template.json> <path/to/App.app>

Reads S3 substitution values from env (SIGNER_ACCESS_ROLE_ARN, SIGNING_BUCKET,
INPUT_KEY, OUTPUT_KEY) and prints the final manifest JSON to stdout. A summary
line is printed to stderr for build logs.
"""

import json
import os
import re
import struct
import sys

APP_ID = "com.amazon.kiro.crew"

# Mach-O magic numbers (thin, both endiannesses).
_THIN_MAGICS = {0xFEEDFACE, 0xFEEDFACF, 0xCEFAEDFE, 0xCFFAEDFE}
# Universal (fat) binary magics; nfat_arch coherence check distinguishes them
# from Java .class files, which share the 0xCAFEBABE magic.
_FAT_MAGIC, _FAT_CIGAM = 0xCAFEBABE, 0xBEBAFECA


def is_macho(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return False
    if len(head) < 8:
        return False
    (magic,) = struct.unpack(">I", head[:4])
    if magic in _THIN_MAGICS:
        return True
    if magic == _FAT_MAGIC:
        (nfat,) = struct.unpack(">I", head[4:8])
        return 0 < nfat < 30
    if magic == _FAT_CIGAM:
        (nfat,) = struct.unpack("<I", head[4:8])
        return 0 < nfat < 30
    return False


def identifier_for(rel_path: str) -> str:
    """Stable, unique bundle id derived from the full relative path.

    Derived from the whole path (not the basename) so duplicate filenames in
    different packages get distinct identifiers.
    """
    stem = rel_path
    if stem.startswith("Contents/Resources/"):
        stem = stem[len("Contents/Resources/"):]
    stem = os.path.splitext(stem)[0]
    # Proven pattern (matches other Amazon CDSigner pipelines): collapse
    # everything non-alphanumeric, including dots, to hyphens so every
    # identifier segment is well-formed.
    suffix = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-")
    return f"{APP_ID}.{suffix}"


def collect_all_machos(app_path: str) -> "list[str]":
    """Every non-symlink Mach-O in the bundle (relative paths). Used for the
    pre-sign ad-hoc signature strip."""
    out: "list[str]" = []
    for root, _dirs, files in os.walk(app_path):
        for name in files:
            full = os.path.join(root, name)
            if os.path.islink(full):
                continue
            if is_macho(full):
                out.append(os.path.relpath(full, app_path))
    return out


def collect_entries(app_path: str) -> "dict[str, dict]":
    """Manifest entries, matching the recipe proven to both sign and
    notarize for Python-runtime apps on this API:
      - EXCLUDE .dylib files (CDSigner signs dynamic libraries
        automatically during the app pass; listing them explicitly is
        rejected by the signing server's validation)
      - EXCLUDE Contents/MacOS/* (main executable, signed by the app pass)
      - INCLUDE everything else (interpreter, .so extensions, ShipIt) that
        lives under Contents/Resources or is a loose framework executable,
        each with the app entitlements
    """
    entries: "dict[str, dict]" = {}
    for rel in collect_all_machos(app_path):
        if rel.endswith(".dylib") and not rel.startswith("Contents/Resources/"):
            # Frameworks dylibs are auto-signed by CDSigner's app pass;
            # Resources dylibs are NOT (verified empirically) and must be listed.
            continue
        if rel.startswith("Contents/MacOS/"):
            continue
        in_resources = rel.startswith("Contents/Resources/")
        is_shipit = os.path.basename(rel) == "ShipIt"
        # Framework bundles are handled by the static template entries +
        # CDSigner auto-detection; ShipIt is the exception (a loose
        # executable in a framework's Resources dir that is NOT covered).
        if not (in_resources or is_shipit):
            continue
        entries[rel] = {
            "full_identifier": identifier_for(rel),
            "signing_args": {
                "entitlements_path": "SIGNING_METADATA/Entitlements.entitlements"
            },
        }
    # Inside-out: sign the deepest binaries first.
    return dict(
        sorted(entries.items(), key=lambda kv: (-kv[0].count("/"), kv[0]))
    )


def main() -> int:
    # --list-machos <App.app>: print every Mach-O in the bundle, one
    # absolute path per line, for the pre-sign ad-hoc signature strip in
    # sign.sh (strip everything; CDSigner re-signs dylibs and the main
    # executable itself during the app pass).
    if len(sys.argv) == 3 and sys.argv[1] == "--list-machos":
        app_path = sys.argv[2]
        if not os.path.isdir(app_path):
            print(f"ERROR: .app not found at {app_path}", file=sys.stderr)
            return 1
        for rel in collect_all_machos(app_path):
            print(os.path.join(app_path, rel))
        return 0

    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <manifest-template.json> <App.app>", file=sys.stderr)
        return 1
    template_path, app_path = sys.argv[1], sys.argv[2]
    if not os.path.isdir(app_path):
        print(f"ERROR: .app not found at {app_path}", file=sys.stderr)
        return 1

    raw = open(template_path, encoding="utf-8").read()
    for var in ("SIGNER_ACCESS_ROLE_ARN", "SIGNING_BUCKET", "INPUT_KEY", "OUTPUT_KEY"):
        value = os.environ.get(var)
        if not value:
            print(f"ERROR: env {var} is required", file=sys.stderr)
            return 1
        raw = raw.replace("${%s}" % var, value)
    doc = json.loads(raw)

    generated = collect_entries(app_path)
    static = doc["manifest"]["app"].get("embedded_requirements", {})
    # Generated (deepest-first) ahead of the static Electron entries; a
    # static entry for the same path wins so hand overrides stay possible.
    merged = dict(generated)
    merged.update(static)
    doc["manifest"]["app"]["embedded_requirements"] = merged

    print(
        f"embedded_requirements: {len(merged)} total "
        f"({len(generated)} generated non-dylib Mach-Os, {len(static)} static; "
        "dylibs auto-signed by CDSigner)",
        file=sys.stderr,
    )
    json.dump(doc, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
