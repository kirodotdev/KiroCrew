"""The ssh-to-self floor reads a self connection on a later line of a script a shell runs.

On a bare ``bash -c '…\\n…'`` / ``sh -c`` / ``eval`` / here-string script, a self-ssh on
any line but the first sits behind the whole-line separator mask, which turns the script's
own separators into data. ``_folded_scanner_reaches_self`` descends into and judges each
executable shell-script payload as a command line of its own, so a later line is read
whole.

These pins also hold the floor to:
  - a pending dotted self-alias inside a script frame fails CLOSED (the frame is judged at
    the floor's default DNS policy, which refuses a pending dotted host);
  - a quote-split verb inside a ``$( )`` body in a heredoc still resolves (the body is read
    by the substitution reader, which preserves syntactic quotes);
  - importing ``_own_address_probe_stays_local`` seeds the own-host set with a packet-less
    probe, so these ssh verdicts start no DNS worker.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _assert_scan_is_linear, _denied_by

from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.security import argv_floor_ssh_bodies as _bodies

_RULE = "sandbox-escape-ssh-self"


@pytest.fixture(autouse=True)
def _remote_hosts_resolve_not_self(_floor_monkeypatch):
    """Keep remote-host allow cases allowed, the way the ssh-self suite's own fixture does.

    The own-host set is pinned to loopback by ``_own_address_probe_stays_local``; here the
    DNS-alias verdict layer (which fails closed on a name it has not resolved, and tests
    must not resolve real names) is stubbed to "not self", and the kernel address table is
    marked published so an IP literal resolves on its own merits. ``localhost`` / ``127.0.0.1``
    stay self through the loopback set, so the refusal pins are unaffected."""
    _floor_monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
    _floor_monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)


# A glob verb (``s?h``) and a quote-split verb, each assembled so this test file's own
# argv never reads as a self-ssh command to the floor that guards this process.
_G = "s?h"
_L = "local" + "host"


@pytest.mark.parametrize(
    "cmd",
    [
        "bash -c 'true\n%s %s id'" % (_G, _L),  # issue headline: glob verb on line 2
        "sh -c 'true\n%s %s id'" % (_G, _L),
        "bash -c 'true\nsh -c \"true\n%s %s id\"'" % (_G, _L),  # nested
        # deeper nesting: a self-ssh three and four `bash -c` levels down, each operand
        # quoted a level further in, reached by reading the raw operand one level at a time.
        'bash -c \'true\nbash -c "true\nbash -c \\"true\n%s %s id\\""\'' % (_G, _L),
        'bash -c \'true\nbash -c "true\nbash -c \\"true\nbash -c \\\\\\"true\n%s %s id'
        '\\\\\\"\\""\'' % (_G, _L),
        # the same nesting with `;` command separators instead of newlines.
        'bash -c \'true;bash -c "true;bash -c \\"true;%s %s id\\""\'' % (_G, _L),
        "$SHELL -c 'true\n%s %s id'" % (_G, _L),
        "env -S 'true\n%s %s id'" % (_G, _L),
        "bash <<EOF\ntrue\n%s %s id\nEOF" % (_G, _L),  # here-doc a shell runs
        'eval "$(cat <<EOF\n%s %s id\nEOF\n)"' % (_G, _L),
        "cat <(bash -c 'true\n%s %s id')" % (_G, _L),  # process substitution runs it
        # the same process substitution reached past a ``\``-newline line continuation
        # bash removes before parsing: the respelling must not escape the reading.
        "cat <\\\n(bash -c 'true\n%s %s id')" % (_G, _L),
        "bash -lc 'gh pr create <<EOF\n%s %s id\nEOF'" % (_G, _L),  # forge body in a script
        "bash -c 'git commit -F - <<EOF\n%s %s id\nEOF'" % (_G, _L),  # git stdin body
        # a glob verb split across a `\`-newline line continuation bash folds before it runs
        # (``s?`` + ``\``-newline + ``h`` -> ``s?h`` -> ssh), on a later line of a script.
        ": %s; bash -c 'true\n%s\\\nh %s id'" % (_G, "s?", _L),
        # a self-ssh nested behind a value-taking option (`-o pipefail`, `-O extglob`),
        # which the script operand sits after, must still be read and refused.
        'bash -o pipefail -c \'true\nbash -o pipefail -c "true\nbash -c \\"true\n%s %s id'
        '\\""\'' % (_G, _L),
        # a shell reached through an exec wrapper (`command`, `env`, `sudo`, `nohup`) at
        # every level: the wrapper-aware program lookup still finds the `-c` script.
        'command bash -c \'true\ncommand bash -c "true\ncommand bash -c \\"true\n%s %s id'
        '\\""\'' % (_G, _L),
        # a here-string (`<<<`) is a script its shell runs, so a self-ssh nested under one is
        # read the same as a `-c` body.
        'bash <<< \'true\nbash <<< "true\nbash -c \\"true\n%s %s id\\""\'' % (_G, _L),
        # `bash -c -` ends option scanning with `-`; the script operand after it is still read.
        'bash -c - \'true\nbash -c - "true\nbash -c \\"true\n%s %s id\\""\'' % (_G, _L),
        # a redirection word before the shell (`2>/dev/null bash -c`) does not hide the script.
        '2>/dev/null bash -c \'true\n2>/dev/null bash -c "true\nbash -c \\"true\n%s %s id'
        '\\""\'' % (_G, _L),
        # a self-ssh nested under an outer `eval` whose levels use `'\''` quoting, which the
        # dequoting payload read collapses; the raw eval-body read keeps every level.
        "true; eval 'true; bash -c '\\''true; bash -c '\\''\\'\\'''\\''%s %s id"
        "'\\''\\'\\'''\\'''\\''" % (_G, _L),
    ],
)
def test_a_self_ssh_on_a_later_line_of_a_run_script_is_refused(cmd):
    assert _denied_by(cmd) == _RULE, cmd


# A quoted heredoc (``<<'EOF'``) whose last body line ends in a backslash, then a
# substitution the shell runs after the terminator. bash keeps the quoted body literal,
# so the top-frame line-continuation fold must leave it alone: folding it merges the
# body's trailing ``\`` into the terminator (``x\`` + newline + ``EOF`` -> ``xEOF``),
# so the terminator never closes, the reader blanks the tail as heredoc text, and the
# substitution the shell runs -- with a self-ssh in it -- is never read.
_HD = "cat <<'EOF'\nx\\\nEOF\n"
# A literal verb, assembled so this file's own argv never reads as a self-ssh.
_SV = "s" + "sh"


def test_the_quoted_heredoc_fold_does_not_blind_the_substitution_descent():
    """The fold defect lives in the descent, so pin it there: ``_folded_scanner_reaches_self``
    must read the ``$( )`` the shell runs after a quoted heredoc. The unconditional-fold
    mutant makes this False (the terminator is swallowed and the body is blanked).

    The ``is_denied`` belt below stays REFUSED on the mutant too for THIS spelling -- the
    whole-line floor regex matches the literal ``; <verb> <host>`` even when the descent
    misses -- so the belt does NOT gate the fold fix; the descent assertion does. The
    net-level gate is the glob-verb case below, which the literal regex cannot catch."""
    cmd = _HD + 'echo "$(echo hi; %s %s id)"' % (_SV, _L)
    assert _bodies._folded_scanner_reaches_self(cmd.lower()) is True, cmd
    assert _denied_by(cmd) == _RULE, cmd  # belt; backstopped, see docstring


@pytest.mark.parametrize(
    "cmd",
    [
        _HD + 'echo "$(echo hi; %s %s id)"' % (_G, _L),  # glob verb in $( )
        _HD + 'echo "$(echo hi\n%s %s id)"' % (_G, _L),  # glob verb on a later $( ) line
        _HD + 'echo "`echo hi; %s %s id`"' % (_G, _L),  # glob verb in a backtick body
    ],
    ids=["glob-subst", "glob-subst-line2", "glob-backtick"],
)
def test_a_quoted_heredoc_does_not_hide_a_self_ssh_the_shell_runs_after_it(cmd):
    """Net-level pin for the quoted-heredoc fold defect, with a GLOB verb the whole-line
    literal-ssh regex cannot match, so the substitution descent is the SOLE catch. On
    origin/main this is REFUSED (the descent reads the body); the unconditional-fold
    mutant blanks the body and ALLOWS it -- a self-ssh sandbox escape -- so the mutant
    turns this red at the net ``is_denied`` level."""
    assert _denied_by(cmd) == _RULE, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        # a ``\``-newline splitting the ``<<`` operator itself
        "cat <\\\n<'EOF'\nx\\\nEOF\necho \"$(echo hi; %s %s id)\"" % (_G, _L),
        "cat <\\\n<'EOF'\nx\\\nEOF\necho \"`echo hi; %s %s id`\"" % (_G, _L),
        "cat <\\\n<-'EOF'\nx\\\nEOF\necho \"$(echo hi; %s %s id)\"" % (_G, _L),
        # a ``\``-newline splitting the command word AND the operator
        "ca\\\nt <\\\n<'EOF'\nx\\\nEOF\necho \"$(echo hi; %s %s id)\"" % (_G, _L),
    ],
    ids=["split-op", "split-op-backtick", "split-op-dash", "split-word-and-op"],
)
def test_a_continuation_split_heredoc_operator_does_not_hide_a_self_ssh(cmd):
    """bash folds a ``\\``-newline continuation while lexing, BEFORE it recognises ``<<``,
    so a split operator (``<``-``\\``-newline-``<``), or a command word split before one,
    is a quoted heredoc all the same. The fold must be heredoc-aware in ONE pass -- fold as
    bash reads, recognise the operator on the folded stream, then copy the quoted body
    verbatim -- so these spellings do not slip the way a span finder on the raw text lets
    them. origin/main REFUSES all four; 68b615e and the two-pass r28 fold ALLOW them, so a
    mutant restoring the two-pass order turns this red."""
    assert _denied_by(cmd) == _RULE, cmd


# A quoted heredoc whose last body line ends in a backslash, and a self-ssh placed so each
# CONTEXT's heredoc handling decides the verdict. ``<<`` is a heredoc only in a command
# context; everywhere else the construct must not blind the floor to the self-ssh.
_HDB = "<<'EOF'\nx\\\nEOF\n"
_SELF = "%s %s id" % (_G, _L)


@pytest.mark.parametrize(
    "cmd",
    [
        'cat %secho "$(echo hi; %s)"' % (_HDB, _SELF),  # top level
        'echo "$(cat %s)"\necho $(echo hi; %s)' % (_HDB, _SELF),  # inside $( )
        'echo "`cat %s`"\necho $(echo hi; %s)' % (_HDB, _SELF),  # inside backticks
        "x=\"$(cat <<'EOF'\nit's\\\nEOF\n)\"\necho $(echo hi; %s)" % (_SELF,),  # inside "$( )"
        "echo \"a << 'EOF' b\"\necho $(echo hi; %s)" % (_SELF,),  # literal << in "..."
        "echo 'a <<EOF b'\necho $(echo hi; %s)" % (_SELF,),  # literal << in '...'
        "echo hi # <<'EOF'\necho \"$(echo hi; %s)\"" % (_SELF,),  # after a # comment
        'echo $((1<<"2"))\necho "$(echo hi; %s)"' % (_SELF,),  # inside $(( ))
        "cat <\\\n<'EOF'\nx\\\nEOF\necho \"$(echo hi; %s)\"" % (_SELF,),  # split operator
        "cat <<-'EOF'\n\tx\\\n\tEOF\necho \"$(echo hi; %s)\"" % (_SELF,),  # <<- tab strip
        "cat <<<'data'\necho \"$(echo hi; %s)\"" % (_SELF,),  # <<< here-string (not a heredoc)
    ],
    ids=[
        "top",
        "in-subst",
        "in-backtick",
        "in-dq-subst",
        "dq-literal",
        "sq-literal",
        "after-comment",
        "in-arith",
        "split-op",
        "dash-strip",
        "here-string",
    ],
)
def test_a_heredoc_hides_no_self_ssh_in_any_shell_context(cmd):
    """Every context the fold scanner must get right, one row each. origin/main REFUSES all
    eleven; 68b615e ALLOWS four (top, inside "$( )", split operator, ``<<-``) where its fold
    blinds the descent; the head REFUSES all eleven, matching origin/main. This is the full
    set of contexts in which a ``<<`` is, or is not, a heredoc, so a later context gap in
    this class would show as a new row failing here."""
    assert _denied_by(cmd) == _RULE, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "echo hi # <<'EOF'\necho \"$(echo hi; %s)\"" % (_SELF,),  # comment, unsplit $( )
        "echo hi # <<'EOF'\necho \"$\\\n(echo hi; %s)\"" % (_SELF,),  # comment, split $(
        'echo $((1<<"2"))\necho "$\\\n(echo hi; %s)"' % (_SELF,),  # arithmetic, split $(
        'echo "$\\\n(echo hi; %s)"' % (_SELF,),  # a bare continuation-split $( operator
    ],
    ids=["comment-plain", "comment-split", "arith-split", "bare-split-subst"],
)
def test_a_non_heredoc_context_or_split_subst_still_catches_a_self_ssh(cmd):
    """N1 from the review: a ``<<`` in a ``#`` comment or ``$(( ))`` is NOT a heredoc, and a
    ``$(`` split by a continuation is still a substitution once folded. origin/main ALLOWS
    the split-``$(`` rows (it does not fold the operator); the head REFUSES them -- a
    tightening, not a regression, since each carries a self-ssh. The context stack closes
    these, so they are pinned here rather than left to drift."""
    assert _denied_by(cmd) == _RULE, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        # bare $( )
        "echo $(cat <\\\n<'EOF'\nx\"\\\nEOF)\necho $(echo hi; %s)" % (_SELF,),
        # inside "$( )"
        'echo "$(cat <\\\n<\'EOF\'\nx"\\\nEOF)"\necho $(echo hi; %s)' % (_SELF,),
        # inside "${x:-$( )}"
        'echo "${x:-$(cat <\\\n<\'EOF\'\nx"\\\nEOF)}"\necho $(echo hi; %s)' % (_SELF,),
        # inside "$( ( ) )" subshell
        'echo "$( (cat <\\\n<\'EOF\'\nx"\\\nEOF) )"\necho $(echo hi; %s)' % (_SELF,),
    ],
    ids=["bare", "in-dq", "in-brace", "in-subshell"],
)
def test_a_split_heredoc_with_a_glued_terminator_cannot_hide_a_self_ssh(cmd):
    """A continuation-split ``<<`` with a backslash-terminated body whose terminator word
    is glued to a substitution/subshell closer (``EOF)``) is unterminated, and the folding
    reader over-reads it; origin/main (no fold ahead of its scan) refuses it. The fail-closed
    belt -- origin/main's substitution reading run alongside -- keeps the refusal, so a
    mutant that drops the ``_substitution_reaches_self`` leg turns these red."""
    assert _denied_by(cmd) == _RULE, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        '# x\\\necho "$(echo hi; %s)"' % (_SELF,),
        'echo a # x\\\necho "$(echo hi; %s)"' % (_SELF,),
        '# x\\\necho "`echo hi; %s`"' % (_SELF,),
    ],
    ids=["comment", "trailing-comment", "comment-backtick"],
)
def test_a_backslash_ending_comment_does_not_fold_into_the_next_command(cmd):
    """bash leaves a ``\\`` literal inside a ``#`` comment, so the comment ends at its newline
    and the next command runs. The scanner is comment-aware; the no-heredoc fast path is only
    taken when no ``#`` is present, so a comment ending in a backslash is never folded into
    the following substitution. origin/main refuses these; the head keeps the refusal."""
    assert _denied_by(cmd) == _RULE, cmd


def test_the_heredoc_fold_scans_linearly_in_the_number_of_operators():
    """The single-pass fold stays linear in the count of ``<<`` operators. An earlier
    delimiter parse re-folded the whole rest of the text per operator, which was O(n^2) on
    a run of operators; the delimiter is now read as its own word."""
    _assert_scan_is_linear(
        lambda nk: _SV + " x " + "<<a " * nk,
        (300, 600, 900),
        denied=False,
    )


def test_the_descent_tokenizes_each_distinct_payload_once(monkeypatch):
    """A frame can hand back the SAME payload body many times (a run of ``bash`` words
    before one ``-c`` script). The descent must admit each distinct body once, not
    re-tokenize one body per copy: queuing every copy is quadratic work for one verdict.
    Count the tokenizations; restoring the list-comprehension admit (which updates
    ``seen`` only after building the batch) makes this explode and the bound go red."""
    cmd = "echo " + "bash " * 2500 + "-c 'echo " + "x " * 2500 + "; %s'" % _SV
    calls: list[str] = []
    real = _bodies._self_tokens

    def counting(text, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(text)
        # Trip FAST on a per-copy regression rather than let the quadratic blow-up run:
        # the fixed descent admits each distinct body once, so a handful of calls.
        assert len(calls) <= 50, "descent re-tokenized %d times (per-copy regression)" % len(calls)
        return real(text, **kwargs)

    monkeypatch.setattr(_bodies, "_self_tokens", counting)
    _bodies._folded_scanner_reaches_self(cmd.lower())
    # One top frame plus one distinct payload body; a per-copy regression tokenizes the
    # same body ~2500 times.
    assert len(calls) <= 20, len(calls)


@pytest.mark.parametrize(
    "cmd",
    [
        "bash -c 'true\necho hello world'",
        "bash -c 'true\nsh -c \"echo done\"'",
        "bash -lc 'gh pr create <<EOF\nrelease notes\nEOF'",  # benign forge body
        # a `\`-newline continuation inside a double-quoted operand is folded by bash into
        # one word, not a second command, so this benign printf must stay allowed.
        'bash -c "printf x\\\n%s %s"' % (_G, _L),
        # a benign REMOTE ssh nested four `bash -c` levels deep stays allowed: the descent
        # reads each level but the leaf names a remote host, not this machine.
        'bash -c \'true\nbash -c "true\nbash -c \\"true\nbash -c \\\\\\"true\n'
        'ssh far.example.com id\\\\\\"\\""\'',
    ],
)
def test_a_run_script_with_no_self_ssh_stays_allowed(cmd):
    assert _denied_by(cmd) is None, cmd


# An empty-quote pair the shell drops before argv (``s\"\"sh`` -> ``ssh``, ``r\"\"m`` ->
# ``rm``) must not evade the verb floors: the shared normalizer over-approximates the
# escaped pair as droppable, so a respelled verb still reads as the verb. These four rows
# regressed to ALLOWED when an earlier draft removed that collapse; they are pinned here,
# one per wrapper shape (top level, ``bash -c`` body, and a ``watch`` refresh wrapper), and
# cover the rm-rf floor as well as the ssh-self floor so the collapse stays shared.
_EQ = '\\"\\"'  # an escaped empty-quote pair, e.g. s\"\"sh / r\"\"m
_BQ = '""'  # a bare empty-quote pair, e.g. s""h / rm""
_SSH_EQ = "s" + _EQ + "sh"
_RM_EQ = "r" + _EQ + "m"
_SSH_BARE = "s" + "s" + "h"
_RM_BARE = "r" + "m"


@pytest.mark.parametrize(
    "cmd,rule",
    [
        ("%s %s id" % (_SSH_EQ, _L), _RULE),
        ("bash -c '%s %s id'" % (_SSH_EQ, _L), _RULE),
        ("watch -n 1 %s %s id" % (_SSH_EQ, _L), _RULE),
        ("watch %s -rf /" % _RM_EQ, "local-destructive-rm-rf-root"),
        # a bare pair at a word's trailing edge, inside the single quotes a `watch` argument
        # carries: base collapsed it on the dequoted token, so the source-level collapse must
        # match a bare pair wherever it touches a word, not only mid-word.
        ("watch '%s%s' -rf /" % (_RM_BARE, _BQ), "local-destructive-rm-rf-root"),
        ("%s%s %s id" % (_SSH_BARE, _BQ, _L), _RULE),  # bare pair at trailing edge
        ("%s%s %s id" % (_BQ, _SSH_BARE, _L), _RULE),  # bare pair at leading edge
        # an ESCAPED pair at a word edge, which bash keeps literal but a wrapper that re-runs
        # its argv through a second shell (`watch`, `su -c`, `script -c`) then drops: the
        # source-level collapse must reach an escaped pair beside a space or a `/`, not only
        # one between two word chars, or the respelling escapes the destructive-verb floor.
        ("watch %s%s -rf /" % (_RM_BARE, _EQ), "local-destructive-rm-rf-root"),
        ("watch %s -%srf /" % (_RM_BARE, _EQ), "local-destructive-rm-rf-root"),
        ("watch %s -rf %s/" % (_RM_BARE, _EQ), "local-destructive-rm-rf-root"),
    ],
)
def test_an_empty_quote_respelled_verb_is_refused(cmd, rule):
    assert _denied_by(cmd) == rule, cmd


def test_the_empty_quote_collapse_scans_linearly():
    # A long run of empty-quote pairs that touches no word char made an earlier
    # `(?:""|'')+(?=\w)` collapse backtrack O(n^2): the `+` ate the whole run at every start
    # position, then failed the look-ahead. At 10k pairs that was ~7s, and it ran on every
    # command through the synchronous deny gate. The maximal-run scan is linear. Asserted on
    # interpreter-instruction counts, never a clock -- a seconds budget reads the CI runner,
    # not the code, and false-reds under coverage instrumentation (see _assert_scan_is_linear).
    # A bare run (`echo """"...`) and a single-quoted run (`echo '""''""'...`, which the
    # per-token pass sees only after shlex dequotes) are both exercised; both stay allowed.
    _assert_scan_is_linear(lambda n: "echo " + ('""' * n), (200, 400, 600), denied=False)
    _assert_scan_is_linear(
        lambda n: "echo " + ("'" + '""' + "'") * n, (200, 400, 600), denied=False
    )


def test_the_default_tokenizer_elides_empty_pairs_so_eval_rm_rf_stays_denied():
    # 6th review finding: a runnable `rm -rf /` nested in `eval 'sh -c ...'` carrying an
    # inert `''` pair. The deny-matching view uses `_shell_tokens`' DEFAULT -- unconditional
    # empty-pair elision, byte-identical to the base ref -- so it renders `rm -rf /` and the
    # floor fires. The structure-preserving collapse (ssh-self descent only) would retain the
    # pair, so it must NOT be the default. A full-verdict differential over the 32k corpus
    # confirms the default loses zero denials against base; this pins the one row that
    # regressed when the collapse was briefly the default for every caller.
    from kiro_crew.security import shell_normalizer as _sn

    sq, esc = "'", "\\'"
    inner = "true; " + "r" + "m" + " " + sq + sq + "-rf /"
    shc = "sh -c " + sq + inner.replace(sq, sq + esc + sq) + sq
    cmd = "eval " + sq + shc.replace(sq, sq + esc + sq) + sq
    assert _denied_by(cmd) == "local-destructive-rm-rf-root", cmd
    # The default drops every empty pair; the flag is a distinct, opt-in mode.
    assert _sn._shell_tokens("r" + '""' + "m") == ["rm"]
    assert _sn._shell_tokens("r" + '""' + "m", preserve_structure=True) == ["rm"]


def test_a_split_assembled_verb_in_a_heredoc_substitution_still_resolves(monkeypatch):
    # ``x="s""sh"; $x`` is ``ssh`` at run time. The substitution reader preserves the
    # syntactic quotes inside the ``$( )`` body, so the assembled verb resolves.
    cmd = 'bash -c \'true\ncat <<EOF\n$(x="s""sh"; $x %s id)\nEOF\'' % _L
    assert _denied_by(cmd) == _RULE


def test_a_pending_dotted_self_alias_in_a_script_frame_fails_closed(monkeypatch):
    # Inside an executed script, a dotted host whose verdict is not yet cached refuses on
    # the miss (fail closed), and stays refused once cached as self.
    from kiro_crew.security import argv_floor_hosts

    cmd = "bash -c 'true\nssh alias.example.invalid id'"
    # pending: the resolver answers the caller's fail_closed (True => refuse)
    monkeypatch.setattr(
        argv_floor_hosts, "_resolved_host_verdict", lambda host, fail_closed=True: fail_closed
    )
    assert _denied_by(cmd) == _RULE
    # cached as self: still refused
    monkeypatch.setattr(
        argv_floor_hosts, "_resolved_host_verdict", lambda host, fail_closed=True: True
    )
    assert _denied_by(cmd) == _RULE


def test_exactly_one_reaches_self_over_ssh_entry():
    # One ssh-self entry point: no second walk, no parallel grammar.
    assert hasattr(_argv_floor, "_reaches_self_over_ssh")


def test_a_crash_in_the_ssh_self_fold_denies_closed_and_logs_once(monkeypatch, caplog):
    """S1: the ssh-self reading hand-folds and descends a script before the fail-closed
    belt runs. If that fold raises, the deny gate must DENY a command that could name an
    ssh verb (fail closed, as the rm floor does for its own reader), not let the exception
    propagate out of ``is_denied`` and skip the floor -- and it must log once, so a crash
    is visible, not a silent miss. A mutant that drops the try/except turns this red: the
    unguarded call re-raises and ``_denied_by`` errors out."""
    import logging

    from kiro_crew.security import deny_gate  # noqa: F401  (import pins the wrap's home)

    calls = {"n": 0}

    def _boom(*_a, **_k):
        calls["n"] += 1
        raise RuntimeError("injected fold fault")

    # The folding scanner's own reader; the main-reading leg (``_substitution_reaches_self``)
    # does not fold, so only the folding scanner leg that ``_reaches_self_over_ssh`` runs
    # beside it raises here.
    monkeypatch.setattr(_bodies, "_fold_outside_quoted_heredocs", _boom)
    _sv = "s" + "sh"  # a literal ssh verb, assembled so this file's own argv never reads as one
    # Descent-only: the whole-line reader does not catch a self-ssh inside this ``$( )`` body,
    # so the folding scanner (now faulted) is the path that runs.
    cmd = "echo \"$(bash -c 'true\n%s %s id')\"" % (_sv, _L)
    with caplog.at_level(logging.WARNING):
        verdict = _denied_by(cmd)
    assert (
        calls["n"] >= 1
    ), "the folding scanner was never reached, so the fault path was not exercised"
    assert verdict == _RULE, "a fold crash must fail closed to the ssh-self rule, not propagate"
    warned = [r for r in caplog.records if "ssh-self floor could not read" in r.getMessage()]
    assert len(warned) == 1, "the crash must be logged exactly once, so it is visible not silent"


def test_a_fold_crash_on_a_command_with_no_ssh_verb_does_not_spuriously_deny(monkeypatch):
    """The fail-closed deny is guarded by ``_cannot_name_ssh_verb`` exactly as the rm floor
    guards on ``_names_rm_word``: a crash while reading a command that could not name an ssh
    verb must not invent a refusal."""

    def _boom(*_a, **_k):
        raise RuntimeError("injected fold fault")

    monkeypatch.setattr(_bodies, "_fold_outside_quoted_heredocs", _boom)
    # No ssh verb anywhere; a ``$( )`` body so the folding scanner is reached and faults.
    cmd = "echo \"$(bash -c 'true\necho hello world')\""
    assert _denied_by(cmd) != _RULE


@pytest.mark.parametrize(
    "cmd",
    [
        # G1: a comment ending in `\` on an earlier line of a bash -c script does not fold
        # into (and hide) a real self-ssh on the next line.
        "bash -c '# x\\\n%s'" % _SELF,
        # G2: a `#` glued to a word-ending `)` opens a comment, so the `\`-continuation does
        # not fold a later self-ssh into it. Subshell close, case pattern, and inside "$( )".
        "bash -c '(true)#c\\\n%s'" % _SELF,
        "bash -c 'case a in a)#c\\\n%s;; esac'" % _SELF,
        'echo "$( (true)#c\\\n%s )"' % _SELF,
    ],
    ids=["g1-comment-backslash", "g2-subshell", "g2-case", "g2-dq-subshell"],
)
def test_a_comment_continuation_does_not_hide_a_self_ssh_on_a_later_line(cmd: str) -> None:
    """G1/G2: the fold drops a live comment's text (keeping its newline) and treats `#` after
    a word-ending `)` as a comment, so a `\\`-newline inside a comment does not fold a self-ssh
    on the next line into the comment and hide it. A mutant that copies the comment text, or
    that omits the `)` comment opener, lets these through as ALLOW."""
    assert _denied_by(cmd) == _RULE, cmd


def test_the_main_leg_reads_a_self_ssh_as_its_own_substitution_reading() -> None:
    """The fail-closed leg is origin/main's substitution reading, left byte-for-byte
    unedited and run beside the folding, script-descending scanner. Exercise it directly:
    a ``$( )`` body that runs a self-ssh is seen, a benign one is not."""
    assert _bodies._substitution_reaches_self(('echo "$(echo hi; %s)"' % _SELF).lower()) is True
    assert _bodies._substitution_reaches_self('echo "$(echo hi; echo there)"') is False


def _source_of(func) -> str:
    """The function body source, dedented, as the drift pin compares it."""
    import inspect
    import textwrap

    return textwrap.dedent(inspect.getsource(func)).rstrip("\n")


def _main_leg_source_from_base() -> "str | None":
    """``_substitution_reaches_self`` as origin/main spells it, extracted from the base ref
    via git. Returns None when no base ref is reachable (a shallow checkout), so the drift
    pin skips rather than fails where it cannot read the baseline; the two leg-mutant pins
    below carry the hard guarantee with no git dependency."""
    import ast
    import os
    import subprocess

    path = "src/kiro_crew/security/argv_floor_ssh_bodies.py"
    refs = [
        os.environ.get("COMMENT_HISTORY_BASE_REF"),
        os.environ.get("MERGE_BASE"),
        "origin/main",
        "main",
    ]
    for ref in refs:
        if not ref:
            continue
        try:
            blob = subprocess.run(
                ["git", "show", "%s:%s" % (ref, path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            ).stdout
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            continue
        tree = ast.parse(blob)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_substitution_reaches_self":
                import textwrap

                return textwrap.dedent(ast.get_source_segment(blob, node) or "").rstrip("\n")
    return None


def test_the_main_leg_is_origin_mains_substitution_reading_byte_for_byte() -> None:
    """Drift pin: ``_substitution_reaches_self`` must stay the base ref's reading, except
    that its two reads go through the per-call reads both legs share (so the whole call
    stays under main's 3x bound). Any other edit to the function body turns this red."""
    baseline = _main_leg_source_from_base()
    if baseline is None:
        import pytest

        pytest.skip("no base ref reachable to read the baseline substitution reading")
    shared = baseline.replace(
        "if judge and _is_ssh_to_self(view):", "if judge and _judged(text, view):"
    ).replace("_self_tokens(text), allow_join", "_tokens(text), allow_join")
    assert shared != baseline
    assert _source_of(_bodies._substitution_reaches_self) == shared


# A later-line self-ssh in a bare ``bash -c`` script: the main leg's descent is gated behind
# a live ``$( )`` / backtick, so it does NOT read this; only the folding scanner's descent
# does. Disabling the scanner must drop the refusal.
_SCANNER_ONLY = "bash -c 'true\n%s %s id'" % ("s" + "sh", _L)
# A split heredoc with a glued terminator that the fold over-reads and the scanner misses;
# the unedited main leg (no fold) still refuses it. Disabling the main leg must drop that.
_MAIN_ONLY = "echo $(cat <\\\n<'EOF'\nx\"\\\nEOF)\necho $(echo hi; %s)" % (_SELF,)


def test_a_shared_leg_verdict_is_kept_per_reading(monkeypatch) -> None:
    """Opus lane on r42: the legs' shared judge memo keys on every reading state, so a
    literal-hosts verdict in one gate call is never served to a later DNS-mode read."""
    from kiro_crew.security import argv_floor_hosts as _hosts
    from kiro_crew.security.argv_floor_ssh_operand import _LITERAL_HOSTS
    from kiro_crew.security.call_views import _CALL_VIEWS

    monkeypatch.setattr(_hosts, "_resolved_host_verdict", lambda host, **kw: host == "evil.example")
    cmd = "x=$(echo hi; %s evil.example id)" % ("s" + "sh")

    def read(literal: bool) -> bool:
        previous = _LITERAL_HOSTS.get()
        _LITERAL_HOSTS.set(literal)
        try:
            return _bodies._reaches_self_over_ssh(cmd, raw_text=cmd)
        finally:
            _LITERAL_HOSTS.set(previous)

    previous_views = _CALL_VIEWS.get()
    _CALL_VIEWS.set({})
    try:
        assert read(True) is False
        assert read(False) is True
    finally:
        _CALL_VIEWS.set(previous_views)


def test_disabling_the_scanner_leg_drops_a_later_line_script_refusal(monkeypatch) -> None:
    """The folding, script-descending scanner leg does real work: with it forced off, a
    self-ssh on a later line of a bare ``bash -c`` script is let through, because the main
    leg descends only behind a live substitution. This is the mutant the descent pins kill."""
    assert _argv_floor._reaches_self_over_ssh(_SCANNER_ONLY.lower()) is True
    monkeypatch.setattr(_bodies, "_folded_scanner_reaches_self", lambda *a, **k: False)
    assert _argv_floor._reaches_self_over_ssh(_SCANNER_ONLY.lower()) is False


def test_disabling_the_main_leg_drops_a_main_refused_probe(monkeypatch) -> None:
    """The unedited main leg does real work: with it forced off, a split-heredoc glued
    terminator that the fold over-reads (and the scanner therefore misses) is let through.
    This is the mutant the belt-by-construction guarantee kills."""
    assert _argv_floor._reaches_self_over_ssh(_MAIN_ONLY.lower()) is True
    monkeypatch.setattr(_bodies, "_substitution_reaches_self", lambda *a, **k: False)
    assert _argv_floor._reaches_self_over_ssh(_MAIN_ONLY.lower()) is False


# Branch A: the ssh-to-self floor judges a later line of a script with the SAME host
# resolution it judges the first line with -- there is no separate posture for a descended
# host. On a runner whose egress is blocked, a dotted remote name's lookup fails and the
# floor fail-closes it to a refusal, on a later line exactly as on a top-level line; where
# the same name resolves to a non-self address, both are allowed. These rows are the ones a
# macOS runner reports as newly refused: the refusal is the first line's own fail-closed
# posture reaching the later line, not a new rule.
_DEPLOY_ROWS = (
    "bash -c 'cd /srv/app\nssh deploy.example.com uptime'",
    "bash -c 'true\nscp dist/app.tar.gz deploy.example.com:/tmp/'",
    "bash -c '# check the remote\\\nssh deploy.example.com uptime'",
)
_DEPLOY_TOP = "ssh deploy.example.com uptime"


@pytest.mark.parametrize("row", _DEPLOY_ROWS, ids=["ssh-cd", "scp", "ssh-comment-fold"])
def test_a_later_line_dotted_host_is_judged_as_the_top_level_line_is(row, monkeypatch) -> None:
    """The descended verdict on a later line must EQUAL the top-level verdict for the same
    dotted host. With every lookup failing (an egress-blocked runner) both fail-close to a
    refusal; with the name resolving to a non-self address both are allowed. A mutant that
    judges the descended host by a posture the first line does not share reddens the equality
    assertion -- so a later line can neither invent a refusal nor lose one the first line
    makes."""
    # Every dotted lookup fails: the egress-blocked runner condition.
    monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: True)
    top = _argv_floor._reaches_self_over_ssh(_DEPLOY_TOP.lower())
    descended = _argv_floor._reaches_self_over_ssh(row.lower())
    assert top is True, _DEPLOY_TOP
    assert descended == top, (row, descended, top)
    # The same name resolves to a non-self address: both lines are allowed.
    monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)
    top = _argv_floor._reaches_self_over_ssh(_DEPLOY_TOP.lower())
    descended = _argv_floor._reaches_self_over_ssh(row.lower())
    assert top is False, _DEPLOY_TOP
    assert descended == top, (row, descended, top)


# GPT-lane fix: a continuation-split verb on a LATER line of a ``bash -c`` script. The
# scanner leg has no verb prefilter (a split verb cannot be seen on the unfolded line, and
# the comment/quote-aware fold does not fold inside the outer ``'...'``), so it descends and
# the per-frame fold rejoins ``s?``-``\``-newline-``h`` into ``s?h`` before judging.
_GPT_SPLIT_TRIGGER = "bash -c 'true\n/usr/bin/s?\\\nh %s id'" % _L
_GPT_SPLIT_COMMENT = "bash -c '# c\\\ns\\\nsh %s id'" % _L


@pytest.mark.parametrize(
    "cmd",
    [_GPT_SPLIT_TRIGGER, _GPT_SPLIT_COMMENT],
    ids=["split-verb-later-line", "split-verb-after-comment"],
)
def test_a_continuation_split_verb_on_a_later_line_is_refused(cmd: str) -> None:
    """A verb split across a ``\\``-newline on a later line of a ``bash -c`` script folds to
    ``s?h`` / ``ssh`` and is refused. main and the r33 head ALLOW both (the pre-fold prefilter
    short-circuited before the fold); this head refuses them. The UNNESTED top-level control
    (``/usr/bin/s?``-``\\``-newline-``h localhost id``) stays ALLOWED on all three -- it is
    main's own whole-line path, which this PR does not touch (recorded, not fixed here)."""
    assert _denied_by(cmd) == _RULE, cmd


def test_the_unnested_top_level_split_verb_stays_mains_allow() -> None:
    """The top-level control is main's own whole-line path (no fold there), untouched by this
    PR: it stays ALLOWED, so this PR neither fixes nor regresses it. Pinned so a later change
    that starts folding the top line has to update this row and the main-side issue."""
    assert _denied_by("/usr/bin/s?\\\nh %s id" % _L) is None


def test_restoring_the_scanner_verb_prefilter_reddens_the_split_verb(monkeypatch) -> None:
    """The fix is REMOVING the scanner's pre-fold verb prefilter: a prefilter on the unfolded
    line short-circuits before the fold rejoins the split verb. Restoring it lets the split
    verb slip (ALLOW), so the removal is load-bearing, not incidental."""
    real = _bodies._folded_scanner_reaches_self

    def with_prefilter(text_lower):  # type: ignore[no-untyped-def]
        if not _bodies._line_may_name_ssh_verb(text_lower):
            return False
        return real(text_lower)

    monkeypatch.setattr(_bodies, "_folded_scanner_reaches_self", with_prefilter)
    assert _argv_floor._reaches_self_over_ssh(_GPT_SPLIT_TRIGGER.lower()) is False


# --- Non-shell-consumer literal bodies are not read as scripts (scanner leg) --------------
# A body a NON-SHELL consumer only prints or writes -- cat / tee / git commit -F - / echo /
# printf, via a QUOTED heredoc (``<<'EOF'``) or a single-quoted here-string (``<<<'..'``) --
# is DATA bash expands nothing in and no shell runs, so the descent must not judge it: these
# are ALLOWED. A body a shell RUNS (``$(..)`` into a shell, ``| bash``, ``<<<`` into bash, a
# quoted heredoc fed to bash) is still a script: REFUSED. ``_scanner_cut_nonshell_literals``
# draws that line in the scanner leg only; the belt leg stays origin/main's reading.

_CONSUMER_LITERAL_ALLOW = {
    "cat-quoted-heredoc": "cat <<'EOF'\nbash -c 'true\n%s %s id'\nEOF" % (_G, _L),
    "cat-heredoc-then-cmd": "cat <<'EOF'\nbash -c 'true\n%s %s id'\nEOF\necho done" % (_G, _L),
    "tee-file-heredoc": "tee notes.txt <<'EOF'\nbash -c 'true\n%s %s id'\nEOF" % (_G, _L),
    "git-commit-F-heredoc": "git commit -F - <<'EOF'\nbash -c 'x\n%s %s id'\nEOF" % (_G, _L),
}

_CONSUMER_LITERAL_REFUSE = {
    "subst-cat-into-bash-c": "bash -c \"$(cat <<'EOF'\n%s %s id\nEOF\n)\"" % (_G, _L),
    "eval-subst-cat": "eval \"$(cat <<'EOF'\n%s %s id\nEOF\n)\"" % (_G, _L),
    "echo-subst-pipe-bash": "echo \"$(cat <<'EOF'\n%s %s id\nEOF\n)\" | bash" % (_G, _L),
    "cat-heredoc-pipe-bash": "cat <<'EOF' | bash\nbash -c 'true\n%s %s id'\nEOF" % (_G, _L),
    "bash-here-string": "bash<<<'true\n%s %s id'" % (_G, _L),
    "bash-quoted-heredoc": "bash <<'EOF'\n%s %s id\nEOF" % (_G, _L),
    "sh-c-subst-cat": "sh -c \"$(cat <<'EOF'\n%s %s id\nEOF\n)\"" % (_G, _L),
}


@pytest.mark.parametrize(
    "cmd", list(_CONSUMER_LITERAL_ALLOW.values()), ids=list(_CONSUMER_LITERAL_ALLOW)
)
def test_a_nonshell_consumer_literal_body_is_not_read_as_a_script(cmd):
    assert _denied_by(cmd) is None, cmd


@pytest.mark.parametrize(
    "cmd", list(_CONSUMER_LITERAL_REFUSE.values()), ids=list(_CONSUMER_LITERAL_REFUSE)
)
def test_a_shell_consumer_body_is_still_read_as_a_script(cmd):
    assert _denied_by(cmd), cmd


def _strip_every_heredoc_and_here_string(text):
    """Mutant: cut EVERY heredoc body and here-string, shell-consumer ones included."""
    from kiro_crew.security.shell_commands import read_commands

    if "<<" not in text:
        return text
    cuts = []
    for cmd in read_commands(text, heredocs=True, nested=False):
        for doc in cmd.heredocs:
            cuts.append((doc.start, doc.end))
        for word, _target in cmd.redirects:
            if word.raw.startswith("<<<"):
                op = len(word.raw) - len(word.raw.lstrip("<"))
                body = [s for s in word.steps if s.start >= word.start + op]
                if body:
                    cuts.append((body[0].start, word.end))
    for a, b in sorted(cuts, reverse=True):
        text = text[:a] + text[b:]
    return text


def test_strip_none_mutant_reddens_the_allow_pins(monkeypatch):
    # Cut nothing: the descent reads the literal bodies and refuses them, so at least one
    # ALLOW pin flips to refused -- the exclusion is load-bearing for the allows.
    monkeypatch.setattr(_bodies, "_scanner_cut_nonshell_literals", lambda text: text)
    assert any(_denied_by(c) for c in _CONSUMER_LITERAL_ALLOW.values())


def test_strip_all_mutant_reddens_the_refuse_pins(monkeypatch):
    # Cut EVERY heredoc/here-string, shell-consumer bodies included: an executed self-ssh
    # slips through, so at least one REFUSE pin flips to allowed -- the consumer and the
    # not-piped guards are load-bearing for the refusals.
    monkeypatch.setattr(
        _bodies, "_scanner_cut_nonshell_literals", _strip_every_heredoc_and_here_string
    )
    assert any(_denied_by(c) is None for c in _CONSUMER_LITERAL_REFUSE.values())


def test_the_scanner_leg_allows_the_literal_and_refuses_the_executed():
    # The two legs split the work: the scanner leg is what judges executed payloads, and it
    # now allows a non-shell-consumer literal body while still refusing one a shell runs.
    assert (
        _bodies._folded_scanner_reaches_self(_CONSUMER_LITERAL_ALLOW["cat-quoted-heredoc"].lower())
        is False
    )
    assert (
        _bodies._folded_scanner_reaches_self(
            _CONSUMER_LITERAL_REFUSE["cat-heredoc-pipe-bash"].lower()
        )
        is True
    )


_CRON = "0 3 * * 0 /usr/local/bin/backup.sh"


# GPT lane on r42: in a descended frame, a heredoc body that only data consumers read, on a
# line where nothing can later run it, is data, as on main.
@pytest.mark.parametrize(
    "cmd",
    [
        "bash -c 'cat <<EOF | crontab -\n%s\nEOF'" % _CRON,
        "bash -c 'cat <<EOF | tee /tmp/x\n%s\nEOF'" % _CRON,
    ],
)
def test_a_data_body_nothing_can_run_is_not_a_script(cmd):
    assert _denied_by(cmd) is None, cmd


# The same bodies stay scripts when anything on the line could run them.
@pytest.mark.parametrize(
    "cmd",
    [
        "bash -c 'cat <<EOF | crontab -\n%s\nEOF' | bash" % _CRON,
        "bash -c 'cat <<EOF > /tmp/x | crontab -; sh /tmp/x\n%s\nEOF'" % _CRON,
        "bash -c 'cat <<EOF | crontab -\n%s\nEOF'; sh /tmp/x" % _CRON,
        "bash -c 'cat <<EOF | crontab -\n%s\nEOF' > /tmp/x" % _CRON,
        # Opus lane on r43: a verb a scheduler's shell assembles by quote removal.
        "bash -c 'cat <<EOF | crontab -\n0 3 * * 0 s\\sh %s id\nEOF'" % _L,
        "bash -c 'cat <<EOF | crontab -\n0 3 * * 0 s\"s\"h %s id\nEOF'" % _L,
        "bash -c 'cat <<EOF | crontab -\n0 3 * * 0 /usr/bin/ss\\h %s id\nEOF'" % _L,
        # GPT lane on r43: a POSIX class or brace list a later shell expands to the verb.
        "bash -c 'cat <<EOF | crontab -\n0 3 * * 0 /usr/bin/s[[:alpha:]]h %s id\nEOF'" % _L,
        "bash -c 'cat <<EOF | crontab -\n0 3 * * 0 /usr/bin/s{s,x}h %s id\nEOF'" % _L,
        # Lanes on r43: an extglob group, and a brace list opened before the last slash.
        "bash -c 'cat <<EOF | crontab -\n0 3 * * 0 bash -O extglob -c /usr/bin/@(ss)h %s id\nEOF'"
        % _L,
        "bash -c 'cat <<EOF | crontab -\n0 3 * * 0 {/usr/bin/ss,-l}h %s id\nEOF'" % _L,
        # GPT lane on r43: a bare ``*`` in the command position globs to any file name.
        "bash -c 'cat <<EOF | crontab -\n0 3 * * 0 * %s id\nEOF'" % _L,
        # GPT lane on r43: a leading ``*`` on a line that is not a full crontab record.
        "bash -c 'cat <<EOF | crontab -\n* %s id\nEOF'" % _L,
        "bash -c 'cat <<EOF | at now\nSHELL=/bin/bash\n* %s id\nEOF'" % _L,
        "bash -c 'cat <<EOF | crontab -\n0 3 * * *\n* %s id\nEOF'" % _L,
        # GPT lane on r43: cron-shaped words fed to a non-crontab shell consumer.
        "bash -c 'cat <<EOF | at now\n* -4 -6 -4 -6 %s id\nEOF'" % _L,
        "bash -c 'cat <<EOF | crontab -\n* -4 -6 -4 -6 %s id\nEOF'" % _L,
        # Opus lane on r43: a quoted body a scheduler's shell expands later.
        "bash -c 'cat <<'\"'\"'EOF'\"'\"' | crontab -\n0 3 * * 0 $(printf %%s%%s%%s s s h) %s id\nEOF'"
        % _L,
    ],
)
def test_a_data_body_something_can_run_stays_a_script(cmd):
    assert _denied_by(cmd) == _RULE, cmd


# GPT CI lane on 000163240e60: the TOP-LEVEL literal cut drops a written body that a later
# command on the list runs, so it must ask the same question the descended cut asks.
_SELF_SCRIPT_1 = "bash -c 'true\n%s %s id'" % (_G, _L)
_SELF_SCRIPT_2 = "bash -c 'true\nbash -c \"true\n/usr/bin/%s %s id\"'" % (_G, _L)


@pytest.mark.parametrize(
    "cmd",
    [
        "cat >/tmp/x <<'EOF'\n%s\nEOF\nbash /tmp/x" % _SELF_SCRIPT_2,
        "cat >/tmp/x <<'EOF'\n%s\nEOF\nbash /tmp/x" % _SELF_SCRIPT_1,
        "cat >/tmp/x <<'EOF'\n%s\nEOF\necho ok\nbash /tmp/x" % _SELF_SCRIPT_1,
        "cat >/tmp/x <<'EOF'\n%s\nEOF\n; bash /tmp/x" % _SELF_SCRIPT_1,
        "tee /tmp/x <<'EOF'\n%s\nEOF\n; . /tmp/x" % _SELF_SCRIPT_1,
    ],
)
def test_a_top_level_written_body_a_later_command_runs_is_a_script(cmd):
    assert _denied_by(cmd) == _RULE, cmd


_CRON_OK = "0 3 * * 0 /usr/local/bin/backup.sh"


# GPT CI lane on 502473f4da36: a plain shell wrapper carrying harmless options (``-ec``,
# ``-o pipefail``, ``--noprofile``, ``-euo pipefail``) is still a lone wrapper, so a benign
# crontab body stays data; the same wrapper with a self-ssh on line 2 is still a script.
_WRAPPERS = ["bash -ec", "bash -o pipefail -c", "bash --noprofile -c", "bash -euo pipefail -c"]


@pytest.mark.parametrize("wrap", _WRAPPERS)
def test_a_harmless_option_wrapper_keeps_a_data_body_data(wrap):
    assert _denied_by("%s 'cat <<EOF | crontab -\n%s\nEOF'" % (wrap, _CRON_OK)) is None, wrap


@pytest.mark.parametrize("wrap", _WRAPPERS)
def test_a_harmless_option_wrapper_still_refuses_a_self_ssh_body(wrap):
    cmd = "%s 'cat <<EOF | crontab -\n0 3 * * 0 %s %s id\nEOF'" % (wrap, _G, _L)
    assert _denied_by(cmd) == _RULE, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "bash -c 'cat <<EOF | crontab -\n%s\nEOF' extra" % _CRON_OK,
        "bash --rcfile /tmp/x -c 'cat <<EOF | crontab -\n%s\nEOF'" % _CRON_OK,
    ],
)
def test_an_unrecognised_wrapper_shape_keeps_todays_verdict(cmd):
    # Two operands and an option taking a path are not lone wrappers; the body is a script.
    assert _denied_by(cmd) == _RULE, cmd


# GPT lane on 502473f4da36: a wrapper that reads a startup file (--login/-l/-i) or BASH_ENV
# before -c can rebind the consumer, so it is NOT plain and the body stays a script.
@pytest.mark.parametrize("wrap", ["bash --login -c", "bash -l -c", "bash -i -c", "bash -lc"])
def test_a_startup_reading_wrapper_is_not_plain(wrap):
    benign = "%s 'cat <<EOF | crontab -\n%s\nEOF'" % (wrap, _CRON_OK)
    selfb = "%s 'cat <<EOF | crontab -\n0 3 * * 0 %s %s id\nEOF'" % (wrap, _G, _L)
    assert _denied_by(benign) == _RULE, benign  # not cut: body kept as a script
    assert _denied_by(selfb) == _RULE, selfb


def test_a_bash_env_prefixed_wrapper_is_not_plain():
    # BASH_ENV is read by a non-interactive bash before -c, so the consumer can be rebound.
    cmd = "BASH_ENV=/tmp/x bash -c 'cat <<EOF | crontab -\n%s\nEOF'" % _CRON_OK
    assert _denied_by(cmd) == _RULE, cmd


# Lanes on 024e4db9ba17 / 86da1c3e1372: a wrapper that can read a startup file or run user
# code before -c is not plain, so its body stays a script. zsh sources ~/.zshenv on every
# start; ksh/sh read a startup file through options the case-blind scanner cannot tell apart,
# so for them only a bare -c is plain; -o takes only an inert set option; -O/+O never plain.
_NOT_PLAIN_WRAPPERS = [
    "zsh -c",
    "zsh -fc",
    "ksh -Ec",
    "ksh -ec",
    "ksh -E -c",
    "ksh -o rc -c",
    "ksh -o errexit -c",
    "sh -ec",
    "bash -o rc -c",
    "bash -O extdebug -c",
]


@pytest.mark.parametrize("wrap", _NOT_PLAIN_WRAPPERS)
def test_a_non_plain_wrapper_keeps_a_data_body_a_script(wrap):
    assert _denied_by("%s 'cat <<EOF | crontab -\n%s\nEOF'" % (wrap, _CRON_OK)) == _RULE, wrap
    selfb = "%s 'cat <<EOF | crontab -\n0 3 * * 0 %s %s id\nEOF'" % (wrap, _G, _L)
    assert _denied_by(selfb) == _RULE, selfb


@pytest.mark.parametrize(
    "wrap",
    [
        "ksh -c",
        "sh -c",
        "bash -ec",
        "bash -euo pipefail -c",
        "bash -o pipefail -c",
        "dash -c",
        "bash --noprofile -c",
    ],
)
def test_a_plain_wrapper_still_allows_a_data_body(wrap):
    assert _denied_by("%s 'cat <<EOF | crontab -\n%s\nEOF'" % (wrap, _CRON_OK)) is None, wrap


# GPT CI lane on c72e19821729: a trailing inert builtin (bare :/true/exit, or exit <int>)
# runs nothing of the body, so it does not keep a data body a script; anything else does.
@pytest.mark.parametrize("tail", ["exit 0", "exit", ":", "true", "exit 137"])
def test_a_trailing_inert_builtin_keeps_a_data_body_data(tail):
    cmd = "bash -c 'cat <<EOF | crontab -\n%s\nEOF\n%s'" % (_CRON_OK, tail)
    assert _denied_by(cmd) is None, cmd


@pytest.mark.parametrize(
    "tail",
    [
        "exit $(bash /tmp/x)",
        'true "$(bash /tmp/x)"',
        ": $(bash /tmp/x)",
        "exit 0; bash /tmp/x",
        "exit 0 >/tmp/x",
        "false",
    ],
)
def test_a_trailing_command_that_is_not_proven_inert_keeps_the_body_a_script(tail):
    # A benign body the scanner does not refuse on its own, so the tail is what this pins.
    cmd = "bash -c 'cat <<EOF | crontab -\n%s\nEOF\n%s'" % (_CRON_OK, tail)
    assert _denied_by(cmd) == _RULE, cmd


# Opus lane on 9ea9efb23833: a login bash runs ~/.bash_logout on `exit`, so `exit` is NOT
# inert at the TOP-LEVEL cut; one line can write that file and the trailing exit run it.
_LOGOUT = "cat >~/.bash_logout <<'EOF'\n%s\nEOF" % _SELF_SCRIPT_2


@pytest.mark.parametrize("tail", ["exit", "exit 0"])
def test_a_top_level_exit_after_writing_bash_logout_is_a_script(tail):
    assert _denied_by("%s\n%s" % (_LOGOUT, tail)) == _RULE, tail


@pytest.mark.parametrize("tail", [":", "true"])
def test_a_top_level_inert_builtin_tail_matches_mains_verdict(tail):
    # ``:`` / ``true`` do not run ~/.bash_logout, so the body may be cut; main allows this
    # shape (it has no data-body cut), and this head agrees.
    assert _denied_by("%s\n%s" % (_LOGOUT, tail)) is None, tail


def test_a_descended_write_then_exit_is_still_a_script():
    # In a descended frame a ``>`` redirect fails closed, so the write-then-exit cannot form.
    cmd = "bash -lc 'cat >~/.bash_logout <<EOF\n%s\nEOF\nexit'" % _SELF_SCRIPT_2
    assert _denied_by(cmd) == _RULE, cmd


def test_a_top_level_written_body_nothing_runs_is_still_data():
    assert _denied_by("cat >/tmp/x <<'EOF'\n%s\nEOF" % _SELF_SCRIPT_1) is None


# Lanes on r44: the cut is gated by an ALLOWLIST of data consumers, so a body on a list that
# carries ANY other command -- a runner we never named included -- stays a script.
@pytest.mark.parametrize(
    "sibling",
    [
        "find /tmp -name x -exec /tmp/x \\;",
        "python -c 'exec(open(\"/tmp/x\").read())'",
        "make -f /tmp/x",
        "xargs -I{} bash -c {} </tmp/x",
        "env BASH_ENV=/tmp/x bash -lc true",
        "nohup /tmp/x",
        'eval "$(cat /tmp/x)"',
        "$(cat /tmp/x)",
        # Opus lane on r44: a command substitution masked inside a quoted word or a redirect.
        'echo "$(bash /tmp/x)"',
        'cat <"$(bash /tmp/x)"',
        'cat >/dev/null 2>"$(bash /tmp/x)"',
        'cat <<<"$(bash /tmp/x)"',
        'echo 2>&1 >"$(bash /tmp/x)"',
        'gh pr comment 1 --body-file - <"$(bash /tmp/x)"',
    ],
)
def test_a_written_body_any_sibling_command_could_run_is_a_script(sibling):
    cmd = "cat >/tmp/x <<'EOF'\n%s\nEOF\n%s" % (_SELF_SCRIPT_2, sibling)
    assert _denied_by(cmd) == _RULE, cmd


def test_mains_own_refusal_of_the_unwrapped_crontab_heredoc_is_unchanged():
    # Pre-existing: origin/main refuses the schedule line when no wrapper is around it.
    assert _denied_by("cat <<EOF | crontab -\n%s\nEOF" % _CRON) == _RULE


# --- A grouped / piped / process-sub consumer is NOT a plain command list: body left in ---
# The literal-body cut fires only for a PLAIN COMMAND LIST: every command head an ordinary
# word (no compound keyword { } if then while until for case coproc function time ! and no
# function-def), no pipe separator, no >( ) / <( ). Any consumer whose output can still reach
# a shell -- piped, inside a ( ) subshell / { } group / $( ) substitution / if-while-for-case
# compound / function body / coproc, or writing to a >( ) -- keeps its body, so a self-ssh a
# shell would run on it stays REFUSED, exactly as the pre-exclusion head refused it (no
# head-added refusal lost; the differential lists the only intended losses).
_CONSUMER_ESCAPE_REFUSE = {
    "tee-procsub-bash": "tee >(bash) <<'EOF'\nbash -c 'true\n%s %s id'\nEOF" % (_G, _L),
    "cat-redirect-procsub-bash": "cat <<'EOF' > >(bash)\nbash -c 'true\n%s %s id'\nEOF" % (_G, _L),
    "subshell-cat-pipe-bash": "(cat <<'EOF'\nbash -c 'true\n%s %s id'\nEOF) | bash" % (_G, _L),
    "inline-group-cat-pipe-bash": "{ cat <<'EOF'\nbash -c 'true\n%s %s id'\nEOF; } | bash"
    % (_G, _L),
    "subst-cat-fed-to-bash": "bash -c \"$(cat <<'EOF'\n%s %s id\nEOF\n)\"" % (_G, _L),
    "newline-group-cat-pipe-bash": "{\ncat <<'EOF'\nbash -c 'true\n%s %s id'\nEOF\n} | bash"
    % (_G, _L),
    "if-then-cat-pipe-bash": "if true; then\ncat <<'EOF'\nbash -c 'true\n%s %s id'\nEOF\nfi | bash"
    % (_G, _L),
    "while-do-cat-pipe-bash": "while :; do\ncat <<'EOF'\nbash -c 'true\n%s %s id'\nEOF\ndone | bash"
    % (_G, _L),
    "function-body-cat-pipe-bash": "f() {\ncat <<'EOF'\nbash -c 'true\n%s %s id'\nEOF\n}\nf | bash"
    % (_G, _L),
    "coproc-cat-to-fd": "coproc bash\ncat <<'EOF' >&\"${COPROC[1]}\"\nbash -c 'true\n%s %s id'\nEOF"
    % (_G, _L),
}


@pytest.mark.parametrize(
    "cmd", list(_CONSUMER_ESCAPE_REFUSE.values()), ids=list(_CONSUMER_ESCAPE_REFUSE)
)
def test_a_grouped_or_piped_consumer_body_stays_refused(cmd):
    assert _denied_by(cmd), cmd


def _cut_even_inside_groups(text):
    """Mutant: cut a non-shell consumer's quoted body / single-quoted here-string with NO
    terminal / group / process-sub guard -- the loose pre-tightening shape."""
    from kiro_crew.security.shell_commands import read_commands

    if "<<" not in text:
        return text
    cuts = []
    for cmd in read_commands(text, heredocs=True, nested=False):
        if not _bodies._is_nonshell_consumer(cmd.argv):
            continue
        for doc in cmd.heredocs:
            if doc.quoted:
                cuts.append((doc.start, doc.end))
        for word, _target in cmd.redirects:
            if word.raw.startswith("<<<"):
                op = len(word.raw) - len(word.raw.lstrip("<"))
                body = [s for s in word.steps if s.start >= word.start + op]
                if body:
                    cuts.append((body[0].start, word.end))
    for a, b in sorted(cuts, reverse=True):
        text = text[:a] + text[b:]
    return text


def test_cutting_a_grouped_consumer_reddens_an_escape_form(monkeypatch):
    # Dropping the terminal/ungrouped guard cuts a consumer whose output reaches a shell,
    # so at least one escape form flips to allowed -- the guard is load-bearing.
    monkeypatch.setattr(_bodies, "_scanner_cut_nonshell_literals", _cut_even_inside_groups)
    assert any(_denied_by(c) is None for c in _CONSUMER_ESCAPE_REFUSE.values())


# --- r37 (option B): the literal cut is TOP-LEVEL ONLY; nested printed bodies over-refuse --
# Five versions of a by-descent (judgment-view) cut each leaked under independent review
# (piped carrier, outer redirect, enclosing runner, glued/fd/quoted/./ path spellings, an
# earlier exec/&/trap sibling). r37 keeps r36's shape: only the top-level command list is
# cut; a body reached by descent is always read as a script. Every leak spelling found is
# pinned DENY below, so a future nested cut must clear them all.
def _wrap(inner: str, levels: int) -> str:
    """Wrap *inner* in ``bash -c '...'`` *levels* times, single-quote-escaped."""
    for _ in range(levels):
        inner = "bash -c '" + inner.replace("'", "'\\''") + "'"
    return inner


_NESTED_LEAK_SPELLINGS_REFUSE = {
    "pipe-bash-parent": "bash -c \"cat <<'EOF'\n%s %s id\nEOF\" | bash" % (_G, _L),
    "pipeamp-bash-parent": "bash -c \"cat <<'EOF'\n%s %s id\nEOF\" |& bash" % (_G, _L),
    "2>&1-pipe-bash": "bash -c \"cat <<'EOF'\n%s %s id\nEOF\" 2>&1 | bash" % (_G, _L),
    "group-pipe-bash": "{ bash -c \"cat <<'EOF'\n%s %s id\nEOF\"; } | bash" % (_G, _L),
    "func-pipe-bash": "g(){ bash -c \"cat <<'EOF'\n%s %s id\nEOF\"; }; g | bash" % (_G, _L),
    "outer-redirect-then-bash": "bash -c \"cat <<'EOF'\n%s %s id\nEOF\" > /tmp/x; bash /tmp/x"
    % (_G, _L),
    "B1-writer-alone-runner-outside": "bash -c 'cat > /tmp/x <<\"E\"\n%s %s id\nE'; bash /tmp/x"
    % (_G, _L),
    "tee-then-source": "bash -c 'tee /tmp/x <<\"E\"\n%s %s id\nE\nsource /tmp/x'" % (_G, _L),
    "tee-then-bash": "bash -c 'tee /tmp/x <<\"E\"\n%s %s id\nE\nbash /tmp/x'" % (_G, _L),
    "write-then-path-bash": "bash -c 'cat > /tmp/x <<\"E\"\n%s %s id\nE\n/bin/bash /tmp/x'"
    % (_G, _L),
    "write-then-env-sh": "bash -c 'tee /tmp/x <<\"E\"\n%s %s id\nE\nenv sh /tmp/x'" % (_G, _L),
    "write-then-direct-exec": 'bash -c \'cat > /tmp/x <<"E"\n%s %s id\nE\nchmod +x /tmp/x\n'
    "/tmp/x'" % (_G, _L),
    "glued-redirect": "bash -c 'cat >/tmp/x <<\"E\"\n%s %s id\nE\nbash /tmp/x'" % (_G, _L),
    "fd1-redirect": "bash -c 'cat 1>/tmp/x <<\"E\"\n%s %s id\nE\nbash /tmp/x'" % (_G, _L),
    "dotslash-run": "bash -c 'cd /tmp; cat > x <<\"E\"\n%s %s id\nE\nbash ./x'" % (_G, _L),
    "quoted-name": 'bash -c \'cat > "/tmp/x" <<"E"\n%s %s id\nE\nbash /tmp/x\'' % (_G, _L),
    "earlier-exec-redirect": "bash -c 'exec >/tmp/x; cat <<\"E\"\n%s %s id\nE\n'; bash /tmp/x"
    % (_G, _L),
    "earlier-background": "bash -c 'sleep 1 && bash /tmp/x & cat > /tmp/x <<\"E\"\n%s %s id\nE'"
    % (_G, _L),
    "earlier-trap": 'bash -c \'trap "bash /tmp/x" EXIT; cat > /tmp/x <<"E"\n%s %s id\nE\''
    % (_G, _L),
}


@pytest.mark.parametrize(
    "cmd", list(_NESTED_LEAK_SPELLINGS_REFUSE.values()), ids=list(_NESTED_LEAK_SPELLINGS_REFUSE)
)
def test_every_nested_cut_leak_spelling_is_refused(cmd):
    assert _denied_by(cmd), cmd


# Opus r37 lane: the same leak spellings one nesting level down -- the printed body itself
# holds an inner ``bash -c`` whose SECOND line is the self-ssh. The cut must not run in a
# descended frame, or the inner script is never queued and these allow.
_ONE_LEVEL_DOWN_REFUSE = {
    k: v.replace("\n%s %s id\n" % (_G, _L), '\nbash -c "true\n%s %s id"\n' % (_G, _L))
    for k, v in _NESTED_LEAK_SPELLINGS_REFUSE.items()
}


@pytest.mark.parametrize(
    "cmd", list(_ONE_LEVEL_DOWN_REFUSE.values()), ids=list(_ONE_LEVEL_DOWN_REFUSE)
)
def test_a_leak_spelling_one_level_down_is_refused(cmd):
    assert _denied_by(cmd), cmd


def test_cutting_in_a_descended_frame_reddens_a_one_level_down_leak(monkeypatch):
    # Mutant: the r36 shape, cutting in every frame. The inner script is dropped.
    real_tokens = _bodies._self_tokens

    def cut_then_tokenize(text, **kwargs):  # type: ignore[no-untyped-def]
        return real_tokens(_bodies._scanner_cut_nonshell_literals(text), **kwargs)

    monkeypatch.setattr(_bodies, "_self_tokens", cut_then_tokenize)
    assert any(_denied_by(c) is None for c in _ONE_LEVEL_DOWN_REFUSE.values())


# Documented over-refusal (GPT lane F2 on r36, the lane's clears-when): a lone cat/tee/
# git commit -F - of a quoted literal body NESTED at depth 1-3. base ALLOWS these; r37 REFUSES
# them (safe false-deny, inside the 0-new-ALLOW bar). Pinned so a change in either direction
# is a visible decision, not drift.
_PRINTED_SELF_SSH = {
    "cat-quoted-heredoc": "cat <<'EOF'\n%s %s id\nEOF" % (_G, _L),
    "tee-file-heredoc": "tee /tmp/x <<'EOF'\n%s %s id\nEOF" % (_G, _L),
    "git-commit-F-heredoc": "git commit -F - <<'EOF'\n%s %s id\nEOF" % (_G, _L),
}
_NESTED_LITERAL_OVER_REFUSED = {
    "%s-L%d" % (name, lvl): _wrap(cmd, lvl)
    for name, cmd in _PRINTED_SELF_SSH.items()
    for lvl in (1, 2, 3)
}

_NESTED_EXECUTED_REFUSE = {
    "%s-L%d" % (name, lvl): _wrap(cmd, lvl)
    for name, cmd in _CONSUMER_LITERAL_REFUSE.items()
    for lvl in (2, 3)
}


@pytest.mark.parametrize(
    "cmd", list(_NESTED_LITERAL_OVER_REFUSED.values()), ids=list(_NESTED_LITERAL_OVER_REFUSED)
)
def test_a_nested_literal_body_is_over_refused_as_documented(cmd):
    assert _denied_by(cmd), cmd


@pytest.mark.parametrize(
    "cmd", list(_NESTED_EXECUTED_REFUSE.values()), ids=list(_NESTED_EXECUTED_REFUSE)
)
def test_a_nested_shell_consumer_body_is_still_read_as_a_script(cmd):
    assert _denied_by(cmd), cmd


# --- r39 (GPT F1): the literal cut applies only to the FIRST command of the list ---------
# An earlier command on the same line can rebind the consumer's name to a shell that runs
# the body (``hash -p /bin/bash cat``, an alias, a function, a PATH change), and no static
# read can rule that out, so a consumer with any preceding command is not cut. Commands
# AFTER the consumer cannot rebind it (``cat-heredoc-then-cmd`` above stays allowed).
_BODY = "<<'EOF'\nbash -c 'true\n%s %s id'\nEOF" % (_G, _L)
_REBOUND_CONSUMER_REFUSE = {
    "hash-p": "cd /usr/bin; hash -p /bin/bash cat; cat " + _BODY,
    "alias": "shopt -s expand_aliases; alias cat=bash; cat " + _BODY,
    "function": "cat() { bash; }; cat " + _BODY,
    "function-keyword": "function cat { bash; }; cat " + _BODY,
    "path-assign": "PATH=/tmp/evil:$PATH; cat " + _BODY,
    "path-export": "export PATH=/tmp/evil; tee x " + _BODY,
    "path-prefix": "PATH=/tmp/evil cat " + _BODY,
}


@pytest.mark.parametrize(
    "cmd", list(_REBOUND_CONSUMER_REFUSE.values()), ids=list(_REBOUND_CONSUMER_REFUSE)
)
def test_a_consumer_an_earlier_command_could_rebind_is_not_cut(cmd):
    assert _denied_by(cmd), cmd


def test_cutting_a_consumer_after_an_earlier_command_reddens_the_hash_p_row(monkeypatch):
    # Mutant: the pre-r39 cut, which cut a consumer anywhere in the list. The hash -p body is
    # cut and the executed self-ssh slips through.
    real = _bodies._scanner_cut_nonshell_literals

    def cut_after_first(text):  # type: ignore[no-untyped-def]
        head, sep, rest = text.partition("; cat ")
        return head + sep + real("cat " + rest)[4:] if sep else real(text)

    monkeypatch.setattr(_bodies, "_scanner_cut_nonshell_literals", cut_after_first)
    assert _denied_by(_REBOUND_CONSUMER_REFUSE["hash-p"]) is None


# Documented over-refusal (GPT F2 on r38, graded LOW): the floor reads the lowercased
# command, so a body line equal to a quoted upper-case tag except for case ends the
# here-document early and the rest of the printed body is read as a script. base ALLOWS
# this; this head REFUSES it (safe false-deny, inside the 0-new-ALLOW bar).
def test_a_case_only_tag_match_in_a_printed_body_is_over_refused_as_documented():
    cmd = "cat <<'EOF'\neof\nbash -c 'true\n%s %s id'\nEOF" % (_G, _L)
    assert _denied_by(cmd), cmd


# --- r40 (Opus fork lane on r39): a ``#`` after a ``\``-escaped character is not a comment --
# bash keeps an escaped blank or metacharacter in the current word, so the ``#`` after it
# does not begin a word and opens no comment; the ``bash -c`` that follows still runs. The
# fold scanner counts a ``\X`` pair as a word character before its ``#`` test.
_RUN = "; bash -c 'true\n%s %s id'" % (_G, _L)
_ESCAPED_HASH_REFUSE = {
    "escaped-blank": "echo a\\ #" + _RUN,
    "escaped-semicolon": "echo a\\;#" + _RUN,
    "escaped-pipe": "echo a\\|#" + _RUN,
    "escaped-amp": "echo a\\&#" + _RUN,
    "escaped-paren": "echo a\\(#" + _RUN,
}


@pytest.mark.parametrize("cmd", list(_ESCAPED_HASH_REFUSE.values()), ids=list(_ESCAPED_HASH_REFUSE))
def test_a_hash_after_an_escaped_character_is_not_a_comment(cmd):
    assert _denied_by(cmd), cmd


def test_a_real_comment_still_folds_and_a_quoted_escape_keeps_its_verdict():
    # An unescaped blank before ``#`` is a real comment: bash drops the rest of the line,
    # so the ``bash -c`` after it never runs. Allowed today; stays allowed.
    assert _denied_by("echo a #" + _RUN) is None
    # Inside single quotes the backslash is literal and ``#`` is data.
    assert _denied_by("echo 'a\\ #'") is None


def test_treating_an_escape_pair_as_a_blank_reddens_the_escaped_hash_rows(monkeypatch):
    # Mutant: the pre-r40 fold, which read the last character of an escape pair as the
    # character before ``#``. It drops the bash -c payload and the self-ssh slips through.
    real = _bodies._fold_outside_quoted_heredocs

    def fold_escape_as_blank(text):  # type: ignore[no-untyped-def]
        return real(text.replace("\\ #", " #"))

    monkeypatch.setattr(_bodies, "_fold_outside_quoted_heredocs", fold_escape_as_blank)
    assert _denied_by(_ESCAPED_HASH_REFUSE["escaped-blank"]) is None


# GPT lane on r40: the same class after a CLOSED word-forming construct. A closed backtick,
# a closed ``$(( ))``, a closed ``<( )`` / ``>( )`` and a closed extglob group each end inside
# a word, so a glued ``#`` after them is not a comment either (``$( )`` was already handled).
_GLUED_CLOSE_HASH_REFUSE = {
    "backtick": "echo `x`#" + _RUN,
    "arithmetic": "echo $((1))#" + _RUN,
    "procsub-in": "cat <(x)#" + _RUN,
    "procsub-out": "tee >(x)#" + _RUN,
    "extglob": "shopt -s extglob; echo @(a)#" + _RUN,
}


@pytest.mark.parametrize(
    "cmd", list(_GLUED_CLOSE_HASH_REFUSE.values()), ids=list(_GLUED_CLOSE_HASH_REFUSE)
)
def test_a_hash_glued_after_a_closed_word_construct_is_not_a_comment(cmd):
    assert _denied_by(cmd), cmd


def test_a_hash_after_a_subshell_close_is_still_a_comment():
    # A plain ``( )`` subshell close is an operator, so a glued ``#`` IS a comment there.
    assert _denied_by("(true)#" + _RUN) is None


# Opus lane on r41: a literal ``((`` inside ``"..."`` or ``${...}`` is text, not the
# arithmetic command, so it must not open a frame that hides the comment and next line.
_LITERAL_DPAREN_REFUSE = {
    "bash-dq": "bash -c 'echo \"((\" # x\\\n%s %s id'" % (_G, _L),
    "sh-dq": "sh -c 'echo \"((\" # x\\\n%s %s id'" % (_G, _L),
    "eval-dq": "eval 'echo \"((\" # x\\\n%s %s id'" % (_G, _L),
    "param": "bash -c 'echo ${v:-((} # x\\\n%s %s id'" % (_G, _L),
}


@pytest.mark.parametrize(
    "cmd", list(_LITERAL_DPAREN_REFUSE.values()), ids=list(_LITERAL_DPAREN_REFUSE)
)
def test_a_literal_double_paren_in_a_string_does_not_hide_the_next_line(cmd):
    assert _denied_by(cmd) == _RULE, cmd


# GPT lane on r41: ``commit`` anywhere in a git argv is not ``git commit``. A global option
# can define a shell alias that runs stdin, so the body stays a script the descent reads.
_GIT_NOT_COMMIT_BODY = "<<'EOF'\nbash -c 'true\n%s %s id'\nEOF" % (_G, _L)


@pytest.mark.parametrize(
    "head",
    [
        "git -c 'alias.x=!f() { bash; }; f' x commit --file=- ",
        "git -C /tmp commit -F - ",
        "git log commit -F - ",
    ],
)
def test_only_a_plain_git_commit_reads_its_stdin_as_data(head):
    assert _denied_by(head + _GIT_NOT_COMMIT_BODY) == _RULE, head


# Security Scope on r41: a gh pr/issue post reads its ``--body-file -`` stdin as text, as
# main allows the same repro in a one-line ``--body``. Another gh verb is not exempt.
@pytest.mark.parametrize(
    "head",
    [
        "gh pr comment 9332 --body-file - ",
        "gh issue create -t x --body-file=- ",
        "gh pr create -F - ",
        "gh --repo owner/repo pr comment 1 --body-file - ",
        "gh -R owner/repo issue comment 1 --body-file - ",
        "gh --repo=owner/repo pr comment 1 --body-file - ",
    ],
)
def test_a_gh_post_reads_its_stdin_body_as_data(head):
    assert _denied_by(head + _GIT_NOT_COMMIT_BODY) is None, head


@pytest.mark.parametrize("head", ["gh x pr comment --body-file - ", "gh api -F - "])
def test_another_gh_verb_reading_stdin_is_still_a_script(head):
    assert _denied_by(head + _GIT_NOT_COMMIT_BODY) == _RULE, head
