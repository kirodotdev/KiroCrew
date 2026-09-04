"""Infer WHICH subject a monitor instruction is about, from its own text.

The point of this module is that nothing new has to be passed in. A babysit
instruction already names its subject -- "Babysit PR #7491
(kirodotdev/KiroCrew, branch ...)" -- so asking the caller to also supply a
target parameter would add an opt-in, and an opt-in is what the previous
attempts at this saving died of: the parameter existed, nobody passed it, and
the measured adoption was zero. Inference has no adoption problem because there
is nothing to adopt.

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

from kiro_crew.probes import GH_PR

#: The host a public GitHub URL names, and the ONLY value this module ever pins.
#: A shorthand subject deliberately gets no host at all -- see :func:`infer`.
_PUBLIC_HOST = "github.com"

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
#: authoritative. Round 23 deleted this pattern outright and that went too far: with
#: only URLs scanned, "drive owner/name#42; blocked on <URL for #7>" silently gated
#: on the BLOCKER, so #7 merging retired a loop whose own work was #42.
#:
#: The lookbehind refuses a PATH fragment. A babysit instruction routinely cites
#: source locations, and ``src/kiro_crew/autonudge.py#1751`` would otherwise read as
#: owner ``kiro_crew`` / repo ``autonudge.py`` / PR 1751 -- so it would manufacture
#: an ambiguity out of a line reference and refuse to gate anything.
#:
#: Quantifiers bounded for the same reason as the URL pattern's, and this is the one
#: CodeQL flagged: it scans arbitrary prose looking for a SECOND reference, so it
#: reads the whole instruction rather than stopping at a match.
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
#: -- it stops at the first token that is neither -- so a later unrelated ``#7511``
#: elsewhere in the instruction is not swept in.
_PR_BARE = re.compile(
    r"\b(?:PRs?|pull requests?)\s*" r"(?P<chain>#\d{1,12}(?:\s*(?:,|and|&)\s*#\d{1,12})*)",
    re.IGNORECASE,
)

#: The individual numbers inside a matched chain.
_PR_BARE_NUMBER = re.compile(r"#(\d{1,12})")


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


def infer(text: str) -> Target | None:
    """Return the single subject *text* is about, or ``None``.

    ``None`` on every doubtful case, and specifically when the text names more
    than one distinct pull request. That case is common and it is exactly where
    guessing does damage: a babysit instruction routinely names its own PR *and*
    a PR it is blocked on ("gated on #4137 merging first"), and a watch armed on
    the blocker would report the blocker's progress while staying silent about
    the PR the loop actually owns.
    """
    if not isinstance(text, str) or not text:
        return None

    found: set[tuple[str, str, int]] = set()
    # ONLY an explicit public pull-request URL gates a loop. A bare
    # ``owner/name#123`` was accepted here for several rounds and it proves
    # neither of the two things this decision needs:
    #
    # * not that the subject is a PULL REQUEST -- ``#123`` is equally an issue
    #   reference, and a same-numbered pull request may exist and be merged, which
    #   would retire a loop that was watching the issue;
    # * not WHICH SERVER it lives on -- a shorthand resolves through the operator's
    #   ambient gh configuration, so on an enterprise host the same slug names a
    #   different repository. That ambiguity produced three separate review
    #   findings on its own.
    #
    # Requiring the full URL also narrows what an agent-written message can cause:
    # a credentialed (audited, read-only, fixed-argv) gh call now happens only for
    # a subject the instruction spelled out in full. A shorthand-only instruction
    # is simply not gated, which costs a turn per interval -- today's cost, and the
    # safe direction.
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
    # "Babysit PR #42; blocked on <URL for #7>" gated on #7, so #7 merging retired a
    # loop whose real work on #42 was unfinished. Only a DIFFERENT number refuses --
    # "watch PR #42 <URL for #42>" is one subject named twice, which is the ordinary
    # phrasing and must keep gating. Over-refusing costs tokens; under-refusing stops
    # work, so this resolves the way the rest of the design does.
    for bare in _PR_BARE.finditer(text):
        for found_number in _PR_BARE_NUMBER.findall(bare.group("chain")):
            try:
                if int(found_number) != number:
                    return None
            except ValueError:
                continue
    slug = f"{owner}/{repo}"
    config: dict[str, object] = {"repo": slug, "pr": number}
    # Always pinned, because the only spelling that reaches here NAMED the host.
    # The pin stops an ambient ``GH_HOST`` from re-pointing the slug at a
    # different server, where a same-numbered pull request could be merged and
    # retire a watch on a live one.
    config["host"] = _PUBLIC_HOST
    return Target(
        kind=GH_PR,
        subject=f"{slug}#{number}",
        host_key=_PUBLIC_HOST,
        # known_reds is deliberately absent: inference cannot know which reds
        # are inherited from the base branch, and inventing that list would
        # either suppress a real failure or wake on a known one. The woken agent
        # is where that judgment already lives.
        message=json.dumps(config),
    )
