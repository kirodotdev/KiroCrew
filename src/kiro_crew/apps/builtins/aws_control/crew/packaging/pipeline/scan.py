"""Credential scanning -- refuse, never warn.

Ported in INTENT from ``crew_export/scan.py``, which delegates to
``kiro_crew.deploy.scan`` for the canonical pattern set. That module is NOT
importable in this venv, so the hard-credential patterns below are a
self-contained subset. This is a real narrowing versus the source and is
called out in the track report: a credential shape the canonical set knows and
this subset does not would pass. The credential-NAME gate is ported verbatim.

``scan_text`` is the one scanner for the text a bundle SHIPS: the prompt, each skill file,
each MCP server definition and the rendered ``agent.json``, and every staged leaf once more
as it is written. A finding carries four characters of the match and its length, never the
matched bytes, so a refusal that quotes a finding does not print the secret it found.
"""

from __future__ import annotations

import base64
import math
import re
from collections.abc import Callable
from dataclasses import dataclass

# The vendor and forge token spellings are imported from the shared module so this
# subset cannot drift from the scrubber: a format added there reaches here with no
# edit, and no one-sided omission can hide. The fallback restates the same shapes
# for the standalone case where ``kiro_crew`` is not importable at all -- with the
# hyphen INSIDE the ``sk-proj-`` / ``sk-ant-`` classes and a length-flexible
# ``github_pat_``, the two spellings whose drifted forms had leaked.
try:  # pragma: no cover - exercised by whichever branch the environment allows
    from kiro_crew.credential_patterns import VENDOR_TOKEN_PATTERNS as _VENDOR_TOKEN_PATTERNS
except Exception:  # pragma: no cover
    _VENDOR_TOKEN_PATTERNS = (
        ("openai-project-key", r"sk-proj-[A-Za-z0-9_-]{16,}"),
        ("anthropic-key", r"sk-ant-[A-Za-z0-9_-]{16,}"),
        ("vendor-key", r"sk-[A-Za-z0-9]{20,}"),
        ("github-fine-grained-pat", r"github_pat_[A-Za-z0-9_]{40,}"),
        ("gitlab-pat", r"glpat-[A-Za-z0-9_-]{16,}"),
        ("npm-token", r"npm_[A-Za-z0-9]{24,}"),
        ("pypi-token", r"pypi-[A-Za-z0-9_-]{16,}"),
    )

#: The vendor/token fragments compiled with word boundaries for the standalone scan.
_VENDOR_TOKEN_COMPILED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(rf"\b{fragment}\b")) for label, fragment in _VENDOR_TOKEN_PATTERNS
)

# A presigned/signed link's signing value, sourced from the shared module so this
# subset cannot drift from it. The key ID (``AKIA`` / ``ASIA``, ``X-Amz-Credential``)
# is Identifier tier and stays allowed; what this refuses is the VALUE that makes the
# link work -- the SigV4/SigV2 signature and the session token -- so a bundle cannot
# ship a live bearer link. The fallback restates the same three alternatives for the
# standalone case where ``kiro_crew`` is not importable.
try:  # pragma: no cover - exercised by whichever branch the environment allows
    from kiro_crew.credential_patterns import URL_SIGNING_VALUE as _URL_SIGNING_VALUE
except Exception:  # pragma: no cover
    _URL_SIGNING_VALUE = (
        r"""(?i:X-Amz-Signature|X-Amz-Security-Token)["']?\s*[:=]\s*["']?[^\s"'&;#<>)\]}]+"""
        r"""|(?:[?&]|&amp;|%26)(?i:Signature)(?:=|%3[Dd])[^\s"'&;#<>)\]}]+"""
        r"""|(?i:Signature)(?:=|%3[Dd])[0-9a-fA-F]{64,}(?![0-9A-Za-z])"""
    )

_HARD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # No AWS access key ID (``AKIA`` / ``ASIA``) row: it is Identifier tier, so a bundle
    # that names one is not refused.
    # A LABELLED secret. The secret access key is 40 characters of base64 with no
    # prefix at all, so `SecretAccessKey=<secret>` in a prompt reached the deployed
    # image until this pattern. What makes it findable is the label, which is
    # how this repo's own detector finds it (`security.py:_HARD_CREDENTIAL_RE`,
    # described in security_posture_controls.py as covering "labelled secret-access-key and
    # session-token forms"). Spelled here from that same shape, and the canonical
    # module is preferred over it below when importable.
    (
        "aws-secret-labelled",
        re.compile(
            r"(?:SecretAccessKey|aws_secret_access_key|SessionToken|aws_session_token)"
            r"[\"']?\s*[:=]\s*[\"']?[^\s\"',}]+",
            re.IGNORECASE,
        ),
    ),
    # A presigned/signed link's signing value. Dropping the AKIA row above left the
    # standalone fallback with nothing that refused a signed link, so a bundle exported
    # without ``kiro_crew`` installed shipped a live SigV4/SigV2 bearer link intact. The
    # key ID stays allowed; only the signature and session token are refused.
    ("aws-signing-parameter", re.compile(_URL_SIGNING_VALUE)),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    # The same header after URL or form encoding, where the spaces have become ``+`` or
    # ``%20``. The shared detector spells its separator ``[\s+%]`` for exactly this, and
    # copying it as a literal space here left the encoded form unmatched -- measured against
    # ``_HARD_CREDENTIAL_RE``, ``BEGIN+RSA+PRIVATE+KEY`` was caught there and missed here.
    # A persona pasted out of a browser or a curl transcript arrives in that shape.
    (
        "private-key-encoded",
        re.compile(r"BEGIN[\s+%]+(?:RSA|DSA|EC|OPENSSH)[\s+%]+PRIVATE[\s+%]+KEY"),
    ),
    # An SSH PUBLIC key line. Not itself a secret, and that is not the test this scan
    # applies: the shared detector refuses these too, because a key line in a bundled
    # persona means a keypair was pasted in and the private half is very likely beside it.
    # Missing from the local subset until a comparison against the shared patterns was run
    # rather than eyeballed.
    ("ssh-public-key", re.compile(r"\b(?:ssh-rsa|ssh-ed25519)[\s+%]")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    # Vendor and forge API tokens (OpenAI project/vendor, Anthropic, fine-grained
    # GitHub PAT, GitLab PAT, npm, PyPI) sourced from the shared module above so the
    # standalone subset stays in lockstep with the scrubber. The fine-grained PAT and
    # the ``sk-proj-`` / ``sk-ant-`` forms are the shapes whose hand-restated spellings
    # here had drifted and shipped credentials unscanned in the deployment venv.
    *_VENDOR_TOKEN_COMPILED,
    # A JWT (three base64url segments split by dots, header starting ``eyJ``). Bearer tokens,
    # session tokens and signed credentials arrive in this shape pasted into a persona, and
    # the local set had no way to see one. The header segment is anchored on ``eyJ`` (``{"``
    # base64url-encoded) so an ordinary dotted identifier is not matched.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
)


@dataclass(frozen=True)
class Leak:
    origin: str
    kind: str
    line: int
    snippet: str

    def render(self) -> str:
        return f"{self.origin}:{self.line}: {self.kind}: {self.snippet}"


#: The repository's own hard-credential detector, when this module can reach it. The
#: local ``_HARD_PATTERNS`` above is a self-contained SUBSET and was documented as a
#: real narrowing; a review then found the exact gap that narrowing left (a labelled
#: AWS secret access key). So prefer the canonical one and keep the subset as the
#: fallback that lets this module run without ``kiro_crew`` installed.
try:  # pragma: no cover - exercised by whichever branch the environment allows
    from kiro_crew.security import _HARD_CREDENTIAL_RE

    _CANONICAL_CREDENTIAL_RE: re.Pattern[str] | None = _HARD_CREDENTIAL_RE
except Exception:  # pragma: no cover
    _CANONICAL_CREDENTIAL_RE = None

#: The repo's redactor, imported for its ENCODED-credential detection. The patterns above
#: all match a credential written literally, so a base64 of the same bytes matched none of
#: them. This one decodes base64 chunks, and its warning list is what ``scan_text`` reads;
#: the redacted text is discarded, because this module refuses rather than edits.
#:
#: Imported rather than restated for the reason the canonical pattern is: a local subset
#: needs a new entry per shape, which does not converge.
try:  # pragma: no cover - exercised by whichever branch the environment allows
    from kiro_crew.security import redact_credentials

    _CANONICAL_REDACTOR: Callable[[str], tuple[str, list[str]]] | None = redact_credentials
except Exception:  # pragma: no cover
    _CANONICAL_REDACTOR = None


#: A percent-encoded letter, digit, ``-`` or ``_``: the octets a parameter NAME is
#: spelled with, never a query separator.
_PCT_ALNUM_RE = re.compile(r"%(3[0-9]|4[1-9A-Fa-f]|5[0-9Aa]|6[1-9A-Fa-f]|7[0-9Aa]|2[Dd]|5[Ff])")

_BARE_SECRET_RUN_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}(?![A-Za-z0-9+/])")
#: A digest's name right before its run: the algorithm prefix of a
#: subresource-integrity digest (``sha256-``), or a field that names a hash
#: (``sha256: ``, ``"digest": "``, ``checksum=``).
_DIGEST_PREFIX_RE = re.compile(
    r"(?i:sha(?:1|224|256|384|512)|digest|checksum|integrity|hash)[\"' \t:=-]{1,4}\Z"
)
_BARE_SECRET_LEN = 40
_BARE_SECRET_ENTROPY_MIN = 4.3
_BARE_SECRET_MAX_LOWER_RUN = 5
_BARE_SECRET_MAX_VOWEL_RATIO = 0.30
_BARE_SECRET_HEX_ONLY_RE = re.compile(r"\A[0-9a-fA-F]+\Z")
_BARE_SECRET_VOWELS = frozenset("aeiouAEIOU")


def _bare_secret_decodes_to_printable(token: str) -> bool:
    """A base64 run whose decode is printable text is an encoded blob, not a bare key."""
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), validate=False)
    except Exception:
        return False
    if not raw:
        return False
    printable = sum(1 for b in raw if 0x20 <= b < 0x7F or b in (0x09, 0x0A, 0x0D))
    return printable / len(raw) >= 0.85


def _bare_secret_window_is_key(token: str) -> bool:
    """One 40-char window has the shape of a bare AWS secret access key.

    A faithful, self-contained mirror of the canonical structural classifier, so the
    standalone deployment-path scan is not strictly weaker than the canonical one for this
    known shape. Every gate must pass, and the bias is toward NOT flagging: a false negative
    reverts to prior behaviour, a false positive refuses a benign build. Gates: exactly 40
    chars; all three of lower + upper + digit (rejects prose and all-one-class runs); not
    hex-only (a git sha or hex digest); no lowercase run over the cap (rejects dictionary-word
    identifiers and path segments); vowel ratio at or under the cap; Shannon entropy at or
    above the floor; and it does not base64-decode to printable text (an encoded blob is the
    decode pass's job, not this one).
    """
    if len(token) != _BARE_SECRET_LEN:
        return False
    if not (
        any(c.islower() for c in token)
        and any(c.isupper() for c in token)
        and any(c.isdigit() for c in token)
    ):
        return False
    if _BARE_SECRET_HEX_ONLY_RE.match(token):
        return False
    run = 0
    for ch in token:
        run = run + 1 if ch.islower() else 0
        if run > _BARE_SECRET_MAX_LOWER_RUN:
            return False
    letters = [ch for ch in token if ch.isalpha()]
    if letters and (
        sum(1 for ch in letters if ch in _BARE_SECRET_VOWELS) / len(letters)
        > _BARE_SECRET_MAX_VOWEL_RATIO
    ):
        return False
    counts: dict[str, int] = {}
    for ch in token:
        counts[ch] = counts.get(ch, 0) + 1
    entropy = -sum((c / len(token)) * math.log2(c / len(token)) for c in counts.values())
    if entropy < _BARE_SECRET_ENTROPY_MIN:
        return False
    return not _bare_secret_decodes_to_printable(token)


def _scan_bare_secret_runs(text: str, origin: str) -> list[Leak]:
    """Findings for a bare, unlabelled AWS secret access key in *text*.

    Neither the canonical redactor (it redacts no value without a prefix, a label or a key ID
    beside it) nor the standalone patterns and decode pass catch it: a bare 40-char secret
    carries no label and decodes to non-UTF-8 bytes, so without this it ships. This gate
    refuses a build rather than rewriting output, so it runs in both modes. A structural DETECTOR rather than
    a fourth literal pattern -- the shape that converges. A genuine 40-char key glued to
    adjacent base64 characters yields a 41+ char run, so a 40-char window is slid across each
    run (disjoint spans keep it linear); a run that decodes whole to printable text is a
    cohesive encoded blob and is left to the decode pass.
    """
    found: list[Leak] = []
    for match in _BARE_SECRET_RUN_RE.finditer(text):
        run = match.group(0)
        if _DIGEST_PREFIX_RE.search(text, max(0, match.start() - 16), match.start()):
            # A digest (``sha256-<base64>``, ``sha256: <base64>``) is a hash of a
            # file the bundle ships, not a key: its name says so.
            continue
        if _bare_secret_decodes_to_printable(run):
            continue
        # Inside a URL the run is a path, and its ``/`` joins path segments
        # (``kirodotdev/KiroCrew/blob/<sha>/src``): each segment is judged alone,
        # so a window never straddles two names. A key that is itself a path
        # segment is still found, and a run no segment of which is long enough
        # to be one is judged whole.
        token_start = max(text.rfind(c, 0, match.start()) for c in " \t\n\"'(<") + 1
        pieces = re.findall(r"[^/]+", run) if "://" in text[token_start : match.start()] else [run]
        if max(map(len, pieces), default=0) < _BARE_SECRET_LEN:
            # No segment could hold a key alone, so a key with its own ``/`` in
            # it (``.../wJalr.../K7MDENG/...``, ``?k=<key>``) is read whole.
            pieces = [run]
        windows = (
            piece[i : i + _BARE_SECRET_LEN]
            for piece in pieces
            for i in range(0, len(piece) - _BARE_SECRET_LEN + 1)
        )
        leak = next((w for w in windows if _bare_secret_window_is_key(w)), None)
        if leak is not None:
            found.append(
                Leak(
                    origin=origin,
                    kind="bare-secret",
                    line=0,
                    snippet=leak[:4] + "…(%d chars)" % len(leak),
                )
            )
    return found


def scan_text(text: str, origin: str) -> list[Leak]:
    """Hard credential findings in *text*. A finding aborts the build."""
    leaks: list[Leak] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        # A name may be spelled with percent-encoded letters (``X-Amz-S%69gnature``);
        # read the line with those letters decoded too. Separators stay encoded.
        decoded = _PCT_ALNUM_RE.sub(lambda o: chr(int(o.group(1), 16)), line)
        for kind, pattern in _HARD_PATTERNS:
            m = pattern.search(line) or (pattern.search(decoded) if decoded != line else None)
            if m:
                token = m.group(0)
                snippet = token[:4] + "…(%d chars)" % len(token)
                leaks.append(Leak(origin=origin, kind=kind, line=lineno, snippet=snippet))
        if _CANONICAL_CREDENTIAL_RE is not None:
            m = _CANONICAL_CREDENTIAL_RE.search(line)
            if m:
                token = m.group(0)
                leaks.append(
                    Leak(
                        origin=origin,
                        kind="repo-credential-detector",
                        line=lineno,
                        snippet=token[:4] + "…(%d chars)" % len(token),
                    )
                )
    # Encoded credentials, via the repo's OWN redactor rather than a fourth local pattern.
    #
    # ``_HARD_PATTERNS`` and the canonical detector both match a credential written
    # literally. A base64 of the same bytes matches neither, so a labelled secret survived
    # every scan and shipped -- and this module already knows that adding one more local
    # pattern per shape is what does not converge, which is why the prompt fence prefers
    # ``is_sensitive_path`` over its own list.
    #
    # ``redact_credentials`` decodes base64 chunks and reports what it found, so its WARNING
    # list is the signal here; the redacted text is discarded because this function refuses
    # rather than edits. Run over the whole text, not per line: an encoded blob can wrap.
    if _CANONICAL_REDACTOR is not None:
        try:
            _, warnings = _CANONICAL_REDACTOR(text)
        except Exception:  # a detector fault must not become a silent pass
            warnings = ["credential redactor raised; treating the content as unscannable"]
        for warning in warnings:
            leaks.append(Leak(origin=origin, kind="repo-redactor", line=0, snippet=warning[:80]))
    else:
        # The import failed, which is the documented standalone mode. Encoded detection must
        # not simply VANISH with it: a build that silently stops looking for a class of leak
        # is worse than one that never claimed to, because the plan's notes still say the
        # content was scanned.
        #
        # So the fallback DECODES rather than re-describing what a credential looks like. It
        # feeds ``_HARD_PATTERNS`` -- the same patterns the literal pass uses -- over the
        # decoded bytes. That is deliberately not a fourth local credential pattern: adding
        # one pattern per shape is the shape that does not converge, and a decoder
        # inherits every future pattern for free where a pattern list would not.
        leaks.extend(_scan_decoded_runs(text, origin))
    # A label-less 40-char secret matches none of the passes above in either mode: the
    # canonical redactor judges a value by its label or the key ID beside it, never by
    # its shape alone. That is right for text it rewrites, but this gate refuses a build,
    # so the structural detector runs in both modes and a bare key never ships. A digest
    # is skipped by its name (``_DIGEST_PREFIX_RE``).
    leaks.extend(_scan_bare_secret_runs(text, origin))
    return leaks


#: Base64 runs long enough to hide a credential. The floor is 20 characters, not 40: 40 is
#: the length of an AWS *secret access key* specifically, but ``_HARD_PATTERNS`` also matches
#: shorter secrets (a labelled ``aws_secret_access_key=<value>`` fragment, a vendor ``sk-``
#: key, a github/slack token) whose base64 run is well under 40 chars, and in the standalone
#: deployment venv this decoder is the REAL scan path (the canonical redactor is not
#: importable), not a rare fallback. 20 base64 chars decode to ~15 bytes -- long enough to
#: carry a short credential, short enough that a bare word is not decoded as one.
_B64_RUN_RE = re.compile(r"[A-Za-z0-9+/]{20,}={0,2}")

#: Ceiling on how much of one text is decoded, so a large file cannot turn the scan into the
#: build's slowest step. Runs are examined longest-first, because a credential plus its label
#: is longer than a bare token and the long runs are the ones worth the budget.
_B64_DECODE_BUDGET = 256 * 1024


def _scan_decoded_runs(text: str, origin: str) -> list[Leak]:
    """Findings from base64 runs in *text*, judged by the same patterns as the literal pass.

    Not recursive: one decode. A credential wrapped twice is out of scope here and stays with
    the canonical redactor, which is preferred whenever it can be imported.
    """
    found: list[Leak] = []
    spent = 0
    skipped_unscanned = 0
    # Longest first, because a credential plus its label is longer than a bare token, so the
    # long runs are the ones worth the budget.
    #
    # ``continue`` and NOT ``break``. This was ``break``, and combined with that ordering it
    # made a single oversized run disable the scan completely: the longest run is examined
    # first, so if it alone exceeded the budget the loop exited before reading anything, and
    # every shorter run -- including the one carrying the credential -- went unscanned. A
    # blob big enough to trip the ceiling is trivially easy to include, which turned a memory
    # bound into an off switch.
    for match in sorted(_B64_RUN_RE.finditer(text), key=lambda m: -len(m.group(0))):
        run = match.group(0)
        if spent + len(run) > _B64_DECODE_BUDGET:
            # FAIL CLOSED. ``continue`` alone was still a silent pass: a credential inside a
            # run past the budget went unscanned and the output said the content was clean.
            # ``break`` was worse (one oversized run disabled everything) but both shared the
            # same flaw -- unscanned reported as scanned. A Leak is appended instead, so the
            # build refuses and names what it could not read.
            skipped_unscanned += 1
            continue
        spent += len(run)
        try:
            raw = base64.b64decode(run + "=" * (-len(run) % 4), validate=True)
            decoded = raw.decode("utf-8", errors="strict")
        except (ValueError, UnicodeDecodeError):
            # Not base64, or not text once decoded. Either way there is nothing here that the
            # literal patterns could read, so it is not a finding.
            continue
        for kind, pattern in _HARD_PATTERNS:
            hit = pattern.search(decoded)
            if hit:
                token = hit.group(0)
                found.append(
                    Leak(
                        origin=origin,
                        kind=f"encoded-{kind}",
                        line=text.count("\n", 0, match.start()) + 1,
                        snippet=token[:4] + "…(%d chars, base64)" % len(token),
                    )
                )
    if skipped_unscanned:
        found.append(
            Leak(
                origin=origin,
                kind="unscannable-encoded",
                line=0,
                snippet=(
                    "%d base64 run(s) past the %d byte decode budget were NOT scanned"
                    % (skipped_unscanned, _B64_DECODE_BUDGET)
                ),
            )
        )
    return found
