"""The main-red issue has the shape of the repository's CI failure issue form.

``main-red-watch.yml`` files its issue through the API, where no form applies, so
``main_red_nodeids.py`` writes what the form would: the form's title prefix, its
labels, and one ``### <label>`` section per field in the form's order. The step
checks out ``.github/scripts`` only and cannot read the form at run time, so the
script holds the form's values as constants and this file fails when
``.github/ISSUE_TEMPLATE/ci_failure.yml`` and those constants differ.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parents[1]
FORM = ROOT / ".github" / "ISSUE_TEMPLATE" / "ci_failure.yml"
WATCHER = ROOT / ".github" / "workflows" / "main-red-watch.yml"
SCRIPT = ROOT / ".github" / "scripts" / "main_red_nodeids.py"

mod = load_skill_script("main_red_nodeids_form_under_test", SCRIPT)

RUN_URL = "https://github.com/example/repo/actions/runs/1000"
SHA = "0123456789abcdef0123456789abcdef01234567"
FAILURES = {"test/a.py::t[x - y]": "assert 2 == 1", "test/b.py::u": ""}


def _form() -> dict[str, Any]:
    return yaml.safe_load(FORM.read_text(encoding="utf-8"))


def _fields() -> list[dict[str, Any]]:
    return [item for item in _form()["body"] if item["type"] != "markdown"]


def _field(field_id: str) -> dict[str, Any]:
    (item,) = [item for item in _fields() if item["id"] == field_id]
    return item["attributes"]


def _body(streak: int = 1, runner: str | None = "Linux") -> str:
    state = mod.next_state(None, FAILURES, SHA, 1000, RUN_URL)
    for _ in range(streak - 1):
        state = mod.next_state(state, FAILURES, SHA, state["last_run_id"] + 1, RUN_URL)
    return mod.render_body(state, [], 77, runner)


def _sections(body: str) -> dict[str, str]:
    parts = re.split(r"^### (.+)$", body, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts), 2)}


def test_the_title_prefix_and_the_labels_are_the_forms() -> None:
    form = _form()
    assert mod.FORM_TITLE_PREFIX == form["title"]
    assert list(mod.FORM_LABELS) == form["labels"]
    assert mod.title_for(FAILURES) == form["title"] + "Main is red: test/a.py::t[x - y]"


def test_the_fields_are_the_forms_in_the_forms_order() -> None:
    expected = [(item["id"], item["attributes"]["label"]) for item in _fields()]
    assert list(mod.FORM_FIELDS) == expected


def test_the_fixed_answers_are_choices_the_form_offers() -> None:
    (box,) = _field("search")["options"]
    assert mod.FORM_SEARCHED == box["label"]
    patterns = _field("pattern")["options"]
    assert mod.FORM_PATTERN_ONCE in patterns and mod.FORM_PATTERN_REPEATED in patterns
    assert list(mod.FORM_RUNNERS) == _field("runner")["options"]


def test_the_body_has_one_section_per_field_in_order_and_each_required_one_is_filled() -> None:
    body = _body()
    labels = re.findall(r"^### (.+)$", body, flags=re.MULTILINE)
    assert labels == [item["attributes"]["label"] for item in _fields()]
    sections = _sections(body)
    for item in _fields():
        answer = sections[item["attributes"]["label"]]
        assert answer, item["id"]
        if (item.get("validations") or {}).get("required"):
            assert answer != mod.NO_RESPONSE, item["id"]


def test_each_section_holds_what_the_run_knows() -> None:
    sections = _sections(_body())
    assert sections["Existing issues"] == f"- [x] {mod.FORM_SEARCHED}"
    assert sections["Failing run"] == RUN_URL
    assert sections["Test or job"] == "`test/a.py::t[x - y]` and 1 more"
    assert sections["Commit"].startswith(SHA[:12])
    assert sections["How it fails"] == "Not sure"
    assert sections["Runner OS"] == "Linux"
    assert sections["First bad commit"].startswith(SHA[:12])
    assert sections["Error output"] == (
        "```shell\ntest/a.py::t[x - y] - assert 2 == 1\ntest/b.py::u\n```"
    )
    rest = sections["Anything else"]
    assert "Streak: 1 red main run(s)" in rest
    assert "Fix in progress: none yet." in rest and "put `Fixes #77` in its body" in rest
    assert "- `test/a.py::t[x - y]`: `assert 2 == 1`" in rest and "- `test/b.py::u`" in rest


def test_a_field_the_run_cannot_fill_reads_as_an_empty_optional_field() -> None:
    assert _sections(_body(runner=None))["Runner OS"] == "_No response_"
    assert _sections(_body(runner="Solaris"))["Runner OS"] == "_No response_"


def test_a_repeat_on_the_same_commit_is_the_forms_red_every_time_answer() -> None:
    assert _sections(_body(streak=3))["How it fails"] == (
        "Red every time on this commit: 3 red push runs in a row"
    )


def test_the_runner_answer_comes_from_the_failed_job_names() -> None:
    assert mod.runner_os(["Backend Tests (3.12, 2)", "Coverage Gate"]) == "Linux"
    assert mod.runner_os(["Backend Tests (Windows) (2)"]) == "Windows"
    assert mod.runner_os(["E2E Gateway Boot (macOS)"]) == "macOS"
    # The matrix legs of ci.yml name the OS by its runner label, in lower case.
    assert mod.runner_os(["Backend Tests (IPv6, windows-latest)"]) == "Windows"
    assert mod.runner_os(["Backend Tests (IPv6, macos-latest)"]) == "macOS"
    assert mod.runner_os(["Backend Tests (IPv6, ubuntu-latest)"]) == "Linux"
    assert mod.runner_os(["Pod Boot Canary (Windows)", "Electron Shell Tests"]) == "Several"
    assert mod.runner_os(["Backend Tests (3.12, 2)", "Backend Tests (Windows) (2)"]) == "Several"
    assert mod.runner_os([]) is None and mod.runner_os(["", " "]) is None


def test_the_state_record_survives_the_form_shape_and_text_cannot_end_a_section() -> None:
    hostile = {"test/x.py::t": "boom\n### Runner OS\n``` --> @octocat"}
    state = mod.next_state(None, hostile, SHA, 1000, RUN_URL)
    cleaned = mod.parse_state(mod.render_body(state, [], 5, "Linux"))
    body = mod.render_body(cleaned, [], 5, "Linux")
    assert mod.parse_state(body) == cleaned
    assert re.findall(r"^### (.+)$", body, flags=re.MULTILINE) == [
        label for _, label in mod.FORM_FIELDS
    ]
    assert body.count("```") == 2


def test_an_issue_filed_with_the_title_of_before_the_form_is_still_found() -> None:
    """The watcher without the form files `Main is red: ...`; that open issue is updated, not doubled."""
    bot = {"login": "app/github-actions"}
    body = _body()
    old = {"number": 4, "title": "Main is red: test/a.py::t", "body": body, "author": bot}
    ours = {"number": 9, "title": mod.title_for(FAILURES), "body": body, "author": bot}
    other = {"number": 2, "title": "ci: something else is red", "body": body, "author": bot}
    assert mod.pick_issue([other, ours])["number"] == 9
    assert mod.pick_issue([other, ours, old])["number"] == 4
    assert mod.pick_issue([other]) is None


def test_the_runner_answer_counts_only_jobs_that_named_a_failing_test(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A Coverage Gate that fails on Linux beside a Windows-only break does not make it `Several`."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "14.log").write_text("FAILED test/a.py::t - boom\n", encoding="utf-8")
    (logs / "15.json").write_text("[]", encoding="utf-8")
    jobs = tmp_path / "jobs.tsv"
    jobs.write_text("14\tBackend Tests (IPv6, windows-latest)\n15\tCoverage Gate\n", "utf-8")
    failures = tmp_path / "f.json"
    failures.write_text('{"test/a.py::t": "boom"}', encoding="utf-8")
    common = ["render", "--failures", str(failures), "--repo", "example/repo", "--sha", SHA]
    common += ["--run-id", "1000", "--run-url", RUN_URL, "--jobs", str(jobs)]
    common += ["--out-title", str(tmp_path / "t.txt"), "--out-body", str(tmp_path / "b.md")]
    assert mod.main([*common, "--logdir", str(logs)]) == 0
    assert _sections((tmp_path / "b.md").read_text(encoding="utf-8"))["Runner OS"] == "Windows"
    assert mod.main(common) == 0
    assert _sections((tmp_path / "b.md").read_text(encoding="utf-8"))["Runner OS"] == "Several"


def test_the_workflow_files_the_issue_with_the_forms_labels() -> None:
    document = yaml.safe_load(WATCHER.read_text(encoding="utf-8"))
    (job,) = document["jobs"].values()
    (step,) = [s for s in job["steps"] if "run" in s]
    run = step["run"]
    labels = " ".join(f'"{label}"' if " " in label else label for label in _form()["labels"])
    assert f"for form_label in {labels}; do" in run
    assert 'gh label create "$form_label" --repo "$REPO" >/dev/null 2>&1 || true' in run
    assert (
        'label_args+=(--label "$form_label")' in run
        and '${label_args[@]+"${label_args[@]}"}' in run
    )
    # Never `--force` on a label the form owns: that rewrites its colour and description.
    forced = [line for line in run.splitlines() if "label create" in line and "--force" in line]
    assert forced and all("label create main-red " in line for line in forced)
    assert '--jobs "$work/failed-jobs.txt" --logdir "$work/logs"' in run
