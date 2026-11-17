"""The fixture memory provider app, served in-process, and the switches a test flips.

``fixture_provider`` starts ``test/fixtures/memory_provider_app/backend/server.py`` on
an ephemeral loopback port and points the wire client at it: the backend port and
proxy secret the gateway would look up are patched to that server, and
``app_client.provider_for_id`` answers the two providers the fixture's manifest
declares. Every call still crosses the real HTTP wire, the real v2 signature and the
real answer validation.
"""

from __future__ import annotations

import importlib.util
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from kiro_crew.config import loader
from kiro_crew.memory_provider import app_client, resolver

FIXTURE_APP = Path(__file__).parent / "fixtures" / "memory_provider_app"
FIXTURE_ID = "app:memory-fixture/fixture"
READONLY_ID = "app:memory-fixture/readonly"
SECRET = "fixture-proxy-secret"


def _server_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "memory_fixture_server", FIXTURE_APP / "backend" / "server.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def fixture_provider(monkeypatch: Any, data_path: Path) -> Iterator[SimpleNamespace]:
    """Serve the fixture app and route ``app:memory-fixture/*`` to it."""
    server = _server_module().make_server(0, SECRET, data_path)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    clients = {
        FIXTURE_ID: app_client.AppMemoryProvider(
            app="memory-fixture",
            provider="fixture",
            route="memory-provider",
            display_name="Fixture memory",
            app_version="1.0.0",
        ),
        READONLY_ID: app_client.AppMemoryProvider(
            app="memory-fixture",
            provider="readonly",
            route="memory-provider-ro",
            display_name="Fixture memory (read-only)",
            app_version="1.0.0",
        ),
    }
    health = SimpleNamespace(port=port)
    monkeypatch.setattr(app_client, "_backend_port", lambda app: health.port)
    monkeypatch.setattr(app_client, "_app_secret", lambda app: SECRET)
    monkeypatch.setattr(app_client, "provider_for_id", lambda pid: clients.get(pid))
    monkeypatch.setattr(app_client, "provider_app_version", lambda pid: "1.0.0")
    monkeypatch.setattr(
        app_client,
        "declared_providers",
        lambda: [
            app_client.DeclaredProvider(
                provider_id=pid,
                app="memory-fixture",
                app_version="1.0.0",
                display_name=client.display_name,
                description="",
                route=client.route,
                settings_page="",
                enabled=True,
            )
            for pid, client in clients.items()
        ],
    )
    monkeypatch.setattr(resolver, "_shared_crew_on", lambda: False)
    resolver.reset_snapshot_cache()
    try:
        yield SimpleNamespace(
            port=port,
            clients=clients,
            fixture=clients[FIXTURE_ID],
            readonly=clients[READONLY_ID],
            data_path=data_path,
            health=health,
            items=lambda store="default": _stored(data_path, store),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        resolver.reset_snapshot_cache()


def _stored(data_path: Path, store: str) -> list[dict]:
    if not data_path.is_file():
        return []
    data = json.loads(data_path.read_text(encoding="utf-8"))
    return list(data["stores"].get(store, {}).values())


def set_provider(mode: str, provider_id: str = "", **extra: Any) -> None:
    """Write ``memory.provider`` into the isolated data home's config.json."""
    path = loader.config_path()
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    memory = data.setdefault("memory", {})
    memory["provider"] = {"mode": mode, "id": provider_id, **extra}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    loader._invalidate_config_cache()
    resolver.reset_snapshot_cache()
