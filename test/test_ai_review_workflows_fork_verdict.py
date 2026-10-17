"""Review-workflow tests for how a fork lane captures, publishes and passes its
verdict, and how it honours an accepted human override.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from ai_review_workflows_helpers import (
    ROOT,
    _bash,
    _child_env,
    _fork_child_env,
    _github_outputs,
    _proc_log,
    _review_contract_module,
    _step_script,
    _workflow,
    _write_stub,
)

#: The two FORK advisory lanes whose verdict capture runs under ``set -uo pipefail``,
#: with the header each one reads. The same-repo ``design-review.yml`` and
#: ``first-principles-review.yml`` are deliberately absent: their post-summary steps
#: carry no ``set -`` line, so an unmatched ``grep``'s status is masked by the
#: pipeline's last command and the step cannot die this way. Adding them here would
#: pin a property they do not have, and would invite a ``pipefail`` to be added to
#: them later without the capture being converted first.
_FORK_VERDICT_LANES = (
    ("fork-design-review.yml", "Design-Verdict:"),
    ("fork-first-principles-review.yml", "First-Principles-Verdict:"),
)


class TestForkVerdictCaptureNeverAbortsAboveItsOwnFallback:
    """A fork advisory lane must live to read the fallback written one line below it.

    Both steps end their capture with ``[ -n "$v" ] && verdict="$v"`` and start it
    with ``verdict="UNKNOWN"``, which is a complete, fail-closed answer for a review
    that names no verdict. Neither line runs if the capture itself takes the step
    down: the lane then writes no verdict output at all, and the comment that would
    have named the cause is never posted.

    Two ordinary model outcomes do exactly that, under the ``bash -e`` a ``run:``
    block with no ``shell:`` key gets plus the ``set -uo pipefail`` these steps add:

    * a review with NO header -- ``grep`` matches nothing and exits 1, and
      ``pipefail`` promotes that status to the pipeline's, so the assignment fails;
    * a review with MANY headers -- ``head -n1`` closes the pipe and its producer
      dies of SIGPIPE (141). This is the worse arm: a value is already in hand, so
      an expression that merely suppressed the status would hand the lane a verdict
      read out of a document the step never finished reading.

    Each step's OWN expression is run, extracted by shape rather than by the program
    it names, so a pin keyed to one tool cannot keep passing after the capture is
    rewritten. Both the status and the value are asserted: asserting the value alone
    passes an expression that returns the right answer and kills the step anyway.

    The two Security Scope lanes read their header with the same single-pass awk,
    for the same reason. The three sites in these files that ALREADY use a
    here-string (each with a comment saying a long summary must not manufacture a
    SIGPIPE status under ``pipefail``) show the rule applies here too: the surviving
    pipeline is the one place in these steps where it is not met.
    """

    #: Headers in the many-headers review. Large enough to fill the pipe buffer and
    #: make ``head -n1``'s exit close it on its producer -- measured to need more
    #: than a few thousand on Git-for-Windows Bash, where a 64 KiB buffer swallows
    #: the smaller body whole -- and still a fixture of a couple of megabytes.
    _MANY_HEADERS = 60_000

    #: Review body kinds. Every one is an ordinary model outcome, not malformed
    #: input. The last three CARRY a verdict, and they are what pins the conversion
    #: as behaviour-preserving: an expression that never aborted and also stopped
    #: reading verdicts would satisfy the status assertion on its own.
    _REVIEW_KINDS = (
        "no-header",
        "header-shaped-prose",
        "many-headers",
        "plain",
        "lowercase",
        "crlf",
    )

    #: The fallback line, which is also how the capture is IDENTIFIED: the capture
    #: is by definition the assignment this line reads, so the variable name comes
    #: from the fallback rather than from a guess. Both steps ALSO trim their
    #: summary to the header with a second assignment mentioning the same header,
    #: and a first-match-wins selector silently measures that one instead.
    _FALLBACK = '[ -n "$v" ] && verdict="$v"'

    def _capture_line(self, workflow: str, header: str) -> str:
        """The verdict ASSIGNMENT in *workflow*, named by what the fallback reads.

        Found by SHAPE, never by the program it runs: a selector keyed to ``grep``
        or to ``awk`` stops finding the capture the day the capture changes tool,
        and then measures nothing while still passing.
        """
        text = _workflow(workflow)
        assert self._FALLBACK in text, f"{workflow}: {self._FALLBACK!r} is gone"
        name = self._FALLBACK.split("-n ", 1)[1].split("]", 1)[0].strip().strip('"$')
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if header.lower() not in stripped.lower():
                continue
            if stripped.startswith(f'{name}="$('):
                return stripped
        raise AssertionError(f"{workflow}: no {header} capture assigned to {name!r}")

    def _body(self, kind: str, header: str) -> tuple[str, str]:
        """The review text for *kind*, and the value the capture must yield for it."""
        if kind == "no-header":
            return "The change refuses nothing new.\n\nNothing to check.\n", ""
        if kind == "header-shaped-prose":
            # The whole header, colon and all, but NOT at the start of a record:
            # the anchor is the only thing that tells it from a real verdict line,
            # so this case fails the moment a rewrite drops the `^`. (Stripping the
            # colon instead would be rejected by the colon, not the anchor, and the
            # case could not catch an unanchored read.)
            return f"I would write {header} PASS as a header if the contract asked.\n", ""
        if kind == "plain":
            return f"{header} CONCERNS\n\nOne thing to watch.\n", "CONCERNS"
        if kind == "lowercase":
            # The capture upper-cases, so every later comparison sees one spelling.
            return f"{header.lower()} block\n", "BLOCK"
        if kind == "crlf":
            # A model emitting CRLF must not leave a carriage return inside the
            # value: that would make each `[ "$verdict" = "BLOCK" ]` test false.
            return f"{header} BLOCK\r\nrest\r\n", "BLOCK"
        body = f"{header} PASS\n" + f"{header} BLOCK\n" * self._MANY_HEADERS
        return body, "PASS"

    @pytest.mark.parametrize(("workflow", "header"), _FORK_VERDICT_LANES)
    def test_the_step_still_carries_the_fallback_this_pin_is_about(
        self, workflow: str, header: str
    ) -> None:
        """If the fallback goes, the test below is measuring something else."""
        text = _workflow(workflow)
        assert 'verdict="UNKNOWN"' in text, (
            f"{workflow}: no UNKNOWN default, so an aborted capture is no longer "
            "distinguishable from a lane that simply read no verdict"
        )
        assert '[ -n "$v" ] && verdict="$v"' in text, (
            f"{workflow}: the capture's own fallback is gone; this pin asserts that "
            "the capture lives long enough to reach it"
        )

    @pytest.mark.parametrize(("workflow", "header"), _FORK_VERDICT_LANES)
    @pytest.mark.parametrize("review_kind", _REVIEW_KINDS)
    def test_every_ordinary_review_reaches_the_fallback_with_the_right_value(
        self, workflow: str, header: str, review_kind: str, tmp_path: Path
    ) -> None:
        bash = _bash()
        if bash is None:
            pytest.skip("the capture is Bash; skip where Bash is absent")
        body, expected = self._body(review_kind, header)
        review = tmp_path / "review.md"
        review.write_text(body, encoding="utf-8")
        line = self._capture_line(workflow, header)
        name = line.split("=", 1)[0]
        # The step's own preamble: `pipefail` is what turns an unmatched grep into
        # the pipeline's status, and `bash -e` below is what the runner supplies for
        # a `run:` block with no `shell:` key. Running without either is what would
        # let this pin pass while the lane died.
        script = "\n".join(
            [
                "set -uo pipefail",
                'summary="$(cat "$IN")"',
                line,
                f'printf %s "${name}"',
            ]
        )
        out = subprocess.run(
            [bash, "-e", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "IN": str(review)},
            cwd=tmp_path,
        )
        assert out.returncode == 0, (
            f"{workflow}: a {review_kind} review aborted the step "
            f"(rc={out.returncode}) under the runner's own `bash -e`, above the "
            f"fallback written for it: {out.stderr.strip()}"
        )
        # Clipped: the many-headers body is megabytes, and a failure message that
        # prints it whole buries the one line saying what went wrong.
        got = out.stdout if len(out.stdout) <= 120 else out.stdout[:120] + "..."
        assert out.stdout == expected, (
            f"{workflow}: a {review_kind} review captured {got!r} "
            f"({len(out.stdout)} chars), expected {expected!r}"
        )


# ---- A fork lane consumes the accepted override record ---------------------
# The Stage-2 fork lanes read the bot-written `/ai-review override` record for
# their own lane at their exact head before any credential or model call. An
# accepted record skips the review and publishes the human decision -- a
# `success` check-run and the override note -- as the same-repo lanes do. Each
# condition fails closed, and a feed the step could not read leaves the lane
# reviewing normally.

_FORK_OVERRIDE_TARGETS = (
    ("fork-opus-review.yml", "fable"),
    ("fork-gpt-review.yml", "gpt"),
    ("fork-design-review.yml", "design"),
    ("fork-ux-review.yml", "ux"),
    ("fork-first-principles-review.yml", "first-principles"),
    ("fork-security-scope-review.yml", "scope"),
)

_OVERRIDE_GATE = "steps.human_override.outputs.active != 'true'"

#: Steps an accepted override skips that are not a credential or claude-code-action
#: step: the GPT lane's own model calls and CLI setup, and every step that fetches
#: the untrusted description or its attachments.
_OVERRIDE_SKIPPED_STEPS = {
    "fork-gpt-review.yml": (
        "Install review CLI",
        "Configure the review CLI for Amazon Bedrock",
        "GPT 6.1 review (discovery pass)",
        "GPT 6.1 review (falsification pass)",
        "Fetch PR intent (stated purpose — data only, for the scope check)",
    ),
    "fork-design-review.yml": (
        "Collect rendered evidence",
        "Capture the PR intent (untrusted data file)",
    ),
    "fork-ux-review.yml": (
        "Collect review evidence (description attachments and committed media)",
        "Capture the PR intent (untrusted data file)",
    ),
    "fork-first-principles-review.yml": ("Fetch PR intent (untrusted data file)",),
}


def _gated_on_the_override(step: dict) -> bool:
    """The override gate is a TOP-LEVEL conjunct of the step's `if:`.

    A substring match would accept `x || <gate>`, which runs the step whenever
    `x` holds, override or not.
    """
    cond = " ".join(str(step.get("if") or "").split())
    if cond == _OVERRIDE_GATE:
        return True
    lead = _OVERRIDE_GATE + " && ("
    if not cond.startswith(lead):
        return False
    # The parenthesis opened after the gate must close at the very end, or a
    # trailing `|| x` would run the step whenever `x` holds.
    depth = 1
    for i, ch in enumerate(cond[len(lead) :], start=len(lead)):
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if depth == 0:
            return i == len(cond) - 1
    return False


class TestForkLaneConsumesTheOverrideRecord:
    HEAD = "1234567890abcdef1234567890abcdef12345678"
    OTHER = "aaaa567890abcdef1234567890abcdef1234aaaa"

    def _record(self, target: str, head: str, login: str = "github-actions[bot]", lead: str = ""):
        return {
            "user": {"login": login},
            "body": (
                f"{lead}<!-- ai-review-human-override target={target} head={head} "
                "actor=alice source=42 -->\n## Human judgment recorded\n\n> a reason"
            ),
        }

    def _run(self, tmp_path: Path, workflow: str, comments: list | None):
        bash = _bash()
        if bash is None or os.name == "nt":
            pytest.skip("the resolve step is Bash with a stubbed gh; POSIX only")
        stub_dir = tmp_path / "stub"
        stub_dir.mkdir()
        attempts = tmp_path / "attempts"
        if comments is None:
            # What gh does on an HTTP error: the API's JSON error body on STDOUT,
            # the message on stderr, a non-zero exit.
            _write_stub(
                stub_dir / "gh",
                f'printf x >> "{attempts}"\n'
                'printf \'{"message":"Resource not accessible by integration","status":"403"}\'\n'
                'echo "gh: Resource not accessible by integration (HTTP 403)" >&2\n'
                "exit 1\n",
            )
        else:
            reply = tmp_path / "comments.json"
            reply.write_text(json.dumps(comments), encoding="utf-8")
            _write_stub(stub_dir / "gh", f'printf x >> "{attempts}"\ncat "{reply}"\n')
        _write_stub(stub_dir / "sleep", "exit 0\n")
        script = tmp_path / "step.sh"
        script.write_text(
            _step_script(_workflow(workflow), "Resolve human override"),
            encoding="utf-8",
            newline="\n",
        )
        out = tmp_path / "github-output"
        result = subprocess.run(
            [bash, "-e", str(script)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=tmp_path,
            env=_fork_child_env(
                tmp_path,
                stub_dir,
                REPO="example/repo",
                PR="7",
                HEAD=self.HEAD,
                GITHUB_OUTPUT=str(out),
            ),
        )
        return result, _github_outputs(out), attempts

    @pytest.mark.parametrize(("workflow", "target"), _FORK_OVERRIDE_TARGETS)
    def test_an_accepted_record_for_this_lane_and_head_is_active(
        self, tmp_path: Path, workflow: str, target: str
    ) -> None:
        result, outputs, _ = self._run(tmp_path, workflow, [self._record(target, self.HEAD)])
        assert result.returncode == 0, _proc_log(result)
        assert outputs == {"active": "true", "actor": "alice", "source": "42"}, _proc_log(result)

    @pytest.mark.parametrize(("workflow", "target"), _FORK_OVERRIDE_TARGETS)
    def test_a_record_for_all_lanes_is_active(self, tmp_path: Path, workflow: str, target: str):
        result, outputs, _ = self._run(tmp_path, workflow, [self._record("all", self.HEAD)])
        assert result.returncode == 0, _proc_log(result)
        assert outputs["active"] == "true", _proc_log(result)

    @pytest.mark.parametrize(
        "case", ["wrong-head", "wrong-lane", "not-the-bot", "not-leading", "short-head"]
    )
    @pytest.mark.parametrize(("workflow", "target"), _FORK_OVERRIDE_TARGETS)
    def test_a_record_that_fails_any_condition_is_ignored(
        self, tmp_path: Path, workflow: str, target: str, case: str
    ) -> None:
        other_lane = "gpt" if target != "gpt" else "design"
        record = {
            "wrong-head": self._record(target, self.OTHER),
            "wrong-lane": self._record(other_lane, self.HEAD),
            "not-the-bot": self._record(target, self.HEAD, login="mallory"),
            "not-leading": self._record(target, self.HEAD, lead="quoting: "),
            # The handler writes the FULL head; a prefix names no exact head.
            "short-head": self._record(target, self.HEAD[:12]),
        }[case]
        result, outputs, _ = self._run(tmp_path, workflow, [record])
        assert result.returncode == 0, _proc_log(result)
        assert outputs == {"active": "false", "actor": "", "source": ""}, _proc_log(result)

    @pytest.mark.parametrize(("workflow", "target"), _FORK_OVERRIDE_TARGETS)
    def test_a_failed_read_falls_back_to_the_model_review(
        self, tmp_path: Path, workflow: str, target: str
    ) -> None:
        # Never an accepted record, and never a failed step: the lane reviews
        # normally, and the JSON error body gh printed on stdout is not read as
        # a comment feed.
        result, outputs, attempts = self._run(tmp_path, workflow, None)
        assert result.returncode == 0, _proc_log(result)
        assert outputs == {"active": "false", "actor": "", "source": ""}, _proc_log(result)
        assert "::warning::" in result.stdout
        assert "Reviewing normally" in result.stdout
        assert attempts.read_text(encoding="utf-8") == "xxx"

    def test_the_resolve_step_is_one_body_across_the_fork_lanes(self) -> None:
        bodies = set()
        for workflow, target in _FORK_OVERRIDE_TARGETS:
            script = _step_script(_workflow(workflow), "Resolve human override")
            # The slice runs up to the next step's `- name:`, so it carries that
            # step's leading comment block; only the step itself is compared.
            script = script[: script.index('\n} >> "$GITHUB_OUTPUT"')]
            needle = f'exact="<!-- ai-review-human-override target={target} head=$HEAD "'
            assert script.count(needle) == 1, workflow
            bodies.add(
                script.replace(needle, 'exact="<!-- ai-review-human-override target=@ head=$HEAD "')
            )
        assert len(bodies) == 1, "the fork lanes' Resolve human override steps drifted apart"

    @pytest.mark.parametrize(("workflow", "target"), _FORK_OVERRIDE_TARGETS)
    def test_no_credential_or_model_step_runs_under_an_accepted_record(
        self, workflow: str, target: str
    ) -> None:
        doc = yaml.safe_load(_workflow(workflow))
        seen = 0
        for job in doc["jobs"].values():
            steps = job["steps"]
            names = [s.get("name") for s in steps]
            named = _OVERRIDE_SKIPPED_STEPS.get(workflow, ())
            for i, step in enumerate(steps):
                uses = str(step.get("uses") or "")
                if (
                    "configure-aws-credentials" not in uses
                    and "claude-code-action" not in uses
                    and step.get("name") not in named
                ):
                    continue
                seen += 1
                # Read in the SAME job, before the step it gates.
                assert "Resolve human override" in names[:i], (workflow, step.get("name"))
                assert _gated_on_the_override(step), (workflow, step.get("name") or uses)
        assert seen >= 2 + len(_OVERRIDE_SKIPPED_STEPS.get(workflow, ())), workflow

    @pytest.mark.parametrize(("workflow", "target"), _FORK_OVERRIDE_TARGETS[:5])
    def test_the_record_is_read_after_the_lanes_row_is_open(
        self, workflow: str, target: str
    ) -> None:
        # The handler posts the record and then reads the lane's row; the lane
        # opens its row and then reads the record. Either order alone leaves a
        # window in which neither sees the other.
        names = [
            s.get("name")
            for s in yaml.safe_load(_workflow(workflow))["jobs"][
                next(iter(yaml.safe_load(_workflow(workflow))["jobs"]))
            ]["steps"]
        ]
        assert names.index("Open check-run (in progress)") < names.index("Resolve human override")

    def test_the_scope_lane_reads_the_record_again_before_it_decides(self) -> None:
        # Its only check-run is the one `publish` posts, so a record posted while
        # `generate`, `validate` or `adjudicate` ran would have no row for the
        # handler to find. `publish` repeats the read just before deciding.
        workflow = _workflow("fork-security-scope-review.yml")
        doc = yaml.safe_load(workflow)
        publish = doc["jobs"]["publish"]["steps"]
        names = [s.get("name") for s in publish]
        i = names.index("Resolve human override")
        assert i < names.index("Decide the lane's conclusion")
        assert publish[i]["if"] == "always()"
        assert publish[i]["env"]["HEAD"] == "${{ needs.generate.outputs.head_sha }}"
        # One read, byte for byte the same in both jobs.
        scripts = {
            s["run"]
            for job in ("generate", "publish")
            for s in doc["jobs"][job]["steps"]
            if s.get("name") == "Resolve human override"
        }
        assert len(scripts) == 1
        either = (
            "${{ needs.generate.outputs.override == 'true' "
            "|| steps.human_override.outputs.active == 'true' }}"
        )
        for step in (
            "Decide the lane's conclusion",
            "Assemble the comment body",
            "Publish check-run",
        ):
            assert (
                next(s for s in publish if s.get("name") == step)["env"]["HUMAN_OVERRIDE"] == either
            )


class TestForkLaneFinalizeHonoursTheOverride:
    """The check-run a fork lane completes is the row readiness reads."""

    HEAD = "1234567890abcdef1234567890abcdef12345678"
    LANES = (
        # workflow, finalize step, check name, how a BLOCK verdict reaches the step
        ("fork-design-review.yml", "Finalize check-run (advisory)", "Design Review", "env"),
        ("fork-ux-review.yml", "Finalize check-run (advisory)", "UX Review", "env"),
        (
            "fork-first-principles-review.yml",
            "Finalize check-run (advisory)",
            "First Principles Review",
            "env",
        ),
        ("fork-opus-review.yml", "Finalize check-run (fail closed)", "Opus 5.5 Review", "OPUS"),
        ("fork-gpt-review.yml", "Finalize check-run (fail closed)", "GPT 6.1 Review", "GPT"),
    )

    def _run(
        self, tmp_path: Path, workflow: str, step: str, how: str, *, override: bool, check_id: str
    ):
        bash = _bash()
        if bash is None or os.name == "nt":
            pytest.skip("the finalize step is Bash with a stubbed gh; POSIX only")
        stub_dir = tmp_path / "stub"
        stub_dir.mkdir()
        calls = tmp_path / "calls"
        calls.mkdir()
        _write_stub(
            stub_dir / "gh",
            'if [ "$1" = "api" ] && [ "$2" = "--method" ] && [ "$3" = "PATCH" ]; then\n'
            f'  printf \'%s\\n\' "$@" >> "{calls}/patch-argv.txt"; exit 0\n'
            "fi\n"
            'if [ "$1" = "api" ] && [ "$2" = "--method" ] && [ "$3" = "POST" ]; then\n'
            f'  printf \'%s\\n\' "$@" >> "{calls}/post-argv.txt"; echo 1; exit 0\n'
            "fi\n"
            "exit 0\n",
        )
        _write_stub(stub_dir / "sleep", "exit 0\n")
        cwd = tmp_path / "workspace"
        cwd.mkdir()
        runner_temp = tmp_path / "runner-temp"
        runner_temp.mkdir()
        env = {
            "REPO": "example/repo",
            "PR": "7",
            "HEAD": self.HEAD,
            "CHECK_ID": check_id,
            "WR_RUN_ID": "900",
            "WR_RUN_ATTEMPT": "2",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_RUN_ID": "5551",
            "RUNNER_TEMP": str(runner_temp),
            "HUMAN_OVERRIDE": "true" if override else "false",
            "OVERRIDE_ACTOR": "alice" if override else "",
            "ADJ_DECISION": "",
            "ADJ_NOTE": "",
            "WITHHELD": "",
            "UNFETCHED": "",
        }
        if how == "env":
            env["VERDICT"] = "BLOCK"
        else:
            out = "claude-review-output.md" if how == "OPUS" else "codex-review-output.md"
            (cwd / out).write_text(
                f"BLOCKING -- src/a.py:1 -- x\n[BLOCK-MERGE] {self.HEAD}\n[{how}-REVIEWED] {self.HEAD}\n",
                encoding="utf-8",
            )
        script = tmp_path / "step.sh"
        script.write_text(_step_script(_workflow(workflow), step), encoding="utf-8", newline="\n")
        result = subprocess.run(
            [bash, "-e", str(script)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=cwd,
            env=_fork_child_env(tmp_path, stub_dir, **env),
        )
        return result, calls

    @pytest.mark.parametrize(("workflow", "step", "name", "how"), LANES)
    def test_an_accepted_override_completes_the_row_success(
        self, tmp_path: Path, workflow: str, step: str, name: str, how: str
    ) -> None:
        # A BLOCK left behind by the skipped review steps does not outvote it.
        result, calls = self._run(tmp_path, workflow, step, how, override=True, check_id="4242")
        assert result.returncode == 0, _proc_log(result)
        argv = (calls / "patch-argv.txt").read_text(encoding="utf-8")
        assert "conclusion=success" in argv and "conclusion=failure" not in argv, argv
        assert f"output[title]={name} — human override accepted" in argv, argv
        assert "@alice" in argv
        assert "<!-- ai-review-fork-lane run=5551 -->" in argv, argv

    @pytest.mark.parametrize(("workflow", "step", "name", "how"), LANES)
    def test_the_fallback_post_carries_the_conclusion_the_verdict_earned(
        self, tmp_path: Path, workflow: str, step: str, name: str, how: str
    ) -> None:
        # No opening row to complete: the fallback POST is the row readiness
        # reads, so a BLOCK must not publish as a neutral pass.
        result, calls = self._run(tmp_path, workflow, step, how, override=False, check_id="")
        assert result.returncode == 0, _proc_log(result)
        argv = (calls / "post-argv.txt").read_text(encoding="utf-8")
        assert "conclusion=failure" in argv, argv
        assert "conclusion=neutral" not in argv and "conclusion=success" not in argv, argv
        assert "external_id=" in argv and "-pr-7-900-2" in argv, argv
        assert "<!-- ai-review-fork-lane run=5551 -->" in argv, argv

    @pytest.mark.parametrize(("workflow", "step", "name", "how"), LANES)
    def test_the_fallback_post_records_an_accepted_override(
        self, tmp_path: Path, workflow: str, step: str, name: str, how: str
    ) -> None:
        result, calls = self._run(tmp_path, workflow, step, how, override=True, check_id="")
        assert result.returncode == 0, _proc_log(result)
        argv = (calls / "post-argv.txt").read_text(encoding="utf-8")
        assert "conclusion=success" in argv, argv
        assert f"output[title]={name} — human override accepted" in argv, argv


class TestForkRequiredLanePassesOnlyAPublishedVerdict:
    """A fork lane's clean verdict is a pass only once it is in the PR's slot.

    The output file is written before publication, so its stamp proves a verdict
    was computed and nothing about whether the PR shows it. The finalize step is
    the row PR Readiness reads, so a pass decided from the file alone presents a
    green required check over a slot that still holds another head's verdict.
    """

    HEAD = "1234567890abcdef1234567890abcdef12345678"
    LANES = (
        ("fork-opus-review.yml", "Opus 5.5 Review", "claude-review-output.md", "OPUS"),
        ("fork-gpt-review.yml", "GPT 6.1 Review", "codex-review-output.md", "GPT"),
    )
    STEP = "Finalize check-run (fail closed)"

    def _run(
        self,
        tmp_path: Path,
        workflow: str,
        out: str,
        stamp: str,
        *,
        publication: str | None,
        check_id: str = "4242",
        block: bool = False,
        adj_decision: str = "",
        override: bool = False,
    ):
        bash = _bash()
        assert bash is not None, "these cases need bash (Git Bash on Windows)"
        calls = tmp_path / "calls"
        calls.mkdir()
        # The stubs are shell FUNCTIONS sourced through BASH_ENV rather than
        # scripts on PATH: Git for Windows' bash prepends its own bin directories
        # to PATH, which would shadow a PATH stub. `jq` is the sweep's check-name
        # encoder only, and its output feeds nothing but the `gh` stub.
        stubs = tmp_path / "stubs.sh"
        stubs.write_text(
            "gh() {\n"
            '  if [ "$1" = "api" ] && [ "$2" = "--method" ] && [ "$3" = "PATCH" ]; then\n'
            '    printf \'%s\\n\' "$@" >> "$STUB_CALLS/patch-argv.txt"; return 0\n'
            "  fi\n"
            '  if [ "$1" = "api" ] && [ "$2" = "--method" ] && [ "$3" = "POST" ]; then\n'
            '    printf \'%s\\n\' "$@" >> "$STUB_CALLS/post-argv.txt"; echo 1; return 0\n'
            "  fi\n"
            "  return 0\n"
            "}\n"
            "sleep() { :; }\n"
            "jq() { :; }\n",
            encoding="utf-8",
            newline="\n",
        )
        cwd = tmp_path / "workspace"
        cwd.mkdir()
        body = f"[{stamp}-REVIEWED] {self.HEAD}\n"
        if block:
            body = f"BLOCKING -- src/a.py:1 -- x\n[BLOCK-MERGE] {self.HEAD}\n" + body
        (cwd / out).write_text(body, encoding="utf-8", newline="\n")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": tmp_path.as_posix(),
            "TMPDIR": tmp_path.as_posix(),
            "GH_TOKEN": "stub-token",
            "GH_CONFIG_DIR": (tmp_path / "gh-config").as_posix(),
            "BASH_ENV": stubs.as_posix(),
            "STUB_CALLS": calls.as_posix(),
            "REPO": "example/repo",
            "PR": "7",
            "HEAD": self.HEAD,
            "CHECK_ID": check_id,
            "WR_RUN_ID": "900",
            "WR_RUN_ATTEMPT": "2",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_RUN_ID": "5551",
            "RUNNER_TEMP": tmp_path.as_posix(),
            "HUMAN_OVERRIDE": "true" if override else "false",
            "OVERRIDE_ACTOR": "alice" if override else "",
            "ADJ_DECISION": adj_decision,
            "ADJ_NOTE": "",
        }
        if publication is not None:
            env["PUBLICATION"] = publication
        script = tmp_path / "step.sh"
        script.write_text(
            _step_script(_workflow(workflow), self.STEP), encoding="utf-8", newline="\n"
        )
        result = subprocess.run(
            [bash, "-e", str(script)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=cwd,
            env=_child_env(env),
        )
        assert result.returncode == 0, _proc_log(result)
        row = "patch-argv.txt" if check_id else "post-argv.txt"
        return (calls / row).read_text(encoding="utf-8")

    @pytest.mark.parametrize(("workflow", "name", "out", "stamp"), LANES)
    @pytest.mark.parametrize("publication", [None, "", "not_published", "withheld", "override"])
    def test_a_clean_verdict_that_missed_the_slot_fails(
        self, tmp_path: Path, workflow: str, name: str, out: str, stamp: str, publication
    ) -> None:
        # No record at all is the post step failing or never running, and it
        # fails closed exactly like a record that says the write did not land.
        # `override` is not a record the post step writes: an override for
        # this head reaches the finalize step through its own input instead.
        argv = self._run(tmp_path, workflow, out, stamp, publication=publication)
        assert "conclusion=failure" in argv and "conclusion=success" not in argv, argv
        assert f"output[title]={name} — verdict not published" in argv, argv
        assert "Re-run this lane to publish it." in argv, argv

    @pytest.mark.parametrize(("workflow", "name", "out", "stamp"), LANES)
    @pytest.mark.parametrize("publication", ["published", "superseded"])
    def test_a_published_verdict_keeps_its_own_conclusion(
        self, tmp_path: Path, workflow: str, name: str, out: str, stamp: str, publication: str
    ) -> None:
        argv = self._run(tmp_path, workflow, out, stamp, publication=publication)
        assert "conclusion=success" in argv and "conclusion=failure" not in argv, argv
        assert f"output[title]={name} — no blocking findings" in argv, argv

    @pytest.mark.parametrize(("workflow", "name", "out", "stamp"), LANES)
    def test_the_fallback_row_carries_the_same_failure(
        self, tmp_path: Path, workflow: str, name: str, out: str, stamp: str
    ) -> None:
        # With no opening row to complete, the fallback POST is what readiness
        # reads, so it must not re-derive a pass from the file either.
        argv = self._run(tmp_path, workflow, out, stamp, publication="not_published", check_id="")
        assert "conclusion=failure" in argv and "conclusion=success" not in argv, argv
        assert f"output[title]={name} — verdict not published" in argv, argv

    @pytest.mark.parametrize(("workflow", "name", "out", "stamp"), LANES)
    def test_an_unpublished_blocking_verdict_still_reads_as_blocking(
        self, tmp_path: Path, workflow: str, name: str, out: str, stamp: str
    ) -> None:
        argv = self._run(tmp_path, workflow, out, stamp, publication="not_published", block=True)
        assert "conclusion=failure" in argv, argv
        assert f"output[title]={name} — changes requested (blocking)" in argv, argv

    @pytest.mark.parametrize(("workflow", "name", "out", "stamp"), LANES)
    @pytest.mark.parametrize("publication", [None, "not_published"])
    def test_an_accepted_override_for_this_head_still_passes(
        self, tmp_path: Path, workflow: str, name: str, out: str, stamp: str, publication
    ) -> None:
        # The override is decided AFTER the publication check, from its own
        # input, so a skipped review with no publication record and a BLOCK
        # left in the output file still completes the row success.
        argv = self._run(
            tmp_path, workflow, out, stamp, publication=publication, block=True, override=True
        )
        assert "conclusion=success" in argv and "conclusion=failure" not in argv, argv
        assert f"output[title]={name} — human override accepted" in argv, argv

    def test_an_adjudicated_clearance_that_missed_the_slot_fails(self, tmp_path: Path) -> None:
        # The GPT lane's second way to a pass, a fully reconciled clearance,
        # is a pass on the same terms as a clean verdict.
        argv = self._run(
            tmp_path,
            "fork-gpt-review.yml",
            "codex-review-output.md",
            "GPT",
            publication="not_published",
            block=True,
            adj_decision="cleared",
        )
        assert "conclusion=failure" in argv and "conclusion=success" not in argv, argv
        assert "output[title]=GPT 6.1 Review — verdict not published" in argv, argv

    @pytest.mark.parametrize(("workflow", "name", "out", "stamp"), LANES)
    def test_the_finalize_step_reads_the_post_steps_record(
        self, workflow: str, name: str, out: str, stamp: str
    ) -> None:
        doc = yaml.safe_load(_workflow(workflow))
        steps = [s for job in doc["jobs"].values() for s in job.get("steps", [])]
        post = [s for s in steps if s.get("id") == "post"]
        assert len(post) == 1 and "guarded_comment_upsert" in post[0]["run"]
        final = next(s for s in steps if s.get("name") == self.STEP)
        assert final["env"]["PUBLICATION"] == "${{ steps.post.outputs.publication }}"


class TestForkLaneSurfacesAnUnstampedReviewBody:
    """A completed fork review whose verdict stamp is missing must stay readable.

    Both fork lanes decide ``kind`` from the presence of ``[<NAME>-REVIEWED]
    <head>`` in the captured output. When a review runs to completion but that
    stamp is absent or SHA-corrupted -- a clean review can carry a truncated
    sha -- ``kind`` falls to ``incomplete`` and the body goes only to the job
    logs behind a one-line "no verdict" notice. A reader then cannot tell a
    clean review with a mangled stamp from a review that produced nothing, and
    those need opposite responses.

    The branch prints the captured body with every marker DE-BRACKETED, which is
    what keeps this a reporting change: ``REVIEWED_STAMP_RE`` and
    ``BLOCK_MERGE_RE`` both anchor on a literal ``[``, so the surfaced body is
    inert to the parser in exactly the way an absent body is. The check-run
    conclusion is re-derived independently and still fails closed.

    Deliberately NOT surfaced: the pass-failure stub. If GPT's pass 1 fails,
    pass 2 still runs but against an empty discovery block, so its output
    reviewed nothing; publishing it as findings would misrepresent it.
    """

    HEAD = "07bdb01215b9161c90ae28b11296bfc60ad30646"
    # (workflow, captured-output filename, comment step, finalize step, stamp name)
    LANES = (
        (
            "fork-gpt-review.yml",
            "codex-review-output.md",
            "Post/update summary comment",
            "GPT",
        ),
        (
            "fork-opus-review.yml",
            "claude-review-output.md",
            "Post/update summary comment",
            "OPUS",
        ),
    )

    def _findings_body(self, stamp: str | None) -> str:
        """A complete review with two findings; ``stamp`` None means unstamped."""
        body = (
            "BLOCKING -- src/kiro_crew/apps/builtins/thing/app.json:2 -- new built-in app\n"
            "Anchor: no-new-builtin-apps\n"
            "\n"
            "BLOCKING -- src/kiro_crew/dashboard/chat_folders.py:618 -- blocking call on the loop\n"
            "Anchor: no-blocking-call-on-event-loop\n"
            "\n"
            f"[BLOCK-MERGE] {self.HEAD}\n"
        )
        if stamp is not None:
            body += f"[{stamp}-REVIEWED] {self.HEAD}\n"
        return body

    def _run_step(
        self,
        tmp_path: Path,
        *,
        workflow: str,
        output_file: str,
        step: str,
        review_output: str | None,
        check_id: str = "",
    ) -> tuple[Path, "subprocess.CompletedProcess[bytes]"]:
        bash = _bash()
        if bash is None or shutil.which("jq") is None:
            pytest.skip("fork-lane comment tests require Bash and jq")
        if os.name == "nt":
            pytest.skip("stubbed-PATH gh interception is exercised on POSIX runners")

        stub_dir = tmp_path / "stub"
        stub_dir.mkdir()
        calls_dir = tmp_path / "calls"
        calls_dir.mkdir()
        cwd = tmp_path / "workspace"
        cwd.mkdir()
        runner_temp = tmp_path / "runner-temp"
        runner_temp.mkdir()

        if review_output is not None:
            (cwd / output_file).write_text(review_output, encoding="utf-8")

        gh_stub = stub_dir / "gh"
        gh_stub.write_text(
            "#!/usr/bin/env bash\n"
            "# Records the argv of every mutating call and answers the comment\n"
            "# finder with an empty array, so the step takes its create path.\n"
            'if [ "$1" = "api" ] && [ "$2" = "--method" ] && [ "$3" = "PATCH" ]; then\n'
            '  printf \'%s\\n\' "$@" >> "$STUB_CALLS/patch-argv.txt"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "api" ] && [ "$2" = "--method" ] && [ "$3" = "POST" ]; then\n'
            '  printf \'%s\\n\' "$@" >> "$STUB_CALLS/post-argv.txt"\n'
            '  echo "1"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "api" ]; then\n'
            '  echo -n ""\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "pr" ] && [ "$2" = "comment" ]; then\n'
            '  printf \'%s\\n\' "$3" >> "$STUB_CALLS/create-calls.txt"\n'
            "  shift 3\n"
            '  if [ -n "${STUB_CREATE_FAIL:-}" ]; then\n'
            "    # STUB_CREATE_LANDS emulates the lost-ack partial failure that\n"
            "    # makes a create unsafe to repeat: GitHub ACCEPTS the POST, so\n"
            "    # the comment now exists, but the CLI still reports failure.\n"
            "    # The body is written into the finder fixture, so the step's own\n"
            "    # confirmation query sees it through real jq.\n"
            '    if [ -n "${STUB_CREATE_LANDS:-}" ] && [ "$1" = "--body-file" ]; then\n'
            '      jq -n --arg b "$(cat "$2")" \\\n'
            "        '[{id:777,user:{login:\"github-actions[bot]\"},body:$b}]' \\\n"
            '        > "$FINDER_COMMENTS_FILE"\n'
            "    fi\n"
            "    exit 7\n"
            "  fi\n"
            '  if [ "$1" = "--body-file" ]; then cp "$2" "$STUB_CALLS/created-body.md"; fi\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        gh_stub.chmod(0o755)

        # `sleep` is an external command, so a shim earlier on PATH intercepts
        # the retry backoff without a test-only knob in the workflow: the lanes
        # keep their real production budget and these tests do not wait it out.
        # Records each interval, so the SCHEDULE is assertable rather than just
        # the attempt count.
        sleep_stub = stub_dir / "sleep"
        sleep_stub.write_text(
            "#!/usr/bin/env bash\n"
            'printf \'%s\\n\' "$1" >> "$STUB_CALLS/sleeps.txt"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        sleep_stub.chmod(0o755)

        script_file = tmp_path / "step.sh"
        script = _step_script(_workflow(workflow), step)
        # Each lane assembles its comment under `$RUNNER_TEMP`, which this harness
        # points at a per-test directory. That is what stops two parametrized cases
        # from racing on one path and reading each other's body -- which is how a
        # stamped case's live markers first showed up in the unstamped case's
        # assertions. Assert the path stays RUNNER_TEMP-scoped, so a regression to a
        # bare `/tmp` (shared between xdist workers, and the operator's own machine
        # when the suite runs locally) fails here instead of silently restoring the
        # race.
        if step == "Post/update summary comment":
            assert re.search(
                r"\$\{RUNNER_TEMP:-[^}]*\}/fork-(?:codex|opus)-comment\.md", script
            ), f"{workflow}: comment path must be RUNNER_TEMP-scoped, not a bare /tmp path"
        # `newline="\n"`: text-mode translation writes CRLF on Windows and bash
        # reads the CR as part of the token (`fi\r` is not the `fi` keyword). These
        # cases skip on Windows today, so this is belt-and-braces.
        script_file.write_text(script, encoding="utf-8", newline="\n")

        env = {
            **os.environ,
            "PATH": f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "REPO": "example/repo",
            "PR": "1",
            "HEAD": self.HEAD,
            "GH_TOKEN": "stub-token",
            "RUNNER_TEMP": str(runner_temp),
            "STUB_CALLS": str(calls_dir),
            # The step records its publication outcome in GITHUB_OUTPUT, and
            # the inherited value is the CI job's own file, which this test
            # must neither write into nor depend on being writable.
            "GITHUB_OUTPUT": str(tmp_path / "github-output.txt"),
            "CHECK_ID": check_id,
            "ADJ_DECISION": "",
            "ADJ_NOTE": "",
        }
        result = subprocess.run(
            [bash, "-e", str(script_file)],
            check=False,
            capture_output=True,
            cwd=cwd,
            env=_child_env(env),
        )
        return calls_dir, result

    @pytest.mark.parametrize(("workflow", "output_file", "step", "stamp"), LANES)
    def test_a_completed_review_with_no_stamp_is_surfaced_not_dropped(
        self, tmp_path: Path, workflow: str, output_file: str, step: str, stamp: str
    ) -> None:
        calls, result = self._run_step(
            tmp_path,
            workflow=workflow,
            output_file=output_file,
            step=step,
            review_output=self._findings_body(stamp=None),
        )
        assert result.returncode == 0, result.stderr.decode()
        posted = (calls / "created-body.md").read_text(encoding="utf-8")

        # The lane still reports that it has no verdict -- that is true.
        assert "No completed" in posted
        # ...and the findings it DID produce are now readable on the PR rather
        # than only in the job logs. Both findings, not just the first.
        assert "no-new-builtin-apps" in posted
        assert "no-blocking-call-on-event-loop" in posted
        assert "chat_folders.py:618" in posted
        # Framed so the body is not mistaken for a verdict.
        assert "NOT a verdict" in posted

    @pytest.mark.parametrize(("workflow", "output_file", "step", "stamp"), LANES)
    def test_the_surfaced_body_is_inert_to_the_real_marker_parsers(
        self, tmp_path: Path, workflow: str, output_file: str, step: str, stamp: str
    ) -> None:
        """The invariant that keeps this a reporting change.

        Surfacing a raw body would publish ``[<NAME>-REVIEWED] <head>`` for a
        review that never completed, and pr_status.py reads that stamp straight
        out of comment text -- an unstamped review would start reading as freshly
        reviewed. De-bracketing keeps the downstream view identical to today's.
        """
        contract = _review_contract_module()
        calls, result = self._run_step(
            tmp_path,
            workflow=workflow,
            output_file=output_file,
            step=step,
            # Worst case: the body carries BOTH a foreign reviewed-stamp and a
            # blocking marker, while lacking this lane's own stamp for the head.
            review_output=self._findings_body(stamp="SOMEOTHER"),
        )
        assert result.returncode == 0, result.stderr.decode()
        posted = (calls / "created-body.md").read_text(encoding="utf-8")

        # The body was surfaced...
        assert "no-new-builtin-apps" in posted
        # ...and carries nothing either parser can read.
        assert contract.REVIEWED_STAMP_RE.findall(posted) == []
        assert contract.BLOCK_MERGE_RE.findall(posted) == []
        # The de-bracketed forms are present, so a human can still see what the
        # model emitted and that it was neutralized rather than deleted.
        assert "SOMEOTHER-REVIEWED-UNSTAMPED" in posted
        assert "BLOCK-MERGE-UNSTAMPED" in posted

    @pytest.mark.parametrize(("workflow", "output_file", "step", "stamp"), LANES)
    def test_the_check_run_still_fails_closed_when_the_stamp_is_missing(
        self, tmp_path: Path, workflow: str, output_file: str, step: str, stamp: str
    ) -> None:
        """Nothing blocks less: the conclusion is re-derived from the same
        missing stamp and is unchanged by the reporting branch."""
        calls, result = self._run_step(
            tmp_path,
            workflow=workflow,
            output_file=output_file,
            step="Finalize check-run (fail closed)",
            review_output=self._findings_body(stamp=None),
            check_id="4242",
        )
        assert result.returncode == 0, result.stderr.decode()
        argv = (calls / "patch-argv.txt").read_text(encoding="utf-8")
        assert "conclusion=failure" in argv
        assert "conclusion=success" not in argv

    @pytest.mark.parametrize(("workflow", "output_file", "step", "stamp"), LANES)
    def test_a_marker_split_across_lines_is_still_neutralized(
        self, tmp_path: Path, workflow: str, output_file: str, step: str, stamp: str
    ) -> None:
        """The bypass that a sha-anchored pattern would leave open.

        Python's ``\\s+`` spans newlines; line-oriented ``sed`` cannot. So a body
        emitting ``[GPT-REVIEWED]\\n<sha>`` would slip past a pattern that required
        a sha on the same line, while pr_status.py still read it as a live stamp.
        That spelling lands in THIS branch precisely because the lane's own
        ``grep -Fq`` is single-line too and so fails to find the marker. The
        neutralization therefore matches the bracketed token alone.
        """
        contract = _review_contract_module()
        split_body = (
            "BLOCKING -- src/foo.py:1 -- something\n"
            f"[BLOCK-MERGE]\n{self.HEAD}\n"
            f"[{stamp}-REVIEWED]\n{self.HEAD}\n"
        )
        # Precondition: the parser really does read the split spelling as live,
        # otherwise this test would pass for the wrong reason.
        assert contract.REVIEWED_STAMP_RE.findall(split_body) == [(stamp, self.HEAD)]
        assert contract.BLOCK_MERGE_RE.findall(split_body) == [self.HEAD]

        calls, result = self._run_step(
            tmp_path,
            workflow=workflow,
            output_file=output_file,
            step=step,
            review_output=split_body,
        )
        assert result.returncode == 0, result.stderr.decode()
        posted = (calls / "created-body.md").read_text(encoding="utf-8")

        assert contract.REVIEWED_STAMP_RE.findall(posted) == []
        assert contract.BLOCK_MERGE_RE.findall(posted) == []

    @pytest.mark.parametrize(("workflow", "output_file", "step", "stamp"), LANES)
    def test_credential_shaped_unstamped_output_is_withheld_entirely(
        self, tmp_path: Path, workflow: str, output_file: str, step: str, stamp: str
    ) -> None:
        """Surfacing an unstamped body must not become a credential exfil path.

        The reviewer runs agentically with credentials in its environment, and a
        malicious fork can inject a prompt that BOTH dumps that environment and
        omits the verdict stamp -- which routes into this branch by construction,
        because the lane's stamp check is what sends it here. Before this branch
        existed nothing was posted, so publishing the body is the exposure. The
        redaction earlier in the lane catches an AWS key ID and named
        ``key=value`` forms but not a bare secret value, a GitHub token or a PEM
        block, so any surviving credential shape must withhold the WHOLE body.
        """
        secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0"  # noqa: S105 - shape, not a key
        calls, result = self._run_step(
            tmp_path,
            workflow=workflow,
            output_file=output_file,
            step=step,
            review_output=f"BLOCKING -- src/foo.py:1 -- finding\nenv dump: {secret}\n",
        )
        assert result.returncode == 0, result.stderr.decode()
        posted = (calls / "created-body.md").read_text(encoding="utf-8")

        # The credential shape never reaches the PR...
        assert secret not in posted
        # ...and neither does the body that carried it -- withheld wholesale, not
        # partially scrubbed, because a partial scrub is what missed it already.
        assert "src/foo.py:1" not in posted
        assert "withheld" in posted
        # The lane still reports honestly that it has no verdict.
        assert "No completed" in posted

    @pytest.mark.parametrize(("workflow", "output_file", "step", "stamp"), LANES)
    def test_a_stamped_review_keeps_its_markers_intact(
        self, tmp_path: Path, workflow: str, output_file: str, step: str, stamp: str
    ) -> None:
        """The neutralization must not leak out of the unstamped branch.

        A properly stamped blocking review still publishes live markers, because
        pr_status.py's freshness and blocking reads depend on them.
        """
        contract = _review_contract_module()
        calls, result = self._run_step(
            tmp_path,
            workflow=workflow,
            output_file=output_file,
            step=step,
            review_output=self._findings_body(stamp=stamp),
        )
        assert result.returncode == 0, result.stderr.decode()
        posted = (calls / "created-body.md").read_text(encoding="utf-8")

        assert (stamp, self.HEAD) in contract.REVIEWED_STAMP_RE.findall(posted)
        assert self.HEAD in contract.BLOCK_MERGE_RE.findall(posted)
        assert "UNSTAMPED" not in posted


class TestForkScopeLaneHonoursTheOverride:
    HEAD = "1234567890abcdef1234567890abcdef12345678"

    def _run(self, tmp_path: Path, step: str, real_python: bool = False, **env: str):
        bash = _bash()
        if bash is None or os.name == "nt":
            pytest.skip("the publish steps are Bash with a stubbed gh; POSIX only")
        stub_dir = tmp_path / "stub"
        stub_dir.mkdir()
        gh_calls = tmp_path / "gh-calls"
        # Any gh call from an override run is a defect: the floor's read of the
        # prior check-runs is exactly what an accepted override skips.
        _write_stub(stub_dir / "gh", f'printf \'%s\\n\' "$*" >> "{gh_calls}"\nexit 1\n')
        if real_python:
            # The conclusion table, run from the repository root like the job runs it.
            _write_stub(stub_dir / "python3", f'exec "{sys.executable}" "$@"\n')
        else:
            _write_stub(
                stub_dir / "python3", 'echo "python3 must not run under an override" >&2\nexit 1\n'
            )
        out = tmp_path / "github-output"
        script = tmp_path / "step.sh"
        script.write_text(
            _step_script(_workflow("fork-security-scope-review.yml"), step),
            encoding="utf-8",
            newline="\n",
        )
        result = subprocess.run(
            [bash, "-e", str(script)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=ROOT if real_python else tmp_path,
            env=_fork_child_env(
                tmp_path,
                stub_dir,
                HEAD=self.HEAD,
                PR="7",
                REPO="example/repo",
                GITHUB_OUTPUT=str(out),
                **env,
            ),
        )
        return result, _github_outputs(out), gh_calls

    def test_an_accepted_override_concludes_success_past_the_floor(self, tmp_path: Path) -> None:
        result, outputs, gh_calls = self._run(
            tmp_path,
            "Decide the lane's conclusion",
            HUMAN_OVERRIDE="true",
            OVERRIDE_ACTOR="alice",
            IN_SCOPE="true",
            FOLD_RC="1",
            MODEL_VERDICT="BLOCK",
            REVIEW=str(tmp_path / "absent.md"),
            BODY=str(tmp_path / "absent-body.md"),
        )
        assert result.returncode == 0, _proc_log(result)
        assert outputs["conclusion"] == "success", outputs
        assert outputs["settled"] == "yes", outputs
        assert "@alice" in outputs["title"], outputs
        assert not gh_calls.exists(), gh_calls.read_text(encoding="utf-8")

    def test_without_an_override_the_floor_is_consulted(self, tmp_path: Path) -> None:
        # Control: the arm is keyed on the record. Without it the step reaches the
        # per-head floor, whose read of the prior check-runs the override skips.
        result, outputs, gh_calls = self._run(
            tmp_path,
            "Decide the lane's conclusion",
            real_python=True,
            HUMAN_OVERRIDE="false",
            IN_SCOPE="true",
            FOLD_RC="1",
            MODEL_VERDICT="BLOCK",
            REVIEW=str(tmp_path / "absent.md"),
            BODY=str(tmp_path / "absent-body.md"),
        )
        assert outputs.get("conclusion") != "success", (outputs, _proc_log(result))
        assert "check-runs" in gh_calls.read_text(encoding="utf-8"), _proc_log(result)

    def test_the_note_is_stamped_for_this_head_like_the_same_repo_one(self, tmp_path: Path) -> None:
        body = tmp_path / "scope-comment.md"
        result, _, _ = self._run(
            tmp_path,
            "Assemble the comment body",
            HUMAN_OVERRIDE="true",
            OVERRIDE_ACTOR="alice",
            CONCLUSION="success",
            OUT=str(body),
            REVIEW=str(tmp_path / "absent.md"),
            BODY=str(tmp_path / "absent-body.md"),
        )
        assert result.returncode == 0, _proc_log(result)
        text = body.read_text(encoding="utf-8")
        assert text.startswith("<!-- security-scope-review -->\n"), text
        assert "✅ human override accepted" in text and "@alice" in text
        assert f"[SCOPE-REVIEWED] {self.HEAD}" in text

    def test_validate_and_adjudicate_are_skipped_under_an_override(self) -> None:
        doc = yaml.safe_load(_workflow("fork-security-scope-review.yml"))
        assert "needs.generate.outputs.override != 'true'" in doc["jobs"]["validate"]["if"]
        # adjudicate runs only on validate's own answer, so skipping validate
        # skips it.
        assert "needs.validate.outputs.adjudicate == 'true'" in doc["jobs"]["adjudicate"]["if"]
        outputs = doc["jobs"]["generate"]["outputs"]
        assert outputs["override"] == "${{ steps.human_override.outputs.active }}"
        # The read needs the comment feed and nothing more.
        assert doc["jobs"]["generate"]["permissions"]["pull-requests"] == "read"
