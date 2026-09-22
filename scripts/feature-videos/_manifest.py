"""Shared contract for the feature-videos release manifest.

The manifest is a signed document: ``signature`` is base64 at the top level and
covers canonical JSON of every other top-level field. The signing plumbing is
the CLI artifact manifest's own, loaded from ``packaging/signing/cli-manifest.py``
by path: the canonical-JSON rule, the key-id derivation, the openssl and AWS CLI
runners and the pinned KMS flow. One trust root and one algorithm, both the CLI
manifest's: a release signs feature videos with the same key ``cli.sh`` pins, so
no consumer needs a second key to trust.

Every limit here is the runtime's own number (``feature_videos_manifest`` and
``feature_videos_cache``), pinned by ``test_the_runtime_limits_are_the_consumer_s``:
a release over any of them is one every dashboard refuses or drops, so publishing
refuses it first, while a person is watching.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import importlib.util
import ipaddress
import json
import math
import re
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLI_MANIFEST_SIGNER = _REPO_ROOT / "packaging" / "signing" / "cli-manifest.py"


def _load_cli_manifest_signer() -> ModuleType:
    """The CLI artifact manifest signer, loaded by path (its name has a hyphen)."""
    spec = importlib.util.spec_from_file_location(
        "kirocrew_cli_manifest_signer", _CLI_MANIFEST_SIGNER
    )
    if spec is None or spec.loader is None:  # pragma: no cover - present in every checkout
        raise RuntimeError(f"cannot load {_CLI_MANIFEST_SIGNER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_signer = _load_cli_manifest_signer()

#: The signer's own objects, not copies: one error class, one algorithm, one set
#: of runners, one key identity, one KMS flow.
ManifestError = _signer.ManifestError
ALGORITHM: str = _signer.ALGORITHM
run_openssl = _signer.run_openssl
public_key_der = _signer.public_key_der
public_key_id = _signer.public_key_id
kms_sign_digest = _signer.kms_sign_digest
MAX_SIGNATURE_BYTES: int = _signer.MAX_SIGNATURE_BYTES

SCHEMA = "kirocrew-feature-videos-manifest-v1"

#: The committed public half of the release signing key -- the same PEM the
#: runtime pins and ``cli.sh`` embeds.
PUBLIC_KEY_PATH = _REPO_ROOT / "packaging" / "signing" / "cli-manifest-public.pem"

#: Every top-level field a manifest carries besides ``signature``.
REQUIRED_FIELDS = ("schema", "release", "cdn_base", "generated_at", "entries", "key_id")

RUNTIME_LIMITS: dict[str, int] = {
    "max_payload_bytes": 256 * 1024,  # feature_videos_manifest._SIGNED_PAYLOAD_MAX_BYTES
    "max_document_bytes": 1024 * 1024,  # feature_videos_manifest._MANIFEST_MAX_BYTES
    "max_entries": 1000,  # feature_videos_manifest._MAX_ENTRIES
    "max_clip_bytes": 64 * 1024 * 1024,  # feature_videos_manifest._MAX_ENTRY_BYTES
    "max_poster_bytes": 8 * 1024 * 1024,  # feature_videos_cache.MAX_POSTER_BYTES
    "max_duration_s": 3600,  # feature_videos_manifest._MAX_DURATION_S
}

#: The runtime refuses a media basename longer than this
#: (``feature_videos_manifest._SAFE_BASENAME_RE``), and ``<id>.mp4`` must fit.
RUNTIME_MAX_BASENAME_CHARS = 96
MAX_ID_CHARS = RUNTIME_MAX_BASENAME_CHARS - len(".mp4")

_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_GENERATED_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
#: Exactly ``major.minor.patch``, no leading zeros: the runtime's
#: ``running_release`` parses each component with ``int()`` and asks the CDN for
#: ``f"{major}.{minor}.{patch}"``, so this is the only folder name that survives
#: that round trip and is ever fetched.
_RELEASE_COMPONENT = r"(?:0|[1-9][0-9]{0,4})"
_RELEASE_RE = re.compile(rf"{_RELEASE_COMPONENT}(?:\.{_RELEASE_COMPONENT}){{2}}\Z")
_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_MAX_TEXT_CHARS = 2048


def canonical_bytes(value: dict[str, Any]) -> bytes:
    """The exact byte form both signer and verifier hash: the CLI signer's rule."""
    try:
        return _signer.canonical_json(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ManifestError(f"manifest payload cannot be canonicalized: {exc}") from exc


def signed_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    """Every top-level field the signature covers, i.e. all but ``signature``."""
    return {key: value for key, value in manifest.items() if key != "signature"}


def require_text(mapping: dict[str, Any], key: str, *, where: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value or len(value) > _MAX_TEXT_CHARS:
        raise ManifestError(f"{where}: field {key!r} must be non-empty text")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ManifestError(f"{where}: field {key!r} must not carry control characters")
    return value


def validate_slug(value: str, *, where: str) -> str:
    if _SLUG_RE.fullmatch(value) is None:
        raise ManifestError(f"{where}: id {value!r} is not a lowercase hyphenated slug")
    if len(value) > MAX_ID_CHARS:
        raise ManifestError(f"{where}: id {value!r} is longer than {MAX_ID_CHARS} characters")
    return value


def validate_duration(value: object, *, where: str) -> float:
    """A positive, finite duration in seconds, rounded to milliseconds.

    Rounded FIRST, then checked: the rounded value is what gets signed. Infinity
    is refused because ``json.dumps`` writes it as a bare ``Infinity`` token that
    is not JSON; a duration over the runtime's ceiling is refused because every
    dashboard reads it as unknown and shows nothing.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestError(f"{where}: duration_s must be a number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ManifestError(f"{where}: duration_s is out of range") from exc
    if not math.isfinite(number):
        raise ManifestError(f"{where}: duration_s must be finite, not {value!r}")
    rounded = round(number, 3)
    if not rounded > 0:
        raise ManifestError(f"{where}: duration_s must be positive, not {value!r}")
    ceiling = RUNTIME_LIMITS["max_duration_s"]
    if rounded > ceiling:
        raise ManifestError(
            f"{where}: duration_s {value!r} is over the runtime's {ceiling} second ceiling"
        )
    return rounded


def validate_release(value: str) -> str:
    if _RELEASE_RE.fullmatch(value) is None:
        raise ManifestError(
            f"release {value!r} must be a three-component numeric version like 0.7.0 with "
            "no leading zero: that is the only folder shape a dashboard asks for"
        )
    return value


def validate_key_id(value: str, *, where: str) -> str:
    if not value.startswith("sha256:") or _SHA256_RE.fullmatch(value[len("sha256:") :]) is None:
        raise ManifestError(f"{where}: key_id must be 'sha256:' followed by 64 hex characters")
    return value


def validate_doc(value: str, *, where: str, allowlist: frozenset[str]) -> str:
    """A video's doc must be a user-facing feature doc: the runtime's tips allowlist."""
    if value not in allowlist:
        raise ManifestError(f"{where}: doc {value!r} is not in the tips doc allowlist")
    return value


def validate_cdn_base(value: str) -> str:
    """An HTTPS directory URL with a trailing slash and nothing to strip.

    Every consumer concatenates a filename onto it, so a base with a query,
    fragment or credentials would build a URL none of them agree on.
    """
    try:
        parsed = urlsplit(value)
        port = parsed.port  # parsed lazily; raises on `:abc` or a value past 65535
    except ValueError as exc:
        raise ManifestError(f"cdn_base is not a well-formed URL: {exc}") from exc
    if port == 0:
        raise ManifestError("cdn_base is not a well-formed URL: port 0 cannot be connected to")
    if parsed.scheme != "https":
        raise ManifestError("cdn_base must be an https URL")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ManifestError("cdn_base must name a host and carry no credentials")
    if parsed.query or parsed.fragment:
        raise ManifestError("cdn_base must carry no query string or fragment")
    if not value.endswith("/"):
        raise ManifestError("cdn_base must end with a slash")
    if "//" in parsed.path or ".." in parsed.path:
        raise ManifestError("cdn_base path must not carry an empty segment or a traversal")
    return value


def _is_dns_name_or_ip(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    if len(hostname) > 253:
        return False
    return all(_DNS_LABEL_RE.fullmatch(label) for label in hostname.split("."))


def validate_cdn_host(value: str) -> str:
    """A DNS name or IP literal with an optional port: what ``--cdn-host`` may carry.

    The host is signed into every asset URL while the upload plan names the
    bucket's ``feature-videos/<release>/``, so a path, credentials, a space or a
    stray character in it would produce URLs no dashboard can fetch.
    """
    try:
        parsed = urlsplit(f"https://{value}/")
        port = parsed.port
    except ValueError as exc:
        raise ManifestError(f"--cdn-host is not a well-formed host: {exc}") from exc
    if (
        parsed.netloc != value
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
    ):
        raise ManifestError(
            "--cdn-host must be a host name with an optional port, like "
            "videos.example.com, and carry no path, credentials, query or fragment"
        )
    if not _is_dns_name_or_ip(parsed.hostname):
        raise ManifestError(f"--cdn-host {value!r} is not a DNS host name or an IP address")
    return value


def parse_generated_at(value: str) -> str:
    if _GENERATED_AT_RE.fullmatch(value) is None:
        raise ManifestError(
            "generated_at must be an ISO 8601 UTC instant like 2026-01-31T09:00:00Z"
        )
    return value


def check_signable(payload: dict[str, Any]) -> bytes:
    """The canonical bytes of *payload*, refusing a document over the runtime's cap."""
    canonical = canonical_bytes(payload)
    limit = RUNTIME_LIMITS["max_payload_bytes"]
    if len(canonical) > limit:
        raise ManifestError(
            f"signed payload is {len(canonical)} bytes, over the runtime's {limit} byte "
            "limit; split the release or shorten the entry text"
        )
    return canonical


def check_document_size(manifest_bytes: bytes) -> None:
    limit = RUNTIME_LIMITS["max_document_bytes"]
    if len(manifest_bytes) > limit:
        raise ManifestError(
            f"manifest.json is {len(manifest_bytes)} bytes, over the runtime's {limit} byte limit"
        )


def check_entry_count(count: int) -> None:
    limit = RUNTIME_LIMITS["max_entries"]
    if count > limit:
        raise ManifestError(
            f"catalog holds {count} entries, over the runtime's {limit} entry limit"
        )


def check_media_size(name: str, size: int, *, kind: str) -> None:
    """Refuse an empty media file or one over the runtime's cap for its *kind*."""
    if size == 0:
        raise ManifestError(f"{name} is empty")
    limit = RUNTIME_LIMITS[f"max_{kind}_bytes"]
    if size > limit:
        raise ManifestError(f"{name} is {size} bytes, over the runtime's {limit} byte {kind} limit")


def hash_file(path: Path, *, where: str) -> tuple[str, int]:
    """The sha256 and size of the regular file at *path*.

    A symlink is refused rather than followed: the bytes hashed must be the
    bytes in the folder, because that folder is what gets uploaded.
    """
    if path.is_symlink():
        raise ManifestError(f"{where}: {path.name} is a symlink; media must be a regular file")
    if not path.is_file():
        raise ManifestError(f"{where}: missing asset: {path.name}")
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def verify_signature(manifest: dict[str, Any], *, public_key: Path) -> str:
    """Verify *manifest*'s signature against *public_key*; returns its key id.

    Raises with the failing part named, so a person running this before an
    upload learns what is wrong. The runtime's side answers the opposite
    question (may I honour this) and fails silently by design.
    """
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be a JSON object")
    if manifest.get("schema") != SCHEMA:
        raise ManifestError(f"unsupported manifest schema: {manifest.get('schema')!r}")

    signature_b64 = manifest.get("signature")
    if not isinstance(signature_b64, str) or not signature_b64:
        raise ManifestError("manifest is missing its signature")
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ManifestError("manifest signature is not valid base64") from exc
    if not signature or len(signature) > MAX_SIGNATURE_BYTES:
        raise ManifestError("manifest signature has an invalid size")

    expected_key_id = public_key_id(public_key)
    payload = signed_payload(manifest)
    missing = [field for field in REQUIRED_FIELDS if field not in payload]
    if missing:
        raise ManifestError(f"manifest is missing field(s): {', '.join(missing)}")
    unknown = set(payload) - set(REQUIRED_FIELDS)
    if unknown:
        raise ManifestError(f"manifest carries unknown field(s): {', '.join(sorted(unknown))}")
    claimed = payload["key_id"]
    if not isinstance(claimed, str):
        raise ManifestError("key_id must be a string")
    validate_key_id(claimed, where="manifest")
    if claimed != expected_key_id:
        raise ManifestError(
            f"manifest names key_id {claimed} but the verifying key is {expected_key_id}"
        )
    canonical = check_signable(payload)

    with tempfile.TemporaryDirectory(prefix="feature-videos-verify-") as scratch:
        payload_path = Path(scratch) / "payload.json"
        signature_path = Path(scratch) / "signature.bin"
        payload_path.write_bytes(canonical)
        signature_path.write_bytes(signature)
        try:
            run_openssl(
                [
                    "dgst",
                    "-sha256",
                    "-verify",
                    str(Path(public_key).absolute()),
                    "-signature",
                    str(signature_path),
                    str(payload_path),
                ]
            )
        except ManifestError as exc:
            raise ManifestError("manifest signature does not verify against the key") from exc
    return expected_key_id


def read_bounded(path: Path, *, limit: int) -> bytes:
    """Read at most *limit* bytes from *path*, refusing anything longer.

    Reads ``limit + 1`` and refuses on the extra byte, so a huge input fails
    the check instead of exhausting memory first.
    """
    if not path.is_file():
        raise ManifestError(f"missing file: {path}")
    with open(path, "rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ManifestError(f"{path} is larger than {limit} bytes")
    return raw


def load_json_object(path: Path, *, limit: int) -> dict[str, Any]:
    """Read a JSON object from *path*, rejecting duplicate keys and oversize input."""
    raw = read_bounded(path, limit=limit)

    def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: dict[str, Any] = {}
        for key, value in pairs:
            if key in seen:
                raise ManifestError(f"{path}: duplicate JSON key {key!r}")
            seen[key] = value
        return seen

    try:
        value = json.loads(raw, object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{path} is not valid JSON: {exc}") from exc
    except RecursionError as exc:
        raise ManifestError(f"{path} is nested too deeply to be a catalog") from exc
    if not isinstance(value, dict):
        raise ManifestError(f"{path} must hold a JSON object")
    return value
