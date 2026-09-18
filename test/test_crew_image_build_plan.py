"""The crew image lane must cover the recipe CLASS, not two named recipes.

``backend-test-crew-container`` imports the crew image's Python modules and runs
them on the host. It never builds the image, so a recipe naming a producer script
that is not in the tree is green in CI: the image cannot be built from a clean
checkout, and no gate says so.

``crew-image-build.yml`` runs the build for real. These tests hold the part of
that lane which a build cannot check about itself:

* the derivation is right, and every refusal fires -- so "the lane could not
  classify a recipe" can never quietly become "the lane skipped a recipe";
* every recipe in the real tree has a derivable role, checked statically on
  every pull request with no Docker;
* the digest the probe bundle is stamped with equals the one the IMAGE computes,
  so the two cannot drift apart into a lane that builds only bundles the
  container would refuse;
* every producer the recipes cite is inside the workflow's ``paths`` filter, so
  editing a producer fires the lane that runs it;
* every registry the lane writes to is a loopback address, which is the whole
  basis of its claim to need no credentials -- a claim that until now lived only
  in a comment beside the code that could break it.
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
SCRIPT = ROOT / "scripts" / "crew_image_build_plan.py"
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
    """A throwaway repo root with one recipe and one executable producer."""
    (root / plan_mod.RUNTIME_SUBPATH).mkdir(parents=True, exist_ok=True)
    script = root / producer
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    recipe = root / plan_mod.RUNTIME_SUBPATH / "Dockerfile"
    recipe.write_text(recipe_text, encoding="utf-8")
    return recipe


# ── the derivation ───────────────────────────────────────────────────────────


def test_a_concrete_from_is_a_base(tmp_path: Path) -> None:
    recipe = _fake_tree(tmp_path, "# built by scripts/p.sh\nFROM python:3.12-slim\n")
    assert plan_mod.classify(recipe, tmp_path).role == "base"


def test_a_pre_from_arg_is_a_digest_pinned_layer(tmp_path: Path) -> None:
    recipe = _fake_tree(tmp_path, "# built by scripts/p.sh\nARG BASE\nFROM ${BASE}\n")
    got = plan_mod.classify(recipe, tmp_path)
    assert (got.role, got.base_arg) == ("layer", "BASE")


def test_the_base_arg_name_is_not_hard_coded(tmp_path: Path) -> None:
    """A recipe pinning through some other ARG name is still a layer.

    The rule is "the first FROM interpolates an ARG declared above it", not "the
    ARG is called BASE" -- a lane keyed on the literal name would silently stop
    recognising a renamed one.
    """
    recipe = _fake_tree(tmp_path, "# built by scripts/p.sh\nARG PARENT_IMAGE\nFROM $PARENT_IMAGE\n")
    got = plan_mod.classify(recipe, tmp_path)
    assert (got.role, got.base_arg) == ("layer", "PARENT_IMAGE")


def test_a_continuation_joined_from_is_read(tmp_path: Path) -> None:
    recipe = _fake_tree(tmp_path, "# built by scripts/p.sh\nARG BASE\nFROM \\\n  ${BASE}\n")
    assert plan_mod.classify(recipe, tmp_path).role == "layer"


@pytest.mark.parametrize(
    "flag",
    ["--platform=$BUILDPLATFORM", "--platform=linux/amd64"],
    ids=["interpolated", "literal"],
)
def test_a_from_option_is_not_mistaken_for_the_image(tmp_path: Path, flag: str) -> None:
    """``FROM --platform=... <image>`` is legal and must still read as a base.

    Taking the first token after ``FROM`` picks the option instead of the image.
    With an interpolated value that then looks like a ``FROM`` naming an
    undeclared ``ARG``, so a sound base recipe is refused with a message about a
    defect it does not have. With a literal value it is worse than a refusal: the
    operand carries no ``$``, so the recipe classifies as ``base`` anyway and the
    wrong answer is indistinguishable from the right one. The resolved operand is
    therefore asserted, not only the role it produces.
    """
    text = f"# built by scripts/p.sh\nFROM {flag} python:3.12-slim\n"
    operand, _ = plan_mod.first_from_and_pre_args(plan_mod.logical_instructions(text))
    assert operand == "python:3.12-slim", f"read {operand!r} as the image of `FROM {flag} ...`"
    recipe = _fake_tree(tmp_path, text)
    assert plan_mod.classify(recipe, tmp_path).role == "base"


def test_a_from_option_does_not_hide_a_layer(tmp_path: Path) -> None:
    """The option is dropped, so the ARG behind it is still the pinned base."""
    recipe = _fake_tree(
        tmp_path,
        "# built by scripts/p.sh\nARG BASE\nFROM --platform=$BUILDPLATFORM ${BASE}\n",
    )
    got = plan_mod.classify(recipe, tmp_path)
    assert (got.role, got.base_arg) == ("layer", "BASE")


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


def test_the_two_dockerfile_readers_agree() -> None:
    """This reader and the sibling ratchet's must not drift apart.

    ``test_docker_wheel_layer_contract.py`` carries its own copy of this loop for
    the gateway image, so two readers decide what a Dockerfile instruction is. A
    divergence would classify a recipe one way for the ratchet and another way
    for this lane, with both green. They are compared on a REAL recipe carrying
    continuations and comments rather than on a synthetic string, so the input is
    one neither reader was written against.

    The sibling's names are looked up rather than assumed. Reaching straight into
    another test module's private API would raise ``AttributeError`` the moment
    that module reshapes its reader or its recipe set, which reads as a broken
    test rather than as the signal it is. So the lookup fails with a message
    naming what to re-point it at. Deleting this pin is right only when one of the
    two readers goes away, not when the surface around one of them moves.
    """
    ratchet = importlib.import_module("test_docker_wheel_layer_contract")
    reader = getattr(ratchet, "_instructions", None)
    recipes = getattr(ratchet, "RECIPES", None)
    assert reader is not None and recipes, (
        "test_docker_wheel_layer_contract no longer exposes _instructions() and a non-empty "
        "RECIPES, so this parity pin cannot reach the reader it compares against. Re-point it "
        "at whatever that module now calls its reader and its recipe set; delete it only once "
        "one of the two readers is gone"
    )
    for recipe in sorted(recipes):
        assert recipe.is_file(), f"the sibling ratchet names a recipe that is missing: {recipe}"
        theirs = reader(recipe)
        mine = plan_mod.logical_instructions(recipe.read_text(encoding="utf-8"))
        assert mine == theirs, (
            "the crew lane's Dockerfile reader and the wheel-layer ratchet's disagree on "
            f"{recipe}, so the same recipe means two different things to two gates"
        )
        assert mine, f"{recipe} yielded no instructions, so this comparison proves nothing"


def test_the_parity_pin_says_what_to_repoint_when_the_sibling_reshapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing the sibling's surface must read as a signal, not as a broken test.

    The sibling owns its reader and its recipe set as private names, so both are
    free to move. When one does, this pin must say what to re-point rather than
    surface an ``AttributeError`` from reaching into a private API.
    """
    ratchet = importlib.import_module("test_docker_wheel_layer_contract")
    monkeypatch.delattr(ratchet, "RECIPES")
    with pytest.raises(AssertionError, match="Re-point it"):
        test_the_two_dockerfile_readers_agree()


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


def test_a_recipe_naming_no_producer_is_refused(tmp_path: Path) -> None:
    recipe = _fake_tree(tmp_path, "FROM python:3.12-slim\n")
    with pytest.raises(plan_mod.PlanError, match="names no scripts"):
        plan_mod.classify(recipe, tmp_path)


def test_a_recipe_mentioning_two_scripts_is_refused(tmp_path: Path) -> None:
    """Which of two mentioned scripts builds the recipe is not guessable.

    The refusal carries the remedy, because the alternative -- a declaration
    syntax -- would be a second way to say the same thing with no recipe using
    it. The first genuinely ambiguous recipe is what should buy that.
    """
    recipe = _fake_tree(tmp_path, "# scripts/p.sh and scripts/q.sh\nFROM python:3.12-slim\n")
    with pytest.raises(plan_mod.PlanError, match="mentions several scripts"):
        plan_mod.classify(recipe, tmp_path)


def test_an_absent_producer_is_refused(tmp_path: Path) -> None:
    """The defect this lane exists for: a recipe citing a script nobody wrote."""
    recipe = _fake_tree(tmp_path, "# built by scripts/gone.sh\nFROM python:3.12-slim\n")
    with pytest.raises(plan_mod.PlanError, match="does not exist"):
        plan_mod.classify(recipe, tmp_path)


@_POSIX_ONLY
def test_a_non_executable_producer_is_refused(tmp_path: Path) -> None:
    recipe = _fake_tree(tmp_path, "# built by scripts/p.sh\nFROM python:3.12-slim\n")
    (tmp_path / "scripts" / "p.sh").chmod(0o644)
    with pytest.raises(plan_mod.PlanError, match="not executable"):
        plan_mod.classify(recipe, tmp_path)


def test_the_executability_check_stands_down_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows the execute bit does not exist, so it is not required there.

    ``os.access(path, os.X_OK)`` answers true for any existing file on Windows,
    so the check cannot fail there whatever the mode is; requiring it would make
    the outcome depend on which platform ran the plan. The requirement binds on
    the Linux runner that invokes the producer. This drives the Windows branch
    from a POSIX host by patching the platform marker the guard reads, so the
    branch is executed rather than argued about.
    """
    recipe = _fake_tree(tmp_path, "# built by scripts/p.sh\nFROM python:3.12-slim\n")
    (tmp_path / "scripts" / "p.sh").chmod(0o644)
    monkeypatch.setattr(plan_mod.os, "name", "nt")
    assert plan_mod.classify(recipe, tmp_path).producer == "scripts/p.sh"


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


def test_a_recipe_without_a_from_is_refused(tmp_path: Path) -> None:
    recipe = _fake_tree(tmp_path, "# built by scripts/p.sh\nRUN true\n")
    with pytest.raises(plan_mod.PlanError, match="no FROM"):
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
    script = tmp_path / "scripts" / "p.sh"
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
    script = tmp_path / "scripts" / "p.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    (runtime / "Dockerfile").write_text("# scripts/p.sh\nFROM python:3.12\n", encoding="utf-8")
    (runtime / "Dockerfile.other").write_text("# scripts/p.sh\nFROM debian:13\n", encoding="utf-8")
    assert [r.role for r in plan_mod.build_plan(tmp_path)] == ["base", "base"]


def test_bases_are_planned_before_layers(tmp_path: Path) -> None:
    runtime = tmp_path / plan_mod.RUNTIME_SUBPATH
    runtime.mkdir(parents=True)
    script = tmp_path / "scripts" / "p.sh"
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
        "scripts/crew_image_build_plan.py and crew-image-build.yml with them"
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

    A restated algorithm would let a probe bundle satisfy the layer producer's
    digest check by construction whatever the container's function did. That
    protection is real, but it already exists without a copy here:
    ``container_tests/test_supervisor_bundle.py`` keeps an ``_independent_digest``
    written out longhand so a scheme change breaks an agreement between two
    implementations there. A copy in this script would be the fifth in-tree
    spelling, and this fails if one comes back.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    assert "from container.supervisor.bundle import _content_digest" in source, (
        "the plan script must take the bundle digest from the container's own module, so a "
        "probe bundle is measured by the function that will re-measure it at boot"
    )
    for restated in ("hashlib", 'separators=(",", ":")'):
        assert restated not in source, (
            f"scripts/crew_image_build_plan.py restates the digest algorithm ({restated!r}); "
            "import it instead -- a fifth spelling is what the container's own "
            "_independent_digest already covers"
        )


def test_the_probe_digest_is_what_the_container_recomputes(tmp_path: Path) -> None:
    """End to end: the stamped digest is the one the container will check.

    The script imports the container's function, so this cannot catch a
    divergence between two implementations -- there is only one. What it does
    catch is a probe bundle stamped over the wrong TREE: a digest computed before
    the last file is written, or over the caller's directory rather than the four
    members the recipe copies.
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

    Reduced to the two glob forms this workflow uses -- a ``dir/**`` subtree and
    a flat ``*`` within one directory -- rather than reimplementing GitHub's
    matcher. A pattern in some other shape would silently match nothing here, so
    the assertion is conservative: it can demand a pattern that is missing, and
    cannot invent coverage that is not there.
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
    for rel in ("scripts/crew_image_build_plan.py", ".github/workflows/crew-image-build.yml"):
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
    ):
        assert not _covered(unrelated, patterns), (
            f"crew-image-build.yml's paths filter matches {unrelated}, which this lane does "
            "not run, so the build would fire on unrelated changes"
        )


# ── the lane's claim to needing no credentials ───────────────────────────────
#
# The lane pushes a base image only to mint a repository digest, and its whole
# claim to running on a fork PR with no secrets rests on that push going to a
# loopback address. Until this assertion existed, "loopback" lived in a comment
# and one `env:` value: nothing failed if a later edit pointed them at a real
# host, or if a third write site hardcoded one. A non-loopback push is the single
# change that would turn this lane from credential-free into credential-
# requiring, so it is the property a reader most wants guaranteed -- and prose
# asserting what the adjacent code does not enforce is this change's own subject
# one level up.

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

    The first ``/``-separated component is a registry host only when it carries a
    ``.`` or a ``:``, or is exactly ``localhost``. Without one the whole reference
    is a Docker Hub repository -- which for something being PUSHED means a real
    remote registry needing real credentials, so ``None`` is a failure here and
    not an exemption.
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

    Fails closed on the registry AUTHORITY, which is the part that decides
    whether a credential is needed: a reference with no host (a Docker Hub name,
    or one built from a name this file never binds) and one whose host is a real
    address are both problems. What follows the host is not read -- a variable
    repository or tag under a loopback host is still a write to this machine.
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

    Not covered: this reads the YAML statically, so an authority built at runtime
    -- by a ``${{ }}`` expression, a command substitution, or the producer script
    itself -- and the ``-p`` address the registry container publishes on, are
    outside it; what is enforced is the reference every write site in the
    workflow text names.
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
        "./scripts/build_crew_base_image.sh --tag registry.example.com/base:ci",
    ],
)
def test_an_off_machine_write_site_is_caught(site: str) -> None:
    """The assertion above must red on a write the lane does not make today.

    A guard whose existence is cited as the guarantee has to be shown to fire.
    Each spelling here is a way a later edit could reach a real registry -- a
    bare ``docker push``, the same under ``docker image``, a Docker Hub name
    with a namespace and one without, a re-tag, a build tag, a buildx push, and a
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

    ``REGISTRY=registry.example.com`` in a run block overrides the job's value for
    that block, and which one a write site resolves through is a fact about shell
    scoping that reading the YAML cannot settle. Resolving through either binding
    alone would let the loopback one vouch for a write that does not use it, so
    two bindings is reported rather than chosen between.
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

    The recipe's prose tells a reader to invoke its producer directly, and the
    plan script refuses a producer that is not executable, so this file must be
    executable too. ``stat`` cannot answer that portably: Windows has no execute
    bit, so a checked-out file reports ``0o100666`` and a mode test fails there
    for every file in the tree. The committed mode is the same fact on every
    platform, and it is the one that decides what a Linux runner checks out.
    """
    listed = subprocess.run(
        ["git", "ls-files", "--stage", "--", "scripts/crew_image_build_plan.py"],
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        check=True,
    ).stdout
    assert listed.strip(), "the plan script is not tracked, so git records no mode for it"
    mode = listed.split()[0]
    assert mode == "100755", (
        f"git records mode {mode} for scripts/crew_image_build_plan.py; it carries a shebang "
        "and the lane invokes it, so the committed mode must be 100755. Fix with "
        "`git update-index --chmod=+x scripts/crew_image_build_plan.py`"
    )
