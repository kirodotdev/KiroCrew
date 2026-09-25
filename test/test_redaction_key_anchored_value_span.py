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

import base64
import json
import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import assert_linear_work
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


def test_a_quoted_bearer_value_split_by_a_line_break_is_redacted_whole() -> None:
    """The Bearer value is the one key-anchored group whose class spans whitespace
    (``Bearer\\s+<token>``), so a YAML folded scalar, an obs-folded header log or
    a wrapped request dump can put the scheme and the token on different lines
    inside one quoted value. The quoted boundary only ever EXTENDS a claim: the
    line break is where a quoted string ends for the quote scan, but the branch
    already matched past it, and the match is the floor.

    Red on the head that assigned the quote scan's answer unconditionally: the
    claim shrank to ``Bearer`` and the token stood in plaintext, with the
    warning reporting six characters."""
    secret = "test-token-not-a-secret-0123"
    for text, expected in (
        (
            f'Authorization: "Bearer\n  {secret}"',
            f'Authorization: "{TAG}"',
        ),
        (
            f'{{"Authorization": "Bearer\r\n    {secret}"}}',
            f'{{"Authorization": "{TAG}"}}',
        ),
        (
            f"authorization='bearer\n\t{secret}' next",
            f"authorization='{TAG}' next",
        ),
    ):
        redacted, warnings = redact_credentials(text)

        assert redacted == expected, text
        assert secret not in redacted, text
        assert warnings == [
            f"Redacted credential pattern ({len(text) - len(expected) + len(TAG)} chars)"
        ], (
            text,
            warnings,
        )
        assert redact_credentials(redacted) == (redacted, []), text

    # Unquoted, the same split value was never in question: the branch's own
    # match is the claim, and the next line's bytes after the token are not.
    redacted, _ = redact_credentials(f"Authorization: Bearer\n  {secret}\nAccept: */*")
    assert redacted == f"Authorization: {TAG}\nAccept: */*"


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
    """Nothing of the value is left outside the redacted span: a key-anchored
    branch ends at its separator with an empty group marking where the value
    begins, and the scanner's claim from there is exactly the value."""
    from kiro_crew.security.redaction import _keyed_value_of

    text = f'{{"{key}": "{value}"}}'
    match = _CREDENTIAL_PATTERNS.search(text)
    assert match is not None

    start, end = _credential_value_span(match)
    assert end == match.end()
    assert text[match.start() : start].startswith(key)

    claim = _keyed_value_of(text, match)
    assert claim is not None
    assert text[claim.start : claim.end] == value
    assert claim.closes and claim.opener == '"'


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
    """Structural guarantee behind the skip: when a key-anchored branch anchors a
    registered tag standing as its value, the scanner's claim is EXACTLY that
    tag (a tag run is one value token, read whole), so declining it never leaves
    a byte outside the literal unredacted. The Bearer branch never matches a tag
    at all (its class excludes `[`)."""
    from kiro_crew.security import CREDENTIAL_REDACTION_TAGS
    from kiro_crew.security.redaction import _keyed_value_of

    claimed = 0
    for key, _ in KEY_ANCHORED_FIXTURES:
        for tag in CREDENTIAL_REDACTION_TAGS:
            text = f"{key}={tag}"
            matches = list(_CREDENTIAL_PATTERNS.finditer(text))
            if (key, _) not in _TAG_SPELLABLE_FIXTURES:
                assert matches == [], (text, matches)
                continue
            for match in matches:
                claim = _keyed_value_of(text, match)
                assert claim is not None, text
                assert text[claim.start : claim.end] == tag, (text, match.group())
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


def test_a_token_value_made_of_adjacent_claims_is_one_tag() -> None:
    """Two credentials standing side by side inside one `token=` value are two
    pass-1 claims with no gap between them. Left as two adjacent tags, the next
    run reads `[REDACTED: credential][REDACTED:` as a value (the class stops at
    the second tag's interior space), mangles it to `[REDACTED: credential]
    credential]` and warns about text that holds no secret -- so
    `decisions.gate.scrub_reason` refuses a state that is already clean and
    the persisted copy drifts from the streamed one. Pass 4 coalesces the
    fully-covered value into ONE tag on the first run, silently: every byte of
    it was claimed and warned for by the pass that found it.

    Red on the head whose pass 4 skipped every value with no gap: the first run
    wrote two tags and the second run mangled them."""
    key_id = "AKIAIOSFODNN7EXAMPLE"
    for text, expected in (
        (f"?token={key_id}{key_id}&x=1", f"?token={TAG}&x=1"),
        (
            f"https://h.example/p?a=1&token={key_id}{key_id} tail",
            f"https://h.example/p?a=1&token={TAG} tail",
        ),
        (f'{{"u": "?token={key_id}{key_id}"}}', f'{{"u": "?token={TAG}"}}'),
    ):
        redacted, warnings = redact_credentials(text)

        assert redacted == expected, text
        assert key_id not in redacted
        pass1 = [w for w in warnings if w.startswith("Redacted credential pattern")]
        assert len(pass1) == 2, warnings
        assert not any(w.startswith("Redacted token parameter") for w in warnings), warnings
        assert redact_credentials(redacted) == (redacted, []), text


def test_a_run_of_whole_tags_standing_as_a_value_is_left_alone() -> None:
    """`main` wrote two adjacent tags for two credentials inside one `token=`
    value and its prefix skip left them alone on every later run, so persisted
    text carries that shape. A run of whole registered tags is a value the
    redactor declines to claim, in pass 4 and in a key-anchored pair alike;
    a run with bytes glued to its last `]` is a value and is redacted whole.

    Red on the head whose tag atom and predicate knew one tag only: the run's
    head up to the second tag's interior space was claimed and mangled."""
    encoded = "[REDACTED: encoded credential]"
    for text in (
        f"?token={TAG}{TAG}&x=1",
        f"see https://h.example/?token={TAG}{encoded}{TAG} now",
        f'aws_secret_access_key="{TAG}{TAG}"',
        f"SessionToken={TAG}{TAG} # x",
    ):
        assert redact_credentials(text) == (text, []), text

    glued = f"?token={TAG}{TAG}Xk9fQ2mP4nR7sT1v&x=1"
    redacted, warnings = redact_credentials(glued)
    assert redacted == f"?token={TAG}&x=1"
    assert "Xk9fQ2mP4nR7sT1v" not in redacted
    assert warnings == [f"Redacted token parameter value ({2 * len(TAG) + 16} chars)"]
    assert redact_credentials(redacted) == (redacted, [])

    glued_pair = f"aws_secret_access_key={TAG}{TAG}test-secret-not-a-credential-0123"
    redacted, warnings = redact_credentials(glued_pair)
    assert redacted == f"aws_secret_access_key={TAG}"
    assert len(warnings) == 1
    assert redact_credentials(redacted) == (redacted, [])


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
    the claim is warned. The next line is untouched. The claim WRITES the closing
    quote the line never had, so the redactor's own output reads as a tag that
    fills a closed quoted value and a second run is silent: the pair was judged
    once, and a re-screen of persisted history, an artifact or an auto-nudge
    state must not refuse text the redactor itself wrote. Only a tag an AUTHOR
    left inside an unterminated quote (no claim, nothing written) is warned on
    every run, because nothing proves it fills its value.

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
            f'"{key}": "{TAG}"',
            f"{value} rest of the line, no quote",
        ),
        (
            f'"{key}": "{value} tail\n"r": "x"',
            f'"{key}": "{TAG}"\n"r": "x"',
            f"{value} tail",
        ),
        (
            f"{key}='{value} tail\r\nnext line",
            f"{key}='{TAG}'\r\nnext line",
            f"{value} tail",
        ),
    ):
        redacted, warnings = redact_credentials(text)

        assert redacted == expected, text
        assert secret not in redacted and claimed not in redacted, text
        assert warnings == [f"Redacted credential pattern ({len(claimed)} chars)"], (text, warnings)

        # One-step fixed point, bytes AND warnings: the written closing quote
        # proves to the second run that the tag fills its value.
        assert redact_credentials(redacted) == (redacted, []), text

    if " " not in value:
        # The reviewed shape: a tag heading an UNTERMINATED quoted value is not a
        # bare tag with a tail -- the tail is inside the value as far as any
        # format can tell -- so it is claimed to the line's end and WARNED.
        for text, expected in (
            (f'"{key}": "{TAG} {value}', f'"{key}": "{TAG}"'),
            (f'"{key}": "{TAG} {value}\n"r": "x"', f'"{key}": "{TAG}"\n"r": "x"'),
            (f"{key}='{TAG} {value}", f"{key}='{TAG}'"),
        ):
            redacted, warnings = redact_credentials(text)
            assert redacted == expected, text
            assert secret not in redacted, text
            assert len(warnings) == 1, (text, warnings)
            assert redact_credentials(redacted) == (redacted, []), text

        # A bare tag an AUTHOR left inside an UNTERMINATED quote: no claim is
        # made, so nothing is written (the bytes are the tag) and nothing proves
        # the tag fills its value -- the line is warned on every run, whether
        # the text ends there or continues on the next line. The redactor never
        # writes this shape itself: its own claim closes the quote (above).
        for text in (
            f'"{key}": "{TAG}',
            f'"{key}": "{TAG}\n"r": "x"',
            f"{key}='{TAG}\r\nnext line",
        ):
            assert redact_credentials(text) == (
                text,
                [f"Redacted credential pattern ({len(TAG)} chars)"],
            ), text
        assert redact_credentials(f'"{key}": "{TAG}"') == (f'"{key}": "{TAG}"', [])
        assert redact_credentials(f"{key}='{TAG}'\nnext") == (f"{key}='{TAG}'\nnext", [])


#: The fixtures whose value group embeds the tag atom (`TAG<class>*|<class>+`):
#: the three AWS key-value pairs. The Bearer branch's value is `Bearer <token>`
#: and never a tag, so the tag-skip rules below cannot be exercised through it.
_TAG_VALUED_FIXTURES = tuple((k, v) for k, v in KEY_ANCHORED_FIXTURES if " " not in v)


@pytest.mark.parametrize(("key", "value"), _TAG_VALUED_FIXTURES, ids=_ids(_TAG_VALUED_FIXTURES))
def test_a_tag_heading_an_unterminated_quote_cannot_silence_the_next_line(
    key: str, value: str
) -> None:
    """A quoted value that runs on to the next line (a YAML or shell string folds
    across a raw line break) whose FIRST line is exactly a tag: the tag does not
    demonstrably fill the value, so the line is claimed -- the bytes are already
    the tag -- and WARNED, and `decisions.gate.scrub_reason` refuses. The next
    line is judged on its own, as the line-bounded scan always has.

    Red on the head that skipped every tag-shaped span: an author-supplied tag
    on the first line silenced the only credential signal the gate has for the
    pair, and a value on the continuation line that no whole-match branch
    recognises stood with NO warning. The continuation value here is shaped so
    that neither pass 2 nor pass 3 can mask the claim."""
    first_line_warning = f"Redacted credential pattern ({len(TAG)} chars)"
    for text in (
        f'{key}: "{TAG}\n  tail-not-a-shape-0123"',
        f"{key}='{TAG}\n  tail-not-a-shape-0123'",
        f'{{"{key}": "{TAG}\n{value}"}}',
    ):
        redacted, warnings = redact_credentials(text)

        assert redacted.split("\n", 1)[0] == text.split("\n", 1)[0], text
        assert warnings[:1] == [first_line_warning], (text, warnings)
        twice, again = redact_credentials(redacted)
        assert twice == redacted, text
        assert again == [first_line_warning], (text, again)


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
    # Converges rather than a one-step fixed point: the clamped URI claim ate the
    # quote that closed the value, so the host bytes glued to the tag sit inside
    # a now-open quote and are uncertified; the second application claims them
    # to the line's end, WRITES the closing quote, and the third is silent. No
    # secret byte survives any application.
    twice, again = redact_credentials(redacted)
    assert twice == f'aws_secret_access_key="{TAG}"' and len(again) == 1
    assert redact_credentials(twice) == (twice, [])

    # NESTED in a claim that runs to the LINE's end (the quote never closes): the
    # access-key id inside the line is covered whole and claimed once, by the
    # claim, which writes the closing quote; the next line is untouched; and the
    # output is a one-step fixed point.
    nested = 'aws_secret_access_key="x AKIAIOSFODNN7EXAMPLE\nnext line'
    redacted, warnings = redact_credentials(nested)
    assert redacted == f'aws_secret_access_key="{TAG}"\nnext line'
    assert warnings == ["Redacted credential pattern (22 chars)"]
    assert redact_credentials(redacted) == (redacted, [])

    # The MULTI-LINE straddle, reachable only through the PEM branch (the one
    # groupless class that crosses a line break): the header sits inside the
    # unterminated claim, the body and footer run on past the line. The claim
    # ends at the line break; the block is clamped to the part past it, which
    # is redacted and warned -- not skipped with the match, which would leave
    # the body standing. The fixture is assembled at runtime so that no source
    # line carries a private-key header and the body decodes to prose.
    header = "-----" + "BEGIN RSA " + "PRIVATE " + "KEY-----"
    footer = "-----" + "END RSA " + "PRIVATE " + "KEY-----"
    body = base64.b64encode(b"not key material, a test fixture").decode()
    pem = f'aws_secret_access_key="x {header}\n{body}\n{footer}'
    redacted, warnings = redact_credentials(pem)
    assert body not in redacted and "END RSA" not in redacted, redacted
    assert redacted == f'aws_secret_access_key="{TAG}"{TAG}'
    assert warnings == [
        f"Redacted credential pattern ({len('x ') + len(header)} chars)",
        f"Redacted credential pattern ({len(body) + len(footer) + 2} chars)",
    ]
    # A one-step fixed point, unlike the URI shape: the first claim ran to the
    # line's end and wrote the closing quote, so the clamped block's tag sits
    # OUTSIDE the quoted value, where no branch anchors on it; no body byte
    # survives any application.
    assert redact_credentials(redacted) == (redacted, [])

    # Fully covered by the claim: skipped, exactly once redacted.
    covered = 'aws_secret_access_key="x https://u:hunter2@h" tail'
    redacted, warnings = redact_credentials(covered)
    assert redacted == f'aws_secret_access_key="{TAG}" tail'
    assert len(warnings) == 1


# ── Presence-only readers of the patterns ─────────────────────────────────────
#
# Several gates never redact: they ask whether text CARRIES a credential and
# refuse when it does -- the ledger push gate over `ledger.jsonl`, the deploy
# and preview file scans, the exfil request gates. Each read the raw patterns
# (`get_credential_patterns().search`, `_contains_fixed_credential`, the hard
# URL regex). Once the key survives redaction, the redactor's own output
# `key=[REDACTED: credential]` matches a key-anchored branch again, so a
# presence-only reader called cleaned text live: the ledger refused every push
# from its first redacted entry on. The readers now go through
# `contains_credential` / `credential_matches`, which apply pass 1's own rule
# for a tag standing as the value, and the hard regex declines that value by
# construction. These pins hold the two sides together: whatever the redactor
# leaves alone in silence is clean to every presence-only reader, and whatever
# it claims or warns on is live to every one of them.


def _presence_readers() -> tuple:
    from kiro_crew.security import (
        _contains_fixed_credential,
        contains_credential,
        credential_matches,
        hard_credential_hit,
    )

    return (
        ("contains_credential", contains_credential),
        ("credential_matches", lambda text: next(credential_matches(text), None) is not None),
        ("_contains_fixed_credential", _contains_fixed_credential),
        ("_HARD_CREDENTIAL_RE", lambda text: hard_credential_hit(text) is True),
    )


@pytest.mark.parametrize(("key", "value"), KEY_ANCHORED_FIXTURES, ids=_ids(KEY_ANCHORED_FIXTURES))
def test_the_redactors_own_output_is_clean_to_every_presence_only_reader(
    key: str, value: str
) -> None:
    """The raw secret is live to every reader; the redactor's output over it is
    clean to every reader -- while the RAW patterns still match that output on
    the key-anchored spellings, which is the whole reason the accessors exist."""
    from kiro_crew.security import get_credential_patterns

    for document in (
        json.dumps({key: value}),
        f"{key}: {value}",
        f"{key}={value}",
        f'{key}="{value}" # trailing',
    ):
        once, warnings = redact_credentials(document)
        assert warnings and _SECRET_OF[value] not in once, document
        for name, reader in _presence_readers():
            if name == "_HARD_CREDENTIAL_RE" and key == "Authorization":
                continue  # the hard URL regex has no Bearer branch; nothing to clean
            assert reader(document) is True, (name, document)
            assert reader(once) is False, (name, once)
        if (key, value) in _TAG_SPELLABLE_FIXTURES:
            assert any(p.search(once) for p in get_credential_patterns()), once


@pytest.mark.parametrize(
    ("key", "value"), _TAG_SPELLABLE_FIXTURES, ids=_ids(_TAG_SPELLABLE_FIXTURES)
)
def test_a_tag_with_glued_bytes_or_in_another_case_is_live_to_every_reader(
    key: str, value: str
) -> None:
    """Trust is byte identity of the whole value with a registered literal, to
    the readers exactly as to the redactor: glued bytes make a value, and so
    does a tag spelled in another case -- the hard regex is case-insensitive,
    so its exclusion is pinned case-sensitive."""
    for live in (f"{key}={TAG}{value}", f"{key}={TAG.lower()}", f"{key}: {TAG.upper()}"):
        redacted, warnings = redact_credentials(live)
        assert redacted != live and warnings, live
        for name, reader in _presence_readers():
            assert reader(live) is True, (name, live)


@pytest.mark.parametrize(("key", "value"), _TAG_VALUED_FIXTURES, ids=_ids(_TAG_VALUED_FIXTURES))
def test_the_presence_check_warns_where_pass_one_warns(key: str, value: str) -> None:
    """A tag heading a quoted value whose quote never closes on its line is
    WARNED by pass 1 on every run (the folded-value refusal), so it is live to
    the presence check; the same tag filling a closed quoted value is clean."""
    from kiro_crew.security import contains_credential

    folded = f'{key}: "{TAG}\n{value}"'
    _, warnings = redact_credentials(folded)
    assert warnings, folded
    assert contains_credential(folded) is True, folded

    closed = f'{key}: "{TAG}"\nnext: line'
    assert redact_credentials(closed) == (closed, []), closed
    assert contains_credential(closed) is False, closed


def test_every_registered_tag_as_a_value_crosses_batch_stream_and_flatten_clean() -> None:
    """One pin over every site that recognises a tag standing as a value.

    The tag shape is read at four places -- pass 1 and pass 4 through
    ``_CREDENTIAL_TAG_ATOM`` and ``_value_is_credential_tag``, the stream's
    anchor and discard through ``_CREDENTIAL_TAG_PREFIX_ATOM`` and the registry,
    and the splitter's hand-spelled ``_KEY_GLUE_BEFORE_TAG``. They must agree,
    and at each one a disagreement is silent. So every registered tag, as the
    value of every key-anchored spelling in every separator shape, is driven
    through all three paths: the batch redactor leaves it alone in silence and
    the presence check calls it clean; the stream emits it byte-identical
    across EVERY chunk boundary and drops nothing; the delivery flatten reads
    clean at every cut, so no fragment shows or rejoins a key."""
    from kiro_crew.messaging.renderer import _default_redactor
    from kiro_crew.messaging.split import _flattened_for_any_cut, _rejoins_a_key
    from kiro_crew.security import CREDENTIAL_REDACTION_TAGS, StreamRedactor, contains_credential

    def _shows_a_key(pieces: list[str]) -> bool:
        return any(_default_redactor(piece) != piece for piece in pieces)

    for tag in CREDENTIAL_REDACTION_TAGS:
        for key, _value in KEY_ANCHORED_FIXTURES:
            for text in (f"{key}={tag}", f"{key}: {tag}", f'{{"{key}": "{tag}"}}'):
                # Batch and presence.
                assert redact_credentials(text) == (text, []), text
                assert contains_credential(text) is False, text
                # Stream, at every chunk boundary.
                for cut in range(1, len(text)):
                    redactor = StreamRedactor()
                    streamed = redactor.feed(text[:cut]) + redactor.feed(text[cut:])
                    assert streamed + redactor.flush() == text, (text, cut)
                # Flatten, at every cut of the flat text.
                flat = _flattened_for_any_cut(text, _default_redactor)
                assert tag in flat, (text, flat)
                for cut in range(len(flat) + 1):
                    pieces = [flat[:cut], flat[cut:]]
                    assert not _shows_a_key(pieces), (text, cut, pieces)
                    assert not _rejoins_a_key(pieces, _default_redactor), (text, cut, pieces)


@pytest.mark.parametrize(("key", "value"), _TAG_VALUED_FIXTURES, ids=_ids(_TAG_VALUED_FIXTURES))
def test_a_doubled_quote_inside_a_quoted_value_is_an_escaped_quote_not_a_close(
    key: str, value: str
) -> None:
    """YAML and SQL spell a quote inside a single-quoted scalar as ``''``, CSV one
    inside a double-quoted field as ``""``. Reading the first of the pair as the
    close let the redactor's own first pass over ``key='<s1>''<s2>'`` emit
    ``key='[REDACTED: credential]''<s2>'``, and the second run then met a tag
    that fills a closed quoted value, skipped it in silence and left ``<s2>``
    standing with no warning -- the egress gate read it clean. The doubled quote
    is two value bytes: the whole scalar is one claim on the first run, the
    output is a closed pair, and every presence-only reader agrees with pass 1."""
    from kiro_crew.security import contains_credential

    for quote in ("'", '"'):
        pair = quote + quote
        # The redactor's own first pass over a scalar with an escaped quote.
        source = f"{key}={quote}part-one-{value}{pair}part-two-{value}{quote} # note"
        once, warnings = redact_credentials(source)
        assert once == f"{key}={quote}{TAG}{quote} # note", (quote, once)
        assert len(warnings) == 1, (quote, warnings)
        assert redact_credentials(once) == (once, []), (quote, once)
        assert contains_credential(source) is True and contains_credential(once) is False

        # The reviewed shape: a tag heading the scalar, the secret's tail behind
        # the doubled quote. The scalar is the value, so it is redacted whole
        # and warned, and the tail never stands behind a skipped tag.
        reviewed = f"msg {key}={quote}{TAG}{pair}{value}{quote}"
        redacted, warnings = redact_credentials(reviewed)
        assert value not in redacted, (quote, redacted)
        assert redacted == f"msg {key}={quote}{TAG}{quote}", (quote, redacted)
        assert len(warnings) == 1, (quote, warnings)
        assert contains_credential(reviewed) is True, (quote, reviewed)
        assert redact_credentials(redacted) == (redacted, [])

        # A tag that fills its closed quoted value, followed by a NEW quoted
        # string after a separator, is still a fixed point: nothing doubled.
        closed = f"{key}={quote}{TAG}{quote}, next={quote}plain{quote}"
        assert redact_credentials(closed) == (closed, []), (quote, closed)


@pytest.mark.parametrize(("key", "value"), KEY_ANCHORED_FIXTURES, ids=_ids(KEY_ANCHORED_FIXTURES))
def test_a_pair_embedded_in_an_enclosing_string_literal_is_read_through_its_escaping(
    key: str, value: str
) -> None:
    """A pair stored through ``json.dumps`` -- the ledger's write path, persisted
    history, a serialized log -- carries its quotes ESCAPED: the redacted pair
    reads ``key=\\"[REDACTED: credential]\\"`` on the line. The label quote may be
    written escaped, a backslash is never a value byte, and the close is read in
    the opener's encoding (``_quoted_value_end``), so the live pair embedded that
    way is redacted whole to ``key=\\"[TAG]\\"`` inside its literal (the document
    still parses, and parses to the pair's own redaction), that output is a
    fixed point, and every presence-only reader calls it clean while calling the
    live line live. The stream emits the redacted shape byte-identical across
    every chunk boundary.

    Red on the head whose label rule knew only a bare quote and whose value
    class admitted a backslash: the one-byte ``\\`` read as the value, so
    ``contains_credential`` called the redactor's own output live (the ledger
    push gate then refused every later push, with "remove the entries by hand"
    the only way out), and a second ``redact_credentials`` tagged the backslash
    and un-escaped the quote, breaking the enclosing JSON document -- the
    Files-view symptom this span rule exists to fix, one level down."""
    from kiro_crew.security import StreamRedactor

    secret = _SECRET_OF[value]
    for inner in (
        f'{key}="{value}"',
        f"{key}='{value}'",
        f'"{key}": "{value}"',
        f'{key}: "{value}" # note',
    ):
        literal = json.dumps({"text": inner, "r": "x"})
        if '"' in inner:
            assert "\\" in literal, literal  # the quotes are escaped inside the literal

        once, warnings = redact_credentials(literal)

        assert secret not in once, (literal, once)
        assert len(warnings) == 1, (literal, warnings)
        # The document still parses, and the embedded text reads as its own redaction.
        assert json.loads(once) == {"text": redact_credentials(inner)[0], "r": "x"}, (literal, once)
        assert redact_credentials(once) == (once, []), once
        for name, reader in _presence_readers():
            if name == "_HARD_CREDENTIAL_RE" and key == "Authorization":
                continue  # the hard URL regex has no Bearer branch; nothing to clean
            assert reader(literal) is True, (name, literal)
            assert reader(once) is False, (name, once)
        # The stream: the redacted shape crosses every chunk boundary unchanged.
        for cut in range(1, len(once)):
            redactor = StreamRedactor()
            streamed = redactor.feed(once[:cut]) + redactor.feed(once[cut:])
            assert streamed + redactor.flush() == once, (once, cut)

    # The reviewed line, verbatim in shape: a ledger entry whose text the write
    # path already redacted, as `json.dumps` stores it.
    entry = json.dumps({"id": "INV-1", "text": f'{key}="{TAG}"'}) if " " not in value else None
    if entry is not None:
        assert redact_credentials(entry) == (entry, []), entry
        for name, reader in _presence_readers():
            assert reader(entry) is False, (name, entry)


@pytest.mark.parametrize(("key", "value"), _TAG_VALUED_FIXTURES, ids=_ids(_TAG_VALUED_FIXTURES))
def test_an_escaped_value_byte_is_part_of_the_value(key: str, value: str) -> None:
    """An escape pair inside a value whose escaped byte is not a quote or
    whitespace -- the ``\\/`` PHP's ``json_encode`` and several Java serializers
    write for every ``/`` of a base64 secret, an escaped backslash, a ``\\u``
    escape -- is part of the VALUE: the pair is redacted whole, unquoted and
    quoted, standing in its own document and embedded in an enclosing literal,
    whether the escape heads the value or sits inside it. Only an escaped QUOTE
    ends a value, because that is the close of a pair written inside a string
    literal. Every presence-only reader calls the live line live and the
    redaction clean, the chat mirror's hard floor matches the live line, and the
    stream equals the batch pass at every chunk boundary.

    Red on the head whose value class stopped at every backslash: a value made
    of ``\\/``-separated runs streamed through ``redact_credentials`` with the
    class run ending at the first escape, so ``aws_secret_access_key=<s1>\\/<s2>``
    kept ``\\/<s2>`` raw, a value HEADING with ``\\/`` -- one secret in 64 begins
    with ``/`` -- matched nothing at all and ``contains_credential`` said False
    (GPT 6.1 and Opus 5.5, `redaction.py:133`). The base commit redacted both."""
    from kiro_crew.security import StreamRedactor
    from kiro_crew.security.exfil import hard_credential_hit

    secret = _SECRET_OF[value]
    head, tail = secret[:12], secret[12:]
    escaped_values = (
        f"\\/{head}\\/{tail}",  # heading and interior escaped slashes
        f"{head}\\/{tail}\\/",  # trailing one
        f"{head}\\\\{tail}",  # an escaped backslash
        f"{head}\\u002f{tail}",  # a unicode escape
    )
    for v in escaped_values:
        for text in (
            f"{key}={v}\nnext: line\n",
            f"{key}: {v}\nnext: line\n",
            f'{key}="{v}"\nnext: line\n',
            f"{key}='{v}'\nnext: line\n",
            '{"' + key + '": "' + v + '", "r": "x"}\n',
        ):
            once, warnings = redact_credentials(text)
            assert head not in once and tail not in once, (text, once)
            assert "\\/" not in once and "\\u002f" not in once, (text, once)
            assert len(warnings) == 1, (text, warnings)
            assert redact_credentials(once) == (once, []), once
            if text.startswith("{"):
                assert json.loads(once) == {key: TAG, "r": "x"}, once
            for name, reader in _presence_readers():
                if name == "_HARD_CREDENTIAL_RE" and key == "Authorization":
                    continue  # the hard URL regex has no Bearer branch
                assert reader(text) is True, (name, text)
                assert reader(once) is False, (name, once)
            for size in (1, 3, 7, 50):
                redactor = StreamRedactor()
                streamed = "".join(
                    redactor.feed(text[i : i + size]) for i in range(0, len(text), size)
                )
                assert streamed + redactor.flush() == once, (text, size)

        # Embedded in an enclosing literal: the pair's quotes are escaped and the
        # value's own escapes are escaped once more; the close is still the
        # escaped quote, never the pair inside the value.
        for inner in (f'{key}="{v}"', f'"{key}": "{v}"'):
            literal = json.dumps({"text": inner, "r": "x"})
            once, warnings = redact_credentials(literal)
            assert head not in once and tail not in once, (literal, once)
            assert len(warnings) == 1, (literal, warnings)
            assert json.loads(once) == {"text": redact_credentials(inner)[0], "r": "x"}, once
            assert redact_credentials(once) == (once, []), once

    if key != "Authorization":
        # The reviewers' own reproductions, verbatim in shape.
        php_json = '{"' + key + '": "\\/abcdEFGH1234\\/wxyzABCD5678\\/qrstUVWX90ab"}'
        once, warnings = redact_credentials(php_json)
        assert "abcdEFGH1234" not in once and warnings, (php_json, once)
        assert json.loads(once) == {key: TAG}, once
        unquoted = f"{key}=wJalrXUt\\/K7MDENG\\/bPxRfiCYEXAMPLEKEY"
        once, warnings = redact_credentials(unquoted)
        assert once == f"{key}={TAG}", once
        assert hard_credential_hit(php_json) is True, php_json
        assert hard_credential_hit(unquoted) is True, unquoted
        # A tag with escaped bytes glued to it is a value to the hard floor as to
        # the redactor; a tag closed by the escaped quote of its literal is not.
        assert hard_credential_hit(f"{key}={TAG}\\/{head}") is True, key
        assert hard_credential_hit(json.dumps({"t": f'{key}="{TAG}"'})) is False, key


@pytest.mark.parametrize(("key", "value"), _TAG_VALUED_FIXTURES, ids=_ids(_TAG_VALUED_FIXTURES))
def test_escaped_whitespace_heading_a_value_does_not_hide_it(key: str, value: str) -> None:
    """An escaped whitespace pair at the HEAD of a value -- ``\\n``, ``\\r``, ``\\t``,
    ``\\f``, ``\\v``, the way a serializer writes a value that begins with a line
    break -- is leading whitespace in the value's encoding, consumed with the
    value, never a byte that ends it before it began: the pair is redacted whole
    to ``key="[REDACTED: credential]"`` with no stray backslash, quoted and
    unquoted; the hard URL floor matches it, so a JSON document carrying that
    value percent-encoded into a URL path is a credential-bearing URL and is
    warned; every presence reader calls the live text live and the redaction
    clean; and the stream equals the batch pass at every chunk size.

    Red on the head whose value atom excluded escaped whitespace with nothing
    admitting it at the head: ``json.dumps({"aws_secret_access_key": "\\n" +
    secret})`` percent-encoded into a URL path passed ``redact_exfiltration_urls``
    and ``redact`` unchanged with no warning -- the URL's author chooses its
    encoding -- while the base's class caught it (GPT 6.1, `redaction.py:143`)."""
    from urllib.parse import quote

    from kiro_crew.security import StreamRedactor, redact, redact_exfiltration_urls
    from kiro_crew.security.exfil import hard_credential_hit

    secret = _SECRET_OF[value]
    for head in ("\\n", "\\t", "\\r", "\\n\\t"):
        quoted = f'{key}="{head}{value}"\nnext: line\n'
        unquoted = f"{key}={head}{value}\nnext: line\n"
        doc = json.dumps(
            {key: f"{head}{value}".replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")}
        )
        assert "\\n" in doc or "\\t" in doc or "\\r" in doc, doc  # the serializer writes the escape
        for text in (quoted, unquoted, doc):
            once, warnings = redact_credentials(text)
            assert secret not in once and "\\" + "[" not in once, (text, once)
            assert len(warnings) == 1, (text, warnings)
            assert redact_credentials(once) == (once, []), once
            for name, reader in _presence_readers():
                if name == "_HARD_CREDENTIAL_RE" and key == "Authorization":
                    continue  # the hard URL regex has no Bearer branch
                assert reader(text) is True, (name, text)
                assert reader(once) is False, (name, once)
            for size in (1, 3, 7, 50):
                redactor = StreamRedactor()
                streamed = "".join(
                    redactor.feed(text[i : i + size]) for i in range(0, len(text), size)
                )
                assert streamed + redactor.flush() == once, (text, size)
        assert json.loads(redact_credentials(doc)[0]) == {key: TAG}, doc
        assert redact_credentials(quoted)[0] == f'{key}="{TAG}"\nnext: line\n', quoted

        if key != "Authorization":
            assert hard_credential_hit(doc) is True, doc
            url = "https://safe.example/" + quote(doc, safe="")
            redacted, warnings = redact_exfiltration_urls(f"see {url} now")
            assert secret not in redacted and quote(secret, safe="") not in redacted, redacted
            assert warnings, redacted
            shown = redact(f"see {url} now")
            assert secret not in shown and quote(secret, safe="") not in shown, shown


@pytest.mark.parametrize(("key", "value"), _TAG_VALUED_FIXTURES, ids=_ids(_TAG_VALUED_FIXTURES))
def test_any_number_of_escaped_whitespace_heads_is_consumed_with_the_value(
    key: str, value: str
) -> None:
    """The head of escaped whitespace a value may carry is UNBOUNDED: the value
    scanner walks it one token at a time, so nine, forty or four hundred escapes
    before the first value byte are leading whitespace consumed with the value --
    in batch, on the hard URL floor, through ``redact`` over a percent-encoded URL
    path, on every presence reader, and on the stream at every chunk size.

    Red on the head that admitted the head as a regex atom with a cap of eight
    (``(?:\\\\[nrtfv]){0,8}``): a JSON ``SecretAccessKey`` value with nine leading
    tab escapes, percent-encoded into a URL path, passed ``redact`` unchanged with
    no warning while eight were caught -- the URL's author chooses its encoding,
    so a fixed cap is a step the author takes (GPT 6.1, `redaction.py:158`)."""
    from urllib.parse import quote

    from kiro_crew.security import StreamRedactor, redact, redact_exfiltration_urls

    secret = _SECRET_OF[value]
    for n in (9, 40, 400):
        head = "\\t" * n
        for text in (f'{key}="{head}{value}"\nnext: line\n', f"{key}={head}{value}\nnext: line\n"):
            once, warnings = redact_credentials(text)
            assert secret not in once and "\\t" not in once, (n, text[:60], once[:80])
            assert len(warnings) == 1, (n, warnings)
            assert redact_credentials(once) == (once, []), once
            for name, reader in _presence_readers():
                if name == "_HARD_CREDENTIAL_RE" and key == "Authorization":
                    continue  # the hard URL regex has no Bearer branch
                assert reader(text) is True, (name, n)
                assert reader(once) is False, (name, n)
            for size in (1, 7, 50):
                if n * 2 >= 512:
                    break  # a head past the stream's holdback cap is the long-value shape the stream does not hold
                redactor = StreamRedactor()
                streamed = "".join(
                    redactor.feed(text[i : i + size]) for i in range(0, len(text), size)
                )
                assert streamed + redactor.flush() == once, (n, size)
        if key != "Authorization":
            doc = json.dumps({key: "\t" * n + value})
            url = "https://safe.example/" + quote(doc, safe="")
            redacted, warnings = redact_exfiltration_urls(f"see {url} now")
            assert secret not in redacted and quote(secret, safe="") not in redacted, (
                n,
                redacted[:100],
            )
            assert warnings, (n, redacted[:100])
            shown = redact(f"see {url} now")
            assert secret not in shown and quote(secret, safe="") not in shown, (n, shown[:100])


@pytest.mark.parametrize(("key", "value"), _TAG_VALUED_FIXTURES, ids=_ids(_TAG_VALUED_FIXTURES))
def test_an_embedded_quote_that_never_closes_ends_at_its_literal(key: str, value: str) -> None:
    """Inside an enclosing literal the inner line ends where the literal does --
    its own BARE quote -- or at an escaped line break, and the close written is
    the opener as written (``\\"``), so the enclosing document still parses and
    the output is a fixed point. A doubled escaped quote (``\\"\\"``) is an
    interior quote, as its bare twin is."""
    from kiro_crew.security import contains_credential

    unterminated = json.dumps({"text": f'{key}="{value} tail'})
    once, warnings = redact_credentials(unterminated)
    assert value not in once and "tail" not in once, once
    assert len(warnings) == 1, warnings
    assert json.loads(once) == {"text": f'{key}="{TAG}"'}, once
    assert redact_credentials(once) == (once, []), once
    assert contains_credential(once) is False, once

    folded = json.dumps({"text": f'{key}="{value} tail\nnext: line'})
    once, warnings = redact_credentials(folded)
    assert value not in once and len(warnings) == 1, (once, warnings)
    assert json.loads(once) == {"text": f'{key}="{TAG}"\nnext: line'}, once
    assert redact_credentials(once) == (once, []), once

    doubled = json.dumps({"text": f'{key}="part-{value}""{value}" # note'})
    once, warnings = redact_credentials(doubled)
    assert value not in once and len(warnings) == 1, (once, warnings)
    assert json.loads(once) == {"text": f'{key}="{TAG}" # note'}, once
    assert redact_credentials(once) == (once, []), once


@pytest.mark.parametrize(("key", "value"), _TAG_VALUED_FIXTURES, ids=_ids(_TAG_VALUED_FIXTURES))
def test_a_tag_heading_a_quoted_value_is_live_to_the_hard_url_floor(key: str, value: str) -> None:
    """``_HARD_CREDENTIAL_RE`` -- the URL path+query floor, case-insensitive, read
    presence-only on every decode layer of a URL -- exempts a tag run only where it
    FILLS the value: behind an opening quote that means the closing quote follows
    the run. A tag that merely heads a quoted value is a value to it as to the
    redactor, which claims that string through its quote and warns.

    Red on the head whose exemption used the unquoted boundary class behind a
    quote too: ``secretaccesskey="[REDACTED: credential] <secret>"`` was exempt at
    the tag's following space, and percent-encoded into a URL's PATH under that
    lower-case key -- which the case-sensitive canonical branches do not read, and
    whose path-only payload the entropy checks never scan -- the credential-bearing
    URL reached display unchanged (GPT 6.1 finding, `exfil.py`)."""
    from urllib.parse import quote

    from kiro_crew.security import redact_exfiltration_urls
    from kiro_crew.security.exfil import hard_credential_hit

    for spelling in (key, key.lower(), key.upper()):
        heading = f'{spelling}="{TAG} {value}"'
        assert hard_credential_hit(heading) is True, heading
        assert hard_credential_hit(heading.replace('"', "'")) is True, heading
        embedded = json.dumps({"text": heading})
        assert hard_credential_hit(embedded) is True, embedded
        for filled in (
            f'{spelling}="{TAG}"',
            f"{spelling}='{TAG}{TAG}' tail",
            json.dumps({"t": f'{spelling}="{TAG}"'}),
        ):
            assert hard_credential_hit(filled) is False, filled
        assert hard_credential_hit(f"{spelling}={TAG} tail") is False, spelling
        assert hard_credential_hit(f"{spelling}={TAG}{value}") is True, spelling

        url = "https://safe.example/" + quote(heading, safe="")
        redacted, warnings = redact_exfiltration_urls(f"see {url} now")
        assert value not in redacted and quote(value, safe="") not in redacted, redacted
        assert "safe.example" not in redacted.replace(
            "[REDACTED: suspicious URL to safe.example]", ""
        ), redacted
        assert warnings, redacted

        clean_url = "https://safe.example/" + quote(f'{spelling}="{TAG}"', safe="")
        assert redact_exfiltration_urls(f"see {clean_url} now") == (
            f"see {clean_url} now",
            [],
        ), clean_url


@pytest.mark.parametrize(("key", "value"), _TAG_VALUED_FIXTURES, ids=_ids(_TAG_VALUED_FIXTURES))
def test_a_doubled_quote_behind_a_quoted_tag_run_is_interior_to_the_hard_url_floor(
    key: str, value: str
) -> None:
    """The hard URL floor reads a DOUBLED quote as the redactor does: an escaped
    interior quote, never the close. ``key='[REDACTED: credential]''<secret>'`` is
    one quoted scalar whose value runs past the tag, so the tag does not fill it
    and the floor matches -- in both quote kinds, in the escaped encoding of a pair
    inside a JSON string (``\\"\\"``), and percent-encoded into a URL path under a
    lower-case key, where the canonical branches do not read and only this floor
    stands between the credential and display.

    Red on the head whose quoted exemption closed at the first quote of the pair:
    the backreference accepted it, the exemption held, and the credential-bearing
    URL passed ``redact_exfiltration_urls`` unchanged (GPT 6.1, `exfil.py:703`)."""
    from urllib.parse import quote

    from kiro_crew.security import redact_exfiltration_urls
    from kiro_crew.security.exfil import hard_credential_hit

    for spelling in (key, key.lower(), key.upper()):
        for q in ('"', "'"):
            doubled = f"{spelling}={q}{TAG}{q}{q}{value}{q}"
            assert hard_credential_hit(doubled) is True, doubled
            # The same pair inside a JSON string: the quotes are written escaped,
            # and the doubled form is two escaped quotes.
            embedded = json.dumps({"text": doubled})
            assert hard_credential_hit(embedded) is True, embedded
            # A tag run that fills the value, with the close followed by anything
            # but the same quote, is exempt exactly as before.
            for filled in (
                f"{spelling}={q}{TAG}{q}",
                f"{spelling}={q}{TAG}{TAG}{q},{q}next{q}",
                f"{spelling}={q}{TAG}{q} tail",
                json.dumps({"t": f"{spelling}={q}{TAG}{q}"}),
            ):
                assert hard_credential_hit(filled) is False, filled

            url = "https://safe.example/" + quote(doubled, safe="")
            redacted, warnings = redact_exfiltration_urls(f"see {url} now")
            assert value not in redacted and quote(value, safe="") not in redacted, redacted
            assert warnings, redacted


_SWEEP_CHUNK_SIZES = (1, 3, 7, 50, 200, 513)


def _sweep_shapes(key: str, value: str, *, escaped: bool = True) -> list[tuple[str, str]]:
    """The key-anchored spellings the stream sweep drives: `=`, `:`, JSON, both
    quote kinds, both doubled-quote escapes, the pair embedded in a JSON string,
    and -- for a live value, `escaped` -- the value written with escaped slashes
    (PHP-style JSON), bare and quoted. A registered tag is never written with an
    escape inside it, so the escaped rows are not built for one."""
    shapes = [
        ("eq", f"{key}={value}\nnext: line\n"),
        ("colon", f"{key}: {value}\nnext: line\n"),
        ("json", json.dumps({key: value, "r": "x"}) + "\n"),
        ("dq", f'{key}="{value}"\nnext: line\n'),
        ("sq", f"{key}='{value}'\nnext: line\n"),
        ("dq-doubled", f'{key}="part-{value}""{value}"\nnext: line\n'),
        ("sq-doubled", f"{key}='part-{value}''{value}'\nnext: line\n"),
        ("embedded", json.dumps({"text": f'{key}="{value}"'}) + "\n"),
    ]
    if escaped:
        shapes += [
            ("escaped-slash", f"{key}=\\/{value[:8]}\\/{value[8:]}\nnext: line\n"),
            ("escaped-slash-json", '{"' + key + '": "\\/' + value[:8] + "\\/" + value[8:] + '"}\n'),
            ("escaped-newline-head", '{"' + key + '": "\\n' + value + '"}\n'),
        ]
    return shapes


def _streamed(text: str, size: int) -> str:
    from kiro_crew.security import StreamRedactor

    redactor = StreamRedactor()
    out = "".join(redactor.feed(text[i : i + size]) for i in range(0, len(text), size))
    return out + redactor.flush()


@pytest.mark.parametrize(("key", "value"), _TAG_VALUED_FIXTURES, ids=_ids(_TAG_VALUED_FIXTURES))
def test_the_stream_sweep_every_spelling_at_every_chunk_size_equals_batch(
    key: str, value: str
) -> None:
    """The permanent sweep over the stream path, bounded so it stays fast: for every
    key-anchored spelling (`=`, `:`, JSON, both quote kinds, both doubled-quote
    escapes, embedded in a JSON string, escaped slashes bare and in JSON, an
    escaped line break heading the value), with a live value and with every
    registered tag standing as the value, at chunk sizes 1, 3, 7, 50, 200 and 513,
    the wire output EQUALS the batch redaction of the joined text -- so no secret
    byte is emitted and no spurious byte is written -- and with a short value
    followed by a quoted prose tail longer than the hold-back cap (the shape whose
    floored hold bisected a token) no secret byte is emitted at any size (equality
    is not asserted there: a value past the cap commits at the cut, where the batch
    pass writes the close -- the documented cost). Rounds that fixed one stream
    leak and introduced another are what this sweep exists to end."""
    from kiro_crew.security import CREDENTIAL_REDACTION_TAGS

    secret = _SECRET_OF[value]
    values = [value, *CREDENTIAL_REDACTION_TAGS]
    for v in values:
        for shape, text in _sweep_shapes(key, v, escaped=v == value):
            batch = redact_credentials(text)[0]
            assert secret not in batch, (shape, text)
            for size in _SWEEP_CHUNK_SIZES:
                out = _streamed(text, size)
                assert out == batch, (shape, v[:24], size, out[:120])

    tail = " plus a long description " + "word " * 160  # past _STREAM_HOLDBACK_MAX
    for quote in ('"', "'"):
        text = f"{key}={quote}{value}{tail}{quote} done\n"
        assert secret not in redact_credentials(text)[0]
        for size in _SWEEP_CHUNK_SIZES:
            out = _streamed(text, size)
            assert secret not in out, (quote, size, out[:120])
            assert TAG in out, (quote, size, out[:120])


@pytest.mark.parametrize(("key", "value"), KEY_ANCHORED_FIXTURES, ids=_ids(KEY_ANCHORED_FIXTURES))
def test_a_quoted_value_past_the_holdback_cap_never_leaks_its_token(key: str, value: str) -> None:
    """The key-anchored hold is WEAK, and a weak hold that would exceed the stream's
    hold-back cap is dropped rather than floored: Phase B's floor cuts at
    ``len(buf) - cap`` wherever that lands, inside the label or the token's run
    included, and a token bisected there streams anchor-less. Red on the head that
    floored the hold: `aws_session_token="<token> <600 bytes of prose>"` streamed in
    1- and 7-byte chunks came out with the token RAW, and in 50-byte chunks with the
    token's tail behind the tag. The natural cut never bisects a credential-class
    run, so the label and token commit whole and only the prose tail past the cap
    takes the close the batch pass writes at the cut (the documented cost of a
    quoted value that runs past the cap)."""
    from kiro_crew.security import StreamRedactor

    secret = _SECRET_OF[value]
    tail = " plus a long description " + "word " * 160  # well past _STREAM_HOLDBACK_MAX
    for text in (
        f'{key}="{value}{tail}" done\n',
        f"{key}: '{value}{tail}' done\n",
        f'{{"{key}": "{value}{tail}", "r": "x"}}\n',
    ):
        assert secret not in redact_credentials(text)[0], text
        for size in (1, 7, 50, 200):
            redactor = StreamRedactor()
            out = "".join(redactor.feed(text[i : i + size]) for i in range(0, len(text), size))
            out += redactor.flush()
            assert secret not in out, (key, size, out[:120])
            assert TAG in out, (key, size, out[:120])


@pytest.mark.parametrize(("key", "value"), _TAG_VALUED_FIXTURES, ids=_ids(_TAG_VALUED_FIXTURES))
def test_the_hard_floor_requires_the_same_closing_quote_after_a_tag_run(
    key: str, value: str
) -> None:
    """Behind an opening quote the hard floor exempts a tag run only when the SAME
    quote closes it (the opener is captured, the close is its backreference). Red
    on the head whose quoted exemption took either quote kind: `key="[TAG]'<secret>'`
    was exempt there while the redactor still claims the string through its real
    close, so the artifact-id floor and the packaging scan let a raw secret through."""
    from kiro_crew.security.exfil import hard_credential_hit

    for opener, other in (('"', "'"), ("'", '"')):
        mismatched = f"{key}={opener}{TAG}{other}{value}{other}"
        assert hard_credential_hit(mismatched) is True, mismatched
        assert value not in redact_credentials(mismatched)[0], mismatched
        filled = f"{key}={opener}{TAG}{opener} {other}{value}{other}"
        assert hard_credential_hit(filled) is False, filled
        embedded = json.dumps({"t": f"{key}={opener}{TAG}{opener}"})
        assert hard_credential_hit(embedded) is False, embedded


@pytest.mark.parametrize(("key", "value"), KEY_ANCHORED_FIXTURES, ids=_ids(KEY_ANCHORED_FIXTURES))
def test_every_cut_of_a_key_anchored_pair_streams_as_the_batch_redacts_it(
    key: str, value: str
) -> None:
    """The stream commits up to the last byte outside its credential class, and
    a key-anchored pair has such bytes INSIDE it: the whitespace after the
    separator, and a space or a backslash inside a quoted value. A cut at the
    first committed the label alone and the value then arrived anchor-less and
    streamed RAW (``{"aws_secret_access_key": `` | ``"<secret> tail"}`` -- the
    same leak on the base commit); a cut at the second had the batch pass over
    the committed head write the close mid-value, so the wire read
    ``key="[REDACTED: credential]"tail"``. The stream now holds a pair the cut
    would bisect (``_key_anchored_hold_start``): at EVERY chunk boundary the
    wire reads exactly what the batch redactor writes for the whole text --
    for a bare pair, a value in progress, a quoted value with a tail, a tag
    heading a quoted value that continues, and the pair embedded in an
    enclosing string literal."""
    from kiro_crew.security import StreamRedactor

    secret = _SECRET_OF[value]
    texts = [
        f"{key}: {value}\nnext: line\n",
        f'{key}="{value} tail"\nnext: line\n',
        f'{{"{key}": "{value} tail", "r": "x"}}\n',
        json.dumps({"text": f'{key}="{value}"', "r": "x"}) + "\n",
    ]
    if " " not in value:
        texts.append(f'{{"{key}": "{TAG} text\\"suffix"}}\n')
        texts.append(json.dumps({"text": f'{key}="{TAG} tail"'}) + "\n")
    for text in texts:
        batch = redact_credentials(text)[0]
        assert secret not in batch, text
        for cut in range(1, len(text)):
            redactor = StreamRedactor()
            streamed = redactor.feed(text[:cut]) + redactor.feed(text[cut:]) + redactor.flush()
            assert streamed == batch, (text, cut, streamed)


def test_no_module_outside_security_calls_the_raw_patterns() -> None:
    """``get_credential_patterns()`` returns the RAW alternation, which matches the
    redactor's own ``key=[REDACTED: credential]`` output; a reader that only asks
    whether a pattern matches must go through ``contains_credential`` /
    ``credential_matches`` instead. ``_HARD_CREDENTIAL_RE`` is the hard floor's
    bare MARKER regex -- the labelled AWS forms left it for the scanner, so a
    reader of the regex alone lets ``aws_secret_access_key=<v>`` through (the
    dashboard's provider-id branch did, `test_handlers_artifacts_coverage.py::
    test_external_id_with_a_labelled_secret_is_replaced`); the floor is
    ``hard_credential_hit``. This pin fails when any non-test module outside
    ``security/`` calls the raw accessor or reads the bare regex -- a Python call
    site or attribute read (by ``ast``, so a docstring mention does not count) or
    a call inside a shell script's embedded program (the artifact-deploy scans)."""
    import ast
    from pathlib import Path

    root = Path(_redaction.__file__).resolve().parents[2]  # src/
    assert (root / "kiro_crew" / "security").is_dir(), root
    offenders: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".sh"}:
            continue
        rel = path.relative_to(root).as_posix()
        if (
            rel.startswith("kiro_crew/security/")
            or "/tests/" in f"/{rel}"
            or path.name.startswith("test_")
        ):
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if "get_credential_patterns" not in source and "_HARD_CREDENTIAL_RE" not in source:
            continue
        if path.suffix == ".sh":
            if re.search(r"get_credential_patterns\s*\(|_HARD_CREDENTIAL_RE\b", source):
                offenders.append(rel)
            continue
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name == "get_credential_patterns":
                    offenders.append(f"{rel}:{node.lineno}")
            elif isinstance(node, (ast.Name, ast.Attribute, ast.alias)):
                read = getattr(node, "id", None) or getattr(node, "attr", None) or node.name
                if read == "_HARD_CREDENTIAL_RE":
                    offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], offenders


def test_no_module_outside_security_spells_a_labelled_value_pattern_without_the_tag_rule() -> None:
    """A copy of a key-anchored labelled-value pattern outside ``security/`` is a
    presence-only reader the raw-accessor pin above cannot see: the packaging scan's
    own ``aws-secret-labelled`` pattern matched the ``[REDACTED:`` head of a stored
    skill's redacted value and aborted the crew build. So any non-test module whose
    STRING CONSTANTS spell a key-anchored label (a key spelling, the ``[:=]`` separator
    idiom and a value class) must also carry the tag rule -- read the tag registry
    (``CREDENTIAL_REDACTION_TAGS`` / a ``_REDACTION_TAGS`` fallback) or route through
    ``contains_credential`` / ``credential_matches``. Comments and docstrings that merely
    mention a key do not count; the constants are what compile into a pattern."""
    import ast
    from pathlib import Path

    keys = ("SecretAccessKey", "aws_secret_access_key", "SessionToken", "aws_session_token")
    keys += ("AccessKeyId", "aws_access_key_id")
    root = Path(_redaction.__file__).resolve().parents[2]  # src/
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if (
            rel.startswith("kiro_crew/security/")
            or "/tests/" in f"/{rel}"
            or path.name.startswith("test_")
        ):
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if not any(key in source for key in keys):
            continue
        tree = ast.parse(source, filename=str(path))
        constants = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        spells_a_label = any(key in c for key in keys for c in constants)
        spells_a_value = any("[:=]" in c for c in constants) and any(
            "[^\\s" in c for c in constants
        )
        if not (spells_a_label and spells_a_value):
            continue
        names = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(tree)
            if isinstance(node, (ast.Name, ast.Attribute))
        }
        if not names & {
            "CREDENTIAL_REDACTION_TAGS",
            "_REDACTION_TAGS",
            "contains_credential",
            "credential_matches",
        }:
            offenders.append(rel)
    assert offenders == [], offenders


_PROPERTY_KEYS = tuple(k for k, _ in KEY_ANCHORED_FIXTURES if k != "Authorization")
_PROPERTY_SECRET_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789/+"


@settings(max_examples=400, deadline=None)
@given(
    key=st.sampled_from(_PROPERTY_KEYS),
    secret=st.text(alphabet=_PROPERTY_SECRET_ALPHABET, min_size=16, max_size=36),
    separator=st.sampled_from(["=", ":", " = ", ": "]),
    quote=st.sampled_from(["", '"', "'"]),
    tail=st.sampled_from(["", " tail", " a=b", " doubled", " escaped", " nested"]),
    closes=st.booleans(),
    embedded=st.booleans(),
    trailer=st.sampled_from(["", " # note", "\nnext: line", ', "r": "x"}']),
)
def test_a_second_pass_over_any_generated_pair_is_silent_and_identical(
    key: str,
    secret: str,
    separator: str,
    quote: str,
    tail: str,
    closes: bool,
    embedded: bool,
    trailer: str,
) -> None:
    """The quote rule is ONE bound -- a quoted value is claimed to its closing
    quote on the line, read in the opener's encoding; a quote that never closes
    claims the line and writes its close -- and this is its property, over
    generated shapes rather than the reviewed instances: for every key spelling,
    separator, quote style (none, ``"``, ``'``), tail (none, prose, a nested
    pair, a doubled quote, a backslash-escaped quote), closed or unterminated,
    bare or embedded in an enclosing string literal, with or without trailing
    text: the first pass removes every secret byte and warns, and the second
    pass is byte-identical and silent, so no non-tag byte is left unwarned
    inside a claimed span and no surface that re-runs the redactor over its own
    output (persisted history, the ledger gate, the Files view, the chat mirror)
    ever refuses or rewrites what the first pass wrote."""
    if tail == " doubled":
        tail = f" x{quote}{quote}y" if quote else " x''y"
    elif tail == " escaped":
        tail = f" a\\{quote}b" if quote else " a\\b"
    elif tail == " nested":
        tail = f" {quote}inner{quote}" if quote else " inner"
    close = quote if closes else ""
    inner = f"{key}{separator}{quote}{secret}{tail}{close}{trailer}"
    text = json.dumps({"text": inner}) if embedded else inner

    once, warnings = redact_credentials(text)

    assert secret not in once, (text, once)
    assert warnings, (text, once)
    twice, again = redact_credentials(once)
    assert twice == once and again == [], (text, once, twice, again)


# ─────────────────────────────────────────────────────────────────────────────
# the loops over one text's matches are linear in the text
# ─────────────────────────────────────────────────────────────────────────────

_REPEATED_KEY = "SecretAccessKey="


def _repeated_key_text(n: int) -> str:
    """A key repeated as its own value, ``n`` times, then an opaque tail: every
    anchor after the first begins inside the value the first one claims."""
    return _REPEATED_KEY * n + "opaque"


def test_pass1_is_linear_on_a_key_repeated_as_its_own_value() -> None:
    """A watched file of 5 000 repeated keys (80 KB) spent 73 s in pass 1 --
    every anchor after the first began inside the first value's run and rescanned
    the run to its end before the coverage check, past the 25 s watchdog budget
    of the loop that runs ``redact_credentials`` synchronously. The loop reads
    through ``_KeyedValueScans``, which answers an anchor inside the last unquoted
    run from that run's end; measured as executed lines of ``redaction``, which
    a loaded runner or coverage's tracer cannot change: the rescan was 4x per
    doubling, the memo 2x. The output is one tag, the key kept, as before."""
    text = _repeated_key_text(200)
    result, warnings = redact_credentials(text)
    assert result == f"{_REPEATED_KEY}{TAG}"
    assert len(warnings) == 1
    assert_linear_work(_redaction, _repeated_key_text, redact_credentials)


def test_the_stream_hold_is_linear_on_a_key_repeated_as_its_own_value() -> None:
    """The hold's walk over the anchors before a cut is the same loop: it read
    every nested anchor's run afresh AND sliced the text from each value's start
    to test a strict tag prefix -- one copy of the remaining text per anchor.
    It reads through the memo and tests the remaining length before slicing."""
    text = _repeated_key_text(200)
    assert _redaction._key_anchored_hold_start(text, len(text) - 3) == 0
    assert_linear_work(
        _redaction,
        _repeated_key_text,
        lambda t: _redaction._key_anchored_hold_start(t, len(t) - 3),
    )


def test_credential_matches_is_linear_on_a_key_repeated_as_its_own_value() -> None:
    """A per-match reader (a line number, a masked snippet) iterates every live
    match; the liveness test read each nested anchor's run afresh too."""
    text = _repeated_key_text(200)
    assert len(list(_redaction.credential_matches(text))) == 200
    assert_linear_work(
        _redaction, _repeated_key_text, lambda t: list(_redaction.credential_matches(t))
    )


_MEMO_FRAGMENTS = (
    "SecretAccessKey=",
    "aws_session_token:",
    '"AccessKeyId": ',
    "x",
    "ab",
    " ",
    "\n",
    '"',
    "'",
    '\\"',
    "\\/",
    "\\n",
    "\\",
    ",",
    "}",
    TAG,
    "Authorization: Bearer abcdefghijklmnop",
)


@settings(max_examples=600, deadline=None)
@given(fragments=st.lists(st.sampled_from(_MEMO_FRAGMENTS), min_size=1, max_size=14))
def test_the_memo_answers_every_match_as_a_fresh_scan_does(fragments: list[str]) -> None:
    """``_KeyedValueScans`` is a shortcut, not a rule: for every match of a text
    assembled from keys, separators, both quote kinds, escapes, whitespace, the
    structural terminators, a tag and a Bearer header, its answer -- start, end,
    ``closes``, ``opener``, ``pending`` -- equals ``_keyed_value_of``'s for that
    match, read fresh. The two can only differ if a bare scan from an anchor
    inside an unquoted run could end somewhere other than the run's end, which
    the boundary argument in the class docstring says it cannot."""
    text = "".join(fragments)
    scans = _redaction._KeyedValueScans()
    for match in _redaction._credential_matches(text):
        assert scans.value_of(text, match) == _redaction._keyed_value_of(text, match), (
            text,
            match.group(),
            match.span(),
        )


# ─────────────────────────────────────────────────────────────────────────────
# a bare-quoted value inside an enclosing literal of the other quote kind
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            '{"text":"SecretAccessKey=\'wJalrXUtnFEMI/K7MDENG","keep":1}',
            '{"text":"SecretAccessKey=\'[REDACTED: credential]\'","keep":1}',
            id="json-dq-literal-sq-value",
        ),
        pytest.param(
            'text: "aws_secret_access_key=\'wJalrXUtnFEMI/K7MDENG"\nkeep: 1\n',
            "text: \"aws_secret_access_key='[REDACTED: credential]'\"\nkeep: 1\n",
            id="yaml-dq-literal-sq-value-line-end",
        ),
        pytest.param(
            "text: 'SessionToken=\"FQoGZXIvYXdzEBYaDFexample', keep: 1\n",
            "text: 'SessionToken=\"[REDACTED: credential]\"', keep: 1\n",
            id="yaml-sq-literal-dq-value",
        ),
        pytest.param(
            '["SecretAccessKey=\'wJalrXUtnFEMI/K7MDENG"]',
            "[\"SecretAccessKey='[REDACTED: credential]'\"]",
            id="json-array-bracket-follower",
        ),
    ],
)
def test_a_bare_quoted_value_stops_at_the_close_of_an_enclosing_literal(
    text: str, expected: str
) -> None:
    """A value opened by a BARE quote inside a literal of the OTHER quote kind
    (`{"text":"SecretAccessKey='x","keep":1}` -- JSON does not escape a single
    quote, so the inner opener is bare): the enclosing literal's close is a quote
    of the other kind followed by a structural byte (`,` `}` `]` `)` `:` `;`,
    whitespace, a line break), and the inner value cannot run past it -- the scan
    that read the close as an interior byte claimed `x","keep":1}` to the line's
    end and returned malformed JSON with the sibling field deleted, where the
    base kept both. The inner line ends there: the claim stops before the close
    and writes the opener as its own close, so the document still parses, the
    sibling survives, and the output is a fixed point."""
    out, warnings = redact_credentials(text)
    assert out == expected
    assert len(warnings) == 1
    if text.lstrip().startswith(("{", "[")):
        json.loads(out)
    assert redact_credentials(out) == (out, [])


def test_an_other_kind_quote_followed_by_a_value_byte_stays_interior() -> None:
    """The follower decides: a quote of the other kind with a value byte after it
    is interior (`key="it's"` keeps `s` inside the claim), and the tag-then-other-
    quote shape is still claimed whole with its secret."""
    out, warnings = redact_credentials('aws_secret_access_key="it\'s-not-a-credential-0123"\n')
    assert out == 'aws_secret_access_key="[REDACTED: credential]"\n'
    assert warnings == ["Redacted credential pattern (26 chars)"]
    out2, _ = redact_credentials(
        f"aws_secret_access_key=\"{TAG}'test-value-not-a-credential-0123'\"\n"
    )
    assert out2 == 'aws_secret_access_key="[REDACTED: credential]"\n'


@pytest.mark.parametrize(
    "text, expected",
    [
        pytest.param(
            '{"SessionToken": " note\' suffix", "keep": 1}',
            '{"SessionToken": "[REDACTED: credential]", "keep": 1}',
            id="json-apostrophe-then-space",
        ),
        pytest.param(
            '{"SecretAccessKey":"a\',b", "keep": 1}',
            '{"SecretAccessKey":"[REDACTED: credential]", "keep": 1}',
            id="json-apostrophe-then-comma",
        ),
        pytest.param(
            "aws_secret_access_key=\"don' t won' t\"\nnext: line\n",
            'aws_secret_access_key="[REDACTED: credential]"\nnext: line\n',
            id="two-apostrophes-before-whitespace",
        ),
        pytest.param(
            "aws_session_token=\"rock 'n' roll\"\n",
            'aws_session_token="[REDACTED: credential]"\n',
            id="quoted-word-inside-the-value",
        ),
        pytest.param(
            '{"text":"aws_secret_access_key=\\" note\' suffix\\"","keep":1}',
            '{"text":"aws_secret_access_key=\\"[REDACTED: credential]\\"","keep":1}',
            id="escaped-encoding-twin",
        ),
    ],
)
def test_an_apostrophe_inside_a_double_quoted_value_is_a_byte_of_the_value(
    text: str, expected: str
) -> None:
    """A `"`-opened value with an apostrophe before whitespace or punctuation
    inside it (`{"SessionToken": " note' suffix", "keep": 1}`) is one value to
    its own close. Reading the apostrophe as the close of a literal enclosing
    the pair cuts the claim there and writes the close, so the JSON view reads
    `"[REDACTED: credential]"' suffix", "keep": 1}` and does not parse, while the
    unredacted document does. No format closes a `"` string with `'`, and JSON
    has no `'` strings at
    all, so inside a `"`-opened value an apostrophe is a byte of the value while
    the value's own close is still to come on its line; the `'`-opened side keeps
    the rule (`{"text":"key='<v>","keep":1}`: JSON never escapes a `'`, so the
    bare `"` after the value is the enclosing string's end). In the escaped
    encoding the enclosing literal is the one that escaped the opener, and ends
    at its own bare quote. Every cut of the stream reproduces the batch answer:
    the hold waits for the close instead of releasing at the apostrophe."""
    from kiro_crew.security import StreamRedactor

    out, warnings = redact_credentials(text)
    assert out == expected
    assert len(warnings) == 1
    if text.lstrip().startswith("{"):
        json.loads(out)
    assert redact_credentials(out) == (out, [])
    for cut in range(1, len(text)):
        redactor = StreamRedactor()
        streamed = redactor.feed(text[:cut]) + redactor.feed(text[cut:]) + redactor.flush()
        assert streamed == expected, (cut, text[:cut])


def test_a_single_quoted_literal_enclosing_an_unterminated_double_quoted_pair_still_ends_at_its_close() -> (
    None
):
    """The other orientation stays: a `"`-opened value that never closes on its
    line inside a `'` literal (YAML, the shell) ends at the `'` followed by a
    structural byte, so the sibling key survives and the opener is written as
    the close; followed by a value byte, or with the value's own close still
    ahead, the `'` is interior."""
    out, warnings = redact_credentials(
        "text: 'aws_secret_access_key=\"test-value-not-a-credential-0123', keep: 1\n"
    )
    assert out == "text: 'aws_secret_access_key=\"[REDACTED: credential]\"', keep: 1\n"
    assert len(warnings) == 1
    assert redact_credentials(out) == (out, [])


# ─────────────────────────────────────────────────────────────────────────────
# the look-back: the scanner knows which literal encloses the key
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('{"template":"aws_secret_access_key=","keep":1}', id="json-compact"),
        pytest.param('{"template": "export SecretAccessKey=", "keep": 1}', id="json-spaced"),
        pytest.param('{"template":"aws_session_token: ","keep":1}', id="json-colon"),
        pytest.param('["aws_secret_access_key=","keep"]', id="json-array"),
        pytest.param(
            json.dumps({"template": '{"template":"aws_secret_access_key=","keep":1}'}),
            id="json-inside-json",
        ),
    ],
)
def test_an_empty_assignment_before_the_enclosing_close_is_left_alone(text: str) -> None:
    """An EMPTY assignment whose next byte is the enclosing JSON string's closing
    quote (`{"template":"aws_secret_access_key=","keep":1}`): reading that quote
    as the value's opener runs the claim into the next field and returns
    `"aws_secret_access_key="[REDACTED: credential]"keep":1}`, which does not
    parse, while the unredacted document does. The scanner reads the line back
    from its start and knows the key stands inside a `"` literal, so a `"` where
    the value would open is that literal's close, the assignment is empty and
    nothing is claimed -- one level down too, where the inner literal's `\\"`
    is the close. Every stream cut agrees with the batch pass."""
    from kiro_crew.security import StreamRedactor

    out, warnings = redact_credentials(text)
    assert out == text
    assert warnings == []
    json.loads(out)
    for cut in range(1, len(text)):
        redactor = StreamRedactor()
        assert redactor.feed(text[:cut]) + redactor.feed(text[cut:]) + redactor.flush() == text, cut


@pytest.mark.parametrize(
    "text, expected",
    [
        pytest.param(
            '{"text": "\\"SecretAccessKey\\": \\"x\\\\", "keep": 1}',
            '{"text": "\\"SecretAccessKey\\": \\"[REDACTED: credential]\\"", "keep": 1}',
            id="finding-verbatim",
        ),
        pytest.param(
            '{"text": "\\"aws_secret_access_key\\": \\"test-value-not-a-credential-0123\\\\", "keep": 1}',
            '{"text": "\\"aws_secret_access_key\\": \\"[REDACTED: credential]\\"", "keep": 1}',
            id="one-inner-backslash",
        ),
        pytest.param(
            '{"text": "\\"aws_secret_access_key\\": \\"test-value-not-a-credential-0123\\\\\\\\\\\\", "keep": 1}',
            '{"text": "\\"aws_secret_access_key\\": \\"[REDACTED: credential]\\"", "keep": 1}',
            id="three-inner-backslashes",
        ),
        pytest.param(
            '{"text": "\\"aws_secret_access_key\\": \\"a\\\\\\"b\\\\", "keep": 1}',
            '{"text": "\\"aws_secret_access_key\\": \\"[REDACTED: credential]\\"", "keep": 1}',
            id="escaped-quote-then-inner-backslash",
        ),
        pytest.param(
            '{"text": "aws_secret_access_key=\\"test-value-not-a-credential-0123\\\\", "keep": 1}',
            '{"text": "aws_secret_access_key=\\"[REDACTED: credential]\\"", "keep": 1}',
            id="eq-separator",
        ),
    ],
)
def test_an_inner_backslash_before_the_enclosing_close_keeps_the_document(
    text: str, expected: str
) -> None:
    """A value in the escaped encoding whose LAST inner token is a backslash,
    followed by the enclosing JSON string's real closing quote
    (`{"text": "\\"SecretAccessKey\\": \\"x\\\\", "keep": 1}`): `\\\\` is one inner
    backslash, a complete escape pair of the enclosing encoding, so the bare `"`
    after it is unescaped there and is the literal's close. Reading `\\"` as an
    escaped quote took the close with it, the claim ran across the field
    separator and the output stopped parsing while the input parsed. Escapes
    pair up left to right at the value's own depth, so the inner line ends at
    the close with the value unterminated, the claim writes its own close, and
    the document keeps its sibling field. Every stream cut agrees with the batch
    pass, and the output is a fixed point."""
    from kiro_crew.security import StreamRedactor

    json.loads(text)
    out, warnings = redact_credentials(text)
    assert out == expected
    assert len(warnings) == 1
    assert json.loads(out)["keep"] == 1
    assert "test-value-not-a-credential-0123" not in out
    assert redact_credentials(out) == (out, [])
    for cut in range(1, len(text)):
        redactor = StreamRedactor()
        assert redactor.feed(text[:cut]) + redactor.feed(text[cut:]) + redactor.flush() == out, cut


def _labelled_line(unit: str):
    """A URL query carrying ``n`` labelled pairs on ONE line whose values are not
    live -- a registered tag, empty, or an empty quoted string -- so no label
    short-circuits the floor and every label is read."""

    def build(n: int) -> str:
        return "https://h.example/x?" + unit * n

    return build


@pytest.mark.parametrize(
    "unit",
    ["SecretAccessKey=[REDACTED: credential],", "SecretAccessKey=, ", 'SecretAccessKey="", '],
    ids=["tag-values", "empty-values", "empty-quoted-values"],
)
def test_the_hard_floor_is_linear_in_the_labels_on_a_line(unit: str) -> None:
    """``hard_credential_hit`` reads every labelled value on a line through the
    scanner; a reader that walked the line back from its start for each label
    read a line of N labels N times over -- 4x the work per doubling, measured on
    a URL of tag-filled pairs the floor must reject one by one -- and a fetched
    page or tool output shaped that way held the event loop until the watchdog
    ended the turn. The floor carries the line's quote state from one label to
    the next (``_KeyedValueScans``) and hands the scanner its enclosing literal,
    so the walk is one pass. Measured as executed lines of ``redaction``, which
    a loaded runner cannot change; the answer is unchanged: no live value."""
    from kiro_crew.security import hard_credential_hit

    build = _labelled_line(unit)
    assert hard_credential_hit(build(50)) is False
    assert (
        hard_credential_hit(build(50) + "SecretAccessKey=test-value-not-a-credential-0123") is True
    )
    assert_linear_work(_redaction, build, hard_credential_hit)


@pytest.mark.parametrize(
    "text",
    [
        '{"Authorization":\n "Bearer short-opaque-test-value-not-a-credential-0123"}',
        '{"SecretAccessKey":\n    "test-value-not-a-credential-0123"\n}',
        "aws_secret_access_key:\n  test-value-not-a-credential-0123\n",
        "AccessKeyId :\n\ttest-value-not-a-credential-0123\n",
    ],
    ids=[
        "bearer-json-next-line",
        "aws-json-next-line",
        "yaml-indented-next-line",
        "tab-indented-next-line",
    ],
)
def test_a_value_on_the_line_after_its_separator_is_redacted(text: str) -> None:
    """A pretty printer writes `"key":` and the value on the next line; a YAML
    author writes `key:` and an indented scalar below it. The separator's
    whitespace crosses the line break, as the base's does, so the value is the
    pair's and is redacted: detection across a line break is the base's. The
    redacted JSON still parses, and the output is a fixed point."""
    out, warnings = redact_credentials(text)
    assert "test-value-not-a-credential-0123" not in out, out
    assert len(warnings) == 1
    if text.startswith("{"):
        json.loads(out)
    assert redact_credentials(out) == (out, [])


@pytest.mark.parametrize("byte", [",", "}", "]"], ids=["comma", "brace", "bracket"])
def test_a_structural_byte_opening_a_value_is_its_first_byte(byte: str) -> None:
    """A value whose FIRST byte is `,`, `}` or `]` and goes on is the value, claimed
    whole: left as "no value" the bytes after it stood in plaintext, and the hard
    URL floor was silent on them. The byte opens no value only when a terminator,
    whitespace, a quote or the text's end follows it at once (`[1, "key="]`)."""
    from kiro_crew.security import contains_credential, hard_credential_hit

    secret = "test-value-not-a-credential-0123"
    plain = f"aws_secret_access_key={byte}{secret}"
    out, warnings = redact_credentials(plain)
    assert out == f"aws_secret_access_key={TAG}" and len(warnings) == 1
    assert contains_credential(plain)
    url = f"https://h.example/x?aws_secret_access_key={byte}{secret}"
    assert hard_credential_hit(url)
    assert secret not in redact_credentials(url)[0]
    empty = '{"keep": [1, "aws_secret_access_key="]}'
    assert redact_credentials(empty) == (empty, [])
    alone = f"aws_secret_access_key={byte}"
    assert redact_credentials(alone) == (alone, [])


def test_a_decoded_chunk_is_judged_from_a_fresh_line_start() -> None:
    """A base64 chunk the stream decodes is its own document: the line state the
    raw pieces before it left (an open quote in `{"text": "log `) belongs to the
    raw piece pass 1 scans, never to the decoded text. Read under that state, a
    decoded `key="[REDACTED: credential]` took the open quote as its enclosing
    literal and the tag-filled value as closed and exempt, so the encoded secret
    passed the stream while the batch pass redacted it. No committed chunk may
    carry the secret, encoded or decoded, at any split."""
    from kiro_crew.security import StreamRedactor

    decoded = 'aws_session_token="[REDACTED: credential]'
    encoded = base64.b64encode(decoded.encode()).decode()
    pieces = ['{"text": "log ', encoded, ' "}']
    text = "".join(pieces)
    expected, _warnings = redact_credentials(text)
    assert encoded not in expected and decoded not in expected
    splits = [pieces, [pieces[0] + pieces[1][:10], pieces[1][10:] + pieces[2]]]
    for split in splits:
        redactor = StreamRedactor()
        chunks = [redactor.feed(piece) for piece in split] + [redactor.flush()]
        assert all(encoded not in c and decoded not in c for c in chunks), chunks
        assert "".join(chunks) == expected
    for cut in range(1, len(text)):
        redactor = StreamRedactor()
        out = redactor.feed(text[:cut]) + redactor.feed(text[cut:]) + redactor.flush()
        assert out == expected, (cut, text[:cut])


@pytest.mark.parametrize("escape", ["\\n", "\\r", "\\r\\n", "\\n\\t"], ids=repr)
def test_an_escaped_line_break_heading_a_value_inside_a_json_string_is_its_head(
    escape: str,
) -> None:
    """Inside a `"` literal the pair `\\n` is one inner line break token. Read as the
    value's END, an empty value was claimed and the whole value stood behind it in
    plaintext (`{"o": "aws_secret_access_key=\\n<secret> more"}` came back
    unchanged, the presence check and the hard URL floor silent, the stream
    passing it at every cut) while the base redacted it. It is the enclosing
    encoding's line break, whitespace to the anchor as a raw one is, consumed as
    the value's head like `\\t`; the `\\r` spelling and a run of them with it. An
    EMPTY assignment followed by the escaped break claims the break and the next
    word, as the base does and as the raw-break shape already did."""
    from kiro_crew.security import StreamRedactor, contains_credential, hard_credential_hit

    secret = "test-value-not-a-credential-0123"
    text = '{"o": "aws_secret_access_key=' + escape + secret + ' more", "keep": 1}'
    expected = '{"o": "aws_secret_access_key=' + TAG + ' more", "keep": 1}'
    out, warnings = redact_credentials(text)
    assert out == expected and len(warnings) == 1
    assert json.loads(out)["keep"] == 1
    assert contains_credential(text)
    assert hard_credential_hit("https://h.example/x?q=" + text)
    for cut in range(1, len(text)):
        redactor = StreamRedactor()
        streamed = redactor.feed(text[:cut]) + redactor.feed(text[cut:]) + redactor.flush()
        assert streamed == expected, (cut, text[:cut])
    assert redact_credentials(secret + " more")[0] == secret + " more"
    empty = '{"o": "aws_secret_access_key=' + escape + 'next word", "keep": 1}'
    assert (
        redact_credentials(empty)[0] == '{"o": "aws_secret_access_key=' + TAG + ' word", "keep": 1}'
    )
    assert (
        redact_credentials("aws_secret_access_key=\nnext word")[0]
        == "aws_secret_access_key=\n" + TAG + " word"
    )


@pytest.mark.parametrize("letter", ["n", "t", "r", "f", "v"], ids=repr)
def test_a_literal_backslash_letter_inside_a_bare_value_is_the_values_bytes(letter: str) -> None:
    """In a bare line (a percent-decoded URL path, a log line) the two bytes `\\n`
    are a backslash and a letter, bytes of the value in the base's grammar. Read
    as a line break, the value stopped before them: a tag ahead of them stood
    alone, read as exempt, and the secret behind them passed the batch pass, the
    presence check, the hard URL floor and the stream, while the base blocked the
    URL. The pair is the value's, so a tag with the secret glued behind it is
    redacted whole and the floor flags it; inside a value the pair is kept with the
    value. Escaped whitespace of an escaped encoding is one token through
    `_inner_token` and never reaches this branch."""
    from kiro_crew.security import StreamRedactor, contains_credential, hard_credential_hit

    secret = "test-value-not-a-credential-0123AbCdEfGhIj"
    glued = f"aws_secret_access_key={TAG}\\{letter}{secret}"
    out, warnings = redact_credentials(glued)
    assert out == f"aws_secret_access_key={TAG}" and len(warnings) == 1, (out, warnings)
    assert contains_credential(glued)
    url = f"https://h.example/p/{glued}/x"
    assert hard_credential_hit(url)
    assert secret not in redact_credentials(url)[0]
    for cut in range(1, len(glued)):
        redactor = StreamRedactor()
        streamed = redactor.feed(glued[:cut]) + redactor.feed(glued[cut:]) + redactor.flush()
        assert secret not in streamed, (cut, streamed)
    inside = f"aws_secret_access_key={secret[:8]}\\{letter}{secret[8:]} more"
    assert redact_credentials(inside)[0] == f"aws_secret_access_key={TAG} more"
    alone = f"aws_secret_access_key={TAG}"
    assert redact_credentials(alone) == (alone, [])


@pytest.mark.parametrize(
    "text, expected",
    [
        pytest.param(
            'echo "export aws_secret_access_key="test-value-not-a-credential-0123"" && run\n',
            'echo "export aws_secret_access_key="[REDACTED: credential]"" && run\n',
            id="shell-concatenation",
        ),
        pytest.param(
            '{"template":"export aws_secret_access_key=test-value-not-a-credential-0123","keep":1}',
            '{"template":"export aws_secret_access_key=[REDACTED: credential]","keep":1}',
            id="bare-value-ends-at-the-literal",
        ),
        pytest.param(
            "text: 'aws_secret_access_key=', keep: 1\n",
            "text: 'aws_secret_access_key=', keep: 1\n",
            id="yaml-single-quoted-empty",
        ),
        pytest.param(
            "don't put aws_secret_access_key='test-value-not-a-credential-0123' here\n",
            "don't put aws_secret_access_key='[REDACTED: credential]' here\n",
            id="prose-apostrophe-is-not-an-opener",
        ),
        pytest.param(
            "don't put aws_secret_access_key=\" note' test-value-not-a-credential-0123\" here\n",
            'don\'t put aws_secret_access_key="[REDACTED: credential]" here\n',
            id="prose-apostrophe-then-interior-apostrophe",
        ),
    ],
)
def test_the_value_is_read_in_the_context_the_look_back_finds(text: str, expected: str) -> None:
    """Inside a literal the value ends at the literal's close and no further; an
    assignment the close follows directly is empty, and a bare run after the
    close (a shell concatenation, `"key="$SECRET"`) is the value, read outside
    the literal. An apostrophe inside a word before the key (`don't`) opens no
    literal, so the value after it reads by the outside rules: a `'`-quoted
    value is claimed to its close and an apostrophe inside a `"` value is a byte
    of it."""
    from kiro_crew.security import StreamRedactor

    out, warnings = redact_credentials(text)
    assert out == expected
    assert len(warnings) == (0 if out == text else 1)
    assert redact_credentials(out) == (out, [])
    for cut in range(1, len(text)):
        redactor = StreamRedactor()
        assert (
            redactor.feed(text[:cut]) + redactor.feed(text[cut:]) + redactor.flush() == expected
        ), cut


def test_the_stream_carries_the_lines_quote_state_across_a_commit() -> None:
    """A stream commits a line in pieces at its spaces, so a piece can begin
    inside a literal whose opening quote went out in the piece before it
    (`{"template": "x ` committed, `aws_secret_access_key=", "keep": 1}` next).
    The look-back over the piece alone finds no literal and reads the `"` as
    the value's opener; the stream carries the quote state of what it committed
    into the hold and the batch pass over the next piece, so every chunking
    agrees with the whole text."""
    from kiro_crew.security import StreamRedactor
    from kiro_crew.security.redaction import _LINE_START, _advance_line_state

    text = '{"template": "x aws_secret_access_key=", "keep": 1}\n'
    assert redact_credentials(text) == (text, [])
    for size in (1, 2, 3, 5, 7, 11, 16):
        redactor = StreamRedactor()
        out = "".join(redactor.feed(text[i : i + size]) for i in range(0, len(text), size))
        assert out + redactor.flush() == text, size
    # The carried state itself: inside the `"` literal after the committed prefix.
    state = _advance_line_state(_LINE_START, '{"template": "x ', 0, 16)
    assert state[0] == '"'
    assert _advance_line_state(state, "aws_secret_access_key=", 0, 22)[0] == '"'
    # A raw line break resets it; a prose apostrophe opens nothing.
    assert _advance_line_state(state, "\n", 0, 1)[:3] == _LINE_START[:3]
    assert _advance_line_state(_LINE_START, "don't say ", 0, 10)[0] == ""


def test_interleaved_streams_never_share_the_carried_line_state() -> None:
    """The carried line state belongs to ONE stream: two redactors fed turn and
    turn about -- one inside a `"` literal when it commits, the other in plain
    text -- each read their own next piece in their own context, the module's
    carrier is back at its default the moment a commit returns, and a flush, a
    reset or a new instance starts at a line start. A state shared across
    streams would let one session's open quote decide what another session's
    text means, which is a cross-session leak class, not a parsing nit."""
    from kiro_crew.security import StreamRedactor
    from kiro_crew.security.redaction import _LINE_START, _LINE_STATE

    inside = '{"template": "x aws_secret_access_key=", "keep": 1}\n'
    plain = 'note: aws_secret_access_key="test-value-not-a-credential-0123" tail\n'
    expected_inside, _ = redact_credentials(inside)
    expected_plain, _ = redact_credentials(plain)
    assert expected_inside == inside
    assert "test-value" not in expected_plain
    for cut_inside in range(1, len(inside)):
        for cut_plain in (6, 28):
            a, b = StreamRedactor(), StreamRedactor()
            out_a = a.feed(inside[:cut_inside])
            assert _LINE_STATE.get() == _LINE_START
            out_b = b.feed(plain[:cut_plain])
            assert _LINE_STATE.get() == _LINE_START
            out_a += a.feed(inside[cut_inside:]) + a.flush()
            out_b += b.feed(plain[cut_plain:]) + b.flush()
            assert out_a == expected_inside, (cut_inside, cut_plain)
            assert out_b == expected_plain, (cut_inside, cut_plain)
            # Both streams start their next segment at a line start.
            assert a._line_state == _LINE_START and b._line_state == _LINE_START
    # A reset mid-literal forgets the literal; a new instance never knew it.
    a = StreamRedactor()
    a.feed('{"template": "x ')
    assert a._line_state[0] == '"'
    a.reset()
    assert a._line_state == _LINE_START
    assert StreamRedactor()._line_state == _LINE_START
    assert _LINE_STATE.get() == _LINE_START


@pytest.mark.parametrize("prefix", ["see ", "it is 'quoted ", 'it is "quoted '], ids=repr)
@pytest.mark.parametrize(
    "source",
    [
        "aws_secret_access_key=' test-value-not-a-credential-0123",
        'aws_secret_access_key=" test-value-not-a-credential-0123',
        "aws_secret_access_key='test-value-not-a-credential-0123 more'",
        "@startuml\nSessionToken=test-value-not-a-credential-0123\n@enduml",
    ],
    ids=[
        "unterminated-sq-space",
        "unterminated-dq-space",
        "sq-value-with-space",
        "bare-in-diagram",
    ],
)
def test_a_decoded_diagram_is_judged_from_a_fresh_line_start_under_any_carried_state(
    prefix: str, source: str
) -> None:
    """The diagram a PlantUML link carries is a document of its own. The stream
    commits the text around the link with the line state the pieces before it
    left, and the nested pass over the decoded source must not read that state:
    under a carried `'` the quote opening the source's value read as that
    literal's close, the assignment stood empty, the whitespace after it opened
    no value, and a diagram the batch pass masked streamed out intact with the
    secret inside it. Batch and stream agree at every cut, the link never
    survives when its source carries a credential, and the nested pass answers
    as it does from a line start whatever state the carrier holds."""
    from test_plantuml_link_redaction import _encode

    from kiro_crew.security import StreamRedactor
    from kiro_crew.security.redaction import (
        _LINE_START,
        _LINE_STATE,
        _advance_line_state,
        _plantuml_verdict,
    )

    encoded = _encode(source)
    text = f"{prefix}https://www.plantuml.com/plantuml/svg/{encoded} done\n"
    batch, _warnings = redact_credentials(text)
    assert encoded not in batch, (prefix, source)
    for cut in range(1, len(text)):
        redactor = StreamRedactor()
        out = redactor.feed(text[:cut]) + redactor.feed(text[cut:]) + redactor.flush()
        assert out == batch, (prefix, source, cut, out)
    carried = _advance_line_state(_LINE_START, prefix, 0, len(prefix))
    previous = _LINE_STATE.get()
    _LINE_STATE.set(carried)
    try:
        under_carried = _plantuml_verdict(encoded, 16384)
    finally:
        _LINE_STATE.set(previous)
    assert under_carried == _plantuml_verdict(encoded, 16384) == (False, len(source))


@pytest.mark.parametrize(
    "text",
    [
        "eyJnotjson.part.aws_session_token: test-session-not-a-credential-0123\n",
        "id eyJabc.def.aws_secret_access_key=test-secret-not-a-credential-0123 tail\n",
        'ref eyJx.y.SecretAccessKey="test-secret-not-a-credential-0123" more\n',
    ],
    ids=["colon-key", "eq-key", "quoted-key"],
)
def test_the_hold_reads_the_matches_the_batch_pass_reads(text: str) -> None:
    """The stream hold and the batch pass iterate the SAME matches. The JWT
    branch of the pattern accepts any dotted identifier beginning with `eyJ`,
    and the batch pass rejects a hit whose header is not JSON and retries the
    other branches inside its span (`_credential_matches`); a hold that iterated
    the raw pattern took the rejected span whole, never saw the key anchor
    written inside it, held nothing, and a cut inside the value streamed the
    secret out that the batch pass redacted. Batch equals the stream at every
    cut, and no committed piece carries the value."""
    from kiro_crew.security import StreamRedactor

    batch, _warnings = redact_credentials(text)
    assert "not-a-credential" not in batch
    for cut in range(1, len(text)):
        redactor = StreamRedactor()
        pieces = [redactor.feed(text[:cut]), redactor.feed(text[cut:]), redactor.flush()]
        assert "".join(pieces) == batch, (cut, pieces)
        assert all("not-a-credential" not in piece for piece in pieces), (cut, pieces)


# ─────────────────────────────────────────────────────────────────────────────
# the stream holds a value that ends in a strict tag prefix anywhere in it
# ─────────────────────────────────────────────────────────────────────────────

_SECOND_TAG_TOKEN = "opaque-token-not-a-credential-0123"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(f"{TAG}{TAG}{_SECOND_TAG_TOKEN}", id="tag-then-tag-glued"),
        pytest.param(
            f"{TAG}[REDACTED: encoded credential]{_SECOND_TAG_TOKEN}",
            id="tag-then-encoded-tag-glued",
        ),
        pytest.param(f"{TAG}{TAG}{TAG}{_SECOND_TAG_TOKEN}", id="two-tags-then-tag-glued"),
        pytest.param(f"x{TAG}{_SECOND_TAG_TOKEN}", id="byte-then-tag-glued"),
    ],
)
def test_a_value_ending_in_a_cut_off_tag_after_whole_tags_is_held_from_its_key(
    value: str,
) -> None:
    """The strict-tag-prefix hold judged the buffer's tail from the VALUE's start,
    so a value whose last token is a tag cut off at a chunk boundary AFTER one or
    more whole tags (`key=[REDACTED: credential][REDACTED: `) was longer than any
    tag, read as terminated at the cut tag's interior space, and released the
    key's hold: the next chunk's `credential]<token>` streamed raw with no key in
    front of it, and only the stored copy's batch pass caught it. The hold judges
    the tail from every position inside the value, so a cut-off tag after whole
    tags, or after glued bytes, holds the pair from its key; every cut reproduces
    the batch pass's answer."""
    from kiro_crew.security import StreamRedactor

    text = f"aws_secret_access_key={value} tail\n"
    expected, warnings = redact_credentials(text)
    assert _SECOND_TAG_TOKEN not in expected
    assert warnings
    for cut in range(1, len(text)):
        redactor = StreamRedactor()
        out = redactor.feed(text[:cut]) + redactor.feed(text[cut:]) + redactor.flush()
        assert out == expected, (cut, text[:cut])


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            "aws_access_key_id = \naws_secret_access_key = test-value-not-a-credential-0123\nnext: line\n",
            id="eq-then-eq",
        ),
        pytest.param(
            "aws_access_key_id:\naws_secret_access_key: test-value-not-a-credential-0123\nnext: line\n",
            id="colon-then-colon",
        ),
        pytest.param(
            'aws_access_key_id=\naws_secret_access_key="test-value-not-a credential-0123"\nnext: line\n',
            id="eq-then-quoted",
        ),
    ],
)
def test_a_cut_pulled_back_to_a_waiting_label_inside_an_earlier_pair_holds_that_pair_too(
    text: str,
) -> None:
    """A key line with an EMPTY value followed by another key line: the first
    anchor's separator whitespace crosses the line break, so the batch pass reads
    the next key's name as the first pair's value and claims it, then the next
    pair's own value. The stream pulled its cut back to the second label while
    that label still waited for its separator -- a position inside the first
    pair's extent, where the first pair had been judged complete against the
    natural cut -- and committed the first label without the value the batch
    pass claims for it, so the two surfaces disagreed on the second key's name
    (every value was redacted on both). The pulled-back cut is judged again
    against every pair it would split, so the hold runs from the first key and
    every cut reproduces the batch answer."""
    from kiro_crew.security import StreamRedactor

    expected, warnings = redact_credentials(text)
    assert "test-value-not-a" not in expected
    assert len(warnings) == 2
    for cut in range(1, len(text)):
        redactor = StreamRedactor()
        out = redactor.feed(text[:cut]) + redactor.feed(text[cut:]) + redactor.flush()
        assert out == expected, (cut, text[:cut])
