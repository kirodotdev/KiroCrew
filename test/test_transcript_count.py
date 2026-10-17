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
