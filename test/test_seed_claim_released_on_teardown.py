"""The settings-seed claim is released on every teardown path, including the
synchronous one that bypasses the client's shutdown.

``AcpClient.shutdown`` hands back the ``settings.local.json`` registrations a
client took in :mod:`kiro_crew.acp.seed_provenance` (``_discard_claude_settings_seed``
and ``_reset_state``). A provider torn down through ``_dispatch_hard_kill`` reaches
``session_pid._sync_kill_provider`` instead and never runs ``shutdown``, so that
path has to hand them back itself. Without that hand-back the client's live owner
claim (``_LIVE``) and its persisted owner holder stay in place for the rest of the
gateway process, and every later client on that ``work_dir`` finds the path held
by a client that is gone: ``claim`` refuses the adoption, ``held_by_another``
refuses a fresh create, and a client whose payload the stale seed cannot be shared
with runs with its whole ``mcpServers`` array withheld.

These tests drive the teardown entry point directly with a stub provider wrapping
a real client. No process is started and the teardown acts on none: the stub
either never published a process, or records one whose identity cannot be proven,
so the teardown completes without reaching any process.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew import model_registry as mr
from kiro_crew import runtime_ownership as ro
from kiro_crew import session_pid
from kiro_crew.acp import client as acp_client
from kiro_crew.acp import seed_provenance as sp
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION
from kiro_crew.session_pid import _sync_kill_provider

_SERVED = [
    "global.anthropic.claude-opus-5[1m]",
    "global.anthropic.claude-opus-4-8[1m]",
]

# A recorded pid that names no process on this host; its liveness probes are
# pinned below so the teardown's identity checks answer deterministically.
_TORN_DOWN_PID = 2**22 + 7
_TORN_DOWN_START_ID = "torn-down-process-incarnation"
# A recorded pid the gate-following tests present as a runtime that may still be
# alive. It names no process on this host either; each test pins the probes it
# needs, or returns at the ownership gate before any probe is read.
_LIVE_PID = 2**22 + 11
_LIVE_START_ID = "live-process-incarnation"


@pytest.fixture(autouse=True)
def _pinned_kiro_cli_version(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the kiro-cli release the spec ``permissions`` gate believes is installed."""
    _floor_monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version",
        lambda: SPEC_PERMISSIONS_MIN_VERSION,
    )


@pytest.fixture(autouse=True)
def isolated_records(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-test provenance state; the sidecar lands in the per-test data home.

    Patched through the isolation floor's own ``MonkeyPatch``, never the shared
    ``monkeypatch``, so a test's ``undo()`` cannot lift this isolation (D11).
    """
    _floor_monkeypatch.setattr(sp, "_RECORDS", {})
    _floor_monkeypatch.setattr(sp, "_LIVE", {})
    _floor_monkeypatch.setattr(sp, "_SHARERS", {})


@pytest.fixture(autouse=True)
def warm_model_cache(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """A warm advertised-model cache, so the seed carries the model half."""
    _floor_monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})


def _client(tmp_path: Path, **kw) -> AcpClient:
    kw.setdefault("acp_backend", ACP_BACKEND_CLAUDE)
    kw.setdefault("permission_mode", "default")
    return AcpClient(work_dir=tmp_path, **kw)


def _settings(tmp_path: Path) -> Path:
    return tmp_path / ".claude" / "settings.local.json"


def _stub_provider(client: AcpClient) -> MagicMock:
    """A provider stand-in the way ``_sync_kill_provider`` reads one: ``_client`` only."""
    provider = MagicMock(spec=["_client", "_proc", "_active_proc"])
    provider._client = client
    provider._proc = None
    provider._active_proc = None
    return provider


@pytest.fixture(autouse=True)
def _pin_torn_down_process(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the recorded pid read as a process that is gone, everything else real."""
    real_pid_exists = sp.platform_compat.pid_exists
    real_get_start_id = sp.platform_compat.get_process_start_id

    def _pid_exists(pid: int) -> bool:
        if pid == _TORN_DOWN_PID:
            return False
        return real_pid_exists(pid)

    def _get_start_id(pid: int) -> str | None:
        if pid == _TORN_DOWN_PID:
            return None
        return real_get_start_id(pid)

    _floor_monkeypatch.setattr(sp.platform_compat, "pid_exists", _pid_exists)
    _floor_monkeypatch.setattr(sp.platform_compat, "get_process_start_id", _get_start_id)


def _tear_down_without_shutdown(client: AcpClient) -> None:
    """Tear the client's provider down the way ``_dispatch_hard_kill`` does.

    The stub never published a process, so the teardown has nothing to act on
    and returns after its hand-backs; the client object is then dropped exactly
    as the dispatch sites drop it, with ``shutdown`` never called.
    """
    assert client._pid is None
    _sync_kill_provider(_stub_provider(client))


def _torn_down_with_a_recorded_process(client: AcpClient) -> None:
    """The same teardown for a client that had published a process.

    The identity recorded at spawn matches nothing on the host, so the teardown
    refuses to act on the recorded pid and completes without acting on any
    process -- the shape a provider whose process is already gone presents.
    """
    client._pid = _TORN_DOWN_PID
    client._start_time = _TORN_DOWN_START_ID
    _sync_kill_provider(_stub_provider(client))


class TestOwnerClaimReleasedOnTeardown:
    """A torn-down owner leaves a recorded orphan, never a live claim."""

    def test_a_torn_down_owner_no_longer_holds_the_live_slot(self, tmp_path):
        owner = _client(tmp_path, model=_SERVED[0])
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        key = os.fspath(path)
        assert sp._LIVE[key] == owner._seed_owner

        _tear_down_without_shutdown(owner)

        # The in-process slot and the persisted owner holder are both handed
        # back; the file and its durable record stay, which is the recorded-orphan
        # shape the next session adopts and repairs.
        assert key not in sp._LIVE
        assert owner._seed_owner not in sp._read_disk_seeds()[key]["holders"][sp._HOLDER_OWNERS]
        assert path.is_file()
        assert sp.recorded_durable(path) is not None
        assert sp.held_by_another(path, "a-later-session") is False

    def test_the_next_client_adopts_the_seed_a_torn_down_owner_left(self, tmp_path):
        """The reporter's shape: the stale seed pins a model the next client does not run.

        A pinned owner is torn down; an unpinned client then starts on the same
        ``work_dir``. The seed cannot be shared with it (a sharer may not be left on
        the owner's pin), so without the hand-back it fell to the leave-it-alone
        branch and ran with its ``mcpServers`` array withheld. With the claim
        released it adopts the orphan, re-seeds it for itself and is governed.
        """
        owner = _client(tmp_path, model=_SERVED[0])
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        key = os.fspath(path)

        _torn_down_with_a_recorded_process(owner)

        successor = _client(tmp_path)
        successor._write_claude_local_settings()

        assert successor._claude_settings_authored is True
        assert successor._permission_surface_governed is True
        assert sp._LIVE[key] == successor._seed_owner
        assert path.read_text(encoding="utf-8") == successor._claude_settings_written

    def test_a_different_permission_mode_is_re_seeded_not_frozen(self, tmp_path):
        """A stale permission mode must not become permanent project state."""
        owner = _client(tmp_path, permission_mode="bypassPermissions")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)

        _torn_down_with_a_recorded_process(owner)

        successor = _client(tmp_path, permission_mode="default")
        successor._write_claude_local_settings()

        assert successor._claude_settings_authored is True
        assert '"bypassPermissions"' not in path.read_text(encoding="utf-8")

    def test_the_in_memory_half_alone_does_not_free_the_path(self, tmp_path):
        """Why the hand-back is the durable ``release``, not ``release_local``.

        The owner holder ``record`` persisted carries this process's live
        identity, and ``claim`` re-reads it under the cross-process lock: with
        only the in-process slot dropped, the next client's adoption is still
        refused by a holder whose client is gone.
        """
        owner = _client(tmp_path, model=_SERVED[0])
        owner._write_claude_local_settings()
        path = _settings(tmp_path)

        sp.release_local(path, owner._seed_owner)

        assert os.fspath(path) not in sp._LIVE
        assert sp.claim(path, "a-later-session", expect_digest=sp.recorded_durable(path)) is False
        assert sp.held_by_another(path, "a-later-session") is True

    def test_a_refused_durable_withdrawal_still_frees_the_live_slot(
        self, tmp_path, monkeypatch, caplog
    ):
        """Same contract as ``_reset_state`` after a refused release.

        With the registry lock unopenable the sidecar cannot be rewritten: the
        in-process slot is handed back anyway so this process is not wedged, the
        refusal is logged, and the persisted holder stands until this process
        exits, after which it is stale and reclaimable.
        """
        owner = _client(tmp_path, model=_SERVED[0])
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        key = os.fspath(path)

        def _unopenable_lock():
            raise OSError("EACCES: .settings_seeds.lock is not openable")

        # Scoped to this block, so the hand-back's lock fault is lifted before the
        # sidecar is read back below.
        with monkeypatch.context() as broken:
            broken.setattr(sp, "_cross_process_lock", _unopenable_lock)
            with caplog.at_level(logging.WARNING, logger=acp_client.__name__):
                _torn_down_with_a_recorded_process(owner)

        assert key not in sp._LIVE
        assert owner._claude_settings_authored is False
        assert "could not durably hand back Crew's registration" in caplog.text
        assert owner._seed_owner in sp._read_disk_seeds()[key]["holders"][sp._HOLDER_OWNERS]

    def test_an_owed_hand_back_on_a_replaced_file_is_retried(self, tmp_path, monkeypatch):
        """A user replaced the seed and the in-session hand-back could not land.

        The client then owes only the durable owner holder, which the discard
        would have retried at shutdown. The teardown path retries it the same
        way and never touches the user's file.
        """
        owner = _client(tmp_path)
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        key = os.fspath(path)
        replacement = '{"permissions": {"allow": ["Bash(ls)"]}}\n'
        path.write_text(replacement, encoding="utf-8")
        with monkeypatch.context() as refused:
            refused.setattr(sp, "forget", lambda _p, _o: False)
            refused.setattr(sp, "release", lambda _p, _o: False)
            owner._write_claude_local_settings()
        assert owner._claude_settings_claim_unrevoked is True
        assert owner._seed_owner in sp._read_disk_seeds()[key]["holders"][sp._HOLDER_OWNERS]

        _torn_down_with_a_recorded_process(owner)

        assert owner._claude_settings_claim_unrevoked is False
        # ``forget`` lands when nothing else holds the record, so the record itself
        # is gone; under a live sharer it would be ``release`` and the holder alone.
        remaining = sp._read_disk_seeds().get(key, {}).get("holders", {}).get(sp._HOLDER_OWNERS, {})
        assert owner._seed_owner not in remaining
        assert path.read_text(encoding="utf-8") == replacement


class TestSharerLeaseReleasedOnTeardown:
    """A torn-down sharer stops pinning the owner's seed."""

    def test_a_torn_down_sharer_withdraws_its_lease(self, tmp_path):
        owner = _client(tmp_path)
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        key = os.fspath(path)
        sharer = _client(tmp_path)
        sharer._write_claude_local_settings()
        assert sharer._claude_settings_shared is True
        assert sp.has_sharers(path) is True

        _torn_down_with_a_recorded_process(sharer)

        assert sharer._seed_owner not in sp._SHARERS.get(key, set())
        assert sp.has_sharers(path) is False
        # The owner's own live claim is untouched by a sibling's teardown.
        assert sp._LIVE[key] == owner._seed_owner


class TestTeardownTouchesNothingItDoesNotHold:
    """The hand-back is gated on the registrations this client actually holds."""

    def test_a_client_without_a_registration_writes_no_sidecar(self, tmp_path):
        """Every kiro-backend client, and a claude client that never seeded."""
        for client in (AcpClient(work_dir=tmp_path), _client(tmp_path)):
            _tear_down_without_shutdown(client)

        assert not sp._sidecar_path().exists()
        assert not (tmp_path / ".claude").exists()

    def test_a_sibling_teardown_does_not_release_the_owners_claim(self, tmp_path):
        owner = _client(tmp_path, model=_SERVED[0])
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        key = os.fspath(path)

        # A sibling that was refused the seed holds nothing on this path.
        refused = _client(tmp_path)
        refused._write_claude_local_settings()
        assert refused._claude_settings_authored is False
        assert refused._claude_settings_shared is False

        _torn_down_with_a_recorded_process(refused)

        assert sp._LIVE[key] == owner._seed_owner
        assert owner._seed_owner in sp._read_disk_seeds()[key]["holders"][sp._HOLDER_OWNERS]


class _RuntimeStandIn:
    """A tenancy target for the ownership table: a pid and a liveness answer.

    A real object rather than a ``MagicMock``: the table rejects a mock's pid (it
    coerces to 1 through ``__index__``) and would read ``is_alive`` as a truthy mock.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid

    def is_alive(self) -> bool:
        return True


@pytest.fixture
def ownership_table(_floor_monkeypatch: pytest.MonkeyPatch):
    """A clean runtime-ownership table, restored after the test."""
    ro._reset_for_tests()
    yield ro
    ro._reset_for_tests()


class TestClaimFollowsTheGate:
    """The hand-back follows the kill gate's verdict, not the teardown's dispatch.

    A seed claim is released when the teardown is authorized, when there is no pid,
    or when the recorded root is known to be gone or recycled. It is KEPT while a
    lease or a tenancy still holds the runtime and while the root's identity cannot
    be read: in both the process that read the seed may still be running, and a
    successor adopting the seed would re-write ``permissions.defaultMode`` under it.
    """

    def test_a_replacement_spawn_cannot_reseed_under_a_live_tenancy(
        self, tmp_path, ownership_table
    ):
        """The race: a successor seeds while the gate refuses the old runtime's kill.

        A party that does not own the runtime is mid-flight on it, so the kill is
        refused and the runtime stays alive. The successor that starts on the same
        ``work_dir`` beside that teardown must find the seed held: it cannot adopt
        it, cannot put its own ``permissions.defaultMode`` on disk, and runs with
        its array withheld -- the live-sibling rule -- instead of re-seeding the
        permission file under a running process.
        """
        owner = _client(tmp_path, permission_mode="bypassPermissions")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        key = os.fspath(path)
        before = path.read_text(encoding="utf-8")
        owner._pid = _LIVE_PID
        owner._start_time = _LIVE_START_ID
        handle = ownership_table.RUNTIME_TENANCY.claim(
            _RuntimeStandIn(_LIVE_PID), holder="test:shared-turn"
        )
        assert handle is not None

        _sync_kill_provider(_stub_provider(owner))

        # The gate refused: the claim and its persisted holder both stand.
        assert sp._LIVE[key] == owner._seed_owner
        assert owner._seed_owner in sp._read_disk_seeds()[key]["holders"][sp._HOLDER_OWNERS]
        assert owner._claude_settings_authored is True

        successor = _client(tmp_path, permission_mode="default")
        successor._write_claude_local_settings()

        assert successor._claude_settings_authored is False
        assert successor._claude_settings_shared is False
        assert sp._LIVE[key] == owner._seed_owner
        assert path.read_text(encoding="utf-8") == before
        assert '"bypassPermissions"' in path.read_text(encoding="utf-8")
        ownership_table.release_runtime_tenancy(handle)

    def test_a_held_lease_keeps_the_claim(self, tmp_path, ownership_table, monkeypatch):
        """The other refusal ground: a lease that outlived its release site."""
        owner = _client(tmp_path)
        owner._write_claude_local_settings()
        key = os.fspath(_settings(tmp_path))
        owner._pid = _LIVE_PID
        owner._start_time = _LIVE_START_ID
        # The gate reads the lease count through its own module's name.
        monkeypatch.setattr(ro, "outstanding_leases", lambda target: 1)

        _sync_kill_provider(_stub_provider(owner))

        assert sp._LIVE[key] == owner._seed_owner
        assert owner._seed_owner in sp._read_disk_seeds()[key]["holders"][sp._HOLDER_OWNERS]

    def test_an_unreadable_identity_keeps_the_claim(self, tmp_path, monkeypatch):
        """The pid exists but its start identity cannot be read: our runtime or not?

        Deny-by-default for the kill, and the same answer for the seed: a claim
        released here would let a successor re-seed under a process that may be
        ours and still running.
        """
        owner = _client(tmp_path)
        owner._write_claude_local_settings()
        key = os.fspath(_settings(tmp_path))
        owner._pid = _LIVE_PID
        owner._start_time = _LIVE_START_ID
        monkeypatch.setattr(sp.platform_compat, "pid_exists", lambda pid: True)
        monkeypatch.setattr(sp.platform_compat, "get_process_start_id", lambda pid: None)

        _sync_kill_provider(_stub_provider(owner))

        assert sp._LIVE[key] == owner._seed_owner
        assert owner._seed_owner in sp._read_disk_seeds()[key]["holders"][sp._HOLDER_OWNERS]

    def test_a_recycled_pid_releases_the_claim(self, tmp_path, monkeypatch):
        """The pid now names a stranger: the runtime that read the seed is gone."""
        owner = _client(tmp_path)
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        key = os.fspath(path)
        owner._pid = _LIVE_PID
        owner._start_time = _LIVE_START_ID
        monkeypatch.setattr(sp.platform_compat, "pid_exists", lambda pid: True)
        monkeypatch.setattr(
            sp.platform_compat, "get_process_start_id", lambda pid: "a-strangers-incarnation"
        )
        # The root is unverified, so the teardown signals nothing and sweeps the
        # recorded descendants only (the stub records none); the grace it would
        # wait out for them is pinned to zero, since this test asserts on the claim.
        monkeypatch.setattr(session_pid, "_PROVIDER_TERM_GRACE_SECONDS", 0.0)

        _sync_kill_provider(_stub_provider(owner))

        assert key not in sp._LIVE
        assert owner._seed_owner not in sp._read_disk_seeds()[key]["holders"][sp._HOLDER_OWNERS]
        assert sp.held_by_another(path, "a-later-session") is False
