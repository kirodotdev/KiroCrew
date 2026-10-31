"""Read the default Kerberos ccache name from the krb5 config, as libkrb5 does.

:func:`kiro_crew.env.krb5_keyring_ccache` asks this module which
``default_ccache_name`` a child would get when ``KRB5CCNAME`` is unset. The
reader follows libkrb5's lookup order across ``KRB5_CONFIG`` (else
``/etc/krb5.conf``) and its ``include`` / ``includedir`` files, opens only
regular files, and never raises on an unreadable config.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

# A krb5 config file is a few KiB; read at most this much of one.
_KRB5_CONF_READ_CAP = 256 * 1024

# The keyring kinds the kernel registers per user namespace. A session,
# process or thread keyring is the calling process's own and is not looked up
# through a per-namespace register, so it is not claimed here.
_KRB5_PER_USERNS_KEYRINGS = ("KEYRING:persistent:", "KEYRING:user:")
# The same kinds with no uid, which libkrb5 resolves to the caller's own.
_KRB5_PER_USERNS_BARE = ("KEYRING:persistent", "KEYRING:user")


def _krb5_conf_default_ccache(env: Mapping[str, str]) -> str | None:
    """First ``default_ccache_name`` in a ``[libdefaults]`` section, or ``None``.

    Follows libkrb5's lookup order: the ``KRB5_CONFIG`` files (else
    ``/etc/krb5.conf``) in order, each read top to bottom with its ``include``
    / ``includedir`` files read where the directive stands, and the first
    value found wins.
    """
    for root in env.get("KRB5_CONFIG", "/etc/krb5.conf").split(":"):
        if root:
            found = _libdefaults_ccache(Path(root), depth=0)
            if found is not None:
                return found
    return None


def _krb5_includedir_files(directory: Path) -> list[Path]:
    # libkrb5 reads only names made of [A-Za-z0-9_-] or ending in ``.conf``.
    try:
        entries = sorted(directory.iterdir())
    except (OSError, ValueError):
        return []
    return [
        p
        for p in entries
        if (p.name.endswith(".conf") or all(c.isalnum() or c in "_-" for c in p.name))
        and p.is_file()
    ]


def _read_krb5_conf(path: Path) -> str | None:
    """Text of *path* when it is a regular file, capped; ``None`` otherwise.

    The path is ``stat``-ed first so nothing but a regular file is opened, and
    the open itself uses ``O_NONBLOCK | O_NOCTTY`` with an ``fstat`` re-check, so
    a FIFO or device named in the config never blocks the spawn path.
    """
    flags = os.O_RDONLY
    for name in ("O_NONBLOCK", "O_NOCTTY", "O_CLOEXEC"):
        flags |= getattr(os, name, 0)
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return None
        fd = os.open(path, flags)
    except (OSError, ValueError):  # ValueError: a NUL byte in the path
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, _KRB5_CONF_READ_CAP).decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def _libdefaults_ccache(path: Path, depth: int, seen: set[Path] | None = None) -> str | None:
    seen = set() if seen is None else seen
    if path in seen:
        return None
    seen.add(path)
    text = _read_krb5_conf(path)
    if text is None:
        return None
    section = ""
    # Subsection depth: a key inside ``name = { ... }`` is not a libdefaults key.
    braces = 0
    for raw in text.splitlines():
        line = raw.strip()
        # libkrb5 honours include/includedir only at the start of a line.
        parts = raw.split(None, 1) if raw[:1] not in (" ", "\t") else []
        directive = parts[0] if parts else ""
        target = parts[1].strip() if len(parts) > 1 else ""
        if directive in ("include", "includedir") and target:
            if depth >= 2:
                continue
            included = (
                [Path(target)] if directive == "include" else _krb5_includedir_files(Path(target))
            )
            for child in included:
                found = _libdefaults_ccache(child, depth + 1, seen)
                if found is not None:
                    return found
        elif line.startswith("[") and line.endswith(("]", "]*")):
            # A trailing ``*`` marks the section final; it is still a header.
            section = line[1 : line.rindex("]")].strip().lower()
            braces = 0
        elif line.startswith("}"):
            braces = max(braces - 1, 0)
        elif not line.startswith(("#", ";")):
            key, sep, value = line.partition("=")
            value = value.strip()
            if not sep:
                continue
            if value[:1] == "{":
                braces += 1
            # libkrb5 ends the tag at the first ``*`` (the relation-final
            # marker); a tag with blanks inside it is a syntax error there.
            elif (
                section == "libdefaults"
                and braces == 0
                and not any(c.isspace() for c in key.strip())
                and key.strip().split("*", 1)[0] == "default_ccache_name"
            ):
                if value[:1] == '"':
                    value = _krb5_quoted_value(value[1:])
                if value:
                    return value
    return None


# libkrb5's profile parser decodes these escapes in a double-quoted value; any
# other escaped character stands for itself.
_KRB5_QUOTED_ESCAPES = {"n": "\n", "t": "\t", "b": "\b"}


def _krb5_quoted_value(text: str) -> str:
    """Decode a double-quoted krb5 profile value, *text* starting after the opening quote."""
    out: list[str] = []
    chars = iter(text)
    for ch in chars:
        if ch == '"':
            break
        if ch == "\\":
            escaped = next(chars, "")
            out.append(_KRB5_QUOTED_ESCAPES.get(escaped, escaped))
        else:
            out.append(ch)
    return "".join(out)
