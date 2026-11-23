"""The chmod/chown deny rows and the dd raw-disk row, matched on a command's real argv.

As text regexes (``chmod 777.*``, ``chmod.*/usr/.*``) they ran across the whole
compound command and refused ordinary work: a path given to ``ls`` read as a
``chmod`` target, and an ssh ``-o BatchMode`` held the letters of ``chmod``. These tests pin both halves: what is allowed, and the real
invocations, wrappers and nested shells that still are.

Coverage is narrower on purpose, and the cases it gives up are pinned below too
(``test_given_up_by_design``): a refusal that stops ordinary work costs more than
these rare shapes, and the sandbox still confines what any command can touch.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _denied_by

from kiro_crew import security

pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

# Spelled in halves so this file can be read and grepped without every command
# that names it tripping the rules under test.
_CM = "ch" + "mod"
_CO = "ch" + "own"


def _refused(cmd: str) -> bool:
    return _denied_by(cmd) is not None


@pytest.mark.parametrize(
    "cmd",
    [
        # Reported: a path that belongs to another command.
        f"{_CM} +x a.sh && ls /usr/lib",
        f"{_CM} +x guard.sh && /usr/bin/env true",
        f"{_CM} +x build/usr/bin/tool",
        # The verb as data.
        f"git commit -m '{_CM} 777 is bad'",
        f"grep -n '{_CM} 777' src/kiro_crew/security/denied_rules.py",
        f"git show origin/main:src/x.py | grep -nE '{_CM}|{_CO}|/etc/' | head -50",
        f"rg -n '{_CM}|{_CO}' /etc/profile.d",
        f"sed -n '/{_CM}/p' /etc/passwd",
        f"awk '/{_CM}/ {{print $1}}' /etc/passwd",
        f"grep -rn '{_CM}|/etc/' src/ | sort | uniq | head -20",
        f"ls # {_CM} 777 x",
        # ``--reference`` reads that file's mode; it writes only ``f``.
        f"{_CM} --reference=/etc/passwd f",
        f"{_CM} --reference=template 777",
        f"{_CM} --reference /etc localdir",
        f"{_CO} --reference /etc localdir",
        # A dash-led symbolic mode is the mode, so ``777`` here is a file name.
        f"{_CM} -w 777",
        f"{_CM} -R -x 0777",
        # Printed data, not a command.
        f"cat <<'EOF'\n{_CM} -R 777 dir\nEOF",
        f"printf '%s\\n' ';' {_CM} -R 777",
        f"cat <<'EOF' > notes.md\n{_CM} -R 777 dir\nEOF\nls",
        f"cat <<A <<B\n{_CM} 777 x\nA\n{_CO} root /etc/x\nB",
    ],
)
def test_no_longer_refused(cmd):
    assert not _refused(cmd), cmd


def test_ssh_batchmode_is_not_refused(monkeypatch):
    # ``BatchMode`` folds to the verb's letters; it is an ssh option.
    from kiro_crew.security import argv_floor

    monkeypatch.setattr(argv_floor, "_resolved_host_verdict", lambda host, **kwargs: False)
    cmd = "ssh -o BatchMode=yes host ls /usr/lib"
    assert not _refused(cmd), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        f"{_CM} 600 /etc/shadow",
        f"/bin/{_CM} 600 /etc/shadow",
        f"'{_CM}' 600 /etc/shadow",
        f'ch""{_CM[2:]} 600 /etc/shadow',
        f"{_CM} -R g-w /etc/ssl",
        f"{_CM} -x /usr/bin/foo",
        f'{_CM} "" 777 /etc/passwd',
        f"{_CO} 0:0 /etc/sudoers",
        f"sudo {_CM} 755 /usr/bin/ls",
        f"sudo {_CM} 600 /boot/vmlinuz",
        f"sudo {_CM} 640 /sbin/init",
        f"sudo {_CM} 644 /lib/libc.so",
        f"sudo {_CM} 644 /lib64/ld.so",
        f"sudo {_CO} root /usr/bin/ls",
        f"sudo {_CO} root /lib64/ld.so",
        f"sudo {_CM} -R 755 /etc",
        # The mode row needs no system path.
        f"{_CM} 777 ~",
        f"{_CM} -R 777 dir",
        # After ``--`` a ``--reference`` word is a file name, not the option.
        f"{_CM} 777 -- --reference .",
        f"{_CM} 777 -- --reference=template .",
        f"{_CM} 600 -- --reference /etc/shadow",
        "builtin ch''mod 777 ~/.ssh/id_rsa",
        # Wrappers that run the next words as a command.
        f"env X=1 {_CM} 600 /etc/shadow",
        f"X=1 {_CM} 600 /etc/shadow",
        f"timeout 5 {_CO} root /etc/passwd",
        f"nice -n 5 {_CO} root /etc/passwd",
        f"nohup {_CO} root /etc/passwd",
        f"runuser -u root -- {_CM} 600 /etc/shadow",
        f"chroot / {_CM} 600 /etc/shadow",
        f"pkexec {_CM} 600 /etc/shadow",
        f"systemd-run {_CM} 600 /etc/shadow",
        f"doas {_CM} 600 /etc/shadow",
        f"echo x | xargs {_CM} 600 /etc/shadow",
        f"find . -name x -exec {_CM} 600 /etc/shadow ;",
        f"su -c '{_CM} 600 /etc/shadow'",
        f"time -p {_CM} 777 x",
        # A wrapper's long option and its value are stepped over, both forms.
        f"printf '%s\\n' report.txt | xargs --max-args 1 {_CM} 777",
        f"xargs --max-args=1 {_CM} 777 report.txt",
        f"sudo --user root {_CM} 777 report.txt",
        f"sudo --user=root {_CM} 777 report.txt",
        f"sudo --group staff {_CM} 777 report.txt",
        f"/usr/bin/time --format %e {_CM} 777 report.txt",
        f"stdbuf --output L {_CM} 777 report.txt",
        f"cat <<-EOF\n\thello\n\tEOF\n{_CM} 600 /etc/shadow",
        f"cat <<'EOF'\nhello\nEOF\n{_CM} 777 report.txt",
        f"cat <<'EOF' && {_CM} 600 /etc/shadow\nhello\nEOF",
        f"bash -c 'cat <<EOF\nhi\nEOF\n{_CM} 600 /etc/shadow'",
        # Spellings only the reader decodes.
        "$'\\x63hmod' 600 /etc/shadow",
        "ch\\\nmod 600 /etc/shadow",
        # Nested shells and substitutions.
        f"bash -c '{_CM} 000 /etc/shadow'",
        f'sh -c "{_CM} 000 /etc/shadow"',
        f"eval '{_CM} 600 /etc/shadow'",
        f"echo $({_CM} 600 /etc/shadow)",
        f"cat <({_CM} 600 /etc/shadow)",
        f"echo `{_CM} 600 /etc/shadow`",
        f'echo "`{_CM} 600 /etc/shadow`"',
        f"echo \"$(bash -c '{_CM} 600 /etc/shadow')\"",
        f"sh <<EOF\n{_CM} 600 /etc/shadow\nEOF",
        f"echo '{_CM} 600 /etc/shadow' | bash",
        f"grep -h '{_CM} 600 /etc/shadow' f | sh",
        # Chained, glued separators included.
        f"ls /etc/profile.d& {_CM} -R g+w /etc/profile.d",
        f"ls /etc/profile.d&& {_CM} -R g+w /etc/profile.d",
        f"grep -rn x src/;{_CM} 600 /etc/shadow",
        f"grep -rn {_CM} src/ || sudo {_CO} root /etc/passwd",
        f"grep x f && ({_CM} 600 /etc/shadow)",
        f"grep -rn {_CM} src/\n{_CM} 600 /etc/shadow",
        f'grep "$(ls /etc/x& {_CM} -R g+w /etc/x)" f',
        f"ch\"\"mod 777 ~/.ssh/id_rsa ; grep -h '{_CM} 777 /etc/x' f",
        f"{_CM} +w '#' /etc/shadow",
        f"sudo find . -exec {_CM} 777 {{}} ;",
        f"X=1 find . -exec {_CM} 777 {{}} +",
        f"sudo find / -exec {_CO} root /etc/x {{}} +",
    ],
)
def test_real_permission_changes_are_refused(cmd):
    assert _refused(cmd), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        # A program that runs a string it was given. Reading these needs a model
        # of each program's options; the old text match refused them only by
        # refusing every search that named the verb.
        f"awk 'BEGIN{{system(\"{_CM} 600 /etc/shadow\")}}'",
        f"sed '1e {_CM} 777 /etc/shadow' /dev/null",
        f"less +'!{_CM} 777 /etc/shadow' payload.txt",
        f"sort --compress-program='{_CM} 777 /etc/shadow' big.txt",
        # Writing the text to a file runs nothing.
        f"echo '{_CM} 600 /etc/shadow' > /tmp/s.sh",
        # Another program writing a system path is not a permission change.
        f"cp {_CM} /etc/shadow",
        f"xxd {_CM} /usr/local/bin/git",
    ],
)
def test_given_up_by_design(cmd):
    assert _denied_by(cmd) is None, cmd


_RAW_DISKS = ("xvda", "hda", "vda", "mmcblk0", "loop0", "dm-0", "md0", "dasda", "disk/by-id/x")


@pytest.mark.parametrize(
    ("cmd", "refused"),
    [
        # dd reads standard input, so an of= raw disk alone wipes it.
        ("dd of=/dev/sda bs=1M", True),
        ("echo x | dd of=/dev/sda", True),
        ("dd of=/dev/sda < /dev/zero", True),
        ("sudo dd of=/dev/nvme0n1", True),
        *((f"dd of=/dev/{disk}", True) for disk in _RAW_DISKS),
        ("dd of=disk.img bs=1M", False),
        ("dd of=/dev/null", False),
        ("grep -rn 'dd of=/dev/sda' docs/", False),
        ("git commit -m 'flash it with dd of=/dev/sda'", False),
    ],
)
def test_dd_writing_a_raw_disk_is_refused(cmd, refused):
    assert _denied_by(cmd) == ("local-destructive-dd-of-device" if refused else None), cmd


def test_a_disabled_row_stays_disabled():
    from kiro_crew.security import argv_rules

    pattern = argv_rules.ARGV_RULE_BY_ID["local-destructive-chmod-etc"]
    enabled = [r.pattern for r in security.BUILTIN_DENIED_RULES if r.pattern != pattern]
    cmd = f"{_CM} 600 /etc/shadow"
    assert security.is_denied(cmd, denied_regexes=enabled) is None
    assert security.is_denied(cmd, denied_regexes=[pattern]) is not None


def test_the_refusal_names_the_rule_and_the_argv_match():
    from kiro_crew.security import argv_rules

    reason = security.is_denied(f"{_CM} 600 /etc/shadow")
    assert reason is not None
    assert argv_rules.ARGV_RULE_BY_ID["local-destructive-chmod-etc"] in reason
    assert argv_rules.ARGV_RULE_NOTE in reason


def test_every_argv_rule_is_a_catalog_row():
    from kiro_crew.security import argv_rules

    ids = {r.id for r in security.BUILTIN_DENIED_RULES}
    assert set(argv_rules.ARGV_RULE_BY_ID) <= ids
    assert len(argv_rules.ARGV_RULE_BY_ID) == 14


def test_a_command_that_cannot_name_the_programs_is_not_read(monkeypatch):
    from kiro_crew.security import argv_rules

    calls = []
    monkeypatch.setattr(argv_rules, "_commands", lambda c: calls.append(c) or [])
    assert argv_rules.argv_rule_hit("ls -la /tmp", argv_rules.ARGV_RULE_PATTERNS) is None
    assert calls == []


def test_a_separator_glued_to_a_word_starts_a_new_command():
    from kiro_crew.security import argv_rules as r

    def words(tokens):
        return [[w for _, w in cmd] for cmd in r._split_commands_indexed(tokens, glue_split=True)]

    # Off by default: an unmasked caller keeps a glued operator inside the word,
    # exactly as before, because it cannot tell a quoted operator from a real one.
    assert [[w for _, w in c] for c in r._split_commands_indexed(["a;VAR=v", "b"])] == [
        ["a;VAR=v"],
        ["b"],
    ]

    # A real ; | || && glued to a word still separates commands, so the piece
    # after it begins its own command.
    assert words(["a;VAR=v", "b"]) == [["a"], ["VAR=v", "b"]]
    assert words(["a&&VAR=v", "b"]) == [["a"], ["VAR=v", "b"]]
    assert words(["a||VAR=v", "b"]) == [["a"], ["VAR=v", "b"]]
    assert words(["a|VAR=v", "b"]) == [["a"], ["VAR=v", "b"]]
    # A subshell opener glued to a following word opens a new command too.
    assert words(["(VAR=v", "b)"]) == [["VAR=v", "b)"]]
    # A quoted operator was masked to a placeholder upstream, so it is NOT a split.
    assert words(["VAR=\x00v", "b"]) == [["VAR=\x00v", "b"]]
    # The one rule: a word carrying any other shell metacharacter is left whole --
    # a substitution, a ``)`` (case pattern / subshell close) or a ``#`` comment.
    assert words(["$(a;b)", "c"]) == [["$(a;b)"], ["c"]]
    assert words(["a|b)", "c"]) == [["a|b)"], ["c"]]
    assert words(["a;#c", "b"]) == [["a;#c"], ["b"]]
    # A bare separator token, a redirection & and brace expansion are unchanged.
    assert words([";"]) == []
    assert words(["2>&1"]) == [["2>&1"]]
    assert words(["a{b,c}"]) == [["a{b,c}"]]
    assert words(["(", "echo", "hi", ")"]) == [["("], ["echo", "hi", ")"]]


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("true;GIT_SSH_COMMAND='ssh localhost' git fetch o:x", id="semicolon"),
        pytest.param("true&&GIT_SSH_COMMAND='ssh localhost' git fetch o:x", id="and"),
        pytest.param("true||GIT_SSH_COMMAND='ssh localhost' git fetch o:x", id="or"),
        pytest.param("a|GIT_SSH_COMMAND='ssh localhost' git fetch o:x", id="pipe"),
        pytest.param("(GIT_SSH_COMMAND='ssh localhost' git fetch o:x)", id="subshell"),
    ],
)
def test_an_unspaced_command_line_reads_like_its_spaced_spelling(line):
    # The split reads an unspaced command line the same as its spaced spelling,
    # so both reach the same verdict the row already covers.
    assert security.is_denied(line) is not None
    assert (
        security.is_denied(
            line.replace(";", "; ", 1).replace("&&", "&& ", 1).replace("||", "|| ", 1)
        )
        is not None
    )


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(
            "bash -c 'printf \"%s\\n\" \"$@\"' -- 'x;chmod' 777 a", id="quoted-sep-in-payload-arg"
        ),
        pytest.param("echo ok >|git clone ssh://localhost/repo", id="noclobber-redirect"),
        pytest.param(
            "echo ok;#note;GIT_SSH_COMMAND='ssh localhost'\ngit fetch remote:x", id="comment"
        ),
    ],
)
def test_a_quoted_or_redirected_operator_is_not_a_command_boundary(line):
    # The split must not invent a command: a quoted operator in an unmasked
    # payload argument is kept whole (the split is off for that caller), and a
    # ``>|`` redirection is not a pipe. Both stay allowed, as on main.
    assert security.is_denied(line) is None


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("git -C $(pwd&&true) fetch localhost:x", id="operator-in-substitution"),
        pytest.param(
            "git -C $(true status&&printf .) fetch localhost:x",
            id="operator-in-multiword-substitution",
        ),
        pytest.param(
            "case x in a|b) GIT_SSH_COMMAND='ssh localhost' git fetch o:x;; esac",
            id="operator-in-case-pattern",
        ),
    ],
)
def test_an_operator_that_is_not_a_real_boundary_does_not_drop_a_refusal(line):
    # The floor reads the command twice -- once on main's unsplit boundaries,
    # once on the glued-split boundaries -- and refuses if EITHER refuses. So an
    # operator that is NOT a real command boundary (one inside a $()/backtick
    # substitution, whether that substitution is one word or several, or a case
    # pattern word) can never drop a refusal main's reading reaches.
    assert security.is_denied(line) is not None
