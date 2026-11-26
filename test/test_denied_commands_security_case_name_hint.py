"""The ssh-to-self and git/tar-to-self refusals ask the agent to rename a variable.

When one of these floors refuses a command only because two of its shell variable
names differ only in case, the refusal ends with a fixed sentence asking the agent
to rename one and retry. That sentence is presentation only: every verdict below
is the same as on main, and a real self-dial gets no such sentence.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _denied_by  # noqa: F401

from kiro_crew.security import deny_gate, diagnostics, is_denied

_S = "local" + "host"
_GIT_TAR = "sandbox-escape-git-tar-self"

# A Playwright screenshot script: `S=` names the script, the here-document's python
# assigns `s=`, and nothing runs git or tar. The git/tar floor refuses it.
_REPORTED = (
    "S=$KIROCREW_SCRATCH/shots.mjs; python3 - \"$S\" <<'EOF'\n"
    "import sys\n"
    "p=sys.argv[1]; s=open(p).read()\n"
    "a=\"await page.goto(base + '/capabilities?tab=templates&template=team-writer')\\n\"\n"
    's=s.replace(a,a+"""const skip = page.getByText(\'Skip all\')\n'
    "await skip.waitFor({ timeout: 20000 }).then(() => skip.click()).catch(() => {})\n"
    "await page.waitForTimeout(1500)\n"
    "for (const t of ['Skip all', 'Skip', 'Not now', 'Close']) {\n"
    "  const b = page.getByRole('button', { name: t, exact: true })\n"
    "  if (await b.isVisible().catch(() => false)) "
    "{ await b.click().catch(() => {}); await page.waitForTimeout(800) }\n"
    "}\n"
    '""",1)\n'
    "open(p,'w').write(s)\n"
    "EOF\n"
    "cd /home/dev/KiroCrew/.worktrees/x/website && "
    "node $S $KIROCREW_SCRATCH/fb-shots $KIROCREW_SCRATCH/jar 2>&1 | grep -v OpenSSL "
    "| tail -6; ls $KIROCREW_SCRATCH/fb-shots"
)

# (command, the rule that refuses it on main or None). Each verdict is pinned, so
# this change cannot move one; case-colliding rows are the ones that get the hint.
_ROWS = [
    (_REPORTED, _GIT_TAR, True),
    ('s=s.".p.e"; echo $S', _GIT_TAR, True),
    ('s=s.".p.e"\n$S', _GIT_TAR, True),
    (f's=s.".p.e"; echo $S; sudo -H git clone {_S}:x', _GIT_TAR, False),
    (f's=s.".p.e"; echo $S; sudo -H tar -xf {_S}:/x', _GIT_TAR, False),
    (f's=s.".p.e"; echo $S; git clone ssh://{_S}/r', _GIT_TAR, False),
    (f"S=-H; s=-h; sudo $S git clone {_S}:x", _GIT_TAR, False),
    (f's=s.".p.e"; echo $S; read t <<<"git clone {_S}:x"; T=1; eval "$t"', _GIT_TAR, False),
    (f's=s.".p.e"; echo $S; for t in "git clone {_S}:x"; do T=1; eval "$t"; done', _GIT_TAR, False),
    (f's=s.".p.e"; echo $S; printf -v t "git clone {_S}:x"; T=1; eval "$t"', _GIT_TAR, False),
    (f's=s.".p.e"; echo $S; v=t; t={_S}; T=github.com; git clone "${{!v}}":x', _GIT_TAR, False),
    (f's=s.".p.e"; echo $S; h=x{_S}; n=1; N=0; git clone "${{h:n}}":x', _GIT_TAR, False),
    (f"S=x; s=github.com; s_kc1={_S}; git clone $s:x", None, False),
    ("S=x; s=github.com; git clone $s:x", None, False),
    (f"git clone {_S}:x", _GIT_TAR, False),
    (f"ssh {_S} true", "sandbox-escape-ssh-self", False),
    (f"S=1; s=2; ssh {_S} true", "sandbox-escape-ssh-self", False),
    # A real self-dial that spells no host name literally: still refused with the
    # names renamed apart, so no hint.
    ('S=1; s=2; git -c core.sshCommand="ssh -o x" clone l*:x', _GIT_TAR, False),
    ("S=1; s=2; ssh l?calhos? true", "sandbox-escape-ssh-self", False),
    ("git clone github.com:x", None, False),
    ("echo hello", None, False),
]


@pytest.mark.parametrize(("cmd", "rule", "hint"), _ROWS)
def test_the_hint_changes_no_verdict(cmd, rule, hint, monkeypatch):
    with_hint = is_denied(cmd)
    monkeypatch.setattr(deny_gate, "refused_for_case_names", lambda *_a, **_k: False)
    without_hint = is_denied(cmd)
    # Same refusal, same rule, same text but for the one added sentence.
    assert (with_hint is None) == (rule is None), cmd
    if rule is None:
        assert without_hint is None, cmd
        return
    assert _denied_by(cmd) == rule, cmd
    assert with_hint.replace(diagnostics.CASE_NAME_HINT, "", 1) == without_hint, cmd


@pytest.mark.parametrize(("cmd", "rule", "hint"), [r for r in _ROWS if r[1] is not None])
def test_only_a_refusal_caused_by_case_names_asks_to_rename(cmd, rule, hint):
    # The flag is set when the names renamed apart would pass every floor and
    # the command names no self host; a real self-dial keeps the plain wording.
    assert deny_gate.refused_for_case_names(rule, cmd) is hint, cmd
    assert (diagnostics.CASE_NAME_HINT in is_denied(cmd)) is hint, cmd


def test_the_hint_names_no_command_text():
    # Fixed text: it never echoes the agent's variable names or command bytes.
    cmd = 'Secretname=s.".p.e"; echo $secretname'
    verdict = is_denied(cmd)
    assert verdict is not None and diagnostics.CASE_NAME_HINT in verdict, cmd
    assert "secretname" not in diagnostics.CASE_NAME_HINT.lower()


def test_case_names_are_renamed_apart_only_when_two_spellings_differ_by_case():
    rename = diagnostics.case_distinct_names
    assert rename("S=1; echo $S ${S} ${#S}") is None
    assert rename("echo --Opt=1 --opt=2") is None
    assert rename('s=a; S=b; echo $s ${S} "$s"') == 's=a; s_kc1=b; echo $s ${s_kc1} "$s"'
    # A new name skips one the command already has.
    assert rename("s=a; S=b; s_kc1=c; echo $S") == "s=a; s_kc2=b; s_kc1=c; echo $s_kc2"
    assert rename("x" * 70000 + " s=1 S=2") is None


def test_a_command_still_refused_with_names_renamed_apart_gets_no_hint(monkeypatch):
    # With the self-host text guard out of the way, the re-read decides alone: a
    # self-dial that the renamed copy still dials is not caused by the case names.
    from kiro_crew.security import argv_floor

    monkeypatch.setattr(deny_gate, "_SELF_HOST_TEXT", ())
    monkeypatch.setattr(argv_floor, "_value_names_literal_self", lambda _v: False)
    assert not deny_gate.refused_for_case_names(_GIT_TAR, f"S=-H; s=-h; sudo $S git clone {_S}:x")
    assert deny_gate.refused_for_case_names(_GIT_TAR, 's=s.".p.e"; echo $S')


def test_other_rules_never_get_the_hint():
    assert not deny_gate.refused_for_case_names("self-protection-kill", 's=s.".p.e"; echo $S')
