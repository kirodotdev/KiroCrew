"""Coverage for the best-effort paths of the durable workflow-run store.

``WorkflowRunStore`` is a side-effect-only persistence layer whose contract is
"a storage failure must never break a run", so most of its code is failure
handling: an unusable runs dir, an unserializable payload, a failed atomic
replace, a corrupt run file. Those branches are what these tests pin, plus the
``workflows.dir`` config resolution and the injective ``run_id`` -> filename
mapping.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.workflows import store as store_mod
from kiro_crew.workflows.store import WorkflowRunStore, _redact, default_workflows_dir


class TestDefaultWorkflowsDir:
    def test_honors_configured_workflows_dir(self, monkeypatch, tmp_path) -> None:
        configured = tmp_path / "cfg-wf"

        class _Cfg:
            @staticmethod
            def load():
                return SimpleNamespace(workflows=SimpleNamespace(dir=str(configured)))

        monkeypatch.setattr(store_mod, "KiroCrewConfig", _Cfg)
        assert default_workflows_dir() == configured

    def test_expands_user_in_configured_dir(self, monkeypatch) -> None:
        class _Cfg:
            @staticmethod
            def load():
                return SimpleNamespace(workflows=SimpleNamespace(dir="~/wf-relative"))

        monkeypatch.setattr(store_mod, "KiroCrewConfig", _Cfg)
        assert default_workflows_dir() == Path("~/wf-relative").expanduser()

    def test_blank_config_value_falls_back_to_config_dir(self, monkeypatch, tmp_path) -> None:
        class _Cfg:
            @staticmethod
            def load():
                return SimpleNamespace(workflows=SimpleNamespace(dir=""))

        monkeypatch.setattr(store_mod, "KiroCrewConfig", _Cfg)
        monkeypatch.setattr(store_mod, "config_dir", lambda: tmp_path)
        assert default_workflows_dir() == tmp_path / "workflows"

    def test_config_load_failure_falls_back_to_config_dir(self, monkeypatch, tmp_path) -> None:
        class _Cfg:
            @staticmethod
            def load():
                raise RuntimeError("config layer unavailable")

        monkeypatch.setattr(store_mod, "KiroCrewConfig", _Cfg)
        monkeypatch.setattr(store_mod, "config_dir", lambda: tmp_path)
        assert default_workflows_dir() == tmp_path / "workflows"

    def test_missing_config_dependency_falls_back(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(store_mod, "KiroCrewConfig", None)
        monkeypatch.setattr(store_mod, "config_dir", lambda: tmp_path)
        assert default_workflows_dir() == tmp_path / "workflows"

    def test_store_without_base_dir_uses_default(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(store_mod, "default_workflows_dir", lambda: tmp_path / "resolved")
        assert WorkflowRunStore().runs_dir == tmp_path / "resolved" / "runs"


class TestPathFor:
    def test_wellformed_id_keeps_plain_filename(self, tmp_path) -> None:
        store = WorkflowRunStore(base_dir=tmp_path)
        assert store._path_for("wf_000001") == store.runs_dir / "wf_000001.json"

    def test_traversal_attempt_stays_inside_runs_dir(self, tmp_path) -> None:
        store = WorkflowRunStore(base_dir=tmp_path)
        path = store._path_for("../../etc/wf1")
        assert path.parent == store.runs_dir
        assert ".." not in path.name

    def test_sanitized_ids_stay_injective(self, tmp_path) -> None:
        store = WorkflowRunStore(base_dir=tmp_path)
        assert store._path_for("wf/1") != store._path_for("wf1")

    def test_fully_stripped_id_falls_back_to_digest(self, tmp_path) -> None:
        store = WorkflowRunStore(base_dir=tmp_path)
        name = store._path_for("///").name
        assert name.endswith(".json")
        assert not name.startswith("-")


class TestSave:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
    def test_round_trip_and_owner_only_mode(self, tmp_path) -> None:
        store = WorkflowRunStore(base_dir=tmp_path)
        store.save("wf_000009", {"run_id": "wf_000009", "status": "finished"})
        path = store.runs_dir / "wf_000009.json"
        assert json.loads(path.read_text(encoding="utf-8"))["status"] == "finished"
        assert oct(path.stat().st_mode)[-3:] == "600"
        assert list(store.runs_dir.glob("*.tmp")) == []

    def test_blank_run_id_is_a_noop(self, tmp_path) -> None:
        store = WorkflowRunStore(base_dir=tmp_path)
        store.save("", {"run_id": ""})
        assert not store.runs_dir.exists()

    def test_unusable_runs_dir_is_swallowed(self, tmp_path) -> None:
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        store = WorkflowRunStore(base_dir=blocker)
        store.save("wf_000010", {"run_id": "wf_000010"})
        assert not store.runs_dir.exists()

    def test_unserializable_payload_writes_nothing(self, tmp_path) -> None:
        store = WorkflowRunStore(base_dir=tmp_path)
        store.save("wf_000011", {("tuple", "key"): "unserializable"})
        assert list(store.runs_dir.glob("*.json")) == []

    def test_failed_chmod_still_persists_the_run(self, monkeypatch, tmp_path) -> None:
        store = WorkflowRunStore(base_dir=tmp_path)
        monkeypatch.setattr(
            store_mod.os, "chmod", lambda *a, **k: (_ for _ in ()).throw(OSError("no chmod"))
        )
        store.save("wf_000012", {"run_id": "wf_000012"})
        assert (store.runs_dir / "wf_000012.json").is_file()

    def test_failed_replace_cleans_up_the_temp_file(self, monkeypatch, tmp_path) -> None:
        store = WorkflowRunStore(base_dir=tmp_path)

        def _boom(*_a, **_k):
            raise OSError("replace failed")

        monkeypatch.setattr(store_mod.os, "replace", _boom)
        store.save("wf_000013", {"run_id": "wf_000013"})
        assert list(store.runs_dir.glob("*.json")) == []
        assert list(store.runs_dir.glob("*.tmp")) == []

    def test_temp_cleanup_failure_is_also_swallowed(self, monkeypatch, tmp_path) -> None:
        store = WorkflowRunStore(base_dir=tmp_path)

        def _boom(*_a, **_k):
            raise OSError("replace failed")

        def _unlink_boom(*_a, **_k):
            raise OSError("unlink failed")

        monkeypatch.setattr(store_mod.os, "replace", _boom)
        monkeypatch.setattr(Path, "unlink", _unlink_boom)
        store.save("wf_000014", {"run_id": "wf_000014"})
        assert list(store.runs_dir.glob("*.json")) == []


class TestDelete:
    def test_delete_removes_the_run_file(self, tmp_path) -> None:
        store = WorkflowRunStore(base_dir=tmp_path)
        store.save("wf_000015", {"run_id": "wf_000015"})
        store.delete("wf_000015")
        assert not (store.runs_dir / "wf_000015.json").exists()

    def test_missing_file_is_not_an_error(self, tmp_path) -> None:
        store = WorkflowRunStore(base_dir=tmp_path)
        store.delete("wf_never_saved")

    def test_unlink_failure_is_swallowed(self, monkeypatch, tmp_path) -> None:
        store = WorkflowRunStore(base_dir=tmp_path)
        store.save("wf_000016", {"run_id": "wf_000016"})

        def _unlink_boom(*_a, **_k):
            raise OSError("unlink failed")

        monkeypatch.setattr(Path, "unlink", _unlink_boom)
        store.delete("wf_000016")
        assert (store.runs_dir / "wf_000016.json").is_file()


class TestLoadAll:
    def test_missing_runs_dir_returns_empty(self, tmp_path) -> None:
        assert WorkflowRunStore(base_dir=tmp_path / "absent").load_all() == []

    def test_returns_oldest_file_first(self, tmp_path) -> None:
        store = WorkflowRunStore(base_dir=tmp_path)
        store.save("wf_new", {"run_id": "wf_new"})
        store.save("wf_old", {"run_id": "wf_old"})
        os.utime(store.runs_dir / "wf_old.json", (1_000_000, 1_000_000))
        os.utime(store.runs_dir / "wf_new.json", (2_000_000, 2_000_000))
        assert [r["run_id"] for r in store.load_all()] == ["wf_old", "wf_new"]

    def test_corrupt_and_idless_files_are_skipped(self, tmp_path) -> None:
        store = WorkflowRunStore(base_dir=tmp_path)
        store.save("wf_good", {"run_id": "wf_good"})
        (store.runs_dir / "corrupt.json").write_text("{not json", encoding="utf-8")
        (store.runs_dir / "idless.json").write_text(json.dumps({"status": "x"}), encoding="utf-8")
        (store.runs_dir / "notadict.json").write_text(json.dumps([1, 2]), encoding="utf-8")
        assert [r["run_id"] for r in store.load_all()] == ["wf_good"]


class TestRedact:
    def test_recurses_through_containers_and_preserves_scalars(self) -> None:
        payload = {"nested": [{"n": 1}, "plain"], "flag": True, "none": None}
        assert _redact(payload) == payload

    def test_non_json_scalar_passes_through_unchanged(self) -> None:
        sentinel = object()
        assert _redact(sentinel) is sentinel


@pytest.mark.parametrize("run_id", ["wf_000001", "wf-with-dash", "wf_ünïcode"])
def test_save_then_load_round_trips_any_run_id(tmp_path, run_id) -> None:
    store = WorkflowRunStore(base_dir=tmp_path)
    store.save(run_id, {"run_id": run_id, "name": "demo"})
    assert [r["run_id"] for r in store.load_all()] == [run_id]
