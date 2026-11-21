"""Stream/whole parity and linear-time tests for the structured credential detector."""

from __future__ import annotations

import random
from collections.abc import Callable

import pytest
from redaction_structured_cases import (
    B64,
    PEM_BEGIN,
    PEM_BODY,
    PGP_BEGIN,
    assert_linear_work,
    aws_key_id,
    aws_secret,
    benign_cases,
    github_classic,
    jwt,
    secret_cases,
    session_token,
    synth,
)

from kiro_crew.security.redaction_structured import TAG, StreamRedactor, redact
from kiro_crew.security.redaction_structured.stream import CONTEXT

ALL_TEXTS = [(n, t) for n, t, _, _ in secret_cases()] + benign_cases()
ALL_TEXTS += [
    ("mixed", "\n".join(t for _, t in ALL_TEXTS[:12])),
    ("pair_across", f"{aws_secret()} " + "y" * 250 + f" {aws_key_id()} tail text"),
]


def run_stream(chunks: list[str]) -> tuple[str, list[tuple[int, int, str, str]]]:
    s = StreamRedactor()
    out = "".join(s.feed(c) for c in chunks) + s.flush()
    return out, s.spans


def split_at(text: str, cuts: list[int]) -> list[str]:
    bounds = [0, *sorted(cuts), len(text)]
    return [text[a:b] for a, b in zip(bounds, bounds[1:], strict=False)]


@pytest.mark.parametrize("text", [pytest.param(t, id=n) for n, t in ALL_TEXTS])
def test_chunked_equals_whole_at_every_split_point(text: str) -> None:
    whole = redact(text)
    for cut in range(len(text) + 1):
        assert run_stream([text[:cut], text[cut:]]) == whole, f"split at {cut}"


@pytest.mark.parametrize("text", [pytest.param(t, id=n) for n, t in ALL_TEXTS])
def test_chunked_equals_whole_at_random_multi_splits(text: str) -> None:
    whole = redact(text)
    rng = random.Random(len(text))
    for _ in range(25):
        cuts = [rng.randrange(len(text) + 1) for _ in range(rng.randrange(2, 12))]
        assert run_stream(split_at(text, cuts)) == whole, cuts


@pytest.mark.parametrize("text", [pytest.param(t, id=n) for n, t in ALL_TEXTS[:20]])
def test_one_character_feeds(text: str) -> None:
    assert run_stream(list(text)) == redact(text)


def _long_cases() -> list[tuple[str, str]]:
    sess = session_token(6000, seed=7)
    return [
        ("pem_past_max_block", f"a {PEM_BEGIN}\n" + "B" * 17000 + "\nafter " * 20),
        ("pgp_unterminated_long", f"{PGP_BEGIN}\n" + PEM_BODY * 300),
        ("session_run_fail_closed", f"AWS_SESSION_TOKEN={sess}=\nnext {github_classic()}"),
        ("session_run_padded", f'{{"SessionToken": "{sess}=="}} ok'),
        ("long_jose_run", "eyJ" + "A" * 20000 + " " + jwt()),
        ("long_benign", ("plain words, a://b and arn:aws:s3:::bkt " * 600)),
        ("context_reach", "x" * (CONTEXT * 3) + f" {aws_key_id()} {aws_secret()}"),
    ]


@pytest.mark.parametrize("text", [pytest.param(t, id=n) for n, t in _long_cases()])
def test_long_units_match_whole_text(text: str) -> None:
    whole = redact(text)
    step = max(1, len(text) // 300)
    for cut in range(0, len(text) + 1, step):
        assert run_stream([text[:cut], text[cut:]]) == whole, f"split at {cut}"
    rng = random.Random(7)
    for _ in range(10):
        cuts = [rng.randrange(len(text) + 1) for _ in range(rng.randrange(5, 40))]
        assert run_stream(split_at(text, cuts)) == whole
    assert run_stream([text[i : i + 997] for i in range(0, len(text), 997)]) == whole


def test_session_run_tail_is_discarded_not_resumed() -> None:
    sess = session_token(9000, seed=3)
    text = f"AWS_SESSION_TOKEN={sess}\nafter"
    s = StreamRedactor()
    pieces = [s.feed(text[i : i + 512]) for i in range(0, len(text), 512)]
    out = "".join(pieces) + s.flush()
    assert out == f"AWS_SESSION_TOKEN={TAG}\nafter"
    assert sess[-50:] not in out


def test_pending_text_stays_bounded() -> None:
    s = StreamRedactor()
    longest = 0
    rng = random.Random(1)
    corpus = [t for _, t in ALL_TEXTS] + ["word " * 50, synth(400, B64, 3), "eyJ" + "Q" * 3000]
    for _ in range(400):
        s.feed(rng.choice(corpus) + "\n")
        longest = max(longest, len(s._pending))  # noqa: SLF001
    s.flush()
    # The largest hold is a private-key block (16,000 plus its END line) or a JOSE run.
    assert longest < 20_000


@pytest.mark.parametrize("unit", ["x", "X", "*"])
def test_a_mask_run_fed_in_pieces_is_not_held_past_its_bound(unit: str) -> None:
    s = StreamRedactor()
    longest = 0
    for _ in range(64):
        s.feed(unit * 4096)
        longest = max(longest, len(s._pending))  # noqa: SLF001
    s.flush()
    assert longest < 20_000


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


def _benign_cookie_line(length: int) -> str:
    """``Cookie: a=1; a=1; ...`` cut to *length* characters, with no line end."""
    return ("Cookie: " + "a=1; " * (length // 5 + 1))[:length]


def test_a_running_cookie_line_is_held_to_its_bound_then_masked() -> None:
    import tracemalloc

    from kiro_crew.security.redaction_structured.headers import COOKIE_LINE_HOLD

    line = _benign_cookie_line(10_008)
    s = StreamRedactor()
    tracemalloc.start()
    out, longest = "", 0
    for i in range(0, len(line), 64):
        out += s.feed(line[i : i + 64])
        longest = max(longest, len(s._pending))  # noqa: SLF001
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    out += s.feed("\nafter") + s.flush()
    # Held right up to the bound (feeds are 64 characters), never past it.
    assert COOKIE_LINE_HOLD - 64 <= longest < COOKIE_LINE_HOLD
    assert peak < 1_000_000
    assert out == f"Cookie: {TAG}\nafter"


@pytest.mark.parametrize("entry", ["structured", "production"])
def test_a_cookie_line_at_its_bound_is_masked_and_one_short_is_not(entry: str) -> None:
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor
    from kiro_crew.security.redaction_structured.headers import COOKIE_LINE_HOLD

    def run(line: str) -> str:
        s = StreamRedactor() if entry == "structured" else ProductionStreamRedactor()
        return "".join(s.feed(c) for c in (line, "\nafter")) + s.flush()

    short = _benign_cookie_line(COOKIE_LINE_HOLD - 1)
    assert run(short) == short + "\nafter"
    assert run(_benign_cookie_line(COOKIE_LINE_HOLD)) == f"Cookie: {TAG}\nafter"
    assert run(_benign_cookie_line(COOKIE_LINE_HOLD + 1)) == f"Cookie: {TAG}\nafter"


def _token_overflow_line(name: str, session: str) -> str:
    return (
        f"{name}: theme={'a' * 750}; url=https://h.test/p?token={'b' * 3500}"
        f"; session={session}\nafter"
    )


def _feed_in(redactor: object, text: str, chunk: int) -> str:
    feed = redactor.feed  # type: ignore[attr-defined]
    out = "".join(feed(text[i : i + chunk]) for i in range(0, len(text), chunk))
    return out + redactor.flush()  # type: ignore[attr-defined]


@pytest.mark.parametrize("chunk", [1, 7, 128, 1024])
def test_a_token_overflow_inside_an_open_cookie_line_masks_through_its_end(chunk: int) -> None:
    # The 4096-character token ceiling fires while the Cookie line is held from its
    # name; dropping only the token run would release the session pair after it.
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    value = synth(48, B64, 13)
    text = _token_overflow_line("Cookie", value)
    out = _feed_in(ProductionStreamRedactor(), text, chunk)
    assert value not in out
    assert out.endswith(f"{TAG}\nafter")
    assert value not in redact(text)[0]
    assert value not in run_stream([text])[0]


@pytest.mark.parametrize("chunk", [1, 7, 128, 1024])
def test_a_cookie_header_after_an_overflowing_token_keeps_its_line(chunk: int) -> None:
    # Only a Cookie name before the dropped credential widens the drop; a Cookie
    # header after it is read on its own and keeps its benign text.
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    value = synth(48, B64, 19)
    text = f"X: token={'b' * 4300}; Cookie: theme={'a' * 2000}; session={value}\nafter"
    out = _feed_in(ProductionStreamRedactor(), text, chunk)
    assert value not in out
    assert out.endswith(f"theme={'a' * 2000}; session={TAG}\nafter")


@pytest.mark.parametrize("chunk", [1, 7, 128, 1024])
@pytest.mark.parametrize(
    "head, close",
    [
        pytest.param('{"Cookie": "', '"}', id="json-quoted"),
        pytest.param("Cookie\u00a0:\u2003", "", id="unicode-spaced"),
        pytest.param("Cookie: ", "", id="plain"),
        pytest.param("Cook\u0131e: ", "", id="dotless-i"),
    ],
)
def test_a_token_overflow_in_any_cookie_spelling_masks_through_its_end(
    head: str, close: str, chunk: int
) -> None:
    # The open-line check reads the Cookie name with the cookie reader's grammar,
    # so every spelling that reader holds is also dropped through its line end.
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    value = synth(48, B64, 23)
    body = f"theme={'a' * 750}; url=https://h.test/p?token={'b' * 3500}; session={value}"
    text = f"{head}{body}{close}\nafter"
    out = _feed_in(ProductionStreamRedactor(), text, chunk)
    assert value not in out
    assert out.endswith("\nafter")
    assert value not in redact(text)[0]
    assert value not in _feed_in(StreamRedactor(), text, chunk)


def test_a_token_overflow_outside_a_cookie_line_drops_only_the_run() -> None:
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    text = _token_overflow_line("X-Note", "plainword")
    out = _feed_in(ProductionStreamRedactor(), text, 128)
    head = f"X-Note: theme={'a' * 750}; url=https://h.test/p?token="
    assert out == f"{head}{TAG}; session=plainword\nafter"


def test_a_set_cookie_response_streams_without_spinning() -> None:
    import threading

    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor
    from kiro_crew.testing.wait import default_timeout

    text = (
        "HTTP/1.1 302 Found\nLocation: /dashboard\n"
        f"Set-Cookie: session={synth(40, B64, 17)}; Path=/; HttpOnly\n\nbody"
    )
    result: list[str] = []
    worker = threading.Thread(
        target=lambda: result.append(_feed_in(ProductionStreamRedactor(), text, 1)), daemon=True
    )
    worker.start()
    worker.join(default_timeout())
    assert result, "the stream did not finish a one-character feed of a Set-Cookie response"
    assert result[0].count(TAG) == 1


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


@pytest.mark.parametrize("entry", ["structured", "production"])
@pytest.mark.parametrize("chunk", [1, 4096])
def test_a_cookie_separator_past_its_bound_masks_the_line(entry: str, chunk: int) -> None:
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    value = synth(48, B64, 43)
    text = f"Cookie{' ' * 65}: theme={'a' * 1500}; session={value}\nafter"
    s = StreamRedactor() if entry == "structured" else ProductionStreamRedactor()
    out = _feed_in(s, text, chunk)
    assert value not in out
    assert out.endswith("\nafter")
    assert value not in redact(text)[0]


def _cookie_line_with_open_key(lead: str) -> tuple[str, str]:
    from redaction_structured_cases import PEM_BEGIN

    end = PEM_BEGIN.replace("BEGIN", "END")
    body = synth(96, B64, 47)
    text = f"Cookie: theme={lead}{PEM_BEGIN}{'A' * 8200}\n{body}\n{end}\nafter"
    return text, body


@pytest.mark.parametrize("chunk", [1024, 4096])
@pytest.mark.parametrize("entry", ["structured", "production"])
@pytest.mark.parametrize("lead", ["", "a" * 3000], ids=["key-at-value", "key-inside-run"])
def test_a_cookie_line_drop_waits_for_a_private_key_block_opened_on_it(
    lead: str, entry: str, chunk: int
) -> None:
    # The 8 KiB Cookie cap drops to the line end; a private-key block opened on that
    # line is still undecided there, so the drop waits for it (bounded by the block's
    # own limit) instead of losing its BEGIN line and streaming the key body.
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    text, body = _cookie_line_with_open_key(lead)
    s = StreamRedactor() if entry == "structured" else ProductionStreamRedactor()
    out = _feed_in(s, text, chunk)
    assert body[:24] not in out
    assert out == redact(text)[0]


@pytest.mark.parametrize("entry", ["structured", "production"])
def test_a_long_cookie_line_with_no_open_unit_drops_as_before(entry: str) -> None:
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    line = _benign_cookie_line(10_008)
    s = StreamRedactor() if entry == "structured" else ProductionStreamRedactor()
    out = _feed_in(s, line + "\nafter", 1024)
    assert out == f"Cookie: {TAG}\nafter"


def _hex_ids_cookie_line(size: int) -> str:
    """``Cookie: ids=`` and comma-joined 40-hex ids to *size*, no LF:
    every id is a candidate the key pairing reads."""
    import hashlib

    ids = [hashlib.sha256(str(i).encode()).hexdigest()[:40] for i in range(size // 41 + 1)]
    return ("Cookie: ids=" + ",".join(ids))[:size]


def _redactor(entry: str) -> tuple[object, str]:
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    if entry == "structured":
        return StreamRedactor(), "_pending"
    return ProductionStreamRedactor(), "_buf"


def _key_rows(count: int, seed: int) -> list[str]:
    return [synth(64, B64, seed + k) for k in range(count)]


@pytest.mark.parametrize("entry", ["structured", "production"])
def test_an_unterminated_key_opened_during_the_drop_masks_to_the_end(entry: str) -> None:
    # GPT's trigger: the block's BEGIN leaves the look-behind and no END arrives
    # inside its bound, so the drop switches to the end of the text, as whole-text
    # armour reads an unterminated block, instead of ending at a deadline.
    from redaction_structured_cases import PEM_BEGIN

    lines = _key_rows(280, 300)
    text = f"Cookie: theme={'a' * 8200}{PEM_BEGIN}\n" + "\n".join(lines)
    s, _ = _redactor(entry)
    out = _feed_in(s, text, 1024)
    whole = redact(text)[0]
    assert not any(line[:24] in whole for line in lines)
    assert not any(line[:24] in out for line in lines)
    assert out == f"Cookie: {TAG}"


@pytest.mark.parametrize("entry", ["structured", "production"])
def test_an_end_line_past_sixteen_thousand_from_the_begin_is_honoured(entry: str) -> None:
    # Opus's second case: the END starts past BEGIN + 16,000 but inside the block's
    # own bound (header end + max_block + END line), so it closes the block.
    from redaction_structured_cases import PEM_BEGIN

    end = PEM_BEGIN.replace("BEGIN", "END")
    head = f"{PEM_BEGIN}\n"
    rows, size = [], len(head)
    k = 0
    while size < 16_010:
        rows.append(synth(64, B64, 600 + k))
        size += 65
        k += 1
    body = "\n".join(rows)
    text = f"Cookie: theme={'a' * 8200}{head}{body}\n{end}\nAFTERLINE visible\n"
    begin = text.index(PEM_BEGIN)
    assert text.index(end, begin) - begin > 16_000
    s, _ = _redactor(entry)
    out = _feed_in(s, text, 1024)
    # Nothing of the body is released: the drop runs to and through the END line.
    assert out == f"Cookie: {TAG}{end}\nAFTERLINE visible\n"
    whole = redact(text)[0]
    assert whole.endswith(f"{end}\nAFTERLINE visible\n")


def test_completed_keys_on_one_dropped_line_stream_in_linear_work() -> None:
    # Each completed block folds into one furthest end; only open blocks are kept,
    # so blocks on one dropped line cost the same per byte however many there are.
    from redaction_structured_cases import PEM_BEGIN, package_lines

    end = PEM_BEGIN.replace("BEGIN", "END")
    block = f"{PEM_BEGIN}{synth(40, B64, 900)}{end} "

    def per_byte(entry: str, count: int) -> float:
        text = f"Cookie: theme={'a' * 8200}" + block * count + "\nafter"

        def run() -> None:
            s, _ = _redactor(entry)
            _feed_in(s, text, 1024)

        return package_lines(run) / len(text)

    def most_kept(entry: str, count: int) -> int:
        # The records the drop keeps, at their largest while the line runs. The line
        # counter cannot see a C-level max() over a growing dict, so this is the pin
        # on what the drop retains.
        text = f"Cookie: theme={'a' * 8200}" + block * count
        s, _ = _redactor(entry)
        most = 0
        for i in range(0, len(text), 1024):
            s.feed(text[i : i + 1024])  # type: ignore[attr-defined]
            drop = getattr(s, "_line_drop" if entry == "structured" else "_engine_line")
            if drop is not None:
                kept = (getattr(drop, n) for n in type(drop).__slots__)
                most = max(most, sum(len(v) for v in kept if isinstance(v, dict)))
        return most

    for entry in ("structured", "production"):
        rates = [per_byte(entry, count) for count in (250, 500, 1000)]
        assert all(b <= a * 2.2 for a, b in zip(rates, rates[1:])), (entry, rates)
        assert most_kept(entry, 1000) <= 1


@pytest.mark.parametrize("entry", ["structured", "production"])
def test_a_begin_split_across_feeds_that_proves_false_holds_nothing(entry: str) -> None:
    # A chunk ends inside what could be a BEGIN line; the next feed shows it is not
    # one, so it is dropped as a candidate and the following line streams.
    s, _ = _redactor(entry)
    line = f"Cookie: theme={'a' * 8200}"
    out = "".join(s.feed(line[i : i + 1024]) for i in range(0, len(line), 1024))  # type: ignore[attr-defined]
    out += s.feed("-----BEGIN RSA PRIV")  # type: ignore[attr-defined]
    out += s.feed("ATE KEX nonsense\nAFTERLINE visible\n")  # type: ignore[attr-defined]
    out += s.flush()  # type: ignore[attr-defined]
    assert out == f"Cookie: {TAG}\nAFTERLINE visible\n"


_GLUED_HEAD = "-----BEGIN RSA " "PRIVATE KEY-----" "END RSA " "PRIVATE KEY-----"
_GLUED_ROWS = [f"SECRETROW{k:03d}" + synth(52, B64, 700 + k) for k in range(40)]
_GLUED_CASES = {
    "unterminated_LF": "\n".join([_GLUED_HEAD, *_GLUED_ROWS, "TAIL visible\n"]),
    "unterminated_CR": "\r".join([_GLUED_HEAD, *_GLUED_ROWS, "TAIL visible\r"]),
    "terminated_LF": "\n".join(
        [_GLUED_HEAD, *_GLUED_ROWS, "-----END RSA " "PRIVATE KEY-----", "TAIL visible\n"]
    ),
}


@pytest.mark.parametrize("chunk", [1, 7, 48, 1024, 4097])
@pytest.mark.parametrize("case", sorted(_GLUED_CASES))
@pytest.mark.parametrize("entry", ["structured", "production"])
def test_an_end_line_glued_to_the_begin_header_does_not_close_the_drop(
    entry: str, case: str, chunk: int
) -> None:
    # The header's closing dashes also open an END line; the drop searches the END
    # from the header end, where whole-text armour does, so that text is not one.
    text = f"Cookie: theme={'a' * 8200}" + _GLUED_CASES[case]
    s, _ = _redactor(entry)
    out = _feed_in(s, text, chunk)
    whole = redact(text)[0]
    assert not any(row[:12] in whole for row in _GLUED_ROWS)
    # From the key on, the stream equals the whole text up to the line break at the
    # drop's edge; before it, the stream masks the over-8-KiB Cookie run the whole
    # text keeps (the documented exception).
    assert out.startswith(f"Cookie: {TAG}")
    rest = whole[whole.rindex(TAG) + len(TAG) :]
    assert out[len(f"Cookie: {TAG}") :].strip("\r\n") == rest.strip("\r\n")


def _main_side_tail_text() -> tuple[str, str]:
    """A fuzz text whose tail main's own streams release: a glued-END PGP header, rows
    joined by a literal backslash-n, then glued PGP, EC and fake-END headers."""
    kind = "PRIVATE KEY BLOCK"
    pgp = f"-----BEGIN PGP {kind}-----END PGP {kind}-----"
    rows = "\\n".join(f"M{k:05d}Q" + synth(56, B64, 800 + k) for k in range(40))
    tail = "M32702Z"
    text = (
        f"Cookie: theme={'a' * 9100}{pgp}\\n{rows}\\n{pgp}-----BEGIN EC "
        "PRIVATE KEY-----"
        f"-----END {'X' * 58} "
        "PRIVATE KEY-----"
        "-----BEGIN "
        "PRIVATE KEY-----"
        f"\nTAIL {tail} visible\n"
    )
    return text, tail


# (chunk, first-feed offset) -> whether this head releases the tail. origin/main
# releases it at all four rows; this head releases it only where main does.
_MAIN_SIDE_TAIL_ROWS = [
    ((1024, 0), False),
    ((48, 8200), False),
    ((1024, 8156), True),
    ((4097, 8171), True),
]


@pytest.mark.parametrize(("feed", "released"), _MAIN_SIDE_TAIL_ROWS)
@pytest.mark.parametrize("entry", ["structured", "production"])
def test_a_tail_main_releases_after_glued_headers_is_not_covered_yet(
    entry: str, feed: tuple[int, int], released: bool
) -> None:
    # Not covered (main's own): after these headers main's streams release the tail
    # whole text masks, at every row here. This head never releases more than main;
    # where its line drop holds the headers it masks the tail. The row flips once
    # main's gap is fixed.
    chunk, offset = feed
    text, tail = _main_side_tail_text()
    assert tail not in redact(text)[0]
    s, _ = _redactor(entry)
    out = s.feed(text[:offset]) if offset else ""  # type: ignore[attr-defined]
    out += _feed_in(s, text[offset:], chunk)
    assert (tail in out) == released


def _ceiling_key_text(nl: str, body_len: int) -> tuple[str, str, str]:
    """GPT's trigger: a Cookie line past the token ceiling with a key block on it;
    the ceiling fires at the Bearer token after the key body."""
    from redaction_structured_cases import PEM_BEGIN

    end = PEM_BEGIN.replace("BEGIN", "END")
    body = synth(body_len, B64, 41)
    row = "ROWSECRET" + synth(55, B64, 43)
    text = (
        "Cookie: "
        + "a=1; " * 800
        + PEM_BEGIN
        + body
        + " Authorization: Bearer "
        + synth(48, B64, 45)
        + nl
        + row
        + nl
        + end
        + nl
        + "after visible"
        + nl
    )
    return text, body, row


# No chunk 1: CI's per-test cap is 120 s, and at one character per feed the line
# drop rescans the 1 KiB look-behind each time, about 1 KiB of work per character
# (47.66 s locally on the production lane, 24.27 s on the structured lane). The
# mutant restoring the late construction reddens at chunks 7 and 48, so chunk 1
# adds no proof power.
@pytest.mark.parametrize("chunk", [7, 48, 1024, 4097])
@pytest.mark.parametrize("nl", ["\n", "\r"], ids=["LF", "CR"])
@pytest.mark.parametrize("body_len", [900, 1100, 1500], ids=["inside", "outside", "lane"])
@pytest.mark.parametrize("entry", ["structured", "production"])
def test_a_key_on_a_cookie_line_past_the_token_ceiling_is_carried_to_its_end(
    entry: str, body_len: int, nl: str, chunk: int
) -> None:
    # The token ceiling arms the line drop over a window larger than the look-behind;
    # the drop reads that window before it is dropped, so a BEGIN anywhere on the
    # line (900 / 1100 characters before the cut sit inside / outside the 1 KiB
    # look-behind) is carried to its END and the row after the line stays masked.
    text, body, row = _ceiling_key_text(nl, body_len)
    whole = redact(text)[0]
    assert row[:12] not in whole and body[:20] not in whole
    s, _ = _redactor(entry)
    out = _feed_in(s, text, chunk)
    assert row[:12] not in out and body[:20] not in out
    assert out.endswith(f"{nl}after visible{nl}")


@pytest.mark.parametrize("chunk", [7, 512, 4097])
@pytest.mark.parametrize("entry", ["structured", "production"])
def test_a_bearer_value_after_a_long_blank_run_on_a_cookie_line_is_not_covered_yet(
    entry: str, chunk: int
) -> None:
    # Not covered (main's own): after more blanks than the 1 KiB look-behind,
    # a Bearer value on the next line is released by main's streams with or without
    # a Cookie line before it, at every chunk here. This PR never releases more than
    # main; the row flips once main reads that value.
    token = synth(40, B64, 77)
    text = f"Cookie: theme={'a' * 8200} Authorization: Bearer{' ' * 1100}\n{token}\nafter\n"
    assert token not in redact(text)[0]
    s, _ = _redactor(entry)
    assert token in _feed_in(s, text, chunk)


@pytest.mark.parametrize("entry", ["structured", "production"])
def test_a_long_cookie_line_of_pairing_candidates_drops_at_its_bound(entry: str) -> None:
    # A pairing open never holds the drop: only a private-key block can decide text
    # past the line end. Holding on any open kept this line whole and rescanned it.
    text = _hex_ids_cookie_line(20_000) + "\nafter"
    s, pending = _redactor(entry)
    out, longest = "", 0
    for i in range(0, len(text), 1024):
        out += s.feed(text[i : i + 1024])  # type: ignore[attr-defined]
        longest = max(longest, len(getattr(s, pending)))
    out += s.flush()  # type: ignore[attr-defined]
    assert longest < 8 << 10, longest
    # The whole text leaves this line unchanged; the stream masks its rest (the
    # documented over-8-KiB exception), so it masks more, never less.
    assert out == f"Cookie: {TAG}\nafter"


@pytest.mark.parametrize("entry", ["structured", "production"])
def test_a_chunk_ending_inside_a_short_run_on_a_long_cookie_line_still_drops(
    entry: str,
) -> None:
    line = _benign_cookie_line(9_000) + "; v=ab12cd34ef"
    cut = len(line) - 6
    s, _ = _redactor(entry)
    out = s.feed(line[:cut]) + s.feed(line[cut:] + "\nafter") + s.flush()  # type: ignore[attr-defined]
    assert out == f"Cookie: {TAG}\nafter"


def test_a_long_cookie_line_of_pairing_candidates_streams_in_linear_work() -> None:
    from redaction_structured_cases import package_lines

    def per_byte(entry: str, size: int) -> float:
        text = _hex_ids_cookie_line(size) + "\nafter"

        def run() -> None:
            s, _ = _redactor(entry)
            for i in range(0, len(text), 1024):
                s.feed(text[i : i + 1024])  # type: ignore[attr-defined]
            s.flush()  # type: ignore[attr-defined]

        return package_lines(run) / size

    # Work per byte stays flat: within 2.2x across 16, 32 and 64 KiB, counted in
    # executed lines. Holding on every open rescanned the line, so the rate grew
    # about 1.85x per doubling (3.4x across the range).
    for entry in ("structured", "production"):
        rates = [per_byte(entry, size) for size in (16 << 10, 32 << 10, 64 << 10)]
        assert max(rates) <= min(rates) * 2.2, (entry, rates)


@pytest.mark.parametrize("entry", ["structured", "production"])
def test_a_pairing_split_by_the_line_drop_masks_no_less_than_the_whole_text(
    entry: str,
) -> None:
    # The key id is dropped with the line; its secret on the next line is scanned
    # as usual, so the stream masks at least what the whole text masks.
    from redaction_structured_cases import aws_key_id, aws_secret

    key_id, secret = aws_key_id(), aws_secret()
    text = f"Cookie: theme={'a' * 8200} {key_id}\n{secret}\nafter"
    s, _ = _redactor(entry)
    out = _feed_in(s, text, 1024)
    assert secret not in redact(text)[0]
    assert out == f"Cookie: {TAG}\n{TAG}\nafter"


@pytest.mark.parametrize("entry", ["structured", "production"])
def test_a_scheme_value_after_a_long_blank_run_is_not_covered_yet(entry: str) -> None:
    # Not covered by this stream: a scheme value on the next line after more blanks
    # than the stream's look-behind keeps loses its scheme, with or without a Cookie
    # line before it. This row records today's verdict so a change that widens the gap
    # shows; it flips once that gap is fixed.
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    token = synth(32, B64, 53)
    text = f" Authorization: Bearer{' ' * 1100}\n{token}\nafter"
    s = StreamRedactor() if entry == "structured" else ProductionStreamRedactor()
    assert token not in redact(text)[0]
    # The production stream's own Bearer anchor still masks it; the structured
    # stream alone releases it.
    assert (token in _feed_in(s, text, 512)) == (entry == "structured")


def _key_after_cookie_run(split: bool = False) -> tuple[list[str], str, str]:
    """GPT's trigger: a private-key block that opens after the 8 KiB drop began."""
    from redaction_structured_cases import PEM_BEGIN

    end = PEM_BEGIN.replace("BEGIN", "END")
    body = synth(1100, B64, 59)
    more = synth(64, B64, 61)
    text = f"Cookie: theme={'a' * 8200}{PEM_BEGIN}{body}\n{more}\n{end}\nafter"
    if not split:
        return [text[i : i + 1024] for i in range(0, len(text), 1024)], body, more
    # Two chunks, cut inside the BEGIN marker.
    cut = text.index(PEM_BEGIN) + 7
    return [text[:cut], text[cut:]], body, more


@pytest.mark.parametrize("split", [False, True], ids=["chunks", "split-marker"])
@pytest.mark.parametrize("entry", ["structured", "production"])
def test_a_key_opened_during_a_cookie_line_drop_is_carried_through(entry: str, split: bool) -> None:
    # A private-key block that opens after the line drop began is read during the
    # drop (never emitted), and the drop runs through the block's own end instead of
    # stopping at the line end with the BEGIN line already gone.
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    chunks, body, more = _key_after_cookie_run(split)
    text = "".join(chunks)
    s = StreamRedactor() if entry == "structured" else ProductionStreamRedactor()
    out = "".join(s.feed(c) for c in chunks) + s.flush()
    assert body[:24] not in out and more[:24] not in out
    # The line is over the 8 KiB cap, so the stream masks the benign run the whole
    # text keeps (the documented fail-closed exception); from the END line on, both
    # agree.
    end = out[out.rindex("-----END") :]
    whole = redact(text)[0]
    assert end == whole[whole.rindex("-----END") :]
    assert body[:24] not in whole


@pytest.mark.parametrize("entry", ["structured", "production"])
def test_a_pair_after_the_key_on_the_cookie_line_stays_masked(entry: str) -> None:
    # The drop is carried to the LATER of the line end and the key's end, so a key
    # pair on the same line after the key's END is still dropped with the line.
    from redaction_structured_cases import PEM_BEGIN, aws_key_id, aws_secret

    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    end = PEM_BEGIN.replace("BEGIN", "END")
    body = synth(200, B64, 67)
    secret = aws_secret()
    session = synth(48, B64, 69)
    text = (
        f"Cookie: theme={'a' * 8200}{PEM_BEGIN}{body}{end} {aws_key_id()} {secret}"
        f"; session={session}\nafter"
    )
    s = StreamRedactor() if entry == "structured" else ProductionStreamRedactor()
    out = _feed_in(s, text, 1024)
    # The session pair after the key has no Cookie name left in the look-behind, so
    # only the line drop masks it.
    assert secret not in out and body[:24] not in out and session not in out
    assert out.endswith("\nafter")


@pytest.mark.parametrize("entry", ["structured", "production"])
def test_a_key_ending_after_the_cookie_line_is_dropped_to_its_end_only(entry: str) -> None:
    # The drop runs through the key's END and no further: the line after it streams.
    from redaction_structured_cases import PEM_BEGIN

    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    end = PEM_BEGIN.replace("BEGIN", "END")
    body = [synth(64, B64, 71 + k) for k in range(2)]
    text = f"Cookie: theme={'a' * 8200}{PEM_BEGIN}\n{body[0]}\n{body[1]}\n{end}\nkept line\n"
    s = StreamRedactor() if entry == "structured" else ProductionStreamRedactor()
    out = _feed_in(s, text, 1024)
    assert body[0][:24] not in out and body[1][:24] not in out
    assert out.endswith(f"{end}\nkept line\n")


@pytest.mark.parametrize("chunk", [7, 1024])
@pytest.mark.parametrize("entry", ["structured", "production"])
def test_a_long_key_opened_during_the_drop_ends_it_at_its_end_line(entry: str, chunk: int) -> None:
    # A real-size key body (3,000 characters) moves its BEGIN out of the look-behind
    # before its END arrives; the drop still stops at that END line, read from the
    # header, and the line after it streams.
    from redaction_structured_cases import PEM_BEGIN

    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    end = PEM_BEGIN.replace("BEGIN", "END")
    lines = [synth(64, B64, 90 + k) for k in range(47)]
    text = f"Cookie: theme={'a' * 8200}{PEM_BEGIN}\n" + "\n".join(lines)
    text += f"\n{end}\nAFTERLINE visible\n"
    s = StreamRedactor() if entry == "structured" else ProductionStreamRedactor()
    out = _feed_in(s, text, chunk)
    assert not any(line[:24] in out for line in lines)
    assert out.endswith(f"{end}\nAFTERLINE visible\n")


@pytest.mark.parametrize("chunk", [7, 48, 1024])
@pytest.mark.parametrize("entry", ["structured", "production"])
def test_an_end_line_before_the_begin_does_not_close_the_drop(entry: str, chunk: int) -> None:
    # A stray END line on the Cookie line ahead of the BEGIN does not end the block.
    from redaction_structured_cases import PEM_BEGIN

    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    end = PEM_BEGIN.replace("BEGIN", "END")
    lines = [synth(64, B64, 140 + k) for k in range(47)]
    text = f"Cookie: theme={'a' * 8200}{end}{'x' * 30}{PEM_BEGIN}\n" + "\n".join(lines)
    text += f"\n{end}\nAFTERLINE visible\n"
    s = StreamRedactor() if entry == "structured" else ProductionStreamRedactor()
    out = _feed_in(s, text, chunk)
    assert not any(line[:24] in out for line in lines)


def _hit_anchored_below(base: int) -> object:
    from kiro_crew.security.redaction_structured.model import SECRET, Hit

    return Hit(base + 5, base + 100, "http.session_cookie", SECRET, "cookie", "known_key", anchor=0)


def _returns_in_time(call: Callable[[], object]) -> list[object]:
    import threading

    from kiro_crew.testing.wait import default_timeout

    result: list[object] = []
    worker = threading.Thread(target=lambda: result.append(call()), daemon=True)
    worker.start()
    worker.join(default_timeout())
    return result


def test_the_cut_retreat_ends_for_a_hit_anchored_in_emitted_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hit decided from text already emitted (anchor below the window's base)
    # cannot pull the cut back past the base: the retreat stops and holds.
    from kiro_crew.security.redaction_structured import compat

    ctx, buf = "Cookie: theme=" + "a" * 30, "x" * 5 + "V" * 95 + "y" * 20
    hit = _hit_anchored_below(len(ctx))
    monkeypatch.setattr(
        compat,
        "_read",
        lambda text, final=True: compat._Read(
            ctx + text, [hit], [hit], None, len(ctx), None
        ),  # noqa: SLF001
    )
    result = _returns_in_time(lambda: compat.stream_read(ctx, buf, boundary=False))
    assert result, "stream_read did not return"
    assert result[0].cut == 0 and result[0].undecided  # type: ignore[attr-defined]


def test_the_structured_stream_cut_retreat_ends_for_a_hit_anchored_in_committed_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.security.redaction_structured import stream
    from kiro_crew.security.redaction_structured.engine import ScanResult

    ctx, buf = "Cookie: theme=" + "a" * 30, "x" * 5 + "V" * 95 + "y" * 20
    hit = _hit_anchored_below(len(ctx))
    monkeypatch.setattr(stream, "scan", lambda w, final, origin: ScanResult([hit], None))
    monkeypatch.setattr(stream, "resolve", lambda hits: hits)
    s = StreamRedactor()
    s._ctx = ctx  # noqa: SLF001
    result = _returns_in_time(lambda: s.feed(buf))
    assert result == [""]


def test_a_cookie_name_split_from_its_value_across_feeds_returns() -> None:
    from kiro_crew.security import StreamRedactor as ProductionStreamRedactor

    value = synth(48, B64, 29)
    tail = f'": theme={PEM_BEGIN}; session={value}";\nafter'
    for first, second in (("Cookie ", tail), ('"Cookie', " " + tail)):
        s = ProductionStreamRedactor()
        result = _returns_in_time(lambda: s.feed(first) + s.feed(second) + s.flush())
        assert result and value not in str(result[0])


def test_a_cookie_name_with_no_separator_streams_in_linear_work() -> None:
    # Each ``cookie`` anchor with no separator after it is decided from a bounded
    # run of blanks, so the package's work per input byte stays flat: within 2.2x
    # across 8 KiB, 64 KiB and 512 KiB, counted in executed lines, never timed.
    from redaction_structured_cases import package_lines

    def per_byte(size: int) -> float:
        text = ("cookie x " * (size // 9 + 1))[:size]

        def run() -> None:
            s = StreamRedactor()
            s.feed(text)
            s.flush()

        return package_lines(run) / size

    rates = [per_byte(size) for size in (8 << 10, 64 << 10, 512 << 10)]
    assert max(rates) <= min(rates) * 2.2, rates


# ── Linear work on adversarial input ──


def _adversarial() -> list[tuple[str, Callable[[int], str]]]:
    def fill(unit: str, lead: str = "") -> Callable[[int], str]:
        return lambda n: lead + (unit * (n // len(unit) + 1))[:n]

    return [
        ("ghp_near_miss", fill("ghp_" + "a" * 35 + " ")),
        ("ghp_glued", fill("ghp_")),
        ("eyj_dots", fill("eyJ.")),
        ("eyj_link_shape", fill("eyJ" + "A" * 96 + ".")),
        ("begin_headers", fill("-----" + "BEGIN ")),
        ("begin_typed", fill(PEM_BEGIN + " ")),
        ("key_ids_beside_secrets", fill(f"{aws_key_id()} {aws_secret()} ")),
        ("akia_eq", fill("AKIA=")),
        ("known_key_repeat", fill("aws_secret_access_key=")),
        ("known_key_json", fill('"SecretAccessKey": "')),
        ("arn_repeat", fill("arn:")),
        ("url_repeat", fill("https://")),
        ("url_nested_query", fill("a://h/?state=")),
        ("anthropic_near", fill("sk-ant-api0")),
        ("b64_run", fill("A")),
        ("session_run", fill("A", lead="SessionToken=")),
        ("mask_run_lower", fill("x")),
        ("mask_run_upper", fill("X")),
        ("star_run", fill("*")),
        ("plain", fill("lorem ipsum dolor ")),
        ("deep_quotes", fill('"\\"')),
        ("cookie_pairs", fill("cookie=AAAAAAAA; ")),
        ("cookie_header_pairs", fill("a=AAAAAAAA; cookie=B; ", lead="Cookie: ")),
        ("cookie_colon", fill("cookie: ")),
        ("cookie_line", fill("a=1; ", lead="Cookie: ")),
        ("cookie_no_sep", fill("cookie x ")),
    ]


def _streamed(text: str) -> None:
    s = StreamRedactor()
    for i in range(0, len(text), 4096):
        s.feed(text[i : i + 4096])
    s.flush()


@pytest.mark.parametrize("build", [pytest.param(b, id=n) for n, b in _adversarial()])
def test_adversarial_input_costs_linear_work_whole(build: Callable[[int], str]) -> None:
    assert_linear_work(build, redact)


@pytest.mark.parametrize("build", [pytest.param(b, id=n) for n, b in _adversarial()])
def test_adversarial_input_costs_linear_work_streamed(build: Callable[[int], str]) -> None:
    assert_linear_work(build, _streamed)


@pytest.mark.parametrize("window", [' password="Zq8v', ' password="Zq8v Lm', ' password="Zq8v密'])
def test_an_open_unit_that_started_before_the_origin_still_holds(window: str) -> None:
    """The label sits in the look-behind (before ``origin``) and its value is
    still open, so the scan reports it open from ``origin`` instead of dropping it."""
    from kiro_crew.security.redaction_structured.engine import scan

    assert scan(window, final=False, origin=1).open_from == 1
