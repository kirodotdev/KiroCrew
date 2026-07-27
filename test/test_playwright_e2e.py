"""Dashboard Playwright E2E suite, folded into ``test_e2e`` (E2eTestCommand).

Boots a real gateway via the same ``spawn_feature_gateway`` harness the smoke
suite uses, then runs the credential-less, crash-free Playwright spec set
(``website/playwright``) against it. Uses Playwright's own bundled Chromium
(``playwright install`` at website-setup time); this OSS fork does not vend a
browser binary.

Gating:
  * ``KIROCREW_E2E`` (set by E2eTestCommand) lifts the skipif, same as the
    smoke suite.
  * Skips gracefully when the in-tree ``website`` dir or its Playwright CLI
    can't be resolved (e.g. a python-only checkout without the built frontend
    dependency).
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import NoReturn

import pytest

# Gate behind KIROCREW_E2E so it never runs in the default unit-test pass.
pytestmark = pytest.mark.skipif(
    not os.environ.get("KIROCREW_E2E"),
    reason="E2E Playwright suite. Set KIROCREW_E2E=1 to run.",
)

_WEBSITE = "website"


def _node_major(node_bin: str) -> int | None:
    """Return the major version of ``node_bin``, or None if it can't run."""
    try:
        out = subprocess.check_output(
            [node_bin, "--version"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    m = re.match(r"v(\d+)\.", out)
    return int(m.group(1)) if m else None


def _resolve_node18_dir() -> str | None:
    """Return a bin dir holding a real node>=18 binary, or None.

    Playwright 1.58 requires Node>=18. We cannot rely on the ambient ``node``:
    the website dir often pins an older Node via mise (its shim is cwd-sensitive
    and resolves to the pinned version when Playwright runs there), and the
    build env may not expose Node on PATH at all. So scan a prioritized list of
    *concrete* node binaries (never a mise shim, which is cwd-sensitive) and
    return the first dir whose node is >=18; prepending it to PATH makes the
    Playwright shebang resolve it regardless of mise.
    """
    candidates: list[str] = []
    # mise-managed concrete installs (local dev) — not the shim.
    candidates += sorted(
        glob.glob(os.path.expanduser("~/.local/share/mise/installs/node/*/bin/node")),
        reverse=True,
    )
    # Ambient node last; skip mise shims (cwd-sensitive, unreliable here).
    onpath = shutil.which("node")
    if onpath and "/shims/" not in onpath:
        candidates.append(onpath)
    for c in candidates:
        maj = _node_major(c)
        if maj is not None and maj >= 18:
            return str(Path(c).resolve().parent)
    return None


def _resolve_website_dir() -> Path | None:
    """Locate the in-tree ``website`` root (with ``playwright/`` + ``node_modules``).

    Mirrors ``kiro_crew.frontend`` dist resolution: the canonical frontend lives
    in-tree at ``<repo-root>/website``. ``test/`` sits at the repo root, so the
    website is a sibling of this file's parent directory.
    """
    repo_root = Path(__file__).resolve().parent.parent  # KiroCrew repo root
    in_tree = repo_root / _WEBSITE
    return in_tree if (in_tree / "playwright").is_dir() else None


def test_dashboard_playwright_suite() -> None:
    """Boot a gateway and run the credential-less Playwright spec set against it."""

    def _unresolved(msg: str) -> NoReturn:
        # On the required PR gate (KIROCREW_E2E_REQUIRE, set by that step) an
        # environment-resolution miss is a HARD failure: pytest counts a skip as
        # a pass, so the gate would go green having run zero browser specs -- the
        # exact "dead suite, silent UI drift" rot this fold exists to catch. Keep
        # the graceful skip for ad-hoc local/dev runs (marker unset).
        if os.environ.get("KIROCREW_E2E_REQUIRE"):
            pytest.fail(msg)
        pytest.skip(msg)

    website = _resolve_website_dir()
    if website is None:
        _unresolved("website dir not resolvable (no playwright/ dir)")

    pw_bin = website / "node_modules" / ".bin" / "playwright"
    if not pw_bin.exists():
        _unresolved(f"Playwright CLI not found at {pw_bin}")

    node_dir = _resolve_node18_dir()
    if node_dir is None:
        _unresolved("No Node.js >=18 found; Playwright 1.58 requires it")

    # Point the gateway's ACP client at the packaged fake backend so the
    # agent-driven specs (chat, fork) run deterministic, credential-less turns
    # instead of needing real model access. acp/client.py reads
    # KIROCREW_KIRO_BIN and, when set, spawns it as the agent binary; the
    # harness gateway inherits os.environ at spawn time.
    from kiro_crew.testing import fake_acp_backend
    from kiro_crew.testing.harness import spawn_feature_gateway

    prev_kiro_bin = os.environ.get("KIROCREW_KIRO_BIN")
    os.environ["KIROCREW_KIRO_BIN"] = str(fake_acp_backend.__file__)
    try:
        with spawn_feature_gateway(fixture="minimal", approval="reads") as gw:
            env = dict(os.environ)
            env.update(
                {
                    # Prepend a node>=18 bin dir so the playwright shebang
                    # resolves it ahead of any cwd-pinned mise shim.
                    "PATH": node_dir + os.pathsep + env.get("PATH", ""),
                    "PLAYWRIGHT_BASE_URL": f"http://localhost:{gw.port}",
                    "PLAYWRIGHT_TOKEN": gw.token,
                    # Fake ACP backend is wired, so the agent specs (chat/fork)
                    # can run headlessly -- opt them back in.
                    "PLAYWRIGHT_RUN_AGENT_SPECS": "1",
                    # Explicit ephemeral-harness marker: this gateway runs on an
                    # isolated tmp KIROCREW_HOME (spawn_feature_gateway --test-mode),
                    # so its slots are disposable.
                    "KIROCREW_E2E_EPHEMERAL": "1",
                    # CI mode: serial workers + retries:2 (absorbs gateway-load
                    # timeout flakes) + html reporter, per playwright.config.ts.
                    "CI": "1",
                }
            )
            print(
                f"[test:e2e:playwright] base={env['PLAYWRIGHT_BASE_URL']} "
                f"node_dir={node_dir} fake_acp={fake_acp_backend.__file__} cwd={website}",
                flush=True,
            )
            rc = subprocess.call([str(pw_bin), "test"], cwd=str(website), env=env)
            assert rc == 0, f"playwright test exited {rc}"
    finally:
        if prev_kiro_bin is None:
            os.environ.pop("KIROCREW_KIRO_BIN", None)
        else:
            os.environ["KIROCREW_KIRO_BIN"] = prev_kiro_bin
