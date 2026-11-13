"""Tool-step labels as a Slack thread may show them.

A tool step's label is the dashboard's derived title (``tool_call_title``): a
humanized action ("Read config/loader.py", "Git status") when the command
classifies, else the raw command. That raw fallback is written for the
operator's own screen; a Slack thread is read by everyone in the channel, so
before a label goes there it loses what describes the machine rather than the
action:

* environment assignments (``VENV=/abs/.venv;``, ``FOO=1 cmd``,
  ``export X=…``, ``sudo X=… cmd``), which carry paths and values, not the
  action;
* absolute paths: under the session's working directory they become
  relative to it, under the home directory ``~/…``; any other path rooted in
  a filesystem area (``/opt``, ``/var``, ``/usr``, ``/tmp``, ...) becomes
  ``…/parent/name``; a path-shaped argument that is not one (``/api/v1``)
  stays;
* this host's name.

Display only. Nothing here decides what a command does or whether it may
run; the label already says no more than the command in effect, and this
only removes detail from it. Line breaks are kept, so a caller that cuts the
label to its first line still marks that more followed.
"""

from __future__ import annotations

import os
import re
import socket
from pathlib import PurePosixPath

#: What a label with nothing left after the scrub reads.
FALLBACK_LABEL = "Run a command"
#: Model-authored and scanned on the event loop, so only this much is read.
MAX_LABEL_INPUT_CHARS = 4096
#: Stands for this host's name. Not ``<host>``: Slack reads ``<…>`` as markup.
HOST_PLACEHOLDER = "[host]"

# A chain of assignment words where a command starts: at the start of a line,
# after a separator or an opening brace/bang, or after a word that runs the
# rest as a command. Matched whole in one pass; the lead is kept.
_ASSIGNMENTS = re.compile(
    r"(?P<lead>(?:^|[;&|({!]|\b(?:sudo|time|then|do|else|env)\b(?:[ \t]+-\S+)*)[ \t]*)"
    r"(?:export[ \t]+)?"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s;&|]*)[ \t]*)+",
    re.MULTILINE,
)
# A separator left with no command before or after it.
_DANGLING = re.compile(r"^[ \t]*(?:;|&&|\|\|)[ \t]*|(?:;|&&|\|\|)[ \t]*(?=;|&&|\|\||$)", re.M)
# An absolute path token. Not preceded by a word character, a dot, a tilde, a
# slash, a dash or a colon, so relative paths, ``~/x`` and URLs are left alone.
_ABS_PATH = re.compile(r"(?<![\w.~/:-])/[^\s'\"`;|&<>():]*")
#: First components that make an absolute path a filesystem location.
_FS_ROOTS = frozenset(
    {
        "home",
        "Users",
        "root",
        "local",
        "opt",
        "var",
        "srv",
        "mnt",
        "media",
        "usr",
        "tmp",
        "private",
        "Volumes",
        "data",
        "nix",
        "workspace",
        "workplace",
        "etc",
    }
)


def _variants(path: str) -> list[str]:
    """*path* as written and resolved, in both separator styles, longest first."""
    out: set[str] = set()
    for p in (path, os.path.realpath(path) if path else ""):
        for v in (p, p.replace("\\", "/")):
            v = v.rstrip("/\\")
            if len(v) > 1:
                out.add(v)
    return sorted(out, key=len, reverse=True)


def _hostnames() -> list[str]:
    try:
        name = socket.gethostname()
    except OSError:
        return []
    names = {name, name.split(".", 1)[0]}
    return sorted((n for n in names if len(n) >= 3), key=len, reverse=True)


def _replace_prefix(text: str, prefix: str, with_sep: str, bare: str) -> str:
    """Replace *prefix* wherever it starts a path (any left context) in *text*."""
    # Either separator may follow: a Windows path is often written with ``/``
    # after a ``\``-separated root (``C:\Users\me/src/x``).
    for sep in ("/", "\\"):
        text = text.replace(prefix + sep, with_sep)
    return re.sub(re.escape(prefix) + r"(?![\w.\-/\\])", bare, text)


def _path_label(path: str) -> str:
    # The label quotes a POSIX-shaped path (``_ABS_PATH`` matches only those),
    # so it is read as one on every host. Its root (``/``, or the ``//`` POSIX
    # keeps distinct) is not a component.
    parts = [p for p in PurePosixPath(path).parts if p.strip("/")]
    if len(parts) <= 1 or parts[0] not in _FS_ROOTS:
        return path
    return f"…/{PurePosixPath(*parts[-2:])}"


def slack_step_label(label: str, cwd: str = "") -> str:
    """*label* with environment assignments, absolute paths and this host removed."""
    text = (label or "")[:MAX_LABEL_INPUT_CHARS]
    if not text.strip():
        return text
    text = _ASSIGNMENTS.sub(lambda m: m.group("lead"), text)
    text = _DANGLING.sub("", text)
    # The session's own directories, anywhere they appear (``-I<cwd>/inc``,
    # ``host:<home>/x``, ``file://<home>/x``): the cwd first, as it usually
    # lies under home.
    for root in _variants(cwd) if cwd else []:
        text = _replace_prefix(text, root, "", ".")
    for home in _variants(os.path.expanduser("~")) + _variants(os.environ.get("HOME", "")):
        text = _replace_prefix(text, home, "~/", "~")
    text = _ABS_PATH.sub(lambda m: _path_label(m.group(0)), text)
    for host in _hostnames():
        text = re.sub(rf"(?i)(?<![\w-]){re.escape(host)}(?![\w-])", HOST_PLACEHOLDER, text)
    lines = (" ".join(line.split()) for line in text.split("\n"))
    text = "\n".join(line for line in lines if line)
    text = text.strip()
    return text or FALLBACK_LABEL
