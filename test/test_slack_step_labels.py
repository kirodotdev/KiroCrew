"""Slack tool-step labels never show env assignments, absolute paths or the host."""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from test_slack_renderer import _Provider, _RecSlack  # noqa: E402

from kiro_crew.acp.types import (  # noqa: E402
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    AcpEvent,
)
from kiro_crew.messaging import TurnDriver  # noqa: E402
from kiro_crew.slack.renderer import SlackRenderer  # noqa: E402
from kiro_crew.slack.step_labels import (  # noqa: E402
    FALLBACK_LABEL,
    HOST_PLACEHOLDER,
    slack_step_label,
)

HOME = str(Path.home())
WORKTREE = f"{HOME}/src/project"


class TestSlackStepLabel:
    def test_the_reported_venv_command_loses_its_assignment_and_paths(self):
        raw = f"VENV={WORKTREE}/.venv; $VENV/bin/python -m pytest -q test/test_x.py"
        label = slack_step_label(raw, WORKTREE)
        assert label == "$VENV/bin/python -m pytest -q test/test_x.py"
        assert HOME not in label and "VENV=" not in label

    def test_a_path_under_the_worktree_is_relative_to_it(self):
        label = slack_step_label(f"cat {WORKTREE}/src/kiro_crew/app.py", WORKTREE)
        assert label == "cat src/kiro_crew/app.py"

    def test_the_worktree_itself_is_dot(self):
        assert slack_step_label(f"cd {WORKTREE} && git status", WORKTREE) == "cd . && git status"

    def test_a_path_under_home_is_tilde_relative(self):
        label = slack_step_label(f"ls {HOME}/.kiro/crew/workspace", WORKTREE)
        assert label == "ls ~/.kiro/crew/workspace"

    def test_any_other_absolute_path_keeps_only_its_last_parts(self):
        assert slack_step_label("python /opt/tools/bin/run.py", "") == "python …/bin/run.py"

    def test_device_and_one_component_paths_stay(self):
        assert slack_step_label("make 2>/dev/null > /tmp", "") == "make 2>/dev/null > /tmp"

    def test_prefix_and_exported_assignments_are_dropped(self):
        assert slack_step_label("PYTHONPATH=src FOO='a b' pytest -q", "") == "pytest -q"
        assert slack_step_label("export TOKEN_DIR=/x/y && make", "") == "make"
        assert slack_step_label("cd src; X=1 make test", "") == "cd src; make test"

    def test_arguments_that_look_like_assignments_are_kept(self):
        assert slack_step_label("git config user.name=bob", "") == "git config user.name=bob"
        assert slack_step_label("grep -n 'a=b' f.txt --color=never", "") == (
            "grep -n 'a=b' f.txt --color=never"
        )

    def test_urls_and_relative_paths_are_left_alone(self):
        text = "curl https://github.com/kirodotdev/KiroCrew/pull/1 -o out/x.json"
        assert slack_step_label(text, "") == text

    def test_this_hosts_name_is_hidden(self):
        host = socket.gethostname()
        if len(host) < 3:
            pytest.skip("host name too short to tell apart")
        label = slack_step_label(f"ssh {host} uptime", "")
        assert host not in label and HOST_PLACEHOLDER in label

    def test_paths_glued_to_flags_urls_and_hosts_are_scrubbed(self):
        for raw, want in (
            (f"gcc -I{WORKTREE}/inc -o{HOME}/out", "gcc -Iinc -o~/out"),
            (f"curl file://{HOME}/x", "curl file://~/x"),
            (f"rsync a box:{HOME}/b", "rsync a box:~/b"),
            (f"run --path=/opt/a:{HOME}/bin", "run --path=…/opt/a:~/bin"),
        ):
            assert slack_step_label(raw, WORKTREE) == want, raw

    def test_assignments_after_command_words_and_line_starts_are_dropped(self):
        assert slack_step_label("sudo FOO=x make", "") == "sudo make"
        assert slack_step_label("time A=1 B=2 make", "") == "time make"
        assert (
            slack_step_label("cd src\nVENV=x\n$VENV/bin/python", "") == "cd src\n$VENV/bin/python"
        )

    def test_line_breaks_are_kept_for_the_first_line_cut(self):
        assert slack_step_label("echo a\necho b", "") == "echo a\necho b"

    def test_a_windows_root_followed_by_either_separator_is_relative(self):
        # A Windows path is often written with ``/`` after a ``\``-separated root.
        cwd = "C:\\Users\\me\\src\\project"
        assert slack_step_label(f"cat {cwd}/src/app.py", cwd) == "cat src/app.py"
        assert slack_step_label(f"cat {cwd}\\src\\app.py", cwd) == "cat src\\app.py"

    def test_a_doubled_leading_slash_is_still_shortened(self):
        # POSIX keeps a leading ``//`` as a root of its own; it is still a
        # filesystem location and is shortened like ``/``.
        assert slack_step_label("cat //opt/secret/deep/file.txt", "") == "cat …/deep/file.txt"
        assert slack_step_label("//usr/local/bin/tool", "") == "…/bin/tool"
        assert slack_step_label("cat /opt/secret/deep/file.txt", "") == "cat …/deep/file.txt"

    def test_a_path_shaped_argument_that_is_not_a_location_stays(self):
        assert slack_step_label("Search for /api/v1/users", "") == "Search for /api/v1/users"
        assert slack_step_label("sed 's/a/b/' x", "") == "sed 's/a/b/' x"

    def test_a_long_assignment_chain_is_cheap(self, monkeypatch: pytest.MonkeyPatch):
        # Cheap by bound, not by a clock: the scrub only ever reads the first
        # MAX_LABEL_INPUT_CHARS of a label, however long the model made it.
        from kiro_crew.slack import step_labels

        real = step_labels._ASSIGNMENTS
        seen: list[int] = []

        class _Recording:
            def sub(self, repl, text):
                seen.append(len(text))
                return real.sub(repl, text)

        monkeypatch.setattr(step_labels, "_ASSIGNMENTS", _Recording())
        assert slack_step_label("A=1 " * 32000 + "make", "") == FALLBACK_LABEL
        assert seen == [step_labels.MAX_LABEL_INPUT_CHARS]

    def test_humanized_titles_pass_through(self):
        assert slack_step_label("Read config/loader.py", WORKTREE) == "Read config/loader.py"
        assert slack_step_label("Git status", WORKTREE) == "Git status"

    def test_a_label_that_was_only_an_assignment_reads_as_an_action(self):
        assert slack_step_label(f"VENV={WORKTREE}/.venv", WORKTREE) == FALLBACK_LABEL


class TestSlackRendererStepLabels:
    """Through the real driver and renderer: what the thread shows of a step."""

    def test_no_step_row_status_or_detail_shows_the_home_path(self):
        rec = _RecSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
        renderer.authorize_upload_root(WORKTREE)
        command = f"VENV={WORKTREE}/.venv; $VENV/bin/python -m pytest -q"
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="a",
                title=command,
                tool_kind="execute",
                tool_input=f'{{"command": "{command}"}}',
                is_shell=True,
            ),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="Tests pass."),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        asyncio.run(TurnDriver(_Provider(events), renderer, approval_mode="auto").run("x"))
        shown = [
            str(kw.get(k, ""))
            for m, kw in rec.calls
            if m in ("append_task", "set_thread_status")
            for k in ("title", "details", "status")
        ]
        assert shown, rec.calls
        assert not [s for s in shown if HOME in s or "VENV=" in s], shown
        titles = [kw["title"] for m, kw in rec.calls if m == "append_task"]
        assert titles[0].startswith("$VENV/bin/python -m pytest"), titles
