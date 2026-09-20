"""Name-based auto-approve on Windows: PowerShell's lookup, modelled.

A Windows user's auto-approve for shell commands depends on this model -- the
one kiro-cli's shell actually uses. These tests pin the model and every place
it still fails closed.

Every test here runs on EVERY platform: the Windows flag is patched on, and the
resolver only asks ``os.path.isfile``, which needs neither an execute bit nor a
Windows filesystem. The few assertions that depend on ``ntpath`` semantics (an
absolute path with a drive letter) are marked native-only. Unlike
``test_name_grant.py`` there is no module-wide Windows skip: the Windows CI
shards are exactly where these must run.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import subprocess

import pytest

from kiro_crew import name_grant, platform_compat
from kiro_crew.subprocess_utf8 import UTF8_TEXT


@pytest.fixture(autouse=True)
def _clear_pins():
    name_grant._PINS.clear()
    yield
    name_grant._PINS.clear()


def _file(directory, name: str, content: bytes = b"MZ not a real image\n") -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(content)
    return str(path)


@pytest.fixture
def win(tmp_path, monkeypatch):
    """A Windows host with no profile, a fake System32 and a user-writable bin dir.

    Returns ``(system_dir, user_dir, documents)``. ``user_dir`` comes FIRST on
    the search path, the ordering a real host has (version-manager shims and
    ``%LOCALAPPDATA%`` installs precede ``System32``).
    """

    system_dir = tmp_path / "Windows" / "System32"
    user_dir = tmp_path / "Users" / "u" / "AppData" / "Local" / "bin"
    documents = tmp_path / "Users" / "u" / "Documents"
    system_dir.mkdir(parents=True)
    user_dir.mkdir(parents=True)
    documents.mkdir(parents=True)

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        platform_compat,
        "windows_powershell_profile_paths",
        lambda: tuple(
            str(documents / sub / name) for sub, name in platform_compat._POWERSHELL_USER_PROFILES
        ),
    )
    monkeypatch.setattr(
        name_grant,
        "_agent_search_path",
        lambda: os.pathsep.join([str(user_dir), str(system_dir)]),
    )

    def fake_system_bin(name: str) -> str | None:
        # Windows resolves file names case-insensitively, so `os.path.isfile`
        # in the real `trusted_system_bin` matches `find.exe` for `FIND`. A
        # case-sensitive host (a POSIX CI runner with the Windows model patched
        # on) would not, so the stand-in folds case the way the function it
        # replaces does on the platform it models.
        existing = {entry.name.lower(): entry.name for entry in system_dir.iterdir()}
        for suffix in platform_compat._WINDOWS_BIN_SUFFIXES:
            wanted = (name + suffix).lower()
            actual = existing.get(wanted)
            if actual is not None and (system_dir / actual).is_file():
                return str(system_dir / actual)
        return None

    monkeypatch.setattr(platform_compat, "trusted_system_bin", fake_system_bin)
    monkeypatch.setattr(name_grant, "_agent_writable_roots", lambda: ())
    monkeypatch.setattr(name_grant, "_path_is_ambiguous", lambda: False)
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD;.PY")
    # COMSPEC names the system cmd.exe, as it does on every stock host.
    monkeypatch.setenv("COMSPEC", _file(system_dir, "cmd.exe"))
    return system_dir, user_dir, documents


class TestTokenizer:
    """PowerShell's command line, read the way PowerShell reads it."""

    def test_a_backslash_path_survives(self, win):
        # The Windows lexer has no escape character, so the path arrives whole
        # and takes the path branch instead of being read as a bare name.
        assert name_grant.program_names(r"C:\workspace\tool.exe run") == [r"C:\workspace\tool.exe"]

    def test_the_call_operator_opens_a_command_position(self, win):
        assert name_grant.program_names("& git status") == ["git"]
        assert name_grant.program_names("git status; & gh pr view") == ["git", "gh"]

    def test_every_pipeline_stage_is_collected(self, win):
        assert name_grant.program_names("git log | Select-String fix | head") == [
            "git",
            "Select-String",
            "head",
        ]

    def test_redirects_are_operands_including_stream_merges(self, win):
        assert name_grant.program_names("git status > out.txt 2>&1") == ["git"]
        assert name_grant.program_names("2>&1 git status") == ["git"]

    @pytest.mark.parametrize(
        "command",
        [
            "foreach ($x in 1) { git status }",
            "If (1) { git status }",
            "try { git status } catch {}",
            "function head { evil }; head file",
            "FOO=bar git status",
            "git status; { evil }",
        ],
    )
    def test_grammar_this_walk_does_not_model_is_refused(self, win, command):
        assert name_grant.program_names(command) is None

    def test_double_quotes_are_not_escapable(self, win):
        # PowerShell ends a double-quoted string at the next `"`; a backslash
        # before it is a literal backslash. Reading `\"` as an escape would
        # swallow the closing quote and hide the command after it.
        assert name_grant.program_names(r'git commit -m "fix: a\b"') == ["git"]
        assert name_grant.program_names(r'echo "C:\" ; evil') == ["echo", "evil"]


class TestPowerShellBlockComments:
    """``<# ... #>`` is a comment PowerShell discards, and the walk cannot see it.

    ``shlex`` has no block-comment rule, so the delimiters arrive as the redirect
    operators ``<`` and ``>`` and each consumes the token after it as a redirect
    TARGET. The program that actually runs is therefore absent from the walk's
    output -- sometimes replaced by a word from inside the comment, sometimes
    leaving no names at all -- so the whole line has to refuse.
    """

    @pytest.mark.parametrize(
        "command",
        [
            # CI's blocking case: the walk reports ['echo', 'echo'] and `evil`
            # is consumed as the target of the `>` half of `#>`.
            "echo ok; <# echo #> evil",
            # No space anywhere: the walk reports NO names at all, so every
            # downstream per-name check is skipped and the line auto-approves.
            "<#c#>evil",
            # Comment first: the walk reports a word from INSIDE the comment.
            "<# c #> evil",
            # Comment between an inert name and the real program.
            "echo <# c #> evil",
            "echo ok; <#c#> evil",
            # Trailing comment after the program.
            "evil <# c #>",
            # Nested opener, which PowerShell reads as one comment.
            "echo ok; <# a <# b #> #> evil",
            # A comment spanning lines: the delimiters are on different lines,
            # so a per-line walk sees an unbalanced half of one on each.
            "echo ok; <#\nc\n#> evil",
            # Either delimiter alone is enough to make the reading unsafe.
            "echo ok; #> evil",
            "echo ok; <# evil",
        ],
    )
    def test_a_block_comment_refuses_the_line(self, win, command):
        # The exact assertion that flips between the fixed and reverted source.
        assert name_grant.program_names(command) is None

    def test_the_blocking_case_no_longer_auto_approves(self, win):
        # Without the guard the walk reports two inert names, `_program_refusal`
        # clears both, and `name_grant_refusal` returns None -- an auto-approval
        # of a line whose only real program is `evil`. Pin the walk output the
        # exploit depends on as well, so a repair that keeps returning those
        # names but refuses "for another reason" does not read as a fix.
        assert name_grant.name_grant_refusal("echo ok; <# echo #> evil") is not None
        for name in ("echo",):
            assert name in name_grant._WINDOWS_INERT_BUILTINS

    def test_a_comment_never_pins_a_name_it_merely_mentions(self, win):
        # `pin_human_approval` reads the same walk, so a reverted guard would
        # record `c` -- a word from inside a comment -- as a program the human
        # approved, and a later grant for `c` would ride that pin.
        name_grant.pin_human_approval("<# c #> evil")
        assert not name_grant._PINS

    @pytest.mark.parametrize(
        "command,expected",
        [
            # `#` with no `<`/`>` beside it is an ordinary operand, not a
            # comment delimiter this guard reacts to.
            ("git commit -m fix#3", ["git"]),
            ("echo a#b", ["echo"]),
            # A redirect keeps working when the target is not a `#`.
            ("head x > out", ["head"]),
            ("git status", ["git"]),
        ],
    )
    def test_controls_are_unaffected(self, win, command, expected):
        assert name_grant.program_names(command) == expected

    def test_posix_keeps_the_redirect_reading(self, monkeypatch):
        # In a POSIX shell `<#` opens stdin from a file named `#` and `#> evil`
        # writes stdout into `evil`; nothing runs that the walk missed, so the
        # guard must NOT fire and the redirect reading must stand.
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        assert name_grant.program_names("echo ok; <# echo #> evil") == ["echo", "echo"]


class TestPowerShellExpressionInvocation:
    """PowerShell can invoke a VALUE, not just a literal name.

    ``& (expr)`` runs whatever the parenthesized expression evaluates to, ``&
    {...}`` runs the scriptblock's body, ``& $var`` runs the variable's value,
    and the dot-source operator ``.`` does the same in the current scope. The
    walk can read the tokens but not the value, so a grant naming a fixed name
    says nothing about the file that runs. Each of these must refuse the whole
    line rather than vouch for whatever tokens HAPPENED to sit in the position.
    """

    @pytest.mark.parametrize(
        "command",
        [
            # CI's blocking case: two inert cmdlet names occupy the walk while
            # the parenthesized expression names the program that actually runs.
            r"& (Get-Content .\program.txt) echo",
            # Same shape, aliases; the walk would collect only `gc`.
            r"& (gc .\p.txt)",
            r"& (ls .\p.exe)",
            r"& (Write-Output .\p.exe)",
            # A pipeline whose FIRST stage is inert followed by an expression
            # invocation: the walk collects `echo` and the inert `gc`.
            r"echo hi; & (gc .\p.txt)",
            # An expression that computes a name at runtime -- refused for the
            # right REASON (unmodelled), not incidentally.
            "& ('ca' + 'lc')",
            r"& (Get-Item .\p.exe).FullName",
            # Scriptblock invocation; whole thing is one token starting with `{`.
            "& {calc}",
            # Dot-source of an expression: same value/name gap as `&`.
            ". {calc}",
            # Variable and env-variable invocation.
            "& $prog",
            "& $env:COMSPEC",
            # Bare scriptblock and variable at a command position, no operator.
            "{calc}",
            "$env:COMSPEC",
            "$prog args",
        ],
    )
    def test_expression_shaped_command_positions_refuse(self, win, command):
        # The exact assertion that flips between the fixed and reverted source:
        # every one of these MUST return None, not a token list that
        # `_program_refusal` would then judge by the class of the tokens the
        # walk saw. See probe_negative.py.
        assert name_grant.program_names(command) is None

    def test_the_ci_blocking_case_no_longer_auto_approves(self, win):
        # The exploit CI's own adjudicator upheld: on the reverted walk both
        # collected names are inert built-ins, so `_program_refusal` returns
        # None and `name_grant_refusal` auto-approves. With the guard, the
        # WALK refuses and no downstream check ever runs.
        assert name_grant._program_names_line(r"& (Get-Content .\program.txt) echo") is None
        # Pin that the walk output IS what the exploit relies on when the
        # guard is absent: the two names the walk WOULD collect are both inert.
        # A repair that leaves the walk returning them but refuses "for another
        # reason" would still auto-approve when the check-order is rearranged;
        # this pins the class rather than one downstream tier's answer.
        for name in ("Get-Content", "echo"):
            assert name.lower() in name_grant._WINDOWS_INERT_BUILTINS

    @pytest.mark.parametrize(
        "command,expected",
        [
            # Literal name after `&` must keep working: this is the shape a
            # user reaches for on purpose ("run whatever's on my PATH").
            ("& git status", ["git"]),
            # An absolute path after `&` is a literal, not an expression.
            (
                r'& "C:\Windows\System32\where.exe" x',
                [r"C:\Windows\System32\where.exe"],
            ),
            ("head a && echo b", ["head", "echo"]),
            ("git status", ["git"]),
            ("docker-compose up", ["docker-compose"]),
        ],
    )
    def test_controls_still_collect_the_program(self, win, command, expected):
        assert name_grant.program_names(command) == expected

    def test_posix_subshell_is_unchanged(self, monkeypatch):
        # `( head file )` on POSIX opens a SUBSHELL whose first word is the
        # program that runs, so the walk collects `head`. The Windows guard
        # must not reach across the platform flag and refuse this.
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(name_grant.platform_compat, "IS_WINDOWS", False)
        assert name_grant.program_names("(head file)") == ["head"]

    def test_call_operator_followed_by_the_end_of_the_line_does_not_index_past(self, win):
        # The look-ahead must be bounded: `... ; &` at end of line has NO next
        # token to inspect, so the guard cannot fire. It also must not crash
        # with an IndexError on `tokens[index]`. The walk falls through to the
        # existing branches and returns whatever it already collected before
        # the trailing `&`, which is correct: no PROGRAM is decided by a `&`
        # with nothing after it, so there is nothing new to refuse here.
        assert name_grant.program_names("git status; &") == ["git"]
        # And with only `&` on the line, the walk collects no name at all.
        assert name_grant.program_names("&") == []


class TestEnvironment:
    """What must be true of the session before any name can be vouched for."""

    def test_no_profile_means_no_environment_refusal(self, win):
        assert name_grant.windows_environment_refusal() is None
        assert name_grant.platform_scope_notice() is None

    @pytest.mark.parametrize("subdir,filename", platform_compat._POWERSHELL_USER_PROFILES)
    def test_any_per_user_profile_refuses_every_grant(self, win, subdir, filename):
        # kiro-cli starts PowerShell without -NoProfile, so this script runs
        # before the command, and a function it defines resolves ahead of any
        # program -- measured. Same threat as BASH_ENV on POSIX, same answer.
        system_dir, _, documents = win
        _file(system_dir, "find.exe")
        profile = _file(documents / subdir, filename, b"function find { evil }\n")
        refusal = name_grant.name_grant_refusal("find /c x nul")
        assert refusal is not None
        assert refusal.code == name_grant.AMBIGUOUS_ENV
        assert profile in refusal.detail
        assert profile not in refusal.log_text
        # Command scope, not platform scope: the user can remove the file.
        assert name_grant.platform_scope_notice() is None

    def test_unknown_documents_folder_is_platform_scope(self, win, monkeypatch):
        # Without the folder the profile check cannot run, and a check that
        # cannot run its own precondition declines. This is the ONE state that
        # still produces the platform-scope code, so doctor reports it.
        system_dir, _, _ = win
        _file(system_dir, "find.exe")
        monkeypatch.setattr(platform_compat, "windows_powershell_profile_paths", lambda: None)
        refusal = name_grant.name_grant_refusal("find /c x nul")
        assert refusal is not None
        assert refusal.code == name_grant.WINDOWS_UNMODELLED
        assert name_grant.platform_scope_notice() == name_grant.WINDOWS_UNMODELLED
        assert refusal.code in name_grant._PLATFORM_SCOPE_CODES

    def test_bash_preload_variables_do_not_apply(self, win, monkeypatch):
        # PowerShell never reads BASH_ENV; refusing on it here would describe a
        # threat the running shell does not have.
        system_dir, _, _ = win
        _file(system_dir, "find.exe")
        monkeypatch.setenv("BASH_ENV", "/tmp/rc")
        assert name_grant.name_grant_refusal("find /c x nul") is None

    def test_a_relative_search_path_entry_still_refuses(self, win, monkeypatch):
        monkeypatch.setattr(name_grant, "_path_is_ambiguous", lambda: True)
        refusal = name_grant.name_grant_refusal("find /c x nul")
        assert refusal is not None
        assert refusal.code == name_grant.AMBIGUOUS_PATH

    def test_no_pin_is_recorded_while_a_profile_can_hide_the_program(self, win):
        # An approval taken while a profile exists did NOT establish the file
        # behind the name: the profile function is what ran, and the card showed
        # the command, not the file. Pinning there banks an identity no human
        # approved and OUTLIVES the profile, so removing the profile would turn
        # it into an auto-approve. Nothing is pinned, so the name is still
        # refused afterwards and the next approval -- the one that does identify
        # the file -- is what pins it.
        system_dir, user_dir, documents = win
        _file(user_dir, "gh.exe")
        profile = _file(
            documents / "WindowsPowerShell",
            "Microsoft.PowerShell_profile.ps1",
            b"function gh { evil }\n",
        )
        assert name_grant.name_grant_refusal("gh pr list").code == name_grant.AMBIGUOUS_ENV
        name_grant.pin_human_approval("gh pr list")
        assert not name_grant._PINS

        os.remove(profile)
        after = name_grant.name_grant_refusal("gh pr list")
        assert after is not None, "a pin taken behind a profile auto-approved once it was removed"
        assert after.code == name_grant.UNWITNESSED
        # And the approval that CAN identify the file still works.
        name_grant.pin_human_approval("gh pr list")
        assert name_grant.name_grant_refusal("gh pr list") is None

    def test_the_profile_refusal_keeps_a_fixed_size_fingerprint(self, win, monkeypatch):
        # The notice ledger is bounded by ENTRY COUNT, so anything variable-length
        # it retains has to be digested or the bound bounds the wrong dimension:
        # a Documents folder redirected deep enough makes the profile path as long
        # as the filesystem allows, and each profile edit adds another entry.
        _, _, documents = win
        deep = documents / ("redirected" * 12) / "WindowsPowerShell"
        profile = _file(deep, "Microsoft.PowerShell_profile.ps1", b"# long path\n")
        monkeypatch.setattr(platform_compat, "windows_powershell_profile_paths", lambda: (profile,))
        refusal = name_grant.windows_environment_refusal()
        assert refusal is not None
        assert profile in refusal.dedupe_key  # the producer keeps the readable value
        assert len(profile) > 100, profile  # the length the ledger must not retain

        name_grant._DECLINE_NOTICES.clear()
        assert name_grant.should_log_decline("s", refusal) is True
        assert name_grant.should_log_decline("s", refusal) is False
        (key,) = name_grant._DECLINE_NOTICES
        assert profile not in key[2]
        assert len(key[2]) == 32, key  # blake2b(digest_size=16), whatever the path

        # Still ONE line per distinct state: an edited profile is a fresh fact.
        os.utime(profile, ns=(0, 1_234_567_891_000_000_000))
        edited = name_grant.windows_environment_refusal()
        assert edited.dedupe_key != refusal.dedupe_key
        assert name_grant.should_log_decline("s", edited) is True
        assert len(name_grant._DECLINE_NOTICES) == 2

    def test_the_derivation_check_spawns_the_shell_the_module_names(self):
        # The tables and the check that proves them fresh must mean the SAME
        # shell; a second spelling here is how they would drift apart while both
        # look right. Also pins that the constant is what gets spawned, so
        # re-pointing it at another shell re-points the check with it.
        assert "-NoProfile" in name_grant.MODELLED_WINDOWS_SHELL
        source = inspect.getsource(
            TestBuiltinsPrecedeThePath.test_the_builtin_tables_cover_every_name_this_shell_resolves
        )
        assert "name_grant.MODELLED_WINDOWS_SHELL" in source
        assert '"powershell"' not in source

    def test_only_the_5_1_per_user_profiles_are_checked(self):
        # The `("PowerShell", ...)` pairs cover pwsh 7's profile directory. kiro-cli
        # spawns Windows PowerShell 5.1 (`powershell.exe`), whose profiles live under
        # `Documents\WindowsPowerShell` -- pwsh 7's `Documents\PowerShell` file is
        # never read by the shell that actually runs the command. Listing the pwsh 7
        # paths refuses a grant on a file the running shell would not have read.
        # If a maintainer re-adds them, this pin catches it.
        assert platform_compat._POWERSHELL_USER_PROFILES == (
            ("WindowsPowerShell", "profile.ps1"),
            ("WindowsPowerShell", "Microsoft.PowerShell_profile.ps1"),
        )

    def test_a_pwsh_7_profile_does_not_refuse(self, win):
        # A file at pwsh 7's profile path exists but is never read by the shell
        # kiro-cli spawns. The env refusal must not fire on it.
        system_dir, _, documents = win
        _file(system_dir, "find.exe")
        pwsh7_profile = _file(
            documents / "PowerShell",
            "Microsoft.PowerShell_profile.ps1",
            b"function find { evil }\n",
        )
        assert pwsh7_profile  # created
        # The 5.1 profile dirs stay clear.
        assert name_grant.windows_environment_refusal() is None
        assert name_grant.name_grant_refusal("find /c x nul") is None


class TestResolutionOrder:
    """Directory-major, `.ps1` first, then PATHEXT -- as measured on 5.1."""

    def test_ps1_beats_exe_in_the_same_directory(self, win):
        _, user_dir, _ = win
        _file(user_dir, "tool.exe")
        script = _file(user_dir, "tool.ps1", b"Write-Output hi\n")
        assert name_grant._windows_which("tool", name_grant._agent_search_path()) == script

    def test_an_earlier_directory_beats_a_better_extension(self, win):
        system_dir, user_dir, _ = win
        _file(system_dir, "tool.exe")
        later_script = _file(user_dir, "tool.cmd", b"@echo hi\n")
        assert name_grant._windows_which("tool", name_grant._agent_search_path()) == later_script

    def test_an_explicit_extension_is_tried_as_written(self, win):
        system_dir, _, _ = win
        exe = _file(system_dir, "whoami.exe")
        assert name_grant._windows_which("whoami.exe", name_grant._agent_search_path()) == exe
        # And a name whose extension is not one the shell runs is not padded
        # into one: `tool.txt` is looked up as `tool.txt.exe`, never `tool.txt`.
        _file(system_dir, "tool.txt")
        assert name_grant._windows_which("tool.txt", name_grant._agent_search_path()) is None

    def test_pathext_is_read_from_the_environment(self, win, monkeypatch):
        _, user_dir, _ = win
        _file(user_dir, "tool.vbs")
        assert name_grant._windows_which("tool", name_grant._agent_search_path()) is None
        monkeypatch.setenv("PATHEXT", ".EXE;.VBS")
        assert name_grant._windows_which("tool", name_grant._agent_search_path()) is not None

    def test_a_pathext_that_lists_ps1_does_not_try_it_twice(self, win, monkeypatch):
        # Some hosts add `.PS1` to `PATHEXT` themselves. The shell tries a
        # spelling once, so the candidate list must hold it once -- and still
        # first, because that position is what makes a `.ps1` beat a sibling
        # `.exe`.
        monkeypatch.setenv("PATHEXT", ".EXE;.PS1;.CMD")
        extensions = name_grant._windows_extensions()
        assert extensions == (".ps1", ".exe", ".cmd")
        assert len(extensions) == len(set(extensions))

    def test_the_search_path_is_never_the_shell_which(self, win):
        # `shutil.which` neither knows `.ps1` nor tries it first, so the module
        # must not fall back to it on Windows.
        source = inspect.getsource(name_grant._program_refusal)
        assert source.count("shutil.which(") == 1
        assert "_windows_which(name" in source


class TestFileAssociations:
    """A hit Windows runs through the registry is not a file this check can pin."""

    def test_a_pathext_script_with_an_associated_interpreter_is_refused(self, win):
        _, user_dir, _ = win
        script = _file(user_dir, "tool.py", b"print('hi')\n")
        refusal = name_grant.name_grant_refusal("tool run")
        assert refusal is not None
        assert refusal.code == name_grant.FILE_ASSOCIATION
        assert script in refusal.detail
        assert script not in refusal.log_text

    def test_a_pinned_association_script_is_still_refused(self, win):
        # A witness cannot help: the pin binds the script's bytes while the
        # interpreter comes from HKCU, which the same user can rewrite.
        _, user_dir, _ = win
        _file(user_dir, "tool.py", b"print('hi')\n")
        name_grant.pin_human_approval("tool run")
        refusal = name_grant.name_grant_refusal("tool run")
        assert refusal is not None
        assert refusal.code == name_grant.FILE_ASSOCIATION

    def test_a_batch_file_needs_the_system_comspec(self, win, monkeypatch):
        _, user_dir, _ = win
        _file(user_dir, "tool.cmd", b"@echo hi\n")
        name_grant.pin_human_approval("tool run")
        assert name_grant.name_grant_refusal("tool run") is None
        monkeypatch.setenv("COMSPEC", str(user_dir / "cmd.exe"))
        refusal = name_grant.name_grant_refusal("tool run")
        assert refusal is not None
        assert refusal.code == name_grant.AMBIGUOUS_ENV
        monkeypatch.delenv("COMSPEC")
        refusal = name_grant.name_grant_refusal("tool run")
        assert refusal is not None
        assert refusal.code == name_grant.AMBIGUOUS_ENV

    def test_a_ps1_shebang_is_not_followed(self, win):
        # PowerShell runs a `.ps1` itself and reads `#!` as a comment. `npm.ps1`
        # ships with `#!/usr/bin/env pwsh`; following it would refuse a program
        # that runs for the sake of one that does not.
        _, user_dir, _ = win
        _file(user_dir, "npm.ps1", b"#!/usr/bin/env pwsh\nWrite-Output hi\n")
        name_grant.pin_human_approval("npm --version")
        assert name_grant.name_grant_refusal("npm --version") is None


class TestBuiltinsPrecedeThePath:
    """Aliases and session functions resolve before any file -- measured."""

    @pytest.mark.parametrize(
        "command", ["ls -la", "cat x", "Get-ChildItem -Recurse", "dir", "echo hi"]
    )
    def test_an_inert_builtin_needs_no_file(self, win, command):
        # `ls` is Get-ChildItem on PowerShell even with an `ls.exe` planted
        # first on the search path: the alias wins, so the file is irrelevant.
        _, user_dir, _ = win
        _file(user_dir, "ls.exe")
        assert name_grant.name_grant_refusal(command) is None

    @pytest.mark.parametrize("name", ["where", "sc", "curl", "iwr", "sp", "kill", "ri", "ni"])
    def test_a_non_inert_builtin_is_refused_not_resolved(self, win, name):
        # `sc.exe` and `where.exe` sit in System32; PowerShell runs Set-Content
        # and Where-Object instead. Vouching for the file would answer for a
        # program the shell does not run.
        system_dir, _, _ = win
        _file(system_dir, f"{name}.exe")
        refusal = name_grant.name_grant_refusal(f"{name} x")
        assert refusal is not None
        assert refusal.code == name_grant.BUILTIN_SHADOWS, name

    def test_an_explicit_extension_bypasses_the_alias(self, win):
        # PowerShell: `sort.exe` is not the alias `sort`. The file is judged.
        system_dir, _, _ = win
        _file(system_dir, "sort.exe")
        assert name_grant.name_grant_refusal("sort.exe x") is None
        assert name_grant.name_grant_refusal("where.exe git") is not None  # no such file

    def test_the_inert_table_is_a_subset_of_known_builtins_or_cmdlets(self):
        # Every alias in the inert table must be a real default alias; a cmdlet
        # name is Verb-Noun. A typo here would silently allow a real program of
        # that name to bypass resolution.
        for name in name_grant._WINDOWS_INERT_BUILTINS:
            assert name in name_grant._POWERSHELL_DEFAULT_ALIASES or "-" in name, name

    def test_dispatching_builtins_are_never_inert(self):
        for name in ("iex", "icm", "saps", "start", "ii", "foreach", "%", "where", "?"):
            assert name not in name_grant._WINDOWS_INERT_BUILTINS

    #: Names whose `-Property`/`-GroupBy` argument PowerShell evaluates as a
    #: calculated property, measured on 5.1 with object input: the block runs
    #: once per input object, so the argument -- which the agent writes -- can
    #: start any process.
    CALCULATED_PROPERTY_NAMES = (
        "select",
        "select-object",
        "sort",
        "sort-object",
        "group",
        "group-object",
        "compare",
        "diff",
        "compare-object",
        "fl",
        "ft",
        "fw",
        "format-list",
        "format-table",
        "format-wide",
        "format-custom",
    )

    @pytest.mark.parametrize("name", CALCULATED_PROPERTY_NAMES)
    def test_a_calculated_property_taker_is_never_inert(self, name):
        # A script block in ANY parameter disqualifies a name, not only in the
        # pipeline position `where`/`foreach` use.
        assert name not in name_grant._WINDOWS_INERT_BUILTINS, name

    @pytest.mark.parametrize("name", CALCULATED_PROPERTY_NAMES)
    def test_a_calculated_property_taker_is_refused(self, win, name):
        # The command the block would run is never named to this check, so an
        # inert verdict here is an unwitnessed process launch. `select` is
        # refused one branch earlier -- it is also a POSIX shell keyword, so the
        # walk fails closed before the Windows tables are consulted -- which is
        # why the accepted set is both fail-closed codes rather than one.
        _, user_dir, _ = win
        _file(user_dir, f"{name}.exe")
        command = f"{name} -InputObject x -Property {{ Start-Process notepad }}"
        refusal = name_grant.name_grant_refusal(command)
        assert refusal is not None, name
        assert refusal.code in (
            name_grant.BUILTIN_SHADOWS,
            name_grant.UNTOKENIZABLE,
        ), (name, refusal.code)
        assert not name_grant._PINS

    @pytest.mark.parametrize("name", ["measure", "measure-object", "gm", "get-member"])
    def test_a_string_typed_property_taker_stays_inert(self, win, name):
        # `measure-object` types `-Property` as `String[]`, so a block is
        # coerced to its source text and never evaluated (measured: zero
        # evaluations). Refusing it would cost a prompt for nothing.
        assert name in name_grant._WINDOWS_INERT_BUILTINS, name
        assert name_grant.name_grant_refusal(f"{name} -Property {{ 1 }}") is None, name

    @pytest.mark.parametrize("name", ["cfs", "get-verb"])
    def test_a_builtin_for_an_autoloadable_command_still_refuses(self, win, name):
        # An alias (`cfs` -> ConvertFrom-String) and a session function
        # (`get-verb`) both resolve ahead of an application unconditionally --
        # unlike the FULL cmdlet name, whose precedence depends on what the line
        # already auto-loaded. So neither may reach the search-path walk: a
        # planted file must not be judged, witnessed or pinned.
        _, user_dir, _ = win
        planted = _file(user_dir, f"{name}.exe")
        refusal = name_grant.name_grant_refusal(f"{name} x")
        assert refusal is not None, name
        assert refusal.code == name_grant.BUILTIN_SHADOWS, name
        assert planted not in refusal.detail
        assert not name_grant._PINS

    @pytest.mark.skipif(not platform_compat.IS_WINDOWS, reason="enumerates the live shell")
    def test_the_builtin_tables_cover_every_name_this_shell_resolves(self, win):
        # THE DERIVATION CHECK for the two built-in tables. A hand-written list
        # goes stale silently and the failure is a wrong ANSWER, not an error:
        # an unlisted built-in falls through to the search path and the walk
        # vouches for a file the shell will not run. So ask the shell itself,
        # and require every name it resolves to be refused -- by a table, or by
        # an earlier branch for the ones whose spelling makes them a path.
        #
        # This is what caught `cfs` and `get-verb`.
        _, user_dir, _ = win
        script = (
            "Get-Alias | ForEach-Object { $_.Name }; "
            "Get-ChildItem function: | ForEach-Object { $_.Name }"
        )
        completed = subprocess.run(
            [*name_grant.MODELLED_WINDOWS_SHELL, "-Command", script],
            capture_output=True,
            timeout=120,
            **UTF8_TEXT,
        )
        assert completed.returncode == 0, completed.stderr
        resolved = sorted({line.strip() for line in completed.stdout.splitlines() if line.strip()})
        assert len(resolved) > 100, resolved  # a real 5.1 session, not an empty read

        tabled = name_grant._POWERSHELL_DEFAULT_ALIASES | name_grant._POWERSHELL_CORE_COMMANDS
        for name in resolved:
            lowered = name.lower()
            # Give a fall-through something to resolve TO, so a miss shows up as
            # a wrong approval rather than as an unknown-bare-word refusal.
            if not set(name) & set('\\/:*?"<>|'):
                _file(user_dir, f"{name}.exe")
            name_grant._PINS.clear()
            refusal = name_grant.name_grant_refusal(f"{name} x")
            if lowered in name_grant._WINDOWS_INERT_BUILTINS:
                # Inert by construction: the built-in runs, and no file on disk
                # can change what it does, so approving is the right answer and
                # the planted file is irrelevant. Pinned by the inert tests.
                continue
            assert refusal is not None, f"{name} would be answered for by a planted file"
            if lowered in tabled:
                continue
            # Not in a table: the only acceptable reason is that its own
            # spelling took it out of the bare-name branch (`cd\`, `c:`).
            assert refusal.code == name_grant.RELATIVE_PATH, (
                f"{name} reaches the search-path walk and is in no built-in table "
                f"(refused as {refusal.code}, which is not a spelling refusal)"
            )

    @pytest.mark.skipif(not platform_compat.IS_WINDOWS, reason="enumerates the live shell")
    def test_the_shell_agrees_the_names_kept_inert_cannot_take_a_block(self, win):
        # THE DERIVATION CHECK for the other half of the table: which names
        # STAY. Membership is not the property that decides it -- a calculated
        # property is possible only where the parameter is typed loosely enough
        # to accept a script block, so ask the shell for the TYPE instead of
        # trusting a measurement written down once. A shell that widens
        # `-Property` from a string to a property expression turns this red,
        # rather than leaving a green pin on the exact hole this table closes.
        names = sorted(name_grant._WINDOWS_INERT_BUILTINS)
        listed = ", ".join(f"'{name}'" for name in names)
        script = (
            f"foreach ($n in @({listed})) {{ "
            "$c = Get-Command $n -ErrorAction SilentlyContinue; "
            "if (-not $c -or -not $c.Parameters) { continue } "
            "foreach ($p in @('Property', 'GroupBy', 'ExpandProperty')) { "
            "if ($c.Parameters.ContainsKey($p)) { "
            '"$n`t$p`t" + $c.Parameters[$p].ParameterType.FullName } } }'
        )
        completed = subprocess.run(
            [*name_grant.MODELLED_WINDOWS_SHELL, "-Command", script],
            capture_output=True,
            timeout=120,
            **UTF8_TEXT,
        )
        assert completed.returncode == 0, completed.stderr
        rows = [line.split("\t") for line in completed.stdout.splitlines() if line.strip()]
        # The inert set does keep property takers (`measure-object`), so an
        # empty read means the query failed rather than that the set is clean.
        assert rows, f"the shell reported no property-taking parameter at all: {completed.stdout!r}"
        for name, parameter, kind in rows:
            assert kind in ("System.String", "System.String[]"), (
                f"{name} -{parameter} is typed {kind}, which accepts a script block, "
                f"so {name} cannot stay in the inert set"
            )


class TestFullCmdletNamesPrecedeThePath:
    """A cmdlet spelled out is refused, because precedence is not decidable here.

    Measured on 5.1: a fresh ``powershell -Command`` session has `Utility` loaded
    and `Management` not, so a planted `Set-Content.cmd` on `PATH` DOES win there.
    But `Core` is always loaded, and one earlier `Management`/`Utility` command in
    the same line auto-loads that module and flips every later name in it. The
    name alone cannot say which happened, so the class is refused.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "Set-Content",  # Management, auto-loaded by any earlier inert name
            "Invoke-WebRequest",  # Utility, ditto
            "Where-Object",  # Core -- always loaded, always beats a file
            "New-Item",
            "Remove-Item",
        ],
    )
    def test_a_cmdlet_name_is_refused_even_with_a_system_file_of_that_name(self, win, name):
        system_dir, _, _ = win
        _file(system_dir, f"{name}.exe")
        refusal = name_grant.name_grant_refusal(f"{name} x")
        assert refusal is not None, name
        assert refusal.code == name_grant.BUILTIN_SHADOWS, name

    @pytest.mark.parametrize(
        "name", ["Invoke-Command", "Invoke-Expression", "Start-Process", "Invoke-Item"]
    )
    def test_a_dispatching_cmdlet_keeps_its_more_specific_refusal(self, win, name):
        # These are in the dispatcher table, which names the stronger reason:
        # whatever wins precedence, the command runs a program from its own
        # arguments. The cmdlet table must not mask that.
        system_dir, _, _ = win
        _file(system_dir, f"{name}.exe")
        refusal = name_grant.name_grant_refusal(f"{name} x")
        assert refusal is not None, name
        assert refusal.code == name_grant.DISPATCHER, name

    def test_the_refusal_precedes_the_search_path_walk(self, win):
        # Nothing of that name exists anywhere. The point of the table is that
        # the answer does not depend on whether a file happens to be there.
        assert name_grant.name_grant_refusal("Invoke-WebRequest u").code == (
            name_grant.BUILTIN_SHADOWS
        )

    @pytest.mark.parametrize(
        "command", ["Get-Content x", "Test-Path x", "Join-Path a b", "Select-String p x"]
    )
    def test_an_inert_cmdlet_name_is_still_allowed(self, win, command):
        assert name_grant.name_grant_refusal(command) is None

    def test_a_hyphenated_program_that_is_not_a_cmdlet_still_resolves(self, win):
        # The table must not cost every Verb-Noun-looking real program its
        # auto-approve: `docker-compose` is not a PowerShell command.
        system_dir, _, _ = win
        _file(system_dir, "docker-compose.exe")
        assert name_grant.name_grant_refusal("docker-compose up") is None

    def test_an_explicit_extension_is_not_a_cmdlet_name(self, win):
        # PowerShell resolves `set-content.exe` as a file, not as Set-Content.
        system_dir, _, _ = win
        _file(system_dir, "set-content.exe")
        assert name_grant.name_grant_refusal("set-content.exe x") is None

    def test_every_entry_is_a_lower_case_verb_noun(self):
        for name in name_grant._POWERSHELL_CORE_COMMANDS:
            assert name == name.lower(), name
            assert "-" in name, name
            assert " " not in name, name

    def test_the_inert_cmdlet_names_are_a_subset_of_the_refused_class(self):
        # The inert table EXEMPTS names from this refusal, so every hyphenated
        # entry in it must really be one of these commands -- a typo there would
        # otherwise let a real program of that name bypass resolution.
        #
        # `clear-host` is the one exception by construction: on 5.1 it is a
        # global session function, not a module export, so it is absent from the
        # measured module set while still being a built-in.
        hyphenated = {n for n in name_grant._WINDOWS_INERT_BUILTINS if "-" in n}
        assert hyphenated - name_grant._POWERSHELL_CORE_COMMANDS == {"clear-host"}

    def test_the_cmdlet_table_and_the_alias_table_do_not_overlap(self):
        # Aliases are short forms; a name in both would mean one of the two
        # tables was measured wrong.
        assert not (name_grant._POWERSHELL_CORE_COMMANDS & name_grant._POWERSHELL_DEFAULT_ALIASES)


class TestSystemAndWitness:
    """The trusted-directory and pin rules, on Windows spellings."""

    def test_a_system_program_needs_no_witness(self, win):
        system_dir, _, _ = win
        _file(system_dir, "find.exe")
        assert name_grant.name_grant_refusal("find /c x nul") is None
        assert name_grant.name_grant_refusal("FIND /c x nul") is None

    def test_a_planted_copy_ahead_of_system32_is_shadowing(self, win):
        system_dir, user_dir, _ = win
        _file(system_dir, "find.exe")
        shim = _file(user_dir, "find.exe")
        refusal = name_grant.name_grant_refusal("find /c x nul")
        assert refusal is not None
        assert refusal.code == name_grant.SHADOWED
        assert shim in refusal.detail

    def test_a_non_system_program_needs_a_witness_and_shares_the_pin_across_spellings(self, win):
        _, user_dir, _ = win
        _file(user_dir, "git.exe")
        refusal = name_grant.name_grant_refusal("git status")
        assert refusal is not None
        assert refusal.code == name_grant.UNWITNESSED
        name_grant.pin_human_approval("git status")
        # PowerShell treats these as the same file, so the pin must too.
        for spelling in ("git status", "Git status", "GIT.EXE status", "git.exe status"):
            assert name_grant.name_grant_refusal(spelling) is None, spelling

    def test_a_swapped_file_loses_the_pin(self, win):
        _, user_dir, _ = win
        path = _file(user_dir, "git.exe")
        name_grant.pin_human_approval("git status")
        assert name_grant.name_grant_refusal("git status") is None
        with open(path, "ab") as handle:
            handle.write(b"replaced\n")
        refusal = name_grant.name_grant_refusal("git status")
        assert refusal is not None
        assert refusal.code == name_grant.IDENTITY_CHANGED

    def test_a_program_in_an_agent_writable_tree_is_refused(self, win, monkeypatch):
        _, user_dir, _ = win
        _file(user_dir, "git.exe")
        monkeypatch.setattr(
            name_grant, "_agent_writable_roots", lambda: (os.path.normcase(str(user_dir)),)
        )
        name_grant.pin_human_approval("git status")
        refusal = name_grant.name_grant_refusal("git status")
        assert refusal is not None
        assert refusal.code == name_grant.AGENT_TREE

    @pytest.mark.parametrize(
        "command",
        [
            "POWERSHELL.EXE -c x",
            "Cmd /c dir",
            "wmic process call create x",
            "iex 'x'",
            "Start-Process x",
        ],
    )
    def test_dispatchers_match_case_insensitively_with_or_without_extension(self, win, command):
        system_dir, _, _ = win
        _file(system_dir, "wmic.exe")
        refusal = name_grant.name_grant_refusal(command)
        assert refusal is not None
        assert refusal.code == name_grant.DISPATCHER, command

    @pytest.mark.parametrize("program", ["iex", "start", "wmic"])
    def test_a_windows_only_dispatcher_by_name_is_refused(self, win, program):
        # The by-name consult site (the one that reads the token as written)
        # refuses these on Windows, so a resolvable file on disk cannot get
        # `iex` / `start` / `wmic` past this check.
        system_dir, _, _ = win
        _file(system_dir, f"{program}.exe")
        refusal = name_grant.name_grant_refusal(f"{program} x")
        assert refusal is not None
        assert refusal.code == name_grant.DISPATCHER

    @pytest.mark.parametrize("program", ["iex", "start", "wmic"])
    def test_a_windows_only_dispatcher_by_resolved_basename_is_refused(self, win, program):
        # The by-resolved-file consult site catches an ALIAS that spells its way
        # past the by-name site: `runner` resolves to a file whose basename IS a
        # dispatcher, so the rule has to be asked of the file too. Called
        # directly because a Windows symlink needs administrative privileges
        # (the POSIX alias-of-a-dispatcher test uses ``symlink_to`` for the
        # same shape).
        system_dir, _, _ = win
        target = _file(system_dir, f"{program}.exe")
        refusal = name_grant._dispatcher_target_refusal("runner", target, as_interpreter=False)
        assert refusal is not None
        assert refusal.code == name_grant.DISPATCHER

    def test_the_windows_dispatcher_table_stays_disjoint_from_the_shared_one(self):
        # Guards against a future edit dropping a Windows-only name back into
        # `_DISPATCHERS`, which would refuse it on POSIX too (where `iex` is
        # Elixir's REPL, `start` is a program a user is free to install, etc.).
        assert name_grant._DISPATCHERS.isdisjoint(name_grant._WINDOWS_DISPATCHERS)

    @pytest.mark.parametrize("program", ["iex", "start", "wmic"])
    def test_a_windows_dispatcher_name_is_absent_from_the_shared_table(self, program):
        assert program not in name_grant._DISPATCHERS
        assert program in name_grant._WINDOWS_DISPATCHERS

    @pytest.mark.parametrize("command", [r".\tool.exe", r"\tool.exe", "C:tool.exe", r"..\tool.exe"])
    def test_a_program_named_relative_to_a_working_directory_is_refused(self, win, command):
        refusal = name_grant.name_grant_refusal(command)
        assert refusal is not None
        assert refusal.code == name_grant.RELATIVE_PATH

    @pytest.mark.parametrize("command", ["%COMSPEC% /c x", "^git status", "@args"])
    def test_a_program_token_the_shell_rewrites_is_refused(self, win, command):
        refusal = name_grant.name_grant_refusal(command)
        assert refusal is not None
        assert refusal.code == name_grant.EXPANDED

    def test_an_unknown_bare_word_is_refused(self, win):
        # A word no cmdlet, alias, function or file answers to. `Remove-Item`
        # cannot stand in here: it is a Management cmdlet, so the cmdlet table
        # refuses it under BUILTIN_SHADOWS before this branch is reached.
        refusal = name_grant.name_grant_refusal("kcprobe-nosuchprogram x")
        assert refusal is not None
        assert refusal.code == name_grant.UNKNOWN_COMMAND

    def test_one_file_reached_by_two_spellings_has_one_identity(self, win, tmp_path):
        # The resolver answers with the spelling the caller asked for, and a
        # case-insensitive model reaches one file through several spellings, so
        # the path element of the identity folds through the model's own case
        # rule. A raw path gives one file as many identities as it has
        # spellings, and a pin recorded under one spelling then fails to match
        # the same file asked for under another.
        program = tmp_path / "Kcprobe.exe"
        program.write_bytes(b"MZ\x00probe")
        asked = str(program)
        identity = name_grant._identity(asked)
        assert identity is not None
        assert identity[0] == name_grant._model_normcase(asked)

        # Where the host filesystem is itself case-insensitive, the second
        # spelling reaches the same file and must produce the same identity.
        other = str(tmp_path / "kcprobe.exe")
        if os.path.isfile(other):
            assert name_grant._identity(other) == identity


@pytest.mark.skipif(not platform_compat.IS_WINDOWS, reason="drive-letter absolute paths")
class TestAbsolutePathsNative:
    """The absolute-path branch on a real ``ntpath``."""

    def test_the_system_program_by_full_path_needs_no_witness(self, win):
        system_dir, _, _ = win
        path = _file(system_dir, "whoami.exe")
        assert name_grant.name_grant_refusal(f"{path} /user") is None

    def test_an_absolute_association_script_is_refused(self, win):
        _, user_dir, _ = win
        path = _file(user_dir, "tool.py", b"print('hi')\n")
        refusal = name_grant.name_grant_refusal(f"{path} run")
        assert refusal is not None
        assert refusal.code == name_grant.FILE_ASSOCIATION

    def test_an_absolute_non_system_program_needs_a_witness(self, win):
        _, user_dir, _ = win
        path = _file(user_dir, "tool.exe")
        refusal = name_grant.name_grant_refusal(f"{path} run")
        assert refusal is not None
        assert refusal.code == name_grant.UNWITNESSED
        name_grant.pin_human_approval(f"{path} run")
        assert name_grant.name_grant_refusal(f"{path} run") is None

    def test_the_real_documents_folder_resolves(self):
        # The Known Folder API answers on a real Windows session; the profile
        # paths PowerShell 5.1 reads are derived from it -- one all-hosts and
        # one host-specific per profile directory, and only the 5.1 directory
        # matters because that is the shell kiro-cli spawns.
        paths = platform_compat.windows_powershell_profile_paths()
        assert paths is not None
        assert len(paths) == len(platform_compat._POWERSHELL_USER_PROFILES)
        assert all(os.path.splitdrive(p)[0] for p in paths)


class TestOffLoop:
    """Windows does filesystem work, so it takes the worker thread like every platform."""

    def test_windows_is_not_answered_on_the_loop(self):
        source = inspect.getsource(name_grant.refusal_for_command_off_loop)
        assert "IS_WINDOWS" not in source
        assert "asyncio.to_thread(name_grant_refusal" in source

    def test_the_off_loop_entry_gives_the_windows_verdict(self, win):
        system_dir, _, _ = win
        _file(system_dir, "find.exe")
        assert asyncio.run(name_grant.refusal_for_command_off_loop("find /c x nul")) is None
        refusal = asyncio.run(name_grant.refusal_for_command_off_loop("where git"))
        assert refusal is not None
        assert refusal.code == name_grant.BUILTIN_SHADOWS
