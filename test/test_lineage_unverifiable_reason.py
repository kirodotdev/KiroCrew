"""A ``lineage_unverifiable`` refusal says what repairs it.

Every binding writer (crew create, crew rebind, the default-agent write, publish
and reset) refuses with ``409 lineage_unverifiable`` when it cannot tell whether
the target template is another crew's private copy. Some of those failures clear
on their own and some persist until the operator changes a file, so the
exception classifies the cause and the ``error`` sentence follows it:

* ``ambiguous_template_name``: two or more spec files resolve to the target and
  at least one of them resolves under another name. The sentence names every
  one of them; a retry cannot help. Files that all declare the target (the
  twins a package installer writes) are admitted instead: the lineage read of
  that one name already answered for each of them.
* ``ownership_record_unreadable``: the lineage sidecar exists but is not a
  readable JSON object. The sentence names the file and says to restore it; a
  retry cannot help, and deleting the file is never offered.
* ``read_failed``: an I/O error a retry can clear. The sentence keeps the retry hint.

The status code and the body's ``error`` and ``code`` fields are unchanged, so
a client that switches on ``code`` keeps working.
"""

from __future__ import annotations

import ast
import errno
import json
import unittest.mock
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

#: The ending of the one sentence every cause shared: ``... is a private copy; retry.``
_GENERIC_RETRY = "; retry."
#: Fragments of the two persistent-cause sentences.
_AMBIGUOUS = "more than one agent spec file resolves to that name"
_UNREADABLE = "its ownership record"


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    """Run as the dashboard owner: the owner gate has its own coverage in
    test_agents_endpoints_owner_auth.py."""
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _crud_app() -> web.Application:
    from kiro_crew.dashboard.handlers import (
        api_kirocrew_agent_update,
        api_kirocrew_agents_create,
    )

    app = web.Application()
    app.router.add_post("/api/agents", api_kirocrew_agents_create)
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    return app


def _seed_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "agents": {
                    "default": {
                        "kiro_agent": "kirocrew",
                        "workspace": "default",
                        "memory_store": "default",
                    },
                    "test-agent": {"kiro_agent": "kirocrew"},
                },
                "default_agent": "default",
                "workspaces": {"default": {"dir": "workspace"}},
            }
        ),
        encoding="utf-8",
    )
    return path


#: An agent two packages vend, which a package installer writes once per package.
_SHARED = "amzn-pipeline-assistant"


def _package_twins(name: str) -> list[Path]:
    """What a package installer writes for one agent two packages ship: a copy
    per dependent package plus a ``local-`` twin, all declaring *name*, each
    with its own prompt and hooks."""
    from kiro_crew.config.paths import kiro_agents_dir

    agents_dir = kiro_agents_dir()
    agents_dir.mkdir(parents=True, exist_ok=True)
    files = [
        agents_dir / f"PkgA-{name}.json",
        agents_dir / f"local-PkgA-{name}.json",
        agents_dir / f"PkgB-{name}.json",
    ]
    for i, f in enumerate(files):
        spec = {
            "name": name,
            "prompt": f"prompt {i}",
            "hooks": {"agentSpawn": [{"command": f"echo {i}"}]},
        }
        f.write_text(json.dumps(spec), encoding="utf-8")
    return files


def _claimants_of_two_names(name: str, package: str = "somepkg") -> list[Path]:
    """``<name>.json`` declaring a recorded private copy, beside a package file
    declaring *name*. A binding to *name* can run either file (the backend
    matches the file stem too), and one of them is another crew's copy, so the
    ownership read cannot answer."""
    from kiro_crew import agent_state
    from kiro_crew.config.paths import kiro_agents_dir

    agents_dir = kiro_agents_dir()
    agents_dir.mkdir(parents=True, exist_ok=True)
    stem_claimant = agents_dir / f"{name}.json"
    stem_claimant.write_text(json.dumps({"name": "design-copy", "prompt": "p"}), encoding="utf-8")
    agent_state.set_fork_info("design-copy", forked_from="kirocrew", private_to="design-crew")
    declarer = agents_dir / f"{package}-{name}.json"
    declarer.write_text(json.dumps({"name": name, "prompt": "p"}), encoding="utf-8")
    return [stem_claimant, declarer]


async def _create(client: TestClient, kiro_agent: str, name: str = "new-crew") -> tuple[int, dict]:
    resp = await client.post("/api/agents", json={"name": name, "kiro_agent": kiro_agent})
    return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_two_specs_for_one_name_name_both_files_and_drop_the_retry(
    tmp_path: Path,
) -> None:
    files = _claimants_of_two_names("sh-eval")
    cfg = _seed_config(tmp_path)
    try:
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg):
            async with TestClient(TestServer(_crud_app())) as client:
                # Named after the template: the one non-default template a
                # create admits, so the lineage check is what answers it.
                status, body = await _create(client, "sh-eval", name="sh-eval")
        assert status == 409
        assert set(body) == {"error", "code"}
        assert body["code"] == "lineage_unverifiable"
        assert _AMBIGUOUS in body["error"]
        for f in files:
            assert str(f) in body["error"]
        assert not body["error"].endswith(_GENERIC_RETRY)
        assert "sh-eval" not in json.loads(cfg.read_text(encoding="utf-8"))["agents"]
    finally:
        for f in files:
            f.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_a_credential_shaped_spec_filename_is_masked(tmp_path: Path) -> None:
    """Spec file names are written by whoever writes the agents directory, so
    the listed paths pass through the agents API's credential redaction."""
    probe = "AKIAIOSFODNN7EXAMPLE"
    files = _claimants_of_two_names("sh-eval", package=probe)
    cfg = _seed_config(tmp_path)
    try:
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg):
            async with TestClient(TestServer(_crud_app())) as client:
                status, body = await _create(client, "sh-eval", name="sh-eval")
        assert status == 409
        assert _AMBIGUOUS in body["error"]
        assert probe not in body["error"]
        assert str(files[0]) in body["error"]
    finally:
        for f in files:
            f.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "contents",
    [
        pytest.param("{not json", id="corrupt-json"),
        pytest.param("[]", id="not-an-object"),
    ],
)
@pytest.mark.asyncio
async def test_an_unreadable_ownership_record_names_the_file_and_the_repair(
    tmp_path: Path, contents: str
) -> None:
    from kiro_crew import agent_state

    sidecar = agent_state._state_path()
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(contents, encoding="utf-8")
    cfg = _seed_config(tmp_path)
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg):
        async with TestClient(TestServer(_crud_app())) as client:
            status, body = await _create(client, "kirocrew")
    assert status == 409
    assert body["code"] == "lineage_unverifiable"
    assert _UNREADABLE in body["error"]
    assert str(sidecar) in body["error"]
    assert "restore" in body["error"].lower()
    assert not body["error"].endswith(_GENERIC_RETRY)
    # The file was left as it was: the refusal never rewrites lineage state.
    assert sidecar.read_text(encoding="utf-8") == contents


@pytest.mark.asyncio
async def test_a_permission_denied_ownership_record_is_permanent(tmp_path: Path) -> None:
    from kiro_crew import agent_state

    denied = PermissionError(errno.EACCES, "Permission denied")
    cfg = _seed_config(tmp_path)
    with (
        unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg),
        unittest.mock.patch.object(agent_state, "get_fork_info", side_effect=denied),
    ):
        async with TestClient(TestServer(_crud_app())) as client:
            status, body = await _create(client, "kirocrew")
    assert status == 409
    assert _UNREADABLE in body["error"]
    assert "Permission denied" in body["error"]
    assert not body["error"].endswith(_GENERIC_RETRY)


@pytest.mark.asyncio
async def test_a_transient_read_error_keeps_the_retry_hint(tmp_path: Path) -> None:
    from kiro_crew import agent_state

    cfg = _seed_config(tmp_path)
    with (
        unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg),
        unittest.mock.patch.object(
            agent_state, "get_fork_info", side_effect=OSError(errno.EIO, "I/O error")
        ),
    ):
        async with TestClient(TestServer(_crud_app())) as client:
            status, body = await _create(client, "kirocrew")
    assert status == 409
    assert body["code"] == "lineage_unverifiable"
    assert body["error"].endswith(_GENERIC_RETRY)
    assert "I/O error" in body["error"]


@pytest.mark.parametrize("winerror", [32, 33], ids=["sharing-violation", "lock-violation"])
def test_a_windows_sharing_or_lock_violation_is_transient(winerror: int) -> None:
    """On Windows an indexer, antivirus scanner or concurrent writer holding the
    sidecar open surfaces as a ``PermissionError`` with a sharing or lock
    violation code. The file is fine and the next read succeeds, so the
    refusal keeps the retry hint instead of asking for a repair."""
    from kiro_crew.dashboard.handlers import agents

    held = PermissionError(errno.EACCES, "The process cannot access the file")
    held.winerror = winerror  # type: ignore[attr-defined]
    exc = agents._sidecar_unverifiable("t", held)
    assert exc.reason == "read_failed"
    assert "retry" in agents._lineage_unverifiable_error("t", exc).lower()

    denied = PermissionError(errno.EACCES, "Access is denied")
    denied.winerror = 5  # type: ignore[attr-defined]
    assert agents._sidecar_unverifiable("t", denied).reason == "ownership_record_unreadable"


@pytest.mark.asyncio
async def test_the_rebind_path_names_the_same_cause(tmp_path: Path) -> None:
    """``PUT /api/agents/{name}`` with only ``kiro_agent`` takes the locked
    rebind, whose ownership check runs inside the config write."""
    files = _claimants_of_two_names("sh-eval")
    cfg = _seed_config(tmp_path)
    try:
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg):
            async with TestClient(TestServer(_crud_app())) as client:
                resp = await client.put("/api/agents/test-agent", json={"kiro_agent": "sh-eval"})
                body = await resp.json()
        assert resp.status == 409
        assert body["code"] == "lineage_unverifiable"
        assert _AMBIGUOUS in body["error"]
        assert all(str(f) in body["error"] for f in files)
        on_disk = json.loads(cfg.read_text(encoding="utf-8"))
        assert on_disk["agents"]["test-agent"]["kiro_agent"] == "kirocrew"
    finally:
        for f in files:
            f.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_package_twins_of_one_agent_can_be_bound(tmp_path: Path) -> None:
    """Every twin declares the target, whose strict lineage read found no
    record, so the target is not a private copy whichever file runs. Which
    file a session runs is ``agent.spec_pins``'s question, not the binding's."""
    files = _package_twins(_SHARED)
    cfg = _seed_config(tmp_path)
    try:
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg):
            async with TestClient(TestServer(_crud_app())) as client:
                resp = await client.put("/api/agents/test-agent", json={"kiro_agent": _SHARED})
                body = await resp.json()
        assert resp.status == 200, body
        on_disk = json.loads(cfg.read_text(encoding="utf-8"))
        assert on_disk["agents"]["test-agent"]["kiro_agent"] == _SHARED
    finally:
        for f in files:
            f.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_a_crew_bound_to_package_twins_can_save_its_editor(tmp_path: Path) -> None:
    """The crew editor re-sends ``kiro_agent`` on every save, so the generic
    update path runs the ownership read even when the binding does not move."""
    files = _package_twins(_SHARED)
    cfg = _seed_config(tmp_path)
    seeded = json.loads(cfg.read_text(encoding="utf-8"))
    seeded["agents"]["test-agent"]["kiro_agent"] = _SHARED
    cfg.write_text(json.dumps(seeded), encoding="utf-8")
    try:
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg):
            async with TestClient(TestServer(_crud_app())) as client:
                resp = await client.put(
                    "/api/agents/test-agent",
                    json={"kiro_agent": _SHARED, "display_name": "Pipelines"},
                )
                body = await resp.json()
        assert resp.status == 200, body
        entry = json.loads(cfg.read_text(encoding="utf-8"))["agents"]["test-agent"]
        assert entry["kiro_agent"] == _SHARED
        assert entry["display_name"] == "Pipelines"
    finally:
        for f in files:
            f.unlink(missing_ok=True)


def test_the_ownership_read_finds_no_owner_for_package_twins() -> None:
    from kiro_crew.dashboard.handlers import agents

    files = _package_twins(_SHARED)
    try:
        assert agents._foreign_private_copy_owner("c", _SHARED) is None
    finally:
        for f in files:
            f.unlink(missing_ok=True)


def test_package_twins_of_a_private_copy_still_name_its_owner() -> None:
    """A recorded private copy is refused before any file is read, however
    many files declare it."""
    from kiro_crew import agent_state
    from kiro_crew.dashboard.handlers import agents

    files = _package_twins(_SHARED)
    agent_state.set_fork_info(_SHARED, forked_from="kirocrew", private_to="design-crew")
    try:
        assert agents._foreign_private_copy_owner("c", _SHARED) == "design-crew"
    finally:
        for f in files:
            f.unlink(missing_ok=True)


def _lineage_bodies() -> list[tuple[str, int, ast.Dict]]:
    """Every dict literal in the agent-admin owners whose ``code`` is
    ``lineage_unverifiable``."""
    root = (
        Path(__file__).resolve().parent.parent / "src" / "kiro_crew" / "dashboard" / "agent_admin"
    )
    found = []
    for path in sorted(root.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Dict):
                continue
            pairs = {
                k.value: v
                for k, v in zip(node.keys, node.values)
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            code = pairs.get("code")
            if isinstance(code, ast.Constant) and code.value == "lineage_unverifiable":
                found.append((path.name, node.lineno, node))
    return found


def test_every_lineage_refusal_builds_its_sentence_from_the_cause() -> None:
    """Each writer builds its body from the exception, so no site can fall back
    to a fixed "retry" sentence for a cause a retry cannot clear."""
    bodies = _lineage_bodies()
    # crew create, both crew-update paths, the default-agent write, publish, reset.
    assert len(bodies) == 6, bodies
    for where, line, node in bodies:
        keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
        assert keys == {"error", "code"}, f"{where}:{line}"
        error = next(
            v
            for k, v in zip(node.keys, node.values)
            if isinstance(k, ast.Constant) and k.value == "error"
        )
        assert isinstance(error, ast.Call), f"{where}:{line} builds a fixed error sentence"
        assert ast.unparse(error.func) == "_lineage_unverifiable_error", f"{where}:{line}"


def test_the_sentence_matches_each_reason() -> None:
    from kiro_crew.dashboard.handlers import agents

    listed = (Path("/a/t.json"), Path("/a/pkg-t.json"))
    exc = agents._UnverifiableLineage("t", "ambiguous_template_name", paths=listed)
    text = agents._lineage_unverifiable_error("t", exc)
    assert all(str(p) in text for p in listed)
    assert not text.endswith(_GENERIC_RETRY)

    exc = agents._UnverifiableLineage("t", "read_failed", cause=OSError(errno.EIO, "I/O error"))
    assert "retry" in agents._lineage_unverifiable_error("t", exc).lower()

    sidecar = Path("/s.json")
    exc = agents._UnverifiableLineage(
        "t", "ownership_record_unreadable", cause=ValueError("bad"), paths=(sidecar,)
    )
    text = agents._lineage_unverifiable_error("t", exc)
    assert str(sidecar) in text and "delete" in text.lower()
    assert not text.endswith(_GENERIC_RETRY)


@pytest.mark.parametrize("sentinel", ["agent_state_file_invalid", "agent_state_too_large"])
def test_a_wrong_kind_or_oversized_record_gets_a_remedy_that_covers_it(sentinel: str) -> None:
    """The strict reader refuses a symlinked, hard-linked or oversized sidecar
    with a bare sentinel. The remedy names those states and the reader's
    actual size cap, so the operator is not sent to fix a syntax error that
    is not there."""
    from kiro_crew import agent_state
    from kiro_crew.dashboard.handlers import agents

    exc = agents._sidecar_unverifiable("t", ValueError(sentinel))
    assert exc.reason == "ownership_record_unreadable"
    text = agents._lineage_unverifiable_error("t", exc)
    assert sentinel in text
    assert f"{agent_state.STATE_MAX_BYTES // (1024 * 1024)} MiB" in text
    assert "symlinked or hard-linked" in text
