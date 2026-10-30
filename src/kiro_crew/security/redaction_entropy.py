"""The entropy machinery: whether a run with no marker is a key, judged by its shape.

A bare high-entropy run carries no marker to anchor on, so pass 3 judges it by
shape alone -- length, character classes, entropy, decodability -- and each gate
is a separate predicate so a refusal can name the one that fired. Beside the
gates sit the shapes the pass leaves alone on purpose: a commit permalink on a
code host, an ordered alphabet or a data URI payload in code text, this host's
macOS per-user directory id, and the document and PlantUML links whose grammar
is declared here. The base64 decode that finds a credential encoded inside a run
is here too.

The two functions that apply the document-link grammar, ``_document_link_spans``
and ``_plantuml_verdict``, stay in :mod:`kiro_crew.security.redaction`: judging
a PlantUML diagram runs the redactor over its decoded source, so they sit with
the redactor.

:mod:`kiro_crew.security.redaction` is the import path of every name defined
here.
"""

from __future__ import annotations

import base64
import bisect
import contextvars
import math
import re
import zlib
from collections import Counter
from collections.abc import Iterator

from .redaction_patterns import (
    _PREFILTER_MIN_LEN,
    _contains_credential_pattern,
    _might_contain_credential,
)

# Base64 alphabet: at least 40 chars of [A-Za-z0-9+/] ending with optional =
_B64_CHUNK_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


# ── Label-independent bare-secret detection ──
# A 40-char AWS *secret access key* (the value paired with an AKIA/ASIA access
# key ID) is a bare run of the base64 alphabet with NO distinctive prefix and NO
# key= label, so none of the labelled/prefixed patterns in _CREDENTIAL_PATTERNS
# catch it when it appears standalone (e.g. echoed alone, in a log line, or in a
# JSON array element). We add a conservative, entropy-gated detector for this
# shape. This is the HIGHEST false-positive-risk redaction rule in the module, so
# it is deliberately over-gated: a token must clear EVERY gate below to be
# redacted. The gates are ordered cheapest-first.
#
# AWS secret access keys are exactly 40 base64 characters. We match ANY isolated
# run of >=40 base64-alphabet chars (word-boundary look-arounds keep surrounding
# prose intact and stop a longer high-entropy blob from being split and missed),
# then require the *specific 40-char secret shape* per token.
#
# NOT CONSULTED BY `redact_credentials`. Pass 3 derives its runs from
# `_B64_CHUNK_RE` instead (`run = chunk.rstrip("=")`), because that one scan feeds
# both pass 2 and pass 3 and the two patterns select identical spans. The only
# remaining consumer here is `_text_contains_bare_secret`. That split is a
# desync hazard: WIDENING THIS PATTERN ALONE (adding base64url `-_`, say) would
# change the URL scan and leave the redactor untouched, silently. Any edit to the
# character class or the `{40,}` floor must be mirrored in `_B64_CHUNK_RE` above.
# `test_the_two_base64_run_patterns_stay_structurally_coupled` pins both literals
# so such an edit fails loudly rather than drifting.
_BARE_SECRET_RUN_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}(?![A-Za-z0-9+/])")

# Exactly-40 is the AWS secret-key length. Keeping the shape check length-exact
# (rather than ">=40") is what lets the structural gates below cleanly separate
# real keys from 64-char sha256 hex, base64 document blobs, etc.
_SECRET_KEY_LEN = 40

#: Inside code text (``redaction_switch.code_text``) pass 3 leaves a run alone only when it is
#: one of two things that can be told from a key by what they are, never by a name near them.
#: A data URI payload: a run of ``_DATA_URI_PAYLOAD_MIN`` characters or more that follows
#: ``data:<type>;base64,``. An alphabet: every 40-character window of the run has at least
#: ``_ALPHABET_STEPS`` adjacent pairs that step up by one character code (``abc...``,
#: ``ABC...``, ``012...``), which a table a program indexes into has and a random key does
#: not. Everything else, a glued run included, is judged exactly as outside the scope.
_DATA_URI_BEFORE_RE = re.compile(
    r"data:[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+(?:;[A-Za-z0-9=.+-]+)*;base64,\Z", re.IGNORECASE
)
#: How far back from a run the data URI header may start.
_DATA_URI_REACH = 128
#: A payload shorter than this is judged, so a lone key behind a ``data:`` header is masked.
_DATA_URI_PAYLOAD_MIN = 64
#: An alphabet run longer than this is judged, which bounds the cost of reading one.
_ALPHABET_RUN_MAX = 4096
#: Adjacent pairs in a 40-character window (39) that must step up by one. A table of the
#: letters and digits has 59 of 61 per 62; a random key has one such pair in about 64.
_ALPHABET_STEPS = 30


def _steps_up(run: str, at: int) -> int:
    """1 when the character after ``run[at]`` is the next character code, else 0."""
    return 1 if ord(run[at + 1]) == ord(run[at]) + 1 else 0


def _is_code_literal(text: str, start: int, run: str) -> bool:
    """Whether the run at *start* is a data URI payload or an ordered alphabet.

    Both are read off the run and the few characters before it, and a run that is neither
    stops the read at the first window that fails, so the cost of one run is fixed by
    ``_DATA_URI_REACH`` and ``_ALPHABET_RUN_MAX`` whatever the text holds. A run this cannot
    place is judged, which is the side that masks.
    """
    if len(run) >= _DATA_URI_PAYLOAD_MIN and _DATA_URI_BEFORE_RE.search(
        text, max(0, start - _DATA_URI_REACH), start
    ):
        return True
    if not _SECRET_KEY_LEN <= len(run) <= _ALPHABET_RUN_MAX:
        return False
    window = _SECRET_KEY_LEN - 1
    total = sum(_steps_up(run, i) for i in range(window))
    if total < _ALPHABET_STEPS:
        return False
    for i in range(window, len(run) - 1):
        total += _steps_up(run, i) - _steps_up(run, i - window)
        if total < _ALPHABET_STEPS:
            return False
    return True


# Shannon-entropy floor (bits/char). A uniformly-random 40-char base64 string
# averages ~4.78 bits/char and empirically almost never drops below ~4.4;
# English-word identifiers, hex digests, and repeated/low-alphabet runs sit
# below this. 4.3 is a conservative floor that admits real keys (the canonical
# AWS example scores 4.66) while rejecting camelCase code identifiers and file
# paths, which cluster around 4.0-4.3.
_SECRET_ENTROPY_MIN = 4.3

# Even after the entropy floor, camelCase / PascalCase code identifiers and
# slash-delimited file paths (e.g. src/main/java/com/Example/FooBarBazClas1) can
# survive on entropy ALONE. Two structural signals separate a random secret from
# a word-based identifier or path: (a) a random key almost never contains a long
# unbroken lowercase run, whereas identifiers/paths are built from dictionary
# words that do; (b) a random key has a low vowel ratio, whereas English words
# do not. NOTE: unlike a naive design we deliberately do NOT treat the presence
# of '/' or '+' as a free pass to redact — 40-char mixed-case file paths contain
# '/' yet are benign, so a '/' token must still clear both structural gates.
# Neither gate can speak for a path LONGER than one window, though: its straddling
# sub-windows are built from fragments of several components and clear both, which
# is what `_SECRET_MAX_SLASHES` below is for.
# Thresholds are chosen from measured distributions (see test_security.py) with a
# wide margin toward NOT redacting.
_SECRET_MAX_LOWER_RUN = 5
_SECRET_MAX_VOWEL_RATIO = 0.30

# Ceiling on the separators a window CUT OUT OF A LONGER RUN may hold, applied by
# :func:`_contains_bare_secret`. A window straddling several path components is a
# token nobody wrote, and it clears both gates above; its separator density is
# what gives it away -- `/` is 1 base64 character in 64, so a real 40-char key
# averages 0.6 of them, while a window spanning components carries one per
# component. Measured on 200,000 uniformly random 40-char keys: this declines
# 0.36% on its own, against 9.29% for the lowercase-run gate and 6.60% for the
# vowel-ratio gate. Three is the knee: four leaves the reported paths redacted.
_SECRET_MAX_SLASHES = 3

# The second ceiling a window CUT OUT OF A LONGER RUN is held to: it may not cross
# the opening `/` of a routed commit and take eight of its digits. A permalink is
# one run (`com/Owner/RepoName/blob/<commit>/src/file`), and its window straddling
# `RepoName/blob/` and the start of the commit clears every gate on two
# separators: capitals from the repository name, lowercase and digits from the hex.
# A routed commit is a path segment of exactly 40 or 64 lowercase hex digits, as git
# prints one, right after a code-host route below (some hosts spell them `blobs/`
# and `trees/`) and ending at a `/` or the run's edge. The route keeps a key out: a
# key's own window crosses a routed commit's opening `/` only when the key itself
# ends in a route word, that `/` and eight or more hex digits. Eight is what a
# `packages/<name>/blobs/` link needs: its widest firing window is `kages/`, a
# 19-character name and `/blobs/`, leaving eight digits (none of 4,000 such links
# fire at eight, 766 at nine); twelve left a third redacted. A lower floor frees
# more of a padded key: at one, a key needs only `/blob/` and one hex digit. On
# ten commits of each of the 1,000 most-starred GitHub repositories, 3.58% of blob
# permalinks fire without this and 0.02% at twelve. Residuals: a repository or
# package name whose capitals fill a whole window (a name over 19 characters can
# leave fewer than eight digits), a window starting inside the commit that runs
# into a path with capitals (1.08% of links to `src/File.py`), a key ending in a
# route word, `/` and eight or more hex the text runs on into a commit,
# raw.githubusercontent.com links and upper-case commits.
_ROUTED_COMMIT_RE = re.compile(
    r"(?:(?<=/)|\A)(?:blobs?|trees?|blame|raw|commits?|src)/"
    r"(?P<commit>[0-9a-f]{64}|[0-9a-f]{40})(?=/|\Z)"
)
_ROUTED_COMMIT_PIECE_MIN = 8

# A token that base64-decodes to >=85% printable ASCII is encoded *text*, not a
# random key (random 40-char keys decode to mostly non-printable bytes). Such a
# token is left to the existing base64 decode-and-scan path in redact_credentials
# so we do not double-count or mis-classify it here.
_SECRET_PRINTABLE_DECODE_RATIO = 0.85

_VOWELS: frozenset[str] = frozenset("aeiouAEIOU")

# All-hex runs are git SHAs (40 hex), sha256 (64 hex), md5 (32 hex), etc. — never
# an AWS secret key (which uses the full base64 alphabet). Reject them outright.
_HEX_ONLY_RE = re.compile(r"\A[0-9a-fA-F]+\Z")

# The macOS per-user directory id: ``confstr`` names the temp and cache roots
# ``/var/folders/<2>/<30>/T`` and ``.../C``, the two variable components an
# OS-generated lowercase encoding of the user's UUID and uid. ``/`` is in the run
# alphabet, so a path under them is one run whose window straddling the id and
# the ``T`` clears every gate, and every temp path -- a computer-use screenshot
# echoed as ``![](path)`` among them -- would read as a key. Pass 3 therefore
# scans the original run, exempting only windows that share at least
# ``_HOST_ID_EXEMPT_OVERLAP`` bytes IN PLACE with THIS host's OS-reported id. Any
# other positive window overlapping the id is still a hit, and pass 3 redacts
# every uncovered piece such a window touches; the id and the prefix stay
# plaintext because they are withheld from the scan, not redacted. Fast paths,
# the fragment separator ceiling and printable-base64 exclusion use whole-run
# context. In free text a writer picks an id's bytes and could fill most of a
# key-shaped window, while the host's id is chosen by nobody who writes text. The
# prefix must start a path (no run-alphabet byte, ``/`` included, before it) and
# the byte after the directory letter must not continue the run: ``/`` starts the
# next component, and any byte outside the run alphabet ends the run there, so a
# bare root printed, quoted, or followed by a newline is the same two-byte ``/T``
# piece as one that ends the text. ``/Tevil`` is a chosen name and gets no
# exemption. A key after ``/T_evil`` sits in the next run and is judged there;
# the pieces the exemption leaves in this run are the prefix and the ``/T``, both
# shorter than one key, so no key-shaped window survives in them.
#: Least overlap at which a key-shaped window is exempted from the bare-secret
#: scan: a key can occupy an exempt window only by sharing this many of its bytes
#: IN PLACE with an id the OS chose, which no writer controls. 24 exempts the
#: screenshot spooler path (32), the bare root (33) and an ``mkdtemp`` ``tmpXXXXXXXX``
#: dir with a one-character leaf (24); a temp path whose run goes 15 or more run-alphabet
#: bytes past the directory letter can still yield a positive window below 24 and stays
#: redacted, as without the exemption. A key glued across the id with less overlap is judged.
_HOST_ID_EXEMPT_OVERLAP = 24
_DARWIN_USER_DIR_PREFIX_RE = r"(?<![A-Za-z0-9+/])(?:/private)?/var/folders/"
_DARWIN_USER_DIR_SUFFIX_RE = r"/[CT](?![A-Za-z0-9+])"
# ``_CS_DARWIN_USER_TEMP_DIR`` in Darwin's ``<unistd.h>``; Python has no name for
# it, and its answer is the temp root with a trailing separator.
_CS_DARWIN_USER_TEMP_DIR = 65537
_DARWIN_USER_TEMP_DIR_RE = re.compile(
    r"\A(?:/private)?/var/folders/(?P<id>[a-z0-9_]{2}/[a-z0-9_]{30})/T/?\Z"
)

# The Shannon term ``(c / _SECRET_KEY_LEN) * log2(c / _SECRET_KEY_LEN)``, indexed by
# the character count ``c``. Element 0 is a ``0.0`` placeholder that keeps ``c``
# usable as a direct index; it is never read, because a count of zero cannot appear
# in a :class:`~collections.Counter` built from an iterable, and ``log2(0)`` would
# raise.
#
# Built for ONE length rather than parameterised over lengths, because
# :func:`_looks_like_secret_key` reaches the entropy gate only through its
# exactly-``_SECRET_KEY_LEN`` check, so that is the only length any production call
# can ask about. A per-length table would need a size cap and an eviction policy to
# bound what an arbitrary caller could materialise -- machinery guarding a caller
# that does not exist. Any other length falls through to the inline formula, which
# is what this table was derived from, so the general path is exactly as it was
# before the table existed.
#
# The terms are computed with the same operations the inline expression used, which
# is what makes this a pure precomputation rather than a re-derivation.
_ENTROPY_TERMS_KEY_LEN: tuple[float, ...] = (0.0,) + tuple(
    (c / _SECRET_KEY_LEN) * math.log2(c / _SECRET_KEY_LEN) for c in range(1, _SECRET_KEY_LEN + 1)
)


def _shannon_entropy(token: str) -> float:
    """Return the Shannon entropy of *token* in bits per character.

    The result is compared against :data:`_SECRET_ENTROPY_MIN` by
    :func:`_looks_like_secret_key`, so this is a gate on a redaction verdict and
    NOT a statistic anybody displays. A one-ULP drift at the boundary flips that
    verdict, and a flip in the permissive direction leaks a credential. The
    optimisation below is therefore built to be BIT-IDENTICAL, not merely close,
    and is pinned that way by ``TestShannonEntropyIsBitIdentical``.

    Each addend is ``(c / length) * log2(c / length)``. The sole production caller
    reaches this only through the exactly-``_SECRET_KEY_LEN`` check in
    :func:`_looks_like_secret_key`, and reaches it over and over --
    :func:`_contains_bare_secret` slides a 40-char window byte by byte across each
    base64-alphabet run that clears its prefilters -- so at that one length every
    addend is drawn from the fixed set :data:`_ENTROPY_TERMS_KEY_LEN` holds. That
    retires TWO true divisions and one ``math.log2`` call per DISTINCT CHARACTER per
    call -- ``c / length`` appears twice in the expression and CPython evaluates it
    twice, and a 40-char base64 window holds ~30 distinct characters -- plus the
    generator frames, in favour of a C-level ``map`` over a tuple index.

    Any other length takes the inline formula, unchanged from before the table
    existed. That keeps the fast path to the single length that is actually asked
    for, so no size cap or cache-eviction policy is needed to bound what an
    arbitrary caller could make this allocate.

    Why this is bit-identical rather than approximately equal:

    * Each addend is produced by the same three IEEE-754 operations on the same
      operands as before -- divide, ``log2``, multiply -- so each addend carries
      the same bit pattern. Precomputation changes WHEN a term is computed, never
      HOW.
    * ``Counter(token).values()`` still supplies the addends, in the same
      first-occurrence order, and ``map`` is consumed in order, so ``sum``
      accumulates identical addends in an identical sequence. The equality
      therefore does not rest on float addition being associative, which it is
      not. An algebraic rearrangement such as
      ``log2(length) - sum(c * log2(c)) / length`` IS mathematically equal and is
      measurably NOT bit-equal, which is why it is not used here.
    """
    if not token:
        return 0.0
    counts = Counter(token)
    length = len(token)
    if length != _SECRET_KEY_LEN:
        return -sum((c / length) * math.log2(c / length) for c in counts.values())
    return -sum(map(_ENTROPY_TERMS_KEY_LEN.__getitem__, counts.values()))


def _has_all_three_char_classes(text: str) -> bool:
    """Return True if *text* holds at least one lowercase, uppercase AND digit.

    One pass with early exit, rather than three ``any()`` scans. Semantically
    identical, but this is the hottest predicate in the redaction path:
    :func:`_contains_bare_secret` slides a 40-char window BYTE BY BYTE across a
    base64-alphabet run that clears its prefilters, so a 512-char run reaching that
    loop asks this question 473 times. Three ``any()`` scans build three generators
    per call and cost the SUM of their three first-match offsets; one loop breaks on
    completion and costs the MAX. Both forms short-circuit, so the saving is
    generator frames plus that sum-vs-max difference.

    Absence of a class is closed under substring, which is what lets
    :func:`_contains_bare_secret` ask this about a whole run and retire every
    window at once.
    """
    has_lower = has_upper = has_digit = False
    for ch in text:
        if not has_lower and ch.islower():
            has_lower = True
        elif not has_upper and ch.isupper():
            has_upper = True
        elif not has_digit and ch.isdigit():
            has_digit = True
        if has_lower and has_upper and has_digit:
            return True
    return False


# The byte set counted as "printable" by :func:`_decodes_to_printable_text`: tab,
# LF, CR and the printable ASCII range 0x20-0x7E. Held as ``bytes`` so the count
# can be delegated to ``bytes.translate``, which runs in C.
_PRINTABLE_BYTES: bytes = bytes(sorted({0x09, 0x0A, 0x0D} | set(range(0x20, 0x7F))))


def _decodes_to_printable_text(token: str) -> bool:
    """Return True if *token* base64-decodes to mostly-printable ASCII.

    Encoded human-readable text (a base64 document blob) decodes to printable
    bytes; a random 40-char secret key decodes to mostly non-printable bytes. We
    use this to exclude encoded-text blobs from the bare-secret heuristic (they
    are handled by the existing decode-and-scan pass instead).
    """
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), validate=False)
    except Exception:
        return False
    if not raw:
        return False
    # Count the printable bytes by DELETING them in C and measuring what is left,
    # rather than testing every byte in a Python loop. ``translate(None, set)``
    # returns exactly the bytes NOT in *set*, so ``len(raw) - len(...)`` is the
    # member count -- an integer identity, so the ratio and the comparison below
    # are bit-identical to the previous per-byte sum (asserted against a verbatim
    # copy of that sum in ``test_printable_count_matches_the_per_byte_sum``,
    # including all 256 single-byte inputs exhaustively).
    #
    # This is the single most expensive operation in pass 3, because the helper
    # runs once per base64-alphabet run AND again per 40-char window as gate 7 of
    # `_looks_like_secret_key`, and the old loop cost scaled with the DECODED byte
    # count rather than with the 40-char window. Measured 14.6x at 48 bytes rising
    # to 69x at 1500; a 2 KB encoded blob fell from 86.3 us to 1.2 us, which is
    # 98% of what `_contains_bare_secret` spent on such a run.
    printable = len(raw) - len(raw.translate(None, _PRINTABLE_BYTES))
    return printable / len(raw) >= _SECRET_PRINTABLE_DECODE_RATIO


def _lowercase_run_exceeds(token: str, cap: int) -> bool:
    """Return True if any run of consecutive lowercase letters is longer than *cap*.

    Dictionary-word identifiers and file-path segments contain long lowercase
    word runs; a uniformly random base64 secret almost never does. This is the
    primary discriminator that keeps camelCase identifiers and mixed-case file
    paths out of the bare-secret heuristic.

    The only question the caller asks is whether the longest run EXCEEDS a
    threshold, so this stops at cap+1 rather than scanning the whole token to
    find the true maximum. On the tokens this gate exists to reject -- the ones
    with a long lowercase run -- it exits after a handful of characters instead
    of all 40, which measured 3.97 -> 1.65 us per window.
    """
    current = 0
    for ch in token:
        if ch.islower():
            current += 1
            if current > cap:
                return True
        else:
            current = 0
    return False


def _vowel_ratio(token: str) -> float:
    """Return the fraction of alphabetic characters in *token* that are vowels.

    Deliberately left in this two-pass comprehension form. A single-pass rewrite
    measured 1.18x -- about 0.4 us on a 2.89 us gate -- which does not justify
    replacing the clearest possible expression of "fraction of letters that are
    vowels", and would owe its own independent-oracle test. Its neighbour
    :func:`_lowercase_run_exceeds` WAS rewritten because that one measured 2.4x.
    Do not optimise this unmeasured.
    """
    letters = [ch for ch in token if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if ch in _VOWELS) / len(letters)


def _looks_like_secret_key(token: str) -> bool:
    """Return True if *token* has the shape of a bare AWS secret access key.

    Conservative, multi-gate classifier for a label-less 40-char base64 secret.
    Every gate must pass; the design bias is toward NOT
    redacting (a false negative merely reverts to today's behavior, a false
    positive corrupts benign output).

    Gates are ordered by MEASURED cost per rejection, cheapest-per-reject first.
    Every gate is a pure predicate whose failure returns False, so the order is
    verdict-neutral and can be chosen purely for cost. Measured on a corpus of
    1705 windows that clear gates 1-3 (cost per window, share of windows that
    gate rejects on its own):

        lowercase run   1.65 us   66.5%  ->  2.5 us per rejection
        vowel ratio     2.89 us   62.3%  ->  4.6 us per rejection
        entropy         8.48 us   54.5%  -> 15.5 us per rejection
        decode          3.01 us    0.0%  ->  rejected nothing in that corpus

    These numbers are a SNAPSHOT from one corpus on one machine: treat them as a
    relative ranking, not a budget, and do not turn them into assertions (this
    repo's CI enables coverage on 3.12 only, so absolute durations are not
    comparable across shards). The ordering is the durable claim, and it is
    guarded by a test that counts which gates get evaluated -- see
    ``TestSecretGateOrderIsCostOrdered``.

    Putting the two cheap structural gates ahead of the entropy computation, and
    the decode check last, halves the cost of gates 4-7 and measured -47% on
    ``redact_credentials`` end to end. Do not reorder these back into
    "structural last" without re-measuring: the structural gates are both
    cheaper AND higher-yield than entropy, which is the opposite of the
    intuition that entropy is the primary discriminator.

    1. Length is EXACTLY 40 (AWS secret-key length).
    2. Contains all three of lower + upper + digit (rejects all-lower prose runs,
       all-upper CONSTANT_NAMES, base32, digit strings).
    3. Not an all-hex run (rejects git SHAs, sha256/md5 digests).
    4. No lowercase run longer than _SECRET_MAX_LOWER_RUN.
    5. Vowel ratio <= _SECRET_MAX_VOWEL_RATIO. Gates 4 and 5 are the
       structural-randomness pair: they separate a random key from word-based
       identifiers and slash-delimited file paths that survive the entropy
       floor. Both apply to EVERY token (a '/' or '+' does not exempt a token,
       so 40-char mixed-case file paths stay intact).
    6. Shannon entropy >= _SECRET_ENTROPY_MIN (rejects low-entropy repeats/prose
       and most code identifiers, which cluster below 4.3).
    7. Does not base64-decode to printable text (rejects encoded-text blobs).
       Last because it is the lowest-yield gate, not because it is optional --
       it is what keeps legitimate OAuth ``code_challenge`` values in sign-in
       URLs from being redacted (guarded by the OAuth-URL corpus).

    BOUNDARY ASSUMPTION: this classifier deliberately evaluates an EXACTLY-40-char
    window (gate 1). It does NOT itself scan longer runs — a real key glued to an
    adjacent base64 char with no delimiter (e.g. ``X`` + key, key + ``A``,
    ``SECRET=`` + key + ``ABC``, key + ``X`` + key) forms a 41+ char run that would
    fail the exact-40 gate and leak verbatim. Callers that receive raw ``{40,}``
    runs MUST use :func:`_contains_bare_secret`, which slides a 40-char window
    across the run so a glued secret is still caught. Keep the exact-40 shape here:
    it is what lets the structural gates cleanly separate real keys from 64-char
    sha256 hex, base64 document blobs, etc.
    """
    if len(token) != _SECRET_KEY_LEN:
        return False
    if not _has_all_three_char_classes(token):
        return False
    if _HEX_ONLY_RE.match(token):
        return False
    if _lowercase_run_exceeds(token, _SECRET_MAX_LOWER_RUN):
        return False
    if _vowel_ratio(token) > _SECRET_MAX_VOWEL_RATIO:
        return False
    if _shannon_entropy(token) < _SECRET_ENTROPY_MIN:
        return False
    return not _decodes_to_printable_text(token)


def _contains_bare_secret(run: str) -> bool:
    """Return True if any 40-char window of *run* looks like a bare secret key.

    :func:`_looks_like_secret_key` only accepts an EXACTLY-40-char token, but the
    ``_BARE_SECRET_RUN_RE`` boundary look-arounds capture the longest possible run
    of base64-alphabet chars. A genuine 40-char secret glued to an adjacent
    base64 char with no delimiter (``X`` + key, key + ``A``, ``SECRET=`` + key +
    ``ABC``, key + ``X`` + key) produces a 41+ char run that would fail the
    exact-40 gate and leak verbatim. We slide a 40-char window across the run and
    report a hit if ANY window clears every gate. This stays linear in the run
    length (the regex yields disjoint spans), so cost is bounded overall.

    ENCODED-TEXT-BLOB EXCLUSION: if the WHOLE run base64-decodes to printable
    text it is a cohesive encoded blob (e.g. an OAuth/PKCE ``code_challenge``,
    which is ``base64(sha256-hex)``), not a bare secret — those are handled by
    the decode-and-scan pass instead. We must skip it here because sliding a
    40-char window byte-by-byte across such a blob creates base64-*misaligned*
    sub-windows whose garbage decode looks high-entropy and would clear every
    per-window gate, wrongly redacting a legitimate sign-in URL (regression
    guarded by the OAuth-URL corpus). This is the same bias-toward-not-redacting
    that :func:`_looks_like_secret_key` already applies per-window (gate 7),
    lifted to run granularity so a misaligned window cannot defeat it. A genuine
    glued secret (``X`` + key, key + ``ABC``, key + ``X`` + key) does NOT decode
    cleanly as a whole run, so it still reaches the sliding window below.

    SEPARATOR AND COMMIT CEILINGS FOR A FRAGMENT. ``/`` is in the run alphabet, so
    a deep absolute path, or a permalink carrying a commit hash, is one run whose
    straddling sub-windows clear every per-window gate. A key-shaped window
    carrying a path's separator density (``_SECRET_MAX_SLASHES``) or crossing into a
    routed commit (``_ROUTED_COMMIT_RE``, ``_ROUTED_COMMIT_PIECE_MIN``) is declined,
    on two conditions that keep this a gate rather than a hole: only when the run is
    LONGER than one whole key (a 40-char run IS the token somebody wrote, so a
    standalone key is never subject to it), and only AFTER
    :func:`_looks_like_secret_key` has answered, so every offset is still classified
    and a glued key is still found at its own offset.
    """
    return next(_bare_secret_window_starts(run), None) is not None


def _bare_secret_window_starts(
    run: str, skip_spans: tuple[tuple[int, int], ...] = ()
) -> Iterator[int]:
    """Omit windows with >= _HOST_ID_EXEMPT_OVERLAP bytes in a run-relative *skip_spans* entry."""
    if len(run) < _SECRET_KEY_LEN:
        return
    # RUN-LEVEL FAST PATH. Two of the per-window gates reject on a property that
    # is closed under substring, so asking about the whole run once can retire
    # every window without classifying any of them:
    #   gate 2 -- a character class absent from the run is absent from all of its
    #             substrings, so no window can hold all three;
    #   gate 3 -- every substring of an all-hex run is itself all-hex.
    # Both answers are False either way, so this only reorders WHICH check
    # returns False, never the verdict. Guarded on a run longer than one window,
    # because at exactly 40 chars the sole window pays the same two gates anyway
    # and the pre-check would be pure duplicate work. This is what keeps the
    # slide affordable on long non-secret runs (hex digests, lowercase blobs),
    # which are the common shape in tool output.
    is_fragment = len(run) > _SECRET_KEY_LEN
    if is_fragment:
        if not _has_all_three_char_classes(run):
            return
        if _HEX_ONLY_RE.match(run):
            return
    if _decodes_to_printable_text(run):
        return
    commit_starts = (
        [match.start("commit") for match in _ROUTED_COMMIT_RE.finditer(run)] if is_fragment else []
    )
    for start in range(len(run) - _SECRET_KEY_LEN + 1):
        if skip_spans and any(
            min(start + _SECRET_KEY_LEN, end) - max(start, begin) >= _HOST_ID_EXEMPT_OVERLAP
            for begin, end in skip_spans
        ):
            continue
        window = run[start : start + _SECRET_KEY_LEN]
        if not _looks_like_secret_key(window):
            continue
        if is_fragment and (
            window.count("/") > _SECRET_MAX_SLASHES
            or _crosses_into_a_routed_commit(start, commit_starts)
        ):
            # Key-shaped, but a fragment carrying a path's separators or a commit.
            continue
        yield start


def _crosses_into_a_routed_commit(start: int, commit_starts: list[int]) -> bool:
    """Whether the window at *start* crosses a routed commit's opening ``/`` and
    takes ``_ROUTED_COMMIT_PIECE_MIN`` of its digits.

    Commits are a whole window long and a route apart, so only the first one
    starting after *start* can be crossed: one bisect, however many the run holds.
    """
    index = bisect.bisect_right(commit_starts, start)
    return (
        index < len(commit_starts)
        and commit_starts[index] + _ROUTED_COMMIT_PIECE_MIN <= start + _SECRET_KEY_LEN
    )


def _decode_b64_chunk(chunk: str) -> str:
    """Decode ONE `_B64_CHUNK_RE` match; return decoded credential text or ''.

    Equivalent to `_decode_b64_safe(chunk)` when *chunk* is itself a
    `_B64_CHUNK_RE` match, but without re-scanning it. `_decode_b64_safe` exists
    to find chunks inside arbitrary text; re-running that scan over a string that
    IS already one chunk can only rediscover the same single span —
    `[A-Za-z0-9+/]{40,}` is greedy so it consumes the whole run, and `={0,2}`
    takes the padding — so the inner `finditer` was pure duplicate work on the
    hot path, once per base64-looking run in every redacted message.
    """
    # NO LENGTH SHORT-CIRCUIT HERE, deliberately. It is tempting to skip the decode
    # when `len(chunk) % 4` is non-zero, on the reasoning that `validate=True`
    # rejects a length that is not a multiple of 4. That reasoning is INTERPRETER
    # DEPENDENT and would be a redaction bypass: `binascii.a2b_base64`'s padding
    # leniency changed with `strict_mode`, so on Python 3.10 and 3.11 a chunk of 40
    # data characters plus one `=` (length 41) DECODES, while on 3.12 it raises.
    # Skipping it would leave a base64-encoded credential in that shape unredacted
    # on exactly the interpreters CI still builds. No version-invariant form of the
    # test exists either -- 43 data characters plus `==` decodes on 3.10 while
    # failing both a total-length and a stripped-length predicate. Pinned by
    # `test_a_decode_length_precondition_would_be_version_dependent`.
    try:
        decoded = base64.b64decode(chunk, validate=True).decode("utf-8", errors="ignore")
    except Exception:
        return ""
    # Gate the alternation behind the cheap superset pre-filter, exactly as pass 1
    # does. `_might_contain_credential` may return True where the pattern would not
    # match but never False where it would, so the verdict cannot move -- only the
    # cost. Real decoded blobs almost never look like credentials: 0 of 18 in the
    # session corpus and 1 of 849 in a hash-heavy corpus reach the alternation.
    #
    # LENGTH-GATED, because here the pre-filter is NOT unconditionally cheaper. Its
    # ~540 ns floor is fixed while the alternation's cost scales with length, so
    # below `_PREFILTER_MIN_LEN` the alternation wins outright. A decoded blob is
    # exactly the size where that matters -- 48 raw bytes from a 64-char run, and
    # shorter once `errors="ignore"` drops invalid sequences, measured 12-31
    # characters -- so this straddles the crossover instead of sitting above it.
    if len(decoded) >= _PREFILTER_MIN_LEN and not _might_contain_credential(decoded):
        return ""
    return decoded if _contains_credential_pattern(decoded) else ""


def _decode_b64_safe(text: str) -> str:
    """Try to base64-decode chunks in text; return decoded content or ''.

    Deliberately left UNOPTIMISED. `_decode_b64_chunk` above is the hot-path
    single-chunk form, and this function is what pins it: the differential test
    asserts the two agree on every chunk in the corpus, and the pre-optimisation
    reference oracle calls this one. Applying the same gates here would make both
    sides of that comparison share the change and the check would stop detecting
    anything.
    """
    for m in _B64_CHUNK_RE.finditer(text):
        try:
            decoded = base64.b64decode(m.group(), validate=True).decode("utf-8", errors="ignore")
            if _contains_credential_pattern(decoded):
                return decoded
        except Exception:
            continue
    return ""


def _contains_fixed_credential(text: str) -> bool:
    """Return True for canonical literal or base64-encoded credentials.

    Deliberately excludes the bare 40-character entropy heuristic. OAuth
    front-channel state and PKCE values are high-entropy by design, while the
    canonical signatures and decoded credentials remain unambiguous.
    """
    return bool(_contains_credential_pattern(text) or _decode_b64_safe(text))


def _text_contains_bare_secret(text: str) -> bool:
    """Return True when *text* contains an isolated bare AWS-secret run."""
    return any(_contains_bare_secret(match.group()) for match in _BARE_SECRET_RUN_RE.finditer(text))


# ── Document links the bare-secret pass leaves alone ──
# `/` is in the run alphabet, so in `https://docs.google.com/document/d/<id>/edit`
# the whole of `com/document/d/<id>/edit` is ONE run, and a Google document id is
# ~44 uniformly random base64url characters: a 40-char window inside it clears
# every gate of `_looks_like_secret_key`. No entropy or structural gate can tell
# that id from a key, so the decision is made on the link instead. Pass 3 skips a
# run only when the run lies wholly inside a match of `_DOCUMENT_LINK_RE`, which
# admits nothing but a fixed route on a fixed host:
#   * HTTPS, a lowercase literal host right after the scheme (no userinfo, no
#     port), `docs.google.com`, `drive.google.com`, `<tenant>.atlassian.net`, or
#     a PlantUML server: a host whose first label is `plantuml` (after an
#     optional `www.`), such as `www.plantuml.com`;
#   * every path segment fixed or drawn from a closed class: a Google id is
#     25-72 chars of `[A-Za-z0-9_-]`, a Confluence space key is alphanumeric, a
#     page id is digits, a PlantUML diagram is `[/plantuml]/<png|svg|txt|uml>/`
#     then its deflate text in PlantUML's `[0-9A-Za-z_-]` alphabet;
#   * no piece of the link is a whole key: a piece exactly 40 characters long
#     voids the match when `_looks_like_secret_key` accepts it. Pieces are cut
#     twice, once at every non-run character (`/`, `-`, `_`, `%`, `.`, `~`)
#     and once also at `+`, the space of a Confluence title slug, so a key
#     pasted in as the id or as a title word is still redacted. A PlantUML
#     diagram is random-looking by construction, so it is judged by decoding
#     instead: it must inflate, whole, to printable text with nothing after the
#     stream but the encoder's zero padding, and every credential pass must
#     leave that text unchanged. The text inflated per call is capped (see
#     `_PLANTUML_SOURCE_CAP`). A key as the diagram, glued to one, or written
#     in its source voids the match;
#   * the link ends where the route ends, with one optional trailing `/`: a
#     further base64-alphabet character (a glued key, another `/segment`)
#     fails the match.
# The query and fragment are outside the match, so a key there, a `?token=`
# value and every pass-1 and pass-2 hit are judged exactly as before.
#
# ACCEPTED RESIDUAL: a markerless key without `/` glued to more letters or
# digits inside the id or the page title, or carrying its own `+` inside a
# title, is indistinguishable from the text it sits in and is not redacted.
# About 2% of real Google ids carry a key-shaped 40-char piece between their
# random `-`/`_` and stay redacted. That placement is not an accidental leak,
# and pass 3 is no control against a deliberate one: a single `-` already
# splits any run.
_DOCUMENT_ID = r"[A-Za-z0-9_-]{25,72}"
_PLANTUML_DIAGRAM = r"[A-Za-z0-9_-]{1,16384}"
_GOOGLE_USER = r"(?:u/[0-9]{1,2}/)?"
_RUN_PIECE_RES = (re.compile(r"[A-Za-z0-9+]+"), re.compile(r"[A-Za-z0-9]+"))
_DOCUMENT_LINK_RE = re.compile(
    r"(?<![A-Za-z0-9+.-])https://(?:"
    r"docs\.google\.com/(?:document|spreadsheets|presentation|drawings|forms)/"
    + _GOOGLE_USER
    + r"d/(?:e/)?"
    + _DOCUMENT_ID
    + r"(?:/(?:edit|view|preview|copy|viewform|pub|pubhtml|htmlview))?"
    r"|drive\.google\.com/"
    + _GOOGLE_USER
    + r"(?:file/d/"
    + _DOCUMENT_ID
    + r"(?:/(?:view|edit|preview))?"
    r"|drive/" + _GOOGLE_USER + r"folders/" + _DOCUMENT_ID + r")"
    r"|[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.atlassian\.net/wiki/spaces/~?[A-Za-z0-9]{1,64}/"
    r"(?:overview|pages/(?:edit-v2/)?[0-9]{1,20}"
    r"(?:/[A-Za-z0-9+%._~-]{1,255})?)"
    r"|(?:www\.)?plantuml(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+(?:/plantuml)?/(?:png|svg|txt|uml)/"
    r"(?P<diagram>" + _PLANTUML_DIAGRAM + r")"
    r")/?(?![A-Za-z0-9+/_-])"
)
_PLANTUML_ALPHABET = {
    c: i for i, c in enumerate("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_")
}
#: Decoded diagram text one call may inflate, in total, and per encoded
#: character. Real diagrams stay under 10x; anything larger keeps its link
#: masked, which is how it was judged before this route. The total bounds the
#: extra scan a call can be made to do to that of a plain text this long.
_PLANTUML_SOURCE_CAP = 16384
_PLANTUML_RATIO_CAP = 16
#: Set while a decoded diagram is being scanned, so a diagram link inside it is
#: judged as any other link and decoding never nests.
_IN_DIAGRAM_SCAN: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_IN_DIAGRAM_SCAN", default=False
)


def _plantuml_source(encoded: str, limit: int) -> str | None:
    """Return the diagram text *encoded* inflates to, whole, or None."""
    if limit <= 0 or len(encoded) % 4:
        return None
    bits = [_PLANTUML_ALPHABET[c] for c in encoded]
    raw = bytearray()
    for i in range(0, len(bits), 4):
        word = bits[i] << 18 | bits[i + 1] << 12 | bits[i + 2] << 6 | bits[i + 3]
        raw += word.to_bytes(3, "big")
    inflater = zlib.decompressobj(-zlib.MAX_WBITS)
    try:
        source = inflater.decompress(bytes(raw), limit).decode("utf-8")
    except (zlib.error, UnicodeDecodeError):
        return None
    # The encoder emits whole 4-character groups and pads only the last one,
    # with at most two zero bytes. A further group, a stream cut short, or one
    # over the limit is not a whole diagram, so text glued after a diagram, or a
    # diagram cut mid-group, is never kept.
    padding = inflater.unused_data
    if not inflater.eof or len(padding) > 2 or padding.strip(b"\0") or not source.strip():
        return None
    if not all(c.isprintable() or c in "\t\n\r" for c in source):
        return None
    return source
