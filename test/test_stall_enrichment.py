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


class _DumpFile:
    """Records every write, so a test can prove the crash sentinel is untouched."""

    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, text: str) -> int:
        self.writes.append(text)
        return len(text)

    def fileno(self) -> int:  # pragma: no cover - never armed in these tests
        raise AssertionError("dump_file must not be used by lag enrichment")


def _lag_watchdog(caplog):
    from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog

    calls: list[float] = []
    dump_file = _DumpFile()

    def enrich(lag: float) -> list[str]:
        calls.append(lag)
        return ["=== STALL ENRICHMENT ===", "10.0.0.1:1 -> 52.40.255.127:443 rx_queue=9B"]

    clock = [0.0]
    wd = LoopStallWatchdog(
        now=lambda: clock[0],
        dump_file=dump_file,
        enrich=enrich,
        arm_later=lambda _t: None,
        cancel_later=lambda: None,
    )
    caplog.set_level("WARNING", logger="kiro_crew.dashboard.loop_watchdog")
    return wd, calls, dump_file, clock


def _beat(wd, lag: float) -> None:
    # The heartbeat's own wiring: claim before beat(), capture off the loop.
    capture = wd.claim_lag_enrichment(lag)
    wd.beat()
    if capture:
        wd.log_lag_enrichment(lag)


def _lag_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "heartbeat lag" in r.getMessage()]


def test_lag_over_threshold_logs_one_enrichment_line(caplog) -> None:
    wd, calls, dump_file, _clock = _lag_watchdog(caplog)

    _beat(wd, 3.2)

    lines = _lag_lines(caplog)
    assert len(lines) == 1
    assert "3.2s" in lines[0]
    assert "52.40.255.127:443 rx_queue=9B" in lines[0]
    # The collector's stall-time header is replaced, not repeated.
    assert "=== STALL ENRICHMENT ===" not in lines[0]
    assert calls == [3.2]
    assert dump_file.writes == []


def test_lag_below_threshold_captures_nothing(caplog) -> None:
    wd, calls, dump_file, _clock = _lag_watchdog(caplog)

    _beat(wd, 0.4)

    assert _lag_lines(caplog) == []
    assert calls == []


def test_lag_episode_logs_once_until_the_loop_recovers(caplog) -> None:
    wd, calls, dump_file, clock = _lag_watchdog(caplog)

    _beat(wd, 3.2)
    clock[0] = 65.0  # still lagging, past the cooldown: same episode
    _beat(wd, 4.8)
    assert len(_lag_lines(caplog)) == 1
    assert calls == [3.2]

    _beat(wd, 0.1)  # a healthy beat ends the episode
    clock[0] = 130.0
    _beat(wd, 2.5)
    assert len(_lag_lines(caplog)) == 2
    assert calls == [3.2, 2.5]
    assert dump_file.writes == []


def test_stall_already_captured_by_check_is_not_captured_again(caplog) -> None:
    wd, calls, dump_file, clock = _lag_watchdog(caplog)

    clock[0] = 16.0  # silent past enrich_after: check() captures this stall
    wd.check()
    assert calls == [16.0]

    _beat(wd, 11.0)  # the recovery beat of that same stall
    assert _lag_lines(caplog) == []
    assert calls == [16.0]
    assert dump_file.writes == []


def test_capture_in_flight_blocks_a_second_one(caplog) -> None:
    wd, calls, dump_file, clock = _lag_watchdog(caplog)

    assert wd.claim_lag_enrichment(3.0)
    wd.claim_lag_enrichment(0.1)  # episode ends, capture still running
    clock[0] = 61.0  # past the cooldown
    assert not wd.claim_lag_enrichment(3.0)
    wd.log_lag_enrichment(3.0)
    wd.claim_lag_enrichment(0.1)
    assert wd.claim_lag_enrichment(3.0)


def test_lag_episodes_inside_the_cooldown_capture_once(caplog) -> None:
    wd, calls, dump_file, clock = _lag_watchdog(caplog)

    for second in range(0, 60, 10):  # laggy, healthy, laggy... for a minute
        clock[0] = float(second)
        _beat(wd, 3.0)
        _beat(wd, 0.1)
    assert calls == [3.0]

    clock[0] = 60.0
    _beat(wd, 3.0)
    assert calls == [3.0, 3.0]
    assert dump_file.writes == []
