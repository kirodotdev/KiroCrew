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

:data:`CREW_DOCKERFILE`
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
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from kiro_crew.cloud.microvm.image import Recipe

logger = logging.getLogger(__name__)

#: The Dockerfile name a recipe zip must carry at its root. Lambda builds what it
#: finds there, so a zip with the file under a directory builds nothing and the
#: failure arrives minutes later as a build error rather than here as a refusal.
DOCKERFILE_NAME = "Dockerfile"

#: The crew runtime's own directory, located from this file rather than by
#: importing the AWS Control app, so reading a recipe does not pull that app's
#: import graph into ``cloud``.
RUNTIME_DIR = (
    Path(__file__).resolve().parents[2] / "apps" / "builtins" / "aws_control" / "crew" / "runtime"
)

#: The base recipe: the runtime, the AWS CLI, kiro-cli, every ``FROM`` pinned
#: inside it. Reused from the Fargate lane rather than copied.
BASE_DOCKERFILE = RUNTIME_DIR / "Dockerfile"

#: The crew layer: the bundle, and nothing else. Also the Fargate lane's own.
CREW_DOCKERFILE = RUNTIME_DIR / "Dockerfile.crew"

#: The MicroVM layer: the platform's prerequisites and the hook listener as PID 1.
#: The ONLY recipe this lane adds, and it adds nothing of ours -- the serving code
#: and the crew content are already in the image when it starts.
MICROVM_DOCKERFILE = RUNTIME_DIR / "Dockerfile.microvm"

#: The POSIX user the crew runs as inside the image, and therefore the user every
#: in-guest command must be wrapped in.
#:
#: ONE constant shared by the image and the registration, because the two have to
#: agree and the failure when they do not is remote: ``cloud/ssm.py`` wraps every
#: Run Command as ``sudo -u <run_as> -i <cmd>``, so a user the guest does not have
#: makes the command exit 1 with ``sudo: unknown user``. The dashboard's token
#: mint is such a command, and a failed mint takes the tunnel down with it -- so
#: the crew is online, registered and unreachable, and nothing in the control
#: plane says why.
#:
#: ``crew`` is the base image's own ``USER``, which is what the supervisor and its
#: children inherit. ``test_the_registration_and_the_image_agree`` reads it back
#: out of the base Dockerfile and out of the guest's own module, so a rename in
#: either place fails there rather than on a live VM.
GUEST_RUN_AS = "crew"

#: The port the crew's FRONT listens on inside the guest, which is what a
#: port-forward must target.
#:
#: Deliberately not 8080: the platform's lifecycle-hook listener owns that port,
#: and it is the only one the VM's public endpoint reaches. A constant of the
#: IMAGE rather than a launch parameter, and shared with the registration for the
#: same reason the run-as user is -- the instances registry defaults to the
#: gateway dashboard's port, and a headless crew serves nothing there, so the
#: default points every forward at a port nobody listens on.
#:
#: ``test_the_front_port_matches_the_image`` reads it back out of
#: ``Dockerfile.microvm``, so a change in either place fails there.
GUEST_FRONT_PORT = 8081

#: The guest's SSM agent configuration, which travels in the zip.
SSM_AGENT_CONFIG = RUNTIME_DIR / "microvm-ssm-agent.json"

#: The container package the image serves from.
CONTAINER_DIR = RUNTIME_DIR / "container"

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

    Used where the Fargate lane uses a base IMAGE digest, because this lane has
    no separately-built ARM64 base to take a digest of: MicroVMs are ARM64-only,
    an x86 machine cannot build that base without binfmt emulation, and the whole
    point of letting Lambda build is that nothing is pushed from a laptop. So the
    three recipes are concatenated and this digest covers their inputs instead --
    the Dockerfiles, the container package, the SSM config and the wheel. A change
    to any of them is a different base, which is exactly what the cache key needs.

    What it does NOT give up is the pinning the base recipe does itself: every
    ``FROM``, kiro-cli, the AWS CLI and gh stay pinned by digest inside that file.

    Names are hashed with the bytes so moving a file is a different digest, and
    the walk is sorted so two machines agree.
    """
    digest = hashlib.sha256()
    roots = [
        BASE_DOCKERFILE,
        CREW_DOCKERFILE,
        MICROVM_DOCKERFILE,
        CONTAINER_DIR,
        SSM_AGENT_CONFIG,
        wheel,
    ]
    for root in roots:
        files = (
            [root]
            if root.is_file()
            else sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        )
        for path in files:
            digest.update(path.name.encode("utf-8"))
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
    base = BASE_DOCKERFILE.read_text(encoding="utf-8")
    crew = CREW_DOCKERFILE.read_text(encoding="utf-8")
    micro = MICROVM_DOCKERFILE.read_text(encoding="utf-8")
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
#:
#: ``scripts/build_crew_base_image.sh`` already settled that a crew image does not
#: want these -- "this image is headless, the front process forwards a turn to the
#: backend's API over loopback and never serves the interface" -- and it gets there
#: by not BUILDING the assets first, relying on ``setup.py`` warning and continuing
#: when the tree has none. Its own comment allows that "a tree that already has one
#: ships it, harmlessly", which holds for a Docker image and not for a recipe zip:
#: measured at 53.26 MB of an 89.93 MB wheel against the 64 MiB ceiling above, so a
#: checkout that has ever built the dashboard cannot build a MicroVM image at all.
#: Since ``kirocrew pod provision`` builds it, that is most checkouts.
#:
#: Hence removed rather than merely not built: the only mechanism that holds for a
#: tree whose state this lane does not control.
DASHBOARD_PREFIX = "kiro_crew/static/"


class RecipeRefused(RuntimeError):
    """The recipe was not assembled, and the reason is one a caller must read."""


@dataclass(frozen=True)
class AssembledRecipe:
    """A zip on disk, and the digests that identify what is in it."""

    path: Path
    recipe: Recipe
    size_bytes: int


def bundle_digest_of(bundle_dir: Path) -> str:
    """The bundle's digest, from ``packaging.build``'s own function.

    Imported at call time rather than at module scope: ``cloud`` must not pull the
    AWS Control app's packaging graph in just by being imported, and this is the
    only function here that needs it.

    Delegated rather than computed, because this digest is half of the image
    cache's key. Two implementations of "what is this bundle" would be two
    functions deciding whether an image may be reused, and the looser one is the
    one that serves the wrong content.
    """
    from kiro_crew.apps.builtins.aws_control.crew.packaging.build import (
        bundle_digest as _bundle_digest,
    )

    if not bundle_dir.is_dir():
        raise RecipeRefused(f"no crew bundle at {bundle_dir}")
    try:
        return _bundle_digest(bundle_dir)
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

    The name the GUEST will serve. Inside the VM, ``hooks.supervisor_env`` sets
    ``SMC_CREW_NAME`` from this same manifest -- the one baked into the image --
    and the supervisor refuses to boot when the bundle's manifest and that
    variable disagree. The control plane needs the value for a different reason:
    a turn addresses a crew by name, and the launch tag is not that name. The tag
    is this launch's id, minted by the launcher; the name belongs to the bundle
    and is the operator's.

    Read from the operator's configured ``bundle_dir`` -- the same directory the
    image was built from -- rather than from the running VM, because the control
    plane needs it at launch, before the guest can be asked anything.

    Empty for every way the manifest is not readable, and the caller's answer to
    empty is to say the crew's name is not recorded. The alternative is sending
    the tag, which the guest's own addressing check answers with a 404 naming the
    crew it does serve: a crew the dashboard can see, connect and never talk to.
    """
    try:
        raw = (bundle_dir / "manifest.json").read_text(encoding="utf-8")
        document = json.loads(raw)
    except (OSError, ValueError):
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
    for source in (BASE_DOCKERFILE, CREW_DOCKERFILE, MICROVM_DOCKERFILE, SSM_AGENT_CONFIG):
        if not source.is_file():
            raise RecipeRefused(
                f"no {source.name} at {source}: this lane builds the Fargate lane's own "
                "recipes, so an absent one is a broken install rather than a thing to "
                "substitute for"
            )
    base_digest = base_recipe_digest(wheel)
    recipe = Recipe(
        base_image_arn=base_image_arn,
        base_digest=base_digest,
        bundle_digest=bundle_digest_of(bundle_dir),
    )
    dockerfile = assemble_dockerfile(base_digest)
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    # Deterministic by construction: every member is added in sorted order with a
    # fixed timestamp and mode, so the same inputs produce the same bytes. Not
    # cosmetic -- a zip that differs run to run makes the upload non-reproducible
    # and makes "did the recipe change" unanswerable from the artifact.
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_text(archive, DOCKERFILE_NAME, dockerfile)
        _write(archive, SSM_AGENT_CONFIG.name, SSM_AGENT_CONFIG)
        _write(archive, f"vendor/{wheel.name}", wheel)
        for member in sorted(
            path
            for path in CONTAINER_DIR.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        ):
            rel = member.relative_to(CONTAINER_DIR).as_posix()
            _write(archive, f"container/{rel}", member)
        bundle_members = sorted(_bundle_files(bundle_dir))
        for member in bundle_members:
            _write(archive, member.relative_to(bundle_dir).as_posix(), member)
        # A bundle with NO skills still has to produce a context that
        # ``Dockerfile.crew`` can copy. ``_bundle_files`` yields files only, and a
        # zip carries no empty directories, so ``skills/`` simply vanished from
        # the context and the build died on its own COPY:
        #
        #     #15 [stage-1 13/17] COPY --chmod=0755 skills /app/crew-bundle/skills
        #     #15 ERROR: failed to calculate checksum of ref ...: "/skills": not found
        #
        # ``packaging.build`` advertises exactly this bundle
        # ("Nothing private was selected: a valid bundle with the crew's persona
        # only"), so the empty case is the SUPPORTED one and the lane could not
        # build it -- the first launch of a crew that ships no skills failed, and
        # the burned image name is the recipe digest, so it failed permanently
        # for that content.
        #
        # A DIRECTORY entry, and never a placeholder file.
        #
        # The bundle's digest is signed over every file except the top-level
        # manifest, and the guest recomputes it the same way before installing --
        # so ANY file added here changes the recompute, the comparison fails, and
        # the supervisor refuses the bundle at boot. The crew then answers nothing
        # and every turn reports the crew as unreachable, which reads like a
        # network fault rather than a signature one.
        #
        # A genuine directory is the one entry both digests pass over: the
        # producer skips ``S_ISDIR`` because it has no bytes to hash, and the
        # guest's walk skips anything that is not a file. So this makes the path
        # exist in the build context without entering the signed set.
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


def _write(archive: zipfile.ZipFile, arcname: str, source: Path) -> None:
    """Add one file with fixed metadata, so the zip is byte-reproducible."""
    archive.writestr(_info(arcname), source.read_bytes())


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
