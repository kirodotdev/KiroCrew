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

        Two admissible sources: a ``git show`` from the base ref into a staging
        directory, or a workspace that IS a trusted tree (the base commit, or the
        default branch). Both are proven the way the harness's other two files are
        -- by refusing when the file is absent -- so the pin is that the lane
        carries that refusal.
        """
        text = _workflow(workflow)
        assert "if [ ! -s scripts/scope_redact.py ]" in text or (
            'git show "$BASE_SHA:scripts/scope_redact.py"' in text
        )
        assert "Refusing to publish" in text

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    def test_an_always_upload_of_derived_text_reads_the_scrub_verdict(self, workflow: str) -> None:
        """``if: always()`` may not outlive the scrub that gates the file.

        An artifact is world-readable on a public repository, so an upload is an
        outbound surface of its own. ``always()`` is legitimate on one whose
        content is lane-authored or already proven scrubbed; on one that can carry
        unscrubbed derived text it republishes precisely what the refusal
        withheld. The corpus upload is the deliberate exception -- it is not
        scrubbed at all, and is gated on the probe's verdict instead, which the
        test below pins.
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

        The scrub moved out of the credentialed job, so the review text crosses to
        the scrubbing job unscrubbed. That is admissible only while nothing else
        consumes it: a `publish` that read the raw name would post exactly the text
        the scrub exists to rewrite, and the whole move would have bought nothing.
        So every `-raw` artifact is downloaded by exactly one job, that job holds no
        `id-token`, and it scrubs.
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

        `Write` is granted so the model can produce `candidates.json`, and the tool
        takes an ABSOLUTE path -- so no directory on the runner is provably out of
        its reach, the checkout and `runner.temp` alike. Staging the redactor after
        the call was the earlier answer, and it was one ordering away from being
        wrong. The answer now is that the model's job does not scrub at ALL: it
        holds the Bedrock credential, so it runs no program this repository ships,
        and the captured text crosses to a job holding nothing. Nothing there to
        replace, and nothing there to steal.
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
    """The recurring defect this closes, stated once for every job in both lanes.

    A job holding the Bedrock role or a write-scoped token must not execute a
    program the change under review can supply. Four separate fixes each protected
    one path while its sibling kept the defect, so the pin is per JOB and per
    SPELLING rather than on the branch that was last reported: the base-ref read is
    the normal path, and the bootstrap window -- absent at base, present in the
    working tree -- is where every one of those fixes leaked.
    """

    #: The lane's own programs. A privileged job may run these only from a staged
    #: copy of a COMMITTED blob, which is a path under `runner.temp`, never a path
    #: in the workspace and never a `cp` out of it.
    HARNESS = ("scope_candidates.py", "deny_diff.py", "scope_redact.py")

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    def test_no_privileged_job_copies_a_program_out_of_the_workspace(self, workflow: str) -> None:
        """`cp` out of `scripts/` is the spelling that reintroduced this each time.

        A `git show <sha>:scripts/<program>` reads committed content, which the
        reviewer's `Write` cannot reach and no earlier step in the job can rewrite.
        A `cp` reads the checked-out tree, which on a `pull_request` event IS the
        change under review. The two are one line apart and only one of them is safe
        in a job that holds something.

        The subject is the SOURCE DIRECTORY, not the file name: the staging loops
        spell their file as `$f`, so a pin naming `scope_candidates.py` matches
        nothing and reports green over exactly the line it was written for. `scripts/`
        is also the whole of what must not be copied -- the review contract is data
        the model reads, and its own bootstrap is not program execution.
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

        A job whose checkout is the base commit or the default branch has a
        workspace that IS a trusted tree, and running `scripts/<program>` there is
        running the committed harness. A job whose checkout resolves to the pull
        request's merge ref has the change under review on disk, and the same line
        runs the change's own code -- so such a job may execute only a staged copy
        of a committed blob. Both lanes are asserted against the same rule, which is
        what keeps one lane's posture from drifting from the other's.
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

        `id-token: write` is an AWS credential: the job can assume the Bedrock role
        whenever it likes, so "the credential is only live after this step" is not a
        property anything can check. Repository Python is the code a pull request
        can supply -- every one of this lane's three programs has a bootstrap window
        in which the executed copy comes from the change under review -- so the two
        must not share a job. `jq`, `gh`, `git` and `awk` may: they are the runner's
        own tools, and a pull request cannot rewrite them.

        Stated as "no python at all" rather than as an allowlist of programs,
        because the previous shape of this pin permitted exactly one -- the outbound
        scrubber -- and that permission is what kept a PR-suppliable blob executing
        beside the role for four rounds of fixes.
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

        `contents: read` is what a checkout needs and is the whole grant. A
        `pull-requests: read` here would be a comment feed for a program the pull
        request supplied, and any write at all would be the defect moved rather
        than fixed.
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
        """The probe survived the move, and what it can still buy is what it buys.

        The rows must reach the classifier byte-faithful: a placeholder standing
        where a command belongs is a command nobody ever refused, and it classifies
        as allowed. So the file cannot be redacted, and the answer is to probe a
        copy and refuse the run. The probe is `scope_redact.py`, though, so it may
        not run beside the credential -- it runs in `validate`, which means the rows
        are already public when it fires. What it still prevents is the part that
        lasts: a row carrying a credential shape is never adjudicated, and the
        normalized corpus the legs read never travels.
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

    The arguments come off the PARSED ``claude_args`` block, so a flag that reaches
    this list is one the action is actually handed: a commented-out or prose mention
    of the same flag lives in a YAML comment outside the block scalar and never
    appears here. A ``#`` line INSIDE the block would be literal text in the
    argument string, so those are dropped too.
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

    ``claude-code-action`` auto-loads the CHECKOUT's ``CLAUDE.md`` (and what it
    imports) and ``.claude/`` as instructions, and a ``.claude/settings.json``
    ``SessionStart`` hook EXECUTES. The same-repo ``generate`` job checks out with no
    explicit ``ref``, which on a ``pull_request`` event is the merge ref -- so a pull
    request that adds such a hook runs its own code while the Bedrock credential is
    live, through a channel that is not a ``run:`` step and that no
    ``--allowedTools`` grant bounds. ``--setting-sources user`` consults the
    runner's (empty) home and nothing from the checkout.

    Pinned per LANE and per CALL rather than on the site that was last fixed: this
    defect class has recurred on this pull request by being closed at one lane and
    left at its sibling. The fork lane checks out ``base_sha``, so its project
    settings are base-owned and it is not exposed today -- the flag is still
    required there, because the exposure is one checkout-ref edit away.
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

        Adding a source is how this would be reintroduced while the flag stays
        present, so the pin is on the VALUE and not on the flag's existence.
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

    Both lanes upload the model's text from the job that holds the credential, and
    the credential-shape probe runs LATER, in `validate` -- so a value the model
    wrote is already public by the time anything looks at it. Filtering the way out
    therefore cannot be the control: a value that is chunked, delimited or
    re-encoded matches no shape rule. The control is that the credential is not
    reachable, which takes all three of these at once:

    * no `Bash` grant, so there is no shell to print an environment with;
    * `Read(//proc/**)` + `Read(//sys/**)` DENIED, because `Read` is path-unscoped
      and `/proc/self/environ` is an ordinary file that needs no shell;
    * the runner's own on-disk copy of the exported credential
      (`$RUNNER_TEMP/_runner_file_commands/set_env_*`) truncated first, because it
      is a FILE and so is covered by neither of the other two.

    Asserted over the PARSED `claude_args` and the parsed step list, never a
    substring grep of the file: a NEW model call added without the fence is the
    regression this exists to catch, and a whole-file assertion passes with one
    unfenced step sitting beside a fenced sibling.
    """

    ACTION = "anthropics/claude-code-action"
    SCRUB = "Scrub persisted credential files before the model runs"
    #: One canonical scrub across every lane holding this credential. Read from the
    #: reference lane rather than restated, so a fix there cannot leave these two
    #: behind -- which is this pull request's recurring defect.
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
            # `Write` is granted plainly and bounded by DENY rules instead, which is
            # not laziness: bounding it here to `Write(candidates.json)` was tried and
            # broke the lane (the matcher resolves to an absolute path, so the narrow
            # allow never matched the write the prompt asks for). The escalation is
            # bounded by the two out-of-workspace denies asserted in the fence test
            # below. What this pin still guarantees is the shape of the ALLOW list:
            # the read trio, `Write`, and nothing else -- above all no Bash.
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
            # Same rule for the WRITE bound, which is the other fence in this step.
            # It must be a DENY: a narrowed allow rests on the prompt fallback this
            # test exists to reject, and bounding the allow was also measured to break
            # the lane outright. The two regions are the ones that carry the
            # escalation, and BOTH are required -- `_actions` holds the action's own
            # later-executing JavaScript, and the runner temp holds `$GITHUB_ENV`,
            # a write to which injects environment into every later step. Denying one
            # is half a fence.
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

    The model is granted `Read`/`Grep`/`Glob` and the credential fence is a PATH
    deny, `Read(//proc/**)`. A TRACKED SYMLINK committed in the pull request --
    `notes.md -> /proc/self/environ` -- is materialized at a WORKSPACE path, and
    reading THAT path matches no glob in the fence: the value reaches the review
    text this lane uploads as a world-readable artifact, before anything has probed
    a byte of it. No deny list closes that, because a symlink is precisely how a
    workspace path becomes a kernel-filesystem path.

    So the head reaches the runner as the prefetched unified diff and not as files.
    Both lanes are asserted against the one rule: the fork lane has always run this
    posture, and the same-repo lane diverging from it is this pull request's
    recurring defect class.
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

        One rule with two readers: a workspace judged trusted for "may this job run
        `scripts/x.py`" is the same workspace judged here for "may the model read
        it". Splitting them is how the two lanes came apart in the first place.
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
        """The bootstrap window is where the base-owned rule has leaked every time.

        With the base checked out there is no head copy on disk, so the bootstrap
        reads the head BLOB -- committed content -- exactly as `publish`'s own
        bootstraps do. A `cp` here would be the head-side read arriving by another
        name.
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

        That is the trap the fork lane's step ordering exists for: with no head
        object the surface diff names nothing, and the `else` branch writes
        `in_scope=false` -- an honest-looking skip the tri-state gate above is
        REQUIRED to honor. So an unreachable ref and a failed diff both fail closed
        here, where the tri-state cannot see them.

        BOTH lanes, because this pin read the same-repo lane alone while the fork
        twin kept `2>/dev/null || true` on the identical diff -- and the fork side
        is the worse half: `in_scope=false` there publishes a GREEN check-run and
        sets no per-head floor, so a fork tightening whose diff merely failed to
        run would pass unmeasured on the more hostile source.
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
