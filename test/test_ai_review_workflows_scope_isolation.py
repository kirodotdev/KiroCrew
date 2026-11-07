"""Review-workflow tests for what the security-scope lanes keep out of a privileged
job: the model's text through one redactor, programs and project settings from the
reviewed tree, the credential, and the change under review.
"""

from __future__ import annotations

import re

import pytest
import yaml
from ai_review_workflows_helpers import (
    _SCOPE_LANES,
    ROOT,
    _step_by_name,
    _step_script,
    _workflow,
)

# The two Security Scope Review lanes publish only text a base-owned redactor has
# rewritten. That is one property with two halves, and each half fails silently on
# its own: a scrub that CORRUPTS its input makes the report unparseable, which the
# verdict folder answers with a hard block naming no rows, and an upload that runs
# on `always()` republishes the raw file a refused scrub withheld. So both halves
# are pinned here rather than left to a reader comparing copies of a regex.

#: A scrub call, by either spelling the lanes use. A step whose workspace IS a
#: trusted tree runs `scripts/scope_redact.py` by name; a step handed a staged copy
#: runs `"$REDACTOR"`, the path of a blob taken from the base ref. Matching only the
#: first spelling silently drops every staged call out of the pins below, which is
#: how a staged call with no `--mode` would reach a job log instead of a test.
_REDACTOR_CALL = re.compile(r'python3?\s+"?(?:\$\{?REDACTOR\}?|[^\s"]*scope_redact\.py)"?')


def _scrub_calls(workflow_name: str) -> list[str]:
    return [line for line in _workflow(workflow_name).splitlines() if _REDACTOR_CALL.search(line)]


def _scope_steps(workflow_name: str) -> list[tuple[str, dict]]:
    doc = yaml.safe_load(_workflow(workflow_name))
    pairs: list[tuple[str, dict]] = []
    for job_name, job in (doc.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            pairs.append((job_name, step))
    return pairs


class TestTheScopeLanesScrubThroughOneRedactor:
    """One implementation, called everywhere, and no upload that outruns it."""

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    def test_no_embedded_redaction_program_survives(self, workflow: str) -> None:
        """A copied regex is the defect generator this replaces.

        A copy that is wrong is wrong on every surface it guards, and a program
        embedded in a ``run:`` body has no seam a test can call. A new copy would
        re-open exactly that, so the absence is the pin.
        """
        assert "perl -i -pe" not in _workflow(workflow)

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    def test_every_scrub_calls_the_shared_redactor(self, workflow: str) -> None:
        text = _workflow(workflow)
        assert "scope_redact.py" in text
        # Every invocation declares its file shape. A call with no --mode is
        # argparse-refused at runtime, which reds the lane rather than publishing
        # unscrubbed -- but it is still a defect a reader can catch here instead of
        # in a job log.
        calls = _scrub_calls(workflow)
        assert calls, f"{workflow}: no scrub call found"
        for line in calls:
            assert "--mode json" in line or "--mode text" in line, line

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    def test_the_redactor_is_never_taken_from_the_reviewed_tree(self, workflow: str) -> None:
        """The change under review must not supply the program that redacts its output.

        The redactor comes from the base ref or a trusted workspace, and the lane
        refuses when the file is absent.
        """
        text = _workflow(workflow)
        assert "if [ ! -s scripts/scope_redact.py ]" in text or (
            'git show "$BASE_SHA:scripts/scope_redact.py"' in text
        )
        assert "Refusing to publish" in text

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    def test_an_always_upload_of_derived_text_reads_the_scrub_verdict(self, workflow: str) -> None:
        """``if: always()`` may not outlive the scrub that gates the file.

        An artifact is world-readable on a public repository, so ``always()`` on
        unscrubbed derived text republishes what the refusal withheld. The `-raw`
        transfer upload is the exception, bounded by retention and the test below.
        """
        for job_name, step in _scope_steps(workflow):
            if "upload-artifact" not in str(step.get("uses", "")):
                continue
            condition = str(step.get("if", ""))
            path = str((step.get("with") or {}).get("path", ""))
            where = f"{workflow}:{job_name}:{step.get('name')}"
            name = str((step.get("with") or {}).get("name", ""))
            if name.endswith("-raw"):
                # The deliberate exception, and the whole cost of the credential
                # split: the scrub is a program this repository ships, so it cannot
                # run in the job that holds the credential, and the file crosses to
                # the job that scrubs it unscrubbed. Bounded rather than waved
                # through -- one day of retention, and the test below pins that no
                # publisher reads this name.
                assert (step.get("with") or {}).get("retention-days") == 1, (
                    f"{where}: the unscrubbed transfer artifact {name} outlives the run "
                    f"it was made for"
                )
                continue
            if "always()" not in condition:
                # Not unconditional, so it already cannot outrun a refused scrub:
                # a failed scrub step skips this one too.
                assert condition, f"{where}: an ungated upload of {path}"
                continue
            assert "scrub_ok" in condition, f"{where}: always() upload of {path}, no scrub gate"

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    def test_no_publisher_reads_an_unscrubbed_transfer_artifact(self, workflow: str) -> None:
        """A `-raw` upload exists to cross ONE job boundary, and no further.

        The review text crosses to the scrubbing job unscrubbed, so every `-raw`
        artifact is downloaded by exactly one job, which holds no `id-token` and scrubs.
        A `publish` reading it would post the text the scrub exists to rewrite.
        """
        doc = yaml.safe_load(_workflow(workflow))
        jobs = doc.get("jobs") or {}
        raw = {
            str((step.get("with") or {}).get("name", ""))
            for _, step in _scope_steps(workflow)
            if "upload-artifact" in str(step.get("uses", ""))
            and str((step.get("with") or {}).get("name", "")).endswith("-raw")
        }
        assert raw, f"{workflow}: the transfer artifacts are not named for being unscrubbed"
        for name in sorted(raw):
            readers = [
                job_id
                for job_id, job in jobs.items()
                for step in job.get("steps") or []
                if "download-artifact" in str(step.get("uses", ""))
                and str((step.get("with") or {}).get("name", "")) == name
            ]
            assert len(readers) == 1, f"{workflow}: {name} is read by {readers}"
            reader = jobs[readers[0]]
            perms = reader.get("permissions") or {}
            assert (
                perms.get("id-token") != "write"
            ), f"{workflow}: {name} is read beside a credential"
            assert not [
                k for k, v in perms.items() if v == "write"
            ], f"{workflow}: {readers[0]} reads {name} and holds a write scope"
            bodies = "\n".join(str(step.get("run") or "") for step in reader.get("steps") or [])
            assert _REDACTOR_CALL.search(
                bodies
            ), f"{workflow}: {readers[0]} reads {name} and never scrubs it"

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    def test_the_corpus_upload_is_gated_on_the_probe_not_on_a_scrub(self, workflow: str) -> None:
        """The one upload that must stay byte-faithful, and how it is still safe.

        Redacting the corpus would hand the classifier a command nobody ever
        refused, which classifies as allowed -- a false green in its least visible
        shape. So the corpus is probed on a COPY, the run is refused when the probe
        changes anything, and the upload is gated on that verdict.
        """
        corpus_uploads = [
            step
            for _, step in _scope_steps(workflow)
            if "upload-artifact" in str(step.get("uses", ""))
            and "normalized.json" in str((step.get("with") or {}).get("path", ""))
        ]
        assert corpus_uploads, f"{workflow}: no corpus upload found"
        for step in corpus_uploads:
            condition = str(step.get("if", ""))
            assert "adjudicate == 'true'" in condition, f"{workflow}: corpus upload not probe-gated"
            assert "always()" not in condition

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    def test_a_json_surface_is_redacted_in_json_mode(self, workflow: str) -> None:
        """The report and the rows are parsed by their readers, so their mode is fixed.

        A raw-bytes substitution over either can leave text that fails to parse,
        and an unparseable report is exit 2 in the folder: a hard block that names
        no rows, from the lane whose purpose is preventing that over-refusal.
        """
        json_surfaces = [
            line
            for line in _scrub_calls(workflow)
            if ".json" in line or '"$ROWS"' in line or "$NORMALIZED" in line
        ]
        assert json_surfaces, f"{workflow}: no JSON surface is redacted"
        for line in json_surfaces:
            assert "--mode json" in line, line

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    def test_the_model_s_own_job_scrubs_nothing(self, workflow: str) -> None:
        """The reviewer must not be able to replace the program that scrubs it.

        `Write` takes an absolute path, so no directory on the runner is out of the
        model's reach. Its job holds the Bedrock credential and so runs no program
        this repository ships; the captured text is scrubbed in a job holding nothing.
        """
        for job_name, job in (yaml.safe_load(_workflow(workflow)).get("jobs") or {}).items():
            steps = job.get("steps") or []
            if not any("claude-code-action" in str(s.get("uses", "")) for s in steps):
                continue
            where = f"{workflow}:{job_name}"
            for i, step in enumerate(steps):
                for line in (step.get("run") or "").splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    assert not _REDACTOR_CALL.search(
                        stripped
                    ), f"{where}: step {i} scrubs beside the model: {stripped}"
            assert not any(
                "Materialize the base-owned outbound redactor" in str(s.get("name", ""))
                for s in steps
            ), f"{where}: a redactor is staged in a job that scrubs nothing"

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    def test_the_marker_spellings_the_seed_path_greps_for_are_unchanged(
        self, workflow: str
    ) -> None:
        """Both lanes read a redaction marker back as a refusal on the way IN.

        A renamed marker stops matching, and a row nobody can read then travels
        into the next run's candidate set, so the grep and the redactor's
        vocabulary are one contract.
        """
        assert "[REDACTED-" in _workflow(workflow)
        redactor = (ROOT / "scripts" / "scope_redact.py").read_text(encoding="utf-8")
        for marker in ("[REDACTED-AWS-KEY-ID]", "[REDACTED-ARN]", "[REDACTED-ACCT]"):
            assert marker in redactor, marker


#: Anything that makes a job worth attacking. `id-token: write` mints an AWS
#: credential; the three write scopes hand it a token that can speak for the repo.
_PRIVILEGED_SCOPES = ("id-token", "contents", "pull-requests", "checks", "issues", "actions")


def _privileged_jobs(workflow_name: str) -> list[tuple[str, dict, list[str]]]:
    """Every job holding a credential or a write scope, with what it holds."""
    doc = yaml.safe_load(_workflow(workflow_name))
    out: list[tuple[str, dict, list[str]]] = []
    for job_id, job in (doc.get("jobs") or {}).items():
        perms = job.get("permissions") or {}
        if not isinstance(perms, dict):
            continue
        held = [
            f"{scope}: {perms[scope]}"
            for scope in _PRIVILEGED_SCOPES
            if perms.get(scope) == "write"
        ]
        if held:
            out.append((job_id, job, held))
    return out


def _workspace_is_trusted(workflow_name: str, job: dict) -> bool:
    """Is this job's checked-out tree a trusted one, or the change under review?

    An explicit `ref:` naming the base commit is trusted. No `ref:` at all is the
    workflow's own default: the pull request's MERGE ref on a `pull_request` event
    (so, untrusted), and the repository default branch on any other trigger.
    """
    doc = yaml.safe_load(_workflow(workflow_name))
    triggers = doc.get("on") or doc.get(True) or {}
    on_pull_request = "pull_request" in set(triggers)
    for step in job.get("steps") or []:
        if "actions/checkout" not in str(step.get("uses", "")):
            continue
        ref = str((step.get("with") or {}).get("ref", ""))
        if ref:
            return "base" in ref
        return not on_pull_request
    return True


class TestAPrivilegedScopeJobRunsNoProgramFromTheReviewedTree:
    """A privileged job never runs a program the change under review can supply.

    Pinned per job and per spelling. The bootstrap window (absent at base, present
    in the working tree) is where a path-by-path fix leaks.
    """

    #: The lane's own programs. A privileged job may run these only from a staged
    #: copy of a COMMITTED blob, which is a path under `runner.temp`, never a path
    #: in the workspace and never a `cp` out of it.
    HARNESS = ("scope_candidates.py", "deny_diff.py", "scope_redact.py")

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    def test_no_privileged_job_copies_a_program_out_of_the_workspace(self, workflow: str) -> None:
        """A privileged job never runs `cp` out of `scripts/`.

        `git show <sha>:scripts/<program>` reads committed content; `cp` reads the
        checked-out tree, which on a `pull_request` event is the change under review.

        The check matches the source directory, not the file name: the staging loops
        spell their file as `$f`, so a pin naming `scope_candidates.py` matches nothing.
        """
        for job_id, job, held in _privileged_jobs(workflow):
            for index, step in enumerate(job.get("steps") or []):
                for line in (step.get("run") or "").splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#") or not stripped.startswith("cp "):
                        continue
                    source = stripped.split()[1].strip('"').strip("'")
                    assert not source.startswith("scripts/"), (
                        f"{workflow}:{job_id} (holds {held}) step {index} copies a "
                        f"program out of the workspace: {stripped}"
                    )

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    def test_a_privileged_job_executes_no_program_from_an_untrusted_workspace(
        self, workflow: str
    ) -> None:
        """Whether a `scripts/` path is safe to run is a property of the CHECKOUT.

        A base or default-branch checkout is a trusted tree. A merge-ref checkout has
        the change under review on disk, so its job may run only a staged copy of a
        committed blob.
        """
        for job_id, job, held in _privileged_jobs(workflow):
            if _workspace_is_trusted(workflow, job):
                continue
            for index, step in enumerate(job.get("steps") or []):
                for line in (step.get("run") or "").splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if "python " not in stripped and "python3 " not in stripped:
                        continue
                    for program in self.HARNESS:
                        assert f"scripts/{program}" not in stripped, (
                            f"{workflow}:{job_id} (holds {held}, untrusted workspace) "
                            f"step {index} runs the workspace copy: {stripped}"
                        )

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    def test_a_job_holding_aws_credentials_executes_no_repository_python(
        self, workflow: str
    ) -> None:
        """The rule, stated once, positively, for both lanes.

        `id-token: write` lets the job assume the Bedrock role at any step, and every
        lane program has a bootstrap window where the change under review supplies it.
        `jq`, `gh`, `git` and `awk` are allowed: a pull request cannot rewrite them.
        No allowlist: any allowed program is one a pull request might supply.
        """
        for job_id, job in (yaml.safe_load(_workflow(workflow)).get("jobs") or {}).items():
            if (job.get("permissions") or {}).get("id-token") != "write":
                continue
            for index, step in enumerate(job.get("steps") or []):
                for line in (step.get("run") or "").splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    assert not re.search(r"\bpython3?\s", stripped), (
                        f"{workflow}:{job_id} holds an AWS credential and step {index} "
                        f"runs python: {stripped}"
                    )

    def test_the_execution_the_credentialed_jobs_gave_up_still_happens(self) -> None:
        """Otherwise the rule above is satisfied by deleting the checks.

        Each lane's validation, its credential-shape probe and its outbound scrub
        all still run -- in a job with no `id-token` -- so the move traded the
        exposure for nothing but a job boundary.
        """
        same = _step_script(
            _workflow("security-scope-review.yml"),
            "Validate the candidates against the base corpus",
        )
        assert 'python "$HARNESS/scope_candidates.py"' in same
        fork = _step_script(
            _workflow("fork-security-scope-review.yml"),
            "Validate candidates against the base-owned corpus",
        )
        assert 'python3 "$HARNESS/scope_candidates.py" validate' in fork
        for workflow, step, job in (
            ("security-scope-review.yml", "Scrub the review text", "validate"),
            ("fork-security-scope-review.yml", "Redact credential shapes", "validate"),
            (
                "security-scope-review.yml",
                "Probe the candidate set for credential shapes",
                "validate",
            ),
        ):
            doc = yaml.safe_load(_workflow(workflow))
            names = [str(s.get("name", "")) for s in doc["jobs"][job]["steps"]]
            assert step in names, f"{workflow}: {step} is not in {job}"

    def test_the_same_repo_validation_job_holds_read_scope_only(self) -> None:
        """The job that can execute the checked-out harness holds exactly one read.

        `contents: read` is the whole grant: even `pull-requests: read` would hand a
        comment feed to a program the pull request supplied.
        """
        doc = yaml.safe_load(_workflow("security-scope-review.yml"))
        assert doc["jobs"]["validate"]["permissions"] == {"contents": "read"}
        # It is the only job in the lane that reads the harness out of the working
        # tree, and it says so where the bootstrap happens.
        script = _step_script(
            _workflow("security-scope-review.yml"),
            "Validate the candidates against the base corpus",
        )
        assert 'cp "scripts/$f" "$HARNESS/$f"' in script
        assert "exit 1" in script

    def test_the_candidate_set_is_probed_before_the_classifier_reads_it(self) -> None:
        """The candidate set is probed in `validate` before the classifier reads it.

        Rows must reach the classifier byte-faithful (a placeholder classifies as
        allowed), so a copy is probed and the run refused. The probe runs in
        `validate`, away from the credential, so a row with a credential shape is
        never adjudicated.
        """
        workflow = _workflow("security-scope-review.yml")
        script = _step_script(workflow, "Probe the candidate set for credential shapes")
        assert "--fail-if-changed" in script
        assert "cp candidates.json scrub-probe-candidates.json" in script
        assert 'if [ "$probe" = "10" ]; then' in script
        assert "candidates_ok=false" in script
        doc = yaml.safe_load(workflow)
        probes = [
            job_id
            for job_id, job in doc["jobs"].items()
            for step in job.get("steps") or []
            if str(step.get("name", "")) == "Probe the candidate set for credential shapes"
        ]
        assert probes == ["validate"], probes
        uploads = [
            (job_id, step)
            for job_id, job in doc["jobs"].items()
            for step in job.get("steps") or []
            if "upload-artifact" in str(step.get("uses", ""))
            and (step.get("with") or {}).get("path") == "candidates.json"
        ]
        assert len(uploads) == 1, "the candidate set must travel exactly once"
        job_id, upload = uploads[0]
        assert job_id == "generate"
        # NAMED for being unprobed, so no reader can mistake it for the checked
        # corpus, and retained for a day rather than a week.
        assert (upload.get("with") or {}).get("name") == "security-scope-candidates-raw"
        assert (upload.get("with") or {}).get("retention-days") == 1
        assert "always()" not in str(upload["if"])

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    def test_every_needs_reference_resolves_to_a_declared_job(self, workflow: str) -> None:
        """A job move breaks these silently: the expression reads empty, not red.

        `needs.<job>.outputs.<name>` where `<job>` is not in this job's `needs`
        evaluates to the empty string, so a gate keyed on it turns off and a lane
        skips itself while reporting success -- the same failure shape as a scope
        diff with no head object to read.
        """
        doc = yaml.safe_load(_workflow(workflow))
        jobs = doc["jobs"]
        for job_id, job in jobs.items():
            needs = job.get("needs") or []
            declared = {needs} if isinstance(needs, str) else set(needs)
            for producer in declared:
                assert producer in jobs, f"{workflow}:{job_id} needs absent job {producer}"
            body = yaml.safe_dump(job)
            for producer in sorted(set(re.findall(r"needs\.([A-Za-z0-9_-]+)\.", body))):
                assert (
                    producer in declared
                ), f"{workflow}:{job_id} reads needs.{producer} with needs={sorted(declared)}"
            for producer, name in re.findall(
                r"needs\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)", body
            ):
                produced = jobs[producer].get("outputs") or {}
                assert name in produced, (
                    f"{workflow}:{job_id} reads needs.{producer}.outputs.{name}, "
                    f"which {producer} does not produce"
                )


def _claude_args(workflow_name: str) -> list[tuple[str, str, list[str]]]:
    """Every ``claude-code-action`` call in a lane, as (job, step, ARGUMENT lines).

    Read off the parsed ``claude_args`` block, so a YAML comment never appears;
    ``#`` lines inside the block are dropped too.
    """
    doc = yaml.safe_load(_workflow(workflow_name))
    calls: list[tuple[str, str, list[str]]] = []
    for job_id, job in (doc.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            if "claude-code-action" not in str(step.get("uses", "")):
                continue
            raw = str((step.get("with") or {}).get("claude_args", ""))
            args = [
                line.strip()
                for line in raw.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            calls.append((job_id, str(step.get("name", "?")), args))
    return calls


class TestTheScopeLanesLoadNoProjectSettingsFromTheReviewedTree:
    """A model call is code execution beside the credential unless this flag is set.

    ``claude-code-action`` loads the checkout's ``CLAUDE.md`` and ``.claude/``, and a
    ``.claude/settings.json`` ``SessionStart`` hook executes, outside any
    ``--allowedTools`` grant. ``--setting-sources user`` reads nothing from the checkout.

    Pinned per lane and per call. The fork lane checks out ``base_sha`` and still
    needs the flag: the exposure is one checkout-ref edit away.
    """

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    def test_every_model_call_reads_only_the_user_setting_source(self, workflow: str) -> None:
        calls = _claude_args(workflow)
        assert calls, f"{workflow}: no claude-code-action call found"
        for job_id, step_name, args in calls:
            assert "--setting-sources user" in args, (
                f"{workflow}:{job_id} step {step_name!r} passes no `--setting-sources "
                f"user`, so the action loads the checkout's CLAUDE.md/.claude as "
                f"instructions: {args}"
            )

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    def test_no_model_call_widens_the_setting_sources(self, workflow: str) -> None:
        """``user`` is the whole grant: ``project`` or ``local`` is the defect back.

        The pin is on the value: an added source widens it while the flag stays present.
        """
        for job_id, step_name, args in _claude_args(workflow):
            for arg in args:
                if not arg.startswith("--setting-sources"):
                    continue
                assert arg == "--setting-sources user", (
                    f"{workflow}:{job_id} step {step_name!r} widens the setting " f"sources: {arg}"
                )


class TestTheScopeLanesKeepTheCredentialOutOfTheModelsReach:
    """Neither scope model call can read the Bedrock credential it runs beside.

    The model's text is public before the shape probe in `validate` runs, and a
    re-encoded value matches no shape rule. So the credential must be unreachable,
    which takes all three of these:

    * no `Bash` grant, so there is no shell to print an environment with;
    * `Read(//proc/**)` + `Read(//sys/**)` DENIED, because `Read` is path-unscoped
      and `/proc/self/environ` is an ordinary file that needs no shell;
    * the runner's own on-disk copy of the exported credential
      (`$RUNNER_TEMP/_runner_file_commands/set_env_*`) truncated first, because it
      is a FILE and so is covered by neither of the other two.

    Asserted per parsed call, not by a whole-file grep, which passes with one
    unfenced step beside a fenced sibling.
    """

    ACTION = "anthropics/claude-code-action"
    SCRUB = "Scrub persisted credential files before the model runs"
    #: One canonical scrub across every lane holding this credential. Read from the
    #: reference lane rather than restated, so a fix there cannot leave these two behind.
    REFERENCE_LANE = "fork-opus-review.yml"

    def _steps(self, workflow: str) -> list[dict]:
        doc = yaml.safe_load(_workflow(workflow))
        return [step for job in (doc["jobs"] or {}).values() for step in (job.get("steps") or [])]

    def _model_steps(self) -> list[tuple[str, dict]]:
        found: list[tuple[str, dict]] = []
        for workflow in _SCOPE_LANES:
            steps = [s for s in self._steps(workflow) if self.ACTION in str(s.get("uses") or "")]
            assert steps, f"{workflow}: found no {self.ACTION} step to check"
            found.extend((workflow, step) for step in steps)
        # Both lanes make exactly ONE model call. A second one is not covered by
        # this class's reasoning until someone has thought about it.
        assert len(found) == 2, f"expected 2 scope model calls, found {len(found)}"
        return found

    @staticmethod
    def _flags(step: dict) -> dict[str, str]:
        """`claude_args` as {flag: value}, quotes stripped. Parsed, never grepped."""
        flags: dict[str, str] = {}
        for line in str(step.get("with", {}).get("claude_args") or "").splitlines():
            line = line.strip()
            if not line.startswith("--"):
                continue
            flag, _, value = line.partition(" ")
            flags[flag] = value.strip().strip('"')
        return flags

    @staticmethod
    def _where(workflow: str, step: dict) -> str:
        return f"{workflow}:{step.get('name') or step.get('id') or '<unnamed>'}"

    def test_no_scope_model_call_grants_a_shell(self) -> None:
        # Bash grants are PREFIX-matched, so `Bash(git diff:*)` also admits
        # `git diff ... > somewhere`: a grant is a redirection primitive beside a
        # live credential, not just a reader.
        for workflow, step in self._model_steps():
            flags = self._flags(step)
            where = self._where(workflow, step)
            granted = flags["--allowedTools"].split(",")
            # `Write` is granted plainly and bounded by the deny rules in the fence test:
            # `Write(candidates.json)` breaks the lane, because the matcher resolves to an
            # absolute path and the narrow allow never matches.
            assert granted == ["Read", "Grep", "Glob", "Write"], f"{where}: {granted}"
            assert not any(
                tool.startswith("Bash") for tool in granted
            ), f"{where}: a Bash grant is back"

    def test_every_scope_model_call_denies_the_kernel_filesystems(self) -> None:
        for workflow, step in self._model_steps():
            flags = self._flags(step)
            where = self._where(workflow, step)
            denied = flags.get("--disallowedTools", "").split(",")
            assert "Read(//proc/**)" in denied, f"{where}: /proc is still readable"
            assert "Read(//sys/**)" in denied, f"{where}: /sys is still readable"

    def test_the_fence_is_a_deny_not_a_narrowed_allow(self) -> None:
        # A deny rule outranks every allow rule and CLI flag. An allow rule that
        # fails to match falls back to PROMPTING, which in a non-interactive run is
        # not a guarantee about what was read -- so the fence must not be expressed
        # by narrowing the allow list.
        for workflow, step in self._model_steps():
            flags = self._flags(step)
            where = self._where(workflow, step)
            assert "proc" not in flags["--allowedTools"], f"{where}: /proc named as an allow rule"
            # The write bound is a deny too, over both regions: `_actions` holds the
            # action's later-executing JavaScript, and the runner temp holds
            # `$GITHUB_ENV`, which injects environment into every later step.
            denied = flags.get("--disallowedTools", "").split(",")
            assert (
                "Write(//home/runner/work/_actions/**)" in denied
            ), f"{where}: the action's own code is writable by the model"
            assert any(
                tool.startswith("Write(") and "runner.temp" in tool for tool in denied
            ), f"{where}: the workflow command files ($GITHUB_ENV) are writable: {denied}"
            for tool in ("Edit", "MultiEdit", "NotebookEdit"):
                assert tool in denied, f"{where}: {tool} is another path to the same write"

    def test_the_persisted_credential_file_is_truncated_first(self) -> None:
        for workflow in _SCOPE_LANES:
            steps = self._steps(workflow)
            models = [i for i, s in enumerate(steps) if self.ACTION in str(s.get("uses") or "")]
            assert models, f"{workflow}: no model step"
            for index in models:
                where = f"{workflow}:{steps[index].get('name')}"
                assert index > 0, f"{where}: model step is first, so nothing was scrubbed"
                assert (
                    steps[index - 1].get("name") == self.SCRUB
                ), f"{where}: the step before it is {steps[index - 1].get('name')!r}"

    def test_the_scrub_is_the_reference_lanes_scrub_byte_for_byte(self) -> None:
        reference = [
            s.get("run") for s in self._steps(self.REFERENCE_LANE) if s.get("name") == self.SCRUB
        ]
        assert reference, f"{self.REFERENCE_LANE}: the reference scrub is gone"
        bodies = [
            s.get("run")
            for w in _SCOPE_LANES
            for s in self._steps(w)
            if s.get("name") == self.SCRUB
        ]
        assert len(bodies) == 2, f"expected one scrub per scope lane, found {len(bodies)}"
        assert set(bodies) == {
            reference[0]
        }, "the scope lanes' scrub has drifted from the reference"

    def test_neither_prompt_asks_the_model_to_compute_the_diff(self) -> None:
        # The grants existed only because the same-repo PROMPT told the model to run
        # `git diff` itself. Dropping the grants without dropping that instruction
        # would leave the lane asking for something it cannot do -- and the model
        # reporting on nothing.
        for workflow, step in self._model_steps():
            prompt = str(step.get("with", {}).get("prompt") or "")
            where = self._where(workflow, step)
            assert "git diff" not in prompt, f"{where}: the prompt still asks for a shell diff"
            assert "authentic.patch" in prompt, f"{where}: the prompt names no prefetched diff"

    def test_the_same_repo_diff_is_prefetched_before_any_credential_exists(self) -> None:
        steps = self._steps("security-scope-review.yml")
        names = [str(s.get("name") or s.get("uses") or "") for s in steps]
        prefetch = next(i for i, n in enumerate(names) if n.startswith("Prefetch the diff"))
        creds = next(i for i, n in enumerate(names) if "configure-aws-credentials" in n)
        model = next(i for i, s in enumerate(steps) if self.ACTION in str(s.get("uses") or ""))
        assert prefetch < creds < model, (prefetch, creds, model)


def _credentialed_jobs(workflow_name: str) -> list[tuple[str, dict]]:
    """Every job that can mint the Bedrock credential.

    `id-token: write` is the whole test: the job may assume the role whenever it
    likes, so "the credential is only live after this step" is not a property
    anything can check.
    """
    doc = yaml.safe_load(_workflow(workflow_name))
    out: list[tuple[str, dict]] = []
    for job_id, job in (doc.get("jobs") or {}).items():
        perms = job.get("permissions") or {}
        if isinstance(perms, dict) and perms.get("id-token") == "write":
            out.append((job_id, job))
    return out


class TestNoCredentialedScopeJobMaterializesTheChangeUnderReview:
    """The credential and the reviewed tree must never be on the same runner.

    The fence is a path deny, `Read(//proc/**)`, and a committed symlink
    (`notes.md -> /proc/self/environ`) reads through a workspace path no deny matches.
    So the head reaches the runner as the prefetched diff, never as files.
    """

    def test_each_lane_has_exactly_one_credentialed_job(self) -> None:
        """A vacuous pass is the failure mode: assert the subject exists."""
        for workflow in _SCOPE_LANES:
            jobs = _credentialed_jobs(workflow)
            assert len(jobs) == 1, f"{workflow}: {[job_id for job_id, _ in jobs]}"

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    def test_the_credentialed_job_checks_out_a_base_ref(self, workflow: str) -> None:
        """Read off the PARSED workflow, per checkout step, never off a substring.

        An ABSENT `ref:` is the defect's own spelling -- on a `pull_request` event
        that resolves to the merge ref, which is the change under review -- so a
        missing key fails here rather than being skipped.
        """
        for job_id, job in _credentialed_jobs(workflow):
            checkouts = [
                step
                for step in (job.get("steps") or [])
                if "actions/checkout" in str(step.get("uses", ""))
            ]
            assert checkouts, f"{workflow}:{job_id} holds the credential and no checkout"
            for index, step in enumerate(checkouts):
                ref = str((step.get("with") or {}).get("ref", ""))
                assert ref, (
                    f"{workflow}:{job_id} checkout {index} names no `ref:`, so it "
                    "resolves to the merge ref -- the tree under review, beside a "
                    "live Bedrock credential"
                )
                assert "base" in ref and "head" not in ref, (
                    f"{workflow}:{job_id} checkout {index} checks out {ref!r}, "
                    "which is not the trusted base"
                )

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    def test_the_credentialed_workspace_is_trusted_by_the_shared_rule(self, workflow: str) -> None:
        """The same predicate the program-execution pins use, on the same jobs.

        One rule answers both "may this job run `scripts/x.py`" and "may the model
        read it", so the two lanes cannot drift apart.
        """
        for job_id, job in _credentialed_jobs(workflow):
            assert _workspace_is_trusted(workflow, job), (
                f"{workflow}:{job_id} holds the Bedrock credential with the change "
                "under review checked out"
            )

    def test_the_same_repo_prompt_no_longer_offers_head_side_files(self) -> None:
        """Prose that implies readable head files is the same defect, in words."""
        prompt = str(
            _step_by_name("security-scope-review.yml", "generate", "Security scope review")["with"][
                "prompt"
            ]
        )
        assert "authentic.patch" in prompt
        assert "trusted BASE tree" in prompt
        assert "checked-out tree" not in prompt, prompt

    def test_the_contract_is_read_from_a_commit_and_never_from_the_workspace(self) -> None:
        """The contract bootstrap reads committed blobs, never the workspace.

        With the base checked out there is no head copy on disk, so the bootstrap
        reads the head blob, as `publish`'s bootstraps do. A `cp` would be a head-side read.
        """
        body = str(
            _step_by_name(
                "security-scope-review.yml", "generate", "Materialize the base-owned contract"
            )["run"]
        )
        assert 'git show "$BASE_SHA:.github/review-prompts/security-scope.md"' in body
        assert 'git show "$HEAD_SHA:.github/review-prompts/security-scope.md"' in body
        for line in body.splitlines():
            stripped = line.strip()
            assert not stripped.startswith("cp "), f"contract copied out of the tree: {stripped}"

    def test_neither_scope_resolver_answers_false_unless_it_measured(self) -> None:
        """A base checkout has no head object until the resolver fetches it.

        With no head object the surface diff names nothing and the `else` branch
        writes `in_scope=false`, which the tri-state gate must honor. So an
        unreachable ref and a failed diff both fail closed here.

        Both lanes: on the fork lane `in_scope=false` publishes a green check-run, so a
        failed diff would pass a fork tightening unmeasured.
        """
        for lane in ("security-scope-review.yml", "fork-security-scope-review.yml"):
            body = str(_step_by_name(lane, "generate", "Resolve review scope")["run"])
            assert "could not diff" in body, f"{lane}: a failed surface diff is not reported"
            assert 'git diff --name-only "$BASE_SHA...$HEAD_SHA"' in body, lane
            assert "rc=$?" in body, f"{lane}: the surface diff's exit status is not captured"
            assert '2>/dev/null || true)"' not in body, (
                f"{lane}: the surface diff still swallows its own failure into an "
                "empty result, which the else branch publishes as a measured `false`"
            )
        # The head-object guard is same-repo-only BY DESIGN: the fork lane reaches
        # its head through `refs/pull/N/head`, and that its fetch precedes this
        # step is pinned separately by
        # `test_the_fork_scope_step_runs_where_its_two_inputs_exist`.
        same_repo = str(
            _step_by_name("security-scope-review.yml", "generate", "Resolve review scope")["run"]
        )
        assert "is not present after fetching it" in same_repo
