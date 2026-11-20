"""Local machine: the OS-protected data key that seals connected-computer secrets.

The local grant document (:mod:`.local_store`) lives in the file vault, whose
key sits beside it on disk. That hides it from the agent sandbox but does not
bind it to the user's OS login, so the connector credential, the peer key and
the install key are sealed again under a 32-byte **data key** that the OS keeps:

* Linux -- an item in the Secret Service (GNOME Keyring, KWallet), through
  ``secret-tool`` from libsecret;
* macOS -- a generic-password item in the user's login Keychain, through
  ``/usr/bin/security``;
* Windows -- the key sealed by DPAPI for this Windows user
  (``CryptProtectData``, no UI), kept in an owner-only file in the data home.

Each adapter only loads and saves that one key. A tool gets the key on stdin
and gives it back on stdout; it is never in an argv, a log line or an error
message. One item per data home (:func:`account_for`), so two installs on one
login never share a key.

Anything else fails CLOSED with :class:`ProtectionUnavailable`: no tool, no
session bus, a locked keyring that cannot prompt, a save that does not read
back, any other platform. There is no plaintext fallback, by design: the
Windows file holds only the DPAPI-sealed key, which opens only for this user.

Adapter API (also what a native host adapter, e.g. the desktop app's
``safeStorage``, implements):

``backend: str``
    a short name shown in a refusal;
``load() -> bytes | None``
    the 32-byte key, or None when this account never stored one; raises
    :class:`ProtectionUnavailable` when the store cannot answer;
``save(key: bytes) -> None``
    store or replace the key, then confirm it reads back; raises
    :class:`ProtectionUnavailable` otherwise.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Final, Optional, Protocol

from kiro_crew.platform_compat import trusted_system_bin_quiet

#: The OS item's service name (Secret Service ``service``, Keychain ``-s``).
SERVICE: Final[str] = "kiro-crew-connected-computers"
LABEL: Final[str] = "Kiro Crew connected computers"
KEY_BYTES: Final[int] = 32
#: Longest wait for the OS store (a locked keyring may show an unlock prompt).
TOOL_TIMEOUT_SECS: Final[float] = 30.0
_MAX_OUTPUT: Final[int] = 4096
#: ``security`` exits 44 when the item does not exist (errSecItemNotFound).
_MAC_NOT_FOUND: Final[int] = 44
_MAC_SECURITY: Final[str] = "/usr/bin/security"
#: The Windows file holding the DPAPI-sealed key, in the data home.
DPAPI_KEY_FILE: Final[str] = "connected-computers-key.dpapi"
#: Largest sealed key file read; a sealed 32-byte key is a few hundred bytes.
_MAX_DPAPI_BLOB: Final[int] = 16384
#: ``CRYPTPROTECT_UI_FORBIDDEN``: fail rather than show any prompt.
_CRYPTPROTECT_UI_FORBIDDEN: Final[int] = 0x1

#: ``(argv, stdin) -> (returncode, stdout, stderr)``
Runner = Callable[..., tuple[int, bytes, bytes]]


class ProtectionUnavailable(RuntimeError):
    """The OS cannot protect connected-computer secrets here; the message is safe to show."""


class ProtectedStore(Protocol):
    backend: str

    def load(self) -> Optional[bytes]: ...

    def save(self, key: bytes) -> None: ...


def _run(argv: list[str], stdin: bytes = b"") -> tuple[int, bytes, bytes]:
    try:
        proc = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            timeout=TOOL_TIMEOUT_SECS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProtectionUnavailable("the OS keyring did not answer in time") from exc
    except OSError as exc:
        raise ProtectionUnavailable("the OS keyring tool could not be started") from exc
    return proc.returncode, proc.stdout[:_MAX_OUTPUT], proc.stderr[:_MAX_OUTPUT]


def account_for(config_dir: str | Path) -> str:
    """The item's account for one data home (no path is revealed in it)."""
    real = os.path.realpath(os.fspath(config_dir))
    return "home-" + hashlib.sha256(real.encode("utf-8")).hexdigest()[:24]


def _parse_key(stdout: bytes, backend: str) -> bytes:
    text = stdout.strip()
    try:
        key = bytes.fromhex(text.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtectionUnavailable(f"the {backend} item for this computer is unreadable") from exc
    if len(key) != KEY_BYTES:
        raise ProtectionUnavailable(f"the {backend} item for this computer is unreadable")
    return key


def _check_key(key: bytes) -> str:
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_BYTES:
        raise ValueError("the data key must be 32 bytes")
    return bytes(key).hex()


class LibsecretStore:
    """The Linux Secret Service, through ``secret-tool``."""

    backend = "Secret Service"

    def __init__(self, account: str, *, runner: Runner = _run, tool: str = "secret-tool") -> None:
        self._account = account
        self._run = runner
        self._tool = tool

    @classmethod
    def for_account(cls, account: str) -> "LibsecretStore":
        # Fixed system folders only: a secret-tool planted earlier on PATH would
        # be handed the data key on its stdin.
        tool = trusted_system_bin_quiet("secret-tool")
        if tool is None:
            raise ProtectionUnavailable(
                "this computer has no Secret Service tool (secret-tool from libsecret)"
            )
        if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and not _runtime_bus_exists():
            raise ProtectionUnavailable("this login has no session bus for the Secret Service")
        return cls(account, tool=tool)

    def _attrs(self) -> list[str]:
        return ["service", SERVICE, "account", self._account]

    def load(self) -> Optional[bytes]:
        """An empty lookup is ambiguous: absent item or dismissed unlock.

        The grant store treats None as unavailable while sealed values exist.
        """
        code, out, err = self._run([self._tool, "lookup", *self._attrs()])
        if code == 0 and out.strip():
            return _parse_key(out, self.backend)
        if code in (0, 1) and not out.strip() and not err.strip():
            return None
        raise ProtectionUnavailable("the Secret Service is locked or not answering")

    def save(self, key: bytes) -> None:
        hexkey = _check_key(key)
        code, _out, _err = self._run(
            [self._tool, "store", f"--label={LABEL}", *self._attrs()],
            stdin=hexkey.encode("ascii"),
        )
        if code != 0:
            raise ProtectionUnavailable("the Secret Service did not store the key")
        try:
            back = self.load()
        except ProtectionUnavailable as exc:
            raise ProtectionUnavailable("the Secret Service did not keep the key") from exc
        if back != bytes(key):
            raise ProtectionUnavailable("the Secret Service did not keep the key")


def _runtime_bus_exists() -> bool:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    return bool(runtime) and os.path.exists(os.path.join(runtime, "bus"))


class MacKeychainStore:
    """The user's login Keychain, through ``/usr/bin/security``."""

    backend = "Keychain"

    def __init__(self, account: str, *, runner: Runner = _run, tool: str = _MAC_SECURITY) -> None:
        self._account = account
        self._run = runner
        self._tool = tool

    @classmethod
    def for_account(cls, account: str) -> "MacKeychainStore":
        # The fixed system path only: a ``security`` planted on PATH would be
        # handed the data key on its stdin.
        if not os.path.isfile(_MAC_SECURITY):
            raise ProtectionUnavailable("this Mac has no security tool for the Keychain")
        return cls(account)

    def load(self) -> Optional[bytes]:
        """None only for "no such item"; a locked or dismissed Keychain is unavailable."""
        code, out, _err = self._run(
            [self._tool, "find-generic-password", "-s", SERVICE, "-a", self._account, "-w"]
        )
        if code == 0:
            return _parse_key(out, self.backend)
        if code == _MAC_NOT_FOUND:
            return None
        raise ProtectionUnavailable("the Keychain is locked or not answering")

    def save(self, key: bytes) -> None:
        hexkey = _check_key(key)
        # ``security -i`` reads its command from stdin, so the key is in no argv.
        # Every token is a fixed literal, a hex account or the hex key, so
        # nothing needs quoting but the fixed label.
        command = (
            f"add-generic-password -U -s {SERVICE} -a {self._account} "
            f'-l "{LABEL}" -w {hexkey}\n'
        )
        code, _out, _err = self._run([self._tool, "-i"], stdin=command.encode("ascii"))
        if code != 0:
            raise ProtectionUnavailable("the Keychain did not store the key")
        try:
            back = self.load()
        except ProtectionUnavailable as exc:
            raise ProtectionUnavailable("the Keychain did not keep the key") from exc
        if back != bytes(key):
            # ``security -i`` can exit 0 when its one command failed: the read
            # back is what proves the save.
            raise ProtectionUnavailable("the Keychain did not keep the key")


class _Blob(ctypes.Structure):
    """``DATA_BLOB``: a byte count and a pointer."""

    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _load_dpapi() -> tuple[Any, Any]:
    """``crypt32`` and ``kernel32``; :class:`ProtectionUnavailable` off Windows."""
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise ProtectionUnavailable("Windows data protection is not available here")
    try:
        crypt32 = loader("crypt32", use_last_error=True)
        kernel32 = loader("kernel32", use_last_error=True)
    except OSError as exc:
        raise ProtectionUnavailable("Windows data protection could not be loaded") from exc
    blob = ctypes.POINTER(_Blob)
    for fn in (crypt32.CryptProtectData, crypt32.CryptUnprotectData):
        fn.argtypes = [
            blob,
            ctypes.c_wchar_p,
            blob,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            blob,
        ]
        fn.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _blob_of(buffer: Any, size: int) -> _Blob:
    return _Blob(size, ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _dpapi(seal: bool, data: bytes, entropy: bytes) -> bytes:
    """Seal (``CryptProtectData``) or open (``CryptUnprotectData``) for this user, no UI."""
    crypt32, kernel32 = _load_dpapi()
    data_buf = ctypes.create_string_buffer(data, len(data))
    entropy_buf = ctypes.create_string_buffer(entropy, len(entropy))
    source = _blob_of(data_buf, len(data))
    salt = _blob_of(entropy_buf, len(entropy))
    out = _Blob()
    call = crypt32.CryptProtectData if seal else crypt32.CryptUnprotectData
    description = LABEL if seal else None
    ok = call(
        ctypes.byref(source),
        description,
        ctypes.byref(salt),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out),
    )
    if not ok:
        what = "seal" if seal else "open"
        raise ProtectionUnavailable(f"Windows could not {what} the key for this computer")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


class WindowsDpapiStore:
    """Windows: the key sealed by DPAPI for this user, in an owner-only file.

    DPAPI binds the sealed key to the Windows login; the entropy binds it to
    this data home's account, so a file copied to another data home does not
    open there. The file is written atomically and owner-only from birth
    (``atomic_write(restrict_to_owner=True)``: an owner-only DACL).
    """

    backend = "Windows DPAPI"

    def __init__(
        self,
        path: Path,
        account: str,
        *,
        seal: Optional[Callable[[bytes, bytes], bytes]] = None,
        unseal: Optional[Callable[[bytes, bytes], bytes]] = None,
    ) -> None:
        self._path = Path(path)
        self._entropy = f"{SERVICE}:{account}".encode("ascii")
        self._seal = seal or (lambda data, entropy: _dpapi(True, data, entropy))
        self._unseal = unseal or (lambda data, entropy: _dpapi(False, data, entropy))

    def load(self) -> Optional[bytes]:
        try:
            with open(self._path, "rb") as handle:
                blob = handle.read(_MAX_DPAPI_BLOB + 1)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ProtectionUnavailable(
                "the protected key for this computer cannot be read"
            ) from exc
        if not blob or len(blob) > _MAX_DPAPI_BLOB:
            raise ProtectionUnavailable(f"the {self.backend} item for this computer is unreadable")
        key = self._unseal(blob, self._entropy)
        if len(key) != KEY_BYTES:
            raise ProtectionUnavailable(f"the {self.backend} item for this computer is unreadable")
        return key

    def save(self, key: bytes) -> None:
        from kiro_crew.atomic_write import atomic_write

        _check_key(key)
        blob = self._seal(bytes(key), self._entropy)
        try:
            atomic_write(self._path, blob, fsync=True, restrict_to_owner=True)
        except OSError as exc:
            raise ProtectionUnavailable("Windows did not store the key") from exc
        try:
            back = self.load()
        except ProtectionUnavailable as exc:
            raise ProtectionUnavailable("Windows did not keep the key") from exc
        if back != bytes(key):
            raise ProtectionUnavailable("Windows did not keep the key")


def protected_store_for(config_dir: str | Path) -> ProtectedStore:
    """This machine's OS store for the data home ``config_dir``, or a refusal."""
    account = account_for(config_dir)
    if sys.platform.startswith("linux"):
        return LibsecretStore.for_account(account)
    if sys.platform == "darwin":
        return MacKeychainStore.for_account(account)
    if sys.platform == "win32":
        return WindowsDpapiStore(Path(config_dir) / DPAPI_KEY_FILE, account)
    raise ProtectionUnavailable(
        "this computer has no OS protection Kiro Crew can use for connected computers"
    )


def protected_store_for_this_machine() -> ProtectedStore:
    from kiro_crew.config.paths import config_dir

    return protected_store_for(config_dir())
