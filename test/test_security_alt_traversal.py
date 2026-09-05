"""Tests for the alternate-traversal pass in ``security.py``.

``find`` is not the only program that factors a fenced path into a root plus a
name and hands the result to a reader. These tests pin the shapes ``fd``,
``grep -r``, ``rg`` and ``du`` can spell, the legitimate forms of each that must
keep working, and the residuals that are deliberately left open.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from kiro_crew import security
from kiro_crew.security import is_sensitive_bash_command

#: A crew-home directory that HOLDS fenced leaves (``.env``,
#: ``token_signing.key``) without being fenced itself -- the root every blocked
#: case below traverses.
CREW = "~/.kiro/crew"

#: The legacy data-home prefix, fenced by the same leaf list.
CREW_LEGACY = "~/.kirocrew"


def _denied(command: str) -> bool:
    return is_sensitive_bash_command(command) is not None


def _clean_dir_candidates() -> tuple[str, ...]:
    """Absolute directories worth testing for cleanliness on THIS platform.

    The POSIX system roots do not exist on Windows, where every one of them
    fails ``isdir`` -- so that platform's own roots have to be named too, or the
    search comes up empty there and finds nothing to assert against.
    """
    candidates = ["/usr/share", "/usr/lib", "/etc", "/opt", "/tmp"]
    for variable in ("SystemRoot", "ProgramFiles", "ProgramData"):
        value = os.environ.get(variable)
        if value:
            candidates.append(value)
    candidates.append(tempfile.gettempdir())
    return tuple(candidates)


def _first_clean_dir() -> str | None:
    """An absolute directory this environment agrees holds no fenced path.

    A must-stay-ALLOW case needs a target that is clean HERE, not one that happens
    to be clean on the author's host: ``/tmp`` is unfenced on a dev box but IS
    fenced on the CI runner, where the crew home lives under it, so hard-coding it
    made the assertion an environment fact rather than a code fact.

    Returns ``None`` when the environment offers none, so the single case that
    needs one SKIPS. Raising instead would raise during import, and pytest splits
    tests only after collecting them, so every xdist shard imports every module:
    an import-time raise errors the whole run in all shards rather than dropping
    the one case it concerns.
    """
    for candidate in _clean_dir_candidates():
        if os.path.isdir(candidate) and not security.path_contains_sensitive(candidate):
            return candidate
    return None


#: Resolved once: a directory that is clean in THIS environment, or ``None``.
_CLEAN_ELSEWHERE = _first_clean_dir()

#: Marks the one case whose target must be an unfenced directory.
_needs_clean_dir = pytest.mark.skipif(
    _CLEAN_ELSEWHERE is None,
    reason="no unfenced directory here, so the allow-side assertion would be vacuous",
)


# ── fd: a positional regex, a root, and find's -exec under another name ──


@pytest.mark.parametrize(
    "command",
    [
        # The issue's headline shapes.
        f"fd '^\\.env$' {CREW} -x cat",
        f"fd -e key . {CREW} -X cat",
        # The Debian/Ubuntu binary name for the same tool.
        f"fdfind '^\\.env$' {CREW} -x cat",
        # Long spellings of both exec forms.
        f"fd . {CREW} --exec cat",
        f"fd . {CREW} --exec-batch cat",
        # The reader does not have to be `cat`.
        f"fd . {CREW} -x base64",
        f"fd . {CREW} -x head -c 100",
        # A path-qualified program word.
        f"/usr/bin/fd . {CREW} -x cat",
        # A quoted root, which shlex unwraps.
        f"fd . '{CREW}' -x cat",
        # $HOME instead of a tilde.
        "fd . $HOME/.kiro/crew -x cat",
        # The legacy data-home carries the same leaves.
        f"fd . {CREW_LEGACY} -x cat",
        # The root arrives through a flag rather than as a positional.
        f"fd --search-path {CREW} '^\\.env$' -x cat",
        f"fd --search-path={CREW} nothing --exec-batch cat",
        # No exec flag, but the name list is piped into a reader.
        f"fd . {CREW} | xargs cat",
        f"fd . {CREW} | xargs -0 head -c 100",
        f"fd . {CREW} | parallel cat",
        # xargs flags that take a VALUE put it where the payload would sit, so
        # the payload cannot be found by stopping at the first non-flag token.
        f"fd . {CREW} | xargs -n 1 cat",
        f"fd . {CREW} | xargs -P 4 cat",
        f"fd . {CREW} | xargs -I {{}} cat {{}}",
        # An `env` wrapper hides the program word from a naive first-token read.
        f"env fd . {CREW} -x cat",
        f"env FOO=1 fd . {CREW} -x cat",
        f"env grep -r secret {CREW}",
        # `env`'s own options sit where the program word would, so they have to be
        # skipped as well -- otherwise `-i` is read as the program.
        f"env -i grep -r . {CREW}",
        f"env -i -u FOO grep -r . {CREW}",
        f"env -u FOO grep -r . {CREW}",
        f"env --unset=FOO grep -r . {CREW}",
        # `|&` is ONE operator; consuming only the bar left `&` as the next
        # stage's program and the reader after it was never looked at.
        f"rg --files {CREW} |& xargs cat",
        f"fd . {CREW} |& xargs cat",
        # The workspace root holds the Notes vault's PAT.
        f"grep -r secret {CREW}/workspace",
    ],
)
def test_fd_traversal_into_crew_home_is_denied(command: str) -> None:
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # A listing is not a read: names are not the secret.
        f"fd . {CREW}",
        f"fd '^\\.env$' {CREW}",
        # `cat` on the PIPE prints the name list on stdin; it does not open the
        # files those names point to, so it is not a sink.
        f"fd . {CREW} | cat",
        f"fd . {CREW} | wc -l",
        # An ordinary project tree holds no fenced leaf.
        "fd '^main.py$' ./src -x cat",
        "fd -e py . src",
        "fd -e ts . website/src -x npx prettier --check",
        "fd . ~/Documents",
        "fd . ~/projects/app -x cat",
        # A crew subdirectory that holds no fenced leaf stays readable.
        f"fd . {CREW}/workspace/memory -x cat",
    ],
)
def test_fd_without_delivery_or_fence_is_allowed(command: str) -> None:
    assert not _denied(command), command


# ── grep -r / rg: the reader IS the traversal, so there is no sink to find ──


@pytest.mark.parametrize(
    "command",
    [
        f"grep -r secret {CREW}",
        f"grep -R secret {CREW}",
        # Clustered short flags -- the spelling a person actually types.
        f"grep -rn secret {CREW}",
        f"grep -rl secret {CREW}",
        f"grep -irn secret {CREW}",
        # Long spellings.
        f"grep --recursive secret {CREW}",
        f"grep --dereference-recursive secret {CREW}",
        # grep's aliases, including the one that is recursive with no flag.
        f"egrep -r secret {CREW}",
        f"fgrep -r secret {CREW}",
        f"rgrep secret {CREW}",
        # ripgrep recurses with no flag at all.
        f"rg secret {CREW}",
        # `-l` still opens every file to decide whether to print its name.
        f"rg -l secret {CREW}",
        f"rg --files-with-matches secret {CREW}",
        # `--files` is a pure lister, so it needs a sink -- and here it has one.
        f"rg --files {CREW} | xargs cat",
        f"rg --files {CREW} | parallel cat",
        # Reached through a sequencer rather than as the whole line.
        f"true && grep -r secret {CREW}",
        f"cd /tmp; grep -r secret {CREW}",
        f"grep -r secret {CREW_LEGACY}",
    ],
)
def test_recursive_read_rooted_above_fence_is_denied(command: str) -> None:
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # Not recursive: a single named file is the normalizer pass's business,
        # and this one is not fenced.
        "grep -n secret ~/projects/notes.txt",
        "grep secret ./src/main.py",
        # Recursive, but rooted where no fenced leaf lives.
        "grep -r TODO ./src",
        "grep -r secret ~/projects/app",
        "grep -r secret /tmp/scratch",
        f"grep -r TODO {CREW}/workspace/memory",
        "rg secret ./website/src",
        # A pure lister with no sink discloses nothing.
        f"rg --files {CREW}",
        f"rg --files {CREW} | wc -l",
        "rg --files ./src | xargs cat",
        # The words appear as data, not as a program.
        "echo grep -r",
        f"echo 'grep -r secret {CREW}'",
    ],
)
def test_non_recursive_or_unfenced_reads_are_allowed(command: str) -> None:
    assert not _denied(command), command


# ── A root held in a variable the command itself assigns ──


@pytest.mark.parametrize(
    "command",
    [
        # Each stage is tokenized on its own, so without assignment tracking the
        # `$D` operand stayed literal and the fence was never consulted.
        'D=$HOME/.kiro/crew; rg . "$D"',
        'D=$HOME/.kiro/crew; grep -r secret "$D"',
        'D=$HOME/.kiro/crew; fd . "$D" -x cat',
        # `+=` appends, so a root assembled in two steps resolves too.
        'P=$HOME/.kiro; P+=/crew; rg . "$P"',
        # A declaration keyword assigns just as a bare `NAME=value` does, so the
        # scan has to look past it -- and past its options.
        'export D=$HOME/.kiro/crew; rg . "$D"',
        'readonly D=$HOME/.kiro/crew; rg . "$D"',
        'declare D=$HOME/.kiro/crew; rg . "$D"',
        'declare -x D=$HOME/.kiro/crew; rg . "$D"',
        'typeset D=$HOME/.kiro/crew; rg . "$D"',
        'local D=$HOME/.kiro/crew; grep -r x "$D"',
    ],
)
def test_variable_expanded_root_is_denied(command: str) -> None:
    assert _denied(command), command


def test_variable_expanded_root_outside_the_fence_is_allowed() -> None:
    assert not _denied('D=$HOME/projects; rg . "$D"')
    assert not _denied('export D=$HOME/projects; rg . "$D"')


# ── grep's other recursive switch: the directory ACTION ──


@pytest.mark.parametrize(
    "command",
    [
        f"grep -d recurse . {CREW}",
        f"grep --directories=recurse . {CREW}",
        f"grep --directories recurse . {CREW}",
        f"grep -drecurse . {CREW}",
        # The action flag ends a short-flag cluster, in both spellings.
        f"grep -nd recurse . {CREW}",
        f"grep -ndrecurse . {CREW}",
    ],
)
def test_directory_recurse_action_is_denied(command: str) -> None:
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # `-d skip` and `-d read` are the non-recursive actions.
        f"grep -d skip . {CREW}",
        f"grep --directories=skip . {CREW}",
        "grep --directories skip . ./src",
        # Recursive, but rooted where no fenced leaf lives.
        "grep -n -d recurse pattern ./src",
    ],
)
def test_non_recursive_directory_action_is_allowed(command: str) -> None:
    assert not _denied(command), command


# ── A reader wrapped in a shell ──


@pytest.mark.parametrize(
    "command",
    [
        # The xargs payload is `sh`, and reading only the direct payload called
        # that clean while the `-c` string runs `cat`.
        f"rg --files {CREW} | xargs sh -c 'cat \"$@\"' sh",
        f"fd . {CREW} | xargs bash -c 'cat \"$@\"' bash",
        # The traversal itself inside a shell command string.
        f"sh -c 'grep -r secret {CREW}'",
        f"bash -c 'rg secret {CREW}'",
        # `env -S` carries a whole command the same way `sh -c` does.
        f"env -S 'grep -r secret {CREW}'",
        # `-c` takes a value, so it ends a short-option cluster: `-lc` is the
        # spelling a tool actually emits, and matching the whole token missed it.
        f"bash -lc 'rg . {CREW}'",
        f"sh -lc 'grep -r . {CREW}'",
        f"bash -c'rg . {CREW}'",
        # UPPERCASE letters cluster too (`-C` is noclobber), and this pass feeds
        # case-PRESERVING tokens -- a lowercase-only flag class dropped these
        # while the deleted local scan tolerated any prefix (#8197).
        f"bash -Cc'rg . {CREW}'",
        f"bash -Cc 'rg . {CREW}'",
        # A decoy that satisfies the same token predicate must not eat the stop
        # through which the real carrier's payload is found, and any prefix
        # before the first lowercase `c` still marks a carrier -- both are what
        # the deleted local scan's every-token walk provided.
        f"ksh -onoclobber -c'rg . {CREW}'",
        f"bash -c 'true' -c 'rg . {CREW}'",
        f"bash -1c 'rg . {CREW}'",
        f"rg --files {CREW} | xargs bash -lc 'cat \"$@\"' bash",
        f"rg --files {CREW} | xargs sh -c 'exec cat \"$@\"' sh",
    ],
)
def test_shell_wrapped_traversal_and_sink_are_denied(command: str) -> None:
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # A word meaning "run the thing that follows" sits where the program word
        # would, so the traversal stage matched no rule at all.
        f"command grep -r . {CREW}",
        f"command rg . {CREW}",
        f"command fd . {CREW} -x cat",
        f"command du -a {CREW} | xargs cat",
        f"builtin grep -r . {CREW}",
        f"exec grep -r . {CREW}",
        # `exec -a NAME` takes a value, which would otherwise be read as the
        # program.
        f"exec -a x grep -r . {CREW}",
        # Peeling repeats, so a stacked spelling resolves too.
        f"command env -i grep -r . {CREW}",
    ],
)
def test_execution_wrappers_do_not_hide_the_traversal(command: str) -> None:
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        "command grep -r TODO ./src",
        "exec grep -r TODO ./src",
        "bash -lc 'rg TODO ./src'",
        # `-c` means something else entirely on other programs.
        "head -c 100 ./src/main.py",
        "wc -c ./src/main.py",
    ],
)
def test_wrapped_forms_outside_the_fence_are_allowed(command: str) -> None:
    assert not _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        "sh -c 'grep -r TODO ./src'",
        "rg --files ./src | xargs sh -c 'cat \"$@\"' sh",
        # A shell payload that opens nothing is not a sink.
        f"fd . {CREW} | xargs sh -c 'rm \"$@\"' sh",
    ],
)
def test_shell_wrapped_forms_outside_the_fence_are_allowed(command: str) -> None:
    assert not _denied(command), command


# ── The search PATTERN is text to look for, not a place to look in ──


@pytest.mark.parametrize(
    "command",
    [
        # Searching source for a reference to the crew home is a legitimate read
        # of an ordinary tree; the pattern merely spells a fenced parent.
        'grep -r "$HOME/.kiro" ./src',
        f"grep -r {CREW} ./src",
        f"rg '{CREW}' src/",
        # Pattern only, so the root is the working directory.
        f"rg {CREW}",
    ],
)
def test_a_pattern_naming_a_fenced_parent_is_not_a_root(command: str) -> None:
    assert not _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # With the pattern supplied by a flag, every positional is a root again.
        f"grep -r -e secret {CREW}",
        f"rg -e secret {CREW}",
        # A root arriving through a root-supplying flag occupies the slot the
        # pattern would hold, so it must not be exempted with it.
        f"fd --search-path {CREW} '^\\.env$' -x cat",
        f"fd --search-path={CREW} nothing --exec-batch cat",
        # A short pattern flag takes a value, so it ends a cluster and the value
        # may be glued on. Matching whole tokens saw only `-e secret`, so these
        # spellings fired the exemption and dropped the credential-store root --
        # a false NEGATIVE, which is the direction that actually costs something.
        f"grep -r -esecret {CREW}",
        f"grep -refoo {CREW}",
        f"grep -r -fpatterns.txt {CREW}",
        f"rg -esecret {CREW}",
        # `rgrep` takes a pattern exactly as `grep` does, so it shares the rule --
        # and the root after that pattern is still tested.
        f"rgrep secret {CREW}",
    ],
)
def test_the_pattern_exemption_never_hides_a_root(command: str) -> None:
    assert _denied(command), command


def test_rgrep_shares_the_pattern_exemption() -> None:
    """`rgrep` is recursive with no flag, but its first positional is the pattern."""
    assert not _denied(f"rgrep '{CREW}' ./src")


# ── Wrappers that only change HOW the traversal runs ──


@pytest.mark.parametrize(
    "command",
    [
        # An argv-forwarding program runs the traversal that follows it, so
        # reading the first word as the program matched no rule at all.
        f"nice grep -r . {CREW}",
        f"nohup grep -r . {CREW}",
        f"setsid rg . {CREW}",
        f"ionice rg . {CREW}",
        f"stdbuf -o0 grep -r . {CREW}",
        f"sudo grep -r . {CREW}",
        f"doas grep -r . {CREW}",
        # A wrapper's own positional argument is not the program.
        f"timeout 5 rg . {CREW}",
        f"timeout 1.5m rg . {CREW}",
        f"taskset 0x3 rg . {CREW}",
        f"chrt 10 rg . {CREW}",
        f"nice -n 10 grep -r . {CREW}",
        # Stacked, and mixed with the shell wrappers.
        f"nohup nice rg . {CREW}",
        f"command nice grep -r . {CREW}",
        f"nice env -i grep -r . {CREW}",
        # The delivery rules still apply through a wrapper.
        f"nice fd . {CREW} -x cat",
        f"nice du -a {CREW} | xargs cat",
    ],
)
def test_argv_forwarding_wrappers_do_not_hide_the_traversal(command: str) -> None:
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        "nice grep -r TODO ./src",
        "timeout 5 rg TODO ./src",
        "nohup nice rg TODO ./website/src",
    ],
)
def test_wrapped_traversals_outside_the_fence_are_allowed(command: str) -> None:
    assert not _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # `_program_basename` keeps a Windows suffix on purpose, so this pass has
        # to strip it or every Windows spelling matches no rule.
        f"grep.exe -r . {CREW}",
        f"rg.EXE . {CREW}",
        f"rg.cmd . {CREW}",
        f"fd.exe . {CREW} -x cat",
    ],
)
def test_windows_executable_suffix_is_stripped(command: str) -> None:
    assert _denied(command), command


def test_program_word_strips_only_a_windows_suffix() -> None:
    assert security._alt_program_word("grep.exe") == "grep"
    assert security._alt_program_word("/usr/bin/RG.EXE") == "rg"
    assert security._alt_program_word("rg.cmd") == "rg"
    # A dot that is not an executable suffix is part of the name.
    assert security._alt_program_word("python3.12") == "python3.12"


# ── `--` ends option parsing, so the word after it is the pattern ──


@pytest.mark.parametrize(
    "command",
    [
        # Requiring a non-dash token to be the pattern skipped `-foo` and exempted
        # the ROOT instead, so the recursive read of the crew home read clean.
        f"grep -r -- -foo {CREW}",
        f"rg -- -foo {CREW}",
        f"grep -r -- --foo {CREW}",
        # The ordinary spelling still works.
        f"grep -r -- foo {CREW}",
    ],
)
def test_a_dash_prefixed_pattern_after_end_of_flags_does_not_hide_the_root(
    command: str,
) -> None:
    assert _denied(command), command


# ── A flag-supplied pattern is text to search for, not a place to search ──


@pytest.mark.parametrize(
    "command",
    [
        # The value carries the pattern, so testing it as a root refused an
        # ordinary source search -- the same false positive the positional
        # exemption exists to prevent, reached through the flag spelling.
        'grep -r -e "$HOME/.kiro" ./src',
        'grep -re "$HOME/.kiro" ./src',
        "grep -r --regexp=$HOME/.kiro ./src",
        'grep -r --regexp "$HOME/.kiro" ./src',
        'rg -e "$HOME/.kiro" ./src',
        "grep -r -f $HOME/.kiro ./src",
    ],
)
def test_a_pattern_supplied_by_a_flag_is_not_a_root(command: str) -> None:
    assert not _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # Dropping the VALUE must not drop the root that follows it.
        f"grep -r -e secret {CREW}",
        f"grep -re secret {CREW}",
        f"grep -r --regexp=secret {CREW}",
        f"grep -r --regexp secret {CREW}",
        f"grep -r -f pats.txt {CREW}",
        f"rg -e secret {CREW}",
    ],
)
def test_dropping_the_pattern_value_keeps_the_root(command: str) -> None:
    assert _denied(command), command


def test_pattern_flag_value_removal_reads_both_spellings() -> None:
    strip = security._alt_without_pattern_flag_values
    assert strip(["-r", "-e", "PAT", "/root"]) == ["-r", "/root"]
    assert strip(["-re", "PAT", "/root"]) == ["/root"]
    assert strip(["-r", "--regexp=PAT", "/root"]) == ["-r", "/root"]
    # `--files` is a MODE with no value, so the next word stays a root.
    assert strip(["--files", "/root"]) == ["/root"]
    # `--` is an operand marker, not a pattern flag.
    assert strip(["-r", "--", "/root"]) == ["-r", "--", "/root"]


# ── The working directory is a fallback for a ROOTLESS traversal only ──


@pytest.mark.parametrize(
    "command",
    [
        # Consulting the cwd unconditionally refused every recursive read on a
        # gateway launched from the home directory, including one rooted at a
        # clean tree.
        "grep -r TODO ./src",
        "rg TODO ./website/src",
        "grep -r secret /tmp/scratch",
    ],
)
def test_an_explicit_clean_root_is_not_overridden_by_the_working_directory(
    command: str,
) -> None:
    assert not _denied(command), command


def test_explicit_root_detection_reads_path_shaped_operands() -> None:
    names = security._alt_names_an_explicit_root
    assert names(["./src"])
    assert names(["/tmp/scratch"])
    assert names(["~/projects"])
    # A reader payload and flags are not roots.
    assert not names(["-x", "cat"])
    assert not names(["-r"])


# ── du: a size lister used as a path producer ──


@pytest.mark.parametrize(
    "command",
    [
        f"du -a {CREW} | xargs cat",
        # An intervening stage does not hide the sink.
        f"du -a {CREW} | awk '{{print $2}}' | xargs cat",
    ],
)
def test_size_lister_with_reader_sink_is_denied(command: str) -> None:
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        f"du -a {CREW}",
        f"du -sh {CREW}",
        "du -a ~/projects | xargs cat",
    ],
)
def test_size_lister_without_sink_or_fence_is_allowed(command: str) -> None:
    assert not _denied(command), command


# ── Residuals: named here so a later change cannot quietly assume coverage ──


@pytest.mark.parametrize(
    "command",
    [
        # `locate` has NO root operand -- the database supplies the path -- so the
        # root-containment clause every rule above rests on has nothing to test.
        # Recognising only the leaf names the fence DECLARES would still miss
        # `id_rsa` (`.ssh` is fenced as a whole directory, so no leaf name is
        # declared for it) while reading as covered, so it is left open and named
        # rather than half-closed.
        "locate id_rsa | xargs cat",
        "plocate id_rsa | xargs cat",
        # A name list delivered through a command substitution rather than xargs.
        f"cat $(fd '^\\.env$' {CREW})",
        # A traversal with NO root operand, after the same line moved into the
        # fenced directory. The root is the shell's working directory, which this
        # pass resolves against the gateway's rather than the shell's. The
        # explicit-root spelling of the same read IS denied, by the cd-taint pass.
        f"cd {CREW}; rg secret",
        # A whole-directory COPIER rooted at the fence reaches the same bytes
        # without searching for them. Treating copiers as traversals is a new
        # program class with its own false positives, so it is named rather than
        # half-closed. The lister-pipe spelling (`rg --files … | xargs rsync
        # --files-from=-`) IS denied, because rsync reads as a sink there.
        f"tar -cf out.tar {CREW}",
        f"cp -r {CREW} /tmp",
        f"zip -r out.zip {CREW}",
        f"rsync -r {CREW} /tmp",
    ],
)
def test_documented_residuals_are_not_yet_covered(command: str) -> None:
    """Pins the residuals the module's block comment names.

    A failure here is GOOD news -- it means a later change closed the shape. Move
    the case up to the denied set and delete it from the block comment's residual
    list; do not relax the pass to keep this test passing.
    """
    assert not _denied(command), command


# ── Unit-level behaviour of the pass's own helpers ──


def test_reader_sink_requires_the_payload_to_be_a_reader() -> None:
    stages = security._alt_pipeline_stages("fd . x | xargs cat")
    assert security._alt_has_reader_sink(stages)
    stages = security._alt_pipeline_stages("fd . x | xargs rm")
    assert not security._alt_has_reader_sink(stages)


def test_reader_sink_survives_an_xargs_flag_that_takes_a_value() -> None:
    """`-n 1` puts its value where the payload would sit."""
    stages = security._alt_pipeline_stages("fd . x | xargs -n 1 cat")
    assert security._alt_has_reader_sink(stages)


def test_stage_head_skips_assignments_and_an_env_wrapper() -> None:
    program, operands = security._alt_stage_head(["env", "FOO=1", "fd", ".", "/tmp"])
    assert program == "fd"
    assert operands == [".", "/tmp"]
    program, operands = security._alt_stage_head(["FOO=1", "grep", "-r", "x"])
    assert program == "grep"
    assert operands == ["-r", "x"]


def test_bare_pipe_to_a_reader_is_not_a_sink() -> None:
    """`| cat` prints the NAME list, it does not open the named files."""
    stages = security._alt_pipeline_stages("fd . x | cat")
    assert not security._alt_has_reader_sink(stages)


@pytest.mark.parametrize(
    "command",
    [
        # A bare `&` backgrounds what precedes it and starts a new command.
        # `_split_shell_segments` breaks on `&&` but not on one `&`, so anything
        # placed before it made the stage read as that first program and the
        # traversal after it was never examined.
        f"echo start & grep -r secret {CREW}",
        f"true & rg secret {CREW}",
        f"echo start & fd . {CREW} -x cat",
        f"rg --files {CREW} & xargs cat",
        # Trailing `&` keeps the traversal in its own stage.
        f"grep -r secret {CREW} &",
        f"grep -r secret {CREW} & echo done",
    ],
)
def test_a_bare_ampersand_separates_stages(command: str) -> None:
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # An `&` belonging to a redirection is data, not a separator, so the stage
        # it sits in must stay intact.
        f"grep -r secret {CREW} 2>&1",
        f"rg secret {CREW} >&2",
    ],
)
def test_a_redirection_ampersand_does_not_split_the_stage(command: str) -> None:
    assert _denied(command), command


def test_redirect_ampersand_keeps_one_stage() -> None:
    stages = security._alt_pipeline_stages("grep -r x ./src 2>&1")
    programs = [security._alt_stage_head(s)[0] for s in stages]
    assert programs == ["grep"]


def test_bare_ampersand_yields_two_stages() -> None:
    stages = security._alt_pipeline_stages("echo start & grep -r x ./src")
    programs = [security._alt_stage_head(s)[0] for s in stages]
    assert "echo" in programs and "grep" in programs


def test_pipe_with_stderr_is_one_operator() -> None:
    """`|&` must not leave `&` as the next stage's program word."""
    stages = security._alt_pipeline_stages("rg --files x |& xargs cat")
    programs = [security._alt_stage_head(s)[0] for s in stages]
    assert "xargs" in programs
    assert "&" not in programs


def test_command_string_payloads_become_their_own_stages() -> None:
    stages = security._alt_pipeline_stages("sh -c 'grep -r x ./src'")
    programs = [security._alt_stage_head(s)[0] for s in stages]
    assert "sh" in programs and "grep" in programs


def test_nested_shell_recursion_is_depth_bounded() -> None:
    """A shell inside a shell resolves; an unbounded walk is not on offer."""
    nested = "sh -c 'sh -c 'sh -c ''cat x''''"
    assert security._alt_pipeline_stages(nested) is not None


def test_assignments_are_collected_across_stages() -> None:
    stages = security._alt_pipeline_stages("D=/a/b; P=/c; P+=/d; rg . $D")
    assignments = security._alt_assignments(stages)
    assert assignments["D"] == "/a/b"
    assert assignments["P"] == "/c/d"


def test_env_options_are_skipped_before_the_program_word() -> None:
    program, operands = security._alt_stage_head(["env", "-i", "grep", "-r", "."])
    assert program == "grep"
    assert operands == ["-r", "."]
    program, _rest = security._alt_stage_head(["env", "-u", "FOO", "grep", "-r"])
    assert program == "grep"


def test_execution_wrappers_are_peeled_before_the_program_word() -> None:
    for wrapper in ("command", "builtin", "exec"):
        program, operands = security._alt_stage_head([wrapper, "grep", "-r", "."])
        assert program == "grep", wrapper
        assert operands == ["-r", "."], wrapper
    # `exec -a NAME` takes a value.
    program, _rest = security._alt_stage_head(["exec", "-a", "x", "grep", "-r"])
    assert program == "grep"
    # Peeling repeats across stacked wrappers.
    program, _rest = security._alt_stage_head(["command", "env", "-i", "grep", "-r"])
    assert program == "grep"


def test_shell_c_payload_is_read_from_a_cluster() -> None:
    # Exactly once: both extractors see this spelling, and re-staging one payload
    # twice doubles the walk below it for no new reading.
    assert security._alt_command_string_payloads(["bash", "-lc", "cat x"]) == ["cat x"]
    assert security._alt_command_string_payloads(["sh", "-c", "cat x"]) == ["cat x"]
    assert security._alt_command_string_payloads(["bash", "-c cat x"]) == [" cat x"]
    # `-c` on a non-shell program is not a command string.
    assert security._alt_command_string_payloads(["head", "-c", "100"]) == []


def test_env_split_string_payload_is_read() -> None:
    assert security._alt_command_string_payloads(["env", "-S", "cat x"]) == ["cat x"]


def test_payload_extraction_does_not_repeat_a_shared_spelling() -> None:
    """Both extractors find `sh -c '…'`; the union must not stage it twice."""
    payloads = security._alt_command_string_payloads(["sh", "-c", "grep -r x ./src"])
    assert payloads == ["grep -r x ./src"]
    assert security._alt_command_string_payloads(["env", "--split-string=cat x"]) == ["cat x"]


def test_pattern_supplying_detection_reads_glued_and_clustered_flags() -> None:
    assert security._alt_pattern_is_flag_supplied(["-e", "secret"])
    assert security._alt_pattern_is_flag_supplied(["-esecret"])
    assert security._alt_pattern_is_flag_supplied(["-refoo"])
    assert security._alt_pattern_is_flag_supplied(["-fpatterns.txt"])
    assert security._alt_pattern_is_flag_supplied(["--regexp=secret"])
    # Uppercase only chooses a regex dialect; it supplies no pattern.
    assert not security._alt_pattern_is_flag_supplied(["-F", "-E", "secret"])
    assert not security._alt_pattern_is_flag_supplied(["-rn", "secret"])
    # Everything after `--` is an operand.
    assert not security._alt_pattern_is_flag_supplied(["--", "-e"])


def test_root_operands_exempt_the_pattern_but_not_a_root_flag_value() -> None:
    # Exactly one operand is dropped -- the pattern. Flags stay, because a flag's
    # value can itself be a root and testing a flag only ever answers "no".
    assert security._alt_root_operands("grep", ["-r", "PAT", "/root"]) == [
        "-r",
        "/root",
    ]
    # `du` names only roots, so nothing is exempted there.
    assert security._alt_root_operands("du", ["-a", "/root"]) == ["-a", "/root"]
    # A pattern-supplying flag means nothing is exempted.
    assert "/root" in security._alt_root_operands("grep", ["-r", "-e", "PAT", "/root"])
    # A root-supplying flag's value survives the exemption.
    assert "/root" in security._alt_root_operands(
        "fd", ["--search-path", "/root", "PAT", "-x", "cat"]
    )


def test_pipeline_split_respects_quoting() -> None:
    stages = security._alt_pipeline_stages("grep -r 'a|b' ./src")
    assert len(stages) == 1
    assert security._alt_stage_head(stages[0]) == ("grep", ["-r", "a|b", "./src"])


def test_grep_recursion_is_read_off_clustered_short_flags() -> None:
    assert security._grep_is_recursive(["-rn", "secret", "."])
    assert security._grep_is_recursive(["--recursive", "secret", "."])
    assert not security._grep_is_recursive(["-n", "secret", "."])
    # Everything after `--` is an operand, not a flag.
    assert not security._grep_is_recursive(["--", "-r", "."])


def test_root_check_accepts_a_flag_value_as_a_candidate_root() -> None:
    """A root can arrive as a flag's value, so every operand is tested.

    Tracking which flags take a value would need a table per tool, and each
    omission from such a table would be a MISS.
    """
    home = os.path.expanduser("~")
    assert security._alt_root_reaching_fence([f"--search-path={home}/.kiro/crew"], {})
    assert security._alt_root_reaching_fence([f"{home}/.kiro/crew"], {})
    assert not security._alt_root_reaching_fence(["--max-depth", "2", "secret"], {})


def test_root_check_resolves_a_root_held_in_an_assignment() -> None:
    home = os.path.expanduser("~")
    assignments = {"D": f"{home}/.kiro/crew"}
    assert security._alt_root_reaching_fence(["$D"], assignments)
    assert not security._alt_root_reaching_fence(["$D"], {"D": f"{home}/projects"})


def test_pass_is_reachable_from_the_public_gate() -> None:
    """The pass must be wired into ``is_sensitive_bash_command``, not just defined."""
    reason = is_sensitive_bash_command(f"grep -r secret {CREW}")
    assert reason is not None
    assert "recursive traversal" in reason


# ── Program identification: the traversal is found by NAME, not by position ──


@pytest.mark.parametrize(
    "command",
    [
        # `eval` takes the command as ordinary operands, so quoting the whole
        # thing left the stage holding one opaque word.
        f"eval 'rg --hidden . {CREW}'",
        f'eval "grep -r secret {CREW}"',
        f"eval grep -r secret {CREW}",
        # Stacked shells: one payload per level, deeper than a shell-in-a-shell.
        f'sh -c "sh -c \\"sh -c \\\\\\"rg . {CREW}\\\\\\"\\""',
        # A dispatcher that takes the real program as its first operand.
        f"busybox grep -r . {CREW}",
        f"busybox rg . {CREW}",
        # A wrapper whose own option takes a NON-numeric value, which sat exactly
        # where the program word does.
        f"stdbuf -o L grep -r . {CREW}",
        f"timeout -s KILL 5 rg . {CREW}",
        f"timeout --signal=KILL 5 rg . {CREW}",
        f"ionice -c 3 grep -r secret {CREW}",
    ],
)
def test_a_word_in_front_of_the_program_cannot_hide_it(command: str) -> None:
    """Whatever precedes the traversal, the traversal is still found.

    Enumerating what may sit in front of a program is unbounded -- a builtin, a
    wrapper, an applet dispatcher, a wrapper option's value -- so the program is
    matched by its own name wherever it appears in the stage.
    """
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        f'R=rg; "$R" --hidden . {CREW}',
        f'G=grep; "$G" -r secret {CREW}',
        f'R=rg; "${{R}}" . {CREW}',
        f"export R=rg; $R . {CREW}",
        f'F=fd; "$F" . {CREW} -x cat',
    ],
)
def test_a_program_held_in_a_variable_is_resolved(command: str) -> None:
    """The program slot resolves through the command's own assignments.

    The same indirection the ROOT operands already resolve through, applied to the
    program word -- reading it literally saw ``$R`` and matched no rule.
    """
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # getopt_long accepts any unambiguous prefix, so each of these recurses.
        f"grep --recurs . {CREW}",
        f"grep --recursi . {CREW}",
        f"grep --rec . {CREW}",
        f"grep --der . {CREW}",
        f"grep --direct=recurse . {CREW}",
        f"grep --dir recurse . {CREW}",
        # fd's exec flags and its root-supplying flag abbreviate the same way.
        f"fd . {CREW} --exe cat",
        f"fd --sear {CREW} . -x cat",
    ],
)
def test_an_abbreviated_long_option_means_what_it_abbreviates(command: str) -> None:
    """A GNU long option matches by unique prefix, not as a whole token."""
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # A traversal program's NAME as ordinary text. Scanning every token for a
        # program name must not turn any of these into a traversal.
        "which rg",
        "which fd",
        "type du",
        "command -v rg",
        "man grep",
        "cat ~/bin/rg",
        "ls -la ~/.local/bin/fd",
        "sudo cp rg /usr/local/bin/",
        "chmod +x ./bin/rg",
        "cargo install ripgrep fd-find",
        "echo rg",
        # The search PATTERN happening to be a program name.
        "grep -r rg ./src",
        "rg du ./src",
        "grep -rn fd ./test",
    ],
)
def test_mentioning_a_traversal_program_is_not_running_one(command: str) -> None:
    """A name-scan reading needs an EXPLICIT root that reaches the fence.

    Without that, every command that merely names ``rg`` would be refused whenever
    the gateway happens to run from a directory holding the crew home -- the
    working-directory fallback belongs to the program POSITION alone.
    """
    assert not _denied(command), command


def test_long_flag_matching_accepts_a_prefix_but_not_an_ambiguous_one() -> None:
    candidates = frozenset({"--recursive", "--dereference-recursive"})
    assert security._alt_long_flag_matches("--recursive", candidates)
    assert security._alt_long_flag_matches("--recurs", candidates)
    assert security._alt_long_flag_matches("--rec", candidates)
    # Below the floor: `--r` stands for nothing.
    assert not security._alt_long_flag_matches("--re", candidates)
    assert not security._alt_long_flag_matches("--r", candidates)
    # A short flag is answered exactly -- clusters are read letter by letter.
    assert not security._alt_long_flag_matches("-r", candidates)
    # Longer than the candidate is not a prefix OF it.
    assert not security._alt_long_flag_matches("--recursively", candidates)
    assert not security._alt_long_flag_matches("--directories", candidates)


def test_program_word_resolves_through_an_assignment() -> None:
    assert security._alt_resolved_program_word("$R", {"R": "rg"}) == "rg"
    assert security._alt_resolved_program_word("${R}", {"R": "/usr/bin/rg"}) == "rg"
    # No assignment reaches a traversal name, so the literal reading is returned --
    # which is what `_program_basename` makes of the token, `$` and all.
    assert security._alt_resolved_program_word("$R", {"R": "cat"}) == "r"
    assert security._alt_resolved_program_word("cat", {}) == "cat"


def test_stage_readings_give_the_cwd_fallback_to_the_program_position_only() -> None:
    """The head reading may assume the working directory; a name-scan one may not."""
    head = security._alt_stage_readings(["nice", "rg", "secret"], {})
    assert ("rg", ["secret"], True) in head
    scanned = security._alt_stage_readings(["busybox", "rg", "secret"], {})
    assert ("rg", ["secret"], False) in scanned
    assert not any(program == "rg" and cwd for program, _ops, cwd in scanned)


def test_stage_collection_is_bounded_by_the_stage_budget() -> None:
    """Payload extraction branches, so depth alone does not bound the walk."""
    wide = " | ".join(["echo x"] * (security._ALT_MAX_STAGES + 200))
    assert len(security._alt_pipeline_stages(wide)) <= security._ALT_MAX_STAGES


@pytest.mark.parametrize(
    "command",
    [
        # Reached by unioning in `_nested_shell_payloads`, the extractor the
        # self-protection floor already uses. Each is a payload spelling the pass's
        # own scan declines to see.
        f"bash <<< 'rg . {CREW}'",
        f"bash <<<'grep -r secret {CREW}'",
        f"$SHELL -c 'rg . {CREW}'",
        f"bash -c -- 'rg . {CREW}'",
        f"alias x='grep -r secret {CREW}'; x",
    ],
)
def test_payloads_the_shared_extractor_finds_are_read_as_commands(command: str) -> None:
    """The reused extractor's spellings reach this pass too.

    Re-implementing payload extraction here meant the two disagreed about which
    spellings carry a command, and every spelling only one of them knew was a
    traversal never looked at.
    """
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # A payload GLUED to a short cluster: the token holds characters the shared
        # extractor's flag pattern rejects, so this pass's own scan is what sees it.
        f"sh -c'rg . {CREW}'",
        f"bash -lc 'rg . {CREW}'",
        f"env -S 'grep -r secret {CREW}'",
    ],
)
def test_the_local_scan_still_finds_what_the_shared_extractor_declines(
    command: str,
) -> None:
    """Both extractors are needed: the union is not a replacement of either."""
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # `-c` means something else on a program that is not a shell, so unioning in
        # a second extractor must not turn these into payload carriers.
        "head -c 100 ./README.md",
        "wc -c ./README.md",
        "bash <<< 'rg TODO ./src'",
        "alias x='grep -r TODO ./src'; x",
    ],
)
def test_payload_extraction_stays_scoped_to_shells(command: str) -> None:
    assert not _denied(command), command


def test_exhausting_the_stage_budget_is_a_refusal_not_a_gap() -> None:
    """Padding a command past the budget must not hide the traversal after it.

    The cap keeps an attacker-shaped string cheap; dropping the stages past it
    silently made the cap ITSELF the bypass.
    """
    padded = "; ".join(["echo x"] * (security._ALT_MAX_STAGES + 100))
    assert _denied(f"{padded}; rg . {CREW}")
    # The same padding with no traversal is refused too -- the pass cannot see far
    # enough to say otherwise, and this many stages is not a shape anyone types.
    reason = is_sensitive_bash_command(padded)
    assert reason is not None and "pipeline stages" in reason


def test_an_ordinary_pipeline_is_nowhere_near_the_budget() -> None:
    """The refusal above must not reach a command a person would actually write."""
    stages, truncated = security._alt_pipeline_stages_bounded(
        "rg --files ./src | xargs grep -l TODO | sort | uniq -c | head -20"
    )
    assert not truncated
    assert len(stages) < security._ALT_MAX_STAGES


@pytest.mark.parametrize(
    "command",
    [
        # An abbreviated long PATTERN flag. Missing it exempted the first positional
        # as the pattern -- and here that positional is the ROOT.
        f"grep -r --reg=secret {CREW}",
        f"grep -r --regex=secret {CREW}",
        f"grep -r --reg secret {CREW}",
    ],
)
def test_an_abbreviated_pattern_flag_does_not_exempt_the_root(command: str) -> None:
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # The flag that makes grep recursive, held in a variable.
        f'R=-r; grep "$R" secret {CREW}',
        f'R=-rn; grep "$R" secret {CREW}',
        f'D=recurse; grep -d "$D" secret {CREW}',
        # The sink program, held in a variable.
        f'C=cat; rg --files {CREW} | xargs "$C"',
        f'C=cat; fd . {CREW} | xargs "$C"',
    ],
)
def test_a_flag_or_sink_held_in_a_variable_is_resolved(command: str) -> None:
    """Assignments already resolved the ROOT and the program word; now the rest."""
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # `dd` and its family copy a file's bytes, so a lister piped into one
        # delivers content exactly as `xargs cat` does.
        f"rg --files {CREW} | xargs -I{{}} dd if={{}}",
        f"fd . {CREW} | xargs -I{{}} gzip -c {{}}",
        f"du -a {CREW} | xargs -I{{}} sha512sum {{}}",
        f"fd . {CREW} | xargs -I{{}} hexdump {{}}",
    ],
)
def test_a_copying_reader_is_a_sink(command: str) -> None:
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # An abbreviated pattern flag whose VALUE names the fence is still a
        # pattern, so the clean root stands.
        'grep -r --reg="$HOME/.kiro" ./src',
        'grep -r --regexp="$HOME/.kiro/crew" ./src',
        'grep -r --reg "$HOME/.kiro" ./src',
        # A variable holding a flag that does NOT make grep recursive.
        'R=-n; grep "$R" secret ./src',
        # A variable holding a sink that opens nothing.
        'C=echo; rg --files ./src | xargs "$C"',
        # An ordinary lister into a copying reader, rooted in a clean tree.
        "fd . ./src | xargs -I{} gzip -c {}",
    ],
)
def test_the_round_five_fixes_do_not_refuse_ordinary_commands(command: str) -> None:
    assert not _denied(command), command


def test_pattern_flag_value_arity_resolves_an_ambiguous_abbreviation() -> None:
    """An abbreviation shared with a no-value flag must not consume the next word.

    `--fil` prefixes both `--file` (takes a value) and `--files` (takes none), so
    consuming the next word on that reading would swallow the root.
    """
    assert security._alt_pattern_flag_supplies_a_value("-e")
    assert security._alt_pattern_flag_supplies_a_value("--regexp")
    assert security._alt_pattern_flag_supplies_a_value("--reg")
    assert security._alt_pattern_flag_supplies_a_value("--file")
    # Ambiguous across a value-taking and a no-value flag.
    assert not security._alt_pattern_flag_supplies_a_value("--fil")
    # A mode that takes no value at all.
    assert not security._alt_pattern_flag_supplies_a_value("--files")
    assert not security._alt_pattern_flag_supplies_a_value("--recursive")


def test_token_readings_keep_the_literal_alongside_the_resolved() -> None:
    assert security._alt_token_readings("-r", {}) == ["-r"]
    assert security._alt_token_readings("-r", {"R": "-r"}) == ["-r"]
    readings = security._alt_token_readings("$R", {"R": "-r"})
    assert "$R" in readings and "-r" in readings


@pytest.mark.parametrize(
    "command",
    [
        # A root spelled RELATIVE to a `cd` the same line performs. Resolved against
        # the gateway's own directory this answered no while the shell walked the
        # crew home. The `cd`-into-the-fence spelling was already denied by the
        # cd-taint pass; this is the parent-then-subdirectory one.
        "cd ~/.kiro && rg . crew",
        "cd ~/.kiro; grep -r secret crew",
        "cd ~ && rg . .kiro/crew",
        f"cd {CREW}/.. && rg . crew",
    ],
)
def test_a_root_relative_to_a_cd_resolves_against_it(command: str) -> None:
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # A root whose value the command COMPUTES. The tokenizer splits the
        # substitution across words, so the recorded value is truncated and the
        # fenced path sits in a separate token of the same stage -- the value itself
        # can never carry the answer.
        'D=$(printf %s "$HOME/.kiro/crew"); rg . "$D"',
        'D=$(echo $HOME/.kiro/crew); rg . "$D"',
        'D=`echo $HOME/.kiro/crew`; rg . "$D"',
        'D=$(printf %s "$HOME/.kiro/crew"); grep -r secret "$D"',
    ],
)
def test_a_root_computed_by_a_substitution_fails_closed(command: str) -> None:
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # Every ROLE this pass keys a decision on, reached through a variable.
        f'X=xargs; rg --files {CREW} | "$X" cat',
        f"S=sh; \"$S\" -c 'rg . {CREW}'",
        f'E=--; grep -r "$E" -e {CREW}',
        # A later reassignment does not unwind what the earlier stage already read.
        'D=$HOME/.kiro/crew; rg . "$D"; D=/tmp',
        'D=$HOME/.kiro/crew; grep -r secret "$D"; D=/tmp; D=/var',
    ],
)
def test_a_routed_word_held_in_a_variable_is_resolved(command: str) -> None:
    """Resolving only the traversal name left the sink, the payload carrier and the
    option terminator reading their literal ``$X``."""
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # A `cd` to a clean tree must not make its relative reads suspicious.
        "cd ./src && grep -r TODO .",
        "cd ./src; rg TODO lib",
        "cd ~/Documents && rg . notes",
        # A computed value that names no fence resolves normally -- this is what
        # keeps the fail-closed rule from swallowing every `$(pwd)`.
        'D=$(pwd); rg . "$D"',
        'D=$(printf %s "./src"); rg . "$D"',
        # A variable holding a word in a routed slot, pointed at a clean tree.
        'X=xargs; rg --files ./src | "$X" cat',
        "S=sh; \"$S\" -c 'rg . ./src'",
        'E=--; grep -r "$E" -e ./src',
        pytest.param(
            f'D=./src; rg . "$D"; D={_CLEAN_ELSEWHERE or "/usr/share"}',
            marks=_needs_clean_dir,
        ),
    ],
)
def test_the_round_seven_fixes_do_not_refuse_ordinary_commands(command: str) -> None:
    assert not _denied(command), command


def test_assignment_history_keeps_every_value_a_name_took() -> None:
    stages = security._alt_pipeline_stages("D=/a; rg . $D; D=/b; D+=/c")
    history = security._alt_assignment_history(stages)
    assert history["D"] == ["/a", "/b", "/b/c"]
    # The last-wins view is unchanged, so callers that want it still get it.
    assert security._alt_assignments(stages)["D"] == "/b/c"


def test_cd_bases_are_collected_from_the_command_itself() -> None:
    stages = security._alt_pipeline_stages("cd /a/b && rg . c")
    bases = security._alt_cd_bases(stages, {})
    assert "/a/b" in bases
    # `cd` takes ONE operand, so a following word is not a base.
    stages = security._alt_pipeline_stages("cd /a/b /c/d")
    assert "/c/d" not in security._alt_cd_bases(stages, {})


def test_a_computed_assignment_is_only_fenced_when_its_stage_names_one() -> None:
    home = os.path.expanduser("~")
    fenced = security._alt_pipeline_stages(f'D=$(printf %s "{home}/.kiro/crew"); rg . "$D"')
    assert "D" in security._alt_substitution_assignment_fences(fenced)
    clean = security._alt_pipeline_stages('D=$(pwd); rg . "$D"')
    assert not security._alt_substitution_assignment_fences(clean)


def test_a_wrapper_hidden_traversal_with_no_root_is_a_documented_residual() -> None:
    """Pins the residual the block comment names, without needing a fenced cwd.

    The traversal IS found -- that is the point of the name scan -- but the reading
    is not in the program position, so it may not assume the working directory. A
    failure here means the fallback was widened; re-read why it is narrow before
    relaxing this.
    """
    readings = security._alt_stage_readings(["busybox", "rg", "secret"], {})
    rg_readings = [r for r in readings if r[0] == "rg"]
    assert rg_readings, "the traversal must still be found"
    assert not any(may_assume_cwd for _p, _o, may_assume_cwd in rg_readings)


# ── round ten: program history, positionals, globs, env abbreviation, cost ──


@pytest.mark.parametrize(
    "command",
    [
        # A trailing reassignment does not unwind what the earlier stage read, so
        # the PROGRAM slot is judged on every value its name ever held -- the same
        # rule the roots have followed since round seven.
        f'R=rg; "$R" . {CREW}; R=echo',
        f'G=grep; "$G" -r secret {CREW}; G=true',
        f'R=rg; "${{R}}" . {CREW}; R=echo',
        f'R=rg; "$R" . {CREW_LEGACY}; R=:',
    ],
)
def test_a_routed_program_is_judged_on_every_value_its_name_held(command: str) -> None:
    assert _denied(command), command


def _survives_tokenization(candidate: str) -> bool:
    """Does *candidate* reach the gate's own tokenizer unchanged?

    The gate tokenizes with POSIX rules, where a backslash is an ESCAPE. A Windows
    absolute root therefore arrives as ``C:Usersrunner…`` -- every separator eaten --
    and names nothing fenced. Asked through the real tokenizer rather than a
    hand-written rule, so the two cannot disagree.
    """
    stages = security._alt_pipeline_stages(f"rg . {candidate}")
    return any(candidate in tokens for tokens in stages)


def _fenced_root() -> str | None:
    """A root that is fenced HERE and survives POSIX tokenization, or None.

    Two independent requirements, each learned from a shard failure:

    * fenced in THIS environment, because the suite pins ``KIROCREW_HOME`` per test,
      so ``~/.kiro/crew`` is not where the fence lives while the tests run -- and the
      tokenizer expands ``~`` before this pass sees it, so on Windows that expansion
      mixes separators and stops matching;
    * unchanged by tokenization, because pointing the cases at the pinned home in
      round twelve substituted a BACKSLASH path into a POSIX command string and the
      escapes ate it.

    Resolved at CALL time: a module-level constant would capture whatever was set
    before the per-test fixture ran. Returns None when the platform offers no usable
    spelling, so the cases SKIP rather than assert an environment fact -- which is
    the honest answer for a platform where a POSIX command cannot name the root.
    """
    home = os.environ.get("KIROCREW_HOME")
    candidates = []
    if home:
        # Forward slashes first: Windows accepts them and they survive tokenization.
        candidates.extend([home.replace("\\", "/"), home])
    candidates.append(CREW)
    for candidate in candidates:
        if not candidate or not security.path_contains_sensitive(candidate):
            continue
        if _survives_tokenization(candidate):
            return candidate
    return None


@pytest.mark.parametrize(
    "template",
    [
        # `sh -c 'script' name arg…` binds arg… as $1 onward, so the payload's
        # positional root is a real root and not an unresolvable token.
        "bash -c 'rg . \"$1\"' _ {root}",
        "sh -c 'grep -r secret \"$1\"' _ {root}",
        "sh -c 'rg . \"$@\"' _ {root}",
        "sh -c 'rg . $1' _ {root}",
        "bash -c 'rg . \"${{1}}\"' _ {root}",
        # Glued to the cluster, which is the other spelling of the same flag.
        "sh -c'rg . \"$1\"' _ {root}",
    ],
)
def test_a_positional_root_passed_to_a_shell_payload_is_resolved(template: str) -> None:
    root = _fenced_root()
    if root is None:
        pytest.skip("no fenced root here survives POSIX tokenization")
    command = template.format(root=root)
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # The shell expands these before the gate is asked, so the literal token
        # holds no fence while what runs does.
        "rg . ~/.kiro/cr*",
        "grep -r secret ~/.kiro/cr*/",
        "rg . ~/.kiro/cre?",
        "rg . ~/.kiro/[cd]rew",
        # A brace alternative names the crew home outright.
        "rg . ~/.kiro/{crew,other}",
        f"rg . {CREW}/../cr*",
    ],
)
def test_a_glob_or_brace_root_is_answered_by_what_it_can_expand_to(
    command: str,
) -> None:
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # `getopt_long` accepts any unambiguous prefix, so every abbreviation of
        # env's own chdir flag enters the fenced directory too.
        f"env --chd {CREW} rg secret .",
        f"env --chdi {CREW} rg secret .",
        f"env --chdir {CREW} rg secret .",
        f"env --chd={CREW} rg secret .",
        f"env -C {CREW} rg secret .",
    ],
)
def test_every_spelling_of_env_chdir_supplies_a_traversal_base(command: str) -> None:
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # Each round-ten fix could have been "fixed" by refusing more broadly.
        # These are the commands that would have cost.
        'R=rg; "$R" . ./src; R=echo',
        "bash -c 'rg . \"$1\"' _ ./src",
        "sh -c 'rg . \"$@\"' _ ./src",
        "rg . ./sr*",
        "rg . /usr/sha*",
        "grep -r 'cr*' ./src",
        "rg . ./src/{lib,bin}",
        "env --chd ./src rg secret .",
        "env --chdir ./src grep -r TODO .",
    ],
)
def test_the_round_ten_fixes_do_not_refuse_ordinary_commands(command: str) -> None:
    assert not _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # A renamed grepper is the same traversal. `ag`/`ack` recurse with no flag.
        f"ag secret {CREW}",
        f"ack secret {CREW}",
        f"ack-grep secret {CREW}",
        # `ugrep` is GNU-compatible, so it needs -r and then walks like grep.
        f"ugrep -r secret {CREW}",
        f"ugrep --recursive secret {CREW}",
        # The same wrapper/variable spellings the other traversals are found through.
        f"busybox ag secret {CREW}",
        f'A=ag; "$A" secret {CREW}',
        f"env --chd {CREW} ag secret .",
    ],
)
def test_a_renamed_grepper_is_the_same_traversal(command: str) -> None:
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # The pattern exemption applies to them too, so searching source FOR the
        # crew path stays allowed -- the false positive the exemption exists for.
        f"ag '{CREW}' ./src",
        'ag "$HOME/.kiro" ./src',
        "ag secret ./src",
        "ack secret ./src",
        # ugrep without a recursion flag is not a traversal.
        f"ugrep secret {CREW}/config.json",
        "ugrep -r secret ./src",
    ],
)
def test_the_renamed_greppers_keep_their_ordinary_forms(command: str) -> None:
    assert not _denied(command), command


@pytest.mark.parametrize(
    "count",
    [
        security._ALT_MAX_BRACE_ALTERNATIVES,
        security._ALT_MAX_BRACE_ALTERNATIVES + 1,
        200,
    ],
)
def test_a_brace_cap_overflow_fails_closed(count: int) -> None:
    """Padding a brace past the cap must not drop the fenced alternative.

    The cap used to truncate, so alternative 17 was discarded silently while bash
    expanded it regardless. A failure here means the overflow went back to failing
    open, which is worse than the cap being low.
    """
    padded = ",".join(["a"] * count)
    assert _denied(f"rg secret ~/.kiro/{{{padded},crew}}")


def test_a_brace_overflow_still_allows_a_clean_tree() -> None:
    """Failing closed on overflow must not refuse an ordinary over-long brace."""
    padded = ",".join(["a"] * 40)
    assert not _denied(f"rg secret ./src/{{{padded},lib}}")


def test_the_brace_overflow_reading_is_the_containing_directory() -> None:
    over = "~/.kiro/{" + ",".join(["a"] * 40) + ",crew}"
    assert "~/.kiro" in security._alt_glob_root_readings(over)
    # Within the cap there is nothing to contain, so no parent is added.
    assert security._alt_glob_root_readings("~/.kiro/{a,crew}") == [
        "~/.kiro/a",
        "~/.kiro/crew",
    ]


@pytest.mark.parametrize(
    "template",
    [
        # A quoted command template is argv, so its reader is a real sink.
        "rg --files {root} | parallel 'cat {{}}'",
        'rg --files {root} | parallel "cat {{}}"',
        "rg --files {root} | parallel -I{{}} 'cat {{}}'",
        "rg --files {root} | parallel 'head -c 200 {{}}'",
        "fd . {root} | parallel 'cat {{}}'",
        "rg --files {root} | xargs -I{{}} 'cat {{}}'",
        # `sh -c` inside the template still resolves through the payload extractor.
        "rg --files {root} | parallel 'sh -c \"cat {{}}\"'",
    ],
)
def test_a_quoted_sink_template_is_still_a_sink(template: str) -> None:
    root = _fenced_root()
    if root is None:
        pytest.skip("no fenced root here survives POSIX tokenization")
    command = template.format(root=root)
    assert _denied(command), command


@pytest.mark.parametrize(
    "payload",
    ["cat", "echo", "wc -l", "head -c 200", "printf %s"],
)
def test_a_quoted_template_agrees_with_its_unquoted_payload(payload: str) -> None:
    """The quoted spelling must not have its own policy.

    Whatever `xargs <payload>` decides, `parallel '<payload> {}'` decides too. This
    is the guard against "fixing" the quoted form by inventing a narrower reader
    set for it, which would leave the two spellings disagreeing.
    """
    root = _fenced_root()
    if root is None:
        pytest.skip("no fenced root here survives POSIX tokenization")
    unquoted = _denied(f"rg --files {root} | xargs {payload}")
    quoted = _denied(f"rg --files {root} | parallel '{payload} {{}}'")
    assert unquoted == quoted, f"{payload}: unquoted={unquoted} quoted={quoted}"


def test_a_template_that_opens_nothing_is_not_a_sink() -> None:
    root = _fenced_root()
    if root is None:
        pytest.skip("no fenced root here survives POSIX tokenization")
    assert not _denied(f"rg --files {root} | parallel 'true'")
    # And a clean tree is never a sink question in the first place.
    assert not _denied("rg --files ./src | parallel 'cat {}'")


def test_reading_construction_is_charged_to_the_budget() -> None:
    """Pins Opus's round-13 finding: CONSTRUCTION was the uncharged axis.

    `_alt_stage_readings` built one reading per traversal token before the root
    check's budget was consulted, so a stage of N repeated traversal words cost
    O(N^2) with every declared budget reading as untouched.
    """
    tokens = ["rg"] * 400 + ["/x"]
    budget = security._AltWorkBudget(10)
    readings = security._alt_stage_readings(tokens, {}, None, budget)
    # Enumeration stops once the budget is spent, so it cannot build 400 readings.
    assert len(readings) <= 12, len(readings)
    assert budget.remaining <= 0
    # With a budget that is not exhausted, every traversal token is still read.
    roomy = security._AltWorkBudget(10_000)
    assert len(security._alt_stage_readings(tokens, {}, None, roomy)) >= 2


@pytest.mark.parametrize(
    "command",
    [
        # A later reassignment does not unwind what an earlier stage already read,
        # and that rule now reaches EVERY control slot rather than only the root.
        f'R=-r; grep "$R" secret {CREW}; R=-n',
        f'R=--recursive; grep "$R" secret {CREW}; R=--count',
        f'C=cat; rg --files {CREW} | xargs "$C"; C=true',
        f'D={CREW}; cd "$D" && rg . .; D=/usr/share',
        # The root slot, covered since round eight, must stay covered.
        f'D={CREW}; rg . "$D"; D=/usr/share',
    ],
)
def test_assignment_history_reaches_every_control_slot(command: str) -> None:
    """Pins round 14's GPT finding: history was root-only.

    The recursion flag, the sink program and the chdir base each resolved through
    the shared primitive with the LAST-WINS view alone, so the same
    trailing-reassignment trick worked on all three while the root spelling denied.
    """
    assert _denied(command), command


@pytest.mark.parametrize(
    "command",
    [
        # These copy file CONTENT, so they are sinks like `cat`.
        f"rg --files {CREW} | xargs git hash-object -w",
        f"rg --files {CREW} | xargs git cat-file --batch",
        f"rg --files {CREW} | xargs rsync -a --files-from=-",
        f"fd . {CREW} | xargs git hash-object -w",
    ],
)
def test_content_copying_sinks_are_readers(command: str) -> None:
    assert _denied(command), command


@pytest.mark.parametrize(
    "verb",
    ["cd", "pushd", "chdir", "sl", "set-location", "push-location"],
)
def test_every_chdir_verb_supplies_a_traversal_base(verb: str) -> None:
    """The literal `"cd"` covered one of the six verbs the module already owns.

    `pushd ~/.kiro && rg . crew` walked the fence while the `cd` spelling denied.
    Sourced from `_CHDIR_VERBS` now, so a verb added there needs no second edit.
    """
    assert _denied(f"{verb} ~/.kiro && rg . crew"), verb


def test_the_chdir_verbs_come_from_the_shared_set() -> None:
    """Guards the subtraction: a second, narrower spelling must not come back."""
    stages = security._alt_pipeline_stages("pushd /a/b && rg . c")
    assert "/a/b" in security._alt_cd_bases(stages, {})
    # Every verb the shared set declares is honoured, so the two cannot diverge.
    for verb in security._CHDIR_VERBS:
        stages = security._alt_pipeline_stages(f"{verb} /a/b && rg . c")
        assert "/a/b" in security._alt_cd_bases(stages, {}), verb


@pytest.mark.parametrize(
    "command",
    [
        # Each round-14 fix could have been "fixed" by refusing more broadly.
        'R=-r; grep "$R" secret ./src; R=-n',
        'C=cat; rg --files ./src | xargs "$C"; C=true',
        "pushd ./src && rg . lib",
        "rg --files ./src | xargs git hash-object -w",
        "rg --files ./src | xargs rsync -a --files-from=-",
        "git status",
        "cp -r ./src /tmp",
    ],
)
def test_the_round_fourteen_fixes_do_not_refuse_ordinary_commands(
    command: str,
) -> None:
    assert not _denied(command), command


def test_the_history_walk_lives_in_one_place() -> None:
    """The primitive every slot shares owns the history expansion.

    Round 14 replaced four call-site patches with one mechanism, so a slot added
    later inherits history instead of needing a fifth copy. A failure here means the
    walk was re-inlined somewhere.
    """
    history = {"R": ["-r", "-n"]}
    readings = security._alt_token_readings("$R", {"R": "-n"}, history)
    assert "-r" in readings and "-n" in readings
    # Without history, only the last-wins value resolves.
    assert "-r" not in security._alt_token_readings("$R", {"R": "-n"})
    # A token naming no variable never builds the scratch copy at all.
    assert security._alt_token_readings("plain", {"R": "-n"}, history) == ["plain"]


@pytest.mark.parametrize(
    "traversal",
    [
        # The applet-dispatched spelling is the one the padding attack deleted: it
        # is caught ONLY by the traversal token-scan, which is what stopped being
        # produced once the budget was spent.
        f"busybox grep -r secret {CREW}",
        f"grep -r secret {CREW}",
        f"rg . {CREW}",
        f"stdbuf -o L grep -r secret {CREW}",
        f"env grep -r secret {CREW}",
    ],
)
def test_padding_out_the_budget_cannot_unblock_a_traversal(
    traversal: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins round 15's fail-open: exhaustion must refuse, not answer with less.

    A padding stage spends the shared budget, so any helper that stopped early
    returned a PARTIAL reading set -- and the walk then concluded "no fenced root
    found", which it was not entitled to conclude. Every spelling is checked because
    the reported one was only the cheapest to find.

    The budget is shrunk rather than the command grown: driving the real 4,096-unit
    budget needs ~5,000 tokens, and the normalizer's cost on a string that long is
    57s per case. Same code path, same assertion, milliseconds instead.
    """
    monkeypatch.setattr(security, "_ALT_MAX_RESOLUTIONS", 4)
    padding = " ".join(["x=1"] * 12)
    assert _denied(f"{padding}; {traversal}"), traversal
    # And the same traversal is still denied without the padding, so the test cannot
    # pass merely because padded commands are refused wholesale.
    assert _denied(traversal), traversal


def test_budget_exhaustion_is_always_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The invariant, stated directly: exhausted budget => refusal.

    Asserted on the flag rather than on one command, because this PR shipped a
    fail-open limit three times and each round's test pinned only the single
    spelling that had been reported.
    """
    monkeypatch.setattr(security, "_ALT_MAX_RESOLUTIONS", 4)
    padding = " ".join(["x=1"] * 12)
    reason = security._check_alt_traversal_reaches_fence(f"{padding}; echo hello")
    assert reason is not None
    assert "more traversal analysis" in reason
    # The message is built from `_ALT_MAX_RESOLUTIONS` at call time, so seeing the
    # PATCHED value proves the refusal path reads the live production constant
    # rather than a hard-coded number -- the same thing an end-to-end run against
    # the real 4,096-unit budget would show, without its 40 seconds.
    assert "(4 units)" in reason


def test_history_resolution_is_charged_in_every_slot() -> None:
    """No slot may resolve assignment history without charging the budget.

    Round 14 threaded `history` into the recursion-flag, sink and chdir-base helpers
    WITHOUT the budget, which reopened a 62-second wedge. Each helper is called here
    with a nearly-spent budget and must stop rather than walk the whole history.
    """
    history = {"V": [f"/{i}" for i in range(500)]}
    assignments = {"V": "/0"}
    operands = ['"$V"'] * 500

    spent = security._AltWorkBudget(5)
    security._grep_is_recursive(operands, assignments, history, spent)
    assert spent.exhausted is True

    spent = security._AltWorkBudget(5)
    stages = [["cd", '"$V"'], ["rg", ".", "."]]
    security._alt_cd_bases(stages, assignments, history, spent)
    assert spent.exhausted is True

    spent = security._AltWorkBudget(5)
    security._alt_sink_program_names(operands, assignments, history, spent)
    assert spent.exhausted is True


def test_the_budget_has_no_silently_truncating_spend_path() -> None:
    """Guards the class fix: a caller must not be able to proceed with less.

    `drain` was removed rather than kept as an alias, because its contract -- "stop
    enumerating and return what you have" -- is the fail-open itself. A future
    reintroduction should fail here.
    """
    assert not hasattr(security._AltWorkBudget, "drain")
    assert hasattr(security._AltWorkBudget, "charge")
    assert hasattr(security._AltWorkBudget, "spend")


def test_a_glob_root_reads_as_the_directory_it_globs() -> None:
    """The wildcard's own parent is the question, because the match set is runtime.

    Expanding a glob against the real filesystem inside a security gate would make
    the verdict depend on directory contents at check time, so the parent directory
    is asked the same question every other root is asked.
    """
    assert security._alt_glob_root_readings("~/.kiro/cr*") == ["~/.kiro"]
    assert security._alt_glob_root_readings("/a/b/c?") == ["/a/b"]
    # A brace group enumerates concrete paths rather than a parent.
    assert security._alt_glob_root_readings("/a/{b,c}") == ["/a/b", "/a/c"]
    # No metacharacter, no extra reading -- an ordinary path costs nothing.
    assert security._alt_glob_root_readings("/a/b") == []
    # A bare pattern has no parent to name, so it contributes nothing.
    assert security._alt_glob_root_readings("cr*") == []


def test_positional_binding_follows_the_shell_argument_order() -> None:
    """``sh -c script name a b`` makes ``name`` $0 and ``a``/``b`` $1/$2."""
    bound = security._alt_positional_bound_payload('rg . "$1"', ["_", "/x"])
    assert bound == 'rg . "/x"'
    assert security._alt_positional_bound_payload('rg . "$2"', ["_", "/x", "/y"]) == ('rg . "/y"')
    # `$@` stands for the whole positional list, not including $0.
    assert security._alt_positional_bound_payload("rg . $@", ["_", "/x", "/y"]) == ("rg . /x /y")
    # No positional referenced, so no second reading is manufactured.
    assert security._alt_positional_bound_payload("rg . /x", ["_", "/y"]) is None
    # $0 alone supplies no arguments to bind.
    assert security._alt_positional_bound_payload('rg . "$1"', ["_"]) is None


def test_the_work_budget_is_charged_from_every_axis_not_only_variables() -> None:
    """Pins Opus's round-ten finding: the unbounded axis had no assignments at all.

    A single stage of repeated traversal words spends readings x operands work, and
    the earlier budget only decremented inside the assignment-history loop, which
    never runs for a command that assigns nothing. A failure here means the budget
    stopped covering that axis.
    """
    budget = security._AltWorkBudget(3)
    budget.spend()
    budget.spend()
    budget.spend()
    with pytest.raises(security._AltResolutionBudget):
        budget.spend()
    # `charge` reports instead of raising, for the callers outside the main try --
    # and RECORDS exhaustion, so running out can never be expressed as a smaller
    # answer. That flag is what `_check_alt_traversal_reaches_fence` refuses on.
    soft = security._AltWorkBudget(1)
    assert soft.charge() is True
    assert soft.exhausted is True, "hitting zero must be recorded, not just returned"
    assert soft.charge() is False
    assert soft.exhausted is True
    # A budget that was never overdrawn must not claim exhaustion.
    roomy = security._AltWorkBudget(10)
    assert roomy.charge() is True
    assert roomy.exhausted is False


@_needs_clean_dir
def test_a_repeated_program_command_refuses_rather_than_wedging() -> None:
    """The wedge shape itself: one stage, no assignments, N traversal words.

    The root has to be a directory that is clean HERE. The test suite relocates the
    crew home under the temporary directory, so a hard-coded ``/tmp`` root is fenced
    while the tests run and the command is refused for reaching a fence -- never
    reaching the budget this test exists to pin.
    """
    command = " ".join(["rg"] * 600) + f" {_CLEAN_ELSEWHERE}"
    reason = is_sensitive_bash_command(command)
    assert reason is not None
    assert "more traversal analysis" in reason
