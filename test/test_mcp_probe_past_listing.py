"""The discovery probe of a server that lists its tools before it loads its handlers.

``kirocrew-core``, ``kirocrew-cron``, ``kirocrew-guide`` and ``kirocrew-mate`` answer
``initialize`` and ``tools/list`` before they import the code that runs their tools
(``kiro_crew.mcp_shared_listing``). So their tool list does not show that they can run a
tool: a handler import that fails ends the process after the list is out. The probe asks
such a server for one more answer, which only its dispatch loop gives, and reports a
server that stopped instead of answering, with the reason the child printed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp_discovery_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _passthrough_sandbox,
    _stub_remote_probe_ssrf_vet,
)
from test_mcp_listing_import_budget import gateway_port  # noqa: F401 -- a fixture
from test_mcp_slim_entry import _child_env

from kiro_crew import mcp_entry
from kiro_crew.mcp_discovery import McpServerInfo, probe_server

_INIT = b'{"jsonrpc":"2.0","id":1,"result":{"capabilities":{}}}\n'
_LIST = b'{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"cron_list"}]}}\n'
_PONG = b'{"jsonrpc":"2.0","id":3,"result":{}}\n'
_NO_SUCH_METHOD = b'{"jsonrpc":"2.0","id":3,"error":{"code":-32601,"message":"Method not found"}}\n'
_WHY = "kirocrew: cannot start - No module named 'a_handler_dependency'"


def _process(stdout: list[bytes | BaseException], stderr: bytes = b"") -> AsyncMock:
    proc = AsyncMock()
    proc.returncode = 0
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    proc.stdout = AsyncMock()
    proc.stdout.readline = AsyncMock(side_effect=stdout)
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(side_effect=[stderr, b""] if stderr else [b""])
    return proc


async def _probe(server: McpServerInfo, proc: AsyncMock) -> McpServerInfo:
    with (
        patch("kiro_crew.mcp_discovery.asyncio.create_subprocess_exec", return_value=proc),
        patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/kirocrew"),
    ):
        return await probe_server(server)


def _methods_sent(proc: AsyncMock) -> list[str]:
    return [json.loads(call.args[0])["method"] for call in proc.stdin.write.call_args_list]


def _cron() -> McpServerInfo:
    return McpServerInfo(name="kirocrew-cron", command="kirocrew", args=["mcp-cron"])


def test_every_server_that_answers_early_is_one_the_probe_asks_again() -> None:
    from kiro_crew.mcp_discovery_managed import _MANAGED_SERVER_SUBCOMMANDS

    subcommands = set(_MANAGED_SERVER_SUBCOMMANDS.values())
    assert set(mcp_entry.EARLY_ANSWERS) <= subcommands
    assert all(mcp_entry.lists_before_handlers([command]) for command in mcp_entry.EARLY_ANSWERS)
    assert not mcp_entry.lists_before_handlers([])
    assert not mcp_entry.lists_before_handlers(["mcp-panel"])


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [_PONG, _NO_SUCH_METHOD], ids=["a-result", "an-error"])
async def test_any_answer_after_the_list_shows_the_handlers_loaded(answer: bytes) -> None:
    proc = _process([_INIT, _LIST, answer, b""])

    result = await _probe(_cron(), proc)

    assert (result.status, result.tools, result.error) == ("ok", ["cron_list"], "")
    assert _methods_sent(proc)[-2:] == ["tools/list", "ping"]


@pytest.mark.asyncio
async def test_a_server_that_stops_after_listing_is_an_error_with_its_reason() -> None:
    proc = _process([_INIT, _LIST, b""], stderr=_WHY.encode())

    result = await _probe(_cron(), proc)

    assert result.status == "error"
    assert result.tools == []
    assert result.error.startswith("the server listed its tools and then stopped")
    assert f"stderr: {_WHY}" in result.error


#: What a server can print as it starts, before the line that says why it stopped: two
#: warnings that together are longer than the stderr text an error carries.
_NOISE = "".join(
    f"Cannot restrict /some/data/dir/part-{n} to owner-only; it may be readable by other "
    f"users (Operation not permitted). The likely cause is an attribute on the path.\n"
    for n in range(4)
)


@pytest.mark.asyncio
async def test_the_reason_at_the_end_of_stderr_is_shown_past_what_was_printed_first() -> None:
    """The reason a server stopped is the LAST thing it wrote. It arrives here in a
    second read, after more start-up text than an error carries."""
    assert len(_NOISE) > 500
    proc = _process([_INIT, _LIST, b""])
    proc.stderr.read = AsyncMock(side_effect=[_NOISE.encode(), f"{_WHY}\n".encode(), b""])

    result = await _probe(_cron(), proc)

    stderr = result.error.partition("\nstderr: ")[2]
    assert stderr.endswith(_WHY)
    assert "part-0" not in stderr and len(stderr) <= 500
    assert stderr.startswith("Cannot restrict"), "the text shown begins inside a line"


@pytest.mark.asyncio
async def test_a_stderr_that_never_ends_is_read_to_a_bound() -> None:
    from kiro_crew import mcp_discovery_probe as probe

    proc = _process([_INIT, _LIST, b""])
    proc.stderr.read = AsyncMock(return_value=b"x" * 4095 + b"\n")

    result = await _probe(_cron(), proc)

    assert proc.stderr.read.await_count == probe._STDERR_READS
    assert len(result.error.partition("\nstderr: ")[2]) <= probe._STDERR_SHOWN_CHARS


@pytest.mark.asyncio
async def test_a_long_private_key_in_stderr_is_redacted_whole_before_its_end_is_shown() -> None:
    """The redactor knows a private key by its first line. A key longer than what the
    probe shows must reach the redactor from that line on: its last body line is short
    enough to pass for ordinary text once the first line is gone."""
    body = "".join("QUJD" * 16 + "\n" for _ in range(200))  # 13 KiB of base64 lines
    last_line = "c2hvcnRUYWlsTGluZQ=="
    label = "RSA PRIVATE KEY"
    key = f"-----BEGIN {label}-----\n{body}{last_line}\n-----END {label}-----\n"
    raw = (_NOISE + key).encode()
    proc = _process([_INIT, _LIST, b""])
    chunks = [raw[at : at + 4096] for at in range(0, len(raw), 4096)]
    proc.stderr.read = AsyncMock(side_effect=[*chunks, b""])

    result = await _probe(_cron(), proc)

    assert "stderr: " in result.error
    assert last_line not in result.error and "QUJDQUJD" not in result.error


@pytest.mark.asyncio
async def test_a_stand_in_stderr_that_returns_no_bytes_ends_the_read_too() -> None:
    """Only bytes are kept and the reads are counted, so what a stand-in's read
    returns can neither keep the probe reading nor reach the error text."""
    proc = _process([_INIT, _LIST, b""])
    proc.stderr.read = AsyncMock(return_value=MagicMock())

    result = await _probe(_cron(), proc)

    assert result.status == "error" and "stderr:" not in result.error


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("short", "short"),
        ("first line\nthe reason", "the reason"),
        ("no line break at all", "k at all"),
    ],
)
def test_the_shown_end_starts_at_a_line_where_one_fits(text: str, shown: str) -> None:
    from kiro_crew.mcp_discovery_probe import _last_lines

    assert _last_lines(text, 12 if "\n" in text else 8) == shown


@pytest.mark.asyncio
async def test_a_server_that_has_closed_its_input_is_the_same_error() -> None:
    proc = _process([_INIT, _LIST, b""], stderr=_WHY.encode())
    proc.stdin.drain = AsyncMock(side_effect=[None, None, None, BrokenPipeError()])

    result = await _probe(_cron(), proc)

    assert result.status == "error"
    assert result.error.startswith("the server listed its tools and then stopped")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "server",
    [
        McpServerInfo(name="other-server", command="other", args=["mcp-cron"]),
        McpServerInfo(name="kirocrew-cron", command="kirocrew", args=["mcp-cron", "--verbose"]),
        McpServerInfo(name="kirocrew-panel", command="kirocrew", args=["mcp-panel"]),
    ],
    ids=["not-ours", "not-the-bare-subcommand", "ours-but-loads-handlers-first"],
)
async def test_no_other_server_is_asked_again(server: McpServerInfo) -> None:
    proc = _process([_INIT, _LIST, b""])

    result = await _probe(server, proc)

    assert (result.status, result.tools) == ("ok", ["cron_list"])
    assert "ping" not in _methods_sent(proc)


#: A real ``kirocrew mcp-cron`` whose handler module cannot be imported: it answers the
#: two early requests from the real entry, then ends the way the console script does.
_BROKEN_SERVER = """
import os
import sys


class _HandlersMissing:
    def find_spec(self, name, path=None, target=None):
        if name == "kiro_crew.mcp_cron":
            raise ImportError("No module named 'a_handler_dependency'")
        return None


sys.meta_path.insert(0, _HandlersMissing())
sys.stderr.write(os.environ["PROBE_TEST_NOISE"])
from kiro_crew import mcp_entry

try:
    mcp_entry.run_if_slim([sys.argv[-1]])
except ImportError as exc:
    sys.stderr.write(f"kirocrew: cannot start - {exc}\\n")
    raise SystemExit(1)
"""


@pytest.mark.asyncio
async def test_a_real_server_whose_handler_import_fails_turns_the_probe_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gateway_port: int  # noqa: F811
) -> None:
    (tmp_path / "home").mkdir()
    script = tmp_path / "broken_server.py"
    script.write_text(_BROKEN_SERVER, encoding="utf-8")
    env = _child_env(
        tmp_path,
        KIROCREW_PROJECT_DIR=str(tmp_path),
        KIROCREW_PORT=str(gateway_port),
        KIROCREW_SESSION_KEY="dashboard:probe",
        PROBE_TEST_NOISE=_NOISE,
    )
    monkeypatch.setattr(
        "kiro_crew.mcp_discovery.sandboxed_spawn_argv",
        lambda argv, *_args, **_kwargs: (list(argv), env, None),
    )
    server = McpServerInfo(
        name="kirocrew-cron", command=sys.executable, args=[str(script), "mcp-cron"]
    )

    result = await probe_server(server)

    assert result.status == "error", result.error
    assert result.error.startswith("the server listed its tools and then stopped")
    assert "No module named 'a_handler_dependency'" in result.error
    assert "part-0" not in result.error  # the start-up text ahead of the reason
