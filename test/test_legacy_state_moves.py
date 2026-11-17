"""Adoption never strands a built-in's pending legacy state move (``legacy_state_moves``).

A built-in whose state core still moves out of a layout core owned (Notes: from
``md-notebook-staging/`` into ``apps/md-notebook/data/state/``) has a row in the
core-owned table. APPLY holds the swap until that move has sealed, running it itself
whether or not the app is enabled; a record already adopted under pin trust gets the
move at every boot; and the backend spawn waits for it on the same provenance. Nothing
here is reachable from a manifest.

The fixture app ``fixture-former`` stands in for the built-in (see
``builtin_adoption_helpers``); its row runs the REAL Notes moves
(``md_notebook_state.sealed_legacy_state``) with ``apps/fixture-former/data/state/`` as
the destination, so the swap carries what the move wrote. Offline and deterministic.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import MappingProxyType

import pytest
from builtin_adoption_helpers import (
    NAME,
    agent_section,
    build_world,
    requires_git,
    seed_agent,
    write_builtin_record,
)

from kiro_crew import md_notebook_state as mns
from kiro_crew.apps import builtin_adoption as adoption
from kiro_crew.apps import builtin_adoption_apply as apply_mod
from kiro_crew.apps import legacy_state_moves as moves
from kiro_crew.apps import manager
from kiro_crew.apps.builtin_adoption_gate import adopted_under_pin_trust
from kiro_crew.apps.builtin_adoption_state import read_adoption_markers, read_ledger
from kiro_crew.config.loader import _invalidate_config_cache

pytestmark = [requires_git]

#: The real Notes moves are pinned on POSIX only (``test_md_notebook_state``); the tests
#: that drive them through APPLY follow. The fake-row tests below run everywhere.
_POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="the real Notes moves are POSIX")

#: The one row the shipped table holds (Notes).
NOTES = "md-notebook"
PAT = b"ghp_the_users_token"
VAULTS = b'[{"id": "v1", "localPath": "/notes"}]\n'
SETTINGS = b'{"autoSync": true}\n'
STATE = {"pat": PAT, "vaults.json": VAULTS, "settings.json": SETTINGS}
OLDER_PID = 4242


class _HardKill(BaseException):
    """Stands in for a hard kill: no ``except Exception`` on the APPLY path catches it."""


class _Audit:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def log_api_access(self, **kwargs) -> None:
        self.rows.append(kwargs)

    def outcomes(self) -> list[str]:
        return [r["outcome"] for r in self.rows if r.get("operation") == "app_builtin_adoption"]


@pytest.fixture
def audit(monkeypatch):
    recorder = _Audit()
    import kiro_crew.sel as sel_mod

    monkeypatch.setattr(sel_mod, "sel", lambda: recorder)
    return recorder


class _Host:
    """The older-sandbox probe, answered by the test."""

    def __init__(self) -> None:
        self.older: list[int] | None = []
        self.calls = 0

    def probe(self) -> list[int] | None:
        self.calls += 1
        return self.older


@pytest.fixture
def host(monkeypatch):
    probe = _Host()
    monkeypatch.setattr(mns, "older_sandboxes_masking_only_the_old_dir", probe.probe)
    return probe


def _home() -> Path:
    return Path(os.environ["KIROCREW_HOME"])


def _state_dir() -> Path:
    return _home() / mns.STATE_DIR_LEAF


def _legacy_dir() -> Path:
    return _home().joinpath(*mns.LEGACY_DIR_PARTS)


def _app_state_dir() -> Path:
    return manager.app_dir(NAME) / "data" / "state"


def _fixture_row() -> moves.LegacyStateMove:
    return moves.LegacyStateMove(
        seal=lambda: mns.sealed_legacy_state(_state_dir(), _legacy_dir(), _app_state_dir()),
        start_refusal=lambda: None,
    )


def _install_row(monkeypatch, row: moves.LegacyStateMove | None = None) -> None:
    table = {**moves.LEGACY_STATE_MOVES, NAME: row or _fixture_row()}
    monkeypatch.setattr(moves, "LEGACY_STATE_MOVES", MappingProxyType(table))


@pytest.fixture
def world(tmp_path, monkeypatch, audit, host):
    w = build_world(tmp_path, monkeypatch)
    write_builtin_record()
    _invalidate_config_cache()
    _install_row(monkeypatch)
    return w


def _previous_layout() -> None:
    """A pre-move host: the state in ``md-notebook-staging/``, its first move sealed."""
    state = _state_dir()
    state.mkdir(parents=True)
    for name, data in STATE.items():
        (state / name).write_bytes(data)
    (state / mns.MIGRATION_MARKER).write_text("{}\n", encoding="utf-8")


def _oldest_layout() -> None:
    """An older host: the state at the bare ``workspace/md-notebook/`` names, no pass run."""
    legacy = _legacy_dir()
    legacy.mkdir(parents=True)
    for name, data in STATE.items():
        (legacy / name).write_bytes(data)


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _the_token_is_somewhere() -> bool:
    return PAT in (
        _read(_legacy_dir() / "pat"),
        _read(_state_dir() / "pat"),
        _read(_app_state_dir() / "pat"),
    )


def _assert_carried() -> None:
    for name, data in STATE.items():
        assert _read(_app_state_dir() / name) == data, name
    assert _read(_state_dir() / "pat") == b"", "the moved PAT is left at a second name"


async def _stage():
    return await adoption.run_staging_pass(now=lambda: apply_mod._utcnow())


async def _boot():
    return await adoption.apply_builtin_adoptions()


def _builtin_owned() -> bool:
    return manager.builtin_owns_installed(NAME)


# --------------------------------------------------------------------------- #
# The two failures the seam closes
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_a_disabled_built_in_s_state_moves_before_the_swap_and_is_carried(world, audit):
    """Disabled at the upgrade, so its backend never started and nothing moved it."""
    write_builtin_record(enabled=False)
    _previous_layout()
    assert await _stage() == {NAME: "staged"}
    assert _read(_app_state_dir() / "pat") is None, "STAGE moves nothing"
    assert await _boot() == {NAME: "applied"}
    record = manager._read_installed(NAME)
    assert record is not None and record.origin == "registry" and record.enabled is False
    _assert_carried()
    assert (_state_dir() / mns.APP_MOVE_MARKER).is_file()
    assert audit.outcomes() == ["staged", "applied"]


@_POSIX_ONLY
@pytest.mark.parametrize("older", [[OLDER_PID], None], ids=["older-sandbox", "unreadable"])
@pytest.mark.asyncio
async def test_an_older_sandbox_holds_the_swap_until_it_ends(world, host, monkeypatch, older):
    _previous_layout()
    await _stage()
    copies: list[str] = []
    real_prepare = apply_mod._prepare_tree

    def _counting(*a, **k):
        copies.append("copy")
        return real_prepare(*a, **k)

    monkeypatch.setattr(apply_mod, "_prepare_tree", _counting)
    host.older = older
    assert await _boot() == {NAME: "state_move_pending"}
    assert await _boot() == {NAME: "state_move_pending"}, "every boot asks again"
    assert _builtin_owned()
    assert copies == [], "a held boot copies and hashes no tree"
    ledger = read_ledger(NAME)
    assert ledger is not None and ledger.state == "staged"
    assert NAME not in read_adoption_markers()
    assert _read(_state_dir() / "pat") == PAT, "the token stays where every sandbox masks it"
    assert _read(_app_state_dir() / "pat") is None
    host.older = []
    assert await _boot() == {NAME: "applied"}
    _assert_carried()


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_a_crash_surviving_grant_is_rolled_back_while_the_move_holds_the_swap(
    world, host, monkeypatch
):
    _previous_layout()
    await _stage()
    original = manager.adopt_builtin_record

    def killed(*_a, **_k):
        raise _HardKill

    monkeypatch.setattr(manager, "adopt_builtin_record", killed)
    with pytest.raises(_HardKill):
        await _boot()
    monkeypatch.setattr(manager, "adopt_builtin_record", original)
    assert read_adoption_markers()[NAME]["state"] == "granting"
    # The move sealed before the grant. With it pending again, the hold meets the grant
    # the crash left and rolls it back, as the other holds before the swap do.
    (_state_dir() / mns.APP_MOVE_MARKER).unlink()
    host.older = [OLDER_PID]
    assert await _boot() == {NAME: "state_move_pending"}
    assert NAME not in read_adoption_markers()
    assert NAME not in agent_section(world.home).get("apps_trusted", [])
    assert _builtin_owned()


# --------------------------------------------------------------------------- #
# Crash at each step of the move, soft and hard
# --------------------------------------------------------------------------- #


def _fault_at(monkeypatch, step: str, exc: type[BaseException]) -> None:
    real_write, real_sync, real_empty = mns.atomic_write, mns.fsync_dir, mns._empty_in_place
    targets = {
        "first-copy": _state_dir() / "vaults.json",
        "first-seal": _state_dir() / mns.MIGRATION_MARKER,
        "second-copy-pat": _app_state_dir() / "pat",
        "second-copy-settings": _app_state_dir() / "settings.json",
        "second-seal": _state_dir() / mns.APP_MOVE_MARKER,
    }

    def _write(path, *a, **k):
        if step in targets and Path(path) == targets[step]:
            raise exc("injected")
        return real_write(path, *a, **k)

    def _sync(path, *a, **k):
        if step == "destination-sync" and Path(path) == _app_state_dir():
            raise exc("injected")
        if (
            step == "seal-sync"
            and Path(path) == _state_dir()
            and (_state_dir() / mns.APP_MOVE_MARKER).exists()
        ):
            raise exc("injected")
        return real_sync(path, *a, **k)

    def _empty(path, *a, **k):
        if step == "empty-old-pat" and Path(path) == _state_dir() / "pat":
            raise exc("injected")
        return real_empty(path, *a, **k)

    monkeypatch.setattr(mns, "atomic_write", _write)
    monkeypatch.setattr(mns, "fsync_dir", _sync)
    monkeypatch.setattr(mns, "_empty_in_place", _empty)


_STEPS = [
    "first-copy",
    "first-seal",
    "second-copy-pat",
    "second-copy-settings",
    "destination-sync",
    "second-seal",
    "seal-sync",
    "empty-old-pat",
]


@_POSIX_ONLY
@pytest.mark.parametrize("kind", ["error", "kill"])
@pytest.mark.parametrize("step", _STEPS)
@pytest.mark.asyncio
async def test_a_move_stopped_at_any_step_holds_the_swap_and_loses_nothing(
    world, monkeypatch, step, kind
):
    """From the oldest layout, so both moves run; the next boot finishes and adopts."""
    _oldest_layout()
    await _stage()
    real = (mns.atomic_write, mns.fsync_dir, mns._empty_in_place)
    exc: type[BaseException] = OSError if kind == "error" else _HardKill
    _fault_at(monkeypatch, step, exc)
    if kind == "kill":
        with pytest.raises(_HardKill):
            await _boot()
        assert _builtin_owned()
    elif step == "empty-old-pat":
        # Sealed before the old PAT is emptied: the swap goes on, and a later pass
        # empties the old copy.
        assert await _boot() == {NAME: "applied"}
    else:
        assert await _boot() == {NAME: "state_move_pending"}
        assert _builtin_owned()
    assert _the_token_is_somewhere()
    for attr, fn in zip(("atomic_write", "fsync_dir", "_empty_in_place"), real):
        monkeypatch.setattr(mns, attr, fn)
    outcome = await _boot()
    assert outcome in ({NAME: "applied"}, {})
    assert not _builtin_owned()
    _assert_carried()
    assert _read(_legacy_dir() / "pat") == b"", "the bare legacy PAT is emptied"


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_a_second_hard_link_on_the_old_pat_holds_the_swap(world, tmp_path):
    _previous_layout()
    extra = tmp_path / "second-name"
    os.link(_state_dir() / "pat", extra)
    await _stage()
    assert await _boot() == {NAME: "state_move_pending"}
    assert _builtin_owned()
    assert _read(_app_state_dir() / "pat") is None
    extra.unlink()
    assert await _boot() == {NAME: "applied"}
    _assert_carried()


# --------------------------------------------------------------------------- #
# An adopted record whose move never ran
# --------------------------------------------------------------------------- #


async def _adopt_without_the_row(monkeypatch) -> None:
    table = {k: v for k, v in moves.LEGACY_STATE_MOVES.items() if k != NAME}
    monkeypatch.setattr(moves, "LEGACY_STATE_MOVES", MappingProxyType(table))
    await _stage()
    assert await _boot() == {NAME: "applied"}
    assert adopted_under_pin_trust(NAME)
    _install_row(monkeypatch)


@_POSIX_ONLY
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.asyncio
async def test_an_adopted_record_whose_move_never_ran_gets_it_at_the_next_boot(
    world, monkeypatch, enabled
):
    write_builtin_record(enabled=enabled)
    await _adopt_without_the_row(monkeypatch)
    _previous_layout()
    assert await _boot() == {}
    _assert_carried()


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_an_adopted_record_s_move_waits_for_an_older_sandbox_too(world, host, monkeypatch):
    await _adopt_without_the_row(monkeypatch)
    _previous_layout()
    host.older = [OLDER_PID]
    await _boot()
    assert _read(_app_state_dir() / "pat") is None
    host.older = []
    await _boot()
    _assert_carried()


@pytest.mark.asyncio
async def test_an_adopted_record_s_backend_start_waits_for_its_row(world, monkeypatch):
    """Not the shipped built-in, so only the pin-trust branch asks the row; the
    spawn's side of the same answer is pinned in ``test_app_backend_spawn_env.py``."""
    await _adopt_without_the_row(monkeypatch)
    asked: list[str] = []

    def _refusal() -> str | None:
        asked.append("start")
        return "held"

    _install_row(monkeypatch, moves.LegacyStateMove(seal=lambda: True, start_refusal=_refusal))
    assert moves.backend_start_refusal(NAME, builtin=False) == "held"
    assert asked == ["start"]


# --------------------------------------------------------------------------- #
# Negative: nothing beyond the row, the provenance and the app it names
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_record_adopted_under_another_trust_gets_no_move(world, monkeypatch):
    """The move writes the former built-in's token into the record's data directory."""
    await _adopt_without_the_row(monkeypatch)
    seed_agent(
        world.home,
        {"apps_trusted_repositories": {NAME: "https://github.com/Elsewhere/kirocrew-app-x"}},
    )
    assert not adopted_under_pin_trust(NAME)
    _previous_layout()
    await _boot()
    assert _read(_state_dir() / "pat") == PAT
    assert _read(_app_state_dir() / "pat") is None
    assert moves.backend_start_refusal(NAME, builtin=False) is None


@pytest.mark.asyncio
async def test_an_app_without_a_row_is_never_held_and_another_app_s_move_never_runs(
    world, monkeypatch, host
):
    """Notes' pending move holds Notes alone, and runs for no other app's adoption."""
    table = {k: v for k, v in moves.LEGACY_STATE_MOVES.items() if k != NAME}
    monkeypatch.setattr(moves, "LEGACY_STATE_MOVES", MappingProxyType(table))
    _previous_layout()
    host.older = [OLDER_PID]
    await _stage()
    assert await _boot() == {NAME: "applied"}
    assert host.calls == 0, "no move was asked about"
    assert _read(_state_dir() / "pat") == PAT
    assert not (_home() / "apps" / NOTES).exists()


def test_a_third_party_record_gets_no_wait_and_no_move(tmp_path, monkeypatch):
    """No pin, no built-in: the row is never asked, so nothing is moved into its data dir."""
    asked: list[str] = []
    row = moves.LegacyStateMove(
        seal=lambda: asked.append("seal") or True,
        start_refusal=lambda: asked.append("start") or "held",
    )
    monkeypatch.setattr(moves, "LEGACY_STATE_MOVES", MappingProxyType({NAME: row}))
    assert moves.backend_start_refusal(NAME, builtin=False) is None
    moves.seal_adopted(NAME)
    assert asked == []
    assert moves.backend_start_refusal(NAME, builtin=True) == "held"
    assert asked == ["start"]
    assert moves.backend_start_refusal("another-app", builtin=True) is None
    assert moves.sealed_for_adoption("another-app") is True
    assert asked == ["start"]


@pytest.mark.asyncio
async def test_a_row_that_has_not_sealed_holds_the_swap_on_every_platform(world, monkeypatch):
    sealed = [False]
    asked: list[str] = []

    def _seal() -> bool:
        asked.append("seal")
        return sealed[0]

    _install_row(monkeypatch, moves.LegacyStateMove(seal=_seal, start_refusal=lambda: None))
    await _stage()
    assert await _boot() == {NAME: "state_move_pending"}
    assert _builtin_owned()
    ledger = read_ledger(NAME)
    assert ledger is not None and ledger.state == "staged"
    sealed[0] = True
    assert await _boot() == {NAME: "applied"}
    assert asked == ["seal", "seal"]
    # Adopted under pin trust: the row is asked again at every boot.
    assert await _boot() == {}
    assert asked == ["seal", "seal", "seal"]


def test_the_table_is_core_data_and_names_only_the_notes_move():
    assert isinstance(moves.LEGACY_STATE_MOVES, MappingProxyType)
    assert sorted(moves.LEGACY_STATE_MOVES) == [NOTES]
    with pytest.raises(TypeError):
        moves.LEGACY_STATE_MOVES["planted"] = moves.LEGACY_STATE_MOVES[NOTES]  # type: ignore[index]


def test_a_manifest_cannot_declare_a_move(tmp_path):
    """The row is never read from ``app.json``: a manifest naming one adds nothing."""
    from kiro_crew.apps.manifest import AppManifest

    manifest = AppManifest.from_dict(
        {"name": "planted", "version": "1.0.0", "legacyStateMoves": {"seal": "x"}}
    )
    assert manifest.name == "planted" and "legacyStateMoves" in manifest.extra
    assert moves.legacy_state_move("planted") is None


# --------------------------------------------------------------------------- #
# The Notes row's start gate keeps the built-in's wording
# --------------------------------------------------------------------------- #


def test_the_notes_row_refuses_an_unsealed_move_with_the_built_in_s_wording(monkeypatch):
    monkeypatch.setattr(mns, "sealed_default_legacy_state", lambda: False)
    assert moves.backend_start_refusal(NOTES, builtin=True) == (
        "not starting until its one-time state move finishes (see the warning above); "
        "the next start retries it"
    )


def test_the_notes_row_refuses_a_linked_legacy_directory_with_the_built_in_s_wording(
    monkeypatch,
):
    import kiro_crew.sandbox as sb

    monkeypatch.setattr(mns, "sealed_default_legacy_state", lambda: True)
    linked = str(_home().joinpath(*mns.LEGACY_DIR_PARTS))
    monkeypatch.setattr(sb, "_md_notebook_degraded_mask_dirs", lambda: [linked])
    assert moves.backend_start_refusal(NOTES, builtin=True) == (
        "not starting: workspace/md-notebook is reached through a link, so its writes "
        "would be lost on restart; replace the link with a real directory"
    )
    monkeypatch.setattr(sb, "_md_notebook_degraded_mask_dirs", lambda: ["/elsewhere/md-notebook"])
    assert moves.backend_start_refusal(NOTES, builtin=True) is None
