"""Coverage for the deploy profile registry's failure and migration paths.

The registry is the only record of which AWS profile a deploy may run with, so a
half-written registry is silent data loss: the atomic-write failure paths (temp
file removed, exception re-raised) and the read-through migrations from both
legacy locations are pinned here, along with the write allowlist and the
Windows fail-loud guards.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kiro_crew.deploy import profiles as profiles_mod

# profiles.py refuses deploy outright on Windows ("deploy features are not supported on
# Windows"), so a test asserting the POSIX behaviour can never hold there. Applied
# per-test rather than as a module-level mark: this file also covers the Windows refusal
# itself, and those cases must keep running on Windows.
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="deploy features are POSIX-only"
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(profiles_mod, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(profiles_mod, "_registry_path", lambda: tmp_path / "profiles.json")
    monkeypatch.setattr(
        profiles_mod, "_legacy_registry_path", lambda: tmp_path / "legacy_profiles.json"
    )
    monkeypatch.setattr(profiles_mod, "_legacy_config_path", lambda: tmp_path / "config.json")
    return tmp_path


# --- registry load / migration ----------------------------------------------


class TestLoadRegistry:
    def test_dangling_default_falls_back_to_first_profile(self, tmp_path: Path) -> None:
        (tmp_path / "profiles.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "default": "evicted",
                    "profiles": [{"name": "kept", "region": "eu-west-1"}],
                }
            ),
            encoding="utf-8",
        )
        assert profiles_mod.load_registry()["default"] == "kept"

    def test_dangling_default_with_no_profiles_clears(self, tmp_path: Path) -> None:
        (tmp_path / "profiles.json").write_text(
            json.dumps({"version": 2, "default": "evicted", "profiles": []}), encoding="utf-8"
        )
        assert profiles_mod.load_registry() == {"version": 2, "profiles": [], "default": ""}

    def test_legacy_app_dir_registry_is_migrated(self, tmp_path: Path) -> None:
        (tmp_path / "legacy_profiles.json").write_text(
            json.dumps(
                {
                    "profiles": [
                        {"name": "old-a", "region": "eu-west-1", "account": "123456789012"},
                        "not-a-dict",
                        {"region": "us-east-1"},
                    ],
                    "default": "old-a",
                }
            ),
            encoding="utf-8",
        )
        reg = profiles_mod.load_registry()
        assert [p["name"] for p in reg["profiles"]] == ["old-a"]
        assert reg["default"] == "old-a"
        assert reg["profiles"][0]["account"] == "123456789012"

    def test_legacy_registry_dangling_default_falls_back(self, tmp_path: Path) -> None:
        (tmp_path / "legacy_profiles.json").write_text(
            json.dumps({"profiles": [{"name": "old-b"}], "default": "gone"}), encoding="utf-8"
        )
        reg = profiles_mod.load_registry()
        assert reg["default"] == "old-b"
        assert reg["profiles"][0]["region"] == profiles_mod.DEFAULT_REGION

    def test_legacy_registry_dangling_default_with_no_profiles_clears(self, tmp_path: Path) -> None:
        (tmp_path / "legacy_profiles.json").write_text(
            json.dumps({"profiles": [], "default": "gone"}), encoding="utf-8"
        )
        assert profiles_mod.load_registry()["default"] == ""

    def test_v1_config_without_profile_key_yields_empty_registry(self, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text(json.dumps({"region": "eu-west-1"}), encoding="utf-8")
        assert profiles_mod.load_registry() == {"version": 2, "profiles": [], "default": ""}

    def test_corrupt_registry_falls_through_to_empty(self, tmp_path: Path) -> None:
        (tmp_path / "profiles.json").write_text("{not json", encoding="utf-8")
        assert profiles_mod.load_registry() == {"version": 2, "profiles": [], "default": ""}

    def test_note_is_truncated_on_load(self, tmp_path: Path) -> None:
        (tmp_path / "profiles.json").write_text(
            json.dumps({"profiles": [{"name": "n1", "note": "x" * 400}], "default": "n1"}),
            encoding="utf-8",
        )
        assert len(profiles_mod.load_registry()["profiles"][0]["note"]) == 256


# --- atomic writes ----------------------------------------------------------


class TestAtomicWrites:
    def test_locked_registry_persists_on_clean_exit(self, tmp_path: Path) -> None:
        with profiles_mod.locked_registry() as reg:
            reg["profiles"].append(profiles_mod.make_entry("p-locked", "us-west-2"))
            reg["default"] = "p-locked"
        assert profiles_mod.load_registry()["default"] == "p-locked"

    def test_locked_registry_skips_save_when_body_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="body failed"):
            with profiles_mod.locked_registry() as reg:
                reg["profiles"].append(profiles_mod.make_entry("never", "us-west-2"))
                raise RuntimeError("body failed")
        assert not (tmp_path / "profiles.json").exists()

    def test_locked_registry_write_failure_reraises_and_cleans_temp(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        def _boom(*_a, **_k):
            raise OSError("replace failed")

        monkeypatch.setattr(profiles_mod.os, "replace", _boom)
        with pytest.raises(OSError, match="replace failed"):
            with profiles_mod.locked_registry() as reg:
                reg["default"] = "doomed"
        assert list(tmp_path.glob("*.json.tmp")) == []

    def test_locked_registry_temp_cleanup_failure_still_reraises(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        def _replace_boom(*_a, **_k):
            raise OSError("replace failed")

        def _unlink_boom(*_a, **_k):
            raise OSError("unlink failed")

        monkeypatch.setattr(profiles_mod.os, "replace", _replace_boom)
        monkeypatch.setattr(profiles_mod.os, "unlink", _unlink_boom)
        with pytest.raises(OSError, match="replace failed"):
            with profiles_mod.locked_registry() as reg:
                reg["default"] = "doomed"

    def test_save_registry_write_failure_reraises_and_cleans_temp(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        def _boom(*_a, **_k):
            raise OSError("replace failed")

        monkeypatch.setattr(profiles_mod.os, "replace", _boom)
        with pytest.raises(OSError, match="replace failed"):
            profiles_mod.save_registry({"version": 2, "profiles": [], "default": ""})
        assert list(tmp_path.glob("*.json.tmp")) == []

    def test_save_registry_temp_cleanup_failure_still_reraises(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        def _replace_boom(*_a, **_k):
            raise OSError("replace failed")

        def _unlink_boom(*_a, **_k):
            raise OSError("unlink failed")

        monkeypatch.setattr(profiles_mod.os, "replace", _replace_boom)
        monkeypatch.setattr(profiles_mod.os, "unlink", _unlink_boom)
        with pytest.raises(OSError, match="replace failed"):
            profiles_mod.save_registry({"version": 2, "profiles": [], "default": ""})


# --- resolution -------------------------------------------------------------


class TestResolveProfile:
    def test_empty_request_resolves_to_default(self) -> None:
        profiles_mod.save_registry(
            {
                "version": 2,
                "default": "d1",
                "profiles": [profiles_mod.make_entry("d1", "eu-west-1")],
            }
        )
        assert profiles_mod.resolve_profile() == ("d1", "eu-west-1")

    def test_blank_region_resolves_to_the_engine_default(self) -> None:
        entry = profiles_mod.make_entry("d2", "")
        entry["region"] = ""
        profiles_mod.save_registry({"version": 2, "default": "d2", "profiles": [entry]})
        assert profiles_mod.resolve_profile("d2") == ("d2", profiles_mod.DEFAULT_REGION)

    def test_unregistered_name_never_resolves(self) -> None:
        profiles_mod.save_registry(
            {
                "version": 2,
                "default": "d1",
                "profiles": [profiles_mod.make_entry("d1", "eu-west-1")],
            }
        )
        assert profiles_mod.resolve_profile("stranger") is None

    def test_empty_registry_resolves_to_none(self) -> None:
        assert profiles_mod.resolve_profile() is None

    def test_get_entry_returns_none_for_unknown_name(self) -> None:
        reg = {"profiles": [profiles_mod.make_entry("d1", "eu-west-1")]}
        assert profiles_mod.get_entry(reg, "nope") is None
        assert profiles_mod.get_entry(reg, "d1")["region"] == "eu-west-1"


# --- discovery --------------------------------------------------------------


class TestDiscoverAwsProfiles:
    def test_windows_degrades_to_empty_list(self, monkeypatch) -> None:
        monkeypatch.setattr(profiles_mod.os, "name", "nt")

        def _unexpected(*_a, **_k):
            pytest.fail("ran an aws command on an unsupported platform")

        monkeypatch.setattr(profiles_mod.engine, "run_aws", _unexpected)
        assert profiles_mod.discover_aws_profiles() == []

    def test_cli_failure_degrades_to_empty_list(self, monkeypatch) -> None:
        monkeypatch.setattr(
            profiles_mod.engine, "run_aws", lambda *a, **k: (1, "", "could not be found")
        )
        assert profiles_mod.discover_aws_profiles() == []

    @_POSIX_ONLY
    def test_only_wellformed_names_are_returned(self, monkeypatch) -> None:
        out = "  p-one  \n\nbad name!\np.two\n"
        monkeypatch.setattr(profiles_mod.engine, "run_aws", lambda *a, **k: (0, out, ""))
        assert profiles_mod.discover_aws_profiles() == ["p-one", "p.two"]

    @_POSIX_ONLY
    def test_discovery_is_capped(self, monkeypatch) -> None:
        out = "\n".join(f"p{i}" for i in range(300))
        monkeypatch.setattr(profiles_mod.engine, "run_aws", lambda *a, **k: (0, out, ""))
        assert len(profiles_mod.discover_aws_profiles()) == 200


# --- writes -----------------------------------------------------------------


class TestConfigureSet:
    def test_non_allowlisted_key_is_refused_before_any_aws_call(self, monkeypatch) -> None:
        def _unexpected(*_a, **_k):
            pytest.fail("ran an aws command for a non-allowlisted key")

        monkeypatch.setattr(profiles_mod.engine, "run_aws", _unexpected)
        err = profiles_mod._configure_set("aws_access_key_id", "value", "p1")
        assert err is not None
        assert "non-allowlisted" in err

    def test_cli_error_is_returned_and_truncated(self, monkeypatch) -> None:
        monkeypatch.setattr(
            profiles_mod.engine, "run_aws", lambda *a, **k: (1, "", "  " + "e" * 400)
        )
        err = profiles_mod._configure_set("region", "us-west-2", "p1")
        assert err is not None
        assert len(err) <= 200

    def test_silent_cli_failure_gets_a_synthetic_message(self, monkeypatch) -> None:
        monkeypatch.setattr(profiles_mod.engine, "run_aws", lambda *a, **k: (1, "", ""))
        assert profiles_mod._configure_set("region", "us-west-2", "p1") == (
            "aws configure set region failed"
        )


class TestCreateAwsProfile:
    def test_windows_is_refused_before_any_aws_call(self, monkeypatch) -> None:
        monkeypatch.setattr(profiles_mod.os, "name", "nt")

        def _unexpected(*_a, **_k):
            pytest.fail("ran an aws command on an unsupported platform")

        monkeypatch.setattr(profiles_mod.engine, "run_aws", _unexpected)
        err = profiles_mod.create_aws_profile("p1", "us-west-2")
        assert err is not None
        assert "not supported on Windows" in err

    @_POSIX_ONLY
    def test_empty_name_never_touches_the_default_profile(self, monkeypatch) -> None:
        def _unexpected(*_a, **_k):
            pytest.fail("aws configure set ran without a profile name")

        monkeypatch.setattr(profiles_mod.engine, "run_aws", _unexpected)
        assert profiles_mod.create_aws_profile("", "us-west-2") == (
            "profile name must not be empty"
        )

    @_POSIX_ONLY
    def test_region_write_failure_aborts_before_credential_process(self, monkeypatch) -> None:
        seen: list[str] = []

        def _fake_run_aws(args, profile, timeout=30):
            seen.append(args[2])
            return 1, "", "region write denied"

        monkeypatch.setattr(profiles_mod.engine, "run_aws", _fake_run_aws)
        err = profiles_mod.create_aws_profile(
            "p1", "us-west-2", account="123456789012", role="Admin"
        )
        assert err == "region write denied"
        assert seen == ["region"]

    @_POSIX_ONLY
    def test_credential_process_write_failure_is_returned(self, monkeypatch) -> None:
        def _fake_run_aws(args, profile, timeout=30):
            if args[2] == "credential_process":
                return 1, "", "cred write denied"
            return 0, "", ""

        monkeypatch.setattr(profiles_mod.engine, "run_aws", _fake_run_aws)
        err = profiles_mod.create_aws_profile(
            "p1", "us-west-2", account="123456789012", role="Admin"
        )
        assert err == "cred write denied"

    @_POSIX_ONLY
    def test_region_only_profile_writes_no_credential_process(self, monkeypatch) -> None:
        written: list[str] = []

        def _fake_run_aws(args, profile, timeout=30):
            written.append(args[2])
            return 0, "", ""

        monkeypatch.setattr(profiles_mod.engine, "run_aws", _fake_run_aws)
        assert profiles_mod.create_aws_profile("p1", "us-west-2") is None
        assert written == ["region"]

    @_POSIX_ONLY
    def test_account_without_role_writes_region_only(self, monkeypatch) -> None:
        written: list[str] = []

        def _fake_run_aws(args, profile, timeout=30):
            written.append(args[2])
            return 0, "", ""

        monkeypatch.setattr(profiles_mod.engine, "run_aws", _fake_run_aws)
        assert profiles_mod.create_aws_profile("p1", "us-west-2", account="123456789012") is None
        assert written == ["region"]


def test_now_iso_is_second_precision_utc() -> None:
    stamp = profiles_mod.now_iso()
    assert stamp.endswith("+00:00")
    assert stamp.count(":") == 3


def test_legacy_path_helpers_live_under_the_config_dir(monkeypatch, tmp_path: Path) -> None:
    # The autouse fixture patches the canonical accessors; the legacy directory
    # helpers are unpatched, so they exercise the real config_dir() derivation.
    monkeypatch.setattr(profiles_mod, "config_dir", lambda: tmp_path)
    assert profiles_mod._legacy_app_dir() == tmp_path / "apps" / "deploy-web"
    assert profiles_mod._legacy_data_dir() == tmp_path / "apps" / "deploy-web" / "data"
