"""The repo has ONE Node toolchain pin: ``.nvmrc`` at the repo root.

Every ``actions/setup-node`` step in ``.github/workflows/`` must track it, so a
future Node bump (or a copy-pasted workflow) cannot silently reintroduce an
EOL major in one job while the rest of the repo moves on:

* floating pins (``24``) must EQUAL the ``.nvmrc`` major — a job quietly ahead
  of the toolchain pin is as much drift as one behind it;
* exact pins (``24.19.0``) must have a major >= the ``.nvmrc`` major (the
  vulnerability-scan workflow pins an exact patch on purpose).

Static and offline by design: this reads only files in the repo, never
nodejs.org, so it cannot flake on network and never gates on "is there a newer
Node" — only on internal consistency.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
NVMRC = ROOT / ".nvmrc"


def _nvmrc_major() -> int:
    text = NVMRC.read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+", text), f".nvmrc must contain a bare major, got {text!r}"
    return int(text)


def _setup_node_versions() -> list[tuple[str, str]]:
    """Return (workflow-relative-path, node-version) for every setup-node step."""
    found: list[tuple[str, str]] = []
    workflow_files = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    for wf in workflow_files:
        doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
        for job in (doc.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                if not str(step.get("uses", "")).startswith("actions/setup-node@"):
                    continue
                version = (step.get("with") or {}).get("node-version")
                assert version is not None, f"{wf.name}: setup-node step without node-version"
                found.append((wf.name, str(version)))
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
    for wf_name, version in pins:
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
