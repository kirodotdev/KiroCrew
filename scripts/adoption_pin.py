#!/usr/bin/env python3
"""Release-captain tool for ``src/kiro_crew/apps/adoption-pins.json``.

    adoption_pin.py compute <repository-url> <commit> [--min-sdk-level N]
                                                        print the pin row for that commit
    adoption_pin.py verify                              re-check every row of the shipped file

``compute`` clones the repository at the commit AND downloads its codeload archive,
computes ``treeSha256`` on both (they must match) and ``archiveSha256``, and runs
the adoption tree checks against the shipped built-in of the same name. The
``legacyNav`` route and icon are read from the shipped built-in's first page.
``verify`` does the same for every row and fails on any difference.

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

from kiro_crew.apps import builtin_adoption_fetch as fetch  # noqa: E402
from kiro_crew.apps.app_sdk_level import APP_SDK_LEVEL  # noqa: E402
from kiro_crew.apps.builtin_adoption_checks import pinned_tree_refusal  # noqa: E402
from kiro_crew.apps.builtin_adoption_pins import (  # noqa: E402
    _COMMIT_RE,
    _PINS_PATH,
    _REPOSITORY_RE,
    AdoptionPin,
    parse_adoption_pins,
)

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
}

#: A receiver whose last name says it is the app's context (``ctx``, ``self._ctx``,
#: ``context``, ``app_ctx``). ``getattr``/``hasattr`` of a seam name counts on any
#: receiver, since an app reaches its context through helpers (``getattr(bound(), ...)``).
_CONTEXT_NAME = re.compile(r"ctx|context", re.IGNORECASE)

#: The text form, for a source file this interpreter cannot parse.
_SEAM_TEXT = re.compile(r"[.\"'](%s)\b" % "|".join(_CONTEXT_SEAMS))

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
    ``workerAutoApprove`` at level 2, ``credentialMasks`` at level 3) and the
    context seams any non-test ``.py`` file reaches. A miss lets a pin adopt on a
    host without the seam, so the scan errs wide: a false hit only asks for a
    higher ``--min-sdk-level``.
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
    if raw.get("credentialMasks"):
        uses["app.json credentialMasks"] = 3
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
    if shipped is None:
        raise SystemExit(f"{pin.name}: no shipped built-in of that name")
    shipped_root, shipped_raw = shipped
    refusal = pinned_tree_refusal(
        pin,
        manifest,
        extracted,
        shipped_manifest=AppManifest.from_json_file(shipped_root / "app.json"),
    )
    if refusal:
        raise SystemExit(f"{pin.name}: the pinned tree is refused: {refusal}")
    sdk_refusal = _sdk_level_refusal(pin.name, pin.min_app_sdk_level, _seam_level_uses(extracted))
    if sdk_refusal:
        raise SystemExit(sdk_refusal)
    page = (shipped_raw.get("ui", {}).get("pages") or [{}])[0]
    return {
        "name": pin.name,
        "repository": pin.repository,
        "commit": pin.commit,
        "version": manifest.version,
        "treeSha256": git_tree,
        "archiveSha256": archive_sha,
        "minAppSdkLevel": pin.min_app_sdk_level,
        "legacyNav": {"route": page.get("route", ""), "icon": page.get("icon", "")},
    }


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


def _verify() -> int:
    document = json.loads(_PINS_PATH.read_text(encoding="utf-8"))
    pins = parse_adoption_pins(document)
    if len(pins) != len(document.get("pins", [])):
        print("a row of the shipped pin file does not validate", file=sys.stderr)
        return 1
    failed = 0
    for name, pin in sorted(pins.items()):
        with tempfile.TemporaryDirectory() as tmp:
            row = _measure(pin, Path(tmp))
        expected = {
            "version": pin.version,
            "treeSha256": pin.tree_sha256,
            "archiveSha256": pin.archive_sha256,
            "legacyNav": {"route": pin.legacy_route, "icon": pin.legacy_icon},
        }
        actual = {key: row[key] for key in expected}
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
    sub.add_parser("verify")
    args = parser.parse_args(argv)
    if args.command == "compute":
        return _compute(args.repository, args.commit, args.min_sdk_level)
    return _verify()


if __name__ == "__main__":
    raise SystemExit(main())
