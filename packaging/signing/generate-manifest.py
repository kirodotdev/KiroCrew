#!/usr/bin/env python3
"""Generate the signing manifest with full nested Mach-O coverage.

Why this exists: Apple notarization requires EVERY nested Mach-O binary to be
Developer-ID signed with hardened runtime + secure timestamp. The signing
service only auto-detects frameworks/dylibs under Contents/Frameworks; everything under
Contents/Resources (our embedded Python backend: the interpreter, every .so
C-extension, every vendored .dylib) plus Squirrel's ShipIt helper must be
listed explicitly in `embedded_requirements`, or notarization returns
`Invalid` (confirmed: submission 3fefb424, 72 rejected binaries).

The binary set changes whenever a Python dependency changes, the app is
renamed, or Electron is upgraded, so EVERYTHING is generated at sign time
from the actual .app rather than maintained by hand:
  - backend entries (Resources Mach-Os + ShipIt) via collect_entries()
  - Electron shell entries (helper .apps, Electron Framework + its Helpers)
    via collect_shell_entries(), identifiers read from each bundle's
    Info.plist

A fail-closed layout tripwire (validate_layout) aborts the sign with a clear
message when the bundle contains something this generator does not
understand (a Mach-O outside the known scopes, or a nested bundle under
Contents/Resources, which needs bundle-level signing that per-file entries
cannot provide). Unknown layouts must break loudly at sign time, not
silently at notarize time weeks later.

Usage:
    generate-manifest.py <manifest-template.json> <path/to/App.app>

Reads S3 substitution values from env (SIGNER_ACCESS_ROLE_ARN, SIGNING_BUCKET,
INPUT_KEY, OUTPUT_KEY) and prints the final manifest JSON to stdout. A summary
line is printed to stderr for build logs.
"""

import glob
import json
import os
import plistlib
import re
import struct
import sys

# Fallback identifier prefix when the app bundle's own CFBundleIdentifier
# cannot be read (never expected for a real electron-builder output).
APP_ID = "com.amazon.kiro.crew"

_ENTITLEMENTS = {"entitlements_path": "SIGNING_METADATA/Entitlements.entitlements"}

# Bundle suffixes that require bundle-level signing (identifier + sealed
# resources). Finding one under Contents/Resources is a layout error.
# Covers all code-bearing bundle types, not just app/framework: XPC
# services, loadable bundles, and legacy plugins are sealed bundles too.
_BUNDLE_SUFFIXES = (".app", ".framework", ".appex", ".xpc", ".bundle", ".plugin")

# The only places a Mach-O may live in a layout this generator understands:
#   Contents/MacOS/       -- main executable, signed by the signing service's app pass
#   Contents/Frameworks/  -- bundles auto-signed by the service + explicit shell
#                            entries from collect_shell_entries()
#   Contents/Resources/   -- embedded backend, enumerated by collect_entries()
_UNDERSTOOD_SCOPES = (
    "Contents/MacOS/",
    "Contents/Frameworks/",
    "Contents/Resources/",
)

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


def read_bundle_identifier(bundle_path: str) -> "str | None":
    """CFBundleIdentifier of a .app/.framework, or None if unreadable.

    Checks the flat app layout (Contents/Info.plist), the versioned
    framework layout (Versions/*/Resources/Info.plist), and the flat
    framework layout (Resources/Info.plist). plistlib handles both XML and
    binary plists.
    """
    candidates = [os.path.join(bundle_path, "Contents", "Info.plist")]
    candidates.extend(
        sorted(glob.glob(os.path.join(bundle_path, "Versions", "*", "Resources", "Info.plist")))
    )
    candidates.append(os.path.join(bundle_path, "Resources", "Info.plist"))
    for plist_path in candidates:
        if not os.path.isfile(plist_path):
            continue
        try:
            with open(plist_path, "rb") as fh:
                ident = plistlib.load(fh).get("CFBundleIdentifier")
        except Exception:
            continue
        if ident:
            return str(ident)
    return None


def app_identifier(app_path: str) -> str:
    """The app's own bundle id (prefix for generated identifiers). Derived
    from the bundle so an app rename cannot silently desynchronize the
    generated identifiers; falls back to the historical constant."""
    return read_bundle_identifier(app_path) or APP_ID


def identifier_for(rel_path: str, app_id: str = APP_ID) -> str:
    """Stable, unique bundle id derived from the full relative path.

    Derived from the whole path (not the basename) so duplicate filenames in
    different packages get distinct identifiers.
    """
    stem = rel_path
    if stem.startswith("Contents/Resources/"):
        stem = stem[len("Contents/Resources/"):]
    stem = os.path.splitext(stem)[0]
    # Proven pattern (matches other signing pipelines): collapse
    # everything non-alphanumeric, including dots, to hyphens so every
    # identifier segment is well-formed.
    suffix = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-")
    return f"{app_id}.{suffix}"


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


def validate_layout(machos: "list[str]") -> "list[str]":
    """Fail-closed tripwire. Returns a list of layout errors (empty = OK).

    Three classes of unknown layout, each of which would otherwise surface
    as a notarization `Invalid` weeks later with no bisectable trail:
      1. A Mach-O outside the understood scopes (e.g. Contents/PlugIns/*.appex,
         Contents/Library/LoginItems) -- nothing signs it.
      2. A nested bundle under Contents/Resources -- its binaries would be
         signed per-file, but Apple requires bundle-level signing (identifier
         + sealed resources) for nested bundles.
      3. A loose Mach-O under Contents/Frameworks with no .app/.framework
         in its path -- bundle interiors are auto-signed or explicitly
         listed, but a bare file dropped in Frameworks/ has no signing rule.
    """
    errors: "list[str]" = []
    for rel in machos:
        if not rel.startswith(_UNDERSTOOD_SCOPES):
            errors.append(
                f"Mach-O outside understood scopes: {rel}\n"
                "    No signing rule covers this location. Extend "
                "generate-manifest.py deliberately (and verify notarization) "
                "before shipping binaries here."
            )
            continue
        if rel.startswith("Contents/Frameworks/"):
            segments = rel.split("/")[2:-1]
            in_bundle = any(s.endswith((".app", ".framework")) for s in segments)
            # Loose .dylibs directly under Frameworks/ are auto-signed by
            # the signing service's app pass (documented + verified); only a loose
            # EXECUTABLE with no bundle in its path has no signing rule.
            if not in_bundle and not rel.endswith(".dylib"):
                errors.append(
                    f"Loose Mach-O executable under Contents/Frameworks: {rel}\n"
                    "    Not inside any .app/.framework bundle, so neither "
                    "the service's auto-signing nor the generated entries cover "
                    "it. Move it into a bundle or extend the generator "
                    "deliberately."
                )
            continue
        if rel.startswith("Contents/Resources/"):
            # Any path segment that is itself a bundle means per-file signing
            # would be the wrong granularity.
            for segment in rel.split("/")[2:-1]:
                if segment.endswith(_BUNDLE_SUFFIXES):
                    errors.append(
                        f"Nested bundle under Contents/Resources: {segment} (in {rel})\n"
                        "    Nested bundles need bundle-level signing; per-file "
                        "entries are rejected at notarization. Move it under "
                        "Contents/Frameworks or add explicit bundle handling."
                    )
                    break
    return errors


def collect_shell_entries(app_path: str) -> "dict[str, dict]":
    """Electron shell entries, derived from the actual bundle.

    Replaces the previously hand-maintained template entries, which encoded
    one specific Electron layout and one specific app name and silently
    rotted on either changing (exactly the bug class this generator exists
    to prevent). electron-builder stamps each shell bundle's
    CFBundleIdentifier with the app id prefix, so the identifiers are read,
    never synthesized:
      - every helper .app under Contents/Frameworks -> one entry
      - every framework that ships loose Helpers executables (Electron
        Framework's chrome_crashpad_handler) -> one entry for the framework
        plus one per helper executable, all under the framework's identifier
        (the recipe Apple has accepted)
      - frameworks without Helpers (Mantle, ReactiveObjC, Squirrel) stay
        unlisted: the signing service's app pass auto-signs them. ShipIt, the loose
        executable in Squirrel's Resources dir, is covered by
        collect_entries().
    """
    entries: "dict[str, dict]" = {}
    frameworks_dir = os.path.join(app_path, "Contents", "Frameworks")
    if not os.path.isdir(frameworks_dir):
        return entries
    for name in sorted(os.listdir(frameworks_dir)):
        bundle = os.path.join(frameworks_dir, name)
        rel = f"Contents/Frameworks/{name}"
        if os.path.islink(bundle) or not os.path.isdir(bundle):
            continue
        if name.endswith(".app"):
            ident = read_bundle_identifier(bundle)
            if not ident:
                raise RuntimeError(f"no CFBundleIdentifier readable for {rel}")
            entries[rel] = {
                "full_identifier": ident,
                "signing_args": dict(_ENTITLEMENTS),
            }
        elif name.endswith(".framework"):
            # Versioned layout (Versions/<v>/Helpers) and flat layout
            # (Helpers at framework root). Versions/Current is a symlink to
            # the real version dir; resolve helpers only through real dirs
            # or every helper enumerates twice.
            helper_dirs = sorted(
                d
                for d in glob.glob(os.path.join(bundle, "Versions", "*", "Helpers"))
                if not os.path.islink(os.path.dirname(d))
            )
            flat_helpers = os.path.join(bundle, "Helpers")
            if os.path.isdir(flat_helpers) and not os.path.islink(flat_helpers):
                helper_dirs.append(flat_helpers)
            helpers: "list[str]" = []
            for helper_dir in helper_dirs:
                for root, _dirs, files in os.walk(helper_dir):
                    for fname in files:
                        full = os.path.join(root, fname)
                        if not os.path.islink(full) and is_macho(full):
                            helpers.append(os.path.relpath(full, app_path))
            if not helpers:
                continue  # auto-signed by the signing service's app pass
            ident = read_bundle_identifier(bundle)
            if not ident:
                raise RuntimeError(f"no CFBundleIdentifier readable for {rel}")
            for helper_rel in sorted(helpers):
                entries[helper_rel] = {
                    "full_identifier": ident,
                    "signing_args": dict(_ENTITLEMENTS),
                }
            entries[rel] = {
                "full_identifier": ident,
                "signing_args": dict(_ENTITLEMENTS),
            }
    return entries


def collect_entries(app_path: str, app_id: str = APP_ID) -> "dict[str, dict]":
    """Backend manifest entries, matching the recipe proven to both sign and
    notarize for Python-runtime apps on this API:
      - EXCLUDE .dylib files (the signing service signs dynamic libraries
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
            # Frameworks dylibs are auto-signed by the signing service's app pass;
            # Resources dylibs are NOT (verified empirically) and must be listed.
            continue
        if rel.startswith("Contents/MacOS/"):
            continue
        in_resources = rel.startswith("Contents/Resources/")
        # Loose executables in a framework's Resources dir (Squirrel's
        # ShipIt today) are NOT auto-signed by the signing service's app pass and must
        # be listed (verified empirically -- the original 72-binary
        # rejection included ShipIt). Matched by LOCATION, not by name, so
        # a future Squirrel-like helper is signed instead of silently
        # skipped.
        in_framework_resources = (
            rel.startswith("Contents/Frameworks/")
            and ".framework/" in rel
            and "/Resources/" in "/" + rel.split(".framework/", 1)[1]
        )
        if not (in_resources or in_framework_resources):
            continue
        entries[rel] = {
            "full_identifier": identifier_for(rel, app_id),
            "signing_args": dict(_ENTITLEMENTS),
        }
    # Inside-out: sign the deepest binaries first.
    return dict(
        sorted(entries.items(), key=lambda kv: (-kv[0].count("/"), kv[0]))
    )


def main() -> int:
    # --list-machos <App.app>: print every Mach-O in the bundle, one
    # absolute path per line, for the pre-sign ad-hoc signature strip in
    # sign.sh (strip everything; the signing service re-signs dylibs and the main
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

    machos = collect_all_machos(app_path)
    layout_errors = validate_layout(machos)
    if layout_errors:
        print("ERROR: bundle layout not understood by this generator:", file=sys.stderr)
        for err in layout_errors:
            print(f"  - {err}", file=sys.stderr)
        print(
            "Failing the sign so this surfaces now instead of as a "
            "notarization rejection later.",
            file=sys.stderr,
        )
        return 1

    app_id = app_identifier(app_path)
    # Keep the manifest header in lockstep with the actual bundle: sign.sh
    # packages the tarball under the real .app name, so a renamed app with
    # a hardcoded template name would make the signing service look for a path that
    # is not in the archive. The template values remain as documentation;
    # the bundle is the source of truth.
    app_name = os.path.basename(os.path.normpath(app_path))
    doc["manifest"]["name"] = app_name
    for output in doc["manifest"].get("outputs", []):
        output["path"] = app_name
    doc["manifest"]["app"]["identifier"] = app_id
    backend = collect_entries(app_path, app_id)
    try:
        shell = collect_shell_entries(app_path)
    except RuntimeError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    static = doc["manifest"]["app"].get("embedded_requirements", {})
    # Backend + shell are generated; a template entry for the same path wins
    # so deliberate hand overrides stay possible. Final map is globally
    # sorted inside-out (deepest paths sign first).
    merged = dict(backend)
    merged.update(shell)
    merged.update(static)
    merged = dict(
        sorted(merged.items(), key=lambda kv: (-kv[0].count("/"), kv[0]))
    )
    doc["manifest"]["app"]["embedded_requirements"] = merged

    print(
        f"embedded_requirements: {len(merged)} total "
        f"({len(backend)} backend Mach-Os incl. Resources dylibs, "
        f"{len(shell)} Electron shell, {len(static)} template overrides; "
        "Frameworks dylibs auto-signed by the signing service)",
        file=sys.stderr,
    )
    json.dump(doc, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
