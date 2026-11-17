"""The macOS backend ships bytecode for its startup set only, and never writes more.

``precompile_macos_backend`` in ``packaging/desktop-bytecode.sh`` is run here, as
the build runs it, against a small venv laid out like a bundled backend: the
standard library and the ``kiro_crew`` package are compiled whole, third-party
modules only when a traced entry point imports them, and a ``.pth`` bars the
interpreter from writing any other cache. The signing service reads every file in
the app on every request, so a cache this lane did not mean to ship is signing
time on every release, and a cache the runtime writes later breaks the seal.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

pytestmark = [
    pytest.mark.skipif(os.name == "nt", reason="the macOS lane is a POSIX bash function"),
    pytest.mark.skipif(
        sys.version_info[:2] != (3, 12), reason="the bundle layout is named for python3.12"
    ),
]

REPO = Path(__file__).resolve().parent.parent

_SOURCES = {
    # The standard library half: compiled whole, traced or not.
    "lib/python3.12/bundled_stdlib_module.py": "",
    "lib/python3.12/site-packages/kiro_crew/__init__.py": "",
    # Run by every `-m kiro_crew` launch, so a trace must never import it.
    "lib/python3.12/site-packages/kiro_crew/__main__.py": "raise SystemExit('ran the CLI')\n",
    "lib/python3.12/site-packages/kiro_crew/cli_server.py": "import traced_dependency\n",
    "lib/python3.12/site-packages/kiro_crew/cli.py": "",
    # Imported later by a running gateway, never by the trace: compiled because
    # kiro_crew is compiled whole, while its own dependency is not.
    "lib/python3.12/site-packages/kiro_crew/lazy_feature.py": "import lazy_dependency\n",
    "lib/python3.12/site-packages/kiro_crew/mcp_entry.py": (
        'SLIM_SERVERS = {"mcp-sample": ("kiro_crew.mcp_sample", "run")}\n'
    ),
    "lib/python3.12/site-packages/kiro_crew/mcp_sample.py": "import server_dependency\n",
    "lib/python3.12/site-packages/kiro_crew/mcp_gateway/__init__.py": "",
    "lib/python3.12/site-packages/kiro_crew/mcp_gateway/gatewayd.py": "",
    "lib/python3.12/site-packages/kiro_crew/mcp_gateway/stub.py": "",
    "lib/python3.12/site-packages/traced_dependency.py": "",
    "lib/python3.12/site-packages/server_dependency.py": "",
    "lib/python3.12/site-packages/lazy_dependency.py": "",
    "lib/python3.12/site-packages/lark_oapi/__init__.py": "",
}

_SHIPPED = {
    "lib/python3.12/bundled_stdlib_module.py",
    "lib/python3.12/site-packages/kiro_crew/__init__.py",
    "lib/python3.12/site-packages/kiro_crew/__main__.py",
    "lib/python3.12/site-packages/kiro_crew/cli_server.py",
    "lib/python3.12/site-packages/kiro_crew/cli.py",
    "lib/python3.12/site-packages/kiro_crew/lazy_feature.py",
    "lib/python3.12/site-packages/kiro_crew/mcp_entry.py",
    "lib/python3.12/site-packages/kiro_crew/mcp_sample.py",
    "lib/python3.12/site-packages/kiro_crew/mcp_gateway/__init__.py",
    "lib/python3.12/site-packages/kiro_crew/mcp_gateway/gatewayd.py",
    "lib/python3.12/site-packages/kiro_crew/mcp_gateway/stub.py",
    "lib/python3.12/site-packages/traced_dependency.py",
    "lib/python3.12/site-packages/server_dependency.py",
}


def _backend(tmp_path: Path) -> Path:
    out = tmp_path / "kirocrew-backend-arm64"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(out)],
        check=True,
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    for relative, text in _SOURCES.items():
        path = out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return out


def _run_macos_lane(out: Path) -> subprocess.CompletedProcess[str]:
    script = (
        "set -euo pipefail\n"
        'ROOT="$1"; OS=darwin\n'
        "log() { :; }\n"
        '. "$ROOT/packaging/desktop-bytecode.sh"\n'
        'precompile_macos_backend "$2"\n'
    )
    # A clean environment, as on a build runner: an exported PYTHONPYCACHEPREFIX
    # would move every cache away from its source.
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(out.parent)}
    return subprocess.run(
        ["bash", "-c", script, "bash", str(REPO), str(out)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=FALLBACK_TIMEOUT_SECS,
    )


def _shipped_caches(out: Path) -> set[str]:
    # Globbed beside each source rather than asked of cache_from_source: the
    # suite redirects this process's own caches with sys.pycache_prefix.
    return {
        relative
        for relative in _SOURCES
        if list(((out / relative).parent / "__pycache__").glob(f"{Path(relative).stem}.*.pyc"))
    }


def test_the_macos_lane_ships_caches_for_the_startup_set_and_nothing_else(
    tmp_path: Path,
) -> None:
    out = _backend(tmp_path)

    result = _run_macos_lane(out)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _shipped_caches(out) == _SHIPPED
    for relative in _SHIPPED:
        cache = (out / relative).parent / "__pycache__"
        payload = next(cache.glob(f"{Path(relative).stem}.*.pyc")).read_bytes()
        assert int.from_bytes(payload[4:8], "little") == 0b11, f"{relative} is not checked-hash"


def test_the_macos_lane_leaves_an_interpreter_that_writes_no_bytecode(tmp_path: Path) -> None:
    out = _backend(tmp_path)
    assert _run_macos_lane(out).returncode == 0

    # A child that never saw the shell's PYTHONDONTWRITEBYTECODE, importing a
    # module the lane left uncompiled: the agent runtime's MCP stubs run so.
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)}
    subprocess.run(
        [str(out / "bin" / "python3.12"), "-s", "-c", "import lazy_dependency, lark_oapi"],
        check=True,
        env=env,
        timeout=FALLBACK_TIMEOUT_SECS,
    )

    site_packages = out / "lib" / "python3.12" / "site-packages"
    assert not list(site_packages.glob("__pycache__/lazy_dependency.*.pyc"))
    assert not (site_packages / "lark_oapi" / "__pycache__").exists()
