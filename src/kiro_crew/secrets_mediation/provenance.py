"""Owner-authenticated provenance for the mediated-secret authorization policy.

The policy file (``secret_request_policy.json``) names, per stored secret, the
exact https origin the agent may reach with it. Because a sandboxed agent runs
as the SAME uid as the owner, file ownership/mode cannot distinguish an
owner-authored policy from one an agent planted. The policy therefore carries
an owner-authored HMAC, and the host dispatcher refuses any authorization whose
signature does not verify.

The HMAC root is a gateway-created key under ``secret-request-policy-signing/``.
That whole directory is a sandbox HIDDEN leaf, so no agent subprocess can read
or replace the key. The key document is certified by the independently hidden
dashboard token-signing root. A file planted before the hidden leaf existed
cannot certify itself and is refused rather than adopted or replaced.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, fsync_dir
from kiro_crew.config.paths import config_dir as resolve_config_dir

#: Bumped if the canonical serialization or MAC construction ever changes, so an
#: old signature cannot be replayed against a new verifier that reads it
#: differently. Part of the signed payload.
_PROVENANCE_VERSION = 1

#: The JSON key that carries the signature at the top level of the policy file.
SIGNATURE_FIELD = "sig"

#: The JSON key holding the map this module signs.
AUTHORIZATIONS_FIELD = "authorizations"

#: Gateway-only directory containing the certified policy-signing root. This is
#: named publicly so the sandbox and sensitive-path registries can pin the exact
#: same leaf without importing this module into their low-level dependency chain.
HOST_KEY_DIRNAME = "secret-request-policy-signing"
_HOST_KEY_FILENAME = "key.json"
_HOST_KEY_BYTES = 32
_HOST_KEY_MAX_BYTES = 4096
_HOST_KEY_CERT_DOMAIN = b"kirocrew.secret_request_policy.host-key-cert.v1\x00"
_SUBKEY_DOMAIN = b"kirocrew.secret_request_policy.sig.v2"


def _token_root() -> bytes:
    """Return the independently hidden gateway token-signing root."""
    # Imported locally: token_secret pulls in modules that import token_auth,
    # and provenance is a low-level signing module in the sensitive-path floor —
    # keeping the import local avoids widening its module-load dependency chain.
    from kiro_crew.dashboard.token_secret import _get_secret

    return _get_secret()


def _host_key_cert(key: bytes) -> str:
    return hmac.new(_token_root(), _HOST_KEY_CERT_DOMAIN + key, hashlib.sha256).hexdigest()


def _host_key_path(config_dir: str | Path) -> Path:
    return Path(config_dir) / HOST_KEY_DIRNAME / _HOST_KEY_FILENAME


def _load_host_root(config_dir: str | Path) -> bytes | None:
    """Load and authenticate the host-only root; return ``None`` only if absent.

    Every occupied but unverifiable shape raises. The gateway initializer must
    never replace such a file because it could be an agent's pre-seeded key.
    Callers that verify a policy convert the raise to a fail-closed miss.
    """
    path = _host_key_path(config_dir)
    if platform_compat.is_link_or_junction(path.parent):
        raise RuntimeError("the mediated-secret policy signing directory is a link")
    try:
        fd = os.open(
            str(path),
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError("the mediated-secret policy signing key could not be opened") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > _HOST_KEY_MAX_BYTES
        ):
            raise RuntimeError("the mediated-secret policy signing key has an unsafe shape")
        raw = os.read(fd, _HOST_KEY_MAX_BYTES + 1)
    finally:
        os.close(fd)
    try:
        document = json.loads(raw.decode("utf-8"))
        key = bytes.fromhex(document["key"])
        cert = document["cert"]
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise RuntimeError("the mediated-secret policy signing key is malformed") from exc
    if len(key) != _HOST_KEY_BYTES or not isinstance(cert, str):
        raise RuntimeError("the mediated-secret policy signing key is malformed")
    if not hmac.compare_digest(cert, _host_key_cert(key)):
        raise RuntimeError("the mediated-secret policy signing key is not gateway-certified")
    return key


def initialize_host_key(config_dir: str | Path | None = None) -> bytes:
    """Load or mint the certified host-only root during gateway startup.

    Creation publishes a complete document with a non-clobbering hard link, so
    concurrent gateway starts converge on one key. An occupied invalid document
    is never repaired or re-certified; startup fails closed instead.
    """
    if config_dir is None:
        config_dir = resolve_config_dir()
    existing = _load_host_root(config_dir)
    if existing is not None:
        return existing

    directory = Path(config_dir) / HOST_KEY_DIRNAME
    if platform_compat.is_link_or_junction(directory):
        raise RuntimeError("the mediated-secret policy signing directory is a link")
    platform_compat.make_owner_only_dir(directory)
    platform_compat.restrict_dir_to_owner(directory)
    if platform_compat.is_link_or_junction(directory):
        raise RuntimeError("the mediated-secret policy signing directory became a link")

    key = secrets.token_bytes(_HOST_KEY_BYTES)
    payload = json.dumps({"key": key.hex(), "cert": _host_key_cert(key)}) + "\n"
    destination = directory / _HOST_KEY_FILENAME
    staged = directory / f".{_HOST_KEY_FILENAME}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    try:
        atomic_write(staged, payload, fsync=True, restrict_to_owner=True)
        staged_info = os.lstat(staged)
        if not stat.S_ISREG(staged_info.st_mode) or staged_info.st_nlink != 1:
            raise RuntimeError("the staged mediated-secret policy signing key is unsafe")
        try:
            os.link(staged, destination)
        except FileExistsError:
            winner = _load_host_root(config_dir)
            if winner is None:  # pragma: no cover - destination existed a moment ago
                raise RuntimeError("the mediated-secret policy signing key disappeared")
            return winner
        fsync_dir(directory)
        return key
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


def _member_key(config_dir: str | Path, *, create: bool = False) -> bytes | None:
    """Derive the policy MAC subkey from the certified host-only root.

    ``create`` is retained for caller compatibility but deliberately ignored:
    only gateway startup may mint the root. Missing, malformed, linked, aliased,
    or uncertified key material makes verification fail closed.
    """
    del create
    try:
        root = _load_host_root(config_dir)
    except RuntimeError:
        return None
    if root is None:
        return None
    return hmac.new(root, _SUBKEY_DOMAIN, hashlib.sha256).digest()


def _canonical_payload(authorizations: Any) -> bytes:
    """Return deterministic versioned bytes for *authorizations*."""
    body = json.dumps(
        authorizations,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"{_PROVENANCE_VERSION}\n{body}".encode("utf-8")


def sign_authorizations(authorizations: Any, config_dir: str | Path) -> str:
    """Return the owner HMAC, refusing when the gateway root is unavailable."""
    key = _member_key(config_dir)
    if key is None:
        raise RuntimeError(
            "cannot load the gateway-certified host-only signing key for the "
            "mediated-secret authorization policy; start the gateway to initialize it"
        )
    return hmac.new(key, _canonical_payload(authorizations), hashlib.sha256).hexdigest()


def verify_authorizations(authorizations: Any, signature: Any, config_dir: str | Path) -> bool:
    """Whether *signature* is a valid host-only HMAC over *authorizations*."""
    if not isinstance(signature, str) or not signature:
        return False
    key = _member_key(config_dir, create=False)
    if key is None:
        return False
    expected = hmac.new(key, _canonical_payload(authorizations), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)
