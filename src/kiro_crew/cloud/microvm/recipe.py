"""Zip the Fargate lane's own crew recipe so Lambda can build a MicroVM image from it.

The whole point of this module is that it is a JOIN rather than a second path. The
Fargate lane already answers "what is in a crew's image": an operator-built,
signed, deny-by-default bundle produced by ``packaging.build``, laid on a headless
crew runtime base by ``Dockerfile.crew``. This lane asks the same question and
takes the same answer. What differs is only the build TARGET -- Fargate runs
``docker build`` over the bundle directory and pushes the result to a registry,
and this lane zips the identical build context and calls ``CreateMicrovmImage`` so
Lambda runs the build instead.

So nothing here re-derives a bundle, a digest, a Dockerfile or a layout:

``runtime_path("CREW_DOCKERFILE")``
    is ``Dockerfile.crew`` itself, read from disk, not a MicroVM copy of it. A
    second Dockerfile would be a second answer to "what does a crew image
    contain", and the two would drift the first time either lane's base changed.

:data:`REQUIRED_BUNDLE_MEMBERS`
    is that Dockerfile's own ``COPY`` list. Checked here rather than left to the
    build, because a build run by Lambda reports a missing bundle member minutes
    later and in someone else's log.

:func:`bundle_digest_of`
    calls ``packaging.build``'s function rather than reimplementing it. It is what
    the image cache keys on, so a second implementation would mean two functions
    deciding whether an image may be reused -- and the looser one would be the one
    that served the wrong content.

The zip's members sit at its ROOT, with no ``bundle/`` prefix, for the same
reason: ``Dockerfile.crew``'s build context IS the bundle directory, and a prefix
would mean editing its ``COPY`` lines, which is how a copy starts.

What this module owns is narrow on purpose: assembling those into a zip, and
refusing to assemble one that could not produce a usable image.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from kiro_crew.cloud.microvm.image import Recipe, default_build_digest

logger = logging.getLogger(__name__)

#: The Dockerfile name a recipe zip must carry at its root. Lambda builds what it
#: finds there, so a zip with the file under a directory builds nothing and the
#: failure arrives minutes later as a build error rather than here as a refusal.
DOCKERFILE_NAME = "Dockerfile"

#: The crew runtime's files this lane reads, relative to the runtime directory of
#: whichever app provides the ``crew_runtime`` seam. Resolved per call through
#: :func:`runtime_path`, not at import, so ``cloud`` does not pull in that app's
#: import graph and still imports when no app provides one.
#:
#: ``BASE_DOCKERFILE`` -- the base recipe (runtime, AWS CLI, kiro-cli), shared with Fargate.
#: ``CREW_DOCKERFILE`` -- the crew layer: the bundle only, shared with Fargate.
#: ``MICROVM_DOCKERFILE`` -- the platform's prerequisites and the hook listener as PID 1.
#: ``SSM_AGENT_CONFIG`` -- the guest's SSM agent configuration, carried in the zip.
#: ``CONTAINER_DIR`` -- the container package the image serves from.
_RUNTIME_FILES: dict[str, str] = {
    "BASE_DOCKERFILE": "Dockerfile",
    "CREW_DOCKERFILE": "Dockerfile.crew",
    "MICROVM_DOCKERFILE": "Dockerfile.microvm",
    "SSM_AGENT_CONFIG": "microvm-ssm-agent.json",
    "CONTAINER_DIR": "container",
}


def runtime_dir() -> Path:
    """The crew runtime's directory, from the app that provides the ``crew_runtime`` seam.

    Refuses when no app provides one: every recipe this lane builds is that runtime's
    own, so there is nothing to substitute.
    """
    from kiro_crew.apps import seams

    runtime = seams.crew_runtime()
    if runtime is None:
        raise RecipeRefused(
            "no crew runtime is installed, so this lane has no recipe to build: the "
            "remote-crew image is built from the crew runtime an app provides through "
            "the crew_runtime seam (the aws-control app ships it)"
        )
    return runtime.runtime_dir


def runtime_path(name: str) -> Path:
    """The runtime file :data:`_RUNTIME_FILES` names *name*, or ``RUNTIME_DIR`` itself.

    A function rather than module attributes served by a module ``__getattr__``: the
    paths are computed from the seam on every call, and a module-level ``__getattr__``
    would make this module a mirror of a surface it does not re-export. An unknown
    name raises ``KeyError`` naming it.
    """
    if name == "RUNTIME_DIR":
        return runtime_dir()
    if name not in _RUNTIME_FILES:
        raise KeyError(f"{name!r} is not a crew runtime file")
    return runtime_dir() / _RUNTIME_FILES[name]


#: The POSIX user the crew runs as inside the image, and so the user every in-guest
#: command is wrapped in (``cloud/ssm.py`` runs ``sudo -u <run_as> -i <cmd>``).
#: Shared by the image and the registration because a user the guest lacks fails the
#: dashboard's token mint with ``sudo: unknown user``, leaving the crew registered
#: and unreachable. ``crew`` is the base image's own ``USER``;
#: ``TestTheRunAsUserIsOneValue`` reads it back out of the Dockerfile and the guest.
GUEST_RUN_AS = "crew"

#: The port the crew's FRONT listens on inside the guest: the port-forward target.
#: Not 8080, which the platform's lifecycle-hook listener owns. Shared with the
#: registration because the instances registry otherwise defaults to the gateway
#: dashboard's port, where a headless crew serves nothing.
#: ``test_the_front_port_matches_the_image`` reads it back out of ``Dockerfile.microvm``.
GUEST_FRONT_PORT = 8081

#: The crew layer's base indirection, which this lane replaces with the base's own
#: layers. Matched as whole lines so a comment mentioning either cannot be taken
#: for the directive.
_BASE_ARG = re.compile(r"^ARG\s+BASE\s*$", re.MULTILINE)
_BASE_FROM = re.compile(r"^FROM\s+\$\{BASE\}\s*$", re.MULTILINE)


def dashboard_members(wheel: Path) -> list[str]:
    """The dashboard-SPA members of ``wheel``, in the order the archive holds them.

    Read from the central directory, so asking costs no decompression. The caller
    decides what an answer means: :func:`strip_dashboard` removes them and
    :func:`assemble` refuses a wheel that still has them.
    """
    with zipfile.ZipFile(wheel) as archive:
        return [name for name in archive.namelist() if name.startswith(DASHBOARD_PREFIX)]


def strip_dashboard(wheel: Path) -> int:
    """Rewrite ``wheel`` in place without its dashboard assets. Returns bytes freed.

    Byte-stable for a given input, which is a requirement rather than a courtesy:
    :func:`base_recipe_digest` hashes this file, so a rewrite that varied run to run
    would miss the image cache on every build and pay for a rebuild each time. Each
    kept member therefore keeps its own timestamp, compression and mode, and the
    original member order is preserved.

    ``RECORD`` is rewritten too. It lists every installed file with a hash and a
    size, so leaving rows for members that are gone would describe a distribution
    that does not match itself -- which ``pip`` reads when it uninstalls.

    Writes a sibling temp file and replaces, so an interrupted run leaves the staged
    wheel either whole or absent, never half-rewritten.
    """
    before = wheel.stat().st_size
    scratch = wheel.with_name(wheel.name + ".stripping")
    try:
        with (
            zipfile.ZipFile(wheel) as source,
            zipfile.ZipFile(scratch, "w", compression=zipfile.ZIP_DEFLATED) as target,
        ):
            for info in source.infolist():
                if info.filename.startswith(DASHBOARD_PREFIX):
                    continue
                payload = source.read(info.filename)
                if info.filename.endswith(".dist-info/RECORD"):
                    payload = _record_without_dashboard(payload)
                # Carried over rather than rebuilt: a fresh ZipInfo would re-date
                # every member and change this file's bytes on each run.
                kept = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                kept.compress_type = info.compress_type
                kept.external_attr = info.external_attr
                kept.create_system = info.create_system
                target.writestr(kept, payload)
        scratch.replace(wheel)
    except BaseException:
        scratch.unlink(missing_ok=True)
        raise
    return before - wheel.stat().st_size


def _record_without_dashboard(record: bytes) -> bytes:
    """A wheel ``RECORD`` with its dashboard rows dropped.

    Parsed as the CSV it is rather than split on commas, because a path may be
    quoted. Line endings are normalised to ``\\n``, which is what ``wheel`` writes.
    """
    rows = csv.reader(io.StringIO(record.decode("utf-8"), newline=""))
    kept = [row for row in rows if not (row and row[0].startswith(DASHBOARD_PREFIX))]
    out = io.StringIO(newline="")
    csv.writer(out, lineterminator="\n").writerows(kept)
    return out.getvalue().encode("utf-8")


def base_recipe_digest(wheel: Path) -> str:
    """The identity of everything in the image that is not the crew's bundle.

    Stands in for the Fargate lane's base IMAGE digest: MicroVMs are ARM64-only,
    an x86 machine cannot build that base without emulation, and nothing is pushed
    from a laptop. So it covers the recipes' inputs instead -- the Dockerfiles, the
    container package, the SSM config and the wheel. The base recipe still pins
    every ``FROM``, kiro-cli, the AWS CLI and gh by digest itself.

    Each file is hashed under its path RELATIVE TO ITS ROOT: ``container/`` holds
    several ``__init__.py``, so a bare name cannot tell two files apart, and moving
    one between packages would change the image but not the digest. The walk is
    sorted and uses posix separators so every machine and OS agrees.
    """
    digest = hashlib.sha256()
    roots = [
        *(
            runtime_path(name)
            for name in (
                "BASE_DOCKERFILE",
                "CREW_DOCKERFILE",
                "MICROVM_DOCKERFILE",
                "CONTAINER_DIR",
                "SSM_AGENT_CONFIG",
            )
        ),
        wheel,
    ]
    for root in roots:
        files = (
            [root]
            if root.is_file()
            else sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        )
        for path in files:
            # Relative to the root's PARENT, so a single-file root hashes as its
            # own name and a directory root hashes as ``<dir>/<path inside it>``.
            # The wheel's name carries its version, which is why that one stays
            # part of the digest rather than being reduced to a fixed label.
            label = path.relative_to(root.parent).as_posix()
            digest.update(label.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def assemble_dockerfile(digest: str) -> str:
    """base recipe + crew layer (minus its base reference) + MicroVM layer.

    The concatenation is not a text trick: it removes exactly two lines
    (``ARG BASE`` / ``FROM ${BASE}``) and REFUSES if it does not find them, so a
    crew layer that stops starting from a base reference breaks this build rather
    than being silently mis-assembled.
    """
    base = runtime_path("BASE_DOCKERFILE").read_text(encoding="utf-8")
    crew = runtime_path("CREW_DOCKERFILE").read_text(encoding="utf-8")
    micro = runtime_path("MICROVM_DOCKERFILE").read_text(encoding="utf-8")
    if not _BASE_ARG.search(crew) or not _BASE_FROM.search(crew):
        raise RecipeRefused(
            "Dockerfile.crew no longer starts 'ARG BASE' / 'FROM ${BASE}'. This lane "
            "replaces that indirection with the base's own layers, so it cannot assemble "
            "a recipe whose shape it does not recognise. Re-read Dockerfile.crew and "
            "update this function deliberately."
        )
    crew = _BASE_FROM.sub("", _BASE_ARG.sub("", crew))
    # ${BASE} is also the value of the OCI base.name label, and with no registry
    # reference to put there, naming the recipe digest is the honest answer: it
    # says which base layers are in the image, which is what the label is for.
    # Substituted as a LITERAL rather than left as a build arg, because an
    # undeclared arg expands to an empty string and an empty base.name label is
    # the defect Dockerfile.crew's own comment records being caught once.
    crew = crew.replace("${BASE}", digest)
    return (
        "# GENERATED by kiro_crew.cloud.microvm.recipe -- do not edit.\n"
        "# base recipe + crew layer + MicroVM layer, concatenated because this lane has\n"
        "# no registry to hold a separately-built ARM64 base. See that module.\n\n"
        + base
        + "\n\n# ---- crew layer (runtime/Dockerfile.crew) ----\n\n"
        + crew
        + "\n\n# ---- MicroVM layer (runtime/Dockerfile.microvm) ----\n\n"
        + micro
    )


#: What ``Dockerfile.crew`` copies, and therefore what a bundle must carry. Taken
#: from its four explicit ``COPY`` sources: three files and the skills directory,
#: which may be empty but must exist. A bundle short one of these builds an image
#: whose crew is partly absent, and that failure shows up as a crew that cannot
#: serve rather than as a build error.
REQUIRED_BUNDLE_MEMBERS = ("manifest.json", "agent.json", "mcp.json")

#: The one required directory. Separate from the files above because an empty
#: directory is valid here and an empty file is not.
REQUIRED_BUNDLE_DIR = "skills"

#: Ceiling on the assembled zip. A crew bundle is settings, skills and agent specs
#: -- kilobytes to a few megabytes -- so this is generous; it exists because a
#: recipe is uploaded on every cache miss and an unbounded one is an unbounded
#: upload charged to the owner.
MAX_RECIPE_BYTES = 64 * 1024 * 1024

#: Wheel members a headless crew never reads: the dashboard single-page app.
#: Removed rather than merely not built, because a checkout that ever built the
#: dashboard (``kirocrew pod provision`` does) ships it in the wheel: 53.26 MB of an
#: 89.93 MB wheel, over the 64 MiB recipe ceiling above.
DASHBOARD_PREFIX = "kiro_crew/static/"


class RecipeRefused(RuntimeError):
    """The recipe was not assembled, and the reason is one a caller must read."""


#: Top-level names the runtime writes into the build context. A bundle file at
#: one of them would land in the same ``COPY`` as the runtime's own code, and the
#: guest's hook listener runs as root from ``container/``: a bundle carrying
#: ``container/microvm/hooks/__init__.py`` would be imported in place of
#: ``hooks.py``. So a bundle may not write under these names at all.
RUNTIME_OWNED_PREFIXES = ("container/", "vendor/")


def _refuse_runtime_names(bundle_dir: Path, members: list[Path]) -> None:
    """Raise :class:`RecipeRefused` if a bundle file would take a runtime name."""
    # The Dockerfile has its own, earlier refusal with a more specific message.
    reserved = {DOCKERFILE_NAME, _RUNTIME_FILES["SSM_AGENT_CONFIG"]}
    for member in members:
        rel = member.relative_to(bundle_dir).as_posix()
        if rel in reserved or rel.startswith(RUNTIME_OWNED_PREFIXES):
            raise RecipeRefused(
                f"bundle file {rel!r} uses a name the runtime writes into the image "
                f"({', '.join(sorted(reserved))} or {', '.join(RUNTIME_OWNED_PREFIXES)}); "
                "rename it inside the bundle"
            )


@dataclass(frozen=True)
class AssembledRecipe:
    """A zip on disk, and the digests that identify what is in it."""

    path: Path
    recipe: Recipe
    size_bytes: int


def bundle_digest_of(bundle_dir: Path) -> str:
    """The bundle's digest, from ``packaging.build``'s own function.

    Asked of the app that provides the ``crew_runtime`` seam, which ships the bundle
    builder beside the runtime, and only when called: ``cloud`` must not pull that
    app's packaging graph in just by being imported, and this is the only function
    here that needs it. Refused when no app provides the seam.

    Delegated rather than computed, because this digest is half of the image
    cache's key. Two implementations of "what is this bundle" would be two
    functions deciding whether an image may be reused, and the looser one is the
    one that serves the wrong content.
    """
    from kiro_crew.apps import seams

    runtime = seams.crew_runtime()
    if runtime is None:
        raise RecipeRefused(
            "no crew runtime is installed, so no bundle digest can be computed: the "
            "bundle builder ships with the app that provides the crew_runtime seam"
        )
    if not bundle_dir.is_dir():
        raise RecipeRefused(f"no crew bundle at {bundle_dir}")
    try:
        return runtime.bundle_digest(bundle_dir)
    except Exception as exc:  # noqa: BLE001 - the owner's refusal, in our type
        # Re-raised as THIS module's type, not swallowed. ``packaging.build``
        # refuses a bundle it cannot sign -- a link it would have to follow, a
        # file above its ceiling -- and those refusals are this lane's refusals
        # too. Letting its exception type escape would make every caller here
        # catch two unrelated classes to handle one outcome, and the one that
        # forgot would crash a launch instead of reporting a bad bundle.
        if type(exc).__name__ == "ExportRefused":
            raise RecipeRefused(
                f"the crew bundle at {bundle_dir} cannot be signed, so no image may be built "
                f"from it: {exc}"
            ) from exc
        raise


def bundle_crew_name(bundle_dir: Path) -> str:
    """The crew name this bundle's manifest declares, or ``""``.

    The name the GUEST will serve (``hooks.supervisor_env`` sets ``SMC_CREW_NAME``
    from the same manifest). A turn addresses a crew by this name, not by the
    launch tag. Read from ``bundle_dir`` because the control plane needs it at
    launch, before the guest can be asked anything.

    Empty when the manifest is unreadable; the caller then records no name rather
    than sending the tag, which the guest would answer with a 404.

    Read through :func:`_read_bundle_member` because this runs before the snapshot
    that validates the bundle: a ``manifest.json`` linked to a credential file must
    not be opened here either.
    """
    try:
        raw = _read_bundle_member(bundle_dir, bundle_dir / "manifest.json")
        document = json.loads(raw.decode("utf-8"))
    except (RecipeRefused, OSError, ValueError):
        return ""
    if not isinstance(document, dict):
        return ""
    return str(document.get("crew_name") or "")


def check_layout(bundle_dir: Path) -> None:
    """Refuse a bundle ``Dockerfile.crew`` could not build from.

    Checked against that Dockerfile's own ``COPY`` list, here rather than in the
    build, because the build runs on Lambda's capacity: a missing member there is
    minutes of wait and an error in a log the operator does not hold.
    """
    if not bundle_dir.is_dir():
        raise RecipeRefused(f"no crew bundle at {bundle_dir}")
    missing = [name for name in REQUIRED_BUNDLE_MEMBERS if not (bundle_dir / name).is_file()]
    if not (bundle_dir / REQUIRED_BUNDLE_DIR).is_dir():
        missing.append(f"{REQUIRED_BUNDLE_DIR}/")
    if missing:
        raise RecipeRefused(
            f"the crew bundle at {bundle_dir} is missing {missing!r}, which Dockerfile.crew "
            "copies by name. Re-run packaging.build rather than assembling a recipe whose "
            "build fails on Lambda's capacity minutes from now"
        )
    if (bundle_dir / DOCKERFILE_NAME).exists():
        raise RecipeRefused(
            f"the crew bundle at {bundle_dir} carries its own {DOCKERFILE_NAME}, which would "
            f"collide with the one this recipe puts at the zip's root; refusing rather than "
            "letting whichever was written last decide what gets built"
        )


def assemble(
    *,
    bundle_dir: Path,
    wheel: Path,
    base_image_arn: str,
    out_zip: Path,
    account_id: str = "",
    region: str = "",
) -> AssembledRecipe:
    """Build the recipe zip for *bundle_dir*, and the :class:`Recipe` naming it.

    The zip is the Fargate lane's own build context plus what the platform needs
    to start the guest:

    ======================================  ==========================================
    ``Dockerfile``                          base + crew + MicroVM recipes, concatenated
    ``manifest.json`` ``agent.json``        the bundle, at the context root, which is
    ``mcp.json`` ``skills/**``              the layout ``Dockerfile.crew`` asserts
    ``container/**``                        the serving code the base installs
    ``vendor/<wheel>``                      the Kiro Crew wheel that base installs
    ``microvm-ssm-agent.json``              the guest's SSM agent configuration
    ======================================  ==========================================

    The digests are computed before anything is written, so a caller can ask what
    the image WOULD be identified as without paying for an upload -- which is what
    lets the cache be consulted before a zip is assembled at all.

    *account_id* and *region* are the account and region the guest's ``/run`` will
    accept a payload for. They go in NOTHING this function writes -- they are baked
    into the image's environment by ``CreateMicrovmImage``, not carried in the zip --
    and they are taken here only so the recipe's own digest covers them. Both
    default to empty, which is an unpinned image; a launch always supplies them.
    """
    check_layout(bundle_dir)
    if not wheel.is_file():
        raise RecipeRefused(
            f"no Kiro Crew wheel at {wheel}: the base recipe installs it from the build "
            "context, so a zip without one builds an image with no serving code"
        )
    try:
        carried = dashboard_members(wheel)
    except zipfile.BadZipFile as exc:
        raise RecipeRefused(
            f"the staged wheel {wheel.name} is not a readable zip ({exc}); a wheel is one, so "
            "this would fail inside the image build on Lambda where the log is harder to reach"
        ) from exc
    if carried:
        raise RecipeRefused(
            f"the staged wheel {wheel.name} carries {len(carried)} dashboard asset(s) under "
            f"{DASHBOARD_PREFIX}, which this lane's crew never serves and which on their own "
            f"put the zip over its {MAX_RECIPE_BYTES}-byte ceiling. Stage the wheel with "
            "scripts/build_microvm_image_zip.py, which removes them, rather than installing a "
            "wheel built for a desktop. Refused here because the size alone would report a "
            "byte count and not this cause"
        )
    for source in (
        runtime_path(name)
        for name in ("BASE_DOCKERFILE", "CREW_DOCKERFILE", "MICROVM_DOCKERFILE", "SSM_AGENT_CONFIG")
    ):
        if not source.is_file():
            raise RecipeRefused(
                f"no {source.name} at {source}: this lane builds the Fargate lane's own "
                "recipes, so an absent one is a broken install rather than a thing to "
                "substitute for"
            )
    base_digest = base_recipe_digest(wheel)
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    # ONE read of the bundle, and both the digest and the zip come from it. Hashing
    # the live directory and then archiving it again would let a rebuild swap the
    # bundle in between, and the image built from the new bytes would be cached
    # under the old bundle's digest.
    snapshot = _snapshot_bundle(bundle_dir, out_zip.parent)
    try:
        return _assemble_from_snapshot(
            snapshot,
            wheel=wheel,
            base_image_arn=base_image_arn,
            base_digest=base_digest,
            out_zip=out_zip,
            account_id=account_id,
            region=region,
        )
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)


def _assemble_from_snapshot(
    bundle_dir: Path,
    *,
    wheel: Path,
    base_image_arn: str,
    base_digest: str,
    out_zip: Path,
    account_id: str,
    region: str,
) -> AssembledRecipe:
    """:func:`assemble`'s digest and zip, both from the snapshot at *bundle_dir*."""
    recipe = Recipe(
        base_image_arn=base_image_arn,
        base_digest=base_digest,
        bundle_digest=bundle_digest_of(bundle_dir),
        # The build settings are not in this zip and are sent beside it, so they
        # are stamped on here: a recipe that did not carry them would be an image
        # identity that ignores the guest environment, the memory floor, the
        # architecture, the hook switches and the account and region the guest
        # refuses a payload outside. ``ImageResolver`` refuses a recipe whose stamp
        # is not the one it is about to send.
        build_digest=default_build_digest(account_id=account_id, region=region),
    )
    bundle_members = sorted(_bundle_files(bundle_dir))
    dockerfile = assemble_dockerfile(base_digest)
    # Deterministic by construction: every member is added in sorted order with a
    # fixed timestamp and mode, so the same inputs produce the same bytes. Not
    # cosmetic -- a zip that differs run to run makes the upload non-reproducible
    # and makes "did the recipe change" unanswerable from the artifact.
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_text(archive, DOCKERFILE_NAME, dockerfile)
        ssm_agent_config = runtime_path("SSM_AGENT_CONFIG")
        container_dir = runtime_path("CONTAINER_DIR")
        _write(archive, ssm_agent_config.name, ssm_agent_config)
        _write(archive, f"vendor/{wheel.name}", wheel)
        for member in sorted(
            path
            for path in container_dir.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        ):
            rel = member.relative_to(container_dir).as_posix()
            _write(archive, f"container/{rel}", member)
        for member in bundle_members:
            _write_bundle_member(archive, bundle_dir, member)
        # A bundle with NO skills (a supported bundle) still needs ``skills/`` in the
        # context, or ``Dockerfile.crew``'s COPY fails with '"/skills": not found'. A
        # zip carries no empty directories, so write one explicitly.
        #
        # A DIRECTORY entry, never a placeholder file: the bundle digest is signed
        # over every file, so an added file makes the guest refuse the bundle at boot.
        # Both digest walks skip directories, so this stays outside the signed set.
        has_skills = any(
            m.relative_to(bundle_dir).parts[0] == REQUIRED_BUNDLE_DIR for m in bundle_members
        )
        if not has_skills:
            _write_dir(archive, REQUIRED_BUNDLE_DIR)
    size = out_zip.stat().st_size
    if size > MAX_RECIPE_BYTES:
        out_zip.unlink(missing_ok=True)
        raise RecipeRefused(
            f"the recipe zip is {size} bytes, over the {MAX_RECIPE_BYTES}-byte ceiling; "
            "refusing before the upload rather than after paying for it"
        )
    logger.info(
        "assembled MicroVM recipe %s (%d bytes) for bundle %s on base %s",
        out_zip,
        size,
        recipe.bundle_digest,
        base_digest,
    )
    return AssembledRecipe(path=out_zip, recipe=recipe, size_bytes=size)


def _write_dir(archive: zipfile.ZipFile, arcname: str) -> None:
    """Store an empty DIRECTORY, so a path can exist without a file in it.

    Zip represents a directory as a zero-length entry whose name ends in ``/``
    and whose external attributes carry the directory bit. Both matter: the
    trailing slash is what an extractor reads as "make this a directory", and the
    mode is what makes it traversable once created.

    Shares :func:`_info`'s fixed timestamp for the reason that function gives --
    the zip's bytes key the image cache, so anything varying per run misses it --
    and then overrides the mode, because a directory needs its execute bit and a
    file does not.
    """
    info = _info(f"{arcname.rstrip('/')}/")
    # The directory bit plus rwxr-xr-x. ``0x10`` is the MS-DOS directory flag,
    # which some extractors read instead of the unix mode, so both are set.
    info.external_attr = (0o40755 << 16) | 0x10
    archive.writestr(info, b"")


def _write_text(archive: zipfile.ZipFile, arcname: str, text: str) -> None:
    """Add one generated member with the same fixed metadata a file gets."""
    archive.writestr(_info(arcname), text.encode("utf-8"))


def _bundle_files(bundle_dir: Path) -> list[Path]:
    """Every regular file in the bundle, REFUSING any redirecting entry.

    A symlink or junction is refused, not skipped, and that is the same rule
    ``packaging.build``'s digest applies -- for the same reason, which matters
    more here than there. A skipped entry still SHIPS: ``zipfile.write`` follows
    a link and stores the bytes it points at, so a bundle carrying
    ``agent.json -> ~/.aws/credentials`` would travel those bytes into a zip
    uploaded to S3 and baked into an image. Refusing is also what keeps the two
    in step: a redirect the digest will not sign must not be a redirect this zip
    carries.

    Skipping a genuine DIRECTORY is the only pass, because the walk descends into
    it on its own.
    """
    out: list[Path] = []
    for path in bundle_dir.rglob("*"):
        if path.is_symlink():
            raise RecipeRefused(
                f"the crew bundle entry {path.relative_to(bundle_dir).as_posix()} is a link; "
                "refusing to assemble a recipe that would travel bytes from outside the "
                f"bundle. packaging.build refuses to sign it for the same reason -- re-run the "
                "build."
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise RecipeRefused(
                f"the crew bundle entry {path.relative_to(bundle_dir).as_posix()} is not a "
                "regular file, so what it would contribute to the image cannot be stated"
            )
        out.append(path)
    return out


def _snapshot_bundle(bundle_dir: Path, parent: Path) -> Path:
    """Copy every bundle file into a fresh private directory under *parent*.

    Each file is read once through :func:`_read_bundle_member`, and the copy is
    what the digest and the zip are then taken from. *parent* is the directory
    the zip is written in, which a launch cuts under the masked staging root.
    """
    # The signer's own refusals (a link or junction, a special file) first, with
    # their own messages; the digest itself is taken from the snapshot below.
    bundle_digest_of(bundle_dir)
    members = sorted(_bundle_files(bundle_dir))
    _refuse_runtime_names(bundle_dir, members)
    snapshot = Path(tempfile.mkdtemp(prefix=".bundle-snapshot-", dir=str(parent)))
    try:
        for member in members:
            rel = member.relative_to(bundle_dir)
            data = _read_bundle_member(bundle_dir, member)
            target = snapshot / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
    except BaseException:
        shutil.rmtree(snapshot, ignore_errors=True)
        raise
    return snapshot


def _write(archive: zipfile.ZipFile, arcname: str, source: Path) -> None:
    """Add one file with fixed metadata, so the zip is byte-reproducible."""
    archive.writestr(_info(arcname), source.read_bytes())


def _read_bundle_member(bundle_dir: Path, source: Path) -> bytes:
    """Read one BUNDLE file through the credential-file guard.

    The bundle is operator-supplied, so a member is read with
    :func:`kiro_crew.hooks.safe_read_file_bytes_nolink`: the
    descriptor that is checked is the one that is read, a hardlinked inode is
    refused, the opened file must resolve inside the bundle, and a sensitive path
    is refused. A plain ``read_bytes`` would follow a member hardlinked to, say,
    ``~/.aws/credentials`` and upload those bytes in the recipe.
    """
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink

    rel = source.relative_to(bundle_dir).as_posix()
    try:
        # The reader's own per-file cap; the whole zip is bounded separately by
        # MAX_RECIPE_BYTES once it is written.
        data = safe_read_file_bytes_nolink(str(source), str(bundle_dir))
    except FileTooLargeError as exc:
        raise RecipeRefused(f"the crew bundle entry {rel} is too large to read: {exc}") from exc
    if data is None:
        raise RecipeRefused(
            f"the crew bundle entry {rel} could not be read safely (a hardlink, a "
            "link out of the bundle, a sensitive file or an unreadable file), so it "
            "is not put in the recipe"
        )
    return data


def _write_bundle_member(archive: zipfile.ZipFile, bundle_dir: Path, source: Path) -> None:
    """Add one file from the bundle snapshot.

    *bundle_dir* is the private snapshot :func:`_snapshot_bundle` wrote, whose
    files were already read through the guard; it sits under the masked staging
    root, which the guard itself treats as sensitive, so it is read directly.
    """
    archive.writestr(_info(source.relative_to(bundle_dir).as_posix()), source.read_bytes())


def _info(arcname: str) -> zipfile.ZipInfo:
    """One member's metadata, fixed rather than read off the builder's disk.

    The timestamp and the mode are both constants. A mode that varied with the
    builder's umask, or a timestamp that varied with the clock, would make the
    zip vary run to run -- and the whole point of keying the image cache on the
    recipe is that identical inputs produce an identical artifact.

    ``0o644`` here is the ZIP member's mode and has nothing to do with what the
    image sees: ``Dockerfile.crew`` sets the in-image modes itself with
    ``COPY --chmod``, because the mode a bundle file carries on the operator's
    disk is not one the crew user can read.
    """
    info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info


#: Assembled recipes by digest, so :func:`uploader` can find the zip that
#: :func:`assemble` produced without the two having to be wired through
#: ``ImageResolver``'s narrow ``upload_recipe`` callable. Process-local and small:
#: one entry per distinct bundle a launch assembled in this process.
_STAGED: dict[str, Path] = {}


def remember(assembled: AssembledRecipe) -> AssembledRecipe:
    """Record an assembled zip so :func:`uploader` can find it by digest."""
    _STAGED[assembled.recipe.digest()] = assembled.path
    return assembled


def forget_all() -> None:
    """Drop every remembered zip. For a test that must not see another's."""
    _STAGED.clear()


def uploader(
    *,
    bucket: str,
    put_object: Callable[[str, str, Path], None],
    key_prefix: str = "recipes",
) -> Callable[[Recipe], str]:
    """An ``upload_recipe`` for :class:`~kiro_crew.cloud.microvm.image.ImageResolver`.

    Keyed by the recipe digest, so the uploaded object is named by what is in it
    and a re-upload of identical content overwrites itself rather than
    accumulating. The resolver only calls this on a cache MISS, so an image that
    is reused costs no upload at all.

    ``put_object`` is injected because the S3 call belongs to the lane's own AWS
    seam and this module should not be a second place that makes one.
    """

    def upload(recipe: Recipe) -> str:
        key = f"{key_prefix}/{recipe.digest()}.zip"
        staged = _staged_path_for(recipe)
        if staged is None:
            raise RecipeRefused(
                f"no assembled recipe for {recipe.digest()}: assemble() must run before "
                "the resolver asks for an upload"
            )
        put_object(bucket, key, staged)
        return f"s3://{bucket}/{key}"

    return upload


def _staged_path_for(recipe: Recipe) -> Optional[Path]:
    path = _STAGED.get(recipe.digest())
    if path is None or not path.is_file():
        return None
    return path
