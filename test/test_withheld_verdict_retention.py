"""A withheld review verdict is retained where no later run can overwrite it.

A review lane that computes a verdict and cannot publish it holds that verdict
only in the job's own filesystem. Neither carrier the board has can keep it:
the lane's marker comment is updated IN PLACE, so a later run's body takes the
slot, and a check-run's conclusion and annotations are superseded by the next
attempt's. Both are also the only places the verdict's existence is recorded, so
losing the carrier loses the fact that a verdict was ever reached.

That gap is not hypothetical. On one pull request a fenced blocking verdict was
reported at one time and absent at another on the SAME head with no code change
between them, because a body edit re-ran the lane and the second sample did not
re-report the finding. Nothing had adjudicated the first verdict away; the only
record of it was a carrier that got rewritten.

A workflow artifact is neither rewritten nor replaced: it is written once under a
name unique to one run and one attempt. So the lanes retain a withheld verdict
there, and these tests hold three properties:

* the artifact carries the verdict's full text, not a summary of it;
* a second run on the same head adds a record BESIDE the first rather than
  replacing it;
* the record is reachable from the pull request side -- named in an annotation,
  and derivable from the head SHA alone so that a replaced annotation does not
  strand it.

The lanes are DISCOVERED from source rather than listed. The recurrence this
guards against is a review lane added later that publishes a verdict and never
wires the retention in, and a hardcoded list cannot fail for a lane that is not
on it.
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

RETAIN = "retain_unpublished_verdict"
ARTIFACT_OUTPUT = "retained_verdict_artifact"
RETAIN_DIR = "withheld-review-verdict"
ARTIFACT_PREFIX = "withheld-verdict-"

# The list this discovery is checked against, kept in step with
# test_ai_review_workflows.py's own `_VERDICT_PUBLISHING_LANES`. It is not what
# drives the tests -- it is the CONTROL that proves the discovery predicate
# still selects the lanes everyone already agrees publish a verdict. A predicate
# that silently stopped matching would otherwise pass by measuring nothing.
KNOWN_VERDICT_LANES = frozenset(
    {
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
    }
)


def _discover_verdict_steps() -> dict[str, tuple[str, dict]]:
    """Every workflow step that publishes a head-stamped review verdict.

    Keyed on two properties of the step's own script, neither of which is part
    of the mechanism under test: it WRITES a pull-request comment, and the body
    it writes carries a head-scoped ``[<LANE>-REVIEWED]`` stamp. Those two
    together are what makes a step a verdict publisher, so a lane added later
    is selected by being one rather than by being added to a list here.
    """
    found: dict[str, tuple[str, dict]] = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        for job_name, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                script = step.get("run")
                if not isinstance(script, str):
                    continue
                writes_comment = "gh pr comment " in script or "issues/comments/" in script
                if writes_comment and "-REVIEWED]" in script:
                    assert path.name not in found, f"{path.name}: two verdict steps"
                    found[path.name] = (job_name, step)
    assert found, "no verdict-publishing step discovered at all"
    return found


DISCOVERED = _discover_verdict_steps()
LANES = sorted(DISCOVERED)
LANE_PARAMS = [pytest.param(lane, id=lane) for lane in LANES]


def _steps(lane: str) -> list[dict]:
    doc = yaml.safe_load((WORKFLOWS / lane).read_text(encoding="utf-8"))
    job_name = DISCOVERED[lane][0]
    return list(doc["jobs"][job_name]["steps"])


def _shell_function(script: str, name: str) -> str:
    lines = script.split("\n")
    heads = [i for i, line in enumerate(lines) if line.strip() == f"{name}() {{"]
    assert len(heads) == 1, f"expected one {name}(), found {len(heads)}"
    start = heads[0]
    for j in range(start + 1, len(lines)):
        if lines[j].strip() == "}":
            return "\n".join(lines[start : j + 1])
    raise AssertionError(f"{name}() is never closed")


# --------------------------------------------------------------------------- #
# The mechanism reaches every lane that produces a verdict.
# --------------------------------------------------------------------------- #
class TestEveryVerdictLaneRetainsAWithheldVerdict:
    def test_discovery_still_selects_the_known_publishing_lanes(self) -> None:
        """The control on the predicate itself.

        Every lane the suite already agrees publishes a verdict must be
        discovered. Without this, a predicate that stopped matching would make
        every test below pass over an empty set.
        """
        missing = sorted(KNOWN_VERDICT_LANES - set(LANES))
        assert not missing, f"discovery stopped selecting known verdict lanes: {missing}"

    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_the_publish_step_retains_what_it_could_not_publish(self, lane: str) -> None:
        script = DISCOVERED[lane][1]["run"]
        assert f"{RETAIN}() {{" in script, (
            f"{lane}: publishes a verdict but defines no {RETAIN}, so a verdict it "
            f"cannot publish is lost with the job"
        )

    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_every_withheld_arm_retains_before_it_returns(self, lane: str) -> None:
        """Enumerated from the function's own arms, not from a list of them.

        Statuses 1 and 2 are exactly the answers that mean this run's verdict is
        not in the slot while the head it was computed for is still current.
        Every one of them must retain first. An arm added later -- the way this
        mechanism would most plausibly be bypassed -- fails here without anyone
        remembering to extend a list.
        """
        script = DISCOVERED[lane][1]["run"]
        body = _shell_function(script, "retry_comment_write").split("\n")
        withheld = [i for i, line in enumerate(body) if line.strip() in {"return 1", "return 2"}]
        assert withheld, f"{lane}: no withheld arm found; the primitive changed shape"
        for i in withheld:
            previous = body[i - 1].strip()
            assert previous.startswith(f"{RETAIN} "), (
                f"{lane}: the withheld arm at {body[i].strip()!r} is preceded by "
                f"{previous!r}, so that verdict is dropped rather than retained"
            )
        # Every retention names WHAT IT ESTABLISHED, the status it is retaining
        # for, the stamp that proves the body is a verdict for this head, the body
        # file, and the write command (which is where a PATCH's body lives).
        for i in withheld:
            call = body[i - 1].strip()
            status = body[i].strip().split()[1]
            outcome = call.split()[1]
            assert outcome in {"withheld", "publication_unknown"}, call
            assert call.startswith(f"{RETAIN} {outcome} {status} "), (call, body[i].strip())
            assert '"$needle"' in call, call
            assert '"$body_file"' in call, call
            assert call.endswith('"$@"'), call

    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_only_a_confirmed_absence_may_claim_non_publication(self, lane: str) -> None:
        """`withheld` is a claim about the world, so it needs to be established.

        A write that returned non-zero may still have landed -- the API can commit
        a POST and lose the acknowledgement -- and the read that would confirm its
        absence can fail in the same degradation, which is why it is not an
        independent second chance. So only two kinds of arm may say `withheld`:
        one where no write was attempted at all, and one where a SUCCESSFUL read
        found another run's comment in a slot this run had confirmed empty. Every
        other arm attempted a write whose absence was never confirmed and must say
        `publication_unknown`, or the record asserts non-publication the same way
        an ungated record asserted a verdict -- one fault, pointing either way.
        """
        body = _shell_function(DISCOVERED[lane][1]["run"], "retry_comment_write").split("\n")
        calls = [
            (i, line.strip())
            for i, line in enumerate(body)
            if line.strip().startswith(f"{RETAIN} ")
        ]
        assert len(calls) == 8, calls

        claims = [(i, c) for i, c in calls if c.split()[1] == "withheld"]
        unknown = [(i, c) for i, c in calls if c.split()[1] == "publication_unknown"]
        assert len(claims) == 3, claims
        assert len(unknown) == 5, unknown

        # The three that may claim it, identified by the message each arm printed
        # rather than by position, so a reordered function fails here.
        established = (
            "so nothing was posted",
            "so this run wrote NOTHING",
        )
        for i, call in claims:
            preceding = body[i - 1]
            assert any(phrase in preceding for phrase in established), (call, preceding.strip())

        # And every `publication_unknown` arm sits after a write was attempted.
        write_lines = [i for i, line in enumerate(body) if line.strip().startswith('if "$@"')]
        assert write_lines, "the write call moved"
        for i, call in unknown:
            assert i > min(write_lines), (call, i, write_lines)

    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_retention_is_gated_on_proof_before_it_writes_anything(self, lane: str) -> None:
        """A withheld ARM is not a withheld VERDICT.

        Two of these arms run before the caller's own stamp check, and the
        no-stamp arm is reached precisely because the body carries no stamp. So
        the gate has to be inside the retention, and it has to come BEFORE the
        first write: a receipt saying ``outcome=withheld`` for a run that reached
        no verdict asserts the opposite of what this mechanism is for, and the
        record is unique per run and attempt so nothing later contradicts it.
        """
        retain = _shell_function(DISCOVERED[lane][1]["run"], RETAIN)
        lines = retain.split("\n")
        # The proof: the caller's needle where it named one, the same head-scoped
        # stamp read off the text where it did not.
        proof = [i for i, line in enumerate(lines) if 'grep -Fq "$needle"' in line]
        generic = [i for i, line in enumerate(lines) if "-(REVIEWED|OVERRIDE)" in line]
        assert len(proof) == 1, proof
        assert len(generic) == 1, generic
        # And it is checked before ANYTHING touches the retention directory. Keyed
        # on the directory's own name rather than on a particular write, because a
        # write added later would spell its path differently and slip past a pin
        # that only knew today's spelling.
        touches = [i for i, line in enumerate(lines) if RETAIN_DIR in line]
        assert touches, "the retention directory name moved"
        assert max(proof + generic) < min(touches), (proof, generic, touches)
        for marker in ('echo "outcome=$outcome"', 'mkdir -p "$dir"', 'name="withheld-verdict-'):
            written = [i for i, line in enumerate(lines) if marker in line]
            assert written, marker
            assert max(proof + generic) < min(written), (marker, proof, generic, written)

    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_a_published_verdict_is_not_retained(self, lane: str) -> None:
        """No retention on a success arm, so a record cannot mean "published"."""
        script = DISCOVERED[lane][1]["run"]
        body = _shell_function(script, "retry_comment_write").split("\n")
        for i, line in enumerate(body):
            if line.strip() in {"return 0", "return 3", "return 4", "return 5"}:
                assert not body[i - 1].strip().startswith(f"{RETAIN} "), (
                    f"{lane}: {line.strip()!r} retains a verdict that is either "
                    f"published, already in the slot, or genuinely superseded"
                )

    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_the_retained_verdict_is_uploaded_as_an_artifact(self, lane: str) -> None:
        """The record has to leave the runner, or it dies with the job."""
        uploads = [
            step
            for step in _steps(lane)
            if "upload-artifact" in str(step.get("uses", ""))
            and RETAIN_DIR in str((step.get("with") or {}).get("path", ""))
        ]
        assert (
            len(uploads) == 1
        ), f"{lane}: expected one upload of the retained verdict, found {len(uploads)}"
        step = uploads[0]
        with_ = step.get("with") or {}
        condition = str(step.get("if") or "")
        # Gated on the retention having HAPPENED, so a published verdict uploads
        # nothing and a lane that reached no verdict leaves no record at all.
        assert f"steps.post.outputs.{ARTIFACT_OUTPUT}" in condition, condition
        assert f"steps.post.outputs.{ARTIFACT_OUTPUT}" in str(with_.get("name")), with_
        # A missing directory is a fault in this mechanism, not a normal run: the
        # same code writes the files and sets the output that gated this step.
        assert with_.get("if-no-files-found") == "error", with_
        # And it may be loud without being FATAL. A lane's conclusion is read by
        # pr-readiness.yml, which maps any non-success conclusion on the three
        # advisory labels to `<label> (BLOCK)` -- a judged-wrong verdict. Without
        # this, a transport flake in the upload, or that `error` above firing,
        # would invent a verdict nobody reached, which is the one thing these
        # lanes' own design forbids: infrastructure noise may never block a
        # merge. Retention is evidence ABOUT a verdict, never part of one.
        assert step.get("continue-on-error") is True, (
            f"{lane}: the retention upload can fail the lane, so a transport "
            f"fault in it would be reported as a judged BLOCK"
        )

    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_the_step_that_retains_is_the_step_the_upload_reads(self, lane: str) -> None:
        """``steps.post`` has to be the publishing step, in every lane."""
        steps = _steps(lane)
        posts = [step for step in steps if step.get("id") == "post"]
        assert len(posts) == 1, f"{lane}: expected one id: post, found {len(posts)}"
        assert posts[0].get("run") == DISCOVERED[lane][1]["run"], (
            f"{lane}: id: post is not the verdict-publishing step, so the upload "
            f"reads an output the retention never writes"
        )


# --------------------------------------------------------------------------- #
# The name is what makes the record additive and findable.
# --------------------------------------------------------------------------- #
class TestTheRecordIsAdditiveAndFindable:
    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_the_name_is_unique_per_run_and_per_attempt(self, lane: str) -> None:
        """Why a second run does not overwrite the first record.

        An artifact name is unique within one workflow run, so a name that did
        not carry the run and the attempt would either collide or replace on the
        re-run -- which is the exact case this exists for, since a re-run is how
        a withheld verdict is currently chased.
        """
        script = DISCOVERED[lane][1]["run"]
        retain = _shell_function(script, RETAIN)
        name_lines = [line for line in retain.split("\n") if line.strip().startswith("name=")]
        assert len(name_lines) == 1, name_lines
        name = name_lines[0]
        assert "GITHUB_RUN_ID" in name, name
        assert "GITHUB_RUN_ATTEMPT" in name, name

    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_the_name_is_derivable_from_the_head_sha(self, lane: str) -> None:
        """The pointer that cannot dangle, because it is computed not announced.

        An annotation is replaced by the next attempt's, so a record found only
        through an annotation is strandable. A name whose fixed prefix is the
        head SHA is reachable from the revision alone, for every run that ever
        withheld a verdict on it.
        """
        retain = _shell_function(DISCOVERED[lane][1]["run"], RETAIN)
        name = next(line for line in retain.split("\n") if line.strip().startswith("name="))
        assert f'"{ARTIFACT_PREFIX}${{HEAD}}' in name.strip(), name

    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_the_annotation_names_the_artifact(self, lane: str) -> None:
        """And the board carries the pointer while it is still the newest attempt."""
        retain = _shell_function(DISCOVERED[lane][1]["run"], RETAIN)
        notices = [line for line in retain.split("\n") if "::notice::" in line]
        assert len(notices) == 1, notices
        notice = notices[0]
        assert "$name" in notice, notice
        assert "GITHUB_RUN_ID" in notice and "GITHUB_RUN_ATTEMPT" in notice, notice
        # It says which of the two things happened, because the whole point is
        # that "a verdict exists and was not published" is not "no verdict" --
        # and it distinguishes a CONFIRMED non-publication from an unconfirmed
        # one, which is the difference the receipt's outcome field carries.
        assert "$note" in notice, notice
        assert 'note="did NOT publish it"' in retain, retain
        assert "could not confirm whether it published" in retain, retain


# --------------------------------------------------------------------------- #
# Behavioural: the lane's REAL bash, driven with a scripted `gh`.
# --------------------------------------------------------------------------- #
def _bash() -> str | None:
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            candidate = Path(git).resolve().parent.parent / "bin" / "bash.exe"
            if candidate.is_file():
                return str(candidate)
        return None
    return shutil.which("bash")


def _slice(script: str, start: str, end: str) -> str:
    lines = script.split("\n")
    heads = [i for i, line in enumerate(lines) if line.strip().startswith(start)]
    assert len(heads) == 1, f"expected one {start!r}, found {len(heads)}"
    i = heads[0]
    tails = [j for j in range(i, len(lines)) if lines[j].strip() == end]
    assert tails, f"no {end!r} after {start!r}"
    return "\n".join(lines[i : tails[0] + 1])


def _harness(lane: str) -> str:
    """The lane's real read-error helper, real retention, real write primitive."""
    script = DISCOVERED[lane][1]["run"]
    return "\n".join(
        (
            _slice(script, 'READ_ERR_FILE="', "}"),
            _shell_function(script, RETAIN),
            _shell_function(script, "retry_comment_write"),
            "",
        )
    )


HEAD = "0" * 40
OTHER_HEAD = "b" * 40


def _fake_bin(tmp_path: Path, gh_body: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text("#!/usr/bin/env bash\n" + gh_body, encoding="utf-8", newline="\n")
    gh.chmod(0o755)
    sleep = bin_dir / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n")
    sleep.chmod(0o755)
    return bin_dir


def _run(
    lane: str,
    tmp_path: Path,
    gh_body: str,
    driver: str,
    run_id: str = "5001",
    run_attempt: str = "1",
) -> subprocess.CompletedProcess[str]:
    bash = _bash()
    if bash is None:
        pytest.skip("no native bash available")
    bin_dir = _fake_bin(tmp_path, gh_body)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["RUNNER_TEMP"] = "."
    env["REPO"] = "owner/repo"
    env["PR"] = "13659"
    env["HEAD"] = HEAD
    env["GITHUB_RUN_ID"] = run_id
    env["GITHUB_RUN_ATTEMPT"] = run_attempt
    env["GITHUB_JOB"] = "publish"
    env["GITHUB_WORKFLOW"] = "Some Review"
    env["GITHUB_OUTPUT"] = "step-output.txt"
    (tmp_path / "step-output.txt").write_text("", encoding="utf-8", newline="\n")
    path = tmp_path / "driver.sh"
    path.write_text(_harness(lane) + driver, encoding="utf-8", newline="\n")
    return subprocess.run(
        [bash, path.name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(tmp_path),
        check=False,
    )


VERDICT_BODY = (
    "<!-- some-review -->\n"
    "## Some Review\n\n"
    f"[SOME-REVIEWED] {HEAD}\n"
    f"[BLOCK-MERGE] {HEAD}\n\n"
    "UPHOLD-FENCED F1 src/kiro_crew/knowledge/store.py:3175 -- the finding a\n"
    "reader has to be able to see in full, including this second line.\n"
)

# A slot held by ANOTHER revision's comment: the shape that withholds with the
# head still current, and the one measured live on the issue this fixes.
GH_SLOT_HELD_BY_OTHER_HEAD = f"""
case "$*" in
  *"issues/13659/comments"*) echo "5821447217 other" ;;
  *"pulls/13659"*) echo "{HEAD}" ;;
  *) exit 0 ;;
esac
"""

# What a run that reached NO verdict writes: a failure notice naming the head but
# carrying no review stamp for it. This is what the no-stamp arm is entered with.
UNSTAMPED_BODY = (
    "<!-- some-review -->\n"
    "## Some Review\n\n"
    f"This review did not complete for {HEAD}. No verdict was reached.\n"
)

GH_SLOT_EMPTY_WRITE_OK = f"""
case "$*" in
  *"issues/13659/comments"*) : ;;
  *"pulls/13659"*) echo "{HEAD}" ;;
  *) exit 0 ;;
esac
"""

# Slot confirmed empty, head unchanged, every comment WRITE refused.
GH_WRITE_ALWAYS_REFUSED = f"""
case "$*" in
  *"issues/13659/comments"*) : ;;
  *"pulls/13659"*) echo "{HEAD}" ;;
  *) echo "422 Unprocessable" >&2; exit 1 ;;
esac
"""

# The lost-acknowledgement shape. The FIRST comments read is the pre-write slot
# check: it succeeds and reports an empty slot, and the primitive breaks out of its
# six-attempt budget there, so it consumes exactly one. The write is then refused,
# and the SECOND comments read -- the landing check that would confirm whether that
# write actually landed -- fails. A counter file carries the count across `gh`
# invocations, since each one is a separate process.
GH_ACK_LOST_THEN_READ_FAILS = f"""
case "$*" in
  *"issues/13659/comments"*)
    n=0
    [ -f reads.n ] && n=$(cat reads.n)
    n=$((n + 1))
    printf '%s' "$n" > reads.n
    if [ "$n" -le 1 ]; then
      exit 0
    fi
    echo "gh: API rate limit exceeded for installation" >&2
    exit 1
    ;;
  *"pulls/13659"*) echo "{HEAD}" ;;
  *) echo "502 Bad Gateway" >&2; exit 1 ;;
esac
"""

# Every write refused, head unchanged, no override in the slot: the PATCH path
# runs its budget out and ends withheld.
GH_PATCH_ALWAYS_REFUSED = f"""
case "$*" in
  *"--method PATCH"*) echo "422 Unprocessable" >&2; exit 1 ;;
  *"issues/comments/5821447217"*) echo "an older revision's verdict" ;;
  *"pulls/13659"*) echo "{HEAD}" ;;
  *"issues/13659/comments"*) : ;;
  *) exit 0 ;;
esac
"""

CREATE_CALL = (
    "rc=0\n"
    'retry_comment_write "<!-- some-review -->" "[SOME-REVIEWED] $HEAD" body.md "" \\\n'
    '  gh pr comment "$PR" --body-file body.md || rc=$?\n'
    'echo "RC=$rc"\n'
)

PATCH_CALL = (
    "rc=0\n"
    'retry_comment_write "" "" "" 5821447217 gh api --method PATCH \\\n'
    '  "repos/$REPO/issues/comments/5821447217" \\\n'
    '  --field body="$(cat body.md)" || rc=$?\n'
    'echo "RC=$rc"\n'
)


def _driver(call: str, body: str = "") -> str:
    """The body is written with a heredoc, never through a substitution.

    ``$(cat ...)`` strips trailing newlines, so a body built that way is not the
    body the lane assembled and a byte-for-byte comparison would be measuring
    the harness instead of the retention.
    """
    return "cat > body.md <<'BODY_EOF'\n" + (body or VERDICT_BODY) + "BODY_EOF\n" + call


def _receipt(tmp_path: Path) -> dict[str, str]:
    text = (tmp_path / RETAIN_DIR / "receipt.txt").read_text(encoding="utf-8")
    out = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key] = value
    return out


class TestAWithheldVerdictIsRetainedInFull:
    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_a_slot_held_by_another_revision_retains_this_verdict(
        self, lane: str, tmp_path: Path
    ) -> None:
        """Status 2, the dominant withhold, keeps the whole verdict text."""
        result = _run(lane, tmp_path, GH_SLOT_HELD_BY_OTHER_HEAD, _driver(CREATE_CALL))
        assert "RC=2" in result.stdout, (result.stdout, result.stderr)

        receipt = _receipt(tmp_path)
        assert receipt["outcome"] == "withheld", receipt
        assert receipt["write_rc"] == "2", receipt
        assert receipt["head"] == HEAD, receipt
        assert receipt["artifact"].startswith(f"{ARTIFACT_PREFIX}{HEAD}"), receipt

        # The verdict, not a summary of it: every line of the body, including the
        # finding a reader has to judge.
        retained = (tmp_path / RETAIN_DIR / "verdict.md").read_text(encoding="utf-8")
        assert retained == VERDICT_BODY, retained
        assert "UPHOLD-FENCED F1" in retained
        assert "including this second line." in retained

    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_a_refused_patch_retains_the_body_it_could_not_write(
        self, lane: str, tmp_path: Path
    ) -> None:
        """The replace path carries its body in the command, not in a file."""
        result = _run(lane, tmp_path, GH_PATCH_ALWAYS_REFUSED, _driver(PATCH_CALL))
        assert "RC=1" in result.stdout, (result.stdout, result.stderr)

        receipt = _receipt(tmp_path)
        # A write WAS attempted here and its absence was never confirmed, so the
        # honest label is the uncertain one, not a claim of non-publication.
        assert receipt["outcome"] == "publication_unknown", receipt
        assert receipt["head"] == HEAD, receipt
        retained = (tmp_path / RETAIN_DIR / "verdict.md").read_text(encoding="utf-8")
        assert "UPHOLD-FENCED F1" in retained, retained
        assert "[BLOCK-MERGE] " + HEAD in retained, retained

    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_a_published_verdict_leaves_no_record(self, lane: str, tmp_path: Path) -> None:
        """So a record can never be read as "this was published"."""
        result = _run(lane, tmp_path, GH_SLOT_EMPTY_WRITE_OK, _driver(CREATE_CALL))
        assert "RC=0" in result.stdout, (result.stdout, result.stderr)
        assert not (tmp_path / RETAIN_DIR).exists(), "a published verdict left a record"
        assert ARTIFACT_OUTPUT not in (tmp_path / "step-output.txt").read_text(encoding="utf-8")

    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_withheld_is_told_from_no_verdict_without_reading_the_board(
        self, lane: str, tmp_path: Path
    ) -> None:
        """The distinction the whole mechanism exists for.

        A run that could not publish and a revision nobody reviewed look the same
        on the board: the slot holds no verdict for this head either way, and an
        annotation can read like a completed publish while the write was refused.
        So the answer may not be taken from the annotation or the step's status.
        It is taken from the write itself, which is why these three cases are
        distinguishable with the board ignored entirely.
        """
        withheld = tmp_path / "withheld"
        published = tmp_path / "published"
        for directory in (withheld, published):
            directory.mkdir()

        refused = _run(lane, withheld, GH_SLOT_HELD_BY_OTHER_HEAD, _driver(CREATE_CALL))
        assert "RC=2" in refused.stdout, refused.stdout
        wrote = _run(lane, published, GH_SLOT_EMPTY_WRITE_OK, _driver(CREATE_CALL))
        assert "RC=0" in wrote.stdout, wrote.stdout

        # A verdict exists and did not reach its slot.
        assert _receipt(withheld)["outcome"] == "withheld"
        assert (withheld / RETAIN_DIR / "verdict.md").read_text(encoding="utf-8") == VERDICT_BODY
        # A verdict exists and did reach it.
        assert not (published / RETAIN_DIR).exists()
        # No verdict was reached at all: the lane never runs the write, so there
        # is nothing to retain and no record appears. The absence of a record is
        # therefore not evidence of a published verdict on its own -- which is
        # why the record carries the outcome rather than only the body.
        never = tmp_path / "never"
        never.mkdir()
        idle = _run(lane, never, GH_SLOT_EMPTY_WRITE_OK, _driver('echo "RC=none"\n'))
        assert "RC=none" in idle.stdout, idle.stdout
        assert not (never / RETAIN_DIR).exists()

    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_the_withhold_is_announced_and_the_upload_is_armed(
        self, lane: str, tmp_path: Path
    ) -> None:
        """The two pull-request-side pointers, both written at the withhold."""
        result = _run(lane, tmp_path, GH_SLOT_HELD_BY_OTHER_HEAD, _driver(CREATE_CALL))
        name = _receipt(tmp_path)["artifact"]

        assert "::notice::" in result.stdout, result.stdout
        notice = next(line for line in result.stdout.splitlines() if line.startswith("::notice::"))
        assert name in notice, notice
        assert "did NOT publish" in notice, notice

        armed = (tmp_path / "step-output.txt").read_text(encoding="utf-8")
        assert f"{ARTIFACT_OUTPUT}={name}" in armed, armed


class TestNoVerdictIsNeverRecordedAsAWithheldOne:
    """The inverse failure, and the one that would break the whole mechanism.

    Several withhold arms are reached with a body that is not a verdict: a skip
    notice, a staleness notice, or the failure notice a run writes when it
    reached no verdict at all. Two of them run before the caller's stamp check,
    and the no-stamp arm is entered *because* the body carries no stamp. If any
    of those were retained, the record would assert that a verdict exists for the
    revision when none was ever reached -- and since the record is unique per run
    and attempt, no later run could contradict it. That is worse than retaining
    nothing, because the distinction this mechanism exists to keep would be
    inverted rather than merely missing.
    """

    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_an_unstamped_body_in_an_occupied_slot_retains_nothing(
        self, lane: str, tmp_path: Path
    ) -> None:
        """Status 2, reached before the caller's stamp check."""
        result = _run(
            lane, tmp_path, GH_SLOT_HELD_BY_OTHER_HEAD, _driver(CREATE_CALL, body=UNSTAMPED_BODY)
        )
        assert "RC=2" in result.stdout, (result.stdout, result.stderr)
        assert not (tmp_path / RETAIN_DIR).exists(), "a run with no verdict left a record"
        assert ARTIFACT_OUTPUT not in (tmp_path / "step-output.txt").read_text(encoding="utf-8")
        assert "::notice::" not in result.stdout, result.stdout
        assert "Nothing is retained" in result.stdout, result.stdout

    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_an_unstamped_body_whose_write_is_refused_retains_nothing(
        self, lane: str, tmp_path: Path
    ) -> None:
        """The no-stamp arm: entered because the body is not a verdict."""
        result = _run(
            lane, tmp_path, GH_WRITE_ALWAYS_REFUSED, _driver(CREATE_CALL, body=UNSTAMPED_BODY)
        )
        assert "RC=1" in result.stdout, (result.stdout, result.stderr)
        assert "no current-head stamp" in result.stdout, result.stdout
        assert not (tmp_path / RETAIN_DIR).exists(), "a run with no verdict left a record"
        assert "::notice::" not in result.stdout, result.stdout

    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_a_patch_carrying_only_an_older_heads_stamp_retains_nothing(
        self, lane: str, tmp_path: Path
    ) -> None:
        """The replace path names a comment id, so it supplies no needle.

        Its body is not always a verdict for this head: one lane prepends a
        staleness notice to the comment it is preserving, which carries the
        PREVIOUS head's stamp. That must not be recorded as this head's withheld
        verdict.
        """
        stale = VERDICT_BODY.replace(HEAD, OTHER_HEAD)
        result = _run(lane, tmp_path, GH_PATCH_ALWAYS_REFUSED, _driver(PATCH_CALL, body=stale))
        assert "RC=1" in result.stdout, (result.stdout, result.stderr)
        assert not (
            tmp_path / RETAIN_DIR
        ).exists(), "another head's verdict was retained as this one's"
        assert "Nothing is retained" in result.stdout, result.stdout

    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_a_stamped_body_whose_write_is_refused_is_still_retained(
        self, lane: str, tmp_path: Path
    ) -> None:
        """The gate must not swallow the case the mechanism exists for."""
        result = _run(lane, tmp_path, GH_WRITE_ALWAYS_REFUSED, _driver(CREATE_CALL))
        assert "RC=1" in result.stdout, (result.stdout, result.stderr)
        receipt = _receipt(tmp_path)
        assert receipt["outcome"] == "publication_unknown", receipt
        assert receipt["head"] == HEAD, receipt
        assert (tmp_path / RETAIN_DIR / "verdict.md").read_text(encoding="utf-8") == VERDICT_BODY


class TestNonPublicationIsClaimedOnlyWhenEstablished:
    """A lost acknowledgement must not become a claim that nothing was published.

    GitHub can commit a POST and lose the acknowledgement, so a write returning
    non-zero does not mean the comment is absent. The read that would settle it
    can fail in the same API degradation -- correlated with the lost ack, not an
    independent second chance -- and unlike every other gating read in this chain
    it gets one attempt, not six. A record saying `withheld` there asserts
    non-publication that nothing established, which is the first defect in this
    file pointing the other way.
    """

    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_a_failed_confirmation_read_records_unknown_not_withheld(
        self, lane: str, tmp_path: Path
    ) -> None:
        """Write refused, then the landing read fails: publication is UNKNOWN."""
        result = _run(lane, tmp_path, GH_ACK_LOST_THEN_READ_FAILS, _driver(CREATE_CALL))
        assert "RC=1" in result.stdout, (result.stdout, result.stderr)
        assert "Cannot confirm whether the previous attempt landed" in result.stdout

        receipt = _receipt(tmp_path)
        assert receipt["outcome"] == "publication_unknown", receipt
        assert receipt["head"] == HEAD, receipt
        # The verdict is still kept in full -- the uncertainty is about where it
        # went, never about what it said.
        assert (tmp_path / RETAIN_DIR / "verdict.md").read_text(encoding="utf-8") == VERDICT_BODY

        notice = next(line for line in result.stdout.splitlines() if line.startswith("::notice::"))
        assert "could not confirm whether it published" in notice, notice
        # And it may not ALSO assert non-publication. Anchored on the claim
        # itself, not on one punctuation of it, so a notice that hedges both
        # ways fails here rather than reading as honest.
        assert "did NOT publish" not in notice, notice

    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_a_confirmed_absence_does_claim_non_publication(
        self, lane: str, tmp_path: Path
    ) -> None:
        """No write attempted at all, so `withheld` is established and said."""
        result = _run(lane, tmp_path, GH_SLOT_HELD_BY_OTHER_HEAD, _driver(CREATE_CALL))
        assert "RC=2" in result.stdout, (result.stdout, result.stderr)
        assert _receipt(tmp_path)["outcome"] == "withheld", _receipt(tmp_path)
        notice = next(line for line in result.stdout.splitlines() if line.startswith("::notice::"))
        assert "did NOT publish it" in notice, notice


class TestASecondRunDoesNotReplaceTheFirstRecord:
    @pytest.mark.parametrize("lane", LANE_PARAMS)
    def test_two_runs_on_one_head_retain_under_two_names(self, lane: str, tmp_path: Path) -> None:
        """The property the issue's live instance turns on.

        Two runs on the SAME head with no code change between them is exactly
        how a verdict was lost: the second sample replaced the first everywhere
        the first was recorded. Each run's record has to stand on its own.
        """
        first = tmp_path / "run-5001-attempt-1"
        second = tmp_path / "run-5002-attempt-1"
        # A re-run of ONE run is the other half: same run id, new attempt. An
        # artifact name is unique within a run, so an attempt that reused the
        # name would collide with, or replace, the attempt that came before it.
        third = tmp_path / "run-5001-attempt-2"
        for directory in (first, second, third):
            directory.mkdir()

        names = []
        for directory, run_id, attempt in (
            (first, "5001", "1"),
            (second, "5002", "1"),
            (third, "5001", "2"),
        ):
            result = _run(
                lane,
                directory,
                GH_SLOT_HELD_BY_OTHER_HEAD,
                _driver(CREATE_CALL),
                run_id=run_id,
                run_attempt=attempt,
            )
            assert "RC=2" in result.stdout, (result.stdout, result.stderr)
            receipt = _receipt(directory)
            # Each record stands alone: its own head, its own timestamp, its own
            # status -- not a diff against another run's.
            assert receipt["head"] == HEAD, receipt
            assert receipt["run_id"] == run_id, receipt
            assert receipt["run_attempt"] == attempt, receipt
            assert receipt["write_rc"] == "2", receipt
            assert receipt["withheld_at"], receipt
            body = (directory / RETAIN_DIR / "verdict.md").read_text(encoding="utf-8")
            assert body == VERDICT_BODY
            names.append(receipt["artifact"])

        assert len(set(names)) == 3, names
        # And all three are still reachable from the one thing that does not
        # change between them: the revision they judged.
        for name in names:
            assert name.startswith(f"{ARTIFACT_PREFIX}{HEAD}"), name
