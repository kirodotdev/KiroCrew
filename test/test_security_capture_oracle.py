"""The credential-mint floor binds on the verb's PRESENCE in a substitution.

What a substitution prints is outside the static floor.  Every generated BODY is
read twice, as ``python -m kiro_crew "$(BODY)"`` and as ``T=$(: "'"; BODY); kirocrew
$T``, and the floor must refuse exactly the bodies that name the verb as a word.  As an
interpreter flag's operand (``python -X "$(BODY)" -m kiro_crew doctor``) it never mints.

Bash itself then measures the trade.  Every BODY is captured as ``printf %s
"$(BODY)"`` (its own command substitution, batched a thousand to a bash child) in a
temporary directory, where only ``echo``, ``printf``, ``cat`` and
``:`` ever execute.  Bodies that name the verb but whose output bash discards are
refused on purpose (the fail-closed delta).  Bodies that compute the verb without
naming it are allowed, as on main (the residual).  Both are counted, so a change to
either is visible.
"""

from __future__ import annotations

import itertools
import os
import re
import shutil
import subprocess

import pytest

from kiro_crew.security import argv_floor_self
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

VERB = "token"

PRINTERS = [
    f"echo {VERB}",
    "echo doctor",
    f"printf {VERB}",
    "printf doctor",
    "printf %s%s tok en",
    f"printf '%s\\n' {VERB}",
    "printf 'doc%s' tor",
    f"printf %s {VERB} doctor",
    f"cat <<<{VERB}",
    "<<<doctor cat",
    f"command printf {VERB}",
    f": echo {VERB}",
    "echo -e 'tok\\x65n'",
    f"echo 'a; {VERB} ; b'",
]

REDIRECTS = [
    "",
    ">f",
    ">>f",
    ">|f",
    "> f",
    ">&2",
    "1>&2",
    "1>&3",
    "&>f",
    ">& f",
    ">/dev/null",
    ">/dev/stdout",
    ">/dev/fd/1",
    "2>&1",
    "2>&1 >&2",
    ">&2 2>&1",
    ">/dev/null 2>&1",
]

TAILS = [
    "",
    "; echo doctor",
    f"; echo 'ignore; {VERB} ; text' >&2",
    f" # {VERB}",
]

# ``printf`` after the ``--`` option terminator, and with an option the reader does not compute.
DASH_PRINTERS = [
    f"printf -- {VERB}",
    "printf -- --version",
    f"printf -- -%s {VERB}",
    "printf -- %s%s tok en",
    "printf -- --",
    f"printf -v x {VERB}",
]

# Words that look like operators but are quoted or escaped: arguments, never redirects.
LOOKALIKE_PRINTERS = [
    f'printf {VERB} ">/dev/null"',
    f"echo {VERB} '|' cat",
    f'echo {VERB} "2>&1"',
    f"echo {VERB} \\>f",
    f"printf %s {VERB} '>&2'",
    f"echo '>' {VERB}",
    f'echo "{VERB};" x',
]

# Descriptor duplications, swaps and closes beyond fds 1 and 2.
FD_REDIRECTS = [
    "3>&1 1>&3",
    "3>&1 1>/dev/null 1>&3",
    "3>&1 >/dev/null 1>&3",
    "3>&1 1>&3 3>&-",
    "3>&1 1>&3-",
    "3>/dev/null 1>&3",
    "3>f 1>&3",
    "2>&1 1>&2",
    "1>&2 2>&1",
    ">&2 2>&1 1>&2",
    "2>&1 >/dev/null",
    "1>&-",
    "1>&3 3>&-",
    "{v}>&1",
    "{v}>/dev/null",
    ">/dev/stderr",
    "3>&1 >/dev/fd/3",
]

# Pipelines: only the last stage writes to the capture.
PIPELINES = [
    "{p} | cat",
    "{p} | cat >f",
    "{p} | cat >/dev/null",
    "{p} >/dev/null | cat",
    "{p}>/dev/null|cat",
    "{p} 2>&1 | cat",
    "{p} >&2 2>&1 | cat",
    "{p} |& cat",
    "{p} >&2 |& cat",
    "{p} | echo doctor",
    "{p} | cat | cat >f",
    "{p} | cat | cat",
    "{p} || echo doctor",
    "{p} && echo doctor",
]


#: Bodies per bash child.  One child per body was ~6,900 sequential ``bash -c``
#: starts (each also forking for its ``$( )``), which under Git Bash on the Windows
#: shard ran past the per-test timeout and killed the xdist worker.
_BATCH = 1000


def _bash_captures(bodies: "list[str]", cwd: str) -> "list[str]":
    """What ``printf %s "$(BODY)"`` prints for each body, in order.

    The bodies run in a few bash children rather than one each.  Every body is still
    its OWN command substitution (``$(eval BODY)``), so one body's redirects, exit
    status and output cannot reach the next, and each result is printed behind its
    index and NUL-delimited, so a result is attributed to the body that produced it
    and a missing one names that body.
    """
    out: dict[int, str] = {}
    for start in range(0, len(bodies), _BATCH):
        lines = []
        for index, body in enumerate(bodies[start : start + _BATCH], start):
            quoted = "'" + body.replace("'", "'\\''") + "'"
            # The newline keeps a trailing ``# comment`` from swallowing the ``)``.
            lines.append(f"__b={quoted}\nprintf '%s\\0%s\\0' {index} \"$(eval \"$__b\"$'\\n')\"")
        script = os.path.join(cwd, f"bodies-{start}.sh")
        with open(script, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
        run = subprocess.run(
            ["bash", script],
            cwd=cwd,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=FALLBACK_TIMEOUT_SECS,
        )
        parts = run.stdout.decode("utf-8", "replace").split("\0")
        for k in range(0, len(parts) - 1, 2):
            out[int(parts[k])] = parts[k + 1]
    missing = [bodies[i] for i in range(len(bodies)) if i not in out]
    assert not missing, f"bash printed no result for: {missing[:3]}"
    return [out[i] for i in range(len(bodies))]


def _bodies() -> "list[str]":
    printers = PRINTERS + DASH_PRINTERS + LOOKALIKE_PRINTERS
    redirects = REDIRECTS + FD_REDIRECTS
    spaced = [f"{p} {r}{t}" for p, r, t in itertools.product(printers, redirects, TAILS)]
    # The same redirect glued to the last word (``token>/dev/null``, ``token2>&1``).
    glued = [
        f"{p}{r}{t}"
        for p, r, t in itertools.product(printers, REDIRECTS, TAILS)
        if r and not r.startswith(" ")
    ]
    piped = [pipe.format(p=p) + t for p, pipe, t in itertools.product(printers, PIPELINES, TAILS)]
    return sorted({b.strip() for b in spaced + glued + piped})


_VERB_WORD = re.compile(rf"(?<![^\s;&|()<>'\"`=]){VERB}(?![^\s;&|()<>'\"`])")
# Refused although bash discards the output that names the verb.
FAIL_CLOSED_DELTA = 4577
# Allowed although bash captures the verb: the body computes it without naming it.
COMPUTED_VERB_RESIDUAL = 86
# Floor-form bodies that name the verb only inside a here-string (``cat <<<token``):
# the static floor cannot read a word the shell feeds in on stdin, so it allows them,
# exactly as main does.  The precise body reader that closed these was dropped (see the
# PR body); this pins the residual so a future change to it is noticed.
FLOOR_HERESTRING_RESIDUAL = 120


def _mints(command: str) -> bool:
    return bool(argv_floor_self._is_credential_mint(command.lower()))


#: Both oracle sweeps -- the verb-presence floor check below and the monotonicity sweep --
#: are split into shards so each instance stays well under the repo's 120s per-test
#: timeout on slow CI runners (the verb-presence test was a shard-4 timeout on head
#: 3d406deee885, after the fail-closed rule raised the gate's cost on the pathological
#: corpus; the monotonicity sweep was a shard-4 timeout on an earlier head) and xdist can
#: run them in parallel.  Each shard runs the identical per-body assertion over
#: ``_bodies()[shard::_MONOTONIC_SHARDS]``; the shards partition the corpus with no
#: overlap or gap, so the full corpus is still covered.
_MONOTONIC_SHARDS = 8


def _is_herestring_body(body: str) -> bool:
    """A body that names the verb ONLY inside a ``<<<verb`` here-string: the static floor
    cannot read a word fed in on stdin, so it allows it (the ``FLOOR_HERESTRING_RESIDUAL``).
    Structural, with no floor call -- the body names the verb, but with the here-string's
    verb removed no readable verb word is left -- so each shard pins its own slice of the
    residual from the corpus rather than from a hand table.
    """
    if _VERB_WORD.search(body) is None:
        return False
    return _VERB_WORD.search(body.replace("<<<" + VERB, "<<<")) is None


def test_the_here_string_residual_total_is_pinned() -> None:
    # Per-shard tests pin each slice's here-string residual from _is_herestring_body; this
    # pins the WHOLE-corpus total against the constant, so a new residual anywhere (or a
    # miscounting predicate) still reddens a test.
    assert sum(_is_herestring_body(b) for b in _bodies()) == FLOOR_HERESTRING_RESIDUAL


@pytest.mark.parametrize("shard", range(_MONOTONIC_SHARDS))
def test_the_floor_binds_on_the_verbs_presence(shard) -> None:
    # The floor binds on the verb's PRESENCE as a readable argv word in the quoted
    # substitution, so the redirect games bash would use to DISCARD that output do not
    # change the verdict.  It never refuses a body that does not name the verb (no
    # over-refusal), and the only bodies it misses are the here-strings above.  The
    # ``T=$(: "'"; body); kirocrew $T`` assignment form is NOT asserted here: under the
    # fail-closed rule the resolver binds the first mint-relevant name rather than
    # reading the body precisely, so its per-body behaviour is covered by the targeted
    # grammar rows, not by a verb-presence oracle.
    # Sharded by body index (see _MONOTONIC_SHARDS above): each instance covers
    # _bodies()[shard::_MONOTONIC_SHARDS] and pins its slice's here-string residual from
    # _is_herestring_body, so the shards partition the corpus and spread the 120-body
    # residual across the slices with no gap.
    slice_bodies = list(_bodies())[shard::_MONOTONIC_SHARDS]
    over = under = 0
    for body in slice_bodies:
        names = _VERB_WORD.search(body) is not None
        mints = _mints(f'python -m kiro_crew "$({body})"')
        over += mints and not names
        under += names and not mints
        assert not _mints(f'python -X "$({body})" -m kiro_crew doctor'), f"operand: {body}"
    assert over == 0, "floor refused a body that names no verb word"
    assert under == sum(_is_herestring_body(b) for b in slice_bodies), (shard, under)


def _bash_lacks_required_syntax() -> bool:
    """True when no bash on PATH supports the oracle corpus's dialect (needs >= 4.1).

    The bodies use ``|&`` (bash 4) and ``{v}>&1`` (bash 4.1); on an older bash (macOS
    ships 3.2) those raise syntax errors and change the counts, so the fixed assertion
    would fail for a reason unrelated to the floor. Read the resolved bash's
    ``BASH_VERSINFO`` major/minor and skip below 4.1.
    """
    bash = shutil.which("bash")
    if bash is None:
        return True
    try:
        proc = subprocess.run(
            [bash, "-c", "echo ${BASH_VERSINFO[0]} ${BASH_VERSINFO[1]}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
            timeout=FALLBACK_TIMEOUT_SECS,
        )
        major_s, minor_s = proc.stdout.split()[:2]
        version = (int(major_s), int(minor_s))
    except (OSError, ValueError, subprocess.SubprocessError):
        return True
    return version < (4, 1)


@pytest.mark.skipif(
    _bash_lacks_required_syntax(),
    reason="needs bash >= 4.1 for the oracle corpus (|& and {v}>&1)",
)
def test_the_trade_against_bash_is_counted(tmp_path) -> None:
    delta = residual = 0
    bodies = _bodies()
    for body, output in zip(bodies, _bash_captures(bodies, str(tmp_path))):
        captured = VERB in output.split()
        names = _VERB_WORD.search(body) is not None
        delta += names and not captured
        residual += captured and not names
    assert (delta, residual) == (FAIL_CLOSED_DELTA, COMPUTED_VERB_RESIDUAL)


#: The monotonicity sweep below shares ``_MONOTONIC_SHARDS`` with the verb-presence sweep
#: (defined above); both partition ``_bodies()`` the same way.
@pytest.mark.parametrize("shard", range(_MONOTONIC_SHARDS))
def test_every_command_main_refuses_this_head_refuses(monkeypatch, shard) -> None:
    # Monotonicity over the whole oracle corpus.  The head keeps main's resolution as is
    # and only APPENDS an alternative frame for a substitution-valued assignment, and the
    # floor refuses when ANY frame is a mint.  So if main's frames (the alternative
    # switched off, which is main's resolver unchanged) are a subset of the head's, the
    # head refuses everything main refuses.  The frame subset is pinned in the grammar
    # suite; here the claim itself is checked on the floor's own verdict, for every body
    # in each assignment form the alternative reads.  Sharded by body index so each
    # instance covers ``_bodies()[shard::_MONOTONIC_SHARDS]`` and the shards partition
    # the corpus with no overlap or gap.
    from kiro_crew.security import shell_normalizer as sn

    def refuses(command: str, alternative: bool) -> bool:
        # A scoped context, so only this patch is undone: a bare ``monkeypatch.undo()``
        # would also drop every patch an autouse fixture made on the same instance.
        with monkeypatch.context() as scoped:
            if not alternative:
                scoped.setattr(sn, "_alternative_mint_overrides", lambda *a, **k: {})
            return _mints(command)

    for body in list(_bodies())[shard::_MONOTONIC_SHARDS]:
        for command in (f'T=$(: "\'"; {body}); kirocrew $T', f"T=$({body}); kirocrew $T"):
            assert refuses(command, True) or not refuses(command, False), command
