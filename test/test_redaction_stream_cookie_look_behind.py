"""Stream/whole parity for a Cookie header's name in the stream look-behind
(``headers.cookie_look_behind``): the name and separator a running Cookie line keeps
in front of the raw tail, read raw and in the shared ignorable view, so a later pair
is read from that name as whole text reads it."""

from __future__ import annotations

import pytest
from redaction_structured_cases import B64, synth

from kiro_crew.security.redaction_structured import TAG, StreamRedactor, redact
from kiro_crew.security.redaction_structured.stream import CONTEXT

_SET_COOKIE_TOKEN = "Zqr8eXogsl91eOcHdmweLBWS57JZIDk3Pq"


@pytest.mark.parametrize("chunk", [1, 7, 47, 256])
@pytest.mark.parametrize(
    "name", ["Cook\u200bie", "C\u200dookie", "Cookie\u2060", "Coo\u00adkie", "Cookie"]
)
def test_a_cookie_name_holding_an_ignorable_keeps_its_look_behind(name: str, chunk: int) -> None:
    # Whole text reads ``Cook\u200bie`` as a Cookie header in its ignorable view, so
    # the look-behind keeps that name too and a later session pair is read from it.
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor
    from kiro_crew.security import redact as production_redact

    text = f"{name}: theme={'q' * 1500}; session={_SET_COOKIE_TOKEN}\nafter"
    whole = production_redact(text)
    whole = whole if isinstance(whole, str) else whole[0]
    s = ProductionStreamRedactor()
    out = "".join(s.feed(text[i : i + chunk]) for i in range(0, len(text), chunk)) + s.flush()
    assert _SET_COOKIE_TOKEN not in whole
    assert out == whole


def _mixed_cookie_line(shape: str, name: str) -> str:
    # The lanes' shapes: a raw Set-Cookie and an ignorable-spelled plain Cookie name on
    # one line, in either order.
    value = f"theme={'q' * 1500}; session={_SET_COOKIE_TOKEN}; b=1"
    if shape == "json_set_first":
        return '{"h":{"Set-Cookie":"x=1","%s":"%s"}}\nnext\n' % (name, value)
    if shape == "set_first":
        return f"Set-Cookie: a=1; {name}: {value}\nafter\n"
    return f"{name}: theme={'q' * 1500}; Set-Cookie: a=1; session={_SET_COOKIE_TOKEN}; b=1\nafter\n"


@pytest.mark.parametrize("chunk", [1, 7, 47, 256])
@pytest.mark.parametrize("shape", ["json_set_first", "set_first", "cookie_first"])
@pytest.mark.parametrize("name", ["Cook\u200bie", "C\u200dookie", "Cookie\u2060", "Coo\u00adkie"])
def test_a_raw_set_cookie_does_not_hide_an_ignorable_cookie_name(
    name: str, shape: str, chunk: int
) -> None:
    # The look-behind reads the line's Cookie name raw and in the ignorable view, as
    # whole text does: a plain Cookie name in either reading wins over a Set-Cookie.
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor
    from kiro_crew.security import redact as production_redact

    text = _mixed_cookie_line(shape, name)
    whole = production_redact(text)
    whole = whole if isinstance(whole, str) else whole[0]
    s = ProductionStreamRedactor()
    out = "".join(s.feed(text[i : i + chunk]) for i in range(0, len(text), chunk)) + s.flush()
    assert _SET_COOKIE_TOKEN not in whole
    assert out == whole


def test_a_closed_credential_over_the_ceiling_in_a_cookie_line_keeps_the_line() -> None:
    # The token ceiling fires on a Cookie line whose credential has already closed:
    # the drop still runs to the line end, so the header name is not lost.
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor
    from kiro_crew.testing.synthetic_credentials import aws_secret

    value = synth(48, B64, 41)
    head = f"Cookie: theme={'a' * 4100} \"SessionToken\": \"{aws_secret()}\""
    s = ProductionStreamRedactor()
    out = s.feed(head) + s.feed(f"; session={value}\nafter") + s.flush()
    assert value not in out
    assert out.endswith("\nafter")
    assert value not in redact(f"{head}; session={value}\nafter")[0]


def _redactor(entry: str) -> tuple[object, str]:
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    if entry == "structured":
        return StreamRedactor(), "_pending"
    return ProductionStreamRedactor(), "_buf"


def _feeds(text: str, plan: object) -> list[str]:
    if isinstance(plan, int):
        return [text[i : i + plan] for i in range(0, len(text), plan)]
    cut = text.index(_SET_COOKIE_TOKEN)
    return [text[:cut], text[cut:]]


@pytest.mark.parametrize("entry", ["structured", "production"])
@pytest.mark.parametrize("plan", [1, 3, 16, 32, "value_after_name"])
def test_a_cookie_name_after_a_quoted_set_cookie_name_keeps_reading_every_pair(
    entry: str, plan: object
) -> None:
    # The reader reads this line's pairs from the plain ``cookie`` name after the
    # quoted ``Set-Cookie``; a kept window must carry that name, so ``auth`` after it
    # is still masked as the whole text masks it.
    value = synth(40, B64, 191)
    text = (
        '"Set-Cookie" : "cookie="'
        + "z" * 1500
        + '"; sessionid="1"; auth='
        + _SET_COOKIE_TOKEN
        + value
        + '"\nafter'
    )
    whole = redact(text)[0]
    assert _SET_COOKIE_TOKEN not in whole
    s, _ = _redactor(entry)
    out = "".join(s.feed(c) for c in _feeds(text, plan)) + s.flush()  # type: ignore[attr-defined]
    assert out == whole


@pytest.mark.parametrize("head", ["Cookie : ", "Cookie = ", '"Cookie" : ', "cookie\u2003:\u2003"])
@pytest.mark.parametrize("entry", ["structured", "production"])
def test_a_cookie_name_held_while_its_separator_is_still_to_come(entry: str, head: str) -> None:
    # Fed one character at a time, the name arrives before its separator: the
    # name is held until the separator decides it, so later pairs still read it.
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    value = synth(48, B64, 37)
    text = f"{head}{'x=y; ' * 1700}session={value}\nafter"
    s = StreamRedactor() if entry == "structured" else ProductionStreamRedactor()
    out = "".join(s.feed(ch) for ch in text[:3000]) + s.feed(text[3000:]) + s.flush()
    assert value not in out
    assert out.endswith("\nafter")


def test_a_cut_inside_a_table_name_retreats_and_takes_no_stand_in() -> None:
    from kiro_crew.security.redaction_structured.headers import (
        _header_name_max,
        _tail_from,
    )

    for name in ("x-goog-api-key", "Proxy-Authorization"[:14], "Set-Cookie"):
        assert len(name) <= _header_name_max()
        text = "a " + name + ": v"
        for cut in range(3, 2 + len(name)):
            assert _tail_from(text, cut) == text[2:]


def test_a_cut_inside_an_over_long_run_keeps_the_cut_behind_a_stand_in() -> None:
    from kiro_crew.security.redaction_structured.headers import (
        _header_name_max,
        _tail_from,
    )

    text = "a " + "VeryLongCustomPrefix-Api-Key" + ": v"
    cut = text.index("Api-Key")
    assert cut - 2 > _header_name_max()
    assert _tail_from(text, cut) == "-" + text[cut:]
    assert _tail_from(text, cut + 1) == "_" + text[cut + 1 :]


@pytest.mark.parametrize("entry", ["structured", "production"])
def test_a_line_cap_decided_from_a_kept_name_drops_from_the_origin(entry: str) -> None:
    value = synth(40, B64, 53)
    first, rest = "Cookie: a=1; ", f"{'b=2; ' * 1700}session={value}; c=3\nafter"
    assert value not in redact(first + rest)[0]
    s, _ = _redactor(entry)
    out = s.feed(first) + s.feed(rest) + s.flush()  # type: ignore[attr-defined]
    assert value not in out
    assert out.endswith("\nafter")


@pytest.mark.parametrize("chunk", [1, 7, 256, 1024, 4097])
@pytest.mark.parametrize("entry", ["structured", "production"])
@pytest.mark.parametrize("gap", [1500, 3000])
def test_a_session_cookie_past_the_look_behind_from_its_header_is_masked(
    entry: str, chunk: int, gap: int
) -> None:
    # The pair sits further from ``Cookie:`` than the stream's look-behind reaches,
    # so a window that starts after the header name cannot read it again.
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    value = synth(48, B64, 11)
    text = f"Cookie: theme={'a' * gap}; session={value}\nafter"
    s = StreamRedactor() if entry == "structured" else ProductionStreamRedactor()
    out = "".join(s.feed(text[i : i + chunk]) for i in range(0, len(text), chunk)) + s.flush()
    assert value not in out
    assert out.endswith(f"session={TAG}\nafter")


def _first_pair_cut_cases() -> list[tuple[str, str]]:
    return [
        ("before", "Cookie" + " " * 1040 + ": session="),
        ("at", "Cookie" + " " * (CONTEXT - 2) + ": session="),
        ("one_in", "Cookie" + " " * (CONTEXT - 3) + ": session="),
        ("mid_pair", "Cookie: theme=" + "q" * 1200 + "; session="),
        ("at_separator", "Cookie: theme=" + "q" * (CONTEXT - 8) + "; session="),
    ]


@pytest.mark.parametrize("entry", ["structured", "production"])
@pytest.mark.parametrize("back", [0, 3])
@pytest.mark.parametrize(("where", "head"), _first_pair_cut_cases())
def test_a_set_cookie_cut_around_its_first_pair_reads_as_the_whole_text(
    entry: str, back: int, where: str, head: str
) -> None:
    head = "Set-" + head
    value = synth(40, B64, 179)
    text = head + value + "\nafter"
    s, _ = _redactor(entry)
    split = len(head) - back
    out = s.feed(text[:split]) + s.feed(text[split:]) + s.flush()  # type: ignore[attr-defined]
    assert out == redact(text)[0], where


@pytest.mark.parametrize("plan", [1, 3, 16, 32, "value_after_name"])
@pytest.mark.parametrize(
    "head",
    [
        "Set-Cookie:" + " " * 1030 + "session=",
        "Set-Cookie:" + " " * 2000 + "session=",
        "Set-Cookie: session=",
    ],
)
def test_a_set_cookie_session_value_never_streams_once_its_name_is_cut(
    head: str, plan: object
) -> None:
    # The look-behind keeps a ``Set-Cookie`` name as it keeps ``Cookie``, so a session
    # value read after the name left the window is still read as that header's pair.
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    text = head + _SET_COOKIE_TOKEN + "; c=1\nafter\n"
    assert _SET_COOKIE_TOKEN not in redact(text)[0]
    s = ProductionStreamRedactor()
    out = "".join(s.feed(c) for c in _feeds(text, plan)) + s.flush()
    assert _SET_COOKIE_TOKEN not in out


def test_blanks_after_a_cookie_separator_keep_the_look_behind_bounded() -> None:
    from kiro_crew.security.redaction_structured.headers import cookie_look_behind

    kept = cookie_look_behind("Cookie:" + " " * 8191 + "a=1", CONTEXT)
    assert len(kept) <= CONTEXT + len("Cookie:")
    assert kept.startswith("Cookie:")


@pytest.mark.parametrize("entry", ["structured", "production"])
@pytest.mark.parametrize("back", [0, 3])
@pytest.mark.parametrize(("where", "head"), _first_pair_cut_cases())
def test_the_look_behind_cut_around_the_first_pair_reads_as_the_whole_text(
    entry: str, back: int, where: str, head: str
) -> None:
    # Everything before the session value is committed in one feed, so the cut lands
    # before, at, one past, inside or at a separator after the first pair's start.
    value = synth(40, B64, 171)
    text = head + value + "\nafter"
    s, _ = _redactor(entry)
    split = len(head) - back
    out = s.feed(text[:split]) + s.feed(text[split:]) + s.flush()  # type: ignore[attr-defined]
    assert out == redact(text)[0], where


def test_the_placeholder_names_where_the_cut_fell() -> None:
    from kiro_crew.security.redaction_structured.headers import cookie_look_behind

    between = "Cookie: a=1;" + " " * 1500
    assert cookie_look_behind(between, CONTEXT).startswith("Cookie:_=;")
    inside = "Cookie: a=" + "q" * 1500
    kept = cookie_look_behind(inside, CONTEXT)
    assert kept.startswith("Cookie:_=") and not kept.startswith("Cookie:_=;")


def test_the_separator_back_scan_costs_linear_work() -> None:
    # The back-scan reads the blanks back to the pair's separator with no budget, so
    # it masks the value after any blank run, and the package's work per input byte
    # stays flat as the run grows: within 2.2x from 32 KiB to 256 KiB of blanks, with the
    # whole run let go before the value arrives, counted in executed lines, never timed.
    from redaction_structured_cases import package_lines

    def per_byte(size: int) -> float:
        head = "Cookie: a=1;" + " " * size + "session="
        text = head + _SET_COOKIE_TOKEN + "; c=1\n"
        out: list[str] = []

        def run() -> None:
            s = StreamRedactor()
            out.extend((s.feed(head), s.feed(text[len(head) :])))
            out.append(s.flush())

        rate = package_lines(run) / len(text)
        assert _SET_COOKIE_TOKEN not in "".join(out), size
        return rate

    rates = [per_byte(size) for size in (32 << 10, 256 << 10)]
    assert max(rates) <= min(rates) * 2.2, rates


@pytest.mark.parametrize("chunk", [1, 3, 7])
@pytest.mark.parametrize("gap", [1024, 1040, 2000])
def test_a_set_cookie_after_a_cookie_pair_keeps_its_name_in_the_look_behind(
    gap: int, chunk: int
) -> None:
    # The look-behind keeps the last header name before the cut: here the Set-Cookie
    # whose first pair is still open, not the plain Cookie earlier on the line.
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor
    from kiro_crew.security import redact as production_redact

    text = f"Cookie: a=1; Set-Cookie:{' ' * gap}session={_SET_COOKIE_TOKEN}; c=1\nafter\n"
    whole = production_redact(text)
    whole = whole if isinstance(whole, str) else whole[0]
    s = ProductionStreamRedactor()
    out = "".join(s.feed(text[i : i + chunk]) for i in range(0, len(text), chunk)) + s.flush()
    assert _SET_COOKIE_TOKEN not in whole
    assert _SET_COOKIE_TOKEN not in out


@pytest.mark.parametrize("gap", [2000, 2060])
def test_a_long_cookie_pair_name_is_held_whole_across_the_look_behind(gap: int) -> None:
    # A pair name still running at the end of a window is held from its pair start
    # (``headers._held``), so the cut never falls inside it.
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor
    from kiro_crew.security import redact as production_redact

    text = f"Cookie: a=1; {'x' * gap}_session={_SET_COOKIE_TOKEN}; c=1\nafter\n"
    whole = production_redact(text)
    whole = whole if isinstance(whole, str) else whole[0]
    s = ProductionStreamRedactor()
    out = "".join(s.feed(ch) for ch in text) + s.flush()
    assert _SET_COOKIE_TOKEN not in whole
    assert _SET_COOKIE_TOKEN not in out


@pytest.mark.parametrize("feed", ["two_chunk", "chunk_1"])
@pytest.mark.parametrize("gap", [1009, 1024, 2000])
def test_a_closed_set_cookie_list_gives_way_to_the_open_cookie_list(gap: int, feed: str) -> None:
    # A Set-Cookie list closes at the ``;`` after its first pair: once that pair has
    # ended, the earlier Cookie list is the open one, and the look-behind keeps it.
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor
    from kiro_crew.security import redact as production_redact

    head = f"Cookie: a=1; Set-Cookie: b=2;{' ' * gap}session="
    text = f"{head}{_SET_COOKIE_TOKEN}; c=1\nafter\n"
    whole = production_redact(text)
    whole = whole if isinstance(whole, str) else whole[0]
    s = ProductionStreamRedactor()
    if feed == "two_chunk":
        parts = [text[: len(head) - 8], text[len(head) - 8 :]]
    else:
        parts = list(text)
    out = "".join(s.feed(p) for p in parts) + s.flush()
    assert _SET_COOKIE_TOKEN not in whole
    assert _SET_COOKIE_TOKEN not in out


def _feed_chunks(text: str, chunk: int) -> str:
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    s = ProductionStreamRedactor()
    return "".join(s.feed(text[i : i + chunk]) for i in range(0, len(text), chunk)) + s.flush()


def _whole(text: str) -> str:
    from kiro_crew.security import redact as production_redact

    whole = production_redact(text)
    return whole if isinstance(whole, str) else whole[0]


@pytest.mark.parametrize("chunk", [1, 7])
def test_a_raw_cookie_name_after_an_ignorable_character_keeps_its_look_behind(
    chunk: int,
) -> None:
    # ``X\u200bCookie``: the view joins it into one longer name, but the raw text reads
    # ``Cookie`` (the ignorable character is not a name character), as whole text
    # does, so the raw reading of the name counts too.
    text = f"X\u200bCookie: theme={'q' * 1500}; session={_SET_COOKIE_TOKEN}\nafter\n"
    assert _SET_COOKIE_TOKEN not in _whole(text)
    assert _SET_COOKIE_TOKEN not in _feed_chunks(text, chunk)


@pytest.mark.parametrize("chunk", [1, 7])
@pytest.mark.parametrize("pair", ["b=2", f"theme={'q' * 1500} "])
def test_an_open_cookie_list_wins_over_a_later_open_set_cookie(pair: str, chunk: int) -> None:
    # Both lists are open at the cut: the plain Cookie's runs to the end of the line,
    # so every later pair (``session`` after the Set-Cookie's first pair) is its own.
    text = f"Cookie: a=1; Set-Cookie: {pair}; session={_SET_COOKIE_TOKEN}\nafter\n"
    assert _SET_COOKIE_TOKEN not in _whole(text)
    assert _SET_COOKIE_TOKEN not in _feed_chunks(text, chunk)


@pytest.mark.parametrize("chunk", [1, 47, 256])
def test_a_cookie_name_waiting_on_its_separator_keeps_its_look_behind(chunk: int) -> None:
    # The CI GPT lane's case: ``Cookie`` then 1,500 blanks before its colon. The name
    # is held while only blanks follow it, up to its bound, then the line fails closed.
    text = f"Cookie{' ' * 1500}: theme={'q' * 300}; session={_SET_COOKIE_TOKEN}\nafter\n"
    assert _SET_COOKIE_TOKEN not in _whole(text)
    out = _feed_chunks(text, chunk)
    assert _SET_COOKIE_TOKEN not in out
    assert out.endswith("\nafter\n")


class _CountingText(str):
    """A ``str`` that counts the characters its ``find`` and ``rfind`` searches span."""

    examined = 0

    def _span(self, args: tuple[object, ...]) -> None:
        start = args[1] if len(args) > 1 and args[1] is not None else 0
        end = args[2] if len(args) > 2 and args[2] is not None else len(self)
        _CountingText.examined += max(0, int(end) - int(start))  # type: ignore[call-overload]

    def find(self, *args):  # type: ignore[no-untyped-def,override]
        self._span(args)
        return super().find(*args)

    def rfind(self, *args):  # type: ignore[no-untyped-def,override]
        self._span(args)
        return super().rfind(*args)


def test_the_look_behind_reads_a_line_of_set_cookie_names_in_linear_work() -> None:
    # Each Set-Cookie name on the line asks whether its first pair has closed: one
    # search per call answers them all, so the characters searched per input byte stay
    # flat as the line grows, counted through the text's own searches, never timed.
    from kiro_crew.security.redaction_structured.headers import cookie_look_behind

    def per_byte(count: int) -> float:
        text = _CountingText("Set-Cookie:a " * count)
        _CountingText.examined = 0
        cookie_look_behind(text, CONTEXT)
        return _CountingText.examined / len(text)

    rates = [per_byte(count) for count in (2000, 8000)]
    assert max(rates) <= min(rates) * 2.2, rates


@pytest.mark.parametrize("chunk", [1, 47, 1024])
@pytest.mark.parametrize(
    "text",
    [
        "Cookie: a=1; " + "lorem ipsum dolor " * 300 + "\nafter\n",
        '{"h":{"cookie":"a=1"},"body":"note; ' + "lorem ipsum dolor " * 300 + '"}\nafter\n',
    ],
    ids=["prose", "json"],
)
def test_an_equals_free_run_past_the_hold_bound_streams_as_whole_text(
    text: str, chunk: int
) -> None:
    # A Cookie pair name that runs past main's 4 KiB bound with no ``=`` is masked to
    # the end of its line by both paths (one deliberate fail-closed rule; main keeps
    # it), so the stream reads the line exactly as the whole text does.
    whole = _whole(text)
    assert TAG in whole and whole.endswith("\nafter\n")
    assert _feed_chunks(text, chunk) == whole


@pytest.mark.parametrize("chunk", [1, 7, 47])
@pytest.mark.parametrize("gap", [65, 1000])
def test_a_word_cookie_before_a_blank_run_streams_as_whole_text(gap: int, chunk: int) -> None:
    # The separator hold fails closed only past the bound the look-behind can carry
    # (CONTEXT less the kept prefix): a benign ``cookie`` followed by fewer blanks
    # streams unchanged.
    text = f"the cookie{' ' * gap}jar is tasty\nafter\n"
    whole = _whole(text)
    assert whole == text
    assert _feed_chunks(text, chunk) == whole


@pytest.mark.parametrize("chunk", [1, 7, 47, 256, 1024])
@pytest.mark.parametrize("run", [4095, 4096, 4097])
def test_a_long_unbroken_cookie_name_past_the_hold_bound_stays_masked(run: int, chunk: int) -> None:
    # One unbroken token run can still be a cookie name: past main's 4 KiB bound it is
    # not released, so the session value after it never streams.
    text = f"Cookie: a=1; {'x' * run}_session={_SET_COOKIE_TOKEN}; c=1\nafter\n"
    assert _SET_COOKIE_TOKEN not in _whole(text)
    out = _feed_chunks(text, chunk)
    assert _SET_COOKIE_TOKEN not in out
    assert out.endswith("\nafter\n")


@pytest.mark.parametrize("chunk", [1, 47, 1024])
@pytest.mark.parametrize("words", [100, 400])
def test_prose_before_a_session_pair_reads_as_one_pair_name(words: int, chunk: int) -> None:
    # Whole text reads every ``=``-free character before a ``session=`` as one pair
    # name, blanks included, and masks the value: the stream masks it too.
    text = f"Cookie: a=1; {'lorem ipsum ' * words}session={_SET_COOKIE_TOKEN}\nafter\n"
    whole = _whole(text)
    assert _SET_COOKIE_TOKEN not in whole
    out = _feed_chunks(text, chunk)
    assert _SET_COOKIE_TOKEN not in out
    assert out == whole


def test_an_unbroken_run_past_the_hold_bound_fails_closed() -> None:
    # The token-run hold keeps main's bound: past it the line is masked and dropped,
    # so a run that never ends cannot grow the stream's hold without limit.
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    s = ProductionStreamRedactor()
    out = [s.feed("Cookie: a=1; " + "x" * 1011)]
    held = []
    for _ in range(40):
        out.append(s.feed("x" * 1024))
        held.append(len(s._buf))  # noqa: SLF001
    out.append(s.feed("\nafter\n") + s.flush())
    text = "".join(out)
    assert max(held) <= 8192, held
    assert TAG in text and text.endswith("\nafter\n")
    assert "x" * 5000 not in text


@pytest.mark.parametrize("chunk", [1, 7, 47, 256])
@pytest.mark.parametrize("zw", [12, 13, 20, 200])
@pytest.mark.parametrize(
    "tail", [f"{' ' * 1500}session=", f" theme={'q' * 1500}; session="], ids=["gap", "theme"]
)
def test_a_set_cookie_spelled_with_ignorables_is_read_as_set_cookie(
    tail: str, zw: int, chunk: int
) -> None:
    # The local Opus lane's case: ``S`` + ZWSP * k + ``et-Cookie``. The view reading
    # that finds the name also decides it is a Set-Cookie, so the earlier open Cookie
    # list keeps the look-behind and the later session pair is read from it.
    text = f"Cookie: a=1; S{chr(0x200B) * zw}et-Cookie: b=2;{tail}{_SET_COOKIE_TOKEN}\nafter\n"
    whole = _whole(text)
    assert _SET_COOKIE_TOKEN not in whole
    out = _feed_chunks(text, chunk)
    assert _SET_COOKIE_TOKEN not in out
    assert out == whole


@pytest.mark.parametrize("chunk", [1, 7, 47, 256])
def test_an_ignorable_before_set_cookie_never_reads_as_a_plain_cookie(chunk: int) -> None:
    # The mirror case (a view-read plain Cookie whose raw spelling reads as Set-) cannot
    # be built: removing ignorable characters only joins characters, so the view can
    # lose the raw ``Set-`` step back only by gluing it into a longer name, which is no
    # Cookie name at all. ``x\u200bSet-Cookie`` is that case: raw reads Set-Cookie, the
    # view reads ``xSet-Cookie`` (no header), and the stream reads it as whole text does.
    text = f"Cookie: a=1; x{chr(0x200B)}Set-Cookie: b=2;{' ' * 1500}session={_SET_COOKIE_TOKEN}\nafter\n"
    whole = _whole(text)
    assert _SET_COOKIE_TOKEN not in whole
    assert _feed_chunks(text, chunk) == whole


def _held_after(make, feeds: list[str]) -> tuple[int, str]:
    s = make()
    out = []
    peak = 0
    for piece in feeds:
        out.append(s.feed(piece))
        held = getattr(s, "_pending", None)
        held = s._buf if held is None else held  # noqa: SLF001
        peak = max(peak, len(held))
    out.append(s.flush())
    return peak, "".join(out)


@pytest.mark.parametrize("entry", ["structured", "production"])
@pytest.mark.parametrize(
    "feeds",
    [
        ["Cookie: a=1; " + "x" * 5000 + "x=1"] + ["y" * 1024] * 200,
        ["Cookie: " + "n" * 4096 + "=v;"] + ["theme=light;" * 85] * 40,
    ],
    ids=["opus_name_then_more", "gpt_name_then_pairs"],
)
def test_a_pair_name_past_the_hold_bound_on_an_open_line_holds_a_bounded_buffer(
    entry: str, feeds: list[str]
) -> None:
    # The CI lanes' cases: a pair name whose ``=`` arrives past main's 4 KiB bound on a
    # line that has not ended. The stream drops the rest of the line (fail closed)
    # instead of holding it, so its buffer stays bounded and its output is the whole
    # text's.
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    make = StreamRedactor if entry == "structured" else ProductionStreamRedactor
    peak, out = _held_after(make, feeds)
    text = "".join(feeds)
    assert peak <= 8192, peak
    whole = redact(text)[0] if entry == "structured" else _whole(text)
    assert out == whole


def _separator_gap_text(gap: int) -> str:
    return f"x Cookie{' ' * gap}: session={_SET_COOKIE_TOKEN}; b=1\nafter\n"


def test_the_lane_separator_gap_shape_streams_as_whole_text() -> None:
    # The local GPT lane's case: 1,025 blanks at 47-character feeds let the name leave
    # the look-behind before the separator bound tripped.
    text = _separator_gap_text(1025)
    whole = _whole(text)
    assert _SET_COOKIE_TOKEN not in whole
    out = _feed_chunks(text, 47)
    assert _SET_COOKIE_TOKEN not in out
    assert TAG in out and out.endswith("\nafter\n")


@pytest.mark.parametrize("chunk", [1, 7, 47])
def test_no_separator_gap_lets_the_cookie_name_leave_the_look_behind(chunk: int) -> None:
    # The separator hold fails closed before the name can leave the look-behind, at
    # every gap around CONTEXT.
    leaks = [
        gap
        for gap in range(1000, 1101)
        if _SET_COOKIE_TOKEN in _feed_chunks(_separator_gap_text(gap), chunk)
    ]
    assert leaks == []


def test_a_separator_gap_under_the_bound_still_streams_as_whole_text() -> None:
    # Control: a gap the look-behind still carries is held and read as whole text reads
    # it, with no new over-mask.
    text = _separator_gap_text(1000)
    assert _feed_chunks(text, 47) == _whole(text)


@pytest.mark.parametrize("chunk", [1, 7, 47, 256])
def test_an_ignorable_separator_gap_is_bounded_in_raw_offsets(chunk: int) -> None:
    # The lanes' case: 700 x " \u200b" is 700 characters in the ignorable view but 1,400
    # raw, so a bound counted in the view let the name leave the raw look-behind.
    text = "Cookie" + " \u200b" * 700 + f": session={_SET_COOKIE_TOKEN}; b=1\nafter\n"
    assert _SET_COOKIE_TOKEN not in _whole(text)
    out = _feed_chunks(text, chunk)
    assert _SET_COOKIE_TOKEN not in out
    # Past the bound the line fails closed from the name's end; the name and the next line
    # survive.
    assert out.startswith("Cookie") and out.endswith("\nafter\n")


@pytest.mark.parametrize("chunk", [1, 7, 47])
def test_a_mixed_ignorable_gap_short_in_the_view_is_bounded_raw(chunk: int) -> None:
    # View length under the bound, raw length over it. A combining mark (U+0301) is not
    # default-ignorable: whole text does not read ``Cookie`` + marks + ``:`` as a header,
    # so it sits in the control below, not here.
    run = " \u200b\ufeff\u00ad"
    gap = run * 260
    assert (
        len(gap.replace("\u200b", "").replace("\ufeff", "").replace("\u00ad", "")) < 1008 < len(gap)
    )
    text = f"Cookie{gap}: session={_SET_COOKIE_TOKEN}; b=1\nafter\n"
    assert _SET_COOKIE_TOKEN not in _whole(text)
    out = _feed_chunks(text, chunk)
    assert _SET_COOKIE_TOKEN not in out
    assert out.startswith("Cookie") and out.endswith("\nafter\n")


@pytest.mark.parametrize("chunk", [1, 7, 47, 1024])
def test_a_long_ignorable_gap_with_no_separator_keeps_the_next_line(chunk: int) -> None:
    # GPT's review41 case: no separator ever comes; the hold stays bounded and fails
    # closed from the name's end, so the next line streams as written.
    text = "Cookie" + " \u200b" * 20000 + "\nafter\n"
    out = _feed_chunks(text, chunk)
    assert out == f"Cookie{TAG}\nafter\n"


@pytest.mark.parametrize("chunk", [1, 7, 47])
def test_ignorables_inside_an_ordinary_header_value_mask_nothing_extra(chunk: int) -> None:
    # Control: ignorables (and a combining mark) in a benign value: nothing extra masks.
    text = "X-Note: " + "ab\u200bc\u00ad\u0301 " * 300 + "\nafter\n"
    whole = _whole(text)
    assert TAG not in whole
    assert _feed_chunks(text, chunk) == whole


@pytest.mark.parametrize("chunk", [1, 7, 47])
def test_a_quote_after_an_ignorable_run_still_counts_in_the_raw_gap(chunk: int) -> None:
    # GPT's review42 case: the view reads the quote as the name's closing quote, so the
    # raw gap runs on past it.
    text = (
        "Cookie"
        + "\u200b" * 100
        + '"'
        + " \u200b" * 500
        + f": session={_SET_COOKIE_TOKEN}; b=1\nafter\n"
    )
    assert _SET_COOKIE_TOKEN not in _whole(text)
    out = _feed_chunks(text, chunk)
    assert _SET_COOKIE_TOKEN not in out
    assert out.startswith("Cookie") and out.endswith("\nafter\n")


def _view_gap(name: str, gap: str) -> str:
    return name + gap + f": session={_SET_COOKIE_TOKEN}; b=1\nafter\n"


@pytest.mark.parametrize(
    ("name", "chunk"),
    [("Cook\u200bie", c) for c in (1, 2, 4, 8)]
    + [("Set-Coo\u200bkie", c) for c in (1, 2, 3, 4, 6, 12)],
)
def test_a_view_only_cookie_name_counts_its_separator_gap_raw(name: str, chunk: int) -> None:
    # The review43 lanes' case: only the ignorable view reads the name, so its gap must
    # be counted in the raw offsets the look-behind keeps (600 in the view, 1,200 raw).
    text = _view_gap(name, " \u200b" * 600)
    whole = _whole(text)
    assert _SET_COOKIE_TOKEN not in whole
    out = _feed_chunks(text, chunk)
    assert _SET_COOKIE_TOKEN not in out
    assert out.startswith(name[:4]) and out.endswith("\nafter\n")


@pytest.mark.parametrize("chunk", [1, 2, 4, 8])
def test_a_view_only_name_with_a_mixed_gap_counts_it_raw(chunk: int) -> None:
    gap = " \u200b\ufeff\u00ad" * 260  # 260 in the view, 1,040 raw
    text = _view_gap("Cook\u200bie", gap)
    assert _SET_COOKIE_TOKEN not in _whole(text)
    assert _SET_COOKIE_TOKEN not in _feed_chunks(text, chunk)


@pytest.mark.parametrize("chunk", [1, 2, 4, 8])
def test_a_view_only_name_under_the_raw_bound_streams_as_whole_text(chunk: int) -> None:
    # Control: 300 pairs (600 raw) stay under the bound and stream as whole text.
    text = _view_gap("Cook\u200bie", " \u200b" * 300)
    assert _feed_chunks(text, chunk) == _whole(text)
