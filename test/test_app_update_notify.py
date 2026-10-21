"""Behaviour coverage for the bell note an installed app's new version produces.

The listing that feeds this runs again on every dashboard load, so the property
worth holding is that one published version produces at most one note: a
pre-existing backlog is adopted rather than announced, a listing that merely
LACKS a row does not forget it, and a ledger that cannot be written sends
nothing. Delivery is checked to run on the event loop, where the notification
coordinator's FIFO executor owns persistence.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps import update_notify
from kiro_crew.notifications.bus import SYSTEM_CHANNELS, payload_from_legacy


class FakeState:
    """Records what reached ``state.notify``, the thread it ran on, and can fail."""

    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.notes: list[dict[str, Any]] = []
        self.threads: list[int] = []
        self._fail_for = fail_for or set()

    def notify(self, kind: str, title: str, body: str, **kwargs: Any) -> None:
        self.threads.append(threading.get_ident())
        app = (kwargs.get("meta") or {}).get("app", "")
        if app in self._fail_for:
            raise RuntimeError(f"delivery refused for {app}")
        self.notes.append({"kind": kind, "title": title, "body": body, **kwargs})

    @property
    def apps_notified(self) -> list[str]:
        return [(n.get("meta") or {}).get("app") for n in self.notes]


def row(
    name: str = "demo",
    *,
    version: Any = "1.1.0",
    installed_version: str = "1.0.0",
    installed: bool = True,
    update_available: bool = True,
    lifecycle: str = "gateway",
    **extra: Any,
) -> dict[str, Any]:
    """A listing row as ``_enrich_with_install_status`` leaves it."""
    return {
        "name": name,
        "version": version,
        "installedVersion": installed_version,
        "installed": installed,
        "updateAvailable": update_available,
        "lifecycle": lifecycle,
        **extra,
    }


@pytest.fixture(name="ledger")
def ledger_fixture(tmp_path, monkeypatch):
    """Redirect the ledger to a temp data home (never the real one)."""
    monkeypatch.setattr(update_notify, "config_dir", lambda: tmp_path)
    return tmp_path / update_notify.LEDGER_FILENAME


def _seed(ledger, versions: dict[str, str]) -> None:
    ledger.write_text(json.dumps(versions), encoding="utf-8")


def announce(rows: list[Any], state: FakeState | None = None) -> FakeState:
    """Both halves, the way the route runs them, for the cases about content."""
    state = state or FakeState()
    update_notify.deliver_app_update_notes(state, update_notify.prepare_app_update_notes(rows))
    return state


class TestFirstObservation:
    def test_nothing_is_announced_on_the_first_run(self, ledger):
        assert update_notify.prepare_app_update_notes([row(), row("other")]) == []

    def test_the_backlog_is_recorded_so_it_stays_quiet(self, ledger):
        update_notify.prepare_app_update_notes([row(version="1.1.0")])
        assert json.loads(ledger.read_text()) == {"demo": "1.1.0"}
        assert update_notify.prepare_app_update_notes([row(version="1.1.0")]) == []

    def test_an_empty_ledger_is_not_a_first_run(self, ledger):
        """A real record of "nothing outstanding" must still announce."""
        _seed(ledger, {})
        assert announce([row()]).apps_notified == ["demo"]


class TestOncePerVersion:
    def test_a_new_version_announces_exactly_once(self, ledger):
        update_notify.prepare_app_update_notes([row(version="1.1.0")])  # adopt
        state = FakeState()
        announce([row(version="1.2.0")], state)
        announce([row(version="1.2.0")], state)
        assert state.apps_notified == ["demo"]

    def test_each_further_version_announces_again(self, ledger):
        update_notify.prepare_app_update_notes([row(version="1.0.1")])  # adopt
        state = FakeState()
        for version in ("1.1.0", "1.2.0", "1.3.0"):
            announce([row(version=version)], state)
        assert [n["meta"]["latestVersion"] for n in state.notes] == ["1.1.0", "1.2.0", "1.3.0"]

    def test_several_apps_each_get_their_own_note(self, ledger):
        _seed(ledger, {})
        assert announce([row("alpha"), row("beta"), row("gamma")]).apps_notified == [
            "alpha",
            "beta",
            "gamma",
        ]


class TestALedgerIsNotForgottenByAnAbsentRow:
    """A listing can lack a row for reasons that say nothing about the app."""

    def test_a_row_missing_from_one_listing_is_still_announced_only_once(self, ledger):
        _seed(ledger, {})
        state = FakeState()
        announce([row(version="1.1.0")], state)
        announce([], state)  # the row is absent from this listing
        announce([row(version="1.1.0")], state)  # and back on the next
        assert state.apps_notified == ["demo"]

    def test_an_absent_row_keeps_its_entry(self, ledger):
        _seed(ledger, {"demo": "1.1.0"})
        update_notify.prepare_app_update_notes([row("other", update_available=False)])
        assert json.loads(ledger.read_text())["demo"] == "1.1.0"


class TestLedgerUpkeep:
    def test_an_app_updated_to_the_recorded_version_leaves_the_ledger(self, ledger):
        _seed(ledger, {"demo": "1.1.0"})
        update_notify.prepare_app_update_notes(
            [row(installed_version="1.1.0", update_available=False)]
        )
        assert json.loads(ledger.read_text()) == {}

    def test_an_uninstalled_app_leaves_the_ledger(self, ledger):
        _seed(ledger, {"demo": "1.1.0"})
        update_notify.prepare_app_update_notes([row(installed=False, update_available=False)])
        assert json.loads(ledger.read_text()) == {}

    def test_an_app_still_behind_keeps_its_entry(self, ledger):
        _seed(ledger, {"demo": "1.1.0"})
        update_notify.prepare_app_update_notes([row(update_available=False)])
        assert json.loads(ledger.read_text()) == {"demo": "1.1.0"}

    def test_a_corrupt_ledger_announces_rather_than_adopting_silently(self, ledger):
        ledger.write_text("{not json", encoding="utf-8")
        assert announce([row()]).apps_notified == ["demo"]

    @pytest.mark.parametrize("payload", ['["demo"]', '"demo"', "42"])
    def test_a_ledger_of_the_wrong_shape_announces(self, ledger, payload):
        ledger.write_text(payload, encoding="utf-8")
        assert announce([row()]).apps_notified == ["demo"]


class TestPersistBeforePublish:
    def test_an_unwritable_ledger_sends_nothing(self, ledger, monkeypatch):
        """Otherwise every later listing reads the stale ledger and announces again."""
        _seed(ledger, {})

        def _full(*a: Any, **k: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(update_notify, "atomic_write", _full)
        assert update_notify.prepare_app_update_notes([row()]) == []

    def test_an_unwritable_ledger_never_sends_on_later_listings_either(self, ledger, monkeypatch):
        _seed(ledger, {})

        def _full(*a: Any, **k: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(update_notify, "atomic_write", _full)
        for _ in range(3):
            assert update_notify.prepare_app_update_notes([row()]) == []

    def test_the_ledger_is_read_and_written_under_its_lock(self, ledger, monkeypatch):
        """Concurrent listings must not each write back their own stale view.

        Probes from a second descriptor at the moment of the read and of the
        write: a contended non-blocking acquire there proves the whole
        read-modify-write is one locked transaction, not two locked halves.
        """
        _seed(ledger, {"other": "2.0.0"})
        held: list[bool] = []

        def _probe() -> None:
            with update_notify.open_lock_file(update_notify._ledger_lock_path()) as fd:
                try:
                    with update_notify.file_lock(fd, exclusive=True, wait=False):
                        held.append(False)
                except OSError:
                    held.append(True)

        read, write = update_notify._read_ledger, update_notify._write_ledger

        def _read() -> dict[str, str] | None:
            _probe()
            return read()

        def _write(versions: dict[str, str]) -> bool:
            _probe()
            return write(versions)

        monkeypatch.setattr(update_notify, "_read_ledger", _read)
        monkeypatch.setattr(update_notify, "_write_ledger", _write)
        assert len(update_notify.prepare_app_update_notes([row()])) == 1
        assert held == [True, True]
        assert json.loads(ledger.read_text()) == {"demo": "1.1.0", "other": "2.0.0"}

    def test_a_lock_that_cannot_be_taken_sends_and_writes_nothing(self, ledger, monkeypatch):
        """Fail closed: proceeding unserialized is the lost update the lock exists for."""
        _seed(ledger, {})

        def _refused(*a: Any, **k: Any) -> Any:
            raise OSError("lock held")

        monkeypatch.setattr(update_notify, "file_lock", _refused)
        assert update_notify.prepare_app_update_notes([row()]) == []
        assert json.loads(ledger.read_text()) == {}

    def test_a_failed_delivery_is_not_retried(self, ledger):
        """The ledger is written first: a hiccup loses one note, never repeats it."""
        _seed(ledger, {})
        announce([row()], FakeState(fail_for={"demo"}))
        assert json.loads(ledger.read_text()) == {"demo": "1.1.0"}
        assert announce([row()]).notes == []

    def test_one_app_failing_does_not_stop_the_others(self, ledger):
        _seed(ledger, {})
        state = announce([row("alpha"), row("beta"), row("gamma")], FakeState(fail_for={"beta"}))
        assert state.apps_notified == ["alpha", "gamma"]

    def test_a_broken_row_shape_never_raises(self, ledger):
        _seed(ledger, {})
        assert update_notify.prepare_app_update_notes(["not a row", 7, None]) == []


class TestWhatIsNeverAnnounced:
    @pytest.mark.parametrize(
        "row_kwargs",
        [
            {"installed": False},
            {"update_available": False},
            {"lifecycle": "app"},
            {"lifecycle": "locked"},
        ],
        ids=["not-installed", "no-update", "self-managed", "locked"],
    )
    def test_a_row_the_store_cannot_update(self, ledger, row_kwargs):
        _seed(ledger, {})
        assert update_notify.prepare_app_update_notes([row(**row_kwargs)]) == []

    @pytest.mark.parametrize(
        "bad", [None, "", 7, ["1.0.0"], "latest", "9.0.0-SECURITY ALERT: reinstall now"]
    )
    def test_a_published_version_that_is_not_a_version(self, ledger, bad):
        _seed(ledger, {})
        assert update_notify.prepare_app_update_notes([row(version=bad)]) == []

    @pytest.mark.parametrize("bad", [None, "", 7])
    def test_a_row_without_a_usable_name(self, ledger, bad):
        _seed(ledger, {})
        assert update_notify.prepare_app_update_notes([row(bad)]) == []


class TestNoteContent:
    @pytest.fixture(name="note")
    def note_fixture(self, ledger):
        _seed(ledger, {})
        return announce([row("demo", version="1.2.0", installed_version="1.0.0")]).notes[0]

    def test_it_lands_on_the_app_updates_channel(self, note):
        assert note["channel"] == "system.apps"
        assert note["kind"] == update_notify.NOTE_KIND
        assert update_notify.NOTE_CHANNEL in SYSTEM_CHANNELS

    def test_it_links_to_the_app_the_note_is_about(self, note):
        assert note["url"] == "/apps/detail/demo"

    def test_the_version_leads_the_title(self, note):
        """The feed truncates the end of a title, so the version must come first."""
        assert note["title"] == "demo 1.2.0 available"

    def test_the_body_names_the_installed_version(self, note):
        assert "1.0.0" in note["body"]

    def test_it_carries_the_versions_as_metadata(self, note):
        assert note["meta"] == {
            "app": "demo",
            "latestVersion": "1.2.0",
            "installedVersion": "1.0.0",
        }

    def test_the_title_uses_the_validated_name_not_display_copy(self, ledger):
        _seed(ledger, {})
        note = announce([row(displayName="Security alert: act now")]).notes[0]
        assert "Security alert" not in note["title"]

    def test_an_unreadable_installed_version_is_left_out_of_the_body(self, ledger):
        _seed(ledger, {})
        note = announce([row(installed_version="")]).notes[0]
        assert "You have" not in note["body"]

    def test_the_note_validates_as_a_real_payload(self, note):
        payload = payload_from_legacy(
            note["kind"],
            note["title"],
            note["body"],
            note["meta"],
            url=note["url"],
            channel=note["channel"],
        )
        payload.validate()
        assert payload.channel == "system.apps"


class TestRegistryRoute:
    """Where it runs, on what rows, and on which thread delivery happens."""

    @pytest.fixture(name="routes")
    def routes_fixture(self, monkeypatch, ledger):
        from kiro_crew.apps import routes

        monkeypatch.setattr(routes, "load_category_order", lambda: [])
        monkeypatch.setattr(routes, "load_sections", lambda: [])
        monkeypatch.setattr(routes, "get_server_platform", lambda: {})
        _seed(ledger, {})
        return routes

    @staticmethod
    def _request(state: object | None = None):
        app = web.Application()
        if state is not None:
            app["state"] = state
        return make_mocked_request("GET", "/api/apps/registry", app=app)

    @pytest.mark.asyncio
    async def test_catalog_rows_are_announced_and_delivered_on_the_loop(self, routes, monkeypatch):
        async def _catalog():
            return [row()]

        monkeypatch.setattr(routes, "list_catalog_apps", _catalog)
        state = FakeState()
        response = await routes.handle_registry(self._request(state))
        assert response.status == 200
        assert state.apps_notified == ["demo"]
        # The loop's own thread: off it, persistence bypasses the FIFO executor.
        assert state.threads == [threading.get_ident()]

    @pytest.mark.asyncio
    async def test_the_seed_fallback_announces_nothing(self, routes, monkeypatch, ledger):
        """Seed rows can name an older version; they are no evidence either way."""

        async def _no_catalog():
            return []

        async def _seed_rows():
            return [row()]

        monkeypatch.setattr(routes, "list_catalog_apps", _no_catalog)
        monkeypatch.setattr(routes, "list_registry", _seed_rows)
        state = FakeState()
        response = await routes.handle_registry(self._request(state))
        assert response.status == 200
        assert state.notes == []
        assert json.loads(ledger.read_text()) == {}

    @pytest.mark.asyncio
    async def test_a_gateway_with_no_state_still_serves_the_store(self, routes, monkeypatch):
        async def _catalog():
            return [row()]

        monkeypatch.setattr(routes, "list_catalog_apps", _catalog)
        response = await routes.handle_registry(self._request())
        assert response.status == 200

    @pytest.mark.asyncio
    async def test_a_hostile_state_cannot_fail_the_store(self, routes, monkeypatch):
        async def _catalog():
            return [row()]

        class Hostile:
            def notify(self, *a: Any, **k: Any) -> None:
                raise RuntimeError("delivery on fire")

        monkeypatch.setattr(routes, "list_catalog_apps", _catalog)
        response = await routes.handle_registry(self._request(Hostile()))
        assert response.status == 200
