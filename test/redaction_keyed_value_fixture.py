"""Generator for the keyed-value fixture shared by the backend, the stream, the hard
URL floor, the packaging scan's vendored scanner and the chat mirror.

The value grammar of a key-anchored credential pair lives in ONE scanner
(``kiro_crew.security.scan_keyed_value``) and in two ports of it that cannot
import the canonical module: the packaging scan's standalone copy and the chat
mirror ``website/src/utils/sanitize.ts``. A port that drifts on a quote or escape
shape is a leak one surface has and another does not, and a regex port drifts
on every new quote or escape shape. So the rows here are generated from the canonical redactor over every
shape the scanner knows, written to ``test/fixtures/redaction_keyed_values.json`` in
the compact encoding of :func:`encode_rows` (decoded by both suites),
and consumed by ``test_redaction_keyed_value_fixture.py`` (pytest: the committed
file equals a fresh generation; every row holds for the redactor, the stream at
six chunk sizes, the hard floor and the vendored scanner) and by
``website/src/test/sanitizeCredentials.fixture.test.ts`` (vitest: the mirror's
output equals ``expected_mirror``). Regenerate with
``PYTHONPATH=src python test/redaction_keyed_value_fixture.py`` (``test/`` is not a
package -- the sibling-module import every test here uses -- and the stdlib ships
a ``test`` package of its own on the CI runners, which a ``-m test.<module>`` spelling
resolved to instead).

Each row: ``text`` (the input), ``expected`` (``redact_credentials(text)[0]``),
``warnings`` (count), ``expected_mirror`` (the chat mirror's output: the backend
tag spelled ``[REDACTED]``, backend tags in the INPUT left as they are), ``live``
(whether a live labelled value stands in the text, as the hard floor and the
packaging scan must read it), ``shape`` and ``key``.
"""

from __future__ import annotations

import json
from pathlib import Path

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "redaction_keyed_values.json"

KEYS = (
    "aws_secret_access_key",
    "SecretAccessKey",
    "aws_session_token",
    "SessionToken",
    "aws_access_key_id",
    "AccessKeyId",
)

#: Values whose only credential shape is the label: short, lowercase-heavy, so no
#: entropy or encoded-chunk pass fires and every row isolates the keyed rule.
VALUE = "test-value-not-a-credential-0123"
SECOND = "second-value-not-a-credential-4567"
TAG = "[REDACTED: credential]"
ENC = "[REDACTED: encoded credential]"


def _shapes(key: str) -> list[tuple[str, str]]:
    v, w = VALUE, SECOND
    # The key that follows an empty-valued line in the `empty-then-key-*` rows:
    # another AWS key, never the row's own.
    other = "aws_session_token" if key != "aws_session_token" else "aws_secret_access_key"
    rows: list[tuple[str, str]] = [
        ("eq", f"{key}={v}\nnext: line\n"),
        ("colon", f"{key}: {v}\nnext: line\n"),
        ("json", json.dumps({key: v, "r": "x"}) + "\n"),
        ("json-compact", "{" + json.dumps(key) + ":" + json.dumps(v) + ',"r":"x"}'),
        ("dq", f'{key}="{v}"\nnext: line\n'),
        ("sq", f"{key}='{v}'\nnext: line\n"),
        ("dq-tail", f'{key}="{v} key, rotated"\nnext: line\n'),
        ("dq-doubled", f'{key}="{v}""{w}"\nnext: line\n'),
        ("sq-doubled", f"{key}='{v}''{w}'\nnext: line\n"),
        ("dq-escaped-interior", f'{key}="{v}\\"{w}" tail\n'),
        ("unterminated", f'{key}="{v} rest of the line\nnext: line\n'),
        ("unterminated-eof", f'"{key}": "{v} tail'),
        ("embedded", json.dumps({"text": f'{key}="{v}"'}) + "\n"),
        ("embedded-json", json.dumps({"text": f'"{key}": "{v}"', "r": "x"}) + "\n"),
        ("embedded-unterminated", json.dumps({"text": f'{key}="{v} tail'}) + "\n"),
        ("escaped-slash", f"{key}=\\/{v[:8]}\\/{v[8:]}\nnext: line\n"),
        ("escaped-slash-json", '{"' + key + '": "\\/' + v[:8] + "\\/" + v[8:] + '"}\n'),
        ("escaped-backslash", f"{key}={v[:8]}\\\\{v[8:]} tail\n"),
        ("unicode-escape", f'{key}="{v[:8]}\\u002f{v[8:]}" tail\n'),
        ("newline-head", '{"' + key + '": "\\n' + v + '"}\n'),
        ("tab-heads-9", f'{key}="' + "\\t" * 9 + v + '"\n'),
        ("tab-heads-unquoted", f"{key}=" + "\\n" * 3 + v + "\nnext: line\n"),
        ("embedded-newline-head", json.dumps({"text": f'{key}="\\n{v}"'}) + "\n"),
        # Raw whitespace after the opening quote is part of the quoted scalar.
        ("dq-leading-space", f'{key}=" {v}"\nnext: line\n'),
        ("sq-leading-space", f"{key}=' {v}'\nnext: line\n"),
        ("dq-leading-tab", f'{key}="\t{v}"\nnext: line\n'),
        ("dq-leading-several", f'{key}="  \t {v}"\nnext: line\n'),
        ("json-leading-space", json.dumps({key: f"  {v}", "r": "x"}) + "\n"),
        ("embedded-leading-space", json.dumps({"text": f'{key}=" {v}"'}) + "\n"),
        # A key repeated as its own value: every anchor after the first begins
        # inside the first value's claim and is covered by it (one tag).
        ("nested-bare", f"{key}={key}={v}\nnext: line\n"),
        ("nested-quoted", f'{key}="{key}={v}"\nnext: line\n'),
        ("nested-twice", f"{key}={key}={key}={v} tail\n"),
        # A bare-quoted value inside a literal of the OTHER quote kind: the
        # enclosing close (other kind + structural byte, whitespace or line end)
        # ends the inner line; the sibling field and the document survive.
        ("enclosing-json-sq-value", '{"text":"' + key + "='" + v + '","keep":1}\n'),
        ("enclosing-json-array", '["' + key + "='" + v + '"]\n'),
        ("enclosing-yaml-dq-line-end", f'text: "{key}=\'{v}"\nkeep: 1\n'),
        ("enclosing-yaml-sq-inner-dq", f"text: '{key}=\"{v}', keep: 1\n"),
        ("interior-apostrophe", f'{key}="it\'s-{v}"\nnext: line\n'),
        # An apostrophe before whitespace or punctuation inside a `"`-opened
        # value is a byte of the value while its own close is still on the line
        # (JSON has no `'` strings); a `"` before a structural byte inside a
        # `'`-opened value is the close of an enclosing `"` string (JSON never
        # escapes a `'`). The `"` side keeps a sibling field and a parsing
        # document, the `'` side keeps the r16c rows above.
        ("dq-apostrophe-space", f'{key}=" note\' {v}"\nnext: line\n'),
        ("dq-apostrophe-comma", f'{key}="{v}\', b"\nnext: line\n'),
        ("dq-apostrophe-brace", f'{key}="{v}\'}} b"\nnext: line\n'),
        ("dq-two-apostrophes", f"{key}=\"don' t won' t {v}\"\nnext: line\n"),
        ("dq-quoted-word", f"{key}=\"rock 'n' roll {v}\"\nnext: line\n"),
        ("json-apostrophe-space", json.dumps({key: f" note' {v}", "keep": 1}) + "\n"),
        ("json-apostrophe-sibling", '{"' + key + '": "' + v + '\' b", "note": "don\'t"}\n'),
        ("embedded-apostrophe-space", json.dumps({"text": f'{key}=" note\' {v}"'}) + "\n"),
        ("sq-doubled-apostrophe-space", f"{key}='note'' {v}'\nnext: line\n"),
        ("sq-interior-dq-value-byte", f"{key}='say \"hi\"-{v}'\nnext: line\n"),
        # A `'` literal enclosing a `"` pair: closed inside it, the pair ends at
        # its own close; left open, at the `'` before a structural byte, so the
        # sibling key survives (YAML and the shell); at the line's end, there.
        ("enclosing-yaml-sq-closed-inner-dq", f"cmd: 'export {key}=\"{v}\" && run'\n"),
        ("enclosing-yaml-sq-inner-dq-colon", f"text: '{key}=\"{v}': 1\n"),
        ("enclosing-shell-sq-inner-dq", f"echo '{key}=\"{v}' ; ls\n"),
        ("enclosing-yaml-sq-inner-dq-line-end", f"text: '{key}=\"{v}'\nkeep: 1\n"),
        # A key line with an EMPTY value followed by another key line: the
        # anchor's trailing whitespace crosses the line break and reads the next
        # key's name as its value, and the next key's anchor begins inside that
        # claim; its value past the claim is still claimed (the mirror skipped
        # the anchor and showed the second value).
        ("empty-then-key-eq", f"{key} = \n{other} = {v}\nnext: line\n"),
        ("empty-then-key-colon", f"{key}:\n{other}: {v}\nnext: line\n"),
        ("empty-then-key-quoted", f'{key}=\n{other}="{v} {w}"\nnext: line\n'),
        ("empty-then-key-glued", f"{key}=\n{other}={v}\nnext: line\n"),
        # The redactor's own output and its neighbours: fixed points and lookalikes.
        ("tag-filled", f"{key}={TAG}\nnext: line\n"),
        ("tag-filled-quoted", f'{key}="{TAG}" # note\n'),
        ("tag-run", f"{key}={TAG}{ENC}\n"),
        ("tag-filled-embedded", json.dumps({"text": f'{key}="{TAG}"'}) + "\n"),
        ("tag-glued", f"{key}={TAG}{v}\n"),
        ("tag-run-glued", f"{key}={TAG}{ENC}{v}\n"),
        ("tag-heading-quoted", f'{key}="{TAG} {v}"\n'),
        ("tag-doubled-close", f"{key}='{TAG}''{v}'\n"),
        ("tag-other-quote-close", f"{key}=\"{TAG}'{v}'\n"),
        ("tag-escaped-glued", f"{key}={TAG}\\/{v}\n"),
        ("tag-lowercase", f"{key}={TAG.lower()}\n"),
        ("tag-with-head", f'{key}="\\n{TAG}"\n'),
        ("empty-value", f"{key}=\nnext: line\n"),
        ("empty-quoted", f'{key}=""\nnext: line\n'),
        # The key INSIDE a string literal, which the look-back along the line
        # finds: an empty assignment followed by the literal's own close is left
        # alone (the close is not an opener), a value runs to the literal's end
        # and no further, a shell concatenation's bare run after the close is a
        # value, and an apostrophe inside a word before the key is prose, not an
        # opener (the value after it reads by the outside rules).
        ("empty-in-json", '{"template":"' + key + '=","keep":1}\n'),
        ("empty-in-json-spaced", '{"template": "export ' + key + '=", "keep": 1}\n'),
        ("empty-in-json-colon", '{"template":"' + key + ': ","keep":1}\n'),
        ("empty-in-yaml-sq", f"text: '{key}=', keep: 1\n"),
        ("empty-in-yaml-dq", f'text: "{key}="\nkeep: 1\n'),
        ("empty-in-json-array", '["' + key + '=","keep"]\n'),
        ("bare-in-json", '{"template":"export ' + key + "=" + v + '","keep":1}\n'),
        ("shell-dq-concat", f'echo "export {key}="{v}"" && run\n'),
        ("shell-sq-closed", f"run 'export {key}={v}' && next\n"),
        ("prose-apostrophe-before-key", f"don't put {key}='{v}' here\n"),
        ("prose-apostrophe-dq-value", f"don't put {key}=\" note' {v}\" here\n"),
        ("quoted-word-before-key", f"say 'hi' then {key}=\"{v}\" bye\n"),
        ("json-key-then-apostrophe-value", json.dumps({key: f"{v}' tail", "keep": 1}) + "\n"),
        ("escaped-json-inside-json", json.dumps({"text": json.dumps({key: v, "keep": 1})}) + "\n"),
        # A value whose LAST inner token is a backslash, in both encodings. The
        # escapes of a value pair up left to right at the value's own depth: in
        # the escaped encoding `\\` is one inner backslash, and the bare quote
        # after it is the enclosing literal's close, never a quote that inner
        # backslash escapes (the inner encoding writes its quotes `\"`). Reading
        # it as escaped consumed the close and the claim ran into the next
        # field, so the document stopped parsing. The `x` row is the finding's
        # text verbatim; the others carry the synthetic secret so the invariant
        # below is load-bearing.
        ("embedded-json-backslash-tail", '{"text": "\\"' + key + '\\": \\"x\\\\", "keep": 1}\n'),
        (
            "embedded-json-value-backslash-tail",
            '{"text": "\\"' + key + '\\": \\"' + v + '\\\\", "keep": 1}\n',
        ),
        (
            "embedded-json-two-backslash-tail",
            '{"text": "\\"' + key + '\\": \\"' + v + '\\\\\\\\", "keep": 1}\n',
        ),
        (
            "embedded-json-three-backslash-tail",
            '{"text": "\\"' + key + '\\": \\"' + v + '\\\\\\\\\\\\", "keep": 1}\n',
        ),
        (
            "embedded-json-escaped-quote-then-backslash-tail",
            '{"text": "\\"' + key + '\\": \\"' + v + '\\\\\\"' + w + '\\\\", "keep": 1}\n',
        ),
        (
            "embedded-json-backslash-then-escaped-quote-close",
            '{"text": "\\"' + key + '\\": \\"' + v + '\\\\\\\\\\"", "keep": 1}\n',
        ),
        ("embedded-eq-backslash-tail", '{"text": "' + key + '=\\"' + v + '\\\\", "keep": 1}\n'),
        ("json-value-backslash-tail", json.dumps({key: v + "\\", "r": "x"}) + "\n"),
        ("json-value-two-backslash-tail", json.dumps({key: v + "\\\\", "r": "x"}) + "\n"),
        ("json-value-quote-backslash-tail", json.dumps({key: v + '"\\', "r": "x"}) + "\n"),
        ("dq-backslash-tail", f'{key}="{v}\\\\" tail\nnext: line\n'),
        ("dq-escaped-quote-tail", f'{key}="{v}\\"" tail\nnext: line\n'),
        ("dq-mixed-escape-tail", f'{key}="{v}\\\\\\"{w}\\\\" tail\nnext: line\n'),
        (
            "enclosing-json-sq-value-backslash-tail",
            '{"text":"' + key + "='" + v + '\\\\\'","keep":1}\n',
        ),
        # Rows that reach the scanner rules no other row did (the drift pin,
        # `test_every_scanner_rule_is_reached_by_a_fixture_row`, lists any rule
        # without a row): the key inside an INNER escaped literal whose `\"`
        # closes the value; an escaped tab and an escaped slash inside an
        # escaped-encoding value; a lone backslash ending the text; a doubled
        # enclosing-kind quote inside a `"` value, bare and after an inner
        # backslash; a backslash before a raw line break.
        ("key-inside-inner-literal", '{"t":"{\\"cmd\\":\\"' + key + "=" + v + '\\"}","keep":1}\n'),
        ("embedded-tab-interior", json.dumps({"text": f'{key}="{v[:8]}\t{v[8:]}"'}) + "\n"),
        ("embedded-escaped-slash-value", '{"text": "' + key + '=\\"\\/' + v + '\\"", "keep": 1}\n'),
        ("unquoted-backslash-eof", f"{key}={v}\\"),
        (
            "enclosing-yaml-sq-inner-dq-doubled-apostrophe",
            f"text: '{key}=\"it''s {v}\"', keep: 1\n",
        ),
        ("dq-backslash-before-line-break", f'{key}="{v}\\\nnext: line\n'),
        (
            "enclosing-yaml-sq-escaped-dq-inner-backslash-doubled-apostrophe",
            f"text: '{key}=\\\"{v}\\\\''s\\\"', keep: 1\n",
        ),
        # A BARE-quoted value inside an ESCAPED inner literal: the value's bytes are
        # in the inner literal's encoding whatever quote opens the value, so the
        # inner backslash before the enclosing close is still one pair. The first
        # is the round-19 finding verbatim (`x`); the others carry the secret.
        (
            "embedded-sq-in-inner-literal-backslash-tail",
            '{"text":"\\"' + key + '=\'x\\\\","keep":1}\n',
        ),
        (
            "embedded-sq-in-inner-literal-value-backslash-tail",
            '{"text":"\\"' + key + "='" + v + '\\\\","keep":1}\n',
        ),
        (
            "embedded-sq-in-inner-literal-two-backslash-tail",
            '{"text":"\\"' + key + "='" + v + '\\\\\\\\","keep":1}\n',
        ),
        (
            "embedded-sq-in-inner-literal-three-backslash-tail",
            '{"text":"\\"' + key + "='" + v + '\\\\\\\\\\\\","keep":1}\n',
        ),
        (
            "embedded-sq-in-inner-literal-escaped-quote-tail",
            '{"text":"\\"' + key + "='" + v + '\\\\\\"","keep":1}\n',
        ),
        (
            "embedded-sq-in-inner-literal-closed-backslash-interior",
            '{"text":"\\"' + key + "='" + v + '\\\\\' more\\"","keep":1}\n',
        ),
        # Inside an enclosing `"` literal a backslash before a RAW line break is
        # no pair of any encoding: the line ends, the backslash is a bare one.
        (
            "enclosing-dq-unquoted-backslash-before-line-break",
            f'x="{key}={v}\\\nnext: line\n',
        ),
        (
            "enclosing-dq-sq-value-backslash-before-line-break",
            f"x=\"{key}='{v}\\\nnext: line\n",
        ),
        # The three shapes the document sweep surfaced. A `'`-quoted value inside a
        # YAML single-quoted scalar: the scalar spells each apostrophe `''`, so the
        # doubled pair where the value opens is the value's own quote and the
        # doubled pair after the secret its close, never the scalar's end.
        ("enclosing-yaml-sq-sq-value", f"text: '{key}=''{v}'' more', keep: 1\n"),
        # The value's own apostrophe inside such a value is doubled twice.
        (
            "enclosing-yaml-sq-sq-value-interior-apostrophe",
            f"text: '{key}=''{v}''''s'' more', keep: 1\n",
        ),
        # A structural byte as the value's FIRST byte, followed by value bytes: the
        # value's, claimed whole (left unclaimed it stood in plaintext). The hard
        # URL floor reads the same value (pinned in the span tests; a URL row would
        # be dropped whole by the stream's suspicious-URL rule, so none is here).
        ("bracket-first-byte", f"{key}=]{v}\n"),
        ("brace-first-byte", f"{key}=}}{v}\n"),
        ("comma-first-byte", f"{key}=,{v}\n"),
        # A RUN of structural bytes, or of a structural byte and a backslash,
        # where the value starts: the base's value class admits `]` and a
        # backslash, so the run and the secret after it are its value; the run
        # is read whole and the byte after it decides, where a reading of the
        # first byte alone took `]` after `]` as nothing value-like and left the
        # secret standing. Bare lines, and the same run inside a JSON string.
        ("bracket-run-head", f"{key}=]]{v}\n"),
        ("comma-bracket-run-head", f"{key}=,]{v}\n"),
        ("bracket-backslash-run-head", f"{key}=]\\{v}\n"),
        ("brace-comma-brace-run-head", f"{key}=}},}}{v} more\n"),
        ("json-string-bracket-run-head", '{"text": "' + key + "=]]" + v + ' more", "keep": 1}\n'),
        (
            "json-string-run-with-inner-backslash",
            '{"text": "' + key + "=]\\\\" + v + '", "keep": 1}\n',
        ),
        # The same run before the enclosing close, or before whitespace: no value.
        ("json-string-bracket-run-no-value", '{"text": "' + key + '=]]", "keep": 1}\n'),
        ("comma-bracket-run-then-space", f"{key}=,] next word\n"),
        # A bare backslash after the run reads the byte after it, as inside a value:
        # a value byte, and the pair is the value's; a quote, and no value opens.
        ("bracket-then-escaped-slash", f"{key}=]\\/{v}\n"),
        ("bracket-then-backslash-quote-no-value", f'{key}=]\\" next word\n'),
        # A serializer's escaped tab after the separator, inside a JSON string: the
        # enclosing encoding's whitespace, consumed as the value's head.
        ("json-string-escaped-tab-head", '{"text": "' + key + "=\\t" + v + '", "keep": 1}\n'),
        # A serializer's escaped line break after the separator, inside a JSON
        # string (`json.dumps` of a config dump whose value starts on the next
        # line): the enclosing encoding's line break, whitespace to the anchor as
        # a raw one is, consumed as the value's head; read as the value's END it
        # left the whole value standing behind it, in plaintext. The `\r`
        # spelling too.
        (
            "json-string-escaped-break-head",
            '{"text": "' + key + "=\\n" + v + ' more", "keep": 1}\n',
        ),
        ("json-string-escaped-cr-head", '{"text": "' + key + ": \\r" + v + '", "keep": 1}\n'),
        # A tag followed by the LITERAL two-byte spelling `\n` and a secret, in a
        # bare line as a percent-decoded URL path carries it: the pair is bytes of
        # the value (the base's grammar), so the value is a tag with glued bytes,
        # redacted whole; read as a line break, the value stopped at the tag, the
        # tag stood exempt and the secret behind it passed every floor.
        ("tag-literal-backslash-n-glued", f"{key}={TAG}\\n{v}\n"),
        ("bare-literal-backslash-n-inside", f"{key}={v[:8]}\\n{v[8:]} more\n"),
        # An empty assignment before the enclosing close with `]` after it: a
        # structural byte, no value.
        ("json-list-empty-assignment-before-bracket", '{"keep": [1, "' + key + '="]}\n'),
        # A value on the line AFTER its separator, as a pretty printer or a YAML
        # author writes it: the separator's whitespace crosses the line break, as
        # the base's does, so the value is redacted.
        ("yaml-next-line-value", f"{key}:\n  {v}\n"),
        ("json-next-line-value", '{"' + key + '":\n    "' + v + '"\n}\n'),
        # The residual that rule buys: an EMPTY assignment at a line's end takes
        # the next line's first word as its value (`keep:` here), the base's
        # fail-closed over-redaction, disclosed and kept for parity with it.
        ("yaml-block-empty-assignment-then-key", f"text: |\n  {key}=\nkeep: 1\n"),
        # An UNQUOTED value in the escaped encoding with no enclosing literal in
        # view: the escaped quote after it is the out-of-view literal's close.
        (
            "embedded-unquoted-then-escaped-quote",
            '{\\"' + key + '\\": ' + v + '\\", \\"keep\\": 1}\n',
        ),
        (
            "embedded-unquoted-in-inner-literal-backslash-tail",
            '{"text":"\\"' + key + "=" + v + '\\\\","keep":1}\n',
        ),
    ]
    rows.extend(wrapped for shape, text in list(rows) for wrapped in _wrapped(shape, text))
    return rows


#: Shapes also pinned WRAPPED as the string value of a JSON document, compact and
#: indented: the pair in the escaped encoding inside a literal the look-back
#: finds, with a sibling field the redaction must keep and a document that must
#: still parse (`test_every_json_document_in_the_fixture_stays_a_document`). The
#: `embedded-*` shapes are already one escaping deep; wrapped they would be two,
#: an encoding the scanner does not read (`\\\\\\"` is not an opener to it).
_WRAPPED_SHAPES = frozenset(
    {
        "eq",
        "colon",
        "json",
        "dq",
        "sq",
        "dq-tail",
        "unterminated",
        "interior-apostrophe",
        "empty-value",
        "empty-quoted",
        "tag-filled",
        "tag-glued",
        "empty-in-json",
        "prose-apostrophe-before-key",
        "dq-backslash-tail",
        "dq-escaped-quote-tail",
        "dq-mixed-escape-tail",
        "json-value-backslash-tail",
        "json-value-quote-backslash-tail",
    }
)


def _wrapped(shape: str, text: str) -> list[tuple[str, str]]:
    if shape not in _WRAPPED_SHAPES:
        return []
    compact = json.dumps({"template": text, "keep": 1}, separators=(",", ":"))
    indented = json.dumps({"template": text, "keep": 1}, indent=2) + "\n"
    return [(f"wrapped-json:{shape}", compact), (f"wrapped-json-indented:{shape}", indented)]


def _has_tag(text: str) -> bool:
    return TAG in text or ENC in text


def _mirror_expected(text: str) -> str:
    """What the chat mirror (``sanitize.ts``) writes: the same anchor-and-scanner
    walk (the look-back for the enclosing literal advanced anchor to anchor), its
    own ``[REDACTED]`` for every live claim with the close written when the quote
    never closed, and a tag run filling its value left alone. Coverage is the
    backend's: a value covered whole by an earlier claim is skipped, a value
    straddling the claim's end is claimed from there -- never the anchor's own
    start, which skipped the key line after an empty-valued one."""
    from kiro_crew.security.redaction import (
        _CREDENTIAL_PATTERNS,
        _KeyedValueScans,
        _value_is_credential_tag,
    )

    out: list[str] = []
    cursor = 0
    scans = _KeyedValueScans()
    for match in _CREDENTIAL_PATTERNS.finditer(text):
        value = scans.value_of(text, match)
        if value is None or value.end <= value.start or value.end <= cursor:
            continue
        start, closes = value.start, value.closes
        if start < cursor:
            start, closes = cursor, True
        if _value_is_credential_tag(text, start, value.end) and closes:
            continue
        out.append(text[cursor:start])
        out.append("[REDACTED]" + ("" if closes else value.opener))
        cursor = value.end
    out.append(text[cursor:])
    return "".join(out)


def build_rows() -> list[dict[str, object]]:
    from kiro_crew.security import credential_matches, redact_credentials

    rows: list[dict[str, object]] = []
    for key in KEYS:
        for shape, text in _shapes(key):
            expected, warnings = redact_credentials(text)
            live = next(credential_matches(text), None) is not None
            # Invariants every row must hold, or the fixture is not worth pinning.
            assert VALUE not in expected or shape in ("empty-value", "empty-quoted"), (
                shape,
                expected,
            )
            assert SECOND not in expected, (shape, expected)
            assert redact_credentials(expected) == (expected, []), (shape, expected)
            mirror = _mirror_expected(text)
            assert VALUE not in mirror or shape in ("empty-value", "empty-quoted"), (shape, mirror)
            # The mirror's answer is the backend's with the backend's tag spelled as
            # the mirror's, on every row where no backend tag stood in the input.
            if not _has_tag(text):
                assert mirror == expected.replace(TAG, "[REDACTED]"), (shape, mirror, expected)
            rows.append(
                {
                    "key": key,
                    "shape": shape,
                    "text": text,
                    "expected": expected,
                    "warnings": len(warnings),
                    "expected_mirror": mirror,
                    "live": live,
                }
            )
    return rows


#: The file on disk is the row list in a COMPACT encoding: the keys once, then one
#: entry per shape, holding the row's strings with the key written as the slot
#: ``{key}`` (the same shape reads the same for every key, the key's own bytes
#: aside) or, where the rows of a shape differ by more than the key (a second key
#: written in the text), the rows themselves; the per-record field names are one
#: letter each. The review lanes read the whole change as one diff and refuse one
#: over a megabyte, and the pretty-printed list stood for two fifths of it. Every
#: (key, shape) row decodes byte for byte: :func:`decode_rows` is the reader of
#: both test suites, and the writer refuses a document that does not round-trip.
_KEY_SLOT = "{key}"
_SHORT = {
    "key": "k",
    "shape": "s",
    "text": "t",
    "expected": "e",
    "warnings": "w",
    "expected_mirror": "m",
    "live": "l",
}
_LONG = {short: long for long, short in _SHORT.items()}
_SLOTTED = ("text", "expected", "expected_mirror")


def _as_template(row: dict[str, object], key: str) -> dict[str, object]:
    entry: dict[str, object] = {"s": row["shape"]}
    for field in _SLOTTED:
        value = str(row[field])
        if _KEY_SLOT in value:
            raise ValueError(f"{row['shape']}: the text carries the key slot literally")
        entry[_SHORT[field]] = value.replace(key, _KEY_SLOT)
    entry["w"] = row["warnings"]
    entry["l"] = row["live"]
    return entry


def encode_rows(rows: list[dict[str, object]]) -> dict[str, object]:
    keys = list(dict.fromkeys(str(row["key"]) for row in rows))
    shapes = list(dict.fromkeys(str(row["shape"]) for row in rows))
    by_key_shape = {(row["key"], row["shape"]): row for row in rows}
    entries: list[dict[str, object]] = []
    for shape in shapes:
        group = [by_key_shape[(key, shape)] for key in keys]
        templates = [_as_template(row, key) for row, key in zip(group, keys, strict=True)]
        if all(template == templates[0] for template in templates):
            entries.append(templates[0])
        else:
            entries.append(
                {"s": shape, "rows": [{_SHORT[k]: v for k, v in row.items()} for row in group]}
            )
    return {"keys": keys, "shapes": entries}


def decode_rows(doc: dict[str, object]) -> list[dict[str, object]]:
    """The row list the compact document encodes, in the generator's order."""
    keys = list(doc["keys"])  # type: ignore[call-overload]
    rows: list[dict[str, object]] = []
    for index, key in enumerate(keys):
        for entry in doc["shapes"]:  # type: ignore[attr-defined]
            if "rows" in entry:
                rows.append({_LONG[k]: v for k, v in entry["rows"][index].items()})
                continue

            def filled(field: str, entry: dict[str, object] = entry, key: str = key) -> str:
                return str(entry[_SHORT[field]]).replace(_KEY_SLOT, key)

            rows.append(
                {
                    "key": key,
                    "shape": entry["s"],
                    "text": filled("text"),
                    "expected": filled("expected"),
                    "warnings": entry["w"],
                    "expected_mirror": filled("expected_mirror"),
                    "live": entry["l"],
                }
            )
    return rows


def render() -> str:
    rows = build_rows()
    doc = encode_rows(rows)
    if decode_rows(json.loads(json.dumps(doc, ensure_ascii=False))) != rows:
        raise ValueError("the compact fixture does not decode to the rows it encodes")
    compact = {"ensure_ascii": False, "separators": (",", ":")}
    entries = [json.dumps(entry, **compact) for entry in doc["shapes"]]  # type: ignore[attr-defined]
    head = '{"keys":' + json.dumps(doc["keys"], **compact)
    return head + ',\n"shapes":[\n' + ",\n".join(entries) + "\n]}\n"


def load_rows() -> list[dict[str, object]]:
    """The committed fixture's rows, as both test suites read them."""
    return decode_rows(json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))


if __name__ == "__main__":
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(render(), encoding="utf-8")
    print(f"wrote {FIXTURE_PATH} ({len(build_rows())} rows)")
