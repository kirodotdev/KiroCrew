"""Tests for the web terminal's subcommand/flag completion engine.

The security-relevant claim about this module is the SHAPE of the command line it
executes, and `_probe_argv` is deliberately separated from execution so that claim
can be asserted without spawning anything. `TestProbeArgv` is where that lives.

No test here runs a real `gh`/`git`; the protocol parsers are fed captured output.
"""

import asyncio
import contextlib
import os
import sys
import threading
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.dashboard import terminal_commands as tc


@pytest.fixture(autouse=True)
def _clear_cache():
    """The listing cache is module state; a leaked entry would let one test's
    answer satisfy another's probe."""
    tc.reset_cache()
    yield
    tc.reset_cache()


def _stat_as_root_with_mode(mode: int):
    """`os.stat` that reports uid 0 and a fixed mode, so a mode-based refusal can be
    tested without needing root to create the file."""
    real = os.stat

    def fake(path, *a, **kw):
        st = real(path, *a, **kw)
        return os.stat_result((mode, st.st_ino, st.st_dev, st.st_nlink, 0, 0,
                               st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
    return fake


class TestParseArgv:
    def test_accepts_a_plain_command_line(self):
        assert tc.parse_argv(["gh", "pr"]) == ["gh", "pr"]

    def test_keeps_flag_words(self):
        # Cobra's position in its own tree depends on them.
        assert tc.parse_argv(["kubectl", "-n", "kube-system", "get"]) == [
            "kubectl", "-n", "kube-system", "get",
        ]

    @pytest.mark.parametrize("raw", [None, [], "gh", {}, 42])
    def test_refuses_a_non_list_or_empty_argv(self, raw):
        assert tc.parse_argv(raw) is None

    def test_refuses_a_non_string_word(self):
        assert tc.parse_argv(["gh", 7]) is None

    def test_refuses_more_words_than_a_command_line_has(self):
        assert tc.parse_argv(["gh"] * (tc.ARGV_MAX_WORDS + 1)) is None

    def test_refuses_an_overlong_word(self):
        assert tc.parse_argv(["gh", "x" * (tc.ARGV_MAX_WORD_LEN + 1)]) is None

    @pytest.mark.parametrize("bad", ["gh\n", "gh\x00", "gh\x1b[31m", "gh\udcff"])
    def test_refuses_control_characters_and_lone_surrogates(self, bad):
        # A word carrying these cannot have come from a command line the user is
        # really editing, and it must never reach argv.
        assert tc.parse_argv([bad]) is None
        assert tc.parse_argv(["gh", bad]) is None

    @pytest.mark.parametrize(
        "name",
        [
            "./gh",            # relative path — must resolve via PATH, never cwd
            "/usr/bin/gh",     # absolute path
            "../gh",
            "sub/gh",
            "-gh",             # leading dash
            "",
            "gh;id",           # shell metacharacter
            "gh id",
            "$gh",
            "g" * 65,          # beyond the name cap
        ],
    )
    def test_refuses_a_command_name_that_is_not_a_bare_name(self, name):
        # Refusing a path is what stops a planted `./gh` in the session's cwd from
        # ever being the file that runs.
        assert tc.parse_argv([name]) is None


class TestProtocolFor:
    def test_maps_a_known_command(self):
        assert tc.protocol_for("gh") == tc.PROTOCOL_COBRA
        assert tc.protocol_for("git") == tc.PROTOCOL_GIT

    def test_refuses_an_unlisted_command(self):
        # The allowlist is the load-bearing control: `make __complete` would build
        # a target called `__complete`.
        assert tc.protocol_for("make") is None
        assert tc.protocol_for("rm") is None

    def test_an_operator_may_not_ADD_a_command(self):
        # Config can re-point an allowlisted tool but not widen the allowlist.
        # Mapping `python3` to cobra would make the probe run `python3 __complete ""`,
        # and python3 treats argv[1] as a FILE — so an agent-created `__complete` in
        # the session's directory would execute the moment the user typed `python3`.
        assert tc.protocol_for("mytool", {"mytool": "cobra"}) is None
        assert tc.protocol_for("python3", {"python3": "cobra"}) is None
        assert tc.protocol_for("make", {"make": "cobra"}) is None

    def test_an_operator_may_repoint_a_command(self):
        assert tc.protocol_for("gh", {"gh": "git"}) == tc.PROTOCOL_GIT

    @pytest.mark.parametrize("bad", [{"gh": "shell"}, {"gh": 1}, {"gh": None}, "nope"])
    def test_an_unimplemented_protocol_is_ignored_not_obeyed(self, bad):
        # A config value naming something arbitrary must not become an opt-in to
        # running it; the built-in answer stands.
        assert tc.protocol_for("gh", bad) == tc.PROTOCOL_COBRA

    def test_an_unimplemented_protocol_does_not_add_a_command(self):
        assert tc.protocol_for("make", {"make": "shell"}) is None


class TestProbeArgv:
    """The exact command line that will be executed — asserted without executing.

    This is the module's security surface: every entry below is a claim that the
    probe is the same call the tool's own Tab-completion script makes.
    """

    def test_cobra_subcommands_pass_the_empty_word_explicitly(self):
        # Cobra requires the word being completed as a final argument; the empty
        # string is what asks for "everything at this position", which is what lets
        # one probe answer every keystroke of a prefix.
        assert tc._probe_argv("/b/gh", tc.PROTOCOL_COBRA, ["gh", "pr"], False) == [
            "/b/gh", "__complete", "pr", "",
        ]

    def test_cobra_flags_use_the_double_dash_sentinel(self):
        assert tc._probe_argv("/b/gh", tc.PROTOCOL_COBRA, ["gh", "pr", "create"], True) == [
            "/b/gh", "__complete", "pr", "create", "--",
        ]

    def test_cobra_at_the_top_level(self):
        assert tc._probe_argv("/b/gh", tc.PROTOCOL_COBRA, ["gh"], False) == [
            "/b/gh", "__complete", "",
        ]

    def test_git_subcommands_use_list_cmds(self):
        argv = tc._probe_argv("/b/git", tc.PROTOCOL_GIT, ["git"], False)
        assert argv == ["/b/git", f"--list-cmds={tc._GIT_LIST_GROUPS}"]

    def test_git_flag_completion_is_refused_outright(self):
        # Regression, and a security boundary: `git <sub> --git-completion-helper`
        # reaches a builtin's parse-options only when `<sub>` IS a builtin. For an
        # alias, git expands it first — and a `!` alias body is a shell command git
        # runs via `sh -c`. So probing `git wipe --` for a user with
        # `alias.wipe = "!git reset --hard && git clean -xfd"` would EXECUTE it, in
        # the session's own working tree. No probe is made at all.
        assert tc._probe_argv("/b/git", tc.PROTOCOL_GIT, ["git", "commit"], True) is None
        assert tc._probe_argv("/b/git", tc.PROTOCOL_GIT, ["git", "wipe"], True) is None
        assert tc._probe_argv("/b/git", tc.PROTOCOL_GIT, ["git"], True) is None

    def test_no_probe_argv_ever_names_the_completion_helper(self):
        # Belt and braces on the above: whatever the position, the removed protocol
        # must not reappear anywhere in a built command line.
        for argv in (
            ["git"], ["git", "commit"], ["git", "-c", "x=y", "commit"],
            ["git", "remote"], ["git", "wipe"],
        ):
            for flags in (True, False):
                built = tc._probe_argv("/b/git", tc.PROTOCOL_GIT, argv, flags)
                assert built is None or "--git-completion-helper" not in built

    def test_refuses_to_probe_when_the_line_redirects_the_tool(self):
        # A cobra completer at a leaf position makes a live request, and these flags
        # choose WHERE it goes and WHO as. `kubectl --server=<url> get pod ⎸` would
        # otherwise have a keystroke send an unsolicited request to an arbitrary host
        # with the gateway as the client.
        for argv in (
            ["kubectl", "--server=https://evil.example", "get", "pod"],
            ["kubectl", "--server", "https://evil.example", "get"],
            ["kubectl", "--kubeconfig=/tmp/x", "get"],
            ["kubectl", "--token=abc", "get"],
            ["gh", "--hostname=evil.example", "pr"],
            ["docker", "-H", "tcp://evil.example:2375", "ps"],
            ["kubectl", "--insecure-skip-tls-verify", "get"],
            # Short forms: bare, with `=`, with an ATTACHED value, and clustered.
            # The attached case is the one an exact stem match let through, because
            # `-shttp://host` reads as the stem `shttp://host`.
            ["kubectl", "-s", "https://evil.example", "get"],
            ["kubectl", "-s=https://evil.example", "get"],
            ["kubectl", "-shttp://evil.example", "get", "pods"],
            ["docker", "-itH", "tcp://evil.example:2375", "ps"],
            # Single-dash long form, as some tools spell it.
            ["kubectl", "-server=https://evil.example", "get"],
        ):
            assert tc._probe_argv("/b/x", tc.PROTOCOL_COBRA, argv, False) is None, argv

    def test_ordinary_flags_still_probe(self):
        # The refusal must not be so broad that a normal line stops completing.
        assert tc._probe_argv("/b/gh", tc.PROTOCOL_COBRA, ["gh", "--json", "pr"], False) == [
            "/b/gh", "__complete", "--json", "pr", "",
        ]
        for argv in (
            ["gh", "-q", "pr"],
            ["docker", "-it", "run"],
            ["kubectl", "-Xmx512m", "get"],   # attached value with no dangerous letter
            ["gh", "--", "pr"],
            ["gh", "-", "pr"],
        ):
            assert tc._probe_argv("/b/x", tc.PROTOCOL_COBRA, argv, False) is not None, argv

    def test_git_subcommands_still_work(self):
        # The fix is scoped to flags; the subcommand protocol is untouched.
        assert tc._probe_argv("/b/git", tc.PROTOCOL_GIT, ["git"], False) == [
            "/b/git", f"--list-cmds={tc._GIT_LIST_GROUPS}",
        ]

    def test_git_has_no_source_for_nested_subcommands(self):
        # `git remote ⎸` has no protocol that enumerates `add`/`remove`, and
        # guessing would be worse than answering nothing.
        assert tc._probe_argv("/b/git", tc.PROTOCOL_GIT, ["git", "remote"], False) is None

    def test_never_involves_a_shell(self):
        # Every probe is an argv LIST handed to execve. Nothing here is a string a
        # shell would re-split, which is why a metacharacter in a half-typed word
        # has nothing to escape into.
        for argv in (
            tc._probe_argv("/b/gh", tc.PROTOCOL_COBRA, ["gh", "pr"], False),
            tc._probe_argv("/b/git", tc.PROTOCOL_GIT, ["git"], False),
        ):
            assert isinstance(argv, list)
            assert not any(w in ("sh", "-c", "bash", "/bin/sh") for w in argv)


class TestSanitizedPath:
    def test_drops_relative_and_empty_entries(self, monkeypatch):
        # Trusted-chain stubbed for the same reason as the project-local test: this
        # one's subject is the empty/relative filter, and `/usr/bin` does not exist
        # on Windows, so without the stub it asserts host layout instead.
        monkeypatch.setattr(tc, "_is_trusted_dir", lambda _d: True)
        # An empty or relative PATH entry means "the current directory", and this
        # process's cwd is not the user's — resolving a command name there would let
        # a planted executable run with gateway privileges.
        monkeypatch.setenv("PATH", f"/usr/bin{os.pathsep}{os.pathsep}.{os.pathsep}rel/bin")
        assert tc._sanitized_path() == "/usr/bin"

    def test_returns_none_when_nothing_absolute_survives(self, monkeypatch):
        monkeypatch.setenv("PATH", f".{os.pathsep}rel")
        assert tc._sanitized_path() is None

    def test_returns_none_for_an_unset_path(self, monkeypatch):
        monkeypatch.delenv("PATH", raising=False)
        assert tc._sanitized_path() is None

    def test_drops_project_local_tool_directories(self, monkeypatch):
        # These are on PATH whenever a workspace environment is active and are
        # writable by anything that can write the project — including the agent. A
        # planted `gh` there would run the moment the user types `gh`, before Enter.
        #
        # The trusted-chain test is stubbed out so this isolates its own subject.
        # Asserting against real system directories made it depend on host
        # ownership, and it broke on CI runners where `/usr/local/bin` is not
        # root-owned — a true fact about the runner, not about this filter.
        monkeypatch.setattr(tc, "_is_trusted_dir", lambda _d: True)
        monkeypatch.setenv("PATH", os.pathsep.join([
            "/usr/bin",
            "/home/u/proj/.venv/bin",
            "/home/u/proj/node_modules/.bin",
            "/home/u/proj/.tox/py312/bin",
            "/usr/local/bin",
        ]))
        assert tc._sanitized_path() == f"/usr/bin{os.pathsep}/usr/local/bin"

    def test_matches_project_segments_wholesale_not_as_substrings(self):
        # `/opt/venv-tools/bin` is an installed prefix that merely CONTAINS the
        # text; `/home/u/proj/.venv/bin` genuinely is project-local.
        assert tc._is_project_local("/home/u/proj/.venv/bin") is True
        assert tc._is_project_local("/home/u/p/node_modules/.bin") is True
        assert tc._is_project_local("/opt/venv-tools/bin") is False
        assert tc._is_project_local("/usr/lib/build-helpers") is False

    def test_matches_either_separator_on_any_host(self):
        # Regression: splitting on `os.sep` alone made this silently stop matching
        # POSIX-shaped paths on Windows — a security filter that quietly returns
        # False while its own tests still pass on the authoring host.
        assert tc._is_project_local(r"C:\Users\u\proj\.venv\Scripts") is True
        assert tc._is_project_local(r"C:\proj\node_modules\.bin") is True
        assert tc._is_project_local("/home/u/proj/.venv/bin") is True
        assert tc._is_project_local(r"C:\Program Files\git\cmd") is False

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="plants an extensionless executable; Windows resolves via PATHEXT",
    )
    def test_refuses_a_binary_whose_target_is_project_local(self, tmp_path, monkeypatch):
        # A symlink in an allowed prefix pointing into a project tree would
        # otherwise reintroduce exactly what the PATH filter removes — and the
        # TARGET is the file that executes.
        proj = tmp_path / "proj" / ".venv" / "bin"
        proj.mkdir(parents=True)
        planted = proj / "gh-real"
        planted.write_text("#!/bin/sh\n")
        planted.chmod(0o755)
        sysbin = tmp_path / "sysbin"
        sysbin.mkdir()
        (sysbin / "gh").symlink_to(planted)
        monkeypatch.setenv("PATH", str(sysbin))
        assert tc._resolve("gh") is None

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="plants an extensionless executable; Windows resolves via PATHEXT",
    )
    def test_refuses_an_ordinary_user_install_by_default(self, tmp_path, monkeypatch):
        # The deliberate cost of the trusted-chain rule: `~/.local/bin` is owned by
        # the user, so a `gh` there is NOT probed until an operator says so. The
        # probe fires mid-keystroke, so a binary in any writable directory would run
        # before the user could decide not to run it.
        binder = tmp_path / "home" / ".local" / "bin"
        binder.mkdir(parents=True)
        real = binder / "gh"
        real.write_text("#!/bin/sh\n")
        real.chmod(0o755)
        monkeypatch.setenv("PATH", str(binder))
        assert tc._resolve("gh") is None

    @pytest.mark.skipif(sys.platform == "win32", reason="uid 0 has no meaning on Windows")
    def test_a_system_directory_is_trusted_without_any_config(self):
        # `/usr/bin` must work out of the box or the tier ships dead. Deliberately
        # NOT `/usr/local/bin`: CI runners hand that to the build user, so asserting
        # it would test the host rather than the predicate.
        assert tc._is_trusted_dir("/usr/bin") is True

    @pytest.mark.skipif(sys.platform == "win32", reason="uid 0 has no meaning on Windows")
    def test_a_user_owned_directory_is_not_trusted(self, tmp_path):
        d = tmp_path / "mine"
        d.mkdir()
        assert tc._is_trusted_dir(str(d)) is False

    @pytest.mark.skipif(sys.platform == "win32", reason="uid 0 has no meaning on Windows")
    def test_the_whole_chain_must_be_trusted_not_just_the_leaf(self, tmp_path):
        # A root-owned `bin` inside a user-writable parent can simply be swapped for
        # a different directory, so the check walks upward — the same reasoning
        # sudo's secure-path handling applies.
        leaf = tmp_path / "parent" / "bin"
        leaf.mkdir(parents=True)
        assert tc._is_trusted_dir(str(leaf)) is False

    def test_a_missing_directory_is_not_trusted(self, tmp_path):
        assert tc._is_trusted_dir(str(tmp_path / "nope")) is False

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="plants an extensionless executable; Windows resolves via PATHEXT",
    )
    @pytest.mark.skipif(sys.platform == "win32", reason="uid 0 has no meaning on Windows")
    def test_refuses_a_user_owned_file_inside_a_trusted_directory(self, tmp_path, monkeypatch):
        # A root-owned directory cannot receive a NEW file from a non-root user, but
        # a file already inside it can still be user-owned or group/world-writable.
        # The directory's trust does not transfer to its contents.
        planted = tmp_path / "gh"
        planted.write_text("#!/bin/sh\n")
        planted.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))
        # Directory check stubbed to isolate the FILE check as the subject.
        monkeypatch.setattr(tc, "_is_trusted_dir", lambda _d: True)
        assert tc._resolve("gh") is None

    @pytest.mark.skipif(sys.platform == "win32", reason="uid 0 has no meaning on Windows")
    def test_refuses_a_group_writable_system_binary(self, tmp_path, monkeypatch):
        # Root-owned but mode 775: anyone in the group can replace it.
        planted = tmp_path / "gh"
        planted.write_text("#!/bin/sh\n")
        planted.chmod(0o775)
        monkeypatch.setattr(tc, "_is_trusted_dir", lambda _d: True)
        monkeypatch.setattr(tc.os, "stat", _stat_as_root_with_mode(0o100775))
        monkeypatch.setenv("PATH", str(tmp_path))
        assert tc._resolve("gh") is None

    def test_resolution_uses_the_sanitized_path(self, tmp_path, monkeypatch):
        planted = tmp_path / "gh"
        planted.write_text("#!/bin/sh\n")
        planted.chmod(0o755)
        # The directory holding the plant is reachable only as a RELATIVE entry.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PATH", ".")
        assert tc._resolve("gh") is None

    def test_an_unusable_path_fails_closed_rather_than_falling_back(self, monkeypatch):
        # Regression: `shutil.which(cmd, path=None)` falls back to
        # `os.environ["PATH"]`, so handing it the sanitizer's None would silently
        # restore the relative entries the sanitizer just removed. `_resolve` must
        # refuse instead of calling `which` at all.
        monkeypatch.setattr(tc, "_sanitized_path", lambda *_a: None)
        called = []
        monkeypatch.setattr(tc.shutil, "which", lambda *a, **k: called.append(a) or "/bin/gh")
        assert tc._resolve("gh") is None
        assert called == []


class TestIsCommandToken:
    @pytest.mark.parametrize("name", ["pr", "dry-run", "run:build", "v2.0", "image_ls"])
    def test_accepts_subcommand_shapes(self, name):
        assert tc.is_command_token(name, False) is True

    @pytest.mark.parametrize("name", ["-v", "--repo", "--dry-run", "--message="])
    def test_accepts_flag_shapes(self, name):
        assert tc.is_command_token(name, True) is True

    @pytest.mark.parametrize(
        "name", ["--x; rm -rf ~", "$(id)", "`id`", "a b", "a\nb", "", "--", "a|b", "a>b"]
    )
    def test_refuses_anything_a_shell_would_reinterpret(self, name):
        # The client types a command token VERBATIM, so a value needing escaping is
        # not a real flag and is refused rather than escaped.
        assert tc.is_command_token(name, True) is False
        assert tc.is_command_token(name, False) is False

    def test_holds_each_kind_to_its_own_shape(self):
        assert tc.is_command_token("--repo", False) is False
        assert tc.is_command_token("pr", True) is False


class TestParseCobra:
    #: Captured verbatim from `gh __complete pr ""` (gh 2.96.0).
    GH_PR = (
        "checkout\tCheck out a pull request in git\n"
        "close\tClose a pull request\n"
        "create\tCreate a pull request\n"
        ":4\n"
    )

    def test_reads_names_and_descriptions(self):
        entries = tc.parse_cobra(self.GH_PR, want_flags=False)
        assert [(e.name, e.desc) for e in entries] == [
            ("checkout", "Check out a pull request in git"),
            ("close", "Close a pull request"),
            ("create", "Create a pull request"),
        ]
        assert all(e.flag is False for e in entries)

    def test_preserves_the_tools_own_order(self):
        # Cobra emits a meaningful order; re-sorting would discard it.
        assert [e.name for e in tc.parse_cobra(self.GH_PR, False)] == [
            "checkout", "close", "create",
        ]

    def test_stops_at_the_directive_line(self):
        out = "a\tone\n:4\nb\ttwo\n"
        assert [e.name for e in tc.parse_cobra(out, False)] == ["a"]

    def test_an_entry_without_a_description_is_kept(self):
        assert [(e.name, e.desc) for e in tc.parse_cobra("plain\n:4\n", False)] == [
            ("plain", ""),
        ]

    def test_reads_flags_when_asked(self):
        out = "--repo\tSelect another repository\n--json\tOutput JSON\n:4\n"
        entries = tc.parse_cobra(out, want_flags=True)
        assert [e.name for e in entries] == ["--repo", "--json"]
        assert all(e.flag is True for e in entries)

    def test_drops_values_of_the_wrong_shape_for_the_request(self):
        # A completer is free to return either kind; filtering by shape means a
        # stray value cannot land in the wrong menu, where the client would then
        # type it under the wrong rules.
        out = "pr\tsub\n--repo\tflag\n:4\n"
        assert [e.name for e in tc.parse_cobra(out, want_flags=False)] == ["pr"]
        assert [e.name for e in tc.parse_cobra(out, want_flags=True)] == ["--repo"]

    def test_drops_a_value_that_is_not_a_protocol_shaped_token(self):
        # Would otherwise be cached and re-served for the whole TTL.
        out = "ok\tfine\nrm -rf ~\thostile\n:4\n"
        assert [e.name for e in tc.parse_cobra(out, False)] == ["ok"]

    def test_honours_the_error_directive_despite_a_zero_exit(self):
        # Cobra exits 0 even when it failed, so the directive bit is the only
        # reliable signal — this is what stops `gh notreal ⎸` producing nonsense.
        assert tc.parse_cobra("junk\tx\n:1\n", False) == []

    def test_an_error_bit_inside_a_combined_directive_still_refuses(self):
        assert tc.parse_cobra("junk\tx\n:5\n", False) == []

    def test_carries_the_no_space_directive(self):
        entries = tc.parse_cobra("--repo\tx\n:2\n", want_flags=True)
        assert [e.nospace for e in entries] == [True]

    def test_no_space_is_absent_by_default(self):
        assert [e.nospace for e in tc.parse_cobra("pr\tx\n:4\n", False)] == [False]

    def test_a_missing_directive_line_is_tolerated(self):
        # Not every cobra version emits one on every path.
        assert [e.name for e in tc.parse_cobra("pr\tx\n", False)] == ["pr"]

    def test_an_unparsable_directive_is_treated_as_default(self):
        assert [e.name for e in tc.parse_cobra("pr\tx\n:notanint\n", False)] == ["pr"]

    def test_empty_output_yields_nothing(self):
        assert tc.parse_cobra("", False) == []
        assert tc.parse_cobra(":0\n", False) == []

    def test_caps_the_entry_count(self):
        out = "".join(f"s{i}\td\n" for i in range(tc.ENTRIES_MAX + 50)) + ":4\n"
        assert len(tc.parse_cobra(out, False)) == tc.ENTRIES_MAX


class TestParseGit:
    def test_reads_bare_subcommand_names(self):
        entries = tc.parse_git_subcommands("add\nam\ncommit\n")
        assert [e.name for e in entries] == ["add", "am", "commit"]
        # `--list-cmds` supplies no descriptions, and inventing one would be worse.
        assert all(e.desc == "" and e.flag is False for e in entries)

    def test_deduplicates_names_across_groups(self):
        # The groups requested overlap, so the same command appears twice.
        assert [e.name for e in tc.parse_git_subcommands("commit\ncommit\n")] == ["commit"]

    def test_ignores_blank_and_malformed_lines(self):
        assert [e.name for e in tc.parse_git_subcommands("add\n\n  \n--x\nrm -rf\n")] == [
            "add",
        ]


class TestFilterEntries:
    ENTRIES = [
        tc.CmdEntry("checkout", "", False),
        tc.CmdEntry("checks", "", False),
        tc.CmdEntry("close", "", False),
        tc.CmdEntry("Create", "", False),
    ]

    def test_an_empty_prefix_keeps_everything(self):
        assert tc.filter_entries(self.ENTRIES, "") == self.ENTRIES

    def test_narrows_by_prefix(self):
        assert [e.name for e in tc.filter_entries(self.ENTRIES, "che")] == [
            "checkout", "checks",
        ]

    def test_is_case_insensitive(self):
        assert [e.name for e in tc.filter_entries(self.ENTRIES, "cr")] == ["Create"]

    def test_matches_a_prefix_not_a_substring(self):
        # The path tier searches substrings because a filename can be long and
        # reached by its middle. A subcommand is short and typed from the front, so
        # matching `m` inside `comment` would offer rows that look unrelated.
        assert tc.filter_entries(self.ENTRIES, "heck") == []

    def test_preserves_order(self):
        assert [e.name for e in tc.filter_entries(self.ENTRIES, "c")] == [
            "checkout", "checks", "close", "Create",
        ]


class TestVettedCwd:
    def test_passes_an_ordinary_directory_through_the_chokepoint(self, tmp_path):
        assert tc._vetted_cwd(str(tmp_path)) == os.path.realpath(str(tmp_path))

    def test_none_stays_none(self):
        assert tc._vetted_cwd(None) is None
        assert tc._vetted_cwd("") is None

    def test_refuses_a_sensitive_directory(self, tmp_path):
        with patch.object(tc, "validate_file_path", side_effect=ValueError):
            assert tc._vetted_cwd(str(tmp_path)) is None


class TestEntryWireForm:
    def test_a_subcommand_carries_kind_sub(self):
        assert tc.CmdEntry("pr", "Manage PRs", False).to_json() == {
            "name": "pr", "desc": "Manage PRs", "kind": "sub", "at": 0,
        }

    def test_a_flag_carries_kind_flag(self):
        assert tc.CmdEntry("--repo", "", True).to_json() == {
            "name": "--repo", "desc": "", "kind": "flag", "at": 0,
        }

    def test_the_match_offset_is_always_zero(self):
        # Command matching is a PREFIX match, so the typed span always starts at
        # the beginning — emitted rather than omitted so the client highlights it
        # with the same field the path tier uses.
        assert tc.CmdEntry("update-branch", "", False).to_json()["at"] == 0

    def test_nospace_is_emitted_only_when_set(self):
        # Absent rather than false, so the path tier's entry shape is unchanged for
        # a client that does not know the field.
        assert "nospace" not in tc.CmdEntry("pr", "", False).to_json()
        assert tc.CmdEntry("--m=", "", True, nospace=True).to_json()["nospace"] is True


class TestCompleteOrchestration:
    """`complete()` end to end, with the subprocess stubbed out."""

    @staticmethod
    def _resolved(path="/usr/bin/gh", cwd=None):
        return ((path, (path, 1, 2)), cwd)

    @pytest.mark.asyncio
    async def test_an_unlisted_command_is_never_probed(self):
        run = AsyncMock()
        with patch.object(tc, "_run_probe", run):
            entries, reason = await tc.complete(["make"], "", None)
        assert (entries, reason) == ([], "cmd_unknown")
        # The point of the allowlist: no process at all.
        run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lists_subcommands_for_an_allowlisted_command(self):
        with patch.object(tc, "_resolve_and_vet", return_value=self._resolved()), \
             patch.object(tc, "_run_probe", AsyncMock(return_value="pr\tPRs\n:4\n")):
            entries, reason = await tc.complete(["gh"], "", None)
        assert [e.name for e in entries] == ["pr"]
        assert reason == "cmd_listed"

    @pytest.mark.asyncio
    async def test_reports_cmd_none_when_the_tool_offers_nothing(self):
        with patch.object(tc, "_resolve_and_vet", return_value=self._resolved()), \
             patch.object(tc, "_run_probe", AsyncMock(return_value=":0\n")):
            entries, reason = await tc.complete(["gh"], "", None)
        assert (entries, reason) == ([], "cmd_none")

    @pytest.mark.asyncio
    async def test_a_command_not_on_path_is_indistinguishable_from_unlisted(self):
        # Both answer `cmd_unknown`: distinguishing them in the audit log would
        # disclose which tools are installed.
        with patch.object(tc, "_resolve_and_vet", return_value=(None, None)):
            assert await tc.complete(["gh"], "", None) == ([], "cmd_unknown")

    @pytest.mark.asyncio
    async def test_refuses_to_run_a_probe_in_a_sensitive_directory(self):
        # The session `cd`-ed somewhere this gateway will not read, so it will not
        # run a program there either.
        run = AsyncMock()
        with patch.object(tc, "_resolve_and_vet", return_value=(("/usr/bin/gh", ("x", 1, 2)), None)), \
             patch.object(tc, "_run_probe", run):
            entries, reason = await tc.complete(["gh"], "", "/home/u/.kiro/crew")
        assert (entries, reason) == ([], "sensitive_path")
        run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runs_the_probe_in_the_vetted_session_directory(self):
        # `gh` finds the repo from the cwd, so a probe that ignored it would answer
        # for the wrong repository.
        run = AsyncMock(return_value="pr\tx\n:4\n")
        with patch.object(tc, "_resolve_and_vet", return_value=self._resolved(cwd="/w/proj")), \
             patch.object(tc, "_run_probe", run):
            await tc.complete(["gh"], "", "/w/proj")
        assert run.await_args.args[1] == "/w/proj"

    @pytest.mark.asyncio
    async def test_narrows_a_cached_listing_without_a_second_probe(self):
        # The whole latency story: one subprocess per argv PATH, then every
        # keystroke of the prefix is in-process string filtering.
        run = AsyncMock(return_value="create\tmake one\ncheckout\tget one\n:4\n")
        with patch.object(tc, "_resolve_and_vet", return_value=self._resolved()), \
             patch.object(tc, "_run_probe", run):
            first, _ = await tc.complete(["gh", "pr"], "", None)
            second, _ = await tc.complete(["gh", "pr"], "cre", None)
            third, _ = await tc.complete(["gh", "pr"], "creat", None)
        assert len(first) == 2
        assert [e.name for e in second] == ["create"]
        assert [e.name for e in third] == ["create"]
        assert run.await_count == 1

    @pytest.mark.asyncio
    async def test_probes_again_for_a_different_position_in_the_tree(self):
        run = AsyncMock(return_value="x\td\n:4\n")
        with patch.object(tc, "_resolve_and_vet", return_value=self._resolved()), \
             patch.object(tc, "_run_probe", run):
            await tc.complete(["gh", "pr"], "", None)
            await tc.complete(["gh", "repo"], "", None)
        assert run.await_count == 2

    @pytest.mark.asyncio
    async def test_flags_and_subcommands_are_cached_separately(self):
        run = AsyncMock(return_value="--repo\td\n:4\n")
        with patch.object(tc, "_resolve_and_vet", return_value=self._resolved()), \
             patch.object(tc, "_run_probe", run):
            await tc.complete(["gh", "pr"], "", None)
            await tc.complete(["gh", "pr"], "--", None)
        assert run.await_count == 2

    @pytest.mark.asyncio
    async def test_a_new_binary_identity_invalidates_the_cache(self):
        # Upgrading the tool (or a version manager repointing a shim) must not keep
        # serving the previous version's subcommands.
        run = AsyncMock(return_value="pr\td\n:4\n")
        with patch.object(tc, "_run_probe", run):
            with patch.object(tc, "_resolve_and_vet",
                              return_value=(("/usr/bin/gh", ("/usr/bin/gh", 1, 2)), None)):
                await tc.complete(["gh"], "", None)
            with patch.object(tc, "_resolve_and_vet",
                              return_value=(("/usr/bin/gh", ("/usr/bin/gh", 99, 2)), None)):
                await tc.complete(["gh"], "", None)
        assert run.await_count == 2

    @pytest.mark.asyncio
    async def test_a_negative_result_is_cached_too(self):
        # Otherwise the failed probe is paid again on every keystroke.
        run = AsyncMock(return_value=None)
        with patch.object(tc, "_resolve_and_vet", return_value=self._resolved()), \
             patch.object(tc, "_run_probe", run):
            await tc.complete(["gh"], "", None)
            await tc.complete(["gh"], "", None)
        assert run.await_count == 1

    @pytest.mark.asyncio
    async def test_a_negative_result_expires_sooner_than_a_positive_one(self, monkeypatch):
        run = AsyncMock(return_value=None)
        clock = [1000.0]
        monkeypatch.setattr(tc.time, "monotonic", lambda: clock[0])
        with patch.object(tc, "_resolve_and_vet", return_value=self._resolved()), \
             patch.object(tc, "_run_probe", run):
            await tc.complete(["gh"], "", None)
            clock[0] += tc.CACHE_NEGATIVE_TTL_S + 1
            await tc.complete(["gh"], "", None)
        assert run.await_count == 2

    @pytest.mark.asyncio
    async def test_a_positive_result_survives_the_negative_ttl(self, monkeypatch):
        run = AsyncMock(return_value="pr\td\n:4\n")
        clock = [1000.0]
        monkeypatch.setattr(tc.time, "monotonic", lambda: clock[0])
        with patch.object(tc, "_resolve_and_vet", return_value=self._resolved()), \
             patch.object(tc, "_run_probe", run):
            await tc.complete(["gh"], "", None)
            clock[0] += tc.CACHE_NEGATIVE_TTL_S + 1
            await tc.complete(["gh"], "", None)
        assert run.await_count == 1

    @pytest.mark.asyncio
    async def test_the_cache_is_bounded(self):
        run = AsyncMock(return_value="pr\td\n:4\n")
        with patch.object(tc, "_resolve_and_vet", return_value=self._resolved()), \
             patch.object(tc, "_run_probe", run):
            for i in range(tc.CACHE_MAX_ENTRIES + 20):
                await tc.complete(["gh", f"sub{i}"], "", None)
        assert len(tc._cache) <= tc.CACHE_MAX_ENTRIES

    @pytest.mark.asyncio
    async def test_a_position_with_no_source_answers_nothing(self):
        run = AsyncMock()
        with patch.object(tc, "_resolve_and_vet",
                          return_value=(("/usr/bin/git", ("/usr/bin/git", 1, 2)), None)), \
             patch.object(tc, "_run_probe", run):
            entries, reason = await tc.complete(["git", "remote"], "", None)
        assert (entries, reason) == ([], "cmd_none")
        run.assert_not_awaited()


def _sandbox_backend_available() -> bool:
    """Whether this host can actually build the probe's sandbox.

    `sandboxed_spawn_argv` fails closed with `SandboxUnavailableError` where no
    backend exists — notably a GitHub runner, where AppArmor restricts
    unprivileged user namespaces. The tests below exercise probe MECHANICS
    (stdout capture, stdin EOF, timeout, cwd, caps), all of which need a child to
    actually run, so they are skipped there rather than asserting a degradation
    they are not about. `TestProbeWithoutASandbox` covers that degradation and
    runs everywhere.
    """
    try:
        _, _, cleanup = tc.sandboxed_spawn_argv(["/bin/true"], "strict")
    except Exception:
        return False
    if cleanup:
        with contextlib.suppress(OSError):
            os.unlink(cleanup)
    return True


_NEEDS_SANDBOX = pytest.mark.skipif(
    not _sandbox_backend_available(),
    reason="no OS sandbox backend on this host (probes fail closed)",
)


@pytest.mark.skipif(sys.platform == "win32", reason="probes are POSIX-only")
class TestProbeWithoutASandbox:
    """The host has no usable sandbox backend.

    Regression test for a real defect: `sandboxed_spawn_argv` was called OUTSIDE
    the probe's error handling, so on such a host the `SandboxUnavailableError`
    escaped the route and a completion keystroke became an HTTP 500. This tier's
    whole contract is that "no completions" is a normal answer, so an unavailable
    sandbox must degrade to no menu.

    Windows-skipped as a whole, not per-test: `_run_probe` returns before it
    reaches the sandbox at all there, so every assertion here would either pass
    VACUOUSLY (returning None for the wrong reason) or, for the off-loop check,
    fail because the code never runs.
    """

    @pytest.mark.asyncio
    async def test_an_unavailable_sandbox_yields_no_completions(self, monkeypatch):
        def boom(*_a, **_kw):
            raise tc.SandboxUnavailableError(
                "no backend here", "no_backend", "unshare(CLONE_NEWNS) failed", "",
            )

        monkeypatch.setattr(tc, "sandboxed_spawn_argv", boom)
        assert await tc._run_probe(["/bin/echo", "x"], None) is None

    @pytest.mark.asyncio
    async def test_it_never_spawns_when_the_sandbox_cannot_be_built(self, monkeypatch):
        # Fail closed in the literal sense: no child at all, rather than an
        # unsandboxed one.
        def boom(*_a, **_kw):
            raise tc.SandboxUnavailableError(
                "no backend here", "no_backend", "unshare(CLONE_NEWNS) failed", "",
            )

        spawned = []

        async def fake_spawn(*a, **kw):
            spawned.append(a)

        monkeypatch.setattr(tc, "sandboxed_spawn_argv", boom)
        monkeypatch.setattr(tc, "create_subprocess_limited", fake_spawn)
        await tc._run_probe(["/bin/echo", "x"], None)
        assert spawned == []

    @pytest.mark.asyncio
    async def test_the_whole_tier_reports_no_completions_not_an_error(self, monkeypatch):
        # End to end through `complete`, so the route sees the normal empty answer
        # and its fixed audit vocabulary, not an exception.
        def boom(*_a, **_kw):
            raise tc.SandboxUnavailableError(
                "no backend here", "no_backend", "unshare(CLONE_NEWNS) failed", "",
            )

        monkeypatch.setattr(tc, "sandboxed_spawn_argv", boom)
        monkeypatch.setattr(
            tc, "_resolve_and_vet",
            lambda *_a: (("/usr/bin/gh", ("/usr/bin/gh", 1, 2)), None),
        )
        assert await tc.complete(["gh"], "", None) == ([], "cmd_none")

    @pytest.mark.asyncio
    async def test_sandbox_preparation_runs_off_the_event_loop(self, monkeypatch):
        # Building the wrapper writes a temp launcher (`mkstemp`/`os.write`), which
        # on a slow filesystem would stall the gateway at keystroke rate.
        seen = {}
        real = tc.sandboxed_spawn_argv

        def spy(*a, **kw):
            seen["thread"] = threading.current_thread().name
            return real(*a, **kw)

        monkeypatch.setattr(tc, "sandboxed_spawn_argv", spy)
        monkeypatch.setattr(
            tc, "create_subprocess_limited", AsyncMock(side_effect=OSError("stop here")),
        )
        await tc._run_probe(["/bin/echo", "x"], None)
        assert "thread" in seen, "sandbox prep was never reached"
        assert seen["thread"] != threading.current_thread().name


@pytest.mark.skipif(sys.platform == "win32", reason="probes are POSIX-only")
@_NEEDS_SANDBOX
class TestRunProbe:
    """The real subprocess path, driven with stock POSIX utilities rather than a
    CLI that may not be installed on the runner."""

    @pytest.mark.asyncio
    async def test_captures_stdout(self):
        out = await tc._run_probe(["/bin/echo", "hello"], None)
        assert out == "hello\n"

    @pytest.mark.asyncio
    async def test_a_missing_binary_yields_no_completions(self):
        # At keystroke rate a tool that cannot be run simply has no completions,
        # and this must never raise. Note the sentinel: the sandbox launcher execs
        # fine and the INNER exec is what fails, so the observable result is empty
        # output rather than the spawn-time OSError an unwrapped exec would raise.
        # Both parse to no entries, which is the contract callers depend on — and
        # `_resolve` has already confirmed the binary exists before a probe is
        # even built, so this is the backstop, not the normal path.
        out = await tc._run_probe(["/nonexistent/tool", "x"], None)
        assert not out
        assert tc.parse_cobra(out or "", False) == []

    @pytest.mark.asyncio
    async def test_stdin_is_closed_so_a_prompting_probe_cannot_hang(self):
        # `cat` with no argument reads stdin; DEVNULL gives it EOF immediately.
        assert await tc._run_probe(["/bin/cat"], None) == ""

    @pytest.mark.asyncio
    async def test_stderr_never_reaches_the_output(self):
        # cobra writes a human-readable directive line to stderr.
        out = await tc._run_probe(
            ["/bin/sh", "-c", "echo out; echo noise >&2"], None,
        )
        assert out == "out\n"

    @pytest.mark.asyncio
    async def test_a_hung_probe_is_timed_out_and_reaped(self, monkeypatch):
        monkeypatch.setattr(tc, "PROBE_TIMEOUT_S", 0.3)
        assert await tc._run_probe(["/bin/sleep", "30"], None) is None

    @pytest.mark.asyncio
    async def test_runs_in_the_given_directory(self, tmp_path):
        out = await tc._run_probe(["/bin/pwd"], str(tmp_path))
        assert out is not None and out.strip() == os.path.realpath(str(tmp_path))

    @pytest.mark.asyncio
    async def test_routes_through_the_sandbox_at_the_strict_tier(self, monkeypatch):
        # The default `standard` tier deliberately leaves workflow credential dirs
        # visible (git-over-SSH, the AWS CLI). That is the wrong trade for a probe:
        # `kubectl --kubeconfig ~/.kube/config config use-context ⎸` would have it
        # read the protected config. A probe needs no credential to read a static
        # subcommand table, so the strictest tier costs nothing — this pins it so a
        # future edit cannot quietly drop back to the default.
        seen = {}

        def fake(argv, mode="standard", **kw):
            seen["mode"] = mode
            seen["argv"] = argv
            return ["/bin/echo", "x"], {}, None

        monkeypatch.setattr(tc, "sandboxed_spawn_argv", fake)
        await tc._run_probe(["/usr/bin/gh", "__complete", ""], None)
        assert seen["mode"] == "strict"
        # And the wrapper is handed the real probe argv, not a rebuilt one.
        assert seen["argv"] == ["/usr/bin/gh", "__complete", ""]

    @pytest.mark.asyncio
    async def test_the_child_environment_is_credential_scrubbed(self, monkeypatch):
        # The point of routing through the sandbox chokepoint: a completion probe is
        # agent-influenced argv running with gateway privileges, so it must not be
        # able to read the gateway's secrets straight out of the environment. The
        # static subcommand tables the tier needs live in the binary, so scrubbing
        # costs no correctness.
        name = "SSH_AUTH_SOCK"          # one of scrub_env's prefixes
        monkeypatch.setenv(name, "/tmp/should-not-be-visible.sock")
        out = await tc._run_probe(["/bin/sh", "-c", f'echo "[${name}]"'], None)
        assert out is not None
        assert "should-not-be-visible" not in out
        # Confirms the probe really did read the variable it was told to.
        assert out.strip() == "[]"

    @pytest.mark.asyncio
    async def test_the_environment_is_an_allowlist_not_a_filtered_inherit(self, monkeypatch):
        # A denylist covers the credential names it knows and, by construction,
        # cannot cover the ones it does not — `GH_TOKEN`, `KUBECONFIG`, `NPM_TOKEN`
        # and every future tool's variable would have reached the child. So the
        # environment is built from nothing instead of filtered down.
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "KUBECONFIG", "NPM_TOKEN", "HOME"):
            monkeypatch.setenv(name, f"leaked-{name}")
        out = await tc._run_probe(["/bin/sh", "-c", "env"], None)
        assert out is not None
        assert "leaked-" not in out
        # And the allowlist really is minimal. The extras are not ours: the sandbox
        # launcher injects its own markers (`KIROCREW_*`, `GIT_SSH_COMMAND`) and the
        # shell adds `PWD`/`SHLVL`/`_`, so they are named rather than blanket-allowed
        # — a NEW name appearing here should fail this and be looked at.
        ours = {"TERM", "NO_COLOR", "PAGER", "GIT_PAGER", "PATH", "LANG", "LC_ALL", "LC_CTYPE"}
        sandbox_injected = {"KIROCREW_HOST_PID", "KIROCREW_SANDBOX_ACTIVE",
                            "KIROCREW_SPAWNED", "GIT_SSH_COMMAND"}
        shell_added = {"PWD", "SHLVL", "_"}
        names = {line.split("=", 1)[0] for line in out.splitlines() if "=" in line}
        assert names <= ours | sandbox_injected | shell_added, names

    @pytest.mark.asyncio
    async def test_the_child_path_is_the_sanitized_one(self, monkeypatch):
        # So a probe cannot find a planted helper on a path the parent already
        # refused to resolve against.
        monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/home/u/p/.venv/bin"]))
        out = await tc._run_probe(["/bin/sh", "-c", 'echo "[$PATH]"'], None)
        assert out is not None
        assert ".venv" not in out

    @pytest.mark.asyncio
    async def test_probe_preparation_including_the_env_runs_off_the_loop(self, monkeypatch):
        # Regression: `_probe_env` walks every PATH entry through realpath+stat to
        # build the sanitized PATH, so evaluating it EAGERLY at the call site put
        # that walk back on the event loop at keystroke rate. It must be built inside
        # the callable the executor runs.
        import threading
        seen = {}
        real_env = tc._probe_env

        def spy_env():
            seen["env_thread"] = threading.current_thread().name
            return real_env()

        monkeypatch.setattr(tc, "_probe_env", spy_env)
        await tc._run_probe(["/bin/echo", "x"], None)
        assert "env_thread" in seen, "probe env was never built"
        assert seen["env_thread"] != threading.current_thread().name

    @pytest.mark.asyncio
    async def test_forces_colour_off_and_a_dumb_terminal(self):
        out = await tc._run_probe(
            ["/bin/sh", "-c", 'echo "$NO_COLOR:$TERM:$PAGER"'], None,
        )
        assert out == "1:dumb:cat\n"

    @pytest.mark.asyncio
    async def test_output_is_capped(self, monkeypatch):
        monkeypatch.setattr(tc, "PROBE_MAX_BYTES", 16)
        out = await tc._run_probe(["/bin/sh", "-c", "printf 'x%.0s' $(seq 200)"], None)
        assert out is not None and len(out) <= 16

    @pytest.mark.asyncio
    async def test_concurrent_probes_are_capped(self, monkeypatch):
        # A room full of typists must not be able to fork-bomb the gateway.
        monkeypatch.setattr(tc, "PROBE_CONCURRENCY", 2)
        monkeypatch.setattr(tc, "_probe_gate", None)
        live = 0
        peak = 0
        real = asyncio.create_subprocess_exec

        async def counting(*args, **kwargs):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            try:
                return await real(*args, **kwargs)
            finally:
                live -= 1

        with patch.object(asyncio, "create_subprocess_exec", counting):
            await asyncio.gather(*(
                tc._run_probe(["/bin/echo", "x"], None) for _ in range(8)
            ))
        assert peak <= 2

    @pytest.mark.asyncio
    async def test_a_cancelled_probe_does_not_leak_the_child(self, monkeypatch):
        # React Query aborts a superseded request; the child must not be left
        # holding a semaphore slot.
        monkeypatch.setattr(tc, "PROBE_TIMEOUT_S", 30)
        task = asyncio.ensure_future(tc._run_probe(["/bin/sleep", "30"], None))
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The slot is free again, so a following probe is not blocked.
        assert await asyncio.wait_for(
            tc._run_probe(["/bin/echo", "ok"], None), timeout=5,
        ) == "ok\n"
