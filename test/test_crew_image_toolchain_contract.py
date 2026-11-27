"""The crew base image must keep a working CLI toolchain, and keep it pinned.

A crew container runs ``kiro-cli`` with ``--approval yolo``, so every tool it
calls runs unprompted -- which makes the set of tools present in the image the
actual boundary on what a crew can do. An image without ``git`` or ``aws``
still builds and still boots; the agent asked to touch a repository or a cloud
resource simply gets ``command not found``, at run time, with a crew already
started.

Two regressions are possible here and BOTH are silent -- the image still
builds, still boots, and still passes every functional gate:

**Dropping a tool.** ``--no-install-recommends`` means a package removed from
the apt operand list simply is not there. Nothing in the serving code imports
``git``, so no import error, no failing test, no smaller-image alarm: only a
crew, at run time, discovers the tool is gone.

**Purging a kept tool.** ``curl`` and ``xz-utils`` are part of the toolchain a
running crew uses, not build-time helpers that exist only to fetch
``kiro-cli``, so the recipe installs them and keeps them. A cleanup pass
reading the install layer alone sees a fetch-then-keep and can "tidy" it into a
fetch-then-purge, taking ``curl`` back out of the image.
:func:`test_kept_tools_are_never_purged` makes that boundary enforceable rather
than a comment someone has to notice.

**Why the pinning half is here too.** ``aws`` and ``gh`` are not in Debian
apt, so each is fetched from an upstream URL and must carry a pinned version
plus a per-architecture ``sha256``. A bump that updates the version and only
ONE of the two checksums breaks the build on exactly one architecture, which
on a single-arch CI run is invisible.
:func:`test_fetched_tools_pin_both_architectures` asserts the symmetry instead.

Static and offline: this reads only the Dockerfile text, so it needs no Docker
daemon and cannot flake -- the same shape as
``test_docker_wheel_layer_contract.py``, whose instruction parser it reuses.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_docker_wheel_layer_contract import _instructions

ROOT = Path(__file__).resolve().parents[1]

CREW_DOCKERFILE = (
    ROOT
    / "src"
    / "kiro_crew"
    / "apps"
    / "builtins"
    / "aws_control"
    / "crew"
    / "runtime"
    / "Dockerfile"
)

#: ``apt package -> the executable a crew actually calls``.
#:
#: The two names differ often enough that asserting only one of them is a real
#: hole: ``procps`` provides ``ps``, ``xz-utils`` provides ``xz``,
#: ``openssh-client`` provides ``ssh``. The package name is what the recipe
#: says, so that is what is asserted here; the executable is recorded beside it
#: so a reader can tell WHY the package is in the list, and so removing a
#: package cannot be defended as "nothing calls that".
APT_TOOLS: dict[str, str] = {
    "ca-certificates": "(TLS trust store)",
    "curl": "curl",
    "file": "file",
    "git": "git",
    "jq": "jq",
    "less": "less",
    "openssh-client": "ssh",
    "patch": "patch",
    "procps": "ps",
    "unzip": "unzip",
    "xz-utils": "xz",
}

#: Tools that are NOT in Debian apt and are therefore fetched from upstream,
#: mapped to the ``ARG`` prefix carrying their version and checksums.
#:
#: ``aws`` arrives as a ``COPY --from`` out of a throwaway builder stage rather
#: than an unpack in the final image, so its fetch and its presence are checked
#: through the ARG pins rather than by looking for a download in the final
#: stage.
FETCHED_TOOLS: dict[str, str] = {
    "kiro-cli": "KIRO",
    "aws": "AWSCLI",
    "gh": "GH",
}

#: Packages installed for the build and legitimately absent from the final
#: image. The builder stage unpacks the AWS CLI zip, so ``unzip`` there is
#: build-time-only -- but the FINAL stage installs ``unzip`` for the crew too,
#: which is why it appears in :data:`APT_TOOLS` as well. Nothing in the final
#: stage is build-time-only, which is the whole reason there is no purge.
BUILDER_ONLY_PURGE_EXEMPT: frozenset[str] = frozenset()


def _final_stage_instructions(dockerfile: Path) -> list[str]:
    """Instructions belonging to the LAST build stage only.

    A multi-stage recipe's earlier stages are discarded, so an ``apt-get
    install`` in the ``awscli-builder`` stage says nothing about what the crew
    can call. Splitting on the last ``FROM`` is what keeps this contract from
    passing on a tool that only ever existed in a throwaway stage.
    """
    instructions = _instructions(dockerfile)
    last_from = max(
        (i for i, inst in enumerate(instructions) if inst.upper().startswith("FROM ")),
        default=-1,
    )
    return instructions[last_from + 1 :]


def _apt_install_operands(instructions: list[str]) -> set[str]:
    """Every package named by an ``apt-get install`` in these instructions."""
    packages: set[str] = set()
    for inst in instructions:
        if "apt-get install" not in inst:
            continue
        tail = inst.split("apt-get install", 1)[1]
        # Stop at the next chained command: `&& rm -rf /var/lib/apt/lists/*`
        # names paths, not packages, and would otherwise be read as one.
        tail = re.split(r"&&|;", tail)[0]
        for token in tail.split():
            if token.startswith("-"):
                continue
            packages.add(token)
    return packages


def test_final_stage_installs_the_whole_toolchain() -> None:
    """Every tool a crew is expected to have is installed in the final stage."""
    installed = _apt_install_operands(_final_stage_instructions(CREW_DOCKERFILE))
    missing = {pkg: binary for pkg, binary in APT_TOOLS.items() if pkg not in installed}
    assert not missing, (
        "the crew base image no longer installs "
        + ", ".join(f"{pkg} (provides {binary})" for pkg, binary in sorted(missing.items()))
        + " -- a crew calling it gets `command not found` at run time, which no "
        "other gate catches"
    )


def test_kept_tools_are_never_purged() -> None:
    """No tool installed for the crew is removed again later in the recipe.

    ``curl`` and ``xz-utils`` are the ones at risk: they are the only kept
    packages that a fetch step also uses, so they read as build-time helpers to
    anyone scanning the install layer alone. They are toolchain, so any
    ``apt-get purge`` / ``apt-get remove`` naming a kept package is a defect.
    """
    offenders: dict[str, str] = {}
    for inst in _final_stage_instructions(CREW_DOCKERFILE):
        if not re.search(r"apt-get\s+(purge|remove)", inst):
            continue
        for pkg in APT_TOOLS:
            if pkg in BUILDER_ONLY_PURGE_EXEMPT:
                continue
            if re.search(rf"\b{re.escape(pkg)}\b", inst):
                offenders[pkg] = inst.strip()
    assert not offenders, (
        "the recipe installs these for the crew and then purges them again: "
        + ", ".join(sorted(offenders))
        + f" -- {sorted(offenders.values())[0][:120]!r}. curl and xz-utils are "
        "part of the toolchain, not fetch-only helpers; purging them takes the "
        "them puts the old design back and a crew loses the tool at run time"
    )


@pytest.mark.parametrize(
    ("tool", "arg_prefix"), sorted(FETCHED_TOOLS.items()), ids=sorted(FETCHED_TOOLS)
)
def test_fetched_tools_pin_both_architectures(tool: str, arg_prefix: str) -> None:
    """An upstream-fetched tool pins a version and BOTH per-arch checksums.

    A bump that refreshes the version and only one checksum breaks a single
    architecture, which a single-arch CI run cannot see.
    """
    text = CREW_DOCKERFILE.read_text(encoding="utf-8")
    required = {
        f"{arg_prefix}_VERSION": rf"^ARG\s+{arg_prefix}_VERSION=\S+",
        f"{arg_prefix}_SHA256_X86_64": rf"^ARG\s+{arg_prefix}_SHA256_X86_64=[0-9a-f]{{64}}\s*$",
        f"{arg_prefix}_SHA256_AARCH64": rf"^ARG\s+{arg_prefix}_SHA256_AARCH64=[0-9a-f]{{64}}\s*$",
    }
    missing = [
        name for name, pattern in required.items() if not re.search(pattern, text, re.MULTILINE)
    ]
    assert not missing, (
        f"{tool} is fetched from upstream but does not declare {', '.join(missing)} "
        "as a pinned ARG with a 64-hex sha256 -- an unpinned or half-pinned fetch "
        "means the build accepts an artifact nobody reviewed, on at least one "
        "architecture"
    )


def test_fetched_tools_verify_their_download() -> None:
    """Every pinned checksum is actually CHECKED, not merely declared.

    An ``ARG ..._SHA256_...`` that no ``sha256sum -c`` ever consumes is
    decoration: the build would accept a substituted artifact while the recipe
    still reads as pinned.
    """
    text = CREW_DOCKERFILE.read_text(encoding="utf-8")
    declared = set(re.findall(r"^ARG\s+(\w+_SHA256_\w+)=", text, re.MULTILINE))
    assert declared, "no per-arch sha256 ARGs found at all"

    checked_instructions = [
        inst for inst in _instructions(CREW_DOCKERFILE) if "sha256sum -c" in inst
    ]
    assert checked_instructions, "no instruction runs `sha256sum -c` -- nothing verifies a download"

    joined = " ".join(checked_instructions)
    # The RUN bodies reference the checksum through a shell variable assigned
    # from the ARG, so look for the ARG name appearing anywhere in a verifying
    # instruction rather than adjacent to sha256sum.
    unverified = sorted(name for name in declared if name not in joined)
    assert not unverified, (
        "these checksums are declared but never reach a `sha256sum -c`: "
        + ", ".join(unverified)
        + " -- the recipe reads as pinned while the build would accept a "
        "substituted artifact"
    )


#: An architecture token appearing literally in a download URL.
#:
#: Matched against the URL ITSELF, never against the whole instruction. Judging
#: the instruction makes this check vacuous: a fetch and its
#: ``case "${TARGETARCH}"`` dispatch are one backslash-continued instruction, so
#: "does the instruction mention TARGETARCH / _ARCH / case" is true even when the
#: URL beside it has ``x86_64`` baked in -- which is the regression this exists to
#: catch.
#:
#: The boundaries admit ``_`` as a DELIMITER rather than a word character.
#: ``gh_2.101.0_linux_amd64.tar.gz`` is the common upstream shape, so a
#: ``(?<![A-Za-z0-9_])`` boundary declines to match ``amd64`` there and passes a
#: baked-in arch. Both properties are pinned by mutating the real AWS CLI, gh and
#: kiro-cli URLs, not a synthetic one.
_LITERAL_ARCH_IN_URL = re.compile(r"(?<![A-Za-z0-9])(x86_64|aarch64|amd64|arm64)(?![A-Za-z0-9])")

#: A shell expansion, stripped before the arch scan so ``${AWS_ARCH}`` and
#: ``${TARGETARCH:-amd64}`` are not read as literal architectures. The default
#: inside a ``:-`` fallback is a resolved value, not a pinned one.
_EXPANSION = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*")

#: A URL literal in an instruction, up to the quote or whitespace ending it.
_URL = re.compile(r"https?://[^\s\"']+")


#: Instructions that PLACE a file, as opposed to ones that merely name a path.
#:
#: The distinction carries the assertion: the builder stage ends with
#: ``/usr/local/bin/aws --version``, which names the final path while installing
#: nothing into the final image, so matching a path alone would accept a tool that
#: exists only in a discarded stage.
_PLACES_A_FILE = re.compile(r"\b(ln\s+-s|install\s+-m|cp\s|mv\s)|^COPY\s", re.MULTILINE)

#: ``ln -s <target> <link>`` inside a final-stage instruction.
_SYMLINK = re.compile(r"ln\s+-s\s+(\S+)\s+(\S+)")


def _sub_commands(instruction: str) -> list[str]:
    """One shell command per element, so two conditions cannot be met apart.

    A ``RUN`` body is one logical instruction holding many commands, and one
    kiro-cli block both installs ``kiro-cli-chat`` and runs
    ``test -x /usr/local/bin/kiro-cli``. Asking "does this instruction place a
    file" and "does it name this path" separately is then satisfied by two
    different commands, and dropping the real ``install`` line changes neither
    answer. Splitting first is what forces both onto the same command.
    """
    return [part for part in re.split(r";|&&|\|\|", instruction) if part.strip()]


def _bin_path_placement(tool: str) -> re.Pattern[str]:
    """Match ``/usr/local/bin/<tool>`` as a WHOLE path, not as a prefix.

    A substring match collides: ``/usr/local/bin/aws`` is a prefix of
    ``aws_completer`` and ``/usr/local/bin/kiro-cli`` a prefix of
    ``kiro-cli-chat``, so a prefix test reports ``aws`` placed by the line that
    only links its completer, and ``kiro-cli`` placed by the line that only
    installs the chat binary.
    """
    return re.compile(re.escape(f"/usr/local/bin/{tool}") + r"(?![\w.-])")


#: Executables that must exist on PATH in the final image, whether or not they
#: carry pins of their own.
#:
#: ``kiro-cli-chat`` has no version or checksum of its own -- it ships inside the
#: same tarball as ``kiro-cli`` -- but it is the binary ``kiro-cli acp``
#: dispatches to, resolved as a SIBLING through PATH. An image holding only the
#: launcher builds, boots, and fails on the crew's first turn, so the placement
#: contract covers it even though the pinning contract cannot.
REQUIRED_BINARIES: tuple[str, ...] = ("kiro-cli", "kiro-cli-chat", "aws", "gh")


@pytest.mark.parametrize("tool", REQUIRED_BINARIES, ids=REQUIRED_BINARIES)
def test_required_binaries_reach_the_final_stage(tool: str) -> None:
    """Each required executable is placed on PATH in the final stage.

    ``aws`` is unpacked in a throwaway builder stage, so its pins say nothing
    about whether the installed tree is carried across: dropping the
    ``COPY --from`` leaves every checksum ARG intact and every pinning contract
    green while the final image has no ``aws`` at all.
    """
    wanted = _bin_path_placement(tool)
    placements = [
        command
        for inst in _final_stage_instructions(CREW_DOCKERFILE)
        for command in _sub_commands(inst)
        if wanted.search(command) and _PLACES_A_FILE.search(command.strip())
    ]
    assert placements, (
        f"no command in the FINAL stage places {tool} at /usr/local/bin/{tool} "
        "(via COPY, ln -s, install, cp or mv) -- a binary that exists only in a "
        "discarded builder stage, or that is never installed at all, is absent "
        "from the image while every pinning contract still reads green"
    )


def test_symlinked_tools_have_their_tree_copied_in() -> None:
    """A ``/usr/local/bin`` symlink points into a tree the final stage owns.

    ``aws`` is two instructions: the installed tree arrives by
    ``COPY --from=awscli-builder``, then ``/usr/local/bin/aws`` is linked into
    it. Asserting only the link accepts a dangling one -- the image builds, the
    path exists, and calling ``aws`` fails.
    """
    final = _final_stage_instructions(CREW_DOCKERFILE)
    copied = " ".join(inst for inst in final if inst.upper().startswith("COPY "))

    dangling: list[str] = []
    for inst in final:
        for target, link in _SYMLINK.findall(inst):
            if not link.startswith("/usr/local/bin/"):
                continue
            # The tree the link reaches into, e.g.
            # /usr/local/aws-cli/v2/current/bin/aws -> /usr/local/aws-cli
            parts = [p for p in target.split("/") if p]
            if len(parts) < 3:
                continue
            tree = "/" + "/".join(parts[:2])
            if tree not in copied:
                dangling.append(f"{link} -> {target} (needs {tree} in the final stage)")

    assert not dangling, (
        "these PATH symlinks point into a tree no COPY brings into the final "
        f"stage: {dangling} -- the link exists, the image builds, and the tool "
        "fails only when it is first called"
    )


def test_architecture_is_resolved_not_hardcoded() -> None:
    """Download URLs derive the architecture instead of baking one in.

    A hardcoded ``x86_64`` in a fetch URL builds and passes on amd64 and then
    installs an amd64 binary into an arm64 image, where it fails only when the
    tool is first called -- so a single-arch CI run cannot see it.
    """
    offenders: list[str] = []
    for inst in _instructions(CREW_DOCKERFILE):
        for url in _URL.findall(inst):
            if _LITERAL_ARCH_IN_URL.search(_EXPANSION.sub("", url)):
                offenders.append(url)

    assert not offenders, (
        "these download URLs name an architecture literally instead of "
        f"expanding one resolved from TARGETARCH: {offenders} -- the wrong-arch "
        "binary installs cleanly and fails only when the tool is first called, "
        "which a single-arch CI run cannot see"
    )
