"""``atomic_write.read_json_or`` signals a read-degrade the hand-written guards muted.

Many small readers in the tree are shaped
``try: json.loads(path.read_text()) except (OSError, ...): return <fallback>``.
On Windows a read of a whole, correct file raises ``PermissionError``
(``WinError 32``) while another handle holds it open for a
tmp-file-plus-``os.replace`` write -- the read twin of the window
``replace_with_retry`` survives. POSIX permits the read, so the fault only ever
surfaces on the ``Backend Tests (Windows)`` matrix. Those guards fold that
transient failure into the SAME silent fallback returned for a genuinely absent
or malformed file, so a benign miss and a degraded read are indistinguishable
in ``kirocrew logs``.

``windows_sim.read_sharing_violation`` reproduces the fault deterministically on
any OS by faulting ``Path.read_bytes`` -- the call ``read_bytes_with_retry``
makes -- so these drive the exact code path a Windows host would take. They pin
the leg-splitting, the single rate-limited warning, and its no-path/no-content
shape; the real OS behaviour is proved by the win32 canary.
"""

from __future__ import annotations

import hashlib
import json
import logging

import pytest
from windows_sim import read_sharing_violation

from kiro_crew import atomic_write as aw
from kiro_crew import platform_compat


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """Keep the bounded retry loop instant; behaviour, not timing, is pinned."""
    monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)


@pytest.fixture(autouse=True)
def _fresh_dedup_state(monkeypatch):
    """Each test starts with an empty per-process dedup index."""
    monkeypatch.setattr(aw, "_READ_DEGRADE_SEEN", aw.OrderedDict())


@pytest.fixture
def logger():
    return logging.getLogger("test.read_json_or.site")


def _written(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


# -- criterion 1: a non-absent OSError on a file that EXISTS -> one WARNING -----


def test_read_degrade_emits_one_warning_and_returns_default(tmp_path, logger, monkeypatch, caplog):
    """A file that exists but whose read faults: exactly one WARNING, same fallback."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "on_event_loop", lambda: True)  # single attempt, no sleep
    path = _written(tmp_path / "signals.json", {"schema": 1})

    with caplog.at_level(logging.WARNING, logger=logger.name):
        with read_sharing_violation(match="signals.json", times=10_000):
            result = aw.read_json_or(path, "FALLBACK", logger=logger, what="issue signals")

    assert result == "FALLBACK"
    recs = [r for r in caplog.records if r.name == logger.name]
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING


def test_warning_names_the_error_type_and_a_fixed_outcome(tmp_path, logger, monkeypatch, caplog):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "on_event_loop", lambda: True)
    path = _written(tmp_path / "signals.json", {"schema": 1})

    with caplog.at_level(logging.WARNING, logger=logger.name):
        with read_sharing_violation(match="signals.json", times=10_000):
            aw.read_json_or(path, None, logger=logger, what="issue signals")

    msg = caplog.records[-1].getMessage()
    assert "error_type=PermissionError" in msg
    assert "outcome=default_returned" in msg
    assert "what=issue signals" in msg


def test_warning_carries_no_path_and_no_file_content(tmp_path, logger, monkeypatch, caplog):
    """The line must never become a channel for the path or the bytes that failed."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "on_event_loop", lambda: True)
    secret_name = "a-very-distinctive-filename-9f3c1.json"
    path = _written(tmp_path / secret_name, {"a-distinctive-json-key-x7z": "v"})

    with caplog.at_level(logging.WARNING, logger=logger.name):
        with read_sharing_violation(match=secret_name, times=10_000):
            aw.read_json_or(path, None, logger=logger, what="issue signals")

    msg = caplog.records[-1].getMessage()
    assert secret_name not in msg
    assert str(path) not in msg
    assert "a-distinctive-json-key-x7z" not in msg


def test_degrade_is_attributed_to_the_callers_logger(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "on_event_loop", lambda: True)
    site_logger = logging.getLogger("kirocrew.some.specific.site")
    path = _written(tmp_path / "cfg.json", {"k": 1})

    with caplog.at_level(logging.WARNING, logger=site_logger.name):
        with read_sharing_violation(match="cfg.json", times=10_000):
            aw.read_json_or(path, None, logger=site_logger, what="cfg")

    assert any(r.name == "kirocrew.some.specific.site" for r in caplog.records)


# -- criterion 2: absent / malformed / clean -> NO new signal, same fallback ---


def test_absent_file_is_silent(tmp_path, logger, caplog):
    with caplog.at_level(logging.WARNING, logger=logger.name):
        result = aw.read_json_or(tmp_path / "gone.json", {"fb": 1}, logger=logger, what="x")
    assert result == {"fb": 1}
    assert [r for r in caplog.records if r.name == logger.name] == []


def test_malformed_json_is_silent(tmp_path, logger, caplog):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        result = aw.read_json_or(path, "DEF", logger=logger, what="x")
    assert result == "DEF"
    assert [r for r in caplog.records if r.name == logger.name] == []


def test_undecodable_bytes_are_silent(tmp_path, logger, caplog):
    path = tmp_path / "bytes.json"
    path.write_bytes(b"\xff\xfe not utf-8 \x80")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        result = aw.read_json_or(path, "DEF", logger=logger, what="x")
    assert result == "DEF"
    assert [r for r in caplog.records if r.name == logger.name] == []


def test_clean_read_returns_the_parsed_value_silently(tmp_path, logger, caplog):
    path = _written(tmp_path / "ok.json", {"a": 1, "b": [2, 3]})
    with caplog.at_level(logging.WARNING, logger=logger.name):
        result = aw.read_json_or(path, None, logger=logger, what="x")
    assert result == {"a": 1, "b": [2, 3]}
    assert [r for r in caplog.records if r.name == logger.name] == []


# -- criterion 3: the OSError leg fires identically on every platform ----------


def test_oserror_leg_emits_on_posix_too(tmp_path, logger, monkeypatch, caplog):
    """A non-absent OSError signals identically whether or not IS_WINDOWS."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    path = _written(tmp_path / "signals.json", {"schema": 1})

    with caplog.at_level(logging.WARNING, logger=logger.name):
        with read_sharing_violation(match="signals.json", times=1):
            result = aw.read_json_or(path, "FALLBACK", logger=logger, what="x")

    assert result == "FALLBACK"
    recs = [r for r in caplog.records if r.name == logger.name]
    assert len(recs) == 1
    assert "error_type=PermissionError" in recs[0].getMessage()


# -- the rate limit: one line per (logger, path) per process by default --------


def test_degrade_warns_once_per_logger_and_path(tmp_path, logger, monkeypatch, caplog):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "on_event_loop", lambda: True)
    path = _written(tmp_path / "hot.json", {"k": 1})

    with caplog.at_level(logging.WARNING, logger=logger.name):
        with read_sharing_violation(match="hot.json", times=10_000):
            for _ in range(5):
                aw.read_json_or(path, None, logger=logger, what="x")

    assert len([r for r in caplog.records if r.name == logger.name]) == 1


def test_dedup_store_holds_fixed_size_digests_never_the_path(tmp_path, logger, monkeypatch, caplog):
    """The dedup key is a 16-byte digest, not the caller-controlled path string.

    A path kept verbatim is unbounded caller-controlled memory; a fixed-size
    digest is not. Assert the stored key is a 16-byte ``bytes`` and that the
    path text does not appear anywhere in the store.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "on_event_loop", lambda: True)
    path = _written(tmp_path / "distinctive-name.json", {"k": 1})

    with caplog.at_level(logging.WARNING, logger=logger.name):
        with read_sharing_violation(match="distinctive-name.json", times=10_000):
            aw.read_json_or(path, None, logger=logger, what="x")

    keys = list(aw._READ_DEGRADE_SEEN)
    assert len(keys) == 1
    assert isinstance(keys[0], bytes) and len(keys[0]) == 16
    # The path must not survive anywhere in the store, in any byte form.
    assert all(b"distinctive-name" not in k for k in keys)


def _degrade_digest(logger_name: str, path) -> bytes:
    """Reproduce the helper's dedup key so the test can assert on specific entries."""
    return hashlib.blake2b(
        f"{logger_name}\x00{path}".encode("utf-8", "surrogatepass"), digest_size=16
    ).digest()


def test_dedup_store_is_bounded_and_evicts_oldest(tmp_path, logger, monkeypatch, caplog):
    """A flood of distinct contended paths cannot grow the store without limit,
    and the eviction is LRU: a re-touched entry is kept fresh and warns only
    once, while the least-recently-used digest is dropped.

    The warning count is what distinguishes LRU from a plain FIFO cache. Under
    LRU, re-touching ``kept`` before each new path moves it to the fresh end, so
    it is never the eviction victim: every re-touch is a dedup hit and ``kept``
    warns exactly once. Under FIFO (no ``move_to_end``), ``kept`` ages out and,
    when re-touched after eviction, is re-inserted and warns AGAIN -- so a FIFO
    store would warn for ``kept`` two or more times and fail this test. (An
    eviction-membership assertion cannot discriminate: a re-touch re-inserts the
    evicted entry either way, so ``kept`` would be present under FIFO too.)"""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "on_event_loop", lambda: True)
    monkeypatch.setattr(aw, "_READ_DEGRADE_SEEN_CAP", 8)

    first = _written(tmp_path / "f0.json", {})
    first_digest = _degrade_digest(logger.name, first)
    kept = _written(tmp_path / "keep.json", {})

    with caplog.at_level(logging.WARNING, logger=logger.name):
        with read_sharing_violation(times=10_000):
            aw.read_json_or(first, None, logger=logger, what="x")
            aw.read_json_or(kept, None, logger=logger, what="kept")  # inserted early
            assert first_digest in aw._READ_DEGRADE_SEEN  # oldest, present before the flood
            for i in range(1, 50):
                # Re-touch ``kept`` mid-flood so LRU moves it to the fresh end;
                # under FIFO it would have aged out and been re-warned on re-touch.
                aw.read_json_or(kept, None, logger=logger, what="kept")
                p = _written(tmp_path / f"f{i}.json", {})
                aw.read_json_or(p, None, logger=logger, what="x")

    # Never exceeds the cap, however many distinct paths degrade.
    assert len(aw._READ_DEGRADE_SEEN) <= 8
    # The oldest entry was actually evicted, not merely counted against the cap.
    assert first_digest not in aw._READ_DEGRADE_SEEN
    # LRU, not FIFO: ``kept`` was re-touched every iteration, so an LRU store
    # keeps it fresh and warns for it EXACTLY ONCE; a FIFO store would evict and
    # re-warn it, giving two or more ``kept`` warnings.
    kept_warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "what=kept" in r.getMessage()
    ]
    assert len(kept_warnings) == 1, f"expected 1 kept warning (LRU), got {len(kept_warnings)}"


def test_two_paths_each_get_their_own_line(tmp_path, logger, monkeypatch, caplog):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "on_event_loop", lambda: True)
    a = _written(tmp_path / "a.json", {})
    b = _written(tmp_path / "b.json", {})

    with caplog.at_level(logging.WARNING, logger=logger.name):
        with read_sharing_violation(times=10_000):
            aw.read_json_or(a, None, logger=logger, what="x")
            aw.read_json_or(b, None, logger=logger, what="x")

    assert len([r for r in caplog.records if r.name == logger.name]) == 2


# -- one converted site proves the wiring end to end --------------------------


def test_converted_site_read_signals_emits_the_degrade(tmp_path, monkeypatch, caplog):
    """issue_radar ``read_signals`` (site 14) now signals a read-degrade.

    Before this change its ``except (OSError, json.JSONDecodeError): return {}``
    folded the Windows sharing-violation window into the same silent ``{}`` a
    genuinely absent cache returns.
    """
    from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "on_event_loop", lambda: True)
    path = crew_runtime.signals_path("o", "r", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": crew_runtime.SIGNALS_SCHEMA, "items": {}}), "utf-8")

    with caplog.at_level(logging.WARNING, logger=crew_runtime.logger.name):
        with read_sharing_violation(match=path.name, times=10_000):
            result = crew_runtime.read_signals("o", "r", tmp_path)

    assert result == {}  # unchanged fallback
    recs = [r for r in caplog.records if r.name == crew_runtime.logger.name]
    assert len(recs) == 1
    assert "outcome=default_returned" in recs[0].getMessage()
