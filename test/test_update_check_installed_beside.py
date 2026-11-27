"""An update another process installed must not be offered again.

``kirocrew update`` in a terminal, a re-run of the install script, or a second
gateway on the same install builds the offered version beside the running one and
repoints ``crew-venv-current`` without the running gateway taking part. Before this,
the gateway's "update ready, restart to use it" note was set only by its own apply,
so the dashboard kept offering an update a restart alone was missing, and pressing
it built the same version again.

Contract under test:
- a check that finds a newer version a restart would already start marks the note
- a newer version a restart would NOT start (nothing installed, or a live target
  sending the next start elsewhere) leaves the note unset
- a found install is confirmed by reading the tree again under the layout's
  update lock, taken without waiting: a writer still holding it (the install
  script mid-rebuild, which may yet roll back) leaves the note unset for now,
  and a tree that reads as another version under the lock is not marked
- the restart tree's version is read from its dist-info, whichever managed tree it
  is: a versioned one ``kirocrew update`` built, or the fixed ``crew-venv`` the
  install script rebuilds in place, for the ``kirocrew`` and ``kirocrew-lts``
  distributions; a tree outside the managed layout (a pipx venv) reads as nothing
- an existing note for the same version keeps its ``mandatory`` flag
- a status frame looks too, so an install made after the last check reaches an
  open tab within one frame, and it looks only while an unnoted update is offered
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from update_check_install_aware_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _wheel_install,
)

from kiro_crew import update_restart_ready
from kiro_crew.dashboard.handlers import updates
from kiro_crew.platform import wheel_engine
from kiro_crew.service import live_target

_MANIFEST = {
    "algorithm": "RSASSA_PKCS1_V1_5_SHA_256",
    "channel": "insider",
    "key_id": "sha256:d3a83f0c",
    "pub_date": "2026-08-05T07:49:33Z",
    "python_requires": ">=3.10",
    "schema": "kirocrew-cli-artifact-manifest-v1",
    "sha256": "ea681adb",
    "signature": "V9MGrlYt",
    "version": "0.1.3rc2",
    "wheel_url": "https://download.crew.kiro.dev/cli/insider/0.1.3rc2/x.whl",
}


@pytest.fixture(autouse=True)
def _clean_note():
    update_restart_ready.clear()
    yield
    update_restart_ready.clear()


@pytest.fixture(autouse=True)
def lock(_floor_monkeypatch):
    """The layout's update lock, free unless a test makes it busy. Records take/release."""
    calls: list[str] = []
    state = {"busy": False}

    def _hold() -> int:
        calls.append("hold")
        if state["busy"]:
            raise wheel_engine.WheelUpdateBusy("another kirocrew update is already in progress")
        return 99

    def _release(fd: int) -> None:
        assert fd == 99
        calls.append("release")

    _floor_monkeypatch.setattr(wheel_engine, "hold_update_lock", _hold)
    _floor_monkeypatch.setattr(wheel_engine, "release_update_lock", _release)
    return {"calls": calls, "state": state}


@pytest.fixture
def feed(monkeypatch):
    async def _fake(url: str) -> tuple[int, bytes]:
        return 200, json.dumps(_MANIFEST).encode()

    monkeypatch.setattr(updates, "_fetch_feed_bytes", _fake)
    monkeypatch.setattr(updates, "_local_version", "0.1.2rc3")
    monkeypatch.setattr(live_target, "read_target", lambda: None)


def _reaches(monkeypatch, versions: set[str]) -> list[str]:
    """Stub the restart tree to hold the one version in *versions* (or none)."""
    asked: list[str] = []
    installed = next(iter(versions), "")

    def _fake() -> str:
        asked.append(installed)
        return installed

    monkeypatch.setattr(updates, "_restart_tree_version", _fake)
    return asked


def _tree(root, name: str, version: str, dist_name: str = "kirocrew"):
    """A venv tree holding *dist_name* *version*, as the installer or the engine lays it out."""
    site = root / name / "lib" / "python3.12" / "site-packages"
    dist = site / f"{dist_name.replace('-', '_')}-{version}.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {dist_name}\nVersion: {version}\n\nlong description\n",
        encoding="utf-8",
    )
    (root / name / "bin").mkdir()
    (root / name / "bin" / "kirocrew").write_text("#!/bin/sh\n", encoding="utf-8")
    return root / name


@pytest.fixture
def layout(monkeypatch, tmp_path):
    """The managed layout rooted under ``tmp_path``: ``crew-venv`` and its siblings."""
    managed = wheel_engine.ManagedVenvLayout(
        legacy=tmp_path / "crew-venv", stable_link=tmp_path / "crew-venv-current"
    )
    monkeypatch.setattr(wheel_engine, "managed_venv_layout", lambda: managed)
    return managed


def test_a_version_installed_outside_the_gateway_is_marked_ready(monkeypatch, feed):
    asked = _reaches(monkeypatch, {"0.1.3rc2"})

    asyncio.run(updates._do_update_check())

    # Read once, then once more under the update lock.
    assert asked == ["0.1.3rc2", "0.1.3rc2"]
    assert updates.get_update_info()["update_available"] is True
    fields = update_restart_ready.status_fields()
    assert fields["update_restart_ready"] is True
    assert fields["update_restart_ready_version"] == "0.1.3rc2"
    assert fields["update_restart_ready_reason"] == update_restart_ready.REASON_STAGED


def test_a_version_a_restart_would_not_start_is_left_offered(monkeypatch, feed, lock):
    _reaches(monkeypatch, set())

    asyncio.run(updates._do_update_check())

    assert updates.get_update_info()["update_available"] is True
    assert update_restart_ready.current() is None
    # Nothing new on disk: the lock is never contended for.
    assert lock["calls"] == []


def test_a_found_install_is_confirmed_under_the_update_lock(monkeypatch, feed, lock):
    _reaches(monkeypatch, {"0.1.3rc2"})

    asyncio.run(updates._do_update_check())

    assert update_restart_ready.current() is not None
    assert lock["calls"] == ["hold", "release"]


def test_an_install_still_holding_the_lock_is_not_marked_yet(monkeypatch, feed, lock):
    # The install script is mid-rebuild: the tree already reads as the new
    # version, but its EXIT rollback may still restore the old one.
    _reaches(monkeypatch, {"0.1.3rc2"})
    lock["state"]["busy"] = True

    asyncio.run(updates._do_update_check())

    assert update_restart_ready.current() is None

    # Once it commits and lets go, the next look marks it.
    lock["state"]["busy"] = False
    updates.note_installed_beside("0.1.3rc2")
    assert update_restart_ready.current() is not None


def test_a_tree_rolled_back_before_the_lock_is_free_is_not_marked(monkeypatch, feed, lock):
    reads = iter(["0.1.3rc2", "0.1.2rc3"])
    monkeypatch.setattr(updates, "_restart_tree_version", lambda: next(reads))

    updates.note_installed_beside("0.1.3rc2")

    assert update_restart_ready.current() is None
    assert lock["calls"] == ["hold", "release"]


def test_a_live_target_elsewhere_is_not_read_as_installed(monkeypatch, feed, tmp_path):
    _reaches(monkeypatch, {"0.1.3rc2"})
    monkeypatch.setattr(live_target, "read_target", lambda: tmp_path / "elsewhere")

    asyncio.run(updates._do_update_check())

    assert update_restart_ready.current() is None


def test_an_unreadable_layout_reports_nothing(monkeypatch, feed):
    def _boom() -> str:
        raise OSError("layout unreadable")

    monkeypatch.setattr(updates, "_restart_tree_version", _boom)

    asyncio.run(updates._do_update_check())

    assert updates.get_update_info()["check_status"] == "succeeded"
    assert update_restart_ready.current() is None


def test_a_mandatory_note_for_the_same_version_keeps_its_flag(monkeypatch, feed):
    asked = _reaches(monkeypatch, {"0.1.3rc2"})
    update_restart_ready.mark(
        update_restart_ready.REASON_STAGED, version="0.1.3rc2", mandatory=True
    )

    asyncio.run(updates._do_update_check())

    note = update_restart_ready.current()
    assert note is not None and note.mandatory is True
    assert asked == []


def _offer(version: str = "0.1.3rc2") -> None:
    updates._set_update_info(
        update_available=True, latest_version=version, check_status="succeeded"
    )


def test_the_frame_looks_only_while_an_unnoted_update_is_offered(monkeypatch):
    updates._set_update_info(update_available=False, latest_version="0.1.3rc2")
    assert updates.unnoted_offer() == ""

    _offer()
    assert updates.unnoted_offer() == "0.1.3rc2"

    update_restart_ready.mark(update_restart_ready.REASON_STAGED, version="0.1.3rc2")
    assert updates.unnoted_offer() == ""


def test_a_status_frame_notes_an_install_made_after_the_check(monkeypatch):
    # The check ran before `kirocrew update` finished, so it found nothing on
    # disk; the next frame must pick the install up without another check.
    from kiro_crew.dashboard import status_counts

    _offer()
    monkeypatch.setattr(live_target, "read_target", lambda: None)
    _reaches(monkeypatch, {"0.1.3rc2"})
    state = MagicMock()
    captured: dict[str, object] = {}

    def _snapshot(**fields: object) -> dict[str, object]:
        captured.update(fields)
        return dict(fields)

    state.status_snapshot = _snapshot

    async def _counts(_state: object) -> tuple[None, None]:
        return None, None

    async def _prime() -> None:
        return None

    monkeypatch.setattr(status_counts, "_refresh_status_counts", _counts)
    monkeypatch.setattr(updates, "prime_status_auto_update_effect", _prime)
    monkeypatch.setattr(status_counts.DashboardState, "served_bundle_id", staticmethod(lambda: ""))

    asyncio.run(status_counts.cached_status_snapshot(state))

    assert captured["update_restart_ready"] is True
    assert captured["update_restart_ready_version"] == "0.1.3rc2"


@pytest.mark.parametrize(
    "tree_name",
    [
        # `kirocrew update` (or a second gateway) built a versioned tree.
        "crew-venv-0.1.3rc2",
        # A re-run of the install script rebuilt the fixed tree in place.
        "crew-venv",
    ],
)
def test_the_restart_tree_is_read_whatever_installed_it(monkeypatch, tmp_path, layout, tree_name):
    tree = _tree(tmp_path, tree_name, "0.1.3rc2")
    monkeypatch.setattr(wheel_engine, "respawn_executable", lambda: str(tree / "bin" / "python3"))

    assert updates._restart_tree_version() == "0.1.3rc2"


def test_the_lts_distribution_is_read_too(monkeypatch, tmp_path, layout):
    # LTS ships as `kirocrew-lts`, so its metadata is kirocrew_lts-<v>.dist-info.
    tree = _tree(tmp_path, "crew-venv", "0.1.3", dist_name="kirocrew-lts")
    monkeypatch.setattr(wheel_engine, "respawn_executable", lambda: str(tree / "bin" / "python3"))

    assert updates._restart_tree_version() == "0.1.3"


def test_a_restart_tree_without_kirocrew_reads_as_nothing(monkeypatch, tmp_path, layout):
    (tmp_path / "crew-venv" / "bin").mkdir(parents=True)
    monkeypatch.setattr(
        wheel_engine, "respawn_executable", lambda: str(tmp_path / "crew-venv" / "bin" / "python3")
    )

    assert updates._restart_tree_version() == ""


def test_a_tree_outside_the_managed_layout_reads_as_nothing(monkeypatch, tmp_path, layout):
    # A pipx venv: its own installer lock is not the layout's update lock, so
    # a read there cannot be confirmed as settled.
    tree = _tree(tmp_path / "pipx" / "venvs", "kirocrew", "0.1.3rc2")
    monkeypatch.setattr(wheel_engine, "respawn_executable", lambda: str(tree / "bin" / "python3"))

    assert updates._restart_tree_version() == ""


def test_the_fixed_tree_rebuilt_by_the_installer_is_marked_ready(
    monkeypatch, feed, tmp_path, layout
):
    # End to end through the real tree read: the install script re-run left
    # crew-venv-current -> crew-venv, now holding the offered version.
    tree = _tree(tmp_path, "crew-venv", "0.1.3rc2")
    monkeypatch.setattr(wheel_engine, "respawn_executable", lambda: str(tree / "bin" / "python3"))

    asyncio.run(updates._do_update_check())

    note = update_restart_ready.current()
    assert note is not None and note.version == "0.1.3rc2"


def test_no_newer_version_asks_nothing(monkeypatch, feed):
    asked = _reaches(monkeypatch, {"0.1.3rc2"})
    monkeypatch.setattr(updates, "_local_version", "0.1.3rc2")

    asyncio.run(updates._do_update_check())

    assert updates.get_update_info()["update_available"] is False
    assert asked == []
    assert update_restart_ready.current() is None
