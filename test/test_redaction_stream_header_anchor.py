"""A header value decided from a name far back stays masked when streamed.

The stream keeps ``CONTEXT`` committed characters of look-behind. A Cookie pair or a
scheme header's value is decided from its header name, which can sit further back than
that, so the readers keep the name held with the value. All values are synthetic.
"""

from __future__ import annotations

import base64
import threading
from collections.abc import Callable

import pytest
from redaction_structured_cases import PEM_BEGIN, PEM_BODY, PEM_END

from kiro_crew.security import StreamRedactor as ProductionStream
from kiro_crew.security import text_redaction
from kiro_crew.security.redaction_structured import TAG, StreamRedactor, redact
from kiro_crew.security.redaction_structured.headers import _COOKIE_LINE_HOLD
from kiro_crew.security.redaction_structured.model import SECRET, Hit

COOKIE = "Q" * 20 + "x9Z" * 10
TOKEN = "Zq7Rk2Lm9Xw4Vb8Nc3Hd6Jf1Pg5Ts0Ye"
BASIC = base64.b64encode(b"user:Zx9Qm4Lr7Vk2Np8Wq3T").decode()
SIZES = (1, 7, 64, 512, 1024, 0)  # 0: the whole text in one feed


def _feed(stream: object, text: str, size: int) -> str:
    size = size or len(text)
    out = "".join(stream.feed(text[i : i + size]) for i in range(0, len(text), size))  # type: ignore[attr-defined]
    return out + stream.flush()  # type: ignore[attr-defined]


def _cookie_cases() -> list[tuple[str, str]]:
    far = "a" * 1500
    many = "; ".join(f"p{i}=v{i}" for i in range(300))
    return [
        ("issue_38129", f"Cookie: theme={far}; session={COOKIE}\nafter"),
        ("many_short_pairs", f"Cookie: {many}; sid={COOKIE}\nafter"),
        ("lowercase_after_a_line", f"GET / HTTP/1.1\ncookie: theme={far}; sessionid={COOKIE}\nok"),
        ("json_quoted", f'{{"Cookie": "theme={far}; session={COOKIE}"}}\n'),
        ("assignment", f"Cookie=theme={far}; session={COOKIE}\nafter"),
        ("crlf", f"Cookie: theme={far}; session={COOKIE}\r\nHost: example.test\r\n"),
        ("ends_the_text", f"Cookie: theme={far}; session={COOKIE}"),
        ("two_session_pairs", f"Cookie: session={COOKIE}; theme={far}; auth_token={TOKEN}\n"),
        ("blanks_before_the_separator", f"Cookie{' ' * 1100}: theme=a; session={COOKIE}\nok"),
        ("set_cookie_blanks_after_the_colon", f"Set-Cookie:{' ' * 1100}session={COOKIE}; Path=/\n"),
    ]


def _scheme_cases() -> list[tuple[str, str]]:
    return [
        ("issue_39803_bearer", f" Authorization: Bearer{' ' * 1100}\n{TOKEN}\nafter"),
        ("basic", f"Authorization: Basic{' ' * 1100}\n{BASIC}\nafter"),
        ("lowercase_crlf", f"authorization: bearer{' ' * 1100}\r\n{TOKEN}\r\n"),
        ("proxy_authorization", f"Proxy-Authorization: Bearer{' ' * 3000}\n{TOKEN}\n"),
        ("start_of_text_blanks_both_sides", f"Authorization: Bearer{' ' * 1100}\n   {TOKEN}"),
        ("api_key_blanks_on_the_line", f"x-api-key:{' ' * 1100}{TOKEN}\nafter"),
    ]


_ALL = _cookie_cases() + _scheme_cases()
_SECRET = {name: (COOKIE if name in dict(_cookie_cases()) else TOKEN) for name, _ in _ALL}
_SECRET["basic"] = BASIC


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize(("name", "text"), _ALL, ids=[n for n, _ in _ALL])
def test_structured_stream_equals_whole_text(name: str, text: str, size: int) -> None:
    whole = redact(text)[0]
    assert _SECRET[name] not in whole
    assert _feed(StreamRedactor(), text, size) == whole


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize(("name", "text"), _ALL, ids=[n for n, _ in _ALL])
def test_production_stream_masks_it(name: str, text: str, size: int) -> None:
    out = _feed(ProductionStream(), text, size)
    assert _SECRET[name] not in out
    assert out == text_redaction.redact(text)


@pytest.mark.parametrize("stream", [StreamRedactor, ProductionStream])
def test_a_short_benign_cookie_line_streams_unchanged(stream: type) -> None:
    text = "Cookie: theme=dark; lang=en\nHost: example.test\n"
    assert _feed(stream(), text, 1) == text


@pytest.mark.parametrize("stream", [StreamRedactor, ProductionStream])
def test_a_cookie_line_is_held_no_further_than_its_bound(stream: type) -> None:
    """Past the bound the rest of a running Cookie line is one tag (fail closed), and the
    text after its line break streams on."""
    pairs = "; ".join(f"p{i}=v{i:04d}" for i in range(_COOKIE_LINE_HOLD // 8))
    text = f"head\nCookie: {pairs}; session={COOKIE}\nafter"
    s = stream()
    out = "".join(s.feed(text[i : i + 64]) for i in range(0, len(text), 64)) + s.flush()
    assert out == f"head\nCookie: {TAG}\nafter"


@pytest.mark.parametrize("stream", [StreamRedactor, ProductionStream])
def test_a_long_prose_line_naming_a_cookie_hides_only_that_line(stream: type) -> None:
    """Past the bound a running line that names a cookie is hidden to its line break
    (fail closed), and the text after the break streams."""
    text = "the cookie: " + "chocolate chip and oat " * 300 + "\nnext"
    out = _feed(stream(), text, 64)
    assert "chocolate" not in out
    assert out.endswith("\nnext")


@pytest.mark.parametrize("blanks", range(4083, 4090))
@pytest.mark.parametrize("stream", [StreamRedactor, ProductionStream])
def test_the_bound_crossed_inside_a_pair_name_keeps_the_pair_masked(
    stream: type, blanks: int
) -> None:
    """One character at a time, the bound falls inside the pair's name before its ``=``."""
    text = f"Cookie:{' ' * blanks}session={COOKIE}\nafter"
    out = _feed(stream(), text, 1)
    assert COOKIE not in out
    assert out.endswith("\nafter")


def test_a_hidden_cookie_line_stays_hidden_across_a_segment_flush() -> None:
    """A display boundary (a tool call) flushes the production stream mid-line; the
    line stays hidden to its break, so a pair after the boundary is not shown.
    The structured stream ends at its flush and takes no feed after it."""
    pairs = "; ".join(f"p{i}=v{i:04d}" for i in range(_COOKIE_LINE_HOLD // 8))
    s = ProductionStream()
    out = s.feed(f"Cookie: {pairs}; ") + s.release_at_boundary() + s.flush()
    out += s.feed(f"session={COOKIE}\nafter") + s.flush()
    assert COOKIE not in out
    assert out.endswith("\nafter")


_FAR_BLANKS = " " * (_COOKIE_LINE_HOLD + 100)
_BLANK_RUNS = [
    ("after_the_separator", f"Cookie:{_FAR_BLANKS}session={COOKIE}\nafter"),
    ("before_the_separator", f"Cookie{_FAR_BLANKS}: session={COOKIE}\nafter"),
    ("set_cookie", f"Set-Cookie:{_FAR_BLANKS}session={COOKIE}; Path=/\nafter"),
    ("tabs", f"Cookie:{chr(9) * (_COOKIE_LINE_HOLD + 100)}session={COOKIE}\nafter"),
]


@pytest.mark.parametrize("size", (1, 7, 64, 4096, 0))
@pytest.mark.parametrize("stream", [StreamRedactor, ProductionStream])
@pytest.mark.parametrize(("name", "text"), _BLANK_RUNS, ids=[n for n, _ in _BLANK_RUNS])
def test_a_cookie_name_followed_only_by_blanks_past_the_bound_hides_its_line(
    stream: type, name: str, text: str, size: int
) -> None:
    """Past the bound a name with only blanks after it hides its line, so a pair that
    comes after the blanks is not read without its name."""
    out = _feed(stream(), text, size)
    assert COOKIE not in out
    assert out.endswith("\nafter")


@pytest.mark.parametrize("stream", [StreamRedactor, ProductionStream])
def test_a_pair_after_a_blank_run_fed_alone_stays_masked(stream: type) -> None:
    s = stream()
    out = s.feed("Cookie:" + " " * 5000) + s.feed(f"session={COOKIE}\nafter") + s.flush()
    assert COOKIE not in out
    assert out.endswith("\nafter")


@pytest.mark.parametrize(("first", "size"), [(926, 4096), (5033, 1), (5033, 4), (5033, 4096)])
@pytest.mark.parametrize("key", ["auth_token", "session"])
@pytest.mark.parametrize("stream", [StreamRedactor, ProductionStream])
def test_a_pair_held_before_the_hidden_line_keeps_its_cookie_name(
    stream: type, key: str, first: int, size: int
) -> None:
    """A hold that stops the cut before the line to hide keeps the Cookie name buffered,
    so the line is still hidden on a later read and the value never streams."""
    text = f"Cookie:{' ' * 5000}{key}={TOKEN}\nafter"
    s = stream()
    rest = text[first:]
    out = s.feed(text[:first])
    out += "".join(s.feed(rest[i : i + size]) for i in range(0, len(rest), size)) + s.flush()
    assert TOKEN not in out
    assert out.endswith("\nafter")


@pytest.mark.parametrize("size", (0, 64, 1000))
@pytest.mark.parametrize("stream", [StreamRedactor, ProductionStream])
def test_a_run_dropped_inside_a_long_cookie_line_keeps_the_line_hidden(
    stream: type, size: int
) -> None:
    """A long value run decided inside a Cookie line past the bound is hidden with the
    line, so a pair after it on the same line is not read without its name."""
    head = "Cookie: theme=dark; aws_session_token=" + "R7xV" * 1100
    rest = f"; session={COOKIE}\nafter\n"
    s = stream()
    size = size or len(head)
    out = "".join(s.feed(head[i : i + size]) for i in range(0, len(head), size))
    out += s.feed(rest) + s.flush()
    assert COOKIE not in out
    assert out.endswith("\nafter\n")


def test_pending_text_stays_bounded_on_a_running_cookie_line() -> None:
    s = StreamRedactor()
    longest = 0
    s.feed("Cookie: ")
    for i in range(2000):
        s.feed(f"p{i}=v{i}; ")
        longest = max(longest, len(s._pending))  # noqa: SLF001
    s.flush()
    assert longest <= _COOKIE_LINE_HOLD + 64


def test_a_drop_inside_a_hidden_line_reads_every_chunk() -> None:
    """While a value is being dropped inside a hidden Cookie line, each chunk goes to the
    drop: only its carried tail stays buffered and every dropped character is counted."""
    s = ProductionStream()
    s.feed(f"Cookie: {_LONG}; Authorization: Bearer Zq7Rk2")
    longest = 0
    for _ in range(1500):
        s.feed("Lm9Xw4Vb8N")
        if s.discarding:
            longest = max(longest, len(s._buf))  # noqa: SLF001
    assert s._hiding  # noqa: SLF001
    assert longest <= 64
    assert s._discarded > 10_000  # noqa: SLF001
    assert (s.feed(" done\nafter") + s.flush()).endswith("\nafter")


_LONG = "; ".join(f"pref_{i}=v{i:04d}" for i in range(400))  # a benign line past the bound
_REPLY = "Here is what it says: a = b, and the request worked.\n" * 20
_BODY = PEM_BODY.split("\n")[1]
# (name, text, secret, text that must stream after the hidden line)
_LONG_LINES = [
    ("benign_then_reply", f"> Cookie: {_LONG}\n> Host: example.test\n{_REPLY}", "", _REPLY),
    (
        "session_then_bearer",
        f"Cookie: {_LONG}; sid={COOKIE}\nAuthorization: Bearer {TOKEN}\n",
        COOKIE,
        "\nAuthorization: Bearer [REDACTED: credential]\n",
    ),
    (
        "bearer_name_in_the_line",
        f"Cookie: {_LONG}; Authorization: Bearer\n{TOKEN}\nok",
        TOKEN,
        "\nok",
    ),
    (
        "key_block_starts_in_the_line",
        f"Cookie: {_LONG}; x={PEM_BEGIN}\n{PEM_BODY}\n{PEM_END}\nok",
        _BODY,
        "\nok",
    ),
    (
        "two_long_lines",
        f"Cookie: {_LONG}; a={COOKIE}\nCookie: {_LONG}; session={COOKIE}\nok",
        COOKIE,
        "\nok",
    ),
    (
        "crlf",
        f"Cookie: {_LONG}; session={COOKIE}\r\nHost: example.test\r\n",
        COOKIE,
        "\r\nHost: example.test\r\n",
    ),
]


@pytest.mark.parametrize("size", (1, 4, 64, 1000))
@pytest.mark.parametrize("stream", [StreamRedactor, ProductionStream])
@pytest.mark.parametrize(
    ("name", "text", "secret", "after"), _LONG_LINES, ids=[c[0] for c in _LONG_LINES]
)
def test_only_the_long_cookie_line_is_hidden(
    stream: type, name: str, text: str, secret: str, after: str, size: int
) -> None:
    """Past the bound only the rest of that line is hidden, not the reply after it."""
    out = _feed(stream(), text, size)
    assert not secret or secret not in out
    assert "pref_399" not in out
    assert out.endswith(after)


def test_a_scheme_word_at_the_window_end_waits_for_its_value() -> None:
    """The window ends right after ``Bearer``, with the header name far back."""
    s = StreamRedactor()
    head = "Authorization:" + " " * 1100 + "Bearer"
    out = s.feed(head) + s.feed(f" {TOKEN}\nafter") + s.flush()
    assert TOKEN not in out
    assert out.endswith("\nafter")


@pytest.mark.parametrize("stream", [StreamRedactor, ProductionStream])
def test_consecutive_cookie_lines_under_the_bound_read_once_per_line(
    stream: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chunk that only extends a held Cookie line skips the read, so many small chunks
    cost about one read per line, and the output is unchanged."""
    from kiro_crew.security.redaction_structured import compat
    from kiro_crew.security.redaction_structured import stream as structured

    line = "Cookie: " + "; ".join(f"p{i}=v{i:03d}" for i in range(440))[:3990] + "\n"
    text = line * 5
    whole = redact(text)[0] if stream is StreamRedactor else text_redaction.redact(text)
    reads = [0]
    real_scan, real_read = structured.scan, compat._read  # noqa: SLF001

    def scan(*a: object, **k: object) -> object:
        reads[0] += 1
        return real_scan(*a, **k)  # type: ignore[arg-type]

    def read(*a: object, **k: object) -> object:
        reads[0] += 1
        return real_read(*a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(structured, "scan", scan)
    monkeypatch.setattr(compat, "_read", read)
    assert _feed(stream(), text, 4) == whole
    assert reads[0] < len(text) // 4 // 20


def _hit_below(base: int) -> Hit:
    return Hit(base + 5, base + 100, "http.session_cookie", SECRET, "cookie", "known_key", anchor=0)


def _within(call: Callable[[], object]) -> list[object]:
    result: list[object] = []
    worker = threading.Thread(target=lambda: result.append(call()), daemon=True)
    worker.start()
    worker.join(30)
    return result


_CTX = "Cookie: theme=" + "a" * 30
_BUF = "x" * 5 + "V" * 95 + "y" * 20


def test_the_seam_cut_retreat_ends_for_an_anchor_already_emitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hit whose anchor sits in the look-behind holds the cut at the base, and the
    read returns (main loops forever here)."""
    from kiro_crew.security.redaction_structured import compat

    hit = _hit_below(len(_CTX))
    read = compat._Read(_CTX + _BUF, [hit], [hit], None, len(_CTX), None)  # noqa: SLF001
    monkeypatch.setattr(compat, "_read", lambda text, final=True: read)
    result = _within(lambda: compat.stream_read(_CTX, _BUF))
    assert result, "stream_read did not return"
    assert result[0].cut == 0 and result[0].undecided  # type: ignore[attr-defined]


def test_the_structured_cut_retreat_ends_for_an_anchor_already_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same guard in the structured stream's own commit."""
    from kiro_crew.security.redaction_structured import stream
    from kiro_crew.security.redaction_structured.engine import ScanResult

    hit = _hit_below(len(_CTX))
    monkeypatch.setattr(stream, "scan", lambda w, final, origin: ScanResult([hit], None))
    monkeypatch.setattr(stream, "resolve", lambda hits: hits)
    s = StreamRedactor()
    s._ctx = _CTX  # noqa: SLF001
    assert _within(lambda: s.feed(_BUF)) == [""]
