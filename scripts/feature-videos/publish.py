#!/usr/bin/env python3
"""Sign a feature-videos release folder for the CDN, in place.

Put an ``<id>.mp4`` and an ``<id>.jpg`` for every entry of ``catalog.json`` in
``dist/feature-videos/<release>/``, then run this. It hashes every file where it
is, signs the result with the release KMS key and writes ``manifest.json`` into
the same folder. It never uploads: it prints the ``aws s3 sync`` and CloudFront
invalidation commands and stops, so the credentials that can write to a public
origin stay with the human who owns them.

Signing is the CLI artifact manifest's: same KMS key, same
``RSASSA_PKCS1_V1_5_SHA_256``, same canonical-JSON bytes, through the signer's
own code loaded by path. The signature is verified against the committed public
key before the manifest is written, so an unverifiable folder is never produced.

Usage:

    python3 scripts/feature-videos/publish.py \\
        --catalog catalog.json --cdn-host videos.example.com --kms-key-arn <arn>
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The tips doc allowlist is the runtime's list, imported from its
# dependency-free module so a catalog cannot point a clip at an internal note.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from _manifest import (  # noqa: E402
    PUBLIC_KEY_PATH,
    SCHEMA,
    ManifestError,
    canonical_bytes,
    check_document_size,
    check_entry_count,
    check_media_size,
    check_signable,
    hash_file,
    kms_sign_digest,
    load_json_object,
    parse_generated_at,
    public_key_der,
    public_key_id,
    require_text,
    validate_cdn_base,
    validate_cdn_host,
    validate_doc,
    validate_duration,
    validate_release,
    validate_slug,
    verify_signature,
)

from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Ceiling on ``catalog.json`` itself: a release ships a handful of clips.
_MAX_CATALOG_BYTES = 256 * 1024

#: A floor is a bare release, matching what the runtime's version compare reads.
_MIN_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*\Z")

_CATALOG_FIELDS = frozenset(
    {"id", "feature", "title", "description", "doc", "used_when", "min_version", "duration_s"}
)


def _repo_version() -> str:
    """The version in ``pyproject.toml``, used when ``--release`` is omitted."""
    text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        raise ManifestError("could not read version from pyproject.toml; pass --release")
    return match.group(1)


def _validate_catalog_entry(raw: Any, index: int, seen: set[str]) -> dict[str, Any]:
    where = f"catalog entry {index}"
    if not isinstance(raw, dict):
        raise ManifestError(f"{where}: must be a JSON object")
    unknown = set(raw) - _CATALOG_FIELDS
    if unknown:
        raise ManifestError(f"{where}: unknown field(s): {', '.join(sorted(unknown))}")

    entry_id = validate_slug(require_text(raw, "id", where=where), where=where)
    if entry_id in seen:
        raise ManifestError(f"{where}: duplicate id {entry_id!r}")
    seen.add(entry_id)

    used_when = raw.get("used_when", [])
    if not isinstance(used_when, list) or not all(
        isinstance(signal, str) and signal and len(signal) <= 200 for signal in used_when
    ):
        raise ManifestError(f"{where}: used_when must be a list of non-empty strings")
    # Signal names are not checked against the runtime's probe registry: the
    # runtime treats an unregistered signal as "feature not used" and logs it.

    min_version = raw.get("min_version", "")
    if not isinstance(min_version, str):
        raise ManifestError(f"{where}: min_version must be a string")
    if min_version and _MIN_VERSION_RE.fullmatch(min_version) is None:
        raise ManifestError(f"{where}: min_version must be a bare release like 0.7.0")

    # Required: nothing here inspects the media, so the catalog is the only
    # source of a duration, and the dashboard shows a clip's length from it.
    if "duration_s" not in raw:
        raise ManifestError(f"{where}: duration_s is required")

    return {
        "id": entry_id,
        "feature": require_text(raw, "feature", where=where),
        "title": require_text(raw, "title", where=where),
        "description": require_text(raw, "description", where=where),
        "doc": validate_doc(
            require_text(raw, "doc", where=where), where=where, allowlist=TIP_DOC_ALLOWLIST
        ),
        "used_when": list(used_when),
        "min_version": min_version,
        "duration_s": round(validate_duration(raw["duration_s"], where=where), 3),
    }


def load_catalog(path: Path) -> list[dict[str, Any]]:
    document = load_json_object(path, limit=_MAX_CATALOG_BYTES)
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ManifestError(f"{path.name} must carry a non-empty 'entries' array")
    seen: set[str] = set()
    return [_validate_catalog_entry(raw, index, seen) for index, raw in enumerate(entries)]


def check_release_dir(release_dir: Path, catalog: list[dict[str, Any]]) -> None:
    """The folder is exactly the catalog's media, and not yet a release.

    A folder already holding ``manifest.json`` is a release someone may be
    serving: a changed clip is a new release, never a re-signed one. And it must
    hold nothing the catalog does not name, because ``aws s3 sync`` uploads the
    whole folder and a stray file would be served under a signature that never
    covered it.
    """
    if release_dir.is_symlink() or not release_dir.is_dir():
        raise ManifestError(f"release folder is not a directory: {release_dir}")
    expected = {f"{entry['id']}.mp4" for entry in catalog} | {
        f"{entry['id']}.jpg" for entry in catalog
    }
    present = {item.name for item in release_dir.iterdir()}
    if "manifest.json" in present:
        raise ManifestError(
            f"{release_dir} already holds manifest.json: a release is never re-signed. Cut a "
            "new release, or, if nothing was uploaded, delete manifest.json and run again."
        )
    stray = sorted(present - expected)
    if stray:
        raise ManifestError(
            f"{release_dir} holds file(s) the catalog does not name: {', '.join(stray[:5])}"
            f"{' and more' if len(stray) > 5 else ''}"
        )
    missing = sorted(expected - present)
    if missing:
        raise ManifestError(f"{release_dir} is missing: {', '.join(missing[:5])}")


def build_entries(catalog: list[dict[str, Any]], release_dir: Path) -> list[dict[str, Any]]:
    """Hash every entry's media in place and return the manifest entries."""
    built: list[dict[str, Any]] = []
    for entry in catalog:
        clip_name = f"{entry['id']}.mp4"
        poster_name = f"{entry['id']}.jpg"
        clip_sha, clip_size = hash_file(release_dir / clip_name, where="release folder")
        poster_sha, poster_size = hash_file(release_dir / poster_name, where="release folder")
        check_media_size(clip_name, clip_size, kind="clip")
        check_media_size(poster_name, poster_size, kind="poster")
        built.append(
            {
                "id": entry["id"],
                "feature": entry["feature"],
                "title": entry["title"],
                "description": entry["description"],
                "file": clip_name,
                "poster": poster_name,
                "sha256": clip_sha,
                "poster_sha256": poster_sha,
                "bytes": clip_size,
                "duration_s": entry["duration_s"],
                "doc": entry["doc"],
                "used_when": entry["used_when"],
                "min_version": entry["min_version"],
            }
        )
    return built


def sign_document(document: dict[str, Any], *, kms_key_arn: str) -> dict[str, Any]:
    """Return *document* with ``key_id`` and a signature the committed key verifies.

    ``kms_sign_digest`` refuses before signing if the KMS key's public half is
    not the committed one, so a mistyped ARN cannot produce a folder every
    dashboard refuses. The signature is then verified against the committed
    public key before anything is written.
    """
    signed = {**document, "key_id": public_key_id(PUBLIC_KEY_PATH)}
    payload = check_signable(signed)
    signature = kms_sign_digest(
        kms_key_arn, public_key_der(PUBLIC_KEY_PATH), hashlib.sha256(payload).digest()
    )
    if not signature:
        raise ManifestError("signing produced no signature")
    manifest = {**signed, "signature": base64.b64encode(signature).decode("ascii")}
    verify_signature(manifest, public_key=PUBLIC_KEY_PATH)
    return manifest


def write_manifest(release_dir: Path, manifest: dict[str, Any]) -> None:
    """Write ``manifest.json`` beside the media, creating it exclusively.

    A write that fails part-way removes what it created, so the folder is left
    as the operator assembled it rather than as a half-release the next run
    refuses.
    """
    data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    check_document_size(data)
    path = release_dir / "manifest.json"
    try:
        handle = open(path, "xb")
    except FileExistsError as exc:
        raise ManifestError(f"{path.name} already exists in the release folder") from exc
    try:
        with handle:
            handle.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _print_upload_plan(release_dir: Path, release: str) -> None:
    prefix = f"feature-videos/{release}/"
    target = f"s3://<BUCKET>/{prefix}"
    folder = shlex.quote(str(release_dir))  # a space in the path must stay one word
    print()
    print("Nothing was uploaded. Run these yourself, in this order:")
    print()
    print(f"  python3 scripts/feature-videos/verify.py {folder}")
    print(f"  aws s3 sync --dryrun {folder}/ {target}")
    print(f"  aws s3 sync {folder}/ {target}")
    print(
        f"  aws cloudfront create-invalidation --distribution-id <DISTRIBUTION_ID> "
        f"--paths '/{prefix}*'"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sign a feature-videos release folder in place.")
    parser.add_argument(
        "--catalog",
        type=Path,
        required=True,
        help="catalog.json describing the entries; kept OUTSIDE the release folder",
    )
    parser.add_argument(
        "--release-dir",
        type=Path,
        help="the folder holding <id>.mp4 and <id>.jpg per entry (default dist/feature-videos/<release>)",
    )
    parser.add_argument(
        "--cdn-host", required=True, help="CDN host serving the release, e.g. videos.example.com"
    )
    parser.add_argument("--release", help="release version; defaults to pyproject.toml's version")
    parser.add_argument("--kms-key-arn", required=True, help="the release KMS key's ARN")
    args = parser.parse_args(argv)

    release = validate_release(args.release or _repo_version())
    # The base names the CDN's feature-videos ROOT, not this release's folder: the
    # runtime builds every asset URL as ``<cdn_base>/<release>/<name>``.
    cdn_base = validate_cdn_base(f"https://{validate_cdn_host(args.cdn_host)}/feature-videos/")
    generated_at = parse_generated_at(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    release_dir = args.release_dir or (_REPO_ROOT / "dist" / "feature-videos" / release)

    catalog = load_catalog(args.catalog)
    check_entry_count(len(catalog))
    check_release_dir(release_dir, catalog)
    entries = build_entries(catalog, release_dir)

    document: dict[str, Any] = {
        "schema": SCHEMA,
        "release": release,
        "cdn_base": cdn_base,
        "generated_at": generated_at,
        "entries": entries,
    }
    manifest = sign_document(document, kms_key_arn=args.kms_key_arn)
    write_manifest(release_dir, manifest)

    payload_bytes = len(canonical_bytes({k: v for k, v in manifest.items() if k != "signature"}))
    print(f"signed {release_dir}")
    print(f"  {len(entries)} entry/entries, signed payload {payload_bytes} bytes")
    print(f"  key_id {manifest['key_id']}")
    _print_upload_plan(release_dir, release)
    return 0


if __name__ == "__main__":
    # OSError too: a full disk or an unreadable folder is a refusal with a
    # reason, not a traceback.
    try:
        raise SystemExit(main())
    except (ManifestError, OSError) as exc:
        print(f"publish: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
