"""The crew image lane must cover the recipe CLASS, not two named recipes.

``crew-image-build.yml`` builds the image for real; these tests pin what a build cannot
check about itself, statically and with no Docker:

* every refusal fires, so an unclassifiable recipe is never silently skipped;
* every recipe in the tree has a derivable role;
* the probe bundle's digest equals the one the image computes;
* every producer the recipes cite is inside the workflow's ``paths`` filter;
* every registry the lane writes to is loopback, which is why it needs no credentials.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src/kiro_crew/apps/builtins/aws_control/crew/scripts/crew_image_build_plan.py"
WORKFLOW = ROOT / ".github" / "workflows" / "crew-image-build.yml"

#: Windows reports success from ``os.access(path, os.X_OK)`` for any file that
#: exists, whatever ``chmod`` was called with, so a non-executable fixture cannot
#: be expressed there and the refusal it drives cannot be observed. The
#: requirement itself binds on the Linux runner that invokes a producer.
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows has no execute bit, so a non-executable file cannot be expressed",
)

_SPEC = importlib.util.spec_from_file_location("crew_image_build_plan", SCRIPT)
assert _SPEC and _SPEC.loader
plan_mod = importlib.util.module_from_spec(_SPEC)
# Registered BEFORE execution: `@dataclass` resolves its own module out of
# `sys.modules` while the class body runs, and an unregistered module makes that
# lookup return None.
sys.modules[_SPEC.name] = plan_mod
_SPEC.loader.exec_module(plan_mod)


def _fake_tree(root: Path, recipe_text: str, *, producer: str = "scripts/p.sh") -> Path:
    """A throwaway repo root with one recipe and one executable producer.

    *producer* is spelled as a recipe cites it, relative to the crew tree.
    """
    (root / plan_mod.RUNTIME_SUBPATH).mkdir(parents=True, exist_ok=True)
    script = root / plan_mod.CREW_SUBPATH / producer
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    recipe = root / plan_mod.RUNTIME_SUBPATH / "Dockerfile"
    recipe.write_text(recipe_text, encoding="utf-8")
    return recipe


# ── the derivation ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, role",
    [
        pytest.param(
            "# built by scripts/p.sh\nFROM python:3.12-slim\n",
            "base",
            id="test_a_concrete_from_is_a_base",
        ),
        pytest.param(
            "# built by scripts/p.sh\nARG BASE\nFROM \\\n  ${BASE}\n",
            "layer",
            id="test_a_continuation_joined_from_is_read",
        ),
    ],
)
def test_the_first_from_decides_the_role(tmp_path: Path, text: str, role: str) -> None:
    recipe = _fake_tree(tmp_path, text)
    assert plan_mod.classify(recipe, tmp_path).role == role


@pytest.mark.parametrize(
    "text, base_arg",
    [
        pytest.param(
            "# built by scripts/p.sh\nARG BASE\nFROM ${BASE}\n",
            "BASE",
            id="test_a_pre_from_arg_is_a_digest_pinned_layer",
        ),
        # The rule is "the first FROM interpolates an ARG declared above it", not
        # "the ARG is called BASE": a lane keyed on the literal name would
        # silently stop recognising a renamed one.
        pytest.param(
            "# built by scripts/p.sh\nARG PARENT_IMAGE\nFROM $PARENT_IMAGE\n",
            "PARENT_IMAGE",
            id="test_the_base_arg_name_is_not_hard_coded",
        ),
        # A ``--platform`` option is dropped, so the ARG behind it is still the
        # pinned base.
        pytest.param(
            "# built by scripts/p.sh\nARG BASE\nFROM --platform=$BUILDPLATFORM ${BASE}\n",
            "BASE",
            id="test_a_from_option_does_not_hide_a_layer",
        ),
    ],
)
def test_a_pre_from_arg_makes_a_layer(tmp_path: Path, text: str, base_arg: str) -> None:
    recipe = _fake_tree(tmp_path, text)
    got = plan_mod.classify(recipe, tmp_path)
    assert (got.role, got.base_arg) == ("layer", base_arg)


@pytest.mark.parametrize(
    "flag",
    ["--platform=$BUILDPLATFORM", "--platform=linux/amd64"],
    ids=["interpolated", "literal"],
)
def test_a_from_option_is_not_mistaken_for_the_image(tmp_path: Path, flag: str) -> None:
    """``FROM --platform=... <image>`` is legal and must still read as a base.

    The operand is asserted, not just the role: with a literal ``--platform`` the
    wrong token has no ``$``, so it would still classify as ``base``.
    """
    text = f"# built by scripts/p.sh\nFROM {flag} python:3.12-slim\n"
    operand, _ = plan_mod.first_from_and_pre_args(plan_mod.logical_instructions(text))
    assert operand == "python:3.12-slim", f"read {operand!r} as the image of `FROM {flag} ...`"
    recipe = _fake_tree(tmp_path, text)
    assert plan_mod.classify(recipe, tmp_path).role == "base"


def test_a_stage_alias_is_not_mistaken_for_the_image(tmp_path: Path) -> None:
    """``AS <stage>`` follows the image, so the image is still operand one.

    Asserted on the operand rather than the role for the same reason as the
    option case: a reader that returned the alias would still yield ``base``.
    """
    text = "# built by scripts/p.sh\nFROM python:3.12-slim AS builder\n"
    operand, _ = plan_mod.first_from_and_pre_args(plan_mod.logical_instructions(text))
    assert operand == "python:3.12-slim", f"read {operand!r} as the image of a `FROM ... AS` line"
    recipe = _fake_tree(tmp_path, text)
    assert plan_mod.classify(recipe, tmp_path).role == "base"


def test_a_comment_inside_a_continuation_does_not_break_an_instruction() -> None:
    """The Dockerfile parser drops such a comment; so must this reader."""
    joined = plan_mod.logical_instructions("RUN a \\\n# a note\n    b\n")
    assert len(joined) == 1
    assert joined[0].split() == ["RUN", "a", "b"]


# ── the refusals ─────────────────────────────────────────────────────────────


def test_an_arg_declared_after_from_is_refused(tmp_path: Path) -> None:
    """The out-of-scope ARG defect, refused rather than expanded to empty.

    An ARG declared anywhere but before the FROM is out of scope there, so
    ``FROM ${BASE}`` resolves an empty string with no error. The crew layer's own
    base LABEL carries the same shape one line lower, where it expands blank in
    every build and nothing fails.
    """
    recipe = _fake_tree(tmp_path, "# built by scripts/p.sh\nFROM ${BASE}\nARG BASE\n")
    with pytest.raises(plan_mod.PlanError, match="EMPTY STRING"):
        plan_mod.classify(recipe, tmp_path)


@pytest.mark.parametrize(
    "text, match",
    [
        pytest.param(
            "FROM python:3.12-slim\n",
            "names no scripts",
            id="test_a_recipe_naming_no_producer_is_refused",
        ),
        # Which of two mentioned scripts builds the recipe is not guessable. The
        # refusal carries the remedy instead of a declaration syntax that no
        # recipe would use yet.
        pytest.param(
            "# scripts/p.sh and scripts/q.sh\nFROM python:3.12-slim\n",
            "mentions several scripts",
            id="test_a_recipe_mentioning_two_scripts_is_refused",
        ),
        # The defect this lane exists for: a recipe citing a script nobody wrote.
        pytest.param(
            "# built by scripts/gone.sh\nFROM python:3.12-slim\n",
            "does not exist",
            id="test_an_absent_producer_is_refused",
        ),
        pytest.param(
            "# built by scripts/p.sh\nRUN true\n",
            "no FROM",
            id="test_a_recipe_without_a_from_is_refused",
        ),
    ],
)
def test_an_unbuildable_recipe_is_refused(tmp_path: Path, text: str, match: str) -> None:
    recipe = _fake_tree(tmp_path, text)
    with pytest.raises(plan_mod.PlanError, match=match):
        plan_mod.classify(recipe, tmp_path)


@_POSIX_ONLY
def test_a_non_executable_producer_is_refused(tmp_path: Path) -> None:
    recipe = _fake_tree(tmp_path, "# built by scripts/p.sh\nFROM python:3.12-slim\n")
    (tmp_path / plan_mod.CREW_SUBPATH / "scripts" / "p.sh").chmod(0o644)
    with pytest.raises(plan_mod.PlanError, match="not executable"):
        plan_mod.classify(recipe, tmp_path)


def test_the_executability_check_stands_down_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows the execute bit does not exist, so it is not required there.

    ``os.access(path, os.X_OK)`` is true for any existing file on Windows. The
    platform marker is patched so the Windows branch runs on a POSIX host.
    """
    recipe = _fake_tree(tmp_path, "# built by scripts/p.sh\nFROM python:3.12-slim\n")
    (tmp_path / plan_mod.CREW_SUBPATH / "scripts" / "p.sh").chmod(0o644)
    monkeypatch.setattr(plan_mod.os, "name", "nt")
    assert plan_mod.classify(recipe, tmp_path).producer == f"{plan_mod.CREW_SUBPATH}/scripts/p.sh"


def test_an_absent_producer_is_still_refused_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Standing down on the MODE must not stand down on EXISTENCE.

    A missing producer is the defect this lane exists for, and it is platform
    independent. Without this, widening the Windows carve-out to skip the whole
    producer check would pass every test above.
    """
    recipe = _fake_tree(tmp_path, "# built by scripts/gone.sh\nFROM python:3.12-slim\n")
    monkeypatch.setattr(plan_mod.os, "name", "nt")
    with pytest.raises(plan_mod.PlanError, match="does not exist"):
        plan_mod.classify(recipe, tmp_path)


def test_an_empty_recipe_glob_is_refused(tmp_path: Path) -> None:
    """Guard the guard: an empty glob would make the whole lane vacuous."""
    (tmp_path / plan_mod.RUNTIME_SUBPATH).mkdir(parents=True)
    with pytest.raises(plan_mod.PlanError, match="vacuously green"):
        plan_mod.build_plan(tmp_path)


def test_a_layer_with_no_base_to_pin_is_refused(tmp_path: Path) -> None:
    """A digest needs a base that was built here; nothing else can mint one."""
    _fake_tree(tmp_path, "# built by scripts/p.sh\nARG BASE\nFROM ${BASE}\n")
    with pytest.raises(plan_mod.PlanError, match="no base recipe"):
        plan_mod.build_plan(tmp_path)


def test_a_layer_with_several_candidate_bases_is_refused(tmp_path: Path) -> None:
    """Which base a layer is pinned to has to be derivable, not positional.

    A layer is built on the digest of a base this run pushed. With one base that
    is exact; with two, picking whichever came last would let a layer sit on the
    wrong parent and still report success -- a pass that proves nothing, which is
    the failure this whole lane exists to retire. So it is refused.
    """
    runtime = tmp_path / plan_mod.RUNTIME_SUBPATH
    runtime.mkdir(parents=True)
    script = tmp_path / plan_mod.CREW_SUBPATH / "scripts" / "p.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    (runtime / "Dockerfile").write_text("# scripts/p.sh\nFROM python:3.12\n", encoding="utf-8")
    (runtime / "Dockerfile.other").write_text("# scripts/p.sh\nFROM debian:13\n", encoding="utf-8")
    (runtime / "Dockerfile.crew").write_text(
        "# scripts/p.sh\nARG BASE\nFROM ${BASE}\n", encoding="utf-8"
    )
    with pytest.raises(plan_mod.PlanError, match="which base"):
        plan_mod.build_plan(tmp_path)


def test_several_bases_alone_are_fine(tmp_path: Path) -> None:
    """The refusal is about an ambiguous PAIRING, not about counting bases.

    Without this, tightening the guard to reject two bases outright would pass
    the test above while breaking a tree that has no layer to mis-pin.
    """
    runtime = tmp_path / plan_mod.RUNTIME_SUBPATH
    runtime.mkdir(parents=True)
    script = tmp_path / plan_mod.CREW_SUBPATH / "scripts" / "p.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    (runtime / "Dockerfile").write_text("# scripts/p.sh\nFROM python:3.12\n", encoding="utf-8")
    (runtime / "Dockerfile.other").write_text("# scripts/p.sh\nFROM debian:13\n", encoding="utf-8")
    assert [r.role for r in plan_mod.build_plan(tmp_path)] == ["base", "base"]


def test_bases_are_planned_before_layers(tmp_path: Path) -> None:
    runtime = tmp_path / plan_mod.RUNTIME_SUBPATH
    runtime.mkdir(parents=True)
    script = tmp_path / plan_mod.CREW_SUBPATH / "scripts" / "p.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    # Named so the glob yields the layer FIRST, which is what makes the ordering
    # a real assertion rather than an accident of the filenames.
    (runtime / "Dockerfile").write_text(
        "# scripts/p.sh\nARG BASE\nFROM ${BASE}\n", encoding="utf-8"
    )
    (runtime / "Dockerfile.zbase").write_text(
        "# scripts/p.sh\nFROM python:3.12\n", encoding="utf-8"
    )
    assert [r.role for r in plan_mod.build_plan(tmp_path)] == ["base", "layer"]


# ── the real tree ────────────────────────────────────────────────────────────


def test_the_real_tree_holds_recipes() -> None:
    """Guard the guard, against the real tree this time."""
    assert plan_mod.discover_recipes(ROOT), (
        f"no Dockerfile* under {plan_mod.RUNTIME_SUBPATH}; if the recipes moved, move "
        "crew/scripts/crew_image_build_plan.py and crew-image-build.yml with them"
    )


def test_every_real_recipe_has_a_derivable_role() -> None:
    """No Docker, no producers, no build: just the FROM rule, on every PR.

    This is the cheap half of the lane. It runs in the ordinary backend shards,
    so a recipe added with a FROM this lane cannot classify -- an out-of-scope
    ARG above all -- fails in seconds instead of waiting for a paths-filtered
    build lane to be triggered.
    """
    for recipe in plan_mod.discover_recipes(ROOT):
        role, _ = plan_mod.role_of(recipe, ROOT)
        assert role in {"base", "layer"}, f"{recipe}: unclassifiable role {role!r}"


def test_every_real_recipe_cites_a_producer() -> None:
    """A recipe nothing says how to build is one this lane cannot cover."""
    for recipe, cited in plan_mod.cited_producers(ROOT).items():
        assert cited, (
            f"{recipe} names no scripts/*.sh producer, so neither a reader nor "
            "crew-image-build.yml can know how to build it"
        )


# ── the digest the probe bundle is stamped with ──────────────────────────────


def test_the_script_does_not_restate_the_digest_algorithm() -> None:
    """The probe bundle's digest must come from the container, not a copy.

    A copy would pass the layer producer's check by construction. The longhand
    cross-check already lives in ``container_tests/test_supervisor_bundle.py``
    (``_independent_digest``).
    """
    source = SCRIPT.read_text(encoding="utf-8")
    assert "from container.supervisor.bundle import _content_digest" in source, (
        "the plan script must take the bundle digest from the container's own module, so a "
        "probe bundle is measured by the function that will re-measure it at boot"
    )
    for restated in ("hashlib", 'separators=(",", ":")'):
        assert restated not in source, (
            f"crew_image_build_plan.py restates the digest algorithm ({restated!r}); "
            "import it instead -- a fifth spelling is what the container's own "
            "_independent_digest already covers"
        )


def test_the_probe_digest_is_what_the_container_recomputes(tmp_path: Path) -> None:
    """End to end: the stamped digest is the one the container will check.

    There is one implementation, so this catches a digest over the wrong TREE:
    taken before the last file is written, or over more than the four copied members.
    """
    runtime = ROOT / plan_mod.RUNTIME_SUBPATH
    sys.path.insert(0, str(runtime))
    try:
        from container.supervisor.bundle import _content_digest as image_digest
    finally:
        sys.path.remove(str(runtime))

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    stamped = plan_mod.make_bundle(bundle)
    assert stamped == image_digest(bundle), (
        "the probe bundle's recorded digest does not match its own content, so this lane "
        "would build an image the container refuses at boot"
    )


def test_the_probe_bundle_carries_every_frozen_member(tmp_path: Path) -> None:
    runtime = ROOT / plan_mod.RUNTIME_SUBPATH
    sys.path.insert(0, str(runtime))
    try:
        from container.supervisor.bundle import BUNDLE_ENTRIES
    finally:
        sys.path.remove(str(runtime))

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    plan_mod.make_bundle(bundle)
    for name, kind in BUNDLE_ENTRIES:
        member = bundle / name
        assert member.exists(), f"probe bundle is missing {name}"
        assert member.is_dir() if kind == "dir" else member.is_file()


def test_the_probe_manifest_agrees_with_its_agent(tmp_path: Path) -> None:
    """crew_name and the agent's name are one fact; a build must not split them."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    plan_mod.make_bundle(bundle)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    agent = json.loads((bundle / "agent.json").read_text(encoding="utf-8"))
    assert manifest["crew_name"] == agent["name"]


def test_editing_a_probe_bundle_file_breaks_its_recorded_digest(tmp_path: Path) -> None:
    """Mutation check: the stamp is over the content, not a constant."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    stamped = plan_mod.make_bundle(bundle)
    (bundle / "mcp.json").write_text('{"servers": {}}\n', encoding="utf-8")
    assert plan_mod.content_digest(bundle) != stamped


# ── the lane's own wiring ────────────────────────────────────────────────────


def _workflow_paths() -> list[str]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML resolves a bare `on` key to the boolean True under YAML 1.1, so the
    # trigger block is not reachable under the string "on".
    triggers = doc[True] if True in doc else doc["on"]
    return list(triggers["pull_request"]["paths"])


def _covered(candidate: str, patterns: list[str]) -> bool:
    """Would any of the workflow's ``paths`` patterns match this repo-relative path?

    Only the two forms this workflow uses (``dir/**`` and a flat ``*``). Any other
    shape matches nothing, so the check can miss coverage but never invent it.
    """
    for pattern in patterns:
        if pattern.endswith("/**"):
            prefix = pattern[: -len("/**")]
            if candidate == prefix or candidate.startswith(prefix + "/"):
                return True
        elif fnmatch.fnmatch(candidate, pattern):
            return True
    return False


def test_the_workflow_triggers_on_the_recipe_subtree() -> None:
    patterns = _workflow_paths()
    for recipe in plan_mod.discover_recipes(ROOT):
        rel = recipe.relative_to(ROOT).as_posix()
        assert _covered(rel, patterns), f"{rel} is outside crew-image-build.yml's paths filter"


def test_the_workflow_triggers_on_the_plan_script_and_itself() -> None:
    patterns = _workflow_paths()
    for rel in (
        f"{plan_mod.CREW_SUBPATH}/scripts/crew_image_build_plan.py",
        ".github/workflows/crew-image-build.yml",
    ):
        assert _covered(rel, patterns), f"{rel} is outside crew-image-build.yml's paths filter"


def test_every_cited_producer_is_inside_the_paths_filter() -> None:
    """A producer the lane runs must be one whose edit fires the lane.

    Asserted from what the recipes ACTUALLY cite, with no carve-out for a
    producer that is not in the tree yet: whether the file exists is a separate
    question the build plan already answers, and skipping the check until it does
    would leave the trigger unverified for exactly the producer being written.
    """
    patterns = _workflow_paths()
    cited = plan_mod.cited_producers(ROOT)
    assert any(cited.values()), (
        "no recipe cites a producer, so this assertion checks nothing -- the citation is "
        "what the lane derives its build commands from"
    )
    for recipe, producers in cited.items():
        for producer in producers:
            assert _covered(producer, patterns), (
                f"{recipe} cites {producer}, which is outside crew-image-build.yml's paths "
                "filter -- editing that producer would not run the lane that invokes it"
            )


def test_the_paths_filter_stops_short_of_every_script() -> None:
    """The producer pattern must not have been widened into ``scripts/**``.

    The assertion above is satisfiable by matching everything, which would drag a
    1.5 GB build onto pull requests that cannot affect it. These are scripts in
    the same directory that this lane never runs.
    """
    patterns = _workflow_paths()
    for unrelated in (
        "scripts/local-gate.py",
        "scripts/check_comment_history.py",
        "scripts/build_something_else.sh",
        f"{plan_mod.CREW_SUBPATH}/scripts/build_something_else.sh",
        f"{plan_mod.CREW_SUBPATH}/packaging/build.py",
    ):
        assert not _covered(unrelated, patterns), (
            f"crew-image-build.yml's paths filter matches {unrelated}, which this lane does "
            "not run, so the build would fire on unrelated changes"
        )


# ── the lane's claim to needing no credentials ───────────────────────────────
#
# The lane pushes a base image only to mint a repository digest. It runs on fork PRs
# with no secrets only because every push goes to a loopback address.

#: `NAME=value`, optionally exported, as written in a `run:` block. The value may
#: itself reference other names; resolution below is iterative.
_ASSIGN_RE = re.compile(
    r"""(?m)^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)="""
    r"""("[^"\n]*"|'[^'\n]*'|\S*)[ \t]*$"""
)
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

#: One shell word: a quoted run, or a bare token.
_WORD = r"""("[^"\n]*"|'[^'\n]*'|\S+)"""
#: Leading option flags to step over before the positional argument.
_FLAGS = r"(?:-{1,2}[A-Za-z][\w-]*(?:=\S+)?[ \t]+)*"

_PUSH_RE = re.compile(rf"\bdocker[ \t]+(?:image[ \t]+)?push[ \t]+{_FLAGS}{_WORD}")
#: `docker tag SOURCE DESTINATION` -- only the destination is written to.
_RETAG_RE = re.compile(rf"\bdocker[ \t]+(?:image[ \t]+)?tag[ \t]+{_FLAGS}{_WORD}[ \t]+{_WORD}")
#: `--tag`/`-t` given to anything -- `docker build`, `docker buildx build`, or a
#: producer script that passes it through, which is how this lane spells it.
_TAG_FLAG_RE = re.compile(rf"(?:^|[ \t])(?:--tag|-t)(?:=|[ \t]+){_WORD}", re.MULTILINE)


def _bindings(text: str) -> dict[str, set[str]]:
    """Every name this workflow binds, from its ``env:`` blocks and its scripts.

    Values are collected per name rather than resolved against shell scoping,
    which reading the YAML cannot settle. One name bound to two different values
    is therefore reported, not guessed at.
    """
    doc = yaml.safe_load(text)
    found: dict[str, set[str]] = {}
    for job in (doc.get("jobs") or {}).values():
        if isinstance(job, dict):
            for name, value in (job.get("env") or {}).items():
                found.setdefault(str(name), set()).add(str(value))
    for name, raw in _ASSIGN_RE.findall(text):
        found.setdefault(name, set()).add(raw.strip("\"'"))
    return found


def _write_targets(text: str) -> list[str]:
    """Every image reference this workflow pushes to or tags into.

    Read out of the workflow text rather than listed here: a write site added
    later is covered by the same read, with nobody having to remember this test.
    """
    words = [match.group(1) for match in _PUSH_RE.finditer(text)]
    words += [match.group(2) for match in _RETAG_RE.finditer(text)]
    words += [match.group(1) for match in _TAG_FLAG_RE.finditer(text)]
    return sorted({word.strip("\"'") for word in words})


def _expand(reference: str, bindings: dict[str, str]) -> str:
    """Substitute ``$NAME`` and ``${NAME}`` until the reference stops changing."""
    for _ in range(8):
        expanded = _VAR_RE.sub(
            lambda match: bindings.get(match.group(1) or match.group(2), match.group(0)),
            reference,
        )
        if expanded == reference:
            break
        reference = expanded
    return reference


def _registry_authority(reference: str) -> str | None:
    """The registry authority of an image reference, by Docker's own rule.

    The first component is a host only if it has a ``.`` or ``:`` or is ``localhost``.
    ``None`` means Docker Hub, a real remote registry, so it is a failure here.
    """
    head, _, rest = reference.partition("/")
    if not rest:
        return None
    if head == "localhost" or "." in head or ":" in head:
        return head
    return None


def _is_loopback(authority: str) -> bool:
    """Does this ``host`` or ``host:port`` name an address on this machine only?

    Asked of the address rather than matched against the spelling this lane
    happens to use, so ``127.0.0.2`` and ``[::1]`` answer correctly too.
    """
    host = authority
    if host.startswith("["):
        host = host[1:].partition("]")[0]
    elif host.count(":") == 1:
        host = host.partition(":")[0]
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _off_machine_write_sites(text: str) -> list[str]:
    """Every write site in this workflow text not shown to be loopback.

    Fails closed on the registry AUTHORITY only: no host, or a non-loopback host, is
    a problem; a variable repository or tag under a loopback host is not.
    """
    bindings = _bindings(text)
    ambiguous = {name: sorted(values) for name, values in bindings.items() if len(values) > 1}
    problems = [
        f"{name} is bound to more than one value ({', '.join(values)}), so which one a write "
        "site resolves through cannot be read off the file"
        for name, values in sorted(ambiguous.items())
    ]
    resolved = {name: next(iter(values)) for name, values in bindings.items()}

    for target in _write_targets(text):
        reference = _expand(target, resolved)
        authority = _registry_authority(reference)
        if authority is None:
            problems.append(
                f"writes to {reference!r}, which carries no registry host -- either a Docker Hub "
                "name, or a name this file never binds. This lane must only ever write to a "
                "registry on the runner itself"
            )
            continue
        if not _is_loopback(authority):
            problems.append(
                f"writes to {reference!r}, whose registry {authority!r} is not a loopback "
                "address. This lane's registry exists only to mint a repository digest for an "
                "unpublished base image; writing anywhere reachable off the runner needs "
                "credentials, which this lane deliberately does not have"
            )
    return problems


def test_every_registry_this_lane_writes_to_is_loopback() -> None:
    """No push or tag in this lane may name a registry off this machine.

    Static only: an authority built at runtime (``${{ }}``, command substitution,
    the producer script) and the registry's ``-p`` address are not covered.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    assert _write_targets(text), (
        "no push or tag target was found in crew-image-build.yml, so this assertion checks "
        "nothing. If the lane no longer writes an image, delete this test; if it writes one "
        "in a spelling the derivation above does not read, teach _write_targets that spelling"
    )
    problems = _off_machine_write_sites(text)
    assert not problems, "crew-image-build.yml " + "; also ".join(problems)


@pytest.mark.parametrize(
    "site",
    [
        "docker push registry.example.com/kirocrew-crew-base:ci",
        "docker image push registry.example.com/kirocrew-crew-base:ci",
        "docker push kirocrew/crew-base:ci",
        "docker push kirocrew-crew-base:ci",
        'docker push "$MYSTERY_HOST/kirocrew-crew-base:ci"',
        "docker tag local-only:ci registry.example.com/kirocrew-crew-base:ci",
        "docker build -t registry.example.com/kirocrew-crew-base:ci .",
        "docker buildx build --push --tag registry.example.com/kirocrew-crew-base:ci .",
        "./src/kiro_crew/apps/builtins/aws_control/crew/scripts/build_crew_base_image.sh --tag registry.example.com/base:ci",
    ],
)
def test_an_off_machine_write_site_is_caught(site: str) -> None:
    """The assertion above must red on a write the lane does not make today.

    Each spelling is a way an edit could reach a real registry; the last is a
    producer handed a remote tag, which is how this lane spells its own write.
    """
    real = WORKFLOW.read_text(encoding="utf-8")
    doctored = real.replace('          docker push "$TAG"', f"          {site}")
    assert doctored != real, (
        "the doctored workflow is identical to the real one, so this test proves nothing -- "
        "the push line it edits has been reworded"
    )
    problems = _off_machine_write_sites(doctored)
    assert problems, f"a write to {site!r} was not caught as off-machine"


def test_a_second_binding_of_the_registry_is_caught() -> None:
    """A shell assignment shadows ``env:``, so every binding must be read.

    Which binding a write uses is shell scoping the YAML cannot settle, so two
    bindings are reported rather than letting the loopback one vouch for both.
    """
    real = WORKFLOW.read_text(encoding="utf-8")
    doctored = real.replace(
        '          docker push "$TAG"',
        '          REGISTRY=registry.example.com\n          docker push "$TAG"',
    )
    assert doctored != real, "the push line this test edits has been reworded"
    problems = _off_machine_write_sites(doctored)
    assert any("bound to more than one value" in problem for problem in problems), (
        "a second binding of REGISTRY was not reported, so one binding was silently chosen "
        f"over the other: {problems}"
    )


def test_a_variable_tag_under_a_loopback_host_is_not_refused() -> None:
    """The guard reads the registry host, and must not creep into the whole name.

    Requiring the entire reference to be literal would refuse
    ``127.0.0.1:5000/x:${SOMETHING}``, which is a write to this machine whatever
    the tag resolves to, while adding nothing: a host that does not resolve is
    already caught above, by having no host or by not being a loopback address.
    """
    real = WORKFLOW.read_text(encoding="utf-8")
    doctored = real.replace(
        '          docker push "$TAG"',
        '          docker push "${REGISTRY}/kirocrew-crew-base:${GITHUB_SHA}"',
    )
    assert doctored != real, "the push line this test edits has been reworded"
    assert not _off_machine_write_sites(doctored), (
        "a variable tag under a loopback host was refused, so the guard is reading more than "
        "the registry authority it is about"
    )


def test_the_plan_script_is_recorded_executable() -> None:
    """The mode GIT records is the authority, not the mode on this filesystem.

    The lane invokes it directly. ``stat`` is not portable (Windows reports
    ``0o100666``); the committed mode is what a Linux runner checks out.
    """
    listed = subprocess.run(
        ["git", "ls-files", "--stage", "--", SCRIPT.relative_to(ROOT).as_posix()],
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        check=True,
    ).stdout
    assert listed.strip(), "the plan script is not tracked, so git records no mode for it"
    mode = listed.split()[0]
    assert mode == "100755", (
        f"git records mode {mode} for {SCRIPT.relative_to(ROOT).as_posix()}; it carries a shebang "
        "and the lane invokes it, so the committed mode must be 100755. Fix with "
        f"`git update-index --chmod=+x {SCRIPT.relative_to(ROOT).as_posix()}`"
    )


def test_supervisor_tree_imports_without_kiro_crew(tmp_path: Path) -> None:
    """The build plan imports the container tree with no ``kiro_crew`` on the path.

    The lane imports ``container.supervisor.bundle`` as a top-level package under a bare
    ``python3``, and ``supervisor/__init__.py`` re-exports from ``backend``, so a
    module-scope ``kiro_crew`` import anywhere there fails the lane. ``-S`` drops
    ``site-packages`` (the editable install) to reproduce that. Imports inside a
    function are fine.
    """
    runtime = ROOT / "src/kiro_crew/apps/builtins/aws_control/crew/runtime"
    probe = "import container.supervisor.bundle, container.supervisor.__main__"

    def caches() -> set[Path]:
        return set((runtime / "container").rglob("__pycache__"))

    before = caches()
    # -B, because importing the tree in place would otherwise write __pycache__ into the
    # checkout: a side effect this test would be CAUSING, in the source tree, to check a
    # property that has nothing to do with bytecode. cwd is tmp_path for the same reason.
    out = subprocess.run(
        [sys.executable, "-B", "-S", "-c", probe],
        capture_output=True,
        encoding="utf-8",
        cwd=tmp_path,
        env={"PYTHONPATH": str(runtime), "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert out.returncode == 0, (
        "a supervisor module needs kiro_crew at import time, so "
        "crew_image_build_plan.py cannot reach the bundle digest:\n" + out.stderr
    )
    assert caches() == before, "the probe wrote bytecode into the source tree: " + str(
        sorted(caches() - before)
    )

    # Non-vacuity: the probe must fail for the RIGHT reason when kiro_crew is genuinely
    # required, otherwise a typo in the module names would pass this test silently.
    broken = subprocess.run(
        [sys.executable, "-B", "-S", "-c", "import kiro_crew"],
        capture_output=True,
        encoding="utf-8",
        cwd=tmp_path,
        env={"PYTHONPATH": str(runtime), "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert broken.returncode != 0, "kiro_crew is importable under -S, so this proves nothing"
    assert "ModuleNotFoundError" in broken.stderr
