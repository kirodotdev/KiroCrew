"""Split sweep for the side chat's segment redactor.

The side chat draws a reply as text rows split by tool rows, and settles each
row from a pass over the whole reply (``_SegmentRedactor`` in
``dashboard/handlers/side.py``). The invariant this file pins: whatever the
batch pass ``redact()`` removes from the whole reply is in NO frame the side
panel receives -- not a live delta, not a settling frame, not the terminal
frame -- wherever the tool call lands in the text and however the text is
chunked. Each credential shape below is swept over every cut position (every
120th for the long shapes), with each side of the cut delivered in one chunk
and in small chunks (see ``_chunks``).

The sweep is a model of the handler, not the handler: it drives the same
object the handler drives (``feed`` per chunk, ``close_row`` at the tool call,
``finish`` at the end) and applies the same client rule the handler's tests
apply (``_client_rows``), so the end-to-end tests in ``test_side.py`` and this
sweep share one contract.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.dashboard.handlers.side import _open_credential_start, _SegmentRedactor
from kiro_crew.security import StreamRedactor, redact

# Split spelling of the key header, as the repo's other tests spell it: the
# runtime value is a real header, the source is not. Do not join these back up.
_dashes = "-" * 5
_kind = "RSA PRIVATE KEY"
_BEGIN = f"{_dashes}BEGIN {_kind}{_dashes}"
_END = f"{_dashes}END {_kind}{_dashes}"

_TOKEN = "opaque" + "x7Qz" * 10
_PASSWORD = "s3cretPASSw0rd"
_KEY_BODY = "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFE"  # 35 chars: under the bare-secret floor
_LONG = "A" * 4200  # past the stream redactor's JWT hold-back cap

# (name, reply text, secret runs that redact(reply) removes)
SHAPES: list[tuple[str, str, tuple[str, ...]]] = [
    ("plain", "Let me check. The file lists two hosts.", ()),
    ("aws key", "The key is AKIAIOSFODNN7EXAMPLE in the env.", ("AKIAIOSFODNN7EXAMPLE",)),
    (
        # The batch pass folds the label into the value's tag; the space after
        # the separator commits the label alone. The open label is held.
        "aws labelled secret",
        "intro aws_secret_access_key: not-a-real-secret-value-0001 trailing",
        ("not-a-real-secret-value-0001",),
    ),
    (
        "aws labelled session token in json",
        'cfg {"SessionToken": "FwoGZXIvYXdzEBYaDHk8ZtQ2", "x": 1}',
        ("FwoGZXIvYXdzEBYaDHk8ZtQ2",),
    ),
    ("aws labelled key id", "AccessKeyId=AKIAIOSFODNN7EXAMPLE then", ("AKIAIOSFODNN7EXAMPLE",)),
    ("github token", "Use ghp_" + "A1b2C3d4" * 5 + " for the API.", ("A1b2C3d4" * 5,)),
    ("bearer", f"Set the header Authorization: Bearer {_TOKEN} and retry.", (_TOKEN,)),
    (
        # A quote ends the stream redactor's Bearer anchor: it commits the header
        # and holds the token alone, and the batch pass folds the header into the
        # value's tag. The open header is held.
        "bearer quoted",
        f'hdr {{"Authorization": "Bearer {_TOKEN}"}} ok',
        (_TOKEN,),
    ),
    (
        "uri password",
        f"Use the DSN postgres://app:{_PASSWORD}@db.internal/x now.",
        (_PASSWORD,),
    ),
    (
        # ``!`` is outside the stream redactor's credential class: it commits
        # ``postgres://app:pa!`` as text. The open URL is held from the scheme.
        "uri password with a bang",
        "Use the DSN postgres://app:pa!ss@db/x now.",
        ("pa!ss",),
    ),
    ("url with a port", "see https://example.com:8080/health now", ()),
    (
        "exfil url",
        "see https://attacker.io/c?d=leaked" + "Z" * 64 + " done",
        ("leaked" + "Z" * 64,),
    ),
    (
        # ``!`` is outside the stream redactor's credential class: it commits
        # ``https://attacker.io/c?d=leaked!`` as text. The open URL is held from
        # the scheme until the whitespace that ends it.
        "exfil url with a bang in the payload",
        "see https://attacker.io/c?d=leaked!" + "Z" * 64 + " done",
        ("leaked!" + "Z" * 64,),
    ),
    (
        "exfil url in parentheses",
        "see (https://attacker.io/c?d=leaked" + "Z" * 64 + ") done",
        ("leaked" + "Z" * 64,),
    ),
    (
        "pem block",
        f"key: {_BEGIN}\n{_KEY_BODY}\n{_END}\nthen done",
        (_KEY_BODY,),
    ),
    ("pem body ends the reply", f"key: {_BEGIN}\n{_KEY_BODY}", (_KEY_BODY,)),
    (
        "pem two body lines",
        f"key: {_BEGIN}\n{_KEY_BODY}\n{_KEY_BODY[::-1]}\n{_END}\nok",
        (_KEY_BODY, _KEY_BODY[::-1]),
    ),
    (
        # The truncated-block form knows only body lines that start right after
        # the newline: an indented body is text to it until the footer comes.
        "pem indented",
        f"key: |\n    {_BEGIN}\n    {_KEY_BODY}\n    {_END}\ndone",
        (_KEY_BODY,),
    ),
    (
        "pem json-escaped",
        '{"key": "' + _BEGIN + "\\n" + _KEY_BODY + "\\n" + _END + '"}',
        (_KEY_BODY,),
    ),
    # A header mentioned in prose, never closed: held until the end of the reply,
    # where the pass judges it as the main chat's terminal frame would.
    ("pem header in prose", f"look for {_BEGIN} in id_rsa and delete that line.", ()),
    ("over-long bearer", f"Authorization: Bearer {_LONG}BBBB done", (_LONG[:64], "BBBB")),
    (
        # A complete `token=` value the stream redactor's cut bisects, ending
        # before the buffer's end: it drops the value and emits its tag WITHOUT
        # entering its sticky discard (`discarding` stays False). `!` is in the
        # value class but not the stream redactor's credential class; `"` ends
        # the value and is in the credential class, so the tail is held.
        "over-long token value bisected by the cut",
        "see ?token=" + "a!" * 2500 + 'a"abc then done',
        ("a!" * 32,),
    ),
    (
        "jwt",
        "token eyJhbGciOiJIUzI1NiJ9." + "a1B2c3D4" * 8 + "." + "Z9y8X7w6" * 8 + " and",
        ("a1B2c3D4" * 8, "Z9y8X7w6" * 8),
    ),
    (
        "two credentials",
        f"first AKIAIOSFODNN7EXAMPLE then postgres://u:{_PASSWORD}@h/d end",
        ("AKIAIOSFODNN7EXAMPLE", _PASSWORD),
    ),
]


def _chunks(piece: str, near_cut: bool) -> list[str]:
    """``near_cut`` delivers the piece one character at a time; otherwise the
    64 characters on either side of the piece's ends go one by one and the
    middle in one chunk, so a 4,000-character value does not cost 4,000 feeds."""
    if near_cut or len(piece) <= 128:
        return list(piece)
    return [*piece[:64], piece[64:-64], *piece[-64:]]


def _frames_for(
    text: str, cuts: tuple[int, ...], by_char: bool, fed: list[str] | None = None
) -> list[dict]:
    """Drive the redactor as the handler does and return the frames it would send.

    ``cuts`` are the text offsets at which a tool row lands (each produces a
    ``tool`` frame, preceded by the row's settling frame when the redactor
    returns one); ``by_char`` delivers the text in small chunks (see ``_chunks``).
    """
    return _drive(text, cuts, by_char, fed)[0]


def _drive(
    text: str, cuts: tuple[int, ...], by_char: bool, fed: list[str] | None = None
) -> tuple[list[dict], str]:
    """``_frames_for`` plus the reply as the handler stores it (``reply()``).
    ``fed`` collects the chunks in the order they were fed."""
    r = _SegmentRedactor()
    frames: list[dict] = []

    def _feed(piece: str) -> None:
        for chunk in (_chunks(piece, len(text) <= 256) if by_char else [piece]):
            if chunk:
                if fed is not None:
                    fed.append(chunk)
                out = r.feed(chunk)
                if out:
                    frames.append({"role": "assistant", "content": out})

    pos = 0
    for cut in cuts:
        _feed(text[pos:cut])
        settled = r.close_row()
        if settled is not None:
            frames.append({"role": "assistant", "content": settled, "settle": True})
        frames.append({"role": "tool", "content": "🔧 tool"})
        pos = cut
    _feed(text[pos:])
    frames.append({"role": "assistant", "content": r.finish(), "final": True})
    return frames, r.reply()


def _client_rows(frames: list[dict]) -> list[tuple[str, str]]:
    """The side reducer's rule (``store/chat/side.ts``), as ``test_side.py`` models it."""
    rows: list[tuple[str, str]] = []
    for d in frames:
        if d["role"] == "tool":
            rows.append(("tool", d["content"]))
            continue
        whole = d.get("settle") or d.get("final")
        if rows and rows[-1][0] == "assistant":
            if not whole:
                rows[-1] = ("assistant", rows[-1][1] + d["content"])
            elif d["content"]:
                rows[-1] = ("assistant", d["content"])
            else:
                rows.pop()
            continue
        if whole and not d["content"]:
            continue
        rows.append(("assistant", d["content"]))
    return rows


def _main_chat_shows(fed: list[str]) -> str:
    """What the main chat's live stream draws for these chunks: the stream
    redactor's output as it comes, then its flush."""
    ws = StreamRedactor()
    return "".join(ws.feed(c) for c in fed) + ws.flush()


def _windows(secret: str):
    # Any 12-char window of the secret is a leak: a split can expose a part.
    for i in range(0, max(1, len(secret) - 11), 6):
        yield secret[i : i + 12]


# The stream redactor's credential class (``security._CRED_CLASS``), as a run.
_CRED_RUN_RE = re.compile(r"""[A-Za-z0-9_\-+/=.:@%~"'?&#]{12,}""")


def _assert_parity(name: str, text: str, frames: list[dict], where: str, fed: list[str]):
    """Every 12-character window of credential-class text the side chat shows
    live is one the main chat's stream shows for the same chunks: the side chat
    never draws a byte the stream redactor withheld or dropped, whatever the
    batch pass makes of it (a value past the hold-back ceiling is one the batch
    patterns cannot judge). Checked over the whole reply's credential-class
    runs, not the shapes' listed secrets, so a run no pattern names counts."""
    shown_main = _main_chat_shows(fed)
    shown_side = "".join(d["content"] for d in frames if d["role"] == "assistant")
    for run in _CRED_RUN_RE.findall(text):
        for window in _windows(run):
            if window in shown_side:
                assert window in shown_main, (name, where, window)


def _assert_clean(
    name: str,
    text: str,
    secrets: tuple[str, ...],
    frames: list[dict],
    where: str,
    fed: list[str] | None = None,
    reply: str | None = None,
):
    whole = redact(text)
    # A 12-character window of a planted secret that the batch pass removes from
    # the whole reply is in no frame. (The named shapes plant values the pass
    # removes whole; the random test may plant one it catches only in part, and
    # a window it leaves is judged by parity below, not here.) One exception,
    # shared with the main chat and pinned by parity: a value the stream
    # redactor's 512-character floor bisects (a long run with no anchor it knows
    # delivered in one chunk) streams its head on both surfaces.
    removed = [w for secret in secrets for w in _windows(secret) if w not in whole]
    if fed is not None:
        shown_main = _main_chat_shows(fed)
        removed = [w for w in removed if w not in shown_main]
    for frame in frames:
        for window in removed:
            assert window not in frame["content"], (name, where, window, frame)
    if fed is not None:
        _assert_parity(name, text, frames, where, fed)
    if reply is not None:
        # The stored reply shows nothing the batch pass removes and is a fixed
        # point of that pass (no tag is re-redacted). It is `redact()` over the
        # reply as written except where the stream redactor dropped a value,
        # which it tags as the main chat's stream does.
        for window in removed:
            assert window not in reply, (name, where, window)
        assert redact(reply) == reply, (name, where, reply)
        whole = reply
    # Live deltas drawn for a row never show more than the frame that settles it:
    # a delta is a prefix of what the row ends up as, so the client never has to
    # append a rewritten copy under raw text.
    rows = _client_rows(frames)
    streamed_rows: list[str] = []
    cur = ""
    for d in frames:
        if d["role"] == "tool":
            streamed_rows.append(cur)
            cur = ""
        elif not d.get("settle") and not d.get("final"):
            cur += d["content"]
    streamed_rows.append(cur)
    text_rows = [c for r, c in rows if r == "assistant"]
    assert len(text_rows) <= len(streamed_rows), (name, where)
    # The rows together ARE the stored reply: no row shows a byte the reply does
    # not have (a row above a tool row is settled once and cannot be taken back,
    # so text the batch pass would remove with what the model wrote after the
    # call must never have been settled there), and nothing the model wrote is
    # lost except what the pass removes.
    joined = "".join(text_rows)
    assert joined == whole, (name, where, joined, whole)


def _cuts(text: str) -> range:
    """Every cut position for a short reply; for a long one (the over-long value
    shapes), every ``step``-th position, so the sweep stays within seconds."""
    return range(0, len(text) + 1, max(1, len(text) // 120))


@pytest.mark.parametrize("name,text,secrets", SHAPES, ids=[s[0] for s in SHAPES])
@pytest.mark.parametrize("by_char", [False, True], ids=["one-chunk", "per-char"])
def test_one_tool_call_at_every_position(name, text, secrets, by_char):
    for cut in _cuts(text):
        fed: list[str] = []
        frames, reply = _drive(text, (cut,), by_char, fed)
        _assert_clean(name, text, secrets, frames, f"cut={cut}", fed, reply)


@pytest.mark.parametrize("name,text,secrets", SHAPES, ids=[s[0] for s in SHAPES])
def test_two_tool_calls_at_every_pair_of_positions(name, text, secrets):
    step = max(1, len(text) // 40)
    for a in range(0, len(text) + 1, step):
        for b in range(a, len(text) + 1, step):
            fed: list[str] = []
            frames, reply = _drive(text, (a, b), False, fed)
            _assert_clean(name, text, secrets, frames, f"cuts={a},{b}", fed, reply)


@pytest.mark.parametrize("name,text,secrets", SHAPES, ids=[s[0] for s in SHAPES])
def test_no_tool_call_matches_the_main_chat_contract(name, text, secrets):
    """With no tool row the terminal frame is the whole redacted reply, as in the
    main chat, and the client ends with that one row: a delta the final pass
    rewrites (a key footer's words, drawn before the body proved the block) is
    replaced by the terminal frame, never kept under it. The reply is
    ``redact()`` over the text as written unless the stream redactor dropped a
    value, which only its own tag can stand for."""
    for by_char in (False, True):
        fed: list[str] = []
        frames, reply = _drive(text, (), by_char, fed)
        _assert_clean(name, text, secrets, frames, f"by_char={by_char}", fed, reply)
        assert frames[-1]["content"] == reply
        assert _client_rows(frames) == [("assistant", reply)], (name, by_char)
        if "[REDACTED: credential]" not in _main_chat_shows(fed):
            assert reply == redact(text), (name, by_char)


# A JWT whose claims run past the stream redactor's hold-back ceiling before a
# signature arrives: the stream redactor tags it and DROPS the rest, and the
# batch patterns (a signed JWT has two dots) never recognise it, so ``redact()``
# over the reply as written would hand the claims on. Spelt from a fixed seed so
# the shape is stable and holds no real token.
_JWT_HEAD = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"
_CLAIMS = "".join(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"[(i * 7 + i // 13) % 64]
    for i in range(4200)
)
_UNSIGNED_JWT = f"{_JWT_HEAD}.{_CLAIMS}"
# (name, reply text, runs the stream redactor drops that no frame may show)
DROPPED_SHAPES: list[tuple[str, str, tuple[str, ...]]] = [
    (
        # The chunk that ends the drop also opens a key block: the rest of that
        # chunk must reach the view as written (its header is what the hold and
        # the block pattern need), not as the stream redactor's redaction of it
        # (which tags a header on its own and leaves nothing for the body).
        "unsigned jwt then an indented key block",
        f"token {_UNSIGNED_JWT} key: |\n    {_BEGIN}\n    {_KEY_BODY}\n    {_END}\ndone",
        (_CLAIMS, _KEY_BODY),
    ),
    ("unsigned jwt ends the reply", f"The token is {_UNSIGNED_JWT}", (_CLAIMS,)),
    ("unsigned jwt then text", f"The token is {_UNSIGNED_JWT} and done", (_CLAIMS,)),
    (
        # The chunk that ends the dropped value carries a second credential: the
        # rest of that chunk is kept as written, so the pass still judges it.
        "unsigned jwt then a key",
        f"token {_UNSIGNED_JWT} key AKIAIOSFODNN7EXAMPLE end",
        (_CLAIMS, "AKIAIOSFODNN7EXAMPLE"),
    ),
]


@pytest.mark.parametrize("name,text,secrets", DROPPED_SHAPES, ids=[s[0] for s in DROPPED_SHAPES])
def test_a_value_the_stream_redactor_drops_is_in_no_frame(name, text, secrets):
    """The pass runs over the reply as written except for a value the stream
    redactor dropped, which it sees as the tag. So every window of the value
    the main chat's stream withholds for a chunking is in no frame and not in
    the stored reply, wherever the tool call lands; and the sweep's invariants
    hold (``_assert_clean``: the other planted values too). (``_chunks``
    delivers the long middle of the text in one piece: a value whose terminator
    arrives in that piece is judged by the batch patterns alone, on this surface
    as on the main chat's, and parity is what holds then.)"""
    dropped_somewhere = False
    for cut in _cuts(text):
        fed: list[str] = []
        frames, reply = _drive(text, (cut,), True, fed)
        shown_main = _main_chat_shows(fed)
        for secret in secrets:
            for window in _windows(secret):
                if window in shown_main:
                    continue
                dropped_somewhere = True
                for frame in frames:
                    assert window not in frame["content"], (name, cut, window, frame)
                assert window not in reply, (name, cut, window)
        _assert_clean(name, text, secrets, frames, f"cut={cut}", fed, reply)
    assert dropped_somewhere, name  # the shape exercises a drop for some cut


@pytest.mark.parametrize("name,text,secrets", DROPPED_SHAPES, ids=[s[0] for s in DROPPED_SHAPES])
def test_the_live_view_shows_nothing_of_a_dropped_value_the_main_chat_withholds(
    name, text, secrets
):
    """Parity with the main chat, which draws the stream redactor's output: for
    every chunking, including the one chunk the stream redactor cannot protect
    (head and terminator together), a window of the value the side chat shows
    live is one the main chat shows too."""
    from kiro_crew.security import StreamRedactor

    for chunks in ([text], [text[i : i + 64] for i in range(0, len(text), 64)], list(text)):
        ws = StreamRedactor()
        main = "".join(ws.feed(c) for c in chunks) + ws.flush()
        r = _SegmentRedactor()
        side = "".join(r.feed(c) for c in chunks)
        for secret in secrets:
            for i in range(0, max(1, len(secret) - 11), 6):
                window = secret[i : i + 12]
                assert window not in side or window in main, (name, len(chunks), window)


def test_rows_split_a_plain_reply_where_the_tool_landed():
    text = "Let me check. The file lists two hosts."
    cut = len("Let me check. ")
    rows = _client_rows(_frames_for(text, (cut,), True))
    assert rows == [
        ("assistant", "Let me check. "),
        ("tool", "🔧 tool"),
        ("assistant", "The file lists two hosts."),
    ]


def test_a_pass_that_rewrites_a_drawn_row_is_fixed_by_the_frame_that_settles_it(monkeypatch):
    """A later pass can rewrite text a row already shows when the batch redactor
    judges the earlier text by what follows it. The live view cannot take a byte
    back: the delta that would contradict the row is withheld, and the frame
    that closes the row (``settle`` above a tool row, the terminal frame for the
    last row) carries the row as the pass has it; the client replaces the row.
    No pattern of the real redactor does this once the open key block and URL
    are held, so the mechanism is pinned with a redactor whose judgment of
    ``alpha`` waits for ``omega``."""
    from kiro_crew.dashboard.handlers import side as side_module

    real_redact = side_module.redact

    def _late_judgment(text: str) -> str:
        out = real_redact(text)
        return out.replace("alpha", "[REDACTED: credential]") if "omega" in text else out

    monkeypatch.setattr(side_module, "redact", _late_judgment)
    r = _SegmentRedactor()
    assert r.feed("say alpha ") == "say alpha "
    assert r.feed("omega ") == ""  # the pass now rewrites the drawn ``alpha``: withheld
    assert r.close_row() == "say [REDACTED: credential] omega "
    assert r.feed("done") == ""  # ``done`` is a run the stream redactor still holds
    assert r.finish() == "done"
    frames = [
        {"role": "assistant", "content": "say alpha "},
        {"role": "assistant", "content": "say [REDACTED: credential] omega ", "settle": True},
        {"role": "tool", "content": "🔧 tool"},
        {"role": "assistant", "content": "done", "final": True},
    ]
    assert _client_rows(frames) == [
        ("assistant", "say [REDACTED: credential] omega "),
        ("tool", "🔧 tool"),
        ("assistant", "done"),
    ]
    # With no tool row the terminal frame does the same for the one row.
    r = _SegmentRedactor()
    assert r.feed("say alpha ") == "say alpha "
    assert r.feed("omega done") == ""
    assert r.finish() == "say [REDACTED: credential] omega done"


def test_an_open_key_block_url_or_label_is_held_until_the_text_that_decides_it():
    """What the holds cost a reply with no credential in it: an ordinary URL
    waits for the whitespace that ends it (its path or query may still turn out
    to carry a password or an exfiltration payload); an AWS field name waits for
    its value's first byte; a key header mentioned in prose waits for the end of
    the reply. Nothing is lost, and nothing of either is above a tool row that
    lands inside the hold."""
    r = _SegmentRedactor()
    assert r.feed("see https://example.com:8080") == "see "
    assert r.feed("/health") == ""
    assert r.feed(" now") == "https://example.com:8080/health "
    assert r.finish() == "see https://example.com:8080/health now"

    r = _SegmentRedactor()
    assert r.feed("the aws_secret_access_key ") == "the "  # the label waits
    assert r.feed("field ") == "aws_secret_access_key field "  # no separator: prose
    assert r.finish() == "the aws_secret_access_key field "

    text = f"look for {_BEGIN} in id_rsa and delete that line."
    frames = _frames_for(text, (len(f"look for {_BEGIN} in "),), False)
    assert _client_rows(frames) == [
        ("assistant", "look for "),
        ("tool", "🔧 tool"),
        ("assistant", redact(text)[len("look for ") :]),
    ]


def test_a_long_reply_bounds_its_live_passes(monkeypatch):
    """Past ``_PASS_EVERY_COMMIT_BELOW`` committed characters a live pass waits
    for a step of new text, so the cost of the whole-text pass stays bounded; a
    delta that waited is drawn with the next pass, and nothing is lost."""
    from kiro_crew.dashboard.handlers import side as side_module

    real_redact = side_module.redact
    passes = 0

    def _counting(text: str) -> str:
        nonlocal passes
        passes += 1
        return real_redact(text)

    monkeypatch.setattr(side_module, "redact", _counting)
    r = _SegmentRedactor()
    word = "word "
    drawn = ""
    for _ in range(12_000):  # 60k characters, one word per chunk
        drawn += r.feed(word)
    whole = word * 12_000
    assert whole.startswith(drawn) and len(drawn) > len(whole) - 2_000
    assert r.finish() == whole
    # One pass per chunk for the first 8,192 characters (1,639 chunks), then
    # about 32 per doubling of the committed text up to 32k and one per 1,024
    # characters past it: under 100 more, where one per chunk would be 10,000.
    assert 1_639 < passes < 1_750, passes


# ── Property test: random replies, random chunking, random tool rows ──
#
# The shapes above are the ones a reviewer named. This generates replies from
# every credential family the batch pass knows, joined by separators that
# include bytes outside the stream redactor's credential class (``!``, ``(``,
# ``)``, quotes, newlines), delivers them in random chunks, lands tool rows at
# random offsets, and checks the same four invariants as the sweep: no window
# of a planted secret in any frame or in the stored reply; the side chat shows
# live no credential-class window the main chat's stream withholds; the rows
# together are exactly the stored reply; the stored reply is a fixed point of
# the batch pass.

_B64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def _run(alphabet: str, n: int):
    return st.text(alphabet=alphabet, min_size=n, max_size=n)


@st.composite
def _credential(draw) -> tuple[str, str]:
    """``(text, secret)``: one credential in a shape the batch pass redacts, and
    the run of it that must appear in no frame."""
    kind = draw(
        st.sampled_from(
            [
                "aws",
                "aws label",
                "ghp",
                "slack",
                "stripe",
                "telegram",
                "bearer",
                "token",
                "uri",
                "jwt",
                "pem",
                "pem indented",
                "exfil",
                "long",
            ]
        )
    )
    if kind == "aws":
        return "AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"
    if kind == "aws label":
        label = draw(
            st.sampled_from(["aws_secret_access_key: ", 'SessionToken": "', "AccessKeyId="])
        )
        s = draw(_run(_B64, 32))
        return f"{label}{s}", s
    if kind == "ghp":
        s = draw(_run("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789", 36))
        return f"ghp_{s}", s
    if kind == "slack":
        s = draw(_run("abcdefghijklmnopqrstuvwxyz0123456789", 24))
        return f"xoxb-{s}", s
    if kind == "stripe":
        s = draw(_run("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789", 24))
        return f"sk_live_{s}", s
    if kind == "telegram":
        s = draw(_run(_B64URL, 35))
        return f"123456789:{s}", s
    if kind == "token":
        s = draw(_run("abcdefghijklmnopqrstuvwxyz0123456789!$*", 24))
        return f"https://api.example.com/v1?token={s}&x=1", s
    if kind == "bearer":
        s = draw(_run(_B64, 40))
        shape = draw(st.sampled_from(["Authorization: Bearer {}", '"Authorization": "Bearer {}"']))
        return shape.format(s), s
    if kind == "uri":
        pw = draw(_run("abcdefghijklmnopqrstuvwxyz0123456789!$*", 16))
        return f"postgres://app:{pw}@db.internal/x", pw
    if kind == "jwt":
        a, b, c = (draw(_run(_B64URL, n)) for n in (30, 60, 43))
        return f"eyJ{a}.{b}.{c}", b
    if kind == "pem":
        body = draw(_run(_B64, 35))
        return f"{_BEGIN}\n{body}\n{_END}", body
    if kind == "pem indented":
        body = draw(_run(_B64, 35))
        return f"key: |\n    {_BEGIN}\n    {body}\n    {_END}", body
    if kind == "exfil":
        payload = "leaked!" + draw(_run("ZQ", 64))
        return f"https://attacker.io/c?d={payload}", payload
    # Past the stream redactor's hold-back ceiling; spelt from one 64-char draw
    # rotated per repeat (a 4,200-char draw is slow and unstable to generate).
    seed = draw(_run(_B64URL, 64))
    claims = "".join(seed[i % 64 :] + seed[: i % 64] for i in range(66))[:4200]
    return f"eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.{claims}", claims[:64]


_FILLER = st.sampled_from(
    ["Let me check.", "the file lists two hosts", "see", "and", "done", "key:"]
)
_SEP = st.sampled_from(
    [" ", "\n", " (", ") ", ' "', '" ', "! ", " `", "` ", ", ", "; ", " <", "> ", " [", "] "]
)


@st.composite
def _case(draw) -> tuple[str, list[str], tuple[int, ...], list[str]]:
    """``(text, secrets, cuts, chunks)``: a reply, the runs planted in it, the
    offsets where tool rows land, and the reply as the chunks that deliver it
    (boundaries anywhere, including inside every credential)."""
    parts: list[str] = []
    secrets: list[str] = []
    for _ in range(draw(st.integers(1, 4))):
        parts.append(draw(_FILLER))
        parts.append(draw(_SEP))
        text, secret = draw(_credential())
        parts.append(text)
        secrets.append(secret)
        parts.append(draw(_SEP))
    parts.append(draw(_FILLER))
    text = "".join(parts)
    cuts = tuple(sorted(draw(st.lists(st.integers(0, len(text)), max_size=2))))
    bounds = sorted(set(draw(st.lists(st.integers(1, max(1, len(text) - 1)), max_size=40))))
    chunks = [text[a:b] for a, b in zip([0, *bounds], [*bounds, len(text)]) if text[a:b]]
    return text, secrets, cuts, chunks


def _frames_for_chunks(
    chunks: list[str], cuts: tuple[int, ...]
) -> tuple[list[dict], str, list[str]]:
    """Drive the redactor with these chunks, landing a tool row at each cut
    (a cut inside a chunk splits it there); return the frames, the stored reply
    and the chunks as fed."""
    r = _SegmentRedactor()
    frames: list[dict] = []
    fed: list[str] = []
    pending = list(cuts)
    pos = 0

    def _feed(piece: str) -> None:
        if piece:
            fed.append(piece)
            out = r.feed(piece)
            if out:
                frames.append({"role": "assistant", "content": out})

    def _tool() -> None:
        settled = r.close_row()
        if settled is not None:
            frames.append({"role": "assistant", "content": settled, "settle": True})
        frames.append({"role": "tool", "content": "🔧 tool"})

    for chunk in chunks:
        while pending and pos <= pending[0] <= pos + len(chunk):
            cut = pending.pop(0)
            _feed(chunk[: cut - pos])
            chunk = chunk[cut - pos :]
            pos = cut
            _tool()
        _feed(chunk)
        pos += len(chunk)
    for _ in pending:  # a cut at the very end of an empty reply
        _tool()
    frames.append({"role": "assistant", "content": r.finish(), "final": True})
    return frames, r.reply(), fed


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(case=_case())
def test_random_replies_chunked_and_split_at_random_keep_every_invariant(case):
    text, secrets, cuts, chunks = case
    frames, reply, fed = _frames_for_chunks(chunks, cuts)
    _assert_clean("random", text, tuple(secrets), frames, f"cuts={cuts}", fed, reply)


def test_the_view_is_rebuilt_from_the_stream_redactors_own_commits():
    """``_take`` reads the stream redactor's commits off the batch function it
    is constructed with and puts each back as written in place of its redaction,
    so a committed credential-shaped run (a key header the redactor tags on its
    own, an AWS label) is in the view as the model wrote it, and the redactor's
    own tag for dropped bytes stays a tag. A commit the output does not carry
    where the previous one ended breaks the contract this rests on and fails
    the turn instead of splicing text that is not the reply."""
    r = _SegmentRedactor()
    r.feed(f"key: {_BEGIN}\n")  # the stream redactor commits and tags the header alone
    assert "".join(r._view) == f"key: {_BEGIN}\n"  # the view keeps it as written
    r.feed("x " * 3)
    assert r.feed("end ") == ""  # the open block holds everything from the header

    class _OutOfOrder:
        """A stream redactor whose output does not carry its commit."""

        held = ""
        discarding = False

        def __init__(self, redactor):
            self._redactor = redactor

        def feed(self, text):
            self._redactor(text)  # records a commit ...
            return "[REDACTED: credential]"  # ... that the output does not contain

    r = _SegmentRedactor()
    r._wsred = _OutOfOrder(r._batch)
    with pytest.raises(RuntimeError):
        r.feed("plain text")


def test_the_first_unclosed_key_block_is_held_even_when_a_later_one_closes():
    """Two key blocks, the first never closed: the hold starts at the FIRST
    header without a footer after it, not at the last header, so nothing of the
    first block is settled above a tool row that lands in the prose between
    them (the batch pass spans both into one tag)."""
    text = f"a {_BEGIN}\n{_KEY_BODY}\nprose here\n\n{_BEGIN}\n{_KEY_BODY[::-1]}\n{_END}\ndone"
    assert _open_credential_start(text[: text.index("prose") + 5]) == 2
    for cut in (text.index("prose") + 2, text.index(_END)):
        fed: list[str] = []
        frames, reply = _drive(text, (cut,), True, fed)
        _assert_clean(
            "two blocks", text, (_KEY_BODY, _KEY_BODY[::-1]), frames, f"cut={cut}", fed, reply
        )
        assert _client_rows(frames) == [
            ("assistant", "a "),
            ("tool", "🔧 tool"),
            ("assistant", "[REDACTED: credential]\ndone"),
        ]
