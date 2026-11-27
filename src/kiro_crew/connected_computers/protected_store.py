"""Local machine: the OS-protected data key that seals connected-computer secrets.

The local grant document (:mod:`.local_store`) lives in the file vault, whose
key sits beside it on disk. That hides it from the agent sandbox but does not
bind it to the user's OS login, so the connector credential, the peer key and
the install key are sealed again under a 32-byte **data key** that the OS keeps:

* Linux -- an item in the Secret Service (GNOME Keyring, KWallet), through
  ``secret-tool`` from libsecret.

Each adapter only loads and saves that one key. The key goes to the tool on
stdin and comes back on stdout; it is never in an argv, a log line or an error
message. One item per data home (:func:`account_for`), so two installs on one
login never share a key.

Anything else fails CLOSED with :class:`ProtectionUnavailable`: no tool, no
session bus, a locked keyring that cannot prompt, a save that does not read
back, any other platform (macOS and Windows included, where connected computers
are not available yet). There is no plaintext or file fallback, by design.

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

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Final, Optional, Protocol

from kiro_crew.platform_compat import trusted_system_bin_quiet

#: The OS item's service name (the Secret Service ``service`` attribute).
SERVICE: Final[str] = "kiro-crew-connected-computers"
LABEL: Final[str] = "Kiro Crew connected computers"
KEY_BYTES: Final[int] = 32
#: Longest wait for the OS store (a locked keyring may show an unlock prompt).
TOOL_TIMEOUT_SECS: Final[float] = 30.0
_MAX_OUTPUT: Final[int] = 4096

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


def protected_store_for(config_dir: str | Path) -> ProtectedStore:
    """This machine's OS store for the data home ``config_dir``, or a refusal."""
    account = account_for(config_dir)
    if sys.platform.startswith("linux"):
        return LibsecretStore.for_account(account)
    raise ProtectionUnavailable(
        "this computer has no OS protection Kiro Crew can use for connected computers"
    )


def protected_store_for_this_machine() -> ProtectedStore:
    from kiro_crew.config.paths import config_dir

    return protected_store_for(config_dir())
