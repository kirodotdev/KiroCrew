"""The crew-image publish path must stay usable, and must stay out of the fork lane.

``cloud/fargate/taskdef.py`` refuses a movable tag and requires
``<repository>@sha256:<64 hex>``, and a digest exists only after a push. The
build lane already pushes -- to a throwaway registry on ``127.0.0.1`` that dies
with the job, which is what lets it run on a fork pull request with no secrets at
all. ``scripts/publish_crew_base_image.sh`` is the durable half.

Three properties are worth a ratchet, because breaking any of them is a small,
plausible edit that nothing else would report:

* **The repository has no default.** A default would either name someone's
  account in the tree or quietly publish to the wrong one.
* **The reference is held to the shape taskdef accepts, at the push.** Checking
  it later means a refusal arrives far from the push that caused it, so the
  script's own check is compared against ``taskdef``'s pattern by BEHAVIOUR.
* **The fork lane names no secret and does not call this script.** Wiring
  publishing into it would either break that lane on a fork or make it skip
  silently, and a skipped gate reads exactly like a passing one.

Static and offline: nothing here builds an image, runs Docker or reaches a
network. The tests that execute the script pass no ``--repository`` or an invalid
one, so it exits during argument handling and never reaches a build; the two that
execute a fragment inline the selection loop alone. Every one of them is bounded
by ``REFUSAL_TIMEOUT_SECONDS``, so a guard removed by a mutation cannot fall
through into a real multi-gigabyte build -- it runs out of time and reds.

The tests that execute it take ``publish_shell``, which comes from the shared
``posix_test_shell`` fixture, so the Windows shard runs them under Git Bash rather
than skipping them; the rest read the script's text and need no shell at all.
Nothing is resolved while this file is IMPORTED, because an import runs before the
environment scrub that every test body gets.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.cloud.fargate.taskdef import _DIGEST_REF_RE
from kiro_crew.subprocess_utf8 import UTF8_TEXT

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "publish_crew_base_image.sh"
PRODUCER = ROOT / "scripts" / "build_crew_base_image.sh"
FORK_LANE = ROOT / ".github" / "workflows" / "crew-image-build.yml"

#: A repository with no account id and no real namespace, so the fixture cannot
#: become the hardcoded value the tests below forbid.
SAMPLE_REPOSITORY = "registry.invalid/example/kirocrew-crew-base"


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


#: Seconds a refusal is allowed. Every caller stops the script during argument
#: handling, which takes well under a second -- so a SHORT cap is not impatience,
#: it is what keeps a mutated guard from letting the script fall through into a
#: real multi-minute image build before the assertion can fail it.
REFUSAL_TIMEOUT_SECONDS = 15


def _shell_for_the_script(posix_shell: str) -> str:
    """Bash, given whatever the shared POSIX-shell fixture resolved. Runs nothing.

    A path already naming bash is taken AS IS. That matters most on Windows, where
    the shared fixture has done the work of finding Git Bash and a second lookup by
    name would find ``C:\\Windows\\System32\\bash.exe`` instead -- the WSL launcher,
    which with no distro installed prints a UTF-16 banner, exits 1 and runs nothing.

    On a POSIX host the fixture answers ``sh``, and this script cannot run under it:
    its shebang is ``env bash``, it reads ``${BASH_SOURCE[0]}`` -- an array reference
    -- and it sets ``pipefail``, none of which POSIX ``sh`` has. So bash is resolved
    by name there. A host with a POSIX shell but no bash at all FAILS rather than
    skipping, because a host that cannot run a bash script cannot report on one.
    """
    if Path(posix_shell).stem.lower() == "bash":
        return posix_shell
    resolved = shutil.which("bash")
    assert resolved is not None, (
        f"{posix_shell} was found but no bash was; this script's shebang is "
        "`env bash` and it reads ${BASH_SOURCE[0]}, so a POSIX sh cannot run it"
    )
    return resolved


@pytest.fixture
def publish_shell(posix_test_shell: str) -> str:
    """The shell the tests below run the script with.

    ``posix_test_shell`` (``test/conftest.py``) is the repository's shared answer to
    finding a real POSIX shell on every supported host, and reaching for it is what
    lets the Windows shard EXERCISE these tests rather than skip them.
    """
    return _shell_for_the_script(posix_test_shell)


def test_the_shell_the_shared_fixture_resolves_is_taken_as_it_comes() -> None:
    """The Windows half of the decision, exercised from any platform.

    Windows is the shard whose coverage is the reason these tests execute anything,
    and it is the one an author cannot watch. So the choice is a pure function of a
    path and is checked here with the path Windows produces, rather than being
    believed until a shard reports.
    """
    git_bash = "C:/Program Files/Git/bin/bash.exe"
    assert _shell_for_the_script(git_bash) == git_bash, (
        "a path already naming bash must be used as it comes; looking bash up again "
        "on Windows finds the WSL launcher, which is what the shared fixture avoids"
    )
    assert Path(_shell_for_the_script("/bin/sh")).stem == "bash", (
        "a POSIX sh was accepted as a stand-in for bash, which cannot read " "${BASH_SOURCE[0]}"
    )


def test_the_shell_the_tests_run_the_script_with_actually_runs(publish_shell: str) -> None:
    """Resolution finds a name; this is where that name is executed.

    Executing here rather than at import is what keeps a startup hook out of a shell
    nothing has scrubbed for yet -- see ``test_importing_this_file_starts_no_process``
    below for why an import is the wrong moment. A shell that is found but does not
    run reds here, which is louder than the whole executing half skipping.
    """
    probe = subprocess.run(
        [publish_shell, "-c", "echo ok"],
        capture_output=True,
        timeout=REFUSAL_TIMEOUT_SECONDS,
        **UTF8_TEXT,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert probe.stdout.strip() == "ok", probe.stdout + probe.stderr


#: Attribute calls that start a process. Any of them on the import path would run
#: before the first scrub, so the name of the module they hang off does not matter.
_PROCESS_STARTING_CALLS = frozenset(
    {
        "Popen",
        "call",
        "check_call",
        "check_output",
        "execv",
        "execvp",
        "execvpe",
        "popen",
        "run",
        "spawnl",
        "spawnlp",
        "spawnv",
        "spawnvp",
        "system",
    }
)


def _process_starts_on_the_import_path() -> list[str]:
    """Every process-starting call importing this file would reach.

    Module-level statements, plus the bodies of the module-level functions those
    statements call by name. One hop, which is the whole of the import path here --
    and a second hop introduced later would have to add a module-level call to
    reach it, which this same walk reports.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    defined = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    on_path: list[ast.stmt] = []
    called_by_name: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        on_path.append(statement)
        for node in ast.walk(statement):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called_by_name.add(node.func.id)
    on_path.extend(defined[name] for name in sorted(called_by_name) if name in defined)
    starts: list[str] = []
    for statement in on_path:
        for node in ast.walk(statement):
            if not isinstance(node, ast.Call):
                continue
            called = node.func
            if isinstance(called, ast.Attribute) and called.attr in _PROCESS_STARTING_CALLS:
                starts.append(f"{ast.unparse(called)}(...) on line {node.lineno}")
    return starts


def test_importing_this_file_starts_no_process() -> None:
    """Stated as a property so a later edit cannot quietly reintroduce the spawn.

    This holds on every platform and needs no shell, which is what makes it the
    net under the Windows path as well as the POSIX one.
    """
    assert _process_starts_on_the_import_path() == [], (
        "importing this file would start a process before any environment scrub "
        "has run; resolve at module scope and execute inside a test body instead"
    )


#: Seconds a fresh interpreter is allowed to import this file. Measured at well
#: under a second, and bounded for the same reason a refusal is: an import reads
#: this file's own module body, so no cap here can be spent on an image build.
IMPORT_TIMEOUT_SECONDS = 30

#: Imports the file named on the command line and nothing else, so the child's
#: only shell activity is whatever the import itself performs.
_IMPORT_THE_FILE = """
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("import_under_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
"""


def test_an_inherited_bash_env_hook_does_not_fire_when_this_file_is_imported(
    tmp_path: Path, publish_shell: str
) -> None:
    """The property above, measured against the condition that motivates it.

    ``BASH_ENV`` names a file every non-interactive bash sources before it runs
    anything, so a shell started while this file is imported would execute it with
    nothing scrubbed. The hook here only leaves a marker; a real one is free to
    write into the checkout, which is why the import is held to starting no shell
    at all rather than to starting a harmless one.
    """
    marker = tmp_path / "hook-fired"
    hook = tmp_path / "bash_env_hook.sh"
    hook.write_text(f'printf fired > "{marker.as_posix()}"\n', encoding="utf-8")
    environment = dict(os.environ)
    environment["BASH_ENV"] = hook.as_posix()
    # The child must resolve the same imports this test process did, whether the
    # package is installed or merely on the path.
    environment["PYTHONPATH"] = os.pathsep.join(entry for entry in sys.path if entry)
    imported = subprocess.run(
        [sys.executable, "-c", _IMPORT_THE_FILE, str(Path(__file__).resolve())],
        capture_output=True,
        cwd=tmp_path,
        env=environment,
        timeout=IMPORT_TIMEOUT_SECONDS,
        **UTF8_TEXT,
    )
    assert imported.returncode == 0, imported.stderr
    assert not marker.exists(), (
        "importing this file started a shell, which sourced the inherited BASH_ENV "
        "hook in the checkout before a single guard was active"
    )


#: Ways a test file takes itself out of a run.
_SKIP_CALLS = frozenset({"skip", "skipif", "xfail"})


def test_this_file_adds_no_skip_of_its_own() -> None:
    """A skip added here would report a pass for tests a shard never ran.

    The one skip in this path belongs to ``posix_test_shell``, and it is the
    repository's, applied the same way across every suite that needs a POSIX shell.
    A condition written HERE is this file's own, and the shard it fires on is
    usually the one whose coverage was the reason to have these tests -- Windows,
    where the executable contract is least like the author's machine.

    Read from this file's own text, so it holds wherever it runs.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    skips = [
        f"{ast.unparse(node.func)}(...) on line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _SKIP_CALLS
    ]
    assert skips == [], (
        "this file skips itself rather than running; take the shell from "
        f"posix_test_shell so every shard exercises the script: {skips}"
    )


def _run(shell: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the script. Every caller here stops it during argument handling.

    The script is named by its POSIX path: Git Bash reads a forward-slash path
    reliably, and a Windows path's backslashes are ambiguous to it.
    """
    return subprocess.run(
        [shell, SCRIPT.as_posix(), *args],
        capture_output=True,
        cwd=ROOT,
        timeout=REFUSAL_TIMEOUT_SECONDS,
        **UTF8_TEXT,
    )


def _run_harness(shell: str, harness: str) -> subprocess.CompletedProcess[str]:
    """Run a fragment that inlines a block of the script.

    Bounded by the same cap as a refusal, and for the same reason: these
    harnesses define ``die`` and a candidate list and run the selection loop
    alone, so nothing here reaches a build either.
    """
    return subprocess.run(
        [shell, "-c", harness],
        capture_output=True,
        timeout=REFUSAL_TIMEOUT_SECONDS,
        **UTF8_TEXT,
    )


def test_the_script_exists_and_is_committed_executable() -> None:
    """Executability is read from GIT, not from the filesystem.

    A Windows checkout does not carry the POSIX exec bit -- ``st_mode`` comes back
    as ``0o100666`` there -- so a filesystem check fails on Windows for a file that
    is perfectly executable everywhere it runs. What actually has to be true is
    that the COMMITTED mode is ``100755``, which is the same on every platform.
    """
    assert SCRIPT.is_file(), f"{SCRIPT} is missing"
    listed = subprocess.run(
        ["git", "ls-files", "--stage", "--", str(SCRIPT.relative_to(ROOT).as_posix())],
        capture_output=True,
        cwd=ROOT,
        timeout=REFUSAL_TIMEOUT_SECONDS,
        **UTF8_TEXT,
    )
    assert listed.returncode == 0, listed.stderr
    assert listed.stdout.strip(), f"{SCRIPT} is not tracked by git"
    mode = listed.stdout.split()[0]
    assert mode == "100755", f"the committed mode is {mode}, so the script is not executable"


def test_the_repository_is_required_and_has_no_default(publish_shell: str) -> None:
    """Absent is refused, not guessed."""
    result = _run(publish_shell)
    assert result.returncode != 0, "publishing with no repository must refuse"
    assert "--repository is required" in result.stderr, result.stderr


def test_no_default_repository_is_assigned_anywhere() -> None:
    """The other half of "no default", and the half that holds on every platform.

    An empty initialiser is the whole point; a registry-shaped one would make the
    refusal above unreachable. Read from the text, so this keeps its verdict on a
    shard that cannot execute the script at all.
    """
    assignments = re.findall(r"^REPOSITORY=(.*)$", _script(), flags=re.MULTILINE)
    assert assignments, "REPOSITORY is never initialised, so the guard reads an unset variable"
    assert all(value.strip() in {'""', "''"} for value in assignments), assignments


@pytest.mark.parametrize(
    ("repository", "expected"),
    [
        ("registry.invalid/example/repo:sometag", "must not carry a tag"),
        ("registry.invalid/example/repo@sha256:" + "a" * 64, "must not carry a digest"),
    ],
    ids=["tag", "digest"],
)
def test_a_repository_carrying_a_tag_or_digest_is_refused(
    repository: str, expected: str, publish_shell: str
) -> None:
    """Refused rather than trimmed, and refused by ITS OWN guard.

    ``repo:1.2`` is a tag or a port on a registry host and the two readings are
    not distinguishable here, so dropping the wrong half publishes somewhere the
    caller did not name.

    The expected message is matched exactly per case, not loosely. A digest
    reference's last segment also contains a colon, so the TAG guard refuses it
    too -- which means a test asserting only "must not carry" passes with the
    digest guard deleted. Measured: that mutation was the one hole in this file's
    first draft.
    """
    result = _run(publish_shell, "--repository", repository)
    assert result.returncode != 0, f"{repository!r} must be refused"
    assert expected in result.stderr, f"expected {expected!r}, got: {result.stderr}"


def test_no_account_id_and_no_real_namespace_is_hardcoded() -> None:
    """The account and namespace belong to whoever runs this, not to the tree."""
    text = _script()
    account_ids = re.findall(r"(?<![0-9])[0-9]{12}(?![0-9])", text)
    assert not account_ids, f"a 12-digit account id is hardcoded: {account_ids}"
    # `public.ecr.aws` may appear in prose as the example form; what must not
    # appear is a concrete namespace under it.
    concrete = re.findall(r"public\.ecr\.aws/(?!<)[A-Za-z0-9._-]+", text)
    assert not concrete, f"a concrete registry namespace is hardcoded: {concrete}"


def test_the_scripts_reference_check_accepts_exactly_what_taskdef_accepts(
    publish_shell: str, tmp_path: Path
) -> None:
    """A behavioural comparison of the two patterns, not a string compare.

    The script writes its check as a POSIX ERE and ``taskdef`` writes it in
    Python, so the two can never be compared literally. They can be compared on
    verdicts, which is what actually has to agree.
    """
    patterns = re.findall(r"grep -qE '(\^[^']*sha256[^']*\$)'", _script())
    assert len(patterns) == 1, f"expected one reference-shape check, found {len(patterns)}"
    ere = patterns[0]

    # The ERE reaches grep through a FILE rather than an argument. It contains a
    # colon, and Git Bash rewrites a colon-bearing argument on the way in -- it
    # reads `a:b` as a path list -- so passed as argv the pattern grep receives on
    # Windows is not the pattern the script wrote. Inside the script the ERE is a
    # literal in the shell source and is never an argument, so a file keeps the
    # comparison on the same text while taking that rewriting out of the path.
    #
    # Written with NO trailing newline on purpose: with `-f`, a blank line in the
    # file is an empty pattern, and an empty pattern matches everything.
    pattern_file = tmp_path / "reference-shape.ere"
    pattern_file.write_text(ere, encoding="utf-8", newline="\n")
    assert not pattern_file.read_text(encoding="utf-8").endswith("\n")

    digest = "b" * 64
    candidates = [
        f"registry.invalid/example/repo@sha256:{digest}",
        f"repo@sha256:{digest}",
        "registry.invalid/example/repo:latest",
        f"registry.invalid/example/repo@sha256:{'b' * 63}",
        f"registry.invalid/example/repo@sha256:{'B' * 64}",
        f"registry.invalid/example/repo@sha512:{digest}",
        f"has space@sha256:{digest}",
        f"two@at@sha256:{digest}",
        "",
    ]
    for candidate in candidates:
        # Run the grep through the resolved shell, so it is the one the SCRIPT
        # itself would get -- same PATH, same ERE dialect -- rather than whatever
        # a host happens to put in front of it. The candidate arrives on stdin, so
        # none of its characters is ever parsed as shell.
        shell = subprocess.run(
            [publish_shell, "-c", 'grep -qE -f "$1"', "probe", pattern_file.as_posix()],
            input=candidate,
            capture_output=True,
            timeout=REFUSAL_TIMEOUT_SECONDS,
            **UTF8_TEXT,
        )
        mine = shell.returncode == 0
        theirs = bool(_DIGEST_REF_RE.match(candidate))
        assert mine == theirs, (
            f"{candidate!r}: the script says {mine}, taskdef._DIGEST_REF_RE says "
            f"{theirs}{shell.stderr and ' -- ' + shell.stderr.strip()}"
        )


def _selection_block() -> str:
    """The script's repository-selection logic, from the loop to its refusal.

    The end marker is the heredoc's CLOSING ``EOF``, found AFTER the ``done <<EOF``
    line -- that line itself ends in ``EOF``, so searching for the first one
    truncates the block mid-heredoc and the loop then reads nothing. That
    silently turns both tests below into "the selection returned empty", which
    looks exactly like the defect they exist to catch.
    """
    text = _script()
    start = text.index('REFERENCE=""')
    heredoc = text.index("done <<EOF", start)
    close = text.index("\nEOF\n", heredoc) + len("\nEOF\n")
    tail = text.index("\n", text.index("Present:", close)) + 1
    return text[start:tail]


def test_the_digest_is_selected_by_repository_not_by_position(publish_shell: str) -> None:
    """``RepoDigests[0]`` can name a DIFFERENT repository.

    The list accumulates per image ID across pushes, so content already pushed to
    another repository on the same daemon leaves that repository's entry in the
    list -- possibly first. A shape check alone passes it, and the script then
    prints a reference naming a registry the task cannot pull from.

    This runs the script's ACTUAL selection block against a fabricated candidate
    list, rather than asserting on its text, so a rewrite that keeps the wording
    and loses the property still reds.
    """
    block = _selection_block()
    assert "RepoDigests" not in block, "the selection block should not re-read the daemon"

    wanted = "registry.invalid/example/kirocrew-crew-base"
    foreign = "registry.invalid/somebody-else/kirocrew-crew-base"
    mine = f"{wanted}@sha256:{'b' * 64}"
    theirs = f"{foreign}@sha256:{'c' * 64}"
    # Quoted by hand with a REAL newline: bash single-quoted strings span lines,
    # while a Python repr would embed a literal backslash-n that bash does not
    # interpret, collapsing both candidates onto one unmatchable line.
    assert "'" not in mine + theirs, "the fixture values must contain no single quote"

    # The foreign entry deliberately comes FIRST, which is the failing case.
    harness = f"""
set -euo pipefail
die() {{ echo "error: $*" >&2; exit 1; }}
REPOSITORY='{wanted}'
CANDIDATES='{theirs}
{mine}'
{block}
printf '%s' "${{REFERENCE}}"
"""
    result = _run_harness(publish_shell, harness)
    assert result.returncode == 0, result.stderr
    assert result.stdout == mine, (
        f"selected {result.stdout!r}; with {foreign} listed first, position-based "
        f"selection would have returned {theirs!r}"
    )


def test_a_push_that_names_no_matching_repository_is_refused(publish_shell: str) -> None:
    """No match must refuse, not fall back to whatever is present."""
    harness = f"""
set -euo pipefail
die() {{ echo "error: $*" >&2; exit 1; }}
REPOSITORY='registry.invalid/example/kirocrew-crew-base'
CANDIDATES='registry.invalid/somebody-else/kirocrew-crew-base@sha256:{'c' * 64}'
{_selection_block()}
printf 'REACHED-END'
"""
    result = _run_harness(publish_shell, harness)
    assert result.returncode != 0, f"expected a refusal, got: {result.stdout!r}"
    assert "none of the image's repository digests names" in result.stderr, result.stderr


@pytest.mark.parametrize(
    "repository",
    ["myuser/myrepo", "kirocrew-crew-base", "library/ubuntu"],
    ids=["hub-namespace", "bare-name", "official-style"],
)
def test_a_short_repository_name_is_refused_before_anything_is_built(
    repository: str, publish_shell: str
) -> None:
    """A short name is rewritten by Docker, so the digest read-back cannot match it.

    ``myuser/myrepo`` comes back out of ``RepoDigests`` in Docker's familiar form,
    which is not the string this script was handed. The exact-prefix match then
    finds nothing and the run dies AFTER a successful push -- reporting failure for
    work that landed.

    Refused rather than normalized, deliberately: matching Docker's rewriting means
    keeping a second copy of its reference-parsing rules here, and a second copy of
    somebody else's table drifts silently. The decided delivery is a public
    registry, whose references are always fully qualified, so the refusal costs
    nothing real.
    """
    result = _run(publish_shell, "--repository", repository)
    assert result.returncode != 0, f"{repository!r} must be refused"
    assert "must be fully qualified" in result.stderr, result.stderr
    assert "Building" not in result.stdout, "the refusal must come before any build"


@pytest.mark.parametrize(
    "repository",
    ["docker.io/acme/base", "index.docker.io/acme/base"],
    ids=["docker-io", "index-docker-io"],
)
def test_a_docker_hub_repository_is_refused_before_anything_is_built(
    repository: str, publish_shell: str
) -> None:
    """Fully qualified and rewritten anyway, which is the same defect by another name.

    ``docker.io`` and ``index.docker.io`` carry a dot, so the host guard's accept arm
    would take them. Docker's reference parser reads both as the official registry and
    drops the prefix, so ``RepoDigests`` reports ``acme/base@sha256:...`` -- not the
    string the script was handed. The exact-prefix match finds nothing and the run
    dies AFTER a successful push, reporting failure for work that landed.

    That is the outcome the fully-qualified guard exists to prevent, reached through a
    host that satisfies it, so the guard has to name these two or its own reasoning is
    only nearly true. Refused rather than normalized, for the reason the short-name
    test gives: a second copy of Docker's parsing rules drifts.
    """
    result = _run(publish_shell, "--repository", repository)
    assert result.returncode != 0, f"{repository!r} must be refused"
    assert "must not name Docker Hub" in result.stderr, result.stderr
    assert "Building" not in result.stdout + result.stderr, (
        "the refusal must come before any build; a push that lands under a rewritten "
        "name is exactly what this refusal is for"
    )


def test_the_qualified_forms_docker_recognises_are_all_accepted() -> None:
    """Checked on the guard's own arms rather than by running it.

    Running an ACCEPTED repository would start a real image build, so the accept
    side is read from the source: Docker's own test is a first segment carrying a
    dot or a port, or exactly ``localhost``.
    """
    text = _script()
    start = text.index('case "${HOST}" in')
    arms = text[start : text.index("esac", start)]
    for form in ("localhost", "*.*", "*:*"):
        assert form in arms, f"the guard does not accept {form}, which Docker treats as a host"
    # The refusal for a rewritten host must come BEFORE the arm that would accept it:
    # `docker.io` carries a dot, so `*.*` matches it and the first matching arm wins.
    assert arms.index("docker.io") < arms.index("*.*"), (
        "the Docker Hub refusal sits after the arm that accepts any dotted host, so it "
        "is unreachable and Hub is accepted"
    )


def test_the_tag_built_pushed_and_inspected_is_unique_per_invocation() -> None:
    """A shared tag can be moved between the push and the read-back.

    The digest is resolved by inspecting a TAG, so any name another process can
    re-point in that window reports that process's image instead -- silently, and
    the reference still passes every shape check. A per-invocation name closes it.
    """
    text = _script()
    assignments = re.findall(r"^readonly PUBLISH_TAG=(.*)$", text, flags=re.MULTILINE)
    assert len(assignments) == 1, f"expected one publish tag, found {assignments}"
    value = assignments[0]
    assert "$$" in value, f"the publish tag carries no pid, so it is not per-invocation: {value}"
    assert "date" in value, f"the publish tag carries no timestamp: {value}"
    assert "PUBLISH_NONCE" in value, (
        f"the publish tag carries no random nonce: {value}. Two containers sharing one "
        "daemon have independent pid spaces, so time plus pid can agree by scheduling"
    )

    # Build, push and inspect must all name that tag, and none of them the shared one.
    for verb, needle in (
        ("build", 'build_crew_base_image.sh" --tag "${REPOSITORY}:'),
        ("push", 'docker push "${REPOSITORY}:'),
        ("inspect", 'docker image inspect "${REPOSITORY}:'),
    ):
        index = text.index(needle)
        following = text[index + len(needle) : index + len(needle) + 20]
        assert following.startswith(
            "${PUBLISH_TAG}"
        ), f"the {verb} step uses {following!r} rather than the per-invocation tag"


def test_no_shared_tag_is_written_at_all() -> None:
    """Not ``latest``, not any other fixed name.

    A movable name is what every failure mode in this script came back to: used for
    the build and the read-back it lets another run's image be reported, and moved
    afterwards a concurrent publish can still point it at the wrong image.
    Serializing that move needs a lock across machines sharing one registry, which
    is a lot of machinery for a name nothing reads -- a task definition is
    registered against the digest. So the convenience is absent by design, and this
    asserts it stays absent.
    """
    text = _script()
    assert "docker tag" not in text, "the script re-tags an image, reintroducing a movable name"
    # Every push must name this invocation's own tag. A push of anything else is a
    # shared name by another spelling.
    pushes = re.findall(r"^docker push \"([^\"]+)\"", text, flags=re.MULTILINE)
    assert pushes, "the script never pushes, so it cannot publish"
    for target in pushes:
        assert (
            target == "${REPOSITORY}:${PUBLISH_TAG}"
        ), f"a push names {target!r} rather than this invocation's own tag"


def test_the_dry_run_stops_before_the_push() -> None:
    """Ordering, because a dry run that reaches the push is not a dry run."""
    lines = _script().splitlines()
    exits = [i for i, line in enumerate(lines) if line.strip() == "exit 0"]
    pushes = [i for i, line in enumerate(lines) if line.strip().startswith("docker push")]
    assert pushes, "the script never pushes, so it cannot publish"
    assert exits, "the dry run never exits"
    assert min(exits) < min(pushes), "the dry-run exit comes after the push"


def test_the_published_digest_says_it_names_the_base_and_stdout_stays_one_line() -> None:
    """What is handed over is the BASE, and nothing may share stdout with it.

    A task registered against this digest alone stops at ``install_bundle``'s
    fail-closed check (``bundle.py``) before the backend starts, and the bundle
    directory cannot be redirected at runtime -- ``SMC_BUNDLE_DIR`` is in
    ``runtask.py``'s ``REFUSED_ENV``. So the reference is the input to composing a
    crew, not a launchable artifact, and saying so belongs where it is printed
    rather than only in a pull request.

    It goes to STDERR because stdout is a machine interface: exactly one line, the
    reference, so a caller can capture it without parsing prose out of it. Both
    halves are asserted, since a caveat moved to stdout breaks that capture and a
    caveat deleted leaves the digest looking launchable.

    The scan covers the WHOLE script, not the part after the push. Reading only the
    tail is what let progress lines sit on stdout from the first step onwards while
    this file reported the property as held.
    """
    text = _script()
    tail = text[text.index('step "Published"') :]
    caveat = [
        line for line in tail.splitlines() if "BASE image" in line and line.startswith("echo")
    ]
    assert caveat, "the published digest is handed over with no note that it names the base"
    for line in caveat:
        assert line.rstrip().endswith(">&2"), f"the caveat goes to stdout, not stderr: {line}"

    # Every echo in the file must be the reference or redirected.
    assert _echoes_that_reach_stdout(text) == [], (
        "something other than the reference reaches stdout: " f"{_echoes_that_reach_stdout(text)}"
    )


def _echoes_that_reach_stdout(text: str) -> list[str]:
    """Every ``echo`` in the script that is neither redirected nor the reference.

    The search runs anywhere on a line rather than at its start, which is what
    reaches the one inside a one-line helper -- and ``step`` is exactly that, so a
    scan anchored at the start of the line reads the whole success path as clean
    while every step lands on stdout.

    Splitting on a semicolon has to ignore one inside a quoted string, or a caveat
    line containing prose punctuation is cut in half and its redirect goes missing.
    """
    loose: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        commands: list[str] = []
        current: list[str] = []
        quoted = False
        for character in line:
            if character == '"':
                quoted = not quoted
            if character == ";" and not quoted:
                commands.append("".join(current))
                current = []
                continue
            current.append(character)
        commands.append("".join(current))
        for command in commands:
            found = re.search(r"\becho\b.*", command)
            if found is None:
                continue
            # A trailing brace closes a one-line function body, not the echo.
            written = found.group(0).strip().removesuffix("}").strip()
            # `endswith('>&2')` alone would pass a caveat printed on BOTH streams.
            if written != 'echo "${REFERENCE}"' and not written.endswith(">&2"):
                loose.append(written)
    return loose


def test_the_commands_that_write_their_own_output_are_folded_into_stderr() -> None:
    """``echo`` is not the only thing that can reach stdout.

    The producer prints its build, ``docker push`` prints its layers, and either
    one lands on stdout unless it is redirected -- so a caller's ``$(...)`` gets
    them joined to the reference. ``docker image inspect`` needs no redirect: it is
    read through a command substitution, which is the capture.
    """
    text = _script()
    invocations = [
        line.strip()
        for line in text.splitlines()
        if not line.lstrip().startswith("#")
        and (line.lstrip().startswith("docker push") or f'/{PRODUCER.name}"' in line)
    ]
    assert len(invocations) == 2, f"expected the producer and one push, found: {invocations}"
    for line in invocations:
        assert line.endswith(">&2"), f"this writes to stdout: {line}"


#: A digest the stub registry reports for the repository under test, and one for a
#: DIFFERENT repository placed ahead of it -- so a run that takes ``RepoDigests``
#: by position prints the wrong registry and this file says so.
STUB_DIGEST = "sha256:" + "ab" * 32
STUB_DECOY = f"registry.invalid/other/kirocrew-crew-base@{STUB_DIGEST}"
STUB_REFERENCE = f"{SAMPLE_REPOSITORY}@{STUB_DIGEST}"

_STUB_PRODUCER = """#!/bin/sh
echo "producer: staging the wheel"
echo "producer: built $*"
"""

_STUB_DOCKER = """#!/bin/sh
if [ "$1" = "push" ]; then
  echo "The push refers to repository [$2]"
  echo "0123456789ab: Pushed"
  exit 0
fi
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
  if [ -n "${STUB_NO_DIGESTS}" ]; then
    exit 0
  fi
  printf '%s\\n' "${STUB_DECOY}" "${STUB_REFERENCE}"
  exit 0
fi
echo "stub docker: unexpected invocation: $*" >&2
exit 64
"""


def _publish_offline(
    tmp_path: Path,
    shell: str,
    *args: str,
    no_digests: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the script to COMPLETION with the two things it shells out to stubbed.

    The script is copied so its ``SCRIPT_DIR`` lands beside a stub producer -- it
    calls the producer by that path, so a PATH entry could not stand in for it. The
    copy is byte-for-byte, so what runs is the shipped script.

    Nothing here builds or pushes an image: the producer is a two-line shell script
    and ``docker`` is another, so the same short cap a refusal gets is enough.

    ``no_digests`` makes the stub registry report an image with no repository
    digests, which is what a push that did not land looks like -- a condition no real
    registry produces on demand, and the input to a fail-closed path.
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    copy = scripts / SCRIPT.name
    copy.write_text(_script(), encoding="utf-8")
    producer = scripts / PRODUCER.name
    producer.write_text(_STUB_PRODUCER, encoding="utf-8")
    producer.chmod(0o755)
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    docker = stub_bin / "docker"
    docker.write_text(_STUB_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    environment = dict(os.environ)
    environment["PATH"] = os.pathsep.join([str(stub_bin), environment.get("PATH", "")])
    environment["STUB_DECOY"] = STUB_DECOY
    environment["STUB_REFERENCE"] = STUB_REFERENCE
    environment["STUB_NO_DIGESTS"] = "1" if no_digests else ""
    return subprocess.run(
        [shell, copy.as_posix(), *args],
        capture_output=True,
        cwd=tmp_path,
        env=environment,
        timeout=REFUSAL_TIMEOUT_SECONDS,
        **UTF8_TEXT,
    )


def test_a_successful_publish_puts_the_reference_and_nothing_else_on_stdout(
    tmp_path: Path, publish_shell: str
) -> None:
    """Captured the way the caller captures it, which is the only reading that counts.

    Every text property above can hold while the run still prints progress on
    stdout, because a property read off the source is a claim about the lines
    someone thought to look at. This one runs the whole success path and compares
    stdout to the reference exactly, so a progress line anywhere in it fails here.

    The stub reports a DIFFERENT repository's digest first, so the reference on
    stdout is also the evidence the selection matches by repository rather than
    taking position zero.
    """
    done = _publish_offline(tmp_path, publish_shell, "--repository", SAMPLE_REPOSITORY)
    assert done.returncode == 0, done.stderr
    assert (
        done.stdout == f"{STUB_REFERENCE}\n"
    ), f"stdout is not just the reference: {done.stdout!r}"
    assert _DIGEST_REF_RE.fullmatch(done.stdout.strip()), (
        "the one line on stdout is not a reference taskdef would accept: "
        f"{done.stdout.strip()!r}"
    )
    # The progress and the caveat are not deleted, only moved.
    assert "==> Published" in done.stderr, "the progress went missing rather than to stderr"
    assert "BASE image" in done.stderr, "the caveat went missing rather than to stderr"


def test_a_dry_run_puts_nothing_at_all_on_stdout(tmp_path: Path, publish_shell: str) -> None:
    """A dry run has no reference to hand over, so its stdout is empty.

    Anything at all there is a line a caller would capture and take for a
    reference.
    """
    done = _publish_offline(tmp_path, publish_shell, "--repository", SAMPLE_REPOSITORY, "--dry-run")
    assert done.returncode == 0, done.stderr
    assert done.stdout == "", f"a dry run wrote to stdout: {done.stdout!r}"
    assert "Dry run" in done.stderr, "the dry run reported nothing on either stream"
    assert STUB_DIGEST not in done.stderr, "a dry run resolved a digest"


def test_a_push_that_left_no_repository_digest_is_reported_as_a_failure(
    tmp_path: Path, publish_shell: str
) -> None:
    """``RepoDigests`` empty after a push means the push did not land.

    The check for it reads like a formality, so it is the kind of line a cleanup
    removes. Without it the selection loop finds nothing, and what the caller gets
    is the later refusal about no matching repository -- a message that sends
    whoever reads it looking at the repository name instead of at the push.
    """
    done = _publish_offline(
        tmp_path, publish_shell, "--repository", SAMPLE_REPOSITORY, no_digests=True
    )
    assert done.returncode != 0, "an image with no repository digest published anyway"
    assert done.stdout == "", f"a failed publish wrote to stdout: {done.stdout!r}"
    assert (
        "the push did not land" in done.stderr
    ), f"the failure does not name the push as the cause: {done.stderr!r}"


def test_a_nonce_that_could_not_be_read_stops_the_run_before_it_builds() -> None:
    """An empty nonce is refused rather than folded into the tag.

    The tag is unique because of the nonce, so a nonce that silently came back empty
    leaves a tag made of time and pid -- the collision two containers on one daemon
    can reach, and the one this name exists to prevent.

    Read from the text rather than produced. Producing it means replacing ``od``, and
    a stub cannot reliably win that name: Git Bash carries its own ``od`` in
    ``/usr/bin`` and puts it ahead of an inherited PATH entry, so on Windows the real
    one answers and the refusal never fires. Measured on the Windows shard. A stub
    ``docker`` is not shadowed that way -- Git Bash ships no ``docker`` -- which is
    why the three offline runs above stay behavioural and this one does not.
    """
    lines = [line.strip() for line in _script().splitlines()]
    read = next(
        (index for index, line in enumerate(lines) if line.startswith("readonly PUBLISH_NONCE=")),
        None,
    )
    assert read is not None, "the publish tag carries no nonce, so it is not unique per run"
    guard = lines[read + 1]
    assert guard.startswith('[ -n "${PUBLISH_NONCE}" ]') and "die" in guard, (
        "the line after the nonce read does not refuse an empty one, so an unreadable "
        f"entropy source leaves a tag of time and pid: {guard!r}"
    )
    built = next((index for index, line in enumerate(lines) if line.startswith("step ")), None)
    assert built is not None and read < built, "the nonce is read after the build starts"


#: The producer stages its wheel into ONE fixed directory in the checkout, so the
#: unique tag above does not make two publishes independent. The lock is scoped to
#: exactly what is shared -- this checkout -- and sits beside that directory.
PUBLISH_STAGING_RELATIVE = Path("src/kiro_crew/apps/builtins/aws_control/crew/runtime")
PUBLISH_LOCK_NAME = ".publish-crew-base-image.lock"


def test_a_second_publish_in_the_same_checkout_is_refused_before_it_builds(
    tmp_path: Path, publish_shell: str
) -> None:
    """Two publishes from one checkout would race on the producer's wheel staging.

    ``build_crew_base_image.sh`` empties ``runtime/vendor`` on entry and again in its
    EXIT trap, and copies its own wheel in before the build reads it through the bind
    mount. So the second run's ``rm`` can delete the first run's staged wheel, and the
    first build fails on a missing wheel with nothing pointing at the run that took
    it. The unique tag does not help: it isolates the image name, not the directory.

    Held here as REFUSAL rather than queueing, and the refusal has to land before the
    producer is invoked -- after it, the wheel is already gone.
    """
    staging = tmp_path / PUBLISH_STAGING_RELATIVE
    staging.mkdir(parents=True)
    (staging / PUBLISH_LOCK_NAME).mkdir()

    done = _publish_offline(tmp_path, publish_shell, "--repository", SAMPLE_REPOSITORY)

    assert done.returncode != 0, "a second concurrent publish ran anyway"
    assert done.stdout == "", f"a refused publish wrote to stdout: {done.stdout!r}"
    assert "another publish is already building" in done.stderr, (
        "the refusal does not say a concurrent publish is the cause: " f"{done.stderr!r}"
    )
    assert PUBLISH_LOCK_NAME in done.stderr, (
        "the refusal does not name the lock, so a stale one from a killed run leaves "
        f"no way to clear it: {done.stderr!r}"
    )
    assert "producer:" not in done.stderr, (
        "the producer ran despite the refusal, so the wheel staging was already "
        f"entered: {done.stderr!r}"
    )


@pytest.mark.parametrize("no_digests", [False, True], ids=["success", "failure"])
def test_the_lock_is_released_however_the_run_ends(
    tmp_path: Path, publish_shell: str, no_digests: bool
) -> None:
    """A lock a finished run keeps is a checkout no later publish can use.

    Both endings are exercised, because the failing one exits through ``die`` rather
    than off the end of the script -- a release written as a final line would hold on
    the success path and leak on every refusal after the lock is taken.

    The staging directory is asserted to EXIST, so this cannot pass by looking for the
    lock somewhere the script never puts one.
    """
    done = _publish_offline(
        tmp_path, publish_shell, "--repository", SAMPLE_REPOSITORY, no_digests=no_digests
    )
    assert done.returncode == (1 if no_digests else 0), done.stderr

    staging = tmp_path / PUBLISH_STAGING_RELATIVE
    assert staging.is_dir(), (
        "the run left no staging directory, so this test is looking in a place the "
        f"script never used: {staging}"
    )
    assert not (staging / PUBLISH_LOCK_NAME).exists(), (
        "the run kept its lock, so the next publish in this checkout is refused " "forever"
    )


def test_the_lock_is_taken_before_the_producer_is_invoked() -> None:
    """Ordering, because a lock taken after the build guards nothing.

    The two runs collide inside the producer. A guard placed after the invocation
    would let both enter the staging directory and then serialize what is already
    broken, and both orderings look equally deliberate when read out of sequence.

    The trap is checked too: armed before the acquisition, a refused run would delete
    the lock the run it lost to is still holding.
    """
    lines = [line.strip() for line in _script().splitlines()]

    acquire = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith('if ! mkdir "${PUBLISH_LOCK}"')
        ),
        None,
    )
    assert acquire is not None, (
        "nothing takes an exclusive lock, so two publishes in one checkout race on the "
        "producer's wheel staging"
    )
    invoke = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith(f'"${{SCRIPT_DIR}}/{PRODUCER.name}"')
        ),
        None,
    )
    assert invoke is not None, "the producer is no longer invoked by path"
    assert acquire < invoke, (
        f"the lock is taken at line {acquire + 1}, after the producer is invoked at "
        f"line {invoke + 1}, so both runs are already in the staging directory"
    )

    release = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith("trap ") and "PUBLISH_LOCK" in line
        ),
        None,
    )
    assert release is not None, "the lock is never released, so one run poisons the checkout"
    assert acquire < release, (
        f"the release trap is armed at line {release + 1}, before the lock is acquired "
        f"at line {acquire + 1}, so a refused run deletes the holder's lock"
    )

    assert str(PUBLISH_STAGING_RELATIVE.as_posix()) in _script(), (
        "the lock is not scoped to the directory it protects, so it either guards the "
        "wrong thing or is shared by checkouts that do not collide"
    )


def test_publishing_is_a_caller_of_the_producer_not_a_step_inside_it() -> None:
    """The producer keeps its verification in the path, and keeps no credential.

    Folding the push into the producer would put a credential-needing step in the
    one place the fork lane runs.
    """
    assert f'"${{SCRIPT_DIR}}/{PRODUCER.name}"' in _script(), (
        "publishing must invoke the documented producer, so a published image cannot "
        "skip the wheel staging and architecture cross-check a local build gets"
    )
    assert "docker push" not in PRODUCER.read_text(
        encoding="utf-8"
    ), "the producer must not push: it is the script the fork lane runs"


def test_the_fork_lane_needs_no_credential_and_does_not_publish() -> None:
    """The property that keeps the build lane runnable on a fork.

    Its loopback registry exists so the lane needs no secret. A publish step added
    here would break it on a fork, or skip -- and a skipped lane reads exactly
    like a passing one.
    """
    lane = FORK_LANE.read_text(encoding="utf-8")
    assert "secrets." not in lane, (
        "the crew image build lane must name no secret; its loopback registry exists "
        "precisely so a fork pull request can run it"
    )
    assert (
        SCRIPT.name not in lane
    ), f"{SCRIPT.name} must not be invoked by the fork-runnable build lane"


if __name__ == "__main__":  # pragma: no cover - convenience for a manual run
    sys.exit(pytest.main([__file__, "-q"]))
