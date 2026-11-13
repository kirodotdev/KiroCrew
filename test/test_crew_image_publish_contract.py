"""The crew-image publish path must stay usable, and must stay out of the fork lane.

``cloud/fargate/taskdef.py`` requires ``<repository>@sha256:<64 hex>``, which exists only
after a push; the aws-control app's ``crew/scripts/publish_crew_base_image.sh`` makes it.
Pinned: the repository has no default, the reference matches ``taskdef``'s pattern at
the push, and the fork lane names no secret and never calls this script.

Offline: runs that execute the script exit in argument handling or use stubs, bounded by
``REFUSAL_TIMEOUT_SECONDS`` so a removed guard cannot fall into a real build. Nothing is
resolved at import, which runs before the per-test environment scrub.
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
SCRIPT = ROOT / "src/kiro_crew/apps/builtins/aws_control/crew/scripts/publish_crew_base_image.sh"
PRODUCER = ROOT / "src/kiro_crew/apps/builtins/aws_control/crew/scripts/build_crew_base_image.sh"
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

    A path naming bash is kept: on Windows a lookup by name finds the WSL launcher
    ``C:\\Windows\\System32\\bash.exe``, which runs nothing without a distro.

    On POSIX the fixture gives ``sh``, but the script needs bash (``BASH_SOURCE``,
    ``pipefail``). No bash at all fails rather than skips.
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

    Every non-interactive bash sources ``BASH_ENV`` first, and import runs before the
    scrub, so the import must start no shell at all.
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

    The only allowed skip is the shared ``posix_test_shell`` one; a local skip would
    most likely fire on Windows, the shard these tests matter most on.
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

    ``repo:1.2`` may be a tag or a registry port, so trimming could publish elsewhere.
    The message is matched per case: the tag guard also refuses a digest (its last
    segment has a colon), so a loose match would pass with the digest guard deleted.
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

    The end is the closing ``EOF`` after ``done <<EOF``, which itself ends in ``EOF``;
    stopping there would make the selection return empty, like the defect itself.
    """
    text = _script()
    start = text.index('REFERENCE=""')
    heredoc = text.index("done <<EOF", start)
    close = text.index("\nEOF\n", heredoc) + len("\nEOF\n")
    tail = text.index("\n", text.index("Present:", close)) + 1
    return text[start:tail]


def test_the_digest_is_selected_by_repository_not_by_position(publish_shell: str) -> None:
    """``RepoDigests[0]`` can name a DIFFERENT repository.

    The list collects every repository the image was pushed to on that daemon.
    Runs the script's actual selection block against a fabricated list.
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

    Otherwise the exact-prefix match fails after a successful push. Refused, not
    normalized: a copy of Docker's parsing rules would drift.
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

    ``docker.io`` and ``index.docker.io`` pass the dot check, but Docker drops the prefix
    in ``RepoDigests``, so the match fails after a successful push.
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

    A movable name lets a concurrent run's image be reported or retagged, and nothing
    reads it: task definitions use the digest.
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

    The base alone stops at ``install_bundle``'s fail-closed check (``bundle.py``), and
    ``SMC_BUNDLE_DIR`` is in ``runtask.py``'s ``REFUSED_ENV``, so it is not launchable.

    The caveat goes to stderr: stdout is exactly one line, the reference. The stdout
    scan covers the whole script, since progress lines can appear from the first step.
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

    Searched anywhere on a line, to reach one-line helpers such as ``step``. Semicolons
    inside quotes are not split on, or a caveat's redirect would be cut off.
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

    A byte-for-byte copy, so ``SCRIPT_DIR`` sits beside the stub producer (called by
    path, not PATH). ``no_digests`` mimics a push that did not land.
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

    Stdout must equal the reference exactly. The stub lists another repository's digest
    first, so this also pins selection by repository, not position.
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

    Without the nonce, two containers on one daemon can collide on time and pid.
    Read from the text: Git Bash puts its own ``od`` ahead of PATH, so a stub cannot win.
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
#: exactly what is shared -- this checkout -- and sits beside that directory. Relative
#: to the crew tree the script's ``scripts/`` sits in, which is ``tmp_path`` for a copy.
PUBLISH_STAGING_RELATIVE = Path("runtime")
PUBLISH_LOCK_NAME = ".publish-crew-base-image.lock"


def test_a_second_publish_in_the_same_checkout_is_refused_before_it_builds(
    tmp_path: Path, publish_shell: str
) -> None:
    """Two publishes from one checkout would race on the producer's wheel staging.

    ``build_crew_base_image.sh`` empties ``runtime/vendor`` on entry and exit, so a second
    run can delete the first run's wheel. The refusal must come before the producer runs.
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

    The failing run exits through ``die``, so a release on the last line would leak.
    The staging directory must exist, or the lock check would look in the wrong place.
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

    The runs collide inside the producer. The trap must be armed after acquisition, or a
    refused run would delete the winner's lock.
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
