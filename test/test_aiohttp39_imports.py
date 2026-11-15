"""The dashboard still loads, and still behaves the same, on aiohttp 3.9.

setup.cfg allows ``aiohttp>=3.9``, but ``ClientConnectionResetError`` and
``web_fileresponse.CONTENT_TYPES`` only exist from 3.10. A child process takes both names
out of aiohttp to stand in for 3.9 and then imports the modules that used them. It is a
child because importing a facade composes its owner modules for the whole process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import kiro_crew
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_IMPORTERS = (
    "kiro_crew.dashboard.chat_api.chat_send",
    "kiro_crew.dashboard.chat_handlers",
    "kiro_crew.dashboard.file_api.transfer",
    "kiro_crew.dashboard.handlers.core",
    "kiro_crew.dashboard.handlers.files",
    "kiro_crew.dashboard.handlers.updates_streams",
    "kiro_crew.dashboard.server",
)

_CHILD = """
import asyncio, importlib, json, mimetypes, sys
from pathlib import Path
import aiohttp, aiohttp.client_exceptions, aiohttp.web_fileresponse
for owner, name in ((aiohttp.client_exceptions, "ClientConnectionResetError"),
                    (aiohttp, "ClientConnectionResetError"),
                    (aiohttp.web_fileresponse, "CONTENT_TYPES")):
    if hasattr(owner, name):
        delattr(owner, name)
added = []
stdlib_add_type = mimetypes.add_type

def recording_add_type(type_, ext, strict=True):
    added.append([type_, ext])
    stdlib_add_type(type_, ext, strict)

mimetypes.add_type = recording_add_type
report = {"load_errors": {}, "log_stream_ok": None, "woff2_type": None}
for name in sys.argv[1:]:
    try:
        importlib.import_module(name)
        report["load_errors"][name] = None
    except ImportError as exc:
        report["load_errors"][name] = repr(exc)
try:
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    from kiro_crew.dashboard.handlers import updates_streams

    async def hang_up(self, request):
        raise ConnectionResetError("Cannot write to closing transport")

    web.StreamResponse.prepare = hang_up

    async def main():
        resp = await updates_streams.api_logs(make_mocked_request("GET", "/api/logs"))
        return isinstance(resp, web.StreamResponse)

    report["log_stream_ok"] = asyncio.run(main())
except BaseException as exc:
    report["log_stream_ok"] = repr(exc)
try:
    from kiro_crew.dashboard import dist_file_response

    report["woff2_type"] = dist_file_response._guess_type(Path("font.woff2"))
except BaseException as exc:
    report["woff2_type"] = repr(exc)
report["mimetypes_added"] = added
print("REPORT " + json.dumps(report))
"""


@pytest.fixture(scope="module")
def on_aiohttp_39(tmp_path_factory) -> dict:
    """What a 3.9 stand-in process saw when it loaded the dashboard modules."""
    src = str(Path(kiro_crew.__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([src, os.environ.get("PYTHONPATH", "")])}
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_CHILD), *_IMPORTERS],
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
        cwd=str(tmp_path_factory.mktemp("aiohttp39")),
        env=env,
        **UTF8_TEXT,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("REPORT ")]
    assert result.returncode == 0 and lines, result.stderr[-2000:]
    return json.loads(lines[-1][len("REPORT ") :])


@pytest.mark.parametrize("name", _IMPORTERS)
def test_every_module_loads_without_the_310_names(on_aiohttp_39, name):
    assert on_aiohttp_39["load_errors"][name] is None


def test_a_39_closed_transport_write_is_a_hang_up_not_an_error(on_aiohttp_39):
    assert on_aiohttp_39["log_stream_ok"] is True


def test_on_39_the_font_types_land_on_the_stdlib_table_file_response_reads(on_aiohttp_39):
    assert ["font/woff2", ".woff2"] in on_aiohttp_39["mimetypes_added"]
    assert on_aiohttp_39["woff2_type"] == "font/woff2"
