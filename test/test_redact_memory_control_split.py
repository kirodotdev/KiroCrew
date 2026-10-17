"""Control characters must not carry credential material past the memory scrubber.

Both redactors behind ``_redact_memory_field`` decide by matching a pattern against
text. A control character embedded mid-token splits the token, so the pattern cannot
match and the field leaves on an egress path still carrying the credential -- broken
for a consumer that renders it verbatim, whole again for any consumer that drops
control bytes. Separators are spelled as explicit ``bytes`` here so the case under
test is the byte sequence rather than an editor's rendering of it.
"""

from __future__ import annotations

import json
import unicodedata

import pytest

from kiro_crew import memory_edit
from kiro_crew.dashboard.handlers import _shared
from kiro_crew.dashboard.handlers._shared import _UNSCANNABLE_JSON, _redact_memory_field
from kiro_crew.terminal_safe import normalize_for_scanning

#: A documentation-only AWS key id, matched by the plaintext credential pass.
CREDENTIAL = "AKIAIOSFODNN7EXAMPLE"

#: Every character the scrubber removes: the C0 and C1 controls other than the three
#: kept as content, plus the Unicode format characters. The output is checked against
#: this set, because an output holding none of them cannot be reinterpreted or rejoined
#: by any consumer.
INVISIBLE_CHARACTERS = (
    frozenset(
        chr(code)
        for code in list(range(0x00, 0x20)) + list(range(0x7F, 0xA0))
        if chr(code) not in "\t\n\r"
    )
    | frozenset(chr(code) for code in range(0x110000) if unicodedata.category(chr(code)) == "Cf")
    | frozenset("\ufe0f\u034f\u3164\u180b\u17b4\U000e0100")
)

#: Separators made only of control characters. Removing them rejoins the token, so the
#: credential pattern matches and the field is redacted.
CONTROL_ONLY_SEPARATORS = [
    pytest.param(b"\x1b", id="escape"),
    pytest.param(b"\x00", id="c0-nul"),
    pytest.param(b"\x08", id="c0-backspace"),
    pytest.param(b"\x0b", id="c0-vertical-tab"),
    pytest.param(b"\x0c", id="c0-form-feed"),
    pytest.param(b"\x1f", id="c0-unit-separator"),
    pytest.param(b"\x7f", id="delete"),
    pytest.param(b"\x80", id="c1-low"),
    pytest.param(b"\x9b", id="c1-csi"),
    pytest.param(b"\x9c", id="c1-string-terminator"),
    pytest.param(b"\x9d", id="c1-osc"),
    pytest.param(b"\x90", id="c1-dcs"),
    pytest.param(b"\x9e", id="c1-pm"),
    pytest.param(b"\x9f", id="c1-apc"),
    pytest.param(b"\x1b\x1b", id="two-escapes"),
    pytest.param(b"\x00\x9b\x7f", id="mixed-controls"),
]

#: Invisible Unicode code points, whether or not ``unicodedata`` calls them ``Cf``.
#: They render as nothing, so a consumer that drops default-ignorable code points
#: rejoins whatever one of them split.
INVISIBLE_SEPARATORS = [
    pytest.param("\u200b", id="zero-width-space"),
    pytest.param("\u200c", id="zero-width-non-joiner"),
    pytest.param("\u200d", id="zero-width-joiner"),
    pytest.param("\u2060", id="word-joiner"),
    pytest.param("\u00ad", id="soft-hyphen"),
    pytest.param("\u202e", id="right-to-left-override"),
    pytest.param("\ufeff", id="zero-width-no-break-space"),
    pytest.param("\U000e0041", id="tag-latin-a"),
    pytest.param("\ufe0f", id="variation-selector-16"),
    pytest.param("\u034f", id="combining-grapheme-joiner"),
    pytest.param("\u3164", id="hangul-filler"),
    pytest.param("\u180b", id="mongolian-free-variation-selector"),
    pytest.param("\U000e0100", id="variation-selector-17"),
    pytest.param("\u17b4", id="khmer-inherent-vowel"),
]

#: Complete sequences, whose printable payload is content and survives. The token stays
#: split around that payload, which is safe because the output carries no invisible
#: character for a consumer to act on.
PAYLOAD_SEPARATORS = [
    pytest.param(b"\x1b[0m", id="csi-sgr-reset"),
    pytest.param(b"\x9b0m", id="csi-8bit-with-parameters"),
    pytest.param(b"\x1b]0;t\x07", id="osc-through-bel"),
    pytest.param(b"\x9d0;t\x9c", id="osc-8bit-through-st"),
    pytest.param(b"\x1bPq\x1b\\", id="dcs-through-st"),
    pytest.param(b"\x9fx\x9c", id="apc-8bit-through-st"),
]

#: Kept as content, so a token split by one stays split.
CONTENT_SEPARATORS = [
    pytest.param(b"\n", id="newline"),
    pytest.param(b"\t", id="tab"),
    pytest.param(b"\r", id="carriage-return"),
    pytest.param(b"\r\n", id="crlf"),
]

ALL_SEPARATORS = (
    CONTROL_ONLY_SEPARATORS + INVISIBLE_SEPARATORS + PAYLOAD_SEPARATORS + CONTENT_SEPARATORS
)

#: Every separator whose removal rejoins the token, so the credential is matched.
REJOINING_SEPARATORS = CONTROL_ONLY_SEPARATORS + INVISIBLE_SEPARATORS


def _split_credential(separator: bytes | str) -> str:
    """Return ``CREDENTIAL`` with ``separator`` spliced in after its ``AKIA`` prefix."""
    text = separator.decode("latin-1") if isinstance(separator, bytes) else separator
    prefix, suffix = CREDENTIAL[:4], CREDENTIAL[4:]
    return f"{prefix}{text}{suffix}"


#: A value shaped like a Discord bot token, built from repeated characters so the file
#: holds no credential-shaped literal. Its pattern is guarded by a negative lookbehind
#: for a non-word character, which makes it the case where normalising alone is not
#: enough. The runs are sized to the pattern: an ``[MNO]`` anchor, 22 to 30 characters,
#: a six-character middle, then 25 or more.
BOUNDARY_GUARDED_TOKEN = "M" + "A" * 25 + "." + "B" * 6 + "." + "C" * 27


def test_a_word_char_before_an_invisible_char_does_not_hide_the_token() -> None:
    """The scan of the text as stored is what catches a boundary-guarded credential.

    The token's pattern requires a non-word character before it, and the invisible
    character supplies one. Removing it glues the preceding word character onto the
    token, so a scan of the normalised text alone fails to match and the whole token
    would egress. Scanning the original first keeps that verdict.
    """
    stored = f"X\x1b{BOUNDARY_GUARDED_TOKEN}"

    redacted = _redact_memory_field(stored)

    assert BOUNDARY_GUARDED_TOKEN not in redacted
    assert "[REDACTED" in redacted


def test_a_split_boundary_guarded_token_is_still_caught_when_rejoining_helps() -> None:
    """With a clean left boundary, the second pass catches the split token."""
    stored = f"see \x1b{BOUNDARY_GUARDED_TOKEN}"

    redacted = _redact_memory_field(stored)

    assert BOUNDARY_GUARDED_TOKEN not in redacted
    assert "[REDACTED" in redacted


def test_contiguous_credential_is_redacted() -> None:
    """The unsplit case is the control: the pattern matches and the field is scrubbed."""
    redacted = _redact_memory_field(f"never commit {CREDENTIAL}")

    assert CREDENTIAL not in redacted
    assert "[REDACTED" in redacted


@pytest.mark.parametrize("separator", REJOINING_SEPARATORS)
def test_control_split_credential_is_redacted(separator: bytes | str) -> None:
    """A credential split by invisible characters alone is scrubbed, not passed through."""
    redacted = _redact_memory_field(f"never commit {_split_credential(separator)}")

    assert "[REDACTED" in redacted
    assert "IOSFODNN7EXAMPLE" not in redacted


@pytest.mark.parametrize("separator", REJOINING_SEPARATORS)
def test_stripping_never_reassembles_the_credential(separator: bytes | str) -> None:
    """Output carries no whole credential, which is what pins the order of the two steps.

    Redacting first and normalising second leaves this assertion failing while the one
    above still passes: normalisation rejoins the halves the credential pattern just
    failed to match, so the field egresses the complete secret in plaintext.
    """
    redacted = _redact_memory_field(f"never commit {_split_credential(separator)}")

    assert CREDENTIAL not in redacted


@pytest.mark.parametrize("separator", ALL_SEPARATORS)
def test_no_invisible_character_reaches_the_caller(separator: bytes | str) -> None:
    """The output holds no invisible character, whatever split went in.

    This is the guarantee the whole approach rests on. With none left,
    no consumer can reinterpret the text or drop bytes out of it, so a token that still
    looks split to the scanner looks split to every consumer as well.
    """
    redacted = _redact_memory_field(f"never commit {_split_credential(separator)}")

    assert [ch for ch in redacted if ch in INVISIBLE_CHARACTERS] == []


@pytest.mark.parametrize("separator", ALL_SEPARATORS)
def test_the_credential_is_never_recoverable_from_the_output(separator: bytes | str) -> None:
    """No split leaves the credential contiguous in what the caller receives."""
    redacted = _redact_memory_field(f"never commit {_split_credential(separator)}")

    assert CREDENTIAL not in redacted


@pytest.mark.parametrize("separator", PAYLOAD_SEPARATORS)
def test_a_sequence_payload_survives_as_text(separator: bytes) -> None:
    """A sequence's printable bytes are content, so they are kept rather than deleted.

    Consuming them would mean deleting however much visible text sits between an
    introducer and the next terminator, which loses a user's stored note instead of
    sanitising it. The token stays split around the payload, and safely so: the bytes
    that would have told a terminal to hide it are gone.
    """
    redacted = _redact_memory_field(f"never commit {_split_credential(separator)}")
    printable = [ch for ch in separator.decode("latin-1") if ch not in INVISIBLE_CHARACTERS]

    for character in printable:
        assert character in redacted


def test_visible_text_between_an_introducer_and_a_terminator_is_kept() -> None:
    """A stored note bracketed by control characters keeps every visible word."""
    note = "note: \x9d my deploy runbook lives here \x9c see also"

    assert _redact_memory_field(note) == "note:  my deploy runbook lives here  see also"


@pytest.mark.parametrize("separator", CONTENT_SEPARATORS)
def test_whitespace_is_preserved_as_content(separator: bytes) -> None:
    """Tab, newline and carriage return survive, so stored documents are unchanged.

    Dropping the carriage return alone would rewrite every CRLF document into LF, which
    is why it sits with the other two rather than with the controls.
    """
    text = separator.decode("latin-1")

    assert text in _redact_memory_field(f"first{text}second")


def test_crlf_line_endings_round_trip() -> None:
    """A CRLF document comes back byte for byte."""
    document = "line one\r\nline two\r\nline three"

    assert _redact_memory_field(document) == document


def test_control_split_credential_inside_a_container() -> None:
    """The recursion reaches list and dict members, and shape is preserved."""
    by_escape = _split_credential(b"\x1b")
    by_nul = _split_credential(b"\x00")
    payload = {
        "superseded": [f"never commit {by_escape}"],
        "nested": {"rule": f"never commit {by_nul}"},
    }

    redacted = _redact_memory_field(payload)

    assert isinstance(redacted, dict)
    assert isinstance(redacted["superseded"], list)
    assert CREDENTIAL not in redacted["superseded"][0]
    assert CREDENTIAL not in redacted["nested"]["rule"]


def test_ordinary_text_is_unchanged() -> None:
    """Text with no credential and no invisible character passes through byte for byte."""
    value = "commit early, commit often, one\ttab and one\nnewline"

    assert _redact_memory_field(value) == value


def test_binary_is_still_dropped() -> None:
    """Bytes remain unscannable and so remain dropped rather than returned unread."""
    assert _redact_memory_field(b"\x1b[0m") is None
    assert _redact_memory_field(memoryview(b"\x1b[0m")) is None


def test_a_format_char_between_controls_does_not_survive() -> None:
    """Controls cannot shelter a format character inside an ASCII credential.

    Removing the controls first is what makes the flank judgement read the credential's
    own characters rather than the controls that were about to be removed.
    """
    redacted = _redact_memory_field("never commit AKIA\x9b\u200d\x9cIOSFODNN7EXAMPLE")

    assert "[REDACTED" in redacted
    assert CREDENTIAL not in redacted
    assert "\u200d" not in redacted


def test_a_json_encoded_field_does_not_carry_the_credential_out() -> None:
    """Encoding hides the control character from the scan, so the payload is scanned too.

    A memory row carries the same value as text and as a JSON document. JSON writes a
    control character as the six printable characters of an escape, so the control pattern
    finds nothing to remove and the credential it splits matches neither redactor pass.
    Whatever decodes the document gets the control character back, where it renders as
    nothing and the credential reads as whole.
    """
    stored = f"never commit {CREDENTIAL[:8]}\x01{CREDENTIAL[8:]}"
    row = {"text": stored, "value_json": json.dumps(stored)}

    out = _redact_memory_field(row)

    assert CREDENTIAL not in out["text"]
    assert "[REDACTED" in out["value_json"]
    decoded = json.loads(out["value_json"])
    assert CREDENTIAL not in decoded
    assert "\x01" not in decoded


def test_a_json_encoded_object_field_is_scanned_through_its_values() -> None:
    """A document decoding to an object is scanned value by value, shape preserved."""
    stored = f"never commit {CREDENTIAL[:8]}\x01{CREDENTIAL[8:]}"

    out = _redact_memory_field({"value_json": json.dumps({"rule": stored, "tier": "always"})})

    decoded = json.loads(out["value_json"])
    assert CREDENTIAL not in decoded["rule"]
    assert decoded["tier"] == "always"


def test_a_json_field_holding_nothing_sensitive_is_passed_through_unchanged() -> None:
    """Scanning a payload must not re-spell a document it had no reason to touch.

    The document here is spelled with spacing no serialiser would choose, so passing it
    through and re-serialising it are distinguishable.
    """
    document = '{"count":3,  "note":  "commit early, commit often"}'

    assert _redact_memory_field({"value_json": document})["value_json"] == document
    assert _redact_memory_field("123") == "123"
    assert _redact_memory_field('{"broken"') == '{"broken"'
    assert _redact_memory_field("") == ""


def test_a_json_object_name_carries_the_credential_out_unless_it_is_scanned() -> None:
    """A name is text the document carries and the field egresses, so it is scanned too.

    Whoever writes a structured memory row chooses both halves of a pair, and the name is
    the easier half to hide something in: walking only values leaves it byte for byte.
    """
    hidden = f"{CREDENTIAL[:8]}\x01{CREDENTIAL[8:]}"
    row = {"text": f"never commit {hidden}", "value_json": json.dumps({hidden: "ok"})}

    out = _redact_memory_field(row)

    assert CREDENTIAL not in out["text"]
    names = list(json.loads(out["value_json"]).keys())
    assert all(CREDENTIAL not in normalize_for_scanning(name) for name in names), names


def test_two_names_that_scrub_to_one_withhold_the_whole_document() -> None:
    """A dict holds one value per name, so keeping either silently drops the other's.

    Dropping a value is a data loss the reader cannot see, and choosing which to drop is
    not this scrubber's call, so the document is withheld and a marker goes out instead.
    """
    first = f"{CREDENTIAL[:8]}\x01{CREDENTIAL[8:]}"
    second = f"{CREDENTIAL[:8]}\u200b{CREDENTIAL[8:]}"

    out = _redact_memory_field({"value_json": json.dumps({first: 1, second: 2})})

    assert out["value_json"] == json.dumps(_UNSCANNABLE_JSON)
    assert CREDENTIAL not in normalize_for_scanning(out["value_json"])


def test_a_document_nested_past_the_cap_is_withheld_rather_than_half_scanned() -> None:
    """The descent is bounded, and a payload it cannot reach the bottom of is uncertified.

    Emitting the part it did scan would ship whatever sits below the cap unscanned, which
    is the leak this scrubber exists to close, so the whole document is withheld.
    """
    deep = json.dumps(json.loads("[" * 200 + "]" * 200))

    out = _redact_memory_field({"value_json": deep})

    assert out["value_json"] == json.dumps(_UNSCANNABLE_JSON)
    assert json.loads(out["value_json"]) == _UNSCANNABLE_JSON


def test_a_document_that_exhausts_the_parser_does_not_break_the_field() -> None:
    """``RecursionError`` is not a ``ValueError``, so the parse guard has to name it.

    Uncaught, one row of hostile data takes out the whole listing it appears in: the reader
    gets an error instead of every other row. The field is withheld and the rest survives.
    """
    blows_the_parser = "[" * 200000 + "]" * 200000

    out = _redact_memory_field({"text": "fine", "value_json": blows_the_parser})

    assert out["text"] == "fine"
    assert out["value_json"] == json.dumps(_UNSCANNABLE_JSON)


def test_the_withheld_marker_is_valid_json_for_whoever_parses_the_field() -> None:
    """The field's contract is that it parses, so the substitute parses as well."""
    assert json.loads(json.dumps(_UNSCANNABLE_JSON)) == _UNSCANNABLE_JSON
    assert CREDENTIAL not in _UNSCANNABLE_JSON


def test_the_depth_bound_spans_encoding_levels_as_well_as_structural_ones() -> None:
    """Descending into a nested document must not restart the budget it descended under.

    The bound exists because the walk is recursive and the document is untrusted. A count
    reset at each encoding level buys a hostile document the whole budget again per level,
    so the bound stops bounding anything. Here 65 structural levels sit inside ONE encoding
    level: the encode costs one, which puts the innermost past the cap and withholds it.
    """
    inside = json.dumps(json.dumps(json.loads("[" * 65 + "]" * 65)))

    out = _redact_memory_field({"value_json": inside})["value_json"]

    assert json.loads(out) == json.dumps(_UNSCANNABLE_JSON)
    assert "[[[[" not in out

    shallow = json.dumps(json.dumps(json.loads("[" * 40 + "]" * 40)))
    assert _redact_memory_field({"value_json": shallow})["value_json"] == shallow


def test_a_document_inside_a_document_is_scanned_at_every_encoding_level() -> None:
    """One decode does not reach the bottom of a field that carries a document twice.

    A memory record keeps its own value as a JSON document, so a revision snapshot of that
    record is a document holding a document. The outer decode yields a STRING whose escapes
    are still printable text: no control character exists there to remove, and the split
    credential matches neither redactor. Whoever parses twice gets the control character
    back and reads the credential whole, so the descent has to continue.
    """
    hidden = f"{CREDENTIAL[:8]}\x01{CREDENTIAL[8:]}"
    inner = json.dumps({"rule": f"never commit {hidden}"})
    snapshot = json.dumps({"kind": "lesson", "value_json": inner})

    out = _redact_memory_field({"before_json": snapshot})["before_json"]

    assert out != snapshot
    twice = json.loads(json.loads(out)["value_json"])
    assert CREDENTIAL not in normalize_for_scanning(twice["rule"])


def test_an_invisible_character_a_user_wrote_is_not_deleted_from_a_clean_field() -> None:
    """The normalised copy is evidence for the decision, never the value handed back.

    Most invisible characters are content: a soft hyphen inside a word, a zero-width joiner
    holding an emoji together, a bidi mark ordering mixed scripts. Returning the normalised
    copy rewrites all of them on a path that runs for every row of every listing, and none
    of that rewriting redacts anything.
    """
    for text in (
        "co\u00adoperate with the team",
        "hello\u200bworld",
        "\U0001f469\u200d\U0001f4bb",
        "\u0645\u200c\u06cc",
        "1\u200e\u0627",
    ):
        assert _redact_memory_field(text) == text


def test_a_field_that_hides_a_credential_is_still_rewritten() -> None:
    """Preserving content is not a reason to ship a credential the stored bytes hide."""
    stored = f"never commit {CREDENTIAL[:8]}\u200b{CREDENTIAL[8:]}"

    out = _redact_memory_field(stored)

    assert "[REDACTED" in out
    assert CREDENTIAL not in normalize_for_scanning(out)
    assert "\u200b" not in out


def _semantic_row(value: object) -> dict:
    raw = json.dumps(value)
    return {"kind": "fact", "id": "r1", "key": "k", "text": raw, "value_json": raw}


class _PermissiveStore:
    """Accepts every semantic value, so only the guard under test can refuse a write."""

    def validate_semantic(self, key, value, weight, source):  # noqa: D102
        return None


def test_a_record_whose_content_is_not_shown_exactly_refuses_a_whole_value_edit() -> None:
    """The editor drafts from the display form, so writing that draft back destroys content.

    A record the scrub transforms reaches the browser as a tag or a marker, and the content
    editor seeds its draft from exactly that response. Accepting the draft stores the display
    form, the original is gone, and every surface reads the same scrub so nothing shows the
    loss. The stored source surviving the scrub unchanged is what makes a draft faithful.
    """
    hidden = f"{CREDENTIAL[:8]}\x01{CREDENTIAL[8:]}"
    row = _semantic_row({"rule": f"never commit {hidden}"})

    with pytest.raises(memory_edit.MemoryEditError) as caught:
        memory_edit._after(_PermissiveStore(), row, {"type": "set", "value": {"rule": "x"}})

    assert caught.value.code == "memory_record_redacted"
    assert caught.value.status == 409


def test_a_document_the_scan_cannot_certify_also_refuses_a_whole_value_edit() -> None:
    """The marker is the widest substitution, so it is the one a write back destroys most."""
    unscannable = "[" * 200 + "]" * 200
    row = {"kind": "fact", "id": "r2", "key": "k", "text": unscannable, "value_json": unscannable}

    assert _redact_memory_field(unscannable) == json.dumps(_UNSCANNABLE_JSON)
    with pytest.raises(memory_edit.MemoryEditError) as caught:
        memory_edit._after(_PermissiveStore(), row, {"type": "set", "value": [1]})

    assert caught.value.code == "memory_record_redacted"


def test_an_episode_whose_text_is_not_shown_exactly_refuses_a_whole_value_edit() -> None:
    """An episode carries its content in ``text``, so that is the source the bar tests."""
    row = {
        "kind": "episode",
        "id": "r3",
        "key": "k",
        "text": f"note {CREDENTIAL}",
        "value_json": "",
    }

    with pytest.raises(memory_edit.MemoryEditError) as caught:
        memory_edit._after(_PermissiveStore(), row, {"type": "set", "text": "a rewritten note"})

    assert caught.value.code == "memory_record_redacted"


def test_a_record_shown_exactly_stays_editable() -> None:
    """The bar is display fidelity, not the presence of a scrubber on the path."""
    row = _semantic_row({"rule": "keep the scoped tests fast"})

    after = memory_edit._after(
        _PermissiveStore(), row, {"type": "set", "value": {"rule": "keep them faster"}}
    )

    assert after is not None
    assert "faster" in after["value_json"]


def test_a_credential_in_sibling_metadata_does_not_lock_a_record() -> None:
    """Only the source the edit replaces decides, so unrelated columns cannot lock a record."""
    row = _semantic_row({"rule": "clean"})
    row["superseded_by"] = f"see {CREDENTIAL}"

    after = memory_edit._after(
        _PermissiveStore(), row, {"type": "set", "value": {"rule": "still clean"}}
    )

    assert after is not None


def test_find_and_replace_stays_available_on_a_record_that_is_not_shown_exactly() -> None:
    """Replacement rewrites the stored value server side, so display text never lands.

    Barring every edit would leave a record holding a credential permanently unchangeable.
    Find and replace reaches the stored spelling and changes only the matched span, so it is
    the path that stays open.
    """
    row = _semantic_row({"rule": f"never commit {CREDENTIAL}"})

    after = memory_edit._after(
        _PermissiveStore(),
        row,
        {"type": "replace_text", "find": "never", "replacement": "do not", "match_case": False},
    )

    assert after is not None
    assert "do not commit" in after["value_json"]


def test_an_object_name_carrying_a_document_is_descended_like_a_value() -> None:
    """A name is written by whoever writes the value, so both halves take the same path.

    Encoding hides a control character from the scan, and a name can hold a whole document
    just as a value can. Scrubbing a name as flat text alone leaves that document
    un-walked, so the field egresses with the credential inside its own key while the
    value beside it is defended.
    """
    hidden = f"{CREDENTIAL[:8]}\x01{CREDENTIAL[8:]}"
    inner = json.dumps({"note": f"never commit {hidden}"})
    stored = json.dumps({inner: "see the name", "rule": "keep the scoped tests fast"})

    out = _redact_memory_field({"value_json": stored})["value_json"]

    assert out != stored
    decoded = json.loads(out)
    assert decoded["rule"] == "keep the scoped tests fast"
    for name in decoded:
        try:
            nested = json.loads(name)
        except ValueError:
            continue
        if isinstance(nested, dict) and "note" in nested:
            assert CREDENTIAL not in normalize_for_scanning(nested["note"])


def test_two_names_that_reduce_to_one_document_withhold_the_field() -> None:
    """A dict holds one value per name, so keeping either silently drops the other's value.

    The descent can make two distinct names identical, which is the same hazard as two
    names colliding after a flat scrub. Withholding the whole field is the answer in both
    cases, because choosing a survivor is not this scrubber's decision to make.
    """
    first = json.dumps({"note": f"token {CREDENTIAL[:8]}\x01{CREDENTIAL[8:]}"})
    second = json.dumps({"note": f"token {CREDENTIAL[:8]}\x02{CREDENTIAL[8:]}"})
    stored = json.dumps({first: 1, second: 2})

    assert first != second
    out = _redact_memory_field({"value_json": stored})["value_json"]

    assert json.loads(out) == _UNSCANNABLE_JSON


def test_a_repeated_object_name_withholds_the_document() -> None:
    """Decoding is lossy on a repeated name, so what the scan sees is not the document.

    ``json.loads`` keeps the LAST value for a name and discards the rest, so a credential in
    an earlier duplicate is gone before the scan runs. What survives then looks exactly like
    a clean document, the unchanged comparison holds, and the raw text egresses still
    carrying it. Reading all of a document is what certifies it.
    """
    escaped = f"{CREDENTIAL[:8]}\\u0001{CREDENTIAL[8:]}"
    stored = '{"note": "never commit ' + escaped + '", "note": "clean"}'

    assert json.loads(stored) == {"note": "clean"}
    out = _redact_memory_field({"value_json": stored})["value_json"]

    assert json.loads(out) == _UNSCANNABLE_JSON
    assert CREDENTIAL not in normalize_for_scanning(out)


def test_a_repeated_name_one_level_down_withholds_the_document() -> None:
    """The bar is every object in the document, not only the outermost one."""
    escaped = f"{CREDENTIAL[:8]}\\u0001{CREDENTIAL[8:]}"
    stored = '{"outer": {"k": "' + escaped + '", "k": "x"}}'

    out = _redact_memory_field({"value_json": stored})["value_json"]

    assert json.loads(out) == _UNSCANNABLE_JSON


def test_shapes_that_only_look_like_a_repeat_are_left_alone() -> None:
    """Withholding is for a name that repeats inside ONE object, and nothing wider.

    Two names sharing a value, and the same name in sibling objects, are both ordinary
    documents that lose nothing on decoding, so they keep passing through byte for byte.
    """
    for stored in (
        json.dumps({"a": "same", "b": "same"}),
        json.dumps([{"a": 1}, {"a": 2}]),
        '{ "rule" : "keep the scoped tests fast" ,  "n" : 3 }',
    ):
        assert _redact_memory_field({"value_json": stored})["value_json"] == stored


def test_a_document_that_parses_but_cannot_be_decoded_here_is_withheld() -> None:
    """Not every decode failure means the text is not JSON, and only one of them is safe.

    This interpreter caps converting a very long integer to an int, and that cap raises a
    plain ``ValueError`` rather than the parser's own malformed-input error -- on text that
    is perfectly well-formed JSON. Treating every ``ValueError`` as "not JSON" hands such a
    document to the pass-through branch, so it egresses raw with its escaped control
    character intact, and a consumer whose parser has no such cap decodes it and reads the
    credential whole.
    """
    escaped = f"{CREDENTIAL[:8]}\\u0001{CREDENTIAL[8:]}"
    stored = '{"note": "never commit ' + escaped + '", "n": ' + "9" * 5000 + "}"

    with pytest.raises(ValueError) as caught:
        json.loads(stored)
    assert not isinstance(caught.value, json.JSONDecodeError)

    out = _redact_memory_field({"value_json": stored})["value_json"]

    assert json.loads(out) == _UNSCANNABLE_JSON
    assert CREDENTIAL not in out
    assert CREDENTIAL not in normalize_for_scanning(out)


def test_text_the_parser_itself_rejects_still_passes_through() -> None:
    """Malformed text holds no payload to certify, so the text scrub is the whole of it."""
    for stored in (
        "just a note",
        '{"a": ',
        "{oops}",
        '{"a": 1,}',
        "{'a': 1}",
    ):
        assert _redact_memory_field({"value_json": stored})["value_json"] == stored


class _StoreWithProposals(_PermissiveStore):
    """Answers the one pending-proposal query the guard makes, with the rows handed in."""

    def __init__(self, *after_json: str | None) -> None:
        self.rows = [{"after_json": value} for value in after_json]
        self.asked: list[tuple[str, tuple]] = []

    @property
    def db(self):  # noqa: D102
        outer = self

        class _Db:
            def execute(self, sql, params):
                outer.asked.append((sql, params))

                class _Rows:
                    def fetchall(self):
                        return outer.rows

                return _Rows()

        return _Db()


def _proposal(rule: str) -> str:
    return json.dumps({"kind": "fact", "value_json": json.dumps({"rule": rule})})


def _record_with_metadata(value: object) -> dict:
    row = _semantic_row(value)
    row["metadata"] = {"record_id": "rec-1", "revision": 7}
    return row


def test_a_pending_proposal_shown_inexactly_refuses_a_whole_value_edit() -> None:
    """Reviewing a proposal loads it into the editor, so a proposal is a second draft source.

    That offer is built from the history response, which is scrubbed like every other, so a
    proposal the scrub transformed is a view. The record's own value says nothing about it:
    a clean current value passes the first bar and lets the view be written as the record.
    """
    hidden = f"{CREDENTIAL[:8]}\x01{CREDENTIAL[8:]}"
    store = _StoreWithProposals(_proposal(f"token {hidden}"))

    with pytest.raises(memory_edit.MemoryEditError) as caught:
        memory_edit._after(
            store, _record_with_metadata({"rule": "clean"}), {"type": "set", "value": {"rule": "x"}}
        )

    assert caught.value.code == "memory_proposal_redacted"
    assert caught.value.status == 409
    sql, params = store.asked[-1]
    assert params == ("rec-1", 7)
    assert "status = 'conflict'" in sql


def test_a_clean_pending_proposal_leaves_the_record_editable() -> None:
    """The bar is display fidelity of the proposal, not the existence of one."""
    store = _StoreWithProposals(_proposal("a clean proposal"), None)

    after = memory_edit._after(
        store,
        _record_with_metadata({"rule": "clean"}),
        {"type": "set", "value": {"rule": "accepted"}},
    )

    assert after is not None
    assert "accepted" in after["value_json"]


def test_one_hidden_proposal_among_clean_ones_still_refuses() -> None:
    """A record carries several proposals, and any one of them being a view is enough."""
    store = _StoreWithProposals(
        _proposal("first"), _proposal(f"token {CREDENTIAL}"), _proposal("third")
    )

    with pytest.raises(memory_edit.MemoryEditError) as caught:
        memory_edit._after(
            store, _record_with_metadata({"rule": "clean"}), {"type": "set", "value": {"rule": "x"}}
        )

    assert caught.value.code == "memory_proposal_redacted"


def test_find_and_replace_stays_available_when_a_proposal_is_hidden() -> None:
    """Replacement never writes display text, so a hidden proposal does not bar it."""
    store = _StoreWithProposals(_proposal(f"token {CREDENTIAL}"))

    after = memory_edit._after(
        store,
        _record_with_metadata({"rule": "keep this clean"}),
        {"type": "replace_text", "find": "clean", "replacement": "tidy", "match_case": False},
    )

    assert after is not None
    assert "keep this tidy" in after["value_json"]


def test_a_document_the_scrub_itself_breaks_is_withheld() -> None:
    """The parser must judge the STORED bytes, because the scrub can break the document.

    A credential-assignment pattern spans a name, its colon and its value, so splicing one
    out of a JSON document leaves text that does not parse. Judging JSON-ness on that text
    calls a real document prose and hands it back unscanned, and the escaped control-split
    credential beside it never reaches the descent.
    """
    escaped = f"{CREDENTIAL[:8]}\\u0001{CREDENTIAL[8:]}"
    stored = '{"aws_secret_access_key": "x", "d": "never commit ' + escaped + '"}'

    assert json.loads(stored)["d"]
    spliced = _shared._scrub_text(stored)
    with pytest.raises(json.JSONDecodeError):
        json.loads(spliced)

    out = _redact_memory_field({"value_json": stored})["value_json"]

    assert json.loads(out) == _UNSCANNABLE_JSON
    assert escaped not in out
    assert CREDENTIAL not in normalize_for_scanning(out)


def test_text_that_was_never_a_document_still_passes_through_after_scrubbing() -> None:
    """Withholding is for a document the scrub broke, not for prose it rewrote."""
    prose = f"aws_secret_access_key = {CREDENTIAL}"

    out = _redact_memory_field(prose)

    assert "[REDACTED" in out
    assert out != json.dumps(_UNSCANNABLE_JSON)
    assert _redact_memory_field({"value_json": '{"a": '})["value_json"] == '{"a": '


def test_keeping_the_current_value_changes_no_content_and_is_not_refused() -> None:
    """Closing a proposal by keeping the current value is a no-op, not an edit.

    The browser spells it by submitting the record body it was shown, which on a record the
    scrub rewrites is the display form. Refusing that leaves the pending proposal impossible
    to close, and accepting it as a value would store the display form. Neither is right:
    the stored value is left exactly as it is, so the resolution bookkeeping can close.
    """
    hidden = f"{CREDENTIAL[:8]}\x01{CREDENTIAL[8:]}"
    row = _semantic_row({"rule": f"token {hidden}"})
    shown = json.loads(_redact_memory_field(row["value_json"]))

    after = memory_edit._after(_PermissiveStore(), row, {"type": "set", "value": shown})

    assert after is not None
    assert after["value_json"] == row["value_json"]
    assert "[REDACTED" not in after["value_json"]


def test_a_value_that_only_resembles_the_shown_form_is_still_refused() -> None:
    """The no-op is exact equality with the display form, and nothing looser."""
    hidden = f"{CREDENTIAL[:8]}\x01{CREDENTIAL[8:]}"
    row = _semantic_row({"rule": f"token {hidden}"})
    shown = json.loads(_redact_memory_field(row["value_json"]))
    shown["rule"] = f"{shown['rule']} plus"

    with pytest.raises(memory_edit.MemoryEditError) as caught:
        memory_edit._after(_PermissiveStore(), row, {"type": "set", "value": shown})

    assert caught.value.code == "memory_record_redacted"


def test_keeping_the_current_value_passes_the_proposal_bar_too() -> None:
    """A hidden proposal must not make its own record permanently unresolvable either."""
    hidden = f"{CREDENTIAL[:8]}\x01{CREDENTIAL[8:]}"
    row = _record_with_metadata({"rule": f"token {hidden}"})
    shown = json.loads(_redact_memory_field(row["value_json"]))
    store = _StoreWithProposals(_proposal(f"token {CREDENTIAL}"))

    after = memory_edit._after(store, row, {"type": "set", "value": shown})

    assert after is not None
    assert after["value_json"] == row["value_json"]


def test_a_nested_document_the_leaf_scrub_breaks_is_withheld_too() -> None:
    """The stored-bytes test belongs at every level, not only the outermost field.

    A revision snapshot is a document carried inside a document. The outer text can survive
    the scrub untouched while the inner one, once decoded and scanned, is spliced apart by a
    credential-assignment match. Judging the inner level on its scrubbed form hands the leaf
    back with its own payload unwalked.
    """
    escaped = f"{CREDENTIAL[:8]}\\u0001{CREDENTIAL[8:]}"
    inner = '{"aws_secret_access_key": "x", "d": "never commit ' + escaped + '"}'
    stored = json.dumps({"snapshot": inner})

    assert _shared._scrub_text(stored) == stored
    with pytest.raises(json.JSONDecodeError):
        json.loads(_shared._scrub_text(inner))

    out = _redact_memory_field({"value_json": stored})["value_json"]

    leaf = json.loads(out)["snapshot"]
    assert json.loads(leaf) == _UNSCANNABLE_JSON
    assert escaped not in out
    assert CREDENTIAL not in normalize_for_scanning(out)
