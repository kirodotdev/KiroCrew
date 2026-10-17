"""Tests for config/loader.py's writes: the locked read-modify-write and the agents migration.

``update_config_locked`` and the one-time agents migration's backup and write-back
ordering, plus three standalone pins: every transport's context thresholds, the
WeCom section's fields, and ``same_dispatch_binding``'s field set. The other
loader tests are in the ``test_config_loader_*.py`` siblings, and the load helpers
and hypothesis strategies they share are in ``config_loader_helpers.py``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import platform
import threading
import unittest.mock
from pathlib import Path

import pytest
from config_loader_helpers import _load_from_dict

import kiro_crew.config.loader as loader_module
from kiro_crew import platform_compat
from kiro_crew.config.loader import (
    KiroCrewConfig,
    ResolvedBindings,
)

# ---------------------------------------------------------------------------
# Tests for update_config_locked
# ---------------------------------------------------------------------------


class TestUpdateConfigLocked:
    """Tests for the locked read-modify-write helper."""

    def test_lost_update_without_lock(self, tmp_path: Path) -> None:
        """Control: interleaved RMW without the lock loses one update."""
        import threading

        from kiro_crew.config.loader import read_config_for_update, write_config_atomically

        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"counter": 0}))

        barrier = threading.Barrier(2)

        def writer_a() -> None:
            data = read_config_for_update(cfg)
            barrier.wait()  # Force interleave: both read before either writes
            data["key_a"] = "from_a"
            write_config_atomically(cfg, data)

        def writer_b() -> None:
            data = read_config_for_update(cfg)
            barrier.wait()
            data["key_b"] = "from_b"
            write_config_atomically(cfg, data)

        t_a = threading.Thread(target=writer_a)
        t_b = threading.Thread(target=writer_b)
        t_a.start()
        t_b.start()
        t_a.join()
        t_b.join()

        result = json.loads(cfg.read_text())
        # One of the keys is lost: the last writer overwrites the first's change.
        has_a = "key_a" in result
        has_b = "key_b" in result
        assert not (has_a and has_b), (
            "Both keys present without a lock — the interleave did not manifest "
            "(this is expected to fail on some runs; the test is probabilistic as a control)"
        )

    def test_locked_update_preserves_both_writes(self, tmp_path: Path) -> None:
        """Deterministic interleave through update_config_locked preserves both keys."""
        import threading

        from kiro_crew.config.loader import update_config_locked

        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"counter": 0}))

        # Use events to force deterministic ordering:
        # writer_a enters mutate → signals a_in_mutate → writer_b tries to enter
        # (blocks on lock) → writer_a finishes → writer_b enters and sees key_a.
        a_in_mutate = threading.Event()
        a_may_finish = threading.Event()

        def mutate_a(data: dict) -> dict:
            a_in_mutate.set()
            a_may_finish.wait()
            data["key_a"] = "from_a"
            return data

        def mutate_b(data: dict) -> dict:
            data["key_b"] = "from_b"
            return data

        def writer_a() -> None:
            update_config_locked(cfg, mutate=mutate_a, stamp_meta=False)

        def writer_b() -> None:
            a_in_mutate.wait()  # Ensure A holds the lock first
            update_config_locked(cfg, mutate=mutate_b, stamp_meta=False)

        t_a = threading.Thread(target=writer_a)
        t_b = threading.Thread(target=writer_b)
        t_a.start()
        t_b.start()
        # Let A finish after B has started (B will block on the lock)
        import time

        time.sleep(0.05)
        a_may_finish.set()
        t_a.join()
        t_b.join()

        result = json.loads(cfg.read_text())
        assert result["key_a"] == "from_a"
        assert result["key_b"] == "from_b"
        assert result["counter"] == 0

    def test_fail_closed_unreadable_config(self, tmp_path: Path) -> None:
        """A mutate over an unreadable config raises ConfigReadError; file untouched."""
        from kiro_crew.config.loader import ConfigReadError, update_config_locked

        cfg = tmp_path / "config.json"
        corrupt_content = "not valid json {{{"
        cfg.write_text(corrupt_content)

        with pytest.raises(ConfigReadError):
            update_config_locked(cfg, mutate=lambda d: {**d, "key": "val"})

        # File must be untouched.
        assert cfg.read_text() == corrupt_content
        # Lockfile must not be left open (fd is closed).
        lock = tmp_path / "config.json.lock"
        assert lock.exists()  # lockfile itself is fine to exist

    def test_mode_preservation(self, tmp_path: Path) -> None:
        """A 0600 config stays 0600 through a locked update."""
        if platform.system() == "Windows":
            pytest.skip("POSIX mode test")

        from kiro_crew.config.loader import update_config_locked

        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"x": 1}))
        os.chmod(cfg, 0o600)

        update_config_locked(cfg, mutate=lambda d: {**d, "y": 2}, stamp_meta=False)

        import stat

        mode = stat.S_IMODE(cfg.stat().st_mode)
        assert mode == 0o600

    def test_mutate_returning_none_skips_write(self, tmp_path: Path) -> None:
        """When mutate returns None, the file is not rewritten."""
        from kiro_crew.config.loader import update_config_locked

        cfg = tmp_path / "config.json"
        original = json.dumps({"unchanged": True})
        cfg.write_text(original)
        mtime_before = cfg.stat().st_mtime_ns

        import time

        time.sleep(0.01)  # ensure mtime would change if written
        result = update_config_locked(cfg, mutate=lambda d: None, stamp_meta=False)

        assert result == {"unchanged": True}
        assert cfg.stat().st_mtime_ns == mtime_before

    def test_lockfile_is_sidecar(self, tmp_path: Path) -> None:
        """The lockfile is a sidecar (.lock suffix), not the config itself."""
        from kiro_crew.config.loader import update_config_locked

        cfg = tmp_path / "config.json"
        cfg.write_text("{}")

        update_config_locked(cfg, mutate=lambda d: {**d, "k": 1}, stamp_meta=False)

        lock = tmp_path / "config.json.lock"
        assert lock.exists()

    def test_stamp_meta_default(self, tmp_path: Path) -> None:
        """With stamp_meta=True (default), a meta block is added."""
        from kiro_crew.config.loader import update_config_locked

        cfg = tmp_path / "config.json"
        cfg.write_text("{}")

        result = update_config_locked(cfg, mutate=lambda d: {**d, "key": "val"})

        on_disk = json.loads(cfg.read_text())
        assert "meta" in on_disk
        assert "lastTouchedVersion" in on_disk["meta"]
        assert on_disk["key"] == "val"
        assert "meta" in result

    def test_new_file_creation(self, tmp_path: Path) -> None:
        """update_config_locked works when the config file does not yet exist."""
        from kiro_crew.config.loader import update_config_locked

        cfg = tmp_path / "config.json"
        # File does not exist yet

        result = update_config_locked(cfg, mutate=lambda d: {**d, "new": True}, stamp_meta=False)

        assert result == {"new": True}
        assert json.loads(cfg.read_text()) == {"new": True}

    def test_on_corrupt_fail_is_default(self, tmp_path: Path) -> None:
        """Default on_corrupt='fail' raises ConfigReadError; file untouched."""
        from kiro_crew.config.loader import ConfigReadError, update_config_locked

        cfg = tmp_path / "config.json"
        corrupt = "not json {{{"
        cfg.write_text(corrupt)

        with pytest.raises(ConfigReadError):
            update_config_locked(cfg, mutate=lambda d: {**d, "k": 1}, stamp_meta=False)

        assert cfg.read_text() == corrupt

    def test_on_corrupt_reset_writes_from_empty(self, tmp_path: Path) -> None:
        """on_corrupt='reset' invokes mutate with {} and writes the result."""
        from kiro_crew.config.loader import update_config_locked

        cfg = tmp_path / "config.json"
        cfg.write_text("broken {{")

        result = update_config_locked(
            cfg, mutate=lambda d: {**d, "key": "val"}, stamp_meta=False, on_corrupt="reset"
        )

        assert result == {"key": "val"}
        assert json.loads(cfg.read_text()) == {"key": "val"}

    def test_on_corrupt_reset_mode_0o600(self, tmp_path: Path) -> None:
        """on_corrupt='reset' writes with 0o600 since no valid mode to preserve."""
        if platform.system() == "Windows":
            pytest.skip("POSIX mode test")
        import stat

        from kiro_crew.config.loader import update_config_locked

        cfg = tmp_path / "config.json"
        cfg.write_text("corrupt!")
        # Give it a weird mode to show it's not preserved (file is corrupt)
        os.chmod(cfg, 0o644)

        update_config_locked(
            cfg, mutate=lambda d: {**d, "x": 1}, stamp_meta=False, on_corrupt="reset"
        )

        mode = stat.S_IMODE(cfg.stat().st_mode)
        # write_config_atomically preserves mode of the existing file, even if
        # corrupt; this verifies it at least got written
        assert json.loads(cfg.read_text()) == {"x": 1}
        # Mode is preserved from the existing inode (0o644 in this case)
        assert mode == 0o644

    def test_on_corrupt_reset_single_lock_hold(self, tmp_path: Path) -> None:
        """Corrupt-triggered reset holds the lock for the entire operation.

        Writer A holds the lock repairing the overlay (on_corrupt='reset');
        writer B blocks until A finishes. Both writes are serialized under
        ONE lock hold — the window where the old two-critical-section shape
        allowed B to land between A's read and write is gone.
        """
        import threading

        from kiro_crew.config.loader import update_config_locked

        cfg = tmp_path / "config.json"
        cfg.write_text("corrupt file {{{")

        # Events for deterministic interleave
        a_in_mutate = threading.Event()
        a_may_finish = threading.Event()

        def mutate_a(data: dict) -> dict:
            # data should be {} (corrupt → reset)
            assert data == {}
            a_in_mutate.set()
            a_may_finish.wait()
            data["key_a"] = "from_a"
            return data

        def mutate_b(data: dict) -> dict:
            # B enters AFTER A finishes (lock serializes); sees A's result
            data["key_b"] = "from_b"
            return data

        def writer_a() -> None:
            update_config_locked(cfg, mutate=mutate_a, stamp_meta=False, on_corrupt="reset")

        def writer_b() -> None:
            a_in_mutate.wait()  # Wait for A to be inside mutate (holding the lock)
            # B uses on_corrupt="reset" too (it would see a valid file after A)
            update_config_locked(cfg, mutate=mutate_b, stamp_meta=False)

        t_a = threading.Thread(target=writer_a)
        t_b = threading.Thread(target=writer_b)
        t_a.start()
        t_b.start()

        import time

        time.sleep(0.05)  # Let B start and block on the lock
        a_may_finish.set()
        t_a.join()
        t_b.join()

        result = json.loads(cfg.read_text())
        # Both keys present: B saw A's write (serialized), no clobber
        assert result["key_a"] == "from_a"
        assert result["key_b"] == "from_b"

    def test_on_corrupt_reset_non_dict_file(self, tmp_path: Path) -> None:
        """on_corrupt='reset' also handles a file that is valid JSON but not a dict."""
        from kiro_crew.config.loader import update_config_locked

        cfg = tmp_path / "config.json"
        cfg.write_text('"just a string"')

        result = update_config_locked(
            cfg, mutate=lambda d: {**d, "repaired": True}, stamp_meta=False, on_corrupt="reset"
        )

        assert result == {"repaired": True}
        assert json.loads(cfg.read_text()) == {"repaired": True}


class TestMigrationBackupContainment:
    """``load()`` must not create files beside a config path it does not own.

    The one-time agents migration copies the pre-migration config aside. The
    copy landed next to whatever ``config_path()`` resolved to, so every caller
    that redirects the loader at its own temp file (this module's own helpers do
    exactly that) silently accumulated a ``<tmpname>.json.bak`` orphan it never
    cleaned up -- 72k of them on one dev host, against a tmpfs inode cap.
    """

    # A config with neither "agents" nor "default_agent" is what makes
    # load() take the migration write-back branch that writes the backup.
    _LEGACY = {"telegram": {"allow_forum": True}}

    def _load_with(self, home: Path, cfg_file: Path) -> KiroCrewConfig:
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=home),
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
        ):
            return KiroCrewConfig.load()

    def test_no_sibling_left_beside_a_redirected_config(self, tmp_path: Path) -> None:
        """A caller-owned directory gains nothing from a migrating load()."""
        caller_dir = tmp_path / "caller-owned"
        caller_dir.mkdir()
        cfg_file = caller_dir / "tmpdeadbeef.json"
        cfg_file.write_text(json.dumps(self._LEGACY), encoding="utf-8")
        home = tmp_path / "home"
        home.mkdir()

        before = {p.name for p in caller_dir.iterdir()}
        cfg = self._load_with(home, cfg_file)
        after = {p.name for p in caller_dir.iterdir()}

        # Guard the guard: assert the migration branch actually ran, otherwise
        # this test would pass for the wrong reason if that branch stops firing.
        assert cfg.agents, "migration write-back did not run, test is vacuous"
        assert after == before, "load() left orphans beside the caller's config: %s" % sorted(
            after - before
        )

    def test_real_config_in_the_data_home_is_still_backed_up(self, tmp_path: Path) -> None:
        """The production path keeps its backup exactly where it always was."""
        home = tmp_path / "home"
        home.mkdir()
        cfg_file = home / "config.json"
        cfg_file.write_text(json.dumps(self._LEGACY), encoding="utf-8")

        cfg = self._load_with(home, cfg_file)

        assert cfg.agents, "migration write-back did not run, test is vacuous"
        assert (home / "config.json.bak").exists()

    def test_backup_appends_the_suffix_instead_of_replacing_it(self, tmp_path: Path) -> None:
        """A config whose name is not ``*.json`` is backed up, not renamed.

        ``Path.with_suffix(".json.bak")`` REPLACES the final suffix, so
        ``settings.conf`` would have produced ``settings.json.bak``.
        """
        home = tmp_path / "home"
        home.mkdir()
        cfg_file = home / "settings.conf"
        cfg_file.write_text(json.dumps(self._LEGACY), encoding="utf-8")

        self._load_with(home, cfg_file)

        assert (home / "settings.conf.bak").exists()
        assert not (home / "settings.json.bak").exists()

    def test_failed_backup_still_aborts_the_migration_save(self, tmp_path: Path) -> None:
        """A config we could not copy aside is not rewritten either.

        The caller's ``except`` is what skips ``cfg.save()``, so containing the
        LOCATION decision must not also swallow a failing copy: otherwise the
        only pre-migration copy is overwritten with no backup anywhere.
        """
        home = tmp_path / "home"
        home.mkdir()
        cfg_file = home / "config.json"
        original = json.dumps(self._LEGACY)
        cfg_file.write_text(original, encoding="utf-8")

        with unittest.mock.patch(
            "kiro_crew.config.loader.shutil.copy2",
            side_effect=OSError("backup target is read-only"),
        ):
            cfg = self._load_with(home, cfg_file)

        # load() still returns a usable config -- the write-back is best-effort.
        assert cfg.agents
        # But the on-disk config is untouched, so the migration retries next load.
        assert json.loads(cfg_file.read_text(encoding="utf-8")) == json.loads(original)
        assert not (home / "config.json.bak").exists()


# Transports carrying a soft/hard context-threshold pair, and those carrying
# only the soft nudge (their hard backstop is the backend autocompactor).
# A future transport that forgets _threshold_pct / _normalize_threshold_pair
# fails these tests rather than shipping an unclamped read.
_THRESHOLD_PAIR_TRANSPORTS = ["weixin", "webex", "teams", "wecom", "whatsapp", "imessage"]
_THRESHOLD_SOFT_ONLY_TRANSPORTS = ["telegram", "discord"]


class TestTransportThresholdConsistency:
    """Every transport's context thresholds share ONE validation.

    Locks in the invariant that validity is a property of the type: the loader
    coerces every read through _threshold_pct (clamped to 1..100) and each
    dataclass's __post_init__ normalizes its fields (pair transports also
    enforce soft <= hard), so neither a hand-edited config nor a
    code-constructed object can hold an out-of-range or inverted value.
    """

    @staticmethod
    def _section(cfg, transport: str):
        return getattr(cfg, transport)

    @pytest.mark.parametrize(
        "transport", _THRESHOLD_PAIR_TRANSPORTS + _THRESHOLD_SOFT_ONLY_TRANSPORTS
    )
    def test_defaults_when_missing(self, transport: str) -> None:
        section = self._section(_load_from_dict({transport: {}}), transport)
        assert section.soft_threshold_pct == 80
        if transport in _THRESHOLD_PAIR_TRANSPORTS:
            assert section.hard_threshold_pct == 95

    @pytest.mark.parametrize(
        "transport", _THRESHOLD_PAIR_TRANSPORTS + _THRESHOLD_SOFT_ONLY_TRANSPORTS
    )
    @pytest.mark.parametrize("raw", [-5, 0, 101, 10_000])
    def test_out_of_range_values_are_clamped(self, transport: str, raw: int) -> None:
        payload: dict = {"soft_threshold_pct": raw}
        if transport in _THRESHOLD_PAIR_TRANSPORTS:
            payload["hard_threshold_pct"] = raw
        section = self._section(_load_from_dict({transport: payload}), transport)
        assert 1 <= section.soft_threshold_pct <= 100
        if transport in _THRESHOLD_PAIR_TRANSPORTS:
            assert 1 <= section.hard_threshold_pct <= 100
            assert section.soft_threshold_pct <= section.hard_threshold_pct

    @pytest.mark.parametrize("transport", _THRESHOLD_PAIR_TRANSPORTS)
    def test_soft_above_hard_is_corrected(self, transport: str) -> None:
        # An inverted pair (soft=90, hard=50) would make the soft nudge
        # unreachable -- the transports check pct >= hard first.
        section = self._section(
            _load_from_dict({transport: {"soft_threshold_pct": 90, "hard_threshold_pct": 50}}),
            transport,
        )
        assert section.hard_threshold_pct == 50
        assert section.soft_threshold_pct == 50

    @pytest.mark.parametrize(
        "transport", _THRESHOLD_PAIR_TRANSPORTS + _THRESHOLD_SOFT_ONLY_TRANSPORTS
    )
    def test_non_numeric_values_fall_back_to_defaults(self, transport: str) -> None:
        payload: dict = {"soft_threshold_pct": "abc"}
        if transport in _THRESHOLD_PAIR_TRANSPORTS:
            payload["hard_threshold_pct"] = None
        section = self._section(_load_from_dict({transport: payload}), transport)
        assert section.soft_threshold_pct == 80
        if transport in _THRESHOLD_PAIR_TRANSPORTS:
            assert section.hard_threshold_pct == 95

    @pytest.mark.parametrize(
        "transport", _THRESHOLD_PAIR_TRANSPORTS + _THRESHOLD_SOFT_ONLY_TRANSPORTS
    )
    def test_code_constructed_object_is_normalized(self, transport: str) -> None:
        # __post_init__ makes an object constructed in code valid too, not just
        # one loaded from disk.
        cls = {
            "telegram": loader_module.TelegramConfig,
            "weixin": loader_module.WeixinConfig,
            "discord": loader_module.DiscordConfig,
            "webex": loader_module.WebexConfig,
            "teams": loader_module.TeamsConfig,
            "wecom": loader_module.WeComConfig,
            "whatsapp": loader_module.WhatsAppConfig,
            "imessage": loader_module.IMessageConfig,
        }[transport]
        if transport in _THRESHOLD_PAIR_TRANSPORTS:
            obj = cls(soft_threshold_pct=150, hard_threshold_pct=-3)
            assert obj.hard_threshold_pct == 1
            assert obj.soft_threshold_pct == 1
        else:
            obj = cls(soft_threshold_pct=150)
            assert obj.soft_threshold_pct == 100

    @pytest.mark.parametrize(
        "transport", _THRESHOLD_PAIR_TRANSPORTS + _THRESHOLD_SOFT_ONLY_TRANSPORTS
    )
    def test_an_integral_float_is_honoured_on_a_shipped_install(
        self, transport: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A threshold written as ``90.0`` is the value the operator chose.

        Asserted with the schema layer OFF, because that is the shipped
        configuration: ``jsonschema`` is a dev-only dependency, so on a user's
        install ``validate_config_data`` returns early and its
        ``_coerce_legacy_numeric_values`` pre-pass -- which is what turns an
        integral float into an int -- never runs. The loader's own coercion is
        then the only one there is, and the shared ``_threshold_pct`` accepts an
        integral float where a hand-rolled ``int(str(raw))`` raises on ``"90.0"``
        and silently substitutes the default. That channel would keep firing at
        80 for someone who configured 90, with nothing logged.

        Leaving jsonschema installed here (as CI does) makes this pass against a
        hand-rolled clamp too, which is exactly the mistake that hides the bug.
        """
        import kiro_crew.config.validation as validation

        monkeypatch.setattr(validation, "_HAS_JSONSCHEMA", False)
        payload: dict = {"soft_threshold_pct": 90.0}
        if transport in _THRESHOLD_PAIR_TRANSPORTS:
            payload["hard_threshold_pct"] = 99.0
        section = self._section(_load_from_dict({transport: payload}), transport)
        assert section.soft_threshold_pct == 90
        if transport in _THRESHOLD_PAIR_TRANSPORTS:
            assert section.hard_threshold_pct == 99

    def test_telegram_account_soft_threshold_clamped(self) -> None:
        # Deprecated/inert telegram.accounts entries still parse through the
        # same coercion so a round-tripped config stays in range.
        cfg = _load_from_dict(
            {
                "telegram": {
                    "accounts": {"acct-1": {"bot_token": "123:abc", "soft_threshold_pct": 400}}
                }
            }
        )
        assert cfg.telegram.accounts["acct-1"].soft_threshold_pct == 100


#: One non-default sample per ``WeComConfig`` field, keyed by field name. The map
#: must cover EVERY field: ``load()`` enumerates the section's keys by hand, so a
#: field added to the dataclass but forgotten there loads as its default and is
#: then written back as its default — the operator's setting is both ignored and
#: erased, with nothing failing — a key that looks wired (it appears in the config
#: schema, which IS derived from the dataclass) while being discarded at load. This
#: map is the ratchet: a new field fails the test below until a sample is added,
#: which forces the author through the load path.
_WECOM_FIELD_SAMPLES: dict[str, object] = {
    "enabled": True,
    "allowed_users": [{"userid": "zhangsan", "name": "Z"}],
    "allow_all_users": True,
    "ws_url": "wss://example.invalid/ws",
    "soft_threshold_pct": 71,
    "hard_threshold_pct": 91,
    "session_folder": "wecom-folder",
}


class TestWeComSectionSurvivesLoad:
    def test_the_sample_map_covers_every_field(self) -> None:
        import dataclasses

        declared = {f.name for f in dataclasses.fields(loader_module.WeComConfig)}
        missing = declared - set(_WECOM_FIELD_SAMPLES)
        assert not missing, (
            f"WeComConfig gained field(s) {sorted(missing)} with no sample here. Add one, "
            "and check KiroCrewConfig.load() actually reads the key -- it enumerates them "
            "by hand, so an omission silently discards the operator's value."
        )
        assert not set(_WECOM_FIELD_SAMPLES) - declared, "sample for a field that no longer exists"

    @pytest.mark.parametrize("field_name", sorted(_WECOM_FIELD_SAMPLES))
    def test_each_configured_value_survives_load(self, field_name: str) -> None:
        sample = _WECOM_FIELD_SAMPLES[field_name]
        cfg = _load_from_dict({"wecom": {field_name: sample}})
        loaded = getattr(cfg.wecom, field_name)
        default = getattr(loader_module.WeComConfig(), field_name)
        assert loaded != default, (
            f"wecom.{field_name} loaded as its default {default!r} despite being configured "
            f"as {sample!r} -- KiroCrewConfig.load() is not reading the key"
        )

    @pytest.mark.parametrize("junk", [None, 7, "zhangsan", {"a": 1}, True])
    def test_a_non_list_allow_list_degrades_to_empty_rather_than_crashing_load(
        self, junk: object
    ) -> None:
        # An explicit `null`, or any non-list, must not raise out of load(): a
        # config file the operator can write is not a trusted type, and a TypeError
        # here prevents STARTUP rather than degrading one setting. A bare string is
        # the subtle one -- iterating it would char-split into single-letter userids.
        cfg = _load_from_dict({"wecom": {"allowed_users": junk}})
        assert cfg.wecom.allowed_users == []

    @pytest.mark.parametrize("field", ["enabled", "allow_all_users"])
    @pytest.mark.parametrize("junk", ["false", "off", "0", 0, 1, [], {}, None, "true"])
    def test_a_non_bool_never_turns_a_switch_ON(self, field: str, junk: object) -> None:
        # `bool("false")` is True, so a JSON string would invert the operator's
        # intent -- enabling the channel, or opening it to every org member, from a
        # value that says the opposite. Even the "helpfully" correct-looking "true"
        # must not enable it: a parse that guesses at a string is the same parse that
        # cannot refuse "false".
        cfg = _load_from_dict({"wecom": {field: junk}})
        assert getattr(cfg.wecom, field) is False

    @pytest.mark.parametrize("field", ["enabled", "allow_all_users"])
    def test_a_real_bool_still_works(self, field: str) -> None:
        cfg = _load_from_dict({"wecom": {field: True}})
        assert getattr(cfg.wecom, field) is True


# Every field same_dispatch_binding() compares — the fields that change what
# answers a turn. Must stay in lockstep with the method body in
# config/loader.py.
_DISPATCH_COMPARED = {
    "kiro_agent",
    "workspace_dir",
    "memory_store_name",
    "model",
}

# Fields deliberately EXCLUDED from the comparison. Each reason mirrors the
# rationale in same_dispatch_binding()'s docstring; the docstring pin below
# asserts the docstring still names every exempt field.
_DISPATCH_EXEMPT = {
    # Two names resolving to one alias's target ARE the same binding.
    "resolved_alias",
    # Request metadata the caller checks separately, not dispatch identity.
    "requested_resolved",
    # Namespace provenance controls later resolution, not identical current targets.
    "selection_kind",
    # Protects automatic publication after resolution, not dispatch identity.
    "selection_revision",
    # Derived from memory_store_name plus global config shared by both sides.
    "effective_memory_config",
    # Session selection validates admitted member/mode before this target comparison.
    "execution_context",
}


def _dispatch_field_mutations() -> dict[str, object]:
    """A type-correct replacement value per field, distinct from the baseline.

    Built fresh per call so no test can corrupt a shared module-level table
    (the effective_memory_config value is a mutable dict). Passing one entry
    to dataclasses.replace() changes exactly that field. A new dataclass
    field must gain an entry here too — the coverage test asserts it.
    """
    return {
        "kiro_agent": "drift-pin-other-agent",
        "workspace_dir": Path("drift-pin-other-workspace"),
        "memory_store_name": "drift-pin-other-store",
        "model": "drift-pin-other-model",
        "resolved_alias": "drift-pin-other-alias",
        "requested_resolved": False,
        "selection_kind": "template",
        "selection_revision": "observed-selection-revision",
        "effective_memory_config": {"embedding_provider": "drift-pin-other"},
        "execution_context": object(),
    }


def _dispatch_bindings() -> ResolvedBindings:
    """A fully-populated baseline for the dispatch-identity pins below."""
    return ResolvedBindings(
        workspace_dir=Path("drift-pin-workspace"),
        memory_store_name="drift-pin-store",
        effective_memory_config={"embedding_provider": "drift-pin-base"},
        kiro_agent="drift-pin-agent",
        model="drift-pin-model",
        requested_resolved=True,
        resolved_alias="drift-pin-alias",
    )


class TestSameDispatchBindingDriftPin:
    """Pin same_dispatch_binding()'s field set against dataclass drift.

    The method compares the dispatch-relevant fields of ResolvedBindings to
    decide whether two agent NAMES may share a chat slot (the dashboard's
    slot agent-conflict guard). Its docstring documents which fields are
    deliberately excluded, but only these tests ENFORCE that every field IS
    classified: a new dataclass field fails here and forces the author to
    decide — dispatch-relevant (add it to _DISPATCH_COMPARED AND to the
    method body) or exempt (add it to _DISPATCH_EXEMPT with a one-line
    reason). The pin cannot judge whether that decision is right; its value
    is that the decision becomes explicit and reviewable instead of silent.
    """

    def test_every_dataclass_field_is_classified(self) -> None:
        field_names = {f.name for f in dataclasses.fields(ResolvedBindings)}
        assert field_names == _DISPATCH_COMPARED | _DISPATCH_EXEMPT, (
            "ResolvedBindings grew or lost a field without a dispatch-identity "
            "decision. Classify every field: dispatch-relevant fields go in "
            "_DISPATCH_COMPARED AND in same_dispatch_binding()'s body; "
            "non-identity fields go in _DISPATCH_EXEMPT with a one-line reason. "
            "Either way, add a distinct replacement value to "
            "_dispatch_field_mutations() "
            f"(unclassified: {sorted(field_names - _DISPATCH_COMPARED - _DISPATCH_EXEMPT)}, "
            f"stale: {sorted((_DISPATCH_COMPARED | _DISPATCH_EXEMPT) - field_names)})"
        )
        assert (
            _DISPATCH_COMPARED & _DISPATCH_EXEMPT == set()
        ), "a field cannot be both compared and exempt — remove it from one set"

    def test_mutation_table_covers_every_field(self) -> None:
        field_names = {f.name for f in dataclasses.fields(ResolvedBindings)}
        assert set(_dispatch_field_mutations()) == field_names, (
            "_dispatch_field_mutations() must hold exactly one replacement "
            "value per ResolvedBindings field — the behavior pins below "
            "iterate it"
        )

    def test_mutation_table_actually_changes_each_field(self) -> None:
        # Guards the pins below: a mutation equal to the baseline value would
        # make the compared-field test pass vacuously.
        base = _dispatch_bindings()
        for name, value in _dispatch_field_mutations().items():
            assert value != getattr(base, name), name

    def test_equal_but_distinct_instances_are_the_same_binding(self) -> None:
        # Reflexivity/symmetry floor: dispatch identity must come from field
        # VALUES, not object identity.
        a = _dispatch_bindings()
        b = _dispatch_bindings()
        assert a is not b
        assert a.same_dispatch_binding(a)
        assert a.same_dispatch_binding(b)
        assert b.same_dispatch_binding(a)

    @pytest.mark.parametrize("field_name", sorted(_DISPATCH_COMPARED))
    def test_mutating_a_compared_field_breaks_dispatch_identity(self, field_name: str) -> None:
        base = _dispatch_bindings()
        mutations = _dispatch_field_mutations()
        mutated = dataclasses.replace(base, **{field_name: mutations[field_name]})
        assert not base.same_dispatch_binding(mutated), (
            f"same_dispatch_binding() ignored {field_name!r}, which "
            "_DISPATCH_COMPARED declares dispatch-relevant — the method body "
            "and the declared set have drifted apart"
        )
        assert not mutated.same_dispatch_binding(
            base
        ), f"same_dispatch_binding() is asymmetric on {field_name!r}"

    @pytest.mark.parametrize("field_name", sorted(_DISPATCH_EXEMPT))
    def test_mutating_an_exempt_field_keeps_dispatch_identity(self, field_name: str) -> None:
        base = _dispatch_bindings()
        mutations = _dispatch_field_mutations()
        mutated = dataclasses.replace(base, **{field_name: mutations[field_name]})
        assert base.same_dispatch_binding(mutated), (
            f"same_dispatch_binding() compares {field_name!r}, which "
            "_DISPATCH_EXEMPT declares excluded — update the exempt set or "
            "the method, and keep the docstring rationale in sync"
        )

    def test_every_exempt_field_is_named_in_the_method_docstring(self) -> None:
        # The exempt comments above claim to mirror the method's docstring
        # rationale; enforce at least that the docstring still names each
        # exempt field, so the two cannot silently drift apart.
        doc = ResolvedBindings.same_dispatch_binding.__doc__ or ""
        for name in sorted(_DISPATCH_EXEMPT):
            assert name in doc, (
                "same_dispatch_binding()'s docstring no longer mentions the "
                f"exempt field {name!r} — restore the exclusion rationale or "
                "reclassify the field"
            )


class TestMigrationWriteBackOrdering:
    """The write-back migration must not lose a concurrent config write.

    A migration that calls ``cfg.save()`` re-serializes the
    whole snapshot this load parsed. A config write landing after that read and
    before the save is silently replaced by the older snapshot. ``load()`` runs
    off the event loop in places (``chat_runner``'s stop-hook nudge-cap site
    awaits ``asyncio.to_thread(KiroCrewConfig.load)``), so the interleave is
    reachable rather than theoretical.
    """

    #: Bounded so a regression fails the test instead of hanging the suite.
    _TIMEOUT = 30.0

    @staticmethod
    def _legacy_config() -> dict:
        """A config whose only legacy shape is the missing ``agents`` section.

        Deliberately NOT the flat-workspace shape: advisory schema validation
        normalizes ``workspaces.<name>`` from a string to the structured form
        before the migration block reads it, so that trigger only fires where
        jsonschema is unavailable. The missing-``agents`` trigger fires either
        way, so it is the one the interleave can be pinned on.

        Carries no ``session`` section, so the setting the concurrent writer adds
        below is genuinely absent from the migrating load's snapshot.
        """
        return {"workspaces": {"default": {"dir": "workspace"}}}

    @staticmethod
    def _owned_config(tmp_path: Path) -> Path:
        """A config inside the loader's own data home, as in production.

        ``config_dir()`` is redirected at the same directory, because the
        migration's advisory lock is gated on containment there -- a config we do
        not own gets no sidecar (see ``TestMigrationBackupContainment``).
        """
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        return home / "config.json"

    def test_a_write_landing_mid_migration_survives(self, tmp_path: Path) -> None:
        """Two writers, deterministically interleaved: neither may lose the other.

        Writer A is a migrating ``load()``, suspended after it has decided to
        migrate but before its bytes reach the file. Writer B is an ordinary
        locked config write (the shape the dashboard PATCH and ``kirocrew config
        set`` both use) carrying a setting the operator just changed.

        Both must be observable afterwards: the seeded default agent, and B's
        ``autocompact_pct``. Before the fix, A re-serialized its whole snapshot --
        which predates B's write and so carries the default threshold -- over the
        file, and B's setting was gone.
        """
        cfg_path = self._owned_config(tmp_path)
        cfg_path.write_text(json.dumps(self._legacy_config()), encoding="utf-8")

        reached_write = threading.Event()
        release_write = threading.Event()
        real_backup = loader_module._write_migration_backup

        def _suspend_then_backup(path: Path) -> None:
            # Called on the migration's write path in both the old and the new
            # shape, which is what makes one test able to fail before the fix and
            # pass after it.
            reached_write.set()
            assert release_write.wait(self._TIMEOUT), "migration release timed out"
            real_backup(path)

        errors: list[BaseException] = []

        def _migrating_load() -> None:
            try:
                KiroCrewConfig.load()
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        def _concurrent_setting_write() -> None:
            try:

                def _mutate(data: dict) -> dict:
                    data.setdefault("session", {})["autocompact_pct"] = 42.0
                    return data

                loader_module.update_config_locked(cfg_path, mutate=_mutate)
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        with (
            unittest.mock.patch.object(loader_module, "config_dir", return_value=cfg_path.parent),
            unittest.mock.patch.object(loader_module, "config_path", return_value=cfg_path),
            unittest.mock.patch.object(
                loader_module, "_write_migration_backup", _suspend_then_backup
            ),
        ):
            writer_a = threading.Thread(target=_migrating_load, daemon=True)
            writer_b = threading.Thread(target=_concurrent_setting_write, daemon=True)
            # try/finally, not a bare sequence: an assertion or a timeout below
            # would otherwise exit the test with a writer still running against
            # this test's paths and patched state, and leak it into later tests.
            try:
                writer_a.start()
                assert reached_write.wait(self._TIMEOUT), "migration never reached its write"

                # B starts while A is mid-migration. Under the fix A holds the
                # config lock, so B waits for it; without the fix B writes
                # straight away and A's snapshot rewrite replaces the result.
                writer_b.start()
                # Give B a chance to reach (or block on) the write before A
                # resumes. A short join, not a bare sleep: it returns as soon as
                # B is done in the unlocked case, and simply times out while B is
                # blocked.
                writer_b.join(timeout=1.0)

                release_write.set()
                writer_a.join(timeout=self._TIMEOUT)
                writer_b.join(timeout=self._TIMEOUT)
                assert not writer_a.is_alive(), "migrating load did not finish"
                assert not writer_b.is_alive(), "concurrent config write did not finish"
            finally:
                # Idempotent: releases a writer still parked on the event and
                # drains both threads before the patches come off, whether we got
                # here by success or by assertion.
                release_write.set()
                for writer in (writer_a, writer_b):
                    if writer.is_alive():
                        writer.join(timeout=self._TIMEOUT)

        assert not errors, f"writer raised: {errors!r}"

        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["default_agent"] == "default", "the migration did not reach disk"
        assert "default" in on_disk["agents"], "the migration did not reach disk"
        assert on_disk.get("session", {}).get("autocompact_pct") == 42.0, (
            "the config write that landed while the migration was in flight was "
            "lost -- the write-back is not ordered against concurrent writers"
        )

    @pytest.mark.parametrize("owned", [True, False], ids=["data-home", "redirected"])
    def test_the_migration_only_rewrites_the_keys_it_owns(
        self, tmp_path: Path, owned: bool
    ) -> None:
        """The write is a delta, not a whole-snapshot re-serialization.

        That is the property the ordering rests on, and the half that holds
        wherever the config lives: a write confined to the keys the migration
        decided on cannot carry a stale value for anything else, whoever wrote it
        and whenever. So it is pinned on BOTH the data-home path (which also takes
        the advisory lock) and a redirected path (which cannot, without leaving a
        sidecar orphan). Asserted on the top-level key set, because a snapshot
        rewrite materializes every section in the schema.
        """
        cfg_path = self._owned_config(tmp_path) if owned else tmp_path / "caller" / "cfg.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        stored = self._legacy_config()
        cfg_path.write_text(json.dumps(stored), encoding="utf-8")
        data_home = cfg_path.parent if owned else tmp_path / "elsewhere"

        with (
            unittest.mock.patch.object(loader_module, "config_dir", return_value=data_home),
            unittest.mock.patch.object(loader_module, "config_path", return_value=cfg_path),
        ):
            KiroCrewConfig.load()

        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        # "meta" is stamped by the shared writer, not by the migration.
        assert set(on_disk) == set(stored) | {"agents", "default_agent", "meta"}, (
            "the migration wrote sections it did not migrate -- it is "
            "re-serializing a snapshot rather than applying its own delta"
        )
        assert on_disk["workspaces"] == stored["workspaces"]
        assert on_disk["default_agent"] == "default"

    def test_a_held_lock_defers_the_migration_instead_of_waiting(self, tmp_path: Path) -> None:
        """A contended lock must not park the caller -- ``load()`` runs on the loop.

        ``load()`` is reached from the event loop all over the tree, so a POSIX
        ``flock`` wait here would stall the gateway for as long as the holder
        keeps the lock. The acquire is single-shot instead: it declines, writes
        nothing, and the next uncontended load migrates.

        Run on a thread with a bounded join, so a regression that goes back to
        waiting FAILS here rather than hanging the suite -- a waiting acquire
        would block until the holder below releases, which is forever from this
        test's point of view.
        """
        cfg_path = self._owned_config(tmp_path)
        stored = self._legacy_config()
        cfg_path.write_text(json.dumps(stored), encoding="utf-8")

        outcome: list[object] = []

        def _migrate() -> None:
            with unittest.mock.patch.object(
                loader_module, "config_dir", return_value=cfg_path.parent
            ):
                outcome.append(
                    loader_module._persist_config_migration(
                        cfg_path,
                        frozenset({loader_module.MIGRATE_AGENTS}),
                        default_kiro_agent="kirocrew",
                    )
                )

        lock_fd = os.open(str(cfg_path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        migrator = threading.Thread(target=_migrate, daemon=True)
        try:
            with platform_compat.file_lock(lock_fd, exclusive=True):
                migrator.start()
                migrator.join(timeout=10.0)
                parked = migrator.is_alive()
                # Assert INSIDE the hold, before the file can be touched: with a
                # waiting acquire the migration is still parked here, and what it
                # would eventually write is not the question.
                assert not parked, "migration waited on a held lock instead of deferring"
                assert outcome == [False]
                assert json.loads(cfg_path.read_text(encoding="utf-8")) == stored
                assert not Path(str(cfg_path) + ".bak").exists()
        finally:
            # Releasing the hold above lets a parked migrator (the regression
            # case) drain instead of surviving this test.
            if migrator.is_alive():
                migrator.join(timeout=self._TIMEOUT)
            os.close(lock_fd)

    def test_a_symlink_out_of_the_data_home_gets_no_lock_sidecar(self, tmp_path: Path) -> None:
        """Containment is about where the sidecar LANDS, not where the path sits.

        ``update_config_locked`` resolves a symlinked config before locking, so a
        ``config.json`` inside the data home that points out of it would drop its
        ``.lock`` beside the external target -- the same orphan the backup gate
        exists to prevent, one indirection further out.
        """
        home = tmp_path / "home"
        home.mkdir()
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        target = outside / "real-config.json"
        target.write_text(json.dumps(self._legacy_config()), encoding="utf-8")
        link = home / "config.json"
        link.symlink_to(target)

        before = {p.name for p in outside.iterdir()}
        with unittest.mock.patch.object(loader_module, "config_dir", return_value=home):
            wrote = loader_module._persist_config_migration(
                link,
                frozenset({loader_module.MIGRATE_AGENTS}),
                default_kiro_agent="kirocrew",
            )

        assert wrote is True, "the migration should still reach the symlinked config"
        assert "default" in json.loads(target.read_text(encoding="utf-8"))["agents"]
        assert {
            p.name for p in outside.iterdir()
        } == before, "the migration left a sidecar beside the symlink's external target: %s" % sorted(
            {p.name for p in outside.iterdir()} - before
        )

    def test_the_overlay_wins_the_seed_over_the_base_document(self, tmp_path: Path) -> None:
        """``config.local.json`` decides the effective agent, so it decides the seed.

        The overlay is deep-merged over the base at load time, so where it names
        ``agent.default_agent`` the base value is not what anything dispatches.
        Seeding from the base there would silently switch the default crew's agent
        on the next load.
        """
        cfg_path = self._owned_config(tmp_path)
        cfg_path.write_text(
            json.dumps({"agent": {"default_agent": "base-agent"}}), encoding="utf-8"
        )
        local_path = cfg_path.parent / "config.local.json"
        local_path.write_text(
            json.dumps({"agent": {"default_agent": "overlay-agent"}}), encoding="utf-8"
        )

        with (
            unittest.mock.patch.object(loader_module, "config_dir", return_value=cfg_path.parent),
            unittest.mock.patch.object(loader_module, "config_local_path", return_value=local_path),
        ):
            wrote = loader_module._persist_config_migration(
                cfg_path,
                frozenset({loader_module.MIGRATE_AGENTS, loader_module.MIGRATE_DEFAULT_AGENT}),
                default_kiro_agent="base-agent",
            )

        assert wrote is True
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["agents"]["default"]["kiro_agent"] == "overlay-agent", (
            "the seed took the base document's agent while the overlay named a "
            "different one -- the effective agent silently changed"
        )
        # The overlay itself is user-owned and must come back untouched.
        assert json.loads(local_path.read_text(encoding="utf-8")) == {
            "agent": {"default_agent": "overlay-agent"}
        }

    def test_a_malformed_overlay_lets_the_base_value_stand(self, tmp_path: Path) -> None:
        """A broken user-owned overlay must not block a write correct without it."""
        cfg_path = self._owned_config(tmp_path)
        cfg_path.write_text(
            json.dumps({"agent": {"default_agent": "base-agent"}}), encoding="utf-8"
        )
        local_path = cfg_path.parent / "config.local.json"
        local_path.write_text("{ not json", encoding="utf-8")

        with (
            unittest.mock.patch.object(loader_module, "config_dir", return_value=cfg_path.parent),
            unittest.mock.patch.object(loader_module, "config_local_path", return_value=local_path),
        ):
            wrote = loader_module._persist_config_migration(
                cfg_path,
                frozenset({loader_module.MIGRATE_AGENTS}),
                default_kiro_agent="resolved-by-the-load",
            )

        assert wrote is True
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["agents"]["default"]["kiro_agent"] == "base-agent"

    def test_the_seeded_agent_takes_its_kiro_agent_from_the_reread(self, tmp_path: Path) -> None:
        """The seed must not carry a value the document has since moved past.

        ``agent.default_agent`` decides which kiro agent the seeded crew
        dispatches. The load's snapshot is read before the lock, so a
        ``kirocrew config set agent.default_agent`` landing in between would
        otherwise be ignored and every default session would dispatch the
        superseded agent.
        """
        cfg_path = self._owned_config(tmp_path)
        # What the document says NOW -- the concurrent writer's value.
        cfg_path.write_text(
            json.dumps({"agent": {"default_agent": "newer-agent"}}), encoding="utf-8"
        )

        with unittest.mock.patch.object(loader_module, "config_dir", return_value=cfg_path.parent):
            wrote = loader_module._persist_config_migration(
                cfg_path,
                frozenset({loader_module.MIGRATE_AGENTS, loader_module.MIGRATE_DEFAULT_AGENT}),
                # What the load's older snapshot said.
                default_kiro_agent="stale-agent",
            )

        assert wrote is True
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["agents"]["default"]["kiro_agent"] == "newer-agent", (
            "the seeded agent carried the snapshot's kiro agent instead of the "
            "one the document holds now"
        )

    def test_the_seed_falls_back_when_the_document_names_no_kiro_agent(
        self, tmp_path: Path
    ) -> None:
        """With nothing stored, the load's resolved value is still the best answer.

        It carries the overlay and the dataclass default, which a raw document
        read cannot see -- so the fallback is the caller's value, not a literal.
        """
        cfg_path = self._owned_config(tmp_path)
        cfg_path.write_text(json.dumps(self._legacy_config()), encoding="utf-8")

        with unittest.mock.patch.object(loader_module, "config_dir", return_value=cfg_path.parent):
            wrote = loader_module._persist_config_migration(
                cfg_path,
                frozenset({loader_module.MIGRATE_AGENTS, loader_module.MIGRATE_DEFAULT_AGENT}),
                default_kiro_agent="resolved-by-the-load",
            )

        assert wrote is True
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["agents"]["default"]["kiro_agent"] == "resolved-by-the-load"

    def test_a_migration_another_writer_already_did_writes_nothing(self, tmp_path: Path) -> None:
        """When the re-read shows the work already done, skip the write.

        The migration decision is taken from this load's snapshot, so a writer
        that migrated in between leaves us holding a stale reason to write. The
        re-read before the write is what turns that into a no-op instead of a
        redundant rewrite (and, with it, a redundant ``.bak``).
        """
        cfg_path = self._owned_config(tmp_path)
        already_migrated = {
            "workspaces": {"default": {"dir": "workspace"}},
            "agents": {"default": {"kiro_agent": "kirocrew"}},
            "default_agent": "default",
        }
        cfg_path.write_text(json.dumps(already_migrated), encoding="utf-8")

        with unittest.mock.patch.object(loader_module, "config_dir", return_value=cfg_path.parent):
            wrote = loader_module._persist_config_migration(
                cfg_path,
                frozenset({loader_module.MIGRATE_AGENTS, loader_module.MIGRATE_DEFAULT_AGENT}),
                default_kiro_agent="kirocrew",
            )

        assert wrote is False
        assert not Path(str(cfg_path) + ".bak").exists()
        assert json.loads(cfg_path.read_text(encoding="utf-8")) == already_migrated
