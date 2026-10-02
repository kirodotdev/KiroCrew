"""Device-bound short-lived gateway credentials for mobile SSH clients."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import shlex
import stat
import struct
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, fsync_dir
from kiro_crew.config.paths import data_home

MOBILE_SSH_SCHEMA = "kirocrew-mobile-ssh-devices-v1"
MOBILE_ENROLLMENT_SCHEMA = "kirocrew-mobile-ssh-enrollment-v1"
MOBILE_REVOCATION_SCHEMA = "kirocrew-mobile-ssh-revocation-v1"
MOBILE_TOKEN_SCHEMA = "kirocrew-mobile-ssh-token-v1"
MOBILE_ERROR_SCHEMA = "kirocrew-mobile-ssh-error-v1"
MOBILE_TOKEN_KIND = "mobile_ssh"
MOBILE_TOKEN_AUDIENCE = "kirocrew-mobile-gateway"
MOBILE_TOKEN_SCOPE = "gateway:mobile"
MOBILE_TOKEN_TTL_SECS = 15 * 60
MOBILE_GATEWAY_PORT = 5476
MOBILE_SSH_PORT = 22
MOBILE_ORIGINAL_COMMAND = "kirocrew-mobile-token-v1"
MOBILE_SSH_DIR = "mobile-ssh"
MOBILE_SSH_STORE = "devices.json"

MOBILE_TOKEN_ALLOWED_PATHS = frozenset(
    {
        "/api/status",
        "/api/auth/me",
        "/api/models",
        "/api/effort-levels",
        "/api/slash-commands",
        "/api/suggestions",
        "/api/optimizer/optimize",
        "/api/upload/file",
    }
)
MOBILE_TOKEN_READ_ONLY_PATHS = frozenset({"/api/artifact-folders", "/api/cron-folders"})
_READ_METHODS = frozenset({"GET", "HEAD"})
MOBILE_TOKEN_ALLOWED_PREFIXES = (
    "/api/sessions/",
    "/api/chat/",
    "/api/approvals/",
    "/api/artifacts/",
    "/api/tasks/",
    "/api/crons/",
    "/api/agents/",
    "/api/notifications/",
    "/api/spawn/",
    # Answer, dismiss and pending; the bare POST that opens a question stays agent-only.
    "/api/ask-question/",
    # Downloads of files the agent sent.
    "/api/outbox/",
)
# Internal writers under an allowed prefix.
MOBILE_TOKEN_DENIED_PATHS = frozenset({"/api/outbox/notify"})
MOBILE_TOKEN_ALLOWED_COLLECTIONS = frozenset(
    {
        "/api/sessions",
        "/api/chat",
        "/api/approvals",
        "/api/artifacts",
        "/api/tasks",
        "/api/crons",
        "/api/agents",
        "/api/notifications",
        "/api/spawn",
    }
)

_HOST_PUBLIC_KEY_PATHS = (Path("/etc/ssh/ssh_host_ed25519_key.pub"),)

_STORE_VERSION = 1
MOBILE_SSH_MAX_DEVICES = 64
_DEVICE_ID_MAX = 63
_LABEL_MAX = 100
_PUBLIC_KEY_MAX = 1024
_BINDING_BYTES = 32
_DENY_LISTEN = "127.0.0.1:1"
_ED25519_KEY_BYTES = 32
_STORE_LOCK = "devices.lock"
_REQUIRED_DEVICE_FIELDS = frozenset(
    {
        "device_id",
        "label",
        "key_sha256",
        "ssh_fingerprint",
        "enrollment_id",
        "binding_sha256",
        "enrolled_at",
        "revoked_at",
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
    key_sha256: str
    ssh_fingerprint: str
    enrollment_id: str
    binding_sha256: str
    enrolled_at: str
    revoked_at: str = ""
    gateway_port: str = ""
    home: str = ""

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
            "gateway_port": int(self.gateway_port) if self.gateway_port.isdigit() else None,
            "home": self.home or None,
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


def _validate_device_id(device_id: str) -> str:
    value = device_id.strip()
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


_DNS_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_USERNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]{0,63}$")


def validate_ssh_host_descriptor(ssh_host: str) -> str:
    """Validate a host-agnostic OpenSSH DNS name or IP address."""
    if not isinstance(ssh_host, str):
        raise MobileSshError("invalid_ssh_host", "SSH host must be a DNS name or IP address")
    value = ssh_host.strip()
    if not value or len(value) > 253 or any(ch.isspace() or ord(ch) < 33 for ch in value):
        raise MobileSshError("invalid_ssh_host", "SSH host must be a DNS name or IP address")
    if "://" in value or "@" in value or any(ch in value for ch in "'\"`$;|&<>(){}"):
        raise MobileSshError("invalid_ssh_host", "SSH host contains unsupported syntax")
    ip_value = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    try:
        address = ipaddress.ip_address(ip_value)
    except ValueError:
        name = value[:-1] if value.endswith(".") else value
        labels = name.split(".")
        if not name or any(not _DNS_LABEL_RE.fullmatch(label) for label in labels):
            raise MobileSshError("invalid_ssh_host", "SSH host must be a DNS name or IP address")
        if name.lower() == "localhost" or name.lower().endswith(".localhost"):
            raise MobileSshError(
                "invalid_ssh_host", "SSH host must identify a reachable remote host"
            )
        return name.lower()
    # The mobile pairing contract accepts IPv4 only.
    if not isinstance(address, ipaddress.IPv4Address):
        raise MobileSshError("invalid_ssh_host", "SSH host must be an IPv4 address or DNS name")
    if (
        address.is_unspecified
        or address.is_loopback
        or address.is_multicast
        or address.is_link_local
    ):
        raise MobileSshError("invalid_ssh_host", "SSH host must identify a reachable remote host")
    return address.compressed


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
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise MobileSshError("host_key_untrusted", "OpenSSH host public key is not a regular file")
    if before.st_uid != expected_owner_uid or before.st_mode & 0o022:
        raise MobileSshError(
            "host_key_untrusted", "OpenSSH host public key is not root-owned and write-protected"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
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
            public_key = _read_verified_host_public_key(path)
            _canonical, _digest, fingerprint = validate_ed25519_public_key(public_key)
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
    algorithm, offset = _read_ssh_string(blob, 0)
    key_bytes, offset = _read_ssh_string(blob, offset)
    if algorithm != b"ssh-ed25519" or len(key_bytes) != _ED25519_KEY_BYTES or offset != len(blob):
        raise MobileSshError("invalid_public_key", "public key is not a canonical Ed25519 key")
    canonical = f"ssh-ed25519 {base64.b64encode(blob).decode('ascii')}"
    key_sha256 = hashlib.sha256(blob).hexdigest()
    fingerprint = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return canonical, key_sha256, f"SHA256:{fingerprint}"


def _binding_digest(binding: str) -> str:
    return hashlib.sha256(binding.encode("ascii")).hexdigest()


def _authorized_keys_line(
    public_key: str,
    device_id: str,
    binding: str,
    launcher: str,
    gateway_port: int,
    home: Path,
) -> str:
    # sshd starts the forced command without the gateway's environment, and `restrict`
    # blocks environment=, so the data home that holds the local secret is passed explicitly.
    command = shlex.join(
        [
            launcher,
            "mobile",
            "ssh",
            "token",
            "--device-id",
            device_id,
            "--binding",
            binding,
            "--port",
            str(gateway_port),
            "--home",
            str(home),
        ]
    )
    encoded_command = command.replace("\\", "\\\\").replace('"', '\\"')
    # `port-forwarding` re-enables both directions and key options cannot say "no -R"
    # (permitlisten="none" fails the whole key), so -R is pinned to privileged loopback port 1.
    options = (
        f'restrict,port-forwarding,permitopen="127.0.0.1:{gateway_port}",'
        f'permitlisten="{_DENY_LISTEN}",'
        f'command="{encoded_command}",no-pty,no-agent-forwarding,'
        "no-X11-forwarding,no-user-rc"
    )
    return f"{options} {public_key} kirocrew-mobile-{device_id}"


class MobileSshDeviceStore:
    """Atomic owner-only registry for mobile SSH public-key enrollments."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or data_home()
        self._dir = self._root / MOBILE_SSH_DIR
        self._path = self._dir / MOBILE_SSH_STORE
        self._lock_path = self._dir / _STORE_LOCK
        self._memory_lock = threading.RLock()
        self._ensure_dir()
        self._loaded_signature: tuple[int, int, int] | None = None
        self._devices = self._load()
        # Read by validate_claims without the lock; rebound, never mutated.
        self._snapshot: dict[str, MobileSshDevice] = dict(self._devices)

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
            if not self._path.exists():
                self._loaded_signature = None
                return {}
            if platform_compat.is_link_or_junction(self._path):
                raise MobileSshError(
                    "store_unavailable", "mobile SSH device registry is a link", status=503
                )
            file_stat = os.lstat(self._path)
            if not stat.S_ISREG(file_stat.st_mode):
                raise MobileSshError(
                    "store_unavailable",
                    "mobile SSH device registry is not a regular file",
                    status=503,
                )
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            devices = self._parse_registry(raw)
            self._loaded_signature = self._signature(file_stat)
            return devices
        except (OSError, TypeError, ValueError) as exc:
            raise MobileSshError(
                "store_unavailable", "mobile SSH device registry is unreadable", status=503
            ) from exc

    @staticmethod
    def _parse_registry(raw: object) -> dict[str, MobileSshDevice]:
        if not isinstance(raw, dict):
            raise ValueError("unsupported store schema")
        if raw.get("version") != _STORE_VERSION or not isinstance(raw.get("devices"), list):
            raise ValueError("unsupported store schema")
        if len(raw["devices"]) > MOBILE_SSH_MAX_DEVICES:
            raise ValueError("too many device records")
        expected_fields = set(MobileSshDevice.__dataclass_fields__)
        devices: dict[str, MobileSshDevice] = {}
        for item in raw["devices"]:
            if not isinstance(item, dict) or not (
                _REQUIRED_DEVICE_FIELDS <= set(item) <= expected_fields
            ):
                raise ValueError("device record has unexpected fields")
            if any(not isinstance(value, str) for value in item.values()):
                raise ValueError("device record has non-string fields")
            device = MobileSshDevice(**item)
            try:
                canonical_id = _validate_device_id(device.device_id)
            except MobileSshError as exc:
                raise ValueError("device record has an invalid device_id") from exc
            if canonical_id != device.device_id:
                raise ValueError("device record has a non-canonical device_id")
            if device.device_id in devices:
                raise ValueError("device registry has duplicate device_id records")
            devices[device.device_id] = device
        return devices

    def _reload_locked(self) -> None:
        self._devices = self._load()
        self._publish_locked()

    def _publish_locked(self) -> None:
        self._snapshot = dict(self._devices)

    def refresh(self) -> None:
        with self._memory_lock:
            try:
                self._refresh_if_changed_locked()
            except MobileSshError:
                # Fail closed until a later refresh reads the registry cleanly.
                self._loaded_signature = None
                self._snapshot = {}
                raise

    def _refresh_if_changed_locked(self) -> None:
        # Another gateway process sharing this data home may enroll or revoke; a stat per
        # call notices it, and the JSON is re-parsed only when the file identity changed.
        try:
            file_stat = os.lstat(self._path)
        except FileNotFoundError:
            if self._loaded_signature is not None:
                self._devices = {}
                self._loaded_signature = None
                self._publish_locked()
            return
        except OSError as exc:
            raise MobileSshError(
                "store_unavailable", "mobile SSH device registry is unreadable", status=503
            ) from exc
        if self._signature(file_stat) != self._loaded_signature:
            self._reload_locked()

    def _persist_locked(self) -> None:
        payload = {
            "version": _STORE_VERSION,
            "devices": [asdict(self._devices[key]) for key in sorted(self._devices)],
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
        self._loaded_signature = signature
        self._publish_locked()

    def _with_file_lock(self) -> int:
        self._ensure_dir()
        if platform_compat.is_link_or_junction(self._lock_path):
            raise MobileSshError("store_unavailable", "mobile SSH lock is a link", status=503)
        try:
            fd = os.open(
                self._lock_path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
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

    def enroll(
        self,
        device_id: str,
        public_key: str,
        *,
        label: str = "",
        launcher: str,
        gateway_port: int = MOBILE_GATEWAY_PORT,
    ) -> EnrollmentResult:
        ident = _validate_device_id(device_id)
        display = _validate_label(label)
        canonical, key_sha256, fingerprint = validate_ed25519_public_key(public_key)
        if not 1 <= gateway_port <= 65535:
            raise MobileSshError("invalid_port", "gateway port must be between 1 and 65535")
        resolved_launcher = os.path.abspath(os.path.expanduser(launcher))
        if not Path(resolved_launcher).is_file() or not os.access(resolved_launcher, os.X_OK):
            raise MobileSshError("launcher_unavailable", "kirocrew launcher is not executable")
        binding = os.urandom(_BINDING_BYTES).hex()
        home = self.home
        device = MobileSshDevice(
            device_id=ident,
            label=display,
            key_sha256=key_sha256,
            ssh_fingerprint=fingerprint,
            enrollment_id=os.urandom(16).hex(),
            binding_sha256=_binding_digest(binding),
            enrolled_at=_utc_now(),
            gateway_port=str(gateway_port),
            home=str(home),
        )
        with self._memory_lock:
            fd = self._with_file_lock()
            try:
                with platform_compat.file_lock(fd, exclusive=True, required=True):
                    self._reload_locked()
                    current = self._devices.get(ident)
                    if current is not None and current.active:
                        raise MobileSshError(
                            "device_exists",
                            "an active enrollment already uses this device_id; revoke it first",
                            status=409,
                        )
                    if current is None and len(self._devices) >= MOBILE_SSH_MAX_DEVICES:
                        raise MobileSshError(
                            "device_limit_reached",
                            "mobile SSH device limit reached; re-enroll an existing device_id instead",
                            status=409,
                        )
                    if any(
                        existing.active
                        and existing.device_id != ident
                        and hmac.compare_digest(existing.key_sha256, key_sha256)
                        for existing in self._devices.values()
                    ):
                        raise MobileSshError(
                            "public_key_exists",
                            "this public key is already enrolled to another active device",
                            status=409,
                        )
                    self._devices[ident] = device
                    try:
                        self._persist_locked()
                    except MobileSshError:
                        # Never honor an enrollment the caller was told failed.
                        if current is None:
                            self._devices.pop(ident, None)
                        else:
                            self._devices[ident] = current
                        raise
            finally:
                os.close(fd)
        return EnrollmentResult(
            device=device,
            authorized_keys_line=_authorized_keys_line(
                canonical, ident, binding, resolved_launcher, gateway_port, home
            ),
        )

    @property
    def home(self) -> Path:
        return self._root.resolve()

    def list_devices(self) -> list[dict[str, object]]:
        with self._memory_lock:
            self._refresh_if_changed_locked()
            return [self._devices[key].public_metadata() for key in sorted(self._devices)]

    def revoke(self, device_id: str) -> MobileSshDevice:
        ident = _validate_device_id(device_id)
        with self._memory_lock:
            fd = self._with_file_lock()
            try:
                with platform_compat.file_lock(fd, exclusive=True, required=True):
                    self._reload_locked()
                    current = self._devices.get(ident)
                    if current is None:
                        raise MobileSshError(
                            "device_not_found", "device is not enrolled", status=404
                        )
                    if not current.active:
                        return current
                    revoked = MobileSshDevice(**{**asdict(current), "revoked_at": _utc_now()})
                    self._devices[ident] = revoked
                    try:
                        self._persist_locked()
                    except MobileSshError:
                        self._devices[ident] = current
                        raise
            finally:
                os.close(fd)
        return revoked

    def mint(self, device_id: str, binding: str) -> dict[str, object]:
        ident = _validate_device_id(device_id)
        if len(binding) != _BINDING_BYTES * 2 or any(
            ch not in "0123456789abcdef" for ch in binding
        ):
            raise MobileSshError(
                "key_binding_failed", "SSH key binding was not established", status=403
            )
        with self._memory_lock:
            self._refresh_if_changed_locked()
            device = self._devices.get(ident)
            if device is None:
                raise MobileSshError("device_not_found", "device is not enrolled", status=404)
            if not device.active:
                raise MobileSshError("device_revoked", "device enrollment is revoked", status=403)
            if not hmac.compare_digest(device.binding_sha256, _binding_digest(binding)):
                raise MobileSshError(
                    "key_binding_failed", "SSH key binding was not established", status=403
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
        from kiro_crew.dashboard.token_auth import _b64url_decode, generate_token

        token = generate_token(
            f"mobile:{device.device_id}", ttl_seconds=MOBILE_TOKEN_TTL_SECS, extra=claims
        )
        payload = json.loads(_b64url_decode(token.split(".", 1)[0]))
        expires_at_epoch = float(payload["session_exp"])
        expires_at = (
            datetime.fromtimestamp(expires_at_epoch, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        return {
            "schema": MOBILE_TOKEN_SCHEMA,
            "token": token,
            "token_type": "Bearer",
            "device_id": device.device_id,
            "audience": MOBILE_TOKEN_AUDIENCE,
            "scope": MOBILE_TOKEN_SCOPE,
            "expires_at": expires_at,
            "expires_at_epoch": expires_at_epoch,
            "expires_in": MOBILE_TOKEN_TTL_SECS,
            "refresh": "repeat_ssh_exec",
        }

    def validate_claims(self, claims: dict[str, object]) -> tuple[bool, str]:
        expected = {
            "kind": MOBILE_TOKEN_KIND,
            "aud": MOBILE_TOKEN_AUDIENCE,
            "scope": MOBILE_TOKEN_SCOPE,
            "no_refresh": "1",
        }
        if any(claims.get(key) != value for key, value in expected.items()):
            return False, "mobile token audience or scope invalid"
        device_id = claims.get("device_id")
        key_sha256 = claims.get("key_sha256")
        enrollment_id = claims.get("enrollment_id")
        if not isinstance(device_id, str) or not device_id:
            return False, "mobile token binding missing"
        if not isinstance(key_sha256, str) or not key_sha256:
            return False, "mobile token binding missing"
        if not isinstance(enrollment_id, str) or not enrollment_id:
            return False, "mobile token binding missing"
        if claims.get("sub") != f"mobile:{device_id}":
            return False, "mobile token subject invalid"
        issued_raw = claims.get("iat")
        session_exp_raw = claims.get("session_exp")
        if (
            isinstance(issued_raw, bool)
            or not isinstance(issued_raw, (int, float))
            or isinstance(session_exp_raw, bool)
            or not isinstance(session_exp_raw, (int, float))
        ):
            return False, "mobile token lifetime invalid"
        issued = float(issued_raw)
        session_exp = float(session_exp_raw)
        if not math.isfinite(issued) or not math.isfinite(session_exp):
            return False, "mobile token lifetime invalid"
        if session_exp <= issued or session_exp - issued > MOBILE_TOKEN_TTL_SECS + 1:
            return False, "mobile token lifetime invalid"
        # In-memory only: the middleware refreshes the registry off the event loop.
        device = self._snapshot.get(device_id)
        if device is None:
            return False, "mobile device not enrolled"
        if not device.active:
            return False, "mobile device revoked"
        if not hmac.compare_digest(device.key_sha256, key_sha256):
            return False, "mobile device key mismatch"
        if not hmac.compare_digest(device.enrollment_id, enrollment_id):
            return False, "mobile device enrollment replaced"
        return True, ""


def mobile_token_path_allowed(path: str, method: str = "GET") -> bool:
    """Whether the native mobile scope grants this gateway route and method."""
    if path in MOBILE_TOKEN_DENIED_PATHS:
        return False
    if path in MOBILE_TOKEN_READ_ONLY_PATHS:
        return method.upper() in _READ_METHODS
    return (
        path in MOBILE_TOKEN_ALLOWED_PATHS
        or path in MOBILE_TOKEN_ALLOWED_COLLECTIONS
        or path.startswith(MOBILE_TOKEN_ALLOWED_PREFIXES)
    )


_store_singleton: MobileSshDeviceStore | None = None
_store_lock = threading.Lock()


def get_mobile_ssh_store() -> MobileSshDeviceStore:
    global _store_singleton
    if _store_singleton is None:
        with _store_lock:
            if _store_singleton is None:
                _store_singleton = MobileSshDeviceStore()
    return _store_singleton


def refresh_mobile_ssh_store() -> None:
    """Construct or re-read the registry; call off the event loop."""
    try:
        get_mobile_ssh_store().refresh()
    except (MobileSshError, OSError):
        store = _store_singleton
        if store is not None:
            store._snapshot = {}


def validate_mobile_token_claims(claims: dict[str, object]) -> tuple[bool, str]:
    store = _store_singleton
    if store is None:
        return False, "mobile device registry unavailable"
    return store.validate_claims(claims)
