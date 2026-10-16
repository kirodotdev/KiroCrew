"""Fixtures and helpers shared by the ``settings.local.json`` seed-provenance tests.

``test_acp_seed_provenance.py`` and its ``_sibling_sharing``, ``_sharer_teardown`` and
``_durable_grant`` siblings import these by name, and ``test_cc_permission_mode_seed.py``
reaches ``_OWNER``, ``_fp``, ``_pin_holder_identities`` and ``isolated_records`` through
``test_acp_seed_provenance.py``. ``_pinned_kiro_cli_version`` and ``isolated_records`` are
autouse, and every seed-provenance module imports both, so each one pins the floor
kiro-cli release and starts every test with empty process-wide provenance state. The rest
builds a claude-backend ``AcpClient`` over a temp work dir, fingerprints and plants sidecar
holders, pins holder liveness, and stands in for the owning process dying.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path

import pytest

from kiro_crew.acp import seed_provenance as sp
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

_SERVED = [
    "global.anthropic.claude-opus-5[1m]",
    "global.anthropic.claude-opus-4-8[1m]",
]

# The owner token a bare-sidecar test records under. A client uses its own
# ``_seed_owner``; these tests only need one stable identity.
_OWNER = "owner-under-test"


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the test_acp_seed_provenance*.py modules calls monkeypatch.undo()
def _pinned_kiro_cli_version(monkeypatch):
    """Pin the kiro-cli release the spec ``permissions`` gate believes is installed.

    A client start here materialises the agent spec (``ensure_agent_materialized``
    -> ``rebuild_agent_config`` -> ``_write_derived_permissions``), which reads
    ``installed_kiro_cli_version`` function-locally from ``kiro_crew.kiro_cli``:
    one real ``kiro-cli --version`` spawn per binary identity, process-cached, so
    whichever test in the worker starts first pays it against the HOST's install
    with the checkout as the child's cwd. Pinned to the floor release, as
    ``test_agent.py`` and the generated-writer suites pin it.
    """
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version",
        lambda: SPEC_PERMISSIONS_MIN_VERSION,
    )


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the test_acp_seed_provenance*.py modules calls monkeypatch.undo()
def isolated_records(monkeypatch):
    """Per-test provenance state.

    ``_RECORDS`` and ``_LIVE`` are process-wide runtime state (like
    ``model_registry._ADVERTISED_MODELS``); the sidecar itself already lands in a
    per-test ``KIROCREW_HOME``.
    """
    monkeypatch.setattr(sp, "_RECORDS", {})
    monkeypatch.setattr(sp, "_LIVE", {})
    monkeypatch.setattr(sp, "_SHARERS", {})


def _client(tmp_path: Path, **kw) -> AcpClient:
    return AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, **kw)


def _fp(payload: str) -> tuple[int, str]:
    """The ``(size, sha256)`` fingerprint of *payload*, as the record stores it."""
    raw = payload.encode("utf-8")
    return len(raw), hashlib.sha256(raw).hexdigest()


_FOREIGN_PID = 99_999_999_999
_FOREIGN_START_ID = "foreign-process-incarnation"
_OWN_START_ID = "test-process-incarnation"


def _pin_holder_identities(monkeypatch, *, foreign_live: bool) -> dict[str, object]:
    """Give holder-liveness tests deterministic local and foreign identities."""
    own_pid = os.getpid()
    real_pid_exists = sp.platform_compat.pid_exists
    real_get_start_id = sp.platform_compat.get_process_start_id

    def _pid_exists(pid: int) -> bool:
        if pid == _FOREIGN_PID:
            return foreign_live
        if pid == own_pid:
            return True
        return real_pid_exists(pid)

    def _get_start_id(pid: int) -> str | None:
        if pid == _FOREIGN_PID:
            return _FOREIGN_START_ID
        if pid == own_pid:
            return _OWN_START_ID
        return real_get_start_id(pid)

    monkeypatch.setattr(sp.platform_compat, "pid_exists", _pid_exists)
    monkeypatch.setattr(sp.platform_compat, "get_process_start_id", _get_start_id)
    return {"pid": _FOREIGN_PID, "start_id": _FOREIGN_START_ID}


def _write_durable_holder(
    path: Path,
    payload: str,
    *,
    kind: str,
    owner: str,
    identity: dict[str, object],
) -> None:
    """Write one durable holder without publishing it into this process's caches."""
    holders: dict[str, dict[str, dict[str, object]]] = {
        sp._HOLDER_OWNERS: {},
        sp._HOLDER_SHARERS: {},
    }
    holders[kind][owner] = dict(identity)
    size, sha256 = _fp(payload)
    sp._sidecar_path().parent.mkdir(parents=True, exist_ok=True)
    sp._sidecar_path().write_text(
        json.dumps(
            {
                "seeds": {
                    os.fspath(path): {
                        "size": size,
                        "sha256": sha256,
                        "holders": holders,
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _settings(tmp_path: Path) -> Path:
    return tmp_path / ".claude" / "settings.local.json"


def _seed(tmp_path: Path) -> dict:
    return json.loads(_settings(tmp_path).read_text(encoding="utf-8"))


def _teardown(client: AcpClient) -> None:
    """Tear a client down the way every real caller does.

    The seed's removal is a DISK operation -- a durable revoke, then an unlink -- so
    it lives in the async ``_discard_claude_settings_seed`` and reaches the
    filesystem through ``asyncio.to_thread``. ``_reset_state`` stays synchronous and
    keeps only the in-memory claim release, so calling it alone leaves the file
    behind on purpose. Every production caller awaits the discard first and then
    resets, and this helper is that pair, so a test that drifts from the real
    ordering fails here rather than passing on a shape nothing uses.
    """
    asyncio.run(client._discard_claude_settings_seed())
    client._reset_state()


def _unwritable_sidecar(*_args, **_kwargs):
    """What a read-only data home (or a full disk) does to the sidecar publish.

    Patched over ``seed_provenance.atomic_write`` specifically, which is a DIFFERENT
    binding from the one ``acp.client`` uses for the settings file itself -- so the
    seed still lands and only its durable grant fails, which is the case under test.
    """
    raise OSError("EROFS: read-only file system")


def _break_the_lock_when(monkeypatch, broken) -> None:
    """Make the cross-process registry lock unopenable whenever *broken()* is true.

    What a data home turning read-only, or a root-owned ``.settings_seeds.lock``,
    does to ``seed_provenance._cross_process_lock``: its ``os.open`` raises. The
    predicate is evaluated on every acquisition, so a test can leave the lock
    healthy for the barriers that run before a file exists and break it for the
    calls that follow the write (or the move-aside) -- the window in which a
    reader raising out of the lock would leave a seed with no record behind it.
    """
    real_lock = sp._cross_process_lock

    @contextlib.contextmanager
    def _lock():
        if broken():
            raise OSError("EACCES: .settings_seeds.lock is not openable")
        with real_lock():
            yield

    monkeypatch.setattr(sp, "_cross_process_lock", _lock)


def _the_owning_process_died() -> None:
    """Simulate a crashed process: leases stale, sidecar digest still durable."""
    if sp._sidecar_path().is_file():
        sidecar = json.loads(sp._sidecar_path().read_text(encoding="utf-8"))
        for entry in sidecar.get("seeds", {}).values():
            for group in entry.get("holders", {}).values():
                for identity in group.values():
                    identity["start_id"] = "stale-process-incarnation"
        sp._sidecar_path().write_text(json.dumps(sidecar), encoding="utf-8")
    sp._RECORDS.clear()
    sp._LIVE.clear()
    sp._SHARERS.clear()
    sp._load()
