"""A CI-built wheel must never be COPYed into a committed image layer.

Image layers are append-only. ``COPY dist/*.whl /tmp/wheels/`` commits the
~48MB wheel to its own layer, and an ``rm -rf /tmp/wheels`` in a LATER
instruction can only stack a whiteout on top -- the bytes stay in the layer
stack, so every ``docker pull`` fetches the wheel and then discards it, next
to the already-installed copy of the same code. Measured on the published
artifact: 48,047,081 compressed bytes, 5.7% of
``ghcr.io/kirodotdev/kirocrew:latest``; layer 6 contained exactly ``tmp/``,
``tmp/wheels/`` and the wheel, layer 7 exactly the ``tmp/.wh.wheels``
whiteout -- the proof the delete happened a layer too late.

The fix consumes the wheel through a BuildKit context bind mount
(``RUN --mount=type=bind,source=<dir>,target=/tmp/wheels``): the wheel is
visible only for the duration of that RUN and never enters a layer, which
makes the cleanup ``rm`` unnecessary rather than merely late.

**This rule is per recipe, not per repository.** It was written for
``docker/Dockerfile`` and pinned only that path, so the AWS Control crew image
merged with the original defect intact -- a ``COPY vendor/*.whl`` followed by
an ``rm -rf /tmp/wheels`` one instruction later -- and every gate stayed green
because no gate read that file. :data:`RECIPES` is therefore the list of image
recipes that install a wheel, each with the context directory its wheel is
staged in, and a new recipe belongs here as part of adding it.

Why a ratchet: a future "simplification" back to COPY would reintroduce the
dead weight SILENTLY -- the image still builds, boots, and passes every
functional smoke gate, just ~48MB heavier per pull. Static and offline: this
reads only the Dockerfile text, so it cannot flake and needs no Docker
daemon (same shape as ``test_workflow_cache_setup_uniqueness.py``).
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_CREW_RUNTIME = (
    ROOT / "src" / "kiro_crew" / "apps" / "builtins" / "aws_control" / "crew" / "runtime"
)

#: ``recipe path -> the build-context directory its single wheel is staged in``.
#:
#: The directory name differs per recipe and is not cosmetic: it is the
#: ``source=`` operand of the bind mount, so asserting the wrong one would pass a
#: recipe that mounts nothing. ``docker/Dockerfile`` takes the wheel from the
#: repo root's ``dist/`` (admitted by ``.dockerignore``'s inverted allowlist);
#: the crew image's context is ``runtime/`` and its wheel is staged in
#: ``vendor/``, which ``MANIFEST.in`` also ships so the image can be built from
#: an installed copy.
RECIPES: dict[Path, str] = {
    ROOT / "docker" / "Dockerfile": "dist",
    _CREW_RUNTIME / "Dockerfile": "vendor",
}

#: Recipes that install NO wheel, asserted rather than assumed by
#: :func:`test_wheel_free_recipes_install_nothing`. ``Dockerfile.crew`` is a
#: digest-pinned layer over the crew base image whose own contract is that it
#: compiles nothing and installs nothing, so it has no wheel to mishandle. If
#: that ever changes it belongs in :data:`RECIPES`, and the guard below fails
#: until it is moved.
WHEEL_FREE: tuple[Path, ...] = (_CREW_RUNTIME / "Dockerfile.crew",)


def _load_build_plan():
    """The crew image lane's build-plan module, loaded from ``scripts/``.

    ``scripts/`` is not an importable package, so the module is loaded by file
    path -- the same way ``test/test_crew_image_build_plan.py`` loads it. It is
    registered in ``sys.modules`` before execution because ``@dataclass`` looks
    its own module up there while the class body runs.
    """
    name = "crew_image_build_plan"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / "crew_image_build_plan.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _instructions(dockerfile: Path) -> list[str]:
    """Logical Dockerfile instructions, continuations joined, comments dropped.

    A thin per-file wrapper over the single repository reader,
    ``logical_instructions`` in ``scripts/crew_image_build_plan.py``: it reads
    the recipe's text and classifies it through that one reader, so this ratchet
    and the crew image lane cannot classify the same recipe differently.
    """
    text = dockerfile.read_text(encoding="utf-8")
    return _load_build_plan().logical_instructions(text)


def _copy_sources_and_dest(inst: str) -> tuple[list[str], str]:
    """Split a COPY/ADD instruction into (source operands, destination).

    Tokens after the instruction name, minus ``--flag`` options; the last
    remaining token is the destination, the rest are sources.
    """
    operands = [t for t in inst.split()[1:] if not t.startswith("--")]
    return operands[:-1], operands[-1] if operands else ""


def _could_carry_the_wheel(src: str, wheel_dir: str) -> bool:
    """Could this COPY/ADD source operand sweep the staged wheel in?

    Any source that names the wheel, the staging tree, or the whole context
    root can commit the wheel to a layer. ``wheel_dir`` is per recipe because
    the staging directory is not a fixed name (see :data:`RECIPES`).
    """
    normalized = src.lstrip("./")
    return (
        src in {".", "./"} or normalized == "" or normalized.startswith(wheel_dir) or ".whl" in src
    )


def _ids(paths) -> list[str]:
    """Readable parametrize ids: the recipe path relative to the repo root."""
    return [str(p.relative_to(ROOT)) for p in paths]


@pytest.mark.parametrize("dockerfile", list(RECIPES), ids=_ids(RECIPES))
def test_dockerfile_exists(dockerfile: Path) -> None:
    """Guard the guard: a moved Dockerfile would make the ratchet vacuous."""
    assert dockerfile.is_file(), f"expected image recipe at {dockerfile}"


@pytest.mark.parametrize("dockerfile", list(RECIPES), ids=_ids(RECIPES))
def test_wheel_never_enters_a_committed_layer(dockerfile: Path) -> None:
    """No COPY/ADD may bring the wheel (or a tree holding it) into the image."""
    wheel_dir = RECIPES[dockerfile]
    offenders = []
    for inst in _instructions(dockerfile):
        if inst.split(maxsplit=1)[0].upper() not in {"COPY", "ADD"}:
            continue
        sources, dest = _copy_sources_and_dest(inst)
        if "/tmp/wheels" in dest or any(_could_carry_the_wheel(s, wheel_dir) for s in sources):
            offenders.append(inst)
    assert not offenders, (
        f"{dockerfile.relative_to(ROOT)}: the wheel must reach pip via the bind "
        "mount, never COPY/ADD: a copied wheel is committed to its own layer and "
        "a later `rm` only adds a whiteout, shipping ~48MB of dead weight in "
        f"every pull (#5778). Use RUN --mount=type=bind,source={wheel_dir},"
        "target=/tmp/wheels instead:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("dockerfile", list(RECIPES), ids=_ids(RECIPES))
def test_wheel_is_consumed_via_context_bind_mount(dockerfile: Path) -> None:
    """The install RUN keeps the mount AND the exactly-one-wheel guard."""
    wheel_dir = RECIPES[dockerfile]
    rel = dockerfile.relative_to(ROOT)
    runs = [i for i in _instructions(dockerfile) if i.upper().startswith("RUN")]
    install = [r for r in runs if "pip install" in r and "/tmp/wheels" in r]
    assert len(install) == 1, (
        f"{rel}: expected exactly one wheel-installing RUN, found "
        f"{len(install)}: {install!r}. The wheel must be consumed from "
        "/tmp/wheels via the context bind mount -- never COPYed into a layer "
        "(#5778)"
    )
    run = install[0]
    # Option order inside --mount is not significant to BuildKit, so assert
    # the tokens independently rather than one order-sensitive literal.
    assert "--mount=" in run and all(
        token in run for token in ("type=bind", f"source={wheel_dir}", "target=/tmp/wheels")
    ), (
        f"{rel}: the wheel-installing RUN must bind-mount {wheel_dir}/ from the "
        "build context so the wheel never lands in a layer (#5778)"
    )
    assert "-eq 1" in run and "WHEEL_COUNT" in run, (
        f"{rel}: the exactly-one-wheel guard must survive: the staging directory "
        "is globbed rather than named, and the guard is what keeps the glob "
        "version-agnostic while refusing an ambiguous multi-wheel context"
    )


@pytest.mark.parametrize("dockerfile", list(WHEEL_FREE), ids=_ids(WHEEL_FREE))
def test_wheel_free_recipes_install_nothing(dockerfile: Path) -> None:
    """A recipe listed as wheel-free must really have no install step.

    Without this, moving a recipe out of :data:`RECIPES` would be enough to
    silence the ratchet for it -- the same failure mode that let the crew image
    merge with a COPYed wheel, one level up.
    """
    rel = dockerfile.relative_to(ROOT)
    assert dockerfile.is_file(), f"expected image recipe at {dockerfile}"
    instructions = _instructions(dockerfile)
    runs = [i for i in instructions if i.upper().startswith("RUN")]
    assert not runs, (
        f"{rel} is declared wheel-free but has RUN instructions: {runs!r}. If it "
        "now installs a wheel, move it into RECIPES with its staging directory"
    )
    carriers = [
        i for i in instructions if i.split(maxsplit=1)[0].upper() in {"COPY", "ADD"} and ".whl" in i
    ]
    assert not carriers, (
        f"{rel} is declared wheel-free but copies a wheel: {carriers!r}. Move it "
        "into RECIPES so the bind-mount rule applies"
    )


#: Every recipe whose text may name a build script, wheel-carrying or not.
_ALL_RECIPES: tuple[Path, ...] = tuple(RECIPES) + WHEEL_FREE

#: A ``scripts/<name>.sh`` path cited anywhere in a recipe's text.
_NAMED_SCRIPT = re.compile(r"scripts/[A-Za-z0-9_.-]+\.sh")


@pytest.mark.parametrize("dockerfile", list(_ALL_RECIPES), ids=_ids(_ALL_RECIPES))
def test_named_build_scripts_exist_and_are_executable(dockerfile: Path) -> None:
    """A script a recipe names as its producer must exist and be runnable.

    This is the defect that shipped, not a hypothetical one. Both crew recipes
    named a build script in prose -- the wheel producer and the digest-pinned-base
    caller -- and NEITHER script was in the tree, so the image could not be built
    from a clean checkout at all. Nothing failed, because the only lane that reads
    that subtree runs the image's Python modules and never runs ``docker build``.

    Derived from the recipe TEXT rather than a hard-coded list, so it holds in both
    directions: renaming a script without updating the comment fails here, and
    pointing a comment at a path nobody created fails here too. That is the whole
    gap -- a named producer whose existence nothing asserts.

    Static and offline, like its siblings: it reads the recipe and stats a path.
    """
    rel = dockerfile.relative_to(ROOT)
    named = sorted(set(_NAMED_SCRIPT.findall(dockerfile.read_text(encoding="utf-8"))))
    missing = [p for p in named if not (ROOT / p).is_file()]
    assert not missing, (
        f"{rel} names build script(s) that do not exist: {missing}. A recipe citing "
        "an absent producer cannot be built from a clean checkout, and no lane runs "
        "`docker build` to notice -- which is exactly how this subtree merged "
        "unbuildable. Create the script, or stop naming it."
    )
    not_executable = [p for p in named if not os.access(ROOT / p, os.X_OK)]
    assert not not_executable, (
        f"{rel} names build script(s) that are not executable: {not_executable}. The "
        "recipe's own prose tells a reader to invoke them directly, so a "
        "non-executable mode makes the documented command fail for everyone"
    )


def test_the_named_script_guard_is_not_vacuous() -> None:
    """At least one recipe must actually name a script.

    The guard above passes trivially on a recipe that cites nothing, so a reworded
    comment could silence it without ever failing. This pins that the citation
    exists somewhere, which is what makes the parametrized assertion load-bearing.
    """
    cited = {
        path
        for dockerfile in _ALL_RECIPES
        for path in _NAMED_SCRIPT.findall(dockerfile.read_text(encoding="utf-8"))
    }
    assert cited, (
        "no recipe names a scripts/*.sh producer any more, so "
        "test_named_build_scripts_exist_and_are_executable checks nothing. Either "
        "restore the citation or delete both tests deliberately"
    )


# ---------------------------------------------------------------------------
# The bundle directory has to stay traversable.
#
# ``--chmod`` takes ONE octal and BuildKit applies it to every path the COPY
# creates -- including the destination directory, which a COPY into
# ``/app/crew-bundle/`` creates on the way to the files. A mode without the
# owner-execute bit therefore produces ``drw-r--r--`` on that directory, which
# the crew user cannot enter, and then no file inside it is reachable whatever
# its own mode says. The container fails at startup with
#
#     ConfigError: bundle check failed [manifest.json is readable JSON]:
#     /app/crew-bundle/manifest.json could not be read as JSON (Permission denied)
#
# which reads like a corrupt bundle and is a directory mode. Measured live.
#
# Static, like the rest of this file: it reads the Dockerfile text, so it needs
# no Docker daemon and cannot flake. A ratchet rather than a one-off, because
# tightening a payload's modes is exactly the kind of tidy-up that looks correct
# -- these are read-only JSON files that nothing executes -- and whose cost is
# invisible until a crew boots. ``Dockerfile.crew`` is SHARED by the Fargate and
# MicroVM lanes, so a regression here reaches every remote crew.

#: The path whose traversability the crew user depends on, and the recipes that
#: build it. Listed rather than discovered, so adding a recipe that writes this
#: directory is a deliberate edit here.
_BUNDLE_DIR = "/app/crew-bundle"
_BUNDLE_RECIPES: tuple[Path, ...] = (_CREW_RUNTIME / "Dockerfile.crew",)


def _chmod_of(inst: str) -> str:
    """The ``--chmod`` value on one COPY, or ``""`` when it carries none."""
    found = re.search(r"--chmod=(\d+)", inst)
    return found.group(1) if found else ""


def _creates_the_bundle_dir(dest: str) -> bool:
    """Whether a COPY to *dest* creates or writes inside the bundle directory.

    Both shapes count. ``/app/crew-bundle/`` creates the directory itself, and
    ``/app/crew-bundle/skills`` creates it as a parent -- so the mode on either
    line can be the one that makes it unenterable.
    """
    normalized = dest.rstrip("/")
    return normalized == _BUNDLE_DIR or normalized.startswith(f"{_BUNDLE_DIR}/")


@pytest.mark.parametrize("dockerfile", _BUNDLE_RECIPES, ids=_ids(_BUNDLE_RECIPES))
def test_the_bundle_stays_traversable_by_the_crew_user(dockerfile: Path) -> None:
    """Every COPY touching the bundle directory leaves it enterable.

    Asserted on the OWNER-execute bit specifically. The bundle is owned by root
    and read by the ``crew`` user, so the mode that matters for traversal is the
    one BuildKit stamps on a directory it creates, and a value without ``0o100``
    set is the failure this guards.
    """
    offenders: list[str] = []
    checked = 0
    for inst in _instructions(dockerfile):
        if not inst.startswith("COPY"):
            continue
        _sources, dest = _copy_sources_and_dest(inst)
        if not _creates_the_bundle_dir(dest):
            continue
        checked += 1
        mode = _chmod_of(inst)
        if not mode:
            # No --chmod at all is fine: the directory then takes the builder's
            # default, which is traversable. This line is only about an explicit
            # mode that removes the bit.
            continue
        if not int(mode, 8) & 0o100:
            offenders.append(f"{inst.splitlines()[0]}  ->  --chmod={mode}")
    assert checked, (
        f"no COPY in {dockerfile.name} writes {_BUNDLE_DIR}; this test is pinning "
        "nothing and the path or the recipe list moved"
    )
    assert not offenders, (
        f"{dockerfile.name} gives {_BUNDLE_DIR} a mode the crew user cannot enter. "
        "BuildKit applies one --chmod to every path the COPY creates, including the "
        "destination directory, so this produces drw-r--r-- and the supervisor fails "
        "with Permission denied on a bundle that is actually intact:\n  " + "\n  ".join(offenders)
    )


def test_the_traversability_guard_is_not_vacuous() -> None:
    """The guard catches the mode that broke a live crew.

    Without this, a rewrite of ``_creates_the_bundle_dir`` or ``_chmod_of`` that
    matched nothing would leave the test above passing on an empty set -- and the
    ``checked`` assertion only proves the lines were FOUND, not that a bad mode
    would be rejected.
    """
    assert _creates_the_bundle_dir("/app/crew-bundle/") is True
    assert _creates_the_bundle_dir("/app/crew-bundle/skills") is True
    assert _creates_the_bundle_dir("/app/other") is False
    assert _chmod_of("COPY --chmod=0644 a b /app/crew-bundle/") == "0644"
    assert _chmod_of("COPY a b /app/crew-bundle/") == ""
    # 0644 is the mode that produced drw-r--r-- on a real build; 0755 is the one
    # that works. The bit, not the number, is what the guard reads.
    assert not int("0644", 8) & 0o100
    assert int("0755", 8) & 0o100
