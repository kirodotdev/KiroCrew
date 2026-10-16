"""Fake provider guests cannot discover the operator's user-manager bus."""

import json
import sys

import pytest
from test_boxd_session import fake  # noqa: F401 -- fixture for the local CLI
from test_sprites_session import TOKEN, fake_api

from kiro_crew.boxd_session.cli import BoxdCLI
from kiro_crew.sprites_session.client import SpritesClient

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX guest shell")

# Read only: even a broken fake environment cannot start a host service here.
READ_BUS_ENV = (
    "import json, os; print(json.dumps([os.environ.get(k) for k in "
    "['XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS']]))"
)


@pytest.mark.asyncio
async def test_sprites_fake_guest_bus_is_inside_its_home(tmp_path):
    async with fake_api(tmp_path) as (guest, url):
        guest.sprites["isolated"] = {"name": "isolated", "status": "running"}
        code, out, err = await SpritesClient(TOKEN, base_url=url).exec(
            "isolated", [sys.executable, "-c", READ_BUS_ENV]
        )
        assert code == 0 and err == b""
        assert json.loads(out) == [
            str(guest.home / "runtime"),
            f"unix:path={guest.home / 'runtime' / 'bus'}",
        ]


@pytest.mark.asyncio
async def test_boxd_fake_guest_bus_is_inside_its_home(fake):  # noqa: F811 -- pytest fixture
    cli = BoxdCLI(fake.bin)
    name = "kc-0123456789abcdef"
    await cli.create(name, snapshot="", vcpu=1)
    code, out = await cli._run("machine", "exec", name, "--", sys.executable, "-c", READ_BUS_ENV)
    assert code == 0
    assert json.loads(out) == [
        str(fake.home / "runtime"),
        f"unix:path={fake.home / 'runtime' / 'bus'}",
    ]
