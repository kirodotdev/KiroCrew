#!/usr/bin/env python3
"""Build a signed feature-videos release folder for the CDN.

Takes a directory of clips, posters and a ``catalog.json`` describing them, and
writes ``dist/feature-videos/<release>/`` holding the media, a signed
``manifest.json`` and a ``SHA256SUMS``. The folder is what gets uploaded; this
tool never uploads it. It prints the exact ``aws s3 sync --dryrun`` and
CloudFront invalidation commands and stops, so the credentials that can write to
a public origin stay with the human who owns them.

Signing reuses the CLI artifact manifest's trust root: same key, same
``RSASSA_PKCS1_V1_5_SHA_256``, same canonical-JSON bytes. Keys are separated by
purpose. Production signs with ``--kms-key-arn``, where the private half is a
non-exportable AWS KMS key that no human can read and the manifest records
``key_id`` as the hint that it was used. ``--signing-key`` takes a local private
key for staging and tests, omits ``key_id``, and says out loud that the result
is not a production artifact. Either way openssl verifies the signature before
anything reaches disk, so an unverifiable folder is never produced.

Usage:

    python3 scripts/feature-videos/publish.py \\
        --input <dir> --cdn-host videos.example.com --kms-key-arn <arn>
"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _manifest import (  # noqa: E402
    _O_BINARY,
    ALGORITHM,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_DOCUMENT_BYTES,
    DEFAULT_MAX_ENTRIES,
    DEFAULT_MAX_PAYLOAD_BYTES,
    PUBLIC_KEY_PATH,
    SCHEMA,
    ManifestError,
    canonical_bytes,
    check_document_size,
    check_entry_count,
    check_release_dir_is_free,
    check_signable,
    file_sha256,
    key_id_of,
    load_json_object,
    open_regular_file,
    parse_generated_at,
    public_key_der,
    require_text,
    run_openssl,
    validate_cdn_base,
    validate_doc,
    validate_duration,
    validate_release,
    validate_slug,
    verify_signature,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The runtime's tips doc allowlist, as a source file. Read as text, never
#: imported: a publishing tool must run from a bare checkout and must not
#: execute runtime code. Parsing the single source of truth is what keeps this
#: from becoming a second copy of the list that silently drifts, and a test
#: asserts the parse equals what the runtime exposes.
_ALLOWLIST_SOURCE = _REPO_ROOT / "src" / "kiro_crew" / "tips_allowlist.py"
_ALLOWLIST_NAME = "TIP_DOC_ALLOWLIST"

#: Ceiling on ``catalog.json`` itself. A release ships a handful of clips, so a
#: file past this is a mistake rather than a large catalog.
_MAX_CATALOG_BYTES = 256 * 1024

#: Codecs a browser ``<video>`` can play everywhere the dashboard runs.
_REQUIRED_VIDEO_CODEC = "h264"
_ALLOWED_AUDIO_CODECS = frozenset({"aac"})

#: A floor is a bare release, matching what the runtime's version compare reads.
_MIN_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*\Z")
_FFPROBE_TIMEOUT_SECS = 30


def _warn(message: str) -> None:
    print(f"publish: warning: {message}", file=sys.stderr)


def read_tip_doc_allowlist() -> frozenset[str]:
    """The runtime's allowed tip docs, parsed out of its source.

    Finds the module-level ``TIP_DOC_ALLOWLIST`` assignment and evaluates only
    its literal set. A shape this cannot read is an error rather than an empty
    allowlist: an empty one would refuse every doc, and silently permitting
    everything is the failure this gate exists to prevent.
    """
    tree = ast.parse(_ALLOWLIST_SOURCE.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        names = {t.id for t in targets if isinstance(t, ast.Name)}
        if _ALLOWLIST_NAME not in names or getattr(node, "value", None) is None:
            continue
        value = node.value
        # frozenset({...}) — take the call's single literal argument.
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            if value.func.id != "frozenset" or len(value.args) != 1:
                break
            value = value.args[0]
        try:
            literal = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            break
        if not isinstance(literal, (set, frozenset, list, tuple)) or not literal:
            break
        if not all(isinstance(item, str) for item in literal):
            break
        return frozenset(literal)
    raise ManifestError(
        f"could not read {_ALLOWLIST_NAME} from {_ALLOWLIST_SOURCE}; "
        "the allowlist's shape changed and this parser needs updating"
    )


def _repo_version() -> str:
    """The version in ``pyproject.toml``, used when ``--release`` is omitted."""
    text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        raise ManifestError("could not read version from pyproject.toml; pass --release")
    return match.group(1)


def _ffprobe_path() -> str | None:
    """Where ffprobe is, or None. A seam a test repoints for determinism."""
    return shutil.which("ffprobe")


def _ffprobe_json(path: Path) -> dict[str, Any] | None:
    """ffprobe's stream and format report for *path*, or None when unavailable.

    None means "could not inspect", never "inspected and fine": every caller
    degrades to a warning so a host without ffmpeg can still cut a release,
    and the size and hash checks — which need no external tool — still run.
    """
    ffprobe = _ffprobe_path()
    if ffprobe is None:
        return None
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_FFPROBE_TIMEOUT_SECS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _warn(f"ffprobe could not inspect {path.name}: {exc}")
        return None
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ManifestError(f"ffprobe rejected {path.name}: {detail or 'no detail'}")
    try:
        report = json.loads(proc.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _warn(f"ffprobe returned malformed JSON for {path.name}: {exc}")
        return None
    return report if isinstance(report, dict) else None


def _check_media_codecs(
    path: Path, report: dict[str, Any] | None, *, require_probe: bool = False
) -> None:
    """Require H.264 video and either AAC audio or no audio track at all.

    A silent clip is normal here — these are one-shot feature intros — so an
    absent audio stream passes and only a non-AAC one fails.

    *require_probe* makes an uninspectable clip an error instead of a warning.
    The production path sets it: a folder signed by the release key is one every
    dashboard trusts, and a VP9 clip hashes and verifies exactly as well as an
    H.264 one, so "could not check" must not become "signed anyway" there.
    Staging keeps the warning, which is what lets a host without ffmpeg work.
    """
    if report is None:
        if require_probe:
            raise ManifestError(
                f"{path.name}: ffprobe is required to sign a release with the release key "
                "(install ffmpeg, or use --signing-key for a staging folder)"
            )
        _warn(f"ffprobe unavailable; codec check skipped for {path.name}")
        return
    streams = report.get("streams")
    if not isinstance(streams, list):
        if require_probe:
            raise ManifestError(f"{path.name}: ffprobe reported no streams to check")
        _warn(f"ffprobe reported no streams for {path.name}; codec check skipped")
        return
    video = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"]
    audio = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"]
    if len(video) != 1:
        raise ManifestError(f"{path.name}: expected exactly one video stream, found {len(video)}")
    if video[0].get("codec_name") != _REQUIRED_VIDEO_CODEC:
        raise ManifestError(
            f"{path.name}: video codec is {video[0].get('codec_name')!r}, "
            f"expected {_REQUIRED_VIDEO_CODEC!r}"
        )
    for stream in audio:
        if stream.get("codec_name") not in _ALLOWED_AUDIO_CODECS:
            raise ManifestError(
                f"{path.name}: audio codec is {stream.get('codec_name')!r}, "
                "expected aac or no audio track"
            )


def _duration_from(report: dict[str, Any] | None) -> float | None:
    """ffprobe's container duration, or None when it is unusable.

    ffprobe reports ``N/A`` for a stream it cannot measure and can report a
    non-finite value; both read as "unknown" here, which makes the caller demand
    the catalog supply one rather than signing a number no parser accepts.
    """
    if report is None:
        return None
    container = report.get("format")
    raw = container.get("duration") if isinstance(container, dict) else None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or not value > 0:
        return None
    return value


def _validate_catalog_entry(
    raw: Any, index: int, seen: set[str], allowlist: frozenset[str]
) -> dict[str, Any]:
    where = f"catalog entry {index}"
    if not isinstance(raw, dict):
        raise ManifestError(f"{where}: must be a JSON object")
    unknown = set(raw) - {
        "id",
        "feature",
        "title",
        "description",
        "doc",
        "used_when",
        "min_version",
        "duration_s",
    }
    if unknown:
        raise ManifestError(f"{where}: unknown field(s): {', '.join(sorted(unknown))}")

    entry_id = validate_slug(require_text(raw, "id", where=where), where=where)
    if entry_id in seen:
        raise ManifestError(f"{where}: duplicate id {entry_id!r}")
    seen.add(entry_id)

    entry: dict[str, Any] = {
        "id": entry_id,
        "feature": require_text(raw, "feature", where=where),
        "title": require_text(raw, "title", where=where),
        "description": require_text(raw, "description", where=where),
        "doc": validate_doc(
            require_text(raw, "doc", where=where), where=where, allowlist=allowlist
        ),
    }

    used_when = raw.get("used_when", [])
    if not isinstance(used_when, list) or not all(
        isinstance(signal, str) and signal and len(signal) <= 200 for signal in used_when
    ):
        raise ManifestError(f"{where}: used_when must be a list of non-empty strings")
    # Signal NAMES are not checked against the runtime's probe registry: doing
    # so would mean executing runtime code from a publishing tool, and a second
    # copy of the registry here would drift from its answer. The runtime treats
    # an unregistered signal as "feature not used" and logs it, so a typo shows
    # the clip rather than hiding it.
    entry["used_when"] = list(used_when)

    min_version = raw.get("min_version", "")
    if not isinstance(min_version, str):
        raise ManifestError(f"{where}: min_version must be a string")
    if min_version and _MIN_VERSION_RE.fullmatch(min_version) is None:
        raise ManifestError(f"{where}: min_version must be a bare release like 0.7.0")
    entry["min_version"] = min_version

    duration = raw.get("duration_s")
    if duration is not None:
        entry["duration_s"] = validate_duration(duration, where=where)
    return entry


def _load_catalog(input_dir: Path) -> list[dict[str, Any]]:
    document = load_json_object(input_dir / "catalog.json", limit=_MAX_CATALOG_BYTES)
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ManifestError("catalog.json must carry a non-empty 'entries' array")
    allowlist = read_tip_doc_allowlist()
    seen: set[str] = set()
    return [
        _validate_catalog_entry(raw, index, seen, allowlist) for index, raw in enumerate(entries)
    ]


def _snapshot_asset(input_dir: Path, staging: Path, name: str, *, max_bytes: int) -> Path:
    """Copy one asset into *staging* through a single descriptor, and return the copy.

    The source is opened once, proven regular on that descriptor, and copied from
    it. Nothing re-resolves the name afterwards, so the file that gets probed,
    hashed and published is provably the one that passed the check — swapping a
    symlink in mid-publish changes nothing about the bytes already on this
    descriptor.

    The cap bounds the COPY rather than the source's stat, because the copy is
    what would be published, and it stops the copy rather than measuring it after.
    """
    staged = staging / name
    with open_regular_file(input_dir / name, where="input") as source:
        written = source.copy_to(staged, limit=max_bytes)
    if written == 0:
        raise ManifestError(f"{name} is empty")
    return staged


def _build_entries(
    catalog: list[dict[str, Any]],
    input_dir: Path,
    *,
    staging: Path,
    max_bytes: int,
    require_probe: bool = False,
) -> list[dict[str, Any]]:
    built: list[dict[str, Any]] = []
    for entry in catalog:
        clip_name = f"{entry['id']}.mp4"
        poster_name = f"{entry['id']}.jpg"
        clip = _snapshot_asset(input_dir, staging, clip_name, max_bytes=max_bytes)
        poster = _snapshot_asset(input_dir, staging, poster_name, max_bytes=max_bytes)
        clip_size = clip.stat().st_size

        report = _ffprobe_json(clip)
        _check_media_codecs(clip, report, require_probe=require_probe)
        duration = entry.get("duration_s")
        if duration is None:
            duration = _duration_from(report)
        if duration is None:
            raise ManifestError(
                f"{clip_name}: duration is unknown — set duration_s in catalog.json "
                "or install ffprobe"
            )

        built.append(
            {
                "id": entry["id"],
                "feature": entry["feature"],
                "title": entry["title"],
                "description": entry["description"],
                "file": clip_name,
                "poster": poster_name,
                "sha256": file_sha256(clip),
                "poster_sha256": file_sha256(poster),
                "bytes": clip_size,
                "duration_s": round(validate_duration(duration, where=clip_name), 3),
                "doc": entry["doc"],
                "used_when": entry["used_when"],
                "min_version": entry["min_version"],
            }
        )
    return built


def _run_aws_json(args: list[str]) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["aws", *args, "--output", "json", "--no-cli-pager"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManifestError("the AWS CLI is required for KMS signing") from exc
    if proc.returncode != 0 or len(proc.stdout) > 64 * 1024:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ManifestError(f"AWS KMS rejected the request: {detail or 'no detail'}")
    try:
        value = json.loads(proc.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError("AWS KMS returned a malformed response") from exc
    if not isinstance(value, dict):
        raise ManifestError("AWS KMS returned a malformed response")
    return value


def _sign_with_kms(payload: bytes, key_arn: str) -> bytes:
    """Sign *payload* with the release KMS key, pinned to the committed pubkey.

    The KMS key's public half must byte-match the one in the repository. Without
    that check a mistyped ARN would sign with some other key and produce a folder
    every dashboard silently refuses.
    """
    public_response = _run_aws_json(["kms", "get-public-key", "--key-id", key_arn])
    if public_response.get("KeyUsage") != "SIGN_VERIFY":
        raise ManifestError("release KMS key must have SIGN_VERIFY usage")
    if public_response.get("KeySpec") not in {"RSA_3072", "RSA_4096"}:
        raise ManifestError("release KMS key must be RSA_3072 or RSA_4096")
    algorithms = public_response.get("SigningAlgorithms")
    if not isinstance(algorithms, list) or ALGORITHM not in algorithms:
        raise ManifestError("release KMS key does not allow the required algorithm")
    encoded = public_response.get("PublicKey")
    if not isinstance(encoded, str):
        raise ManifestError("AWS KMS did not return a public key")
    try:
        kms_der = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ManifestError("AWS KMS returned an invalid public key") from exc
    if not hmac.compare_digest(kms_der, public_key_der(PUBLIC_KEY_PATH)):
        raise ManifestError("configured KMS key does not match the committed public key")

    digest = hashlib.sha256(payload).digest()
    sign_response = _run_aws_json(
        [
            "kms",
            "sign",
            "--key-id",
            key_arn,
            "--message",
            base64.b64encode(digest).decode("ascii"),
            "--message-type",
            "DIGEST",
            "--signing-algorithm",
            ALGORITHM,
            "--cli-binary-format",
            "base64",
        ]
    )
    encoded_signature = sign_response.get("Signature")
    if not isinstance(encoded_signature, str):
        raise ManifestError("AWS KMS did not return a signature")
    try:
        signature = base64.b64decode(encoded_signature, validate=True)
    except ValueError as exc:
        raise ManifestError("AWS KMS returned an invalid signature") from exc
    return signature


def _sign_with_key(payload: bytes, private_key: Path, scratch: Path) -> bytes:
    """Sign *payload* with a local private key, for staging and tests."""
    if not private_key.is_file():
        raise ManifestError(f"signing key is missing: {private_key}")
    payload_path = scratch / "payload.json"
    payload_path.write_bytes(payload)
    return run_openssl(["dgst", "-sha256", "-sign", str(private_key), str(payload_path)])


def sign_document(
    document: dict[str, Any],
    *,
    scratch: Path,
    signing_key: Path | None,
    kms_key_arn: str | None,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> dict[str, Any]:
    """Return *document* with ``key_id`` where applicable and a verified signature.

    Verification is not a courtesy: the signature is checked against the public
    half before the manifest is assembled, so a folder that reaches disk is one
    whose signature openssl already accepted over exactly these bytes.
    """
    if signing_key is not None:
        public_key = scratch / "public.pem"
        run_openssl(["pkey", "-in", str(signing_key), "-pubout", "-out", str(public_key)])
        # key_id is omitted for a local key. It is a hint about WHICH pinned key
        # signed, and a staging key is not one — claiming an id the runtime does
        # not pin would be a false hint, and claiming the pinned one would be a lie.
        signed = dict(document)
    else:
        public_key = PUBLIC_KEY_PATH
        signed = {**document, "key_id": key_id_of(PUBLIC_KEY_PATH)}

    payload = check_signable(signed, max_payload_bytes=max_payload_bytes)
    if signing_key is not None:
        signature = _sign_with_key(payload, signing_key, scratch)
    elif kms_key_arn:
        signature = _sign_with_kms(payload, kms_key_arn)
    else:  # pragma: no cover - argparse requires one of the two
        raise ManifestError("no signing method given")
    if not signature:
        raise ManifestError("signing produced no signature")

    manifest = {**signed, "signature": base64.b64encode(signature).decode("ascii")}
    verify_signature(manifest, public_key=public_key, max_payload_bytes=max_payload_bytes)
    return manifest


def _write_new_file(path: Path, data: bytes) -> None:
    """Create *path* and write *data*, refusing to write through anything existing.

    ``O_EXCL`` is the point: the destination was proven empty, and creating
    exclusively keeps that true even if something appears in the gap. A plain
    write would follow a symlink planted at the name and overwrite its target.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o644)
    except FileExistsError as exc:
        raise ManifestError(f"{path.name} already exists in the release folder") from exc
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _write_output(
    out_dir: Path,
    staging: Path,
    manifest: dict[str, Any],
    entries: list[dict[str, Any]],
    *,
    max_document_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES,
) -> None:
    """Publish the snapshot in *staging*, which is what was hashed and signed.

    The folder is assembled under a private sibling name and moved into place in
    one step. Assembling in the destination lets two publishes of one release
    interleave: both pass the emptiness check, both copy media, and whichever one
    wins ``manifest.json`` ends up serving the other's clips. A rename publishes
    a folder that is either complete or not there at all.
    """
    # Sized and the destination cleared before anything is written: a folder that
    # fails either check must not be left on disk looking publishable.
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    check_document_size(manifest_bytes, max_document_bytes=max_document_bytes)
    check_release_dir_is_free(out_dir)

    try:
        out_dir.parent.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError) as exc:
        raise ManifestError(
            f"output root {out_dir.parent} is not a directory; nothing was published"
        ) from exc
    pending = _new_assembly_dir(out_dir)
    try:
        for entry in entries:
            for name in (entry["file"], entry["poster"]):
                shutil.copyfile(staging / name, pending / name)

        _write_new_file(pending / "manifest.json", manifest_bytes)

        # manifest.json is listed too: SHA256SUMS is what an operator checks the
        # uploaded folder against, and a manifest missing from it would be the one
        # file the check could not see.
        lines = []
        for entry in entries:
            lines.append(f"{entry['sha256']}  {entry['file']}")
            lines.append(f"{entry['poster_sha256']}  {entry['poster']}")
        lines.append(f"{hashlib.sha256(manifest_bytes).hexdigest()}  manifest.json")
        _write_new_file(pending / "SHA256SUMS", ("\n".join(sorted(lines)) + "\n").encode("utf-8"))

        _rename_into_place(pending, out_dir)
    except BaseException:
        shutil.rmtree(pending, ignore_errors=True)
        raise


def _new_assembly_dir(out_dir: Path) -> Path:
    """Create the owner-only sibling directory the release is assembled in.

    Owner-only for the whole of its life, assembly and published folder alike.
    Signed media sits here between the copy and the rename, so anyone else who
    can write into this directory can swap a clip for one the manifest does not
    describe -- a group-writable mode is enough, and the signature over the
    original bytes stays perfectly valid. Only the operator running the upload
    ever reads a release folder, so widening the mode afterwards would buy
    nothing and reopen exactly that window.
    """
    for _ in range(5):
        pending = out_dir.parent / f".{out_dir.name}.{os.urandom(4).hex()}.pending"
        try:
            os.mkdir(pending, 0o700)
        except FileExistsError:
            continue
        return pending
    raise ManifestError(f"could not create a private folder to assemble {out_dir.name} in")


def _rename_into_place(pending: Path, out_dir: Path) -> None:
    """Move the assembled folder onto *out_dir*, or refuse and leave it alone.

    ``check_release_dir_is_free`` permits an empty destination, so clear that
    directory entry first -- ``rmdir`` refuses anything that holds bytes, so this
    cannot delete a release. Whatever the rename then hits is something that
    arrived during assembly, and the release does not get published over it.
    """
    if out_dir.is_dir() and not out_dir.is_symlink():
        try:
            os.rmdir(out_dir)
        except OSError:
            pass
    try:
        os.rename(pending, out_dir)
    except OSError as exc:
        raise ManifestError(
            f"{out_dir} appeared while the release was being assembled; nothing was published"
        ) from exc


def _print_upload_plan(
    out_dir: Path, release: str, bucket: str | None, distribution_id: str | None
) -> None:
    prefix = f"feature-videos/{release}/"
    target = f"s3://{bucket or '<BUCKET>'}/{prefix}"
    print()
    print("Nothing was uploaded. Run these yourself, in this order:")
    print()
    print(f"  aws s3 sync --dryrun {out_dir}/ {target}")
    print(f"  aws s3 sync {out_dir}/ {target}")
    print(
        f"  aws cloudfront create-invalidation --distribution-id "
        f"{distribution_id or '<DISTRIBUTION_ID>'} --paths '/{prefix}*'"
    )
    print()
    print(f"Verify the folder first: python3 scripts/feature-videos/verify.py {out_dir}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a signed feature-videos release folder.")
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="directory holding catalog.json plus <id>.mp4 and <id>.jpg per entry",
    )
    parser.add_argument(
        "--cdn-host", required=True, help="CDN host serving the release, e.g. videos.example.com"
    )
    parser.add_argument("--release", help="release version; defaults to pyproject.toml's version")
    parser.add_argument(
        "--output",
        type=Path,
        help="output root; defaults to dist/feature-videos beside the repository root",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=f"per-file size cap in bytes (default {DEFAULT_MAX_BYTES})",
    )
    parser.add_argument(
        "--max-payload-bytes",
        type=int,
        default=DEFAULT_MAX_PAYLOAD_BYTES,
        help=(
            "publishing cap on the canonical signed payload; stricter than the "
            f"runtime's own limit (default {DEFAULT_MAX_PAYLOAD_BYTES})"
        ),
    )
    parser.add_argument(
        "--max-document-bytes",
        type=int,
        default=DEFAULT_MAX_DOCUMENT_BYTES,
        help=(
            "publishing cap on manifest.json as fetched; stricter than the "
            f"runtime's own limit (default {DEFAULT_MAX_DOCUMENT_BYTES})"
        ),
    )
    parser.add_argument(
        "--max-entries",
        type=int,
        default=DEFAULT_MAX_ENTRIES,
        help=(
            "publishing cap on entry count; stricter than the runtime's own "
            f"limit (default {DEFAULT_MAX_ENTRIES})"
        ),
    )
    parser.add_argument("--s3-bucket", help="bucket name, used only to print the upload command")
    parser.add_argument(
        "--distribution-id", help="CloudFront id, used only to print the invalidation command"
    )
    signing = parser.add_mutually_exclusive_group(required=True)
    signing.add_argument(
        "--kms-key-arn", help="release KMS key ARN; the production path, key never leaves KMS"
    )
    signing.add_argument(
        "--signing-key",
        type=Path,
        help="local RSA private key; for staging and tests, not for a public release",
    )
    args = parser.parse_args(argv)

    input_dir = args.input.resolve()
    if not input_dir.is_dir():
        raise ManifestError(f"input is not a directory: {input_dir}")
    for name in ("max_bytes", "max_payload_bytes", "max_document_bytes", "max_entries"):
        if getattr(args, name) <= 0:
            raise ManifestError(f"--{name.replace('_', '-')} must be positive")

    release = validate_release(args.release or _repo_version())
    cdn_base = validate_cdn_base(f"https://{args.cdn_host}/feature-videos/{release}/")
    generated_at = parse_generated_at(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    catalog = _load_catalog(input_dir)
    check_entry_count(len(catalog), max_entries=args.max_entries)
    out_root = args.output or (_REPO_ROOT / "dist" / "feature-videos")
    # The ROOT is resolved and the release name is appended to it unresolved. A
    # planted symlink at the release name must reach check_release_dir_is_free as
    # a symlink; resolving the whole path first replaces it with its target, and
    # the guard then inspects wherever that points instead of refusing the link.
    out_dir = out_root.resolve() / release

    # One scratch directory spans snapshot, sign and publish. The bytes that were
    # hashed must be the bytes that get copied out, so the snapshot has to outlive
    # signing rather than the source being read again at the end.
    with tempfile.TemporaryDirectory(prefix="feature-videos-publish-") as scratch:
        scratch_dir = Path(scratch)
        staging = scratch_dir / "staging"
        staging.mkdir()
        entries = _build_entries(
            catalog,
            input_dir,
            staging=staging,
            max_bytes=args.max_bytes,
            # The release key's folder is trusted by every dashboard, so its media
            # must actually be inspected; a staging folder may still be built on a
            # host with no ffmpeg.
            require_probe=args.signing_key is None,
        )
        document: dict[str, Any] = {
            "schema": SCHEMA,
            "release": release,
            "cdn_base": cdn_base,
            "generated_at": generated_at,
            "entries": entries,
        }
        manifest = sign_document(
            document,
            scratch=scratch_dir,
            signing_key=args.signing_key,
            kms_key_arn=args.kms_key_arn,
            max_payload_bytes=args.max_payload_bytes,
        )
        _write_output(
            out_dir, staging, manifest, entries, max_document_bytes=args.max_document_bytes
        )

    payload_bytes = len(canonical_bytes({k: v for k, v in manifest.items() if k != "signature"}))
    print(f"wrote {out_dir}")
    print(f"  {len(entries)} entry/entries, signed payload {payload_bytes} bytes")
    if args.signing_key is not None:
        _warn(
            "signed with a local key and no key_id: this is a staging artifact. "
            "A production release is signed with --kms-key-arn."
        )
    else:
        print(f"  key_id {manifest['key_id']}")
    _print_upload_plan(out_dir, release, args.s3_bucket, args.distribution_id)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ManifestError as exc:
        print(f"publish: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
