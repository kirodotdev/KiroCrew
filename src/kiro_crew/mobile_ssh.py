"""Per-device mobile SSH enrollment and short-lived gateway token minting."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import shlex
import stat
import struct
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, fsync_dir
from kiro_crew.config.paths import data_home

MOBILE_SSH_SCHEMA = "kirocrew-mobile-ssh-devices-v1"
MOBILE_ENROLLMENT_SCHEMA = "kirocrew-mobile-ssh-enrollment-v1"
MOBILE_REVOCATION_SCHEMA = "kirocrew-mobile-ssh-revocation-v1"
MOBILE_CHALLENGE_SCHEMA = "kirocrew-mobile-ssh-challenge-v1"
MOBILE_TOKEN_SCHEMA = "kirocrew-mobile-ssh-token-v1"
MOBILE_ERROR_SCHEMA = "kirocrew-mobile-ssh-error-v1"
MOBILE_TOKEN_KIND = "mobile_ssh"
MOBILE_TOKEN_AUDIENCE = "kirocrew-mobile-gateway"
MOBILE_TOKEN_SCOPE = "gateway:mobile"
MOBILE_TOKEN_TTL_SECS = 15 * 60
MOBILE_GATEWAY_PORT = 5476
# The exec request a client sends; the forced command refuses any other.
MOBILE_BRIDGE_COMMAND = "kirocrew-mobile-bridge"
MOBILE_SIGNATURE_CONTEXT = b"kirocrew-mobile-ssh-token\n"
MOBILE_CHALLENGE_TTL_SECS = 60
MOBILE_SSH_DIR = "mobile-ssh"
MOBILE_SSH_STORE = "devices.json"

_HOST_PUBLIC_KEY_PATHS = (Path("/etc/ssh/ssh_host_ed25519_key.pub"),)

_STORE_VERSION = 1
MOBILE_SSH_MAX_DEVICES = 64
_MAX_CHALLENGES = 256
_NONCE_BYTES = 32
_DEVICE_ID_MAX = 63
_LABEL_MAX = 100
_PUBLIC_KEY_MAX = 1024
_ED25519_KEY_BYTES = 32
_ED25519_SIGNATURE_BYTES = 64
_STORE_LOCK = "devices.lock"
_USERNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]{0,63}$")
_DEVICE_FIELDS = frozenset(
    {
        "device_id",
        "label",
        "public_key",
        "key_sha256",
        "ssh_fingerprint",
        "enrollment_id",
        "enrolled_at",
        "revoked_at",
        "gateway_port",
        "home",
    }
)


class MobileSshError(Exception):
    """A stable machine-readable mobile SSH contract error."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class MobileSshDevice:
    device_id: str
    label: str
    public_key: str
    key_sha256: str
    ssh_fingerprint: str
    enrollment_id: str
    enrolled_at: str
    revoked_at: str
    gateway_port: str
    home: str

    @property
    def active(self) -> bool:
        return not self.revoked_at

    def public_metadata(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "label": self.label,
            "ssh_fingerprint": self.ssh_fingerprint,
            "enrolled_at": self.enrolled_at,
            "revoked_at": self.revoked_at or None,
            "active": self.active,
            "gateway_port": int(self.gateway_port),
        }


@dataclass(frozen=True)
class EnrollmentResult:
    device: MobileSshDevice
    authorized_keys_line: str


@dataclass(frozen=True)
class HostSshIdentity:
    username: str
    host_key_algorithm: str
    host_key_fingerprint: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_device_id(device_id: str) -> str:
    value = device_id.strip() if isinstance(device_id, str) else ""
    if not value or len(value) > _DEVICE_ID_MAX:
        raise MobileSshError("invalid_device_id", "device_id must be 1-63 characters")
    if value[0] not in "abcdefghijklmnopqrstuvwxyz0123456789":
        raise MobileSshError(
            "invalid_device_id", "device_id must start with a lowercase letter or digit"
        )
    if any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in value):
        raise MobileSshError(
            "invalid_device_id", "device_id may contain only lowercase letters, digits, and '-'"
        )
    return value


def _validate_label(label: str) -> str:
    value = label.strip()
    if len(value) > _LABEL_MAX or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise MobileSshError("invalid_label", "label must be printable and at most 100 characters")
    return value


def _current_username() -> str:
    if platform_compat.IS_WINDOWS:
        raise MobileSshError(
            "platform_unsupported",
            "mobile SSH enrollment is not yet validated on Windows; use a Linux or macOS host",
            status=501,
        )
    try:
        import pwd

        username = pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, OSError) as exc:
        raise MobileSshError(
            "ssh_username_unavailable",
            "could not resolve the current non-root SSH user",
            status=503,
        ) from exc
    if os.getuid() == 0:
        raise MobileSshError(
            "root_ssh_unsupported",
            "mobile SSH enrollment must run as the non-root account that runs Kiro Crew",
            status=403,
        )
    if not _USERNAME_RE.fullmatch(username):
        raise MobileSshError(
            "ssh_username_unsupported", "the current account name is not safe for SSH enrollment"
        )
    return username


def _read_verified_host_public_key(path: Path, *, expected_owner_uid: int = 0) -> str:
    """Read one fixed OpenSSH host public key without following links."""
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise MobileSshError(
            "host_key_unavailable",
            "OpenSSH Ed25519 host key is unavailable; enable the SSH server and retry",
            status=503,
        ) from exc
    if not stat.S_ISREG(before.st_mode):
        raise MobileSshError("host_key_untrusted", "OpenSSH host public key is not a regular file")
    if before.st_uid != expected_owner_uid or before.st_mode & 0o022:
        raise MobileSshError(
            "host_key_untrusted", "OpenSSH host public key is not root-owned and write-protected"
        )
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise MobileSshError("host_key_untrusted", "OpenSSH host public key changed")
            raw = os.read(fd, _PUBLIC_KEY_MAX + 1)
        finally:
            os.close(fd)
    except MobileSshError:
        raise
    except OSError as exc:
        raise MobileSshError(
            "host_key_unavailable", "OpenSSH Ed25519 host public key could not be read", status=503
        ) from exc
    if len(raw) > _PUBLIC_KEY_MAX:
        raise MobileSshError("host_key_untrusted", "OpenSSH host public key is too large")
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise MobileSshError("host_key_untrusted", "OpenSSH host public key is not ASCII") from exc


def discover_host_ssh_identity() -> HostSshIdentity:
    """Discover the current account and root-owned Ed25519 host-key fingerprint."""
    username = _current_username()
    last_error: MobileSshError | None = None
    for path in _HOST_PUBLIC_KEY_PATHS:
        try:
            _canonical, _digest, fingerprint = validate_ed25519_public_key(
                _read_verified_host_public_key(path)
            )
            return HostSshIdentity(
                username=username,
                host_key_algorithm="ssh-ed25519",
                host_key_fingerprint=fingerprint,
            )
        except MobileSshError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise MobileSshError(
        "host_key_unavailable",
        "no supported OpenSSH Ed25519 host-key source is configured",
        status=503,
    )


def _read_ssh_string(blob: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 4 > len(blob):
        raise MobileSshError("invalid_public_key", "public key blob is truncated")
    size = struct.unpack(">I", blob[offset : offset + 4])[0]
    start = offset + 4
    end = start + size
    if end > len(blob):
        raise MobileSshError("invalid_public_key", "public key blob is truncated")
    return blob[start:end], end


def _ed25519_key_bytes(blob: bytes) -> bytes:
    algorithm, offset = _read_ssh_string(blob, 0)
    key_bytes, offset = _read_ssh_string(blob, offset)
    if algorithm != b"ssh-ed25519" or len(key_bytes) != _ED25519_KEY_BYTES or offset != len(blob):
        raise MobileSshError("invalid_public_key", "public key is not a canonical Ed25519 key")
    return key_bytes


def validate_ed25519_public_key(public_key: str) -> tuple[str, str, str]:
    """Return canonical key line, key digest, and OpenSSH fingerprint."""
    if not isinstance(public_key, str) or len(public_key) > _PUBLIC_KEY_MAX:
        raise MobileSshError("invalid_public_key", "public key is missing or too long")
    parts = public_key.strip().split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise MobileSshError("invalid_public_key", "only ssh-ed25519 public keys are accepted")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MobileSshError("invalid_public_key", "public key base64 is invalid") from exc
    _ed25519_key_bytes(blob)
    canonical = f"ssh-ed25519 {base64.b64encode(blob).decode('ascii')}"
    digest = hashlib.sha256(blob).digest()
    fingerprint = base64.b64encode(digest).decode("ascii").rstrip("=")
    return canonical, digest.hex(), f"SHA256:{fingerprint}"


def signed_message(device_id: str, nonce: str) -> bytes:
    """The bytes a device signs with its enrolled key to mint a token."""
    return MOBILE_SIGNATURE_CONTEXT + f"{device_id}\n{nonce}".encode("ascii")


def _verify_signature(public_key: str, message: bytes, signature: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        raw_signature = base64.b64decode(signature, validate=True)
    except (binascii.Error, ValueError):
        return False
    if len(raw_signature) != _ED25519_SIGNATURE_BYTES:
        return False
    blob = base64.b64decode(public_key.split()[1])
    try:
        Ed25519PublicKey.from_public_bytes(_ed25519_key_bytes(blob)).verify(raw_signature, message)
    except (InvalidSignature, MobileSshError, ValueError):
        return False
    return True


def forced_command_argv(launcher: str, device_id: str, gateway_port: int, home: Path) -> list[str]:
    # sshd starts the forced command without the gateway's environment, so the data
    # home is passed explicitly.
    return [
        launcher,
        "mobile",
        "ssh",
        "bridge",
        "--device-id",
        device_id,
        "--port",
        str(gateway_port),
        "--home",
        str(home),
    ]


def _authorized_keys_line(
    public_key: str, device_id: str, launcher: str, gateway_port: int, home: Path
) -> str:
    command = shlex.join(forced_command_argv(launcher, device_id, gateway_port, home))
    encoded = command.replace("\\", "\\\\").replace('"', '\\"')
    # `restrict` disables every forwarding kind, the PTY, agent, X11 and user rc.
    return f'restrict,command="{encoded}" {public_key} kirocrew-mobile-{device_id}'


class MobileSshDeviceStore:
    """Atomic owner-only registry for mobile SSH public-key enrollments."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or data_home()
        self._dir = self._root / MOBILE_SSH_DIR
        self._path = self._dir / MOBILE_SSH_STORE
        self._lock_path = self._dir / _STORE_LOCK
        self._memory_lock = threading.RLock()
        self._challenges: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._ensure_dir()
        self._loaded_signature: tuple[int, int, int] | None = None
        self._devices = self._load()

    @property
    def home(self) -> Path:
        return self._root.resolve()

    def _ensure_dir(self) -> None:
        try:
            root = self._root.resolve()
            if platform_compat.is_link_or_junction(self._dir):
                raise MobileSshError("store_unavailable", "mobile SSH store is a link", status=503)
            self._dir.mkdir(parents=True, exist_ok=True)
            resolved = self._dir.resolve()
        except OSError as exc:
            raise MobileSshError(
                "store_unavailable", "mobile SSH store could not be created", status=503
            ) from exc
        if resolved != root / MOBILE_SSH_DIR:
            raise MobileSshError(
                "store_unavailable", "mobile SSH store resolves outside the data home", status=503
            )
        try:
            platform_compat.restrict_dir_to_owner(self._dir)
        except OSError as exc:
            raise MobileSshError(
                "store_permissions", "mobile SSH store is not owner-only", status=503
            ) from exc

    @staticmethod
    def _signature(file_stat: os.stat_result) -> tuple[int, int, int]:
        return (file_stat.st_ino, file_stat.st_size, file_stat.st_mtime_ns)

    def _load(self) -> dict[str, MobileSshDevice]:
        try:
            if platform_compat.is_link_or_junction(self._path):
                raise MobileSshError(
                    "store_unavailable", "mobile SSH device registry is a link", status=503
                )
            try:
                file_stat = os.lstat(self._path)
            except FileNotFoundError:
                self._loaded_signature = None
                return {}
            if not stat.S_ISREG(file_stat.st_mode):
                raise MobileSshError(
                    "store_unavailable",
                    "mobile SSH device registry is not a regular file",
                    status=503,
                )
            devices = self._parse_registry(json.loads(self._path.read_text(encoding="utf-8")))
            self._loaded_signature = self._signature(file_stat)
            return devices
        except (OSError, TypeError, ValueError) as exc:
            raise MobileSshError(
                "store_unavailable", "mobile SSH device registry is unreadable", status=503
            ) from exc

    @staticmethod
    def _parse_registry(raw: object) -> dict[str, MobileSshDevice]:
        if not isinstance(raw, dict) or raw.get("version") != _STORE_VERSION:
            raise ValueError("unsupported store schema")
        records = raw.get("devices")
        if not isinstance(records, list) or len(records) > MOBILE_SSH_MAX_DEVICES:
            raise ValueError("unsupported store schema")
        devices: dict[str, MobileSshDevice] = {}
        for item in records:
            if not isinstance(item, dict) or set(item) != _DEVICE_FIELDS:
                raise ValueError("device record has unexpected fields")
            if any(not isinstance(value, str) for value in item.values()):
                raise ValueError("device record has non-string fields")
            device = MobileSshDevice(**item)
            try:
                if validate_device_id(device.device_id) != device.device_id:
                    raise ValueError("device record has a non-canonical device_id")
                canonical, key_sha256, _fingerprint = validate_ed25519_public_key(device.public_key)
            except MobileSshError as exc:
                raise ValueError("device record is invalid") from exc
            if canonical != device.public_key or key_sha256 != device.key_sha256:
                raise ValueError("device record key fields disagree")
            if not device.gateway_port.isdigit() or not 1 <= int(device.gateway_port) <= 65535:
                raise ValueError("device record has an invalid gateway port")
            if device.device_id in devices:
                raise ValueError("device registry has duplicate device_id records")
            devices[device.device_id] = device
        return devices

    def _refresh_if_changed_locked(self) -> None:
        # Another process sharing this data home may enroll or revoke; a stat per call
        # notices it, and the JSON is re-parsed only when the file identity changed.
        try:
            file_stat = os.lstat(self._path)
        except FileNotFoundError:
            self._devices = {}
            self._loaded_signature = None
            return
        except OSError as exc:
            raise MobileSshError(
                "store_unavailable", "mobile SSH device registry is unreadable", status=503
            ) from exc
        if self._signature(file_stat) != self._loaded_signature:
            self._devices = self._load()

    def _persist_locked(self, devices: dict[str, MobileSshDevice]) -> None:
        payload = {
            "version": _STORE_VERSION,
            "devices": [asdict(devices[key]) for key in sorted(devices)],
        }
        try:
            atomic_write(
                self._path,
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
                mode=0o600,
                fsync=True,
                restrict_to_owner=True,
                restrict_on_error="raise",
            )
            fsync_dir(self._dir, best_effort=True)
            signature = self._signature(os.lstat(self._path))
        except OSError as exc:
            raise MobileSshError(
                "store_unavailable", "mobile SSH device registry could not be written", status=503
            ) from exc
        # Memory changes only after the write succeeded, so a failed write is never honored.
        self._devices = devices
        self._loaded_signature = signature

    def _open_file_lock(self) -> int:
        self._ensure_dir()
        if platform_compat.is_link_or_junction(self._lock_path):
            raise MobileSshError("store_unavailable", "mobile SSH lock is a link", status=503)
        try:
            fd = os.open(
                self._lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
        except OSError as exc:
            raise MobileSshError(
                "store_unavailable", "mobile SSH lock could not be opened", status=503
            ) from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise MobileSshError(
                    "store_unavailable", "mobile SSH lock is not a regular file", status=503
                )
            platform_compat.restrict_to_owner(self._lock_path)
        except OSError as exc:
            os.close(fd)
            raise MobileSshError(
                "store_permissions", "mobile SSH lock is not owner-only", status=503
            ) from exc
        except MobileSshError:
            os.close(fd)
            raise
        return fd

    def _mutate(self, change) -> object:
        with self._memory_lock:
            fd = self._open_file_lock()
            try:
                with platform_compat.file_lock(fd, exclusive=True, required=True):
                    self._devices = self._load()
                    return change(dict(self._devices))
            finally:
                os.close(fd)

    def enroll(
        self,
        device_id: str,
        public_key: str,
        *,
        label: str = "",
        launcher: str,
        gateway_port: int = MOBILE_GATEWAY_PORT,
    ) -> EnrollmentResult:
        ident = validate_device_id(device_id)
        display = _validate_label(label)
        canonical, key_sha256, fingerprint = validate_ed25519_public_key(public_key)
        if not 1 <= gateway_port <= 65535:
            raise MobileSshError("invalid_port", "gateway port must be between 1 and 65535")
        resolved_launcher = os.path.abspath(os.path.expanduser(launcher))
        if not Path(resolved_launcher).is_file() or not os.access(resolved_launcher, os.X_OK):
            raise MobileSshError("launcher_unavailable", "kirocrew launcher is not executable")
        home = self.home
        device = MobileSshDevice(
            device_id=ident,
            label=display,
            public_key=canonical,
            key_sha256=key_sha256,
            ssh_fingerprint=fingerprint,
            enrollment_id=os.urandom(16).hex(),
            enrolled_at=_utc_now(),
            revoked_at="",
            gateway_port=str(gateway_port),
            home=str(home),
        )

        def change(devices: dict[str, MobileSshDevice]) -> None:
            current = devices.get(ident)
            if current is not None and current.active:
                raise MobileSshError(
                    "device_exists",
                    "an active enrollment already uses this device_id; revoke it first",
                    status=409,
                )
            if current is None and len(devices) >= MOBILE_SSH_MAX_DEVICES:
                raise MobileSshError(
                    "device_limit_reached",
                    "mobile SSH device limit reached; re-enroll an existing device_id instead",
                    status=409,
                )
            if any(
                existing.active and hmac.compare_digest(existing.key_sha256, key_sha256)
                for existing in devices.values()
            ):
                raise MobileSshError(
                    "public_key_exists",
                    "this public key is already enrolled to another active device",
                    status=409,
                )
            devices[ident] = device
            self._persist_locked(devices)

        self._mutate(change)
        return EnrollmentResult(
            device=device,
            authorized_keys_line=_authorized_keys_line(
                canonical, ident, resolved_launcher, gateway_port, home
            ),
        )

    def list_devices(self) -> list[dict[str, object]]:
        with self._memory_lock:
            self._refresh_if_changed_locked()
            return [self._devices[key].public_metadata() for key in sorted(self._devices)]

    def device(self, device_id: str) -> MobileSshDevice | None:
        ident = validate_device_id(device_id)
        with self._memory_lock:
            self._refresh_if_changed_locked()
            return self._devices.get(ident)

    def revoke(self, device_id: str) -> MobileSshDevice:
        ident = validate_device_id(device_id)

        def change(devices: dict[str, MobileSshDevice]) -> MobileSshDevice:
            current = devices.get(ident)
            if current is None:
                raise MobileSshError("device_not_found", "device is not enrolled", status=404)
            if not current.active:
                return current
            revoked = MobileSshDevice(**{**asdict(current), "revoked_at": _utc_now()})
            devices[ident] = revoked
            self._persist_locked(devices)
            return revoked

        result = self._mutate(change)
        assert isinstance(result, MobileSshDevice)
        return result

    def _active_device(self, device_id: str) -> MobileSshDevice:
        device = self.device(device_id)
        if device is None:
            raise MobileSshError("device_not_found", "device is not enrolled", status=404)
        if not device.active:
            raise MobileSshError("device_revoked", "device enrollment is revoked", status=403)
        return device

    def issue_challenge(self, device_id: str) -> dict[str, object]:
        device = self._active_device(device_id)
        nonce = os.urandom(_NONCE_BYTES).hex()
        now = time.monotonic()
        with self._memory_lock:
            for stale in [n for n, (_d, exp) in self._challenges.items() if exp <= now]:
                del self._challenges[stale]
            while len(self._challenges) >= _MAX_CHALLENGES:
                self._challenges.popitem(last=False)
            self._challenges[nonce] = (device.device_id, now + MOBILE_CHALLENGE_TTL_SECS)
        return {
            "schema": MOBILE_CHALLENGE_SCHEMA,
            "ok": True,
            "device_id": device.device_id,
            "nonce": nonce,
            "expires_in": MOBILE_CHALLENGE_TTL_SECS,
        }

    def _consume_challenge(self, device_id: str, nonce: str) -> bool:
        with self._memory_lock:
            entry = self._challenges.pop(nonce, None) if isinstance(nonce, str) else None
        if entry is None:
            return False
        bound_device, expires = entry
        return bound_device == device_id and time.monotonic() < expires

    def mint(self, device_id: str, nonce: str, signature: str) -> dict[str, object]:
        ident = validate_device_id(device_id)
        # Consumed before any other check, so a nonce answers at most one attempt.
        if not self._consume_challenge(ident, nonce):
            raise MobileSshError(
                "challenge_invalid", "challenge is unknown, expired or already used", status=403
            )
        device = self._active_device(ident)
        if not isinstance(signature, str) or not _verify_signature(
            device.public_key, signed_message(ident, nonce), signature
        ):
            raise MobileSshError(
                "signature_invalid", "signature does not match the enrolled key", status=403
            )
        claims = {
            "kind": MOBILE_TOKEN_KIND,
            "aud": MOBILE_TOKEN_AUDIENCE,
            "scope": MOBILE_TOKEN_SCOPE,
            "device_id": device.device_id,
            "key_sha256": device.key_sha256,
            "enrollment_id": device.enrollment_id,
            "no_refresh": "1",
        }
        from kiro_crew.dashboard.token_auth import generate_token

        issued = time.time()
        token = generate_token(
            f"mobile:{device.device_id}",
            ttl_seconds=MOBILE_TOKEN_TTL_SECS,
            extra=claims,
            register_nonce=False,
        )
        expires_at = datetime.fromtimestamp(issued + MOBILE_TOKEN_TTL_SECS, timezone.utc)
        return {
            "schema": MOBILE_TOKEN_SCHEMA,
            "ok": True,
            "token": token,
            "token_type": "Bearer",
            "device_id": device.device_id,
            "audience": MOBILE_TOKEN_AUDIENCE,
            "scope": MOBILE_TOKEN_SCOPE,
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            "expires_in": MOBILE_TOKEN_TTL_SECS,
        }


_store_singleton: MobileSshDeviceStore | None = None
_store_lock = threading.Lock()


def get_mobile_ssh_store() -> MobileSshDeviceStore:
    global _store_singleton
    if _store_singleton is None:
        with _store_lock:
            if _store_singleton is None:
                _store_singleton = MobileSshDeviceStore()
    return _store_singleton
