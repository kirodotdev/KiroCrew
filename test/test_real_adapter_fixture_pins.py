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

kiro-cli and KAS have no lane pin, so their live fixtures are held to a floor
(``FIXTURE_FLOORS``) instead, with the same shrink-only list (``_BELOW_FLOOR``) for
the ones below it.

Synthesized fixtures are held to the same pins and floors by the release they
name, with their own shrink-only list (``_SYNTHESIZED_BEHIND``) and a count ceiling.

The file carries no marker, so backend-test runs it too: a bump reds there as
well as in the lane, which is the point.
"""

from __future__ import annotations

import json
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

from kiro_crew.acp.kas_pin import KAS_PIN_VERSION
from kiro_crew.agent_sdk.backends_launch import COPILOT_VERIFIED_VERSION
from kiro_crew.kiro_cli import (
    MANDATORY_MCPS_DROP_MIN_VERSION,
    SPEC_PERMISSIONS_MIN_VERSION,
)
from kiro_crew.mcp_hot_reload import MCP_HOT_RELOAD_MIN_KIRO_CLI_VERSION

COPILOT_PACKAGE = "@github/copilot"

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
    "copilot": (COPILOT_PACKAGE, "Copilot"),
}

#: Adapters held to a measured release written here instead of the lane manifest.
#: No lane test drives claude-agent-acp yet, so the lane does not install it; the
#: claude captures are held to the release they were taken on, and the pin moves
#: into ``test/real_adapters/package.json`` when a lane test needs the install.
#: Copilot is held to the one release the harness accepts at the handshake, so
#: raising ``COPILOT_VERIFIED_VERSION`` reds here until the captures are re-taken.
MEASURED_NOT_INSTALLED: dict[str, str] = {
    "@agentclientprotocol/claude-agent-acp": "0.94.0",
    COPILOT_PACKAGE: COPILOT_VERIFIED_VERSION,
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


def _release(version: str) -> tuple[int, ...] | None:
    parts = version.split(".")
    return tuple(int(p) for p in parts) if all(p.isdigit() for p in parts) else None


#: kiro-cli and KAS have no lane pin, so their live captures are held to a floor
#: instead: the highest kiro-cli release a Crew version gate branches on (below it
#: Crew drives a path a current kiro-cli never takes), and for KAS the engine the
#: bundle pins. ``<dir>`` -> header key -> floor. KAS headers that name the
#: kiro-cli they ran under are held to the kiro-cli floor too.
_KIRO_CLI_FLOOR = ".".join(
    map(
        str,
        max(
            MANDATORY_MCPS_DROP_MIN_VERSION,
            SPEC_PERMISSIONS_MIN_VERSION,
            MCP_HOT_RELOAD_MIN_KIRO_CLI_VERSION,
        ),
    )
)
FIXTURE_FLOORS: dict[str, dict[str, str]] = {
    "kiro": {"agent_version": _KIRO_CLI_FLOOR},
    "kas": {"agent_version": KAS_PIN_VERSION, "kiro_cli_version": _KIRO_CLI_FLOOR},
}

#: ``<dir>/<file>:<header key>`` -> (header version, floor it is below). Shrink-only:
#: a fixture below its floor is re-captured, not added here.
_BELOW_FLOOR: dict[str, tuple[str, str]] = {
    "kiro/session.jsonl:agent_version": ("2.21.4", "2.27.0"),
    "kas/session.jsonl:agent_version": ("0.63.3", "0.66.33"),
}

#: ``_BELOW_FLOOR`` as it started; every entry it keeps must equal its entry here.
_BELOW_FLOOR_CEILING: dict[str, tuple[str, str]] = {
    "kiro/session.jsonl:agent_version": ("2.21.4", "2.27.0"),
    "kas/session.jsonl:agent_version": ("0.63.3", "0.66.33"),
}


def _floored_live_versions(corpus: Path = CORPUS) -> list[tuple[str, str, str]]:
    """``(key, header version, floor)`` for every floored header of a live kiro/KAS fixture."""
    found = []
    for directory, floors in sorted(FIXTURE_FLOORS.items()):
        for path in sorted((corpus / directory).glob("*.jsonl")):
            meta, _frames = read_fixture(path)
            if meta.get("recorded") != "live":
                continue
            for header, floor in floors.items():
                if header in meta or header == "agent_version":
                    key = f"{directory}/{path.name}:{header}"
                    found.append((key, str(meta.get(header)), floor))
    return found


def below_floor(
    versions: list[tuple[str, str, str]], allowed: dict[str, tuple[str, str]]
) -> list[str]:
    """Every way *versions* and the *allowed* below-floor list disagree with the floors."""
    problems = []
    seen = set()
    for key, version, floor in versions:
        seen.add(key)
        entry = allowed.get(key)
        release = _release(version)
        if release is None:
            problems.append(f"{key} is {version!r}, not a release to hold to floor {floor}")
        elif release >= _release(floor):
            if entry is not None:
                problems.append(f"{key} is at floor {floor} now: delete its _BELOW_FLOOR entry")
        elif entry is None:
            problems.append(f"{key} is {version}, below floor {floor}: re-capture it")
        elif entry != (version, floor):
            problems.append(
                f"{key} is {version} against floor {floor}, but _BELOW_FLOOR accepted "
                f"{entry[0]} against {entry[1]}: the floor moved, so re-capture it"
            )
    problems += [
        f"_BELOW_FLOOR names {key}, which is not a floored live fixture"
        for key in allowed.keys() - seen
    ]
    return problems


def test_every_live_kiro_and_kas_fixture_is_at_its_floor() -> None:
    problems = below_floor(_floored_live_versions(), _BELOW_FLOOR)
    assert not problems, "\n".join(problems)


def test_the_below_floor_list_only_shrinks() -> None:
    widened = {k: v for k, v in _BELOW_FLOOR.items() if _BELOW_FLOOR_CEILING.get(k) != v}
    assert not widened, f"_BELOW_FLOOR entries not in their original form: {widened}"


@pytest.mark.parametrize(
    ("version", "allowed", "expected"),
    [
        ("2.27.0", {}, ""),
        ("2.34.0", {}, ""),
        ("2.26.9", {}, "re-capture it"),
        ("2.21.4", {"kiro/a.jsonl:agent_version": ("2.21.4", "2.23.0")}, "the floor moved"),
        ("2.27.0", {"kiro/a.jsonl:agent_version": ("2.21.4", "2.27.0")}, "delete"),
        ("unmeasured", {}, "not a release"),
        ("2.27.0", {"kiro/b.jsonl:agent_version": ("2.21.4", "2.27.0")}, "not a floored"),
    ],
    ids=[
        "at-floor",
        "above-floor",
        "below-floor",
        "entry-outlived-floor",
        "entry-outlived-fixture",
        "unparseable",
        "entry-names-no-fixture",
    ],
)
def test_the_floor_reds_on_each_drift(version, allowed, expected) -> None:
    problems = below_floor([("kiro/a.jsonl:agent_version", version, "2.27.0")], allowed)
    assert (expected in problems[0]) if expected else not problems


def test_a_kas_capture_under_an_old_kiro_cli_reds(tmp_path) -> None:
    """A KAS header's ``kiro_cli_version`` is held to the kiro-cli floor, not only the engine."""
    (tmp_path / "kas").mkdir()
    meta = {"backend": "kas", "recorded": "live", "agent_version": KAS_PIN_VERSION}
    (tmp_path / "kas" / "a-live.jsonl").write_text(
        json.dumps({"_meta": dict(meta, kiro_cli_version="2.20.0")}) + "\n"
    )
    problems = below_floor(_floored_live_versions(tmp_path), {})
    want = f"kas/a-live.jsonl:kiro_cli_version is 2.20.0, below floor {_KIRO_CLI_FLOOR}"
    assert problems == [f"{want}: re-capture it"]


def test_another_adapters_capture_in_copilot_reds(monkeypatch, tmp_path) -> None:
    """A capture of some other agent under ``copilot/`` is not measured as Copilot."""
    src = CORPUS / "copilot" / "handshake-live.jsonl"
    (tmp_path / "copilot").mkdir()
    text = src.read_text(encoding="utf-8")
    assert '"agent_name": "Copilot"' in text
    (tmp_path / "copilot" / src.name).write_text(
        text.replace('"agent_name": "Copilot"', '"agent_name": "OpenCode"', 1)
    )
    monkeypatch.setattr(sys.modules[__name__], "CORPUS", tmp_path)
    with pytest.raises(AssertionError, match="answers initialize as 'Copilot'"):
        _pinned_live_fixtures()


def test_a_copilot_verified_version_bump_reds(monkeypatch) -> None:
    at_pin = [key for key, _v, _p in _pinned_live_fixtures() if key.startswith("copilot/")]
    assert at_pin, "no live copilot fixture is held to COPILOT_VERIFIED_VERSION"
    monkeypatch.setitem(MEASURED_NOT_INSTALLED, COPILOT_PACKAGE, COPILOT_VERIFIED_VERSION + "-next")
    problems = stale_against_pin(_pinned_live_fixtures(), _STALE)
    assert {p.split(" ", 1)[0] for p in problems if "re-capture it" in p} == set(at_pin)


def test_a_measured_only_pin_is_not_also_in_the_lane_manifest() -> None:
    """One pin per adapter: an install in the manifest retires the constant."""
    from real_adapter_gate import MANIFEST

    installed = json.loads(MANIFEST.read_text(encoding="utf-8"))["dependencies"]
    assert not set(MEASURED_NOT_INSTALLED) & set(installed), (
        "move the pin into test/real_adapters/package.json and drop it from "
        "MEASURED_NOT_INSTALLED"
    )


#: A synthesized fixture is written by hand, not captured, but it is still written
#: against one adapter release, and a feature resting on it rests on that release.
#: The release it names, in its ``agent_version`` header or, when that is not a
#: release (``unmeasured``), in its note as ``<label> <version>``, must not be older
#: than the lane pin (or, for kiro-cli and KAS, the floor). One naming none at all
#: is held too: otherwise writing ``unmeasured`` would dodge the check.
#: ``<dir>`` -> header key -> the label the note names that release by. pi has
#: none: its pin is two releases, so a pi one carries ``_pin``'s string as written.
_NOTE_LABELS: dict[str, dict[str, str]] = {
    "codex": {"agent_version": "codex-acp"},
    "opencode": {"agent_version": "opencode"},
    "claude": {"agent_version": "claude-agent-acp"},
    "copilot": {"agent_version": "copilot"},
    "kiro": {"agent_version": "kiro-cli"},
    "kas": {"agent_version": "KAS", "kiro_cli_version": "kiro-cli"},
}

#: ``<dir>/<file>:<header key>`` -> (release it names, or "", bar it is behind).
#: Shrink-only: rewrite or re-capture the fixture, never add to this.
_SYNTHESIZED_BEHIND: dict[str, tuple[str, str]] = {
    "codex/mcp-elicitation.jsonl:agent_version": ("", "2.3.0"),
    "codex/session.jsonl:agent_version": ("1.11.0", "2.3.0"),
    "kiro/notifications.jsonl:agent_version": ("2.21.1", "2.27.0"),
}

#: ``_SYNTHESIZED_BEHIND`` as it started; every entry it keeps must equal its entry
#: here, so re-pinning an entry against a newer bar, or reusing a freed key, reds.
_SYNTHESIZED_BEHIND_START: dict[str, tuple[str, str]] = {
    "codex/mcp-elicitation.jsonl:agent_version": ("", "2.3.0"),
    "codex/session.jsonl:agent_version": ("1.11.0", "2.3.0"),
    "kiro/notifications.jsonl:agent_version": ("2.21.1", "2.27.0"),
}

#: ``_SYNTHESIZED_BEHIND`` holds exactly this many entries. It only moves down, and
#: with the list: a ceiling above the list's size is room for the next offender.
SYNTHESIZED_BEHIND_CEILING = 3


def _named_release(meta: dict, header: str, label: str | None, bar: str) -> str:
    """The release *meta* says its fixture was written against, or ``""``."""
    value = str(meta.get(header, ""))
    if value == bar or _release(value) is not None:
        return value
    if label is None:
        return ""
    named = re.findall(
        rf"(?<![\w-]){re.escape(label)} v?(\d+(?:\.\d+)+)(?![\w-]|\.\d)",
        str(meta.get("note", "")),
        flags=re.IGNORECASE,
    )
    # A note naming two releases is held to the older one.
    return min(named, key=_release, default="")


def _synthesized_versions(corpus: Path = CORPUS) -> list[tuple[str, str, str]]:
    """``(key, named release, bar)`` for every synthesized fixture of a pinned or floored adapter."""
    bars = {d: {"agent_version": _pin(p)} for d, (p, _name) in PINNED_FIXTURE_ADAPTERS.items()}
    bars.update(FIXTURE_FLOORS)
    found = []
    for directory, headers in sorted(bars.items()):
        labels = _NOTE_LABELS.get(directory, {})
        for path in sorted((corpus / directory).glob("*.jsonl")):
            meta, _frames = read_fixture(path)
            if meta.get("recorded") != "synthesized":
                continue
            for header, bar in headers.items():
                named = _named_release(meta, header, labels.get(header), bar)
                if header == "agent_version" or header in meta or named:
                    found.append((f"{directory}/{path.name}:{header}", named, bar))
    return found


def synthesized_behind(
    versions: list[tuple[str, str, str]], allowed: dict[str, tuple[str, str]]
) -> list[str]:
    """Every way *versions* and the *allowed* list disagree with the pins and floors."""
    problems = []
    seen = set()
    for key, named, bar in versions:
        seen.add(key)
        entry = allowed.get(key)
        release, floor = _release(named), _release(bar)
        current = named == bar or (release is not None and floor is not None and release >= floor)
        if current:
            if entry is not None:
                problems.append(f"{key} is at {bar} now: delete its _SYNTHESIZED_BEHIND entry")
            continue
        if entry is None:
            said = f"names {named}" if named else "names no adapter release"
            problems.append(
                f"{key} is synthesized and {said}, behind {bar}: re-capture it live "
                "or rewrite it against the current release"
            )
        elif entry != (named, bar):
            problems.append(
                f"{key} names {named or 'no release'} against {bar}, but _SYNTHESIZED_BEHIND "
                f"accepted {entry[0] or 'no release'} against {entry[1]}: rewrite it"
            )
    problems += [
        f"_SYNTHESIZED_BEHIND names {key}, which is not a synthesized fixture behind its bar"
        for key in allowed.keys() - seen
    ]
    return problems


def synthesized_ceiling(allowed: dict[str, tuple[str, str]], ceiling: int) -> list[str]:
    widened = {k: v for k, v in allowed.items() if _SYNTHESIZED_BEHIND_START.get(k) != v}
    if widened:
        return [f"_SYNTHESIZED_BEHIND entries not in their original form: {widened}"]
    if len(allowed) > ceiling:
        return [f"_SYNTHESIZED_BEHIND has {len(allowed)} entries over its ceiling {ceiling}"]
    if len(allowed) < ceiling:
        return [f"SYNTHESIZED_BEHIND_CEILING is {ceiling}: lower it to {len(allowed)}"]
    return []


def test_no_synthesized_fixture_is_behind_its_adapter() -> None:
    problems = synthesized_behind(_synthesized_versions(), _SYNTHESIZED_BEHIND)
    assert not problems, "\n".join(problems)


def test_the_synthesized_behind_list_only_shrinks() -> None:
    problems = synthesized_ceiling(_SYNTHESIZED_BEHIND, SYNTHESIZED_BEHIND_CEILING)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    ("named", "allowed", "expected"),
    [
        ("2.3.0", {}, ""),
        ("2.4.0", {}, ""),
        ("1.11.0", {}, "names 1.11.0, behind 2.3.0"),
        ("", {}, "names no adapter release"),
        ("1.11.0", {"codex/a.jsonl:agent_version": ("1.11.0", "2.2.2")}, "rewrite it"),
        ("2.3.0", {"codex/a.jsonl:agent_version": ("1.11.0", "2.3.0")}, "delete"),
        ("2.3.0", {"codex/b.jsonl:agent_version": ("1.11.0", "2.3.0")}, "not a synthesized"),
    ],
    ids=[
        "at-pin",
        "above-pin",
        "behind-pin",
        "names-nothing",
        "entry-outlived-pin",
        "entry-outlived-fixture",
        "entry-names-no-fixture",
    ],
)
def test_the_synthesized_check_reds_on_each_drift(named, allowed, expected) -> None:
    problems = synthesized_behind([("codex/a.jsonl:agent_version", named, "2.3.0")], allowed)
    assert (expected in problems[0]) if expected else not problems


def _write_synthesized(corpus: Path, directory: str, name: str, **meta: str) -> None:
    (corpus / directory).mkdir(exist_ok=True)
    header = {"_meta": dict({"backend": directory, "recorded": "synthesized"}, **meta)}
    (corpus / directory / name).write_text(json.dumps(header) + "\n")


def test_a_new_stale_synthesized_fixture_reds(tmp_path) -> None:
    _write_synthesized(
        tmp_path, "codex", "new.jsonl", agent_version="unmeasured", note="codex-acp 2.2.2 shapes"
    )
    problems = synthesized_behind(_synthesized_versions(tmp_path), {})
    want = (
        f"codex/new.jsonl:agent_version is synthesized and names 2.2.2, behind "
        f"{_pin(CODEX_ACP_PACKAGE)}: re-capture it live or rewrite it against the current release"
    )
    assert problems == [want]


def test_a_kas_note_is_held_on_both_headers(tmp_path) -> None:
    """A KAS note naming an old kiro-cli reds on that header even with a current engine."""
    note = f"KAS {KAS_PIN_VERSION} under kiro-cli 2.20.0"
    _write_synthesized(tmp_path, "kas", "a.jsonl", agent_version="unmeasured", note=note)
    problems = synthesized_behind(_synthesized_versions(tmp_path), {})
    assert [p.split(" ", 1)[0] for p in problems] == ["kas/a.jsonl:kiro_cli_version"]


def test_dropping_an_entry_while_the_fixture_is_still_behind_reds() -> None:
    allowed = dict(_SYNTHESIZED_BEHIND)
    allowed.pop("codex/session.jsonl:agent_version")
    problems = synthesized_behind(_synthesized_versions(), allowed)
    assert [p.split(" ", 1)[0] for p in problems] == ["codex/session.jsonl:agent_version"]


def test_a_raised_ceiling_or_an_added_entry_reds() -> None:
    assert synthesized_ceiling(_SYNTHESIZED_BEHIND, SYNTHESIZED_BEHIND_CEILING + 1)
    grown = dict(_SYNTHESIZED_BEHIND, **{"codex/x.jsonl:agent_version": ("1.0.0", "2.3.0")})
    assert synthesized_ceiling(grown, SYNTHESIZED_BEHIND_CEILING + 1)


def test_a_repinned_or_reused_entry_reds() -> None:
    """Swapping a key or moving an entry's bar keeps the count but widens the list."""
    key = "codex/session.jsonl:agent_version"
    repinned = dict(_SYNTHESIZED_BEHIND, **{key: ("1.11.0", "2.4.0")})
    assert synthesized_ceiling(repinned, SYNTHESIZED_BEHIND_CEILING)
    reused = {k: v for k, v in _SYNTHESIZED_BEHIND.items() if k != key}
    reused["codex/other.jsonl:agent_version"] = ("1.11.0", "2.3.0")
    assert synthesized_ceiling(reused, SYNTHESIZED_BEHIND_CEILING)
