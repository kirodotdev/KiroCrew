#!/usr/bin/env python3
"""Discover the AWS Control crew image recipes and derive how to build each one.

WHY THIS EXISTS RATHER THAN TWO NAMES IN A WORKFLOW. The defect this lane closes
is not "the crew image did not build" but "a check existed and covered one
instance instead of the property". ``backend-test-crew-container`` imports the
image's Python modules and runs them on the host, so a recipe naming a producer
script that was never written stayed green all the way through review (#9223).
The wheel-layer ratchet is the same story from the other side: it pinned one
file path, so a second recipe reintroduced the exact defect it guarded.

So the lane does not name ``Dockerfile`` and ``Dockerfile.crew``. It asks the
tree which recipes exist and derives each one's role and producer FROM THE
RECIPE ITSELF, and refuses anything it cannot account for. A third recipe added
later is either built or reds the lane; it cannot be silently uncovered.

WHAT IS DERIVED, AND FROM WHAT EVIDENCE

* **role** -- from the first ``FROM``. An operand interpolating an ``ARG``
  declared BEFORE that ``FROM`` is a digest-pinned layer over another image
  (``layer``); a concrete operand is self-contained (``base``). An operand
  interpolating a name NOT declared before ``FROM`` is refused: a build ``ARG``
  is out of scope where that puts it, so it expands to an empty string with no
  error, which is the defect #10711 found in ``Dockerfile.crew``'s base label.
* **producer** -- the single ``scripts/*.sh`` the recipe mentions in prose.
  Nothing else in the tree records which command builds a recipe, so a mention is
  the only evidence there is. Zero mentions and two mentions are both refused
  rather than guessed.

THE INVOCATION CONTRACT. A ``base`` producer is called ``<script> --tag TAG``
and a ``layer`` producer ``<script> --base REPO@sha256:... --bundle DIR``. That
is a contract, not a discovery: a new recipe whose producer takes different
flags reds the lane, and the fix is to teach this file rather than to widen it
into guessing at command lines.

Usage:
    crew_image_build_plan.py                  human-readable plan
    crew_image_build_plan.py --tsv            the plan the workflow executes
    crew_image_build_plan.py --make-bundle D  write a probe bundle into D
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: The subtree whose recipes this lane is responsible for. One directory, not a
#: repo-wide sweep: ``docker/Dockerfile`` is the gateway distribution and is
#: already built by ``docker-smoke.yml``.
RUNTIME_SUBPATH = "src/kiro_crew/apps/builtins/aws_control/crew/runtime"

#: A ``scripts/<name>.sh`` path cited anywhere in a recipe's text, comments
#: included -- the citation IS prose, so the recipe is read raw here.
NAMED_SCRIPT = re.compile(r"scripts/[A-Za-z0-9_.-]+\.sh")

#: ``${NAME}`` or ``$NAME`` inside a ``FROM`` operand.
INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

#: The crew name a probe bundle declares. Lowercase and hyphenated because the
#: layer producer derives a Docker repository from it and refuses a name that
#: cannot form one.
PROBE_CREW_NAME = "ci-probe"


class PlanError(RuntimeError):
    """The tree holds a recipe this lane cannot account for."""


@dataclass(frozen=True)
class Recipe:
    """One discovered image recipe and how to build it."""

    path: str
    role: str
    producer: str
    base_arg: str = ""


def logical_instructions(text: str) -> list[str]:
    """Logical Dockerfile instructions: continuations joined, comments dropped.

    The Dockerfile parser strips whole-line comments even inside a
    backslash-continued instruction, so that is mirrored before joining.

    ``test/test_docker_wheel_layer_contract.py`` reads recipes the same way for
    its own ratchet, against one hard-coded path rather than a caller's text.
    The two are held byte-equal by a parity test in
    ``test/test_crew_image_build_plan.py``, so this reader is a deliberate copy
    and a divergence fails a test rather than silently classifying a recipe one
    way here and another way there.
    """
    logical: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        logical.append((pending + line).strip())
        pending = ""
    if pending:
        logical.append(pending.strip())
    return logical


def first_from_and_pre_args(instructions: list[str]) -> tuple[str, set[str]]:
    """The first ``FROM``'s image operand, and the ARG names declared above it.

    ``--flag`` tokens are dropped before the operand is chosen: ``FROM
    --platform=$BUILDPLATFORM python:3.12`` is legal, and reading the flag as the
    image would refuse a perfectly good base recipe for interpolating a name no
    ``ARG`` declares. The sibling ratchet drops the same option tokens out of a
    ``COPY``'s operands. Only ``FROM``'s options carry a leading ``--``, and an
    ``AS <stage>`` suffix comes after the image, so the first surviving token is
    the image reference in every form.
    """
    pre_args: set[str] = set()
    for inst in instructions:
        head, _, rest = inst.partition(" ")
        upper = head.upper()
        if upper == "ARG":
            for assignment in rest.split():
                name = assignment.split("=", 1)[0].strip()
                if name:
                    pre_args.add(name)
        elif upper == "FROM":
            operands = [t for t in rest.split() if not t.startswith("--")]
            return (operands[0] if operands else ""), pre_args
    return "", pre_args


def _role_of(rel: str, operand: str, pre_args: set[str]) -> tuple[str, str]:
    """Classify one ``FROM`` operand into ``(role, base_arg)``."""
    interpolated = {m.group(1) or m.group(2) for m in INTERPOLATION.finditer(operand)}
    if not interpolated:
        return "base", ""
    undeclared = sorted(interpolated - pre_args)
    if undeclared:
        raise PlanError(
            f"{rel}: FROM {operand} interpolates {undeclared}, which is not declared "
            "with ARG before the FROM. A build argument declared anywhere else is out "
            "of scope there and expands to an EMPTY STRING with no error, so the build "
            "resolves a base nobody chose."
        )
    if len(interpolated) != 1:
        raise PlanError(
            f"{rel}: FROM {operand} interpolates {sorted(interpolated)}; this lane can "
            "pin exactly one base argument to a digest. Teach "
            "scripts/crew_image_build_plan.py how to supply the others."
        )
    return "layer", next(iter(interpolated))


def _producer_of(rel: str, text: str, root: Path) -> str:
    """The one ``scripts/*.sh`` the recipe mentions, checked runnable.

    Any mention counts, which is a real fragility: a recipe that refers to a
    second script in passing has two mentions and no way to say which one builds
    it. That is refused rather than guessed, and the refusal says what to do.

    Refused rather than resolved by a declaration syntax, because no recipe is
    ambiguous today. A marker would be a second way to say the same thing with
    nothing using it; the first genuinely ambiguous recipe is what should buy it.
    """
    cited = sorted(set(NAMED_SCRIPT.findall(text)))
    if not cited:
        raise PlanError(
            f"{rel}: names no scripts/*.sh producer, so nothing in the tree says how to "
            "build it. Name the producer in the recipe, or delete the recipe."
        )
    if len(cited) > 1:
        raise PlanError(
            f"{rel}: mentions several scripts {cited} and nothing says which one builds it. "
            "Leave only the producer named in the recipe and refer to the others some other "
            "way -- by their own recipe, or in the script that uses them."
        )
    producer = cited[0]
    script = root / producer
    if not script.is_file():
        raise PlanError(
            f"{rel}: names producer {producer}, which does not exist. A recipe citing an "
            "absent producer cannot be built from a clean checkout -- exactly how this "
            "subtree merged unbuildable in #9223."
        )
    # Windows has no execute bit, and `os.access(path, X_OK)` there answers true
    # for any file that exists, so the check can neither pass meaningfully nor
    # fail. The requirement binds on the Linux runner that invokes the producer.
    if os.name != "nt" and not os.access(script, os.X_OK):
        raise PlanError(
            f"{rel}: names producer {producer}, which is not executable, so the command "
            "the recipe's own prose tells a reader to run fails for everyone."
        )
    return producer


def role_of(recipe: Path, root: Path = ROOT) -> tuple[str, str]:
    """One recipe's ``(role, base_arg)``, from its first ``FROM`` alone.

    Separate from :func:`classify` so the FROM reasoning can be checked against
    the real tree without the producers having to be present -- which is what
    lets a static test hold every recipe to the out-of-scope-ARG rule on every
    pull request, with no Docker and no build.
    """
    rel = recipe.relative_to(root).as_posix()
    text = recipe.read_text(encoding="utf-8")
    operand, pre_args = first_from_and_pre_args(logical_instructions(text))
    if not operand:
        raise PlanError(f"{rel}: no FROM instruction, so it is not an image recipe")
    return _role_of(rel, operand, pre_args)


def discover_recipes(root: Path = ROOT) -> list[Path]:
    """Every ``Dockerfile*`` under the crew runtime directory, sorted."""
    runtime_dir = root / RUNTIME_SUBPATH
    if not runtime_dir.is_dir():
        raise PlanError(f"no crew runtime directory at {RUNTIME_SUBPATH}")
    return sorted(p for p in runtime_dir.glob("Dockerfile*") if p.is_file())


def cited_producers(root: Path = ROOT) -> dict[str, list[str]]:
    """``recipe -> the scripts/*.sh paths it cites``, checking nothing.

    The trigger-coverage test reads this: a producer this lane actually runs has
    to be inside the workflow's ``paths`` filter, or editing that producer does
    not fire the lane that runs it.
    """
    return {
        recipe.relative_to(root).as_posix(): sorted(
            set(NAMED_SCRIPT.findall(recipe.read_text(encoding="utf-8")))
        )
        for recipe in discover_recipes(root)
    }


def classify(recipe: Path, root: Path = ROOT) -> Recipe:
    """Derive one recipe's role and producer from its own text.

    Raises :class:`PlanError` naming the property that was violated. Every
    refusal here is a recipe a human must account for, never one the lane may
    skip: a lane that skips what it does not understand is the shape of check
    this change exists to retire.
    """
    role, base_arg = role_of(recipe, root)
    rel = recipe.relative_to(root).as_posix()
    text = recipe.read_text(encoding="utf-8")
    return Recipe(path=rel, role=role, producer=_producer_of(rel, text, root), base_arg=base_arg)


def build_plan(root: Path = ROOT) -> list[Recipe]:
    """Every recipe under the crew runtime directory, bases first.

    Ordering is not the whole contract. A layer is built on the digest of a base
    this run pushed, and nothing in a recipe says WHICH base it wants, so the
    pairing is only unambiguous while there is exactly one. More than one is
    refused rather than resolved by order -- see the guard below.
    """
    recipes = discover_recipes(root)
    if not recipes:
        raise PlanError(
            f"no Dockerfile* under {RUNTIME_SUBPATH}. An empty glob would make this lane "
            "vacuously green, which is the failure it exists to prevent -- if the recipes "
            "moved, move this lane with them."
        )
    plan = [classify(recipe, root) for recipe in recipes]
    bases = [r for r in plan if r.role == "base"]
    layers = [r for r in plan if r.role == "layer"]
    if layers and not bases:
        raise PlanError(
            f"the tree holds digest-pinned layer recipe(s) {[r.path for r in layers]} and "
            "no base recipe to pin them to, so no real repository digest can be produced "
            "without credentials."
        )
    if layers and len(bases) > 1:
        raise PlanError(
            f"the tree holds {len(bases)} base recipes {[r.path for r in bases]} and layer "
            f"recipe(s) {[r.path for r in layers]}, and nothing in a layer says which base "
            "it is pinned to. Building on whichever base came last would let a layer sit on "
            "the wrong parent and still report success, which is the vacuous pass this lane "
            "exists to prevent. Record the pairing where it can be derived -- in the recipe "
            "or in a manifest this file reads -- and teach this function to read it."
        )
    return bases + layers


def content_digest(root: Path) -> str:
    """The bundle content digest, from the container's own implementation.

    Imported rather than restated. A restatement would let a probe bundle satisfy
    the layer producer's digest check by construction whatever the container's
    function did, but that protection already exists and does not need a fifth
    in-tree copy of the algorithm: ``container_tests/test_supervisor_bundle.py``
    keeps an ``_independent_digest`` written out longhand for exactly this
    reason, so a scheme change breaks an agreement between two implementations
    there.

    Imported lazily, and by path: the module lives in the image's own tree, not
    on this repository's import path, and the layer producer reaches it the same
    way. Nothing else in this file needs it, so an import failure surfaces only
    where a probe bundle is actually being written.
    """
    runtime = ROOT / RUNTIME_SUBPATH
    if str(runtime) not in sys.path:
        sys.path.insert(0, str(runtime))
    from container.supervisor.bundle import _content_digest

    return str(_content_digest(root))


def make_bundle(out: Path) -> str:
    """Write the minimum bundle a layer recipe can be built from.

    Deliberately synthetic rather than produced by ``crew/packaging``. What this
    lane answers for is that the RECIPES build; the producer has its own suite,
    and driving it here would make a producer regression look like an
    unbuildable image. So the bundle is an input, and the smallest one the
    layout contract admits: ``mcp.json`` may be ``{}`` and ``skills/`` may be
    empty, but every member must exist.
    """
    (out / "skills" / "probe").mkdir(parents=True, exist_ok=True)
    (out / "skills" / "probe" / "SKILL.md").write_text(
        "# probe\n\nOne file, so skills/ is a populated tree rather than an empty\n"
        "directory a recipe could copy trivially.\n",
        encoding="utf-8",
    )
    (out / "agent.json").write_text(
        json.dumps({"name": PROBE_CREW_NAME, "prompt": "CI probe crew."}, indent=2) + "\n",
        encoding="utf-8",
    )
    (out / "mcp.json").write_text("{}\n", encoding="utf-8")
    manifest = {
        "bundle_version": "0",
        "crew_name": PROBE_CREW_NAME,
        "created_at": "1970-01-01T00:00:00Z",
        "digest": "",
    }
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    # The digest excludes manifest.json itself, so one pass is stable.
    manifest["digest"] = content_digest(out)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return str(manifest["digest"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive the AWS Control crew image build plan from the tree.",
    )
    parser.add_argument(
        "--tsv",
        action="store_true",
        help="emit role/path/producer/base_arg as tabs, for a shell read loop",
    )
    parser.add_argument("--make-bundle", metavar="DIR", help="write a probe bundle into DIR")
    args = parser.parse_args(argv)

    if args.make_bundle:
        out = Path(args.make_bundle)
        out.mkdir(parents=True, exist_ok=True)
        print(make_bundle(out))
        return 0
    try:
        plan = build_plan()
    except PlanError as exc:
        print(f"crew_image_build_plan: {exc}", file=sys.stderr)
        return 1
    if args.tsv:
        for recipe in plan:
            print("\t".join((recipe.role, recipe.path, recipe.producer, recipe.base_arg)))
    else:
        for recipe in plan:
            pin = f" (base pinned through ARG {recipe.base_arg})" if recipe.base_arg else ""
            print(f"{recipe.role:6} {recipe.path} <- {recipe.producer}{pin}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
