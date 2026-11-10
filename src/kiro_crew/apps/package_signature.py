"""Signature scheme 2: a publisher signature over the whole manifest and its files.

Scheme 1 (``AppManifest.signing_payload``) covers selected manifest fields only, so
a signed app's packaged files could change with its signature still verifying.
Scheme 2 signs the complete ``app.json`` object (minus ``signature``), and that
object carries ``signedFiles``: the SHA-256 of every packaged file, keyed by its
``/``-separated path under the app root. ``app.json`` itself is not listed; the
signature binds it directly.

What scheme 2 does not cover: a file that is NOT in ``signedFiles`` (files an app
writes into its own folder at runtime, gateway sidecars) is tolerated. Every listed
file must be present, a regular file reached through real directories, and
byte-identical. Sealing the installed tree against extra files is a separate
decision.

Only the admission gate (``apps.admission``) decides when this check applies; this
module computes payloads and inventories and never reads the fleet policy.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import importlib.machinery
import json
import os
import posixpath
import shlex
import stat
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from kiro_crew.platform.admission import canonical_signing_bytes, hmac_signature
from kiro_crew.platform_compat import (
    IS_POSIX,
    PinnedDirectory,
    open_file_no_reparse,
    pinned_directory,
)
from kiro_crew.user_json import loads_user_json

SIGNATURE_SCHEME = 2
SCHEME_KEY = "signatureScheme"
FILES_KEY = "signedFiles"

#: Top-level entries the publisher never signs. ``app.json`` is signed directly;
#: ``data/`` and ``.app_secret`` are the gateway's (the install copy carries them
#: from the source, then the gateway replaces or preserves its own). The names
#: ``manager_tree._COPY_IGNORE`` drops (``installed.json``, the approved-grants
#: file, ``node_modules``, ``.git`` and the rest) are left out at every depth.
_GATEWAY_OWNED_TOP_LEVEL = frozenset({"app.json", ".app_secret", "data"})

_CHUNK = 1024 * 1024

#: Digests of files already hashed, keyed by what ``fstat`` reports about the opened
#: descriptor. On POSIX a write to the file changes ``st_ctime_ns`` (which a same-user process
#: cannot set back), and a replacement changes ``st_ino``, so a hit is a file whose
#: bytes have not changed since they were hashed. The check runs at several
#: boundaries of one activation; this keeps each listed file to one read. POSIX
#: only: on Windows ``st_ctime_ns`` is the creation time, which a rewrite does not
#: move, so every check there hashes again.
_DIGESTS: dict[tuple[int, int, int, int, int], str] = {}
_DIGESTS_LOCK = threading.Lock()
_DIGESTS_MAX = 8192

#: A digest is not cached while either stamp is this close to the time it was taken
#: (the racy-git rule): a filesystem with coarse timestamps can give a rewrite in the
#: same tick the same stamps, so a fresh stamp proves nothing yet.
_RACY_WINDOW_NS = 2_000_000_000

#: The clock the racy rule reads; a test sets it.
_now_ns: Callable[[], int] = time.time_ns


def package_signing_payload(document: Mapping[str, Any]) -> bytes:
    """Canonical bytes a scheme-2 signature covers: the manifest minus ``signature``."""
    return canonical_signing_bytes({k: v for k, v in document.items() if k != "signature"})


def package_signature(secret: str, document: Mapping[str, Any]) -> str:
    """HMAC-SHA256 hex digest of :func:`package_signing_payload` under *secret*."""
    return hmac_signature(secret, package_signing_payload(document))


def declares_scheme_2(document: object) -> bool:
    """Whether *document* names signature scheme 2 (a real JSON integer, not ``true``)."""
    if not isinstance(document, Mapping):
        return False
    scheme = document.get(SCHEME_KEY)
    return type(scheme) is int and scheme == SIGNATURE_SCHEME


def _path_parts(rel: str) -> list[str] | None:
    """The components of an inventory path, or None when it is not a plain relative path."""
    if not rel or "\\" in rel or ":" in rel or "\x00" in rel:
        return None
    parts = rel.split(posixpath.sep)
    if any(part in ("", ".", "..") for part in parts):
        return None
    return parts


def _fd_sha256(fd: int) -> str:
    """SHA-256 of the regular file open at *fd*; OSError for anything else."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise OSError(errno.EINVAL, "not a regular file")
    key = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    cacheable = IS_POSIX and bool(info.st_ino)
    if cacheable:
        with _DIGESTS_LOCK:
            cached = _DIGESTS.get(key)
        if cached is not None:
            return cached
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, _CHUNK)
        if not chunk:
            break
        digest.update(chunk)
    value = digest.hexdigest()
    now = _now_ns()
    if cacheable and all(
        abs(now - stamp) >= _RACY_WINDOW_NS for stamp in (info.st_mtime_ns, info.st_ctime_ns)
    ):
        with _DIGESTS_LOCK:
            if len(_DIGESTS) >= _DIGESTS_MAX:
                _DIGESTS.clear()
            _DIGESTS[key] = value
    return value


def _file_sha256(directory: PinnedDirectory, name: str) -> str:
    """SHA-256 of the regular file *name* in the pinned *directory*.

    The open itself refuses a link or junction at the name, and ``fstat`` on the
    descriptor refuses anything that is not a regular file. On POSIX the open is
    relative to the pinned directory's descriptor (``PinnedDirectory._open_file``,
    the pin's own leaf open), so a parent renamed after the pin is not followed. On
    Windows the pin's handles refuse a rename of every directory above, so the path
    still names the pinned directory.
    """
    if IS_POSIX:
        fd = directory._open_file(name)
    else:
        fd = open_file_no_reparse(
            os.path.join(directory.path, name), nonblocking=True, links_only=True
        )
    try:
        return _fd_sha256(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def _pinned_parent(root: PinnedDirectory, parts: list[str]) -> Iterator[PinnedDirectory | None]:
    """Pin each directory on the way to ``parts[-1]``; None when one is not a real directory."""
    with contextlib.ExitStack() as stack:
        current = root
        for part in parts[:-1]:
            child = current.child_if_real_dir(part)
            if child is None:
                yield None
                return
            current = stack.enter_context(child)
        yield current


def inventory_refusal(
    root: Path,
    document: Mapping[str, Any],
    *,
    skip: Callable[[str], bool] | None = None,
) -> str | None:
    """Why the files under *root* do not match *document*'s signed inventory, or None.

    Call only after the document's signature verified: the inventory is publisher
    text from then on. *skip* names inventory paths left unchecked (the dev-mode
    ``ui`` tree an operator granted). Every directory on the way to a listed file is
    pinned through its parent and no link or junction is followed, so a listed file
    or one of its directories replaced by a link is refused.

    Raises nothing for a file problem: a file that cannot be read is a refusal. An
    ``OSError`` opening *root* itself is raised to the caller, which fails closed.
    """
    inventory = document.get(FILES_KEY)
    if not isinstance(inventory, Mapping):
        return f"the signed manifest has no {FILES_KEY} inventory"
    entries: list[tuple[str, list[str], object]] = []
    for rel, expected in inventory.items():
        parts = _path_parts(rel) if isinstance(rel, str) else None
        if parts is None:
            return f"the signed inventory names an unusable path {rel!r}"
        if skip is not None and skip(rel):
            continue
        entries.append((rel, parts, expected))
    if not entries:
        return None
    with pinned_directory(root, links_only=True) as pinned_root:
        for rel, parts, expected in entries:
            try:
                with _pinned_parent(pinned_root, parts) as parent:
                    if parent is None:
                        return f"packaged file {rel!r} is not inside a real directory"
                    actual = _file_sha256(parent, parts[-1])
            except OSError:
                return f"packaged file {rel!r} is missing, a link, or not a regular file"
            if not isinstance(expected, str) or actual != expected:
                return f"packaged file {rel!r} does not match the signed inventory"
    return None


def _listed(rel: str, inventory: Mapping[str, Any], root: Path) -> bool:
    """Whether *rel* is a listed file, or a directory holding one.

    A listed key spelled in another case counts only when the volume serves both
    spellings as the same directory entries (a case-insensitive volume), so on a
    case-sensitive volume a case twin is still a different, unlisted file.
    """
    if rel in inventory or any(str(key).startswith(rel + "/") for key in inventory):
        return True
    folded = rel.casefold()
    for key in inventory:
        spelled = str(key)[: len(rel)]
        if spelled.casefold() == folded and str(key)[len(rel) : len(rel) + 1] in ("", "/"):
            if _same_entries(root, rel, spelled):
                return True
    return False


def _same_entries(root: Path, rel: str, other: str) -> bool:
    """Whether every prefix of *rel* and *other* under *root* is the same entry (``lstat``)."""
    ours, theirs = PurePosixPath(rel).parts, PurePosixPath(other).parts
    if len(ours) != len(theirs):
        return False
    for depth in range(1, len(ours) + 1):
        try:
            a = os.lstat(root.joinpath(*ours[:depth]))
            b = os.lstat(root.joinpath(*theirs[:depth]))
        except OSError:
            return False
        if not os.path.samestat(a, b):
            return False
    return True


#: Suffixes that make a manifest-named or argv path code.
_CODE_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".sh")

#: Non-Python interpreters whose first non-option argument is the script they run.
_SCRIPT_INTERPRETERS = frozenset({"node", "nodejs", "sh", "bash"})

#: Shell separators after which the next word is a command again.
_COMMAND_SEPARATORS = frozenset({"&&", "||", ";", "|"})

#: Interpreter options whose value is the next word, not a script.
_OPTIONS_WITH_VALUES = frozenset({"-X", "-W", "--check-hash-based-pycs"})


def _command_base(command: object) -> str:
    """The lower-cased basename of *command*, without a Windows ``.exe``."""
    if not isinstance(command, str):
        return ""
    return posixpath.basename(command.replace("\\", "/")).lower().removesuffix(".exe")


def is_python_interpreter(command: object) -> bool:
    """Whether *command* names a Python interpreter itself.

    ``python``, ``python3``, ``python3.12``, ``pythonw`` (bare, or a path with that
    basename). A console script such as ``python-lsp-server`` or ``pythonmcp`` is not
    one, and neither is ``node`` or a shell.
    """
    base = _command_base(command)
    if not base.startswith("python"):
        return False
    rest = base[len("python") :].removesuffix("w")
    return all(char.isdigit() or char == "." for char in rest)


def _is_script_interpreter(command: object) -> bool:
    return is_python_interpreter(command) or _command_base(command) in _SCRIPT_INTERPRETERS


def _normalized(raw: object) -> str | None:
    """*raw* as a ``/``-separated normalised path, or None when it names no path.

    Backslashes are read as separators. A relative path that leaves the app folder
    normalises to one starting with ``..``; an absolute path stays absolute.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip().replace("\\", "/")
    if not text or text.startswith("-"):
        return None
    normal = posixpath.normpath(text)
    return None if normal in ("", ".") else normal


def _cron_script_file(spec: str) -> str:
    """The file of a ``<path>:<func>`` cron script, split as registration splits it.

    Drive-aware (``cron_script._split_script_spec``), so ``C:\\app\\job.py:run``
    names ``C:\\app\\job.py``, not ``C``. A spec without a function is all path.
    """
    # Through the facade, which serves the name (its owners stay its own family).
    from kiro_crew.cron_script import _split_script_spec

    try:
        return _split_script_spec(spec)[0]
    except ValueError:
        return spec


def _hook_module_file(dotted: str) -> list[str]:
    """The one file a hook module loads: ``{base}.py`` (``module_loader.load_app_module``)."""
    parts = dotted.split(".")
    if not dotted or not all(part.isidentifier() for part in parts):
        return []
    return [posixpath.join(*parts) + ".py"]


#: The suffixes Python's path finder tries for one name, in its own order.
_FINDER_SUFFIXES: tuple[str, ...] = (
    *importlib.machinery.EXTENSION_SUFFIXES,
    *importlib.machinery.SOURCE_SUFFIXES,
    *importlib.machinery.BYTECODE_SUFFIXES,
)


def _first_file(directory: Path, stem: str) -> str | None:
    """The file Python's finder picks for *stem* in *directory*, by its suffix order."""
    for suffix in _FINDER_SUFFIXES:
        if (directory / (stem + suffix)).is_file():
            return stem + suffix
    return None


def _run_module_files(root: Path, dotted: str) -> list[str]:
    """The files ``python -m dotted`` runs from *root*, resolved the way Python does.

    For each name a package directory (one holding an ``__init__``) wins over a module
    file, and a module file over a namespace directory; a package run with ``-m``
    also runs its ``__main__``. Every ``__init__`` on the way runs too. Empty when
    nothing under *root* resolves.
    """
    parts = dotted.split(".")
    if not dotted or not all(part.isidentifier() for part in parts):
        return []
    files: list[str] = []
    here = Path(root)
    rel = ""
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        package = here / part
        init = _first_file(package, "__init__") if package.is_dir() else None
        if init is not None:
            files.append(posixpath.join(rel, part, init) if rel else posixpath.join(part, init))
            rel = posixpath.join(rel, part) if rel else part
            here = package
            if last:
                main = _first_file(package, "__main__")
                if main is not None:
                    files.append(posixpath.join(rel, main))
            continue
        module = _first_file(here, part)
        if module is not None:
            files.append(posixpath.join(rel, module) if rel else module)
            return files if last else []
        if package.is_dir():
            rel = posixpath.join(rel, part) if rel else part
            here = package
            if last:
                main = _first_file(package, "__main__")
                if main is not None:
                    files.append(posixpath.join(rel, main))
            continue
        return []
    return files


def _command_tokens(text: object) -> list[str]:
    """Words of a shell string or an argv, for finding package paths it names."""
    if isinstance(text, list):
        return [str(item) for item in text]
    if not isinstance(text, str) or not text:
        return []
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _argv_groups(tokens: list[str], root: Path) -> list[list[str]]:
    """The code paths an argv or shell line names, one candidate group per path.

    A word counts only when it is code: a path with a code suffix, the command word
    itself whatever its extension (it counts in :func:`unsigned_code_path` only when it
    names a file inside the app folder, so a system ``python3`` names none), the script
    argument of an interpreter, or the module after ``-m``. Data words (``state.db``,
    ``app.log``) never count.
    """
    groups: list[list[str]] = []
    command_position = True
    after_interpreter = False
    python = False
    skip_next = False
    for index, token in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if token in _COMMAND_SEPARATORS:
            command_position, after_interpreter = True, False
            continue
        if after_interpreter and python and _is_short_option_group(token):
            # Python reads a short-option group a letter at a time: ``-m`` and ``-c``
            # take the rest of the word or else the next word (``-mworker``,
            # ``-Bm worker``), and ``-X``/``-W`` do the same with a value.
            kind, value, takes_next = _python_option_group(token)
            if kind == "m":
                module = value or (tokens[index + 1] if index + 1 < len(tokens) else "")
                groups.extend([rel] for rel in _run_module_files(root, module))
            if kind in ("m", "c"):
                after_interpreter = False
            skip_next = takes_next
            continue
        if after_interpreter and token == "-m":
            if index + 1 < len(tokens):
                groups.extend([rel] for rel in _run_module_files(root, tokens[index + 1]))
            after_interpreter, skip_next = False, True
            continue
        if after_interpreter and token in ("-c", "-e"):
            after_interpreter, skip_next = False, True
            continue
        if after_interpreter and token in _OPTIONS_WITH_VALUES:
            skip_next = True
            continue
        if after_interpreter and token.startswith("-"):
            continue
        normal = _normalized(token)
        is_command = command_position
        if normal is not None and (
            normal.endswith(_CODE_SUFFIXES) or after_interpreter or is_command
        ):
            groups.append([normal])
        after_interpreter = False
        if is_command and _is_script_interpreter(token):
            after_interpreter = True
            python = is_python_interpreter(token)
        command_position = False
    return groups


def _is_short_option_group(token: str) -> bool:
    """Whether *token* is a single-dash option word (``-B``, ``-mworker``), not ``-``/``--x``."""
    return len(token) > 1 and token[0] == "-" and token[1] != "-"


def _python_option_group(token: str) -> tuple[str, str, bool]:
    """Read a Python short-option group the way the interpreter's own parser does.

    Returns ``(kind, value, takes_next)``: *kind* is ``"m"`` or ``"c"`` when the group
    ends in that option, else ``""``; *value* is the rest of the word after it; and
    *takes_next* says whether the option's value is the next word instead.
    """
    for position, letter in enumerate(token[1:], start=1):
        rest = token[position + 1 :]
        if letter in ("m", "c"):
            return letter, rest, not rest
        if letter in ("X", "W"):
            return "", rest, not rest
    return "", "", False


def _ui_entries(node: object) -> list[str]:
    """Every ``entry`` / ``entryPoint`` string in a UI-facing manifest block, at any depth."""
    found: list[str] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key in ("entry", "entryPoint") and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_ui_entries(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_ui_entries(item))
    return found


def declared_code_paths(manifest: Any, root: Path, *, include_ui: bool = True) -> list[list[str]]:
    """The code paths the manifest names, each as the exact file the runtime runs.

    Agents, skills, SOPs; the backend entry point as the literal path the spawn runs
    (a module-style entry, ``manifest.is_module_style_entry_point``, runs from the
    trusted package and never from the app folder, so it names none); each hook
    module (exactly ``{base}.py``); cron scripts; and the code paths MCP commands and
    arguments, ``setup`` scripts, ``openCommand`` and cron commands name
    (:func:`_argv_groups`, with ``-m`` resolved by :func:`_run_module_files`). UI
    modules (``ui``, ``contributes`` and ``desktop`` entries) resolve under ``ui/``,
    where the host serves them; ``include_ui=False`` leaves them out. Paths are
    normalised; a group counts in :func:`unsigned_code_path` only when its file exists.
    """
    from kiro_crew.apps.manifest import is_module_style_entry_point

    groups: list[list[str]] = []
    for raw in (*manifest.agents, *manifest.skills, *manifest.sops):
        normal = _normalized(raw)
        if normal is not None:
            groups.append([normal])
    backend = manifest.backend.to_dict()
    raw_entry = backend.get("entryPoint")
    entry = _normalized(raw_entry)
    if entry is not None and not is_module_style_entry_point(str(raw_entry), Path(root)):
        groups.append([entry])
    hooks = backend.get("hooks")
    if isinstance(hooks, Mapping):
        for target in hooks.values():
            files = _hook_module_file(str(target or "").partition(":")[0])
            if files:
                groups.append(files)
    for cron in manifest.crons:
        script = _normalized(_cron_script_file(str(cron.script or "")))
        if script is not None:
            groups.append([script])
        groups.extend(_argv_groups(_command_tokens(cron.command), root))
    from kiro_crew.env import expand_spec_args

    for server in manifest.mcpServers.values():
        if isinstance(server, Mapping):
            command = server.get("command")
            args = server.get("args")
            tokens = [str(command)] if isinstance(command, str) and command else []
            # Expanded as the runtime expands a stdio spec (``resolve_mcp_servers``),
            # so a home placeholder is checked as the path the child will run.
            groups.extend(_argv_groups(expand_spec_args([*tokens, *_command_tokens(args)]), root))
    for script in manifest.setup.to_dict().values():
        groups.extend(_argv_groups(_command_tokens(script), root))
    groups.extend(_argv_groups(_command_tokens(manifest.extra.get("openCommand")), root))
    if not include_ui:
        return groups
    for block in (
        manifest.ui.to_dict(),
        manifest.contributes.to_dict(),
        manifest.desktop.to_dict(),
    ):
        for raw in _ui_entries(block):
            normal = _normalized(raw)
            if normal is not None:
                groups.append([posixpath.normpath(posixpath.join("ui", normal))])
    return groups


def unsigned_code_path(root: Path, manifest: Any) -> str | None:
    """Why a code path the signed manifest names is not a signed file, or None.

    One rule for every declaration: a group counts when one of its files exists in
    the app folder, and then one of its files must be listed. A relative path that
    normalises outside the folder is refused; an absolute path outside it (a system
    ``python``) is not a packaged file. A code path where the inventory builder lists
    nothing (under ``data/``, ``node_modules`` and the other dropped names) can never
    be listed, so it is refused. A package directory beside a signed module of the
    same name (``worker/`` beside ``worker.py``) wins the import, so every module file
    in it must be listed too (``signed_first.unlisted_package_files``).
    """
    inventory = manifest.document.get(FILES_KEY)
    if not isinstance(inventory, Mapping):
        return f"the signed manifest has no {FILES_KEY} inventory"
    real_root = os.path.realpath(root)
    for group in declared_code_paths(manifest, root):
        candidates: list[str] = []
        for normal in group:
            if posixpath.isabs(normal) or os.path.isabs(normal):
                try:
                    rel = Path(os.path.realpath(normal)).relative_to(real_root).as_posix()
                except (OSError, ValueError):
                    continue
                candidates.append(rel)
            elif normal == ".." or normal.startswith("../"):
                return f"the manifest names {normal!r}, which leaves the app folder"
            else:
                candidates.append(normal)
        present = [rel for rel in candidates if os.path.lexists(root / rel)]
        if present and not any(_listed(rel, inventory, root) for rel in candidates):
            return f"the manifest names {present[0]!r}, which is not in {FILES_KEY}"
    from kiro_crew.apps.signed_first import unlisted_package_files

    listed = frozenset(str(key) for key in inventory)
    for rel in unlisted_package_files(real_root, listed):
        if not _listed(rel, inventory, root):
            return f"the unsigned package file {rel!r} shadows a signed module"
    return None


def build_file_inventory(root: Path) -> dict[str, str]:
    """SHA-256 of every file the install copy carries from *root*, for signing.

    Mirrors ``manager_tree._COPY_IGNORE``: the names the install copy drops at any
    depth are left out, and so are the gateway-owned top-level entries and links (a
    copy keeps an in-tree link as a link, which no inventory entry can match). The
    tree is walked through pinned directories, so a link is refused by the open that
    would follow it rather than screened by name first. Raises ValueError for a file
    name the verifier would refuse to read.
    """
    from kiro_crew.apps.manager import _COPY_IGNORE, _DEPS_STAGING_SWEEP_RE

    def _dropped(name: str) -> bool:
        return name in _COPY_IGNORE or _DEPS_STAGING_SWEEP_RE.fullmatch(name) is not None

    inventory: dict[str, str] = {}

    def _walk(directory: PinnedDirectory, prefix: str, top: bool) -> None:
        for name in sorted(directory.names()):
            if _dropped(name) or (top and name in _GATEWAY_OWNED_TOP_LEVEL):
                continue
            child = directory.child_if_real_dir(name)
            if child is not None:
                with child:
                    _walk(child, posixpath.join(prefix, name) if prefix else name, False)
                continue
            try:
                digest = _file_sha256(directory, name)
            except OSError:
                continue  # a link, a junction or not a regular file: nothing to sign
            rel = posixpath.join(prefix, name) if prefix else name
            if _path_parts(rel) is None:
                raise ValueError(f"cannot sign a file named {rel!r}: rename it")
            inventory[rel] = digest

    with pinned_directory(root, links_only=True) as pinned_root:
        _walk(pinned_root, "", True)
    return inventory


def sign_app_directory(root: Path, signer: str, secret: str) -> dict[str, Any]:
    """Write a scheme-2 signature into ``root/app.json``; return the signed document."""
    manifest_path = root / "app.json"
    # The parse ``AppManifest.from_json_file`` uses, so a manifest the gateway admits can be signed.
    document = loads_user_json(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("app.json must be a JSON object")
    document.pop("signature", None)
    document["signer"] = signer
    document[SCHEME_KEY] = SIGNATURE_SCHEME
    document[FILES_KEY] = build_file_inventory(root)
    document["signature"] = package_signature(secret, document)
    from kiro_crew.atomic_write import atomic_write

    atomic_write(manifest_path, json.dumps(document, indent=2) + "\n")
    return document
