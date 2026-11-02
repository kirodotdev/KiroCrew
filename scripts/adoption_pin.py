#!/usr/bin/env python3
"""Release-captain tool for ``src/kiro_crew/apps/adoption-pins.json``.

    adoption_pin.py compute <repository-url> <commit>   print the pin row for that commit
    adoption_pin.py verify                              re-check every row of the shipped file

``compute`` clones the repository at the commit AND downloads its codeload archive,
computes ``treeSha256`` on both (they must match) and ``archiveSha256``, and runs
the adoption tree checks against the shipped built-in of the same name. The
``legacyNav`` route and icon are read from the shipped built-in's first page.
``verify`` does the same for every row and fails on any difference.

Networked: run by hand or in a network-allowed release job, never in the offline
test suite. The digest and the checks are the gateway's own
(``kiro_crew.apps.builtin_adoption_fetch`` / ``builtin_adoption_checks``), so the
tool and the runtime cannot disagree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from kiro_crew.apps import builtin_adoption_fetch as fetch  # noqa: E402
from kiro_crew.apps.builtin_adoption_checks import pinned_tree_refusal  # noqa: E402
from kiro_crew.apps.builtin_adoption_pins import (  # noqa: E402
    _PINS_PATH,
    AdoptionPin,
    parse_adoption_pins,
)


def _shipped(name: str) -> tuple[Path, dict[str, Any]] | None:
    from kiro_crew.apps.execution import _shipped_builtin_apps

    for root, manifest_name in _shipped_builtin_apps():
        if manifest_name == name:
            return root, json.loads((root / "app.json").read_text(encoding="utf-8"))
    return None


def _clone(repository: str, commit: str, dest: Path) -> None:
    subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", "init", "-q", str(dest)],
        check=True,
        encoding="utf-8",
        timeout=60,
    )
    for argv in (
        ["fetch", "-q", "--depth", "1", repository, commit],
        ["checkout", "-q", "--detach", "FETCH_HEAD"],
    ):
        subprocess.run(
            ["git", "-c", f"core.hooksPath={os.devnull}", *argv],
            cwd=dest,
            check=True,
            encoding="utf-8",
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


def _draft_pin(repository: str, commit: str, name: str, version: str) -> AdoptionPin:
    row = {
        "name": name,
        "repository": repository,
        "commit": commit,
        "version": version,
        "treeSha256": "0" * 64,
        "archiveSha256": "0" * 64,
        "minAppSdkLevel": 1,
        "legacyNav": {"route": "/x", "icon": "X"},
    }
    pins = parse_adoption_pins({"schemaVersion": 1, "pins": [row]})
    if name not in pins:
        raise SystemExit(
            "repository, commit or version has the wrong shape (see adoption-pins.json rules)"
        )
    return pins[name]


def _compute(repository: str, commit: str) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        probe = work / "probe"
        _clone(repository, commit, probe)
        raw = json.loads((probe / "app.json").read_text(encoding="utf-8"))
        shutil.rmtree(probe)
        row = _measure(_draft_pin(repository, commit, raw["name"], raw["version"]), work)
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
    sub.add_parser("verify")
    args = parser.parse_args(argv)
    if args.command == "compute":
        return _compute(args.repository, args.commit)
    return _verify()


if __name__ == "__main__":
    raise SystemExit(main())
