"""The backend half of the contributed-command conformance suite.

Reads the SAME fixture as
``website/src/apps/command-bar/contributedCommands.conformance.test.ts``. The point is
not extra coverage -- both sides already have their own unit tests -- but that the two
hand-written rulebooks cannot disagree about a verdict without a build failing. They
drifted twice while this contract was being written (the title cap and the per-app
command cap each landed on one side only), and in both cases the manifest accepted an
app the launcher then silently truncated.

If a case fails here but passes in the TypeScript harness, the contract has split. Fix
the validator, not the fixture -- unless the fixture is what is wrong, in which case
fix it once and both sides move together.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from kiro_crew.apps.manifest import AppManifest

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "contributed_commands_conformance.json"
CASES = json.loads(FIXTURE.read_text())["cases"]


def _contributes(case: dict) -> dict:
    """Build the case's `contributes` block, expanding any `generate` shorthand.

    Long values are generated rather than written out so the fixture stays readable --
    a 4001-character prompt literal would bury every other case in the file.
    """
    gen = case.get("generate")
    if not gen:
        return case["contributes"]
    if "commands" in gen:
        return {
            "commands": [
                {"id": f"cmd-{n}", "title": f"Command {n}", "prompt": "Do the thing."}
                for n in range(gen["commands"])
            ]
        }
    cmd: dict = {"id": "do-it", "title": "Do it", "prompt": "Do the thing."}
    if "keywords" in gen:
        cmd["keywords"] = [f"kw{n}" for n in range(gen["keywords"])]
    if "keywordLength" in gen:
        cmd["keywords"] = ["k" * gen["keywordLength"]]
    if "titleLength" in gen:
        cmd["title"] = "x" * gen["titleLength"]
    if "promptLength" in gen:
        cmd["prompt"] = "x" * gen["promptLength"]
    if "hosts" in gen:
        cmd["prompt"] = "Do it to {argument}"
        cmd["argument"] = {
            "kind": "url",
            "hosts": [f"h{n}.test" for n in range(gen["hosts"])],
        }
    return {"commands": [cmd]}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_contract_agrees_with_the_shared_fixture(case):
    manifest = AppManifest.from_dict(
        {
            "name": "conformance",
            "version": "1.0.0",
            "displayName": "Conformance",
            "description": "Fixture app for the contribution conformance suite.",
            "contributes": _contributes(case),
        }
    )
    # Filtered to the contribution contract: an unrelated manifest requirement growing
    # a new field would otherwise fail every case here for a reason this suite is not
    # about.
    errors = [e for e in manifest.validate() if "contributes" in e]
    if case["accept"]:
        assert errors == [], f"{case['name']}: expected acceptance, got {errors}"
    else:
        assert errors, f"{case['name']}: expected a refusal, got none"


def test_every_case_is_named_and_shaped():
    # A fixture the harnesses disagree about how to read is worse than no fixture.
    names = [c["name"] for c in CASES]
    assert len(names) == len(set(names)), "case names must be unique"
    for case in CASES:
        assert isinstance(case["accept"], bool), case["name"]
        assert isinstance(case["commands"], int), case["name"]
        assert ("contributes" in case) != ("generate" in case), case["name"]
        if "asymmetric" in case:
            # A declared per-side difference must say which side does what, so the gap
            # stays the one the fixture describes.
            assert case["asymmetric"]["manifest"] == "refuse", case["name"]
            assert case["asymmetric"]["frontend"], case["name"]
            assert case["accept"] is False, case["name"]
