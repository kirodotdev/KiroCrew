"""Every argv window on the floor survives a ``case`` compound in its argv.

The six consumers of the substitution-depth walker each bound a different
window (the mint-verb scan, the ``pkill`` pattern leg, the bare-``kill`` body
window, the product CLI's operand list, the rsync environment walk and the
ssh-family argument walk).  Before ``_SubstitutionDepth`` they all shared one
blind spot: a ``case`` PATTERN's ``)`` scored as a substitution closer, so the
window closed at the first separator after ``esac`` and the argument that named
the target was never read.  Only the bare-``kill`` window had a compensation
(its raw-text re-derivation).

Each consumer gets the two measurements the issue asks for: the shape that
bash runs and the floor must deny, and a false-positive control -- an allowed
command carrying ``case ... esac`` followed by an unrelated command that
mentions the protected word -- that must stay allowed.  Verdicts go through the
public gate so the tests survive a refactor of how the floors are wired.
"""

from __future__ import annotations

import pytest

from kiro_crew.security import argv_floor, is_denied

_K = "k" + "ill"
_PK = "p" + _K
_NAME = "kiro" + "crew"
_TOK = "to" + "ken"

_RULE_KILL = "self-protection-" + _K
_RULE_MINT = "credential-exfil-" + _NAME + "-" + _TOK
_RULE_SSH = "sandbox-escape-ssh-self"
_RESTART = "re" + "start"
_RULE_RESTART = "self-protection-" + _RESTART

# The esac-tail body the issue measured, parametrised on the trailing command.
_TAIL = "case x in x) :;; esac; {cmd}"


def _rule(cmd: str) -> "str | None":
    verdict = is_denied(cmd)
    if verdict is None:
        return None
    return verdict.rsplit("rule=", 1)[1].split()[0]


class TestRowOneIsDeniedByTheTokenWindowAlone:
    """The issue's acceptance: revert the raw window locally, row 1 still denies."""

    ROW_ONE = f"{_K} $(" + _TAIL.format(cmd=f"pgrep -f {_NAME}") + ")"

    def test_with_the_raw_window_removed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(argv_floor, "_bare_" + _K + "_raw_bodies", lambda source: [])
        assert argv_floor._is_self_kill(self.ROW_ONE) is True

    def test_through_the_gate(self) -> None:
        assert _rule(self.ROW_ONE) == _RULE_KILL


class TestEveryConsumerReadsPastThePattern:
    """One denied shape per consumer whose target sits after ``esac``."""

    @pytest.mark.parametrize(
        ("cmd", "rule"),
        [
            # mint-verb scan: the verb after the substitution's esac-tail body
            (f"{_NAME} $(" + _TAIL.format(cmd="echo status") + f") {_TOK}", _RULE_MINT),
            (f"{_NAME} $(case x in x|y) :;; esac; echo status) {_TOK}", _RULE_MINT),
            # the case WORD is itself a substitution spanning tokens, and the
            # body's ``)`` rides on ``esac`` -- the verb is the next word
            (f"{_NAME} $(case $(echo x) in x) :;; esac) {_TOK}", _RULE_MINT),
            # a QUOTED ``)`` as the pattern: de-quoted it is ``))``, and bash
            # refuses that unquoted, so the run is pattern text
            (f"{_NAME} $(case x in ')') :;; esac) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(case x in 'x)') :;; esac; echo status) {_TOK}", _RULE_MINT),
            # newlines where bash allows them: before ``in``, before the first
            # pattern, before ``;;`` and before ``esac`` (a frame renders each
            # as a standalone ``;``)
            (f"{_NAME} $(case x\nin x) :;; esac; :) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(case x in\nx) :;; esac; :) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(case x in x)\n:;;\nesac; :) {_TOK}", _RULE_MINT),
            # an option-shaped pattern (``-c)``) the operand scan would skip
            (f"{_NAME} $(case $1 in -c) echo status;; esac) {_TOK}", _RULE_MINT),
            # the empty pattern de-quotes to a bare ``)`` and terminates
            (f"{_K} $(case x in '') :;; esac; pgrep -f {_NAME})", _RULE_KILL),
            # a ``)`` inside a parameter expansion, as the pattern and as a body word
            (f"{_NAME} $(case x in ${{v:-x)}}) :;; esac; :) {_TOK}", _RULE_MINT),
            (f"{_PK} -f $(case x in ${{v:-x)}}) :;; esac; echo {_NAME})", _RULE_KILL),
            (f"{_NAME} $(: ${{v:-x)}}; echo status) {_TOK}", _RULE_MINT),
            # the expansion split across tokens by a blank (minted on the R9 head)
            (f"{_NAME} $(case x in x) : ${{v:-x y)}};; esac; :) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(case ${{v:-x y)}} in x) :;; esac; :) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(: ${{v:-x y)}}; echo status) {_TOK}", _RULE_MINT),
            (f"{_PK} -f $(case x in x) : ${{v:-x y)}};; esac; printf {_NAME})", _RULE_KILL),
            ("rsync -e ssh $(case x in x) : ${v:-x y)};; esac; echo src) 127.0.0.1:/x", _RULE_SSH),
            # the case WORD is a process substitution: its ``)`` is not the window's
            (f"{_NAME} $(case <(printf x) in *) :;; esac) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(case <(printf x) in *) :;; esac; :) {_TOK}", _RULE_MINT),
            (f"{_PK} -f $(case >(cat) in *) :;; esac; echo {_NAME})", _RULE_KILL),
            # ``case`` glued to a control operator, and ``esac`` as a quoted pattern
            (f"{_NAME} $(:;case x in x) :;; esac; echo status) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(x&&case x in x) :;; esac; echo status) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(case x in x) :;; esac;case y in y) :;; esac; :) {_TOK}", _RULE_MINT),
            (f"{_K} $(case x in x) :;; esac;case y in y) :;; esac; pgrep -f {_NAME})", _RULE_KILL),
            (f"{_NAME} $(case x in x) : ${{v:-x) y}};; esac; :) {_TOK}", _RULE_MINT),
            (f"{_K} $(pgrep -f 'sync-${{ENV'; pgrep -f {_NAME})", _RULE_KILL),
            # the next pattern glued to ``;;``, and the first pattern glued to ``in``
            (f"{_NAME} $(case x in x) :;;y) :;; esac) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(case x in(x) :;; esac; :) {_TOK}", _RULE_MINT),
            (f"{_K} $(case x in x) :;;y) :;; esac; pgrep -f {_NAME})", _RULE_KILL),
            ("rsync -e ssh $(case x in 'esac') :;; esac; echo src) 127.0.0.1:/x", _RULE_SSH),
            # a quoted ``esac`` pattern with an EMPTY body: ``esac);;`` is one token
            (f"{_NAME} $(case x in 'esac');; esac; echo status) {_TOK}", _RULE_MINT),
            (f"{_PK} -f $(case x in 'esac');; esac; echo {_NAME})", _RULE_KILL),
            ("rsync -e ssh $(case x in 'esac');; esac; echo src) 127.0.0.1:/x", _RULE_SSH),
            # a nested ``case`` glued to the outer pattern
            (f"{_NAME} $(case x in x)case y in y) :;; esac;; esac; :) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(case $(echo x) in x) :;; esac; echo status) {_TOK}", _RULE_MINT),
            # pkill pattern leg: the name is produced INSIDE the body, after esac
            (f"{_PK} -f $(" + _TAIL.format(cmd=f"echo {_NAME}") + ")", _RULE_KILL),
            (f"{_PK} -f $(case $(echo x) in x) :;; esac; echo {_NAME})", _RULE_KILL),
            # bare kill body window (row 1 of the issue, plus the paren-pattern spelling)
            (f"{_K} $(" + _TAIL.format(cmd=f"pgrep -f {_NAME}") + ")", _RULE_KILL),
            (f"{_K} $(case x in (x) :;; esac; pgrep -f {_NAME})", _RULE_KILL),
            # ssh-family argument walk: the self-host operand after the body
            ("rsync -e ssh $(" + _TAIL.format(cmd="echo src") + ") 127.0.0.1:/x", _RULE_SSH),
            # a quoted ``)`` inside a bracket pattern (R10: ``x[\\)]|x)`` minted)
            (f"{_NAME} $(case x in x[\\)]|x) :;; esac; echo status) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(case x in [[:alpha:]\\)]) :;; esac; echo status) {_TOK}", _RULE_MINT),
            (f"{_PK} -f $(case x in x[\\)]|x) :;; esac; echo {_NAME})", _RULE_KILL),
            ("rsync -e ssh $(case x in x[\\)]|x) :;; esac; echo src) 127.0.0.1:/x", _RULE_SSH),
            # the self host AFTER the substitution: a word opening or inside ``$( )``
            # is not the ssh destination, so the positional slot is still open
            ("ssh $(case x in x) echo -v;; esac) localhost", _RULE_SSH),
            ("ssh -p $(case x in 22) echo 22;; esac) localhost", _RULE_SSH),
            ("scp $(case x in a) echo a;; *) echo b;; esac) localhost:/x", _RULE_SSH),
            ("rsync -e ssh $(case x in a) echo -z;; *) echo -v;; esac) f 127.0.0.1:/x", _RULE_SSH),
            ("ssh $(echo -v) localhost", _RULE_SSH),  # the plain spelling, allowed on base
            # a body word that spells the self host is still checked (fail-closed)
            ("scp $(case x in a) echo a;; *) echo localhost;; esac) host:/x", _RULE_SSH),
            # a backtick-spelled case (R10: ``\`case`` read as the word with depth 0)
            (f"{_NAME} `case x in x) :;; esac; :` {_TOK}", _RULE_MINT),
            # a backtick substitution as the pattern, with a quoted ``)`` inside (R12)
            (f"{_NAME} $(case x in `printf ')'`) :;; esac; echo status) {_TOK}", _RULE_MINT),
            (f"{_PK} -f $(case x in `printf ')'`) :;; esac; echo {_NAME})", _RULE_KILL),
            # a prefixed opener (R12: ``$x$(case`` armed nothing)
            (f"{_NAME} $x$(case y in y) :;; esac; :) {_TOK}", _RULE_MINT),
            (f"{_PK} -f $x$(case y in y) :;; esac; echo {_NAME})", _RULE_KILL),
            (f"{_K} x$(case y in y) :;; esac; pgrep -f {_NAME})", _RULE_KILL),
            # a case INSIDE a pattern substitution: its pattern's ``)`` is its own (R14
            # GPT: the inner ``y)`` closed the outer substitution and the verb minted)
            (f"{_NAME} $(case x in $(case y in y) :;; esac)) :;; esac; :) {_TOK}", _RULE_MINT),
            (
                f"{_NAME} $(case x in $(case y in y) :;; esac; case z in z) :;; esac)) "
                f":;; esac) {_TOK}",
                _RULE_MINT,
            ),
            (f'{_NAME} $(case x in "$(case y in y) :;; esac)"|x) :;; esac; :) {_TOK}', _RULE_MINT),
            (f"{_PK} -f $(case x in $(case y in y) :;; esac)) :;; esac; echo {_NAME})", _RULE_KILL),
            (f"{_K} $(case x in `case y in y) :;; esac`) :;; esac; pgrep -f {_NAME})", _RULE_KILL),
            # a quoted ``'esac'`` as a body command, and as a pattern with a blank before ``)``
            (f"{_NAME} $(case x in x) 'esac';; y) :;; esac; :) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(case x in 'esac' ) :;; esac; :) {_TOK}", _RULE_MINT),
            # a quoted ``'${'`` literal is a WORD (R14 Opus: rewritten to ``{``, read as
            # a function-body opener, the window ended before the target)
            (f"{_NAME} '${{' to$()ken", _RULE_MINT),
            (f"{_K}all '${{' kiro$()crew", _RULE_KILL),
            (f"{_K}all '${{' >/dev/null {_NAME}", _RULE_KILL),
            # the top-level text glued after a substitution's closer is the word bash
            # hands over (R15 GPT: the whole token ``esac)<verb>`` was classified)
            (f"{_NAME} $(case x in x) :;; esac){_TOK}", _RULE_MINT),
            (f"{_NAME} $(true; :){_TOK}", _RULE_MINT),
            (f"{_NAME} $(case x in x) :;; esac){_RESTART}", _RULE_RESTART),
            (f"{_NAME} $(: ; :) {_RESTART}", _RULE_RESTART),
            # a substitution opened in a REDIRECT word is still a command list this
            # window reads through (R15 GPT: the data token never advanced the walker)
            (f"{_NAME} 2>$(case x in x) :;; esac; echo /dev/null) {_RESTART}", _RULE_RESTART),
            (f"{_NAME} 2> $(: ; echo /dev/null) {_RESTART}", _RULE_RESTART),
            (f"{_NAME} 2>$(: ; echo /dev/null) {_TOK}", _RULE_MINT),
            # a keeper in ARGUMENT position is a word, not a hand-through (R15 Opus:
            # ``time`` armed a phantom case and the host slot was never checked)
            ("scp /etc/passwd time case x in localhost:/tmp/", _RULE_SSH),
            (f"{_PK} -f time case x in {_NAME}", _RULE_KILL),
            # the token that POPS the substitution carries the host glued after it
            # (R15 Opus: ``top_level`` was read before the token was fed)
            ("ssh -C $(: )localhost", _RULE_SSH),
            ("ssh $(: )localhost", _RULE_SSH),
            ("ssh -v $(: )localhost uptime", _RULE_SSH),
            # a substitution whose output IS the self host, statically
            ("ssh $(printf 127.0.0.1) uptime", _RULE_SSH),
            ("ssh $(echo localhost) uptime", _RULE_SSH),
            # scp/rsync check every operand, a substitution's body words included (base)
            ("scp file $(grep -v localhost hosts):/tmp/", _RULE_SSH),
            # a quoted ``'esac')`` PATTERN with a clause body (R16 GPT: read as the
            # closer, the body's ``;`` ended the window before the real closer)
            (f"{_NAME} $(case x in 'esac') echo hi; :;; esac; :) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(case x in 'esac') echo hi; esac; :) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(case x in 'esac')\n echo hi;; esac; :) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(case x in 'esac') >/dev/null;; esac; :) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(case x in 'esac')$(echo hi);; esac; :) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(case x in x) :;; 'esac') echo hi; :;; esac; :) {_TOK}", _RULE_MINT),
            (f"{_K} $(case x in 'esac') echo hi; :;; esac; pgrep -f {_NAME})", _RULE_KILL),
            (f"{_K}all $(case x in 'esac') echo hi; :;; esac; echo nginx) {_NAME}", _RULE_KILL),
            ("ssh $(case x in 'esac') echo -v;; esac; :) localhost", _RULE_SSH),
            # a ``#`` behind a quoted blank is data, not a comment (R16 Opus: the
            # de-quoted ``a #`` ended the window and the verb behind it minted)
            (f"{_NAME} >'a #' {_TOK}", _RULE_MINT),
            (f"{_NAME} 'a #' {_TOK}", _RULE_MINT),
            (f"{_K}all 'a #' {_NAME}", _RULE_KILL),
            # a QUOTED grammar word is an argument to bash, so the substitution's
            # ``)`` is its closer and the operand behind it is this command's
            # (R17 GPT: de-quoted ``case`` armed a pattern, ``)`` ended the pattern,
            # the self host was never read).  Every quoting spelling, every floor.
            ('ssh $("case" x in y 2>/dev/null) localhost', _RULE_SSH),
            ("ssh $('case' x in y 2>/dev/null) localhost", _RULE_SSH),
            ("ssh $(\\case x in y) localhost", _RULE_SSH),
            ("ssh $($'case' x in y) localhost", _RULE_SSH),
            # ...and the ANSI-C body compared DECODED (R20 GPT: ``$'\\x63ase'`` is ``case``)
            ("ssh $($'\\x63ase' x in y 2>/dev/null) localhost", _RULE_SSH),
            ("ssh $($'ca\\x73e' x in y) localhost", _RULE_SSH),
            ("ssh $($'\\143ase' x in y) localhost", _RULE_SSH),
            (f"{_NAME} $($'\\x63ase' x in y) {_RESTART}", _RULE_RESTART),
            ('ssh $(ca"se" x in y) localhost', _RULE_SSH),
            ("ssh $(c'a'se x in y) localhost", _RULE_SSH),
            ('ssh $("time" case x in y) localhost', _RULE_SSH),
            ('ssh $(x; "case" x in y) localhost', _RULE_SSH),
            ('ssh $(case x "in" y) localhost', _RULE_SSH),
            ('ssh $("if" case x in y) localhost', _RULE_SSH),
            ('scp $("case" x in y) file localhost:/tmp/', _RULE_SSH),
            (f'{_NAME} $("case" x in y) {_RESTART}', _RULE_RESTART),
            (f"{_NAME} $('case' x in y) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(\\case x in y) {_RESTART}", _RULE_RESTART),
            (f'{_PK} -f $("case" x in y) {_NAME}', _RULE_KILL),
            (f'{_K} $("case" x in y) $(pgrep -f {_NAME})', _RULE_KILL),
            # ...but a substitution's output may be EMPTY, so the word behind a
            # substitution destination is still checked for a LITERAL self spelling
            # (not resolved): ``$(true)`` is nothing and bash dials ``localhost``
            ("ssh $(case x in x) :;; esac; echo) localhost", _RULE_SSH),
            ("ssh $(cat hosts.txt) localhost", _RULE_SSH),
            ("ssh $(cat hosts.txt) 127.0.0.1", _RULE_SSH),
            ("ssh $(get-host) localhost", _RULE_SSH),
            ("ssh $(get-host) -p 2222 localhost", _RULE_SSH),
            # the RSYNC_RSH prefix still reaches an rsync in the same simple command
            ("RSYNC_RSH='ssh localhost' rsync -a src dst", _RULE_SSH),
            # the host glued behind the closer of a substitution whose output is not
            # static is the destination when that output is empty (R18 GPT: the
            # token closing ``esac)`` was skipped as grammar and ``localhost`` behind
            # it never read; the one-token ``$(true)localhost`` read only as a whole)
            ("ssh $(case x in x) :;; esac)localhost", _RULE_SSH),
            ("ssh -v $(case x in x) :;; esac)localhost uptime", _RULE_SSH),
            ("ssh $(case x in x) :;; esac)127.0.0.1", _RULE_SSH),
            ("ssh user@$(case x in x|y) :;; esac)localhost", _RULE_SSH),
            ("ssh $(true)localhost", _RULE_SSH),
            ("ssh `true`localhost", _RULE_SSH),
            ("ssh user@$(true)localhost", _RULE_SSH),
            ("ssh $(true)local$(true)host", _RULE_SSH),
            ("ssh -C $(true)localhost uptime", _RULE_SSH),
            ("sftp $(true)localhost", _RULE_SSH),
            ("scp file $(true)localhost:/tmp/", _RULE_SSH),
            ("scp $(case x in x) :;; esac)localhost:/x /tmp/", _RULE_SSH),
            ("rsync -a . $(case x in x) :;; esac)localhost:/tmp/", _RULE_SSH),
            # the value of an option that consumes one is checked the same way
            ("ssh -o $(true)localhost build-host", _RULE_SSH),
            # a QUOTED backtick is text to bash: it opens no substitution, so the
            # destination behind it is read (R18 Opus: the frame it opened never closed
            # and ``localhost`` was never checked; denied on the base)
            ("ssh -l 'a`b' localhost", _RULE_SSH),
            ("ssh -l a\\`b localhost", _RULE_SSH),
            ("ssh -o 'x`' localhost", _RULE_SSH),
            ("ssh -l 'a`b' localhost uptime", _RULE_SSH),
            ("sftp -P 'a`' localhost", _RULE_SSH),
            # ...an ANSI-C escape that DECODES to a backtick is that same text (R21 GPT)
            ("ssh -l $'\\x60' localhost", _RULE_SSH),
            ("ssh -l $'a\\140b' localhost", _RULE_SSH),
            ("ssh -l $'\\u0060' localhost", _RULE_SSH),
            # a QUOTED blank inside a case WORD or PATTERN is that word's text, so the
            # compound is still read and the verb behind its closer is this argv's
            # (R22 GPT: the walker split ``x y`` and the mint verb went through)
            (f'{_NAME} $(case "x y" in "x y") :;; esac; echo status) {_TOK}', _RULE_MINT),
            (f'{_NAME} $(case "x y" in x) :;; esac; echo status) {_TOK}', _RULE_MINT),
            (f'{_NAME} $(case "x y" in "x y") :;; esac){_RESTART}', _RULE_RESTART),
            (f"{_K}all $(case 'a b' in 'a b') :;; esac) {_NAME}", _RULE_KILL),
            ('ssh $(case "a b" in "a b") :;; esac; echo) localhost', _RULE_SSH),
            # an ENCLOSING clause's ``;;`` fools the ``esac)`` lookahead into the pattern
            # reading; the closer reading's word (glued, or the next token) is checked
            # as well -- the deny direction (R22 Opus; allowed on the base)
            ("case a in a) ssh $(case x in x) :;; esac)localhost ;; esac", _RULE_SSH),
            ("case a in a) ssh $(case x in x) :;; esac) localhost ;; esac", _RULE_SSH),
            ("case a in a) scp f $(case x in x) :;; esac) localhost:/x ;; esac", _RULE_SSH),
            (f"case a in a) {_NAME} $(case x in x) :;; esac){_RESTART} ;; esac", _RULE_RESTART),
            (f"case a in a) {_NAME} $(case x in x) :;; esac) {_TOK} ;; esac", _RULE_MINT),
            # a value a resolver splices in is never a reserved word to bash (R22 Opus)
            ("a=case; ssh $($a x in y) localhost", _RULE_SSH),
            ("a=case ssh $(${a} x in y) localhost", _RULE_SSH),
            ("a=in; ssh $(case x $a y) localhost", _RULE_SSH),
            # ...through every expansion spelling the resolver reads (R23 Opus)
            ("a=case; ssh $(${a:0} x in y) localhost", _RULE_SSH),
            ("a=case; p=a; ssh $(${!p} x in y) localhost", _RULE_SSH),
            (f"a=case; {_NAME} $($a x in y) {_RESTART}", _RULE_RESTART),
            # ...and a FRAGMENT the text around the expansion completes (R30 Opus)
            ("a=cas; ssh $(: ; ${a}e x in y) localhost", _RULE_SSH),
            ("a=ca; b=se; ssh $(: ; ${a}${b} x in y) localhost", _RULE_SSH),
            (f"a=cas; {_NAME} $(${{a}}e x in y) {_RESTART}", _RULE_RESTART),
            # a blank after a fresh pattern's ``esac`` still makes it the reserved word
            # (R30 Opus: ``esac )`` closes the case, so the closer's glued verb is read)
            (f'{_NAME} "$(case y in y) :;; esac )"{_TOK} create', _RULE_MINT),
            (f'{_NAME} "$(case y in y) :;; esac\t)"{_TOK} create', _RULE_MINT),
            ("ssh $(case x in x) :;; esac )localhost", _RULE_SSH),
            # ...while a QUOTED blank behind ``esac`` is the pattern's own text (R31 GPT)
            (f"{_NAME} $(case x in 'esac ') :;; esac; :) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(case x in 'esac\t') :;; esac; :) {_TOK}", _RULE_MINT),
            ("ssh $(case x in 'esac ') :;; esac; :) localhost", _RULE_SSH),
            # a mark byte TYPED into the command is dropped at the gate's entry, never
            # read back as the separator it stands for (R31 Opus)
            ("ssh -i /tmp/k\x10 localhost", _RULE_SSH),
            ("ssh -i /tmp/k\x08 localhost", _RULE_SSH),
            ("ssh -i /tmp/k\x0b\x0c\x1d\x1f localhost", _RULE_SSH),
            (f"{_NAME} $(x '${{' ) {_RESTART} '}}' $(:)", _RULE_RESTART),
            # an ANSI-C body inside a quoted substitution keeps the quote state in step,
            # so the command after it is read as its own words (R32 Opus)
            ("echo \"$(: $'a\\')b\\'c' )\"; ssh -v localhost", _RULE_SSH),
            (f"echo \"$(: $'a\\')b\\'c' )\"; {_NAME} {_TOK}", _RULE_MINT),
            # a ``#`` word inside a substitution opens a comment to the newline, so a
            # ``)`` in it closes nothing and the real closer is the one bash reads
            # (R35 GPT: the commented ``)`` popped the frame and the verb was never a word)
            (f"{_NAME} $(case x in x) :;; esac; # )\n echo) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(: #)\n echo) {_TOK}", _RULE_MINT),
            ("ssh $(: # )\n echo) localhost", _RULE_SSH),
            (f"x $(: # )\n echo); {_NAME} {_RESTART}", _RULE_RESTART),
            # ...and a ``;`` typed INSIDE the comment is comment text too, not the newline
            # sentinel: the comment is dropped from the raw text, where the newline still
            # stands (R37 GPT: the ``;`` cleared the walker's comment state early)
            (f"{_NAME} $(echo # ; )\n :) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(echo # ; ; )\n :) {_TOK}", _RULE_MINT),
            ("x $(echo # ; )\n :); ssh localhost", _RULE_SSH),
            # a ``#`` glued behind a substitution's closer is a word bash hands over
            (f"{_NAME} $(:)# {_TOK}", _RULE_MINT),
            # ...and a ``#`` glued behind a CLOSING backtick is a word too: only the opening
            # backtick starts a command list (R38 GPT: ``\`true\`#x; …`` dropped the second command)
            (f"echo `true`#x; {_NAME} {_RESTART}", _RULE_RESTART),
            (
                f'echo `true`#x; {_NAME[:4]}""{_NAME[4:]} {_RESTART[:2]}""{_RESTART[2:]}',
                _RULE_RESTART,
            ),
            (f"{_NAME} `echo #x\n` {_TOK}", _RULE_MINT),
            # an option word glued behind a closer is read as its DETACHED spelling is,
            # including rsync's remote shell and ssh's forward specs (R37 Opus)
            ("rsync -a $(true)-e 'ssh localhost' src/ dst/", _RULE_SSH),
            ("rsync -a $(true)-ave 'ssh localhost' src/ dst/", _RULE_SSH),
            ("rsync -a $(true)--rsh='ssh localhost' src/ dst/", _RULE_SSH),
            ("rsync -a $(true)--rsh 'ssh localhost' src/ dst/", _RULE_SSH),
            ("ssh $(true)-R 2222:localhost:22 remote.example.com", _RULE_SSH),
            # ...and the ATTACHED spellings run the detached branch's in-token checks
            # (R38 Opus: ``-e'ssh localhost'`` / ``-R2222:localhost:22`` glued behind a closer)
            ("rsync $(true)-e'ssh localhost' /tmp/a remote.example.com:/b", _RULE_SSH),
            ("rsync $(true)-ae'ssh localhost' /tmp/a remote.example.com:/b", _RULE_SSH),
            ("ssh $(true)-R2222:localhost:22 remote.example.com", _RULE_SSH),
            ("ssh $(true)-vR2222:localhost:22 remote.example.com", _RULE_SSH),
            # an option VALUE behind a glued ``-o`` keeps its quoted backtick, so the
            # ProxyCommand hint names this host (R30 Opus; the unglued row denies on base)
            (
                "ssh $(case x in x) :;; esac)-o 'proxycommand=nc `hostname` 22' far.example.com",
                _RULE_SSH,
            ),
            ("ssh -o 'proxycommand=nc `hostname` 22' far.example.com", _RULE_SSH),
            (f"a=esac; {_NAME} $(case x in x) :;; $a) {_TOK}", _RULE_MINT),
            # quoting RESTARTS inside a substitution: the single quotes in ``"$('case' …)"``
            # are real, so the word is an argument and the destination is the option's
            # value; and a QUOTED ``>``/``<`` is text, not a redirection whose target
            # swallows the destination (R23 GPT; both allowed on the base)
            ("ssh -l \"$('case' x in y 2>/dev/null; echo root)\" localhost", _RULE_SSH),
            ('ssh -l "x 2>/dev/null; echo root" localhost', _RULE_SSH),
            ('ssh -l "a>b" localhost', _RULE_SSH),
            ("ssh -l 'a<b' localhost", _RULE_SSH),
            ("ssh \\>localhost", _RULE_SSH),
            (f'{_NAME} "a>b" {_TOK}', _RULE_MINT),
            (f"{_K}all 'a>b' {_NAME}", _RULE_KILL),
            # an OPTION word glued behind a substitution's closer is an option when the
            # output is empty: its value is not the destination (R24 GPT; allowed on base)
            ("ssh $(case x in x) :;; esac)-p 22 localhost", _RULE_SSH),
            ("ssh $(true)-p 22 localhost", _RULE_SSH),
            ("ssh $(x)-l root localhost", _RULE_SSH),
            ("ssh $(case x in x) :;; esac)-o BatchMode=yes localhost", _RULE_SSH),
            ("scp $(x)-P 22 f localhost:/x", _RULE_SSH),
            # ...while a quote that ENCLOSES the substitution keeps its blanks as that
            # command list's separators (one token, R11)
            (f'{_NAME} "$(case y in y) :;; esac)" {_TOK}', _RULE_MINT),
            # a NESTED quote inside a quoted substitution does not end the enclosing
            # quote: the closer is found by a quote-aware scan, so the operands behind
            # ``"$(dirname "$0")/a"`` stay separate words (R25 Opus; denied on base)
            ('scp "$(dirname "$0")/a" b localhost:/tmp/', _RULE_SSH),
            (f'{_NAME} --profile "$(dirname "$0")/p" foo {_TOK}', _RULE_MINT),
            (f'x "$(dirname "$0")/a"; {_NAME} {_RESTART}', _RULE_RESTART),
            # ...and an ARGUMENT spelled ``case`` inside that substitution counts for
            # nothing: only a command-position ``case`` defers the closer (R26 GPT)
            (f'{_NAME} -v"$(echo case>/dev/null; case x in x) ;; esac)" {_RESTART}', _RULE_RESTART),
            (f'{_NAME} -v"$(echo case; case x in x) ;; esac)" {_RESTART}', _RULE_RESTART),
            (f'{_NAME} "$(echo case)" {_TOK}', _RULE_MINT),
            (f'{_NAME} "$(printf case; case x in x) :;; esac)" {_TOK}', _RULE_MINT),  # R26 Opus
            # ...while a keeper or an option word before ``case`` HOLDS command position
            # (R28 GPT: ``time -p case`` is a case to the walker and to the closer scan)
            (f'{_NAME} "$(time -p case x in x) :;; esac)" {_TOK}', _RULE_MINT),
            (f'{_NAME} -v"$(if case x in x) ;; esac; then :; fi)" {_RESTART}', _RULE_RESTART),
            ('ssh -l "$(time case x in x) :;; esac)" localhost', _RULE_SSH),
            # ...and the NAME after ``function``/``coproc`` hands it on (R29 GPT: bash
            # accepts ``function f case x in x) …`` and spans past the pattern's ``)``)
            (f'{_NAME} "$(function f case x in x) :;; esac; :)"{_TOK}', _RULE_MINT),
            (f'{_NAME} "$(coproc c case x in x) :;; esac; :)"{_TOK}', _RULE_MINT),
            ('ssh -l "$(function f case x in x) :;; esac)" localhost', _RULE_SSH),
            ('ssh -l "$(echo case)" localhost', _RULE_SSH),
            # ...and a quoted TAB or NEWLINE is that word's text as well (R25 GPT)
            (f'{_NAME} $(case "x\ty" in "x\ty") :;; esac; echo status) {_TOK}', _RULE_MINT),
            (f"{_NAME} $(case 'x\ny' in 'x\ny') :;; esac; echo status) {_TOK}", _RULE_MINT),
            ("ssh -l 'a`b' -p 'c`d' localhost", _RULE_SSH),
            (f"{_NAME} -v 'a`b' {_TOK}", _RULE_MINT),
            (f"{_K}all 'a`' {_NAME}", _RULE_KILL),
        ],
    )
    def test_denied(self, cmd: str, rule: str) -> None:
        assert _rule(cmd) == rule


class TestAnUnrelatedLaterCommandIsNotAttributed:
    """False-positive controls, one per consumer: the case ends, the argv ends."""

    @pytest.mark.parametrize(
        "cmd",
        [
            # mint-verb scan
            f"{_NAME} status $(case x in a) echo b;; esac); echo {_TOK}",
            f"{_NAME} status $(case x in a|b) echo c;; esac); echo {_TOK}",
            # pkill pattern leg
            f"{_PK} -f $(" + _TAIL.format(cmd="echo other") + f"); echo {_NAME}",
            f"{_PK} -f other; " + _TAIL.format(cmd=f"echo {_NAME}"),
            # bare kill body window
            f"{_K} $(" + _TAIL.format(cmd="pgrep -f other") + f"); echo {_NAME}",
            f"{_K} 123; " + _TAIL.format(cmd=f"echo {_NAME}"),
            f"{_K} $(case x in a) :;; esac>/dev/null); echo {_NAME}",
            # ``esac`` after a newline still ends the compound, so the window ends
            f"{_K} $(case x in x) :;;\nesac); echo {_NAME}",
            f"{_K} $(case x in x) :;;\nesac; pgrep -f other); echo {_NAME}",
            f"{_K} $(case x\nin x) :;; esac; pgrep -f other); echo {_NAME}",
            # the EMPTY pattern (``'')``) terminates: the window ends at the ``;`` after esac
            f"{_PK} -f $(case $x in '') echo foo;; esac); cd ~/{_NAME}",
            "rsync -e ssh $(case $x in '') echo src;; esac) remotebox:/x; echo 127.0.0.1",
            # an option-shaped pattern is fed to the walker even though the operand
            # scan skips it, so the pattern closes and the window ends at ``esac);``
            f"{_NAME} status $(case $1 in -c) echo hi;; esac); grep {_TOK} log",
            f"{_NAME} status $(case $1 in -c) echo hi;; esac) restart",
            # ``esac)`` closing both the case and the substitution stays a closer
            f"{_K} $(case x in x) :;; esac); :;; echo {_NAME}",
            f"{_K} $(pgrep -f x;case y in y) :;; esac); echo {_NAME}",
            f"{_K} $(case x in x) :;; esac;case y in y) :;; esac); echo {_NAME}",
            # a quoted, never-closed ``${`` inside the substitution (scope corpus row)
            f"{_K} $(pgrep -f 'sync-${{ENV'); {_NAME} status",
            f'{_K} $(pgrep -f "sync-${{ENV"); {_NAME} status',
            f"{_K} $(grep -c '${{' conf.tpl); echo {_NAME}",
            f"{_K} $(case x in x) :;;y) :;; esac); echo {_NAME}",
            f"{_K} $(case x in(x) :;; esac); echo {_NAME}",
            # an expansion's own ``)`` does not keep the window open past the ``;``
            f"{_K} ${{v:-x)}}; echo {_NAME}",
            f"{_K} ${{PIDS:-$(pgrep x)}}; echo {_NAME}",
            f"{_K} ${{PIDS:-$(pgrep x; echo 1)}}; echo {_NAME}",
            # a split expansion is carried to its ``}`` and no further
            f"{_K} ${{v:-a b)}}; echo {_NAME}",
            f"{_K} $(: ${{v:-a b}}); echo {_NAME}",
            f"{_NAME} ${{v:-a b)}} status; echo {_TOK}",
            f"{_PK} -f other ${{v:-a b}}; echo {_NAME}",
            # a balanced process substitution closes itself; the argv still ends
            f"{_K} $(pgrep nginx) <(echo); echo {_NAME}",
            f"diff <(ls) <(ls -a); echo {_NAME} {_TOK}",
            # product CLI operands: the leading operand decides, a case body in
            # the argv does not make ``restart`` leading
            f"{_NAME} status $(case x in a) echo b;; esac) restart",
            # rsync environment walk: a pattern alternation is not a separator
            # and the selector never crosses the compound
            "case $x in a|b) rsync -e ssh x y;; esac; echo localhost",
            "case $x in a|b) rsync x remotebox:/y;; esac; echo 127.0.0.1",
            # a quoted ``'${'`` literal at top level and a ``$case`` parameter end
            # the argv where the counter did (R10 walked on past the ``;``)
            f"{_PK} -f '${{a' ; echo {_NAME}",
            f"{_K} '${{' 1; echo {_NAME}",
            f"{_PK} -f $case ; echo {_NAME}",
            # bracket patterns: the window still ends at the ``)`` after the ``]``
            f"{_NAME} status $(case x in [a-z]) echo a;; esac); echo {_TOK}",
            f"{_NAME} status $(case x in x[\\)]) echo a;; esac); echo {_TOK}",
            f"{_NAME} status $(case x in [$(echo ])]) echo a;; esac); echo {_TOK}",
            f"{_NAME} status $(case x in x[) echo a;; esac); echo {_TOK}",
            f"{_PK} -f $(case x in [a-z\\)]) :;; esac; echo other); echo {_NAME}",
            f"case $x in [a-z\\)]) echo a;; esac; {_NAME} status",
            # a ``}`` with no ``)`` after it leaves the ``'${'`` literal quoted (R12 denied)
            f"{_K} $(grep -c '${{' x); awk '{{print}}' y; cd ~/{_NAME}",
            # 500 glued clauses in one token (R12 raised ``RecursionError`` out of the gate)
            f"{_K} $(case x in " + "x);;" * 500 + "esac)",
            # ssh-family argument walk
            "ssh host $(" + _TAIL.format(cmd="echo hi") + "); echo localhost",
            "rsync -e ssh $(" + _TAIL.format(cmd="echo src") + ") remotebox:/x; echo 127.0.0.1",
            "scp $(" + _TAIL.format(cmd="echo f") + ") remotebox:/x; echo localhost",
            # a glob PATTERN is grammar, not a host operand (``*`` matches every self
            # name; the scope corpus rows R13 newly refused)
            'rsync -e ssh $(case "$c" in y) echo -z;; *) echo -v;; esac) ./src backup-host:/srv',
            'scp $(case "$f" in tar) echo out.tar;; *) echo out.txt;; esac) deploy-host:/incoming',
            "scp $(case x in *) echo out.txt;; esac) deploy-host:/incoming",
            "scp $(case localhost in x) echo a;; esac) remotebox:/x",
            "scp $(case x in a) echo a;; localhost) echo b;; esac) remotebox:/x",
            # an opaque destination keeps the slot open, and the remote command is not a host
            "ssh $(get-host) uptime",
            "ssh $(case $e in p) echo bastion-a;; *) echo bastion-b;; esac) uptime",
            "ssh -p 22 $(get-host) 'echo hi'",
            "ssh remotebox $(cmd) localhost",
            # a word INSIDE the substitution is that command's argv, not the host slot
            # (R14 Opus: ``hosts.txt`` resolved as the destination, fail-closed)
            "ssh $(cat hosts.txt | head -1) uptime",
            "ssh $(cat hosts.txt) uptime",
            # an expansion inside an expansion closes where bash closes it (R14 Opus:
            # the inner's open ``$(`` count was doubled into the outer, never closed)
            f"{_K} ${{PIDS:-$(pgrep -f ${{SVC}})}}; echo {_NAME}",
            # a case inside a pattern substitution still lets the outer one close
            f"{_K} $(case x in $(case y in y) :;; esac)) :;; esac); echo {_NAME}",
            # a redirect word's COMPLETE substitution and a quoted target's separator
            # are data: the window still ends at the real separator
            f"{_NAME} status 2>$(mktemp); echo {_TOK}",
            f"{_NAME} > 'a;b' status; echo {_TOK}",
            f"{_NAME} 2>$(mktemp) status",
            # a keeper in argument position: ``time case x in y`` is the remote command
            "ssh remotebox time case x in y",
            # a self name inside an ssh substitution body only EXCLUDES it: the body
            # is that command's argv, not an operand (R16 scope rows, allowed on base)
            "ssh $(grep -v localhost /etc/hosts | awk 'NR==1{print $2}') uptime",
            "ssh $(cat hosts.txt | grep -v 127.0.0.1 | head -1) uptime",
            "ssh $(findstr /V localhost hosts.txt) uptime",
            # ``esac)`` as the closer: a word, then the window ends at the ``;`` -- the
            # rest reads as valid bash under the closer reading (R16)
            f"{_NAME} $(case x in x) :;; esac) status; echo {_TOK}",
            f"{_K}all $(case x in x) :;; esac) nginx; echo {_NAME}",
            f"{_K} $(case x in x) :;; esac)\n echo {_NAME}",
            f"{_K} $(case x in x) :;; esac) 1; echo {_NAME} $(date)",
            # a ``#`` that STARTS the token is a comment (as on base)
            f"{_NAME} '#' {_TOK}",
            # a quoted grammar word as an OPERAND compares as its text (R17): a
            # mint-free CLI operand, a kill of another name
            f'{_NAME} "case" status',
            f'{_NAME} $("case" x in y) status',
            f'{_K}all "case" nginx',
            # a glued word that names another host, or no host at all (R18)
            "ssh $(case x in x) :;; esac)build-host uptime",
            "ssh $(true)build-host",
            "ssh $(echo x)y",
            "scp file $(true)build-host:/tmp/",
            "ssh build-host $(true)localhost",
            "ssh -l $(true)localhost build-host",
            "ssh -p $(true)2222 build-host",
            "ssh $(case x in x) :;; esac)",
            # a QUOTED substitution in the destination slot IS the destination: bash
            # hands ssh one word even when the output is empty, so the next word is
            # the remote command, not a host to resolve (R19 Opus).  The UNQUOTED
            # spellings (``ssh $(get-host) deploy.sh``) take the slot softly and the
            # dotted word behind them is the declared fail-closed class (R41 GPT) --
            # see ``TestAnUnresolvedDottedHostIsTheBaseRefusal``.
            'ssh "$(cat target)" python3.12 -m tool',
            'ssh "$(get-host)" deploy.sh',
            # the ssh floor's outer walker reads every token, so a ``case`` inside an
            # earlier command's substitution does not hold its frame open across the
            # ``;`` that ends an RSYNC_RSH prefix (R19 Opus; allowed on the base)
            "RSYNC_RSH='ssh localhost' echo $(case q in esac) ; rsync -a src dst",
            "RSYNC_RSH='ssh localhost' echo $(case q in q) :;; esac) ; rsync -a src dst",
            # ...and the value names another host, or the destination does
            "ssh $(x)-p 22 build-host",
            "ssh $(x)-l root build-host",
            # a QUOTED backtick in the remote command is text, not a ``hostname``
            # substitution the self-host hints read (R26 scope row; allowed on base)
            "ssh $(get-host) 'echo `hostname`'",
            "ssh build-host 'echo `hostname`'",
            # a quoted ``>`` in the destination or the remote command is text
            'ssh "a>b;c" localhost',
            'ssh build-host "cat > /tmp/x"',
            "ssh 2>/dev/null build-host",
            # the closer reading's extra word names another host: nothing to deny
            "case a in a) ssh $(case x in x) :;; esac) build-host ;; esac",
            # a quoted backtick names no self host and mints nothing
            "ssh -l 'a`b' build-host",
            "ssh build-host 'echo `hostname`'",
            f"{_NAME} 'a`b' status",
            # a quoted ``;`` in an operand is text of that operand, not a substitution
            # mark to strip: ``'local;host'`` is not this host (R18; allowed on the base)
            "ssh 'local;host'",
            "ssh 'local;host' uptime",
            # a quoted blank inside a top-level token is text of ONE word: the remote
            # payload is one argv word to ssh, never the host (R17 Security Scope row)
            "ssh $(vault read -field=host secret/prod/db) 'psql -h localhost -c \"select 1\"'",
            "ssh $(vault read -field=host x) 'echo localhost'",
            "ssh -p $(true)2222 'psql -h localhost'",
            "ssh $(true)'psql -h localhost'",
            f"{_NAME} '{_RESTART} now'",
            # the UNQUOTED spelling keeps the compound reading (as on the base): the
            # pattern's ``)`` is grammar and ``localhost`` sits in the clause body of
            # a command bash refuses without ``esac``
            "ssh $(case x in y) localhost",
        ],
    )
    def test_allowed(self, cmd: str) -> None:
        assert _rule(cmd) is None


class TestAClauseTerminatorAfterEsacStaysOpen:
    """DOCUMENTED LIMIT, fail-closed.  ``esac);;`` (glued or not) is token-identical
    for a quoted ``'esac')`` pattern with an empty body and for a closed case whose
    ``$(`` ends inside an ENCLOSING clause the window never saw; bash refuses ``;;``
    after a closed case otherwise.  The walker keeps the window open (deny direction);
    R13 read the glued spelling as a closer and the quoted pattern minted (GPT)."""

    @pytest.mark.parametrize("gap", ["", " "])
    def test_the_enclosing_clause_shape_is_over_scanned(self, gap: str) -> None:
        cmd = f"case a in a) {_K} $(case x in x) :;; esac){gap};; esac; echo {_NAME}"
        assert _rule(cmd) == _RULE_KILL
        assert _rule(cmd.replace(f"echo {_NAME}", "echo other")) is None


class TestAnUnresolvedDottedHostIsTheBaseRefusal:
    """Security Scope rows R17: ``scp $(case … esac) release@deploy.example.com:/…``
    and ``rsync -e ssh $(case … esac) ./src backup.example.com:/srv`` are allowed on
    the base and refused here.  The refusal is the destination's, not the case's:
    a DOTTED name is refused on first contact until the resolver classifies it as
    remote (``_resolved_host_verdict``, fail-closed), and the base refuses the very
    same rows once the ``case`` is taken out -- it allowed the case spelling only
    because its window closed at the pattern's ``)`` and never read the target.
    Mapping: with the host classified remote the rows are allowed; unresolved,
    they carry the base's own verdict for the case-free sibling.

    The same class holds the dotted word behind an UNQUOTED substitution destination
    (``ssh $(cat hosts.txt) deploy.sh``): the substitution's output may be empty, and
    then ``deploy.sh`` is the host bash dials -- so it is resolved as ``ssh deploy.sh``
    is, hosts-file and DNS layers included (R41 GPT: reading it for a literal self
    spelling only let a loopback alias known to DNS alone through).  R20 Opus had
    asked for the allowance; the conductor settled the two readings for the deny."""

    _ROWS = [
        (
            'scp $(case "$FMT" in tar) echo out.tar;; *) echo out.txt;; esac) '
            "release@deploy.example.com:/incoming/",
            "scp out.tar release@deploy.example.com:/incoming/",
        ),
        (
            'rsync -e ssh $(case "$SPEED" in fast) echo -z;; *) echo -v;; esac) '
            "./src backup.example.com:/srv",
            "rsync -e ssh -v ./src backup.example.com:/srv",
        ),
        (
            'sftp $(case "$ENV" in prod) echo -b jobs.txt;; *) echo -v;; esac) '
            "release@files.example.com",
            "sftp -v release@files.example.com",
        ),
        # the word behind an opaque destination substitution (R41 GPT)
        ("ssh $(cat hosts.txt) deploy.sh", "ssh deploy.sh"),
        ("ssh $(get-host) deploy.sh", "ssh deploy.sh"),
        ("ssh `get-host` run.sh", "ssh run.sh"),
        ("ssh $(get-host --env prod) deploy.sh", "ssh deploy.sh"),
        ("ssh -p 22 $(cat hosts.txt) deploy.sh", "ssh -p 22 deploy.sh"),
        ("ssh $(case x in x) :;; esac) deploy.sh", "ssh deploy.sh"),
        ("ssh $(cat hosts.txt) deploy.sh localhost", "ssh deploy.sh localhost"),
        ("sftp $(get-host) remote.file", "sftp remote.file"),
        ("ssh $(true) self.example", "ssh self.example"),
        # ...and behind rsync's ``-e ssh`` remote shell, which the floor walks as ssh
        (
            "rsync -e ssh $(true) backup.tar remotebox:/srv",
            "rsync -e ssh backup.tar remotebox:/srv",
        ),
    ]

    @pytest.fixture(autouse=True)
    def _own_host_is_pinned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        own = argv_floor.socket.gethostname().strip().lower()
        pinned = frozenset(name for name in {own, own.split(".", 1)[0]} if name)
        monkeypatch.setattr(argv_floor, "_OWN_HOST_NAMES_CACHE", pinned)
        monkeypatch.setattr(argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
        monkeypatch.setattr(argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)

    @pytest.mark.parametrize(("row", "_sibling"), _ROWS)
    def test_a_remote_destination_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch, row: str, _sibling: str
    ) -> None:
        monkeypatch.setattr(argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)
        assert _rule(row) is None

    @pytest.mark.parametrize(("row", "sibling"), _ROWS)
    def test_an_unresolved_destination_carries_the_base_verdict(
        self, monkeypatch: pytest.MonkeyPatch, row: str, sibling: str
    ) -> None:
        # A miss answers *fail_closed* (True for a dotted name) without resolving.
        monkeypatch.setattr(
            argv_floor,
            "_resolved_host_verdict",
            lambda host, **kw: kw.get("fail_closed", True),
        )
        assert _rule(sibling) == _RULE_SSH  # the base's verdict for the case-free row
        assert _rule(row) == _RULE_SSH


class TestAnEmptyDestinationSubstitutionHandsTheSlotToTheNextWord:
    """R41 GPT (F1): ``ssh $(true) self.example`` -- the destination substitution may
    print nothing, and then ``self.example`` is the host bash dials.  The word behind
    an opaque destination was checked for a LITERAL self spelling only (R20 Opus), so
    a loopback alias that exists only in the hosts file or in DNS opened an ssh
    channel into this host with no later layer behind it.  The word is now read as
    any destination is: the hosts file answers same-call, a dotted name fails closed
    until the resolver classifies it, and a dotless name absent from the hosts file
    answers open while DNS revalidates (``ssh $(get-host) uptime`` stays allowed)."""

    @pytest.fixture(autouse=True)
    def _own_host_is_pinned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        own = argv_floor.socket.gethostname().strip().lower()
        pinned = frozenset(name for name in {own, own.split(".", 1)[0]} if name)
        monkeypatch.setattr(argv_floor, "_OWN_HOST_NAMES_CACHE", pinned)
        monkeypatch.setattr(argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
        monkeypatch.setattr(argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)

    @pytest.mark.parametrize(
        "cmd",
        [
            "ssh $(true) self.example",
            "ssh $(cat hosts.txt) self.example",
            "ssh $(case x in x) :;; esac) self.example",
            "ssh -p 22 $(true) self.example uptime",
            "sftp $(true) self.example",
        ],
    )
    def test_a_dns_loopback_alias_behind_an_empty_substitution_is_denied(
        self, monkeypatch: pytest.MonkeyPatch, cmd: str
    ) -> None:
        asked: list[str] = []

        def alias_is_this_host(host: str, **_kw: object) -> bool:
            asked.append(host)
            return True  # the resolver classified the alias as loopback/local

        monkeypatch.setattr(argv_floor, "_resolved_host_verdict", alias_is_this_host)
        assert _rule(cmd) == _RULE_SSH
        assert "self.example" in asked  # the DNS layer WAS consulted for the word

    def test_a_hosts_file_loopback_alias_behind_an_empty_substitution_is_denied(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
    ) -> None:
        # the round-18 vector itself: a dotless alias the hosts file maps to loopback
        hosts = tmp_path / "hosts"  # type: ignore[operator]
        hosts.write_text("127.0.0.1 localhost\n127.0.0.1 devbox\n")
        monkeypatch.setattr(argv_floor, "_hosts_file_paths", lambda: (str(hosts),))
        monkeypatch.setattr(argv_floor, "_HOSTS_FILE_CACHE", {})
        monkeypatch.setattr(
            argv_floor, "_resolved_host_verdict", lambda host, **kw: kw.get("fail_closed", True)
        )
        assert _rule("ssh $(true) devbox") == _RULE_SSH
        assert _rule("ssh $(get-host) devbox uptime") == _RULE_SSH
        # the same hosts file does not know ``uptime``: open, as ``ssh uptime`` is
        assert _rule("ssh $(get-host) uptime") is None

    @pytest.mark.parametrize(
        "cmd",
        [
            "ssh $(true) self.example",
            "ssh $(get-host) deploy.sh",
            "ssh $(get-host) uptime",
            "ssh $(cat hosts.txt) deploy.sh localhost",
        ],
    )
    def test_a_remote_word_behind_the_substitution_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch, cmd: str
    ) -> None:
        monkeypatch.setattr(argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)
        monkeypatch.setattr(argv_floor, "_hosts_file_verdict", lambda host: None)
        assert _rule(cmd) is None


class TestACaseAlternationPrintingASelfHostIsThatHost:
    """R44 GPT: ``ssh $(case x in x) echo localhost;; esac) uptime`` -- the substitution
    PRINTS ``localhost``, so bash dials this host, yet the walker read the body words as
    the inner command's argv and the flat echo/printf splice never saw a compound.  A
    ``case`` whose every clause prints a literal is a static alternation: the branch
    naming this host is spliced in, as ``$(echo localhost)`` is -- deny direction, like
    brace expansion (``quoted_marks.splice_case_alternations``).  A compound with a
    dynamic branch, a fall-through, or no self-naming branch is left as written: the
    general run-time residual (``$(get-host)``) stays documented, not resolved."""

    @pytest.fixture(autouse=True)
    def _own_host_is_pinned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        own = argv_floor.socket.gethostname().strip().lower()
        pinned = frozenset(name for name in {own, own.split(".", 1)[0]} if name)
        monkeypatch.setattr(argv_floor, "_OWN_HOST_NAMES_CACHE", pinned)
        monkeypatch.setattr(argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
        monkeypatch.setattr(argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
        monkeypatch.setattr(argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)

    @pytest.mark.parametrize(
        "cmd",
        [
            "ssh $(case x in x) echo localhost;; esac) uptime",
            'ssh $(case "$h" in prod) echo build-host;; *) echo localhost;; esac) uptime',
            "ssh $(case x in (x) printf 127.0.0.1;; esac)",
            "ssh $(case x in x) echo 'localhost';; esac) uptime",
            "ssh `case x in x) echo localhost;; esac` uptime",
            "ssh user@$(case x in x) echo localhost;; esac)",
            "ssh -p 22 $(case x in a) echo ::1;; b) echo build-host;; esac) uptime",
            "scp file $(case x in x) echo localhost;; esac):/tmp/",
            "sftp $(case x in x) echo 127.0.0.1;; esac)",
        ],
    )
    def test_a_self_naming_branch_is_denied(self, cmd: str) -> None:
        assert _rule(cmd) == _RULE_SSH

    def test_the_payload_read_from_a_file_is_denied(self, tmp_path: pytest.TempPathFactory) -> None:
        # the finding's own shape, arriving as a file's text rather than a literal
        payload = tmp_path / "payload.sh"  # type: ignore[operator]
        payload.write_text("ssh $(case x in x) echo localhost;; esac) uptime\n")
        assert _rule(payload.read_text().strip()) == _RULE_SSH

    @pytest.mark.parametrize(
        "cmd",
        [
            # no branch names this host
            "ssh $(case x in x) echo build-host;; esac) uptime",
            "ssh $(case $e in p) echo bastion-a;; *) echo bastion-b;; esac) uptime",
            # a dynamic branch: the documented run-time residual, as on the base
            "ssh $(case x in x) echo $h;; esac) uptime",
            "ssh $(case x in x) echo $(get-host);; esac) uptime",
            "ssh $(case x in x) cat hosts.txt;; esac) uptime",
            # a fall-through runs two bodies: not a one-word alternation
            "ssh $(case x in x) echo localhost;& esac) uptime",
            # the spliced word lands where bash puts it: here the remote command
            "ssh build-host $(case x in x) echo localhost;; esac)",
            # the option-flag alternations of the scope rows print no host
            'rsync -e ssh $(case "$c" in y) echo -z;; *) echo -v;; esac) ./src backup-host:/srv',
        ],
    )
    def test_without_a_self_naming_literal_branch_nothing_changes(self, cmd: str) -> None:
        assert _rule(cmd) is None

    def test_the_parser_cuts_only_the_one_word_shape(self) -> None:
        from kiro_crew.security import quoted_marks

        assert quoted_marks.case_clause_bodies("x) echo a;; *) echo b") == ["echo a", "echo b"]
        assert quoted_marks.case_clause_bodies("(x) echo a;;") == ["echo a"]
        assert quoted_marks.case_clause_bodies("x) echo a;& *) echo b") is None
        assert quoted_marks.case_clause_bodies("x echo a") is None
        assert quoted_marks.case_clause_bodies("x) echo a) b") is None


class TestAGluedOptionTerminatorEndsTheOptions:
    """R44 Opus: ``ssh $(true)-- devbox`` -- the ``--`` the substitution's closer completes
    is the POSIX option terminator, but the glued-option reader classified it as a
    value-taking LONG option, so the next word was swallowed out of host position and a
    hosts-file or DNS loopback alias behind it dialed this host; the detached ``ssh --
    devbox`` was already denied.  A glued exact ``--`` now ends option parsing as the
    detached one does, in both glued branches (the ``case``-grammar one included)."""

    @pytest.fixture(autouse=True)
    def _own_host_is_pinned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        own = argv_floor.socket.gethostname().strip().lower()
        pinned = frozenset(name for name in {own, own.split(".", 1)[0]} if name)
        monkeypatch.setattr(argv_floor, "_OWN_HOST_NAMES_CACHE", pinned)
        monkeypatch.setattr(argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
        monkeypatch.setattr(argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)

    @pytest.mark.parametrize(
        "cmd",
        [
            "ssh -- devbox",
            "ssh $(true)-- devbox",
            "ssh $(get-host)-- devbox uptime",
            "ssh $(case x in x) :;; esac)-- devbox",
            "ssh `true`-- devbox",
        ],
    )
    def test_a_hosts_file_alias_behind_a_glued_terminator_is_denied(
        self, monkeypatch: pytest.MonkeyPatch, cmd: str
    ) -> None:
        monkeypatch.setattr(
            argv_floor, "_hosts_file_verdict", lambda host: host == "devbox" or None
        )
        monkeypatch.setattr(
            argv_floor, "_resolved_host_verdict", lambda host, **kw: kw.get("fail_closed", True)
        )
        assert _rule(cmd) == _RULE_SSH

    def test_a_dns_alias_behind_a_glued_terminator_is_denied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[str] = []

        def alias_is_this_host(host: str, **_kw: object) -> bool:
            asked.append(host)
            return True

        monkeypatch.setattr(argv_floor, "_resolved_host_verdict", alias_is_this_host)
        assert _rule("ssh $(true)-- self.example") == _RULE_SSH
        assert "self.example" in asked

    @pytest.mark.parametrize(
        "cmd",
        [
            "ssh $(true)-- build-host",
            "ssh $(true)-- build-host uptime",
            # after ``--`` the next word is the destination even when it looks like an option
            "ssh $(true)-- -p 22 build-host",
            "ssh $(true)--",
        ],
    )
    def test_a_remote_destination_behind_a_glued_terminator_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch, cmd: str
    ) -> None:
        monkeypatch.setattr(argv_floor, "_hosts_file_verdict", lambda host: None)
        monkeypatch.setattr(argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)
        assert _rule(cmd) is None


class TestACommentBehindAPatternCloserIsAComment:
    """R46 GPT: ``<name> $(case x in x)# )`` + newline + ``:;; esac; echo status) <tok>`` --
    behind a case PATTERN's ``)`` a ``#`` opens a comment (that ``)`` is an operator;
    measured: bash drops the rest of the line and runs ``<name> status <tok>``), but the
    comment scan knew only that a ``)`` closing a substitution ends no word, so the
    commented ``)`` was read as the substitution's closer and the window ended there.
    ``strip_comments`` now tracks the case grammar (``case`` WORD ``in`` PATTERN ``)``
    body ``;;`` … ``esac``) and treats a PATTERN's closer as a word start; the closers
    of a substitution and of a subshell still end no word (``$(:)# x`` hands ``#`` and
    ``x`` to the command, measured, R35/R38)."""

    _NL = "\n"

    @pytest.fixture(autouse=True)
    def _own_host_is_pinned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        own = argv_floor.socket.gethostname().strip().lower()
        pinned = frozenset(name for name in {own, own.split(".", 1)[0]} if name)
        monkeypatch.setattr(argv_floor, "_OWN_HOST_NAMES_CACHE", pinned)
        monkeypatch.setattr(argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
        monkeypatch.setattr(argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
        monkeypatch.setattr(argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)

    @pytest.mark.parametrize(
        "cmd, rule",
        [
            # the finding's own shape: the mint window
            (f"{_NAME} $(case x in x)# ){_NL} :;; esac; echo status) {_TOK}", _RULE_MINT),
            (f"{_NAME} $(case x in x)#){_NL} :;; esac; echo status) {_TOK}", _RULE_MINT),
            # the kill window
            (f"{_K} $(case x in x)# ){_NL} :;; esac; pgrep -f {_NAME})", _RULE_KILL),
            # a static alternation whose literal hides behind the comment (``case_clause_bodies``)
            (f"ssh $(case x in x)# c{_NL} echo localhost;; esac)", _RULE_SSH),
            (f"ssh $(case x in x)# c{_NL} echo localhost;; esac) uptime", _RULE_SSH),
            # the compound at top level, every pattern spelling
            (f"case x in x)# ){_NL} ssh localhost;; esac", _RULE_SSH),
            (f"case x in (x)# ){_NL} ssh localhost;; esac", _RULE_SSH),
            (f"case x in x|y)# ){_NL} ssh localhost;; esac", _RULE_SSH),
            # a second clause's pattern closer, reached through ``;;``
            (f"case x in a) :;; x)# ){_NL} ssh localhost;; esac", _RULE_SSH),
        ],
    )
    def test_the_commented_closer_closes_nothing(self, cmd: str, rule: str) -> None:
        assert _rule(cmd) == rule

    def test_the_payload_read_from_a_file_is_denied(self, tmp_path: pytest.TempPathFactory) -> None:
        payload = tmp_path / "payload.sh"  # type: ignore[operator]
        payload.write_text(f"{_NAME} $(case x in x)# ){self._NL} :;; esac; echo status) {_TOK}\n")
        assert _rule(payload.read_text().rstrip("\n")) == _RULE_MINT

    def test_the_other_closers_still_end_no_word(self) -> None:
        from kiro_crew.security import quoted_marks, shell_normalizer

        def stripped(text: str) -> str:
            return quoted_marks.strip_comments(list(shell_normalizer._iter_shell_chars(text)))

        # a PATTERN's closer: the comment and its ``)`` are gone, the newline reads `` ; ``
        assert stripped("case x in x)# )\n :;; esac") == "case x in x) ;  :;; esac"
        assert stripped("$(case x in x)# c\n echo a;; esac)") == "$(case x in x) ;  echo a;; esac)"
        # a substitution's closer and a subshell's: ``#`` is text of the next word
        assert stripped("$(:)# x\n y") == "$(:)# x ;  y"
        assert stripped("(true)#x ; y") == "(true)#x ; y"
        assert stripped("`true`#x; y") == "`true`#x; y"
        # a quoted ``case`` is an argument, so its ``)`` is a closer, not a pattern's
        assert stripped('$("case" x in x)# y\n z)') == '$("case" x in x)# y ;  z)'
        # an ``esac`` ends the compound: a later ``)#`` is a closer again
        assert stripped("$(case x in x) :;; esac)# y\n z") == "$(case x in x) :;; esac)# y ;  z"


class TestTheCloserReadingIsASequenceOfItsOwn:
    """R28 Opus: behind an ``esac)`` the lookahead settled as a pattern, only the ONE
    word glued to the closer was recovered for the closer reading, so a two-word
    subcommand behind it (``esac)gateway restart``) never matched -- and when the
    event that refused that reading is an ENCLOSING clause's ``;;`` or a subshell's
    ``)``, the closer reading is bash's own.  Its top-level words up to the event are
    now a sequence of their own, tested independently of the pattern reading."""

    _GW = "self-protection-gateway-" + _RESTART

    @pytest.mark.parametrize(
        "cmd, rule",
        [
            (f"case a in a) {_NAME} $(case x in x) :;; esac)gateway {_RESTART} ;; esac", _GW),
            (f"case a in a) {_NAME} $(case x in x) :;; esac)gateway {_RESTART};; esac", _GW),
            (f"({_NAME} $(case x in x) :;; esac)gateway {_RESTART})", _GW),
            (
                f"if true; then case a in a) {_NAME} $(case x in x) :;; esac)gateway {_RESTART} ;; esac; fi",
                _GW,
            ),
            (
                f"case a in a) {_NAME} $(case x in x) :;; esac)cloud destroy ;; esac",
                "self-protection-cloud",
            ),
            # the one-word specs behind the same closer stay denied
            (f"case a in a) {_NAME} $(case x in x) :;; esac){_TOK} ;; esac", _RULE_MINT),
            (f"case a in a) {_NAME} $(case x in x) :;; esac){_RESTART} ;; esac", _RULE_RESTART),
        ],
    )
    def test_the_closer_reading_behind_an_enclosing_clause_is_read_whole(
        self, cmd: str, rule: str
    ) -> None:
        assert _rule(cmd) == rule

    def test_a_quoted_esac_pattern_with_a_clause_body_is_still_the_pattern(self) -> None:
        # the mark on ``'esac'`` settles it without a lookahead: the body runs INSIDE the
        # substitution, whose output is what the CLI is handed (allowed, as on the base)
        assert _rule(f"{_NAME} $(case x in 'esac')gateway {_RESTART};; esac)") is None
        assert _rule(f"{_NAME} $(case x in 'esac')gateway {_RESTART};; esac) status") is None

    def test_the_walker_lists_the_closer_readings_words_up_to_the_event(self) -> None:
        from kiro_crew.security.shell_normalizer import _SubstitutionDepth

        rest = ["$(case", "x", "in", "x)", ":;;", "esac)gateway", _RESTART, ";;", "esac"]
        depth = _SubstitutionDepth(rest=rest)
        seen: list[list[str]] = []
        for token in rest:
            ended = depth.feed(token)
            seen.append(list(depth.alt_words))
            if ended:
                break
        assert seen[5] == ["gateway", _RESTART]  # both words, not the glued one alone
        assert not depth.top_level  # the pattern reading itself stays open


class TestALongArgvIsReadOnceNotOncePerSpec:
    """R18 GPT: 1,200 program anchors in one argv stalled the gate.  Two structural
    pins, since a wall-clock bound would flake in CI: the five ``_matches_self_subcommand``
    specs share one CLI operand reading per (frame, anchor), and a plain top-level
    token never reaches the walker's character scan."""

    def test_a_subcommand_spec_reads_only_its_leading_words(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.security.shell_normalizer import _SubstitutionDepth

        feeds: list[str] = []
        real = _SubstitutionDepth.feed

        def counting(self: _SubstitutionDepth, token: str) -> bool:
            feeds.append(token)
            return real(self, token)

        monkeypatch.setattr(_SubstitutionDepth, "feed", counting)
        cmd = " ".join([_NAME] * 40) + " status"
        assert argv_floor._is_self_gateway_restart(cmd) is False
        # 40 anchors, a two-word spec: a few tokens per anchor, not the rest of the
        # line each time (which would be ~800), and nothing is retained between calls
        # (R19 Opus: a per-frame cache of full readings held O(anchors^2) words).
        assert len(feeds) < 40 * 4
        assert not hasattr(argv_floor, "_frame_cli_readings")
        assert _rule(" ".join([_NAME] * 40) + f" {_RESTART}") == _RULE_RESTART
        assert (
            _rule(f"{_NAME} $(case x in x) :;; esac) gateway {_RESTART}")
            == "self-protection-gateway-" + _RESTART
        )

    def test_the_mint_window_is_read_once_per_fresh_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # R32 GPT: 2,200 anchors each walked to the argv's end (a 27 s stall).  An anchor
        # the first walk left behind in a fresh state has the identical window, so it is
        # not walked again; anchors inside a substitution still are, and still deny.
        from kiro_crew.security.shell_normalizer import _SubstitutionDepth

        feeds: list[str] = []
        real = _SubstitutionDepth.feed

        def counting(self: _SubstitutionDepth, token: str) -> bool:
            feeds.append(token)
            return real(self, token)

        monkeypatch.setattr(_SubstitutionDepth, "feed", counting)
        n = 400
        assert argv_floor._is_credential_mint(" ".join([_NAME] * n) + " status") is False
        assert len(feeds) < 3 * n
        assert argv_floor._is_credential_mint(" ".join([_NAME] * n) + f" {_TOK}") is True
        assert _rule(f"{_NAME} $({_NAME} {_TOK})") == _RULE_MINT
        assert _rule(f"{_NAME} x $({_NAME} {_TOK})") == _RULE_MINT
        assert _rule(f"{_NAME} -c x {_NAME} {_TOK}") == _RULE_MINT
        assert _rule(f"{_NAME} case x in x) {_NAME} {_TOK};; esac") == _RULE_MINT

    def test_a_brace_reader_is_shared_across_the_anchors_of_one_argv(self) -> None:
        # ...and the ``${`` reader is built once per argv, not once per anchor's suffix:
        # a suffix of the last argv reads from the same tables at an offset (R32 GPT).
        from kiro_crew.security.quoted_marks import BraceCloses
        from kiro_crew.security.shell_normalizer import _SubstitutionDepth

        tokens = [_NAME] * 50 + ["${x}", "status"]
        depth = _SubstitutionDepth(rest=tokens[1:])
        assert depth._brace_closes is None  # lazy: nothing built before a ``${``
        for token in tokens[1:]:
            depth.feed(token)
        assert depth._brace_closes is not None
        first, start = depth._brace_closes
        assert start == 0
        later = _SubstitutionDepth(rest=tokens[30:])
        for token in tokens[30:]:
            later.feed(token)
        assert later._brace_closes is not None
        assert later._brace_closes[0] is first and later._brace_closes[1] == 29
        assert BraceCloses.for_suffix(tokens[30:]) == (first, 29)
        assert BraceCloses.for_suffix(["other"])[1] == 0  # not a suffix: built anew

    def test_a_plain_token_skips_the_character_scan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.security.shell_normalizer import _SubstitutionDepth

        def boom(self: object, token: str) -> bool:
            raise AssertionError(f"scanned {token!r}")

        monkeypatch.setattr(_SubstitutionDepth, "_scan", boom)
        depth = _SubstitutionDepth()
        for token in ["-v", "user@host", "a\x07", "restart"]:
            assert depth.feed(token) is False
        assert depth.words == ["restart"]
        with pytest.raises(AssertionError):
            depth.feed("$(x")
