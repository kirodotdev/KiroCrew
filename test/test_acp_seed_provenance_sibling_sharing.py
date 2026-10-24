"""Which sibling may share a ``settings.local.json`` seed, and the create race for one.

A sibling of the same agent in the same ``work_dir`` whose payload matches the durable
record and the bytes on disk shares the seed instead of running without Kiro Crew's MCP
tools. These pin who may share and the share-validation lease that follows, and the
create race: a loser shares only on a durable record of the winner's bytes, a vacant path
is re-created only with the recorded bytes, and a refused record withdraws its file.
Shared fixtures and helpers live in ``acp_seed_provenance_helpers.py``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

import pytest
from acp_seed_provenance_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_seed_provenance_helpers import isolated_records  # noqa: F401
from acp_seed_provenance_helpers import (
    _OWNER,
    _SERVED,
    _break_the_lock_when,
    _client,
    _fp,
    _pin_holder_identities,
    _seed,
    _settings,
    _teardown,
    _the_owning_process_died,
    _write_durable_holder,
)

from kiro_crew import model_registry as mr
from kiro_crew.acp import client as acp_client
from kiro_crew.acp import seed_provenance as sp
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.testing.clock import ManualClock


class _HalfSecondPolls(ManualClock):
    """The create-race loser's poll clock: each poll sleep costs half a mocked second.

    Four polls reach the poll's 2 s deadline. Installed on ``acp_client`` alone
    (rule D2), so the seed-provenance sidecar lock's backoff -- which sleeps on
    the stdlib clock when another holder has the lock -- stays real time and is
    never counted as a poll.
    """

    def sleep(self, seconds: float) -> None:
        super().sleep(seconds)
        self.advance(0.5 - seconds)


class TestSharedPermissionSurfaceEligibility:
    """A sibling seed carrying this session's permissions is shared, never rewritten.

    The one relaxation of the live-holder rule: two sessions of the same agent
    in the same ``work_dir`` render the same permissions, and without sharing only
    the FIRST one gets Crew's MCP tools -- the second
    lost the live slot, fell to the leave-it-alone branch, and ran with the
    whole ``mcpServers`` array withheld. The hazard the live-holder rule guards
    against (re-seeding with a different ``permissions.defaultMode``, unlinking
    the owner's file) only exists when the permissions DIFFER, so they -- on a
    seed whose bytes match both the durable record and the file on disk -- are
    the boundary of what may be shared. The model half may differ only where the
    session still runs its own model.

    ``TestSharedPermissionSurfaceCreateRaces`` below pins the create race and a record
    refused on the create or re-seed path, and ``TestSharedPermissionSurfaceTeardownAndReseed``
    in ``test_acp_seed_provenance_sharer_teardown.py`` pins teardown, the settle hand-back, a
    re-seed under a sharer and the no-clobber restore.
    """

    def test_a_sibling_with_an_identical_payload_shares_the_surface(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        before = path.read_text(encoding="utf-8")

        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()

        # The surface governs the sibling -- the MCP array precondition holds --
        # but nothing was written and nothing about ownership moved.
        assert sibling._claude_settings_shared is True
        assert sibling._permission_surface_governed is True
        assert sibling._claude_settings_authored is False
        assert sibling._claude_settings_written is None
        assert path.read_text(encoding="utf-8") == before
        assert sp._LIVE[os.fspath(path)] == owner._seed_owner
        assert owner._claude_settings_authored is True

    def test_a_model_pinned_sibling_shares_a_modelless_seed(self, tmp_path, monkeypatch):
        # Another settings source defines availableModels, so the warm cache is written.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path)
        owner._write_claude_local_settings()
        assert "model" not in _seed(tmp_path)

        sibling = _client(tmp_path, model=_SERVED[0])
        assert "model" in json.loads(sibling._render_claude_settings_payload())
        sibling._write_claude_local_settings()

        assert sibling._claude_settings_shared is True
        assert sibling._permission_surface_governed is True
        stub = {"name": "kirocrew-core", "command": "/stub", "args": [], "env": [], "type": "stdio"}
        projection = acp_client.mirror_for(ACP_BACKEND_CLAUDE).session_projection(
            sibling._agent,
            stub_server_names=("kirocrew-core",),
            stub_elements=[stub],
            permission_surface_owned=sibling._permission_surface_governed,
        )
        assert projection.params["mcpServers"] != []

    def test_a_sibling_pinned_to_another_model_shares_the_seed(self, tmp_path, monkeypatch):
        # Another settings source defines availableModels, so the warm cache is written.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, model=_SERVED[0])
        owner._write_claude_local_settings()
        assert _seed(tmp_path)["model"] == _SERVED[0]

        sibling = _client(tmp_path, model=_SERVED[1])
        assert json.loads(sibling._render_claude_settings_payload())["model"] == _SERVED[1]
        sibling._write_claude_local_settings()

        assert sibling._claude_settings_shared is True
        assert sibling._permission_surface_governed is True

    def test_siblings_without_an_allowlist_share_across_different_models(
        self, tmp_path, monkeypatch
    ):
        # No other settings source defines availableModels, so the seed names a model
        # and no list. Each session still runs its own pinned model, and the model key
        # alone does not narrow the alias, so the second session may share the seed.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: False,
        )
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, model=_SERVED[0])
        owner._write_claude_local_settings()
        assert "availableModels" not in _seed(tmp_path)

        sibling = _client(tmp_path, model=_SERVED[1])
        assert json.loads(sibling._render_claude_settings_payload())["model"] == _SERVED[1]
        sibling._write_claude_local_settings()

        assert sibling._claude_settings_shared is True

    def test_a_sibling_offered_another_model_list_shares_the_seed(self, tmp_path, monkeypatch):
        # Another settings source defines availableModels, so the warm cache is written.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, model=_SERVED[0])
        owner._write_claude_local_settings()
        seeded = _seed(tmp_path)

        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(reversed(_SERVED))})
        sibling = _client(tmp_path, model=_SERVED[0])
        rendered = json.loads(sibling._render_claude_settings_payload())
        assert rendered["availableModels"] != seeded["availableModels"]
        sibling._write_claude_local_settings()

        assert sibling._claude_settings_shared is True
        assert sibling._permission_surface_governed is True
        assert _seed(tmp_path) == seeded

    def test_an_auto_sibling_offered_another_model_list_shares_the_seed(
        self, tmp_path, monkeypatch
    ):
        # Another settings source defines availableModels, so the warm cache is written.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path)
        owner._write_claude_local_settings()
        seeded = _seed(tmp_path)

        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(reversed(_SERVED))})
        sibling = _client(tmp_path)
        rendered = json.loads(sibling._render_claude_settings_payload())
        assert "model" not in rendered
        assert rendered["availableModels"] != seeded["availableModels"]
        sibling._write_claude_local_settings()

        assert sibling._claude_settings_shared is True
        assert sibling._permission_surface_governed is True
        assert _seed(tmp_path) == seeded

    def test_a_sibling_whose_model_the_seed_does_not_list_is_refused(self, tmp_path, monkeypatch):
        # Another settings source defines availableModels, so the warm cache is written.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        # The adapter takes a pushed model only from the seed's availableModels, so
        # sharing would leave the sibling on the owner's model instead of its own.
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": _SERVED[:1]})
        owner = _client(tmp_path, model=_SERVED[0])
        owner._write_claude_local_settings()
        before = _settings(tmp_path).read_text(encoding="utf-8")

        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        sibling = _client(tmp_path, model=_SERVED[1])
        sibling._write_claude_local_settings()

        assert sibling._claude_settings_shared is False
        assert _settings(tmp_path).read_text(encoding="utf-8") == before

    def test_a_declined_sibling_seed_is_named_as_crews_not_the_projects(
        self, tmp_path, monkeypatch, caplog
    ):
        # The cause line must not send the user to rename "the project's" file:
        # it is an open Crew session's seed, which that session writes back.
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()

        sibling = _client(tmp_path, permission_mode="acceptEdits")
        with caplog.at_level(logging.INFO, logger=acp_client.logger.name):
            sibling._write_claude_local_settings()

        assert sibling._permission_surface_governed is False
        path = str(_settings(tmp_path))
        lines = [r.getMessage() for r in caplog.records if path in r.getMessage()]
        assert lines
        assert all("belongs to the project" not in line for line in lines)
        assert any("another open Crew session's settings seed" in line for line in lines)

    def test_a_user_authored_file_is_still_named_as_the_projects(
        self, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        path = _settings(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}), encoding="utf-8")

        client = _client(tmp_path, permission_mode="default")
        with caplog.at_level(logging.INFO, logger=acp_client.logger.name):
            client._write_claude_local_settings()

        lines = [r.getMessage() for r in caplog.records if str(path) in r.getMessage()]
        assert any("belongs to the project" in line for line in lines)
        assert all("another open Crew session" not in line for line in lines)

    def test_an_auto_sibling_is_refused_a_pinned_seed(self, tmp_path, monkeypatch):
        # Another settings source defines availableModels, so the warm cache is written.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        # An unpinned session pushes no model, so it would start on the owner's pin.
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, model=_SERVED[0])
        owner._write_claude_local_settings()

        sibling = _client(tmp_path)
        sibling._write_claude_local_settings()

        assert sibling._claude_settings_shared is False

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
    def test_a_fifo_at_the_seed_path_is_refused_without_blocking(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, model=_SERVED[0])
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        path.unlink()
        os.mkfifo(path)

        sibling = _client(tmp_path, model=_SERVED[1])
        worker = threading.Thread(target=sibling._write_claude_local_settings, daemon=True)
        worker.start()
        worker.join(10)
        if worker.is_alive():
            # A blocking open is waiting for a writer; give it one so the thread ends.
            os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
            worker.join(10)
            pytest.fail("the share read blocked on a FIFO at the seed path")
        assert sibling._claude_settings_shared is False

    def test_failed_revalidation_clears_governed_but_keeps_lease(self, tmp_path, monkeypatch):
        client = _client(tmp_path)
        path = _settings(tmp_path)
        client._claude_settings_shared = True
        client._permission_surface_share_validated = True
        monkeypatch.setattr(sp, "share", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(client, "_settings_path_holds", lambda *_args: False)

        def _unexpected_unshare(*_args, **_kwargs):
            raise AssertionError("a failed re-validation must retain the existing lease")

        monkeypatch.setattr(sp, "unshare", _unexpected_unshare)
        monkeypatch.setattr(sp, "unshare_local", _unexpected_unshare)

        assert client._share_settings_seed_if_identical(path, "payload") is False
        assert client._permission_surface_share_validated is False
        assert client._permission_surface_governed is False
        assert client._claude_settings_shared is True

    def test_share_refusal_clears_governed_flag(self, tmp_path, monkeypatch):
        client = _client(tmp_path)
        path = _settings(tmp_path)
        client._claude_settings_shared = True
        client._permission_surface_share_validated = True
        monkeypatch.setattr(sp, "share", lambda *_args, **_kwargs: False)

        assert client._share_settings_seed_if_identical(path, "changed-payload") is False
        assert client._permission_surface_share_validated is False
        assert client._permission_surface_governed is False
        assert client._claude_settings_shared is True

    def test_successful_revalidation_sets_both_flags(self, tmp_path, monkeypatch):
        client = _client(tmp_path)
        path = _settings(tmp_path)
        assert client._permission_surface_share_validated is False
        monkeypatch.setattr(sp, "share", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(client, "_settings_path_holds", lambda *_args: True)

        assert client._share_settings_seed_if_identical(path, "payload") is True
        assert client._claude_settings_shared is True
        assert client._permission_surface_share_validated is True
        assert client._permission_surface_governed is True

    def test_governed_flag_re_earned_when_bytes_return(self, tmp_path, monkeypatch):
        client = _client(tmp_path)
        path = _settings(tmp_path)
        client._claude_settings_shared = True
        client._permission_surface_share_validated = True
        holds = iter((False, True))
        monkeypatch.setattr(sp, "share", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(client, "_settings_path_holds", lambda *_args: next(holds))

        assert client._share_settings_seed_if_identical(path, "payload") is False
        assert client._claude_settings_shared is True
        assert client._permission_surface_share_validated is False
        assert client._share_settings_seed_if_identical(path, "payload") is True
        assert client._claude_settings_shared is True
        assert client._permission_surface_share_validated is True
        assert client._permission_surface_governed is True

    def test_share_validation_flag_starts_and_resets_false(self, tmp_path):
        client = _client(tmp_path)
        assert client._permission_surface_share_validated is False

        client._permission_surface_share_validated = True
        client._reset_state()

        assert client._permission_surface_share_validated is False

    def test_reseed_invalidates_session_mcp_cache(self, tmp_path):
        client = _client(tmp_path)
        client._write_claude_local_settings()
        client._session_mcp_cache = [{"name": "stale"}]
        client._session_mcp_snapshot = acp_client.DerivedSpecSnapshot("old", "old")
        _settings(tmp_path).write_text("{}", encoding="utf-8")

        client._write_claude_local_settings()

        assert client._claude_settings_authored is False
        assert client._permission_surface_governed is False
        assert client._session_mcp_cache is None
        assert client._session_mcp_snapshot is None

    def test_a_differing_payload_is_still_refused(self, tmp_path, monkeypatch):
        # Different permission modes render different bytes: the exact hazard the
        # live-holder rule exists for, and the relaxation must not reach it.
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="bypassPermissions")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        before = path.read_text(encoding="utf-8")

        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()

        assert sibling._claude_settings_shared is False
        assert sibling._permission_surface_governed is False
        assert sibling._claude_settings_authored is False
        assert path.read_text(encoding="utf-8") == before
        assert sp._LIVE[os.fspath(path)] == owner._seed_owner

    def test_a_replaced_file_is_not_shared_on_the_records_word_alone(self, tmp_path, monkeypatch):
        # The record can describe bytes a user has since replaced. Equality must
        # hold on the DISK too, or the sharer would treat a foreign permission
        # surface as governed.
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        replaced = json.dumps({"permissions": {"allow": ["Bash(ls)"]}}, indent=2)
        path.write_text(replaced, encoding="utf-8")

        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()

        assert sibling._claude_settings_shared is False
        assert sibling._permission_surface_governed is False
        assert path.read_text(encoding="utf-8") == replaced

    def test_a_failed_validation_with_a_refused_unshare_leaves_no_phantom_lease(
        self, tmp_path, monkeypatch
    ):
        """A client that never became a sharer leaves no in-memory pin.

        The share persists, the disk validation then fails (a user-replaced
        file), and the withdrawal's own persist fails on a disk error. The
        retain-on-refusal rule exists for REAL sharers whose delivered tools
        depend on the file; kept here it would pin the owner's seed behind a
        phantom lease for the process lifetime, because teardown skips
        withdrawal when the shared flag is off.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)

        real_persist = sp._persist

        def refuse_holder_drops(**kwargs):
            if kwargs.get("drop_holder") is not None:
                return False
            return real_persist(**kwargs)

        monkeypatch.setattr(sp, "_persist", refuse_holder_drops)
        monkeypatch.setattr(AcpClient, "_settings_path_holds", staticmethod(lambda _p, _e: False))
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()

        assert sibling._claude_settings_shared is False
        # No in-memory registration pins the owner's seed for this process's
        # lifetime; the durable holder entry clears through liveness reclaim.
        assert sibling._seed_owner not in sp._SHARERS.get(os.fspath(path), set())

    def test_a_failed_revalidation_keeps_an_existing_lease(self, tmp_path, monkeypatch):
        """A sharer's later failed validation must not drop its earned lease.

        The lease is what pins the file the sharer already delivered its MCP
        array against. A re-validation whose payload moved (a model refresh)
        can fail; dropping the registration then would read as no-sharers to
        the owner's teardown, which would delete the governed seed beneath a
        client whose surface still reports governed.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        before = path.read_text(encoding="utf-8")
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        assert sibling._claude_settings_shared is True

        # The sibling's payload moves (it pins a model now) and it re-validates.
        sibling._model = "global.anthropic.claude-opus-4-8[1m]"
        sibling._write_claude_local_settings()

        assert sibling._claude_settings_shared is True
        assert sp.has_sharers(path) is True
        # And the provenance-level rule directly: a mismatched re-share by an
        # already-registered owner keeps the registration.
        assert sp.share(path, before + "x", sibling._seed_owner) is False
        assert sp.has_sharers(path) is True
        # The pinned consequence: the owner's teardown still keeps the file.
        _teardown(owner)
        assert path.read_text(encoding="utf-8") == before

    def test_a_second_process_shares_from_the_durable_sidecar(self, tmp_path, monkeypatch):
        """A sibling process need not have observed the owner's in-memory publish."""
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        sp._RECORDS.clear()
        sp._LIVE.clear()
        sp._SHARERS.clear()

        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()

        assert sibling._claude_settings_shared is True
        assert sibling._claude_settings_authored is False

    def test_share_ignores_the_live_holder_but_not_the_bytes(self, tmp_path):
        path = tmp_path / "settings.local.json"
        payload = '{"permissions": {"defaultMode": "default"}}\n'
        assert sp.share(path, payload, "reader") is False  # no record at all
        sp.record(path, payload, _OWNER)
        # A live holder hides the record from ``recorded`` -- that is the refusal
        # under relaxation -- but the share check answers on the bytes alone.
        assert sp.recorded(path, "someone-else") is None
        assert sp.share(path, payload, "reader") is True
        sp.unshare(path, "reader")
        assert sp.share(path, payload + " ", "reader") is False
        assert sp.has_sharers(path) is False

    def test_share_registers_before_it_validates(self, tmp_path):
        """A failed validation leaves no registration behind."""
        path = tmp_path / "settings.local.json"
        payload = '{"permissions": {"defaultMode": "default"}}\n'
        sp.record(path, payload, _OWNER)
        assert sp.share(path, payload + "x", "reader") is False
        assert sp.has_sharers(path) is False
        assert sp.share(path, payload, "reader") is True
        assert sp.has_sharers(path) is True
        sp.unshare(path, "reader")
        assert sp.has_sharers(path) is False


class TestSharedPermissionSurfaceCreateRaces:
    """The create race for a vacant seed pathname, its re-creation, and a refused record.

    A loser shares only on a durable record of the winner's bytes; a vanished seed is
    re-created only with the recorded bytes, never under a live owner or a registered sharer;
    and a record refused on the create or re-seed path withdraws the file or restores the
    prior bytes unless a validated sharer reads it.
    """

    def test_refused_record_on_o_excl_path_preserves_foreign_bytes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        foreign = _pin_holder_identities(monkeypatch, foreign_live=True)
        client = _client(tmp_path, permission_mode="default")
        path = _settings(tmp_path)
        foreign_payload = '{"permissions": {"defaultMode": "acceptEdits"}}\n'
        real_record = sp.record
        raced: list[bool] = []

        def _foreign_owner_lands_before_record(
            candidate: Path | str, payload: str, owner: str
        ) -> bool:
            assert os.fspath(candidate) == os.fspath(path)
            assert path.read_text(encoding="utf-8") == payload
            raced.append(True)
            _write_durable_holder(
                path,
                foreign_payload,
                kind=sp._HOLDER_OWNERS,
                owner="foreign-owner",
                identity=foreign,
            )
            path.write_text(foreign_payload, encoding="utf-8")
            return real_record(candidate, payload, owner)

        monkeypatch.setattr(sp, "record", _foreign_owner_lands_before_record)
        client._write_claude_local_settings()

        assert raced == [True]
        assert path.read_text(encoding="utf-8") == foreign_payload
        assert client._claude_settings_authored is False
        owners = sp._read_disk_seeds()[os.fspath(path)]["holders"][sp._HOLDER_OWNERS]
        assert set(owners) == {"foreign-owner"}

    def test_refused_record_reseed_path_restores_prior_bytes_without_clobber(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        first = _client(tmp_path, permission_mode="default")
        first._write_claude_local_settings()
        _the_owning_process_died()

        path = _settings(tmp_path)
        replacement = '{"permissions": {"allow": ["Bash(ls)"]}}\n'
        refused: list[bool] = []

        def _replacement_lands_before_refusal(
            candidate: Path | str, payload: str, _owner: str
        ) -> bool:
            assert os.fspath(candidate) == os.fspath(path)
            assert path.read_text(encoding="utf-8") == payload
            refused.append(True)
            path.write_text(replacement, encoding="utf-8")
            return False

        monkeypatch.setattr(sp, "record", _replacement_lands_before_refusal)
        successor = _client(tmp_path, permission_mode="bypassPermissions")
        successor._write_claude_local_settings()

        assert refused == [True]
        assert path.read_text(encoding="utf-8") == replacement
        assert successor._claude_settings_authored is False

    def test_the_create_race_loser_shares_when_the_winner_recorded(self, tmp_path, monkeypatch):
        """The O_EXCL loser gets the same byte-equality relaxation.

        Both siblings pass the not-exists check; one wins the create. When the
        winner's grant is already durable and the payloads are byte-identical,
        the loser shares the surface instead of running toolless; a loser
        racing ahead of the winner's durable record simply declines.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        loser = _client(tmp_path, permission_mode="default")
        path = _settings(tmp_path)
        real_open = os.open
        fired: list[int] = []

        def winner_lands_first(p, flags, *args, **kwargs):
            if not fired and os.fspath(p) == os.fspath(path) and flags & os.O_EXCL:
                fired.append(1)
                owner._write_claude_local_settings()
            return real_open(p, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", winner_lands_first)
        loser._write_claude_local_settings()

        assert fired, "the race window must have been exercised"
        assert loser._claude_settings_shared is True
        assert loser._claude_settings_authored is False
        assert owner._claude_settings_authored is True

    def test_an_unrelated_persist_keeps_a_sharer_pinned_record(self, tmp_path, monkeypatch):
        """A missing-file record survives persists while its sharer's lease is live.

        The shared seed can vanish out-of-band; an UNRELATED session recording a
        different path then runs _persist, whose dead-file prune would drop the
        shared path's record — and with it the digest the sharer's byte-identical
        repair validates against, leaving the sharer governed with no path back
        to a restorable seed. A live lease pins the record.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        assert sibling._claude_settings_shared is True

        # The seed vanishes out-of-band; an unrelated work dir's session persists.
        path.unlink()
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        unrelated = _client(other_dir, permission_mode="default")
        unrelated._write_claude_local_settings()

        # The sharer-pinned record survived the unrelated persist...
        assert sp.recorded_durable(path) is not None
        # ...so once the owner is gone, the sharer's byte-identical repair can
        # still author the seed back (a live owner refuses recreation; its own
        # repair path covers that case).
        _teardown(owner)
        sibling._claude_settings_shared = False
        sibling._write_claude_local_settings()
        assert sibling._claude_settings_authored is True
        assert path.exists()

    def test_a_promoted_sharer_drops_its_own_reader_lease(self, tmp_path, monkeypatch):
        """A sharer that becomes the author must not stay its own sharer.

        The shared file can vanish out-of-band; the sharer's next re-seed then
        takes the O_EXCL create path and AUTHORS a replacement. An author still
        registered as its own reader would pin its own teardown -- the settle
        transaction reads has_sharers() and would leave the authored
        permissions.defaultMode behind for every later session.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        assert sibling._claude_settings_shared is True

        # The seed vanishes out-of-band (owner teardown + external cleanup), and
        # the sharer's post-capture re-seed re-creates it as the AUTHOR. The
        # durable record survives -- a lease implies the record it validated --
        # and the byte-identical payload is what authorship serialization
        # admits past a registered sharer.
        _teardown(owner)  # keeps the file for the live sharer...
        sp._SHARERS.clear()
        sp._SHARERS[os.fspath(path)] = {sibling._seed_owner}  # only the sharer remains
        path.unlink()
        persist_calls: list[dict] = []
        real_persist = sp._persist

        def _count_persist(*args, **kwargs):
            persist_calls.append(dict(kwargs))
            return real_persist(*args, **kwargs)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(sp, "_persist", _count_persist)
            sibling._write_claude_local_settings()

        assert len(persist_calls) == 1
        assert persist_calls[0]["drop_holder"] == (
            os.fspath(path),
            sp._HOLDER_SHARERS,
            sibling._seed_owner,
        )
        holders = sp._read_disk_seeds()[os.fspath(path)]["holders"]
        assert sibling._seed_owner in holders["owners"]
        assert sibling._seed_owner not in holders["sharers"]
        assert sibling._seed_owner not in sp._SHARERS[os.fspath(path)]
        assert sibling._claude_settings_authored is True
        # The promotion withdrew the reader lease...
        assert sibling._claude_settings_shared is False
        assert sp.has_sharers(path) is False
        # ...so the author's own teardown removes its seed as any author's does.
        _teardown(sibling)
        assert not path.exists()

    def test_a_mismatched_loser_declines_once_at_the_deadline(self, tmp_path, monkeypatch):
        """A settled, differing record never ends the poll early; the deadline does.

        The poll exits early only on a record naming the LOSER's own bytes -- any
        other durable entry could be a stale one a killed session left behind, so
        it proves nothing about the winner. A loser whose payload mismatches
        therefore runs to the bounded 2 s deadline and declines exactly once;
        the clock is mocked so that deadline is a few iterations, not real time.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        loser = _client(tmp_path, permission_mode="bypassPermissions")
        path = _settings(tmp_path)
        real_open = os.open
        fired: list[int] = []
        declines: list[Path] = []
        clock = _HalfSecondPolls()

        def winner_wrote_and_persisted(p, flags, *args, **kwargs):
            if not fired and os.fspath(p) == os.fspath(path) and flags & os.O_EXCL:
                fired.append(1)
                # The winner's file AND record are both settled before the
                # loser's open -- the mismatch can never converge.
                owner._write_claude_local_settings()
                clock.install(monkeypatch, acp_client)
            return real_open(p, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", winner_wrote_and_persisted)
        monkeypatch.setattr(AcpClient, "_log_declined_share", lambda self, p: declines.append(p))
        loser._write_claude_local_settings()

        assert fired, "the race window must have been exercised"
        assert loser._claude_settings_shared is False
        assert loser._claude_settings_authored is False
        # Four 50 ms polls at half a mocked second each reach the deadline; the
        # fifth probe breaks on it, and the loser declines once, not per poll.
        assert clock.sleeps == (0.05,) * 4
        assert declines == [path]

    def test_a_cross_process_winners_differing_record_never_ends_the_poll_early(
        self, tmp_path, monkeypatch
    ):
        """A differing record from ANOTHER process is not the winner's word for THIS loser.

        The create-race winner lives in a different process (a gateway and a
        concurrent CLI on the same work dir), so its record exists only in the
        durable sidecar -- this process's in-memory cache never sees it. The
        poll's exit probe reads that sidecar, but a durable entry ends the poll
        early only when it names the loser's own bytes: the winner's differing
        record, like a stale one, leaves the loser to its bounded deadline.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        loser = _client(tmp_path, permission_mode="bypassPermissions")
        path = _settings(tmp_path)
        real_open = os.open
        fired: list[int] = []
        declines: list[Path] = []
        clock = _HalfSecondPolls()

        def cross_process_winner_landed(p, flags, *args, **kwargs):
            if not fired and os.fspath(p) == os.fspath(path) and flags & os.O_EXCL:
                fired.append(1)
                # The winner writes file and record -- then every trace of it
                # is dropped from THIS process's memory, because the winner
                # lives in a different one; only the durable sidecar knows.
                owner._write_claude_local_settings()
                sp._RECORDS.clear()
                sp._LIVE.clear()
                sp._SHARERS.clear()
                clock.install(monkeypatch, acp_client)
            return real_open(p, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", cross_process_winner_landed)
        monkeypatch.setattr(AcpClient, "_log_declined_share", lambda self, p: declines.append(p))
        loser._write_claude_local_settings()

        assert fired, "the race window must have been exercised"
        assert sp.recorded_durable(path) is not None, "the sidecar still carries the winner"
        assert loser._claude_settings_shared is False
        assert loser._claude_settings_authored is False
        # The differing record was on disk from the first probe and ended nothing:
        # the loser ran to its deadline and declined once.
        assert clock.sleeps == (0.05,) * 4
        assert declines == [path]

    def test_a_recreate_is_refused_while_the_owner_is_still_live(self, tmp_path, monkeypatch):
        """A sibling must not recreate a vanished seed under a LIVE owner.

        record() displaces the live slot unconditionally, so a sibling that
        authored a replacement would become the holder -- and its teardown
        would remove a file the original owner still governs. The create path
        refuses while a different session's live client holds the path;
        promotion stays possible once the owner is gone
        (test_a_promoted_sharer_drops_its_own_reader_lease).
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        assert sibling._claude_settings_shared is True

        # The seed vanishes out-of-band while the OWNER is still live.
        path.unlink()
        sibling._claude_settings_shared = False
        sibling._write_claude_local_settings()

        assert sibling._claude_settings_authored is False
        assert not path.exists()
        # The owner's claim is untouched.
        assert sp.held_by_another(path, sibling._seed_owner) is True

    def test_create_decline_under_live_owner_clears_and_invalidates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        assert sibling._permission_surface_share_validated is True

        path.unlink()
        sibling._session_mcp_cache = [{"name": "stale"}]
        sibling._session_mcp_snapshot = acp_client.DerivedSpecSnapshot("old", "old")
        sibling._write_claude_local_settings()

        assert sibling._permission_surface_share_validated is False
        assert sibling._session_mcp_cache is None
        assert sibling._session_mcp_snapshot is None
        assert sp.held_by_another(path, sibling._seed_owner) is True

    def test_create_decline_recorded_mismatch_clears_and_invalidates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        assert sibling._permission_surface_share_validated is True
        _teardown(owner)

        path.unlink()
        sibling._permission_mode = "bypassPermissions"
        sibling._session_mcp_cache = [{"name": "stale"}]
        sibling._session_mcp_snapshot = acp_client.DerivedSpecSnapshot("old", "old")
        sibling._write_claude_local_settings()

        assert sibling._permission_surface_share_validated is False
        assert sibling._session_mcp_cache is None
        assert sibling._session_mcp_snapshot is None
        assert not path.exists()

    def test_recreate_record_failure_withholds_tools_and_keeps_lease(self, tmp_path, monkeypatch):
        """A failed re-create keeps sharer-pinned bytes but drops governance.

        The durable record still names the byte-identical seed, and this client's
        reader lease protects it. A failed owner record therefore leaves the file
        for the live reader while clearing this client's validated-governance bit
        and cached projection.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        assert sibling._permission_surface_share_validated is True
        _teardown(owner)  # the owner is gone; its record and the sharer lease remain

        # The seed vanishes out-of-band, with the projection warmed while governed;
        # the sidecar is unwritable, so the byte-identical re-create cannot be recorded.
        expected = path.read_bytes()
        path.unlink()
        sibling._session_mcp_cache = [{"name": "stale-crew-tools"}]
        sibling._session_mcp_snapshot = acp_client.DerivedSpecSnapshot("old", "old")
        monkeypatch.setattr(sp, "_persist", lambda **_kwargs: False)
        sibling._write_claude_local_settings()

        assert path.read_bytes() == expected
        assert sibling._claude_settings_authored is False
        assert sibling._permission_surface_governed is False
        assert sibling._session_mcp_cache is None
        assert sibling._session_mcp_snapshot is None
        # The reader lease pins the durable record and its matching file.
        assert sibling._claude_settings_shared is True
        assert sibling._seed_owner in sp._SHARERS[os.fspath(path)]

    def test_create_record_failure_under_a_late_sharer_keeps_the_file(self, tmp_path, monkeypatch):
        """A sibling validating during create-to-record keeps its governed bytes."""
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        prior_owner = _client(tmp_path, permission_mode="default")
        prior_owner._write_claude_local_settings()
        path = _settings(tmp_path)
        payload = prior_owner._claude_settings_written
        assert payload is not None
        assert sp.release(path, prior_owner._seed_owner) is True
        path.unlink()

        creator = _client(tmp_path, permission_mode="default")
        sibling = _client(tmp_path, permission_mode="default")
        share_results: list[bool] = []

        def fail_after_share(record_path: Path, record_payload: str, _owner: str) -> bool:
            assert record_path == path
            assert record_payload == payload
            share_results.append(sibling._share_settings_seed_if_identical(path, payload))
            return False

        monkeypatch.setattr(sp, "record", fail_after_share)
        creator._write_claude_local_settings()

        assert share_results == [True]
        assert path.read_text(encoding="utf-8") == payload
        assert sp.has_sharers(path) is True
        assert sibling._permission_surface_share_validated is True
        assert sibling._permission_surface_governed is True
        assert creator._permission_surface_governed is False

    def test_create_record_failure_with_no_sharer_still_takes_back_the_seed(
        self, tmp_path, monkeypatch
    ):
        """An unrecordable create with no validated reader is still withdrawn."""
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        prior_owner = _client(tmp_path, permission_mode="default")
        prior_owner._write_claude_local_settings()
        path = _settings(tmp_path)
        assert sp.release(path, prior_owner._seed_owner) is True
        path.unlink()

        creator = _client(tmp_path, permission_mode="default")
        monkeypatch.setattr(sp, "record", lambda *_args, **_kwargs: False)
        creator._write_claude_local_settings()

        assert not path.exists()
        assert creator._permission_surface_governed is False

    def test_create_record_failure_sharer_barrier_is_serialized(self, tmp_path, monkeypatch):
        """Cleanup waits for an in-flight sharer validation to settle."""
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        prior_owner = _client(tmp_path, permission_mode="default")
        prior_owner._write_claude_local_settings()
        path = _settings(tmp_path)
        assert sp.release(path, prior_owner._seed_owner) is True
        path.unlink()

        creator = _client(tmp_path, permission_mode="default")
        record_called = threading.Event()
        done = threading.Event()
        errors: list[BaseException] = []

        def refuse_record(*_args, **_kwargs) -> bool:
            record_called.set()
            return False

        def create_seed() -> None:
            try:
                creator._write_claude_local_settings()
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                done.set()

        monkeypatch.setattr(sp, "record", refuse_record)
        with sp.SETTLE_LOCK:
            worker = threading.Thread(target=create_seed)
            worker.start()
            assert record_called.wait(5)
            # flake-ok: a 300 ms absence window while this test holds SETTLE_LOCK; moved verbatim
            assert not done.wait(0.3)
        assert done.wait(5)
        worker.join(5)

        assert errors == []
        assert not path.exists()
        assert creator._permission_surface_governed is False

    def test_create_record_failure_with_the_lock_broken_still_withdraws_the_seed(
        self, tmp_path, monkeypatch
    ):
        """A registry lock that dies mid-create never strands an unrecorded seed.

        The cross-process lock is one ``os.open`` on ``.settings_seeds.lock`` under
        Crew's data home, and it can stop opening between the pre-create barriers
        and the record: the mount flips read-only, or a ``sudo`` run leaves the
        lock file root-owned. ``record`` already reports that as ``False``; the
        cleanup that follows must not itself raise on the same lock, or the
        just-written ``permissions.defaultMode`` outlives every session with no
        record and no author -- the exact file this module exists to remove.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        path = _settings(tmp_path)
        _break_the_lock_when(monkeypatch, lambda: path.exists())
        client = _client(tmp_path, permission_mode="bypassPermissions")

        client._write_claude_local_settings()

        assert not path.exists()
        assert client._claude_settings_authored is False
        assert client._permission_surface_governed is False

    def test_create_record_failure_with_the_lock_broken_keeps_a_sibling_sharer_bytes(
        self, tmp_path, monkeypatch
    ):
        """Positive control: an unreadable registry never over-removes a live sharer.

        The in-process sharer registry answers BEFORE the cross-process lock, so a
        sibling that validated these bytes in this process keeps its file even
        when the durable registry cannot be consulted -- and no sibling in another
        process can newly register while the lock is down, because ``share``
        requires the same lock to persist its lease.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        prior_owner = _client(tmp_path, permission_mode="default")
        prior_owner._write_claude_local_settings()
        path = _settings(tmp_path)
        payload = prior_owner._claude_settings_written
        assert payload is not None
        assert sp.release(path, prior_owner._seed_owner) is True
        path.unlink()

        creator = _client(tmp_path, permission_mode="default")
        sibling = _client(tmp_path, permission_mode="default")
        share_results: list[bool] = []

        def share_then_break_the_lock(record_path: Path, record_payload: str, _owner: str) -> bool:
            assert record_path == path
            assert record_payload == payload
            share_results.append(sibling._share_settings_seed_if_identical(path, payload))
            _break_the_lock_when(monkeypatch, lambda: True)
            return False

        monkeypatch.setattr(sp, "record", share_then_break_the_lock)
        creator._write_claude_local_settings()

        assert share_results == [True]
        assert path.read_text(encoding="utf-8") == payload
        assert sibling._seed_owner in sp._SHARERS[os.fspath(path)]
        assert sibling._permission_surface_governed is True
        assert creator._permission_surface_governed is False

    def test_recreate_with_a_durable_record_publishes_and_stays_governed(
        self, tmp_path, monkeypatch
    ):
        """Positive control for the take-back above: same re-create, record lands.

        Pins that the withheld-on-failure assertions are not vacuous -- when the
        durable record does land, the byte-identical re-create publishes the
        file, promotes the sharer to author and leaves ``_permission_surface_governed``
        TRUE, the precondition the claude mirror admits the tool array on
        (test_an_unowned_permission_surface_withholds_the_stubs_too). Governance
        is therefore lost at the failure exit because of the failure, not because
        a re-create never governs.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        before = path.read_bytes()
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        assert sibling._permission_surface_share_validated is True
        _teardown(owner)

        path.unlink()
        sibling._write_claude_local_settings()

        assert path.read_bytes() == before
        assert sibling._claude_settings_authored is True
        assert sibling._permission_surface_governed is True
        assert sp.recorded_durable(path) == _fp(before.decode("utf-8"))
        assert sp._LIVE[os.fspath(path)] == sibling._seed_owner
        # Promotion: an author holds no sharer lease on its own seed.
        assert sibling._claude_settings_shared is False
        assert sp.has_sharers(path) is False

    def test_unusable_seed_path_clears_governed_flag(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        assert sibling._permission_surface_share_validated is True

        path.unlink()
        target = tmp_path / "foreign-settings.json"
        path.symlink_to(target)
        sibling._session_mcp_cache = [{"name": "stale"}]
        sibling._session_mcp_snapshot = acp_client.DerivedSpecSnapshot("old", "old")
        sibling._write_claude_local_settings()

        assert sibling._permission_surface_share_validated is False
        assert sibling._session_mcp_cache is None
        assert sibling._session_mcp_snapshot is None
        assert not target.exists()

    def test_durable_record_ignores_a_poisoned_local_cache(self, tmp_path):
        """The create-race poll waits until the winner's persist reaches disk."""
        path = _settings(tmp_path)
        key = sp._key(path)
        sp._RECORDS[key] = {"size": 1, "sha256": "poisoned"}

        assert sp._read_disk_seeds() == {}
        assert sp.recorded_durable(path) is None

    def test_a_differing_create_is_refused_while_a_sharer_is_registered(
        self, tmp_path, monkeypatch
    ):
        """Authorship of a vacant pathname is serialized against the registry.

        A registered reader validated the RECORDED bytes. A session whose
        payload differs must not take the vacant name -- its permission mode
        would sit under the sibling's governed surface. The byte-identical
        re-creation (a sharer repairing its own vanished seed) stays allowed
        and is pinned by test_a_promoted_sharer_drops_its_own_reader_lease.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        assert sibling._claude_settings_shared is True

        # The seed vanishes out-of-band while the sharer's lease is live.
        path.unlink()

        intruder = _client(tmp_path, permission_mode="bypassPermissions")
        intruder._write_claude_local_settings()

        assert intruder._claude_settings_authored is False
        assert intruder._claude_settings_shared is False
        assert not path.exists()

    def test_recreation_uses_the_durable_digest_when_the_local_cache_disagrees(
        self, tmp_path, monkeypatch
    ):
        """A cross-process sharer authorizes only the bytes recorded on disk."""
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        durable_payload = path.read_text(encoding="utf-8")
        sharer = _client(tmp_path, permission_mode="default")
        sharer._write_claude_local_settings()
        assert sharer._claude_settings_shared is True
        _teardown(owner)

        intruder = _client(tmp_path, permission_mode="bypassPermissions")
        poisoned_payload = intruder._render_claude_settings_payload()
        assert poisoned_payload != durable_payload
        key = sp._key(path)
        poisoned_size, poisoned_sha = _fp(poisoned_payload)
        sp._RECORDS[key] = {"size": poisoned_size, "sha256": poisoned_sha}
        sp._LIVE.clear()
        sp._SHARERS.clear()
        path.unlink()

        intruder._write_claude_local_settings()
        assert intruder._claude_settings_authored is False
        assert not path.exists()

        sharer._claude_settings_shared = False
        sharer._write_claude_local_settings()
        assert sharer._claude_settings_authored is True
        assert path.read_text(encoding="utf-8") == durable_payload
