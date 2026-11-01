"""Behavioural tests for the shipping narrative in .github/workflows/ship-report.yml.

The narrative step turns every merged PR's title, Goal line and "Why it
matters" section into one Bedrock Converse request, then turns the model's
answer into the Slack body. These tests run the step's own script with `aws`
stubbed, so they pin what reaches the model and what reaches the channel
without calling Bedrock:

* the request carries no `thinking` field: the default model chain answers
  `thinking: {type: "disabled"}` with a 400, which silently dropped every
  report to the fallback list;
* the model reads each PR's own description, minus URLs and template
  comments, fenced as untrusted data;
* a link in the post is never model-written: URLs the model writes are
  removed, and in mrkdwn mode a link is rebuilt only for a PR in the window;
* a truncated or failed answer falls through to the next model, and a run
  where every model fails fails the step so the deterministic template runs.

Skipped where the POSIX toolchain the scripts need (bash, jq) is unavailable,
which is the case on the Windows leg of the matrix.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ship-report.yml"

pytestmark = pytest.mark.skipif(
    not WORKFLOW.exists()
    or os.name == "nt"
    or shutil.which("bash") is None
    or shutil.which("jq") is None,
    reason="requires the workflow file plus a POSIX bash and jq",
)

TEMPLATE_STEP = "Generate template body (fallback)"

AWS_STUB = f"""#!{sys.executable}
import json, os, sys

args = sys.argv[1:]
src = args[args.index("--cli-input-json") + 1]
fixtures = os.environ["FIXTURES"]
with open(src[len("file://"):], encoding="utf-8") as fh:
    request = json.load(fh)
with open(fixtures + "/requests.jsonl", "a", encoding="utf-8") as log:
    log.write(json.dumps(request) + "\\n")
with open(fixtures + "/requests.jsonl", encoding="utf-8") as log:
    count = len(log.read().splitlines())
with open(fixtures + "/responses.json", encoding="utf-8") as fh:
    responses = json.load(fh)
reply = responses[min(count, len(responses)) - 1]
if "error" in reply:
    print(reply["error"], file=sys.stderr)
    sys.exit(254)
json.dump(reply, sys.stdout)
"""

TEMPLATE_BODY = """## Problem / Motivation

**Goal:** Let a user keep their place in the chat when they send a message.

<!-- Fill in the Goal line above: template guidance the model must not see. -->

A user reads a reply a few messages up, then sends a question about it.

## Why it matters

People who reply mid-history lose their place on every send. See https://evil.example/steal for more.

## Not a goal

- Turning off all auto-scroll.

## What changed (motivation → approach → change)

A new switch in Settings.
"""


def _pr(number: int, title: str, kind: str, body: str = TEMPLATE_BODY) -> dict:
    return {
        "number": number,
        "title": title,
        "author": {"login": "someone", "is_bot": False},
        "mergedAt": "2026-10-30T02:00:00Z",
        "url": f"https://github.com/o/r/pull/{number}",
        "body": body,
        "kind": kind,
    }


PRS = [
    _pr(101, "feat(chat): add a setting to stop jumping to the bottom on send", "Features"),
    _pr(102, "fix(memory): clamp an imported semantic confidence to 0..1", "Fixes"),
    _pr(
        103,
        "fix(chat): carry meta.files on the mid-turn steer",
        "Fixes",
        body="Bumps the steer payload.",
    ),
]


def _reply(text: str, stop: str = "end_turn", reasoning: str = "") -> dict:
    content: list[dict] = []
    if reasoning:
        content.append({"reasoningContent": {"reasoningText": {"text": reasoning}}})
    content.append({"text": text})
    return {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": stop}


def _steps() -> dict[str, dict]:
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["report"]["steps"]
    return {s.get("id", s["name"]): s for s in steps}


class Runner:
    def __init__(self, root: Path) -> None:
        self.steps = _steps()
        self.fixtures = root / "fixtures"
        self.work = root / "work"
        bindir = root / "bin"
        for d in (self.fixtures, self.work, bindir):
            d.mkdir(parents=True)
        stub = bindir / "aws"
        stub.write_text(AWS_STUB, encoding="utf-8")
        stub.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FIXTURES": str(self.fixtures),
            "GITHUB_REPOSITORY": "o/r",
            "GITHUB_RUN_ID": "1",
            "MODEL_IDS": "model-a model-b",
            "LINK_STYLE": "plain",
        }

    def run(
        self, step: str, responses: list[dict] | None = None, link_style: str = "plain"
    ) -> subprocess.CompletedProcess[str]:
        (self.fixtures / "responses.json").write_text(
            json.dumps(responses or [_reply("ok")]), encoding="utf-8"
        )
        (self.fixtures / "requests.jsonl").write_text("", encoding="utf-8")
        return subprocess.run(  # noqa: S603 - fixed argv, test-local stubs
            ["bash", "-c", self.steps[step]["run"]],
            cwd=self.work,
            env={**self.env, "LINK_STYLE": link_style},
            text=True,
            encoding="utf-8",
            capture_output=True,
        )

    def narrate(
        self,
        responses: list[dict] | None = None,
        prs: list[dict] | None = None,
        link_style: str = "plain",
    ) -> subprocess.CompletedProcess[str]:
        (self.work / "prs.json").write_text(
            json.dumps(PRS if prs is None else prs), encoding="utf-8"
        )
        return self.run("bedrock", responses, link_style)

    @property
    def requests(self) -> list[dict]:
        lines = (self.fixtures / "requests.jsonl").read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    @property
    def model_input(self) -> str:
        return self.requests[0]["messages"][0]["content"][0]["text"]

    @property
    def body(self) -> str:
        return (self.work / "body.txt").read_text(encoding="utf-8")


@pytest.fixture()
def runner(tmp_path: Path) -> Runner:
    return Runner(tmp_path)


# ── What reaches the model ───────────────────────────────────────────────────


def test_the_request_carries_no_thinking_configuration(runner: Runner) -> None:
    """`thinking: {type: "disabled"}` is a 400 on the default chain, so it must
    not be sent; maxTokens must leave room for the reasoning that runs first."""
    step = runner.narrate()
    assert step.returncode == 0, step.stderr
    request = runner.requests[0]
    assert "additionalModelRequestFields" not in request
    assert request["inferenceConfig"]["maxTokens"] >= 8000


def test_the_issue_request_carries_no_thinking_configuration(runner: Runner) -> None:
    """The issue half is a second Converse call to the same chain."""
    split = {"bugs": [_issue_like(4001)], "features": [_issue_like(4002)], "other": []}
    (runner.work / "split.json").write_text(json.dumps(split), encoding="utf-8")
    verdict = _reply(json.dumps({"must_fix": [], "bugs": "b", "features": "f"}))
    step = runner.run("issue_narrative", [verdict])
    assert step.returncode == 0, step.stderr
    request = runner.requests[0]
    assert "additionalModelRequestFields" not in request
    assert request["inferenceConfig"]["maxTokens"] >= 8000


def _issue_like(number: int) -> dict:
    return {"number": number, "title": f"issue {number}", "labels": [{"name": "bug"}]}


def test_the_model_reads_each_prs_goal_and_why_it_matters(runner: Runner) -> None:
    runner.narrate()
    text = runner.model_input
    assert "#101 [Features] feat(chat): add a setting to stop jumping to the bottom on send" in text
    assert "Goal: Let a user keep their place in the chat when they send a message." in text
    assert "Why it matters: People who reply mid-history lose their place on every send." in text


def test_template_guidance_and_other_sections_stay_out(runner: Runner) -> None:
    """Only the Goal line and the Why section are read; the HTML guidance
    comments and the remaining sections would only dilute the input."""
    runner.narrate()
    text = runner.model_input
    assert "template guidance" not in text
    assert "Turning off all auto-scroll" not in text
    assert "A new switch in Settings" not in text


def test_pr_text_reaches_the_model_without_urls(runner: Runner) -> None:
    prs = [_pr(104, "fix: see https://evil.example/t and www.evil.example/u", "Fixes")]
    runner.narrate(prs=prs)
    text = runner.model_input
    assert "evil.example" not in text
    assert "https://" not in text


def test_a_pr_without_the_template_still_contributes_its_description(runner: Runner) -> None:
    runner.narrate()
    assert (
        "#103 [Fixes] fix(chat): carry meta.files on the mid-turn steer\n  Description: Bumps the steer payload."
        in (runner.model_input)
    )


def test_each_excerpt_is_capped(runner: Runner) -> None:
    """One long description must not crowd the rest of the window out."""
    long_body = "**Goal:** " + "g" * 2000 + "\n\n## Why it matters\n\n" + "w" * 2000 + "\n"
    runner.narrate(prs=[_pr(105, "feat: big", "Features", body=long_body)])
    goal_line, why_line = runner.model_input.splitlines()[2:4]
    assert len(goal_line) <= len("  Goal: ") + 300
    assert len(why_line) <= len("  Why it matters: ") + 500


def test_pr_text_is_fenced_as_untrusted_and_instructions_are_the_system_prompt(
    runner: Runner,
) -> None:
    runner.narrate()
    request = runner.requests[0]
    assert "untrusted data" in request["system"][0]["text"]
    lines = runner.model_input.splitlines()
    opening, closing = lines[0], lines[-1]
    nonce = opening.split()[4]
    assert opening == f"--- MERGED PULL REQUESTS {nonce} (untrusted data) ---"
    assert closing == f"--- END UNTRUSTED DATA {nonce} ---"
    assert len(nonce) >= 12


# ── What reaches the channel ─────────────────────────────────────────────────


def test_urls_the_model_writes_never_reach_the_post(runner: Runner) -> None:
    reply = _reply(
        "Chat keeps your place #101, details at https://evil.example/x and www.evil.example/y."
    )
    step = runner.narrate([reply], link_style="mrkdwn")
    assert step.returncode == 0, step.stderr
    assert "evil.example" not in runner.body
    assert "(link removed)" in runner.body


def test_mrkdwn_links_are_rebuilt_only_for_this_windows_prs(runner: Runner) -> None:
    runner.narrate([_reply("Chat keeps your place #101; see also #999.")], link_style="mrkdwn")
    assert "<https://github.com/o/r/pull/101|#101>" in runner.body
    assert "#999" in runner.body
    assert "|#999>" not in runner.body


def test_plain_mode_posts_no_links(runner: Runner) -> None:
    runner.narrate([_reply("Chat keeps your place #101.")], link_style="plain")
    assert "Chat keeps your place #101." in runner.body
    assert "<" not in runner.body


def test_the_counted_line_comes_from_kind_not_from_the_model(runner: Runner) -> None:
    runner.narrate([_reply("Prose.\n\n\n\nMore prose.")])
    assert runner.body == "Prose.\n\nMore prose.\n\nMerged by type: Fixes 2, Features 1.\n"


def test_reasoning_blocks_are_not_posted(runner: Runner) -> None:
    runner.narrate([_reply("The report.", reasoning="private reasoning")])
    assert "private reasoning" not in runner.body
    assert runner.body.startswith("The report.")


# ── Degradation ──────────────────────────────────────────────────────────────


def test_a_truncated_answer_falls_through_to_the_next_model(runner: Runner) -> None:
    """A `max_tokens` stop is a narrative cut off mid-sentence; never post it."""
    step = runner.narrate([_reply("Cut off mid", stop="max_tokens"), _reply("Complete report.")])
    assert step.returncode == 0, step.stderr
    assert [r["modelId"] for r in runner.requests] == ["model-a", "model-b"]
    assert runner.body.startswith("Complete report.")
    assert "Cut off" not in runner.body


def test_a_model_error_falls_through_and_is_reported(runner: Runner) -> None:
    error = {
        "error": 'ValidationException: "thinking.type.disabled" is not supported for this model.'
    }
    step = runner.narrate([error, _reply("Second model report.")])
    assert step.returncode == 0, step.stderr
    assert "thinking.type.disabled" in step.stdout
    assert runner.body.startswith("Second model report.")


def test_every_model_failing_fails_the_step_and_leaves_no_body(runner: Runner) -> None:
    """The template step runs on `steps.bedrock.outcome != 'success'`."""
    step = runner.narrate([{"error": "boom"}, _reply("", stop="end_turn")])
    assert step.returncode != 0
    assert not (runner.work / "body.txt").exists()


def test_the_template_groups_by_the_kind_the_fetch_step_assigned(runner: Runner) -> None:
    (runner.work / "prs.json").write_text(json.dumps(PRS), encoding="utf-8")
    step = runner.run(TEMPLATE_STEP)
    assert step.returncode == 0, step.stderr
    assert runner.body.splitlines() == [
        "Features 1: #101 feat(chat): add a setting to stop jumping to the bottom on send",
        "Fixes 2: #102 fix(memory): clamp an imported semantic confidence to 0..1; "
        "#103 fix(chat): carry meta.files on the mid-turn steer",
    ]
