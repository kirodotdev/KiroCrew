"""The ``Host crew-sbx-*`` block of an ssh config: one marked block, idempotent.

:func:`ensure_block` writes (or replaces) the text between its two marker lines, first in the file
(ssh takes the first value it reads for each option). Every
other line of the file is kept, including a foreign ``Host crew-sbx-*`` block the user
wrote by hand (this module never reads inside it), with two edits: the rest of the file
loses its leading LF newlines and, when anything is left, follows the block after one blank
line; and an old block cut from below another section, with lines after it, leaves a
``Host *`` in its place so those lines stay global. A marker pair that is damaged
(one marker missing, or markers twice) is refused rather than guessed at. The file is
written through a temp file and renamed, mode 0600 for a new file, and a symlink is
followed to its target.

Settings: the managed environment's ssh server does no client authentication; the Session
Manager stream the ``ProxyCommand`` opens is the credential, minted per ssh run by the
dashboard's owner-checked loopback route (:mod:`.mint_and_attach`). No agent, X11 or remote
forwarding; Crew's tunnel adds only a loopback ``-L``. ``User root``, no key, and the host
key is neither checked nor stored: the stream already ends at the one environment the
service minted it for.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
import sys
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.owner_only_files import mkdirs_owner_only

BEGIN = "# >>> kirocrew crew-sbx (managed block; do not edit between the markers) >>>"
END = "# <<< kirocrew crew-sbx <<<"
HOST_PATTERN = "crew-sbx-*"
#: A ``Host`` or ``Match`` line: it opens a section that runs to the next one.
_SECTION_RE = re.compile(r"^[ \t]*(host|match)\b", re.IGNORECASE | re.MULTILINE)
_MODULE = "kiro_crew.managed_env.mint_and_attach"


class SshConfigError(Exception):
    pass


def render_block(python: str) -> str:
    if not python or not os.path.isabs(python) or any(c in python for c in "\n\r\0"):
        raise SshConfigError("python must be an absolute path")
    proxy = f"{shlex.quote(python.replace('%', '%%'))} -P -m {_MODULE} %n"
    lines = [
        BEGIN,
        f"Host {HOST_PATTERN}",
        "  User root",
        f"  ProxyCommand {proxy}",
        "  PreferredAuthentications none",
        "  StrictHostKeyChecking no",
        "  UserKnownHostsFile /dev/null",
        "  ForwardAgent no",
        "  ForwardX11 no",
        "  GatewayPorts no",
        "  ServerAliveInterval 15",
        "  ConnectTimeout 60",
        "  LogLevel ERROR",
        # Ends the section, so global lines that began the file keep applying to every host.
        "Host *",
        END,
    ]
    return "\n".join(lines) + "\n"


def _locate(text: str) -> tuple[int, int] | None:
    """(start, end) of the managed block, end after its newline; None if absent."""
    nb, ne = text.count(BEGIN), text.count(END)
    if nb == 0 and ne == 0:
        return None
    if nb != 1 or ne != 1:
        raise SshConfigError("the crew-sbx markers are damaged (not exactly one pair): fix by hand")
    start, end = text.index(BEGIN), text.index(END)
    if end < start:
        raise SshConfigError("the crew-sbx markers are out of order")
    # Each marker is a whole line: text beside one would be left behind as an ssh option.
    # A line ends in LF or CRLF (a config saved on Windows).
    for at, marker in ((start, BEGIN), (end, END)):
        after = at + len(marker)
        tail = text[after : after + 2]
        if (at and text[at - 1] != "\n") or not (
            after == len(text) or tail.startswith("\n") or tail == "\r\n"
        ):
            raise SshConfigError("a crew-sbx marker is not a line of its own: fix by hand")
    end += len(END)
    if text[end : end + 2] == "\r\n":
        end += 2
    elif text[end : end + 1] == "\n":
        end += 1
    return start, end


def default_config() -> str:
    return os.path.expanduser("~/.ssh/config")


def gateway_python() -> str:
    """The interpreter the ProxyCommand runs: this install's, by a path that survives its
    updates. A managed venv's versioned tree is pruned two updates on, so it is named
    through the stable ``crew-venv-current`` link; any other install by its own path."""
    try:
        from kiro_crew.platform import wheel_engine

        layout = wheel_engine.managed_venv_layout()
        stable = layout.stable_link / "bin" / "python"
        if wheel_engine.running_from_managed_venv(layout) and stable.exists():
            return str(stable)
    except Exception:  # noqa: BLE001 - an unreadable layout falls back to this process's own
        pass
    return sys.executable


def block_current(config_path: str | os.PathLike[str] | None = None, python: str = "") -> bool:
    """Whether the managed block is there and names *python* (default
    :func:`gateway_python`): a block for any other interpreter reads as missing, so the
    dashboard offers to write it again."""
    try:
        text = Path(config_path or default_config()).read_text(encoding="utf-8")
        span = _locate(text)
        return span is not None and text[span[0] : span[1]] == render_block(
            python or gateway_python()
        )
    except (OSError, UnicodeDecodeError, SshConfigError):
        return False


def ensure_block(config_path: str | os.PathLike[str], python: str) -> bool:
    """Make the file hold exactly one current block. True if the file changed."""
    path = Path(os.path.realpath(config_path))
    block = render_block(python)
    try:
        old = path.read_bytes().decode("utf-8")
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        old, mode = None, 0o600
    except (OSError, UnicodeDecodeError) as e:
        raise SshConfigError(f"cannot read {path} ({type(e).__name__})") from None
    # ssh takes the first value it reads for each option, so the block goes first in
    # the file: a ``Host *`` default above it (a ProxyJump, say) would win otherwise.
    rest = old or ""
    span = _locate(rest)
    if span:
        before, after = rest[: span[0]], rest[span[1] :]
        # The block ended with ``Host *``. Cut from below another section, the lines
        # after it would join that section; a ``Host *`` at the cut keeps them global.
        if after.strip() and _SECTION_RE.search(before):
            after = "Host *\n" + after
        rest = before + after
    rest = rest.lstrip("\n")
    new = block + ("\n" + rest if rest else "")
    if new == old:
        return False
    mkdirs_owner_only(path.parent)
    atomic_write(path, new.encode("utf-8"), fsync=True, mode=mode)
    return True
