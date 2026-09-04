"""The CI-built wheel must never be COPYed into a committed image layer.

Image layers are append-only. ``COPY dist/*.whl /tmp/wheels/`` commits the
~48MB wheel to its own layer, and an ``rm -rf /tmp/wheels`` in a LATER
instruction can only stack a whiteout on top -- the bytes stay in the layer
stack, so every ``docker pull`` fetches the wheel and then discards it, next
to the already-installed copy of the same code. Measured on the published
artifact (#5778): 48,047,081 compressed bytes, 5.7% of
``ghcr.io/kirodotdev/kirocrew:latest``; layer 6 contained exactly ``tmp/``,
``tmp/wheels/`` and the wheel, layer 7 exactly the ``tmp/.wh.wheels``
whiteout -- the proof the delete happened a layer too late.

The fix consumes the wheel through a BuildKit context bind mount
(``RUN --mount=type=bind,source=dist,target=/tmp/wheels``): the wheel is
visible only for the duration of that RUN and never enters a layer, which
makes the cleanup ``rm`` unnecessary rather than merely late.

Why a ratchet: a future "simplification" back to COPY would reintroduce the
dead weight SILENTLY -- the image still builds, boots, and passes every
functional smoke gate, just ~48MB heavier per pull. Static and offline: this
reads only the Dockerfile text, so it cannot flake and needs no Docker
daemon (same shape as ``test_workflow_cache_setup_uniqueness.py``).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "Dockerfile"


def _instructions() -> list[str]:
    """Logical Dockerfile instructions, continuations joined, comments dropped.

    The Dockerfile parser strips whole-line comments even inside a
    backslash-continued instruction, so mirror that here before joining.
    """
    logical: list[str] = []
    pending = ""
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
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


def _copy_sources_and_dest(inst: str) -> tuple[list[str], str]:
    """Split a COPY/ADD instruction into (source operands, destination).

    Tokens after the instruction name, minus ``--flag`` options; the last
    remaining token is the destination, the rest are sources.
    """
    operands = [t for t in inst.split()[1:] if not t.startswith("--")]
    return operands[:-1], operands[-1] if operands else ""


def _could_carry_the_wheel(src: str) -> bool:
    """Could this COPY/ADD source operand sweep the staged wheel in?

    ``.dockerignore``'s inverted allowlist admits ``dist/*.whl`` into the
    context, so any source that names the wheel, the dist/ tree, or the
    whole context root can commit the wheel to a layer.
    """
    normalized = src.lstrip("./")
    return src in {".", "./"} or normalized == "" or normalized.startswith("dist") or ".whl" in src


def test_dockerfile_exists() -> None:
    """Guard the guard: a moved Dockerfile would make the ratchet vacuous."""
    assert DOCKERFILE.is_file(), f"expected image recipe at {DOCKERFILE}"


def test_wheel_never_enters_a_committed_layer() -> None:
    """No COPY/ADD may bring the wheel (or a tree holding it) into the image."""
    offenders = []
    for inst in _instructions():
        if inst.split(maxsplit=1)[0].upper() not in {"COPY", "ADD"}:
            continue
        sources, dest = _copy_sources_and_dest(inst)
        if "/tmp/wheels" in dest or any(map(_could_carry_the_wheel, sources)):
            offenders.append(inst)
    assert not offenders, (
        "the CI wheel must reach pip via the bind mount, never COPY/ADD: a "
        "copied wheel is committed to its own layer and a later `rm` only "
        "adds a whiteout, shipping ~48MB of dead weight in every pull "
        "(#5778). Use RUN --mount=type=bind,source=dist,target=/tmp/wheels "
        "instead:\n  " + "\n  ".join(offenders)
    )


def test_wheel_is_consumed_via_context_bind_mount() -> None:
    """The install RUN keeps the mount AND the exactly-one-wheel guard."""
    runs = [i for i in _instructions() if i.upper().startswith("RUN")]
    install = [r for r in runs if "pip install" in r and "/tmp/wheels" in r]
    assert len(install) == 1, (
        f"expected exactly one wheel-installing RUN, found {len(install)}: "
        f"{install!r}. The wheel must be consumed from /tmp/wheels via the "
        "context bind mount -- never COPYed into a layer (#5778)"
    )
    run = install[0]
    # Option order inside --mount is not significant to BuildKit, so assert
    # the tokens independently rather than one order-sensitive literal.
    assert "--mount=" in run and all(
        token in run for token in ("type=bind", "source=dist", "target=/tmp/wheels")
    ), (
        "the wheel-installing RUN must bind-mount dist/ from the build "
        "context so the wheel never lands in a layer (#5778)"
    )
    assert "-eq 1" in run and "WHEEL_COUNT" in run, (
        "the exactly-one-wheel guard must survive: .dockerignore allowlists "
        "dist/*.whl into the context, and the guard is what keeps the glob "
        "version-agnostic while refusing an ambiguous multi-wheel context"
    )
