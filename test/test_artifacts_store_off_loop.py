"""Which thread the ARTIFACT store's calls run on, for the handlers that write.

Sibling of ``test_artifact_folder_store_off_loop.py``, which pins the same
property for the folder store. The artifact store needs its own because its
writers now sync each file to the device before they publish it: ``_write_text``
and ``_write_bytes`` call ``os.fsync`` inside the ``with``, under the store's
single lock. That makes two things true at once. A handler that writes on the
gateway loop stalls chat turns and heartbeats for however long the device takes,
and a handler that merely READS on the loop can only take the lock once a worker
thread finishes somebody else's write, so it stalls for the same duration.

``get`` is the case worth naming: an artifact stored before the event log existed
has a synthetic history backfilled and PERSISTED on first read, so a plain GET of
a version reaches ``_write_meta`` and its sync. Reading the handler's shape would
not catch that; recording the thread inside the store does.

Harness mirrors the folder-store file: MagicMock requests, a real store rooted
under ``tmp_path``, and the assertion that no watched call landed on the event
loop's own thread.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import artifacts as art_mod
from kiro_crew.apps.builtins.auto_research import handlers as research_handlers
from kiro_crew.apps.builtins.auto_research.campaign import publication
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.dashboard.handlers.artifacts import (
    api_artifact_version_detail,
    api_artifact_versions,
)

#: The store calls the code under test reaches. A regression that moves any of
#: them back inline is what the assertion catches. Setup in each test creates
#: its fixture data first and then clears the record, so only the calls the
#: exercised code makes are judged.
WATCHED = (
    "get",
    "list_versions",
    "create",
    "update",
)


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> ArtifactStore:
    st = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(art_mod, "_default_store", st)
    return st


@pytest.fixture
def threads(monkeypatch) -> dict[str, list[int]]:
    """Record the thread ident of every watched artifact-store call."""
    seen: dict[str, list[int]] = {}
    for name in WATCHED:
        original = getattr(ArtifactStore, name)

        def _wrapper(self, *args, _name=name, _orig=original, **kwargs):
            seen.setdefault(_name, []).append(threading.get_ident())
            return _orig(self, *args, **kwargs)

        monkeypatch.setattr(ArtifactStore, name, _wrapper)
    return seen


def _request(*, match: dict | None = None) -> MagicMock:
    req = MagicMock()
    req.headers = {"X-Session-Key": "dashboard:test"}
    req.match_info = match or {}
    req.query = {}
    req.read = AsyncMock(return_value=b"")
    req.app = {"state": MagicMock(), "_restricted_session": False}
    return req


def _assert_off_loop(seen: dict[str, list[int]], *expected: str) -> None:
    loop_thread = threading.get_ident()
    assert set(expected) <= set(seen), f"expected {expected} to be called, saw {sorted(seen)}"
    on_loop = {
        name: idents for name, idents in seen.items() if any(i == loop_thread for i in idents)
    }
    assert not on_loop, f"artifact-store calls ran on the event loop thread: {sorted(on_loop)}"


@pytest.mark.asyncio
async def test_version_list_reads_off_loop(store: ArtifactStore, threads) -> None:
    art = store.create(name="Report", kind="widget", content="<p>one</p>")
    threads.clear()

    resp = await api_artifact_versions(_request(match={"slug": art.slug}))

    assert resp.status == 200
    _assert_off_loop(threads, "list_versions")


@pytest.mark.asyncio
async def test_version_detail_reads_off_loop(store: ArtifactStore, threads) -> None:
    """The read that can write. ``get`` persists a backfilled event history, so
    leaving it inline puts the publish sync on the loop."""
    art = store.create(name="Report", kind="widget", content="<p>one</p>")
    store.update(art.slug, content="<p>two</p>", snapshot=True)
    threads.clear()

    resp = await api_artifact_version_detail(_request(match={"slug": art.slug, "version": "1"}))

    assert resp.status == 200
    _assert_off_loop(threads, "get")


@pytest.mark.asyncio
async def test_the_report_export_store_phase_runs_off_loop(
    tmp_path: Path, monkeypatch, threads
) -> None:
    """Not a dashboard handler: the research campaign's report export reaches the
    same store from its own async code, and the report HTML is as large as the
    campaign made it."""
    monkeypatch.setattr(
        publication, "ArtifactStore", lambda: ArtifactStore(root=tmp_path / "exported")
    )
    # The slug persist writes the campaigns DB and has its own off-loop tests;
    # stubbing it keeps this one about the store phase.
    monkeypatch.setattr(publication, "_persist_report_slug", lambda *a, **k: None)
    threads.clear()

    art, name, regenerated = await publication._publish_report_artifact(
        "c-1", "Does it sync?", "<p>report</p>", None
    )

    assert art.slug and name.startswith("Research:") and not regenerated
    _assert_off_loop(threads, "create")


@pytest.mark.asyncio
async def test_the_report_status_probe_runs_off_loop(tmp_path: Path, monkeypatch, threads) -> None:
    """The subtle one: a read-only status GET. Its existence probe calls ``get``,
    which persists a backfilled event history for an artifact saved before event
    logs existed -- so the cheapest-looking endpoint on this path is a writer.

    Driven through the handler, not the helper: calling the helper off-loop here
    would pass whatever the handler does.
    """
    probe_store = ArtifactStore(root=tmp_path / "probed")
    art = probe_store.create(name="Report", kind="html", content="<p>r</p>")
    monkeypatch.setattr(publication, "ArtifactStore", lambda: probe_store)
    # The campaigns DB read is already offloaded and has its own tests; stub it
    # so this test needs no DB.
    monkeypatch.setattr(
        publication,
        "_read_report_slug",
        lambda cid: {"report_artifact_slug": art.slug},
    )
    req = _request(match={"id": "a1b2c3d4"})
    # The handler trusts only the middleware-set user, never a raw token.
    req.get = lambda key, default=None: {"user": "tester"}.get(key, default)
    threads.clear()

    resp = await research_handlers._handle_report_status(req)

    assert json.loads(resp.text)["slug"] == art.slug
    _assert_off_loop(threads, "get")


@pytest.mark.asyncio
async def test_a_missing_slug_still_answers_404_from_off_loop(
    store: ArtifactStore, threads
) -> None:
    """The refusal path matters too: ``_run_off_loop`` propagates the store's
    exception unchanged, so the handler's own 404 arm still sees it."""
    resp = await api_artifact_versions(_request(match={"slug": "nope-not-here"}))

    assert resp.status == 404
    _assert_off_loop(threads, "list_versions")
