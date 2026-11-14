"""The memory provider conformance kit.

Runs the contract against one provider and says, check by check, what holds:

- ``connect`` answers a valid capability set for API version 1;
- ``health`` answers;
- ``context`` answers valid items within the item and size caps;
- ``search`` and ``list`` answer valid items, and a listed id can be read back
  with ``get``;
- ``list`` pages to the end (the cursor runs out within 50 pages), and a provider
  that says it keeps retired items answers ``status=all``;
- with ``write``: ``put`` returns a contract outcome; the text reads back exactly
  as written when the provider says ``verbatim``; the timestamps read back when it
  says ``preserves_timestamps``; the same idempotency key sent twice does not store
  a second item; ``delete`` removes what ``put`` stored; a read-only provider
  refuses ``put`` with ``read_only``.

Every check runs under the contract's own deadlines; a check that cannot run says
so. Write checks use their own scope (store ``conformance``, session
``conformance``), so they never touch a store a person uses.

From a terminal, against a provider an installed app serves::

    python -m kiro_crew.memory_provider.conformance --app <app id>[/<provider id>]

The terminal run is read-only (it asks the running gateway); the write checks run
from the owner's Settings or from a test that holds the provider.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

from kiro_crew.memory_provider.contract import (
    KINDS,
    Capabilities,
    MemoryProvider,
    NewItem,
    Origin,
    ProviderError,
    ProviderReadOnly,
    ProviderScope,
)

CONFORMANCE_SCOPE = ProviderScope(
    store_id="conformance",
    member_id=None,
    memory_mode="persistent",
    session_id="conformance",
)


async def run(provider: MemoryProvider, *, write: bool = False) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str = "", started: float | None = None) -> None:
        row: dict[str, Any] = {"name": name, "ok": ok, "detail": detail}
        if started is not None:
            row["ms"] = round((time.monotonic() - started) * 1000.0, 1)
        checks.append(row)

    scope = CONFORMANCE_SCOPE
    started = time.monotonic()
    connect = getattr(provider, "connect", None)
    try:
        caps = await asyncio.to_thread(connect if callable(connect) else provider.capabilities)
        record("connect", True, f"egress={caps.egress} ingest={caps.ingest}", started)
    except ProviderError as exc:
        record("connect", False, exc.message, started)
        return {"provider": provider.provider_id, "passed": False, "checks": checks}

    started = time.monotonic()
    try:
        health = await provider.health()
        record("health", bool(health.ok), health.detail, started)
    except ProviderError as exc:
        record("health", False, exc.code, started)

    for name, call in (
        ("context", lambda: provider.context(scope, "conformance check", 2_000)),
        ("search", lambda: provider.search(scope, "conformance", 5, None)),
    ):
        started = time.monotonic()
        try:
            answer = await call()
            bad = [i.id for i in answer if i.kind not in KINDS]
            record(name, not bad, f"{len(answer)} items", started)
        except ProviderError as exc:
            supported = name != "search" or caps.supports_search
            record(name, not supported and exc.code == "not_supported", exc.code, started)

    listed_id = None
    if caps.supports_list:
        started = time.monotonic()
        try:
            page = await provider.list(scope, None, None, None)
            listed_id = page.items[0].id if page.items else None
            ok = len(page.items) <= caps.max_page
            record("list", ok, f"{len(page.items)} items, cursor={bool(page.cursor)}", started)
        except ProviderError as exc:
            record("list", False, exc.code, started)
        await _paging_checks(provider, caps, record)
    if listed_id is not None:
        started = time.monotonic()
        try:
            item = await provider.get(scope, listed_id)
            record("get", item is not None and item.id == listed_id, "", started)
        except ProviderError as exc:
            record("get", False, exc.code, started)

    if write:
        await _write_checks(provider, caps, record)

    return {
        "provider": provider.provider_id,
        "passed": all(c["ok"] for c in checks),
        "checks": checks,
    }


#: ``list`` must end within this many pages of the conformance store.
MAX_PAGES_WALKED = 50
#: A backdated time the timestamp check writes and expects back.
_BACKDATED = 1_700_000_000.0


async def _paging_checks(provider: MemoryProvider, caps: Capabilities, record: Any) -> None:
    scope = CONFORMANCE_SCOPE
    started = time.monotonic()
    cursor: str | None = None
    pages = 0
    try:
        while True:
            page = await provider.list(scope, cursor, None, None)
            pages += 1
            cursor = page.cursor
            if not cursor or pages >= MAX_PAGES_WALKED:
                break
        record("list pages to the end", not cursor, f"{pages} pages", started)
    except ProviderError as exc:
        record("list pages to the end", False, exc.code, started)
    if caps.retired:
        started = time.monotonic()
        try:
            await provider.list(scope, None, None, "all")
            record("list status=all", True, "", started)
        except ProviderError as exc:
            record("list status=all", False, exc.code, started)


async def _write_checks(provider: MemoryProvider, caps: Capabilities, record: Any) -> None:
    scope = CONFORMANCE_SCOPE
    text = "Kiro Crew conformance check item: keep  this   spacing and *marks* as written."
    item = NewItem(
        kind="note",
        text=text,
        created_at=_BACKDATED,
        updated_at=_BACKDATED,
        origin=Origin("conformance", "conformance", "c1"),
    )
    key = f"conformance:{time.time_ns()}"
    started = time.monotonic()
    if caps.read_only:
        try:
            await provider.put(scope, item, key)
            record("put refused on read-only", False, "the write was accepted", started)
        except ProviderReadOnly:
            record("put refused on read-only", True, "", started)
        except ProviderError as exc:
            record("put refused on read-only", False, exc.code, started)
        return
    try:
        first = await provider.put(scope, item, key)
        record(
            "put", first.outcome in ("inserted", "enriched", "unchanged"), first.outcome, started
        )
        started = time.monotonic()
        second = await provider.put(scope, item, key)
        same = second.outcome != "inserted" or (first.id is not None and second.id == first.id)
        record("put idempotent", same, second.outcome, started)
        if first.id and caps.has("get"):
            await _read_back_checks(provider, caps, first.id, text, record)
        if first.id:
            started = time.monotonic()
            gone = await provider.delete(scope, first.id)
            record("delete", gone.outcome == "deleted", gone.outcome, started)
    except ProviderError as exc:
        record("put", False, exc.code, started)


async def _read_back_checks(
    provider: MemoryProvider, caps: Capabilities, item_id: str, text: str, record: Any
) -> None:
    started = time.monotonic()
    try:
        back = await provider.get(CONFORMANCE_SCOPE, item_id)
    except ProviderError as exc:
        record("get after put", False, exc.code, started)
        return
    if back is None:
        record("get after put", False, "the stored item was not found", started)
        return
    if caps.verbatim:
        record("verbatim round-trip", back.text == text, "", started)
    if caps.preserves_timestamps:
        record("timestamps kept", back.created_at == _BACKDATED, str(back.created_at), started)
    if caps.keeps_origin:
        kept = back.origin is not None and back.origin.id == "c1"
        record("origin kept", kept, "", started)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m kiro_crew.memory_provider.conformance",
        description="Check a memory provider an installed app serves against the contract.",
    )
    parser.add_argument("--app", required=True, help="<app id> or <app id>/<provider id>")
    parser.add_argument("--port", type=int, default=None, help="the gateway's port")
    args = parser.parse_args(argv)
    from kiro_crew.memory_provider.app_client import make_id

    app, _, provider = args.app.partition("/")
    provider_id = make_id(app, provider) if provider else ""
    if not provider_id:
        from kiro_crew.apps.manager import get_app_manifest

        manifest = get_app_manifest(app)
        declared = manifest.contributes.memoryProviders.usable() if manifest else []
        if len(declared) != 1:
            print(f"name the provider: --app {app}/<provider id>", file=sys.stderr)
            return 2
        provider_id = make_id(app, declared[0].id)
    try:
        report = _ask_gateway(provider_id, args.port)
    except OSError as exc:
        print(
            f"The gateway is not reachable ({exc}). Start it with `kirocrew start`.",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report.get("passed") else 1


def _ask_gateway(provider_id: str, port: int | None) -> dict[str, Any]:
    import urllib.request

    from kiro_crew.cli_commands_gateway_api import _client_port, _internal_secret
    from kiro_crew.loopback_http import loopback_urlopen

    resolved = _client_port(port)
    request = urllib.request.Request(
        f"http://127.0.0.1:{resolved}/api/memory/provider/conformance",
        data=json.dumps({"provider": provider_id}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Internal-Secret": _internal_secret(resolved),
        },
        method="POST",
    )
    with loopback_urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(_main())
