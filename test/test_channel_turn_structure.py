"""The structure that keeps the channel turn pipeline the ONLY turn loop.

Four ratchets, each shrink-only:

* **No fork outside PENDING.** A channel dispatcher runs its turns through
  :class:`~kiro_crew.messaging.dispatch.ChannelTurns`; only the channels listed in
  :data:`PENDING` still drive their own ``TurnDriver``. Every behaviour the
  pipeline owns (the shutdown gate and the ceiling composed onto it, both of
  which wrap ``begin_turn``, the death attribution, the re-injection settle, the
  identity publish) is tested once, through the pipeline, in
  ``test_channel_turns``; a dispatcher that grew its own copy of any of them
  would escape those tests, so it fails here. Each PENDING fork still
  publishes the turn identity itself, the half of the identity guarantee the
  pipeline cannot give it. A PENDING entry that does not fork is stale.
* **Every other dispatcher delegates.** A dispatcher outside PENDING answers
  through ``ChannelTurns(...).answer(`` or :func:`drive_turn`, both of which
  publish the turn identity on its behalf -- so the pipeline must still publish
  it. A dispatcher that ran a turn by a third route (neither forking nor
  delegating) would escape both ratchets; only an entry in the shrink-only
  :data:`_RUNS_NO_TURN` list may do neither.
* **Drift may only shrink.** :data:`~kiro_crew.messaging.dispatch.DISCORD_DRIFT`
  is pinned to a literal naming the ruling that would retire each member.
* **The export budget may only fall.** The names other packages import from the
  pipeline's modules are counted; the step helpers leave as the engines that
  still use them move onto the bracket.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from source_corpus import src_root

from kiro_crew.messaging.dispatch import DISCORD_DRIFT, Drift

pytestmark = pytest.mark.xdist_group(name="tree_scan_channel_turn_structure")

#: Channels whose dispatcher still drives its own ``TurnDriver``, each with the
#: step that moves it onto the pipeline.
PENDING = {
    "telegram": "its leg needs the I12 path guards widened first",
    "slack": "its transport keeps Slack-only steps; it adopts the bracket first",
}

#: Constructs only the pipeline (or a PENDING fork) may spell.
_PIPELINE_ONLY = (
    "TurnDriver(",
    ".begin_turn(",
    "charge_turn_failure(",
    "rearm_reinjection(",
    "publish_turn_identity(",
)

#: Dispatchers outside PENDING that run no channel turn at all, each with its
#: reason. May only shrink; empty today.
_RUNS_NO_TURN: dict[str, str] = {}

#: DISCORD_DRIFT, member by member, with the maintainer ruling that retires each.
_DISCORD_DRIFT_RULINGS = {
    Drift.NO_GOVERNANCE_BACKSTOP: "R7: add the per-message recheck to Discord",
    Drift.NO_HOOK_REPLY: "R1: honour an operator's on_message auto-reply on Discord",
    Drift.NO_COMPACTION_RECOVERY: "R2: reset and replay after COMPACTION_FAILED on Discord",
    Drift.OPENS_CREW_LOG: "R3: open the crew log on every channel",
    Drift.BIND_ON_LOOP: "R5: one rule for where the origin/mirror bind runs",
    Drift.GENERIC_DENY_REASON: "R4: steer the hook's deny reason on Discord",
    Drift.SEAL_UNCLOSED_STREAM: "R6: seal an unclosed stream on every renderer",
}

#: Every Drift member there is. A new one is a new divergence, which this file
#: refuses: dispatchers converge, they do not grow new forks.
_ALL_DRIFT = {
    "NO_GOVERNANCE_BACKSTOP",
    "NO_HOOK_REPLY",
    "NO_COMPACTION_RECOVERY",
    "OPENS_CREW_LOG",
    "BIND_ON_LOOP",
    "GENERIC_DENY_REASON",
    "SEAL_UNCLOSED_STREAM",
    "NO_DIRECTIVES",
}

#: Distinct names other packages import from the pipeline's modules. May only fall.
_EXPORT_BUDGET = 31
_PIPELINE_MODULES = ("kiro_crew.messaging.dispatch", "kiro_crew.messaging.turn_bracket")


def _dispatcher_files() -> dict[str, list[Path]]:
    """Each channel's dispatch modules, keyed by the channel's package name."""
    root = src_root()
    found: dict[str, list[Path]] = {}
    for path in sorted(root.glob("*/transport_dispatch.py")) + sorted(root.glob("*/dispatch/*.py")):
        channel = path.relative_to(root).parts[0]
        found.setdefault(channel, []).append(path)
    return found


def _forks(paths: list[Path]) -> list[str]:
    """Which pipeline-only constructs *paths* spell, as ``file: construct``."""
    return [
        f"{path.name}: {construct}"
        for path in paths
        for construct in _PIPELINE_ONLY
        if construct in path.read_text(encoding="utf-8")
    ]


def _delegates(paths: list[Path]) -> bool:
    """Whether *paths* hand their turns to the pipeline."""
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    return ("ChannelTurns(" in text and ".answer(" in text) or "drive_turn(" in text


def test_every_dispatcher_outside_pending_delegates_its_turns() -> None:
    # Delegation stands in for the identity publish only while the pipeline
    # itself publishes; without it every delegating channel loses X-Session-Key.
    pipeline = src_root() / "messaging" / "dispatch.py"
    assert "publish_turn_identity(" in pipeline.read_text(encoding="utf-8"), (
        "messaging/dispatch.py no longer publishes the turn identity, which every "
        "delegating channel depends on (#232: managed MCP tools answer 400)"
    )
    dispatchers = _dispatcher_files()
    missing = [
        channel
        for channel, paths in dispatchers.items()
        if channel not in PENDING and channel not in _RUNS_NO_TURN and not _delegates(paths)
    ]
    assert not missing, (
        "a channel dispatcher neither forks nor delegates its turns; answer through "
        f"messaging.dispatch.ChannelTurns: {missing}"
    )
    stale = [channel for channel in _RUNS_NO_TURN if _delegates(dispatchers.get(channel, []))]
    assert not stale, f"these now delegate; drop them from _RUNS_NO_TURN: {stale}"


def test_the_delegation_check_needs_an_answer_not_just_an_import(tmp_path: Path) -> None:
    answers = tmp_path / "answers.py"
    answers.write_text("t = ChannelTurns('x')\nawait t.answer(a, b, c)\n", encoding="utf-8")
    shim = tmp_path / "shim.py"
    shim.write_text("await drive_turn(turn, sessions=s, ctx_builder=c)\n", encoding="utf-8")
    builds_only = tmp_path / "builds_only.py"
    builds_only.write_text("t = ChannelTurns('x')\n", encoding="utf-8")
    assert _delegates([answers]) and _delegates([shim])
    assert not _delegates([builds_only])


def test_no_dispatcher_outside_pending_forks_the_turn_loop() -> None:
    dispatchers = _dispatcher_files()
    # Non-vacuity: the channels this file knows ride the pipeline are all scanned.
    assert {"discord", "feishu", "teams", "webex", "whatsapp"} <= set(dispatchers)
    offenders = [
        f"{channel}/{hit}"
        for channel, paths in dispatchers.items()
        if channel not in PENDING
        for hit in _forks(paths)
    ]
    assert not offenders, (
        "a channel dispatcher outside PENDING runs its own copy of the turn loop; "
        "drive it through messaging.dispatch.ChannelTurns:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize("channel", sorted(PENDING))
def test_every_pending_entry_still_forks_and_publishes_its_identity(channel: str) -> None:
    paths = _dispatcher_files().get(channel, [])
    assert _forks(paths), f"{channel} no longer forks the turn loop: drop it from PENDING"
    assert any("publish_turn_identity(" in path.read_text(encoding="utf-8") for path in paths), (
        f"{channel} runs its own turn without publishing the turn identity, so managed "
        "MCP tools cannot resolve X-Session-Key there"
    )


def test_the_scan_sees_a_fork_when_there_is_one(tmp_path: Path) -> None:
    fork = tmp_path / "transport_dispatch.py"
    fork.write_text("driver = TurnDriver(provider, renderer)\n", encoding="utf-8")
    rider = tmp_path / "rider.py"
    rider.write_text("outcome = await turns.answer(asker, text, renderer)\n", encoding="utf-8")
    assert _forks([fork, rider]) == ["transport_dispatch.py: TurnDriver("]


def test_discord_drift_is_the_pinned_set_with_a_ruling_per_member() -> None:
    assert set(DISCORD_DRIFT) == set(_DISCORD_DRIFT_RULINGS), (
        "DISCORD_DRIFT changed: a member leaves only by its ruling (delete it here and "
        "from the set together); a member is never added"
    )
    assert all(ruling.strip() for ruling in _DISCORD_DRIFT_RULINGS.values())


def test_no_new_kind_of_drift_appears() -> None:
    assert {member.name for member in Drift} <= _ALL_DRIFT


def _pipeline_imports() -> set[tuple[str, str]]:
    root = src_root()
    names: set[tuple[str, str]] = set()
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith("messaging/"):
            continue
        source = path.read_text(encoding="utf-8")
        if not any(module in source for module in _PIPELINE_MODULES):
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module in _PIPELINE_MODULES:
                names.update((node.module, alias.name) for alias in node.names)
    return names


def test_the_export_budget_may_only_fall() -> None:
    imported = _pipeline_imports()
    # Non-vacuity: the channels' own entry points are among them.
    assert ("kiro_crew.messaging.dispatch", "ChannelTurns") in imported
    assert len(imported) <= _EXPORT_BUDGET, (
        f"{len(imported)} names are imported from the pipeline's modules, over the "
        f"budget of {_EXPORT_BUDGET}; narrow the interface rather than widen it:\n"
        + "\n".join(sorted(f"{m}.{n}" for m, n in imported))
    )
