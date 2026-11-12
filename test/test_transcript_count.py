"""``count_messages`` must equal ``len`` of what ``_read_messages`` returns.

The count path replaces ``len(self._read_messages(key))`` on the consolidator's
startup scan, so it must agree with the full parse on EVERY input, including
the ones where ``str.splitlines`` / ``str.strip`` / ``json.loads`` do something
surprising.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew import history_projection, transcript_count
from kiro_crew.history import ConversationLog

_EDGE_LINES: list[bytes] = [
    b'{"role": "user", "content": "hi"}',
    b'{"_type": "metadata", "title": "t"}',
    b'{"_type": "metad\\u0061ta"}',  # escaped spelling IS metadata
    b'{"_type": "metadata", "_type": "x"}',  # duplicate key: last wins -> message
    b'{"_type": "x", "_type": "metadata"}',  # last wins -> metadata
    b'{"_type": ["metadata"]}',
    b'{"_type": null}',
    b"[1, 2, 3]",
    b'"just a string"',
    b"42",
    b"null",
    b"",
    b"   ",
    b'\t{"a": 1}\t',
    b"{not json",
    b'{"a": NaN, "b": Infinity, "c": -Infinity}',
    b'{"big": 1e400}',
    b'{"bigint": 123456789012345678901234567890}',
    b'{"f": 0.1, "g": 1.7976931348623157e308, "h": 5e-324, "i": -0.0}',
    b'{"lone": "\\ud800"}',
    b'{"pair": "\\ud83d\\ude00"}',
    '{"u2028": "a\u2028b"}'.encode(),
    '{"u2029": "a\u2029b"}'.encode(),
    '{"nel": "a\u0085b"}'.encode(),
    '{"nbsp": 1}\u00a0'.encode(),
    '\u3000{"ideo": 1}'.encode(),
    '\ufeff{"bom": 1}'.encode(),
    b'{"cr": 1}\r',
    b'{"a":\r1}',
    b'{"x": 1}\x0b{"y": 2}',
    b'{"x": 1}\x0c{"y": 2}',
    b'{"x": 1}\x1c{"y": 2}',
    b'{"x": 1}\x1f',
    b'{"ctl": "a\x01b"}',
    b'{"esc": "\\q"}',
    b'{"nested": {"_type": "metadata"}, "role": "assistant"}',
    b'{"deep": ' + b"[" * 200 + b"]" * 200 + b"}",
    b'{"a": 1} {"b": 2}',
    b'{"a": 1}  ',
    b'{"emoji": "\xf0\x9f\x98\x80", "cjk": "\xe4\xb8\xad"}',
    b'{"k": true, "l": false, "m": [], "n": {}}',
    b'{"num": -12, "e": 1E5, "z": 0, "neg0": -0}',
]


def _write(path: Path, lines: list[bytes], *, terminator: bytes = b"\n") -> None:
    path.write_bytes(terminator.join(lines) + terminator)


def _expected(base: Path, path: Path) -> list[dict]:
    """The HEAD full read of exactly *path* (``_path`` pinned, no key folding)."""
    log = ConversationLog(base_dir=base)
    log._path = lambda _key: path  # type: ignore[method-assign]
    return log._read_messages_locked(path.stem, gen=None, flock_witness=None)


def _check(tmp_path: Path, path: Path) -> None:
    assert transcript_count.count_messages(path) == len(_expected(tmp_path, path))


@pytest.mark.parametrize("terminator", [b"\n", b"\r\n", b"\r"])
def test_edge_lines_match_full_read(tmp_path: Path, terminator: bytes) -> None:
    path = tmp_path / "edge.jsonl"
    _write(path, _EDGE_LINES, terminator=terminator)
    _check(tmp_path, path)


def test_each_edge_line_alone(tmp_path: Path) -> None:
    for i, line in enumerate(_EDGE_LINES):
        path = tmp_path / f"one_{i}.jsonl"
        _write(path, [line])
        _check(tmp_path, path)


def test_no_trailing_newline_and_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "nonl.jsonl"
    path.write_bytes(b'{"a": 1}\n{"b": 2}')
    _check(tmp_path, path)
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    _check(tmp_path, empty)


def test_invalid_utf8_raises_like_full_read(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_bytes(b'{"a": 1}\n{"b": "\xff"}\n')
    with pytest.raises(UnicodeDecodeError):
        _expected(tmp_path, path)
    with pytest.raises(UnicodeDecodeError):
        transcript_count.count_messages(path)


def test_huge_integer_raises_like_full_read(tmp_path: Path) -> None:
    # json.loads refuses ints over sys.int_info.str_digits_check_threshold digits
    # with ValueError (not JSONDecodeError), so the full read raises; so must we.
    path = tmp_path / "hugeint.jsonl"
    _write(path, [b'{"n": ' + b"9" * 5000 + b"}"])
    with pytest.raises(ValueError):
        _expected(tmp_path, path)
    with pytest.raises(ValueError):
        transcript_count.count_messages(path)


def test_unconsolidated_count_uses_streamed_count(tmp_path: Path) -> None:
    log = ConversationLog(base_dir=tmp_path)
    key = "dashboard:abc"
    for i in range(7):
        log.append(key, "user", f"m{i}")
    total = len(log._read_messages(key))
    log._msg_cache.clear()
    assert log._count_messages(key) == total
    assert log.unconsolidated_count(key) == total
    # The count must not leave the parsed transcript resident in the cache.
    assert log._msg_cache.get(key) is None


def test_consolidation_counts_warms_the_cache_for_the_idle_sweep(tmp_path: Path) -> None:
    # The idle sweep calls consolidation_counts on the event loop every tick, so
    # it must read through the cache: a later tick is a hit, not a re-stream.
    log = ConversationLog(base_dir=tmp_path)
    key = "dashboard:abc"
    for i in range(4):
        log.append(key, "user", f"m{i}")
    log._msg_cache.clear()
    assert log.consolidation_counts(key) == (4, 4)
    assert log._msg_cache.get(key) is not None
    with patch.object(history_projection, "count_messages") as streamed:
        assert log.consolidation_counts(key) == (4, 4)
    streamed.assert_not_called()


_json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**70), max_value=2**70),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=20),
)
_json_values = st.recursive(
    _json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(max_size=8), children, max_size=4),
    ),
    max_leaves=12,
)


@settings(
    max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    rows=st.lists(
        st.one_of(
            _json_values.map(lambda v: json.dumps(v, ensure_ascii=False).encode()),
            st.dictionaries(
                st.sampled_from(["_type", "role", "content"]), _json_values, max_size=3
            ).map(lambda v: json.dumps(v, ensure_ascii=False).encode()),
            st.binary(max_size=12).filter(lambda b: b"\n" not in b),
        ),
        max_size=12,
    )
)
def test_random_rows_match_full_read(tmp_path: Path, rows: list[bytes]) -> None:
    path = tmp_path / "rand.jsonl"
    _write(path, rows)
    try:
        expected = _expected(tmp_path, path)
    except UnicodeDecodeError:
        with pytest.raises(UnicodeDecodeError):
            transcript_count.count_messages(path)
        return
    assert transcript_count.count_messages(path) == len(expected)


@pytest.mark.skipif(
    not os.environ.get("KIROCREW_TRANSCRIPT_CORPUS"),
    reason="set KIROCREW_TRANSCRIPT_CORPUS to a directory of transcript COPIES",
)
def test_real_corpus_matches_full_read() -> None:
    corpus = Path(os.environ["KIROCREW_TRANSCRIPT_CORPUS"])
    files = sorted(corpus.glob("*.jsonl"))
    assert files
    for path in files:
        try:
            expected = _expected(corpus, path)
        except (UnicodeDecodeError, ValueError):
            continue
        assert transcript_count.count_messages(path) == len(expected), path.name


def _lines_parsed(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count every transcript line a count parses (the work a startup scan does)."""
    parsed = [0]
    real = transcript_count.message_row

    def counting(line: str) -> dict | None:
        parsed[0] += 1
        return real(line)

    monkeypatch.setattr(transcript_count, "message_row", counting)
    return parsed


def _unconsolidated_after_restart(base: Path, key: str) -> int:
    # A new instance has no message cache, as at gateway start.
    log = ConversationLog(base_dir=base)
    log.init()
    return log.unconsolidated_count(key)


@pytest.mark.parametrize("appends", [10, 100, 400, 1600])
def test_startup_after_appends_reads_no_transcript_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, appends: int
) -> None:
    base = tmp_path / "sessions"
    log = ConversationLog(base_dir=base)
    log.init()
    log.append("k", "user", "first")
    assert _unconsolidated_after_restart(base, "k") == 1  # counts once, stores
    for i in range(appends):
        log.append("k", "assistant", f"m{i}")
    parsed = _lines_parsed(monkeypatch)
    assert _unconsolidated_after_restart(base, "k") == appends + 1
    assert parsed[0] == 0


def test_a_transcript_without_a_stored_count_is_counted_once_then_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "sessions"
    log = ConversationLog(base_dir=base)
    log.init()
    for i in range(5):
        log.append("k", "user", f"m{i}")
    for sidecar in (base / transcript_count.COUNT_DIR).glob("*"):
        sidecar.unlink()  # a transcript written before the count was stored
    parsed = _lines_parsed(monkeypatch)
    assert _unconsolidated_after_restart(base, "k") == 5
    assert parsed[0] == 6  # metadata line + 5 rows, once
    assert _unconsolidated_after_restart(base, "k") == 5
    assert parsed[0] == 6


def test_a_rewritten_transcript_is_recounted(tmp_path: Path) -> None:
    base = tmp_path / "sessions"
    log = ConversationLog(base_dir=base)
    log.init()
    for i in range(4):
        log.append("k", "user", f"m{i}")
    assert _unconsolidated_after_restart(base, "k") == 4
    log.rewrite_session("k", [{"role": "user", "content": "only"}])
    assert _unconsolidated_after_restart(base, "k") == 1


def _stored(base: Path, key: str) -> int | None:
    return transcript_count.stored_count(ConversationLog(base_dir=base)._path(key))


def _full(base: Path, key: str) -> int:
    return transcript_count.count_messages(ConversationLog(base_dir=base)._path(key))


def _fixture(tmp_path: Path) -> tuple[Path, ConversationLog]:
    base = tmp_path / "sessions"
    log = ConversationLog(base_dir=base)
    log.init()
    for i in range(6):
        log.append("k", "user" if i % 2 else "assistant", f"m{i} \u2028 \u00e9")
    assert _unconsolidated_after_restart(base, "k") == 6  # stores
    assert _stored(base, "k") == 6
    return base, log


def _same_as_full_count(base: Path) -> None:
    """The restart answer equals the old full count, whatever was done to the file."""
    full = _full(base, "k")
    assert _unconsolidated_after_restart(base, "k") == full
    assert _stored(base, "k") == full  # and the stored value is now right too


def test_stored_count_equals_full_count_after_appends(tmp_path: Path) -> None:
    base, log = _fixture(tmp_path)
    for i in range(7):
        log.append("k", "assistant", f"more {i}")
        assert _stored(base, "k") == _full(base, "k")
    _same_as_full_count(base)


def test_append_of_a_metadata_shaped_entry_adds_nothing(tmp_path: Path) -> None:
    base, log = _fixture(tmp_path)
    log.append("k", "user", "x", entry={"_type": "metadata", "role": "user", "content": "x"})
    _same_as_full_count(base)


@pytest.mark.parametrize("how", ["truncate_rows", "rewrite", "same_size_other_mtime", "grow"])
def test_a_file_changed_outside_the_append_path_is_recounted(tmp_path: Path, how: str) -> None:
    base, log = _fixture(tmp_path)
    path = log._path("k")
    raw = path.read_bytes()
    if how == "truncate_rows":
        path.write_bytes(b"\n".join(raw.split(b"\n")[:3]) + b"\n")
    elif how == "rewrite":
        log.rewrite_session("k", [{"role": "user", "content": "only"}])
    elif how == "same_size_other_mtime":
        lines = raw.split(b"\n")
        lines[2] = lines[2].replace(b'"role"', b'"rolf"')  # same size, still a dict row
        path.write_bytes(b"\n".join(lines))
        os.utime(path, ns=(1, 1))
    else:
        with open(path, "ab") as handle:  # a foreign writer, not the append path
            handle.write(b'{"role": "user", "content": "foreign"}\n')
    _same_as_full_count(base)


def test_a_torn_last_line(tmp_path: Path) -> None:
    base, log = _fixture(tmp_path)
    with open(log._path("k"), "ab") as handle:
        handle.write(b'{"role": "user", "content": "tor')  # killed mid-write
    _same_as_full_count(base)
    log.append("k", "user", "after the tear")  # starts a fresh line
    assert _stored(base, "k") == _full(base, "k") == 7
    _same_as_full_count(base)


def test_an_empty_file(tmp_path: Path) -> None:
    base = tmp_path / "sessions"
    log = ConversationLog(base_dir=base)
    log.init()
    log._path("k").write_bytes(b"")
    _same_as_full_count(base)
    log.append("k", "user", "first")
    _same_as_full_count(base)


@pytest.mark.parametrize("damage", ["missing", "garbage", "wrong_types", "wrong_count_keys"])
def test_a_missing_or_corrupt_sidecar_is_recounted_and_stored(tmp_path: Path, damage: str) -> None:
    base, log = _fixture(tmp_path)
    sidecar = transcript_count._sidecar(log._path("k"))
    if damage == "missing":
        sidecar.unlink()
    elif damage == "garbage":
        sidecar.write_bytes(b"\xff\x00{not json")
    elif damage == "wrong_types":
        sidecar.write_text('{"size": "1", "mtime_ns": 2.5, "count": null}')
    else:
        sidecar.write_text("[]")
    assert _stored(base, "k") is None
    _same_as_full_count(base)
    log.append("k", "user", "next")
    _same_as_full_count(base)
