"""Shadow-venv update engine for the ``cli.sh`` managed-venv install shape.

The managed venv (``${KIROCREW_HOME}-venv``) is the one install whose bytes the
gateway process is itself executing, so an in-place ``pip install --upgrade``
would overwrite a live runtime — the torn-runtime hazard
``docs/request-for-change/rfc-update-architecture.md`` §3 exists to prevent.
This module implements the versioned-trees-with-atomic-promotion design that
RFC's §3 reduced to invariants:

* **A new version is built into a FRESH sibling tree** (``crew-venv-<version>``)
  while the old gateway keeps serving from its own tree, untouched.
* **Promotion is a symlink replaced via ``os.replace``** — sibling symlink +
  ``rename(2)``, never ``ln -sfn`` (unlink+create has a missing-path window).
* **The stable path is a NEW NAME** (``crew-venv-current``) that has always
  been a symlink. The legacy fixed directory ``crew-venv`` is never renamed,
  moved, or converted: renaming it would break its own absolute shebangs while
  a gateway may still be running from it. It remains a functional fallback.
* **No tree a live process might be using is ever moved or deleted.** Pruning
  skips the stable link's target, the tree serving this process, and the
  legacy directory.

Authenticity comes from the same offline trust root ``cli.sh`` pins: the
channel feed serves an RSA-signed manifest, the signature is verified against
the public key embedded below (byte-identical to ``cli.sh``'s copy — a drift
test reads both), and the wheel's SHA-256 must match the SIGNED digest. The
feed alone is never trusted for anything actionable.

What this engine deliberately does NOT change, stated for parity rather than
guarded against: the shadow ``pip install`` resolves the wheel's dependencies
from the index exactly as ``cli.sh``'s own venv branch does today, so the
provenance story covers the Kiro Crew wheel itself, not the dependency set.
Raising that bar (hash-pinned constraints inside the signed payload) is worth
doing for the installer and this engine together, not here alone.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from kiro_crew.config.paths import data_home
from kiro_crew.platform_compat import (
    IS_POSIX,
    make_owner_only_dir,
    release_lock,
    trusted_system_bin,
    try_acquire_lock,
)

logger = logging.getLogger(__name__)

# ── Offline trust root ──────────────────────────────────────────────────────
# MUST stay byte-identical to the constants in cli.sh at the repo root; the
# drift test in test/test_wheel_engine.py reads both and fails the build when
# they disagree. Rotation follows packaging/signing/README.md's dual-trust
# sequence — never an in-place edit.
CLI_MANIFEST_KEY_ID = "sha256:d3a83f0c1ff84a2cbee6bd34d889d8725af34358148a6c18ed3ecbbbcceec06b"
CLI_MANIFEST_PUBLIC_KEY_B64 = (
    "LS0tLS1CRUdJTiBQVUJMSUMgS0VZLS0tLS0KTUlJQm9qQU5CZ2txaGtpRzl3MEJBUUVGQUFPQ0FZOEFN"
    "SUlCaWdLQ0FZRUF0MnR0NnZ3ZFZ4Z0tWbTRGQVdkeApwZjZFckx3Y2ljUHlHUGh2SXdXRTRqNmg1Yjlw"
    "MzFiaktMaWlEakxvK3VpQUJPL21vUjdJUUtoaUNSaXY0d0dTCk1mYnd2ZnNhLy8xNlVBbkNURkRDb1pI"
    "d0IwVm93cTRYWjZ1NHBrdTFqNlBlRXBMNjVqRXZvcjd1a29HS2xiOVMKQlBva01aN0VtYlpWbmJiSWJB"
    "VXYrZ0NWajRCWDRpam5GWkJEMmNPcmtkQWdGR3UraU9jRHVlRDNqTExicXVhUwp0K0tLWXltQ2VxaitP"
    "azZ0OFBMQ2VRZmYrWVc4YS9wRU03Wm1tMTJ0Y3BRdEF0OHVCSVdkZE9qaTN1c3BhVlA3CkZJUlhzNnJI"
    "ajIwTDd0dE9kMGpmKzRWQ0ZtV09FWE4rNWc0YS8rNkcrc3lxeDk4VlR2RVF5cDZVdWZnb0FoQkMKLzFV"
    "NG5XajdmMVRFQkV4dXBSRXFUK1lmUmp6aFJUR2NGN0czRUp3MmZjUU1taElIdFpVanM3endVY3NmblhD"
    "MwpGQzJBR3pBZnExSGV0WHU5amFOQWZSdjdLZXYxT2hvVmMzYUlONEd3UkpZRDNPNUFSQk5SRGpQUVFW"
    "UHBaVW5rCjB1WVdpZExSVDVRUVZMYnlSLzJFKytqTWFyRXBkVXRkZGY1anlwZW5pbFhUQWdNQkFBRT0K"
    "LS0tLS1FTkQgUFVCTElDIEtFWS0tLS0tCg=="
)

#: Same schema string cli.sh and the dashboard check enforce.
_CLI_MANIFEST_SCHEMA = "kirocrew-cli-artifact-manifest-v1"

#: Same ceiling cli.sh applies to the manifest with ``--max-filesize``.
_MANIFEST_MAX_BYTES = 65536

#: Received-bytes ceiling for the wheel download. The wheel is tens of MB
#: today; the cap exists so a misbehaving origin cannot fill the disk, not as
#: a tight bound. Enforced against received bytes, never Content-Length.
_WHEEL_MAX_BYTES = 500 * 1024 * 1024

#: Free-space floor on the venv parent before a shadow build is attempted.
#: A shadow tree is a full second install; failing before pip half-fills the
#: disk beats an ENOSPC mid-build. Sized to a current install (~350 MiB with
#: dependencies) plus headroom.
_SHADOW_MIN_FREE_BYTES = 1 * 1024 * 1024 * 1024

_FETCH_TIMEOUT_SECS = 30
_WHEEL_FETCH_TIMEOUT_SECS = 300
_OPENSSL_TIMEOUT_SECS = 30
_VENV_CREATE_TIMEOUT_SECS = 120
#: pip resolves and downloads the full dependency set into a tree that has
#: never seen it — the same bound dep_sync uses for a cold reinstall, doubled
#: because a shadow build also compiles any sdist fallbacks from scratch.
_PIP_INSTALL_TIMEOUT_SECS = 900
_PROBE_TIMEOUT_SECS = 60

# Signed-but-optional, mirroring cli.sh: a breaking release adds a fleet
# floor (`min_version`). The signature still covers it (it stays in the
# canonical payload), so the set check tolerates exactly this key and
# nothing else. The floor is metadata for RUNNING installs; this engine
# always installs the signed version itself, so format is all it checks.
_MANIFEST_OPTIONAL_FIELDS = frozenset({"min_version"})
_MIN_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*\Z")

_MANIFEST_EXPECTED_FIELDS = {
    "algorithm",
    "channel",
    "key_id",
    "pub_date",
    "python_requires",
    "schema",
    "sha256",
    "signature",
    "version",
    "wheel_url",
}

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PUB_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class WheelUpdateError(Exception):
    """A wheel update step failed; the message is operator-facing.

    Every raise site leaves the install exactly as it found it: the stable
    link is only ever touched by :func:`promote`, which is the last step and
    is atomic.
    """


@dataclass(frozen=True)
class ManagedVenvLayout:
    """Where the managed install's trees live for this data home."""

    #: The legacy fixed venv cli.sh installs into (``crew-venv``). Never
    #: written by this engine; functional fallback per the first-migration
    #: protocol.
    legacy: Path
    #: The stable symlink (``crew-venv-current``) every launch path should
    #: resolve through. May not exist yet on an unmigrated install.
    stable_link: Path

    def versioned_tree(self, version: str) -> Path:
        """The sibling tree a given version installs into."""
        return self.stable_link.with_name(f"{self.legacy.name}-{version}")

    def is_managed_tree(self, path: Path) -> bool:
        """Is *path* inside one of this layout's trees (legacy or versioned)?"""
        try:
            resolved = path.resolve()
        except OSError:
            return False
        prefix = f"{self.legacy.name}-"
        # Identity trust extends ONLY to the legacy root — the one path this
        # layout owns unconditionally. Every versioned sibling (a genuinely
        # promoted tree included) goes through the convention-plus-artifacts
        # branch below, which every real install satisfies (bin/kirocrew ships
        # with every completed build, the sentinel with every in-flight one).
        # The stable link's TARGET is deliberately NOT an identity root: the
        # respawn guard uses this predicate to detect a link repointed OUTSIDE
        # the layout, and trusting the target by identity would make that
        # check vacuously true.
        try:
            legacy_resolved = self.legacy.resolve()
            if resolved == legacy_resolved or resolved.is_relative_to(legacy_resolved):
                return True
        except OSError:
            pass
        # A tree created after this snapshot: matched by naming convention in
        # the same parent — but POSITIVELY identified, never by prefix alone.
        # `KIROCREW_VENV=/srv/crew` makes the prefix "crew-", which a sibling
        # like /srv/crew-dev (someone's unrelated venv) satisfies by name; a
        # restart would then resolve through /srv/crew-current and switch an
        # unmanaged process's runtime. A managed versioned tree additionally
        # carries this engine's own artifacts: the kirocrew console script
        # (every completed install ships it) or the build sentinel (every
        # in-flight build does).
        parent = self.legacy.parent
        try:
            if resolved.is_relative_to(parent.resolve()):
                rel = resolved.relative_to(parent.resolve())
                head = rel.parts[0] if rel.parts else ""
                if head == self.legacy.name:
                    return True
                if not head.startswith(prefix):
                    return False
                candidate = parent / head
                return (candidate / "bin" / "kirocrew").exists() or (
                    candidate / _SHADOW_SENTINEL
                ).exists()
        except OSError:
            pass
        return False


def managed_venv_layout() -> ManagedVenvLayout:
    """Resolve the managed-venv layout for this install.

    Mirrors cli.sh's path rule exactly: ``KIROCREW_VENV`` wins, else the venv
    lives BESIDE the data home (``${KIROCREW_HOME%/}-venv``) — never inside
    it, for the blast-radius reason cli.sh documents.
    """
    override = os.environ.get("KIROCREW_VENV", "").strip()
    if override:
        legacy = Path(os.path.abspath(override))
    else:
        legacy = Path(f"{str(data_home()).rstrip('/')}-venv")
    return ManagedVenvLayout(
        legacy=legacy,
        stable_link=legacy.with_name(f"{legacy.name}-current"),
    )


def running_from_managed_venv(layout: ManagedVenvLayout | None = None) -> bool:
    """Is THIS process served by the managed venv (legacy or versioned tree)?

    The dispatch predicate for the shadow path: a pipx install, a system
    Python, or a dev venv must never take it — their bytes are owned by
    something else, and building a sibling tree beside a data home they do not
    use would be litter at best. POSIX-only by construction: cli.sh is a POSIX
    installer, so on Windows there is no managed venv to detect.
    """
    if not IS_POSIX:
        return False
    if layout is None:
        layout = managed_venv_layout()
    return layout.is_managed_tree(Path(sys.executable))


def respawn_executable() -> str:
    """The interpreter a gateway restart should exec.

    ``sys.executable`` is the answer for every install shape EXCEPT a managed
    venv that has been promoted past the tree this process started from: there
    the cached path resolves into the OLD versioned tree (still on disk, so
    the exec would succeed — and silently resurrect the old version). Routing
    through the stable link is what makes a restart pick up a promotion, and
    it is one of the four persisted launch paths RFC §3 requires to resolve
    through the stable name.

    Falls back to ``sys.executable`` whenever the stable link does not exist
    or does not carry a usable interpreter, so a broken or absent link can
    never take the restart path away.
    """
    if not IS_POSIX:
        return sys.executable
    layout = managed_venv_layout()
    if not running_from_managed_venv(layout):
        return sys.executable
    # The link's TARGET must resolve inside this layout's own trees before it
    # is trusted with an exec: a stable link repointed outside the managed
    # parent answers sys.executable instead. Scope honestly stated: this pins
    # WHERE the interpreter may live, not WHO wrote it — an actor with shell
    # as the gateway's user can rewrite the trees themselves (sys.executable's
    # own tree included), which is the RFC's accepted local-code-execution gap
    # and is not widened by the link.
    if not layout.is_managed_tree(layout.stable_link):
        return sys.executable
    candidate = layout.stable_link / "bin" / os.path.basename(sys.executable)
    if not candidate.exists():
        candidate = layout.stable_link / "bin" / "python3"
    try:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    except OSError:
        pass
    return sys.executable


# ── Manifest fetch and verification ─────────────────────────────────────────


def _staging_dir() -> Path:
    """Per-run staging area under the data home's ``trust/`` keystone directory.

    The verified wheel sits on disk between its SHA-256 check and the pip
    install, and a TMPDIR staging file is writable by the agent (same uid, and
    TMPDIR is deliberately agent-reachable scratch) — a swap in that window
    would promote unverified code. ``trust/`` is the existing whole-directory
    keystone leaf: the agent's file gate and every bash form refuse it, while
    this engine (the gateway or the operator's CLI) writes directly. Same
    accepted residual as every keystone leaf: arbitrary local code as the
    user is out of scope, the AGENT's tool surface is what is fenced.
    """
    root = data_home() / "trust" / "update-staging"
    make_owner_only_dir(root)
    return root


def _download_to_file(url: str, dest: Path, cap: int, timeout: float, expected_sha: str) -> None:
    """Stream *url* into *dest*, enforcing *cap* and the digest INCREMENTALLY.

    Chunked rather than buffered: the wheel ceiling is 500 MiB, and one
    ``resp.read()`` of that size doubles as a memory spike on the very gateway
    the update is trying to keep alive. The hash is folded in as chunks land,
    so the verify step never re-reads the file it just wrote — and the digest
    therefore covers the exact bytes on disk.
    """
    if not url.startswith("https://"):
        raise WheelUpdateError(f"refusing non-HTTPS URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "kirocrew-update/1"})
    digest = hashlib.sha256()
    received = 0
    try:
        with (
            urllib.request.urlopen(  # nosemgrep: dynamic-urllib-use-detected
                req, timeout=timeout
            ) as resp,
            open(dest, "wb") as out,
        ):
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                received += len(chunk)
                if received > cap:
                    raise WheelUpdateError(f"{url} exceeded the {cap}-byte ceiling")
                digest.update(chunk)
                out.write(chunk)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        raise WheelUpdateError(f"could not fetch {url}: {exc}") from exc
    except WheelUpdateError:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    got = digest.hexdigest()
    if got != expected_sha:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        raise WheelUpdateError(
            f"wheel SHA-256 mismatch (expected {expected_sha}, got {got}) — refusing to install"
        )


def _fetch_bytes(url: str, cap: int, timeout: float) -> bytes:
    """GET *url*, refusing more than *cap* received bytes.

    The single network seam of this module, so tests stub one function. HTTPS
    is asserted here as defence in depth — the callers validate the CDN base
    before composing the URL, but this function must hold on its own.
    """
    if not url.startswith("https://"):
        raise WheelUpdateError(f"refusing non-HTTPS URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "kirocrew-update/1"})
    try:
        with urllib.request.urlopen(  # nosemgrep: dynamic-urllib-use-detected
            req, timeout=timeout
        ) as resp:
            raw = resp.read(cap + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise WheelUpdateError(f"could not fetch {url}: {exc}") from exc
    if len(raw) > cap:
        raise WheelUpdateError(f"{url} exceeded the {cap}-byte ceiling")
    return raw


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _verify_signature(canonical: bytes, signature: bytes, workdir: Path) -> None:
    """Verify *signature* over *canonical* against the pinned trust root.

    Delegates the RSA math to the ``openssl`` binary — the same verifier
    cli.sh uses, resolved through :func:`trusted_system_bin` so a planted
    PATH shim cannot stand in for it. The pinned key's fingerprint is
    self-checked first (SHA-256 of its SubjectPublicKeyInfo DER must equal
    :data:`CLI_MANIFEST_KEY_ID`), so an accidental edit to either constant
    fails closed before any signature is considered.
    """
    openssl = trusted_system_bin("openssl")
    if openssl is None:
        raise WheelUpdateError(
            "openssl is required to verify the signed manifest and was not "
            "found in a trusted system directory"
        )
    pem = workdir / "cli-manifest-public.pem"
    try:
        pem.write_bytes(base64.b64decode(CLI_MANIFEST_PUBLIC_KEY_B64, validate=True))
    except (ValueError, OSError) as exc:
        raise WheelUpdateError("embedded manifest public key is malformed") from exc

    der = workdir / "cli-manifest-public.der"
    try:
        proc = subprocess.run(
            [openssl, "pkey", "-pubin", "-in", str(pem), "-outform", "DER", "-out", str(der)],
            capture_output=True,
            timeout=_OPENSSL_TIMEOUT_SECS,
            # Explicit paths carry every output, but the child's CWD is pinned
            # to the step's own workdir anyway so nothing an openssl build
            # chooses to drop (an .rnd seed file, a debug artifact) can land in
            # the gateway's working directory.
            cwd=str(workdir),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WheelUpdateError(f"openssl could not read the pinned key: {exc}") from exc
    if proc.returncode != 0:
        raise WheelUpdateError("embedded manifest public key is invalid")
    fingerprint = "sha256:" + hashlib.sha256(der.read_bytes()).hexdigest()
    if fingerprint != CLI_MANIFEST_KEY_ID:
        raise WheelUpdateError("embedded manifest public key fingerprint mismatch")

    payload = workdir / "signed-payload.json"
    sig = workdir / "manifest-signature.bin"
    payload.write_bytes(canonical)
    sig.write_bytes(signature)
    try:
        proc = subprocess.run(
            [openssl, "dgst", "-sha256", "-verify", str(pem), "-signature", str(sig), str(payload)],
            capture_output=True,
            timeout=_OPENSSL_TIMEOUT_SECS,
            cwd=str(workdir),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WheelUpdateError(f"openssl signature verification failed: {exc}") from exc
    if proc.returncode != 0:
        raise WheelUpdateError("manifest signature verification failed — refusing to install")


def parse_and_validate_manifest(
    raw: bytes,
    *,
    channel: str,
    artifact_base: str,
) -> tuple[dict[str, str], bytes, bytes]:
    """Structural validation + canonicalization, PURE (no I/O).

    Returns ``(payload, canonical_bytes, signature)`` for
    :func:`_verify_signature`. A byte-for-byte port of the two validation
    heredocs in cli.sh: duplicate keys, extra/missing fields, non-string
    values, oversized payloads, and a wheel URL that is not the ONE canonical
    URL implied by the byte host + channel + signed version are all refused.
    Nothing in the payload is acted on until the signature over the canonical
    bytes verifies.
    """
    if len(raw) > _MANIFEST_MAX_BYTES:
        raise WheelUpdateError("manifest exceeds the size ceiling")
    try:
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError) as exc:
        raise WheelUpdateError(f"manifest is not valid JSON: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or not _MANIFEST_EXPECTED_FIELDS <= set(manifest)
        or set(manifest) - _MANIFEST_EXPECTED_FIELDS - _MANIFEST_OPTIONAL_FIELDS
    ):
        raise WheelUpdateError("manifest carries unexpected fields")
    if not all(isinstance(v, str) and v for v in manifest.values()):
        raise WheelUpdateError("manifest carries an invalid field type")
    if "min_version" in manifest and not _MIN_VERSION_RE.match(manifest["min_version"]):
        raise WheelUpdateError("manifest min_version fails validation")
    if manifest["schema"] != _CLI_MANIFEST_SCHEMA:
        raise WheelUpdateError("unsupported manifest schema")
    if manifest["algorithm"] != "RSASSA_PKCS1_V1_5_SHA_256":
        raise WheelUpdateError("unsupported signature algorithm")
    if manifest["key_id"] != CLI_MANIFEST_KEY_ID:
        raise WheelUpdateError("manifest signed by an untrusted key")
    if manifest["channel"] != channel:
        raise WheelUpdateError(
            f"manifest channel {manifest['channel']!r} does not match {channel!r}"
        )
    version = manifest["version"]
    if not _VERSION_RE.match(version):
        raise WheelUpdateError("manifest version fails validation")
    if not _SHA256_RE.match(manifest["sha256"]):
        raise WheelUpdateError("manifest sha256 fails validation")
    if not _PUB_DATE_RE.match(manifest["pub_date"]):
        raise WheelUpdateError("manifest pub_date fails validation")
    requires = manifest["python_requires"]
    if len(requires) > 128 or any(not (0x20 <= ord(c) <= 0x7E) for c in requires):
        raise WheelUpdateError("manifest python_requires fails validation")
    wheel_name = f"kirocrew-{version}-py3-none-any.whl"
    expected_url = f"{artifact_base}/cli/{channel}/{version}/{wheel_name}"
    if manifest["wheel_url"] != expected_url:
        raise WheelUpdateError("manifest wheel_url is not the canonical artifact URL")

    try:
        signature = base64.b64decode(manifest["signature"], validate=True)
    except ValueError as exc:
        raise WheelUpdateError("manifest signature is not valid base64") from exc
    if not signature or len(signature) > 1024:
        raise WheelUpdateError("manifest signature size is invalid")

    unsigned = {k: v for k, v in manifest.items() if k != "signature"}
    canonical = (
        json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("ascii")
    if len(canonical) > 16384:
        raise WheelUpdateError("canonical manifest payload is oversized")
    return {k: str(v) for k, v in unsigned.items()}, canonical, signature


def fetch_verified_manifest(
    *,
    channel: str,
    feed_base: str,
    artifact_base: str,
    workdir: Path,
) -> dict[str, str]:
    """Fetch the channel manifest and return its AUTHENTICATED payload."""
    url = f"{feed_base}/feed/{channel}/latest-cli.json"
    raw = _fetch_bytes(url, _MANIFEST_MAX_BYTES, _FETCH_TIMEOUT_SECS)
    payload, canonical, signature = parse_and_validate_manifest(
        raw, channel=channel, artifact_base=artifact_base
    )
    _verify_signature(canonical, signature, workdir)
    return payload


def download_verified_wheel(payload: dict[str, str], dest_dir: Path) -> Path:
    """Download the wheel the SIGNED payload names and verify its SHA-256."""
    url = payload["wheel_url"]
    expected_sha = payload["sha256"]
    wheel_path = dest_dir / f"kirocrew-{payload['version']}-py3-none-any.whl"
    _download_to_file(url, wheel_path, _WHEEL_MAX_BYTES, _WHEEL_FETCH_TIMEOUT_SECS, expected_sha)
    return wheel_path


# ── Shadow build, verification, promotion ───────────────────────────────────


def _run(argv: list[str], timeout: float, step: str, cwd: str | None = None) -> None:
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        raise WheelUpdateError(f"{step} timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise WheelUpdateError(f"{step} could not run: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise WheelUpdateError(
            f"{step} exited {proc.returncode}" + (f": {detail[-2000:]}" if detail else "")
        )


#: Ownership sentinel: written into a shadow directory the moment this engine
#: creates it, removed only after the tree passes verification. Its PRESENCE
#: proves two things at once — the directory is OURS (an unrelated venv that
#: happens to carry a versioned name never has it) and the build never
#: completed (a completed tree had it removed before promotion). Those are
#: exactly the two conditions under which deletion is safe.
_SHADOW_SENTINEL = ".kirocrew-shadow-incomplete"


def build_shadow_venv(wheel_path: Path, shadow_dir: Path, stable_link: Path | None = None) -> None:
    """Build a FRESH venv at *shadow_dir* and install *wheel_path* into it.

    The interpreter is this process's own Python: the update replaces Kiro
    Crew's code, not the interpreter, so the shadow tree is built on the same
    base the current tree runs on (``python -m venv`` from inside a venv
    creates the new environment against that venv's base interpreter).

    A leftover shadow from a previous FAILED attempt is removed first, but
    "leftover" is proven, not assumed: a directory that is currently the
    stable link's target is a PROMOTED tree — re-running an update for a
    version that is already live reaches this path with ``shadow_dir`` naming
    the live tree, and removing it would leave the stable link dangling for
    the whole rebuild window. That case is refused. Single-writer exclusion
    against a concurrent update run is the caller's job (see
    :func:`apply_wheel_update`'s update lock).
    """
    if stable_link is not None:
        try:
            if stable_link.is_symlink() and stable_link.resolve() == shadow_dir.resolve():
                raise WheelUpdateError(
                    f"{shadow_dir} is the stable link's current target — it is "
                    "already promoted, not a leftover. Nothing to do."
                )
        except OSError:
            # An unreadable stable link cannot prove the directory safe to
            # remove, so it does not: fall through to the venv-marker guard
            # below, which still refuses anything that is not a plain venv.
            pass
    if shadow_dir.exists() or shadow_dir.is_symlink():
        if shadow_dir.is_symlink() or not shadow_dir.is_dir():
            raise WheelUpdateError(
                f"refusing to reuse {shadow_dir}: it exists and is not a plain directory"
            )
        if not (shadow_dir / "pyvenv.cfg").exists():
            raise WheelUpdateError(
                f"refusing to remove {shadow_dir}: it exists but is not a virtual "
                "environment (no pyvenv.cfg)"
            )
        # A pre-existing directory is removed ONLY on proof of ownership:
        # the sentinel this engine writes at build start and removes after
        # verification. Present = our own incomplete debris, the one case a
        # retry must clear. Absent = either a COMPLETED tree (might be
        # serving a gateway whose restart failed — sys.executable vouches
        # only for THIS process) or an UNRELATED venv that happens to carry
        # a versioned name (a custom KIROCREW_VENV shares a parent with
        # whatever else lives there). Both are refused, never deleted.
        try:
            if stable_link is not None and stable_link.is_symlink():
                if stable_link.resolve() == shadow_dir.resolve():
                    raise WheelUpdateError(
                        f"{shadow_dir} is the stable link's current target — it is "
                        "already promoted, not a leftover. Nothing to do."
                    )
        except OSError:
            # An unreadable stable link cannot prove the directory safe to
            # remove, so it does not: fall through to the sentinel guard,
            # which still refuses anything not provably ours-and-incomplete.
            pass
        if not (shadow_dir / _SHADOW_SENTINEL).exists():
            raise WheelUpdateError(
                f"{shadow_dir} already exists and was not left by an "
                "interrupted update — refusing to remove it. If you are "
                "certain nothing needs it, remove the directory and re-run."
            )
        shutil.rmtree(shadow_dir)

    free = shutil.disk_usage(shadow_dir.parent).free
    if free < _SHADOW_MIN_FREE_BYTES:
        raise WheelUpdateError(
            f"not enough free disk space for a shadow install "
            f"({free // (1024 * 1024)} MiB free, "
            f"{_SHADOW_MIN_FREE_BYTES // (1024 * 1024)} MiB required)"
        )

    # Claim ownership BEFORE any build step: the sentinel is what a future
    # retry's reuse guard keys on, so it must exist from the first moment a
    # partial tree can. `python -m venv` tolerates a non-empty directory.
    try:
        shadow_dir.mkdir(parents=True, exist_ok=True)
        (shadow_dir / _SHADOW_SENTINEL).write_text(
            "created by kiro_crew.platform.wheel_engine; removed after verification\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise WheelUpdateError(f"could not claim the shadow directory: {exc}") from exc
    _run(
        [sys.executable, "-m", "venv", str(shadow_dir)],
        _VENV_CREATE_TIMEOUT_SECS,
        "venv creation",
        cwd=str(shadow_dir.parent),
    )
    shadow_python = shadow_dir / "bin" / "python3"
    try:
        # Best-effort pip refresh, exactly as cli.sh does; a failure here is
        # not a failed update.
        subprocess.run(
            [str(shadow_python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
            capture_output=True,
            timeout=_VENV_CREATE_TIMEOUT_SECS,
            cwd=str(shadow_dir),
        )
    except (OSError, subprocess.SubprocessError):
        pass
    _run(
        [str(shadow_python), "-m", "pip", "install", "--quiet", str(wheel_path)],
        _PIP_INSTALL_TIMEOUT_SECS,
        "pip install into the shadow venv",
        cwd=str(shadow_dir),
    )


def verify_shadow_venv(shadow_dir: Path, expected_version: str) -> None:
    """Prove the shadow tree serves the promised version before promotion.

    ``-I`` isolates the probe from this process's CWD and environment (same
    reasoning as ``dep_sync._probe_interpreter``), so the answer describes the
    shadow tree rather than the caller. A tree that cannot import the package,
    or imports a different version, is never promoted.
    """
    shadow_python = shadow_dir / "bin" / "python3"
    try:
        proc = subprocess.run(
            [
                str(shadow_python),
                "-I",
                "-X",
                "utf8",
                "-c",
                "import kiro_crew; print(kiro_crew.__version__)",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(shadow_dir),
            timeout=_PROBE_TIMEOUT_SECS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WheelUpdateError(f"shadow venv verification could not run: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()
        raise WheelUpdateError(
            "shadow venv cannot import kiro_crew — not promoting"
            + (f": {detail[-500:]}" if detail else "")
        )
    got = proc.stdout.strip()
    if got != expected_version:
        raise WheelUpdateError(
            f"shadow venv reports version {got!r}, expected {expected_version!r} — not promoting"
        )
    if not (shadow_dir / "bin" / "kirocrew").exists():
        raise WheelUpdateError("shadow venv is missing the kirocrew console script")


def promote(shadow_dir: Path, stable_link: Path) -> None:
    """Atomically point *stable_link* at *shadow_dir*.

    Sibling symlink + ``os.replace`` — RFC §3's atomicity invariant. There is
    no window in which the stable name is missing, and a crash between the two
    steps leaves at worst an orphaned temp link beside it. A real directory at
    the stable name is corrupt state (the name has always been a symlink) and
    is refused rather than replaced.
    """
    if stable_link.exists() and not stable_link.is_symlink():
        raise WheelUpdateError(f"refusing to promote: {stable_link} exists and is not a symlink")
    tmp = stable_link.with_name(f"{stable_link.name}.{os.getpid()}.new")
    try:
        tmp.unlink(missing_ok=True)
        os.symlink(str(shadow_dir.resolve()), str(tmp))
        os.replace(str(tmp), str(stable_link))
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise WheelUpdateError(f"could not promote the stable link: {exc}") from exc


def repoint_launcher_symlink(layout: ManagedVenvLayout) -> bool:
    """Point ``~/.local/bin/kirocrew`` through the stable link.

    Only rewrites a symlink that already resolves into one of OUR trees — a
    launcher the operator pointed somewhere else (pipx, a wrapper script) is
    not this engine's to move. Returns whether the launcher now resolves
    through the stable link.
    """
    launcher = Path.home() / ".local" / "bin" / "kirocrew"
    target = layout.stable_link / "bin" / "kirocrew"
    if not launcher.is_symlink():
        return False
    try:
        current = Path(os.readlink(launcher))
    except OSError:
        return False
    if not current.is_absolute():
        current = launcher.parent / current
    if current == target:
        return True
    if not layout.is_managed_tree(current):
        logger.info("Launcher %s points outside the managed trees; leaving it", launcher)
        return False
    tmp = launcher.with_name(f"{launcher.name}.{os.getpid()}.new")
    try:
        tmp.unlink(missing_ok=True)
        os.symlink(str(target), str(tmp))
        os.replace(str(tmp), str(launcher))
    except OSError as exc:
        logger.warning("Could not repoint %s: %s", launcher, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def apply_wheel_update(
    *,
    channel: str,
    feed_base: str,
    artifact_base: str,
    expected_version: str,
    progress: Callable[[str], None] = lambda _msg: None,
) -> Path:
    """Run the full shadow flow; return the promoted tree's path.

    Fetch + verify the signed manifest, download + verify the wheel, build the
    shadow tree, prove it imports the promised version, promote the stable
    link, repoint the launcher, prune stale trees. Every failure before
    :func:`promote` leaves the install untouched; promote itself is atomic.
    """
    layout = managed_venv_layout()
    # One update in flight, ever (RFC §5's lease, scoped to what this PR
    # ships): two concurrent runs would interleave rmtree/venv/pip into the
    # same shadow directory and one could promote what the other is deleting.
    # The lock file lives BESIDE the trees it serializes, so every writer for
    # this layout contends on the same file whatever data home spawned it.
    lock_path = layout.stable_link.with_name(f"{layout.legacy.name}.update.lock")
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise WheelUpdateError(f"could not open the update lock: {exc}") from exc
    try:
        if not try_acquire_lock(lock_fd, exclusive=True):
            raise WheelUpdateError(
                "another kirocrew update is already in progress — wait for it "
                "to finish and re-run"
            )
        return _apply_locked(
            layout,
            channel=channel,
            feed_base=feed_base,
            artifact_base=artifact_base,
            expected_version=expected_version,
            progress=progress,
        )
    finally:
        try:
            release_lock(lock_fd)
        except OSError:
            pass
        os.close(lock_fd)


def _apply_locked(
    layout: ManagedVenvLayout,
    *,
    channel: str,
    feed_base: str,
    artifact_base: str,
    expected_version: str,
    progress: Callable[[str], None],
) -> Path:
    """The lock-held body of :func:`apply_wheel_update`."""
    # Idempotent recovery for a handoff interrupted AFTER promote but BEFORE
    # the launcher was repointed: the stable link already targets this
    # version's tree, so a fresh build would hit build_shadow_venv's
    # stable-target refusal ("already promoted, not a leftover") and abort,
    # stranding the launcher on the old venv forever. Detect that state up
    # front and finish the one remaining step — repoint — rather than
    # refusing. Guarded on the RESOLVED target matching this exact version so
    # it never short-circuits a real version change.
    expected_tree = layout.versioned_tree(expected_version)
    try:
        already_promoted = (
            layout.stable_link.is_symlink()
            and layout.stable_link.resolve() == expected_tree.resolve()
        )
    except OSError:
        already_promoted = False
    if already_promoted:
        progress(f"{expected_version} is already promoted; completing the launcher handoff…")
        repoint_launcher_symlink(layout)
        return expected_tree
    with tempfile.TemporaryDirectory(prefix="kirocrew-update-", dir=str(_staging_dir())) as tmp:
        workdir = Path(tmp)
        progress("Verifying the signed release manifest…")
        payload = fetch_verified_manifest(
            channel=channel,
            feed_base=feed_base,
            artifact_base=artifact_base,
            workdir=workdir,
        )
        version = payload["version"]
        if version != expected_version:
            # The feed moved between the caller's check and this fetch. Not an
            # error in itself, but the caller advertised one version and must
            # not silently install another.
            raise WheelUpdateError(
                f"the feed now serves {version}, not the {expected_version} the "
                "check reported — re-run the update"
            )
        progress(f"Downloading kirocrew {version}…")
        wheel_path = download_verified_wheel(payload, workdir)
        progress("Wheel SHA-256 verified against the signed digest.")

        shadow_dir = layout.versioned_tree(version)
        progress(f"Building the new environment at {shadow_dir}…")
        build_shadow_venv(wheel_path, shadow_dir, stable_link=layout.stable_link)
        progress("Verifying the new environment…")
        verify_shadow_venv(shadow_dir, version)
        progress("Promoting…")
        promote(shadow_dir, layout.stable_link)
        # The sentinel comes off only AFTER promotion: a crash in the window
        # between verify and promote would otherwise leave a verified,
        # unpromoted tree with no ownership marker — which the reuse guard
        # then refuses forever. Until the link flips, the tree is still OURS
        # to clear on retry; once it flips, the guard's stable-target refusal
        # covers the brief window in which a promoted tree still carries the
        # sentinel. A failure to unlink after a successful promote is
        # reported, not raised — the update itself already happened.
        try:
            (shadow_dir / _SHADOW_SENTINEL).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not clear the build sentinel in %s: %s", shadow_dir, exc)
            progress(f"Note: could not clear the build sentinel in {shadow_dir}: {exc}")
        if not repoint_launcher_symlink(layout):
            # The stable link is promoted, so restarts pick the new tree up,
            # but a launcher still aimed elsewhere keeps NEW shells on the old
            # version. Surfaced rather than swallowed — the operator can
            # repoint it by re-running the installer.
            progress(
                "Note: the ~/.local/bin/kirocrew launcher was not repointed; "
                "new shells keep the previous version until the installer is re-run."
            )
        # Deliberately NO pruning here. A versioned tree can be deleted only
        # with proof no process is running from it, and this engine cannot
        # prove liveness yet: the gateway may still be serving from a tree
        # older than the one this CLI runs from, and sys.executable only
        # vouches for THIS process. Old trees stay as manual recovery targets
        # until an ownership/liveness protocol lands (tracked as follow-up).
        return shadow_dir


__all__ = [
    "CLI_MANIFEST_KEY_ID",
    "CLI_MANIFEST_PUBLIC_KEY_B64",
    "ManagedVenvLayout",
    "WheelUpdateError",
    "apply_wheel_update",
    "build_shadow_venv",
    "download_verified_wheel",
    "fetch_verified_manifest",
    "managed_venv_layout",
    "parse_and_validate_manifest",
    "promote",
    "repoint_launcher_symlink",
    "respawn_executable",
    "running_from_managed_venv",
    "verify_shadow_venv",
]
