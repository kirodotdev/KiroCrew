"""A name-based auto-approve must not be honoured for a shadowed program name.

Upstream issue #4438: trust grants and the read-only allowlist authorize a
command by program NAME, while the shell resolves that name afterwards through a
``PATH`` that can lead with directories the agent itself writes. These tests pin
both halves -- the decision function and the tiers wired to it.

Every test that resolves a name builds its own ``PATH`` and its own stand-in for
the trusted system directories, so no assertion depends on what the host has
installed.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from kiro_crew import name_grant, platform_compat
from kiro_crew.hooks import TOOL_AUTO_APPROVE, HookManager, HooksConfig

pytestmark = pytest.mark.skipif(
    platform_compat.IS_WINDOWS,
    reason="resolution fixtures rely on the POSIX execute bit and a ':'-joined PATH",
)


def _program(directory, name: str) -> str:
    """Create an executable *name* in *directory* and return its path.

    Written as a BINARY (no shebang), because that is what the programs this
    module vouches for actually are -- ``/usr/bin/head`` is ELF, not a script.
    A test that needs an interpreter chain writes its own shebang file, so the
    chain is exercised where it is the subject rather than everywhere by
    accident.
    """

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"\x7fELF\x02\x01\x01\x00 not a real binary\n")
    path.chmod(0o700)
    return str(path)


@pytest.fixture(autouse=True)
def _clear_pins():
    """Each test starts with no pinned identities.

    The pin store is process-wide by design (a file's identity is not a property
    of one session), so tests must not inherit each other's observations.
    """

    name_grant._PINS.clear()
    yield
    name_grant._PINS.clear()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A hermetic search path plus a stand-in for the system bin directories.

    Returns the tuple ``(system_dir, user_dir)``: ``user_dir`` comes FIRST on the
    search path, which is the ordering the issue reports on a real host.
    """

    system_dir = tmp_path / "usr" / "bin"
    user_dir = tmp_path / "home" / ".local" / "bin"
    system_dir.mkdir(parents=True)
    user_dir.mkdir(parents=True)

    monkeypatch.setattr(
        name_grant,
        "_agent_search_path",
        lambda: os.pathsep.join([str(user_dir), str(system_dir)]),
    )

    def fake_system_bin(name: str) -> str | None:
        candidate = system_dir / name
        return str(candidate) if candidate.is_file() else None

    monkeypatch.setattr(platform_compat, "trusted_system_bin", fake_system_bin)
    # No project checkout / workspace root unless a test declares one.
    monkeypatch.setattr(name_grant, "_agent_writable_roots", lambda: ())
    # The fixture's search path is absolute by construction; pin the ambiguity
    # answer so no assertion here depends on the host's own PATH (a runner with
    # an empty entry would otherwise refuse every command for the right reason
    # and fail every test for the wrong one).
    monkeypatch.setattr(name_grant, "_path_is_ambiguous", lambda: False)
    return system_dir, user_dir


class TestProgramNames:
    """Every command position is collected, and only command positions."""

    def test_pipeline_and_chain_positions(self):
        assert name_grant.program_names("cat a | grep x | wc -l") == ["cat", "grep", "wc"]
        assert name_grant.program_names("cd /tmp && ls") == ["cd", "ls"]
        assert name_grant.program_names("a; b || c") == ["a", "b", "c"]

    def test_environment_prefix_keeps_the_position_open(self):
        assert name_grant.program_names("FOO=bar head x") == ["head"]
        assert name_grant.program_names("A=1 B=2 grep x") == ["grep"]

    @pytest.mark.parametrize(
        "assignment",
        ["PATH=/writable/bin", "LD_PRELOAD=/tmp/eve.so", "BASH_ENV=/tmp/rc", "IFS=x"],
    )
    def test_execution_affecting_assignment_refuses(self, assignment):
        # GPT 5.6 round-10: `PATH=/writable/bin head file` decides which `head`
        # runs, and the loader variables decide what code runs inside it. The walk
        # used to skip every assignment and vouch for the system `head`.
        assert name_grant.program_names(f"{assignment} head file") is None

    @pytest.mark.parametrize(
        "command",
        [
            # A TOOL pointed at a helper command by its environment.
            "GIT_SSH_COMMAND=/writable/evil git fetch ssh://x",
            "GIT_EXTERNAL_DIFF=/writable/evil git diff",
            # An INTERPRETER told to load extra code first.
            "PYTHONPATH=/writable python3 x",
            "NODE_OPTIONS=--require=/writable/evil node x",
            "PERL5OPT=-Mevil perl x",
            "RUBYOPT=-revil ruby x",
            "JAVA_TOOL_OPTIONS=-javaagent:/writable/e.jar java X",
            # The FAMILIES, not just the spellings above: a name this list never
            # enumerates is still refused when it is shaped like one of them.
            "SOMETOOL_OPTIONS=--load=/writable/evil head file",
            "WHATEVERLIB=/writable head file",
            "LD_SOMETHING_NEW=/writable head file",
        ],
    )
    def test_code_injecting_assignment_refuses(self, command):
        # Opus 4.8 round-18. The base program really is the trusted system one,
        # so resolving its name answers nothing: `GIT_SSH_COMMAND=/writable/evil
        # git fetch` vouched for `/usr/bin/git` and ran the planted file. Same
        # shape as `LD_PRELOAD`, arriving through a tool's or interpreter's own
        # configuration. Matched as FAMILIES because each round found one more
        # interpreter with its own spelling, so an exact list is only ever as
        # complete as the last person to think about it.
        assert name_grant.program_names(command) is None

    @pytest.mark.parametrize(
        "command",
        ["FOO=bar head x", "A=1 B=2 head x", "MY_TOKEN=abc head x"],
    )
    def test_an_ordinary_assignment_is_still_skipped(self, command):
        # The other side of the rule: an assignment that changes a program's
        # INPUTS is not a statement about what runs, so it must not cost a prompt.
        assert name_grant.program_names(command) == ["head"]

    @pytest.mark.parametrize(
        "token",
        ["PATH+=:.", "PATH+=:/writable", "A[0]=x", "=bare"],
    )
    def test_non_plain_assignment_token_refuses(self, token):
        # GPT 5.6 round-14: only a strict `NAME=value` prefix is a benign
        # assignment. A compound (`PATH+=:.`) or an array element is a state
        # change this walk cannot evaluate, and skipping it left the program after
        # it unchecked.
        assert name_grant.program_names(f"{token} payload") is None

    def test_a_benign_assignment_prefix_still_walks(self):
        assert name_grant.program_names("FOO=bar head x") == ["head"]

    def test_redirect_target_is_not_a_program(self):
        # `>` is punctuation but does NOT open a command position: what follows
        # is a file. Judging it would resolve an operand.
        assert name_grant.program_names("head x > /tmp/out") == ["head"]
        assert name_grant.program_names("head x 2>&1") == ["head"]

    def test_leading_redirect_does_not_hide_the_program(self):
        # GPT 5.6's round-2 finding: a redirect may PRECEDE the program, and the
        # fd prefix arrives as its own token, so a position-closing rule made the
        # real program invisible.
        assert name_grant.program_names("2>/dev/null head README.md") == ["head"]
        assert name_grant.program_names("2>&1 head x") == ["head"]
        assert name_grant.program_names(">out head x") == ["head"]
        assert name_grant.program_names("cat a | 2>/dev/null grep x") == ["cat", "grep"]

    def test_substitution_inner_program_is_collected(self):
        assert name_grant.program_names("echo $(head x)") == ["echo", "head"]

    def test_quoted_separator_is_not_a_position(self):
        assert name_grant.program_names("echo 'a && b'") == ["echo"]

    def test_untokenizable_is_none_not_empty(self):
        # None means "argv could not be established", which the caller refuses.
        # An empty list would read as "no programs found" and be honoured.
        assert name_grant.program_names("head 'unbalanced") is None

    @pytest.mark.parametrize(
        "command",
        [
            "head x | { evil; }",  # GPT 5.6 round-3: `{` read as the program
            "if true; then evil; fi",
            "for f in a b; do evil; done",
            "while true; do evil; done",
            "! evil",
            "time evil",
            "case x in a) evil;; esac",
        ],
    )
    def test_unmodelled_grammar_is_none(self, command):
        # This walk models simple commands joined by pipes, `&&`/`||`/`;` and
        # subshells. A reserved word means the real program hides behind a syntax
        # word, so the answer is "unknown", never the subset it could see.
        assert name_grant.program_names(command) is None

    def test_newline_is_a_command_separator(self):
        # GPT 5.6 round-4: shlex treats a newline as ordinary whitespace, so
        # `head file\npayload` handed back ['head', 'file', 'payload'] and the
        # second command sat in operand position, invisible.
        assert name_grant.program_names("head file\npayload") == ["head", "payload"]
        assert name_grant.program_names("head a\r\ngrep b\rwc c") == ["head", "grep", "wc"]

    def test_newline_inside_quotes_refuses(self):
        # Splitting a quoted newline leaves that line unbalanced, which is the
        # safe direction: refuse rather than mis-read.
        assert name_grant.program_names("grep 'a\nb' file") is None

    @pytest.mark.parametrize(
        "command",
        [
            "head x;(payload)",  # GPT 5.6 round-5: `;(` arrives as ONE token
            "head x&&(payload)",
            "head x|(payload)",
            "head x;>out payload",
        ],
    )
    def test_composite_operator_is_refused(self, command):
        # `shlex` groups a run of punctuation into one token, so these matched no
        # operator and were skipped -- losing the command they introduce.
        assert name_grant.program_names(command) is None

    def test_spaced_forms_of_those_still_walk(self):
        assert name_grant.program_names("head x; (payload)") == ["head", "payload"]
        assert name_grant.program_names("head x && (payload)") == ["head", "payload"]

    def test_subshell_is_still_walked(self):
        # `(` opens a command position, so a subshell needs no refusal.
        assert name_grant.program_names("head x && (grep y)") == ["head", "grep"]


class TestLoopSafety:
    """The check does filesystem work, so it must only ever run off the loop."""

    def test_the_downgrade_helper_runs_it_in_a_thread(self):
        # Every rung on every surface reaches this check through ONE off-loop
        # entry point, and that entry point is what hands it to a worker
        # thread. Pinning both facts here is what keeps a future edit from
        # calling it straight from the loop -- the shape GPT flagged when the
        # hook layer did exactly that.
        import inspect

        entry = name_grant.refusal_for_command_off_loop
        assert inspect.iscoroutinefunction(entry)
        assert "asyncio.to_thread(name_grant_refusal" in inspect.getsource(entry)
        # And it is the only thread dispatch in the module.
        assert inspect.getsource(name_grant).count("asyncio.to_thread(name_grant_refusal") == 1

    def test_the_loop_bound_hook_no_longer_resolves_anything(self):
        # `HookManager.on_tool_call` is synchronous and called on the loop, so it
        # must not reach this module at all: not `shutil.which`, not a digest.
        import inspect

        from kiro_crew import hooks

        source = inspect.getsource(hooks.HookManager.on_tool_call)
        assert "name_grant" not in source


class TestShadowedResolution:
    """The reported attack and the cases that must stay auto-approvable."""

    def test_system_program_is_honoured(self, world):
        system_dir, _ = world
        _program(system_dir, "head")
        assert name_grant.name_grant_refusal("head -5 /etc/hosts") is None

    def test_planted_shim_that_shadows_a_system_program_is_refused(self, world):
        system_dir, user_dir = world
        _program(system_dir, "head")
        shim = _program(user_dir, "head")
        refusal = name_grant.name_grant_refusal("head -5 /etc/hosts")
        assert refusal is not None
        assert refusal.code == name_grant.SHADOWED
        assert shim in refusal.detail
        assert str(system_dir / "head") in refusal.detail

    def test_shim_in_a_later_pipeline_stage_is_refused(self, world):
        system_dir, user_dir = world
        _program(system_dir, "cat")
        _program(system_dir, "grep")
        _program(user_dir, "grep")
        assert name_grant.name_grant_refusal("cat a | grep x") is not None

    def test_user_installed_program_with_no_system_twin_shadows_nothing(self, world):
        # `gh`, `node`, `kirocrew`: nothing in the system directories answers to
        # the name, so the shadowing rule has no opinion. Such a name is gated by
        # the witnessed pin instead (see TestIdentityPin) -- here the point is
        # only that it is not refused AS A SHADOW.
        _, user_dir = world
        _program(user_dir, "gh")
        refusal = name_grant.name_grant_refusal("gh pr view 1")
        assert refusal is not None
        assert refusal.code != name_grant.SHADOWED

    def test_unresolvable_name_is_honoured(self, world):
        # A shell builtin (`cd`) resolves nowhere. There is no shadowed program,
        # and refusing every builtin would break `cd /tmp && ls`.
        system_dir, _ = world
        _program(system_dir, "ls")
        assert name_grant.name_grant_refusal("cd /tmp && ls") is None

    def test_symlink_to_the_same_system_file_is_honoured(self, world):
        # Distros ship `/usr/bin/head` -> a multi-call binary, and a second
        # spelling of the SAME file is not a substitution.
        system_dir, user_dir = world
        real = _program(system_dir, "head")
        (user_dir / "head").symlink_to(real)
        assert name_grant.name_grant_refusal("head x") is None

    def test_untokenizable_command_is_refused(self, world):
        assert name_grant.name_grant_refusal("head 'unbalanced") is not None

    def test_empty_command_is_not_refused(self, world):
        assert name_grant.name_grant_refusal("   ") is None


class TestAgentWritableTrees:
    """A resolution inside a tree the agent writes needs no shadowing."""

    def test_resolution_inside_the_project_checkout_is_refused(self, world, tmp_path, monkeypatch):
        system_dir, _ = world
        _program(system_dir, "head")
        checkout = tmp_path / "checkout"
        planted = _program(checkout / "bin", "tool")
        monkeypatch.setattr(
            name_grant,
            "_agent_search_path",
            lambda: os.pathsep.join([str(checkout / "bin"), str(system_dir)]),
        )
        monkeypatch.setattr(
            name_grant, "_agent_writable_roots", lambda: (os.path.normcase(str(checkout)),)
        )
        refusal = name_grant.name_grant_refusal("tool --list")
        assert refusal is not None
        assert refusal.code == name_grant.AGENT_TREE
        assert planted in refusal.detail

    def test_project_local_tool_directory_is_refused(self, world, tmp_path, monkeypatch):
        system_dir, _ = world
        venv_bin = tmp_path / "proj" / ".venv" / "bin"
        _program(venv_bin, "tool")
        monkeypatch.setattr(
            name_grant,
            "_agent_search_path",
            lambda: os.pathsep.join([str(venv_bin), str(system_dir)]),
        )
        assert name_grant.name_grant_refusal("tool --list") is not None

    def test_system_install_resolving_through_a_project_segment_is_honoured(
        self, world, tmp_path, monkeypatch
    ):
        # `/usr/bin/npm` -> `…/node_modules/npm/bin/npm-cli.js`. The segment list
        # describes where a name was FOUND, so judging the symlink TARGET would
        # refuse a stock install. Regression guard for that false positive.
        system_dir, _ = world
        target = _program(tmp_path / "usr" / "lib" / "node_modules" / "npm" / "bin", "npm-cli.js")
        (system_dir / "npm").symlink_to(target)
        assert name_grant.name_grant_refusal("npm run build") is None

    def test_roots_lookup_failure_fails_closed(self, world, monkeypatch):
        system_dir, _ = world
        _program(system_dir, "head")
        monkeypatch.setattr(name_grant, "_agent_writable_roots", lambda: None)
        assert name_grant.name_grant_refusal("head x") is not None


class TestPathFormPrograms:
    """A program named by path, not by name."""

    def test_relative_path_is_refused(self, world):
        assert name_grant.name_grant_refusal("./gradlew build") is not None

    def test_absolute_path_outside_the_agent_trees_is_honoured(self, world):
        system_dir, _ = world
        head = _program(system_dir, "head")
        assert name_grant.name_grant_refusal(f"{head} -5 /etc/hosts") is None

    def test_absolute_path_inside_an_agent_tree_is_refused(self, world, tmp_path, monkeypatch):
        checkout = tmp_path / "checkout"
        planted = _program(checkout / "bin", "payload")
        monkeypatch.setattr(
            name_grant, "_agent_writable_roots", lambda: (os.path.normcase(str(checkout)),)
        )
        assert name_grant.name_grant_refusal(f"{planted} --help") is not None

    def test_a_nul_bearing_path_refuses_instead_of_raising(
        self, world
    ):  # GPT 5.6 round-18. A NUL byte makes the filesystem calls raise
        # ValueError, NOT OSError -- `shlex` hands the token through intact, so
        # an OSError-only guard let it escape and abort the whole chat turn with
        # an error card in place of the approval. This module's contract is that
        # it RETURNS a refusal and never raises at its callers, so every
        # inspection site fails closed on both.
        refusal = name_grant.name_grant_refusal("/tmp/a\x00b --version")
        assert refusal is not None
        assert refusal.code == name_grant.UNINSPECTABLE

    @pytest.mark.parametrize(
        "command",
        [
            "head /tmp/a\x00b",  # a NUL in an OPERAND, not the program
            "/tmp/a\x00b",  # the program alone, no arguments
            "head file; /tmp/a\x00b",  # a second command position
        ],
    )
    def test_a_nul_byte_never_raises_from_any_position(self, world, command):
        # The contract is total: whatever a caller passes, this returns a verdict.
        system_dir, _ = world
        _program(system_dir, "head")
        name_grant.name_grant_refusal(command)
        name_grant.program_names(command)

    def test_an_alias_for_a_dispatcher_is_refused(self, world, monkeypatch):
        # GPT 5.6 round-19. The dispatcher rule read the name as WRITTEN, so an
        # ALIAS for one slipped past it: plant `runner -> env`, have a human
        # approve `runner` once (which pins it), and every later
        # `runner <payload>` auto-approves while `env` runs the payload. The rule
        # is about the FILE's behaviour, so it is now asked of the resolved file.
        system_dir, user_dir = world
        env = _program(system_dir, "env")
        alias = user_dir / "runner"
        alias.symlink_to(env)
        # Pin it the way a human approval would, so the pin cannot be what
        # refuses this -- the dispatcher rule has to be what does.
        name_grant.pin_human_approval("runner --version")
        refusal = name_grant.name_grant_refusal("runner /writable/payload")
        assert refusal is not None
        assert refusal.code == name_grant.DISPATCHER

    def test_an_absolute_alias_for_a_dispatcher_is_refused(self, world):
        # The same bypass spelled as a path rather than found on the search path.
        system_dir, user_dir = world
        env = _program(system_dir, "env")
        alias = user_dir / "runner2"
        alias.symlink_to(env)
        name_grant.pin_human_approval(f"{alias} --version")
        refusal = name_grant.name_grant_refusal(f"{alias} /writable/payload")
        assert refusal is not None
        assert refusal.code == name_grant.DISPATCHER

    def test_a_system_program_resolving_to_a_dispatcher_is_still_honoured(self, world):
        # The BusyBox shape, and the reason the new question is asked AFTER the
        # trusted-system branch: there every coreutils name resolves to
        # `/bin/busybox`, a dispatcher by basename. Those names ARE the system
        # program they claim to be, so they must not start costing a prompt.
        system_dir, _ = world
        busybox = _program(system_dir, "busybox")
        head = system_dir / "head"
        head.symlink_to(busybox)
        assert name_grant.name_grant_refusal("head file") is None


class TestUnenumerableConstructs:
    """Seeing part of a command's program set is not a basis for vouching."""

    @pytest.mark.parametrize(
        "command",
        [
            'echo "$(head x)"',  # POSIX quote handling swallows the substitution
            "echo $(head x)",
            'echo "`head x`"',
            "echo `head x`",
            "cat <(head x)",
            "diff <(head a) <(head b)",
        ],
    )
    def test_substitutions_are_refused(self, world, command):
        system_dir, _ = world
        _program(system_dir, "echo")
        _program(system_dir, "cat")
        _program(system_dir, "diff")
        assert name_grant.name_grant_refusal(command) is not None

    def test_expanded_program_token_is_refused(self, world):
        assert name_grant.name_grant_refusal("$CMD --version") is not None
        assert name_grant.name_grant_refusal("./*.sh") is not None


class TestNonRegularFiles:
    """A program name that resolves to something unreadable must never be read."""

    def test_a_planted_fifo_is_refused_without_opening_it(self, world):
        # Opus 4.8: a FIFO passes every test a PATH lookup applies -- it exists,
        # it carries the execute bit, it is not a directory -- so `which` returns
        # it and the digest would open() it O_RDONLY, blocking in the kernel until
        # a writer appears. On a worker thread that leak is permanent, and
        # repeating it drains the shared executor, stalling every other session's
        # approvals. The test would HANG rather than fail if the guard regressed,
        # so it is also its own canary.
        _, user_dir = world
        fifo = user_dir / "gh"
        os.mkfifo(fifo, 0o700)
        refusal = name_grant.name_grant_refusal("gh pr view 1")
        assert refusal is not None
        assert refusal.code == name_grant.UNINSPECTABLE

    def test_a_fifo_interpreter_is_refused_without_opening_it(self, world):
        _, user_dir = world
        fifo = user_dir / "myrunner"
        os.mkfifo(fifo, 0o700)
        script = user_dir / "tool"
        script.write_text(f"#!{fifo}\n")
        script.chmod(0o700)
        name_grant.pin_human_approval("tool run")
        refusal = name_grant.name_grant_refusal("tool run")
        assert refusal is not None

    def test_the_regular_file_predicate_rejects_a_fifo(self, world):
        _, user_dir = world
        fifo = user_dir / "pipe"
        os.mkfifo(fifo)
        assert name_grant._is_regular_file(str(fifo)) is False
        plain = _program(user_dir, "plain")
        assert name_grant._is_regular_file(plain) is True


class TestIdentityPin:
    """A non-system program is vouched for only on the file a human approved."""

    def test_unwitnessed_program_is_refused(self, world):
        # GPT 5.6's round-2 finding: pinning on first SIGHT would bless whatever
        # is there when a tier first looks -- and a tier looks precisely when it
        # is about to auto-approve without asking anyone.
        _, user_dir = world
        _program(user_dir, "gh")
        refusal = name_grant.name_grant_refusal("gh pr view 1")
        assert refusal is not None
        assert refusal.code == name_grant.UNWITNESSED

    def test_human_approval_makes_it_auto_approvable(self, world):
        _, user_dir = world
        _program(user_dir, "gh")
        name_grant.pin_human_approval("gh pr view 1")
        assert name_grant.name_grant_refusal("gh pr view 1") is None
        assert name_grant.name_grant_refusal("gh pr view 2") is None

    def test_replaced_binary_with_no_system_twin_is_refused(self, world):
        # `gh` has no `/usr/bin/gh`, so the shadowing rule cannot see a
        # substitution. The witnessed pin can.
        _, user_dir = world
        _program(user_dir, "gh")
        name_grant.pin_human_approval("gh pr view 1")
        assert name_grant.name_grant_refusal("gh pr view 1") is None
        (user_dir / "gh").write_text("#!/bin/sh\necho pwned\n")
        (user_dir / "gh").chmod(0o700)
        refusal = name_grant.name_grant_refusal("gh pr view 1")
        assert refusal is not None
        assert refusal.code == name_grant.IDENTITY_CHANGED
        assert "gh" in refusal.detail

    def test_mismatch_does_not_re_pin_from_a_check(self, world):
        # Re-pinning on a check would mean "one prompt, then trusted" -- and this
        # code cannot see whether the human said yes to that prompt. Only a real
        # approval re-pins.
        _, user_dir = world
        _program(user_dir, "gh")
        name_grant.pin_human_approval("gh pr view 1")
        (user_dir / "gh").write_text("#!/bin/sh\necho pwned\n")
        (user_dir / "gh").chmod(0o700)
        assert name_grant.name_grant_refusal("gh pr view 1") is not None
        assert name_grant.name_grant_refusal("gh pr view 1") is not None
        name_grant.pin_human_approval("gh pr view 1")
        assert name_grant.name_grant_refusal("gh pr view 1") is None

    def test_system_program_needs_no_witness(self, world):
        # What keeps coreutils and the read-only allowlist working with no
        # approval history at all.
        system_dir, _ = world
        _program(system_dir, "head")
        assert name_grant.name_grant_refusal("head x") is None

    def test_absolute_system_path_needs_no_witness(self, world):
        system_dir, _ = world
        head = _program(system_dir, "head")
        assert name_grant.name_grant_refusal(f"{head} x") is None

    def test_same_name_in_two_directories_is_independent(self, world, tmp_path, monkeypatch):
        # Two projects shipping a same-named tool must not invalidate each other:
        # only a swap IN PLACE is a mismatch.
        system_dir, user_dir = world
        _program(user_dir, "toolx")
        name_grant.pin_human_approval("toolx run")
        assert name_grant.name_grant_refusal("toolx run") is None
        other = tmp_path / "other" / "bin"
        _program(other, "toolx")
        monkeypatch.setattr(
            name_grant,
            "_agent_search_path",
            lambda: os.pathsep.join([str(other), str(system_dir)]),
        )
        # A different directory is a different pin, so it starts unwitnessed
        # rather than inheriting the other one's approval.
        refusal = name_grant.name_grant_refusal("toolx run")
        assert refusal is not None and refusal.code == name_grant.UNWITNESSED
        name_grant.pin_human_approval("toolx run")
        assert name_grant.name_grant_refusal("toolx run") is None

    def test_same_size_rewrite_with_restored_mtime_is_refused(self, world):
        # GPT 5.6 round-3: mtime and size are both under the writer's control, so
        # a same-size in-place rewrite plus os.utime restores them exactly. No
        # sleep here on purpose -- the rewrite lands inside the same ctime tick as
        # the pin, so the metadata alone (including the kernel-set ctime) is
        # identical and only the content digest can tell them apart.
        _, user_dir = world
        gh = user_dir / "gh"
        gh.write_text("#!/bin/sh\nexit 0\n")
        gh.chmod(0o700)
        before = gh.stat()
        name_grant.pin_human_approval("gh pr view 1")
        assert name_grant.name_grant_refusal("gh pr view 1") is None
        gh.write_text("#!/bin/sh\nexit 9\n")  # byte-for-byte same length
        os.utime(gh, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = gh.stat()
        assert after.st_size == before.st_size
        assert after.st_mtime_ns == before.st_mtime_ns
        assert after.st_ino == before.st_ino
        refusal = name_grant.name_grant_refusal("gh pr view 1")
        assert refusal is not None
        assert refusal.code == name_grant.IDENTITY_CHANGED

    def test_a_large_file_is_digested_whole_including_its_middle(self, world):
        # GPT 5.6 round-14: an earlier version hashed only a large file's head and
        # tail, so a MIDDLE-only rewrite that preserved the size and landed in one
        # ctime tick kept the identity equal. Refusing large files instead would
        # have killed witnessing for `node`, `gh` and `docker`, so the digest now
        # covers every byte.
        _, user_dir = world
        big = user_dir / "toolbig"
        payload = bytearray(b"A" * (3 * name_grant._DIGEST_CHUNK))
        big.write_bytes(bytes(payload))
        big.chmod(0o700)
        before = big.stat()
        name_grant.pin_human_approval("toolbig run")
        assert name_grant.name_grant_refusal("toolbig run") is None
        middle = len(payload) // 2
        payload[middle : middle + 8] = b"BBBBBBBB"  # same size, changed MIDDLE
        big.write_bytes(bytes(payload))
        os.utime(big, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert big.stat().st_size == before.st_size
        refusal = name_grant.name_grant_refusal("toolbig run")
        assert refusal is not None
        assert refusal.code == name_grant.IDENTITY_CHANGED

    def test_lost_pin_during_the_touch_refuses_instead_of_raising(self, world, monkeypatch):
        # GPT 5.6 round-5: a concurrent insert can evict the key between the read
        # and the LRU touch. `_PIN_LOCK` makes that unreachable through this
        # module's own paths, so the race cannot be reproduced by timing -- this
        # injects the failure directly, because what matters is the consequence:
        # on the approval path an exception aborts the user's turn, a refusal
        # costs a prompt.
        _, user_dir = world
        _program(user_dir, "gh")
        name_grant.pin_human_approval("gh pr view 1")
        assert name_grant.name_grant_refusal("gh pr view 1") is None

        class Evicting(type(name_grant._PINS)):  # type: ignore[misc]
            def move_to_end(self, key, last=True):  # noqa: D102
                raise KeyError(key)

        monkeypatch.setattr(name_grant, "_PINS", Evicting(name_grant._PINS))
        refusal = name_grant.name_grant_refusal("gh pr view 1")
        assert refusal is not None
        assert refusal.code == name_grant.UNWITNESSED

    def test_concurrent_checks_and_pins_do_not_raise(self, world):
        # Contention smoke test: many checks against a store being churned below
        # the eviction threshold. It does not reproduce the eviction window (the
        # lock closes it), so the test above is what pins the consequence.
        import threading

        _, user_dir = world
        _program(user_dir, "gh")
        name_grant.pin_human_approval("gh pr view 1")
        errors: list[BaseException] = []
        stop = threading.Event()

        def check() -> None:
            try:
                while not stop.is_set():
                    name_grant.name_grant_refusal("gh pr view 1")
            except BaseException as exc:  # noqa: BLE001 - the point is to catch any
                errors.append(exc)

        def churn() -> None:
            try:
                index = 0
                while not stop.is_set():
                    name_grant.pin_human_approval(f"filler{index}")
                    index += 1
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=check), threading.Thread(target=churn)]
        # A small store makes eviction certain rather than theoretical.
        original_limit = name_grant._PIN_LIMIT
        name_grant._PIN_LIMIT = 4
        try:
            for thread in threads:
                thread.start()
            time.sleep(0.4)
            stop.set()
            for thread in threads:
                thread.join(timeout=5)
        finally:
            name_grant._PIN_LIMIT = original_limit
        assert not errors, errors

    def test_pin_store_is_bounded(self, world):
        _, user_dir = world
        _program(user_dir, "gh")
        for index in range(name_grant._PIN_LIMIT + 20):
            name_grant.pin_human_approval(f"synthetic{index}")
        name_grant.pin_human_approval("gh pr view 1")
        assert len(name_grant._PINS) <= name_grant._PIN_LIMIT
        assert name_grant.name_grant_refusal("gh pr view 1") is None


class TestLogSafety:
    """A refusal's log text must not carry the command line or a resolved path."""

    def test_log_text_is_a_module_constant(self, world):
        system_dir, user_dir = world
        _program(system_dir, "head")
        shim = _program(user_dir, "head")
        refusal = name_grant.name_grant_refusal("head /etc/hosts")
        assert refusal is not None
        assert refusal.log_text in name_grant._REFUSAL_LOG_TEXT.values()
        # The path IS in the detail (the person deciding needs it) and must NOT
        # be in the text that reaches a log sink.
        assert shim in refusal.detail
        assert shim not in refusal.log_text
        assert "/etc/hosts" not in refusal.log_text

    def test_every_code_has_log_text(self):
        codes = {
            name_grant.UNENUMERABLE,
            name_grant.UNTOKENIZABLE,
            name_grant.EXPANDED,
            name_grant.RELATIVE_PATH,
            name_grant.AGENT_TREE,
            name_grant.SHADOWED,
            name_grant.IDENTITY_CHANGED,
            name_grant.UNWITNESSED,
            name_grant.DISPATCHER,
            name_grant.AMBIGUOUS_PATH,
            name_grant.WINDOWS_UNMODELLED,
            name_grant.UNINSPECTABLE,
            name_grant.UNKNOWN_COMMAND,
            name_grant.AMBIGUOUS_ENV,
        }
        assert codes == set(name_grant._REFUSAL_LOG_TEXT)


class TestSearchPath:
    """What the check resolves against must be what the child will search."""

    def test_relative_entry_refuses_everything(self, world, monkeypatch):
        # GPT 5.6 round-8, correcting round-6: dropping `.` from the search path
        # is not enough. The CHILD still has it, so the check would resolve
        # `/usr/bin/head` and vouch while the child ran a planted `./head` from
        # its own directory. Neither keeping nor dropping the entry answers the
        # question, so a PATH carrying one refuses outright.
        system_dir, _ = world
        _program(system_dir, "head")
        # Override the fixture's pin: this test is about the ambiguous case.
        monkeypatch.setattr(name_grant, "_path_is_ambiguous", lambda: True)
        refusal = name_grant.name_grant_refusal("head x")
        assert refusal is not None
        assert refusal.code == name_grant.AMBIGUOUS_PATH

    def test_ambiguity_is_detected_from_the_real_path(self, monkeypatch):
        monkeypatch.setenv("PATH", os.pathsep.join([".", "/usr/bin"]))
        assert name_grant._path_is_ambiguous() is True
        monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", ""]))
        assert name_grant._path_is_ambiguous() is True
        monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
        assert name_grant._path_is_ambiguous() is False

    def test_only_absolute_entries_are_searched(self, monkeypatch):
        monkeypatch.setenv("PATH", os.pathsep.join([".", "relative/bin", "/usr/bin"]))
        entries = name_grant._agent_search_path().split(os.pathsep)
        assert all(os.path.isabs(entry) for entry in entries)


class TestDispatchers:
    """A program that runs another program named in its arguments."""

    @pytest.mark.parametrize("wrapper", ["env", "sudo", "nohup", "timeout", "xargs", "command"])
    def test_dispatcher_is_refused(self, world, wrapper):
        # GPT 5.6 round-8: `env head file` is ONE program with two operands as far
        # as the walk can tell, so it would vouch for `/usr/bin/env` while `head`
        # is resolved from PATH at exec time.
        system_dir, _ = world
        _program(system_dir, wrapper)
        _program(system_dir, "head")
        refusal = name_grant.name_grant_refusal(f"{wrapper} head file")
        assert refusal is not None
        assert refusal.code == name_grant.DISPATCHER

    @pytest.mark.parametrize("builtin", ["exec", "eval", "builtin", "source", "."])
    def test_dispatching_builtin_is_refused(self, world, builtin):
        # GPT 5.6 round-9: a builtin is invisible to `which`, and an unresolvable
        # name is otherwise "nothing to shadow, nothing to vouch for" -- so
        # `exec head file` passed while `head` came from PATH at exec time. No
        # program file is created for these on purpose: not being resolvable is
        # the whole point.
        system_dir, _ = world
        _program(system_dir, "head")
        refusal = name_grant.name_grant_refusal(f"{builtin} head file")
        assert refusal is not None
        assert refusal.code == name_grant.DISPATCHER

    def test_absolute_dispatcher_is_refused_too(self, world):
        system_dir, _ = world
        env = _program(system_dir, "env")
        refusal = name_grant.name_grant_refusal(f"{env} head file")
        assert refusal is not None
        assert refusal.code == name_grant.DISPATCHER

    @pytest.mark.parametrize("shell", ["sh", "bash", "zsh", "dash", "fish"])
    def test_command_shell_is_refused(self, world, shell):
        # GPT 5.6 round-12: `sh -c 'head file'` runs an arbitrary command string,
        # so vouching for `/bin/sh` says nothing about what executes. Scoped out in
        # round 9 and asked for here; a grant naming a shell is a grant to run
        # anything, which belongs on the approval card.
        system_dir, _ = world
        _program(system_dir, shell)
        refusal = name_grant.name_grant_refusal(f"{shell} -c 'head file'")
        assert refusal is not None
        assert refusal.code == name_grant.DISPATCHER

    @pytest.mark.parametrize("builtin", ["export", "hash", "alias", "declare", "set"])
    def test_resolution_mutating_builtin_is_refused(self, world, builtin):
        # GPT 5.6 round-13: `export PATH=/agent/bin:$PATH && head file` re-points
        # the lookup for every command after it, so the resolution this check
        # performs describes the PREVIOUS state. `hash` and `alias` re-point one
        # name directly. No program file is created: like the dispatching
        # builtins, not being resolvable is the point.
        system_dir, _ = world
        _program(system_dir, "head")
        refusal = name_grant.name_grant_refusal(f"{builtin} PATH=/agent/bin && head file")
        assert refusal is not None
        assert refusal.code == name_grant.DISPATCHER

    @pytest.mark.parametrize("builtin", ["printf", "read", "mapfile", "let"])
    def test_variable_writing_builtin_is_refused(self, world, builtin):
        # GPT 5.6 round-14: `printf -v PATH /writable; payload` sets PATH with no
        # assignment token at all, so the walk checked `payload` against the
        # previous search path.
        system_dir, _ = world
        _program(system_dir, "head")
        refusal = name_grant.name_grant_refusal(f"{builtin} -v PATH /writable; head file")
        assert refusal is not None

    @pytest.mark.parametrize(
        "command",
        [
            "trap 'payload' DEBUG; head file",
            "enable -f /writable/evil.so head; head file",
        ],
    )
    def test_code_installing_builtin_is_refused(self, world, command):
        # GPT 5.6 round-15. `trap` installs a body that runs before every later
        # command; `enable -f` loads a shared object AS a builtin. Neither is a
        # file on the search path, so `shutil.which` cannot see either one.
        system_dir, _ = world
        _program(system_dir, "head")
        refusal = name_grant.name_grant_refusal(command)
        assert refusal is not None
        assert refusal.code == name_grant.UNKNOWN_COMMAND

    def test_a_hash_inside_a_word_does_not_hide_a_later_command(self, world):
        # GPT 5.6 round-16, and the sharpest finding on this PR because it is the
        # PR's own thesis turned against it. `shlex` discards the rest of the line
        # after `#`; bash does NOT (a `#` only opens a comment at the start of a
        # word). Measured: `bash -c 'echo A file#x; echo B ran'` prints BOTH
        # halves, while the default lexer handed this walk ['head', 'file'] and
        # the second command was never checked.
        system_dir, user_dir = world
        _program(system_dir, "head")
        _program(system_dir, "cat")
        _program(user_dir, "cat")  # the shadowing plant
        names = name_grant.program_names("head file#x; cat secret")
        assert names == ["head", "cat"]
        refusal = name_grant.name_grant_refusal("head file#x; cat secret")
        assert refusal is not None
        assert refusal.code == name_grant.SHADOWED

    def test_a_trailing_comment_is_still_harmless(self, world):
        # Turning commenters off means a real comment arrives as an operand. That
        # is fine: only command POSITIONS are inspected.
        system_dir, _ = world
        _program(system_dir, "head")
        assert name_grant.program_names("head file # a note") == ["head"]

    @pytest.mark.parametrize("var", ["BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"])
    def test_inherited_preload_variable_refuses(self, world, var, monkeypatch):
        # GPT 5.6 round-16. `BASH_ENV=/writable/rc` holding `head() { payload; }`
        # means `bash -c 'head file'` runs the FUNCTION, while the name still
        # resolves to /usr/bin/head. Same threat as the command-line prefix
        # `_EXEC_ENV_VARS` already refuses, arriving through the environment.
        system_dir, _ = world
        _program(system_dir, "head")
        assert name_grant.name_grant_refusal("head file") is None
        monkeypatch.setenv(var, "/writable/rc")
        refusal = name_grant.name_grant_refusal("head file")
        assert refusal is not None
        assert refusal.code == name_grant.AMBIGUOUS_ENV

    @pytest.mark.parametrize(
        "key",
        [
            "BASH_FUNC_head%%",  # bash >= 4.3, the live spelling
            "BASH_FUNC_head()",  # the interim post-Shellshock spelling
            "BASH_FUNC_anything%%",  # a function that shadows nothing in THIS command
        ],
    )
    def test_an_exported_shell_function_refuses(self, world, key, monkeypatch):
        # GPT 5.6 round-17. An exported function shadows the name with no writable
        # file anywhere: bash re-imports `BASH_FUNC_head%%` in the child, so
        # `head file` runs the payload while the name still resolves to
        # /usr/bin/head. Matched on the `BASH_FUNC_` PREFIX so a build that spells
        # the suffix differently cannot hand the bypass back -- which is why the
        # third case, naming a function this command never mentions, refuses too.
        system_dir, _ = world
        _program(system_dir, "head")
        assert name_grant.name_grant_refusal("head file") is None
        monkeypatch.setenv(key, "() { payload; }")
        refusal = name_grant.name_grant_refusal("head file")
        assert refusal is not None
        assert refusal.code == name_grant.AMBIGUOUS_ENV

    def test_a_legacy_bare_name_function_export_refuses(self, world, monkeypatch):
        # The pre-2014 spelling: the key is the bare function name and only the
        # `() {` value marks it as a function. Supported bash no longer imports
        # this, so it is belt-and-braces -- but the value form costs one check.
        system_dir, _ = world
        _program(system_dir, "head")
        assert name_grant.name_grant_refusal("head file") is None
        monkeypatch.setenv("head", "() { payload; }")
        refusal = name_grant.name_grant_refusal("head file")
        assert refusal is not None
        assert refusal.code == name_grant.AMBIGUOUS_ENV

    @pytest.mark.parametrize("builtin", ["compgen", "pushd", "popd", "bind", "caller", "disown"])
    def test_an_unenumerated_builtin_is_refused_by_default(self, world, builtin):
        # THE POINT OF THE INVERSION, and the reason this class is closed rather
        # than four rounds of patching. None of these names appears anywhere in
        # the module: they refuse because an unresolved command word is refused BY
        # DEFAULT, not because someone thought of them. A future bash builtin is
        # covered the same way.
        system_dir, _ = world
        _program(system_dir, "head")
        assert builtin not in name_grant._DISPATCHERS
        refusal = name_grant.name_grant_refusal(f"{builtin} x; head file")
        assert refusal is not None
        assert refusal.code == name_grant.UNKNOWN_COMMAND

    @pytest.mark.parametrize("command", ["cd /tmp && ls", "echo hi", ": && ls", "pwd"])
    def test_inert_builtins_are_still_allowed(self, world, command):
        # The inversion refuses by default, so the allowlist is what keeps the
        # ordinary read-only tier working. `cd /tmp && ls` is the case that made
        # the original "unresolvable means nothing to shadow" branch look right.
        system_dir, _ = world
        for name in ("ls", "pwd"):
            _program(system_dir, name)
        assert name_grant.name_grant_refusal(command) is None

    def test_an_ordinary_program_is_unaffected(self, world):
        system_dir, _ = world
        _program(system_dir, "head")
        assert name_grant.name_grant_refusal("head file") is None


class TestInheritedHostEnvironment:
    """The rootdir conftest scrubs the inherited entries name_grant refuses on.

    RHEL-family hosts export ``which`` as a shell function from
    ``/etc/profile.d/which2.sh``, so ``BASH_FUNC_which%%`` is inherited by every
    login shell -- and the AMBIGUOUS_ENV refusal above is checked before every
    narrower code, so without the scrub 79 of the 163 tests in this file observed
    ``inherited_env_can_redefine_programs`` instead of the code they assert
    (issue #8395). ``_inherited_preload()``'s OTHER half is host state just as
    easily: a login profile exporting ``BASH_ENV``, or a container image setting
    ``ENV``, reproduces the same shape with no ``which2.sh`` anywhere. Ubuntu CI
    carries neither, which is why this class injects both itself: the defect has
    to be reproducible everywhere, not only on the hosts that revealed it.
    """

    _RHEL_KEY = "BASH_FUNC_which%%"
    _RHEL_VALUE = "() {  ( alias; eval ${which_declare} ) | /usr/bin/which $@; }"
    _PRELOAD_KEY = "BASH_ENV"
    _PRELOAD_VALUE = "/etc/kc-inherited-rc"

    @pytest.fixture(scope="class")
    def _inherited_shell_environment(self):
        """Make the variables INHERITED state, not something the test created.

        Class-scoped on purpose: pytest instantiates higher-scoped fixtures
        first, so this runs before the function-scoped autouse scrub in the
        rootdir conftest -- both variables are already in the environment when
        the scrub looks, exactly as a login shell's exports are. The deliberate
        AMBIGUOUS_ENV tests above are the opposite shape: they construct their
        entries inside the test body, AFTER the scrub, which is why a global
        scrub costs them nothing. Raw ``os.environ`` rather than ``monkeypatch``
        because the built-in monkeypatch fixture is function-scoped.

        The teardown asserts the variables are BACK before putting the host's own
        values back: the scrub's contract is save-and-restore, and the restore half
        is observable only here, after the last test's own fixtures -- monkeypatch's
        undo included -- have finished. The prior values are snapshotted first, so a
        worker on a genuinely RHEL-family host keeps its real export: what this
        fixture injects is removed, what the host actually had is restored.
        """
        saved = {key: os.environ.get(key) for key in (self._RHEL_KEY, self._PRELOAD_KEY)}
        os.environ[self._RHEL_KEY] = self._RHEL_VALUE
        os.environ[self._PRELOAD_KEY] = self._PRELOAD_VALUE
        try:
            yield
        finally:
            missing = [key for key in (self._RHEL_KEY, self._PRELOAD_KEY) if key not in os.environ]
            for key, prior in saved.items():
                if prior is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = prior
            assert not missing, (
                "the conftest scrub removed these inherited entries but did not "
                f"restore them after the test: {missing}"
            )

    def test_a_narrower_code_survives_an_inherited_hostile_environment(
        self, _inherited_shell_environment, world
    ):
        # With the rootdir scrub disabled this fails three times over: both
        # variables are still visible here, and the refusal below comes back
        # AMBIGUOUS_ENV -- the broadest code, checked first -- instead of the
        # DISPATCHER code this command actually earns. That inversion is the
        # whole issue: one host-inherited variable rewrote what 79 unrelated
        # assertions saw.
        assert self._RHEL_KEY not in os.environ
        assert self._PRELOAD_KEY not in os.environ
        system_dir, _ = world
        _program(system_dir, "env")
        _program(system_dir, "head")
        refusal = name_grant.name_grant_refusal("env head file")
        assert refusal is not None
        assert refusal.code == name_grant.DISPATCHER

    def test_a_monkeypatched_same_key_does_not_defeat_the_restore(
        self, _inherited_shell_environment, monkeypatch
    ):
        # The teardown-ordering trap: this test overrides the SAME key the host
        # inherited. The scrub removed it first, so monkeypatch records "was
        # absent" and its undo DELETES the key -- the inherited value survives
        # only because the scrub records its removals on the same shared
        # monkeypatch instance, whose LIFO undo deletes this override first and
        # restores the inherited value after. A hand-rolled restore in the
        # scrub would run BEFORE monkeypatch's undo and be deleted by it. The
        # class fixture's teardown assertion is the observer that fails when
        # the inherited entry leaks out of the worker's environment.
        assert self._RHEL_KEY not in os.environ
        monkeypatch.setenv(self._RHEL_KEY, "() { overridden; }")
        assert os.environ[self._RHEL_KEY] == "() { overridden; }"


class TestInterpreterChain:
    """A pinned script binds its bytes; its interpreter must face the same rules."""

    def test_env_shebang_interpreter_is_checked(self, world):
        # GPT 5.6 round-6: `#!/usr/bin/env node` resolves `node` from PATH at exec
        # time, so the script can stay byte-identical while the program that
        # actually runs is replaced underneath it.
        system_dir, user_dir = world
        script = user_dir / "tool"
        # The env path must be the fixture's OWN system env, not a literal
        # `/usr/bin/env`: the shebang's env binary is now held to the same
        # standard as any program (round 21), and a host path would make this
        # test depend on the runner's filesystem, which the fixture exists to
        # avoid.
        system_env = _program(system_dir, "env")
        script.write_text(f"#!{system_env} node\n")
        script.chmod(0o700)
        _program(user_dir, "node")
        name_grant.pin_human_approval("tool run")
        assert name_grant.name_grant_refusal("tool run") is None
        # Replace only the INTERPRETER; the script is untouched.
        (user_dir / "node").write_text("#!/bin/sh\necho pwned\n")
        (user_dir / "node").chmod(0o700)
        refusal = name_grant.name_grant_refusal("tool run")
        assert refusal is not None
        assert refusal.code == name_grant.IDENTITY_CHANGED

    def test_shadowed_interpreter_is_refused(self, world):
        system_dir, user_dir = world
        _program(system_dir, "python3")
        _program(user_dir, "python3")  # shim shadowing the system interpreter
        script = user_dir / "tool"
        system_env = _program(system_dir, "env")
        script.write_text(f"#!{system_env} python3\n")
        script.chmod(0o700)
        name_grant.pin_human_approval("tool run")
        refusal = name_grant.name_grant_refusal("tool run")
        assert refusal is not None
        assert refusal.code == name_grant.SHADOWED

    def test_complex_env_shebang_is_refused(self, world):
        # GPT 5.6 round-10: `env`'s options take operands, so picking the first
        # non-flag token read `UNUSED` as the interpreter -- validating the wrong
        # file while vouching for the command. Only the bare one-name form is read.
        system_dir, user_dir = world
        _program(user_dir, "node")
        system_env = _program(system_dir, "env")
        for line in (
            f"#!{system_env} -S -u UNUSED node",
            f"#!{system_env} -i node",
            f"#!{system_env} FOO=bar node",
        ):
            script = user_dir / "tool"
            script.write_text(f"{line}\n")
            script.chmod(0o700)
            name_grant.pin_human_approval("tool run")
            refusal = name_grant.name_grant_refusal("tool run")
            assert refusal is not None, line
            assert refusal.code == name_grant.UNENUMERABLE, line

    def test_a_shebang_env_that_is_not_the_system_env_is_refused(self, world):
        # GPT 5.6 round-21. `env` was matched by BASENAME, so the path was read
        # for the name after it and then forgotten -- `#!<planted>/env node`
        # validated `node` while the planted `env` is what the kernel actually
        # runs. A pin binds the SCRIPT's bytes, so the script keeps matching
        # while the file behind its shebang is swapped underneath it. The env
        # binary now has to BE the system one, not merely be spelled like it.
        system_dir, user_dir = world
        _program(system_dir, "node")
        planted_env = _program(user_dir, "env")
        script = user_dir / "tool"
        script.write_text(f"#!{planted_env} node\n")
        script.chmod(0o700)
        name_grant.pin_human_approval("tool run")
        refusal = name_grant.name_grant_refusal("tool run")
        assert refusal is not None
        assert refusal.code == name_grant.UNENUMERABLE

    def test_the_system_env_shebang_is_still_honoured(self, world):
        # The other side: tightening the env path must not refuse the ordinary
        # `#!<system>/env node` form, which is what real scripts carry.
        system_dir, user_dir = world
        _program(system_dir, "node")
        system_env = _program(system_dir, "env")
        script = user_dir / "tool"
        script.write_text(f"#!{system_env} node\n")
        script.chmod(0o700)
        name_grant.pin_human_approval("tool run")
        assert name_grant.name_grant_refusal("tool run") is None

    def test_shell_shebang_is_not_treated_as_a_dispatcher(self, world):
        # The distinction the `as_interpreter` step exists for: `sh -c '...'` on a
        # command line names its program in arguments this check cannot read, but
        # `#!/bin/sh` atop a script whose BYTES are pinned runs that script. Adding
        # shells to the dispatcher table without this would have refused every
        # shell script -- an over-refusal, caught by three existing tests.
        system_dir, user_dir = world
        _program(system_dir, "sh")
        script = user_dir / "tool"
        script.write_text(f"#!{system_dir / 'sh'}\necho hi\n")
        script.chmod(0o700)
        name_grant.pin_human_approval("tool run")
        assert name_grant.name_grant_refusal("tool run") is None

    def test_absolute_system_interpreter_is_fine(self, world):
        system_dir, user_dir = world
        _program(system_dir, "sh")
        script = user_dir / "tool"
        script.write_text(f"#!{system_dir / 'sh'}\n")
        script.chmod(0o700)
        name_grant.pin_human_approval("tool run")
        assert name_grant.name_grant_refusal("tool run") is None

    def test_interpreter_inside_an_agent_tree_is_refused(self, world, tmp_path, monkeypatch):
        _, user_dir = world
        checkout = tmp_path / "checkout"
        planted = _program(checkout / "bin", "myrunner")
        script = user_dir / "tool"
        script.write_text(f"#!{planted}\n")
        script.chmod(0o700)
        monkeypatch.setattr(
            name_grant, "_agent_writable_roots", lambda: (os.path.normcase(str(checkout)),)
        )
        name_grant.pin_human_approval("tool run")
        refusal = name_grant.name_grant_refusal("tool run")
        assert refusal is not None
        assert refusal.code == name_grant.AGENT_TREE

    def test_loop_bound_callers_cannot_reach_the_shebang_read(self):
        # The shebang read is one more reason the check may not run on the loop;
        # `TestLoopSafety` pins that the only in-gateway caller is off-loop.
        assert name_grant._shebang_interpreter("/nonexistent/program") is None


class TestDowngradePath:
    """A hook-granted shell auto-approve is re-judged off-loop and can be downgraded."""

    @staticmethod
    def _event(command: str | None, is_shell: bool = True):
        class _Event:
            pass

        event = _Event()
        event.is_shell = is_shell  # type: ignore[attr-defined]
        event.shell_command = command  # type: ignore[attr-defined]
        return event

    def test_clean_name_is_not_downgraded(self, world):
        system_dir, _ = world
        _program(system_dir, "head")
        from kiro_crew.dashboard.chat_runner import _name_grant_refusal_for

        assert asyncio.run(_name_grant_refusal_for(self._event("head x"))) is None

    def test_shadowed_name_is_downgraded(self, world):
        system_dir, user_dir = world
        _program(system_dir, "head")
        _program(user_dir, "head")
        from kiro_crew.dashboard.chat_runner import _name_grant_refusal_for

        refusal = asyncio.run(_name_grant_refusal_for(self._event("head x")))
        assert refusal is not None
        assert refusal.code == name_grant.SHADOWED

    def test_non_shell_and_commandless_events_are_left_alone(self, world):
        from kiro_crew.dashboard.chat_runner import _name_grant_refusal_for

        assert asyncio.run(_name_grant_refusal_for(self._event("head x", is_shell=False))) is None
        assert asyncio.run(_name_grant_refusal_for(self._event(None))) is None


class TestWindowsPaths:
    """A path this tokenizer cannot preserve must not become a silent pass.

    Runs on POSIX by patching the platform flag, because the module-level skip
    keeps the rest of this file off Windows: the fixtures rely on the POSIX
    execute bit. That skip is why this gap existed at all, and it is stated in
    the PR rather than papered over.
    """

    def test_windows_refuses_every_name(self, world, monkeypatch):
        # GPT 5.6 rounds 11 and 12, compounding: POSIX tokenization destroys a
        # backslash path (leaving a bare name that resolves nowhere, which is
        # ALLOWED), and `cmd.exe` searches the command's own directory before
        # PATH -- a directory this check, running in the gateway's, cannot see.
        system_dir, _ = world
        _program(system_dir, "head")
        monkeypatch.setattr(name_grant.platform_compat, "IS_WINDOWS", True)
        for command in (r"C:\workspace\tool.exe run", "head file", "find ."):
            refusal = name_grant.name_grant_refusal(command)
            assert refusal is not None, command
            assert refusal.code == name_grant.WINDOWS_UNMODELLED, command

    def test_the_tokenizer_really_does_destroy_it(self):
        # Half the premise, pinned: if this stops being true the refusal can
        # narrow to the lookup-order half alone.
        assert name_grant.program_names(r"C:\workspace\tool.exe run") == ["C:workspacetool.exe"]

    def test_posix_backslash_is_left_alone(self, world):
        # On POSIX a backslash IS an escape, and the program name survives, so
        # these must keep working.
        system_dir, _ = world
        _program(system_dir, "grep")
        _program(system_dir, "find")
        assert name_grant.name_grant_refusal(r"grep '\d+' file") is None
        assert name_grant.name_grant_refusal(r"find . -name \*.py") is None


class TestHookTierIsUntouched:
    """The hook layer keeps its own behaviour; the name check is not in it."""

    def test_read_only_tier_still_auto_approves(self, world):
        result = HookManager().on_tool_call("list", command="ls -la", is_shell=True)
        assert result.action == TOOL_AUTO_APPROVE

    def test_shadowed_name_is_no_longer_the_hook_layer_s_business(self, world):
        # It grants; the async caller re-judges and downgrades. Pinning this stops
        # a future edit from quietly reintroducing filesystem work on the loop.
        system_dir, user_dir = world
        _program(system_dir, "ls")
        _program(user_dir, "ls")
        result = HookManager().on_tool_call("list", command="ls -la", is_shell=True)
        assert result.action == TOOL_AUTO_APPROVE

    def test_non_shell_tools_are_untouched(self, world):
        cfg = HooksConfig(auto_approve_tools=["ReadFile"])
        assert HookManager(cfg).on_tool_call("ReadFile").action == TOOL_AUTO_APPROVE

    def test_non_system_program_is_granted_here_and_judged_by_the_caller(self, world):
        # The hook layer no longer defers a non-system program: it grants, and the
        # async caller decides. `TestDowngradePath` is where the decision is
        # pinned; this only records that the hook layer stopped doing filesystem
        # work of its own.
        _, user_dir = world
        _program(user_dir, "gh")
        cfg = HooksConfig(auto_approve_tools=["Running: gh *"])
        result = HookManager(cfg).on_tool_call(
            "Running: gh pr view 1", command="gh pr view 1", is_shell=True
        )
        assert result.action == TOOL_AUTO_APPROVE
