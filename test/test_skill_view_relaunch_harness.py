"""Relaunch harness for the skill-view projection under a spec-rewriting launcher.

It rebuilds the host the incident happened on and drives the product through
many spawns, asserting the properties the incident broke rather than any one
code path:

* a FAKE KIRO-CLI WATCHER with the behaviour measured on kiro-cli 2.25.0 and
  2.26.0: it loads the agents directory at spawn and reloads it only on an
  ``IN_CREATE`` / ``IN_MODIFY`` for a ``*.json`` name. A rename into place
  (``IN_MOVED_TO``), an attribute change and a close without a write are all
  ignored, so a spec published by rename stays "not found" and a spec renamed
  over a known name keeps serving its old content. It is fed by real inotify
  events, so it observes exactly what the product did to the directory;
* an EXTERNAL REWRITER, a sandbox launcher that stamps every spec: before each
  spawn, and again while the process runs, it rewrites every ``*.json`` in the
  directory -- authored specs AND aliases -- through its own serializer,
  publishes each by rename (a new inode), stamps a fresh random value into an
  MCP server's ``env``, and leaves an empty ``<stem>.lock`` beside each spec.

Over N spawns, each switching every agent with ``session/set_mode`` through the
real ``AcpRuntime`` bracket and the real projection, it asserts: zero set_mode
failures and no fallback, the host always running the view Kiro Crew
published (never a stale one), exactly one alias per agent throughout,
bounded file counts in the agents directory and its hidden directories, and no
alias-lock timeout. Each fix, mutated away, fails at least one of these.

Linux only: the watcher is driven by inotify, which is what kiro-cli uses there.
"""

from __future__ import annotations

import asyncio
import ctypes
import gc
import json
import logging
import os
import struct
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew.acp import runtime as runtime_mod
from kiro_crew.acp import skill_projection as projection
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import METHOD_SET_MODE

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the fake watcher is driven by inotify"
)

SPAWNS = 50
AGENTS = ("kirocrew", "ops", "review", "plain")

_IN_MODIFY = 0x002
_IN_ATTRIB = 0x004
_IN_CLOSE_WRITE = 0x008
_IN_MOVED_TO = 0x080
_IN_CREATE = 0x100
_IN_DELETE = 0x200
_EVENT = struct.Struct("iIII")


def _canon(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _Inotify:
    def __init__(self, directory: Path) -> None:
        self._libc = ctypes.CDLL(None, use_errno=True)
        self.fd = self._libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        mask = _IN_MODIFY | _IN_ATTRIB | _IN_CLOSE_WRITE | _IN_MOVED_TO | _IN_CREATE | _IN_DELETE
        if self._libc.inotify_add_watch(self.fd, os.fsencode(directory), mask) < 0:
            raise OSError(ctypes.get_errno(), "inotify_add_watch")

    def events(self) -> list[tuple[int, str]]:
        out: list[tuple[int, str]] = []
        while True:
            try:
                buf = os.read(self.fd, 1 << 16)
            except BlockingIOError:
                return out
            offset = 0
            while offset < len(buf):
                _wd, mask, _cookie, length = _EVENT.unpack_from(buf, offset)
                offset += _EVENT.size
                name = buf[offset : offset + length].rstrip(b"\0").decode()
                offset += length
                out.append((mask, name))

    def close(self) -> None:
        os.close(self.fd)


class FakeKiroWatcher:
    """What kiro-cli 2.25/2.26 knows about ``~/.kiro/agents``, as measured."""

    def __init__(self, agents: Path, spawn_agent: str, *, broken_watch: bool = False) -> None:
        self.agents = agents
        self.broken_watch = broken_watch
        self._watch = _Inotify(agents)
        self.loaded = self._load()
        if spawn_agent not in self.loaded:
            raise AssertionError(f"spawn failed: kiro-cli found no {spawn_agent}.json")

    def _load(self) -> dict[str, dict]:
        loaded = {}
        for path in self.agents.glob("*.json"):
            try:
                spec = json.loads(path.read_bytes())
            except (OSError, ValueError):
                continue
            if isinstance(spec, dict) and spec.get("name"):
                loaded[path.stem] = spec
        return loaded

    def session_new_frame(self) -> bytes:
        """The reply line kiro-cli writes for ``session/new``: its per-agent info
        (name, description, source, welcomeMessage) for EVERY spec it loaded,
        aliases included, as ``AgentInfo`` in its session manager."""
        agents = [
            {
                "name": spec.get("name"),
                "description": spec.get("description"),
                "source": "Global",
                "welcomeMessage": spec.get("welcomeMessage"),
            }
            for spec in self.loaded.values()
        ]
        reply = {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s", "agents": agents}}
        return json.dumps(reply).encode() + b"\n"

    def pump(self) -> None:
        events = self._watch.events()
        if self.broken_watch:
            return
        if any(
            mask & (_IN_CREATE | _IN_MODIFY) and name.endswith(".json") for mask, name in events
        ):
            self.loaded = self._load()

    def set_mode(self, name: str) -> dict:
        self.pump()
        if name not in self.loaded:
            raise LookupError(name)
        return self.loaded[name]

    def close(self) -> None:
        self._watch.close()


class ExternalRewriter:
    """A sandbox launcher that rewrites every spec on every launch."""

    def __init__(
        self,
        agents: Path,
        *,
        fresh_nonce: bool = True,
        nonce_key: str = "AIM_CREDS_AGENT_INJECTION",
    ) -> None:
        self.agents = agents
        self.fresh_nonce = fresh_nonce
        self.nonce_key = nonce_key

    def launch(self) -> None:
        for path in sorted(self.agents.glob("*.json")):
            try:
                spec = json.loads(path.read_bytes())
            except (OSError, ValueError):
                continue
            if not isinstance(spec, dict):
                continue
            # The idempotent launcher stamps the same value everywhere, so what it
            # adds to an alias equals what it added to the spec it came from.
            nonce = str(uuid.uuid4()) if self.fresh_nonce else "stable"
            servers = spec.setdefault("mcpServers", {})
            servers["broker"] = {
                "command": "broker",
                "args": ["mcp", "serve"],
                "env": {"BROKER_URL": "${BROKER_URL}", self.nonce_key: nonce},
            }
            tools = spec.get("tools")
            if isinstance(tools, list) and "@broker" not in tools:
                spec["tools"] = [*tools, "@broker"]
            staged = path.with_name(f".{path.name}.launcher")
            staged.write_text(json.dumps(spec, indent=2).replace('": ', '" : '), encoding="utf-8")
            os.replace(staged, path)
            lock = path.with_suffix(".lock")
            if not lock.exists():
                lock.write_bytes(b"")


class _Host:
    """Answers the runtime's awaited requests from a :class:`FakeKiroWatcher`."""

    def __init__(self, reader: asyncio.StreamReader, watcher: FakeKiroWatcher) -> None:
        self.reader = reader
        self.watcher = watcher
        self.activated: list[tuple[str, dict]] = []
        self.failures: list[str] = []

    def write(self, data: bytes) -> None:
        frame = json.loads(data)
        if "id" not in frame:
            return
        if frame.get("method") != METHOD_SET_MODE:
            self._answer(frame["id"], result={})
            return
        mode = frame["params"]["modeId"]
        try:
            spec = self.watcher.set_mode(mode)
        except LookupError:
            self.failures.append(mode)
            self._answer(
                frame["id"],
                error={
                    "code": -32603,
                    "message": "Internal error",
                    "data": f"Mode '{mode}' not found",
                },
            )
            return
        self.activated.append((mode, spec))
        self._answer(frame["id"], result={})

    def _answer(self, req_id: int, **body: object) -> None:
        self.reader.feed_data(
            (json.dumps({"jsonrpc": "2.0", "id": req_id, **body}) + "\n").encode()
        )


@pytest.fixture
def host_tree(tmp_path, monkeypatch):
    monkeypatch.delenv("KIROCREW_NATIVE_SKILL_PROJECTION", raising=False)
    home = tmp_path / "kiro"
    crew_home = tmp_path / "crew"
    agents = home / "agents"
    agents.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(projection, "kiro_home", lambda: home)
    monkeypatch.setattr(projection, "data_home", lambda: crew_home, raising=False)
    monkeypatch.setattr(projection, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(projection.platform_compat, "path_volume_is_remote", lambda path: False)
    monkeypatch.setattr(projection.platform_compat, "first_linked_ancestor", lambda path: None)
    monkeypatch.setattr(
        "kiro_crew.agent.managed_mcp_spec_entry",
        lambda name: {"command": "test-core", "args": []},
    )
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [
            SimpleNamespace(name=name, filename=f"{name}.json", scope="global") for name in AGENTS
        ],
    )
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", home / "settings" / "mcp.json")
    monkeypatch.setattr(runtime_mod, "_PROJECTED_MODE_RETRY_DELAYS_SECS", (0.0, 0.0), raising=False)
    for name in AGENTS:
        resources = [] if name == "plain" else [f"skill://catalog/{name}/*/SKILL.md"]
        spec = {"name": name, "tools": ["read"], "resources": ["file://R.md", *resources]}
        (agents / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")
    return agents, project


def _runtime(watcher: FakeKiroWatcher, prepared, work_dir: Path) -> tuple[AcpRuntime, _Host]:
    # The fixture's project: the bracket re-prepares against it, writing its
    # workspace settings, which must stay inside the test's own tree.
    rt = AcpRuntime(work_dir=str(work_dir))
    reader = asyncio.StreamReader()
    host = _Host(reader, watcher)
    proc = MagicMock()
    proc.stdout = reader
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock(side_effect=host.write)
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = _UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = _UNALLOCATABLE_PID
    rt._initialized = True
    rt._native_skill_projection = prepared
    rt._spawn_skill_projection = prepared  # what spawn() records
    rt.terminate_session = AsyncMock()  # type: ignore[method-assign]
    return rt, host


def _census(agents: Path) -> dict[str, int]:
    prefix = projection.NATIVE_SKILL_ALIAS_PREFIX
    meta = agents / projection._PROJECTION_METADATA_DIR_NAME
    leases = agents / projection._PROJECTION_LEASE_DIR_NAME
    return {
        "aliases": len(list(agents.glob(f"{prefix}*.json"))),
        "alias_locks": len(list(agents.glob(f"{prefix}*.lock"))),
        "entries": len(list(agents.iterdir())),
        # Sidecars only: the directory also holds the view ledger.
        "sidecars": len([p for p in meta.glob(f"{prefix}*.json")]) if meta.is_dir() else 0,
        "leases": len(list(leases.iterdir())) if leases.is_dir() else 0,
    }


def _seed_backlog(agents: Path, count: int) -> None:
    """What 0.8.0.2 left on the incident host before the fix: re-serialized
    aliases whose sidecars bind their old bytes, orphaned sidecars, and old
    ``<alias>.lock`` residue, all backdated."""
    home_id = projection.data_home().absolute().as_posix()
    meta = agents / projection._PROJECTION_METADATA_DIR_NAME
    meta.mkdir(exist_ok=True)
    old = time.time() - 3 * 3600
    for n in range(count):
        stem = f"{projection.NATIVE_SKILL_ALIAS_PREFIX}{n:024x}"
        raw = json.dumps(
            {"name": stem, "resources": [], "mcpServers": {"kirocrew-core": {"command": "x"}}}
        )
        record = {
            projection._MANAGED_MARKER: projection._MANAGED_MARKER_VALUE,
            projection._MANAGED_CREW_HOME: home_id,
            projection._MANAGED_ALIAS_SHA256: "0" * 64,
        }
        (meta / f"{stem}.json").write_text(json.dumps(record), encoding="utf-8")
        lock = agents / f"{stem}.lock"
        lock.write_bytes(b"")
        os.utime(lock, (old, old))
        if n % 2 == 0:
            alias = agents / f"{stem}.json"
            alias.write_text(json.dumps(json.loads(raw), indent=2), encoding="utf-8")
            os.utime(alias, (old, old))


async def _one_spawn(
    agents, project, rewriter, *, broken_watch=False, between=None, refused=frozenset()
):
    rewriter.launch()  # a sibling launch before this spawn
    prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    watcher = FakeKiroWatcher(agents, prepared.agent("kirocrew"), broken_watch=broken_watch)
    rt, host = _runtime(watcher, prepared, project)
    del prepared
    rewriter.launch()  # another launch while this process runs
    if between is not None:
        between()
    task = asyncio.ensure_future(rt._reader_loop())
    await asyncio.sleep(0)
    views: dict[str, str] = {}
    try:
        with patch("kiro_crew.agent.require_unchanged_derived_spec", return_value=None):
            for name in AGENTS:
                before = len(host.activated)
                if name in refused:
                    # The host never loads its fresh alias: the start fails and no
                    # other copy of the agent is activated.
                    with pytest.raises(runtime_mod.AcpRuntimeError, match="Restart the gateway"):
                        await asyncio.wait_for(
                            rt._activate_mode_bracketed(
                                "s1", name, budget=5.0, payload_snapshot=None, wire_registered=True
                            ),
                            timeout=10,
                        )
                    assert len(host.activated) == before
                    continue
                await asyncio.wait_for(
                    rt._activate_mode_bracketed(
                        "s1", name, budget=5.0, payload_snapshot=None, wire_registered=True
                    ),
                    timeout=10,
                )
                assert len(host.activated) == before + 1
                mode, spec = host.activated[-1]
                views[name] = mode
                on_disk = agents / f"{mode}.json"
                if not broken_watch:
                    # The host runs exactly the spec on disk -- the view Kiro Crew
                    # published, env values included -- never a stale copy.
                    published = _canon(json.loads(on_disk.read_bytes()))
                    assert _canon(spec) == published, f"stale view of {name}"
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        watcher.close()
    assert rt.terminate_session.await_count == len(refused)
    failures = list(host.failures)
    rt._native_skill_projection = None
    del rt, host
    gc.collect()
    return views, failures


def _problems(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and ("alias lock unavailable" in r.getMessage() or "set_mode" in r.getMessage())
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh_nonce", [True, False], ids=["nonce-per-launch", "reserialize-only"])
async def test_fifty_spawns_under_a_rewriting_launcher_never_fail_and_stay_bounded(
    host_tree, caplog, fresh_nonce
):
    agents, project = host_tree
    caplog.set_level(logging.INFO)
    rewriter = ExternalRewriter(agents, fresh_nonce=fresh_nonce)
    _seed_backlog(agents, 40)
    # The launcher ran moments ago, as it does on every launch on that host, so
    # every alias in the backlog carries a fresh mtime.
    rewriter.launch()
    # The gateway's boot drain, against the backlog the incident left.
    projection.drain_stale_aliases()
    after_drain = _census(agents)
    assert after_drain["aliases"] == 0 and after_drain["sidecars"] == 0
    assert after_drain["alias_locks"] == 0

    first_views: dict[str, str] | None = None
    for spawn in range(SPAWNS):
        views, failures = await _one_spawn(agents, project, rewriter)
        # Recoveries count too: a projection problem must not even need one here.
        assert failures == [], f"spawn {spawn}: set_mode missed {failures}"
        if first_views is None:
            first_views = views
        assert views == first_views, f"spawn {spawn}: alias names moved {views}"
        census = _census(agents)
        assert census["aliases"] == len(AGENTS), f"spawn {spawn}: {census}"
        assert census["sidecars"] == len(AGENTS), f"spawn {spawn}: {census}"
        assert census["leases"] <= 2, f"spawn {spawn}: {census}"
        assert census["alias_locks"] <= len(AGENTS), f"spawn {spawn}: {census}"
        assert census["entries"] <= 2 * len(AGENTS) + 2 * len(AGENTS) + 3, census
    assert _problems(caplog) == []


@pytest.mark.asyncio
async def test_a_launcher_that_only_reserializes_triggers_no_republish(host_tree, monkeypatch):
    """The republish loop, measured directly: after the first
    publication, a preparation writes no alias and no sidecar at all when the
    only change is the launcher's serialization."""
    agents, project = host_tree
    rewriter = ExternalRewriter(agents, fresh_nonce=False)
    await _one_spawn(agents, project, rewriter)
    writes: list[str] = []
    real = projection.atomic_write

    def counting(path, *args, **kwargs):
        writes.append(Path(path).name)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(projection, "atomic_write", counting)
    for _ in range(5):
        await _one_spawn(agents, project, rewriter)
    assert not [w for w in writes if w.startswith(projection.NATIVE_SKILL_ALIAS_PREFIX)]


@pytest.mark.asyncio
async def test_a_host_whose_watch_never_fires_never_runs_a_stale_copy(host_tree, caplog):
    """The worst host: kiro-cli never reloads at all (its watch is gone, e.g. an
    exhausted inotify limit), and the operator edits an agent mid-run so its
    view -- and alias -- really change. The edited agent's start fails with the
    restart remedy after the forced reload and retries; neither its spawn alias
    nor its cached authored spec (either may carry grants the edit removed) is
    ever activated, and every other agent still starts."""
    agents, project = host_tree
    caplog.set_level(logging.INFO)
    rewriter = ExternalRewriter(agents)
    counter = []

    def edit_ops():
        spec = json.loads((agents / "ops.json").read_bytes())
        spec["description"] = f"edited {uuid.uuid4()}"
        spec["tools"] = [*spec.get("tools", []), f"edit-{uuid.uuid4().hex[:6]}"]
        (agents / "ops.json").write_text(json.dumps(spec), encoding="utf-8")

    with patch.object(runtime_mod, "emit_counter", lambda n, a: counter.append(a["outcome"])):
        for _ in range(5):
            prepared_before = projection.prepare_native_skill_projection(project)
            assert prepared_before is not None
            spawn_aliases = set(prepared_before.aliases.values())
            del prepared_before
            views, failures = await _one_spawn(
                agents, project, rewriter, broken_watch=True, between=edit_ops, refused={"ops"}
            )
            # Only the edited agent misses, only on aliases the host never loaded.
            assert failures and not set(failures) & spawn_aliases
            assert "ops" not in views
            assert all(views[name] != name for name in AGENTS if name != "ops")
    assert counter and set(counter) == {"refused_unloaded"}
    assert any("refusing to start" in m for m in _problems(caplog))


@pytest.mark.asyncio
async def test_a_rewriter_that_wins_the_announcement_race_never_runs_an_unverified_copy(
    host_tree, monkeypatch, caplog
):
    """The one window the in-place announcement cannot close: the launcher
    replaces a just-published alias (by rename, which the host ignores) before
    the announcement runs, so the announcement sees bytes it did not write and
    leaves them alone. An agent added after spawn has no spawn alias, so after
    the forced reload and retries its fresh alias is still not loaded: the start
    is refused with the restart remedy, never run on another copy, and counted."""
    agents, project = host_tree
    caplog.set_level(logging.INFO)
    roster = list(AGENTS)
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [
            SimpleNamespace(name=name, filename=f"{name}.json", scope="global") for name in roster
        ],
    )
    rewriter = ExternalRewriter(agents)
    prepared = projection.prepare_native_skill_projection(project)
    watcher = FakeKiroWatcher(agents, prepared.agent("kirocrew"))
    rt, host = _runtime(watcher, prepared, project)
    # An operator adds an agent while the process runs; written in place, so
    # the host loads the authored spec.
    (agents / "late.json").write_text(
        json.dumps({"name": "late", "resources": ["skill://catalog/late/*/SKILL.md"]}),
        encoding="utf-8",
    )
    roster.append("late")
    watcher.pump()  # its reload debounce elapses before the next spawn's set_mode
    real_announce = projection._announce_publication

    def launcher_wins(path, data):
        rewriter.launch()
        real_announce(path, data)

    monkeypatch.setattr(projection, "_announce_publication", launcher_wins)
    counter: list[str] = []
    task = asyncio.ensure_future(rt._reader_loop())
    await asyncio.sleep(0)
    try:
        with (
            patch("kiro_crew.agent.require_unchanged_derived_spec", return_value=None),
            patch.object(runtime_mod, "emit_counter", lambda n, a: counter.append(a["outcome"])),
            pytest.raises(runtime_mod.AcpRuntimeError, match="Restart the gateway"),
        ):
            await asyncio.wait_for(
                rt._activate_mode_bracketed(
                    "s1", "late", budget=5.0, payload_snapshot=None, wire_registered=True
                ),
                timeout=10,
            )
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        watcher.close()
    rt.terminate_session.assert_awaited_once_with("s1")
    assert all(mode != "late" for mode, _spec in host.activated)
    assert counter == ["refused_unloaded"]


@pytest.mark.asyncio
async def test_many_agents_with_huge_welcome_messages_keep_session_new_under_the_frame_cap(
    host_tree, monkeypatch, caplog
):
    """An agent package with 50 agents, each carrying its ~113 KB release notes as
    a welcomeMessage: kiro-cli copies every agent's welcomeMessage into the
    session/new reply, so the authored specs alone are ~5.6 MB. Aliases that
    duplicated it would double that past the 10 MB frame limit, the reply would be
    dropped, and the session would fail. The reply stays under the limit, and
    the package is named."""
    agents, project = host_tree
    notes = "- fixed a thing in the release\n" * 3650  # ~113 KB
    package = [f"pkg-agent-{n:02d}" for n in range(50)]
    for name in package:
        spec = {
            "name": name,
            "tools": ["read"],
            "resources": [f"skill://catalog/{name}/*/SKILL.md"],
            "welcomeMessage": notes,
        }
        (agents / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [
            SimpleNamespace(name=name, filename=f"{name}.json", scope="global")
            for name in (*AGENTS, *package)
        ],
    )
    projection._DISPLAY_TEXT_WARNED.clear()
    caplog.set_level(logging.WARNING, logger="kiro_crew.acp.skill_projection")

    prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None and set(package) <= set(prepared.aliases)
    watcher = FakeKiroWatcher(agents, prepared.agent("kirocrew"))
    try:
        frame = watcher.session_new_frame()
    finally:
        watcher.close()

    assert len(frame) > 5 * 1024 * 1024, "the fixture must carry the field report's load"
    assert len(frame) < runtime_mod._STDOUT_BUFFER_LIMIT
    messages = [r.getMessage() for r in caplog.records]
    assert any("pkg-agent-" in m and "session/new" in m for m in messages)


@pytest.mark.asyncio
async def test_a_launcher_nonce_under_an_unknown_key_is_named_and_can_be_declared(
    host_tree, monkeypatch
):
    """A launcher stamping its per-launch value under a key the volatile set does
    not know mints a view per launch; the census names that key (the doctor line
    prints it), and declaring it volatile stops the growth."""
    agents, project = host_tree
    monkeypatch.delenv("KIROCREW_SKILL_VIEW_VOLATILE_ENV", raising=False)
    rewriter = ExternalRewriter(agents, nonce_key="LAUNCH_ID")
    for _ in range(3):
        await _one_spawn(agents, project, rewriter)
    assert "broker.LAUNCH_ID" in projection.census_churning_env_keys(agents)
    grew = _census(agents)["aliases"]
    assert grew > len(AGENTS)

    monkeypatch.setenv("KIROCREW_SKILL_VIEW_VOLATILE_ENV", "LAUNCH_ID")
    names = set()
    for _ in range(3):
        views, _failures = await _one_spawn(agents, project, rewriter)
        names.add(tuple(sorted(views.values())))
    assert len(names) == 1, "one view name per agent once the key is declared volatile"
