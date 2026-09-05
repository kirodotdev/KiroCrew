"""Packaging guard for the AWS Control crew deploy payload.

`setup.cfg [options.package_data]` selects builtin-app assets by FIXED
subdirectory name: `ui/`, `lib/`, `backend/`, `agents/`, `inject/`, `skills/`,
`scripts/`, `prompts/`, and `*.md` at the app root. None of those names matches
`crew/`, and `*` does not cross a path separator, so `apps/builtins/*/scripts/**/*`
does NOT reach `aws_control/crew/scripts/`. A new top-level directory inside a
builtin app is therefore NOT SHIPPED unless a glob names it.

That failure is silent in the worst way. Everything under `crew/` is read from
disk at the moment someone deploys a crew: the driver, the two CloudFormation
templates, the container build context. From a source checkout every path
resolves, so the whole feature tests green locally and on CI, and on every pip
and DMG install the files are simply absent. Nothing raises at import time
because nothing imports them; the deploy just cannot find its own driver.

This is the same accident the `apple_speech/*.swift` comment in that file
records: the `apple` speech provider reported unavailable on every installed
copy while working perfectly from a checkout.

So `crew/**` ships through one glob, and this file is what stops that glob from
being deleted by an ordinary-looking edit. A glob with no test is a line nobody
would notice losing.

Two lanes are pinned separately, because each selects these files by a different
mechanism and can drop them independently: the wheel's `package_data`, and the
sdist's `MANIFEST.in`. `python -m build` builds the wheel FROM the sdist, so a
file MANIFEST.in omits is absent from the wheel whatever package_data says --
and MANIFEST.in's `recursive-include src/kiro_crew/apps` takes only `*.json` and
`*.md`, which is none of this tree's `.sh`, `.yaml`, `.txt` or extensionless
Dockerfiles. One lane passing says nothing about the other; that is the lesson
`test_vendored_llama_payload.py` records after a published wheel shipped a
vendored library the sdist rules had stripped.

Modelled on `test_vendored_llama_payload.py::test_package_data_declares_the_libs_explicitly`,
and for the same reason: these tests MODEL the packaging rules rather than
building a real wheel. Shelling out to the build backend skips wherever
`build`/`setuptools` is missing (this project's own dev venv included), and a
skip scores as a pass, so the guard would be absent exactly where it matters.
"""

from __future__ import annotations

import configparser
import fnmatch
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PKG_ROOT = _REPO_ROOT / "src" / "kiro_crew"
_CREW_DIR = _PKG_ROOT / "apps" / "builtins" / "aws_control" / "crew"

# The one glob this migration adds. Spelled here so a rename in setup.cfg has to
# be a deliberate edit in two places rather than a silent deletion in one.
_CREW_GLOB = "apps/builtins/*/crew/**/*"

# The sdist's own rule for the same tree. MANIFEST.in takes a literal directory,
# so this one names the app where package_data can wildcard it.
_MANIFEST_RULE = "recursive-include src/kiro_crew/apps/builtins/aws_control/crew *"

# Directives MANIFEST.in may use that this file's rule model understands. A new
# one can REMOVE payload files while the ordering test below still passes, which
# is worse than no test, so `test_manifest_directives_are_all_modelled` fails
# until it is added here.
_MODELLED_DIRECTIVES = frozenset(
    {"include", "recursive-include", "recursive-exclude", "global-exclude", "prune"}
)

# Files a deploy cannot run without, one per mechanism that reads them: the
# driver (bash), the templates (CloudFormation), the gate suite and its guards
# (bash, sourcing the driver), the container build context (docker), and the two
# contracts (read by humans, but shipped because they document live invariants).
# `fixtures/bundle-ok/manifest.json` is deliberately the deepest path: it is what
# proves the glob's `**` recursion actually reaches the bottom of the tree.
_MUST_SHIP = (
    "scripts/smc-deploy.sh",
    "scripts/tests/run_gate_tests.sh",
    "scripts/tests/check_param_seam.sh",
    "scripts/tests/check_no_placeholder_commands.sh",
    "scripts/tests/fixtures/bundle-ok/manifest.json",
    "templates/base.yaml",
    "templates/crew.yaml",
    "runtime/Dockerfile",
    "runtime/Dockerfile.crew",
    "runtime/container/requirements.txt",
    "EPHEMERAL-CONTRACT.md",
    "PACKAGING-CONTRACT.md",
)


def _package_data_globs() -> list[str]:
    """The `kiro_crew` entries of `[options.package_data]`, as written."""
    parser = configparser.ConfigParser()
    parser.read(_REPO_ROOT / "setup.cfg", encoding="utf-8")
    raw = parser["options.package_data"]["kiro_crew"]
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _manifest_lines() -> list[str]:
    """MANIFEST.in's directive lines, in file order, comments and blanks dropped."""
    text = (_REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _manifest_rules() -> list[tuple[str, str, str]]:
    """MANIFEST.in as `(kind, directory, filename-pattern)` triples in file order.

    `recursive-include DIR PAT` and `recursive-exclude DIR PAT` match PAT against
    the basename at any depth under DIR; `global-exclude PAT` matches the basename
    tree-wide; `prune DIR` drops everything under DIR. `include` takes literal
    root-relative paths and is modelled as a one-file include so an unmodelled
    directive cannot silently become a no-op.
    """
    rules: list[tuple[str, str, str]] = []
    for raw in _manifest_lines():
        fields = raw.split()
        directive, args = fields[0], fields[1:]
        if directive == "global-exclude":
            rules += [("exclude", "", pat) for pat in args]
        elif directive == "prune" and args:
            rules.append(("exclude", args[0].rstrip("/"), "*"))
        elif directive == "include":
            for pat in args:
                directory, _, name = pat.rpartition("/")
                rules.append(("include", directory, name))
        elif directive in ("recursive-include", "recursive-exclude") and len(args) > 1:
            kind = "include" if directive == "recursive-include" else "exclude"
            rules += [(kind, args[0].rstrip("/"), pat) for pat in args[1:]]
    return rules


def _payload_files() -> list[Path]:
    """Every real file under `crew/`, which is what both lanes have to carry."""
    return sorted(
        p
        for p in _CREW_DIR.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    )


def test_package_data_declares_the_crew_payload_directory() -> None:
    """`crew/` is not one of the fixed names, so it needs its own glob."""
    assert _CREW_GLOB in _package_data_globs(), (
        "setup.cfg [options.package_data] does not ship "
        f"{_CREW_GLOB!r}. Without it the crew deploy driver, its templates and "
        "the container build context are absent from every pip and DMG "
        "install, while a source checkout keeps working."
    )


def test_every_crew_payload_file_is_matched_by_a_package_data_glob() -> None:
    """Model the globs over the real tree: every file under `crew/` must match one.

    Asserting the whole tree rather than a hand-list is what catches the NEXT
    subdirectory someone adds under `crew/` -- the failure mode is a path the
    globs do not reach, not a specific filename.
    """
    globs = _package_data_globs()
    shipped: set[Path] = set()
    for pattern in globs:
        shipped.update(p for p in _PKG_ROOT.glob(pattern) if p.is_file())

    payload = {
        p
        for p in _CREW_DIR.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    }
    assert payload, f"no crew payload found under {_CREW_DIR}"

    missed = sorted(p.relative_to(_PKG_ROOT).as_posix() for p in payload - shipped)
    assert not missed, (
        "these crew payload files match no [options.package_data] glob, so they "
        f"are missing from every installed copy: {missed}"
    )


def test_the_crew_payload_carries_every_asset_a_deploy_reads() -> None:
    """A glob over an empty directory ships nothing and still passes.

    So pin the files themselves. Each entry is read at deploy time by a
    different mechanism, and each one going absent breaks the deploy at a
    different, unhelpful place.
    """
    for rel in _MUST_SHIP:
        assert (_CREW_DIR / rel).is_file(), f"crew payload is missing {rel}"


def test_the_manifest_rules_keep_every_crew_payload_file() -> None:
    """Evaluate MANIFEST.in's rules over the real payload, later rules winning.

    This is the ordering check, done by EVALUATION rather than by comparing line
    numbers against a hand-list of excludes. A hand-list only knows the excludes
    someone remembered to write down: with one, appending `global-exclude *.sh`
    below the re-include stripped the driver and the entire gate suite out of the
    sdist and this file stayed green. Evaluating the rules catches any remover,
    whatever its pattern and wherever it is added.
    """
    rules = _manifest_rules()
    payload = [p.relative_to(_REPO_ROOT).as_posix() for p in _payload_files()]
    assert payload, f"no crew payload found under {_CREW_DIR}"

    dropped = []
    for rel in payload:
        directory, _, name = rel.rpartition("/")
        shipped = False
        for kind, rule_dir, pattern in rules:
            under = not rule_dir or directory == rule_dir or directory.startswith(rule_dir + "/")
            if under and fnmatch.fnmatch(name, pattern):
                shipped = kind == "include"
        if not shipped:
            dropped.append(rel)

    assert not dropped, (
        "MANIFEST.in's rules exclude these crew payload files from the sdist, and "
        "the wheel is built FROM the sdist, so they reach no installed copy: "
        f"{dropped}"
    )


def test_manifest_reincludes_the_crew_payload_explicitly() -> None:
    """Name the rule, so removing it is a deliberate edit and not a tidy-up.

    `recursive-include src/kiro_crew/apps` further up takes only `*.json` and
    `*.md`, which is none of this tree's `.sh`, `.yaml`, `.txt` or extensionless
    Dockerfiles. The evaluation above also catches this line going missing; this
    one says WHICH line, which is the difference between a readable failure and a
    list of 78 paths.
    """
    assert _MANIFEST_RULE in _manifest_lines(), (
        f"MANIFEST.in does not carry {_MANIFEST_RULE!r}, so the sdist -- and any "
        "wheel built from it -- omits the crew deploy payload."
    )


def test_manifest_directives_are_all_modelled() -> None:
    """The evaluation is only trustworthy while it understands every directive.

    An unmodelled directive that removes files makes the evaluation above pass
    while the real sdist drops them, which is the one outcome worse than having no
    test at all.
    """
    used = {line.split()[0] for line in _manifest_lines()}
    assert (
        used <= _MODELLED_DIRECTIVES
    ), f"unmodelled MANIFEST.in directives: {sorted(used - _MODELLED_DIRECTIVES)}"


def test_the_front_process_test_deps_are_pinned_to_the_image_versions() -> None:
    """The suite must run the FastAPI/uvicorn the container actually installs.

    `crew/runtime/container/requirements.txt` is what goes INTO the image, and the
    front process's tests are declared as dev dependencies so they run at all
    (without them five files fail to import and CI stops on a collection error).
    Those two lists pinning different versions is the quiet failure: the suite
    then proves the front process works on a framework the container does not
    have, which is worse than not running it, because it reads as coverage.

    Both dev declarations are checked. `[dependency-groups] dev` in pyproject.toml
    is what CI installs (`uv pip install --group dev`); setup.cfg's `[dev]` extra
    is what `make build` uses. A pin added to one and not the other makes a local
    run disagree with CI.
    """
    reqs = (_CREW_DIR / "runtime" / "container" / "requirements.txt").read_text(encoding="utf-8")
    image_pins = {
        line.split("==")[0]: line.strip()
        for line in reqs.splitlines()
        if "==" in line and not line.strip().startswith("#")
    }

    setup_cfg = (_REPO_ROOT / "setup.cfg").read_text(encoding="utf-8")
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    for name in ("fastapi", "uvicorn"):
        pin = image_pins.get(name)
        assert pin, f"{name} is no longer pinned in the container's requirements.txt"
        assert pin in setup_cfg, (
            f"setup.cfg's [dev] extra does not pin {pin!r}, so `make build` runs the "
            "front process tests against a version the image does not install"
        )
        assert f'"{pin}"' in pyproject, (
            f"pyproject.toml's dev dependency group does not pin {pin!r}, so CI runs "
            "the front process tests against a version the image does not install"
        )


def test_no_fixed_name_glob_already_covered_the_crew_directory() -> None:
    """Document WHY a new glob was needed, so nobody removes it as redundant.

    `apps/builtins/*/scripts/**/*` looks like it covers
    `aws_control/crew/scripts/`. It does not: `*` does not cross a path
    separator. If setuptools ever changed that, this test fails and the extra
    glob can go -- deliberately, with the reason on the diff.
    """
    others = [g for g in _package_data_globs() if g != _CREW_GLOB]
    covered: set[Path] = set()
    for pattern in others:
        covered.update(p for p in _PKG_ROOT.glob(pattern) if p.is_file())

    inside_crew = sorted(
        p.relative_to(_PKG_ROOT).as_posix() for p in covered if _CREW_DIR in p.parents
    )
    assert not inside_crew, (
        "a fixed-name glob now reaches inside crew/, so the dedicated glob may "
        f"be redundant: {inside_crew}"
    )
