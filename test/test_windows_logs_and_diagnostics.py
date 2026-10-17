"""Windows log reads and config-only diagnostics must not promise runtime health."""

import argparse
import logging.handlers
import os
from types import SimpleNamespace

import pytest

from kiro_crew import cli_doctor, cli_server, platform_compat
from kiro_crew.service.common import Platform


@pytest.fixture
def windows_log(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(cli_server, "current_platform", lambda: Platform.UNSUPPORTED)
    monkeypatch.setattr(cli_server, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        cli_server, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: None)
    )

    def forbidden(*args):
        raise AssertionError("Windows logs must not exec tail")

    monkeypatch.setattr(cli_server.os, "execvp", forbidden)
    return tmp_path / "gateway.log"


def test_windows_logs_read_utf8_tail_without_external_binary(windows_log, capsys):
    windows_log.write_text("first\nsecond\n日本語 👻\nlast\n", encoding="utf-8")
    cli_server._logs_cmd(argparse.Namespace(lines=2, follow=False))
    assert capsys.readouterr().out == "日本語 👻\nlast\n"


def test_windows_empty_log_succeeds(windows_log, capsys):
    windows_log.write_text("", encoding="utf-8")
    cli_server._logs_cmd(argparse.Namespace(lines=2, follow=False))
    assert capsys.readouterr().out == ""


def test_windows_follow_reads_appended_lines_and_stops(windows_log, monkeypatch, capsys):
    windows_log.write_text("initial\n", encoding="utf-8")
    waits = []

    def tick(_seconds):
        waits.append(1)
        if len(waits) == 1:
            with windows_log.open("a", encoding="utf-8") as log:
                log.write("appended\n")
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_server.time, "sleep", tick)
    cli_server._logs_cmd(argparse.Namespace(lines=1, follow=True))
    assert capsys.readouterr().out == "initial\nappended\n"


@pytest.mark.parametrize("system", ["Windows", "Darwin"])
def test_doctor_routing_config_is_not_a_verified_session(system, monkeypatch, capsys):
    monkeypatch.setattr(cli_doctor._plat, "system", lambda: system)
    cfg = SimpleNamespace(
        mcp_gateway=SimpleNamespace(stub_servers=cli_doctor._STRICT_IDENTITY_SERVERS)
    )
    cli_doctor._doctor_strict_identity(cfg)
    output = capsys.readouterr().out
    assert "configured" in output
    assert "not verified" in output
    assert "✅" not in output


def test_doctor_mcp_success_names_its_probe_scope(tmp_path, monkeypatch, capsys):
    import json

    names = cli_doctor._MANAGED_MCPS
    refs = ["@" + name for name in names]
    spec = tmp_path / "agent.json"
    spec.write_text(
        json.dumps(
            {
                "mcpServers": {name: {"command": "probe"} for name in names},
                "tools": refs,
                "allowedTools": refs,
            }
        ),
        encoding="utf-8",
    )

    async def probe(info):
        info.status = "ok"
        info.tools = [{"name": "example"}]
        return info

    monkeypatch.setattr(cli_doctor, "probe_server", probe)
    monkeypatch.setattr(cli_doctor, "warm_backend", lambda: None)
    cli_doctor._doctor_mcp_tools(spec, [], gated_off=frozenset())
    output = capsys.readouterr().out
    assert "host probe" in output
    assert "session tool loading is not verified" in output


def test_windows_follow_handles_truncation(windows_log, monkeypatch, capsys):
    windows_log.write_text("a longer initial line\n", encoding="utf-8")
    waits = []

    def tick(_seconds):
        waits.append(1)
        if len(waits) == 1:
            windows_log.write_text("new\n", encoding="utf-8")
        elif len(waits) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_server.time, "sleep", tick)
    cli_server._logs_cmd(argparse.Namespace(lines=1, follow=True))
    assert capsys.readouterr().out == "a longer initial line\nnew\n"


@pytest.mark.parametrize("during_read", [False, True], ids=["between-polls", "reader-open"])
def test_follow_allows_real_writer_rollover(windows_log, monkeypatch, capsys, during_read):
    """Rotation must persist records even when it races an open read handle."""
    handler = logging.handlers.RotatingFileHandler(
        windows_log, maxBytes=32, backupCount=1, encoding="utf-8"
    )
    logger = logging.Logger("isolated-rotation-test", level=logging.INFO)
    logger.addHandler(handler)
    errors = []
    monkeypatch.setattr(handler, "handleError", errors.append)
    opens = []
    real_open = platform_compat.open_log_file_for_tail

    def rotate():
        # The seed fills the file; these two short records fit in one new file.
        logger.info("after-1")
        logger.info("after-2")

    def read_open(path):
        fd = real_open(path)
        opens.append(fd)
        if during_read and len(opens) == 2:
            rotate()
        return fd

    monkeypatch.setattr(platform_compat, "open_log_file_for_tail", read_open)
    ticks = []

    def tick(_seconds):
        ticks.append(1)
        if not during_read and len(ticks) == 1:
            rotate()
        if len(ticks) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_server.time, "sleep", tick)
    try:
        logger.info("x" * 30)
        cli_server._tail_log_file(windows_log, 1, True)
    finally:
        handler.close()
    assert not errors
    assert windows_log.read_text(encoding="utf-8") == "after-1\nafter-2\n"
    assert capsys.readouterr().out == "x" * 30 + "\nafter-1\nafter-2\n"


@pytest.mark.parametrize("replacement", ["new\n", "a much longer replacement than the seed\n"])
def test_follow_replacement_identity(windows_log, monkeypatch, capsys, replacement):
    windows_log.write_text("seed\n", encoding="utf-8")
    ticks = []

    def tick(_seconds):
        ticks.append(1)
        if len(ticks) == 1:
            windows_log.rename(windows_log.with_suffix(".log.1"))
            windows_log.write_text(replacement, encoding="utf-8")
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_server.time, "sleep", tick)
    cli_server._tail_log_file(windows_log, 1, True)
    assert capsys.readouterr().out == "seed\n" + replacement


def test_follow_missing_path_during_rotation(windows_log, monkeypatch, capsys):
    windows_log.write_text("seed\n", encoding="utf-8")
    ticks = []

    def tick(_seconds):
        ticks.append(1)
        if len(ticks) == 1:
            windows_log.rename(windows_log.with_suffix(".log.1"))
        elif len(ticks) == 2:
            windows_log.write_text("resumed\n", encoding="utf-8")
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_server.time, "sleep", tick)
    cli_server._tail_log_file(windows_log, 1, True)
    assert capsys.readouterr().out == "seed\nresumed\n"


@pytest.mark.parametrize("parts", [(b"\xf0\x9f", b"\x91\xbb\n"), (b"line\r", b"\n")])
def test_follow_split_utf8_and_crlf(windows_log, monkeypatch, capsys, parts):
    windows_log.write_bytes(b"")
    writes = iter(parts)

    def tick(_seconds):
        data = next(writes, None)
        if data is None:
            raise KeyboardInterrupt
        with windows_log.open("ab") as log:
            log.write(data)

    monkeypatch.setattr(cli_server.time, "sleep", tick)
    cli_server._tail_log_file(windows_log, 1, True)
    assert capsys.readouterr().out == b"".join(parts).decode("utf-8").replace("\r\n", "\n")


def test_nonfollow_flushes_incomplete_utf8(windows_log, capsys):
    windows_log.write_bytes(b"line\n\xf0\x9f")
    cli_server._tail_log_file(windows_log, 2, False)
    assert capsys.readouterr().out == "line\n\ufffd"


def test_no_data_polls_close_read_descriptors(windows_log, monkeypatch):
    windows_log.write_bytes(b"")
    real_open = platform_compat.open_log_file_for_tail
    descriptors = []

    def read_open(path):
        fd = real_open(path)
        descriptors.append(fd)
        return fd

    def tick(_seconds):
        with pytest.raises(OSError):
            os.fstat(descriptors[-1])
        if len(descriptors) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(platform_compat, "open_log_file_for_tail", read_open)
    monkeypatch.setattr(cli_server.time, "sleep", tick)
    cli_server._tail_log_file(windows_log, 1, True)
    assert len(descriptors) == 3


def test_tail_zero_initial_lines(windows_log, capsys):
    windows_log.write_text("a\nb\n", encoding="utf-8")
    cli_server._tail_log_file(windows_log, 0, False)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("error", [FileNotFoundError("rotating"), PermissionError("unreadable")])
def test_logs_read_failure_has_guidance(windows_log, monkeypatch, capsys, error):
    windows_log.write_text("seed\n", encoding="utf-8")

    def cannot_open(_path):
        raise error

    monkeypatch.setattr(platform_compat, "open_log_file_for_tail", cannot_open)
    with pytest.raises(SystemExit) as result:
        cli_server._logs_cmd(argparse.Namespace(lines=1, follow=False))
    assert result.value.code == 1
    output = capsys.readouterr()
    assert "Unable to read gateway log" in output.err
    assert "kirocrew logs" in output.err
    assert "Traceback" not in output.err
