"""What an App Kit signature covers, and the check that a tree still matches it.

A scheme-2 signature (``AppManifest.signing_payload``) is taken over the whole
manifest, and the manifest's ``fileDigests`` lists every packaged file with its
SHA-256. So one HMAC binds the manifest and the bytes of every file the app runs.
This module computes that inventory, compares a tree with a declared one and
signs an app directory; ``apps.admission`` decides when the comparison is asked.

A *packaged file* is a file an install carries into the installed app: every
regular file under the app root except

- the names the tree copy never carries, at any depth
  (:func:`kiro_crew.apps.manager.is_uncopied_name`: ``node_modules``,
  ``.git``, ``__pycache__``, ``.venv``, the gateway's records and dependency
  trees), and
- at the root, ``app.json`` (signed as the manifest itself) and the gateway's
  runtime entries ``data``, ``.app_secret`` and ``app-crons.json``. Source
  admission refuses unsigned runtime entries that can survive a local copy,
  and all three in registry publisher trees that run in place. Installed runtime
  state is excluded from the inventory and preserved across updates.

Signer and verifier walk with the same function, so they cannot disagree about
what is in the package. A link, a junction, a device or pipe, a name that is not
valid UTF-8 and two names that differ only by case are refused rather than
skipped: each is a way for the bytes that run to differ from the bytes listed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kiro_crew.apps.manifest import AppManifest
from kiro_crew.atomic_write import atomic_write
from kiro_crew.platform_compat import is_link_or_junction

#: Root entries outside the file inventory: the separately signed manifest and
#: gateway-owned runtime state. Source admission distinguishes local copies
#: (which replace the secret) from registry trees that run publisher code in place.
_ROOT_EXCLUDED = frozenset({"app.json", "data", ".app_secret", "app-crons.json"})

#: Bounds on what one inventory may hold, so a hostile tree cannot turn the
#: admission into an unbounded walk.
MAX_PACKAGED_FILES = 20_000
_MAX_PATH_CHARS = 1024
_HEX = frozenset("0123456789abcdef")
_CHUNK = 1 << 20


class InventoryError(ValueError):
    """The tree cannot be described by a signed inventory (the reason is the text)."""


class _InventoryUnavailableError(InventoryError):
    """An inventory could not be read, rather than proven invalid."""


class _InventoryUnavailable(str):
    """String-compatible refusal retaining an inventory I/O failure's kind."""


def _fold(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _file_sha256(path: Path, rel: str) -> str:
    """SHA-256 of a regular file, opened without following a final link."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise _InventoryUnavailableError(f"{rel} cannot be read: {exc.strerror or exc}") from exc
    digest = hashlib.sha256()
    try:
        stream = os.fdopen(fd, "rb")
    except OSError:
        os.close(fd)
        raise
    with stream as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise InventoryError(f"{rel} is not a regular file")
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def packaged_inventory(root: str | Path) -> dict[str, str]:
    """``{relative POSIX path: sha256 hex}`` for every packaged file under *root*.

    Paths are NFC-normalized and sorted. Raises :class:`InventoryError` for a
    tree no signed inventory can describe (see the module docstring). An I/O
    failure raises its unavailable subclass, so callers never confuse unreadable
    content with proven invalid content. Blocking.
    """
    from kiro_crew.apps.manager import is_uncopied_name  # the facade loads lazily

    root = Path(root)
    if is_link_or_junction(root):
        raise InventoryError(f"{root} is not a directory")
    try:
        root_mode = root.stat().st_mode
    except OSError as exc:
        raise _InventoryUnavailableError(f"{root} cannot be inspected: {exc}") from exc
    if not stat.S_ISDIR(root_mode):
        raise InventoryError(f"{root} is not a directory")
    out: dict[str, str] = {}
    folded: dict[str, str] = {}

    def walk(directory: Path, parts: tuple[str, ...]) -> None:
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            where = "/".join(parts) or "the app root"
            raise _InventoryUnavailableError(
                f"{where} cannot be listed: {exc.strerror or exc}"
            ) from exc
        for entry in entries:
            name = entry.name
            if is_uncopied_name(name) or (not parts and name in _ROOT_EXCLUDED):
                continue
            rel_parts = (*parts, name)
            rel = "/".join(rel_parts)
            try:
                name.encode("utf-8")
            except UnicodeEncodeError:
                raise InventoryError(f"{rel!r} is not a valid UTF-8 name") from None
            if "\\" in name or ":" in name:
                raise InventoryError(f"{rel} has a backslash or colon in its name")
            if len(rel) > _MAX_PATH_CHARS:
                raise InventoryError(f"{rel[:80]}... is longer than {_MAX_PATH_CHARS} characters")
            path = Path(entry.path)
            if is_link_or_junction(path):
                raise InventoryError(f"{rel} is a link; a signed app cannot contain links")
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                walk(path, rel_parts)
                continue
            if not stat.S_ISREG(mode):
                raise InventoryError(f"{rel} is not a regular file")
            key = unicodedata.normalize("NFC", rel)
            fold = _fold(rel)
            if fold in folded:
                raise InventoryError(f"{rel} and {folded[fold]} differ only by case")
            folded[fold] = rel
            if len(out) >= MAX_PACKAGED_FILES:
                raise InventoryError(f"the app has more than {MAX_PACKAGED_FILES} files")
            out[key] = _file_sha256(path, rel)

    try:
        walk(root, ())
    except OSError as exc:
        # Includes entry stat, fdopen, fstat, reading and closing a packaged file.
        raise _InventoryUnavailableError(f"the app tree cannot be read: {exc}") from exc
    return dict(sorted(out.items()))


def source_runtime_entry(root: Path, *, runs_in_place: bool) -> str | None:
    """Unsigned root content that can reach publisher execution, or None.

    Local copies replace ``.app_secret`` before use; registry builds and scripts
    run in the source tree itself. This root-only check never inspects installed
    runtime state or descends into a reused checkout's dependency directories.
    """
    replaced = {"app.json"} if runs_in_place else {"app.json", ".app_secret"}
    excluded = _ROOT_EXCLUDED - replaced
    try:
        with os.scandir(root) as entries:
            return next((entry.name for entry in entries if entry.name in excluded), None)
    except OSError as exc:
        return f"cannot inspect source root: {exc}"


def unpackaged_entry(root: str | Path) -> str | None:
    """The first entry under *root* the signed file list leaves out, or None.

    Asked of a fresh registry clone, which holds only committed content: an entry
    under a name the install never copies (other than the checkout's own root
    ``.git``) or a root ``data``, ``.app_secret`` or cron record there came from
    the repository, and the build and ``onInstall`` would run it in place. Links
    are not followed. Blocking.
    """
    from kiro_crew.apps.manager import is_uncopied_name  # the facade loads lazily

    pending: list[tuple[Path, tuple[str, ...]]] = [(Path(root), ())]
    while pending:
        directory, parts = pending.pop()
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            rel = "/".join((*parts, name))
            if not parts and name == ".git":
                continue
            if is_uncopied_name(name) or (
                not parts and name in _ROOT_EXCLUDED and name != "app.json"
            ):
                return rel
            if entry.is_dir(follow_symlinks=False):
                pending.append((Path(entry.path), (*parts, name)))
    return None


def declared_path_error(path: object) -> str | None:
    """Why *path* cannot be a ``fileDigests`` key, or None when it can."""
    from kiro_crew.apps.manager import is_uncopied_name  # the facade loads lazily

    if not isinstance(path, str) or not path:
        return "fileDigests has an empty or non-string path"
    if len(path) > _MAX_PATH_CHARS:
        return f"fileDigests path is longer than {_MAX_PATH_CHARS} characters"
    if "\\" in path or "\x00" in path or ":" in path:
        return f"fileDigests path {path!r} has a backslash, NUL or colon"
    if path.startswith("/"):
        return f"fileDigests path {path!r} is absolute"
    if unicodedata.normalize("NFC", path) != path:
        return f"fileDigests path {path!r} is not NFC-normalized"
    segments = path.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        return f"fileDigests path {path!r} is not a normalized relative path"
    if segments[0] in _ROOT_EXCLUDED or any(is_uncopied_name(seg) for seg in segments):
        return f"fileDigests path {path!r} names a file an install never carries"
    return None


def inventory_mismatch(root: str | Path, declared: object) -> str | None:
    """Why the tree at *root* differs from the *declared* inventory, or None.

    *declared* is the manifest's ``fileDigests``. Every listed path must be a
    packaged file with that digest, and every packaged file must be listed.
    Blocking: it hashes every packaged file.
    """
    if not isinstance(declared, Mapping):
        return "fileDigests is not an object"
    folded: dict[str, str] = {}
    for path, digest in declared.items():
        error = declared_path_error(path)
        if error:
            return error
        if not isinstance(digest, str) or len(digest) != 64 or not set(digest) <= _HEX:
            return f"fileDigests[{path!r}] is not a lowercase SHA-256 hex digest"
        fold = _fold(path)
        if fold in folded:
            return f"fileDigests lists {path!r} and {folded[fold]!r}, which differ only by case"
        folded[fold] = path
    try:
        actual = packaged_inventory(root)
    except _InventoryUnavailableError as exc:
        return _InventoryUnavailable(str(exc))
    except OSError as exc:
        return _InventoryUnavailable(f"the app tree cannot be read: {exc}")
    except InventoryError as exc:
        return str(exc)
    for path in sorted(declared):
        if path not in actual:
            return f"{path} is signed but missing"
        if not hmac.compare_digest(actual[path], declared[path]):
            return f"{path} does not match its signed digest"
    for path in actual:
        if path not in declared:
            return f"{path} is not in the signed file list"
    return None


def app_signature(secret: str, manifest: AppManifest) -> str:
    """The HMAC-SHA256 hex signature of *manifest* under *secret*.

    The one function signer and verifier share (``admission._signature_valid``).
    Raises ``TypeError``/``ValueError`` for a manifest whose document is not JSON.
    """
    return hmac.new(secret.encode("utf-8"), manifest.signing_payload(), hashlib.sha256).hexdigest()


def sign_app_dir(root: str | Path, *, signer: str, secret: str) -> dict[str, Any]:
    """Sign the app at *root* in place and return the signed manifest document.

    Sets ``signer`` and ``fileDigests`` (the :func:`packaged_inventory` of the
    tree as it is now), then ``signature`` over the result. Run it on the tree
    exactly as it will be installed: under ``require_signature`` any later
    change to a packaged file, including one made by an install-time build or
    ``onInstall``, makes admission refuse the app. Blocking.
    """
    root = Path(root)
    manifest_path = root / "app.json"
    from kiro_crew.user_json import loads_user_json

    # The same reader install and verification use, so a BOM-prefixed manifest
    # they accept can also be signed.
    document = loads_user_json(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{manifest_path} is not a JSON object")
    if not signer:
        raise ValueError("a signer id is required")
    document.pop("signature", None)
    document["signer"] = signer
    document["fileDigests"] = packaged_inventory(root)
    document["signature"] = app_signature(secret, AppManifest.from_dict(document))
    atomic_write(manifest_path, json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    return document
