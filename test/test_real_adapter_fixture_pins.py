"""A pinned adapter's live fixtures must have been captured at the pinned release.

``test/real_adapters/package.json`` pins the adapters the Real Adapter Contract
lane installs, and Dependabot bumps it weekly. The lane's own contract tests
re-measure the handshake on a bump, but the live captures under
``test/fixtures/acp_frames/<backend>/`` do not re-run: a feature that rests on one
of them keeps standing on a release nobody runs any more, as ``codex/session-live``
does at 1.11.0 against a 2.2.2 pin.

So every live fixture of a pinned adapter must carry the pinned version in its
``_meta.agent_version`` header (``test_acp_frame_replay.py`` checks the header
against the capture's own ``initialize`` answer). A bump turns this red until the
captures are re-taken, or until someone writes down, in review, that a fixture
stays stale against the new pin.

``_STALE`` is that written-down list, and it is shrink-only: every entry names the
fixture version AND the pin it was stale against, so a bump invalidates every
entry at once, and an entry whose fixture now matches the pin is red until it is
deleted.

The file carries no marker, so backend-test runs it too: a bump reds there as
well as in the lane, which is the point.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from acp_frame_replay_harness import CORPUS, read_fixture
from real_adapter_gate import (
    CODEX_ACP_PACKAGE,
    OPENCODE_PACKAGE,
    PI_ACP_PACKAGE,
    PI_PACKAGE,
    pinned_version,
)

ROOT = Path(__file__).resolve().parent.parent

#: Fixture directory -> (npm package pinned in the manifest, ``agentInfo.name``
#: that release answers ``initialize`` with). The name is what ties a fixture to
#: the pin: a capture of some other agent in the same directory is not measured
#: against this package's version.
PINNED_FIXTURE_ADAPTERS: dict[str, tuple[str, str]] = {
    "codex": (CODEX_ACP_PACKAGE, "@agentclientprotocol/codex-acp"),
    "opencode": (OPENCODE_PACKAGE, "OpenCode"),
    "claude": ("@agentclientprotocol/claude-agent-acp", "@agentclientprotocol/claude-agent-acp"),
    "pi": (PI_ACP_PACKAGE, "pi-acp"),
}

#: Adapters held to a measured release written here instead of the lane manifest.
#: No lane test drives claude-agent-acp yet, so the lane does not install it; the
#: claude captures are held to the release they were taken on, and the pin moves
#: into ``test/real_adapters/package.json`` when a lane test needs the install.
MEASURED_NOT_INSTALLED: dict[str, str] = {
    "@agentclientprotocol/claude-agent-acp": "0.94.0",
}


def _pin(package: str) -> str:
    """The release *package*'s live fixtures must carry.

    pi is two pinned packages, and either can move the wire on its own, so a pi
    capture's header names both, ``<pi-acp> (pi <pi>)``, and a bump of either reds.
    """
    if package == PI_ACP_PACKAGE:
        return f"{pinned_version(PI_ACP_PACKAGE)} (pi {pinned_version(PI_PACKAGE)})"
    return MEASURED_NOT_INSTALLED.get(package) or pinned_version(package)


_RECAPTURE = "stale, re-capture pending"

#: ``<dir>/<file>`` -> (fixture's agent_version, pin it was stale against, why).
#: Shrink-only. Do not add to it to land a bump: re-capture instead.
_STALE: dict[str, tuple[str, str, str]] = {}

#: ``_STALE`` as it started, written out so that editing an entry above (its pin
#: above all) does not widen this too. ``_STALE`` may lose entries; every one it
#: keeps must equal its entry here.
_STALE_CEILING: dict[str, tuple[str, str]] = {
    "codex/session-live.jsonl": ("1.11.0", "2.2.2"),
    "opencode/compact-live.jsonl": ("1.18.30", "1.19.1"),
    "opencode/effort-live.jsonl": ("1.18.30", "1.19.1"),
    "opencode/mcp-directive-call-live.jsonl": ("1.18.30", "1.19.1"),
    "opencode/permission-request-live.jsonl": ("1.18.30", "1.19.1"),
    "opencode/session-live.jsonl": ("1.18.30", "1.19.1"),
    "opencode/session-load-live.jsonl": ("1.18.30", "1.19.1"),
    "opencode/tool-call-live.jsonl": ("1.18.30", "1.19.1"),
}


def _pinned_live_fixtures() -> list[tuple[str, str, str]]:
    """``(key, fixture version, pin)`` for every live fixture of a pinned adapter."""
    found = []
    for directory, (package, agent_name) in sorted(PINNED_FIXTURE_ADAPTERS.items()):
        pin = _pin(package)
        for path in sorted((CORPUS / directory).glob("*.jsonl")):
            meta, _frames = read_fixture(path)
            if meta.get("recorded") != "live":
                continue
            assert meta.get("agent_name") == agent_name, (
                f"{directory}/{path.name}: _meta.agent_name is {meta.get('agent_name')!r}, "
                f"but {package} answers initialize as {agent_name!r}"
            )
            found.append((f"{directory}/{path.name}", str(meta.get("agent_version")), pin))
    return found


def stale_against_pin(
    fixtures: list[tuple[str, str, str]], stale: dict[str, tuple[str, str, str]]
) -> list[str]:
    """Every way *fixtures* and the *stale* list disagree with the pins."""
    problems = []
    seen = set()
    for key, version, pin in fixtures:
        seen.add(key)
        entry = stale.get(key)
        if version == pin:
            if entry is not None:
                problems.append(f"{key} now matches pin {pin}: delete its _STALE entry")
            continue
        if entry is None:
            problems.append(
                f"{key} was captured at {version} but the lane pins {pin}: re-capture it"
            )
        elif entry[:2] != (version, pin):
            problems.append(
                f"{key} is {version} against pin {pin}, but _STALE accepted {entry[0]} "
                f"against {entry[1]}: the pin moved, so re-capture it"
            )
    problems += [
        f"_STALE names {key}, which is not a live pinned fixture" for key in stale.keys() - seen
    ]
    return problems


def test_every_pinned_live_fixture_matches_the_lane_pin() -> None:
    problems = stale_against_pin(_pinned_live_fixtures(), _STALE)
    assert not problems, "\n".join(problems)


def test_the_stale_list_only_shrinks() -> None:
    widened = {
        key: entry[:2] for key, entry in _STALE.items() if _STALE_CEILING.get(key) != entry[:2]
    }
    assert not widened, f"_STALE entries not in their original form: {widened}"
    assert all(why == _RECAPTURE for _version, _pin, why in _STALE.values())


@pytest.mark.parametrize(
    ("fixture", "stale", "expected"),
    [
        (("codex/a.jsonl", "2.2.2", "2.2.2"), {}, ""),
        (("codex/a.jsonl", "2.2.2", "2.3.0"), {}, "re-capture it"),
        (
            ("codex/a.jsonl", "1.11.0", "2.3.0"),
            {"codex/a.jsonl": ("1.11.0", "2.2.2", "x")},
            "the pin moved",
        ),
        (
            ("codex/a.jsonl", "2.2.2", "2.2.2"),
            {"codex/a.jsonl": ("1.11.0", "2.2.2", "x")},
            "delete",
        ),
        (
            ("codex/a.jsonl", "2.2.2", "2.2.2"),
            {"codex/b.jsonl": ("1.11.0", "2.2.2", "x")},
            "not a live pinned fixture",
        ),
    ],
    ids=[
        "at-pin",
        "pin-bumped",
        "stale-entry-outlived-pin",
        "stale-entry-outlived-fixture",
        "stale-entry-names-no-fixture",
    ],
)
def test_the_gate_reds_on_each_drift(fixture, stale, expected) -> None:
    problems = stale_against_pin([fixture], stale)
    assert (expected in problems[0]) if expected else not problems


@pytest.mark.parametrize("bumped", [PI_ACP_PACKAGE, PI_PACKAGE])
def test_a_pi_bump_of_either_half_reds(monkeypatch, bumped) -> None:
    """The pi pin is both halves: a capture at the old pi under a new pi-acp is stale too."""
    import real_adapter_gate

    pinned = real_adapter_gate.pinned_version
    at_pin = [key for key, version, pin in _pinned_live_fixtures() if key.startswith("pi/")]
    assert at_pin, "no live pi fixture is held to the pin"
    monkeypatch.setattr(
        sys.modules[__name__],
        "pinned_version",
        lambda package: pinned(package) + ("-next" if package == bumped else ""),
    )
    problems = stale_against_pin(_pinned_live_fixtures(), _STALE)
    assert {p.split(" ", 1)[0] for p in problems if "re-capture it" in p} >= set(at_pin)


def test_the_gate_runs_in_the_real_adapter_lane() -> None:
    """The lane a Dependabot bump runs is where this must go red."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    lane = re.split(
        r"\n  [a-z][\w-]*:\n", ci.split("\n  real-adapter-contract:\n", 1)[1], maxsplit=1
    )[0]
    runs = [
        index
        for index, line in enumerate(lane.splitlines())
        if re.match(r"\s+pytest .*test/test_real_adapter_fixture_pins\.py", line)
    ]
    assert runs, "no pytest line in the real-adapter-contract job runs the pin gate"
    as_runner = lane.splitlines().index("      - uses: ./.github/actions/run-as-runner")
    assert runs[0] > as_runner, "the pin gate must run after run-as-runner, through ci-shell"
    # After the contract tests, so a bump PR still measures the new release.
    contract = next(i for i, line in enumerate(lane.splitlines()) if "-m real_adapter" in line)
    assert runs[0] > contract, "the pin gate must run after the contract tests, not skip them"


def test_a_measured_only_pin_is_not_also_in_the_lane_manifest() -> None:
    """One pin per adapter: an install in the manifest retires the constant."""
    import json

    from real_adapter_gate import MANIFEST

    installed = json.loads(MANIFEST.read_text(encoding="utf-8"))["dependencies"]
    assert not set(MEASURED_NOT_INSTALLED) & set(installed), (
        "move the pin into test/real_adapters/package.json and drop it from "
        "MEASURED_NOT_INSTALLED"
    )
