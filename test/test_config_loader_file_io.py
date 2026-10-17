"""config.json on disk: the validated-data cache, write protection and the whole-document save.

``load()`` caches validated data keyed on the file's fingerprint and drops it on
an edit, an atomic replacement or a save; config.json and config.local.json are
write-protected while staying readable, and the edit tool gate denies them; and
``to_dict()``, which ``save()`` writes as the whole document, keeps every section.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.config.loader as loader_module
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig


class TestConfigCache:
    """Validated-data cache keyed on file mtime/size (hot-path load())."""

    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        # Each test starts with a clean module-level cache and leaves one behind.
        from kiro_crew.config.loader import _invalidate_config_cache

        _invalidate_config_cache()
        yield
        _invalidate_config_cache()

    # Canonical scaffold so the one-shot write-back migration (which calls
    # save() and would invalidate the cache mid-test) does not fire.
    _CANON = {
        "agents": {"default": {"kiro_agent": "kirocrew"}},
        "default_agent": "default",
        "workspaces": {"default": {"dir": "~/workspace"}},
    }

    def _write(self, path: Path, data: dict) -> None:
        merged = {**self._CANON, **data}
        path.write_text(json.dumps(merged), encoding="utf-8")

    def test_second_load_skips_validation(self, tmp_path: Path) -> None:
        """A cache hit must not re-run jsonschema validation."""
        from unittest.mock import patch

        cfg_file = tmp_path / "config.json"
        # _write merges a canonical scaffold so the one-shot write-back migration
        # (which calls save() and would invalidate the cache) does not fire.
        self._write(cfg_file, {"agent": {"provider": "acp"}})
        calls = {"n": 0}
        real_validate = loader_module._validate_config_data

        def _counting(data):
            calls["n"] += 1
            return real_validate(data)

        with (
            patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            patch(
                "kiro_crew.config.loader.config_local_path",
                return_value=tmp_path / "config.local.json",
            ),
            patch("kiro_crew.config.loader._validate_config_data", _counting),
        ):
            KiroCrewConfig.load()
            KiroCrewConfig.load()
            KiroCrewConfig.load()
        # Validated once; subsequent loads served from cache.
        assert calls["n"] == 1

    def test_edit_busts_cache(self, tmp_path: Path) -> None:
        """A changed file (new mtime/size) must be re-read, not served stale."""
        import os as _os
        from unittest.mock import patch

        cfg_file = tmp_path / "config.json"
        local = tmp_path / "config.local.json"
        with (
            patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            patch("kiro_crew.config.loader.config_local_path", return_value=local),
        ):
            self._write(cfg_file, {"agent": {"model": "model-a"}})
            first = KiroCrewConfig.load()
            assert first.agent.model == "model-a"
            # Rewrite with different content (model value differs in length, so
            # the size component of the fingerprint changes); also force a distinct
            # mtime so the change is detected on coarse-resolution filesystems too.
            self._write(cfg_file, {"agent": {"model": "model-bbbb"}})
            st = cfg_file.stat()
            _os.utime(cfg_file, ns=(st.st_atime_ns + 1_000_000_000, st.st_mtime_ns + 1_000_000_000))
            second = KiroCrewConfig.load()
        assert second.agent.model == "model-bbbb"

    def test_atomic_replacement_identity_busts_same_size_fingerprint(self, tmp_path: Path) -> None:
        """Atomic mode writes differ even when legacy fingerprint fields match."""
        import os as _os
        from unittest.mock import patch

        from kiro_crew.config.loader import (
            _config_fingerprint,
            update_config_locked,
            write_config_atomically,
        )

        cfg_file = tmp_path / "config.json"
        local = tmp_path / "config.local.json"
        document = {
            **self._CANON,
            "dashboard": {"default_memory_mode": "incognito"},
        }
        with (
            patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            patch("kiro_crew.config.loader.config_local_path", return_value=local),
        ):
            write_config_atomically(cfg_file, document)
            before_stat = cfg_file.stat()
            before = _config_fingerprint()

            def select_temporary(data: dict) -> dict:
                data["dashboard"]["default_memory_mode"] = "temporary"
                return data

            update_config_locked(
                cfg_file,
                mutate=select_temporary,
                stamp_meta=False,
            )
            after_write = cfg_file.stat()
            assert after_write.st_size == before_stat.st_size
            assert after_write.st_mode == before_stat.st_mode
            _os.utime(
                cfg_file,
                ns=(after_write.st_atime_ns, before_stat.st_mtime_ns),
            )
            after = _config_fingerprint()

        # mtime, size, and mode are deliberately identical: the old fingerprint
        # would have reused another process's stale cache. Device/inode/ctime
        # replacement identity must still distinguish the atomic write.
        assert before[0][4:] == after[0][4:]
        assert before[0][1:4] != after[0][1:4]
        assert before != after

    def test_save_invalidates_cache(self, tmp_path: Path) -> None:
        """save() must drop the cache so the next load sees the written value."""
        from unittest.mock import patch

        cfg_file = tmp_path / "config.json"
        local = tmp_path / "config.local.json"
        with (
            patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            patch("kiro_crew.config.loader.config_local_path", return_value=local),
        ):
            self._write(cfg_file, {"agent": {"yolo": False}})
            cfg = KiroCrewConfig.load()
            assert cfg.agent.dangerously_skip_permissions is False
            cfg.agent.dangerously_skip_permissions = True
            cfg.save()
            reloaded = KiroCrewConfig.load()
        assert reloaded.agent.dangerously_skip_permissions is True

    def test_returned_config_is_independent(self, tmp_path: Path) -> None:
        """Mutating a loaded config must not corrupt the cached data for the next
        load — each load() returns freshly-constructed dataclasses."""
        from unittest.mock import patch

        cfg_file = tmp_path / "config.json"
        local = tmp_path / "config.local.json"
        with (
            patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            patch("kiro_crew.config.loader.config_local_path", return_value=local),
        ):
            self._write(cfg_file, {"agent": {"model": "orig-model"}})
            first = KiroCrewConfig.load()
            assert first.agent.model == "orig-model"
            # In-place mutation, as settings handlers do.
            first.agents["injected"] = KiroCrewAgentConfig(kiro_agent="x")
            first.agent.model = "MUTATED"
            second = KiroCrewConfig.load()
        assert "injected" not in second.agents
        assert second.agent.model == "orig-model"

    def test_mid_read_write_does_not_cache_stale(self, tmp_path: Path) -> None:
        """A write landing during the read->store window must NOT be served as a
        false cache hit: the cache is keyed on the PRE-read fingerprint, which
        won't match the post-write on-disk stat, so the next load re-reads."""
        import os as _os
        from unittest.mock import patch

        cfg_file = tmp_path / "config.json"
        local = tmp_path / "config.local.json"
        self._write(cfg_file, {"agent": {"model": "v0"}})

        real_read_text = Path.read_text
        injected = {"done": False}

        def _read_then_write(self_path, *a, **k):
            # Read the OLD content, then simulate a concurrent writer landing
            # before _store_validated_data stamps the cache.
            content = real_read_text(self_path, *a, **k)
            if not injected["done"] and self_path == cfg_file:
                injected["done"] = True
                self._write(cfg_file, {"agent": {"model": "v1"}})
                st = cfg_file.stat()
                _os.utime(
                    cfg_file,
                    ns=(st.st_atime_ns + 1_000_000_000, st.st_mtime_ns + 1_000_000_000),
                )
            return content

        with (
            patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            patch("kiro_crew.config.loader.config_local_path", return_value=local),
            patch.object(Path, "read_text", _read_then_write),
        ):
            first = KiroCrewConfig.load()  # reads v0, writer swaps to v1 mid-read
            # First load returns the v0 it actually read (acceptable).
            assert first.agent.model == "v0"
        # Next load must re-read and see v1 — NOT serve stale v0 from a poisoned cache.
        with (
            patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            patch("kiro_crew.config.loader.config_local_path", return_value=local),
        ):
            second = KiroCrewConfig.load()
        assert second.agent.model == "v1", "stale config served from poisoned cache"


class TestConfigWriteProtection:
    """config.json / config.local.json are WRITE-protected (reads allowed)."""

    def test_config_json_is_write_protected(self) -> None:
        from kiro_crew.security import is_sensitive_write_path

        # Data home moved to ~/.kiro/crew; the legacy ~/.kirocrew stays gated too.
        assert is_sensitive_write_path("~/.kiro/crew/config.json")
        assert is_sensitive_write_path(str(Path.home() / ".kiro" / "crew" / "config.json"))
        assert is_sensitive_write_path("~/.kirocrew/config.json")
        assert is_sensitive_write_path(str(Path.home() / ".kirocrew" / "config.json"))

    def test_config_local_json_is_write_protected(self) -> None:
        from kiro_crew.security import is_sensitive_write_path

        assert is_sensitive_write_path("~/.kiro/crew/config.local.json")
        assert is_sensitive_write_path(str(Path.home() / ".kiro" / "crew" / "config.local.json"))
        assert is_sensitive_write_path("~/.kirocrew/config.local.json")
        assert is_sensitive_write_path(str(Path.home() / ".kirocrew" / "config.local.json"))

    def test_config_json_reads_still_allowed(self) -> None:
        from kiro_crew.security import is_sensitive_bash_command, is_sensitive_path

        assert is_sensitive_path("~/.kiro/crew/config.json") is False
        assert is_sensitive_bash_command("cat ~/.kiro/crew/config.json") is None
        assert is_sensitive_path("~/.kirocrew/config.json") is False
        assert is_sensitive_bash_command("cat ~/.kirocrew/config.json") is None

    def test_write_protection_superset_of_sensitive(self) -> None:
        from kiro_crew.security import is_sensitive_write_path

        assert is_sensitive_write_path("~/.aws/credentials")
        assert is_sensitive_write_path("~/.kiro/crew/security_policy.json")
        assert is_sensitive_write_path("~/.kirocrew/security_policy.json")

    def test_non_config_kirocrew_file_not_write_protected(self) -> None:
        from kiro_crew.security import is_sensitive_write_path

        assert is_sensitive_write_path("~/.kiro/crew/sessions.db") is False
        assert is_sensitive_write_path("~/.kirocrew/sessions.db") is False


class TestConfigEditToolBlocked:
    """The file-edit tool gate (HookManager.on_tool_call) denies edits to config."""

    def _hooks(self):
        from kiro_crew.hooks import HookManager, HooksConfig

        return HookManager(HooksConfig())

    def test_edit_config_json_denied(self) -> None:
        result = self._hooks().on_tool_call(
            "Editing config.json",
            tool_kind="edit",
            raw_params={"path": "~/.kirocrew/config.json"},
        )
        assert result.action == "deny"
        assert "write-protected config" in (result.reason or "")

    def test_read_config_json_not_denied_by_edit_gate(self) -> None:
        result = self._hooks().on_tool_call(
            "config.json",
            tool_kind="read",
            raw_params={"path": "~/.kirocrew/config.json"},
        )
        assert result.action != "deny"

    def test_edit_normal_workspace_file_allowed(self) -> None:
        result = self._hooks().on_tool_call(
            "Editing notes.md",
            tool_kind="edit",
            raw_params={"path": "~/.kirocrew/workspace/notes.md"},
        )
        assert result.action != "deny"


class TestSaveRoundTripPreservesAllSections:
    """to_dict() (which save() writes as the ENTIRE config.json) must not omit
    knowledge/heartbeat/snapshot_dir/watchdog, or any save silently
    deletes those sections from disk. save() fires from many routine paths —
    the theme PUT handler, the AIM auto-update toggle, and (worst) the one-shot
    write-back migration inside load() itself when a config lacks an "agents"
    map. So a user who hand-wrote e.g. {"knowledge": {"pool_idle_ttl_secs": 0}}
    lost it the first time the gateway started."""

    def test_to_dict_includes_previously_dropped_sections(self) -> None:
        cfg = KiroCrewConfig()
        td = cfg.to_dict()
        for key in ("knowledge", "heartbeat", "snapshot_dir", "watchdog"):
            assert key in td, f"to_dict() dropped {key} — save() would delete it"

    def test_migration_save_preserves_hand_written_sections(self) -> None:
        # The finding's exact repro: a config with no "agents" key triggers the
        # write-back migration save() inside load(); the hand-written sections
        # must survive it.
        import json
        import tempfile
        import unittest.mock
        from pathlib import Path

        cfg_data = {
            "knowledge": {"pool_idle_ttl_secs": 0},
            "heartbeat": {"default_deliver": "dashboard"},
            "snapshot_dir": "/tmp/snaps",
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(cfg_data, f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                KiroCrewConfig.load()  # migration write-back save fires
            after = json.loads(tmp.read_text(encoding="utf-8"))
            assert after.get("knowledge", {}).get("pool_idle_ttl_secs") == 0
            assert after.get("heartbeat", {}).get("default_deliver") == "dashboard"
            assert after.get("snapshot_dir") == "/tmp/snaps"
        finally:
            tmp.unlink(missing_ok=True)
