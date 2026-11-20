"""Cost and linearity pins for the two-leg ssh-to-self descent.

Split out of ``test_denied_commands_security_ssh_self.py`` to stay under the file-line cap;
the callers' import path is unchanged because these pins reference the floor directly.

The floor runs origin/main's substitution reading (``_substitution_reaches_self``) and the
fold scanner (``_folded_scanner_reaches_self``) as two legs. A shared-tokenizer memo
(``_modes_may_diverge``) lets them split a quote-free command once, but each leg still reads
its own frames, so the per-check work is about twice a single leg's. These pins hold the
ORDER (linear in command size), show the per-leg split, and bound EACH leg at main's 3x
(main's bound kept verbatim on its own leg, the scanner leg bounded the same on its own).
"""

from __future__ import annotations

import socket
from contextlib import ExitStack
from unittest.mock import patch

import pytest
from denied_commands_security_helpers import _denied_by

from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.security import argv_floor_ssh_bodies as _bodies


@pytest.fixture(autouse=True)
def _pin_resolver(_floor_monkeypatch):
    """Pin the own-host cache and the DNS-alias verdict so a benign dotted remote host
    (``remote.example.com``) reads as not-self without resolving a real name or spawning the
    own-host resolver thread -- the steady state the sibling file's own fixture sets. Uses
    ``_floor_monkeypatch`` (undone independently), not the shared ``monkeypatch`` a later
    ``monkeypatch.undo()`` would lift (D11)."""
    own = socket.gethostname().strip().lower()
    pinned = frozenset(name for name in {own, own.split(".", 1)[0]} if name)
    _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", pinned)
    _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
    _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))
    _floor_monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
    _floor_monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)


def test_each_leg_judges_nested_substitutions_linearly():
    # The constant-bound companion to the eval test, for the per-level JUDGING counter
    # (_is_ssh_to_self) rather than the tokenizer. Same ruling: main's 3x bound is kept
    # verbatim on its own leg and applied again, on its own, to the fold scanner leg,
    # instead of loosening the one bound to 5x of the two legs' combined work.
    def leg_chars(fn, depth: int) -> "tuple[int, int]":
        cmd = ("echo " + "$(" * depth + "ssh -p 22 remote.example.com id" + ")" * depth).lower()
        chars = [0]
        real = _argv_floor._is_ssh_to_self

        def counting(text, *args, **kwargs):  # type: ignore[no-untyped-def]
            chars[0] += len(text)
            return real(text, *args, **kwargs)

        # Patch the SHARED name through the facade, never on one owner (facade mirror guard:
        # _is_ssh_to_self is bound by argv_floor_ssh and argv_floor_ssh_bodies both).
        with patch.object(_argv_floor, "_is_ssh_to_self", counting):
            fn(cmd)
        return chars[0], len(cmd)

    for depth in (20, 40):
        for name, fn in (
            ("belt", _bodies._substitution_reaches_self),
            ("scanner", _bodies._folded_scanner_reaches_self),
        ):
            chars, length = leg_chars(fn, depth)
            assert chars <= 3 * length, (name, depth, chars, length)


@pytest.mark.parametrize("count", [200, 400])
def test_each_leg_tokenizes_linearly_in_the_input(count):
    cmd = ("eval " * count + "echo $(echo ssh)").lower()

    def leg_tokens(fn) -> int:  # type: ignore[no-untyped-def]
        chars = [0]
        real = _bodies._self_tokens

        def counting(text, **kwargs):  # type: ignore[no-untyped-def]
            chars[0] += len(text)
            return real(text, **kwargs)

        with patch.object(_bodies, "_self_tokens", counting):
            fn(cmd)
        return chars[0]

    # Opus's `a-ratchet-may-only-tighten` blocks loosening main's single-leg bound. Main's
    # bound was 3x of the input, measured on its ONE leg; this PR keeps that bound VERBATIM
    # on origin/main's own leg (its substitution reading) and applies the SAME 3x to the
    # fold scanner leg it adds -- two legs, each within main's bound, rather than loosening
    # the one bound to 5x of the combined work. The shared-tokenizer memo
    # (`_modes_may_diverge`) means the two legs split a quote-free command once, not twice,
    # but each leg still reads its own frames, so each is bounded on its own.
    belt = leg_tokens(_bodies._substitution_reaches_self)
    assert 0 < belt <= 3 * len(cmd), ("belt leg", count, belt, len(cmd))
    scanner = leg_tokens(_bodies._folded_scanner_reaches_self)
    assert 0 < scanner <= 3 * len(cmd), ("scanner leg", count, scanner, len(cmd))


def _eval_self_tokens_work(count: int) -> int:
    cmd = "eval " * count + "echo $(echo ssh)"
    chars = [0]
    real = _bodies._self_tokens

    def counting(text, **kwargs):  # type: ignore[no-untyped-def]
        chars[0] += len(text)
        return real(text, **kwargs)

    with patch.object(_bodies, "_self_tokens", counting):
        assert _denied_by(cmd) is None
    return chars[0]


def _nested_judge_work(depth: int) -> int:
    cmd = "echo " + "$(" * depth + "ssh -p 22 remote.example.com id" + ")" * depth
    chars = [0]
    real = _argv_floor._is_ssh_to_self

    def counting(text, *args, **kwargs):  # type: ignore[no-untyped-def]
        chars[0] += len(text)
        return real(text, *args, **kwargs)

    with patch.object(_argv_floor, "_is_ssh_to_self", counting):
        assert _argv_floor._reaches_self_over_ssh(cmd) is False
    return chars[0]


def test_the_two_leg_descent_work_stays_flat_in_size():
    """Order pin, independent of the 5x constant bounds above: doubling the input must not
    more than double the work. A leg that re-read the remainder per frame would be
    quadratic and push a ratio toward ~4; the two linear legs stay near 2. The 2.2 bound
    leaves headroom. repeated-eval is counted by _self_tokens (the descent's tokenization),
    nested-substitution by _is_ssh_to_self (the per-level judging) -- each the counter its
    own linearity test above uses."""
    e = [_eval_self_tokens_work(n) for n in (200, 400, 800)]
    assert e[1] / e[0] <= 2.2, ("repeated-eval", e)
    assert e[2] / e[1] <= 2.2, ("repeated-eval", e)
    # Nested `$()` is read one level at a time, which re-scans O(n) per level -- so the
    # EXECUTION is quadratic (on origin/main too), and the sizes stay modest; what stays
    # linear, and is pinned here, is the per-level JUDGING (_is_ssh_to_self sees a short
    # view per level, not the whole remainder).
    n = [_nested_judge_work(d) for d in (50, 100, 200)]
    assert n[1] / n[0] <= 2.2, ("nested-substitution", n)
    assert n[2] / n[1] <= 2.2, ("nested-substitution", n)


def test_the_two_legs_each_do_about_half_the_descent_work():
    """The cost is two legs, not one: origin/main's substitution reading and the fold
    scanner each tokenize the same text in their own mode (the shared memo dedups a
    quote-free split within one gate call, but measured standalone here each leg reads its
    own frames), so each does about half the total -- a reviewer sees the split (about 2027
    vs 2043 _self_tokens characters at count 200), not one opaque number. If either leg
    stopped doing real work this skews hard, so the share is pinned too."""
    cmd = "eval " * 200 + "echo $(echo ssh)"

    def leg_work(fn) -> int:  # type: ignore[no-untyped-def]
        chars = [0]
        real = _bodies._self_tokens

        def counting(text, **kwargs):  # type: ignore[no-untyped-def]
            chars[0] += len(text)
            return real(text, **kwargs)

        with patch.object(_bodies, "_self_tokens", counting):
            fn(cmd.lower())
        return chars[0]

    main_leg = leg_work(_bodies._substitution_reaches_self)
    scanner_leg = leg_work(_bodies._folded_scanner_reaches_self)
    split = "main-leg %d vs scanner-leg %d _self_tokens chars (count 200)" % (
        main_leg,
        scanner_leg,
    )
    assert 1500 <= main_leg <= 2600, split
    assert 1500 <= scanner_leg <= 2600, split
    assert 0.7 <= main_leg / scanner_leg <= 1.4, split


@pytest.mark.parametrize("n", [60, 120])
def test_a_verb_free_continuation_free_argv_is_not_descended(n):
    """The widened prefilter's load-bearing pin.

    A payload-heavy data-consumer argv -- many ``-c`` payloads, NO ssh-family verb and NO
    ``\\``-newline continuation -- must short-circuit before the fold scanner's per-frame
    descent. Without the short-circuit the descent runs once per nested payload and the argv
    sweep is quadratic in the payload COUNT, which the data-consumer linearity pin in
    ``test_denied_commands_security_cost.py`` catches. Counter-based, not timed: the
    scanner's tokenizer (``_self_tokens``) is not called at all for this shape. The MUTANT
    widens the prefilter to always-descend; it then descends and tokenizes, the shape this
    pin guards against."""
    text = ("bash" + "".join(f" -c a{i}pay" for i in range(n))).lower()
    # The shape genuinely hits neither prefilter escape -- no verb, no continuation.
    assert not _bodies._line_may_name_ssh_verb(text) and "\\\n" not in text

    def scanner_tokenizations(*, always_descend: bool) -> int:
        calls = [0]
        real = _bodies._self_tokens

        def counting(t, **kwargs):  # type: ignore[no-untyped-def]
            calls[0] += 1
            return real(t, **kwargs)

        with ExitStack() as stack:
            if always_descend:
                stack.enter_context(
                    patch.object(_bodies, "_line_may_name_ssh_verb", lambda *_a, **_k: True)
                )
            stack.enter_context(patch.object(_bodies, "_self_tokens", counting))
            # The verdict is False either way (no self-ssh here); only the WORK differs.
            assert _bodies._folded_scanner_reaches_self(text) is False
        return calls[0]

    assert scanner_tokenizations(always_descend=False) == 0, "prefilter did not short-circuit"
    assert scanner_tokenizations(always_descend=True) > 0, "always-descend mutant did not descend"
