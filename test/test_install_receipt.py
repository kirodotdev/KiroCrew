"""Tests for anonymous official-catalog app install receipts."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew import beacon
from kiro_crew.apps import install_receipt, manager, registry
from kiro_crew.apps.manager import AppResult

_SECRET = "0123456789abcdef" * 4  # 64-hex receipt-only secret
_BEACON_ID = "0123456789abcdef0123456789abcdef"  # a heartbeat id, for independence checks

# The autouse fixture stubs install_receipt.receipt_secret for transport tests;
# TestReceiptSecret exercises the real implementation via this import-time ref.
_real_receipt_secret = install_receipt.receipt_secret


@pytest.fixture(autouse=True)
def _clean_pending_receipt_threads():
    """Drop any leftover dispatch()-tracked thread entries between tests.

    Tests that stub ``threading.Thread`` (e.g. to assert on constructor
    kwargs) hand ``dispatch()`` a fake object that is added to
    ``install_receipt._pending_threads`` but never runs, so it never removes
    itself the way a real worker's ``finally`` block does. Left alone these
    fakes just accumulate harmlessly (wait_for_pending_receipt_writes ignores
    anything that is not a real Thread), but clearing them keeps each test's
    view of pending work honest.
    """
    yield
    with install_receipt._pending_lock:
        install_receipt._pending_threads.clear()


@pytest.fixture(autouse=True)
def _eligible_telemetry_host(monkeypatch):
    """Neutralize ambient CI/dev-home suppression for explicit gate tests."""
    monkeypatch.delenv(beacon.DISABLE_ENV, raising=False)
    monkeypatch.setattr(beacon, "is_ci", lambda: False)
    monkeypatch.setattr(beacon, "is_default_home", lambda: True)
    monkeypatch.setattr(
        beacon,
        "is_governance_pinned_off",
        lambda *, audit_tool="": False,
    )
    monkeypatch.setattr(
        install_receipt,
        "receipt_secret",
        lambda *, create=True, config_dir=None: _SECRET,
    )


def _fake_urlopen(calls: list[tuple[str, float]]):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def open_url(request, *, timeout):
        calls.append((request.full_url, timeout))
        return Response()

    return open_url


class TestReceiptToken:
    def test_exact_hmac_is_deterministic_and_app_scoped(self):
        one = install_receipt.receipt_token(_SECRET, "issue-radar")
        again = install_receipt.receipt_token(_SECRET, "issue-radar")
        other = install_receipt.receipt_token(_SECRET, "papyrus")
        expected = hmac.new(
            _SECRET.encode("ascii"),
            b"app-install:issue-radar",
            hashlib.sha256,
        ).hexdigest()[:32]

        assert one == again == expected
        assert other != one
        assert len(one) == 32

    def test_token_contains_no_reusable_install_identifier(self):
        token = install_receipt.receipt_token(_SECRET, "issue-radar")
        assert _SECRET not in token
        assert all(_SECRET[i : i + 8] not in token for i in range(57))

    def test_token_is_independent_of_the_heartbeat_install_id(self):
        """The collector holds every heartbeat's install id. A token keyed by
        that id would let it recompute HMACs for public slugs and link one
        installation's receipts across apps — so the beacon id must neither
        BE the key nor reproduce the token."""
        token = install_receipt.receipt_token(_SECRET, "issue-radar")
        from_beacon_id = hmac.new(
            _BEACON_ID.encode("ascii"),
            b"app-install:issue-radar",
            hashlib.sha256,
        ).hexdigest()[:32]
        assert token != from_beacon_id
        # A 32-hex beacon-style id is not a valid receipt key at all.
        assert install_receipt.receipt_token(_BEACON_ID, "issue-radar") == ""

    def test_secret_file_is_not_the_beacon_id_file(self):
        assert install_receipt._SECRET_FILE != beacon.INSTALL_ID_FILE

    @pytest.mark.parametrize("slug", ["", "Issue-Radar", "../issue-radar", "issue_radar"])
    def test_invalid_slug_is_refused(self, slug):
        assert install_receipt.receipt_token(_SECRET, slug) == ""


class TestReceiptSecret:
    def test_generated_once_and_stable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(beacon, "config_dir", lambda: tmp_path)
        first = _real_receipt_secret()
        second = _real_receipt_secret()
        assert install_receipt._SECRET_RE.fullmatch(first)
        assert first == second

    def test_create_false_reads_but_never_generates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(beacon, "config_dir", lambda: tmp_path)
        assert _real_receipt_secret(create=False) == ""
        assert not (tmp_path / install_receipt._SECRET_FILE).exists()

    def test_corrupt_secret_is_regenerated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(beacon, "config_dir", lambda: tmp_path)
        (tmp_path / install_receipt._SECRET_FILE).write_text("not-hex", encoding="utf-8")
        fresh = _real_receipt_secret()
        assert install_receipt._SECRET_RE.fullmatch(fresh)

    def test_unreadable_home_yields_empty_never_a_fallback(self, monkeypatch):
        def denied():
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(beacon, "config_dir", denied)
        assert _real_receipt_secret() == ""
        assert _real_receipt_secret(create=False) == ""


class TestReceiptTransport:
    def test_url_has_only_the_documented_route_and_query(self):
        url = install_receipt.receipt_url(
            "https://telemetry.example/",
            "issue-radar",
            token="a" * 32,
            kind=install_receipt.KIND_FRESH,
            app_version="0.9.1-nightly.20260804t010203",
        )
        parsed = urllib.parse.urlsplit(url)

        assert parsed.path == "/b/1/install/issue-radar"
        assert urllib.parse.parse_qs(parsed.query) == {
            "t": ["a" * 32],
            "k": ["fresh"],
            "v": ["0.9.1"],
        }

    def test_config_toggle_off_dispatches_no_http(self, monkeypatch):
        calls: list[tuple[str, float]] = []
        monkeypatch.setattr(install_receipt.urllib.request, "urlopen", _fake_urlopen(calls))

        assert not install_receipt.send(
            "https://telemetry.example",
            "issue-radar",
            app_version="1.2.3",
            enabled=False,
            official=True,
            acked=True,
            kind=install_receipt.KIND_FRESH,
        )
        assert calls == []

    @pytest.mark.parametrize("suppression", ["env", "ci"])
    def test_env_kill_switch_and_test_mode_dispatch_no_http(self, monkeypatch, suppression):
        calls: list[tuple[str, float]] = []
        monkeypatch.setattr(install_receipt.urllib.request, "urlopen", _fake_urlopen(calls))
        if suppression == "env":
            monkeypatch.setenv(beacon.DISABLE_ENV, "1")
        else:
            monkeypatch.setattr(beacon, "is_ci", lambda: True)

        assert not install_receipt.send(
            "https://telemetry.example",
            "issue-radar",
            app_version="1.2.3",
            enabled=True,
            official=True,
            acked=True,
            kind=install_receipt.KIND_FRESH,
        )
        assert calls == []

    def test_custom_source_and_missing_id_dispatch_no_http(self, monkeypatch):
        calls: list[tuple[str, float]] = []
        monkeypatch.setattr(install_receipt.urllib.request, "urlopen", _fake_urlopen(calls))

        assert not install_receipt.send(
            "https://telemetry.example",
            "private-roadmap-app",
            app_version="1.2.3",
            enabled=True,
            official=False,
            acked=True,
            kind=install_receipt.KIND_FRESH,
        )
        monkeypatch.setattr(
            install_receipt, "receipt_secret", lambda *, create=True, config_dir=None: ""
        )
        assert not install_receipt.send(
            "https://telemetry.example",
            "issue-radar",
            app_version="1.2.3",
            enabled=True,
            official=True,
            acked=True,
            kind=install_receipt.KIND_FRESH,
        )
        assert calls == []

    def test_success_uses_get_and_beacon_timeout(self, monkeypatch):
        calls: list[tuple[str, float]] = []
        monkeypatch.setattr(install_receipt.urllib.request, "urlopen", _fake_urlopen(calls))

        assert install_receipt.send(
            "https://telemetry.example",
            "issue-radar",
            app_version="1.2.3",
            enabled=True,
            official=True,
            acked=True,
            kind=install_receipt.KIND_UPDATE,
        )
        assert len(calls) == 1
        assert calls[0][1] == beacon.HTTP_TIMEOUT_SECS == 5.0
        assert urllib.parse.parse_qs(urllib.parse.urlsplit(calls[0][0]).query)["k"] == ["update"]

    def test_transport_failures_are_silent(self, monkeypatch):
        monkeypatch.setattr(
            install_receipt.urllib.request,
            "urlopen",
            lambda *_a, **_k: (_ for _ in ()).throw(urllib.error.URLError("offline")),
        )
        assert not install_receipt.send(
            "https://telemetry.example",
            "issue-radar",
            app_version="1.2.3",
            enabled=True,
            official=True,
            acked=True,
            kind=install_receipt.KIND_FRESH,
        )

    def test_dispatch_is_detached_daemon_and_origin_gated(self, monkeypatch):
        threads: list[dict[str, object]] = []

        class Thread:
            def __init__(self, **kwargs):
                threads.append(kwargs)

            def start(self):
                threads[-1]["started"] = True

            def is_alive(self):
                # This fake never runs its target, so nothing is ever in flight;
                # the rootdir conftest's per-test join asks every registered
                # worker this question before the test's env pins are undone.
                return False

        monkeypatch.setattr(install_receipt.threading, "Thread", Thread)

        install_receipt.dispatch(
            "private-app",
            official=False,
            kind=install_receipt.KIND_FRESH,
        )
        assert threads == []

        install_receipt.dispatch(
            "issue-radar",
            official=True,
            kind=install_receipt.KIND_FRESH,
        )
        assert threads[0]["daemon"] is True
        assert threads[0]["started"] is True

    def test_dispatch_resolves_config_dir_before_home_is_unpatched(self, monkeypatch, tmp_path):
        """dispatch()'s worker must write under the KIROCREW_HOME pinned AT

        DISPATCH TIME, even if that env var is unset again before the
        background thread finishes. Regression test for a real-home leak: the
        worker used to call beacon.config_dir() itself, lazily, on the
        background thread — a race against the caller's teardown unpatching
        KIROCREW_HOME meant the secret file could land in the operator's real
        data home instead of the pinned test one.
        """
        pinned_home = tmp_path / "pinned-home"
        real_home_config_dir = tmp_path / "real-home" / ".kiro" / "crew"
        monkeypatch.setattr(beacon, "is_default_home", lambda: True)

        def fake_config_dir():
            # Mirrors production: honors KIROCREW_HOME at call time.
            override = os.environ.get("KIROCREW_HOME")
            if override:
                return Path(override) / ".kiro" / "crew"
            return real_home_config_dir

        monkeypatch.setattr(beacon, "config_dir", fake_config_dir)
        # Real receipt_secret (not the autouse stub) so it actually touches disk.
        monkeypatch.setattr(install_receipt, "receipt_secret", _real_receipt_secret)
        monkeypatch.setattr(
            install_receipt,
            "KiroCrewConfig",
            MagicMock(
                load=lambda: MagicMock(
                    telemetry=MagicMock(
                        beacon_endpoint="https://telemetry.example",
                        beacon_enabled=True,
                    ),
                    dashboard=MagicMock(privacy_acked=True),
                )
            ),
        )
        monkeypatch.setattr(install_receipt.urllib.request, "urlopen", _fake_urlopen([]))

        monkeypatch.setenv("KIROCREW_HOME", str(pinned_home))
        install_receipt.dispatch("issue-radar", official=True, kind=install_receipt.KIND_FRESH)
        # Simulate the test's own teardown unpatching KIROCREW_HOME while the
        # background thread may still be about to run.
        monkeypatch.delenv("KIROCREW_HOME", raising=False)

        install_receipt.wait_for_pending_receipt_writes(timeout=5.0)

        assert not any(
            t.is_alive() for t in threading.enumerate() if t.name == "kirocrew-install-receipt"
        )
        assert (pinned_home / ".kiro" / "crew" / "app_receipt_secret").exists()
        assert not real_home_config_dir.exists()

    @pytest.mark.asyncio
    async def test_dispatch_async_reads_config_off_the_loop_and_hands_it_over(self, monkeypatch):
        """The coroutine form must not do the config read on the loop thread.

        ``KiroCrewConfig.load()`` is file I/O plus schema validation, and the
        registry awaits this at the end of every official install. The read
        has to run in a worker thread, and the config it returns has to be the
        one ``dispatch`` receives — a second load there would put the I/O
        straight back on the loop.
        """
        loop_thread = threading.get_ident()
        load_threads: list[int] = []
        loaded = MagicMock(
            telemetry=MagicMock(beacon_endpoint="https://telemetry.example", beacon_enabled=True),
            dashboard=MagicMock(privacy_acked=True),
        )

        def fake_load():
            load_threads.append(threading.get_ident())
            return loaded

        monkeypatch.setattr(install_receipt, "KiroCrewConfig", MagicMock(load=fake_load))
        handed: list[dict[str, object]] = []
        monkeypatch.setattr(
            install_receipt, "dispatch", lambda slug, **kwargs: handed.append(kwargs)
        )

        await install_receipt.dispatch_async(
            "issue-radar", official=True, kind=install_receipt.KIND_FRESH
        )

        assert load_threads and all(t != loop_thread for t in load_threads)
        assert handed == [{"official": True, "kind": install_receipt.KIND_FRESH, "config": loaded}]

    @pytest.mark.asyncio
    async def test_dispatch_async_is_origin_gated_and_swallows_a_failed_load(self, monkeypatch):
        calls: list[object] = []
        monkeypatch.setattr(install_receipt, "dispatch", lambda slug, **kwargs: calls.append(slug))

        # Not official: nothing is read and nothing is dispatched.
        await install_receipt.dispatch_async(
            "private-app", official=False, kind=install_receipt.KIND_FRESH
        )
        assert calls == []

        # A failing config read is best-effort telemetry's problem, not the install's.
        def boom():
            raise OSError("unreadable")

        monkeypatch.setattr(install_receipt, "KiroCrewConfig", MagicMock(load=boom))
        await install_receipt.dispatch_async(
            "issue-radar", official=True, kind=install_receipt.KIND_FRESH
        )
        assert calls == []


async def _run_registry_install(
    monkeypatch,
    tmp_path,
    *,
    external: bool = False,
    existing: bool = False,
    install_ok: bool = True,
) -> tuple[dict[str, object], list[tuple[str, dict[str, object]]]]:
    source = tmp_path / "source"
    source.mkdir()
    manifest = {
        "name": "issue-radar",
        "version": "1.0.0",
        "displayName": "Issue Radar",
        "description": "Test app",
        "author": "tester",
    }
    (source / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
    entry: dict[str, object] = {
        "name": "issue-radar",
        "repo": "https://example.com/issue-radar.git",
        "branch": "main",
    }
    if external:
        entry["_registry"] = "private-catalog"

    monkeypatch.setattr(registry, "get_registry_app", lambda _name: entry)
    monkeypatch.setattr(registry, "_entry_git_url", lambda _entry: entry["repo"])

    async def fetch_manifest(*_args, **_kwargs):
        return manifest

    async def clone_build(*_args, **_kwargs):
        return {"ok": True, "pkg_dir": source}

    monkeypatch.setattr(registry, "_fetch_app_manifest", fetch_manifest)
    monkeypatch.setattr(registry, "_clone_build_app", clone_build)
    monkeypatch.setattr(registry, "app_admission_denied", lambda *_a, **_k: None)
    monkeypatch.setattr(registry, "app_execution_denied", lambda *_a, **_k: None)
    monkeypatch.setattr(
        registry,
        "get_app",
        lambda _name: {"name": "issue-radar"} if existing else None,
    )
    monkeypatch.setattr(
        registry,
        "install_app",
        lambda _source: AppResult(
            ok=install_ok,
            name="issue-radar",
            message="installed" if install_ok else "",
            error="" if install_ok else "failed",
        ),
    )
    monkeypatch.setattr(
        registry,
        "update_app",
        lambda _source: AppResult(ok=True, name="issue-radar", message="updated"),
    )
    monkeypatch.setattr(registry, "set_app_provenance", lambda *_a, **_k: True)

    dispatched: list[tuple[str, dict[str, object]]] = []

    async def record(slug, **kwargs):
        dispatched.append((slug, kwargs))

    # The registry awaits the async seam (the config read must stay off the
    # loop), so that is the attribute to stub.
    monkeypatch.setattr(registry.install_receipt, "dispatch_async", record)
    return await registry.install_from_registry("issue-radar"), dispatched


class TestRegistryEmitPoint:
    @pytest.mark.asyncio
    async def test_successful_official_fresh_install_dispatches_receipt(
        self, monkeypatch, tmp_path
    ):
        result, dispatched = await _run_registry_install(monkeypatch, tmp_path)
        assert result["ok"] is True
        assert dispatched == [
            (
                "issue-radar",
                {"official": True, "kind": install_receipt.KIND_FRESH},
            )
        ]

    @pytest.mark.asyncio
    async def test_successful_official_update_uses_update_kind(self, monkeypatch, tmp_path):
        result, dispatched = await _run_registry_install(monkeypatch, tmp_path, existing=True)
        assert result["ok"] is True
        assert dispatched[0][1]["kind"] == install_receipt.KIND_UPDATE

    @pytest.mark.asyncio
    async def test_owner_designated_external_repo_still_never_dispatches(
        self, monkeypatch, tmp_path
    ):
        """main's carve-out flips index_originated for owner-designated
        external repos as a CREDENTIAL decision; that must not promote the
        entry to official-catalog status — no receipt, ever."""
        monkeypatch.setattr(registry, "_is_owner_designated_repo", lambda entry: True)
        result, dispatched = await _run_registry_install(monkeypatch, tmp_path, external=True)
        assert result["ok"] is True
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_external_registry_success_never_dispatches(self, monkeypatch, tmp_path):
        result, dispatched = await _run_registry_install(monkeypatch, tmp_path, external=True)
        assert result["ok"] is True
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_failed_install_never_dispatches(self, monkeypatch, tmp_path):
        result, dispatched = await _run_registry_install(monkeypatch, tmp_path, install_ok=False)
        assert result["ok"] is False
        assert dispatched == []


class TestNonRegistryPaths:
    @pytest.fixture()
    def app_home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(manager, "app_admission_denied", lambda *_a, **_k: None)
        monkeypatch.setattr(manager, "app_execution_denied", lambda *_a, **_k: None)
        monkeypatch.setattr(manager, "sel", lambda: MagicMock())
        return home

    def _source(self, tmp_path, name):
        source = tmp_path / "sources" / name
        source.mkdir(parents=True)
        (source / "app.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "version": "1.0.0",
                    "displayName": name,
                    "description": "Test app",
                    "author": "tester",
                }
            ),
            encoding="utf-8",
        )
        return source

    def test_local_directory_install_emits_nothing(self, tmp_path, app_home, monkeypatch):
        monkeypatch.setattr(
            install_receipt,
            "dispatch",
            lambda *_a, **_k: pytest.fail("local install emitted a receipt"),
        )
        assert manager.install_app(self._source(tmp_path, "local-app")).ok

    def test_self_registration_emits_nothing(self, app_home, monkeypatch):
        monkeypatch.setattr(
            install_receipt,
            "dispatch",
            lambda *_a, **_k: pytest.fail("self-registration emitted a receipt"),
        )
        result = manager.register_external_app(
            name="self-registered-app",
            version="1.0.0",
            display_name="Self Registered App",
            manifest_data={
                "name": "self-registered-app",
                "version": "1.0.0",
                "displayName": "Self Registered App",
                "description": "Test app",
                "author": "tester",
            },
        )
        assert result.ok
