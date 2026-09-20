"""AppStorage — app-scoped persistent key-value storage.

Backed by files in the app's data directory:
``~/.kiro/crew/apps/{app_name}/data/kv/{key}.json``

Keys are validated to prevent path traversal. A key that is not a usable
Windows filename is encoded into one, and ``list_keys`` decodes it back, so a
key round-trips to the same value on every platform.
Values are JSON-serializable dicts or strings.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from kiro_crew.atomic_write import atomic_write
from kiro_crew.constants import WINDOWS_DEVICE_STEMS
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Characters Win32 refuses in a filename. ``:`` is in the set for a second
#: reason: on NTFS ``a:b`` names an alternate data stream of ``a``, so the value
#: would be written somewhere a directory listing never shows.
_UNSAFE_KEY_CHARS = frozenset('<>:"|?*') | frozenset(chr(code) for code in range(32))

#: DOS device names. Win32 resolves a path whose last component STARTS with one
#: of these, up to its first ``.``, to the device rather than to a file in the
#: directory, so ``aux.json`` and ``aux.backup.json`` both name AUX. Matching is
#: case-insensitive and ignores trailing spaces.
#:
#: Derived from :data:`~kiro_crew.constants.WINDOWS_DEVICE_STEMS`, which is the one
#: definition of that set. The extras below are additive and stay here: they are
#: reachable in a STORAGE KEY, which is arbitrary app input, while the shared set
#: also serves identifier grammars (a branch name, an app name) whose own charsets
#: already exclude ``$`` and a superscript digit.
_RESERVED_DEVICE_NAMES = frozenset(
    {stem.upper() for stem in WINDOWS_DEVICE_STEMS}
    | {"CONIN$", "CONOUT$"}
    | {f"COM{sup}" for sup in "\u00b9\u00b2\u00b3"}
    | {f"LPT{sup}" for sup in "\u00b9\u00b2\u00b3"}
)

#: Marks a stem that carries a percent-encoded key. It begins with ``.``, which
#: :meth:`AppStorage._key_path` rejects in a key, so no key can ask for a name in
#: this namespace and an encoded stem is unambiguous.
_ENCODED_STEM_PREFIX = ".enc-"

#: Marks a stem that carries a DIGEST of the key rather than the key itself, for a
#: key whose percent-encoded name would not fit. Percent-encoding costs up to
#: three characters per character, so no reversible name-only form can be bounded;
#: this one is fixed width, and the key is recorded inside the file so
#: :meth:`AppStorage.list_keys` still reports it. Same leading ``.``, so the
#: namespace is closed the same way.
_DIGEST_STEM_PREFIX = ".encd-"

#: Bytes a single filename may occupy. NTFS, ext4 and APFS all stop at 255.
_MAX_FILENAME_BYTES = 255

#: Digest width in bytes. 128 bits, so two distinct keys sharing a stem is not a
#: case that has to be handled.
_DIGEST_BYTES = 16


def _key_is_filename_safe(key: str) -> bool:
    """Whether ``f"{key}.json"`` names a file on every platform."""
    if any(char in _UNSAFE_KEY_CHARS for char in key):
        return False
    # Win32 strips trailing spaces from a component and matches a device name
    # against the text before the first ".", which for this file is the text
    # before the first "." of the key.
    head = key.split(".", 1)[0].rstrip(" ")
    return head.upper() not in _RESERVED_DEVICE_NAMES


def _is_digest_stem(stem: str) -> bool:
    """Whether *stem* carries a digest, so the key lives in the file."""
    return stem.startswith(_DIGEST_STEM_PREFIX)


def _encode_key(key: str) -> str:
    """The filename stem for *key*.

    A filename-safe key is its own stem, byte for byte, so a store written by a
    build without this encoding keeps every key it can already address. Anything
    else is percent-encoded behind :data:`_ENCODED_STEM_PREFIX`, which escapes
    every unsafe character and leaves the marker as the component's head so the
    result is not a device name.

    A key whose percent-encoded name would pass :data:`_MAX_FILENAME_BYTES` gets
    the fixed-width digest form instead, so the name a key maps to is bounded
    however long the key is. Only an unsafe key can reach either encoding, so a
    safe key's filename is never rewritten by length.
    """
    if _key_is_filename_safe(key):
        return key
    escaped = _ENCODED_STEM_PREFIX + quote(key, safe="")
    if len(f"{escaped}.json".encode()) <= _MAX_FILENAME_BYTES:
        return escaped
    digest = hashlib.blake2b(key.encode(), digest_size=_DIGEST_BYTES).hexdigest()
    return _DIGEST_STEM_PREFIX + digest


def _decode_stem(stem: str) -> str:
    """The key a percent-encoded or plain filename stem carries.

    The inverse of :func:`_encode_key` for every stem that carries its key in the
    NAME. A digest stem does not, and is read from the file by
    :meth:`AppStorage.list_keys` instead.

    Raises :class:`UnicodeDecodeError` for a marked stem whose escapes are not
    UTF-8, which only a file placed here by something other than this store can
    produce.
    """
    if stem.startswith(_ENCODED_STEM_PREFIX):
        return unquote(stem[len(_ENCODED_STEM_PREFIX) :], errors="strict")
    return stem


class AppStorage:
    """App-scoped persistent key-value storage.

    File-per-key design:
    - Avoids contention (no single-file bottleneck)
    - Supports large values without loading entire store
    - Atomic writes prevent corruption on crash
    - Key validation rejects path traversal
    """

    def __init__(self, app_name: str, data_dir: Path) -> None:
        self._app_name = app_name
        self._kv_dir = data_dir / "kv"
        self._kv_dir.mkdir(parents=True, exist_ok=True)

    @property
    def app_name(self) -> str:
        return self._app_name

    def get(self, key: str) -> dict[str, Any] | str | None:
        """Read a value by key. Returns None if not found.

        Falls back to the unencoded location for a key that needs encoding, so a
        value a store already holds under the key's own name stays readable and
        agrees with what :meth:`list_keys` reports. ``is_file`` gates the read,
        which is also what keeps a DOS device name out of it: a device is not a
        regular file, so ``get("con")`` never opens the console.
        """
        path = self._key_path(key)
        if path.is_file():
            return self._read(path, enveloped=_is_digest_stem(path.stem))
        fallback = self._unencoded_key_path(key)
        if fallback is None or fallback == path or not fallback.is_file():
            return None
        # Written under the key's own name by a build without the encoding, so
        # the file holds the bare value however this key encodes now.
        return self._read(fallback, enveloped=False)

    def _read(self, path: Path, *, enveloped: bool) -> dict[str, Any] | str | None:
        """Load one value file. *enveloped* files carry their key beside the value.

        Bytes that are not UTF-8 are corruption, not a value: ``read_text`` raises
        ``UnicodeDecodeError``, which is a ``ValueError`` and so is caught here
        beside the JSON error rather than reaching the caller.
        """
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None  # corrupted file
        except OSError as exc:
            logger.warning("AppStorage[%s] read error for %s: %s", self._app_name, path.name, exc)
            return None
        if not enveloped:
            return payload
        if not isinstance(payload, dict) or "value" not in payload:
            return None  # corrupted envelope
        value: dict[str, Any] | str = payload["value"]
        return value

    def set(self, key: str, value: dict[str, Any] | str) -> None:
        """Write a value. Atomic write (tmp + rename).

        Always JSON-encodes the value so that round-trip is faithful
        (e.g. the string "42" is stored as '"42"', not raw 42).

        A key stored under a digest name is written beside its value, because the
        digest is one-way and :meth:`list_keys` has nowhere else to read it from.
        A copy of the key under its own name is removed once this write lands, so
        only one file ever holds the current value.
        """
        path = self._key_path(key)
        if _is_digest_stem(path.stem):
            content = json.dumps({"key": key, "value": value}, indent=2)
        else:
            content = json.dumps(value, indent=2)
        atomic_write(path, content)
        # The value under the key's own name is superseded the moment this write
        # lands, and leaving it is not inert: it is what a build without this
        # encoding reads, so it would serve that copy as current.
        superseded = self._unencoded_key_path(key)
        if superseded is not None and superseded != path:
            try:
                superseded.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "AppStorage[%s] could not remove the superseded %s: %s",
                    self._app_name,
                    superseded.name,
                    exc,
                )
        sel().log_api_access(
            caller=f"app:{self._app_name}",
            operation="app_storage.set",
            outcome="ok",
            resources=key,
        )

    def delete(self, key: str) -> bool:
        """Delete a key. Returns True if existed.

        Removes the unencoded location as well, so deleting a key a store holds
        under its own name does not leave a copy that :meth:`get` would serve
        next.
        """
        paths = [self._key_path(key)]
        fallback = self._unencoded_key_path(key)
        if fallback is not None and fallback not in paths:
            paths.append(fallback)
        removed = False
        for path in paths:
            if not path.is_file():
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            removed = True
        if not removed:
            return False
        sel().log_api_access(
            caller=f"app:{self._app_name}",
            operation="app_storage.delete",
            outcome="ok",
            resources=key,
        )
        return True

    def list_keys(self) -> list[str]:
        """List all stored keys.

        The inverse of :meth:`_key_path`: a stem carrying the encoding marker is
        decoded, a digest stem is read back from the file that carries its key,
        and every other stem is the key itself — including a key a store holds
        under its own name, which :meth:`get` reads through the same fallback, so
        the two never disagree about which keys exist. One key present in more
        than one location is reported once. A ``.json`` file this store did not
        write — a marked stem whose escapes are not UTF-8, or a digest name with
        no key in it — is skipped rather than reported under a mangled key.
        """
        if not self._kv_dir.is_dir():
            return []
        keys: set[str] = set()
        for path in self._kv_dir.iterdir():
            if path.suffix != ".json":
                continue
            if _is_digest_stem(path.stem):
                recovered = self._read_digest_key(path)
                if recovered is not None:
                    keys.add(recovered)
                continue
            try:
                keys.add(_decode_stem(path.stem))
            except UnicodeDecodeError:
                logger.warning(
                    "AppStorage[%s] skipping %s: the encoded stem is not UTF-8",
                    self._app_name,
                    path.name,
                )
        return sorted(keys)

    def _read_digest_key(self, path: Path) -> str | None:
        """The key a digest-named file records, or ``None`` when it records none.

        A digest-named file this store did not write is skipped like any other
        foreign file, so every way its bytes can fail to parse is caught here:
        ``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError`` and not a
        ``JSONDecodeError``, and letting it out would make one such file break
        every :meth:`list_keys` call until somebody deleted it.
        """
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("key"), str):
            key: str = payload["key"]
            return key
        logger.warning(
            "AppStorage[%s] skipping %s: no key recorded in a digest-named file",
            self._app_name,
            path.name,
        )
        return None

    def _key_path(self, key: str) -> Path:
        """Validate key and return file path.

        Raises ValueError if key contains path traversal characters.

        The leading-``.`` rejection also keeps :data:`_ENCODED_STEM_PREFIX` and
        :data:`_DIGEST_STEM_PREFIX` reachable only from :func:`_encode_key`, so a
        marked stem always means an encoded key.
        """
        if not key:
            raise ValueError("Storage key must not be empty")
        if ".." in key or "/" in key or "\\" in key:
            raise ValueError(f"Invalid storage key (path traversal): {key!r}")
        # Additional safety: reject keys that would produce unexpected paths
        if key.startswith(".") or key.startswith("~"):
            raise ValueError(f"Invalid storage key (unsafe prefix): {key!r}")
        return self._kv_dir / f"{_encode_key(key)}.json"

    def _unencoded_key_path(self, key: str) -> Path | None:
        """Where *key* sits when its own name was used as the filename.

        A store written without the encoding holds such a file for any key that
        needs encoding now, and the platform that could create it is the one
        whose filenames the key was always legal in.

        ``None`` unless the join produced exactly this one name inside the key
        directory. Both halves are required, and a drive is why: on Windows
        ``C:foo`` is a DRIVE-RELATIVE path, so joining it onto a key directory on
        that same drive yields ``kv/foo.json`` — a name INSIDE the directory that
        belongs to the unrelated key ``foo``, which :meth:`delete` would then
        unlink and :meth:`get` would read across. A different drive collapses to
        ``a:b.json``, outside the directory entirely.
        """
        candidate = self._kv_dir / f"{key}.json"
        if candidate.parent != self._kv_dir or candidate.name != f"{key}.json":
            return None
        return candidate
