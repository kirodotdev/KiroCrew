"""Regression tests for the boot-path / import-cost remediation.

Each test here pins one specific cost that was measured on the startup path and
fails if the corresponding fix is reverted:

* the gateway's boot update check must not be inline-awaited, and the SIGINT /
  SIGTERM handlers must be installed before it starts;
* the OpenTelemetry SDK must not be imported while telemetry is off;
* ``config_dir()`` must be memoized, making its breadcrumb write and archive
  sweep once-per-resolution instead of once-per-call;
* importing ``dashboard.origin`` / ``dashboard.urls`` must not pull the dashboard
  handler tree or aiohttp;
* importing ``dashboard.handlers.memory`` must not pull ``vector_memory``;
* a folder scan must not issue one ``sources`` query and one ``last_seen``
  UPDATE per discovered file;
* the changelog and channel-preset reads must be cached on a stat signature.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import kiro_crew
from kiro_crew.config import paths
from kiro_crew.dashboard import handlers_channel
from kiro_crew.dashboard.handlers import updates
from kiro_crew.slack.gateway import GatewayOrchestrator

_SRC = str(Path(kiro_crew.__file__).resolve().parents[1])


def _probe(snippet: str) -> dict:
    """Run *snippet* in a clean interpreter, returning the JSON it prints.

    Import-graph assertions need a fresh process: pytest has already imported
    most of the package, so ``sys.modules`` in-process says nothing about what a
    given import actually pulls.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    # Coverage's subprocess hook would otherwise import extra modules and
    # pollute the module-count assertions.
    env.pop("COV_CORE_SOURCE", None)
    env.pop("COVERAGE_PROCESS_START", None)
    # parse_dashboard_url honours KIROCREW_PORT; keep the probe deterministic.
    env.pop("KIROCREW_PORT", None)
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, f"probe failed:\n{proc.stdout}\n{proc.stderr}"
    # The data-home conflict warning goes to stderr; only stdout is parsed.
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── Gateway boot: update check off the critical path ───────────────────────


class TestGatewayUpdateCheckIsBackgrounded:
    """``_check_for_updates`` runs five sequential git subprocesses whose
    timeouts sum to ~70s. Inline-awaiting it during ``start()`` delayed the
    dashboard URL by that long on a stalled network AND did so before the signal
    handlers existed, so Ctrl-C did nothing and the gateway looked wedged."""

    def test_update_check_is_not_awaited_inline(self) -> None:
        src = inspect.getsource(GatewayOrchestrator.run)
        assert "await self._check_for_updates()" not in src, (
            "the boot update check must not be inline-awaited — it blocks the "
            "boot for up to ~70s on a stalled network"
        )
        assert "asyncio.create_task(self._check_for_updates())" in src

    def test_signal_handlers_installed_before_update_check(self) -> None:
        src = inspect.getsource(GatewayOrchestrator.run)
        handlers_at = src.index("loop.add_signal_handler(sig, _on_signal)")
        check_at = src.index("asyncio.create_task(self._check_for_updates())")
        assert handlers_at < check_at, (
            "SIGINT/SIGTERM handlers must be installed before the update check "
            "starts, or an early Ctrl-C is ignored"
        )

    def test_update_check_task_is_tracked_and_cancelled(self) -> None:
        run_src = inspect.getsource(GatewayOrchestrator.run)
        assert "self._background_tasks.add(self._update_check_task)" in run_src, (
            "the fire-and-forget task must be strongly referenced or it can be "
            "garbage-collected mid-flight"
        )
        shutdown_src = inspect.getsource(GatewayOrchestrator._shutdown)
        assert "self._update_check_task.cancel()" in shutdown_src


# ── Telemetry: no OTel SDK import while telemetry is off ───────────────────


class TestOtelSdkImportIsDeferred:
    """The OTel metrics SDK costs ~57ms and ~120 modules to import. The metrics
    provider sits on the eager boot chain (cli -> ... -> skills -> get_recorder)
    while telemetry is default-off, so the SDK must not load until a host opts
    in."""

    def test_provider_import_does_not_load_otel_sdk(self) -> None:
        result = _probe(
            "import json, sys\n"
            "import kiro_crew.metrics.provider as p\n"
            "print(json.dumps({\n"
            "    'sdk': 'opentelemetry.sdk.metrics' in sys.modules,\n"
            "    'available': p._OTEL_AVAILABLE,\n"
            "}))\n"
        )
        assert result["sdk"] is False, (
            "importing metrics.provider must not import the OTel metrics SDK"
        )
        # The availability probe must still work — it is what keeps a partial
        # install degrading to the no-op recorder instead of crashing.
        assert result["available"] is True

    def test_disabled_recorder_does_not_load_otel_sdk(self) -> None:
        result = _probe(
            "import json, sys\n"
            "import kiro_crew.metrics.provider as p\n"
            "rec = p.get_recorder()\n"
            "print(json.dumps({\n"
            "    'sdk': 'opentelemetry.sdk.metrics' in sys.modules,\n"
            "    'enabled': rec.enabled,\n"
            "}))\n"
        )
        assert result["enabled"] is False  # default-off consent gate
        assert result["sdk"] is False, (
            "a disabled recorder must not pay for the OTel SDK import"
        )

    def test_enabled_recorder_still_loads_the_sdk(self, tmp_path: Path) -> None:
        """The deferral must not break the opt-in path."""
        result = _probe(
            "import json, sys\n"
            "import kiro_crew.metrics.provider as p\n"
            f"import os; os.environ['KIROCREW_HOME'] = {str(tmp_path)!r}\n"
            "os.environ['KIROCREW_TELEMETRY'] = '1'\n"
            "rec = p.get_recorder()\n"
            "print(json.dumps({\n"
            "    'sdk': 'opentelemetry.sdk.metrics' in sys.modules,\n"
            "    'enabled': rec.enabled,\n"
            "}))\n"
        )
        assert result["enabled"] is True
        assert result["sdk"] is True


# ── config_dir(): memoized resolution ─────────────────────────────────────


class TestConfigDirMemo:
    """``config_dir()`` is called from 323 sites and measured 94.9us per call —
    a ``Path.resolve()`` + ``mkdir`` and, on the default path, a breadcrumb
    read/write plus the leftover-archive sweep, every time."""

    def test_repeat_calls_do_not_redo_breadcrumb_or_sweep(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(paths, "_resolved_home", None)
        monkeypatch.setattr(paths, "_config_dir_memo", None, raising=False)

        calls = {"breadcrumb": 0, "sweep": 0}
        real_breadcrumb = paths._write_recovery_breadcrumb
        real_sweep = paths._sweep_ungated_archive_leftovers

        def _breadcrumb(d: Path) -> None:
            calls["breadcrumb"] += 1
            real_breadcrumb(d)

        def _sweep() -> None:
            calls["sweep"] += 1
            real_sweep()

        monkeypatch.setattr(paths, "_write_recovery_breadcrumb", _breadcrumb)
        monkeypatch.setattr(paths, "_sweep_ungated_archive_leftovers", _sweep)

        first = paths.config_dir()
        for _ in range(50):
            assert paths.config_dir() == first

        assert calls["breadcrumb"] == 1, "breadcrumb write must be once per resolution"
        assert calls["sweep"] == 1, "archive sweep must be once per resolution"

    def test_override_change_is_honoured(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The memo must not pin a stale home when KIROCREW_HOME is repointed —
        pods, worktrees and the test suite all repoint it at runtime."""
        monkeypatch.setattr(paths, "_config_dir_memo", None, raising=False)
        a, b = tmp_path / "a", tmp_path / "b"
        monkeypatch.setenv("KIROCREW_HOME", str(a))
        assert paths.config_dir() == a.resolve()
        assert paths.config_dir() == a.resolve()  # memo hit
        monkeypatch.setenv("KIROCREW_HOME", str(b))
        assert paths.config_dir() == b.resolve()

    def test_clearing_resolved_home_invalidates_the_memo(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The suite's isolation fixture resets ``_resolved_home`` per test; the
        memo is keyed on it so a default-path result cannot outlive that reset."""
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "h1"))
        monkeypatch.setattr(paths, "_resolved_home", None)
        monkeypatch.setattr(paths, "_config_dir_memo", None, raising=False)
        first = paths.config_dir()

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "h2"))
        monkeypatch.setattr(paths, "_resolved_home", None)
        second = paths.config_dir()

        assert second != first
        assert second == (tmp_path / "h2" / ".kiro" / "crew")


# ── dashboard package: lazy exports + stdlib-only URL leaf ────────────────


class TestDashboardImportIsLeaf:
    """``dashboard/__init__`` eagerly imported ``server``, so reaching
    ``parse_dashboard_url`` through ``dashboard.origin`` pulled the whole handler
    tree (measured 605ms / 1124 modules) into every CLI invocation and every MCP
    stdio subprocess."""

    def test_importing_urls_pulls_no_handler_tree_and_no_aiohttp(self) -> None:
        result = _probe(
            "import json, sys\n"
            "from kiro_crew.dashboard.urls import parse_dashboard_url\n"
            "print(json.dumps({\n"
            "    'server': 'kiro_crew.dashboard.server' in sys.modules,\n"
            "    'handlers': 'kiro_crew.dashboard.handlers' in sys.modules,\n"
            "    'aiohttp': 'aiohttp' in sys.modules,\n"
            "    'parsed': list(parse_dashboard_url('http://h:9999')),\n"
            "}))\n"
        )
        assert result["server"] is False
        assert result["handlers"] is False
        assert result["aiohttp"] is False, "the URL leaf must stay stdlib-only"
        assert result["parsed"] == ["h", 9999]

    def test_importing_origin_pulls_no_handler_tree(self) -> None:
        result = _probe(
            "import json, sys\n"
            "import kiro_crew.dashboard.origin as o\n"
            "print(json.dumps({\n"
            "    'server': 'kiro_crew.dashboard.server' in sys.modules,\n"
            "    'handlers': 'kiro_crew.dashboard.handlers' in sys.modules,\n"
            "    'aiohttp': 'aiohttp' in sys.modules,\n"
            "    'modules': len(sys.modules),\n"
            "    'reexports_ok': all(hasattr(o, n) for n in (\n"
            "        'parse_dashboard_url', 'dashboard_origin', 'build_allowed_origins',\n"
            "        'build_allowed_hosts', 'bind_address_for', 'is_loopback',\n"
            "        'machine_hostname', 'resolve_dashboard_host', 'is_local_only',\n"
            "        'build_dashboard_url', 'format_dashboard_urls',\n"
            "        'should_canonicalize_host', 'devspaces_proxy_url', 'socket',\n"
            "    )),\n"
            "}))\n"
        )
        assert result["server"] is False, (
            "dashboard/__init__ must not eagerly import server"
        )
        assert result["handlers"] is False
        assert result["aiohttp"] is False, (
            "origin's aiohttp import must stay under TYPE_CHECKING — the CSRF "
            "helpers only need it for annotations, and CLI / MCP-stdio callers "
            "import this module for URL parsing alone"
        )
        assert result["modules"] < 400, (
            f"dashboard.origin pulled {result['modules']} modules; it was 1124 "
            "before the split and must stay a leaf"
        )
        assert result["reexports_ok"] is True, (
            "origin must keep re-exporting every name it used to define"
        )

    def test_lazy_package_attributes_still_resolve(self) -> None:
        result = _probe(
            "import json\n"
            "from kiro_crew.dashboard import (start_dashboard, start_api_server,\n"
            "    DashboardState, _ChatSlot, _fmt_duration)\n"
            "import kiro_crew.dashboard as d\n"
            "print(json.dumps({\n"
            "    'callables': all(callable(x) for x in (start_dashboard,\n"
            "        start_api_server, DashboardState, _ChatSlot, _fmt_duration)),\n"
            "    'raises': not hasattr(d, 'definitely_not_a_real_attribute'),\n"
            "}))\n"
        )
        assert result["callables"] is True
        assert result["raises"] is True

    def test_memory_handler_does_not_import_vector_memory(self) -> None:
        """``handlers/memory`` needed ``vector_memory`` (snowballstemmer plus the
        optional numpy/faiss imports, ~175ms) for one enum on one error branch."""
        result = _probe(
            "import json, sys\n"
            "import kiro_crew.dashboard.handlers.memory  # noqa: F401\n"
            "print(json.dumps({\n"
            "    'vector_memory': 'kiro_crew.vector_memory' in sys.modules,\n"
            "    'snowballstemmer': 'snowballstemmer' in sys.modules,\n"
            "}))\n"
        )
        assert result["vector_memory"] is False
        assert result["snowballstemmer"] is False


# ── folder_watcher: bounded pause checks + batched last_seen ───────────────


class _CountingDb:
    """sqlite connection wrapper that counts statements by SQL fragment."""

    def __init__(self, db) -> None:
        self._db = db
        self.select_sources = 0
        self.last_seen_execute = 0
        self.last_seen_executemany = 0

    def execute(self, sql, *args, **kwargs):
        if "FROM sources WHERE id" in sql:
            self.select_sources += 1
        if "SET last_seen" in sql:
            self.last_seen_execute += 1
        return self._db.execute(sql, *args, **kwargs)

    def executemany(self, sql, seq, *args, **kwargs):
        seq = list(seq)
        if "SET last_seen" in sql:
            self.last_seen_executemany += 1
        return self._db.executemany(sql, seq, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._db, name)


class TestFolderWatcherScanQueryCount:
    """A scan re-read ``sources.properties`` and issued a ``last_seen`` UPDATE
    once per discovered file — up to 10,000 on-loop sqlite ops per scan."""

    @pytest.mark.asyncio
    async def test_pause_check_and_last_seen_are_not_per_file(
        self, tmp_path: Path
    ) -> None:
        from kiro_crew.knowledge.folder_watcher import (
            _PAUSE_RECHECK_FILES,
            FolderWatcher,
        )
        from kiro_crew.knowledge.store import KnowledgeStore

        n_files = 40
        assert n_files < _PAUSE_RECHECK_FILES  # so one up-front check suffices

        vault = tmp_path / "vault"
        vault.mkdir()
        for i in range(n_files):
            (vault / f"note{i}.md").write_text(f"Note {i}", encoding="utf-8")

        store = KnowledgeStore(tmp_path / "knowledge.db")
        pipeline = MagicMock()
        pipeline._dedup_enabled = False
        fw = FolderWatcher(store, pipeline)
        source_id = store.add_source("t", "local_folder", str(vault), properties={})
        source = {
            "id": source_id,
            "uri": str(vault),
            "source_type": "local_folder",
            "properties": "{}",
        }

        # First pass records every file so the second pass takes the unchanged
        # (last_seen-only) branch for all of them.
        async def _ingest(file_path, source_id, namespace, props, old_ids, root: str = ""):
            return ["item-" + Path(file_path).name], "done"

        fw._ingest_file = _ingest  # type: ignore[assignment]
        first = await fw.scan_source(source)
        assert first["new"] == n_files

        counting = _CountingDb(store.db)
        # ``KnowledgeStore.db`` is a read-only property backed by a per-thread
        # connection; the scan runs on this thread, so swap the thread-local.
        store._thread_local.conn = counting
        await fw.scan_source(source)

        assert counting.select_sources <= 2, (
            f"{counting.select_sources} sources queries for {n_files} files — "
            "the pause check must not run per file"
        )
        assert counting.last_seen_execute == 0, (
            "last_seen touches must be batched, not issued one per file"
        )
        assert counting.last_seen_executemany == 1

    @pytest.mark.asyncio
    async def test_last_seen_is_still_written(self, tmp_path: Path) -> None:
        """The batching must not drop the touches it defers."""
        from kiro_crew.knowledge.folder_watcher import FolderWatcher
        from kiro_crew.knowledge.store import KnowledgeStore

        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "note.md").write_text("Note", encoding="utf-8")

        store = KnowledgeStore(tmp_path / "knowledge.db")
        pipeline = MagicMock()
        pipeline._dedup_enabled = False
        fw = FolderWatcher(store, pipeline)

        async def _ingest(file_path, source_id, namespace, props, old_ids, root: str = ""):
            return ["item-1"], "done"

        fw._ingest_file = _ingest  # type: ignore[assignment]
        source_id = store.add_source("t", "local_folder", str(vault), properties={})
        source = {
            "id": source_id,
            "uri": str(vault),
            "source_type": "local_folder",
            "properties": "{}",
        }
        await fw.scan_source(source)
        store.db.execute(
            "UPDATE folder_file_state SET last_seen = ? WHERE source_id = ?",
            ("1999-01-01", source_id),
        )
        store.db.commit()

        await fw.scan_source(source)  # unchanged file -> batched last_seen touch

        row = store.db.execute(
            "SELECT last_seen FROM folder_file_state WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        assert row["last_seen"] != "1999-01-01", "batched last_seen was never flushed"

    @pytest.mark.asyncio
    async def test_pause_still_stops_a_scan(self, tmp_path: Path) -> None:
        """Bounding the re-check must not remove the pause path."""
        from kiro_crew.knowledge.folder_watcher import FolderWatcher
        from kiro_crew.knowledge.store import KnowledgeStore

        vault = tmp_path / "vault"
        vault.mkdir()
        for i in range(5):
            (vault / f"note{i}.md").write_text(f"Note {i}", encoding="utf-8")

        store = KnowledgeStore(tmp_path / "knowledge.db")
        fw = FolderWatcher(store, MagicMock())
        source_id = store.add_source(
            "t", "local_folder", str(vault), properties={"scan_paused": True}
        )
        source = {
            "id": source_id,
            "uri": str(vault),
            "source_type": "local_folder",
            "properties": "{}",
        }

        stats = await fw.scan_source(source)

        assert stats["status"] == "paused"
        assert stats["new"] == 0


# ── Cached file reads: changelog + channel presets ─────────────────────────


class TestChangelogReadIsCached:
    """``GET /api/changelog`` read and decoded the whole file on the event loop
    on every request."""

    @pytest.mark.asyncio
    async def test_repeat_reads_hit_the_disk_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        changelog = proj / "CHANGELOG.md"
        changelog.write_text("# Changelog\n\n## [1.0.0]\n", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
        monkeypatch.setattr(updates, "_changelog_cache", None, raising=False)

        reads = {"n": 0}
        real_read = Path.read_text

        def _counting_read(self, *a, **k):
            if self.name == "CHANGELOG.md":
                reads["n"] += 1
            return real_read(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", _counting_read)

        first = await updates.api_changelog(MagicMock())
        for _ in range(10):
            await updates.api_changelog(MagicMock())

        assert json.loads(first.body)["content"].startswith("# Changelog")
        assert reads["n"] == 1, f"CHANGELOG.md was read {reads['n']} times, expected 1"

    @pytest.mark.asyncio
    async def test_edit_is_picked_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cache is keyed on the stat signature, so a dev-install edit must
        still be visible without a restart."""
        proj = tmp_path / "proj"
        proj.mkdir()
        changelog = proj / "CHANGELOG.md"
        changelog.write_text("first\n", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
        monkeypatch.setattr(updates, "_changelog_cache", None, raising=False)

        assert json.loads((await updates.api_changelog(MagicMock())).body)[
            "content"
        ] == "first\n"

        changelog.write_text("second edition\n", encoding="utf-8")
        st = changelog.stat()
        os.utime(changelog, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

        assert json.loads((await updates.api_changelog(MagicMock())).body)[
            "content"
        ] == "second edition\n"


class TestChannelPresetsReadIsCached:
    """``/api/channels/presets`` re-read and re-parsed config.json on every call
    so that an edit lands without a restart. The stat-keyed cache keeps that
    contract without the per-call read."""

    @pytest.mark.asyncio
    async def test_repeat_reads_hit_the_disk_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"channel_presets": [{"id": "a"}]}), encoding="utf-8")
        monkeypatch.setattr(handlers_channel, "config_path", lambda: cfg)
        monkeypatch.setattr(handlers_channel, "_presets_cache", None, raising=False)

        reads = {"n": 0}
        real_read = Path.read_text

        def _counting_read(self, *a, **k):
            if self == cfg:
                reads["n"] += 1
            return real_read(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", _counting_read)

        for _ in range(10):
            resp = await handlers_channel.api_channel_presets(MagicMock())

        assert json.loads(resp.body)["presets"] == [{"id": "a"}]
        assert reads["n"] == 1, f"config.json was read {reads['n']} times, expected 1"

    @pytest.mark.asyncio
    async def test_edit_is_picked_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"channel_presets": [{"id": "a"}]}), encoding="utf-8")
        monkeypatch.setattr(handlers_channel, "config_path", lambda: cfg)
        monkeypatch.setattr(handlers_channel, "_presets_cache", None, raising=False)

        resp = await handlers_channel.api_channel_presets(MagicMock())
        assert json.loads(resp.body)["presets"] == [{"id": "a"}]

        cfg.write_text(
            json.dumps({"channel_presets": [{"id": "b"}, {"id": "c"}]}),
            encoding="utf-8",
        )
        st = cfg.stat()
        os.utime(cfg, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

        resp = await handlers_channel.api_channel_presets(MagicMock())
        assert json.loads(resp.body)["presets"] == [{"id": "b"}, {"id": "c"}]
