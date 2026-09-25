"""A review lane that cannot read its comment slot must name the read's error.

Every review lane guards its reads of this PR's comments with a six-attempt
budget: one read finds the lane's own verdict comment, one answers whether the
lane's slot is already taken, and one confirms on the other side of a failed
write whether that write landed. Each discards its stderr, so a spent budget
prints copies of one causeless sentence and the reader cannot tell installation
throttling -- cured by the re-run the annotation recommends -- from an expired
credential or a permissions change, each of which spends another full model
review to land in the same place.

The reads keep the last attempt's exit status and error text and report it ONCE,
inside the annotation a failed publish already writes. The text is an API error
body, so it is scrubbed and bounded before it is printed, and the reporting never
changes what the run claims to have published.

Every publishing lane is covered together because these reads sit inside
``retry_comment_write`` and ``guarded_comment_upsert``, which
test_ai_review_workflows.py holds byte-identical across the lanes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# Every lane whose comment reads this change covers. The set is not a choice:
# `retry_comment_write` and `guarded_comment_upsert` are held byte-identical
# across the publishing lanes by their own invariants in
# test_ai_review_workflows.py, and both guarded reads live inside those two
# functions, so a subset would fail those invariants rather than ship a partial
# improvement.
COVERED_LANES = (
    "claude-review.yml",
    "codex-review.yml",
    "design-review.yml",
    "first-principles-review.yml",
    "fork-design-review.yml",
    "fork-first-principles-review.yml",
    "fork-gpt-review.yml",
    "fork-opus-review.yml",
    "fork-security-scope-review.yml",
    "fork-ux-review.yml",
    "security-scope-review.yml",
    "ux-review.yml",
)

# What a failed publish appends when, and only when, a read is what failed.
NOTE_SUFFIX = "${READ_FAILURE_NOTE:+ Cause of the failed comment read: $READ_FAILURE_NOTE}"


def _bash() -> str | None:
    return shutil.which("bash")


def _text(lane: str) -> str:
    return (WORKFLOWS / lane).read_text(encoding="utf-8")


def _publish_step(lane: str) -> str:
    """The one step script that defines ``retry_comment_write``."""
    doc = yaml.safe_load(_text(lane))
    found = [
        step["run"]
        for job in doc["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step.get("run"), str) and "retry_comment_write() {" in step["run"]
    ]
    assert len(found) == 1, f"{lane}: expected one publish step, found {len(found)}"
    return found[0]


def _slice(script: str, start: str, end: str) -> str:
    """The script from the line starting with ``start`` through the line ``end``."""
    lines = script.split("\n")
    heads = [i for i, l in enumerate(lines) if l.strip().startswith(start)]
    assert len(heads) == 1, f"expected one {start!r}, found {len(heads)}"
    i = heads[0]
    tails = [j for j in range(i, len(lines)) if lines[j].strip() == end]
    assert tails, f"no {end!r} after {start!r}"
    return "\n".join(lines[i : tails[0] + 1])


def _harness(lane: str) -> str:
    """The lane's real helper plus its real ``retry_comment_write``, verbatim."""
    script = _publish_step(lane)
    helper = _slice(script, 'READ_ERR_FILE="', "}")
    fn = _slice(script, "retry_comment_write() {", "}")
    return helper + "\n" + fn + "\n"


def _shell_function(script: str, name: str) -> str:
    """One shell function's text, from its header through its closing brace."""
    lines = script.split("\n")
    heads = [i for i, l in enumerate(lines) if l.strip() == f"{name}() {{"]
    assert len(heads) == 1, f"expected one {name}(), found {len(heads)}"
    i = heads[0]
    pad = len(lines[i]) - len(lines[i].lstrip())
    for j in range(i + 1, len(lines)):
        if lines[j].strip() == "}" and len(lines[j]) - len(lines[j].lstrip()) == pad:
            return "\n".join(lines[i : j + 1])
    raise AssertionError(f"{name}() is never closed")


def _owned_regions(lane: str) -> list[str]:
    """The functions holding the guarded reads this change covers.

    ``guarded_comment_upsert`` is absent from the two same-repo model lanes,
    which publish through ``retry_comment_write`` directly.
    """
    script = _publish_step(lane)
    regions = [_shell_function(script, "retry_comment_write")]
    if "guarded_comment_upsert() {" in script:
        regions.append(_shell_function(script, "guarded_comment_upsert"))
    else:
        # the verdict read sits at step scope in these two lanes
        regions.append(script)
    return regions


def _fake_bin(tmp_path: Path, gh_body: str) -> Path:
    """A PATH holding a scripted ``gh`` and a ``sleep`` that does not wait."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text("#!/usr/bin/env bash\n" + gh_body, encoding="utf-8")
    gh.chmod(0o755)
    sleep = bin_dir / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o755)
    return bin_dir


def _run(bash: str, tmp_path: Path, bin_dir: Path, script: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["RUNNER_TEMP"] = str(tmp_path)
    env["REPO"] = "owner/repo"
    env["PR"] = "13658"
    env["HEAD"] = "0" * 40
    return subprocess.run(
        [bash, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        check=False,
    )


# --------------------------------------------------------------------------- #
# The guarded reads keep their error instead of discarding it.
# --------------------------------------------------------------------------- #
class TestTheGuardedReadsKeepTheirError:
    @pytest.mark.parametrize("lane", COVERED_LANES)
    def test_no_guarded_comment_read_discards_its_stderr(self, lane: str) -> None:
        """Every guarded read of this PR's comments captures stderr to a file.

        A read that sends its error to /dev/null cannot report a cause however
        the annotation is worded, so this is the load-bearing assertion. Scoped
        to the functions holding the budgeted reads: the one-shot
        override-notice lookup at step scope is a separate mechanism with its
        own owner, and its stderr is left as it is rather than given a second,
        competing reporter here.
        """
        for region in _owned_regions(lane):
            discarding = [
                line
                for line in region.split("\n")
                if "2>/dev/null" in line and 'startswith(\\"$' in line
            ]
            assert not discarding, f"{lane}: a guarded comments read discards stderr: {discarding}"

        script = _publish_step(lane)
        captures = [line for line in script.split("\n") if '2>"$READ_ERR_FILE"' in line]
        # three guarded reads: the slot occupancy read, the landing confirmation
        # read on the other side of the write, and the verdict lookup.
        assert len(captures) == 3, f"{lane}: expected 3 captured reads, found {len(captures)}"

    @pytest.mark.parametrize("lane", COVERED_LANES)
    def test_each_spent_budget_names_its_cause_once(self, lane: str) -> None:
        """Each read reports once, after its own decision -- not per attempt."""
        script = _publish_step(lane)
        assert script.count("note_read_failure() {") == 1, f"{lane}: one reporter, not two"
        calls = [line for line in script.split("\n") if "note_read_failure " in line]
        assert len(calls) == 3, f"{lane}: expected 3 reporter calls, found {len(calls)}"
        assert any("to find this lane's slot" in c for c in calls), f"{lane}: slot read silent"
        assert any("previous write landed" in c for c in calls), f"{lane}: landing read silent"
        assert any(
            'comment"' in c and "slot" not in c and "landed" not in c for c in calls
        ), f"{lane}: verdict read silent"

        # The per-attempt lines stay causeless: a read that later succeeds must
        # not print a summary at all.
        per_attempt = [line for line in script.split("\n") if "failed on attempt $attempt" in line]
        assert per_attempt, f"{lane}: lost the per-attempt lines"
        for line in per_attempt:
            assert "READ_FAILURE_NOTE" not in line, f"{lane}: per-attempt line carries the note"

    @pytest.mark.parametrize("lane", COVERED_LANES)
    def test_the_failed_publish_annotation_carries_the_note(self, lane: str) -> None:
        """The note rides the annotation that already reports nothing published."""
        script = _publish_step(lane)
        unpublished = [
            line
            for line in script.split("\n")
            if "could not publish it, so this run's verdict is not in the slot." in line
        ]
        assert unpublished, f"{lane}: lost the failed-publish annotation"
        for line in unpublished:
            assert NOTE_SUFFIX in line, f"{lane}: annotation does not carry the note"

    @pytest.mark.parametrize("lane", COVERED_LANES)
    def test_a_successful_publish_never_carries_the_note(self, lane: str) -> None:
        """The honest-report contract is untouched: a landed write says only that."""
        script = _publish_step(lane)
        for line in script.split("\n"):
            if "READ_FAILURE_NOTE:+" not in line:
                continue
            assert (
                "::error::" in line or "::warning::" in line
            ), f"{lane}: the note reached a line that is not a failure annotation: {line}"
            for claim in ("Published ", "Updated existing"):
                assert (
                    claim not in line
                ), f"{lane}: the note reached a line claiming a write landed: {line}"


# --------------------------------------------------------------------------- #
# The reporter's scrubbing, driven through the lane's own shell.
# --------------------------------------------------------------------------- #
class TestTheReportedErrorIsScrubbed:
    LANE = "claude-review.yml"

    def _report(self, tmp_path: Path, stderr_text: str, rc: int = 1) -> str:
        bash = _bash()
        if bash is None:
            pytest.skip("bash is unavailable on this platform")
        err = tmp_path / "err.txt"
        err.write_text(stderr_text, encoding="utf-8")
        script = (
            _harness(self.LANE)
            + f'note_read_failure {rc} "reading this PR\'s comments" "{err}"\n'
            + 'printf "%s\\n" "$READ_FAILURE_NOTE"\n'
        )
        out = _run(bash, tmp_path, _fake_bin(tmp_path, "exit 0\n"), script)
        assert out.returncode == 0, out.stderr[-2000:]
        return out.stdout.strip()

    def test_an_http_403_reaches_the_reader_with_its_status(self, tmp_path: Path) -> None:
        note = self._report(tmp_path, "gh: HTTP 403: API rate limit exceeded\n", rc=1)
        assert "exited 1" in note
        assert "HTTP 403" in note
        assert "rate limit exceeded" in note

    def test_a_permissions_failure_is_told_apart_from_throttling(self, tmp_path: Path) -> None:
        note = self._report(
            tmp_path, "gh: Resource not accessible by integration (HTTP 403)\n", rc=1
        )
        assert "Resource not accessible by integration" in note
        assert "rate limit" not in note

    def test_a_bad_credential_reaches_the_reader(self, tmp_path: Path) -> None:
        note = self._report(tmp_path, "gh: Bad credentials (HTTP 401)\n", rc=1)
        assert "Bad credentials" in note
        assert "HTTP 401" in note

    @pytest.mark.parametrize(
        "secret",
        [
            "ghp_0123456789abcdefghijABCDEFGHIJklmn",
            "ghs_0123456789abcdefghijABCDEFGHIJklmn",
            "github_pat_0123456789abcdefghij_ABCDEFGHIJklmnop",
            "AKIAIOSFODNN7EXAMPLE",
            "ASIAIOSFODNN7EXAMPLE",
        ],
    )
    def test_a_credential_shape_never_reaches_the_reader(self, tmp_path: Path, secret: str) -> None:
        note = self._report(tmp_path, f"gh: HTTP 401 while sending token {secret}\n")
        assert secret not in note
        assert "REDACTED" in note

    def test_an_authorization_header_never_reaches_the_reader(self, tmp_path: Path) -> None:
        note = self._report(
            tmp_path,
            "gh: rejected request\nAuthorization: token ghp_verysecretvaluehere0123456789\n",
        )
        assert "ghp_verysecretvaluehere0123456789" not in note
        assert "REDACTED" in note

    def test_a_bearer_token_never_reaches_the_reader(self, tmp_path: Path) -> None:
        note = self._report(tmp_path, "gh: sent Bearer aVeryLongOpaqueBearerValue123\n")
        assert "aVeryLongOpaqueBearerValue123" not in note
        assert "Bearer [REDACTED]" in note

    def test_a_cookie_never_reaches_the_reader(self, tmp_path: Path) -> None:
        note = self._report(tmp_path, "gh: HTTP 403\nCookie: _gh_sess=abcd1234sessionvalue\n")
        assert "abcd1234sessionvalue" not in note
        assert "REDACTED" in note

    def test_the_request_url_and_its_query_never_reach_the_reader(self, tmp_path: Path) -> None:
        note = self._report(
            tmp_path,
            "gh: HTTP 403 for "
            "https://api.github.com/repos/o/r/issues/1/comments?per_page=100&token=leaky\n",
        )
        assert "api.github.com" not in note
        assert "token=leaky" not in note
        assert "per_page" not in note
        assert "[REDACTED-URL]" in note
        # the status still survives, which is the whole point of keeping the text
        assert "HTTP 403" in note

    def test_an_account_id_never_reaches_the_reader(self, tmp_path: Path) -> None:
        note = self._report(tmp_path, "gh: HTTP 403 in account 123456789012\n")
        assert "123456789012" not in note
        assert "[REDACTED-ACCT]" in note

    def test_a_runner_path_never_reaches_the_reader(self, tmp_path: Path) -> None:
        note = self._report(
            tmp_path, "gh: cannot open /home/runner/work/_temp/gh-config/hosts.yml\n"
        )
        assert "/home/runner/work" not in note
        assert "[REDACTED-PATH]" in note

    def test_an_empty_stderr_is_reported_as_such(self, tmp_path: Path) -> None:
        note = self._report(tmp_path, "", rc=4)
        assert "exited 4" in note
        assert "nothing on stderr" in note

    def test_the_note_is_one_bounded_line(self, tmp_path: Path) -> None:
        note = self._report(tmp_path, ("x" * 100 + "\n") * 40)
        assert "\n" not in note
        assert len(note) < 400, len(note)


# --------------------------------------------------------------------------- #
# Only the FINAL failure reports. Driven through the lane's real retry loop.
# --------------------------------------------------------------------------- #
class TestOnlyTheFinalFailureReports:
    LANE = "claude-review.yml"

    def _drive(self, tmp_path: Path, gh_body: str) -> subprocess.CompletedProcess[str]:
        bash = _bash()
        if bash is None:
            pytest.skip("bash is unavailable on this platform")
        body = tmp_path / "body.md"
        # The stamp has to be IN the body: a body without it gets one attempt and
        # no repeat, so the landing-confirmation read would never run.
        body.write_text(
            "<!-- opus-review -->\n[OPUS-REVIEWED] " + "0" * 40 + "\nverdict\n",
            encoding="utf-8",
        )
        script = (
            _harness(self.LANE)
            + "rc=0\n"
            + f'retry_comment_write "<!-- opus-review -->" "[OPUS-REVIEWED]" "{body}" "" '
            + "gh pr comment 1 --body-file /dev/null || rc=$?\n"
            + 'printf "rc=%s\\n" "$rc"\n'
            + 'printf "note=%s\\n" "$READ_FAILURE_NOTE"\n'
        )
        return _run(bash, tmp_path, _fake_bin(tmp_path, gh_body), script)

    # a `gh` whose comments read fails N times, then succeeds with an empty slot
    _COUNTER = """
count_file="$RUNNER_TEMP/gh-calls"
if [ "$1 $2" = "pr comment" ]; then echo "created"; exit 0; fi
n=0
[ -f "$count_file" ] && n="$(cat "$count_file")"
n=$(( n + 1 ))
printf '%s' "$n" > "$count_file"
if [ "$n" -le __FAILS__ ]; then
  echo "gh: HTTP 403: Resource not accessible by integration" >&2
  exit 1
fi
exit 0
"""

    def test_six_failures_report_the_cause_exactly_once(self, tmp_path: Path) -> None:
        out = self._drive(tmp_path, self._COUNTER.replace("__FAILS__", "6"))
        combined = out.stdout
        assert "rc=1" in combined, out.stderr[-2000:]
        assert combined.count("failed on attempt") == 6, combined
        summaries = [
            line for line in combined.split("\n") if line.startswith("note=") and line != "note="
        ]
        assert len(summaries) == 1, combined
        assert "Resource not accessible by integration" in summaries[0]
        assert "exited 1" in summaries[0]
        # the retry lines themselves stay causeless
        for line in combined.split("\n"):
            if "failed on attempt" in line:
                assert "Resource not accessible" not in line, line

    def test_five_failures_then_a_success_report_nothing(self, tmp_path: Path) -> None:
        out = self._drive(tmp_path, self._COUNTER.replace("__FAILS__", "5"))
        assert "note=" in out.stdout
        assert "Resource not accessible by integration" not in out.stdout, out.stdout
        assert out.stdout.count("failed on attempt") == 5, out.stdout

    def test_a_first_attempt_success_reports_nothing(self, tmp_path: Path) -> None:
        out = self._drive(tmp_path, self._COUNTER.replace("__FAILS__", "0"))
        assert "failed on attempt" not in out.stdout, out.stdout
        assert "Resource not accessible by integration" not in out.stdout

    def test_an_unreadable_slot_still_publishes_nothing(self, tmp_path: Path) -> None:
        """Reporting a cause does not license a write the old code refused."""
        out = self._drive(tmp_path, self._COUNTER.replace("__FAILS__", "6"))
        assert "rc=1" in out.stdout
        assert "created" not in out.stdout, "a spent read budget must not reach the write"
        assert "unreadable after 6 attempts" in out.stdout

    # the slot read answers once and empty, the write fails, and the landing
    # confirmation on attempt 2 is the read that cannot answer.
    _CONFIRM_FAILS = """
count_file="$RUNNER_TEMP/gh-calls"
if [ "$1 $2" = "pr comment" ]; then
  echo "gh: HTTP 502 Bad Gateway" >&2
  exit 1
fi
n=0
[ -f "$count_file" ] && n="$(cat "$count_file")"
n=$(( n + 1 ))
printf '%s' "$n" > "$count_file"
if [ "$n" -eq 1 ]; then exit 0; fi
echo "gh: Bad credentials (HTTP 401) for token ghp_rotatedsecret0123456789abcdef" >&2
exit 1
"""

    def test_an_unconfirmable_write_names_the_confirming_read(self, tmp_path: Path) -> None:
        """The read on the other side of the write reports its cause too.

        This is the read that decides whether a failed write may be repeated, so
        its silence hid the same three causes as the slot read's.
        """
        out = self._drive(tmp_path, self._CONFIRM_FAILS)
        assert "rc=1" in out.stdout, out.stdout + out.stderr[-1000:]
        assert "Cannot confirm whether the previous attempt landed" in out.stdout
        note = [line for line in out.stdout.split("\n") if line.startswith("note=")]
        assert note and note[0] != "note=", out.stdout
        assert "previous write landed" in note[0], note[0]
        assert "Bad credentials" in note[0], note[0]
        assert "ghp_rotatedsecret0123456789abcdef" not in out.stdout
        assert "REDACTED-GH-TOKEN" in note[0], note[0]
