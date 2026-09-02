"""The repo has ONE Node toolchain pin: ``.nvmrc`` at the repo root.

Every ``actions/setup-node`` step in ``.github/workflows/`` must track it, so a
future Node bump (or a copy-pasted workflow) cannot silently reintroduce an
EOL major in one job while the rest of the repo moves on:

* floating pins (``24``) must EQUAL the ``.nvmrc`` major — a job quietly ahead
  of the toolchain pin is as much drift as one behind it;
* exact pins (``24.19.0``) must have a major >= the ``.nvmrc`` major (the
  vulnerability-scan workflow pins an exact patch on purpose).

One job is deliberately NOT on the toolchain pin: ``lockfile-engines-floor``
exists to prove ``npm ci`` still works on the OLDEST Node the frontend claims
to support, so pinning it to ``.nvmrc`` would make it a duplicate of the jobs
that already run there. It is not exempt, only measured against a different
source of truth — ``website/package.json``'s ``engines.node`` — which
``test_the_engines_floor_job_pins_the_declared_floor`` enforces. Both rules
together mean no pin in this repo is unaccounted for.

Static and offline by design: this reads only files in the repo, never
nodejs.org, so it cannot flake on network and never gates on "is there a newer
Node" — only on internal consistency.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
NVMRC = ROOT / ".nvmrc"
WEBSITE_PACKAGE_JSON = ROOT / "website" / "package.json"

# The one job whose pin is the frontend's SUPPORTED FLOOR, not the toolchain pin.
# Keyed by (workflow file, job id) rather than by workflow name so the carve-out
# cannot widen to the rest of ci.yml.
ENGINES_FLOOR_JOB = ("ci.yml", "lockfile-engines-floor")


def _nvmrc_major() -> int:
    text = NVMRC.read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+", text), f".nvmrc must contain a bare major, got {text!r}"
    return int(text)


def _engines_floor_major() -> int:
    """The major from ``website/package.json``'s ``engines.node``."""
    spec = json.loads(WEBSITE_PACKAGE_JSON.read_text(encoding="utf-8"))["engines"]["node"]
    # Only a `>=` floor is parseable as "the oldest supported major". A caret, a
    # range or an upper bound would make the floor ambiguous, and the workflow
    # step that re-derives it in shell would disagree with this test, so refuse
    # the shape outright rather than guess.
    match = re.fullmatch(r">=(\d+)(?:\.\d+(?:\.\d+)?)?", str(spec))
    assert match, f"engines.node must be a bare `>=major` floor, got {spec!r}"
    return int(match.group(1))


def _setup_node_versions() -> list[tuple[str, str, str]]:
    """Return (workflow file name, job id, node-version) for every setup-node step."""
    found: list[tuple[str, str, str]] = []
    workflow_files = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    for wf in workflow_files:
        doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
        for job_id, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if not str(step.get("uses", "")).startswith("actions/setup-node@"):
                    continue
                version = (step.get("with") or {}).get("node-version")
                assert version is not None, f"{wf.name}: setup-node step without node-version"
                found.append((wf.name, str(job_id), str(version)))
    return found


def test_nvmrc_exists_with_a_bare_major() -> None:
    assert NVMRC.is_file(), ".nvmrc is the single local toolchain pin — do not delete it"
    assert _nvmrc_major() >= 22


def test_every_workflow_node_pin_tracks_nvmrc() -> None:
    target = _nvmrc_major()
    pins = _setup_node_versions()
    # A glob/parse that silently matched nothing would make this gate vacuous;
    # the repo has setup-node steps across ci/build/pages/docker workflows.
    assert len(pins) >= 10, f"expected >= 10 setup-node pins, found {len(pins)}: {pins}"
    for wf_name, job_id, version in pins:
        if (wf_name, job_id) == ENGINES_FLOOR_JOB:
            continue  # measured against engines.node instead — see the test below
        assert re.fullmatch(r"\d+(\.\d+\.\d+)?", version), (
            f"{wf_name}: node-version {version!r} is neither a bare major nor an exact "
            f"major.minor.patch pin"
        )
        major = int(version.split(".")[0])
        if "." in version:
            assert major >= target, (
                f"{wf_name}: exact pin {version} is below the .nvmrc major {target}"
            )
        else:
            assert major == target, (
                f"{wf_name}: floating pin {version} does not equal the .nvmrc major {target} — "
                f"bump .nvmrc and every workflow together"
            )


def test_the_engines_floor_job_pins_the_declared_floor() -> None:
    """The floor job must sit on engines.node, and the floor must not lead the toolchain.

    Without this, skipping the job above would leave its pin governed by nothing:
    the job would keep passing on whatever Node it happened to name, including one
    the frontend never claimed to support.
    """
    wf_name, job_id = ENGINES_FLOOR_JOB
    pins = [v for (w, j, v) in _setup_node_versions() if (w, j) == (wf_name, job_id)]
    assert pins == [str(_engines_floor_major())], (
        f"{wf_name}: job {job_id} must pin exactly the engines.node floor "
        f"{_engines_floor_major()}, found {pins}"
    )
    # A floor ahead of the toolchain pin would mean the repo's own .nvmrc names a
    # Node the frontend refuses to run on. Equal is legal but redundant: the job
    # then duplicates the jobs already on the toolchain pin and can be deleted.
    assert _engines_floor_major() <= _nvmrc_major(), (
        f"engines.node floor {_engines_floor_major()} is ahead of the .nvmrc major "
        f"{_nvmrc_major()}"
    )
