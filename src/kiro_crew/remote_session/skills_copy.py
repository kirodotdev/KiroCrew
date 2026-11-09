"""Gateway side of a remote session's skill copy: the agent's skills, runnable in its workspace.

A remote session's agent reads its skills here, on the gateway (the skills index,
``skill_search``, KAS's own skill tool), but its commands run in the workspace,
where the skill's ``scripts/`` never were. At every runtime start (a new session,
a resume, a restart) this copies the skills that agent may see, and only those,
into the workspace, and every skill path the agent is shown names that copy.

What is copied, per skill: its directory's regular files (``SKILL.md``,
``scripts/``, ``references/``, assets), read without following links. Left out,
each reported: every symlink and hard link; every hidden file or directory (``.env``, ``.git``,
provenance markers); a directory holding its own ``SKILL.md`` (another skill,
copied only when it is in scope itself); credential-named files (private keys,
``credentials``); a file whose text the shared credential scanner matches; a file
over :data:`MAX_FILE_BYTES`; and every skill past :data:`MAX_TOTAL_BYTES` or
:data:`MAX_FILES`. Project skills are not copied: they live in the repository the
workspace checks out.

The bytes travel on the stdin of one guest command over the session's existing
shell (``coder ssh``), as the worker files do; nothing listens. In the guest
``skills_install.py`` writes a content-addressed, read-only copy at
``~/.kirocrew-worker/skills/<digest>/<skill key>/``; a digest already there is
reused without a transfer.

Refreshed at each runtime start, not mid-turn: the copy a running command uses
never changes under it. shortcut: a skill edited mid-session reaches the
workspace at the next start (resume, reset or restart); refresh on change when a
per-turn check can swap copies between turns.
"""

from __future__ import annotations

import codecs
import functools
import hashlib
import json
import logging
import os
import posixpath
import re
import shlex
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.bounded_read import bounded_read
from kiro_crew.remote_session import skills_install
from kiro_crew.session_environment import _REMOTE_WORKSPACES_MAX, remote_workspace_for

logger = logging.getLogger(__name__)

#: Where copies live in the workspace, as documented and as the agent is told.
GUEST_SKILLS_DIR = "~/.kirocrew-worker/skills"
MAX_FILE_BYTES = 1 << 20
#: The guest's own bounds, which it checks again: one definition for both sides.
MAX_TOTAL_BYTES = skills_install.MAX_TOTAL_BYTES
MAX_FILES = skills_install.MAX_FILES
#: Names that hold credentials whatever their content.
_SECRET_NAMES = frozenset(
    {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", "secring.gpg"}
)
_SECRET_SUFFIXES = (".pem", ".key", ".p8", ".p12", ".pfx", ".jks", ".keystore")
_KEY_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@+-]*")
#: Left-out paths kept as examples; :attr:`CopyPlan.skipped_total` counts them all.
_MAX_SKIPPED = 200
#: How long the scope read waits for a first-run skill catalog to finish building.
CATALOG_WAIT_SECONDS = 60.0


@dataclass(frozen=True)
class Skipped:
    """A path left out of the copy, and why (shown to the person and the agent)."""

    path: str
    reason: str

    def __post_init__(self) -> None:
        # Every refusal is shown (the agent's context, the card, the log), and a
        # path may be refused because a segment of it is a credential: the one
        # place every writer passes through masks each such segment, with the
        # same scan that decides what is copied (so the two never disagree).
        # Both separators: a Windows gateway names its paths with ``\``.
        parts = re.split(r"([\\/])", self.path)
        masked = "".join(p if p in ("/", "\\") else _mask_segment(p) for p in parts)
        object.__setattr__(self, "path", masked)


@dataclass
class CopyPlan:
    """What one copy carries: skills by key, their files, and what was left out."""

    #: key -> every spelling of the skill's local directory the agent may be shown.
    skills: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: (path under the copy, mode, bytes), in order.
    files: list[tuple[str, int, bytes]] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    #: Every path left out, including those past the :data:`_MAX_SKIPPED` examples.
    skipped_total: int = 0
    #: Whole skills not copied because they sit outside Crew's skill folders
    #: (the first :data:`_MAX_SKIPPED`; ``outside_total`` counts them all).
    outside: list[Skipped] = field(default_factory=list)
    outside_total: int = 0
    #: Whole skills not copied because a file in them looks like a secret (the
    #: first :data:`_MAX_SKIPPED`; ``dropped_total`` counts them all).
    dropped: list[Skipped] = field(default_factory=list)
    dropped_total: int = 0
    #: Set by :func:`plan_copy` once the files are final (off the event loop).
    digest: str = ""

    def seal(self) -> None:
        h = hashlib.sha256()
        for rel, mode, data in self.files:
            h.update(f"{rel}\0{mode & 0o111}\0{len(data)}\0".encode("utf-8", "surrogateescape"))
            h.update(data)
        self.digest = h.hexdigest()[:32]

    def payload(self) -> bytes:
        header = json.dumps([[rel, len(data), mode] for rel, mode, data in self.files])
        return header.encode("utf-8") + b"\n" + b"".join(data for _r, _m, data in self.files)

    def skip(self, path: str, reason: str) -> None:
        self.skipped_total += 1
        if len(self.skipped) < _MAX_SKIPPED:
            self.skipped.append(Skipped(path, reason))

    def leave_outside(self, path: str) -> None:
        self.outside_total += 1
        if len(self.outside) < _MAX_SKIPPED:
            self.outside.append(Skipped(path, _OUTSIDE))

    def drop(self, path: str, reason: str) -> None:
        self.dropped_total += 1
        if len(self.dropped) < _MAX_SKIPPED:
            self.dropped.append(Skipped(path, reason))


@dataclass(frozen=True)
class SkillCopy:
    """A copy in the workspace, as the gateway records it for one session."""

    root: str
    #: key -> local directory spellings (see :attr:`CopyPlan.skills`).
    skills: dict[str, tuple[str, ...]]
    skipped: tuple[Skipped, ...] = ()
    #: Every left-out path; ``skipped`` holds the first examples.
    skipped_total: int = 0
    python: str = ""
    #: (path under the copy, reason) for copied ``.py`` files the guest cannot compile.
    incompatible: tuple[tuple[str, str], ...] = ()
    #: How many there are in all; ``incompatible`` holds the first examples.
    incompatible_total: int = 0
    #: Skills not copied because they sit outside Crew's skill folders (examples;
    #: ``outside_total`` counts them all).
    outside: tuple[Skipped, ...] = ()
    outside_total: int = 0
    #: Skills not copied because a file in them looks like a secret (examples;
    #: ``dropped_total`` counts them all).
    dropped: tuple[Skipped, ...] = ()
    dropped_total: int = 0

    @property
    def outside_count(self) -> int:
        return max(self.outside_total, len(self.outside))

    @property
    def dropped_count(self) -> int:
        return max(self.dropped_total, len(self.dropped))

    @property
    def incompatible_count(self) -> int:
        """Every copied script the guest cannot compile, past the examples too."""
        return max(self.incompatible_total, len(self.incompatible))

    @property
    def python_older(self) -> bool:
        """Whether the workspace's python3 is older than the gateway's own Python."""
        try:
            guest = tuple(int(part) for part in self.python.split(".")[:2])
        except ValueError:
            return False
        return len(guest) == 2 and guest < sys.version_info[:2]

    def guest_dir(self, key: str) -> str | None:
        return f"{self.root}/{key}" if key in self.skills else None

    def rewrite(self, text: str) -> str:
        """``text`` with each copied skill's local directory replaced by its copy."""
        pairs = sorted(
            (
                (local, f"{self.root}/{key}")
                for key, spellings in self.skills.items()
                for local in spellings
            ),
            key=lambda pair: -len(pair[0]),
        )
        for local, guest in pairs:
            # Both separators: a Windows gateway renders its paths with ``\``.
            for sep in dict.fromkeys(("/", os.sep)):
                text = text.replace(local + sep, guest + "/")
        return text


def _gateway_python() -> str:
    return "%d.%d" % sys.version_info[:2]


def _secret_name(name: str) -> bool:
    lowered = name.lower()
    return lowered in _SECRET_NAMES or lowered.endswith(_SECRET_SUFFIXES)


def _read_regular(
    name: str, limit: int, dir_fd: int | None = None, ident: list[Any] | None = None
) -> tuple[bytes | None, int, str]:
    """``(bytes, mode, "")`` of a regular file opened without following a link, or
    ``(None, 0, reason)``. *name* is opened relative to *dir_fd* when one is given.
    *ident*, when given, receives the descriptor's ``(st_dev, st_ino)``."""
    try:
        # Non-blocking, so a FIFO swapped in after the walk's type check cannot hang
        # the open; the descriptor check below refuses it.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError:
        return None, 0, "unreadable or a link"
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            return None, 0, "not a regular file"
        # A hard link can name a file outside the skill, as the fenced skill reader
        # (``hooks.safe_read_prefix``) also refuses.
        if info.st_nlink > 1:
            return None, 0, "hard link"
        if info.st_size > limit:
            return None, 0, f"over the {limit // 1024} KiB file cap"
        data = bounded_read(handle, limit + 1)
    if ident is not None:
        ident.append((info.st_dev, info.st_ino))
    if len(data) > limit:
        return None, 0, f"over the {limit // 1024} KiB file cap"
    return data, info.st_mode, ""


def _texts(data: bytes) -> list[str]:
    """*data* read as every text a scan must see: UTF-8 (binary included), and
    UTF-16/UTF-32 when marked by a BOM or, unmarked, when it holds NUL bytes (an
    ASCII label in UTF-16 is NUL-interleaved and invisible to a UTF-8 read)."""
    texts = [data.decode("utf-8", errors="replace")]
    for bom, codec in (
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if data.startswith(bom):
            return texts + [data.decode(codec, errors="replace")]
    if b"\x00" in data:
        codecs_ = ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")
        texts += [data.decode(c, errors="replace") for c in codecs_]
    return texts


def _holds_credential(data: bytes) -> bool:
    from kiro_crew.security import _contains_fixed_credential, iter_credential_matches
    from kiro_crew.security.redaction_structured import SECRET, scan

    # Binary and non-UTF-8 files are scanned too: a token in a pickle or a SQLite
    # cache is still a token. Base64-encoded ones (a kubeconfig's client-key-data)
    # count as well, and so does every format the structured detectors know
    # (vendor and checksummed tokens, key-ID pairs, PEM/PGP armour, JOSE). Their
    # raw hits: nothing is suppressed inside a ``[REDACTED...]`` placeholder, which
    # here would let a key wrapped in one through.
    return any(
        next(iter(iter_credential_matches(text)), None) is not None
        or bool(_contains_fixed_credential(text))
        or any(h.tier == SECRET for h in scan(text).hits)
        for text in _texts(data)
    )


def _name_holds_credential(name: str) -> bool:
    """Whether a file or folder name holds a credential, also one glued to other
    words (``key_<token>.py``, ``<token>-cache``): a vendor rule refuses a match
    next to ``_`` or ``-``, so each run of the name's ``_``/``-`` pieces is scanned
    on its own too. A name is short; its pieces are few."""
    # Every window between separators (``_ - . ~`` and spaces), so a token with
    # its own dots (``hvs.<...>``) is seen without the extension glued on.
    seps = list(re.finditer(r"[_.~ -]", name))
    if len(seps) >= _MAX_NAME_SEPARATORS:
        # Too many windows to scan in bounded time: fail closed, as if it held one.
        return True
    starts = [0, *(m.end() for m in seps)]
    ends = [*(m.start() for m in seps), len(name)]
    texts = [name[i:j] for i in starts for j in ends if i < j]
    return any(_holds_credential(f" {text} ".encode("utf-8", "surrogateescape")) for text in texts)


#: A name with more separators than this is not scanned window by window; it is
#: refused instead (no skill on this host's catalog comes near it).
_MAX_NAME_SEPARATORS = 24


def _mask_segment(segment: str) -> str:
    """A path segment, or the credential tag when it holds one."""
    from kiro_crew.security.redaction import REDACTED_CREDENTIAL_TAG

    return REDACTED_CREDENTIAL_TAG if _name_holds_credential(segment) else segment


class _OverBudget(Exception):
    """A skill's files would pass what is left of the copy's byte or file budget."""


class _HoldsHostState(Exception):
    """A skill's tree holds a path the copy invariant refuses (rule (c))."""


#: Why a skill holding a secret is not in the copy (shown as is).
_CREDENTIAL = "skipped: a file looks like it holds a credential"
_CONFIG_SECRET = "skipped: a config file looks like it holds a password"
_CONFIG_SUFFIXES = (".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env", ".properties")
_CONFIG_NAMES = frozenset({"netrc", "_netrc", "env"})
#: Backup and copy suffixes a config document keeps its format under.
_BACKUP_SUFFIX = re.compile(r"(?:~|\.(?:bak|old|orig|save|backup|tmp|swp|dist|copy|\d+))$")


def _config_file(name: str) -> bool:
    """A config-format document (``.netrc``-style names included), also under a
    backup suffix (``settings.json.bak``, ``config.yaml~``)."""
    lower = name.lower()
    while (bare := _BACKUP_SUFFIX.sub("", lower)) != lower:
        lower = bare
    return lower.endswith(_CONFIG_SUFFIXES) or lower in _CONFIG_NAMES


#: Config keys whose value is a password or key (compared lower-case, without
#: ``-``, ``_``, ``.``): the label scan for config documents.
_SECRET_LABELS = frozenset(
    {
        "password", "passwd", "pwd", "passphrase", "secret", "clientsecret", "apikey",
        "apisecret", "apitoken", "accesstoken", "authtoken", "refreshtoken",
        "sessiontoken", "securitytoken", "bearertoken", "token", "privatekey",
        "secretkey", "secretaccesskey", "accountkey", "sharedaccesskey", "authorization",
        "xapikey", "appkey", "authkey", "signingkey", "encryptionkey", "webhooksecret",
        "webhooktoken", "credential", "credentials",
    }
)  # fmt: skip
#: A config pair anywhere on a line: ``key = value``, ``key: value``,
#: ``"key": "value"`` (one-line JSON included); the value quoted or bare. Only
#: horizontal space around the separator, so a YAML key with a nested block
#: (``password:`` then ``  type: string``) assigns nothing; a bare value never
#: opens an object or list (``{"token": {"type": ...}}`` is a schema).
_CONFIG_PAIR = re.compile(
    r"""(?<![A-Za-z0-9_.-])["']?([A-Za-z0-9_.-]{1,64})["']?[ \t]*[:=][ \t]*"""
    r"""(?:"([^"\n]*)"|'([^'\n]*)'|([^\s,}\]#;{\[][^\s,}\]#;]*))"""
)
#: Values that name no secret: placeholders, references, booleans, empties.
_NOT_A_SECRET = re.compile(
    r"""^(?:|null|none|true|false|required|optional|changeme|placeholder|example|redacted"""
    r"""|\$\{[^}]*\}?|\{\{.*|\$[A-Z_][A-Z0-9_]*|<[^>]*>|%[A-Za-z_]\w*%|\*+|x+|\.+|your[\w.-]*)$""",
    re.IGNORECASE,
)
#: The shortest value counted as a secret (shorter ones are flags and names).
_MIN_SECRET_VALUE = 4


def _labelled_secret(data: bytes) -> bool:
    """Whether a config document assigns a value to a password- or key-named field.

    shortcut: a pattern read of ``key: value`` pairs, not a parser for each format;
    a value split across lines is not seen. Upgrade to a shared label detector
    once ``security`` offers one."""
    for text in _texts(data):
        for match in _CONFIG_PAIR.finditer(text):
            key = re.sub(r"[-_.]", "", match.group(1).lower())
            value = next((g for g in match.groups()[1:] if g is not None), "").strip()
            if (
                key in _SECRET_LABELS
                and len(value) >= _MIN_SECRET_VALUE
                and not value.endswith(":")
                and not _NOT_A_SECRET.match(value)
            ):
                return True
    return False


class _HoldsSecret(Exception):
    """A skill holds a file that looks like a secret: the whole skill is dropped."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Outside(Exception):
    """The skill directory opened is not inside the Crew skill folder it was
    selected under (rule (a), checked on the descriptor)."""


class _NotAdmitted(Exception):
    """The ``SKILL.md`` this walk read is not the one the scope check admitted."""


#: Kiro-home folders that hold credentials (``settings/mcp.json`` headers and env,
#: agent specs' MCP env).
_KIRO_CREDENTIAL_DIRS = ("settings", "agents")


def _host_homes() -> tuple[set[str], set[str], set[str]]:
    """Every data home this host may hold (active, default, pre-move legacy), every
    Kiro home (``~/.kiro``, a ``KIRO_HOME`` override), and every credential store
    (the home directory's own hidden folders, such as ``~/.ssh``, ``~/.aws`` and
    ``~/.config``, plus any XDG base directory moved elsewhere), each resolved.

    Raises :class:`OSError` when the home directory cannot be listed; the caller
    then refuses, since the stores are unknown."""
    from kiro_crew.config.paths import (
        CONFIG_DIR_LEAF,
        KIRO_BASE_DIR_NAME,
        config_dir,
        data_home,
        kiro_home,
        legacy_home,
    )

    home = Path.home()
    data = (config_dir(), data_home(), home / KIRO_BASE_DIR_NAME / CONFIG_DIR_LEAF, legacy_home())
    kiro = (kiro_home(), home / KIRO_BASE_DIR_NAME)
    xdg = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
    stores = [os.environ[name] for name in xdg if os.environ.get(name)]
    with os.scandir(home) as scan:
        stores += [entry.path for entry in scan if entry.name.startswith(".") and entry.is_dir()]
    return (
        {os.path.realpath(p) for p in data},
        {os.path.realpath(p) for p in kiro},
        {os.path.realpath(p) for p in stores},
    )


#: How a host-state anchor judges a skill root strictly inside it, strictest last
#: (when two anchors resolve to the same folder the stricter rule wins).
_STORE, _APPS, _DATA, _REFUSE = range(4)


#: THE COPY INVARIANT. A file reaches the workspace only if all hold, else the
#: whole skill is dropped (fail closed):
#:
#: (a) an ALLOWLIST: its skill root resolves strictly inside one of Crew's own
#:     skill folders (``_crew_skill_folders``: the Crew skills dir,
#:     ``~/.kiro/skills``, and an app's ``<app>/skills/<name>`` under the
#:     installed package's built-in apps or the data home's installed apps,
#:     nothing else in an app folder). A skill an agent spec maps from anywhere else still
#:     loads locally but is not copied off this host (``_OUTSIDE``):
#:     ``_inside_crew_folder``, at selection and again at copy. Project skills
#:     are never copied (the workspace checks the repository out);
#: (b) no host-state root sits at, above or below that skill root: every data
#:     home (active, default, legacy) outside its skill folders, a Kiro home's
#:     ``settings``/``agents``, every credential store (each hidden folder of the
#:     home directory, an XDG base directory) outside a ``skills`` folder, the
#:     home directory and the temp dir, and any path the sensitive-path fence
#:     refuses. Each anchor is resolved on its own (an aliased ``settings`` or
#:     ``apps`` keeps its rule) and compared casefolded (a case-insensitive disk):
#:     ``_holds_host_state`` plus the root fence in ``_skill_files``;
#: (c) the file itself passes ``is_sensitive_resolved_path`` and
#:     ``is_unsafe_prompt_path``, and no file looks like a secret (the value
#:     scan on every file, the label scan on config documents): else the whole
#:     skill is dropped and named (``_skill_files``, ``_HoldsSecret``).
#:
#: (b) and (c) are defence in depth below the allowlist. Every path the copy
#: reads comes from one of: the catalog rows (``allowed_skill_rows``:
#: ``dir``/``path``), the row's bound ``real_dir`` (``KasSkillScope.bind``),
#: and the pinned walk below it (``_skill_files``). Each is held to the rules.

#: Why a skill outside Crew's skill folders is not in the copy (shown as is).
_OUTSIDE = "outside Crew's skill folders, not copied to remote workspaces"


def _crew_skill_folders(loader_dir: str | Path) -> tuple[tuple[str, bool], ...]:
    """(a): the only folders a copied skill may sit in, resolved, each with whether
    it is an app root (only its ``<app>/skills/<name>`` folders count): the Crew
    skills dir and ``~/.kiro/skills``; the installed package's built-in apps; the
    data home's installed apps. Apps link their skills into the skills dir, so the
    resolved roots of those links land in an app root."""
    from kiro_crew import skills_discovery
    from kiro_crew.config.paths import KIRO_BASE_DIR_NAME, config_dir, data_home, kiro_home

    plain = (loader_dir, kiro_home() / "skills", Path.home() / KIRO_BASE_DIR_NAME / "skills")
    apps = (
        Path(skills_discovery._INSTALLED_PACKAGE_DIR) / "apps" / "builtins",
        config_dir() / "apps",
        data_home() / "apps",
    )
    folders = []
    for path, in_apps in [(p, False) for p in plain] + [(p, True) for p in apps]:
        # The home above the folder is resolved; the folder itself must be a real
        # directory. A skills folder that is a link would make wherever it points
        # (a credential store's real home, say) an allowed folder: refused.
        slot = os.path.join(os.path.realpath(Path(path).parent), Path(path).name)
        if not os.path.islink(slot):
            folders.append((slot, in_apps))
    return tuple(dict.fromkeys(folders))


def _inside_crew_folder(real_dir: str, folder: str, apps: bool = False) -> bool:
    """(a): the resolved *real_dir* sits strictly inside the resolved Crew skill
    folder *folder*; in an app root, strictly inside one app's ``skills`` folder
    (``<app>/skills/<name>``), so nothing else in an app folder ever counts.

    Lexical: both sides were resolved once, at selection, and no link is ever
    resolved again (an app ``skills`` linked out of its app folder resolved
    elsewhere then, and is refused)."""
    if not folder:
        return False
    base = folder.rstrip(os.sep)
    if not real_dir.startswith(base + os.sep):
        return False
    parts = real_dir[len(base) + 1 :].split(os.sep)
    return not apps or (len(parts) >= 3 and parts[1] == "skills")


def _holds_host_state(real_root: str) -> bool:
    """Whether a skill rooted at *real_root* would sweep up this host's own state.

    Data homes hold secrets no file check can name (the config documents'
    plaintext tokens and their endorsed copies, the receipt key's staging file),
    a Kiro home's ``settings`` and ``agents`` hold MCP API keys, and the temp
    directory's top level holds the cron secret files, and the home directory's
    hidden folders hold the credential stores (``.ssh``, ``.aws``,
    ``.config/pypoetry``...). So a root at or above any of those is refused, a
    root in a Kiro home's credential folders is refused, inside a data home only
    ``<data home>/skills/...`` and ``<data home>/apps/<app>/skills/...`` are
    allowed, and inside a store or Kiro home only a root below a ``skills``
    folder. The rest is refused whole, by construction rather than by naming
    each secret.
    """
    import tempfile

    try:
        data_homes, kiro_homes, stores = _host_homes()
    except OSError:
        return True  # the credential stores are unknown: fail closed
    # Every anchor is resolved on its own, so an aliased ``settings`` or ``apps``
    # keeps its rule at its real target, and compared casefolded, so a home spelt
    # in another case on a case-insensitive disk still matches. Folding can only
    # match more, which refuses more.
    anchors: list[tuple[str, int]] = [(p, _STORE) for p in stores | kiro_homes]
    for home in data_homes:
        anchors += [(home, _DATA), (os.path.realpath(os.path.join(home, "apps")), _APPS)]
    for kiro in kiro_homes:
        # MCP settings and agent specs carry API keys.
        anchors += [
            (os.path.realpath(os.path.join(kiro, leaf)), _REFUSE) for leaf in _KIRO_CREDENTIAL_DIRS
        ]
    above_only = [os.path.realpath(Path.home()), os.path.realpath(tempfile.gettempdir())]

    def fold(path: str) -> str:
        return path.casefold().rstrip(os.sep)

    root = fold(os.path.realpath(real_root))
    for held in [fold(a) for a, _rule in anchors] + [fold(a) for a in above_only]:
        if held == root or held.startswith(root + os.sep):
            return True  # the root sweeps the whole anchor in
    # Strictly inside: the innermost anchor decides (a data home may be set inside
    # another one, or inside ``~/.kiro``), the stricter rule on a tie.
    holding = [
        (len(fold(a)), rule, fold(a)) for a, rule in anchors if root.startswith(fold(a) + os.sep)
    ]
    if not holding:
        return False
    if any(rule == _REFUSE for _len, rule, _a in holding):
        return True  # a credential folder refuses whatever is set up inside it
    _len, rule, anchor = max(holding)
    parts = root[len(anchor) + 1 :].split(os.sep)
    if rule == _DATA:
        in_skills = len(parts) >= 2 and parts[0] == "skills"
        return not (in_skills or (len(parts) >= 4 and parts[0] == "apps" and parts[2] == "skills"))
    if rule == _APPS:
        return not (len(parts) >= 3 and parts[1] == "skills")
    if rule == _STORE:
        # Only below a ``skills`` folder (``~/.claude/skills/<name>``, an installed
        # package's ``.../skills/<name>``): the walk then never reaches a sibling.
        return "skills" not in parts[:-1]
    return True


def _skill_files(
    local_dir: str,
    plan: CopyPlan,
    bytes_left: int,
    files_left: int,
    admit: Callable[[str, tuple[int, int], bytes], bool] | None = None,
    within: Callable[[str], bool] | None = None,
) -> list[tuple[str, int, bytes]] | None:
    """The copyable files under ``local_dir`` (relative paths), or None if unreadable.

    *within*, when given, is asked about the opened root's real path; a refusal
    raises :class:`_Outside` before anything is read.

    *admit*, when given, is asked about the skill's own ``SKILL.md`` with the
    descriptor identity and the bytes this walk read; a refusal leaves the whole
    skill out, so the decision rests on the file actually copied.

    Walked by descriptor (``pinned_fs``): the root is opened once with its ancestors
    pinned, and every directory and file below it is opened relative to its
    parent's descriptor with ``O_NOFOLLOW``, so a component swapped for a link
    mid-walk is refused rather than followed out of the skill.

    Raises :class:`_OverBudget` as soon as the skill passes ``bytes_left`` or
    ``files_left``, before reading further, so a large tree costs no full walk.
    """
    from kiro_crew import pinned_fs
    from kiro_crew.agent_sdk.kas_skill_view import kas_unsafe_to_ship
    from kiro_crew.security.paths import is_sensitive_resolved_path

    out: list[tuple[str, int, bytes]] = []
    budget = [bytes_left, files_left]

    def walk(dir_fd: int, where: str, rel: str) -> None:
        try:
            entries = sorted(os.scandir(dir_fd), key=lambda e: e.name)
        except OSError:
            plan.skip(where, "unreadable directory")
            return
        for entry in entries:
            path = os.path.join(where, entry.name)
            child_rel = f"{rel}/{entry.name}" if rel else entry.name
            if entry.name.startswith("."):
                plan.skip(path, "hidden")
            elif is_sensitive_resolved_path(path) or kas_unsafe_to_ship(path):
                # Invariant (c): the fence every file surface applies, plus the one
                # for a read shipped off this host (the receipt key, ``~/.config/gh``).
                # A skill holding such a path is host state, so the whole skill is
                # dropped. *path* is canonical: the root is resolved and no link
                # below it is ever followed.
                raise _HoldsHostState(path)
            elif "\\" in entry.name or any(ord(c) < 32 for c in entry.name):
                # The guest refuses a backslash (``skills_install.safe_rel``), and a
                # control character could carry a line into the agent's context.
                plan.skip(path, "a name the workspace copy cannot hold")
            elif _name_holds_credential(entry.name):
                # A name ships too (the payload header, the installed tree).
                raise _HoldsSecret(_CREDENTIAL)
            elif entry.is_symlink():
                plan.skip(path, "symlink")
            elif entry.is_dir(follow_symlinks=False):
                try:
                    child = os.open(entry.name, pinned_fs.dir_flags(), dir_fd=dir_fd)
                except OSError:
                    plan.skip(path, "unreadable directory or a link")
                    continue
                try:
                    # Its children are named from where the held descriptor really
                    # is, not the name it was opened by, so the fence below judges
                    # the file actually read even if a folder was renamed in.
                    real_child = pinned_fs.fd_real_path(child)
                    try:
                        os.stat("SKILL.md", dir_fd=child, follow_symlinks=False)
                    except FileNotFoundError:
                        if real_child is None:
                            plan.skip(path, "unreadable directory")
                        else:
                            walk(child, real_child, child_rel)
                    except OSError:
                        plan.skip(path, "unreadable directory")
                    else:
                        plan.skip(path, "another skill")
                finally:
                    os.close(child)
            elif not entry.is_file(follow_symlinks=False):
                plan.skip(path, "not a regular file")
            elif _secret_name(entry.name):
                plan.skip(path, "credential file name")
            else:
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    size = 0
                # A file over its own cap is refused below and costs no bytes.
                budget[0] -= size if size <= MAX_FILE_BYTES else 0
                budget[1] -= 1
                if budget[0] < 0 or budget[1] < 0:
                    raise _OverBudget
                ident: list[Any] = []
                data, mode, reason = _read_regular(
                    entry.name, MAX_FILE_BYTES, dir_fd=dir_fd, ident=ident
                )
                if data is None:
                    plan.skip(path, reason)
                elif (
                    admit is not None
                    and child_rel == "SKILL.md"
                    and not (ident and admit(path, ident[0], data))
                ):
                    raise _NotAdmitted
                elif _holds_credential(data):
                    raise _HoldsSecret(_CREDENTIAL)  # fail closed: the whole skill
                elif _config_file(entry.name) and _labelled_secret(data):
                    # The boundary: skill folders are user-authored content. The copy
                    # refuses host state and recognisable secrets (the value scan
                    # above, on every file), and a config document whose labels say
                    # it holds a password; it does not try to judge every word in a
                    # user's own skill, so prose and code are not dropped on a
                    # label alone.
                    raise _HoldsSecret(_CONFIG_SECRET)
                else:
                    out.append((child_rel, mode, data))

    if not pinned_fs.supports_pinned_tree_walk():
        # Fail closed: a by-name walk is the hole the descriptors close.
        plan.skip(local_dir, "this platform cannot read a skill without following links")
        return None
    try:
        root = pinned_fs.open_dir_pinned(local_dir, what="skill directory")
    except (OSError, pinned_fs.PinnedPathRefusal):
        return None
    try:
        real_root = pinned_fs.fd_real_path(root)
        if real_root is not None and within is not None and not within(real_root):
            # Invariant (a) on the descriptor actually opened: the root swapped
            # for one outside the frozen allowlist since selection is not read.
            raise _Outside
        if (
            real_root is None
            or is_sensitive_resolved_path(real_root)
            or kas_unsafe_to_ship(real_root)
            or _holds_host_state(real_root)
        ):
            plan.skip(local_dir, "sensitive path or unreadable directory")
            return None
        walk(root, real_root, "")
    finally:
        os.close(root)
    return out if any(rel == "SKILL.md" for rel, _m, _d in out) else None


def plan_copy(rows: list[dict[str, Any]]) -> CopyPlan:
    """The copy of ``rows`` (``scoped_skills`` rows: ``key`` and ``dir``)."""
    plan = CopyPlan()
    total = 0
    for row in rows:
        key = str(row.get("key") or "")
        local = str(row.get("dir") or "")
        if key in plan.skills:
            continue
        # A key is a POSIX path in the guest, whatever this gateway's OS.
        if not local or not all(_KEY_SEGMENT.fullmatch(s) for s in key.split(posixpath.sep)):
            plan.skip(local or key, "skill key cannot name a workspace path")
            continue
        if any(_name_holds_credential(part) for part in key.split(posixpath.sep)):
            plan.drop(local, _CREDENTIAL)  # the key names its folder
            continue
        # A selected row names the directory its check was fixed against; the
        # copy reads there and never resolves the row's path again.
        real = str(row.get("real_dir") or os.path.realpath(local))
        # Invariant (a), again at copy: a selected row must sit strictly inside the
        # Crew skill folder it was selected under; one outside them is named.
        copy_root, copy_apps = str(row.get("copy_root") or ""), bool(row.get("copy_apps"))
        if "copy_root" in row and not _inside_crew_folder(real, copy_root, copy_apps):
            plan.leave_outside(local)
            continue
        try:
            files = _skill_files(
                real,
                plan,
                MAX_TOTAL_BYTES - total,
                MAX_FILES - len(plan.files),
                admit=row.get("admit"),
                within=(
                    functools.partial(_inside_crew_folder, folder=copy_root, apps=copy_apps)
                    if "copy_root" in row
                    else None
                ),
            )
        except _Outside:
            plan.leave_outside(local)
            continue
        except _HoldsSecret as exc:
            plan.drop(local, exc.reason)
            continue
        except _OverBudget:
            plan.skip(local, f"over the {MAX_TOTAL_BYTES >> 20} MiB / {MAX_FILES} file copy cap")
            continue
        except _NotAdmitted:
            plan.skip(local, "not a skill this session loads (or it changed since checked)")
            continue
        except _HoldsHostState:
            plan.skip(local, "sensitive path")
            continue
        if files is None:
            plan.skip(local, "SKILL.md missing, unreadable or a link")
            continue
        size = sum(len(d) for _r, _m, d in files)
        if total + size > MAX_TOTAL_BYTES:  # a file that grew between stat and read
            plan.skip(local, f"over the {MAX_TOTAL_BYTES >> 20} MiB / {MAX_FILES} file copy cap")
            continue
        total += size
        plan.skills[key] = tuple(dict.fromkeys((local.rstrip("/"), real)))
        plan.files.extend((f"{key}/{rel}", mode, data) for rel, mode, data in files)
    plan.seal()
    return plan


def allowed_skill_rows(
    session_key: str, agent: str, project_dir: str | Path | None
) -> list[dict[str, Any]]:
    """The skills the KAS session for *agent* may use, read from the spec KAS runs.

    The scope is the one KAS applies to that session's spec
    (``agent_sdk.kas_skill_view.kas_session_skill_scope``), so a checkout cannot
    widen what is copied. A skill from a tree KAS reads is copied only if KAS
    loads it (``kas_skill_scope.loaded_skill_files``, the classifier a KAS start
    runs), and a Crew catalog skill only if the session's rules do not deny it.
    A whole-catalog agent (``kirocrew``,
    ``kirocrew-worker``, the crewmate's) gets Crew's whole catalog plus the
    ``~/.kiro/skills`` tree KAS offers it; an agent that inherits the default
    trees gets ``~/.kiro/skills``. Project trees and project skills are left out:
    they live in the repository the workspace checks out. A failure raises, and
    the caller copies nothing.
    """
    from kiro_crew.agent_discovery import WHOLE_CATALOG_TEMPLATES
    from kiro_crew.agent_sdk.kas_skill_view import kas_session_skill_scope
    from kiro_crew.config.paths import kiro_home
    from kiro_crew.skills import SkillsLoader

    native = [str(kiro_home() / "skills" / "*" / "SKILL.md")]
    kas = kas_session_skill_scope(session_key, agent, project_dir)
    globs = kas.globs
    whole_catalog = globs is None and agent in WHOLE_CATALOG_TEMPLATES
    if globs is None:
        globs = native
    if project_dir:
        checkout = os.path.realpath(project_dir) + os.sep
        globs = [g for g in globs if not os.path.realpath(g).startswith(checkout)]
    scope: list[str] = globs
    loader = SkillsLoader(install_builtins=False)
    try:
        # A first-run catalog still building lists only part of the scope, and a
        # copy taken from it would miss skills for the whole runtime: wait for it,
        # or fail so the agent is told the copy is missing. A read that timed out
        # can be followed by the builder publishing before the status is checked,
        # so the rows kept are only those read after the catalog reported complete.
        folders = _crew_skill_folders(loader._dir)

        def selected(row: dict[str, Any], bound: dict[str, Any] | None) -> dict[str, Any] | None:
            # Invariant (a): the row carries the Crew skill folder it sits in, or
            # none, so ``plan_copy`` names a skill outside them instead of copying it.
            if bound is None:
                return None
            real = str(bound["real_dir"])
            base, apps = next(
                ((f, a) for f, a in folders if _inside_crew_folder(real, f, a)), ("", False)
            )
            return {**row, **bound, "copy_root": base, "copy_apps": apps}

        def read() -> list[dict[str, Any]]:
            # Each row carries its check, which ``plan_copy`` asks again with the
            # descriptor and bytes it copies: a file replaced after this first
            # pass matches no verdict and is left out.
            rows = (
                [
                    picked
                    for row in loader.scoped_skills(project_dir=project_dir, only=None)
                    if (picked := selected(row, kas.bind(str(row.get("path") or ""))))
                ]
                if whole_catalog
                else []
            )
            # The catalog matches globs with fnmatch (``*`` crosses folders), and
            # lists skills KAS refuses; keep only the files KAS loads.
            return rows + [
                picked
                for row in loader.scoped_skills(project_dir=project_dir, only=scope)
                if (picked := selected(row, kas.bind(str(row.get("path") or ""), crew_only=False)))
            ]

        deadline = time.monotonic() + CATALOG_WAIT_SECONDS
        read()  # starts discovery on a cold catalog
        while True:
            while loader.catalog_status(project_dir) == "building":
                if time.monotonic() > deadline:
                    raise RuntimeError("the skill catalog is still building")
                time.sleep(0.5)
            rows = read()
            if loader.catalog_status(project_dir) != "building":
                break
    finally:
        loader.close()
    return [row for row in rows if not row.get("confine_root")]


def _command(mode: str, digest: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{16,64}", digest):
        raise ValueError("invalid skills digest")
    return (
        'set -eu; umask 077; cd "$HOME/.kirocrew-worker"; '
        f"exec python3 skills_install.py {shlex.quote(mode)} {digest}"
    )


#: The guest's answer reaches the agent's context, and the workspace is the agent's
#: to change, so only shapes the gateway can check are kept: a plain absolute
#: path ending in this copy's own folder, a dotted version, and paths it sent.
#: ``mcp_tools/skills._read_capacity`` reserves room for a root this long.
ROOT_MAX_CHARS = 256
_ROOT_RE = re.compile(r"/[A-Za-z0-9._@+/-]{1,%d}" % (ROOT_MAX_CHARS - 1))
_VERSION_RE = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}")


def _parse(output: bytes, plan: CopyPlan) -> SkillCopy:
    try:
        result = json.loads(output.decode("utf-8").strip().splitlines()[-1])
    except (UnicodeDecodeError, IndexError, json.JSONDecodeError) as exc:
        raise ValueError("the workspace sent no readable skills report") from exc
    if not isinstance(result, dict):
        raise ValueError("the workspace sent no readable skills report")
    root = result.get("root")
    if (
        result.get("present") is not True
        or not isinstance(root, str)
        or not _ROOT_RE.fullmatch(root)
        or not root.endswith(f"/.kirocrew-worker/skills/{plan.digest}")
        or "/../" in root
    ):
        raise ValueError("the workspace did not report a complete skills copy")
    python = result.get("python")
    # Only scripts sent can fail to compile there.
    sent = {rel for rel, _m, _d in plan.files if rel.endswith(".py")}
    bad = result.get("incompatible")
    examples = tuple(
        (path, "")
        for path in list(
            dict.fromkeys(
                item[0]
                for item in (bad if isinstance(bad, list) else [])
                if isinstance(item, list) and item and item[0] in sent
            )
        )[: skills_install.MAX_REPORTED]
    )
    # The guest's count, held between the examples it named and the scripts sent.
    total = result.get("incompatible_total")
    sent_py = len(sent)
    total = total if isinstance(total, int) and not isinstance(total, bool) else 0
    return SkillCopy(
        root=root,
        skills=dict(plan.skills),
        skipped=tuple(plan.skipped),
        skipped_total=plan.skipped_total,
        outside=tuple(plan.outside),
        outside_total=plan.outside_total,
        dropped=tuple(plan.dropped),
        dropped_total=plan.dropped_total,
        python=python if isinstance(python, str) and _VERSION_RE.fullmatch(python) else "",
        # Each sent path once, so the guest cannot set the count by repeating one.
        incompatible=examples,
        incompatible_total=min(max(total, len(examples)), sent_py),
    )


async def sync_skills(shell: Any, plan: CopyPlan) -> SkillCopy:
    """Make ``plan``'s copy present in the workspace behind ``shell``; return it.

    Asks first, so a copy another session (or this one's last start) installed is
    reused with no transfer. Needs ``skills_install.py`` beside the worker files.
    """
    digest = plan.digest
    found = await shell.run(_command("check", digest))
    copy = None
    try:
        copy = _parse(found, plan)
    except ValueError:
        pass
    if copy is None:
        copy = _parse(await shell.run(_command("install", digest), plan.payload()), plan)
    return copy


#: A start's copy may not hold the session's start past this.
COPY_TIMEOUT_SECONDS = 90.0
#: The record when the switch is off, and when the copy was tried and failed.
DISABLED = "disabled"
FAILED = "failed"
Record = SkillCopy | str | None
#: One record per session, read by the context and the environment panel: the copy
#: this session's current runtime made, ``DISABLED``, ``FAILED``, or ``None`` (no
#: skill to copy). Bounded by the one cap ``session_environment`` keeps for the
#: same sessions' per-session maps.
_RECORDS_MAX = _REMOTE_WORKSPACES_MAX
_records: dict[str, Record] = {}


def _remember(session_key: str, record: Record) -> None:
    _records.pop(session_key, None)
    if len(_records) >= _RECORDS_MAX:
        # A session that does not run remotely goes first, so ended sessions'
        # records never push out a live one's.
        ended = next((k for k in _records if remote_workspace_for(k) is None), None)
        _records.pop(ended if ended is not None else next(iter(_records)))
    _records[session_key] = record


def record_for(session_key: str | None) -> Record:
    """This session's record, only while it runs remotely (else ``None``)."""
    if not session_key or not (remote_workspace_for(session_key) or ("", ""))[1]:
        return None
    return _records.get(session_key)


def copy_for(session_key: str | None) -> SkillCopy | None:
    record = record_for(session_key)
    return record if isinstance(record, SkillCopy) else None


async def copy_for_session(
    session_key: str, agent: str, project_dir: str | Path | None, shell: Any, *, enabled: bool
) -> None:
    """Copy ``session_key``'s skills into its workspace, at this runtime's start.

    Best effort: a failure costs the session its skill scripts, never its start,
    and is recorded so the agent is not shown a copy that is not there.
    """
    import asyncio

    if not enabled:
        _remember(session_key, DISABLED)
        return

    async def copy() -> SkillCopy | None:
        rows = await asyncio.to_thread(allowed_skill_rows, session_key, agent, project_dir)
        plan = await asyncio.to_thread(plan_copy, rows)
        for item in (*plan.outside, *plan.dropped, *plan.skipped):
            logger.info("skills copy for %s left out %s: %s", session_key, item.path, item.reason)
        if plan.skipped_total > len(plan.skipped):
            logger.info(
                "skills copy for %s left out %d more paths not listed",
                session_key,
                plan.skipped_total - len(plan.skipped),
            )
        if plan.skills:
            return await sync_skills(shell, plan)
        if plan.outside_total or plan.dropped_total or plan.skipped_total:
            # Nothing to send, but the agent and the card are still told why.
            return SkillCopy(
                root="",
                skills={},
                skipped=tuple(plan.skipped),
                skipped_total=plan.skipped_total,
                outside=tuple(plan.outside),
                outside_total=plan.outside_total,
                dropped=tuple(plan.dropped),
                dropped_total=plan.dropped_total,
            )
        return None

    record: Record = FAILED
    try:
        # One deadline for the scope read, the walk and the transfer together.
        record = await asyncio.wait_for(copy(), COPY_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 -- the session starts without the copy
        logger.warning("Could not copy skills into the remote workspace: %r", exc)
    _remember(session_key, record)


def context_note(record: Record) -> str:
    """The per-turn line telling a remote agent where its skills' files are."""
    if record == DISABLED:
        return (
            "[SKILLS] Skill files are not copied into this workspace (Settings > "
            "Session Environments > Copy skills to the workspace is off), so a "
            "skill's scripts cannot run here.\n\n"
        )
    if record == FAILED:
        return (
            "[SKILLS] Copying this session's skills into the workspace failed when it "
            "started, so a skill's scripts are not there; the next start tries again.\n\n"
        )
    if not isinstance(record, SkillCopy):
        return ""
    if not record.skills:
        # Every skill was left out: no copy, but the reasons below still apply.
        return _left_out_note(
            "[SKILLS] No skill was copied into this workspace, so a skill's scripts "
            "cannot run here.",
            record,
        )
    text = (
        f"[SKILLS] Kiro Crew copied the {len(record.skills)} skills you can use into this "
        f"workspace, read-only, at {record.root}/<skill key>/ (SKILL.md, scripts/, "
        "references/, assets). That folder is the skill's directory here: run "
        "`python3 <it>/scripts/<name>.py` with run_command, and read its files with "
        "run_command too (they sit outside the folder the file tools reach). A skill "
        "path that names the gateway "
        "machine (its home, or ~/.kiro/crew/skills) means the same file in this copy. "
        "The copy was made when this session started; a skill edited since then "
        "reaches it at the next start."
    )
    if record.python_older:
        text += (
            f" The workspace's python3 is {record.python}, older than the Python "
            f"{_gateway_python()} the gateway runs skill scripts with. A script that fails "
            "here with a SyntaxError, a TypeError such as `unsupported operand type(s) "
            "for |`, or a missing standard-library name is most likely failing on that "
            "older interpreter, not on its own code; say so rather than patching the script."
        )
    if record.incompatible:
        from kiro_crew.context import _neutralize_structural_markers

        # A file name is the skill author's text, so it gets the same marker scrub as
        # every other piece of skill content that reaches the context.
        count = record.incompatible_count
        shown = _neutralize_structural_markers(", ".join(p for p, _r in record.incompatible[:5]))
        if count > 5:
            shown += f", and {count - 5} more"
        text += (
            f" {count} copied scripts do not compile on the "
            f"workspace's python3 ({shown})"
            + (": they need a newer Python there." if record.python_older else ".")
        )
    return _left_out_note(text, record)


def _left_out_note(text: str, record: SkillCopy) -> str:
    """*text* followed by what the copy left out, and why."""
    from kiro_crew.context import _neutralize_structural_markers

    if record.skipped_total:
        text += (
            f" {record.skipped_total} files were left out (links, hidden or credential "
            "files, oversized files)."
        )
    if record.outside:
        # A folder name is the skill author's text: the same marker scrub.
        shown = _neutralize_structural_markers(", ".join(s.path for s in record.outside[:5]))
        if record.outside_count > 5:
            shown += f", and {record.outside_count - 5} more"
        text += (
            f" {record.outside_count} skills sit outside Crew's skill folders and were not "
            f"copied to this workspace ({shown}): their instructions still load, but "
            "their scripts cannot run here."
        )
    if record.dropped:
        shown = _neutralize_structural_markers(
            "; ".join(f"{s.path} ({s.reason})" for s in record.dropped[:5])
        )
        if record.dropped_count > 5:
            shown += f"; and {record.dropped_count - 5} more"
        text += (
            f" {record.dropped_count} skills were not copied because a file in them looks "
            f"like a secret ({shown}): their scripts cannot run here."
        )
    return text + "\n\n"


def summary(record: Record) -> dict[str, Any] | None:
    """What the environment panel shows about the copy, or ``None`` for nothing."""
    if record == DISABLED:
        return {"state": "off"}
    if record == FAILED:
        return {"state": "failed"}
    if not isinstance(record, SkillCopy):
        return None
    return {
        "state": "copied",
        "root": record.root,
        "count": len(record.skills),
        "python": record.python,
        # The card warns when the workspace's python3 is older than this one.
        "gateway_python": _gateway_python() if record.python_older else None,
        "incompatible": record.incompatible_count,
        "skipped": [{"path": s.path, "reason": s.reason} for s in record.skipped[:50]],
        "skipped_count": record.skipped_total,
        # Each skill left out whole for sitting outside Crew's skill folders.
        "outside": [{"path": s.path, "reason": s.reason} for s in record.outside[:50]],
        "outside_count": record.outside_count,
        # Each skill left out whole because a file in it looks like a secret.
        "dropped": [{"path": s.path, "reason": s.reason} for s in record.dropped[:50]],
        "dropped_count": record.dropped_count,
    }
