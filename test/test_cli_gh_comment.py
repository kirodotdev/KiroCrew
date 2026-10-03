"""``kirocrew gh-comment`` and the instructions that tell agents to use it.

An agent comments on a GitHub issue from its own shell, so the safe path has to be
a command it can run there: it redacts the body, REFUSES it (exit 3, nothing sent)
when its raw markdown would start the Kiro Agent app, and otherwise sends it
byte-for-byte with the agent's own ``gh`` as JSON on stdin. It rewrites nothing.
Every ``gh`` here is a fake on ``PATH``, so no test reaches GitHub.

The instruction half pins what the Issue Radar crew brief and the pipeline
conductor skill tell an agent: both name this command, neither shows a raw way to
post an issue comment except to forbid it, and the command each one shows runs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path

import pytest

from kiro_crew import cli_gh_comment, github_comment_safety, security

_REPO_ROOT = Path(__file__).resolve().parents[1]
BRIEF = _REPO_ROOT / "src/kiro_crew/apps/builtins/issue_radar/backend/crew_brief.md"
CONDUCTOR = _REPO_ROOT / "src/kiro_crew/builtin_skills/pipeline-conductor/SKILL.md"
INSTRUCTIONS = (BRIEF, CONDUCTOR)
REPO = "kirodotdev/KiroCrew"  # brand-ok: the repository slug

_FAKE_GH = """\
import json, os, sys
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}) + "\\n")
if os.environ.get("FAKE_GH_FAIL"):
    sys.stderr.write("gh: HTTP 404: Not Found\\n")
    sys.exit(1)
print(json.dumps({"id": 42, "html_url": "https://github.invalid/c/42"}))
"""


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    """A ``gh`` first on ``PATH`` that records each call and answers like the API."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "fake_gh.py"
    script.write_text(_FAKE_GH, encoding="utf-8")
    posix = bin_dir / "gh"
    posix.write_text(f"#!{sys.executable}\n" + _FAKE_GH, encoding="utf-8")
    posix.chmod(0o755)
    (bin_dir / "gh.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    log = tmp_path / "gh-calls.jsonl"
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    monkeypatch.delenv("FAKE_GH_FAIL", raising=False)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))

    def calls() -> list[dict]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    return calls


def _run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kirocrew gh-comment")
    cli_gh_comment.configure_parser(parser)
    return cli_gh_comment.gh_comment_cmd(parser.parse_args(argv))


def _run_kirocrew(argv: list[str], tmp_path: Path, monkeypatch) -> int:
    """``kirocrew <argv>`` through the real entry point, as an agent's shell runs it."""
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["kirocrew", *argv])
    from kiro_crew.cli import main

    try:
        main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


#: What a crew writes under the brief's guidance: references as `#N`, a commit as a
#: short sha, a path without its `src/` prefix.
SAFE = "Fixed by #16047 in `kiro_crew/x.py line 10`, landed at 641d6dd."
#: The same report written with pasted links and an on-disk path.
UNSAFE = "Fixed by https://github.com/kirodotdev/KiroCrew/pull/16047 in src/kiro_crew/x.py"


def _body_file(tmp_path: Path, text: str, name: str = "body.md") -> str:
    """Write *text* under *tmp_path* and return the path, as a caller does.

    ``--body-file`` is the only body source, so every verb test writes the body to
    a file first: a comment is markdown over several lines, and a file keeps it off
    argv and out of the shell's quoting.
    """
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


# ── post / edit ──────────────────────────────────────────────────────────────


def test_post_sends_the_body_byte_for_byte_as_json_on_stdin(fake_gh, tmp_path, capsys):
    rc = _run(
        ["post", "--repo", REPO, "--number", "15139", "--body-file", _body_file(tmp_path, SAFE)]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"id": 42, "url": "https://github.invalid/c/42"}
    (call,) = fake_gh()
    assert call["argv"] == [
        "api",
        f"repos/{REPO}/issues/15139/comments",
        "--method",
        "POST",
        "--input",
        "-",
    ]
    # Byte-for-byte: nothing rewrote a word of the crew's comment.
    assert json.loads(call["stdin"]) == {"body": SAFE}


def test_edit_patches_the_comment_with_the_body_unchanged(fake_gh, tmp_path):
    argv = [
        "edit",
        "--repo",
        REPO,
        "--comment-id",
        "777",
        "--body-file",
        _body_file(tmp_path, SAFE),
    ]
    assert _run(argv) == 0
    (call,) = fake_gh()
    assert call["argv"][:4] == ["api", f"repos/{REPO}/issues/comments/777", "--method", "PATCH"]
    assert json.loads(call["stdin"])["body"] == SAFE


def test_the_body_never_rides_on_argv(fake_gh, tmp_path):
    body = _body_file(tmp_path, "marker-text here")
    _run(["post", "--repo", REPO, "--number", "1", "--body-file", body])
    (call,) = fake_gh()
    assert not any("marker-text" in arg for arg in call["argv"])


@pytest.mark.parametrize("action", ["post", "edit"])
def test_a_triggered_body_is_refused_with_exit_3_and_nothing_is_sent(
    action, fake_gh, tmp_path, capsys
):
    target = ["--number", "1"] if action == "post" else ["--comment-id", "7"]
    body = _body_file(tmp_path, UNSAFE)
    rc = _run([action, "--repo", REPO, *target, "--body-file", body])
    assert rc == cli_gh_comment.EXIT_REFUSED == 3
    err = capsys.readouterr().err
    assert "REFUSED, nothing sent:" in err
    # The refusal names each place and the replacement to write there.
    assert "-> #16047" in err
    assert "-> kiro_crew/x.py" in err
    assert fake_gh() == []


def test_the_command_exposes_no_way_to_rewrite_a_body(fake_gh, tmp_path):
    with pytest.raises(SystemExit) as exc:
        _run(["neutralize", "--repo", REPO, "--body-file", _body_file(tmp_path, SAFE)])
    assert exc.value.code == 2
    assert fake_gh() == []


# ── one test per crew / conductor write path ─────────────────────────────────
#
# The parametrized test above covers the two VERBS on a one-line body. These cover
# the four PATHS a crew or the conductor actually writes an issue comment on, each
# with the body that path produces and each at its own trigger, so a path that
# stopped being refused fails under its own name.

_PASTED_PR = f"https://github.com/{REPO}/pull/16047"
_MARKER = "<!-- kirocrew-crew v=1 id=c_7f3a phase={phase} pr=16047 updated=2026-10-03T06:00Z -->"


def _claim(*progress: str, phase: str = "implementing", head: str = "#16047") -> str:
    """A claim comment in the crew brief's format, with *progress* folded into it."""
    log = "\n".join(f"- `06:0{n}` {line}" for n, line in enumerate(progress))
    return (
        "👻 **Andromeda** is on this · Kiro Crew Issue Radar\n"
        f"{phase} · {head} · updated 06:00 UTC\n\n"
        "<details><summary>progress</summary>\n\n"
        f"{log}\n\n</details>\n\n" + _MARKER.format(phase=phase)
    )


def _refusal(rc: int, capsys, calls) -> str:
    """Assert the refusal happened with nothing sent, and return its stderr."""
    assert rc == cli_gh_comment.EXIT_REFUSED == 3
    err = capsys.readouterr().err
    assert "REFUSED, nothing sent:" in err
    assert calls() == []
    return err


def test_the_claim_post_is_refused_when_the_claim_body_holds_a_pasted_pr_link(
    fake_gh, tmp_path, capsys
):
    # Step 1 of Claiming: the crew's first comment on the issue.
    body = _claim("claimed — read the issue and the 4 call sites", head=_PASTED_PR)
    file = _body_file(tmp_path, body, "claim.md")
    rc = _run(["post", "--repo", REPO, "--number", "16242", "--body-file", file])
    assert "-> #16047" in _refusal(rc, capsys, fake_gh)


def test_the_progress_check_in_edit_is_refused_when_a_progress_line_holds_a_src_path(
    fake_gh, tmp_path, capsys
):
    # Step 4 of Claiming: the crew edits that same comment as work progresses.
    body = _claim(
        "claimed — read the issue and the 4 call sites",
        "root cause in src/kiro_crew/x.py — fix plus a test that fails first",
    )
    file = _body_file(tmp_path, body, "checkin.md")
    rc = _run(["edit", "--repo", REPO, "--comment-id", "777", "--body-file", file])
    assert "-> kiro_crew/x.py" in _refusal(rc, capsys, fake_gh)


def test_the_takeover_edit_is_refused_when_the_dead_claim_text_holds_the_trigger(
    fake_gh, tmp_path, capsys
):
    # Taking over a dead claim: the note is APPENDED, so the dead crew's own text
    # is re-sent and checked again. The brief's answer to this refusal is to leave
    # that comment untouched, label the issue for a person, and skip it.
    dead = _claim("opened the pull request", phase="preempted", head=_PASTED_PR)
    body = (
        f"{dead}\n\n"
        "Claim taken over by 👻 **Perseus** · Kiro Crew Issue Radar\n"
        "The claim above was last updated 2026-10-03T06:00Z, with no activity on\n"
        "the issue since — past this installation's claim TTL.\n"
    )
    file = _body_file(tmp_path, body, "takeover.md")
    rc = _run(["edit", "--repo", REPO, "--comment-id", "778", "--body-file", file])
    assert "-> #16047" in _refusal(rc, capsys, fake_gh)


def test_the_conductor_stand_down_post_is_refused_when_the_evidence_holds_a_pasted_link(
    fake_gh, tmp_path, capsys
):
    # The conductor unclaims on a stand-down and leaves an evidence comment, which
    # goes through this same command.
    body = (
        "Standing down on this item and unclaiming it: the fix landed in\n"
        f"{_PASTED_PR}, so there is nothing left here to dispatch.\n"
    )
    file = _body_file(tmp_path, body, "standdown.md")
    rc = _run(["post", "--repo", REPO, "--number", "16242", "--body-file", file])
    assert "-> #16047" in _refusal(rc, capsys, fake_gh)


@pytest.mark.parametrize("action", ["post", "edit"])
def test_the_body_has_exactly_one_source(action, fake_gh, tmp_path, capsys):
    # A body is a file and nothing else. Omitting --body-file is a usage error, and
    # the inline flag is gone: `--body <text>` is argparse's prefix of --body-file,
    # so the text is taken for a path and fails the read. Either way no inline text
    # can reach gh.
    target = ["--number", "1"] if action == "post" else ["--comment-id", "7"]
    with pytest.raises(SystemExit) as exc:
        _run([action, "--repo", REPO, *target])
    assert exc.value.code == 2
    assert _run([action, "--repo", REPO, *target, "--body", SAFE]) == cli_gh_comment.EXIT_FAILED
    assert "cannot read the body" in capsys.readouterr().err
    assert fake_gh() == []


def test_a_failed_gh_call_is_exit_1(fake_gh, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_GH_FAIL", "1")
    body = _body_file(tmp_path, "hello")
    assert _run(["post", "--repo", REPO, "--number", "1", "--body-file", body]) == 1
    assert "404" in capsys.readouterr().err


def test_an_empty_body_file_is_refused_before_gh_runs(fake_gh, tmp_path):
    body = _body_file(tmp_path, "  \n")
    assert _run(["post", "--repo", REPO, "--number", "1", "--body-file", body]) == 1
    assert fake_gh() == []


@pytest.mark.parametrize(
    "repo", ["kirodotdev", "o/r/../../x", "o/..", "../r", "o/r?x=1", "-o/r", "o/r name"]
)
def test_a_repo_that_is_not_owner_slash_name_is_a_usage_error(fake_gh, tmp_path, repo):
    with pytest.raises(SystemExit) as exc:
        _run(["post", "--repo", repo, "--number", "1", "--body-file", _body_file(tmp_path, "x")])
    assert exc.value.code == 2
    assert fake_gh() == []


@pytest.mark.parametrize("number", ["0", "-1", "1/../2", "x"])
def test_a_number_that_is_not_positive_is_a_usage_error(fake_gh, tmp_path, number):
    with pytest.raises(SystemExit):
        _run(["post", "--repo", REPO, "--number", number, "--body-file", _body_file(tmp_path, "x")])
    assert fake_gh() == []


# ── the body-file guard ──────────────────────────────────────────────────────


def test_a_sensitive_body_file_is_refused_and_no_gh_runs(fake_gh, tmp_path, monkeypatch, capsys):
    # --body-file is model-authored, so it goes through hooks.safe_read_file: a
    # credential store must not be readable into a public comment.
    home = tmp_path / "home"
    home.mkdir()
    netrc = home / ".netrc"
    netrc.write_text("machine github.com login x password SECRET\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    rc = _run(["post", "--repo", REPO, "--number", "1", "--body-file", str(netrc)])
    assert rc == cli_gh_comment.EXIT_FAILED == 1
    captured = capsys.readouterr()
    assert "cannot read the body" in captured.err
    assert "SECRET" not in captured.err + captured.out
    assert fake_gh() == []


def test_a_sensitive_body_file_reached_through_a_symlink_is_refused(
    fake_gh, tmp_path, monkeypatch, capsys
):
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    (home / ".aws" / "credentials").write_text("[default]\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    link = tmp_path / "body.md"
    try:
        link.symlink_to(home / ".aws" / "credentials")
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not allow the test to create a symlink")
    assert _run(["post", "--repo", REPO, "--number", "1", "--body-file", str(link)]) == 1
    assert "cannot read the body" in capsys.readouterr().err
    assert fake_gh() == []


def test_an_ordinary_body_file_still_works(fake_gh, tmp_path):
    body = tmp_path / "body.md"
    body.write_text(SAFE, encoding="utf-8")
    assert _run(["post", "--repo", REPO, "--number", "1", "--body-file", str(body)]) == 0
    (call,) = fake_gh()
    assert json.loads(call["stdin"])["body"] == SAFE


def test_a_missing_body_file_is_exit_1(fake_gh, tmp_path, capsys):
    missing = tmp_path / "nope.md"
    assert _run(["post", "--repo", REPO, "--number", "1", "--body-file", str(missing)]) == 1
    assert "cannot read the body" in capsys.readouterr().err
    assert fake_gh() == []


def test_the_dashboard_has_no_body_file_equivalent():
    # The HTTP comment routes take the body in a JSON request, never a path: a
    # file-read parameter there would be a browser-reachable read of the host.
    routes = (
        _REPO_ROOT / "src/kiro_crew/apps/builtins/issue_radar/backend/crew_routes.py"
    ).read_text(encoding="utf-8")
    assert "body_file" not in routes


# ── the real entry point ─────────────────────────────────────────────────────


def test_kirocrew_dispatches_gh_comment(fake_gh, tmp_path, monkeypatch, capsys):
    rc = _run_kirocrew(
        [
            "gh-comment",
            "post",
            "--repo",
            REPO,
            "--number",
            "5",
            "--body-file",
            _body_file(tmp_path, SAFE),
        ],
        tmp_path,
        monkeypatch,
    )
    assert rc == 0
    (call,) = fake_gh()
    assert call["argv"][1] == f"repos/{REPO}/issues/5/comments"
    assert '"id": 42' in capsys.readouterr().out


def test_a_refusal_through_kirocrew_exits_3(fake_gh, tmp_path, monkeypatch):
    rc = _run_kirocrew(
        [
            "gh-comment",
            "post",
            "--repo",
            REPO,
            "--number",
            "5",
            "--body-file",
            _body_file(tmp_path, UNSAFE),
        ],
        tmp_path,
        monkeypatch,
    )
    assert rc == 3
    assert fake_gh() == []


# ── the instructions ─────────────────────────────────────────────────────────

#: Every way to put a comment on an issue without the command. ``gh api`` takes its
#: verb on either side of the path.
RAW_POSTING = re.compile(
    r"gh issue comment"
    r"|gh issue close[^`\n]*--comment"
    r"|gh api[^`\n]*(?:\b(?:POST|PATCH)\b[^`\n]*/comments|/comments[^`\n]*\b(?:POST|PATCH)\b)"
)
_SHOWN_COMMAND = re.compile(r"kirocrew gh-comment (?:post|edit)(?: --[a-z-]+ [^\s`]+)+")


def _sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.;!?])\s+|\n\s*\n|\n\s*[-*|]\s", text)


def _raw_posting_offenders(text: str) -> list[str]:
    """A raw posting form is allowed only in a sentence that forbids it."""
    return [
        sentence.strip()[:160]
        for sentence in _sentences(text)
        if RAW_POSTING.search(sentence) and not re.search(r"\b(?:never|not|no)\b", sentence, re.I)
    ]


@pytest.mark.parametrize(
    "sample",
    [
        "To comment: `gh issue comment 5 --body-file x`.",
        "Then `gh issue close 5 --reason completed --comment 'done'`.",
        "Run `gh api -X POST repos/o/r/issues/5/comments -f body=hello`.",
        "Run `gh api repos/o/r/issues/5/comments --method POST -f body=hello`.",
        "Run `gh api --method PATCH repos/o/r/issues/comments/9 -f body=hello`.",
    ],
)
def test_the_sweep_catches_every_raw_posting_form(sample):
    assert _raw_posting_offenders(sample)
    assert (
        _raw_posting_offenders(
            sample.replace("To comment", "Never comment")
            .replace("Then", "Never")
            .replace("Run", "Never run")
        )
        == []
    )


@pytest.mark.parametrize("path", INSTRUCTIONS, ids=lambda p: p.parent.name)
def test_the_instruction_names_the_command_and_forbids_the_raw_forms(path):
    text = path.read_text(encoding="utf-8")
    assert "kirocrew gh-comment post" in text
    for form in ("gh issue comment", "gh issue close --comment"):
        assert form in text, f"{path.name} must name {form!r} as forbidden"
    assert _raw_posting_offenders(text) == []


def test_the_brief_shows_the_edit_every_check_in_uses():
    # A crew keeps ONE claim comment and edits it, so the brief has to show the edit.
    assert "kirocrew gh-comment edit" in BRIEF.read_text(encoding="utf-8")


def test_the_brief_shows_the_refusal_as_the_whole_guard():
    text = " ".join(BRIEF.read_text(encoding="utf-8").split())
    # Refusing is all a crew gets, so the brief has to say what exit 3 means and
    # what to do next, and must show no verb that rewrites or previews a body.
    assert "exits 3, sends nothing" in text
    assert "rewrite the body yourself and run the command again" in text
    assert "gh-comment neutralize" not in text
    assert "gh-comment check" not in text


def _fill(command: str, body: Path) -> list[str]:
    filled = (
        command.replace("<owner>/<repo>", REPO)
        .replace("<owner/repo>", REPO)
        .replace("<n>", "15139")
        .replace("<id>", "777")
        .replace("<file>", "BODY_FILE")
    )
    assert "<" not in filled, filled
    # The path goes in after the split, so a Windows path keeps its backslashes.
    return [str(body) if word == "BODY_FILE" else word for word in shlex.split(filled)]


@pytest.mark.parametrize("path", INSTRUCTIONS, ids=lambda p: p.parent.name)
def test_every_command_the_instruction_shows_runs(path, fake_gh, tmp_path, monkeypatch):
    body = tmp_path / "body.md"
    body.write_text(SAFE, encoding="utf-8")
    commands = _SHOWN_COMMAND.findall(path.read_text(encoding="utf-8"))
    assert commands, f"{path.name} shows no runnable gh-comment command"
    for command in commands:
        argv = _fill(command, body)
        assert argv[0] == "kirocrew"
        assert _run_kirocrew(argv[1:], tmp_path, monkeypatch) == 0, command
    calls = fake_gh()
    assert len(calls) == len(commands)
    for call in calls:
        assert call["argv"][1] in (
            f"repos/{REPO}/issues/15139/comments",
            f"repos/{REPO}/issues/comments/777",
        )
        assert json.loads(call["stdin"])["body"] == SAFE


@pytest.mark.parametrize("path", INSTRUCTIONS, ids=lambda p: p.parent.name)
def test_the_shell_gate_admits_every_command_the_instruction_shows(path, tmp_path):
    # An agent runs these in its own shell, so the default deny rules must let them
    # through, or the brief tells it to do something it cannot.
    for command in _SHOWN_COMMAND.findall(path.read_text(encoding="utf-8")):
        line = shlex.join(_fill(command, tmp_path / "body.md"))
        assert security.is_denied(line) is None, line


def test_the_claim_comment_the_brief_shows_posts_through_the_command(fake_gh, tmp_path):
    # End to end: the template, filled in as the brief tells a crew to write it,
    # passes detection and reaches gh unchanged.
    text = BRIEF.read_text(encoding="utf-8")
    template = text.split("### Claim comment format", 1)[1].split("```", 2)[1]
    filled = (
        template.replace("<Name>", "Ada")
        .replace("<name>", "ada")
        .replace("<phase>", "implementing")
        .replace("<PR link if any>", "#2271")
        .replace("<HH:MM>", "20:44")
        .replace("<n>", "42")
        .replace("<crew-id>", "c_7f3a")
        .replace("<ISO8601 Z>", "2026-10-02T20:44:00Z")
    )
    assert github_comment_safety.find_triggers(filled, REPO) == []
    body = tmp_path / "claim.md"
    body.write_text(filled, encoding="utf-8")
    assert _run(["post", "--repo", REPO, "--number", "42", "--body-file", str(body)]) == 0
    (call,) = fake_gh()
    assert json.loads(call["stdin"])["body"] == filled


@pytest.mark.parametrize("action", ["post", "edit"])
def test_redaction_precedes_the_check_and_the_send(action, fake_gh, tmp_path, capsys):
    token = "ghp_" + "A" * 36
    url = "https://example.com/data?x=" + "%41" * 24
    marker = (
        "<!-- kirocrew-crew v=1 id=c_7f3a phase=implementing pr=2271 "
        "updated=2026-08-08T20:44:12Z -->"
    )
    body = f"{token}\n{url}\n\n{marker}"
    target = ["--number", "1"] if action == "post" else ["--comment-id", "7"]
    argv = [action, "--repo", REPO, *target, "--body-file", _body_file(tmp_path, body)]
    assert _run(argv) == 0
    sent = json.loads(fake_gh()[0]["stdin"])["body"]
    assert token not in sent and url not in sent
    assert "[REDACTED: credential]" in sent
    assert "[REDACTED: suspicious URL to example.com]" in sent
    # The claim marker survives the redaction passes, so a claim stays readable.
    assert marker in sent
    assert capsys.readouterr().err == ""
