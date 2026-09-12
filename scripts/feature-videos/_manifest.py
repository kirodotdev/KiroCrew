"""Shared contract for the feature-videos release manifest.

The manifest is a signed document: ``signature`` is base64 at the top level and
covers canonical JSON of every other top-level field. Nested values are fine —
the canonical encoding sorts nested keys too, so publisher and verifier agree on
the bytes either way.

The canonical-JSON rule is COPIED here rather than imported. A publishing tool
runs from a bare checkout with nothing installed, and coupling it to the runtime
would make the tool's own correctness depend on a module it is meant to produce
input for. The copy is pinned to the runtime's behaviour by a test that signs
with this rule and verifies with the runtime's verifier, so a drift fails there
rather than in production.

One trust root and one algorithm, both the CLI artifact manifest's: a release
signs feature videos with the same offline key ``cli.sh`` pins, so no consumer
needs a second key to trust. Signing keys are separated by purpose — the
production key lives in AWS KMS and is never exportable, and a local key file is
for staging and tests only.
"""

from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_REPO_ROOT = Path(__file__).resolve().parents[2]

SCHEMA = "kirocrew-feature-videos-manifest-v1"

#: The CLI manifest's algorithm. KMS names it this; ``openssl dgst -sha256``
#: produces the same bytes for the local-key path.
ALGORITHM = "RSASSA_PKCS1_V1_5_SHA_256"

#: The committed public half of the release signing key — the same PEM the
#: runtime pins and ``cli.sh`` embeds. Read from the file rather than restated as
#: a constant, so there is nothing here that can drift from the trust root.
PUBLIC_KEY_PATH = _REPO_ROOT / "packaging" / "signing" / "cli-manifest-public.pem"

#: Top-level fields a manifest carries. ``key_id`` is optional: the key is
#: PINNED, so ``key_id`` never established trust — it is a publisher-side hint
#: about which key signed, and the runtime requires it to match only when
#: present.
REQUIRED_FIELDS = ("schema", "release", "cdn_base", "generated_at", "entries")
OPTIONAL_FIELDS = ("key_id",)

#: Same bound the runtime enforces on a decoded signature.
MAX_SIGNATURE_BYTES = 1024

#: Publisher ceiling on the canonical signed payload. Deliberately stricter than
#: the runtime's own ``_SIGNED_PAYLOAD_MAX_BYTES``: a release that only just fits
#: what today's consumer accepts has no headroom for a consumer that tightens,
#: and a catalog this size is already past the point where one manifest is the
#: right shape. Raise it with ``--max-payload-bytes`` when a release needs it.
DEFAULT_MAX_PAYLOAD_BYTES = 64 * 1024

#: Publisher ceiling on ``manifest.json`` as fetched, again stricter than the
#: runtime's ``_MANIFEST_MAX_BYTES``. Separate from the payload cap because the
#: published file is indented while the signed bytes are compact, so the file is
#: the larger of the two and has its own limit.
DEFAULT_MAX_DOCUMENT_BYTES = 256 * 1024

#: Publisher ceiling on entry count, under the runtime's own ``_MAX_ENTRIES``.
#: A catalog over the runtime's limit is refused whole rather than truncated, so
#: it must not leave here.
DEFAULT_MAX_ENTRIES = 500

#: Default per-file ceiling. A clip is fetched on first play by every dashboard
#: that has not seen it, so the cap is a bandwidth decision, not a disk one.
DEFAULT_MAX_BYTES = 25 * 1024 * 1024

_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_GENERATED_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
#: A release is a bare numeric version. The CDN path segment is built from it,
#: so anything needing escaping is refused rather than quoted.
_RELEASE_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*\Z")
#: Matches the runtime catalog's own ceiling on an id.
MAX_ID_CHARS = 100
_MAX_TEXT_CHARS = 2048
_OPENSSL_TIMEOUT_SECS = 30

#: Where a system openssl lives. Tried in order, before PATH.
_SYSTEM_BIN_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/run/current-system/sw/bin")


class ManifestError(ValueError):
    """A manifest, catalog or trust-root contract violation."""


def canonical_bytes(value: dict[str, Any]) -> bytes:
    """The exact byte form both signer and verifier hash.

    Sorted keys — nested ones too — compact separators, ASCII-escaped, one
    trailing newline. This is the runtime's rule, copied deliberately; the
    cross-check test is what keeps the two identical.
    """
    try:
        return (
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ManifestError(f"manifest payload cannot be canonicalized: {exc}") from exc


def signed_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    """Every top-level field the signature covers, i.e. all but ``signature``."""
    return {key: value for key, value in manifest.items() if key != "signature"}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_openssl() -> str:
    """The openssl to use, preferring a system directory over PATH.

    System directories first for the same reason the runtime pins them: a
    planted shim that exits 0 would report a bad signature as good. PATH is a
    fallback rather than a refusal because this tool also runs on developer
    machines whose openssl is a package-manager install outside those
    directories, and a tool that cannot run there would simply not be used.
    The trust decision that matters is the runtime's, which does not fall back.
    """
    for directory in _SYSTEM_BIN_DIRS:
        candidate = os.path.join(directory, "openssl")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("openssl")
    if found is None:
        raise ManifestError("openssl is required and was not found")
    return found


def run_openssl(args: list[str]) -> bytes:
    """Run openssl, raising :class:`ManifestError` on any failure.

    stderr is surfaced in the message but the argument list is not: a private
    key path is an argument, and a signing tool must not widen what it prints
    about the key it was handed.
    """
    try:
        proc = subprocess.run(
            [find_openssl(), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_OPENSSL_TIMEOUT_SECS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManifestError(f"openssl could not be run: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ManifestError(f"openssl rejected the request: {detail or 'no detail'}")
    return proc.stdout


def public_key_der(public_key: Path) -> bytes:
    """The SubjectPublicKeyInfo DER bytes of *public_key*, RSA-3072 or better."""
    if not public_key.is_file():
        raise ManifestError(f"public key is missing: {public_key}")
    if b"UNCONFIGURED" in public_key.read_bytes():
        raise ManifestError(f"public key is not configured: {public_key}")
    details = run_openssl(["pkey", "-pubin", "-in", str(public_key), "-text", "-noout"]).decode(
        "utf-8", errors="replace"
    )
    bits = re.search(r"Public-Key:\s*\((\d+)\s+bit\)", details)
    if bits is None or "Modulus:" not in details:
        raise ManifestError("release signing key must be RSA")
    if int(bits.group(1)) < 3072:
        raise ManifestError("release signing RSA key must be at least 3072 bits")
    return run_openssl(["pkey", "-pubin", "-in", str(public_key), "-outform", "DER"])


def key_id_of(public_key: Path) -> str:
    """The ``sha256:<hex>`` identity of *public_key*, computed as ``cli.sh`` does."""
    return f"sha256:{hashlib.sha256(public_key_der(public_key)).hexdigest()}"


def pinned_key_id() -> str:
    """The identity of the committed release key, derived from the committed PEM."""
    return key_id_of(PUBLIC_KEY_PATH)


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


#: ``O_NOFOLLOW`` makes the kernel refuse a symlink at open time, which is the
#: only way to check and read the same file. Absent on Windows, where the lstat
#: check below is the best available and is documented as such.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

#: A FIFO opened read-only BLOCKS until a writer appears, so opening one would
#: hang the publisher before any check could refuse it. Non-blocking makes the
#: open return so ``fstat`` can reject it; on a regular file it changes nothing.
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

#: Windows opens descriptors in text mode unless told otherwise, translating line
#: endings on read and write. Every byte this tool reads is hashed and every byte
#: it writes is listed in SHA256SUMS, so a translated byte is a broken signature.
_O_BINARY = getattr(os, "O_BINARY", 0)


class RegularFile:
    """An open descriptor on a file proven regular at open time.

    Every fact the publisher records about an asset -- its size, its digest, the
    bytes that reach the release folder -- is read through THIS descriptor. A
    helper that returned a validated path instead would leave the caller to open
    it again, and between those two resolutions the name can be replaced with a
    symlink; the target would then be hashed, signed and served from a public CDN
    under a perfectly valid signature. One open is what closes that, so this
    class exists to make a second one impossible rather than merely discouraged.
    """

    def __init__(self, fd: int, size: int, name: str) -> None:
        self.fd = fd
        self.size = size
        self.name = name

    def __enter__(self) -> "RegularFile":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:  # pragma: no cover - already closed
            pass

    def sha256(self) -> str:
        """Digest the descriptor's contents, from the start."""
        os.lseek(self.fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(self.fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()

    def copy_to(self, destination: Path, *, limit: int) -> int:
        """Copy the descriptor's contents to *destination*, refusing past *limit* bytes.

        The cap bounds the copy rather than measuring it afterwards: a source far
        over the limit would otherwise fill the staging directory before the check
        ran. Reading stops on the first byte past the limit, the same rule
        ``read_bounded`` applies to JSON. Returns bytes written.
        """
        os.lseek(self.fd, 0, os.SEEK_SET)
        written = 0
        with open(destination, "wb") as out:
            while True:
                room = limit + 1 - written
                chunk = os.read(self.fd, min(1024 * 1024, room))
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise ManifestError(f"{self.name} is over the {limit} byte cap")
                out.write(chunk)
        return written


def open_regular_file(path: Path, *, where: str) -> RegularFile:
    """Open *path* for reading, refusing a symlink, directory or device.

    The name is always ``<id>.mp4`` or ``<id>.jpg`` for an id already validated
    as a single-component lowercase slug, so it cannot traverse; what remains is
    a link or a special file at that leaf, and both are refused here at open
    time rather than after a separate stat.
    """
    try:
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_BINARY)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise ManifestError(
                f"{where}: {path.name} is a symlink; release media must be a regular file"
            ) from exc
        if exc.errno == errno.ENOENT:
            raise ManifestError(f"{where}: missing asset: {path.name}") from exc
        raise ManifestError(f"{where}: cannot read {path.name}: {exc.strerror}") from exc

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ManifestError(f"{where}: {path.name} is not a regular file")
        if not _O_NOFOLLOW and path.is_symlink():  # pragma: no cover - Windows only
            raise ManifestError(
                f"{where}: {path.name} is a symlink; release media must be a regular file"
            )
    except BaseException:
        os.close(fd)
        raise
    return RegularFile(fd, info.st_size, path.name)


def validate_duration(value: object, *, where: str) -> float:
    """A positive, finite duration.

    ``math.isfinite`` is the load-bearing half. A bare ``> 0`` admits infinity,
    and ``json.dumps`` renders that as the bare token ``Infinity`` — which is
    not JSON, so the signed bytes would be a document a strict parser refuses
    while the signature over them verifies perfectly.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestError(f"{where}: duration_s must be a number")
    try:
        # A JSON integer has no width limit, and float() on a 400-digit one raises
        # OverflowError -- which is neither ValueError nor TypeError, so it would
        # escape as a traceback instead of a refusal.
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ManifestError(f"{where}: duration_s is out of range") from exc
    if not math.isfinite(number):
        raise ManifestError(f"{where}: duration_s must be finite, not {value!r}")
    # Round FIRST, then validate: the rounded value is what gets signed, and a
    # duration under 0.0005 rounds to 0.0 -- which this tool's own verifier
    # refuses, so publishing it produces a folder nobody can validate.
    rounded = round(number, 3)
    if not rounded > 0:
        raise ManifestError(
            f"{where}: duration_s must be positive after rounding to milliseconds, "
            f"not {value!r}"
        )
    return rounded


def validate_release(value: str) -> str:
    if _RELEASE_RE.fullmatch(value) is None:
        raise ManifestError(f"release {value!r} must be a bare numeric version like 0.7.0")
    return value


def validate_key_id(value: str, *, where: str) -> str:
    if not value.startswith("sha256:") or _SHA256_RE.fullmatch(value[len("sha256:") :]) is None:
        raise ManifestError(f"{where}: key_id must be 'sha256:' followed by 64 hex characters")
    return value


def validate_doc(value: str, *, where: str, allowlist: frozenset[str]) -> str:
    """A video's doc must be a user-facing feature doc, the gate tips use.

    The allowlist is passed in rather than read here so the caller owns where it
    comes from; sharing the runtime's list is what stops a clip pointing at an
    internal design note, which a second copy of the list would eventually let
    through.
    """
    if value not in allowlist:
        raise ManifestError(f"{where}: doc {value!r} is not in the tips doc allowlist")
    return value


def validate_cdn_base(value: str) -> str:
    """An HTTPS directory URL with a trailing slash and nothing to strip.

    Query, fragment and userinfo are refused rather than dropped: the value is
    concatenated with a filename by every consumer, and a base carrying any of
    them would build a URL none of them agree on.
    """
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ManifestError(f"cdn_base is not a well-formed URL: {exc}") from exc
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


def parse_generated_at(value: str) -> str:
    if _GENERATED_AT_RE.fullmatch(value) is None:
        raise ManifestError(
            "generated_at must be an ISO 8601 UTC instant like 2026-01-31T09:00:00Z"
        )
    return value


def check_signable(
    payload: dict[str, Any], *, max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES
) -> bytes:
    """The canonical bytes of *payload*, refusing a document over the cap.

    Checked before signing as well as after: a release the runtime would refuse
    on size must fail while a human is still watching, not once it is on a CDN.
    """
    canonical = canonical_bytes(payload)
    if len(canonical) > max_payload_bytes:
        raise ManifestError(
            f"signed payload is {len(canonical)} bytes, over this tool's "
            f"{max_payload_bytes} byte publishing cap. Split the release, shorten the "
            "entry text, or raise --max-payload-bytes if the runtime still accepts it."
        )
    return canonical


def check_document_size(manifest_bytes: bytes, *, max_document_bytes: int) -> None:
    """Refuse a published ``manifest.json`` over this tool's own ceiling.

    The consumer reads a bounded number of bytes and refuses the rest, so a file
    that outgrows its limit is a release nobody can fetch. This bound sits under
    the consumer's, and it is checked while a person is watching rather than once
    per client.
    """
    if len(manifest_bytes) > max_document_bytes:
        raise ManifestError(
            f"manifest.json is {len(manifest_bytes)} bytes, over this tool's "
            f"{max_document_bytes} byte publishing cap; raise --max-document-bytes "
            "if the runtime still accepts it"
        )


def check_release_dir_is_free(out_dir: Path) -> None:
    """Refuse to publish into a destination that already holds anything.

    A release folder is immutable, and this is the whole rule: absent or empty.
    Comparing what is there against what is about to be written needs an
    exemption for the files this tool regenerates, and that exemption is a hole —
    a symlinked ``manifest.json`` is followed on write, and metadata that changed
    without the media changing rewrites a release someone already fetched. One
    rule with no exemption has neither problem.

    Refusing rather than deleting: the bytes may be a release still being served,
    and a publishing tool must not be the thing that removes them. The operator
    deletes the folder and re-runs, which is also what the docs tell them to do.
    """
    if not out_dir.exists():
        return
    if out_dir.is_symlink() or not out_dir.is_dir():
        raise ManifestError(f"output path exists and is not a directory: {out_dir}")
    existing = sorted(item.name for item in out_dir.iterdir())
    if existing:
        raise ManifestError(
            f"{out_dir} is not empty (holds {', '.join(existing[:5])}"
            f"{' and more' if len(existing) > 5 else ''}). A release folder is "
            "immutable — remove it and publish again, or publish to a new --output."
        )


def check_entry_count(count: int, *, max_entries: int) -> None:
    """Refuse a catalog over this tool's entry ceiling.

    The consumer refuses an over-long entry list whole rather than reading the
    first N, so an over-long release publishes nothing usable.
    """
    if count > max_entries:
        raise ManifestError(
            f"catalog holds {count} entries, over this tool's {max_entries} entry "
            "publishing cap; raise --max-entries if the runtime still accepts it"
        )


def verify_signature(
    manifest: dict[str, Any],
    *,
    public_key: Path,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> str:
    """Verify *manifest*'s signature against *public_key*. Returns its key id.

    Mirrors the runtime's decision, and raises instead of returning False so a
    human running this before an upload is told which part failed. The runtime's
    fail-safe direction is the opposite one — there, unverifiable means untrusted
    and silent — and that asymmetry is deliberate: this side is a person asking
    "is this publishable", that side is a program asking "may I honour this".
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

    expected_key_id = key_id_of(public_key)
    if "key_id" in manifest:
        claimed = manifest["key_id"]
        if not isinstance(claimed, str):
            raise ManifestError("key_id must be a string when present")
        validate_key_id(claimed, where="manifest")
        if claimed != expected_key_id:
            raise ManifestError(
                f"manifest names key_id {claimed} but the verifying key is {expected_key_id}"
            )

    payload = signed_payload(manifest)
    missing = [field for field in REQUIRED_FIELDS if field not in payload]
    if missing:
        raise ManifestError(f"manifest is missing field(s): {', '.join(missing)}")
    unknown = set(payload) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS)
    if unknown:
        raise ManifestError(f"manifest carries unknown field(s): {', '.join(sorted(unknown))}")
    canonical = check_signable(payload, max_payload_bytes=max_payload_bytes)

    import tempfile

    with tempfile.TemporaryDirectory(prefix="feature-videos-verify-") as scratch:
        root = Path(scratch)
        payload_path = root / "payload.json"
        signature_path = root / "signature.bin"
        payload_path.write_bytes(canonical)
        signature_path.write_bytes(signature)
        try:
            run_openssl(
                [
                    "dgst",
                    "-sha256",
                    "-verify",
                    str(public_key),
                    "-signature",
                    str(signature_path),
                    str(payload_path),
                ]
            )
        except ManifestError as exc:
            raise ManifestError(
                "signature does not verify against the release key "
                f"({expected_key_id}): tampered bytes or the wrong key"
            ) from exc
    return expected_key_id


def read_bounded(path: Path, *, limit: int) -> bytes:
    """Read at most *limit* bytes from *path*, refusing anything longer.

    Reads ``limit + 1`` and refuses on the extra byte. Reading the whole file and
    measuring it afterwards makes the limit decorative: a multi-gigabyte input
    exhausts memory before the check it was supposed to fail.
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
