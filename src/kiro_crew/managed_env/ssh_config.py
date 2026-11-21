"""The ``Host crew-sbx-*`` block of an ssh config: one marked block, idempotent.

:func:`ensure_block` writes (or replaces) only the text between its two marker lines. Every
other byte of the file stays as it was, including a foreign ``Host crew-sbx-*`` block the
user wrote by hand (this module never reads inside it). A marker pair that is damaged
(one marker missing, or markers twice) is refused rather than guessed at. The file is
written through a temp file and renamed, mode 0600 for a new file, and a symlink is
followed to its target.

Settings: the managed environment's ssh server does no client authentication; the Session
Manager stream the ``ProxyCommand`` opens is the credential, minted per ssh run by the
dashboard's owner-checked loopback route (:mod:`.mint_and_attach`). No agent, X11 or remote
forwarding; Crew's tunnel adds only a loopback ``-L``. ``User root``, no key, and the host
key is neither checked nor stored: the stream already ends at the one environment the
service minted it for.

Run as a module to write the block for the current Python::

    python -m kiro_crew.managed_env.ssh_config [--config PATH]
"""

from __future__ import annotations

import os
import shlex
import stat
import sys
import tempfile
from pathlib import Path

BEGIN = "# >>> kirocrew crew-sbx (managed block; do not edit between the markers) >>>"
END = "# <<< kirocrew crew-sbx <<<"
HOST_PATTERN = "crew-sbx-*"
_MODULE = "kiro_crew.managed_env.mint_and_attach"


class SshConfigError(Exception):
    pass


def render_block(python: str) -> str:
    if not python or not os.path.isabs(python) or any(c in python for c in "\n\r\0"):
        raise SshConfigError("python must be an absolute path")
    proxy = f"{shlex.quote(python)} -m {_MODULE} %n"
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
    start = text.index(BEGIN)
    if start != 0 and text[start - 1] != "\n":
        raise SshConfigError("the begin marker is not at the start of a line")
    end = text.index(END)
    if end < start:
        raise SshConfigError("the crew-sbx markers are out of order")
    end += len(END)
    if end < len(text) and text[end] == "\n":
        end += 1
    return start, end


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
    if old is None:
        new = block
    else:
        span = _locate(old)
        if span:
            new = old[: span[0]] + block + old[span[1] :]
        else:
            sep = (
                ""
                if old == "" or old.endswith("\n\n")
                else ("\n" if old.endswith("\n") else "\n\n")
            )
            new = old + sep + block
    if new == old:
        return False
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".crew-sbx-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(new.encode("utf-8"))
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def main(argv: list[str]) -> int:
    config = os.path.expanduser("~/.ssh/config")
    if len(argv) == 3 and argv[1] == "--config":
        config = argv[2]
    elif len(argv) != 1:
        sys.stderr.write("usage: python -m kiro_crew.managed_env.ssh_config [--config PATH]\n")
        return 2
    try:
        changed = ensure_block(config, sys.executable)
    except SshConfigError as e:
        sys.stderr.write(f"ssh-config: {e}\n")
        return 1
    print("ssh-config: block written" if changed else "ssh-config: block already current")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
