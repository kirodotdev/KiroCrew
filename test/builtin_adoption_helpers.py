"""Shared fixtures for the former built-in adoption tests (``test_builtin_adoption_*``).

A fixture app, ``fixture-former``, stands in for a shipped built-in: a wheel
package directory with its ``app.json`` that ``execution._shipped_builtin_apps``
reports, a builtin-owned record in the isolated data home, and the "repository"
copy of the same app in a local bare git repository plus a codeload-shaped
``.tar.gz`` of the same commit. Nothing opens a socket: the git fetch clones the
local bare repository with ``file://``, and the archive download is served from
memory through ``builtin_adoption_fetch._open_archive``.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps import builtin_adoption_fetch as fetch_mod
from kiro_crew.apps import builtin_adoption_pins as pins_mod
from kiro_crew.apps import execution as exec_mod
from kiro_crew.apps import manager
from kiro_crew.apps.builtin_adoption_fetch import adoption_tree_digest

NAME = "fixture-former"
PACKAGE = "fixture_former"
OWNER = "FixtureOrg"
REPO = "kirocrew-app-fixture-former"
REPOSITORY = f"https://github.com/{OWNER}/{REPO}"
VERSION = "0.1.0"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs a git binary")


def git(*args: str, cwd: Path) -> str:
    """Run git in a fixture repo, failing loudly on error."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=60,
        env={**os.environ, **_GIT_ENV},
    ).stdout.strip()


def shipped_manifest(**overrides: Any) -> dict[str, Any]:
    """The wheel built-in's ``app.json``."""
    manifest: dict[str, Any] = {
        "name": NAME,
        "version": VERSION,
        "displayName": "Fixture Former",
        "description": "A former built-in used by the adoption tests.",
        "author": "kirocrew",
        "defaultEnabled": False,
        "platform": {"os": ["macos", "linux", "windows"]},
        "permissions": {"api": [f"/api/apps/{NAME}", f"/api/apps/{NAME}/*"], "storage": True},
        "ui": {"pages": [{"route": f"/{NAME}", "label": "Fixture Former", "icon": "Box"}]},
    }
    manifest.update(overrides)
    return manifest


def repository_manifest(**overrides: Any) -> dict[str, Any]:
    """The repository copy's ``app.json``: same identity and permissions, own UI bundle."""
    manifest = shipped_manifest()
    manifest.pop("ui")
    manifest["ui"] = {
        "entry": "dist/index.mjs",
        "pages": [{"route": f"/apps/{NAME}", "label": "F"}],
    }
    manifest.update(overrides)
    return manifest


def write_tree(root: Path, manifest: dict[str, Any], extra: dict[str, str] | None = None) -> Path:
    """An app tree at *root*: ``app.json``, a UI bundle and one backend file."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (root / "ui" / "dist").mkdir(parents=True, exist_ok=True)
    (root / "ui" / "dist" / "index.mjs").write_text("export default () => null\n", encoding="utf-8")
    (root / "backend").mkdir(exist_ok=True)
    (root / "backend" / "routes.py").write_text("ROUTES = []\n", encoding="utf-8")
    for rel, text in (extra or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def make_bare_repo(base: Path, tree: Path) -> tuple[Path, str]:
    """Commit *tree* into a fresh bare repository under *base*; ``(bare, commit)``."""
    work = base / "work"
    shutil.copytree(tree, work)
    git("init", "-q", "-b", "main", ".", cwd=work)
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "app", cwd=work)
    commit = git("rev-parse", "HEAD", cwd=work)
    bare = base / "remote.git"
    git("clone", "-q", "--bare", str(work), str(bare), cwd=base)
    return bare, commit


def make_archive(tree: Path, commit: str) -> bytes:
    """A codeload-shaped ``.tar.gz`` of *tree*: every member under ``<repo>-<commit>/``."""
    buffer = io.BytesIO()
    root = f"{REPO}-{commit}"
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(root)
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        tar.addfile(info)
        for path in sorted(tree.rglob("*")):
            rel = path.relative_to(tree).as_posix()
            if rel == ".git" or rel.startswith(".git/"):
                continue
            info = tarfile.TarInfo(f"{root}/{rel}")
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
                continue
            data = path.read_bytes()
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def pin_row(head: str, tree_sha: str, archive_sha: str, **overrides: Any) -> dict[str, Any]:
    """A pin row for the fixture app at commit *head*; *overrides* replace fields."""
    row: dict[str, Any] = {
        "name": NAME,
        "repository": REPOSITORY,
        "commit": head,
        "version": VERSION,
        "treeSha256": tree_sha,
        "archiveSha256": archive_sha,
        "minAppSdkLevel": 1,
        "legacyNav": {"route": f"/{NAME}", "icon": "Box"},
    }
    row.update(overrides)
    return row


def host_with_seams(monkeypatch: pytest.MonkeyPatch, level: int = 1) -> None:
    """Report the App Kit seam level a pinned app needs (``app_sdk_level.APP_SDK_LEVEL``)."""
    from kiro_crew.apps import app_sdk_level

    monkeypatch.setattr(app_sdk_level, "APP_SDK_LEVEL", level)


def install_pins(monkeypatch: pytest.MonkeyPatch, path: Path, rows: list[dict[str, Any]]) -> None:
    """Repoint the pin table at a file holding *rows*."""
    path.write_text(json.dumps({"schemaVersion": 1, "pins": rows}), encoding="utf-8")
    monkeypatch.setattr(pins_mod, "_PINS_PATH", path)
    pins_mod.load_adoption_pins.cache_clear()


@dataclass
class World:
    """The fixture app on one host: wheel copy, record, repository, archive."""

    tmp: Path
    home: Path
    wheel: Path
    tree: Path
    bare: Path
    commit: str
    archive: bytes
    git_calls: list[str] = field(default_factory=list)
    archive_calls: list[str] = field(default_factory=list)
    archive_body: bytes | None = None
    archive_error: BaseException | None = None
    git_outcome: str = ""

    @property
    def archive_sha(self) -> str:
        return hashlib.sha256(self.archive).hexdigest()

    async def git_fetch(self, pin: Any, dest: Path) -> str:
        """The STAGE git seam: clone the local bare repo and check out the pin commit."""
        self.git_calls.append(pin.commit)
        if self.git_outcome:
            return self.git_outcome

        def _clone() -> None:
            if dest.exists():
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            git("clone", "-q", self.bare.as_uri(), str(dest), cwd=self.tmp)
            git("checkout", "-q", "--detach", pin.commit, cwd=dest)

        await asyncio.to_thread(_clone)
        return ""

    def open_archive(self, request: Any) -> Any:
        """The archive download seam, served from memory."""
        self.archive_calls.append(request.full_url)
        if self.archive_error is not None:
            raise self.archive_error
        return io.BytesIO(self.archive if self.archive_body is None else self.archive_body)


def write_builtin_record(
    *, enabled: bool = True, data: str = "kept", secret: bytes = b"s3cret"
) -> None:
    """The record ``register_builtin_apps`` leaves for the fixture app, with data and a secret."""
    dest = manager.app_dir(NAME)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "app.json").write_text(json.dumps(shipped_manifest(), indent=2), encoding="utf-8")
    (dest / "data").mkdir(exist_ok=True)
    (dest / "data" / "store.txt").write_text(data, encoding="utf-8")
    (dest / ".app_secret").write_bytes(secret)
    meta = manager.InstalledApp(
        name=NAME,
        version=VERSION,
        displayName="Fixture Former",
        enabled=enabled,
        source="builtin",
        origin="builtin",
        resources="gateway",
        lifecycle="locked",
    )
    manager._write_installed(NAME, meta)


def build_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **repo_overrides: Any) -> World:
    """Wheel copy + repository + archive + pins on a host with the seams, under *tmp_path*."""
    home = Path(os.environ["KIROCREW_HOME"])
    wheel_root = tmp_path / "wheel"
    wheel = write_tree(wheel_root / PACKAGE, shipped_manifest())
    monkeypatch.setattr(exec_mod, "_builtin_manifest_sources", lambda: (wheel_root.resolve(),))
    tree = write_tree(tmp_path / "repo-tree", repository_manifest(**repo_overrides))
    bare, commit = make_bare_repo(tmp_path / "git", tree)
    archive = make_archive(tree, commit)
    world = World(
        tmp=tmp_path, home=home, wheel=wheel, tree=tree, bare=bare, commit=commit, archive=archive
    )
    install_pins(
        monkeypatch,
        tmp_path / "adoption-pins.json",
        [pin_row(commit, adoption_tree_digest(tree), world.archive_sha)],
    )
    monkeypatch.setattr(fetch_mod, "_git_fetch", world.git_fetch)
    monkeypatch.setattr(fetch_mod, "_open_archive", world.open_archive)
    from kiro_crew.apps import builtin_adoption_apply as apply_mod

    monkeypatch.setattr(apply_mod, "_git_available", lambda: True)
    host_with_seams(monkeypatch)
    return world


def agent_section(home: Path) -> dict[str, Any]:
    path = home / "config.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("agent", {})


def seed_agent(home: Path, agent: dict[str, Any]) -> None:
    from kiro_crew.config.loader import _invalidate_config_cache

    path = home / "config.json"
    raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    raw.setdefault("agent", {}).update(agent)
    path.write_text(json.dumps(raw), encoding="utf-8")
    _invalidate_config_cache()
