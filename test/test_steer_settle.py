"""Settlement of pending steers against a ``steering_consumed`` echo.

The wrong answer here loses a user's question silently, so each rule gets a test:
equality (not containment), count-awareness, and settle-all on an unusable echo.
Shared by the main chat and the /side sidecar.
"""

from __future__ import annotations

from kiro_crew.acp._dispatch import redact_text
from kiro_crew.dashboard.steer_settle import settle_consumed_steers


def _echo(*messages: str) -> str:
    return "".join(f"<user_message>\n{m}\n</user_message>" for m in messages)


def test_a_consumed_steer_is_settled():
    assert settle_consumed_steers(["use QUIC"], _echo("use QUIC")) == []


def test_a_steer_registered_after_the_snapshot_stays_pending():
    remaining = settle_consumed_steers(["first", "second"], _echo("first"))
    assert remaining == ["second"]


def test_settling_matches_by_equality_not_containment():
    """A short steer must not be settled by a longer one that contains it —
    a falsely-settled steer is never requeued, so the question is lost."""
    remaining = settle_consumed_steers(["ls"], _echo("please run ls in /tmp"))
    assert remaining == ["ls"]


def test_settling_is_count_aware():
    """One echoed block settles exactly one pending entry, so a duplicate
    submitted after the snapshot survives instead of being swept."""
    remaining = settle_consumed_steers(["retry", "retry"], _echo("retry"))
    assert remaining == ["retry"]


def test_a_redaction_collision_settles_nothing_rather_than_guessing():
    """Two DIFFERENT steers that redact to the same key are indistinguishable in the
    echo. Settling either would mark a distinct question consumed — and `consumed`
    suppresses the requeue, so that question would be delivered nowhere. Both stay
    pending instead: the requeue re-asks visibly, which is recoverable.
    """
    a = "deploy with AKIAIOSFODNN7EXAMPLE now"
    b = "deploy with AKIAI44QH8DHBEXAMPLE now"
    # Precondition: the two really do collide once redacted, or this test proves
    # nothing about the case it names.
    assert redact_text(a) == redact_text(b), "fixture no longer collides"

    remaining = settle_consumed_steers([a, b], _echo(redact_text(a)))
    assert remaining == [a, b], "an ambiguous group must stay pending in full"


def test_a_redaction_collision_settles_when_every_member_was_echoed():
    """Ambiguity is about attribution, not about redaction. When the echo accounts
    for the whole group there is nothing left to attribute, so both settle."""
    a = "deploy with AKIAIOSFODNN7EXAMPLE now"
    b = "deploy with AKIAI44QH8DHBEXAMPLE now"
    key = redact_text(a)
    assert redact_text(b) == key

    remaining = settle_consumed_steers([a, b], _echo(key) + _echo(key))
    assert remaining == []


def test_whitespace_does_not_cause_a_false_non_match():
    """The RPC wraps ``message.strip()`` while pending holds the raw text."""
    assert settle_consumed_steers(["  spaced  "], _echo("spaced")) == []


def test_an_unusable_echo_settles_everything():
    """An empty echo means the backend gave no usable text, so it is no evidence
    of consumption and must settle NOTHING.

    The return value is what stays PENDING, and a pending steer is requeued when
    the turn ends. Keeping these costs at worst a duplicate card the user can
    cancel; settling them marks the steers CONSUMED, suppresses the requeue, and
    loses the questions silently. Matches
    `test_an_echo_without_recognisable_blocks_keeps_entries_pending` — the two
    used to assert opposite directions."""
    assert settle_consumed_steers(["a", "b"], "") == ["a", "b"]
    assert settle_consumed_steers(["a", "b"], "   ") == ["a", "b"]


def test_an_echo_without_recognisable_blocks_keeps_entries_pending():
    """Text present but carrying no envelope settles nothing unless it EQUALS the
    steer — the bare-echo path reads it as one block, and this prose matches none,
    so the safe direction is a duplicate card, never a silent loss."""
    assert settle_consumed_steers(["a"], "some unrelated prose") == ["a"]


def test_a_redacted_echo_still_settles_its_steer():
    """The ACP layer redacts the echo before it reaches any surface, so a
    credential-bearing steer comes back with the secret replaced. Comparing that
    against the RAW pending text never matched, and the unmatched steer was
    requeued — running an already-injected question a second time."""
    # An AWS access key ID is a pattern redact_text actually rewrites.
    question = "deploy using AKIAIOSFODNN7EXAMPLE now"

    # The echo carries what ACP produced: the redacted form.
    from kiro_crew.acp._dispatch import redact_text

    echoed = redact_text(question)
    assert echoed != question, "fixture is pointless unless redaction changes it"

    remaining = settle_consumed_steers([question], _echo(echoed))
    assert remaining == [], "a redacted echo must still settle its own steer"


def test_redaction_parity_does_not_settle_an_unrelated_steer():
    """Redacting both sides must not collapse DIFFERENT secrets into one match."""
    from kiro_crew.acp._dispatch import redact_text

    # Distinct surrounding prose keeps the two distinguishable after the
    # secret itself is masked, which is the property that matters: parity
    # must not collapse two different steers into one match.
    a = "deploy using AKIAIOSFODNN7EXAMPLE now"
    b = "rotate AKIAIOSFODNN7EXAMPLE tomorrow"
    # Only a's echo arrives.
    remaining = settle_consumed_steers([a, b], _echo(redact_text(a)))
    assert remaining == [b], f"b must stay pending, got {remaining}"


def test_a_bare_kas_echo_settles_its_steer():
    """KAS's ``steering_injected`` carries its content VERBATIM, with no
    ``<user_message>`` envelope, so wrapper-only parsing found no blocks and left the
    steer pending. A pending steer is requeued when the turn ends, so a question the
    backend had already injected ran a SECOND time while its row claimed it never
    applied. The bare echo is evidence of consumption and must settle its own entry.
    """
    assert settle_consumed_steers(["use QUIC"], "use QUIC") == []


def test_a_bare_echo_settles_by_equality_not_containment():
    """The bare path is a SECOND enforcement site for the equality rule, so it needs
    its own case: a snapshot that merely CONTAINS the steer must settle nothing, or a
    KAS turn would mark a question consumed that was never injected and lose it."""
    assert settle_consumed_steers(["ls"], "please run ls in /tmp") == ["ls"]


def test_a_bare_echo_stays_count_aware():
    """One bare block settles at most one pending entry, matching the wrapped path:
    a duplicate identical steer registered after the snapshot must survive."""
    assert settle_consumed_steers(["retry", "retry"], "retry") == ["retry"]


def test_a_bare_echo_settles_a_redacted_steer():
    """Redaction parity has to hold on the bare path too. ACP redacts the echo
    before it reaches any surface, so a credential-bearing steer comes back masked;
    comparing that against the RAW pending text would never match and the entry
    would be requeued — re-running an already-injected question, on exactly the
    questions where that is least acceptable."""
    question = "deploy using AKIAIOSFODNN7EXAMPLE now"
    echoed = redact_text(question)
    assert echoed != question, "fixture is pointless unless redaction changes it"
    assert settle_consumed_steers([question], echoed) == []


def test_a_bare_echo_tolerates_surrounding_whitespace():
    """Mirrors the wrapped path's whitespace case for the bare one. A wrapped block
    arrives already stripped because the RPC wraps ``message.strip()``, but a bare
    ``content`` field carries whatever the backend put there, so the bare block is
    stripped for the same parity — otherwise a padded echo is a false NON-match and
    the steer is requeued and re-run."""
    assert settle_consumed_steers(["deploy now"], "  deploy now\n") == []
