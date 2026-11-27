#!/usr/bin/env python3
"""Release-captain tool for ``src/kiro_crew/apps/adoption-pins.json``.

    adoption_pin.py compute <repository-url> <commit> [--min-sdk-level N]
                                                        print the pin row for that commit
    adoption_pin.py freeze <name>                       print <name>'s row with its copy frozen
    adoption_pin.py verify                              re-check every row of the shipped file

``compute`` clones the repository at the commit AND downloads its codeload archive,
computes ``treeSha256`` on both (they must match) and ``archiveSha256``, and runs
the adoption tree checks and permission parity (E9) against the shipped built-in of
the same name. The ``legacyNav`` route and icon are read from the shipped built-in's
first page. ``verify`` does the same for every row and fails on any difference.

A pin row can outlive its wheel copy: the copy is deleted and the row kept. For such
a name the tree checks run with no shipped built-in, as the gateway runs them, and
``legacyNav`` is carried from the name's current row. A name that is neither shipped
nor pinned is refused. Such a row must carry ``deletedCopy``, the copy's E9 input and
the input of each limit only a wheel copy supplies, and a row whose copy still ships
must not (``builtin_adoption_baseline``). ``freeze`` prints the row with it, read from
the wheel copy a deletion PR is about to remove, or, once it is gone, from the parent
of the commit that deleted it, written out whole so the copy is read as the gateway's
readers read a shipped one. ``verify`` checks both rules, re-derives every
``deletedCopy`` from that parent commit, and runs E9 and E10 against it, so it reads
this checkout's git history: run it after committing the deletion, in a clone that is
not shallow.

``minAppSdkLevel`` is ``--min-sdk-level`` (default 1); the tool never raises it on
its own. The tree is scanned for the App Kit seams above level 1 it uses
(``_seam_level_uses``), and a level below the highest one found is refused by both
``compute`` and ``verify``, so a tree that uses a level-2 seam needs
``--min-sdk-level 2`` or higher.

Networked: run by hand or in a network-allowed release job, never in the offline
test suite. The digest and the checks are the gateway's own
(``kiro_crew.apps.builtin_adoption_fetch`` / ``builtin_adoption_checks``), so the
tool and the runtime cannot disagree.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from kiro_crew.apps import backend_sdk  # noqa: E402
from kiro_crew.apps import builtin_adoption_fetch as fetch  # noqa: E402
from kiro_crew.apps.app_sdk_level import APP_SDK_LEVEL  # noqa: E402
from kiro_crew.apps.builtin_adoption_baseline import (  # noqa: E402
    deleted_copy_baseline,
    freeze_deleted_copy,
    freeze_grant_inputs,
)
from kiro_crew.apps.builtin_adoption_checks import (  # noqa: E402
    pinned_tree_refusal,
    widening_refusal,
)
from kiro_crew.apps.builtin_adoption_pins import (  # noqa: E402
    _COMMIT_RE,
    _PINS_PATH,
    _REPOSITORY_RE,
    DELETED_COPY_KEY,
    AdoptionPin,
    parse_adoption_pins,
)
from kiro_crew.dashboard.token_auth_app_scope import EXACT_API_ENTRY_MARK  # noqa: E402

#: ``AppContext`` attributes above seam level 1, with the level that publishes each
#: (app-kit-platform.md §32a, §32b, §36). An app reads them as ``ctx.<seam>`` or
#: feature-detects them with ``getattr(<context>, "<seam>", None)``.
_CONTEXT_SEAMS = {
    "turns": 2,
    "untrusted": 2,
    "lifecycle": 2,
    "auto_approve": 2,
    "app_token": 5,
    "gateway_origin": 5,
    "chat_slots": 7,
    "webhook_source": 7,
}

#: A receiver whose last name says it is the app's context (``ctx``, ``self._ctx``,
#: ``context``, ``app_ctx``). ``getattr``/``hasattr`` of a seam name counts on any
#: receiver, since an app reaches its context through helpers (``getattr(bound(), ...)``).
_CONTEXT_NAME = re.compile(r"ctx|context", re.IGNORECASE)

#: The text form, for a source file this interpreter cannot parse.
_SEAM_TEXT = re.compile(r"[.\"'](%s)\b" % "|".join(_CONTEXT_SEAMS))

#: The public backend SDK package and the level that publishes it (app-kit-platform.md
#: §40). An app reaches it by a dotted import, a ``from kiro_crew.apps import
#: backend_sdk``, an attribute of an imported parent or a string lookup, so a file
#: that names both the root package and the package's own name counts, in any form.
_BACKEND_SDK_MODULE = backend_sdk.__name__
_BACKEND_SDK_ROOT = _BACKEND_SDK_MODULE.split(".", 1)[0]
_BACKEND_SDK_LEAF = re.compile(r"\b%s\b" % re.escape(_BACKEND_SDK_MODULE.rsplit(".", 1)[1]))

#: The backend SDK names published above the package's own level, with each one's
#: level (``backend_sdk._EXPORT_LEVELS``). A tree imports the package in one file and
#: calls its names in others, so a name counts in any scanned file once some scanned
#: file names the package.
_BACKEND_SDK_LATER_NAMES = dict(backend_sdk._EXPORT_LEVELS)
#: Those names as whole words; ``None`` while there are none, since an empty
#: alternation would match at every word boundary.
_BACKEND_SDK_LATER_NAME = (
    re.compile(r"\b(%s)\b" % "|".join(re.escape(name) for name in sorted(_BACKEND_SDK_LATER_NAMES)))
    if _BACKEND_SDK_LATER_NAMES
    else None
)

#: Directories a backend never imports from, so a test fake's ``ctx.turns`` is no use.
_UNSCANNED_DIRS = frozenset({"tests", "test", "node_modules", ".git"})


def _receiver_name(node: ast.expr) -> str:
    """The last name in *node*: ``ctx``, ``self._ctx`` and ``_bound_ctx()`` give a ctx name."""
    if isinstance(node, ast.Call):
        return _receiver_name(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _source_seams(source: str) -> set[str]:
    """The context seams *source* reaches, by attribute or by ``getattr``/``hasattr``."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {match.group(1) for match in _SEAM_TEXT.finditer(source)}
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in _CONTEXT_SEAMS
            and _CONTEXT_NAME.search(_receiver_name(node.value))
        ):
            found.add(node.attr)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "hasattr"}
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _CONTEXT_SEAMS
        ):
            found.add(node.args[1].value)
    return found


def _seam_level_uses(tree: Path) -> dict[str, int]:
    """Every App Kit seam above level 1 that the app tree at *tree* uses, with its level.

    The manifest's declarations (``agentTools`` and ``permissions.agentTurns`` /
    ``workerAutoApprove`` at level 2, ``credentialMasks`` at level 3,
    ``agentReadOnlyDirs``, an exact ``permissions.api`` entry,
    ``permissions.chatSlots``, ``permissions.desktopCapabilities`` and
    ``permissions.webhookSource`` at level 7, a directory entry in
    ``credentialMasks``, a shortcut's ``platformAccelerators`` and
    ``contributes.memoryProviders`` at level 8, and at level 10
    ``permissions.chatLaunch`` and any ``credentialMasks`` again, where the agent tool
    gate fences an installed app's masks as it fences the built-ins'), the context
    seams any non-test ``.py`` file reaches, the backend SDK package (level 7) any of
    them names, and, once one of them names the package, each backend SDK name
    published above level 7 that any of them mentions, at that name's level. A miss
    lets a pin adopt on a host without the seam, so the scan errs wide: a false hit
    only asks for a higher ``--min-sdk-level``.
    """
    raw = json.loads((tree / "app.json").read_text(encoding="utf-8"))
    permissions = raw.get("permissions")
    permissions = permissions if isinstance(permissions, dict) else {}
    uses: dict[str, int] = {}
    if "agentTools" in raw:
        uses["app.json agentTools"] = 2
    for flag in ("agentTurns", "workerAutoApprove"):
        if permissions.get(flag) is True:
            uses[f"app.json permissions.{flag}"] = 2
    masks = raw.get("credentialMasks")
    if masks:
        uses["app.json credentialMasks"] = 3
    if raw.get("agentReadOnlyDirs"):
        uses["app.json agentReadOnlyDirs"] = 7
    api = permissions.get("api")
    if isinstance(api, list) and any(
        isinstance(entry, str) and entry.strip().startswith(EXACT_API_ENTRY_MARK) for entry in api
    ):
        uses["app.json permissions.api exact entry"] = 7
    if permissions.get("chatSlots") is True:
        uses["app.json permissions.chatSlots"] = 7
    if permissions.get("desktopCapabilities"):
        uses["app.json permissions.desktopCapabilities"] = 7
    if permissions.get("webhookSource") is True:
        uses["app.json permissions.webhookSource"] = 7
    if isinstance(masks, list) and any(isinstance(m, str) and m.endswith("/") for m in masks):
        uses["app.json credentialMasks directory"] = 8
    desktop = raw.get("desktop")
    desktop = desktop if isinstance(desktop, dict) else {}
    shortcuts = desktop.get("shortcuts")
    if isinstance(shortcuts, list) and any(
        isinstance(s, dict) and "platformAccelerators" in s for s in shortcuts
    ):
        uses["app.json desktop.shortcuts platformAccelerators"] = 8
    contributes = raw.get("contributes")
    if isinstance(contributes, dict) and contributes.get("memoryProviders"):
        uses["app.json contributes.memoryProviders"] = 8
    if masks:
        # A former built-in's masks were on the tool gate's static rows; below level 10
        # the adopted copy's would be fenced by the OS sandbox only.
        uses["app.json credentialMasks tool gate"] = 10
    if "chatLaunch" in permissions:
        uses["app.json permissions.chatLaunch"] = 10
    later_sdk_names: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(tree):
        dirnames[:] = sorted(d for d in dirnames if d not in _UNSCANNED_DIRS)
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if path.suffix != ".py" or path.is_symlink():
                continue
            if filename.startswith("test_") or filename == "conftest.py":
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            for seam in _source_seams(source):
                uses.setdefault(f"ctx.{seam}", _CONTEXT_SEAMS[seam])
            if _BACKEND_SDK_ROOT in source and _BACKEND_SDK_LEAF.search(source):
                uses.setdefault(_BACKEND_SDK_MODULE, backend_sdk.BACKEND_SDK_LEVEL)
            if _BACKEND_SDK_LATER_NAME is not None:
                later_sdk_names.update(m.group(1) for m in _BACKEND_SDK_LATER_NAME.finditer(source))
    if _BACKEND_SDK_MODULE in uses:
        for name in sorted(later_sdk_names):
            uses[f"{_BACKEND_SDK_MODULE}.{name}"] = _BACKEND_SDK_LATER_NAMES[name]
    return uses


def _sdk_level_refusal(name: str, level: int, uses: dict[str, int]) -> str | None:
    """Why *level* cannot be the pin's ``minAppSdkLevel`` for a tree with *uses*."""
    needed = max(uses.values(), default=1)
    if level >= needed:
        return None
    evidence = ", ".join(sorted(seam for seam, seam_level in uses.items() if seam_level > level))
    return (
        f"{name}: minAppSdkLevel {level} is below App Kit seam level {needed}, which the "
        f"pinned tree uses ({evidence}); compute it with --min-sdk-level {needed} or higher"
    )


def _shipped(name: str) -> tuple[Path, dict[str, Any]] | None:
    from kiro_crew.apps.execution import _shipped_builtin_apps

    for root, manifest_name in _shipped_builtin_apps():
        if manifest_name == name:
            return root, json.loads((root / "app.json").read_text(encoding="utf-8"))
    return None


def _current_row(name: str) -> dict[str, Any]:
    """*name*'s row in the shipped table, the only record of the app once its wheel copy is gone."""
    document = json.loads(_PINS_PATH.read_text(encoding="utf-8"))
    if name not in parse_adoption_pins(document):
        raise SystemExit(f"{name}: no shipped built-in and no pin row of that name")
    return next(row for row in document["pins"] if row["name"] == name)


#: Where the wheel copies live, relative to the checkout root.
_BUILTINS_REL = "src/kiro_crew/apps/builtins"


def _git(*args: str) -> str:
    """Run git on this checkout and return its output."""
    return subprocess.run(
        ["git", *args],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        encoding="utf-8",
        timeout=120,
    ).stdout


def _deleted_copy(name: str) -> tuple[str, str]:
    """``(revision, package directory)`` of *name*'s wheel copy before the commit that deleted it.

    The revision is that commit's parent; the most recent deletion wins. The copy is
    found by its manifest ``name``, so no package directory is spelled here.
    """
    pathspec = f"{_BUILTINS_REL}/*/app.json"
    log = _git("log", "--diff-filter=D", "--name-only", "--format=commit %H", "--", pathspec)
    depth = _BUILTINS_REL.count("/") + 2
    commit = ""
    for line in log.splitlines():
        if line.startswith("commit "):
            commit = line.split()[1]
        elif commit and line.endswith("/app.json") and line.count("/") == depth:
            raw = json.loads(_git("show", f"{commit}^:{line}"))
            if isinstance(raw, dict) and raw.get("name") == name:
                return f"{commit}^", line.rsplit("/", 1)[0]
    raise SystemExit(
        f"{name}: this checkout has no commit that deletes a wheel copy of that name"
        " (a shallow clone has no history; fetch it, or commit the deletion first)"
    )


def _deleted_copy_root(name: str, dest: Path) -> Path:
    """*name*'s deleted wheel copy, its whole package directory, written under *dest*.

    Read from the revision :func:`_deleted_copy` finds and extracted by the gateway's own
    archive reader, which refuses a link or a path outside the package. The directory
    keeps the package's name, which the credential masks' built-in leg reads the app's
    name from.
    """
    revision, package = _deleted_copy(name)
    leaf = package.rsplit("/", 1)[1]
    tar = subprocess.run(
        ["git", "archive", "--format=tar", f"--prefix={leaf}/", f"{revision}:{package}"],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        timeout=120,
    ).stdout
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / f"{leaf}.tar.gz"
    archive.write_bytes(gzip.compress(tar))
    root = dest / leaf
    root.mkdir()
    fetch._extract_archive(archive, root, root=leaf)
    return root


def _frozen_deleted_copy(name: str) -> dict[str, Any]:
    """The ``deletedCopy`` of *name*'s wheel copy as the commit that deleted it found it."""
    with tempfile.TemporaryDirectory() as tmp:
        return freeze_deleted_copy(_deleted_copy_root(name, Path(tmp)))


def _frozen_copy(name: str) -> dict[str, Any]:
    """The ``deletedCopy`` *name*'s row freezes: from the shipped copy, else the deleted one."""
    shipped = _shipped(name)
    return freeze_deleted_copy(shipped[0]) if shipped is not None else _frozen_deleted_copy(name)


def _clone(repository: str, commit: str, dest: Path) -> None:
    """Check *commit* out into *dest* under the gateway's STAGE git posture.

    ``anonymous_git_env`` drops the system and global git config, so no
    ``core.autocrlf`` (Git for Windows' installer default) or LFS filter rewrites
    the checkout into a tree the runtime would not measure.
    """
    from kiro_crew.apps.registry_pipeline.subprocess_env import anonymous_git_env

    env = anonymous_git_env()
    subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", "init", "-q", str(dest)],
        check=True,
        encoding="utf-8",
        env=env,
        timeout=60,
    )
    for argv in (
        ["fetch", "-q", "--depth", "1", "--", repository, commit],
        ["checkout", "-q", "--detach", "FETCH_HEAD"],
    ):
        subprocess.run(
            ["git", "-c", f"core.hooksPath={os.devnull}", *argv],
            cwd=dest,
            check=True,
            encoding="utf-8",
            env=env,
            timeout=300,
        )


def _download(url: str, dest: Path) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "kiro-crew-adoption-pin"})
    digest = hashlib.sha256()
    with fetch._open_archive(request) as response, open(dest, "wb") as out:
        while chunk := response.read(1 << 20):
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()


def _measure(pin: AdoptionPin, work: Path) -> dict[str, Any]:
    """Fetch *pin* both ways under *work* and return the row it should be."""
    from kiro_crew.apps.manifest import AppManifest

    checkout = work / "checkout"
    _clone(pin.repository, pin.commit, checkout)
    archive = work / "archive.tar.gz"
    archive_sha = _download(pin.archive_url, archive)
    pin = AdoptionPin(**{**pin.__dict__, "archive_sha256": archive_sha})
    git_tree = fetch.adoption_tree_digest(checkout)
    extracted = work / "extracted"
    extracted.mkdir()
    fetch._extract_archive(archive, extracted, root=pin.archive_root)
    archive_tree = fetch.adoption_tree_digest(extracted)
    if git_tree != archive_tree:
        raise SystemExit(f"{pin.name}: git and archive trees differ ({git_tree} != {archive_tree})")
    manifest = AppManifest.from_json_file(extracted / "app.json")
    shipped = _shipped(pin.name)
    deleted_copy: dict[str, Any] | None = None
    frozen_servers: Any = None
    if shipped is None:
        current = _current_row(pin.name)
        page = current["legacyNav"]
        shipped_manifest = None
        deleted_copy = current.get(DELETED_COPY_KEY)
        if deleted_copy is None:
            raise SystemExit(
                f"{pin.name}: no shipped built-in and the row carries no {DELETED_COPY_KEY};"
                " print it with `adoption_pin.py freeze` and add it to the row"
            )
        baseline = deleted_copy
        frozen_servers = deleted_copy.get("mcpServers")
    else:
        page = (shipped[1].get("ui", {}).get("pages") or [{}])[0]
        shipped_manifest = AppManifest.from_json_file(shipped[0] / "app.json")
        baseline = freeze_grant_inputs(shipped[1])
    refusal = pinned_tree_refusal(
        pin,
        manifest,
        extracted,
        shipped_manifest=shipped_manifest,
        frozen_servers=frozen_servers,
    )
    if refusal:
        raise SystemExit(f"{pin.name}: the pinned tree is refused: {refusal}")
    sdk_refusal = _sdk_level_refusal(pin.name, pin.min_app_sdk_level, _seam_level_uses(extracted))
    if sdk_refusal:
        raise SystemExit(sdk_refusal)
    widening = widening_refusal(manifest, deleted_copy_baseline(baseline))
    if widening:
        copy = "shipped" if shipped is not None else "deleted"
        raise SystemExit(
            f"{pin.name}: the pinned tree declares more than the {copy} copy ({widening})"
        )
    row = {
        "name": pin.name,
        "repository": pin.repository,
        "commit": pin.commit,
        "version": manifest.version,
        "treeSha256": git_tree,
        "archiveSha256": archive_sha,
        "minAppSdkLevel": pin.min_app_sdk_level,
        "legacyNav": {"route": page.get("route", ""), "icon": page.get("icon", "")},
    }
    if deleted_copy is not None:
        row[DELETED_COPY_KEY] = deleted_copy
    return row


def _draft_pin(
    repository: str, commit: str, name: str, version: str, min_sdk_level: int
) -> AdoptionPin:
    row = {
        "name": name,
        "repository": repository,
        "commit": commit,
        "version": version,
        "treeSha256": "0" * 64,
        "archiveSha256": "0" * 64,
        "minAppSdkLevel": min_sdk_level,
        "legacyNav": {"route": "/x", "icon": "X"},
    }
    pins = parse_adoption_pins({"schemaVersion": 1, "pins": [row]})
    if name not in pins:
        raise SystemExit(
            "repository, commit or version has the wrong shape (see adoption-pins.json rules)"
        )
    return pins[name]


def _compute(repository: str, commit: str, min_sdk_level: int = 1) -> int:
    if not 1 <= min_sdk_level <= APP_SDK_LEVEL:
        raise SystemExit(
            f"--min-sdk-level must be between 1 and this build's App Kit level {APP_SDK_LEVEL}"
        )
    if not _REPOSITORY_RE.fullmatch(repository) or repository.endswith(".git"):
        raise SystemExit("repository must be https://github.com/<owner>/<repo>, without .git")
    if not _COMMIT_RE.fullmatch(commit):
        raise SystemExit("commit must be a full 40-character lowercase sha")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        probe = work / "probe"
        _clone(repository, commit, probe)
        raw = json.loads((probe / "app.json").read_text(encoding="utf-8"))
        # The probe stays for the TemporaryDirectory cleanup, which clears the
        # read-only bit git sets on its objects (a bare rmtree fails on Windows).
        draft = _draft_pin(repository, commit, raw["name"], raw["version"], min_sdk_level)
        row = _measure(draft, work)
    print(json.dumps(row, indent=2))
    return 0


def _freeze(name: str) -> int:
    """Print *name*'s current row with the ``deletedCopy`` its wheel copy freezes."""
    row = {**_current_row(name), DELETED_COPY_KEY: _frozen_copy(name)}
    print(json.dumps(row, indent=2))
    return 0


def _verify() -> int:
    document = json.loads(_PINS_PATH.read_text(encoding="utf-8"))
    pins = parse_adoption_pins(document)
    if len(pins) != len(document.get("pins", [])):
        print("a row of the shipped pin file does not validate", file=sys.stderr)
        return 1
    rows = {row["name"]: row for row in document["pins"]}
    failed = 0
    for name, pin in sorted(pins.items()):
        with tempfile.TemporaryDirectory() as tmp:
            row = _measure(pin, Path(tmp))
        expected = {
            "version": pin.version,
            "treeSha256": pin.tree_sha256,
            "archiveSha256": pin.archive_sha256,
            "legacyNav": {"route": pin.legacy_route, "icon": pin.legacy_icon},
            # Carried from the row only while no wheel copy ships, so a row whose copy
            # ships and that carries one differs here.
            DELETED_COPY_KEY: rows[name].get(DELETED_COPY_KEY),
        }
        actual = {key: row.get(key) for key in expected}
        if actual == expected and row.get(DELETED_COPY_KEY) is not None:
            # What the row froze must be the copy the deletion removed, no wider.
            actual[DELETED_COPY_KEY] = _frozen_deleted_copy(name)
        if actual != expected:
            print(f"{name}: shipped {expected} but measured {actual}", file=sys.stderr)
            failed += 1
        else:
            print(f"{name}: ok")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    compute = sub.add_parser("compute")
    compute.add_argument("repository")
    compute.add_argument("commit")
    compute.add_argument(
        "--min-sdk-level",
        type=int,
        default=1,
        metavar="N",
        help="the row's minAppSdkLevel; required at or above the highest seam level the tree uses",
    )
    freeze = sub.add_parser("freeze")
    freeze.add_argument("name")
    sub.add_parser("verify")
    args = parser.parse_args(argv)
    if args.command == "compute":
        return _compute(args.repository, args.commit, args.min_sdk_level)
    if args.command == "freeze":
        return _freeze(args.name)
    return _verify()


if __name__ == "__main__":
    raise SystemExit(main())
