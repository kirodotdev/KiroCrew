"""A registry whose index fetch failed says so, and why, to the dashboard.

A failed clone of a configured registry's index adds no rows to the store listing,
which still answers 200, and the failed-fetch marker then holds the listing back
from cloning again for ``_EXTERNAL_REGISTRY_FAILURE_TTL``. The fetch classifies each
failure into a fixed cause, the marker keeps it, and ``GET /api/apps/registry``
(``registryStatus``) reports it, on the fresh failure and inside the window alike.

No network: the clone subprocess is a fake that fails the way ssh does when the
host name does not resolve, which is what a host behind a VPN does off it.
"""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from external_registry_helpers import cache_dir_fixture  # noqa: F401

from kiro_crew.apps import registry, routes
from kiro_crew.apps.registry import (
    _EXTERNAL_REGISTRY_CACHE_TTL,
    _EXTERNAL_REGISTRY_FAILURE_CLOCK_SLACK,
    _EXTERNAL_REGISTRY_FAILURE_TTL,
    _external_registry_cache_identity,
    _external_registry_cache_path,
    _external_registry_failure_path,
    _write_external_registry_cache,
    _write_external_registry_failure,
    refresh_registries,
)

_HOST = "git.example.invalid"

_DNS_STDERR = (
    b"ssh: Could not resolve hostname git.example.invalid: Name or service not known\r\n"
    b"fatal: Could not read from remote repository.\n\n"
    b"Please make sure you have the correct access rights\nand the repository exists.\n"
)


def _registry() -> SimpleNamespace:
    return SimpleNamespace(
        name="internal-community",
        repo=f"ssh://{_HOST}/pkg/AppRegistry",
        branch="mainline",
        trust="index",
        label="",
        review="",
    )


def _backdate(path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def _body(resp: Any) -> dict[str, Any]:
    return json.loads(resp.body.decode("utf-8"))


def _failed_clone(stderr: bytes) -> AsyncMock:
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    proc.returncode = 128
    return proc


def _clones(spawn: AsyncMock) -> int:
    return sum(1 for call in spawn.call_args_list if "clone" in call.args)


class _Request:
    """Neither handler reads more off the request than its method."""

    method = "GET"


class TestTheCause:
    @pytest.mark.parametrize(
        ("text", "cause"),
        [
            ("ssh: Could not resolve hostname git.example.com: Name or service not known", "dns"),
            (
                "fatal: unable to access 'https://git.example.com/r/': "
                "Could not resolve host: git.example.com",
                "dns",
            ),
            (
                "ssh: Could not resolve hostname git.example.com: "
                "nodename nor servname provided, or not known",
                "dns",
            ),
            ("git transport failed: host could not be resolved (details redacted)", "dns"),
            (
                "Host key verification failed.\nfatal: Could not read from remote repository.",
                "host_key",
            ),
            (
                "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\n"
                "Host key verification failed.",
                "host_key",
            ),
            ("git@git.example.com: Permission denied (publickey).", "auth"),
            ("fatal: Authentication failed for 'https://git.example.com/r'", "auth"),
            ("fatal: unable to access 'x': Could not resolve proxy: proxy.example.com", "network"),
            (
                "git transport failed: the proxy host could not be resolved (details redacted)",
                "network",
            ),
            ("ssh: connect to host git.example.com port 22: No route to host", "network"),
            ("ssh: connect to host git.example.com port 22: Connection timed out", "network"),
            ("fatal: Remote branch mainline not found in upstream origin", "other"),
            ("remote: Could not resolve hostname x\nfatal: the remote end hung up", "other"),
        ],
    )
    def test_each_failure_reads_as_its_cause(self, text, cause):
        assert registry._classify_registry_fetch_failure(text) == cause


@pytest.fixture()
def identity(cache_dir):
    return _external_registry_cache_identity(_registry())


class TestTheMarkerKeepsTheCause:
    def test_the_cause_round_trips(self, identity):
        _write_external_registry_failure(identity, "dns")

        assert registry._read_external_registry_failure_cause(identity) == "dns"

    def test_a_marker_past_the_listing_window_still_names_the_cause(self, identity):
        _write_external_registry_failure(identity, "host_key")
        _backdate(_external_registry_failure_path(identity), _EXTERNAL_REGISTRY_FAILURE_TTL + 60)

        assert registry._read_external_registry_failure_cause(identity) == "host_key"

    @pytest.mark.parametrize(
        "payload",
        [
            "",
            "{}",
            '{"cause": "made-up"}',
            "not json",
            '["dns"]',
            "x" * 300,
            '{"cause": []}',
            '{"cause": {}}',
            '{"cause": 7}',
        ],
    )
    def test_a_marker_naming_no_known_cause_reads_as_other(self, identity, payload):
        _external_registry_failure_path(identity).write_text(payload, encoding="utf-8")

        assert registry._read_external_registry_failure_cause(identity) == "other"

    def test_a_cause_outside_the_vocabulary_is_written_as_other(self, identity):
        _write_external_registry_failure(identity, "made-up")

        marker = _external_registry_failure_path(identity)
        assert json.loads(marker.read_text(encoding="utf-8")) == {"cause": "other"}

    def test_no_marker_reads_as_absent(self, identity):
        assert registry._read_external_registry_failure_cause(identity) is None

    def test_a_marker_dated_in_the_future_reads_as_absent(self, identity):
        marker = _external_registry_failure_path(identity)
        marker.write_text('{"cause": "dns"}', encoding="utf-8")
        later = time.time() + _EXTERNAL_REGISTRY_FAILURE_CLOCK_SLACK + 3600
        os.utime(marker, (later, later))

        assert registry._read_external_registry_failure_cause(identity) is None

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
    def test_a_fifo_at_the_marker_reads_as_absent_without_blocking(self, identity):
        os.mkfifo(_external_registry_failure_path(identity))

        assert registry._read_external_registry_failure_cause(identity) is None


@pytest.fixture()
def unreachable(cache_dir, monkeypatch):
    """One configured registry, and a listing whose external half is the real load.

    Every clone the test lets run fails on a host name that does not resolve.
    """
    reg = _registry()
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", lambda: SimpleNamespace(registries=[reg])
    )

    async def _unwrapped(argv, **_kwargs):
        return list(argv), None

    # The OS sandbox is not available on every CI runner; the clone's argv is not
    # what these tests are about.
    monkeypatch.setattr(registry, "wrap_argv_async", _unwrapped)

    async def _no_catalog():
        return []

    async def _external_only():
        return await registry._load_external_registries()

    monkeypatch.setattr(routes, "list_catalog_apps", _no_catalog)
    monkeypatch.setattr(routes, "list_registry", _external_only)
    monkeypatch.setattr(routes, "load_category_order", lambda: [])
    monkeypatch.setattr(routes, "load_sections", lambda: [])
    monkeypatch.setattr(routes, "get_server_platform", lambda: {"os": "linux", "arch": "x86_64"})
    monkeypatch.setattr(routes, "sel", lambda: MagicMock())
    return SimpleNamespace(reg=reg, identity=_external_registry_cache_identity(reg))


_FAILED = {"name": "internal-community", "ok": False, "cause": "dns", "host": _HOST}


@pytest.mark.asyncio
class TestTheEndpointsSayWhichRegistryFailedAndWhy:
    async def test_the_fresh_failure_and_the_held_back_listing_both_name_the_cause(
        self, unreachable
    ):
        with patch("asyncio.create_subprocess_exec") as spawn:
            spawn.return_value = _failed_clone(_DNS_STDERR)
            first = _body(await routes.handle_registry(_Request()))  # type: ignore[arg-type]
            second = _body(await routes.handle_registry(_Request()))  # type: ignore[arg-type]

        # One clone: the marker held the second listing back, and it still says why.
        assert _clones(spawn) == 1
        assert first["apps"] == [] and second["apps"] == []
        for body in (first, second):
            assert body["registryStatus"] == [{**_FAILED, "cached": False}]

    async def test_a_registry_served_from_its_last_sync_says_so(self, unreachable):
        _write_external_registry_cache(
            unreachable.identity, [{"name": "cool-app", "repo": "https://git.example.com/cool"}]
        )
        _backdate(
            _external_registry_cache_path(unreachable.identity), _EXTERNAL_REGISTRY_CACHE_TTL + 60
        )
        with patch("asyncio.create_subprocess_exec") as spawn:
            spawn.return_value = _failed_clone(_DNS_STDERR)
            body = _body(await routes.handle_registry(_Request()))  # type: ignore[arg-type]

        assert [app["name"] for app in body["apps"]] == ["cool-app"]
        assert body["registryStatus"] == [{**_FAILED, "cached": True}]

    @pytest.mark.parametrize("payload", ["{not json", '{"name": "cool-app"}'])
    async def test_a_cache_the_listing_cannot_read_says_the_apps_are_missing(
        self, unreachable, payload
    ):
        # The file is there, but the reader the listing serves from rejects it,
        # so the registry adds no rows and must not read as last synced.
        cache = _external_registry_cache_path(unreachable.identity)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(payload, encoding="utf-8")
        with patch("asyncio.create_subprocess_exec") as spawn:
            spawn.return_value = _failed_clone(_DNS_STDERR)
            body = _body(await routes.handle_registry(_Request()))  # type: ignore[arg-type]

        assert body["apps"] == []
        assert body["registryStatus"] == [{**_FAILED, "cached": False}]

    async def test_a_planted_cause_that_is_not_a_string_reads_as_other(self, unreachable):
        # The cache directory is open to the agent, so the marker's payload is
        # untrusted: an unhashable cause must not fail the listing's status.
        marker = _external_registry_failure_path(unreachable.identity)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('{"cause": []}', encoding="utf-8")

        with patch("asyncio.create_subprocess_exec") as spawn:
            spawn.return_value = _failed_clone(_DNS_STDERR)
            body = _body(await routes.handle_registry(_Request()))  # type: ignore[arg-type]

        # The fresh marker held the listing back, so the planted cause is what it reads.
        assert _clones(spawn) == 0
        assert body["registryStatus"] == [{**_FAILED, "cause": "other", "cached": False}]

    async def test_a_restarted_gateway_still_names_the_cause(self, unreachable, monkeypatch):
        with patch("asyncio.create_subprocess_exec") as spawn:
            spawn.return_value = _failed_clone(_DNS_STDERR)
            await routes.handle_registry(_Request())  # type: ignore[arg-type]
        # A new process remembers no fetch; the marker on disk is what it reads.
        monkeypatch.setattr(registry, "_FETCH_FAILURE_CAUSES", {})

        body = _body(await routes.handle_registry(_Request()))  # type: ignore[arg-type]

        assert body["registryStatus"] == [{**_FAILED, "cached": False}]

    async def test_a_successful_fetch_clears_the_status(self, unreachable, monkeypatch):
        with patch("asyncio.create_subprocess_exec") as spawn:
            spawn.return_value = _failed_clone(_DNS_STDERR)
            await routes.handle_registry(_Request())  # type: ignore[arg-type]
        _backdate(
            _external_registry_failure_path(unreachable.identity),
            _EXTERNAL_REGISTRY_FAILURE_TTL + 1,
        )
        monkeypatch.setattr(
            registry,
            "_fetch_external_registry_index",
            AsyncMock(return_value=[{"name": "new-app", "repo": "https://git.example.com/new"}]),
        )

        body = _body(await routes.handle_registry(_Request()))  # type: ignore[arg-type]

        assert body["registryStatus"] == []

    async def test_the_refresh_names_the_cause_and_the_host(self, unreachable, monkeypatch):
        monkeypatch.setattr(registry, "list_registry", AsyncMock(return_value=[]))
        with patch("asyncio.create_subprocess_exec") as spawn:
            spawn.return_value = _failed_clone(_DNS_STDERR)
            result = await refresh_registries()

        assert result["results"] == [
            {
                "name": "internal-community",
                "ok": False,
                "cached": False,
                "reason": "dns",
                "host": _HOST,
            }
        ]

    async def test_the_refresh_says_whether_the_last_sync_is_still_served(
        self, unreachable, monkeypatch
    ):
        # The banner names a registry with a cached index as last synced and one
        # without as missing; only the refresh result can tell it which is which.
        _write_external_registry_cache(
            unreachable.identity, [{"name": "cool-app", "repo": "https://git.example.com/cool"}]
        )
        monkeypatch.setattr(registry, "list_registry", AsyncMock(return_value=[]))
        with patch("asyncio.create_subprocess_exec") as spawn:
            spawn.return_value = _failed_clone(_DNS_STDERR)
            result = await refresh_registries()

        [row] = result["results"]
        assert row["cached"] is True
