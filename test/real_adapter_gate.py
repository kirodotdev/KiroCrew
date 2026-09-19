"""The gate the real-adapter contract tests stand on, and the versions they measured.

A test that measures a real adapter has two audiences with opposite needs. A
developer without the adapter installed needs it to skip. The CI lane that exists
to run it needs an absent adapter to be RED, because a lane whose only guard
skipped asserted nothing while reporting success -- which is the whole reason a
skip-only guard is worth replacing.

``KIROCREW_E2E_REQUIRE=1`` separates them, and it is deliberately the switch this
repository already has rather than a second spelling of it:
``test/e2e/scenarios/conftest.py`` reads the same name for the same contract ("skip
on a missing precondition, or FAIL when the job declared the precondition must
hold"), as do the Playwright gate and the gateway-boot matrix. The name says E2E
and this lane is not E2E, but one name for one mechanism is the property worth
keeping: a job sets it for its own module set, and here that set is the guarded
contract tests. Unset, which is every local run, an absent adapter skips exactly as
a ``skipif`` did.

The flag is not the only thing holding the rule. The lane also asserts on its junit
report that no selected case was skipped, so a run where the variable never arrived
cannot pass by skipping -- and every guarded test carries the ``real_adapter``
marker the lane selects on, so a new one is picked up rather than silently left out.

The versions are the pins, and they live in ONE place: ``test/real_adapters/``
holds a small npm manifest with an exact version per adapter and its lockfile. The
lane runs ``npm ci`` there, so what it installs is the whole locked tree --
top-level and transitive -- and a cache miss cannot change what is measured. This
module reads the manifest so the tests and the lane cannot disagree about which
release the assertions were measured against, and the lane asserts the installed
package against the same number.

A bump arrives as its own pull request: Dependabot refreshes that manifest weekly,
and THAT PR's run of the lane is where adapter drift shows up -- red in a change
that carries nothing but the new release, where the answer is to re-measure and
adjust the assertions, never to relax one to match whatever the new tree does. To
bump by hand: edit the exact version, run ``npm install --package-lock-only`` in
that directory, and commit both files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

#: The repository's one switch for "this job declared its preconditions must hold",
#: shared with the E2E suites rather than duplicated. Parsed exactly as they parse
#: it: ``== "1"``.
REQUIRE_ENV = "KIROCREW_E2E_REQUIRE"

#: The marker the lane selects on. Every guarded test carries it, so the lane's
#: collection does not enumerate node ids and a test added later is included.
MARKER = "real_adapter"

#: The locked npm manifest the lane installs from. One exact version per adapter.
MANIFEST_DIR = Path(__file__).resolve().parent / "real_adapters"
MANIFEST = MANIFEST_DIR / "package.json"
LOCKFILE = MANIFEST_DIR / "package-lock.json"

#: The npm package each guarded file's adapter ships as.
CODEX_ACP_PACKAGE = "@agentclientprotocol/codex-acp"
OPENCODE_PACKAGE = "opencode-ai"


def pinned_version(package: str) -> str:
    """The exact version *package* is pinned to in the manifest.

    Exact means exact: a range or a tag is not a version anyone can re-measure
    against, so anything but three dotted integers is refused here rather than
    installed.
    """
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    version = manifest["dependencies"][package]
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"{MANIFEST}: {package} is pinned to {version!r}, not an exact release")
    return version


#: The ``@agentclientprotocol/codex-acp`` release the live measurements in
#: ``test_codex_session_mcp.py`` were taken against.
MEASURED_CODEX_ACP_VERSION = pinned_version(CODEX_ACP_PACKAGE)

#: The ``opencode-ai`` release the live measurements in
#: ``test_opencode_session_mcp.py`` were taken against.
MEASURED_OPENCODE_VERSION = pinned_version(OPENCODE_PACKAGE)


def real_adapters_required() -> bool:
    """Whether an absent adapter must fail rather than skip."""
    return os.environ.get(REQUIRE_ENV, "") == "1"


def require_real_adapter(resolved: object, *, what: str, install: str) -> None:
    """Stop the calling test unless *resolved* names an installed *what*.

    *resolved* is whatever the test's own resolver answered -- a path, an argv, a
    binary name -- and anything falsy means absent. The resolver stays the test's,
    so the gate never introduces a second opinion about what a real session would
    spawn.

    Returns for a present adapter. Otherwise skips, or fails where the job declared
    the adapter must be there, with *install* named either way: the reader of a red
    lane and the reader of a local skip both want the same command.
    """
    if resolved:
        return
    absent = f"{what} is not installed"
    if real_adapters_required():
        pytest.fail(
            f"{absent}, and {REQUIRE_ENV}=1: this job declared the real adapters must "
            f"be present, so an absent one is a red lane and not a skipped guard. "
            f"Install it with: {install}"
        )
    pytest.skip(f"{absent} ({install})")
