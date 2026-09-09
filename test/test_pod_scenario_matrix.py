"""The pod-scenario matrix must track which platforms actually have a pod backend.

That tier's platform coverage is decided by exactly ONE fact -- whether
``runtime.require_backend()`` finds a service manager on the host -- because no
scenario body and no job carries a platform branch. The matrix is therefore a pure
consequence of which backends exist, which is also why a comment saying a platform
is "deferred until a backend lands" cannot be trusted: the day one lands, the
comment is false and nothing notices.

So the deferral is expressed HERE, as an assertion, instead of in prose. Every
platform whose backend exists must either be in the matrix or be named in
:data:`PENDING_VALIDATION` with the reason it is not, and that reason is what a
reviewer reads instead of trusting a comment to still be true.

Deliberately a plain unit test rather than part of the E2E tier: it reads YAML and
imports nothing that needs a host with pods, so it runs on every shard, including
the ones that cannot run a scenario at all.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
NIGHTLY = ROOT / ".github" / "workflows" / "nightly.yml"

#: Runner label -> the backend module whose existence means that platform can run
#: pods. ``None`` means the backend lives in ``runtime`` itself (systemd).
BACKEND_FOR_RUNNER = {
    "ubuntu-latest": None,
    "macos-15": "kiro_crew.pod.launchd",
    "windows-latest": "kiro_crew.pod.windows",
}

#: Platforms whose backend EXISTS but which are deliberately not in the matrix yet.
#: Delete an entry and the assertion below demands the matrix entry -- which is the
#: point: this is the only place the deferral is recorded, so it cannot go stale
#: silently the way a comment does.
PENDING_VALIDATION = {
    "windows-latest": (
        "The Task Scheduler backend exists, so require_backend() succeeds here and "
        "the fixture would select it -- the old 'until a backend lands' reason is "
        "spent. What is missing is a validated RUN: the scenario suite has never "
        "executed on win32, and the nightly sets KIROCREW_E2E_REQUIRE=1, "
        "which turns each precondition skip into a failure. Adding the entry before "
        "one green run would red the nightly rather than cover the platform, and a "
        "red nightly is how the publish jobs downstream of it get skipped."
    ),
}


def _matrix_runners() -> list[str]:
    doc = yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))
    return list(doc["jobs"]["pod-scenarios"]["strategy"]["matrix"]["os"])


def _backend_exists(module: str | None) -> bool:
    if module is None:
        return True
    return importlib.util.find_spec(module) is not None


@pytest.mark.parametrize("runner", sorted(BACKEND_FOR_RUNNER))
def test_a_platform_with_a_backend_is_either_covered_or_explicitly_pending(runner: str) -> None:
    """No third option. A backend with neither a matrix entry nor a stated reason is
    a platform everyone believes is covered and nothing runs."""
    if not _backend_exists(BACKEND_FOR_RUNNER[runner]):
        pytest.skip(f"{runner} has no pod backend yet, so the matrix owes it nothing")

    assert runner in _matrix_runners() or runner in PENDING_VALIDATION, (
        f"{runner} has a pod backend, so require_backend() succeeds there and the "
        "scenario fixture would select it -- add it to nightly.yml's pod-scenarios "
        "matrix, or record here why it is held back"
    )


def test_a_pending_platform_is_really_absent_from_the_matrix() -> None:
    """The list must describe reality, or it is worse than no list.

    A platform that is BOTH listed as pending and present in the matrix means the
    coverage happened and the note was never removed, so the next reader is told
    something false by the very mechanism added to stop that.
    """
    covered = set(_matrix_runners())
    stale = sorted(covered & set(PENDING_VALIDATION))

    assert not stale, (
        f"{', '.join(stale)} is in the pod-scenarios matrix AND listed as pending "
        "validation; delete the PENDING_VALIDATION entry now that it runs"
    )


def test_every_pending_entry_gives_a_reason_that_could_be_acted_on() -> None:
    """A one-word reason ('later', 'TODO') is how a deferral becomes permanent."""
    for runner, reason in PENDING_VALIDATION.items():
        assert len(reason.split()) >= 12, (
            f"{runner}'s pending reason is too thin to act on: state what is "
            "missing and what would have to be true to remove the entry"
        )
