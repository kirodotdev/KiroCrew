"""Shared helpers for the owner-tier store-art prewarm tests.

``_pin_registry`` pins one registry through the edition seam, ``_entry`` builds an
index row, ``_fake_fetch`` stands in for ``_git_fetch_branch`` and lays a tree into
the clone, ``_route_cache_path`` reproduces where the blob proxy reads a URL, and
``_await_barrier`` / ``_settled`` bound a wait by the lost-run ceiling.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from kiro_crew.apps import registry as reg_mod
from kiro_crew.apps.registry import (
    _blob_cache_dir,
    _blob_cache_key,
    _effective_registries,
    _entry_git_url,
)
from kiro_crew.config import loader as loader_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.defaults import DefaultAppsLoader

FORGE = "https://forge.example.com/org/app-registry.git"
SIBLING = "https://forge.example.com/org/some-private-app.git"
OTHER_HOST = "https://elsewhere.example.net/org/some-private-app.git"

MANIFEST: dict[str, Any] = {
    "name": "app",
    "displayName": "App",
    "description": "An app.",
    "iconPath": "ui/icon.svg",
    "heroImage": "ui/hero.png",
    "screenshots": ["ui/shot-1.png", "ui/shot-2.png"],
}

ART: dict[str, bytes] = {
    "ui/icon.svg": b"<svg/>",
    "ui/hero.png": b"\x89PNG hero",
    "ui/shot-1.png": b"\x89PNG one",
    "ui/shot-2.png": b"\x89PNG two",
}


class _Loader(DefaultAppsLoader):
    def __init__(self, rows) -> None:
        self._rows = rows

    def default_registries(self):
        return self._rows


def _pin_registry(monkeypatch, trust: str) -> Any:
    """Pin one registry at *trust* through the edition seam; return its config row."""
    cfg = KiroCrewConfig()
    cfg.registries = []
    monkeypatch.setattr(loader_mod.KiroCrewConfig, "load", staticmethod(lambda: cfg))
    base = build_default_context(KiroCrewConfig())
    ctx = dataclasses.replace(
        base,
        apps_loader=_Loader([{"name": "official", "repo": FORGE, "trust": trust}]),
    )
    monkeypatch.setattr(reg_mod, "current_context", lambda: ctx)
    (reg,) = _effective_registries()
    return reg


def _entry(**overrides: Any) -> dict[str, Any]:
    row = {
        "name": "app",
        "gitUrl": SIBLING,
        "repo": SIBLING,
        "branch": "main",
        "_registry": "official",
    }
    row.update(overrides)
    return row


def _fake_fetch(monkeypatch, tree: dict[str, bytes], manifest: dict[str, Any] | None = MANIFEST):
    """Stand in for ``_git_fetch_branch``: lay *tree* (and app.json) into ``dest``.

    Patched through the facade so every holder -- including ``store_art`` -- sees it.
    Returns the list of recorded calls.
    """
    calls: list[dict[str, Any]] = []

    async def _fetch(git_url, branch, dest, log_lines, *, clone_env, sandbox_mode, **kw):
        calls.append(
            {
                "git_url": git_url,
                "branch": branch,
                "dest": Path(dest),
                "clone_env": dict(clone_env),
                "sandbox_mode": sandbox_mode,
                "mask_local_git_config": kw.get("mask_local_git_config", False),
                "forward_ssh_auth_sock": kw.get("forward_ssh_auth_sock", False),
            }
        )
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        if manifest is not None:
            (dest / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
        for rel, data in tree.items():
            path = dest / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return None

    monkeypatch.setattr(reg_mod, "_git_fetch_branch", _fetch)
    return calls


def _route_cache_path(entry: dict[str, Any], merged_url: str) -> Path:
    """Where ``handle_blob_proxy`` looks for the URL ``_merge_manifest`` emitted.

    Reproduces the route's own resolution -- ``repo`` and ``path`` from the query,
    ``ref`` from the row's branch (the URL carries none), ``clone_url`` from the row --
    so the test asserts against what the proxy DOES with the URL, not against the
    prewarm's idea of it.
    """
    query = parse_qs(urlparse(merged_url).query)
    (repo,) = query["repo"]
    (path,) = query["path"]
    ref = entry.get("branch", "main")
    return _blob_cache_dir() / _blob_cache_key(repo, _entry_git_url(entry)) / ref / path


#: Lost-run ceiling on a barrier the production path always reaches (a fetch held
#: in flight, the copy worker entering, a cancelled row's cleanup). The wait returns
#: the moment the state is reached; the slowest path to these barriers is the
#: pre-batch and pre-copy thread hops, a few seconds with every executor job started
#: late. Reaching the ceiling fails naming the barrier, and it is a quarter of the
#: suite's 120 s ``--timeout``, so a miss is a readable failure, not a killed worker.
_BARRIER_CEILING_SECS = 30.0


async def _await_barrier(reached: Any, what: str, state: Any = lambda: "") -> None:
    """Wait for *reached* (an ``asyncio.Event``) within the ceiling, failing by name.

    Call it from the TEST, never from a fake the prewarm awaits: ``_worker`` logs and
    swallows any exception a row raises, ``pytest.fail`` included.
    """
    import asyncio

    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        await asyncio.wait_for(reached.wait(), timeout=_BARRIER_CEILING_SECS)
    except asyncio.TimeoutError:
        pytest.fail(
            f"{what} not reached after {loop.time() - started:.1f}s "
            f"(lost-run ceiling {_BARRIER_CEILING_SECS:.0f}s); {state()}"
        )


async def _settled(aw: Any, what: str) -> Any:
    """Await *aw* within the lost-run ceiling; past it fail naming ``what`` and the wait."""
    import asyncio

    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        return await asyncio.wait_for(aw, timeout=_BARRIER_CEILING_SECS)
    except asyncio.TimeoutError:
        pytest.fail(
            f"{what} still running after {loop.time() - started:.1f}s "
            f"(lost-run ceiling {_BARRIER_CEILING_SECS:.0f}s)"
        )
