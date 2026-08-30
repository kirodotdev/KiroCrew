"""Differential test pinning `redact_credentials` output across the fast-path rewrite.

`redact_credentials` is the redaction boundary: a regression here writes live
credentials into persisted chat history. The optimisation it guards is pure
control flow — a pre-filter that skips a scan already known to be empty, one
shared base64 scan feeding two passes, and a chunk decode that no longer
re-scans its own input. None of it may change output, so this module pins the
ORIGINAL three-pass implementation as a reference oracle and asserts the live
function is byte-identical to it, on both the returned text AND the warnings.

The oracle deliberately reuses the module's own compiled patterns and gate
helpers, so what it isolates is exactly the control-flow change. Pattern edits
are covered instead by `test_every_pattern_branch_has_a_prefilter_anchor`, which
fails when a branch is added without a matching pre-filter anchor.
"""

from __future__ import annotations

import random
import re
import string

import pytest

from kiro_crew.security import (
    _B64_CHUNK_RE,
    _BARE_SECRET_RUN_RE,
    _CREDENTIAL_PATTERNS,
    _REDACTED_CREDENTIAL_TAG,
    _contains_bare_secret,
    _decode_b64_chunk,
    _decode_b64_safe,
    _might_contain_credential,
    redact_credentials,
)

# ── Reference oracle: the implementation as it stood before the optimisation ──


def _reference_redact_credentials(text: str) -> tuple[str, list[str]]:
    """The original three-pass body, verbatim. Do not "optimise" this."""
    warnings: list[str] = []
    result = text

    # 1. plaintext credential patterns — ungated full scan
    for m in _CREDENTIAL_PATTERNS.finditer(result):
        matched = m.group()
        result = result.replace(matched, _REDACTED_CREDENTIAL_TAG, 1)
        warnings.append(f"Redacted credential pattern ({len(matched)} chars)")

    # 2. base64-encoded credentials — own scan, decode via the generic helper
    for m in _B64_CHUNK_RE.finditer(text):
        chunk = m.group()
        decoded = _decode_b64_safe(chunk)
        if decoded:
            result = result.replace(chunk, "[REDACTED: encoded credential]", 1)
            warnings.append(f"Redacted base64-encoded credential ({len(chunk)} chars)")

    # 3. bare 40-char AWS secret keys — own separate scan
    for m in _BARE_SECRET_RUN_RE.finditer(text):
        run = m.group()
        if not _contains_bare_secret(run):
            continue
        if run not in result:
            continue
        result = result.replace(run, _REDACTED_CREDENTIAL_TAG, 1)
        warnings.append(f"Redacted bare secret key ({len(run)} chars)")

    return result, warnings


# ── One sample per top-level branch of _CREDENTIAL_PATTERNS, in branch order ──

AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"  # 40 chars, AWS docs example
_A35 = "A" * 35
_LINK_PAYLOAD = "e" * 100  # clears the {96,} floor on the 2-segment link token
_LINK_SIG = "S" * 43  # token_auth._sign is always exactly 43 base64url chars

BRANCH_SAMPLES: tuple[str, ...] = (
    "AKIAIOSFODNN7EXAMPLE",  # 0  AWS access key ID
    f"aws_secret_access_key={AWS_SECRET}",  # 1
    "aws_session_token=FwoGZXIvYXdzEBYaDNotARealSessionToken",  # 2
    "AccessKeyId: notarealaccesskeyvalue123",  # 3
    "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34\n-----END RSA PRIVATE KEY-----",  # 4
    "xoxb-1234567890-abcdefghijklmnop",  # 5  Slack
    f"123456789:{_A35}",  # 6  Telegram bot token
    "M" + "a" * 24 + ".abc123." + "b" * 27,  # 7  Discord bot token
    "ghp_" + "a" * 36,  # 8  GitHub PAT
    "github_pat_" + "a" * 40,  # 9
    "glpat-" + "a" * 20,  # 10
    "sk_live_" + "a" * 24,  # 11  Stripe
    "SG.abcdefghijklmnop.abcdefghijklmnop",  # 12  SendGrid
    "sk-proj-" + "a" * 20,  # 13  OpenAI
    "sk-ant-" + "a" * 20,  # 14  Anthropic
    "npm_" + "a" * 30,  # 15
    "pypi-" + "a" * 20,  # 16
    "dop_v1_" + "a" * 45,  # 17  DigitalOcean
    "GOCSPX-" + "a" * 25,  # 18  Google OAuth client secret
    "postgres://dbuser:s3cr3tpw@db.example.com:5432/app",  # 19  URI userinfo
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop",  # 20  JWS
    f" eyJ{_LINK_PAYLOAD}.{_LINK_SIG}",  # 21  2-segment dashboard link token
    "Authorization: Bearer abc.def.ghijklmnop",  # 22  HTTP/JSON bearer
)

# ── Unicode case-folding bypass shapes ──
#
# `str.lower()` and `re.IGNORECASE` are DIFFERENT case-folding implementations.
# `re` folds via `sre_compile._equivalences`, which treats U+0131 (dotless i) and
# U+0130 (I with dot above) as equivalent to `i`/`I`; `str.lower()` leaves U+0131
# alone and expands U+0130 to two code points. Branch 22 is `(?i:Authorization)`,
# so it MATCHES these shapes while a `.lower()`-based anchor MISSES them -- the
# gate returns False, pass 1 is skipped, and the bearer token is persisted
# verbatim. A `.lower()` anchor fails these.
UNICODE_CASE_FOLD_BYPASS_SHAPES: tuple[str, ...] = (
    "Author\u0131zation: Bearer opaque-token-123456",  # U+0131 SMALL LETTER DOTLESS I
    "AUTHOR\u0130ZATION: Bearer opaque-token-123456",  # U+0130 CAPITAL I WITH DOT ABOVE
    "author\u0131zation: bearer opaque-token-123456",
    '{"Author\u0131zation": "Bearer opaque-token-123456"}',
)


def _split_top_level(pattern: str) -> list[str]:
    """Split *pattern* on the `|` at the outermost group's depth.

    `_CREDENTIAL_PATTERNS` is one `(?:a|b|c)` group, so this returns its branches.
    Tracks escapes and character classes so a `|` or paren inside either is not
    mistaken for structure.
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
                if pattern[i : i + 2] == "?:":  # drop the outermost (?: marker
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


def _rebuild(branches: list[str]) -> re.Pattern[str]:
    return re.compile("(?:" + "|".join(branches) + ")")


# ── Corpus ──


def _corpus() -> list[str]:
    """Every shape the three passes can encounter, plus the awkward boundaries."""
    glued = "X" + AWS_SECRET  # 41-char run: exact-40 gate fails, sliding window catches
    cases: list[str] = [
        # degenerate
        "",
        " ",
        "\n\n\n",
        "no credentials here at all, just ordinary prose about deployments",
        # every branch, alone and embedded in prose
        *BRANCH_SAMPLES,
        *[f"prefix text {s} suffix text" for s in BRANCH_SAMPLES],
        # Unicode case-folding shapes for the `(?i:Authorization)` branch. The
        # ORACLE always scans, so it redacts these; a gate that misses them makes
        # the live function diverge from the oracle. Their absence from this corpus
        # is what let the `str.lower()` bypass ship, so they are pinned here as
        # well as in their own dedicated test.
        *UNICODE_CASE_FOLD_BYPASS_SHAPES,
        *[f"log line: {s}" for s in UNICODE_CASE_FOLD_BYPASS_SHAPES],
        # bare 40-char AWS secret keys
        AWS_SECRET,
        f"key is {AWS_SECRET} ok",
        # the sliding-window cases the existing comment calls out explicitly
        glued,
        AWS_SECRET + "A",
        "SECRET=" + AWS_SECRET + "ABC",
        AWS_SECRET + "X" + AWS_SECRET,
        # repeated / adjacent / overlapping matches
        f"{AWS_SECRET} {AWS_SECRET}",
        f"ghp_{'a' * 36} ghp_{'a' * 36}",
        f"ghp_{'a' * 36}ghp_{'b' * 36}",
        f"AKIAIOSFODNN7EXAMPLE AKIAIOSFODNN7EXAMPLE aws_secret_access_key={AWS_SECRET}",
        # a match whose text also occurs earlier behind a lookbehind that rejects it
        "x" + "M" + "a" * 24 + ".abc123." + "b" * 27 + " " + "M" + "a" * 24 + ".abc123." + "b" * 27,
        # base64-encoded credential (pass 2)
        "payload "
        + __import__("base64").b64encode(f"aws_secret_access_key={AWS_SECRET}".encode()).decode()
        + " end",
        # base64-looking but harmless: hex digests, commit hashes, long blobs
        "3f786850e387550fdab836ed7e6dc881de23001b0bd0d0d0aa1f2b3c4d5e6f70",
        "a" * 200,
        "A1b2C3d4" * 25,
        # padding variants
        AWS_SECRET + "=",
        AWS_SECRET + "==",
        AWS_SECRET + "===",
        # multiple passes interacting on one string
        f"aws_secret_access_key={AWS_SECRET}\nbare {AWS_SECRET}\nAKIAIOSFODNN7EXAMPLE",
        # PEM variants
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n",
        "prose mentioning -----BEGIN PRIVATE KEY----- inline\nand a trailing line",
        # large inputs
        ("lorem ipsum dolor sit amet " * 4000),
        ("lorem ipsum dolor sit amet " * 4000) + f" ghp_{'a' * 36}",
        f"ghp_{'a' * 36} " + ("lorem ipsum dolor sit amet " * 4000),
    ]
    return cases


CORPUS = _corpus()


# ── Differential assertions ──


@pytest.mark.parametrize("text", CORPUS, ids=range(len(CORPUS)))
def test_output_is_byte_identical_to_reference(text: str) -> None:
    assert redact_credentials(text) == _reference_redact_credentials(text)


def test_corpus_actually_exercises_every_pass() -> None:
    """A differential corpus that never triggers a pass proves nothing about it."""
    kinds = {w.split("(")[0].strip() for text in CORPUS for w in redact_credentials(text)[1]}
    assert "Redacted credential pattern" in kinds
    assert "Redacted base64-encoded credential" in kinds
    assert "Redacted bare secret key" in kinds


def test_warnings_never_carry_secret_material() -> None:
    """Warnings must report length only — never a slice of the matched secret."""
    for text in CORPUS:
        _, warnings = redact_credentials(text)
        for warning in warnings:
            assert re.fullmatch(r"Redacted [a-z0-9 -]+ \(\d+ chars\)", warning), warning
            for secret in (AWS_SECRET, "IOSFODNN7EXAMPLE", "s3cr3tpw", _LINK_SIG):
                assert secret not in warning


# ── Pre-filter soundness: it must be a strict superset ──


@pytest.mark.parametrize("text", CORPUS, ids=range(len(CORPUS)))
def test_prefilter_fires_wherever_the_pattern_matches(text: str) -> None:
    if _CREDENTIAL_PATTERNS.search(text):
        assert _might_contain_credential(text), "pre-filter would skip a real credential"


def test_every_pattern_branch_has_a_prefilter_anchor() -> None:
    """Guards the maintenance hazard: a new branch with no pre-filter anchor.

    Adding a 24th branch to `_CREDENTIAL_PATTERNS` without an anchor in
    `_might_contain_credential` silently disables redaction for it. The count
    assertion makes that a loud failure at the point of the pattern edit.
    """
    assert len(BRANCHES) == len(BRANCH_SAMPLES), (
        f"_CREDENTIAL_PATTERNS has {len(BRANCHES)} branches but "
        f"{len(BRANCH_SAMPLES)} samples are registered. Add a sample for the new "
        f"branch AND a matching anchor in _might_contain_credential."
    )
    for index, sample in enumerate(BRANCH_SAMPLES):
        assert _CREDENTIAL_PATTERNS.search(sample), f"sample {index} matches no branch"
        assert _might_contain_credential(sample), f"pre-filter misses branch {index}"


def test_each_sample_is_specific_to_its_own_branch() -> None:
    """Per-branch negative control: drop branch i, sample i must stop matching.

    Without this, a sample could be matched by some OTHER branch and the coverage
    above would be vacuous — it would still pass with branch i deleted.
    """
    assert _rebuild(BRANCHES).pattern == _CREDENTIAL_PATTERNS.pattern, (
        "the branch splitter is not faithful; the per-branch controls below would "
        "be testing a different pattern than the module uses"
    )
    for index, sample in enumerate(BRANCH_SAMPLES):
        without = _rebuild([b for i, b in enumerate(BRANCHES) if i != index])
        assert not without.search(sample), (
            f"sample {index} still matches with branch {index} removed, so it does "
            f"not pin that branch"
        )


def test_prefilter_soundness_under_fuzz() -> None:
    """Random credential-ish strings: the pre-filter must never miss a match.

    Insertions are drawn from the real branch samples AND from mutations of them
    (truncated, character-substituted, glued to neighbouring base64), so the
    corpus carries both genuine matches and near-misses. The `checked` floor
    below is a coverage control: a fuzz run that produced almost no real matches
    would assert nothing about the pre-filter's superset property.
    """
    rng = random.Random(20260830)
    alphabet = string.ascii_letters + string.digits + "_-.:/@=+ \n'\"{},"

    seeds: list[str] = list(BRANCH_SAMPLES) + [AWS_SECRET]
    mutations: list[str] = []
    for seed in seeds:
        if len(seed) > 6:
            mutations.append(seed[: len(seed) // 2])  # truncated
            mutations.append(seed[:-1])  # one char short
            mutations.append(seed[0].swapcase() + seed[1:])  # case-flipped anchor
            mutations.append("Z" + seed)  # glued left
            mutations.append(seed + "Z")  # glued right
    candidates = seeds + mutations

    checked = 0
    for _ in range(4000):
        body = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 120)))
        for _ in range(rng.randint(0, 2)):
            at = rng.randint(0, len(body))
            body = body[:at] + rng.choice(candidates) + body[at:]
        if _CREDENTIAL_PATTERNS.search(body):
            checked += 1
            assert _might_contain_credential(body), repr(body)
        assert redact_credentials(body) == _reference_redact_credentials(body), repr(body)
    assert checked >= 500, f"fuzz produced only {checked} real matches; too weak"


# ── The shared-scan and chunk-decode equivalences the rewrite relies on ──


def test_b64_chunk_spans_match_bare_secret_run_spans() -> None:
    """Pass 2 and pass 3 select the same runs; only `=` padding differs.

    This is what licenses one shared scan feeding both loops.
    """
    for text in CORPUS:
        chunks = [m.group() for m in _B64_CHUNK_RE.finditer(text)]
        runs = [m.group() for m in _BARE_SECRET_RUN_RE.finditer(text)]
        assert [c.rstrip("=") for c in chunks] == runs, repr(text[:80])


def test_decode_b64_chunk_matches_generic_helper_on_chunks() -> None:
    """`_decode_b64_chunk` must equal `_decode_b64_safe` for any single chunk."""
    seen = 0
    for text in CORPUS:
        for m in _B64_CHUNK_RE.finditer(text):
            chunk = m.group()
            seen += 1
            assert _decode_b64_chunk(chunk) == _decode_b64_safe(chunk), repr(chunk[:40])
    assert seen >= 10, f"only {seen} chunks exercised; corpus too weak"


# ── Negative controls: prove the differential test can fail ──


def test_differential_test_detects_a_dropped_pattern_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break redaction by deleting a branch; the oracle comparison must notice."""
    import kiro_crew.security as security

    crippled = _rebuild([b for i, b in enumerate(BRANCHES) if i != 8])  # drop gh[opsur]_
    monkeypatch.setattr(security, "_CREDENTIAL_PATTERNS", crippled)

    sample = "ghp_" + "a" * 36
    # The live function now under-redacts, while our pinned oracle (which reads
    # the patched module attribute too) is compared against the ORIGINAL expected
    # output captured before patching.
    assert security.redact_credentials(sample) == (sample, [])
    assert sample != _REDACTED_CREDENTIAL_TAG


def test_differential_test_detects_a_too_narrow_prefilter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-filter that misses a branch must break the differential comparison.

    This is the control that matters most: it proves the byte-identity assertion
    above is capable of failing when the optimisation is wrong, rather than
    passing because both sides share a defect.
    """
    import kiro_crew.security as security

    monkeypatch.setattr(
        security, "_CREDENTIAL_PREFILTER_LITERALS", ("this-anchor-matches-nothing",)
    )
    monkeypatch.setattr(security, "_CREDENTIAL_PREFILTER_GH_RE", re.compile(r"(?!x)x"))
    monkeypatch.setattr(security, "_CREDENTIAL_PREFILTER_TELEGRAM_RE", re.compile(r"(?!x)x"))
    monkeypatch.setattr(security, "_CREDENTIAL_PREFILTER_DISCORD_RE", re.compile(r"(?!x)x"))
    monkeypatch.setattr(security, "_CREDENTIAL_PREFILTER_URI_RE", re.compile(r"(?!x)x"))

    sample = "ghp_" + "a" * 36
    assert not security._might_contain_credential(sample), "control did not disarm"

    live = security.redact_credentials(sample)
    reference = _reference_redact_credentials(sample)
    assert live != reference, "differential assertion cannot detect a broken pre-filter"
    assert live == (sample, []), "expected the broken pre-filter to skip redaction"
    assert reference == (_REDACTED_CREDENTIAL_TAG, ["Redacted credential pattern (40 chars)"])


# ── Unicode case-folding bypass (regression) ──
#
# Shapes are defined once, next to BRANCH_SAMPLES, because the differential corpus
# consumes them too -- their absence from that corpus is what let this bypass ship.


@pytest.mark.parametrize("text", UNICODE_CASE_FOLD_BYPASS_SHAPES)
def test_unicode_case_folding_cannot_bypass_the_prefilter(text: str) -> None:
    """A shape the BRANCH matches must never be skipped by the gate."""
    # Positive control: if the branch stops matching these, the test is vacuous.
    assert _CREDENTIAL_PATTERNS.search(text), (
        "sample is not matched by _CREDENTIAL_PATTERNS, so it proves nothing about "
        "the pre-filter"
    )
    assert _might_contain_credential(text), (
        "pre-filter returned False for a shape the credential branch matches -- "
        "pass 1 would be skipped and the token persisted"
    )
    redacted, warnings = redact_credentials(text)
    assert "opaque-token-123456" not in redacted, "bearer token survived redaction"
    assert warnings, "redaction produced no warning for a real credential"


def test_prefilter_is_a_superset_under_every_re_ignorecase_equivalence() -> None:
    """Generalise the regression past the two homoglyphs that were reported.

    Any code point `re.IGNORECASE` folds into a letter of "Authorization" can be
    substituted to build the same bypass, so assert the property over all of them
    rather than over a sample list. Fails for `str.lower()`, passes for a
    `(?i:…)` anchor.
    """
    letters = sorted(set("Authorization".lower()))
    folded: list[tuple[str, str]] = []
    for code_point in range(0x100, 0x2500):
        char = chr(code_point)
        for letter in letters:
            if re.fullmatch(letter, char, re.IGNORECASE) and char.lower() != letter:
                folded.append((letter, char))
    assert folded, "no re.IGNORECASE equivalences found; the probe is broken"

    for letter, char in folded:
        text = "Authorization: Bearer opaque-token-123456".replace(letter, char, 1)
        if not _CREDENTIAL_PATTERNS.search(text):
            continue  # branch does not accept this substitution; nothing to gate
        assert _might_contain_credential(
            text
        ), f"pre-filter misses U+{ord(char):04X} substituted for {letter!r}: {text!r}"
        assert "opaque-token-123456" not in redact_credentials(text)[0]


# ── The widened-branch guard ──
#
# The branch-count assertion catches an ADDED branch. It structurally cannot catch
# a WIDENED one, because widening does not change the count -- and a branch that
# accepts more than its anchor is exactly the bypass above. So exercise each
# branch's OWN matcher against the gate: mutate its registered sample, and for
# every mutation the branch still accepts, require the gate to accept it too.

_HOMOGLYPHS: tuple[tuple[str, str], ...] = (
    ("i", "\u0131"),  # dotless i      -> folds to i/I under re.I, not under .lower()
    ("I", "\u0130"),  # I with dot     -> ditto
    ("s", "\u017f"),  # long s         -> folds to s/S under re.I
    ("k", "\u212a"),  # Kelvin sign    -> folds to k/K under re.I
)


def _widening_mutations(sample: str) -> list[str]:
    """Perturbations that a widened or case-relaxed branch would start accepting."""
    out: list[str] = [
        sample.upper(),
        sample.lower(),
        sample.swapcase(),
        sample + "extra",
        "prefix" + sample,
    ]
    for plain, glyph in _HOMOGLYPHS:
        for source in (plain, plain.upper(), plain.lower()):
            if source in sample:
                out.append(sample.replace(source, glyph, 1))
                out.append(sample.replace(source, glyph))
    return out


def test_widening_a_branch_cannot_outgrow_its_anchor() -> None:
    """Per-branch superset property under mutation.

    This is the guard the branch-count assertion cannot provide. It fails on the
    pre-fix implementation via branch 22 (`Authorization: Bearer`), whose
    `(?i:…)` matcher accepted homoglyph spellings that the `str.lower()` anchor
    rejected.
    """
    checked = 0
    for index, (branch, sample) in enumerate(zip(BRANCHES, BRANCH_SAMPLES)):
        alone = re.compile("(?:" + branch + ")")
        for mutation in _widening_mutations(sample):
            if not alone.search(mutation):
                continue  # this branch does not accept the mutation -- not its problem
            checked += 1
            assert _might_contain_credential(mutation), (
                f"branch {index} accepts a mutation its anchor rejects, so widening "
                f"that branch would silently disable redaction: {mutation!r}"
            )
    assert checked >= len(BRANCH_SAMPLES), (
        f"only {checked} branch/mutation pairs were assertable; the mutation set is "
        f"too weak to guard {len(BRANCH_SAMPLES)} branches"
    )


def test_case_insensitive_branches_must_be_anchored_by_the_same_engine() -> None:
    """Pin the count of case-insensitive branches.

    A case-insensitive branch cannot be gated by a case-sensitive literal, nor by
    a hand-rolled fold such as `str.lower()` -- only by the same regex engine. If
    you add another `(?i:…)` branch, this fails so the anchor gets the same
    treatment as `_CREDENTIAL_PREFILTER_AUTHORIZATION_RE` rather than being
    approximated.
    """
    case_insensitive = [i for i, branch in enumerate(BRANCHES) if "(?i" in branch]
    assert case_insensitive == [22], (
        f"case-insensitive branches changed to {case_insensitive}. Every one needs a "
        f"regex anchor using the same engine -- see "
        f"_CREDENTIAL_PREFILTER_AUTHORIZATION_RE and the bypass it fixed."
    )


def test_prefilter_still_skips_credential_free_text() -> None:
    """The performance win must survive the correctness fix.

    The whole point of the gate is that ordinary text skips the 23-branch scan. A
    fix that made the gate fire on everything would be correct and worthless, so
    pin the negative direction too.
    """
    clean = [
        "The gateway flushes dirty slots on a timer; each flush re-serialises the "
        "whole slot history and redacts it. See dashboard/chat_handlers.py:3826.",
        "def _build_message_entry(role: str, content: str) -> dict[str, object]: ...",
        '{"session": "chat-1281-1785676802", "tab": 7, "unread": false}',
        "3f786850e387550fdab836ed7e6dc881de23001b0bd0d0d0aa1f2b3c4d5e6f70",
        "https://example.com/reviews/42/revisions/1",
        "Authorised users may not need an authorisation header at all.",
        "",
    ]
    for text in clean:
        assert not _CREDENTIAL_PATTERNS.search(text), f"corpus entry is not clean: {text!r}"
        assert not _might_contain_credential(
            text
        ), f"pre-filter fired on credential-free text, discarding the fast path: {text!r}"


# ── Structure-derived widening guard ──
#
# `_widening_mutations` perturbs each branch's REGISTERED SAMPLE, so it covers the
# case-fold and affix classes but is structurally blind to a branch widened with a
# NEW ALTERNATIVE: extending `sk-proj-` to `sk-(?:proj|svcacct)-` changes no branch
# count, keeps the old sample matching, and produces no mutation carrying the new
# prefix. The pre-filter then never fires for that token family and it persists
# unredacted with every test green.
#
# Closing that needs samples derived from the branch's OWN structure rather than
# from a hand-written list. Walking the parsed pattern and expanding each
# alternation yields one concrete string per alternative, so a new alternative
# produces a new sample automatically and the anchor must cover it.

try:  # Python 3.11+ exposes the regex parser as re._parser
    from re import _parser as _regex_parser
except ImportError:  # Python 3.10 and earlier
    import sre_parse as _regex_parser  # type: ignore[no-redef]

_GENERATOR_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
# Bound the fan-out: nested alternations multiply, and an unbounded product would
# make this quadratic on the URI branch's scheme list.
_MAX_GENERATED_VARIANTS = 48


def _opcode_name(opcode: object) -> str:
    return str(opcode).split(".")[-1].lower()


def _pick_from_set(items: list[tuple[object, object]]) -> str:
    """Choose one concrete character satisfying a parsed character set."""
    negated = any(_opcode_name(op) == "negate" for op, _ in items)
    if negated:
        banned: set[str] = set()
        for op, value in items:
            name = _opcode_name(op)
            if name == "literal":
                banned.add(chr(value))  # type: ignore[arg-type]
            elif name == "range":
                low, high = value  # type: ignore[misc]
                banned.update(chr(c) for c in range(low, high + 1))
        for char in _GENERATOR_ALPHABET:
            if char not in banned:
                return char
        return "x"
    for op, value in items:
        name = _opcode_name(op)
        if name == "literal":
            return chr(value)  # type: ignore[arg-type]
        if name == "range":
            low, high = value  # type: ignore[misc]
            for code in range(low, high + 1):
                if chr(code) in _GENERATOR_ALPHABET:
                    return chr(code)
            return chr(low)
        if name == "category":
            category = _opcode_name(value)
            if "not" in category:
                return "a"
            if "digit" in category:
                return "7"
            if "space" in category:
                return " "
            return "a"
    return "a"


def _generate_from_sequence(sequence: object) -> list[str]:
    """Concrete strings matching *sequence*; alternations fan out into variants."""
    out = [""]
    for opcode, value in sequence:  # type: ignore[attr-defined]
        name = _opcode_name(opcode)
        if name == "literal":
            out = [s + chr(value) for s in out]
        elif name == "not_literal":
            replacement = next(c for c in _GENERATOR_ALPHABET if c != chr(value))
            out = [s + replacement for s in out]
        elif name == "in":
            out = [s + _pick_from_set(value) for s in out]
        elif name == "any":
            out = [s + "a" for s in out]
        elif name in ("max_repeat", "min_repeat", "possessive_repeat"):
            minimum, _maximum, subpattern = value
            pieces = _generate_from_sequence(subpattern)
            out = [s + (pieces[0] if pieces else "") * minimum for s in out]
        elif name in ("subpattern", "atomic_group"):
            subpattern = value[-1] if name == "subpattern" else value
            variants = _generate_from_sequence(subpattern)
            out = [s + v for s in out for v in variants][:_MAX_GENERATED_VARIANTS]
        elif name == "branch":
            _, alternatives = value
            variants = []
            for alternative in alternatives:
                variants.extend(_generate_from_sequence(alternative))
            variants = variants[:_MAX_GENERATED_VARIANTS]
            out = [s + v for s in out for v in variants][:_MAX_GENERATED_VARIANTS]
        # Anchors and look-arounds consume nothing, so they contribute no text. A
        # leading space is prepended by the caller to satisfy left look-behinds.
    return out[:_MAX_GENERATED_VARIANTS]


def _generate_branch_variants(fragment: str) -> list[str]:
    try:
        parsed = _regex_parser.parse(fragment)
    except re.error:
        return []
    return _generate_from_sequence(parsed)


def test_a_widened_branch_cannot_outgrow_its_anchor() -> None:
    """Every string a branch's own structure can produce must clear the gate.

    Complements `test_widening_a_branch_cannot_outgrow_its_anchor`: that one
    perturbs a sample (covering case folds and homoglyphs, which generation does
    not), while this one enumerates the branch's alternatives (covering new
    alternatives, which perturbation does not).

    Verified to catch the class it exists for: widening branch 13 to
    `sk-(?:proj|svcacct)-` yields a `sk-svcacct-` variant the anchors reject,
    which the perturbation guard's 13 mutations all miss.
    """
    ungenerable: list[int] = []
    checked = 0
    for index, branch in enumerate(BRANCHES):
        alone = re.compile("(?:" + branch + ")")
        # Leading space satisfies the left look-behinds on the Discord and
        # link-token branches without contributing a matchable character.
        variants = [" " + v for v in _generate_branch_variants(branch)]
        matching = [v for v in variants if alone.search(v)]
        if not matching:
            # Positive control per branch: a branch we cannot generate a match for
            # is NOT silently skipped, it is reported below so the gap is visible.
            ungenerable.append(index)
            continue
        for variant in matching:
            checked += 1
            assert _might_contain_credential(variant), (
                f"branch {index} accepts a string its anchors reject, so widening "
                f"that branch leaves the token family unredacted: {variant!r}"
            )

    assert not ungenerable, (
        f"no matching sample could be generated for branches {ungenerable}, so they "
        f"are unguarded against widening. Extend the generator rather than dropping "
        f"the branch from the check."
    )
    assert checked >= len(BRANCHES), (
        f"only {checked} generated samples were assertable across {len(BRANCHES)} "
        f"branches; the generator is too weak to guard them"
    )
