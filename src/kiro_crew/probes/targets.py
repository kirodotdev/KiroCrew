"""Infer WHICH subject a monitor instruction is about, from its own text.

The point of this module is that nothing new has to be passed in. A babysit
instruction already names its subject -- "Babysit PR #42
(kirodotdev/KiroCrew, branch ...)" -- so asking the caller to also supply a
target parameter would add an opt-in, and an opt-in only pays off for the
callers that remember to pass it. Inference has no adoption problem because
there is nothing to adopt.

The whole design leans on one asymmetry. Failing to infer costs a loop that
keeps its existing timer -- today's behaviour, no regression. Inferring the
WRONG subject costs a loop that watches something else: it goes quiet about the
thing it was supposed to watch and wakes about a stranger. So every rule here
refuses on doubt, and the refusal path is the tested one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable, Iterable

from kiro_crew.probes import GH_PR, WORK_LEDGER

#: The host a public GitHub URL names, and the ONLY value this module ever pins.
#: A shorthand subject deliberately gets no host at all -- see :func:`infer`.
_PUBLIC_HOST = "github.com"

#: ``host_key`` for a subject that has no remote host: a conductor's own work
#: ledger is on this machine's disk. A distinct token rather than ``"default"``,
#: which means "whatever the operator's gh is configured for" and would read as a
#: remote this subject does not have.
_LOCAL_HOST = "local"

#: ``https://github.com/owner/name/pull/123`` (any host path prefix is refused
#: by the anchor -- an enterprise host is a different API and a different probe).
#:
#: The owner and repo quantifiers are BOUNDED, at GitHub's own limits: an account
#: name is at most 39 characters and a repository name at most 100. Unbounded
#: ``+`` here is a polynomial-ReDoS shape -- a long run of ``-`` with no following
#: ``/`` makes the engine retry the class from every start position -- and the input
#: is agent-authored prose, so the bound is load-bearing rather than cosmetic.
_PR_URL = re.compile(
    r"https?://(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9._-]{1,39})/(?P<repo>[A-Za-z0-9._-]{1,100})/pull/(?P<pr>\d+)\b"
)

#: ``owner/name#123``. NOT a gating source -- see :func:`infer` for why a shorthand
#: cannot decide a subject -- but still needed to notice that the text names ANOTHER
#: pull request besides the URL, which is what makes the URL ambiguous rather than
#: authoritative. Scanning URLs alone is not enough: with no shorthand scan, "drive
#: owner/name#42; blocked on <URL for #7>" gates on the BLOCKER, so #7 merging
#: retires a loop whose own work is #42.
#:
#: The lookbehind refuses a PATH fragment. A babysit instruction routinely cites
#: source locations, and ``src/kiro_crew/autonudge.py#91`` would otherwise read as
#: owner ``kiro_crew`` / repo ``autonudge.py`` / PR 91 -- so it would manufacture
#: an ambiguity out of a line reference and refuse to gate anything.
#:
#: Quantifiers bounded for the same reason as the URL pattern's.
_PR_SHORTHAND = re.compile(
    r"(?<![A-Za-z0-9._/#-])"
    r"(?P<owner>[A-Za-z0-9._-]{1,39})/(?P<repo>[A-Za-z0-9._-]{1,100})#(?P<pr>\d+)\b"
)

#: ``PR #42`` / ``pull request #42`` -- the most common way a person names a pull
#: request in an instruction, and like :data:`_PR_SHORTHAND` this exists ONLY for
#: ambiguity detection. It carries no owner or repo, so it can never select a
#: subject; it can only show that the instruction is talking about more than one.
#: The literal ``PR``/``pull request`` prefix is what keeps a source location like
#: ``autonudge.py#1751`` from reading as a pull request.
#:
#: The prefix covers a CHAINED list, because "PRs #42 and #7" carries the prefix
#: once and then relies on it: matching only the first number let the second one
#: through unseen, so a loop gated on the URL for #42 retired with the work on #7
#: unfinished. The chain is bounded to ``#N`` separated by a comma, ``and`` or ``&``
#: -- it stops at the first token that is neither -- so a later unrelated ``#88``
#: elsewhere in the instruction is not swept in.
_PR_BARE = re.compile(
    r"\b(?:PRs?|pull requests?)\s*(?P<chain>#\d{1,12}(?:\s*(?:,|and|&)\s*#\d{1,12})*)",
    re.IGNORECASE,
)

#: The individual numbers inside a matched chain.
_PR_BARE_NUMBER = re.compile(r"#(\d{1,12})")

#: ``PR 42`` / ``PR #42`` / ``pull request 42`` -- the reference an instruction makes
#: when it names its pull request by NUMBER ALONE, hash or no hash. Read only by
#: :func:`bare_pull_request_number`, and only for text that names no URL and no
#: shorthand, so it never takes part in :data:`_PR_BARE`'s ambiguity detection and
#: never widens that pattern's contract.
#:
#: The hash is optional because people drop it: an instruction reading
#: ``PR <number>`` with no hash is invisible to :data:`_PR_BARE`. Matching
#: ordinary prose ("pull requests 3 of 5") is tolerable HERE in a way it is not for an
#: ambiguity guard, because a number found by this pattern selects nothing on its own
#: -- it gates a loop only when the session's own log names that exact number as a
#: pull request of exactly one repository, see :func:`resolve_bare`.
_PR_BARE_LOOSE = re.compile(
    r"\b(?:PRs?|pull\s+requests?)\s*" r"(?P<chain>#?\d{1,12}\b(?:\s*(?:,|and|&)\s*#?\d{1,12}\b)*)",
    re.IGNORECASE,
)

#: The individual numbers inside a :data:`_PR_BARE_LOOSE` chain.
_PR_BARE_LOOSE_NUMBER = re.compile(r"#?(\d{1,12})")


@dataclass(frozen=True)
class Target:
    """One inferred subject, ready to hand to a driver."""

    kind: str
    #: Human identity, for logs and for the loop's own bookkeeping.
    subject: str
    #: The probe's configuration, in the shape the probe already parses.
    message: str
    #: A stable token for the host this subject resolves against, for a driver
    #: that keys per-subject state. ``"default"`` means "whatever the operator's
    #: gh is configured for", which is what a shorthand subject means -- so two
    #: spellings of the same slug that resolve to DIFFERENT servers get different
    #: keys, and a driver's dedupe memory moves with the host instead of
    #: suppressing the first real signal from the new one.
    host_key: str = "default"


def _pull_requests_named(text: str) -> set[tuple[str, str, int]]:
    """Every distinct pull request *text* names by full URL.

    ONLY an explicit public pull-request URL gates a loop. A bare
    ``owner/name#123`` proves neither of the two things this decision needs:

    * not that the subject is a PULL REQUEST -- ``#123`` is equally an issue
      reference, and a same-numbered pull request may exist and be merged, which
      would retire a loop that was watching the issue;
    * not WHICH SERVER it lives on -- a shorthand resolves through the operator's
      ambient gh configuration, so on an enterprise host the same slug names a
      different repository.

    Requiring the full URL also narrows what an agent-written message can cause:
    a credentialed (audited, read-only, fixed-argv) gh call now happens only for
    a subject the instruction spelled out in full. A shorthand-only instruction
    is simply not gated, which costs a turn per interval -- today's cost, and the
    safe direction.
    """
    found: set[tuple[str, str, int]] = set()
    if not isinstance(text, str) or not text:
        return found
    for match in _PR_URL.finditer(text):
        try:
            number = int(match.group("pr"))
        except ValueError:
            # ``\d+`` is unbounded, and CPython refuses to convert a decimal
            # string past its digit limit. The instruction is agent-written
            # prose, so a pathological run of digits must REFUSE the match
            # rather than raise out of inference: this function is called on
            # the arming path, where an exception would fail to arm the loop
            # at all instead of merely declining to gate it.
            continue
        if number <= 0:
            continue
        found.add((match.group("owner"), match.group("repo"), number))
    return found


def work_ledger_target(slot_key: str) -> Target | None:
    """The watch subject for *slot_key*'s OWN work ledger, or ``None``.

    Public because the arming surface and the driver both need the same answer,
    and because this is the one subject :func:`infer` cannot reach from text.

    A session's own identity is not in its prose. An instruction says "dispatch
    the queue and verify what comes back" -- the session key is nowhere in it, and
    no pattern can recover it, so the inference asymmetry the rest of this module
    relies on does not apply: there is nothing to guess wrong, only nothing to
    guess. That is why the work-ledger watch is asked for by an explicit field
    while the pull-request watch is inferred, and it is not an inconsistency to
    fix by adding a field to the other one: a PR watch has a nameable subject and
    an opt-in field for it would see the adoption this module's header describes.
    """
    key = str(slot_key or "").strip()
    if not key:
        return None
    return Target(
        kind=WORK_LEDGER,
        subject=key,
        host_key=_LOCAL_HOST,
        message=json.dumps({"conductor": key}),
    )


def names_pull_request(text: str) -> bool:
    """Whether *text* names a pull request in ANY grammar this module reads.

    Not the URL grammar alone. :func:`infer` SELECTS a subject only from a full URL,
    but it treats an ``owner/name#123`` shorthand or a bare ``PR #42`` as naming one
    too -- that is how it notices a second subject and refuses. A caller deciding
    whether some OTHER string may supply the subject has to use that same wider
    notion, or the ordinary "Babysit PR #42; blocked on <URL for #7>" instruction
    reads as naming nothing, the other string's entry is taken, and the subject
    becomes the BLOCKER -- so #7 merging retires the loop whose work is #42.

    Wider than ``infer(text) is not None`` in the other direction as well: text
    naming SEVERAL pull requests infers ``None`` while still naming one here, so an
    ambiguity this module deliberately refuses to resolve cannot be resolved by
    another string instead.

    Presence only, never selection: a shorthand still carries no host and ``#123`` is
    still equally an issue reference, so nothing here is a subject a loop can gate
    on. It answers one question -- is this text talking about a pull request at all.
    """
    if _pull_requests_named(text):
        return True
    if not isinstance(text, str) or not text:
        return False
    if _PR_SHORTHAND.search(text):
        return True
    return any(_PR_BARE_NUMBER.search(bare.group("chain")) for bare in _PR_BARE.finditer(text))


def bare_pull_request_number(text: str) -> int | None:
    """The ONE pull-request number *text* names by number alone, or ``None``.

    ``None`` whenever *text* could already decide -- or refuse -- a subject by
    itself: any full URL or ``owner/name#N`` shorthand in it keeps the text on the
    path :func:`infer` has always taken, so nothing here can turn a refusal there
    into a subject. Also ``None`` for two distinct numbers ("PR 42 after PR 7"), for
    the same reason :func:`infer` refuses two URLs, and for zero.
    """
    if not isinstance(text, str) or not text:
        return None
    if _pull_requests_named(text) or _PR_SHORTHAND.search(text):
        return None
    numbers: set[int] = set()
    for bare in _PR_BARE_LOOSE.finditer(text):
        for digits in _PR_BARE_LOOSE_NUMBER.findall(bare.group("chain")):
            numbers.add(int(digits))
    if len(numbers) != 1:
        return None
    number = numbers.pop()
    return number if number > 0 else None


def resolve_bare(text: str, context: Iterable[str]) -> Target | None:
    """Resolve *text*'s bare ``PR <number>`` against the session's own log, or ``None``.

    *text* supplies the NUMBER and the fact that it is a pull request -- it said
    "PR", which an ``issues/N`` link or a bare ``#N`` never does. *context* supplies
    only the REPOSITORY: the strings of the session that armed the loop, scanned for
    a full pull-request URL or an ``owner/name#N`` shorthand whose number is that same
    number. Nothing is guessed: no project git remote, no default repository.

    Exactly one repository or nothing. Two repositories naming the same number in
    one session is precisely the doubt this module resolves by declining, and the
    decline is today's behaviour -- the loop fires on its timer and the woken session
    reads the pull request itself.

    No host is pinned, whichever spelling the context used: the INSTRUCTION never
    named one, so the subject resolves through the operator's gh configuration and
    keys as ``"default"`` -- what :class:`Target` documents a shorthand subject to
    mean. One answer for every spelling is also what lets the loop's stored monitor
    (a canonical ``owner/name#N``, which cannot carry a host) reproduce the subject
    the arm resolved without reading the log again.
    """
    number = bare_pull_request_number(text)
    if number is None:
        return None
    slugs: dict[str, tuple[str, str]] = {}
    for chunk in context:
        if not isinstance(chunk, str) or not chunk:
            continue
        for owner, repo, found in _pull_requests_named(chunk):
            if found == number:
                slugs.setdefault(f"{owner}/{repo}".lower(), (owner, repo))
        for match in _PR_SHORTHAND.finditer(chunk):
            try:
                found = int(match.group("pr"))
            except ValueError:
                continue
            if found == number:
                slug = (match.group("owner"), match.group("repo"))
                slugs.setdefault(f"{slug[0]}/{slug[1]}".lower(), slug)
    # Case-folded, because GitHub slugs are: ``Owner/Repo`` and ``owner/repo`` are
    # one repository and must not read as two.
    if len(slugs) != 1:
        return None
    ((owner, repo),) = slugs.values()
    return _gh_pr_target(owner, repo, number, host=None)


def _gh_pr_target(owner: str, repo: str, number: int, *, host: str | None) -> Target:
    """The one place a gh-pr :class:`Target` is spelled."""
    slug = f"{owner}/{repo}"
    config: dict[str, object] = {"repo": slug, "pr": number}
    if host is not None:
        # Pinned whenever the spelling NAMED the host. The pin stops an ambient
        # ``GH_HOST`` from re-pointing the slug at a different server, where a
        # same-numbered pull request could be merged and retire a watch on a live one.
        config["host"] = host
    return Target(
        kind=GH_PR,
        subject=f"{slug}#{number}",
        host_key=host if host is not None else "default",
        # known_reds is deliberately absent: inference cannot know which reds
        # are inherited from the base branch, and inventing that list would
        # either suppress a real failure or wake on a known one. The woken agent
        # is where that judgment already lives.
        message=json.dumps(config),
    )


def infer(
    text: str,
    *,
    watch: str = "",
    slot_key: str = "",
    context: Callable[[], Iterable[str]] | None = None,
) -> Target | None:
    """Return the single subject *text* is about, or ``None``.

    *context*, when given, is called -- only then, and at most once -- for a text
    that names its pull request by number alone and so cannot select a subject by
    itself; see :func:`resolve_bare`. It is a callable so that the common text, which
    either names a URL or names no number at all, never pays for reading a log.
    Every caller that omits it gets exactly the answer this function gave before.

    ``None`` on every doubtful case, and specifically when the text names more
    than one distinct pull request. That case is common and it is exactly where
    guessing does damage: a babysit instruction routinely names its own PR *and*
    a PR it is blocked on ("gated on #7 merging first"), and a watch armed on
    the blocker would report the blocker's progress while staying silent about
    the PR the loop actually owns.

    *watch* names a subject kind EXPLICITLY and wins over the text when it is a
    kind that cannot be inferred. Today that is ``work-ledger`` alone, whose
    subject is the caller's own session (*slot_key*) -- see
    :func:`work_ledger_target`. A watch instruction is free to mention a pull
    request as well, so the explicit field has to take precedence rather than
    merge: a conductor's instruction that cites the PR its worker is driving
    still means "watch my ledger", and inferring the PR from it would arm the
    wrong subject with full confidence. Any other value FALLS THROUGH to the text
    -- including ``gh-pr``, which is inferrable, so passing a loop's own stored
    kind back in is always safe and never turns a working watch into ``None``.
    """
    if str(watch or "").strip() == WORK_LEDGER:
        return work_ledger_target(slot_key)
    if not isinstance(text, str) or not text:
        return None
    direct = _infer_from_text(text)
    if direct is not None or context is None:
        return direct
    if bare_pull_request_number(text) is None:
        return None
    try:
        return resolve_bare(text, context())
    except Exception:
        # The log is an optional source, read on the arming path and on every tick;
        # an unreadable one must cost the gate, never the loop.
        return None


def _infer_from_text(text: str) -> Target | None:
    """:func:`infer`'s answer from *text* alone -- the original, URL-only rule."""
    found = _pull_requests_named(text)

    # Exactly one subject, or nothing. Ambiguity is not resolved by preferring
    # the first mention: reading order does not tell which PR the loop owns, and
    # a rule that looks like it decides is worse than one that declines.
    if len(found) != 1:
        return None

    owner, repo, number = found.pop()
    # A SHORTHAND naming a different pull request makes the URL ambiguous rather
    # than authoritative. The common shape is a loop whose own subject is written
    # informally and whose BLOCKER is pasted as a link -- "drive owner/name#42;
    # blocked on <URL for #7>" -- where gating on the URL retires the loop the
    # moment the blocker merges, with its real work unfinished. A shorthand cannot
    # be trusted to SELECT a subject, but it is more than good enough to show that
    # the instruction is talking about more than one.
    for other in _PR_SHORTHAND.finditer(text):
        try:
            other_number = int(other.group("pr"))
        except ValueError:
            continue
        if (other.group("owner"), other.group("repo"), other_number) != (owner, repo, number):
            return None
    # The same argument for the BARE form, which is how a person actually writes it:
    # "Babysit PR #42; blocked on <URL for #7>" would otherwise gate on #7, so #7
    # merging retires a loop whose real work on #42 is unfinished. Only a DIFFERENT
    # number refuses -- "watch PR #42 <URL for #42>" is one subject named twice, which
    # is the ordinary phrasing and must keep gating. Over-refusing costs tokens;
    # under-refusing stops work, so this resolves the way the rest of the design does.
    for bare in _PR_BARE.finditer(text):
        for found_number in _PR_BARE_NUMBER.findall(bare.group("chain")):
            try:
                bare_number = int(found_number)
            except ValueError:
                continue
            if bare_number != number:
                return None
    return _gh_pr_target(owner, repo, number, host=_PUBLIC_HOST)
