"""The desktop backends ship the dashboard build without its ``.br``/``.gz`` twins.

``build_backend`` in ``packaging/build-desktop.sh`` stages ``website/dist`` into
each bundled backend, and the universal macOS zip holds two backends under a size
gate. ``drop_precompressed_dist`` removes the twins of the hashed assets, and the
server must still answer a client that accepts them with the plain file. The
helper is extracted from the shipped script, not copied.
"""

from __future__ import annotations

import gzip
import os
import re
import subprocess
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.server import _register_dist_static_routes

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="drop_precompressed_dist is a POSIX bash helper"
)

SCRIPT = Path(__file__).parent.parent / "packaging" / "build-desktop.sh"


def _function(name: str) -> str:
    match = re.search(
        rf"^{name}\(\) \{{.*?^\}}", SCRIPT.read_text(encoding="utf-8"), re.DOTALL | re.MULTILINE
    )
    assert match, f"{name}() not found in packaging/build-desktop.sh"
    return match.group(0)


def _drop(dist: Path) -> None:
    script = f'set -euo pipefail\n{_function("drop_precompressed_dist")}\ndrop_precompressed_dist "{dist}"'
    subprocess.run(
        ["bash", "-c", script], check=True, capture_output=True, text=True, encoding="utf-8"
    )


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets" / "app-windows").mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    for rel in ("assets/main-abc.js", "assets/app-windows/gallery-def.js"):
        (dist / rel).write_bytes(b"export const x = 1\n")
        (dist / f"{rel}.gz").write_bytes(gzip.compress(b"export const x = 1\n"))
        (dist / f"{rel}.br").write_bytes(b"br-bytes")
    return dist


def test_build_backend_drops_the_twins_after_staging() -> None:
    """Wiring guard: ``build_backend`` calls the helper, after the dist is staged."""
    body = _function("build_backend")
    staged = body.index('echo "ERROR: dashboard dist not staged"')
    assert body.index('drop_precompressed_dist "$sp/kiro_crew/static/dist"') > staged


def test_only_the_twins_are_removed(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    _drop(dist)
    left = sorted(p.relative_to(dist).as_posix() for p in dist.rglob("*") if p.is_file())
    assert left == ["assets/app-windows/gallery-def.js", "assets/main-abc.js", "index.html"]


@pytest.mark.asyncio
async def test_a_client_that_accepts_br_and_gzip_gets_the_plain_file(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    _drop(dist)
    app = web.Application()
    _register_dist_static_routes(app, dist)
    async with TestClient(TestServer(app), auto_decompress=False) as client:
        resp = await client.get("/assets/main-abc.js", headers={"Accept-Encoding": "br, gzip"})
        assert resp.status == 200
        assert "Content-Encoding" not in resp.headers
        assert await resp.read() == b"export const x = 1\n"
