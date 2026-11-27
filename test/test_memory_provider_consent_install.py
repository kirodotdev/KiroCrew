"""Memory egress consent names the install it was given to.

A grant matches the provider id, the app version and the install's ``installedAt``,
so another app installed later under the same name and version asks again. A grant
written before grants named their install still holds for the install that was
current when it was given (upgrade path: nobody is asked again). Uninstall and a new
install drop the app's grants.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture
from app_manager_helpers import _make_app_source

from kiro_crew.apps import manager_records
from kiro_crew.apps.manager import install_app, uninstall_app
from kiro_crew.apps.manager_records import InstalledApp
from kiro_crew.config.loader_locations import memory_provider_consent_path
from kiro_crew.memory_provider import app_client, consent

PID = "app:acme-mem/memory"
STAMP = "2026-11-10T09:00:00Z"
STAMP_EPOCH = 1794301200.0  # STAMP as epoch seconds


def _provider(installed_at: str = STAMP, version: str = "1.0.0") -> SimpleNamespace:
    return SimpleNamespace(provider_id=PID, app_version=version, installed_at=installed_at)


def _write_grants(*grants: dict) -> None:
    path = memory_provider_consent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"grants": list(grants)}), encoding="utf-8")


def _legacy(granted_at: float) -> dict:
    """A grant as builds before this one wrote it: no ``installed_at``."""
    return {"provider_id": PID, "app_version": "1.0.0", "egress": True, "granted_at": granted_at}


def test_consent_is_bound_to_the_install() -> None:
    consent.grant_egress(PID, "1.0.0", STAMP)
    assert consent.has_egress_consent(_provider(STAMP))
    assert not consent.has_egress_consent(_provider("2026-11-12T10:00:00Z"))
    assert not consent.has_egress_consent(_provider(STAMP, version="1.1.0"))


def test_a_legacy_grant_holds_for_the_install_it_was_given_to() -> None:
    _write_grants(_legacy(STAMP_EPOCH + 300))
    assert consent.has_egress_consent(_provider(STAMP))
    # Same second as the install stamp (second resolution): still the same install.
    _write_grants(_legacy(STAMP_EPOCH + 0.5))
    assert consent.has_egress_consent(_provider(STAMP))


def test_a_legacy_grant_older_than_the_install_belongs_to_an_earlier_one() -> None:
    _write_grants(_legacy(STAMP_EPOCH - 1))
    assert not consent.has_egress_consent(_provider(STAMP))


@pytest.mark.parametrize("stamp", ["", "not-a-time"])
def test_a_legacy_grant_with_no_readable_install_stamp_keeps_its_answer(stamp: str) -> None:
    _write_grants(_legacy(1.0))
    assert consent.has_egress_consent(_provider(stamp))
    # A provider with no stamp at all (a test double, an unread record) reads the same.
    assert consent.has_egress_consent(SimpleNamespace(provider_id=PID, app_version="1.0.0"))


def test_a_grant_given_with_no_install_stamp_is_kept_as_legacy() -> None:
    consent.grant_egress(PID, "1.0.0")
    (stored,) = consent.read_grants()
    assert "installed_at" not in stored
    assert consent.has_egress_consent(_provider(STAMP))


def test_revoke_app_drops_only_that_apps_grants() -> None:
    consent.grant_egress(PID, "1.0.0", STAMP)
    consent.grant_egress("app:acme-mem-two/memory", "1.0.0", STAMP)
    consent.revoke_app("acme-mem")
    assert [g["provider_id"] for g in consent.read_grants()] == ["app:acme-mem-two/memory"]


def test_revoke_app_soft_logs_a_failure(monkeypatch, caplog) -> None:
    def fail(_name: str) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(consent, "revoke_app", fail)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.memory_provider"):
        consent.revoke_app_soft("acme-mem")
    assert "could not remove memory consent" in caplog.text


def test_the_provider_client_is_keyed_on_the_install(monkeypatch, app_home) -> None:
    decl = SimpleNamespace(id="memory", route="/mem", display_name="Acme")
    manifest = SimpleNamespace(
        version="1.0.0",
        contributes=SimpleNamespace(memoryProviders=SimpleNamespace(usable=lambda: [decl])),
    )
    monkeypatch.setattr(app_client, "_manifest", lambda _app: manifest)
    monkeypatch.setattr(app_client, "_enabled", lambda _app: True)
    app_client.reset_clients()

    def install(stamp: str) -> None:
        manager_records.app_dir("acme-mem").mkdir(parents=True, exist_ok=True)
        manager_records._write_installed(
            "acme-mem",
            InstalledApp(name="acme-mem", version="1.0.0", enabled=True, installedAt=stamp),
        )

    install(STAMP)
    first = app_client.provider_for_id(PID)
    assert first is not None and first.installed_at == STAMP
    assert first.run_copy().installed_at == STAMP
    assert app_client.provider_for_id(PID) is first
    install("2026-11-12T10:00:00Z")
    second = app_client.provider_for_id(PID)
    assert second is not first and second.installed_at == "2026-11-12T10:00:00Z"
    app_client.reset_clients()


def test_install_and_uninstall_drop_the_apps_grants(tmp_path, app_home) -> None:
    other = "app:other-app/memory"
    _write_grants(
        {
            "provider_id": "app:test-app/m",
            "app_version": "1.0.0",
            "egress": True,
            "granted_at": 1.0,
        },
        {"provider_id": other, "app_version": "1.0.0", "egress": True, "granted_at": 1.0},
    )
    assert install_app(_make_app_source(tmp_path)).ok
    assert [g["provider_id"] for g in consent.read_grants()] == [other]

    consent.grant_egress("app:test-app/m", "1.0.0", "2026-11-12T10:00:00Z")
    assert uninstall_app("test-app").ok
    assert [g["provider_id"] for g in consent.read_grants()] == [other]


def test_an_install_refuses_while_an_earlier_consent_cannot_be_removed(
    tmp_path, app_home, monkeypatch
) -> None:
    _write_grants(
        {"provider_id": "app:test-app/m", "app_version": "1.0.0", "egress": True, "granted_at": 1.0}
    )

    def fail(_document: dict) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(consent, "_write", fail)
    result = install_app(_make_app_source(tmp_path))
    assert not result.ok and "memory consent" in (result.error or "")
    assert not (app_home / "apps" / "test-app").exists()


def test_an_install_with_nothing_to_clear_never_writes_the_record(
    tmp_path, app_home, monkeypatch
) -> None:
    def fail(_document: dict) -> None:
        raise AssertionError("nothing to clear, nothing written")

    monkeypatch.setattr(consent, "_write", fail)
    assert install_app(_make_app_source(tmp_path)).ok


def test_every_rewrite_takes_the_cross_process_lock(monkeypatch) -> None:
    import contextlib

    from kiro_crew.apps import protected_app_state

    taken: list[str] = []

    @contextlib.contextmanager
    def lock():
        taken.append("lock")
        yield

    monkeypatch.setattr(protected_app_state, "memory_consent_lock", lock)
    consent.grant_egress(PID, "1.0.0", STAMP)
    consent.revoke_app("acme-mem")
    consent.revoke_app("acme-mem")  # nothing left: no rewrite, no lock
    consent.revoke_egress(PID)
    assert taken == ["lock", "lock", "lock"]


@pytest.mark.parametrize("text", ["", '{"grants": [{"provider_id": "app:acme-mem/m'])
def test_a_record_caught_mid_rewrite_is_checked_under_the_lock(text, monkeypatch) -> None:
    import contextlib

    from kiro_crew.apps import protected_app_state

    taken: list[str] = []

    @contextlib.contextmanager
    def lock():
        taken.append("lock")
        yield

    path = memory_provider_consent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(protected_app_state, "memory_consent_lock", lock)
    with contextlib.suppress(ValueError):
        consent.revoke_app("acme-mem")
    assert taken == ["lock"]


def test_a_sandbox_placeholder_record_needs_no_lock(monkeypatch) -> None:
    from kiro_crew.apps import protected_app_state

    def refuse():
        raise OSError("masked")

    path = memory_provider_consent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(protected_app_state, "memory_consent_lock", refuse)
    consent.revoke_app("acme-mem")


def test_an_oversized_install_stamp_never_reaches_the_record(monkeypatch, app_home) -> None:
    decl = SimpleNamespace(id="memory", route="/mem", display_name="Acme")
    manifest = SimpleNamespace(
        version="1.0.0",
        contributes=SimpleNamespace(memoryProviders=SimpleNamespace(usable=lambda: [decl])),
    )
    monkeypatch.setattr(app_client, "_manifest", lambda _app: manifest)
    monkeypatch.setattr(app_client, "_enabled", lambda _app: True)
    app_client.reset_clients()
    manager_records.app_dir("acme-mem").mkdir(parents=True, exist_ok=True)
    manager_records._write_installed(
        "acme-mem",
        InstalledApp(name="acme-mem", version="1.0.0", enabled=True, installedAt="9" * 4096),
    )
    provider = app_client.provider_for_id(PID)
    assert provider is not None and provider.installed_at == ""
    consent.grant_egress(PID, "1.0.0", "9" * 4096)
    (stored,) = consent.read_grants()
    assert "installed_at" not in stored
    app_client.reset_clients()


@pytest.mark.asyncio
async def test_a_registry_registration_waits_for_a_contended_consent_lock(
    monkeypatch, app_home
) -> None:
    """The registry path clears earlier consent in a worker, which waits for the lock
    another writer holds, where an attempt on the loop would refuse at once."""
    import asyncio
    import contextlib
    import threading

    from kiro_crew.apps import protected_app_state
    from kiro_crew.apps.registry_pipeline import install as registry_install

    real_lock = protected_app_state.memory_consent_lock
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()

    @contextlib.contextmanager
    def signalling_lock():
        loop.call_soon_threadsafe(entered.set)
        with real_lock():
            yield

    _write_grants({"provider_id": PID, "app_version": "1.0.0", "egress": True, "granted_at": 1.0})
    held, release = threading.Event(), threading.Event()

    def holder() -> None:
        with real_lock():
            held.set()
            release.wait(30)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert held.wait(30)
    monkeypatch.setattr(protected_app_state, "memory_consent_lock", signalling_lock)
    task = asyncio.ensure_future(registry_install._clear_earlier_memory_consent("acme-mem"))
    await asyncio.wait_for(entered.wait(), 30)
    assert not task.done()
    release.set()
    assert await asyncio.wait_for(task, 30) == ""
    thread.join(30)
    assert consent.read_grants() == []
