"""Tests for stall-time enrichment (/proc TCP capture for loop-stall dumps).

The /proc parsers are exercised against crafted rows (deterministic, no real
sockets needed); the collector gets one live smoke test on Linux where it runs
against the real ``/proc`` of the test process.
"""

from __future__ import annotations

import sys

from kiro_crew.dashboard.stall_enrichment import (
    _decode_proc_addr,
    _established_lines,
    collect_stall_enrichment,
)


def test_decode_proc_addr_ipv4() -> None:
    # /proc renders IPv4 as one little-endian 32-bit word: 127.0.0.1 -> 0100007F.
    assert _decode_proc_addr("0100007F", "1F90") == ("127.0.0.1", 8080)
    # 52.40.255.127 -> bytes 34 28 FF 7F read LE -> 0x7FFF2834.
    assert _decode_proc_addr("7FFF2834", "01BB") == ("52.40.255.127", 443)


def test_decode_proc_addr_ipv6_loopback() -> None:
    # ::1 in /proc/net/tcp6 is four LE words: 00000000 x3 then 01000000.
    ip, port = _decode_proc_addr("00000000000000000000000001000000", "0050")
    assert ip == "::1"
    assert port == 80


def test_established_lines_filters_and_parses(tmp_path) -> None:
    proc = tmp_path / "tcp"
    rows = [
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode",
        # ESTABLISHED but loopback remote -> excluded.
        "   0: 0100007F:1F90 0100007F:0050 01 00000000:00000000 00:00000000 00000000  1000        0 111 1 0 20 4 30 10 -1",
        # ESTABLISHED, external, our inode -> included; rx_queue 0x1FC30 = 130096.
        "   1: 0A0A0A0A:E88E 7FFF2834:01BB 01 00000000:0001FC30 00:00000000 00000000  1000        0 222 1 0 20 4 30 10 -1",
        # LISTEN (st=0A), external -> excluded.
        "   2: 0A0A0A0A:1F40 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 333 1 0 20 4 30 10 -1",
        # ESTABLISHED, external, NOT our inode -> excluded.
        "   3: 0A0A0A0A:E890 7FFF2834:01BB 01 00000000:00000000 00:00000000 00000000  1000        0 444 1 0 20 4 30 10 -1",
    ]
    proc.write_text("\n".join(rows) + "\n")

    lines = _established_lines(str(proc), inodes={"111", "222", "333"})

    assert len(lines) == 1
    (line,) = lines
    assert "10.10.10.10:59534 -> 52.40.255.127:443" in line
    assert "rx_queue=130096B" in line
    assert "tx_queue=0B" in line
    assert "inode=222" in line


def test_established_lines_missing_file_is_empty() -> None:
    assert _established_lines("/proc/definitely/not/here", inodes={"1"}) == []


def test_collect_smoke_on_linux() -> None:
    lines = collect_stall_enrichment(12.5)
    assert "STALL ENRICHMENT" in lines[0]
    assert "12.5s" in lines[0]
    assert len(lines) >= 2  # header plus at least one capture/degradation line
    if sys.platform.startswith("linux"):
        # Real /proc walk must not degrade to the failure line.
        assert not lines[1].startswith("(socket capture failed")


def test_collect_never_raises_even_if_silence_weird() -> None:
    # Degenerate inputs must not blow up the watchdog thread.
    assert collect_stall_enrichment(0.0)
    assert collect_stall_enrichment(1e9)
