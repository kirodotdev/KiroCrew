"""Settlement of pending mid-turn steers against a ``steering_consumed`` echo.

Pure and shared: the main chat and the ``/side`` sidecar both hand kiro-cli
fire-and-forget steers, and both need the same answer to "which of these did the
backend actually inject?". One subtle parser, two callers — a second copy would
drift, and the failure mode of a wrong answer is a silently lost question.
"""

from __future__ import annotations

import re

from kiro_crew.acp._dispatch import redact_text

#: kiro-cli wraps each injected steer in this envelope inside the echo text.
_BLOCK_RE = re.compile(r"<user_message>\n(.*?)\n</user_message>", re.DOTALL)


def settle_consumed_steers(
    pending: list[str],
    snapshot: str,
    *,
    settle_all_on_empty: bool = False,
) -> list[str]:
    """Return the entries of *pending* that ``snapshot`` did NOT account for.

    kiro-cli injects the CONCATENATION of every steer queued since the last
    consumption, and the echo carries each one ``<user_message>``-wrapped. Parse
    the snapshot into blocks and settle by EQUALITY: substring containment would
    false-positive a short steer against a longer one or against the wrapper
    text itself, and a falsely-settled steer is silently lost when the turn ends.

    Settling is COUNT-AWARE — each block settles at most one pending entry, so a
    duplicate identical steer registered after the snapshot stays pending
    instead of being swept by set membership. An entry registered after kiro-cli
    took its snapshot is simply not among the blocks and stays pending.

    An EMPTY echo (older backend, echo dropped) is no evidence of consumption. By
    default it settles NOTHING: the return value is what stays PENDING, a pending
    steer is requeued when the turn ends, so keeping everything costs at worst a
    visible, cancellable duplicate card, whereas settling on no evidence marks
    steers CONSUMED, suppresses the requeue, and loses the question with no trace.
    That matches an echo which is present but carries no recognisable envelope.

    ``settle_all_on_empty=True`` selects the opposite, and exists only because the
    main chat has long behaved that way; this argument keeps that path byte-identical
    rather than changing a behaviour whose requeue this change does not exercise.
    New callers should leave it False.
    """
    if not snapshot.strip():
        return [] if settle_all_on_empty else list(pending)
    counts: dict[str, int] = {}
    for block in _BLOCK_RE.findall(snapshot):
        # Already redacted upstream; re-running is idempotent and makes both
        # sides of the comparison come from the identical function rather than
        # relying on the caller having done it.
        key = redact_text(block)
        counts[key] = counts.get(key, 0) + 1
    # The steer RPC wraps message.strip(); pending stores the raw message.
    # Strip for parity so whitespace never causes a false NON-match.
    #
    # Redact for parity too. The ACP layer redacts the echo before it reaches
    # any surface, so a credential-bearing steer arrives back with the secret
    # replaced. Comparing that against the RAW pending text can never match,
    # and a falsely-unmatched steer is requeued — running an
    # already-injected question a second time, on exactly the questions where
    # that is least acceptable. Both sides go through the same redactor.
    keys = [redact_text(message.strip()) for message in pending]
    group_size: dict[str, int] = {}
    group_raw: dict[str, set[str]] = {}
    for message, key in zip(pending, keys):
        group_size[key] = group_size.get(key, 0) + 1
        group_raw.setdefault(key, set()).add(message.strip())

    # Decided up front, against the ORIGINAL echo counts: the loop below spends
    # `counts`, and asking mid-loop would judge later members against a count earlier
    # ones had already consumed.
    ambiguous = {
        key
        for key in group_size
        if len(group_raw[key]) > 1 and counts.get(key, 0) < group_size[key]
    }

    remaining: list[str] = []
    for message, key in zip(pending, keys):
        # Redacting for parity costs injectivity: two steers differing only in
        # credential material collapse to one key. If such a group is echoed fewer
        # times than its size, WHICH member was injected is unknowable from the echo
        # — and guessing is the worst option available, because `consumed` suppresses
        # the requeue, so a wrongly-settled entry is a question delivered NOWHERE.
        # Leave the whole group pending instead: a requeue re-asks visibly and
        # cancellably, which is recoverable in a way a silent loss is not.
        #
        # Only a group whose RAW texts differ is ambiguous. Identical raw texts are
        # interchangeable — one was injected, one was not, and they are the same
        # question — so count-based settling is correct there and stays.
        if key in ambiguous:
            remaining.append(message)
            continue
        if counts.get(key, 0) > 0:
            counts[key] -= 1
        else:
            remaining.append(message)
    return remaining
