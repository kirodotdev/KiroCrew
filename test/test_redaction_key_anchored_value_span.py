"""Key-anchored credential branches redact the value, never the key that names it.

Four branches of ``_CREDENTIAL_PATTERNS`` begin at the KEY naming a secret --
``aws_secret_access_key = <v>``, ``SessionToken: <v>``, ``AccessKeyId=<v>`` and
``Authorization: Bearer <v>``. A whole-match replacement there takes the key,
the ``:``/``=`` separator and the opening quote along with the value, so a JSON
pair collapses to one bare string (``{"Authorization": "Bearer t"}`` reads back as
``{"[REDACTED: credential]"}``) and a file viewer reports a file that is valid on
disk as invalid JSON.

Each key-anchored branch therefore carries its value as ONE named capturing
group and ``_credential_value_span`` redacts that group alone -- one rule for
every branch. This file pins the rule by document shape (JSON, header line,
INI / ``.env`` line) for every key-anchored branch, and pins the STRUCTURE of the
alternation so the next branch cannot regress it in either direction: a
key-anchored branch without a value group collapses the pair again, and a
capturing group on a whole-match branch narrows its span and leaks the rest of
the token.

Every fixture value is synthetic (``…-not-a-secret-…``) and has no real
provider-key shape, so the secret scanners that read test files stay quiet.
"""

from __future__ import annotations

import json
import re

import pytest

from kiro_crew.security import (
    _CREDENTIAL_PATTERNS,
    _REDACTED_CREDENTIAL_TAG,
    redact_credentials,
)
from kiro_crew.security import redaction as _redaction

TAG = _REDACTED_CREDENTIAL_TAG


def _credential_value_span(match: "re.Match[str]") -> tuple[int, int]:
    """The live span rule, looked up at call time.

    Resolved lazily rather than imported at module level so this file still
    COLLECTS on a tree without the helper and fails at the assertion instead,
    which is what lets a red-first run observe the defect rather than an import
    error.
    """
    helper = getattr(_redaction, "_credential_value_span", None)
    assert helper is not None, "redaction.py defines no _credential_value_span"
    return helper(match)


#: The separator idiom every key-anchored branch spells between key and value.
#: Its presence in a branch's text is what makes the branch key-anchored.
_KEY_VALUE_SEPARATOR = "[:=]"

#: One registered (key, value) fixture per key spelling a key-anchored branch
#: accepts. ``test_every_key_anchored_branch_is_registered`` fails on the count
#: when a branch is added or a spelling dropped without a row here, so a new
#: key-anchored branch must earn its shape tests below.
KEY_ANCHORED_FIXTURES: tuple[tuple[str, str], ...] = (
    ("aws_secret_access_key", "test-secret-not-a-credential-0123"),
    ("SecretAccessKey", "test-secret-not-a-credential-0123"),
    ("aws_session_token", "test-session-not-a-credential-0123"),
    ("SessionToken", "test-session-not-a-credential-0123"),
    ("aws_access_key_id", "test-key-id-not-a-credential-0123"),
    ("AccessKeyId", "test-key-id-not-a-credential-0123"),
    ("Authorization", "Bearer test-token-not-a-secret-0123"),
)

#: The secret bytes of each fixture value: the part that must never survive.
#: For the Bearer header that is the token after the scheme; for the AWS forms
#: the whole value.
_SECRET_OF = {value: value.split()[-1] for _, value in KEY_ANCHORED_FIXTURES}


def _split_top_level(pattern: str) -> list[str]:
    """Split *pattern* on the ``|`` at the outermost group's depth.

    ``_CREDENTIAL_PATTERNS`` is one ``(?:a|b|c)`` group, so this returns its
    branches. Tracks escapes and character classes so a ``|`` or paren inside
    either is not mistaken for structure.
    """
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    in_class = False
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            buf.append(pattern[i : i + 2])
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
            buf.append(ch)
            i += 1
            continue
        if ch == "[":
            in_class = True
            buf.append(ch)
            i += 1
            continue
        if ch == "(":
            depth += 1
            if depth == 1:
                i += 1
                if pattern[i : i + 2] == "?:":
                    i += 2
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == ")":
            depth -= 1
            if depth == 0:
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == "|" and depth == 1:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


BRANCHES = _split_top_level(_CREDENTIAL_PATTERNS.pattern)
KEY_ANCHORED_BRANCHES = [b for b in BRANCHES if _KEY_VALUE_SEPARATOR in b]
WHOLE_MATCH_BRANCHES = [b for b in BRANCHES if _KEY_VALUE_SEPARATOR not in b]


def _ids(fixtures: tuple[tuple[str, str], ...]) -> list[str]:
    return [key for key, _ in fixtures]


# ── The reported shape: a JSON document keeps its structure ──


@pytest.mark.parametrize(("key", "value"), KEY_ANCHORED_FIXTURES, ids=_ids(KEY_ANCHORED_FIXTURES))
def test_json_document_stays_valid_and_keeps_its_key(key: str, value: str) -> None:
    """A JSON pair redacts to a JSON pair: the key survives, the value is the tag."""
    document = json.dumps({key: value, "region": "us-east-1"}, indent=2)

    redacted, warnings = redact_credentials(document)

    parsed = json.loads(redacted)  # must not raise
    assert parsed == {key: TAG, "region": "us-east-1"}
    assert _SECRET_OF[value] not in redacted
    assert len(warnings) == 1


@pytest.mark.parametrize(("key", "value"), KEY_ANCHORED_FIXTURES, ids=_ids(KEY_ANCHORED_FIXTURES))
def test_compact_json_document_stays_valid(key: str, value: str) -> None:
    """No whitespace around ``:`` -- the shape a serializer emits by default."""
    document = json.dumps({"before": 1, key: value, "after": [1, 2]}, separators=(",", ":"))

    redacted, _ = redact_credentials(document)

    assert json.loads(redacted) == {"before": 1, key: TAG, "after": [1, 2]}
    assert _SECRET_OF[value] not in redacted


def test_authorization_bearer_pair_survives_inside_a_headers_map() -> None:
    """The shape a request dump takes: a headers object beside other fields."""
    document = json.dumps(
        {
            "method": "GET",
            "headers": {
                "Accept": "application/json",
                "Authorization": "Bearer test-token-not-a-secret-0123",
            },
        }
    )

    redacted, _ = redact_credentials(document)

    parsed = json.loads(redacted)
    assert parsed["headers"]["Authorization"] == TAG
    assert parsed["headers"]["Accept"] == "application/json"
    assert parsed["method"] == "GET"
    assert "test-token-not-a-secret-0123" not in redacted


# ── Header lines, YAML-style lines and INI / .env lines keep their key ──


@pytest.mark.parametrize(("key", "value"), KEY_ANCHORED_FIXTURES, ids=_ids(KEY_ANCHORED_FIXTURES))
def test_colon_line_keeps_the_key(key: str, value: str) -> None:
    """``Key: value`` -- an HTTP header line or a YAML mapping line."""
    redacted, warnings = redact_credentials(f"{key}: {value}")

    assert redacted == f"{key}: {TAG}"
    assert warnings == [f"Redacted credential pattern ({len(value)} chars)"]


@pytest.mark.parametrize(("key", "value"), KEY_ANCHORED_FIXTURES, ids=_ids(KEY_ANCHORED_FIXTURES))
def test_equals_line_keeps_the_key(key: str, value: str) -> None:
    """``key=value`` and ``key = value`` -- ``.env`` and INI spellings."""
    for separator in ("=", " = "):
        redacted, _ = redact_credentials(f"{key}{separator}{value}")
        assert redacted == f"{key}{separator}{TAG}", repr(separator)


def test_env_style_uppercase_authorization_keeps_the_key() -> None:
    """``AUTHORIZATION=Bearer <token>``: the header name folds case, the key stays."""
    redacted, _ = redact_credentials("AUTHORIZATION=Bearer test-token-not-a-secret-0123")

    assert redacted == f"AUTHORIZATION={TAG}"
    assert "test-token-not-a-secret-0123" not in redacted


def test_http_header_line_keeps_the_header_name() -> None:
    redacted, _ = redact_credentials("Authorization: Bearer test-token-not-a-secret-0123")

    assert redacted == f"Authorization: {TAG}"
    assert "test-token-not-a-secret-0123" not in redacted


def test_a_header_inside_a_larger_document_leaves_its_neighbours_alone() -> None:
    document = (
        "GET /v1/items HTTP/1.1\n"
        "Host: api.example.com\n"
        "Authorization: Bearer test-token-not-a-secret-0123\n"
        "Accept: */*\n"
    )

    redacted, warnings = redact_credentials(document)

    assert redacted == (
        "GET /v1/items HTTP/1.1\n"
        "Host: api.example.com\n"
        f"Authorization: {TAG}\n"
        "Accept: */*\n"
    )
    assert len(warnings) == 1


def test_the_bearer_value_is_the_whole_credentials_string() -> None:
    """The scheme goes with the token: ``credentials = "Bearer" 1*SP b64token``.

    A quoted JSON value therefore reads back as the tag alone, and neither the
    scheme nor the token is left beside it.
    """
    redacted, _ = redact_credentials('{"Authorization": "Bearer test-token-not-a-secret-0123"}')

    assert redacted == f'{{"Authorization": "{TAG}"}}'
    assert "Bearer" not in redacted


def test_quotes_around_the_value_are_kept_on_both_sides() -> None:
    """Single or double quotes: the closing quote is outside the value class."""
    for quote in ('"', "'"):
        redacted, _ = redact_credentials(
            f"{quote}aws_secret_access_key{quote}: {quote}test-secret-not-a-credential-0123{quote}"
        )
        assert redacted == f"{quote}aws_secret_access_key{quote}: {quote}{TAG}{quote}", repr(quote)


def test_two_key_anchored_pairs_in_one_document_both_keep_their_keys() -> None:
    document = json.dumps(
        {
            "aws_access_key_id": "test-key-id-not-a-credential-0123",
            "aws_secret_access_key": "test-secret-not-a-credential-0123",
            "aws_session_token": "test-session-not-a-credential-0123",
        }
    )

    redacted, warnings = redact_credentials(document)

    assert json.loads(redacted) == {
        "aws_access_key_id": TAG,
        "aws_secret_access_key": TAG,
        "aws_session_token": TAG,
    }
    assert len(warnings) == 3


# ── The mechanism is one rule, and the alternation's shape enforces it ──


def test_every_key_anchored_branch_is_registered() -> None:
    """A new key-anchored branch must add its spellings to the fixture table."""
    spellings = 0
    for branch in KEY_ANCHORED_BRANCHES:
        compiled = re.compile(branch)
        spellings += sum(
            1 for key, value in KEY_ANCHORED_FIXTURES if compiled.match(f"{key}={value}")
        )
    assert spellings == len(KEY_ANCHORED_FIXTURES), (
        f"{len(KEY_ANCHORED_FIXTURES)} fixtures registered but the key-anchored "
        f"branches accept {spellings} of them; add or remove fixture rows"
    )
    assert len(KEY_ANCHORED_BRANCHES) == 4, KEY_ANCHORED_BRANCHES


def test_every_capturing_group_is_named() -> None:
    """The span rule reads the ONE group the matched branch closed; unnamed groups
    could not be audited branch by branch."""
    assert _CREDENTIAL_PATTERNS.groups == len(_CREDENTIAL_PATTERNS.groupindex)


def test_each_key_anchored_branch_has_exactly_one_value_group_after_its_separator() -> None:
    """Structural pin: the value group opens after the separator and closes the branch."""
    for branch in KEY_ANCHORED_BRANCHES:
        compiled = re.compile(branch)
        assert (
            compiled.groups == 1
        ), f"key-anchored branch without exactly one value group: {branch!r}"
        opener = branch.index("(?P<")
        assert opener > branch.rindex(
            _KEY_VALUE_SEPARATOR
        ), f"the value group must open after the key/value separator: {branch!r}"
        assert branch.endswith(")"), f"the value group must close the branch: {branch!r}"
        # The group's text is a regex of its own that spans exactly the tail.
        tail = re.compile(branch[opener:])
        assert (
            tail.groups == 1 and tail.groupindex
        ), f"the branch tail is not one named group: {branch!r}"


def test_no_whole_match_branch_carries_a_capturing_group() -> None:
    """A group on a groupless branch would narrow its span and leak the remainder."""
    for branch in WHOLE_MATCH_BRANCHES:
        assert (
            re.compile(branch).groups == 0
        ), f"whole-match branch with a capturing group: {branch!r}"


def test_the_group_names_are_exactly_the_key_anchored_values() -> None:
    names: set[str] = set()
    for branch in KEY_ANCHORED_BRANCHES:
        names.update(re.compile(branch).groupindex)
    assert names == set(_CREDENTIAL_PATTERNS.groupindex)


@pytest.mark.parametrize(("key", "value"), KEY_ANCHORED_FIXTURES, ids=_ids(KEY_ANCHORED_FIXTURES))
def test_value_span_is_the_group_and_ends_with_the_match(key: str, value: str) -> None:
    """Nothing of the value is left outside the redacted span."""
    text = f'{{"{key}": "{value}"}}'
    match = _CREDENTIAL_PATTERNS.search(text)
    assert match is not None

    start, end = _credential_value_span(match)

    assert text[start:end] == value
    assert end == match.end()
    assert text[match.start() : start].startswith(key)


def test_whole_match_branches_redact_their_whole_span() -> None:
    """A groupless branch IS the secret: the rule falls back to the full match.

    The connection-URI branch is the sample: it starts at the scheme, not at a
    key naming the secret, so ``scheme://user:password@`` goes whole and the
    host that follows stays.
    """
    text = "postgres://app:test-password-not-a-secret@db.example.com/app"
    match = _CREDENTIAL_PATTERNS.search(text)
    assert match is not None
    assert match.group() == "postgres://app:test-password-not-a-secret@"

    assert _credential_value_span(match) == match.span()
    redacted, _ = redact_credentials(text)
    assert redacted == f"{TAG}db.example.com/app"


def test_the_token_itself_is_never_partially_redacted() -> None:
    """A long opaque bearer value goes whole, not up to some inner character."""
    token = "test-token-not-a-secret-" + "0123456789abcdef." * 8 + "tail~end=="
    redacted, _ = redact_credentials(f"Authorization: Bearer {token}")

    assert redacted == f"Authorization: {TAG}"


# ── Re-redaction: the redactor is a fixed point on its own output ──
#
# Several surfaces run the redactor over text it already produced (the
# streaming path re-redacts the persisted copy; `redact_path_segments` requires
# its candidate to be a fixed point). Once a key-anchored branch keeps its key,
# the second run sees `key = [REDACTED: credential]` again, and a value class
# that stops at the tag's interior space would claim the tag's `[REDACTED:`
# head as a new value and mangle it. Pass 1 therefore skips a claimed span that
# lies inside one of the module's own tag literals, exactly as pass 4 does.


@pytest.mark.parametrize(("key", "value"), KEY_ANCHORED_FIXTURES, ids=_ids(KEY_ANCHORED_FIXTURES))
def test_redaction_is_a_fixed_point_on_a_redacted_document(key: str, value: str) -> None:
    """Surfaces re-run the redactor over their own output; the tag must not move."""
    for document in (
        json.dumps({key: value}),
        f"{key}: {value}",
        f"{key}={value}",
        f"{key} = {value} # trailing",
    ):
        once, _ = redact_credentials(document)
        assert key in once and _SECRET_OF[value] not in once, document
        twice, warnings = redact_credentials(once)

        assert twice == once, document
        assert warnings == [], document


@pytest.mark.parametrize(("key", "value"), KEY_ANCHORED_FIXTURES, ids=_ids(KEY_ANCHORED_FIXTURES))
def test_every_registered_tag_as_the_value_is_left_alone(key: str, value: str) -> None:
    """Each module-owned tag literal is trusted by construction, on every key."""
    from kiro_crew.security import CREDENTIAL_REDACTION_TAGS

    for tag in CREDENTIAL_REDACTION_TAGS:
        for text in (f"{key}={tag}", f'{{"{key}": "{tag}"}}', f"{key}: {tag} # trailing"):
            redacted, warnings = redact_credentials(text)
            assert redacted == text
            assert warnings == []


#: The key-anchored fixtures whose value class admits `[`, `]` and `:` -- the
#: three AWS forms. Only these can ever meet a tag-shaped value; the Bearer
#: token class is RFC 6750 `b64token` and cannot spell `[` at all.
_TAG_SPELLABLE_FIXTURES = tuple(
    (key, value) for key, value in KEY_ANCHORED_FIXTURES if " " not in value
)


@pytest.mark.parametrize(
    ("key", "value"), _TAG_SPELLABLE_FIXTURES, ids=_ids(_TAG_SPELLABLE_FIXTURES)
)
def test_a_tag_shaped_value_that_is_not_the_literal_is_redacted(key: str, value: str) -> None:
    """Trust is byte identity with the literal, never a shape: a value that only
    resembles a tag is a value, and it goes the way every other value goes."""
    for lookalike in (
        f"[REDACTED{value}",
        f"[REDACTED:credential]{value}",
        f"[redacted:{value}",
    ):
        text = f"{key}={lookalike}"
        redacted, warnings = redact_credentials(text)

        assert value not in redacted, text
        assert redacted == f"{key}={TAG}", text
        assert len(warnings) == 1, text


def test_the_skip_declines_only_bytes_inside_the_tag_literal() -> None:
    """Structural guarantee behind the skip: when a key-anchored branch matches
    with a registered tag as its value, the value group is EXACTLY that tag (the
    atom in the group consumes it whole), so declining it never leaves a byte
    outside the literal unredacted. The Bearer branch never matches a tag at all
    (its class excludes `[`)."""
    from kiro_crew.security import CREDENTIAL_REDACTION_TAGS

    claimed = 0
    for key, _ in KEY_ANCHORED_FIXTURES:
        for tag in CREDENTIAL_REDACTION_TAGS:
            text = f"{key}={tag}"
            matches = list(_CREDENTIAL_PATTERNS.finditer(text))
            if (key, _) not in _TAG_SPELLABLE_FIXTURES:
                assert matches == [], (text, matches)
                continue
            for match in matches:
                start, end = _credential_value_span(match)
                assert text[start:end] == tag, (text, match.group())
                claimed += 1
    # One match per (key, tag) pair, and its value is the whole tag.
    assert claimed == len(_TAG_SPELLABLE_FIXTURES) * len(CREDENTIAL_REDACTION_TAGS), claimed


@pytest.mark.parametrize(
    ("key", "value"), _TAG_SPELLABLE_FIXTURES, ids=_ids(_TAG_SPELLABLE_FIXTURES)
)
def test_bytes_glued_to_a_tag_are_redacted_with_it_and_warned(key: str, value: str) -> None:
    """A tag with bytes glued to its `]` is one value, not a tag: nothing certified
    those bytes, and a consumer that gates egress on the warning list must not be
    told the line was clean. The whole value goes, and a warning is raised."""
    glued = f"{key}={TAG}{value}"
    redacted, warnings = redact_credentials(glued)

    assert redacted == f"{key}={TAG}"
    assert value not in redacted
    assert warnings == [f"Redacted credential pattern ({len(TAG) + len(value)} chars)"]

    once_more, again = redact_credentials(redacted)
    assert once_more == redacted and again == []


def test_a_tag_followed_by_a_value_boundary_is_a_bare_tag_with_a_tail() -> None:
    """The class's own boundary ends the value: a space, a quote, `,` or `}` after
    the tag leaves a bare tag (skipped) and an ordinary tail (not this branch's
    value, exactly as ` tail` after any value's space)."""
    for text in (
        f"aws_secret_access_key={TAG} tail-not-a-value",
        f'{{"aws_secret_access_key": "{TAG}", "region": "us-east-1"}}',
        f"{{aws_secret_access_key={TAG},region=us-east-1}}",
    ):
        redacted, warnings = redact_credentials(text)
        assert redacted == text, text
        assert warnings == [], text
    plain, _ = redact_credentials("aws_secret_access_key=test-secret-not-a-credential-0123 tail")
    assert plain == f"aws_secret_access_key={TAG} tail"


# ── A key-anchored pair NESTED inside a `?token=` parameter value ──
#
# Pass 4 (`_TOKEN_PARAM_RE`) ranks last and subtracts every earlier claim from
# its value span. Once pass 1 keeps the key, a parameter value such as
# `aws_secret_access_key=<secret>` is only PARTLY claimed -- the secret -- and
# tagging the leftover `aws_secret_access_key=` gap on its own writes two
# adjacent tags. That output is not a fixed point: on the next run the value
# `[REDACTED: credential][REDACTED: credential]` is not byte-identical to a tag,
# the value class stops at the interior space, and the head
# `[REDACTED: credential][REDACTED:` is claimed as a value -- the persisted
# text becomes `?token=[REDACTED: credential] credential]`. A partly-covered
# parameter value is therefore redacted as ONE tag, the same text a
# whole-match claim of the pair produced before the key survived.


@pytest.mark.parametrize(
    ("key", "value"), _TAG_SPELLABLE_FIXTURES, ids=_ids(_TAG_SPELLABLE_FIXTURES)
)
def test_a_pair_nested_in_a_token_parameter_value_is_one_tag(key: str, value: str) -> None:
    """The parameter value is one credential: one tag, and a fixed point from
    the first application. The `&x=1` row rides along because the AWS value
    class runs to whitespace, so the pair's claim reaches past the parameter's
    own `&` boundary and the coalesced span must follow it."""
    for text, expected in (
        (f"?token={key}={value}", f"?token={TAG}"),
        (f"path?token={key}={value}", f"path?token={TAG}"),
        (f"?token={key}={value}&x=1", f"?token={TAG}"),
        (f'{{"url": "path?token={key}={value}"}}', f'{{"url": "path?token={TAG}"}}'),
        (f"a=1&token={key}={value} tail", f"a=1&token={TAG} tail"),
    ):
        once, warnings = redact_credentials(text)

        assert once == expected, text
        assert value not in once, text
        assert once.count(TAG) == 1, text
        assert len(warnings) == 2, (text, warnings)

        twice, again = redact_credentials(once)
        assert twice == once, text
        assert again == [], text


def test_a_glued_tag_with_a_trailing_tail_in_a_token_parameter_is_a_fixed_point() -> None:
    """The two tag shapes pass 4 meets on its own output stay put on a second
    run: a bare tag (skipped) and a tag glued to bytes whose tail sits past the
    value boundary (the glued value goes whole, the tail is ordinary text)."""
    for text in (
        f"?token={TAG}",
        f"?token={TAG}glued-not-a-secret-0123 tail",
        f"?token={TAG}glued-not-a-secret-0123&x=1",
    ):
        once, _ = redact_credentials(text)
        assert once.startswith(f"?token={TAG}"), text
        assert "glued-not-a-secret-0123" not in once, text

        twice, warnings = redact_credentials(once)
        assert twice == once, text
        assert warnings == [], text


# ── A QUOTED value is one value, to its closing quote ──
#
# The value class stops at a space, so inside `"key": "<secret> tail"` the value
# group is `<secret>` alone. Quoted, the quoted string is ONE value and the bytes
# after the class run are part of it, so the claim runs to the closing quote on
# the FIRST run (`_quoted_value_end`): `"key": "[REDACTED: credential]"`. That is
# what keeps redaction a fixed point of itself. Judging the quoted extent only
# when the value is already a tag was not one: the first run over `"<secret>
# tail"` claimed the class run alone and emitted exactly the tag-with-a-quoted-
# tail shape the second run then claimed through the quote -- deleting ` tail`
# and warning afresh on text the first run had already cleaned, on every surface
# that re-redacts its own output. And a tag heading a quoted value that continues
# (`"key": "[REDACTED: credential] <secret>"`) is still claimed through the quote
# and warned, never skipped: a skip there would leave the secret standing with NO
# warning, the one thing `decisions.gate.scrub_reason` must never be told.
#
# The closing quote is the first UNESCAPED one (a backslash escapes the byte after
# it). A quote that never closes on its line ends the value at the line's end:
# nothing after an unterminated opening quote is certified, a raw line break is
# where a quoted string ends in every format this anchors on, and falling back
# to the class run would let `"key": "[REDACTED: credential] <secret>` with no
# closing quote stand with NO warning -- a bypass of the egress gate.


@pytest.mark.parametrize(("key", "value"), KEY_ANCHORED_FIXTURES, ids=_ids(KEY_ANCHORED_FIXTURES))
def test_a_quoted_value_is_claimed_to_its_closing_quote_on_the_first_run(
    key: str, value: str
) -> None:
    """A secret with a tail inside its quotes is one value: the whole quoted
    content goes on the first run, one warning, and the output is a fixed point.

    Red on the head that judged the quoted extent only for a value that was
    already a tag: the first run left `"[REDACTED: credential] key, rotated"`,
    and the second run claimed ` key, rotated` away."""
    for text, expected in (
        (f'{{"{key}": "{value} key, rotated"}}', f'{{"{key}": "{TAG}"}}'),
        (f"{key}='{value} key, rotated' # note", f"{key}='{TAG}' # note"),
        (f'{{"{key}": "{value} key, rotated", "r": "x"}}', f'{{"{key}": "{TAG}", "r": "x"}}'),
    ):
        redacted, warnings = redact_credentials(text)

        assert redacted == expected, text
        assert "key, rotated" not in redacted, text
        assert warnings == [
            f"Redacted credential pattern ({len(value) + len(' key, rotated')} chars)"
        ], (
            text,
            warnings,
        )

        twice, again = redact_credentials(redacted)
        assert twice == redacted and again == [], text


def test_the_reviewed_input_is_a_fixed_point_from_the_first_run() -> None:
    """The exact input the Opus lane reproduced with: a short secret and a tail
    inside a JSON string. One run, one tag, JSON still parses, second run inert."""
    text = '{"SecretAccessKey": "wJalr key, rotated"}'

    redacted, warnings = redact_credentials(text)

    assert json.loads(redacted) == {"SecretAccessKey": TAG}
    assert len(warnings) == 1
    assert redact_credentials(redacted) == (redacted, [])


@pytest.mark.parametrize(
    ("key", "value"), _TAG_SPELLABLE_FIXTURES, ids=_ids(_TAG_SPELLABLE_FIXTURES)
)
def test_a_tag_heading_a_quoted_value_that_continues_is_redacted_through_the_quote(
    key: str, value: str
) -> None:
    """A tag skips only when it fills the quoted value; a tail before the closing
    quote makes the whole quoted content the value, redacted with a warning."""
    for text, expected in (
        (f'{{"{key}": "{TAG} {value}"}}', f'{{"{key}": "{TAG}"}}'),
        (f"{key}='{TAG} {value}'", f"{key}='{TAG}'"),
        (f'{{"{key}": "{TAG} {value}", "r": "x"}}', f'{{"{key}": "{TAG}", "r": "x"}}'),
    ):
        redacted, warnings = redact_credentials(text)

        assert redacted == expected, text
        assert value not in redacted, text
        assert len(warnings) == 1, (text, warnings)

        twice, again = redact_credentials(redacted)
        assert twice == redacted and again == [], text

    # The skip itself is unchanged where the tag fills the value: quoted with
    # the closing quote right after it, or unquoted with an ordinary tail.
    for text in (
        f'{{"{key}": "{TAG}", "r": "x"}}',
        f"{key}={TAG} tail-not-a-value",
        f"{key}: {TAG} # trailing",
    ):
        assert redact_credentials(text) == (text, []), text


@pytest.mark.parametrize(("key", "value"), KEY_ANCHORED_FIXTURES, ids=_ids(KEY_ANCHORED_FIXTURES))
def test_an_escaped_quote_inside_a_quoted_value_does_not_end_it(key: str, value: str) -> None:
    """The closing quote is the first UNESCAPED one, by backslash parity.

    Red on the head that took the first quote byte after the value as the
    close: with `"[REDACTED: credential] text\\"suffix"` the splice ate the
    backslash and left a bare `"` behind it, so a valid JSON document the Files
    view redacts in place came back unparseable. `json.dumps` writes the escapes, so
    every row here is a valid document by construction and must parse after."""
    for tail in ('text"suffix', "text\\", 'a\\"b\\\\"c', '"'):
        document = json.dumps({key: f"{value} {tail}", "r": "x"})

        redacted, warnings = redact_credentials(document)

        assert json.loads(redacted) == {key: TAG, "r": "x"}, document
        assert "suffix" not in redacted and _SECRET_OF[value] not in redacted, document
        assert len(warnings) == 1, (document, warnings)
        assert redact_credentials(redacted) == (redacted, []), document

    # The GPT lane's exact shape: a tag heading the quoted value, the tail
    # carrying an escaped quote. Claimed through the REAL closing quote. (The
    # Bearer branch cannot meet a tag at all: its class excludes `[`.)
    if " " not in value:
        text = f'{{"{key}": "{TAG} text\\"suffix"}}'
        redacted, warnings = redact_credentials(text)
        assert json.loads(redacted) == {key: TAG}, text
        assert len(warnings) == 1
        assert redact_credentials(redacted) == (redacted, [])

    # A secret whose class run ends ON a backslash (the class admits one, the
    # quote it escapes stops the run): parity is scanned from the value's first
    # byte, so that quote is not the close either.
    document = json.dumps({key: f'{value}"more'})
    redacted, warnings = redact_credentials(document)
    assert json.loads(redacted) == {key: TAG}, document
    assert "more" not in redacted


@pytest.mark.parametrize(("key", "value"), KEY_ANCHORED_FIXTURES, ids=_ids(KEY_ANCHORED_FIXTURES))
def test_a_quote_that_never_closes_claims_to_the_end_of_its_line(key: str, value: str) -> None:
    """An unterminated opening quote certifies nothing after it: the value runs
    to the line's end -- the first raw line break, or the end of the text -- and
    the claim is warned. The next line is untouched, and the output is a fixed
    point (a bare tag ending the line IS the extended span, so it is skipped).

    Red on the head that fell back to the class run when the quote never closed:
    `"key": "[REDACTED: credential] <secret>` with no closing quote left the
    secret standing with NO warning, so `decisions.gate.scrub_reason` read the
    state clean -- and the redactor's own first pass over `key="<s1> <s2>` emits
    exactly that shape. The rows below name the value's tail so passes 2 and 3
    cannot mask the claim."""
    secret = _SECRET_OF[value]
    for text, expected, claimed in (
        (
            f'"{key}": "{value} rest of the line, no quote',
            f'"{key}": "{TAG}',
            f"{value} rest of the line, no quote",
        ),
        (
            f'"{key}": "{value} tail\n"r": "x"',
            f'"{key}": "{TAG}\n"r": "x"',
            f"{value} tail",
        ),
        (
            f"{key}='{value} tail\r\nnext line",
            f"{key}='{TAG}\r\nnext line",
            f"{value} tail",
        ),
    ):
        redacted, warnings = redact_credentials(text)

        assert redacted == expected, text
        assert secret not in redacted and claimed not in redacted, text
        assert warnings == [f"Redacted credential pattern ({len(claimed)} chars)"], (text, warnings)

        twice, again = redact_credentials(redacted)
        assert twice == redacted and again == [], text

    if " " not in value:
        # The reviewed shape: a tag heading an UNTERMINATED quoted value is not a
        # bare tag with a tail -- the tail is inside the value as far as any
        # format can tell -- so it is claimed to the line's end and WARNED.
        for text, expected in (
            (f'"{key}": "{TAG} {value}', f'"{key}": "{TAG}'),
            (f'"{key}": "{TAG} {value}\n"r": "x"', f'"{key}": "{TAG}\n"r": "x"'),
            (f"{key}='{TAG} {value}", f"{key}='{TAG}"),
        ):
            redacted, warnings = redact_credentials(text)
            assert redacted == expected, text
            assert secret not in redacted, text
            assert len(warnings) == 1, (text, warnings)
            assert redact_credentials(redacted) == (redacted, []), text

        # A bare tag that ends the line is the extended span itself: skipped, a
        # fixed point, whether the text ends there or continues on the next line.
        for text in (
            f'"{key}": "{TAG}',
            f'"{key}": "{TAG}\n"r": "x"',
            f"{key}='{TAG}\r\nnext line",
        ):
            assert redact_credentials(text) == (text, []), text


def test_a_later_pair_inside_a_quoted_claim_is_not_claimed_twice() -> None:
    """A quoted claim may reach past a later key-anchored match (the tail names
    another key); that match is already redacted by the claim, not spliced again."""
    text = '{"aws_secret_access_key": "test-secret-not-a-credential-0123 aws_session_token=x"}'

    redacted, warnings = redact_credentials(text)

    assert redacted == f'{{"aws_secret_access_key": "{TAG}"}}'
    assert len(warnings) == 1
    assert redact_credentials(redacted) == (redacted, [])


def test_a_match_straddling_a_quoted_claims_end_is_clamped_not_dropped() -> None:
    """A whole-match branch admits a quote byte (the connection URI's user class
    `[^\\s:/@]*`, its password class `[^\\s/]+`, the PEM body `[\\s\\S]*?`), so its
    span can BEGIN inside a quoted claim and END past it. Only a span the claim
    covers whole is skipped; a straddling one is clamped to the part past the
    claim, which is redacted and warned.

    Red on the head that skipped every match starting inside the claim's reach:
    the quoted value's close fell inside the URI's userinfo, the URI match began
    inside the claim, and its tail -- the password -- streamed in plaintext with
    no warning."""
    text = 'aws_secret_access_key="x https://u"ser:hunter2@db.internal/app'

    redacted, warnings = redact_credentials(text)

    assert "hunter2" not in redacted and "ser:" not in redacted, redacted
    assert redacted == f'aws_secret_access_key="{TAG}{TAG}db.internal/app'
    assert warnings == [
        "Redacted credential pattern (11 chars)",
        "Redacted credential pattern (13 chars)",
    ]
    # Converges rather than a one-step fixed point: the host bytes glued to the
    # tag inside the still-open quote are uncertified, so the second application
    # claims them to the line's end (warned), and the third is inert. No secret
    # byte survives any application.
    twice, again = redact_credentials(redacted)
    assert twice == f'aws_secret_access_key="{TAG}' and len(again) == 1
    assert redact_credentials(twice) == (twice, [])

    # NESTED in a claim that runs to the LINE's end (the quote never closes): the
    # access-key id inside the line is covered whole and claimed once, by the
    # claim; the next line is untouched; and the tag that then ends the line is
    # a fixed point -- a second application keeps the tail behind the boundary.
    nested = 'aws_secret_access_key="x AKIAIOSFODNN7EXAMPLE\nnext line'
    redacted, warnings = redact_credentials(nested)
    assert redacted == f'aws_secret_access_key="{TAG}\nnext line'
    assert warnings == ["Redacted credential pattern (22 chars)"]
    assert redact_credentials(redacted) == (redacted, [])

    # Fully covered by the claim: skipped, exactly once redacted.
    covered = 'aws_secret_access_key="x https://u:hunter2@h" tail'
    redacted, warnings = redact_credentials(covered)
    assert redacted == f'aws_secret_access_key="{TAG}" tail'
    assert len(warnings) == 1
