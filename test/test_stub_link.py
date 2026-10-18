"""``stub_link``: the read ceiling a stub adopts from its argv, and its stdout writer.

``_adopt_read_ceiling`` rebinds three module globals at run time -- the per-stream read
ceiling and the two bounds on what a disconnected stub may hold -- and its readers
(``handshake``, ``_no_room_to_hold``, ``_drain_while_disconnected``) live in the same
module, so they see the adopted answer. These tests call it through the facade, the way
``_amain`` does, and read the result through both the facade and the owner.
"""

from __future__ import annotations

import argparse

import pytest

from kiro_crew.mcp_gateway import stub, stub_link
from kiro_crew.mcp_gateway.read_limits import _DEFAULT_READ_BUFFER_LIMIT

_BOUNDS = ("READ_BUFFER_LIMIT_BYTES", "_HELD_FRAME_BYTES", "_HELD_TOTAL_BYTES")


@pytest.fixture
def bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the three bounds after the test: the adoption writes them for good."""
    monkeypatch.delenv("KIROCREW_MCP_READ_LIMIT", raising=False)
    for name in _BOUNDS:
        monkeypatch.setattr(stub, name, getattr(stub, name))


@pytest.mark.usefixtures("bounds")
def test_the_launch_flag_sets_the_ceiling_and_both_hold_bounds() -> None:
    raised = _DEFAULT_READ_BUFFER_LIMIT * 2
    assert stub._adopt_read_ceiling(argparse.Namespace(read_limit=str(raised))) == raised
    for module in (stub, stub_link):
        assert module.READ_BUFFER_LIMIT_BYTES == raised
        assert module._HELD_FRAME_BYTES == raised
        assert module._HELD_TOTAL_BYTES == raised


@pytest.mark.usefixtures("bounds")
def test_a_lowered_ceiling_never_tightens_the_total_hold() -> None:
    assert stub._adopt_read_ceiling(argparse.Namespace(read_limit="4096")) == 4096
    assert stub_link._HELD_FRAME_BYTES == 4096
    assert stub_link._HELD_TOTAL_BYTES == _DEFAULT_READ_BUFFER_LIMIT


@pytest.mark.usefixtures("bounds")
def test_an_unparsable_flag_is_logged_and_resolved_the_long_way(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    asked: list[object] = []

    def resolve(flag_value: object) -> int:
        asked.append(flag_value)
        return 8192

    monkeypatch.setattr(stub, "read_buffer_limit_from_flag", resolve)
    with caplog.at_level("WARNING", logger=stub.logger.name):
        assert stub._adopt_read_ceiling(argparse.Namespace(read_limit="lots")) == 8192
    assert asked == [None]
    assert stub_link.READ_BUFFER_LIMIT_BYTES == 8192
    assert any("unparsable --read-limit" in record.getMessage() for record in caplog.records)


@pytest.mark.usefixtures("bounds")
def test_an_overlay_without_the_flag_resolves_the_long_way(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stub, "read_buffer_limit_from_flag", lambda flag_value: 2048)
    assert stub._adopt_read_ceiling(argparse.Namespace()) == 2048
    assert stub.READ_BUFFER_LIMIT_BYTES == 2048


def test_a_stdout_line_is_terminated_and_flushed(
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    stub._write_stdout_line(b'{"id": 1}')
    stub._write_stdout_line(b'{"id": 2}\n')
    assert capsysbinary.readouterr().out == b'{"id": 1}\n{"id": 2}\n'
