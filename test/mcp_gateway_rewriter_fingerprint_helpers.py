"""Shared fixtures and helpers for the rewrite-fingerprint tests.

Both ``test_mcp_gateway_rewriter_fingerprint*.py`` files import them. ``_CMD`` is the
interpreter every fixture server runs, ``_mk_tree`` builds an agents and settings tree,
``_rewrite`` runs ``rewrite_agents`` over it, ``rewrite_counter`` counts the per-spec
rewrites, ``_bump_mtime`` bumps a file's mtime, ``_settings_unreadable`` faults both reads
of the settings file, and the autouse ``_forward_declared_env_on`` pins declared-env
forwarding on and the pool identity set empty in every module that imports it.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.mcp_gateway import rewriter
from kiro_crew.mcp_gateway.rewriter import rewrite_agents

# The running interpreter: absolute, exists, and executable on every CI
# platform, so the fixture passes the resolver's absolute-path exec check
# and the ``shutil.which`` bare-name path is never entered.
_CMD = sys.executable


def _mk_tree(
    root: Path,
    *,
    n_agents: int = 2,
    with_env: bool = True,
    env: dict[str, Any] | None = None,
) -> Path:
    src = root / "agents"
    src.mkdir(parents=True, exist_ok=True)
    settings = root / "settings"
    settings.mkdir(exist_ok=True)
    (settings / "mcp.json").write_text(
        json.dumps({"mcpServers": {"global-x": {"command": _CMD, "args": ["g"], "poolable": True}}})
    )
    for i in range(n_agents):
        servers: dict[str, Any] = {"srv": {"command": _CMD, "args": [f"a{i}"], "poolable": True}}
        if with_env:
            servers["srv"]["env"] = {"K": "v"} if env is None else dict(env)
        (src / f"agent-{i}.json").write_text(
            json.dumps({"name": f"agent-{i}", "mcpServers": servers})
        )
    return src


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture moved verbatim; no test of the family calls monkeypatch.undo()
def _forward_declared_env_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force declared-env forwarding ON for this module.

    Under cause-B pre-classification a poolable server that declares env while
    forwarding is OFF is left unwrapped — which would gut every
    ``with_env=True`` fixture here (no stub, no sidecar, no target_env).
    These tests exercise the fingerprint/caching machinery, not the
    classification policy (covered in test_mcp_gateway_rewriter.py), so pin
    the flag ON. ``test_forward_declared_env_change_invalidates`` overrides
    this per-call to prove the flag is itself a fingerprint input.
    """
    monkeypatch.setattr(rewriter, "forward_declared_env_enabled", lambda: True)
    # Same reasoning for the identity set: pin it to the default (nothing opted
    # in) so these tests never read the developer's real config, and let
    # ``test_pool_identity_env_change_invalidates`` override it per-call.
    monkeypatch.setattr(rewriter, "pool_identity_env_keys", lambda: frozenset())


def _rewrite(root: Path, **overrides: Any) -> tuple[dict[str, int], dict[str, str]]:
    kwargs: dict[str, Any] = dict(
        source_dir=root / "agents",
        overlay_dir=root / "mcp-gateway" / "agents",
        socket_path=root / "gw.sock",
        work_dir=root / "wd",
        sandbox_mode="auto",
        approval_mode="interactive",
        # Stubbing is opt-in per server name; the fixture's servers must be
        # listed or nothing is wrapped and no sidecar/target_env exists.
        stub_servers=frozenset({"srv", "global-x"}),
        pooling_enabled=True,
    )
    kwargs.update(overrides)
    return rewrite_agents(**kwargs)


@pytest.fixture(name="rewrite_counter")
def rewrite_counter_fixture(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count entries into the per-spec rewrite; zero new counts == skipped."""
    calls = {"n": 0}
    real = rewriter._rewrite_single_spec

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(rewriter, "_rewrite_single_spec", spy)
    return calls


def _bump_mtime(path: Path) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


@contextlib.contextmanager
def _settings_unreadable() -> "Iterator[None]":
    """Make ``settings/mcp.json`` unreadable for the duration, as a real
    transient I/O fault does.

    Both read paths must fail together, and which ones they are is the point.
    ``_rewrite_inputs_fingerprint`` signs the file with ``_stat_sig``, which
    uses ``read_bytes``; the pass itself uses ``read_text``. Faulting only
    ``read_text`` leaves the signature intact, so on otherwise-unchanged inputs
    the cached early return fires and the rewrite loop is never entered -- the
    degraded path under test would not run at all. Faulting both makes the
    signature ``None``, which is what forces the full rewrite on a real fault,
    while leaving every agent source signature comparable.
    """
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes

    def _is_settings(p: Path) -> bool:
        return p.name == "mcp.json" and "settings" in p.parts

    def flaky_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if _is_settings(self):
            raise OSError("transient I/O error")
        return real_read_text(self, *args, **kwargs)

    def flaky_bytes(self: Path, *args: Any, **kwargs: Any) -> bytes:
        if _is_settings(self):
            raise OSError("transient I/O error")
        return real_read_bytes(self, *args, **kwargs)

    Path.read_text = flaky_text  # type: ignore[method-assign]
    Path.read_bytes = flaky_bytes  # type: ignore[method-assign]
    try:
        yield
    finally:
        Path.read_text = real_read_text  # type: ignore[method-assign]
        Path.read_bytes = real_read_bytes  # type: ignore[method-assign]
