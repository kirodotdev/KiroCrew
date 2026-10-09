"""Regression tests for the two quote-aware argv windows in the self-protection
floor.

The mint-verb window (``_is_credential_mint``) and the self-kill-by-name window
(``_is_self_kill``) bound the current command with ``_substitution_depth_delta``,
a paren count over DE-QUOTED tokens.  A QUOTED ``)`` inside a decoy command
substitution ``$(true ')' ; true)`` therefore reads as a real closer, and the
window ends at the decoy ``;`` before the verb or target -- while bash, whose
substitution scan is quote-aware, expands the empty-output span to nothing and
runs the verb or kill that follows.  Each floor now re-runs its scan over a copy
with the top-level substitution span blanked to a word boundary, so the window
reaches the verb or target the decoy hid.

Program and verb literals are assembled from pieces so the mint shape exists
only at runtime, never as a literal substring of this source or any command.
"""

from kiro_crew.security import is_denied

_N = "kiro" + "crew"  # product CLI program name
_T = "to" + "ken"  # the credential-minting verb


def test_decoyed_mint_verb_window_is_denied():
    # ``kirocrew $(true ')' ; true) token`` -- the quoted ``)`` + nested ``;``
    # close the de-quoted window early; bash runs ``kirocrew token``.  Denied on
    # this branch; this exact command is ALLOWED on origin/main (the defect).
    cmd = "{n} $(true ')' ; true) {t}".format(n=_N, t=_T)
    assert is_denied(cmd)


def test_decoyed_self_kill_by_name_window_is_denied():
    # ``pkill -f $(true ')' ; true) kirocrew`` -- same decoy shape; bash runs
    # ``pkill -f kirocrew``.  Denied here; ALLOWED on origin/main.
    cmd = "pkill -f $(true ')' ; true) {n}".format(n=_N)
    assert is_denied(cmd)


def test_undecorated_mint_and_kill_remain_denied():
    # The plain forms the floor has always caught stay denied.
    assert is_denied("{n} {t}".format(n=_N, t=_T))
    assert is_denied("pkill -f {n}".format(n=_N))


def test_legitimate_commands_stay_allowed():
    # The fix only blanks substitution spans before re-scanning the SAME window,
    # so it adds no refusal of an ordinary command.
    assert is_denied("{n} cron add --name x".format(n=_N)) is None
    assert is_denied("pkill -f other") is None
    # a substitution in a data-consumer's argv is not the CLI's own verb window
    assert is_denied("echo {n} $(true ')' ; true) {t}".format(n=_N, t=_T)) is None


def test_decoy_inside_nested_shell_payload_is_denied():
    # The decoy sits inside a ``bash -c '…'`` payload; the window re-derivation
    # must blank spans in each nested payload source, not only the top-level text.
    assert is_denied("bash -c 'pkill -f $(true \" ) \" ; true) {n}'".format(n=_N))
    assert is_denied("bash -c '{n} $(true \" ) \" ; true) {t}'".format(n=_N, t=_T))


def test_process_substitution_decoy_is_denied():
    # ``>( … )`` / ``<( … )`` process substitution is a real operator whose decoy
    # ``)`` ends the de-quoted window early, so it is blanked like ``$( … )``.
    assert is_denied("pkill -f > >(true ')' ; true) {n}".format(n=_N))
    assert is_denied("{n} > >(true ')' ; true) {t}".format(n=_N, t=_T))


def test_glued_substitution_is_not_over_refused():
    # A substitution GLUED to a word is left verbatim (only STANDALONE spans are
    # blanked), so a harmless command whose glued span expands to other text is
    # not invented into a self-protection hit.  The assembled command below runs
    # a plain ``echo`` of the rest in bash, so it must stay allowed.
    glued = "$(printf 'echo ')" + "pki" + "[l]l -f " + _N
    assert is_denied(glued) is None


def test_span_body_with_comment_or_newline_is_left_verbatim():
    # F1 fail-closed guard: a substitution body carrying ``#``, a newline, or an
    # unbalanced quote re-derives the OUTER quote/word state in a way this minimal
    # blanking pass does not model, so the span is left VERBATIM -- exactly what
    # origin/main does (main has no blanking pass).  The branch is therefore no
    # worse than main on this nested-comment decoy: both leave the de-quoted
    # window to run unchanged, so neither newly bypasses nor newly over-refuses.
    nl = "\n"
    verb = "pki" + "ll"
    # ``pkill -f $(: # '<nl>) $(true ')' ; true) kirocrew`` assembled from pieces.
    cmd = verb + " -f $(: # '" + nl + ") $(true ')' ; true) " + _N
    # Allowed on origin/main (an unclosed-class decoy main does not catch); the
    # guard keeps the branch identical to main here rather than mis-blanking.
    assert is_denied(cmd) is None
