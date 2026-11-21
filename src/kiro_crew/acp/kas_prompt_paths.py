"""Which files a KAS agent's ``file://`` prompt may be read from.

On KAS, Crew reads a ``file://`` prompt itself and ships the text over the wire
(:func:`kiro_crew.acp.kas_agents.resolve_prompt`), so the path a spec names
decides which file's contents reach the engine and the model. This module is the
one answer to "may this resolved path be inlined", and it is the same answer for
a relative path and an absolute one: a relative ``file://`` is anchored to the
agents directory and resolved, symlinks included, before it is asked.
"""

from __future__ import annotations

import os
import posixpath
import re
from pathlib import Path, PurePosixPath

from kiro_crew.security import is_sensitive_path
from kiro_crew.security.argv_floor_hosts import _host_is_self
from kiro_crew.security.paths_resolver import _fold_windows_alias

#: Pseudo-filesystems whose contents are process/kernel state, not documents.
#: ``/proc/<pid>/environ`` is the sharp edge: the gateway's own environment.
_PSEUDO_FS_ROOTS = ("/proc", "/sys", "/dev")

#: ``$HOME``-relative credential stores a prompt read must not reach that
#: ``is_sensitive_path`` leaves readable to the agent's own file tools. ``gh``
#: keeps its OAuth token in ``hosts.yml`` here (``.config/gh`` on POSIX,
#: ``AppData/Roaming/GitHub CLI`` on Windows). Fenced for this reader only: a
#: prompt is shipped off the host, a file the agent's tools read is not, so this
#: list widens nothing the sandbox or the tool gate decides.
_PROMPT_ONLY_HOME_SECRETS = (".config/gh", "AppData/Roaming/GitHub CLI")

#: Environment variables that move gh's config folder, each with the leaf gh
#: appends to it, in gh's own order (``GH_CONFIG_DIR``, ``$XDG_CONFIG_HOME/gh``,
#: ``%APPDATA%\GitHub CLI``). gh uses the first one set; every one set is fenced
#: here, on every platform, since fencing one more folder only refuses more.
_GH_CONFIG_DIR_ENV = (("GH_CONFIG_DIR", ""), ("XDG_CONFIG_HOME", "gh"), ("APPDATA", "GitHub CLI"))


#: The file gh keeps its OAuth token in, inside its config folder.
_GH_TOKEN_FILE = "hosts.yml"

#: An absolute folded path: POSIX ``/...`` or a drive ``x:/...`` (UNC is ``//...``).
_ABSOLUTE_FOLDED_RE = re.compile(r"^(?:/|[a-z]:/)")

#: A drive-relative value (``C:`` or ``C:cfg``): that drive's current directory on Windows.
_DRIVE_RELATIVE_RE = re.compile(r"^[A-Za-z]:(?![/\\])")


def _fold(text: str) -> str:
    """*text* with ``\\`` read as ``/``, casefolded, without a trailing ``/``."""
    folded = text.replace("\\", "/").casefold()
    return folded.rstrip("/") or folded[:1]


#: Folded prefixes of the Windows device namespace (``\\\\?\\``, ``\\\\.\\``, ``\\??\\``).
_DEVICE_PREFIXES = ("//?/", "//./", "/??/")

#: Folded prefixes of an extended UNC path (``\\\\?\\UNC\\host\\share``), one per device prefix.
_EXTENDED_UNC_PREFIXES = tuple(prefix + "unc/" for prefix in _DEVICE_PREFIXES)


def _is_unc_or_device(folded: str) -> bool:
    """A folded path in the UNC or Windows device namespace (``//host/...``,
    ``//?/...``, ``//./...``, ``/??/...``)."""
    return folded.startswith("//") or folded.startswith("/??/")


def _names_a_network_host(p: Path, lexical: str) -> bool:
    """*p* (folded to *lexical*) is a real UNC or device path, so resolving it may
    reach the network. On POSIX a literal forward ``//srv/gh`` is a local folder
    and resolves like any other; only a ``\\`` spelling keeps the UNC reading."""
    if not _is_unc_or_device(lexical):
        return False
    return os.name == "nt" or not p.as_posix().startswith("//")


#: Hidden share names that alias a whole local volume: a drive letter or ``admin``.
_LOCAL_VOLUME_SHARE_RE = re.compile(r"^(?:[a-z]|admin)\$$")


def _aliases_a_local_file(folded: str) -> bool:
    """A folded UNC or device path that can name a LOCAL file by a second spelling.

    No anchor holds such a spelling: ``\\\\?\\UNC\\127.0.0.1\\C$\\...``,
    ``\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy1\\...``, the share
    ``\\\\localhost\\Users\\...`` or an admin share ``\\\\fileserver\\C$\\...``. So the
    device namespace, a UNC path to this machine, and a drive-letter or
    ``admin$`` share are refused outright. Any other UNC path (a mapped drive, a
    roaming profile, a hidden home share such as ``home$``) is an ordinary remote
    file and meets the anchors lexically.
    """
    if folded.startswith(_DEVICE_PREFIXES):
        return True
    if not folded.startswith("//"):
        return False
    host, _, rest = folded[2:].partition("/")
    share = rest.partition("/")[0]
    if _LOCAL_VOLUME_SHARE_RE.match(share):
        return True
    # A bracketed IPv6 literal (``[::1]``) is not an address ``ipaddress`` parses.
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return _host_is_self(host, dns_fallback=False)


def _is_root(folded: str) -> bool:
    return folded == "/" or (len(folded) == 2 and folded[1] == ":")


def _gh_config_dirs() -> tuple[list[Path], list[str]]:
    """Where gh may keep ``hosts.yml``, read from the env at call time.

    Returns the absolute folders (and token files) to fence, and the folded
    tails of RELATIVE env values. gh joins a relative value to its own cwd,
    which the gateway cannot know, so such a value fences every folder whose
    path ends in that tail (``GH_CONFIG_DIR=cfg/gh`` refuses ``*/cfg/gh/**``),
    and a tail of nothing (``.``) fences every ``hosts.yml``. A value that is a
    filesystem root (``GH_CONFIG_DIR=/``) fences the token file there, not the
    whole drive. A relative value also fences that folder under the gateway's
    own cwd, resolved, so a link there to another folder is fenced at its target.
    A ``\\\\?\\C:\\`` value is folded to its drive spelling first, as the candidate is.
    """
    dirs = [Path.home() / rel for rel in _PROMPT_ONLY_HOME_SECRETS]
    tails: list[str] = []
    for var, leaf in _GH_CONFIG_DIR_ENV:
        # As gh does: only "" is unset, and the value is kept as written, so a
        # stray space fences the folder gh really reads, not a trimmed one.
        value = _fold_windows_alias(os.environ.get(var, ""))
        if not value:
            continue
        if _DRIVE_RELATIVE_RE.match(value):
            # As gh does: on Windows ``abspath`` joins it to that drive's own cwd.
            try:
                value = os.path.abspath(value)
            except (OSError, ValueError):
                pass
        # One trailing separator, so ``C:\`` stays the drive root and not ``C:``.
        value = value.rstrip("/\\") + "/"
        folded = _fold(value)
        absolute = _ABSOLUTE_FOLDED_RE.match(value.replace("\\", "/").casefold())
        if not (absolute or _is_unc_or_device(folded)):
            parts = PurePosixPath(posixpath.normpath(_fold(value + leaf))).parts
            kept = [p for p in parts if p not in (".", "..")]
            tails.append(posixpath.join(*kept) if kept else "")
            try:
                here = Path.cwd() / value
            except OSError:
                continue
            # A value naming the cwd itself (``.``) fences the token file there, as a root does.
            dirs.append(here / leaf if leaf else here if kept else here / _GH_TOKEN_FILE)
        elif not leaf and _is_root(folded):
            dirs.append(Path(value) / _GH_TOKEN_FILE)
        else:
            dirs.append(Path(value) / leaf if leaf else Path(value))
    return dirs, tails


def _under_tail(folded: str, tail: str) -> bool:
    """*folded* lies in a folder whose path ends in *tail* (see :func:`_gh_config_dirs`)."""
    if not tail:
        return folded.rpartition("/")[2] == _GH_TOKEN_FILE
    return folded.endswith("/" + tail) or ("/" + tail + "/") in folded


#: Data-home leaves the prompt read refuses, also outside ``is_sensitive_path``.
#: The receipt-only HMAC key is ``apps.install_receipt._SECRET_FILE``; the
#: per-token identity mappings are ``session_token_<sha256>.sig``
#: (``session_token_sig._sig_path``). The receipt key sits directly in the data home;
#: every file in the ``session-tokens`` directory under it is refused too.
_PROMPT_ONLY_DATA_HOME_FILES = ("app_receipt_secret",)
_SESSION_TOKEN_SIG_PREFIX = "session_token_"
_SESSION_TOKEN_SIG_SUFFIX = ".sig"


def _under(posix: str, root: str) -> bool:
    return posix == root or posix.startswith(root + "/")


def _anchors(paths: list[Path]) -> set[str]:
    """Each of *paths* both as written and resolved, folded by :func:`_fold`.

    The candidate is resolved, so an anchor must be too: a symlinked
    ``~/.config/gh`` (a dotfile manager's) or data home would otherwise compare a
    canonical target against a link's own name. A UNC or device anchor (a
    redirected ``%APPDATA%``) is matched as written and never resolved, since
    resolving it is a network round trip; a POSIX ``//srv/gh`` is local and is
    resolved (:func:`_names_a_network_host`). The lexical form is kept for an anchor
    whose resolution fails, and so is its lexically normalized form, so a ``..``
    in an anchor that is never resolved meets the canonical candidate. A
    ``\\\\?\\C:\\`` spelling is folded to its drive path, as the candidate is. An
    extended UNC anchor (``\\\\?\\UNC\\host\\share``) is also kept in its plain UNC
    form (``\\\\host\\share``), the spelling a candidate on that share arrives in.
    """
    out: set[str] = set()
    for p in paths:
        lexical = _fold(_fold_windows_alias(p.as_posix()))
        forms = [lexical]
        for prefix in _EXTENDED_UNC_PREFIXES:
            if lexical.startswith(prefix):
                forms.append("//" + lexical[len(prefix) :])
        for form in forms:
            out.add(form)
            out.add(posixpath.normpath(form))
        if _names_a_network_host(p, lexical):
            continue
        try:
            out.add(_fold(_fold_windows_alias(p.resolve().as_posix())))
        except (OSError, RuntimeError):
            pass
    return out


def _data_homes() -> set[str]:
    """The data home as configured and at its default."""
    from kiro_crew.config.paths import CONFIG_DIR_LEAF, KIRO_BASE_DIR_NAME, config_dir

    return _anchors([Path.home() / KIRO_BASE_DIR_NAME / CONFIG_DIR_LEAF, config_dir()])


def _is_data_home_secret(folded: str) -> bool:
    from kiro_crew.session_token_sig import SESSION_TOKEN_DIR

    parent, _, name = folded.rpartition("/")
    homes = _data_homes()
    records_home, _, records_leaf = parent.rpartition("/")
    if records_leaf == SESSION_TOKEN_DIR and records_home in homes:
        return True
    if parent not in homes:
        return False
    if name in _PROMPT_ONLY_DATA_HOME_FILES:
        return True
    return name.startswith(_SESSION_TOKEN_SIG_PREFIX) and name.endswith(_SESSION_TOKEN_SIG_SUFFIX)


def is_unsafe_prompt_path(path: Path) -> bool:
    """True if the RESOLVED *path* must not be read and inlined into a KAS prompt.

    *path* must already be resolved (``Path.resolve``), so a symlink is judged by
    its target. Blocks the credential and governance locations
    ``is_sensitive_path`` knows (which also re-resolves), the pseudo-filesystems,
    gh's config folders (:func:`_gh_config_dirs`), and the data-home receipt key
    and session-token mappings. Paths compare casefolded with ``\\`` read as
    ``/``, so a Windows spelling meets the same anchors. A ``\\\\?\\C:\\`` or
    ``::$DATA`` alias is folded to the plain drive path first; a UNC or device
    path that can alias a local file is refused (:func:`_aliases_a_local_file`).
    """
    if is_sensitive_path(str(path)):
        return True
    folded = _fold(_fold_windows_alias(path.as_posix()))
    if _aliases_a_local_file(folded):
        return True
    dirs, tails = _gh_config_dirs()
    roots = [Path(r) for r in _PSEUDO_FS_ROOTS] + dirs
    for anchor in _anchors(roots):
        if _is_root(anchor):
            # A gh folder that normalizes or resolves to a root fences only its token file.
            if folded == posixpath.join(anchor, _GH_TOKEN_FILE):
                return True
        elif _under(folded, anchor):
            return True
    if any(_under_tail(folded, tail) for tail in tails):
        return True
    return _is_data_home_secret(folded)
