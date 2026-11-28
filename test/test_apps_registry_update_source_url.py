"""An installed app updates only from the exact URL it was installed from.

A git server may serve different repositories at ``/app.git`` and ``/app``. The
end-to-end case serves both, with different contents, through ``git http-backend``
on loopback, installs the app from one, switches the registry row to the other and
runs the real update: it is refused before any fetch, pull or ``setup.onInstall``.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from apps_registry_helpers import _catalog_absent  # noqa: F401 - fixture
from apps_registry_helpers import _explicit_registry_execution_admission  # noqa: F401 - fixture
from apps_registry_helpers import unsandboxed_spawn  # noqa: F401 - fixture

from kiro_crew.apps import registry
from kiro_crew.apps.registry_pipeline import checkout, manifests
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None or os.name == "nt", reason="needs git and /bin/bash")


class _GitHttpBackend(http.server.BaseHTTPRequestHandler):
    """Smart HTTP over the bare repositories under ``root``, recording each path."""

    root: Path
    paths: list[str]

    def _serve(self) -> None:
        self.paths.append(self.path)
        path, _, query = self.path.partition("?")
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        env = {
            "PATH": os.environ.get("PATH", ""),
            "GIT_PROJECT_ROOT": str(self.root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(self.root),
            "REQUEST_METHOD": self.command,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
        }
        for name in ("Content-Encoding", "Git-Protocol"):
            if self.headers.get(name):
                env["HTTP_" + name.upper().replace("-", "_")] = self.headers[name]
        out = subprocess.run(
            [GIT, "http-backend"],
            input=body,
            capture_output=True,
            env=env,
            timeout=FALLBACK_TIMEOUT_SECS,
        ).stdout
        head, _, payload = out.partition(b"\r\n\r\n")
        status, headers = 200, []
        for line in head.decode("latin-1").split("\r\n"):
            key, _, value = line.partition(":")
            if key.lower() == "status":
                status = int(value.split()[0])
            elif key:
                headers.append((key, value.strip()))
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _serve

    def log_message(self, *args):
        pass


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        [GIT, *args], cwd=cwd, check=True, capture_output=True, timeout=FALLBACK_TIMEOUT_SECS
    )


def _bare_app_repo(root: Path, name: str, marker: str) -> None:
    """A bare repository *name* under *root* whose ``onInstall`` writes *marker*.

    Each call adds one commit to the same work tree, so a later repository is a
    fast-forward of an earlier one, which is what lets ``git pull --ff-only`` take it.
    """
    work = root.parent / "work"
    if not work.is_dir():
        work.mkdir()
        _git("init", "-q", "-b", "main", cwd=work)
    manifest = {
        "name": "demo",
        "version": "1.0.0",
        "displayName": "Demo",
        "description": "demo",
        "setup": {"onInstall": f"printf {marker} > onInstall-{marker}"},
    }
    (work / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
    _git("add", "app.json", cwd=work)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", marker, cwd=work)
    _git("clone", "-q", "--bare", str(work), str(root / name), cwd=root.parent)


@pytest.fixture
def two_repositories(tmp_path):
    """``/owner/app.git`` and ``/owner/app``: two repositories with different heads.

    The second is a fast-forward of the first, so only the source pin stops a pull.
    """
    root = tmp_path / "srv"
    (root / "owner").mkdir(parents=True)
    _bare_app_repo(root / "owner", "app.git", "first")
    _bare_app_repo(root / "owner", "app", "second")
    handler = type("_Handler", (_GitHttpBackend,), {"root": root, "paths": []})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/owner/app"
    yield base, handler.paths
    server.shutdown()


@pytest.mark.asyncio
async def test_an_update_listed_at_another_spelling_runs_no_fetch_and_no_hook(
    two_repositories, tmp_path, monkeypatch
):
    base, paths = two_repositories
    first_url, second_url = f"{base}.git", base
    for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    hooks_dir = home  # the hook writes into the app checkout, under the data home
    # The loopback server stands in for a forge the clone-host check allows.
    for module in (checkout, manifests):
        if hasattr(module, "is_clone_host_trusted"):
            monkeypatch.setattr(module, "is_clone_host_trusted", lambda url: True)
    row = {"name": "demo", "gitUrl": first_url, "branch": "main"}
    monkeypatch.setattr(registry, "_registry_app_candidates", lambda name: [dict(row)])
    monkeypatch.setattr(registry, "get_registry_app", lambda name: dict(row))
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    from kiro_crew.config.loader import _invalidate_config_cache

    # Trust as the consent endpoint records it for the first repository.
    (home / "config.json").write_text(
        json.dumps(
            {
                "agent": {
                    "apps_trusted": ["demo"],
                    "apps_trusted_repositories": {
                        "demo": registry._normalize_git_target(first_url)
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    _invalidate_config_cache()

    installed = await registry.install_from_registry("demo")
    assert installed.get("ok") is True, installed
    assert registry.get_app("demo")["sourceUrl"] == first_url
    ran = sorted({p.name for p in hooks_dir.rglob("onInstall-*")})
    assert ran == ["onInstall-first"], ran

    # The registry now lists the other repository under the same app name.
    row["gitUrl"] = second_url
    paths.clear()
    updated = await registry.install_from_registry("demo")

    assert updated["ok"] is False
    assert "recorded source" in updated["error"]
    assert paths == []  # nothing was fetched from either repository
    assert not list(hooks_dir.rglob("onInstall-second"))
    assert registry.get_app("demo")["sourceUrl"] == first_url


@pytest.mark.parametrize(
    ("recorded", "listed", "same"),
    [
        ("https://forge.example/o/app.git", "https://forge.example/o/app.git", True),
        ("https://forge.example/o/app.git", "https://u:token@forge.example/o/app.git", True),
        ("https://forge.example/o/app.git", "https://forge.example/o/app", False),
        ("https://forge.example/o/app", "https://forge.example/o/app.git", False),
        ("https://forge.example/o/app", "https://forge.example/o/app/", False),
        ("https://forge.example/o/app.git", "https://forge.example/o/app.git/", False),
        ("https://forge.example/o/app", "https://forge.example/o/App", False),
        ("https://forge.example/o/app", "https://Forge.example/o/app", False),
        ("https://forge.example/o/app.git", "git@forge.example:o/app.git", False),
    ],
)
def test_the_recorded_source_matches_only_its_own_url_text(monkeypatch, recorded, listed, same):
    monkeypatch.setattr(
        registry, "_registry_app_candidates", lambda name: [{"name": "demo", "gitUrl": listed}]
    )

    entry = registry._pinned_registry_entry("demo", {"sourceUrl": recorded})

    assert (entry is not None) is same
