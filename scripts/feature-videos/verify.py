#!/usr/bin/env python3
"""Re-verify a produced feature-videos release folder before uploading it.

Answers one question: would the dashboard accept this folder? It recomputes
every hash from the bytes on disk against the signed manifest, and verifies the
manifest's signature against the committed release public key using the same
canonical-JSON rule and the same algorithm the runtime uses.

Read-only: it never writes to the folder and never touches the network, and it
never executes runtime code — which is why the canonical-JSON rule lives in this
tool and a test pins it to the runtime's verifier.

Usage:

    python3 scripts/feature-videos/verify.py dist/feature-videos/0.7.0
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _manifest import (  # noqa: E402
    PUBLIC_KEY_PATH,
    RUNTIME_LIMITS,
    SCHEMA,
    ManifestError,
    check_media_size,
    hash_file,
    load_json_object,
    validate_cdn_base,
    validate_duration,
    validate_release,
    validate_slug,
    verify_signature,
)

_ENTRY_FIELDS = frozenset(
    {
        "id",
        "feature",
        "title",
        "description",
        "file",
        "poster",
        "sha256",
        "poster_sha256",
        "bytes",
        "duration_s",
        "doc",
        "used_when",
        "min_version",
    }
)


def _check_entry_shape(entry: Any, index: int, seen: set[str]) -> dict[str, Any]:
    where = f"manifest entry {index}"
    if not isinstance(entry, dict):
        raise ManifestError(f"{where}: must be a JSON object")
    if set(entry) != _ENTRY_FIELDS:
        missing = sorted(_ENTRY_FIELDS - set(entry))
        extra = sorted(set(entry) - _ENTRY_FIELDS)
        detail = ", ".join(
            part
            for part in (
                f"missing: {', '.join(missing)}" if missing else "",
                f"unexpected: {', '.join(extra)}" if extra else "",
            )
            if part
        )
        raise ManifestError(f"{where}: field set does not match schema v1 ({detail})")

    entry_id = validate_slug(str(entry["id"]), where=where)
    if entry_id in seen:
        raise ManifestError(f"{where}: duplicate id {entry_id!r}")
    seen.add(entry_id)
    # The filenames are derived from the id rather than free text: they are
    # joined onto a directory path and onto the CDN base, so a value that is not
    # exactly the id's own basename is refused instead of sanitized.
    if entry["file"] != f"{entry_id}.mp4" or entry["poster"] != f"{entry_id}.jpg":
        raise ManifestError(f"{where}: file and poster must be {entry_id}.mp4 and {entry_id}.jpg")
    if not isinstance(entry["bytes"], int) or isinstance(entry["bytes"], bool):
        raise ManifestError(f"{where}: bytes must be an integer")
    # The publisher's own rule, not a second one: a bare ``> 0`` here would admit
    # infinity, so the two sides would disagree about what a valid release is.
    validate_duration(entry["duration_s"], where=where)
    if not isinstance(entry["used_when"], list) or not all(
        isinstance(signal, str) and signal for signal in entry["used_when"]
    ):
        raise ManifestError(f"{where}: used_when must be a list of non-empty strings")
    if not isinstance(entry["min_version"], str):
        raise ManifestError(f"{where}: min_version must be a string")
    return entry


def verify_folder(
    folder: Path,
    *,
    public_key: Path = PUBLIC_KEY_PATH,
) -> dict[str, Any]:
    """Verify *folder* completely, or raise :class:`ManifestError`.

    Returns a small report the caller prints. Ordered cheapest-first so a typo in
    the manifest is reported before megabytes are hashed, but every check runs
    against bytes on disk rather than against the manifest's own claims — the
    manifest is the thing under test.
    """
    if not folder.is_dir():
        raise ManifestError(f"not a directory: {folder}")
    manifest = load_json_object(
        folder / "manifest.json", limit=RUNTIME_LIMITS["max_document_bytes"]
    )

    if manifest.get("schema") != SCHEMA:
        raise ManifestError(f"unsupported schema: {manifest.get('schema')!r}")
    release = validate_release(str(manifest.get("release", "")))
    cdn_base = validate_cdn_base(str(manifest.get("cdn_base", "")))
    # The runtime builds every asset URL as ``<cdn_base>/<release>/<name>``, so
    # the base must name the feature-videos root and stop there: one carrying
    # this (or any) release would double the segment in every URL it derives.
    if not cdn_base.endswith("/feature-videos/"):
        raise ManifestError("cdn_base does not end in /feature-videos/")

    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ManifestError("manifest carries no entries")
    seen: set[str] = set()
    checked = [_check_entry_shape(entry, index, seen) for index, entry in enumerate(entries)]

    key_id = verify_signature(manifest, public_key=public_key)

    expected_names = {"manifest.json"}
    for entry in checked:
        pairs = ((entry["file"], entry["sha256"]), (entry["poster"], entry["poster_sha256"]))
        for name, claimed in pairs:
            expected_names.add(name)
            # A tampered folder is what this tool exists to inspect, so a symlink
            # here is refused rather than followed: hashing its target would
            # report the release as intact while the CDN serves other bytes. One
            # descriptor supplies both the digest and the size, so they cannot
            # end up describing two different files.
            actual, size = hash_file(folder / name, where="release folder")
            if actual != claimed:
                raise ManifestError(f"{name}: bytes hash to {actual}, manifest claims {claimed}")
            if name == entry["file"] and size != entry["bytes"]:
                raise ManifestError(f"{name} is {size} bytes, manifest claims {entry['bytes']}")
            check_media_size(name, size, kind="clip" if name == entry["file"] else "poster")

    # Recursive, and subdirectories are refused outright. ``aws s3 sync`` uploads
    # the whole tree, so anything nested here is served from the release prefix
    # under a signature that never covered it. A top-level-only scan reads a
    # planted `evil/payload.js` as an empty directory listing and passes. The only
    # name the manifest does not sign is the manifest itself.
    allowed = expected_names
    stray: list[str] = []
    for item in sorted(folder.rglob("*")):
        relative = item.relative_to(folder).as_posix()
        if item.is_symlink():
            stray.append(f"{relative} (symlink)")
        elif item.is_dir():
            stray.append(f"{relative}/")
        elif relative not in allowed:
            stray.append(relative)
    if stray:
        raise ManifestError(f"folder carries unsigned path(s): {', '.join(stray)}")

    return {
        "release": release,
        "cdn_base": cdn_base,
        "entries": len(checked),
        "key_id": key_id,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a feature-videos release folder.")
    parser.add_argument("folder", type=Path, help="a release folder produced by publish.py")
    parser.add_argument(
        "--public-key",
        type=Path,
        default=PUBLIC_KEY_PATH,
        help="public key to verify against; defaults to the committed release key",
    )
    args = parser.parse_args(argv)
    report = verify_folder(args.folder.resolve(), public_key=args.public_key)
    print(f"verified {args.folder}")
    print(f"  release    {report['release']}")
    print(f"  cdn_base   {report['cdn_base']}")
    print(f"  entries    {report['entries']}")
    print(f"  signed by  {report['key_id']}")
    return 0


if __name__ == "__main__":
    # OSError too: an unreadable folder is a refusal with a reason, not a traceback.
    try:
        raise SystemExit(main())
    except (ManifestError, OSError) as exc:
        print(f"verify: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
