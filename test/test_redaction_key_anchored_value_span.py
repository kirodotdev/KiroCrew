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
        _HARD_CREDENTIAL_RE,
        _contains_fixed_credential,
        contains_credential,
        credential_matches,
    )

    return (
        ("contains_credential", contains_credential),
        ("credential_matches", lambda text: next(credential_matches(text), None) is not None),
        ("_contains_fixed_credential", _contains_fixed_credential),
        ("_HARD_CREDENTIAL_RE", lambda text: _HARD_CREDENTIAL_RE.search(text) is not None),
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
    from kiro_crew.security.exfil import _HARD_CREDENTIAL_RE

    for spelling in (key, key.lower(), key.upper()):
        heading = f'{spelling}="{TAG} {value}"'
        assert _HARD_CREDENTIAL_RE.search(heading) is not None, heading
        assert _HARD_CREDENTIAL_RE.search(heading.replace('"', "'")) is not None, heading
        embedded = json.dumps({"text": heading})
        assert _HARD_CREDENTIAL_RE.search(embedded) is not None, embedded
        for filled in (
            f'{spelling}="{TAG}"',
            f"{spelling}='{TAG}{TAG}' tail",
            json.dumps({"t": f'{spelling}="{TAG}"'}),
        ):
            assert _HARD_CREDENTIAL_RE.search(filled) is None, filled
        assert _HARD_CREDENTIAL_RE.search(f"{spelling}={TAG} tail") is None, spelling
        assert _HARD_CREDENTIAL_RE.search(f"{spelling}={TAG}{value}") is not None, spelling

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
    from kiro_crew.security.exfil import _HARD_CREDENTIAL_RE

    for opener, other in (('"', "'"), ("'", '"')):
        mismatched = f"{key}={opener}{TAG}{other}{value}{other}"
        assert _HARD_CREDENTIAL_RE.search(mismatched) is not None, mismatched
        assert value not in redact_credentials(mismatched)[0], mismatched
        filled = f"{key}={opener}{TAG}{opener} {other}{value}{other}"
        assert _HARD_CREDENTIAL_RE.search(filled) is None, filled
        embedded = json.dumps({"t": f"{key}={opener}{TAG}{opener}"})
        assert _HARD_CREDENTIAL_RE.search(embedded) is None, embedded


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
    ``credential_matches`` instead. This pin fails when any non-test module
    outside ``security/`` calls the raw accessor -- a Python call site (read by
    ``ast``, so a docstring mention does not count) or a call inside a shell
    script's embedded program (the artifact-deploy scans)."""
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
        if "get_credential_patterns" not in source:
            continue
        if path.suffix == ".sh":
            if re.search(r"get_credential_patterns\s*\(", source):
                offenders.append(rel)
            continue
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "get_credential_patterns":
                offenders.append(f"{rel}:{node.lineno}")
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
