"""Tests for Mochi's settings store.

New module (no upstream counterpart), so these are intent tests rather than
differential ones. What matters:

- defaults appear when the file is absent, empty, corrupt, or wrong-typed —
  a broken settings file must never take the app down;
- ``petInstance`` is stored opaquely (NOT validated against the live instance
  list) so a temporarily-absent instance survives a restart;
- unknown keys are dropped rather than persisted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.mochi.settings import (
    MAX_PET_NAME_LEN,
    MODE_ACTIVE,
    MODE_QUIET,
    PACK_GHOST,
    PACK_MOCHI,
    SELF_INSTANCE,
    load_settings,
    save_settings,
    settings_path,
)


class TestLoadSettings:
    def test_absent_file_yields_defaults(self, tmp_path):
        assert load_settings(tmp_path)["petInstance"] == SELF_INSTANCE

    def test_corrupt_file_degrades_to_defaults(self, tmp_path):
        settings_path(tmp_path).write_text("{not json")
        assert load_settings(tmp_path)["petInstance"] == SELF_INSTANCE

    def test_non_object_json_degrades_to_defaults(self, tmp_path):
        settings_path(tmp_path).write_text('["a", "list"]')
        assert load_settings(tmp_path)["petInstance"] == SELF_INSTANCE

    @pytest.mark.parametrize("bad", [None, "", 42, True, {}, []])
    def test_non_string_or_empty_pet_instance_reads_as_unset(self, tmp_path, bad):
        settings_path(tmp_path).write_text(json.dumps({"petInstance": bad}))
        assert load_settings(tmp_path)["petInstance"] == SELF_INSTANCE

    def test_reads_a_stored_instance_id(self, tmp_path):
        settings_path(tmp_path).write_text(json.dumps({"petInstance": "inst-7"}))
        assert load_settings(tmp_path)["petInstance"] == "inst-7"


class TestSaveSettings:
    def test_round_trips(self, tmp_path):
        assert save_settings(tmp_path, {"petInstance": "inst-7"})["petInstance"] == "inst-7"
        assert load_settings(tmp_path)["petInstance"] == "inst-7"

    def test_creates_the_data_dir(self, tmp_path):
        nested = tmp_path / "does" / "not" / "exist"
        save_settings(nested, {"petInstance": "inst-1"})
        assert settings_path(nested).exists()

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX permission bits only")
    def test_file_is_owner_only(self, tmp_path):
        save_settings(tmp_path, {"petInstance": "inst-1"})
        assert settings_path(tmp_path).stat().st_mode & 0o777 == 0o600

    @pytest.mark.parametrize("clearing", [None, ""])
    def test_clearing_resets_to_self(self, tmp_path, clearing):
        save_settings(tmp_path, {"petInstance": "inst-7"})
        assert save_settings(tmp_path, {"petInstance": clearing})["petInstance"] == SELF_INSTANCE

    def test_rejects_a_non_string_instance(self, tmp_path):
        with pytest.raises(ValueError):
            save_settings(tmp_path, {"petInstance": 5})

    def test_unknown_keys_are_dropped(self, tmp_path):
        save_settings(tmp_path, {"petInstance": "inst-1", "evil": "x" * 100})
        stored = json.loads(settings_path(tmp_path).read_text())
        assert stored["petInstance"] == "inst-1"

    def test_empty_update_preserves_current_state(self, tmp_path):
        save_settings(tmp_path, {"petInstance": "inst-7"})
        assert save_settings(tmp_path, {})["petInstance"] == "inst-7"

    def test_unknown_instance_ids_are_stored_verbatim(self, tmp_path):
        """Deliberate: instances expire (TTL) or drop (tunnel down). A stored id
        must survive an absent instance — the consumer resolves it at open time
        and falls back to self."""
        save_settings(tmp_path, {"petInstance": "long-gone-instance"})
        assert load_settings(tmp_path)["petInstance"] == "long-gone-instance"


class TestActiveAppearance:
    """The pet's single identity key: the pack id IS the character.

    The original had a second key (``pet.character``) and migrated it away
    because the two could disagree; this port briefly re-created that split as
    ``avatar``, which let the art and the prompt describe different creatures.
    These tests pin the collapsed model AND the migration off the old one.
    """

    def test_defaults_to_the_cat_pack(self, tmp_path: Path) -> None:
        """No first-run gate: enabling the app must produce a companion at once.

        The default is a real pack id rather than "" because with one key there
        is nowhere else for "which built-in" to live.
        """
        assert load_settings(tmp_path)["activeAppearance"] == PACK_MOCHI

    def test_both_built_ins_are_accepted(self, tmp_path: Path) -> None:
        for pack in (PACK_GHOST, PACK_MOCHI):
            assert save_settings(tmp_path, {"activeAppearance": pack})["activeAppearance"] == pack

    def test_switching_is_allowed(self, tmp_path: Path) -> None:
        """Reversible by design — right-click > Avatars, or the settings page."""
        save_settings(tmp_path, {"activeAppearance": PACK_GHOST})
        assert (
            save_settings(tmp_path, {"activeAppearance": PACK_MOCHI})["activeAppearance"]
            == PACK_MOCHI
        )

    def test_a_user_pack_id_is_accepted(self, tmp_path: Path) -> None:
        """Imported packs are first-class: the key is not an enum of built-ins."""
        saved = save_settings(tmp_path, {"activeAppearance": "some-imported-pack"})
        assert saved["activeAppearance"] == "some-imported-pack"

    def test_clearing_normalizes_to_the_default_pack(self, tmp_path: Path) -> None:
        """Empty must resolve HERE, not at every read site."""
        save_settings(tmp_path, {"activeAppearance": PACK_GHOST})
        for cleared in ("", None):
            assert (
                save_settings(tmp_path, {"activeAppearance": cleared})["activeAppearance"]
                == PACK_MOCHI
            )

    def test_a_non_string_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            save_settings(tmp_path, {"activeAppearance": 7})

    def test_migrates_a_legacy_avatar_key(self, tmp_path: Path) -> None:
        """A config from the two-key era folds into the single key."""
        settings_path(tmp_path).write_text('{"avatar": "ghost"}')
        assert load_settings(tmp_path)["activeAppearance"] == PACK_GHOST

    def test_migrates_a_legacy_empty_appearance(self, tmp_path: Path) -> None:
        """`activeAppearance: ""` used to mean "whatever avatar says"."""
        settings_path(tmp_path).write_text('{"avatar": "ghost", "activeAppearance": ""}')
        assert load_settings(tmp_path)["activeAppearance"] == PACK_GHOST

    def test_an_explicit_pack_beats_a_stale_avatar(self, tmp_path: Path) -> None:
        """A user who chose a custom pack must not be dragged back to a built-in."""
        settings_path(tmp_path).write_text('{"avatar": "ghost", "activeAppearance": "my-own-pack"}')
        assert load_settings(tmp_path)["activeAppearance"] == "my-own-pack"

    def test_an_unknown_legacy_avatar_falls_back_to_the_default(self, tmp_path: Path) -> None:
        settings_path(tmp_path).write_text('{"avatar": "dragon"}')
        assert load_settings(tmp_path)["activeAppearance"] == PACK_MOCHI

    def test_the_avatar_key_is_not_persisted(self, tmp_path: Path) -> None:
        """One concept, one key: the old one must not survive a round trip."""
        settings_path(tmp_path).write_text('{"avatar": "ghost"}')
        assert "avatar" not in load_settings(tmp_path)


class TestMode:
    def test_defaults_to_quiet(self, tmp_path: Path) -> None:
        """A companion that interrupts by default gets itself turned off."""
        assert load_settings(tmp_path)["mode"] == MODE_QUIET

    def test_user_can_opt_into_more_activity(self, tmp_path: Path) -> None:
        assert save_settings(tmp_path, {"mode": MODE_ACTIVE})["mode"] == MODE_ACTIVE

    def test_unknown_mode_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            save_settings(tmp_path, {"mode": "loud"})


class TestCatPreset:
    """A Mochi-only breed colorway — part of the avatar, not a global theme."""

    def test_unset_by_default(self, tmp_path: Path) -> None:
        assert load_settings(tmp_path)["catPreset"] is None

    def test_round_trips(self, tmp_path: Path) -> None:
        assert save_settings(tmp_path, {"catPreset": "tabby"})["catPreset"] == "tabby"

    def test_can_be_cleared(self, tmp_path: Path) -> None:
        save_settings(tmp_path, {"catPreset": "tabby"})
        assert save_settings(tmp_path, {"catPreset": None})["catPreset"] is None


class TestAllowMcpServers:
    def test_off_by_default(self, tmp_path: Path) -> None:
        """Mochi reaching the user's installed MCP servers is opt-in."""
        assert load_settings(tmp_path)["allowMcpServers"] is False

    def test_round_trips(self, tmp_path: Path) -> None:
        assert save_settings(tmp_path, {"allowMcpServers": True})["allowMcpServers"] is True

    def test_non_boolean_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            save_settings(tmp_path, {"allowMcpServers": "yes"})


class TestPetName:
    """Naming the companion — an original feature with real sentimental value."""

    def test_empty_by_default_so_it_follows_the_avatar(self, tmp_path: Path) -> None:
        # Not "Mochi": an empty name lets the ghost introduce itself as Kiro.
        assert load_settings(tmp_path)["petName"] == ""

    def test_round_trips(self, tmp_path: Path) -> None:
        assert save_settings(tmp_path, {"petName": "Biscuit"})["petName"] == "Biscuit"

    def test_surrounding_whitespace_is_trimmed_not_rejected(self, tmp_path: Path) -> None:
        # A pasted trailing space is a slip, not worth failing the whole save.
        assert save_settings(tmp_path, {"petName": "  Biscuit "})["petName"] == "Biscuit"

    def test_overlong_name_is_truncated(self, tmp_path: Path) -> None:
        # The name renders in a 320px panel and reaches the agent prompt.
        stored = save_settings(tmp_path, {"petName": "x" * 200})["petName"]
        assert len(stored) == MAX_PET_NAME_LEN

    def test_can_be_cleared_back_to_following_the_avatar(self, tmp_path: Path) -> None:
        save_settings(tmp_path, {"petName": "Biscuit"})
        assert save_settings(tmp_path, {"petName": None})["petName"] == ""

    def test_non_string_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            save_settings(tmp_path, {"petName": 42})

    def test_an_overlong_name_already_on_disk_is_truncated_on_read(self, tmp_path: Path) -> None:
        settings_path(tmp_path).write_text('{"petName": "' + "y" * 500 + '"}')
        assert len(load_settings(tmp_path)["petName"]) == MAX_PET_NAME_LEN


class TestSilentSubagents:
    def test_off_by_default(self, tmp_path: Path) -> None:
        assert load_settings(tmp_path)["silentSubagents"] is False

    def test_round_trips(self, tmp_path: Path) -> None:
        assert save_settings(tmp_path, {"silentSubagents": True})["silentSubagents"] is True

    def test_non_boolean_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            save_settings(tmp_path, {"silentSubagents": "yes"})


class TestLanguage:
    """The pet overlay already read `c.language` before this key existed, so the
    setting had a consumer and no writer — the language selector the original
    shipped had nowhere to persist to."""

    def test_defaults_to_follow_system(self, tmp_path: Path) -> None:
        assert load_settings(tmp_path)["language"] == ""

    def test_round_trips_a_supported_tag(self, tmp_path: Path) -> None:
        assert save_settings(tmp_path, {"language": "zh-CN"})["language"] == "zh-CN"
        assert load_settings(tmp_path)["language"] == "zh-CN"

    def test_none_and_empty_both_mean_follow_system(self, tmp_path: Path) -> None:
        save_settings(tmp_path, {"language": "en"})
        assert save_settings(tmp_path, {"language": None})["language"] == ""
        save_settings(tmp_path, {"language": "en"})
        assert save_settings(tmp_path, {"language": ""})["language"] == ""

    def test_well_formed_tag_is_accepted_without_a_backend_list(self, tmp_path: Path) -> None:
        # Shape, not membership. The frontend's SUPPORTED_LANGUAGES registry is the
        # single source of truth, so adding a language must stay a pure frontend
        # data change; a backend allow-list would make it a two-place edit and
        # would reject a language the frontend already ships. A well-formed tag
        # with no catalog is safe because resolveLanguage() falls back on its own.
        for tag in ("fr", "de", "pt-BR", "zh-Hans-CN"):
            assert save_settings(tmp_path, {"language": tag})["language"] == tag

    def test_malformed_tag_is_rejected(self, tmp_path: Path) -> None:
        # Still validated: a value that cannot be a language tag is a client bug,
        # and persisting it would put an unresolvable code in front of i18next.
        for bad in ("not a tag", "e", "en_US", "../etc/passwd", "en-"):
            with pytest.raises(ValueError):
                save_settings(tmp_path, {"language": bad})

    def test_unreadable_stored_value_falls_back(self, tmp_path: Path) -> None:
        (tmp_path / "mochi-settings.json").write_text('{"language": 7}')
        assert load_settings(tmp_path)["language"] == ""

    def test_malformed_stored_value_falls_back(self, tmp_path: Path) -> None:
        (tmp_path / "mochi-settings.json").write_text('{"language": "not a tag"}')
        assert load_settings(tmp_path)["language"] == ""


class TestShortcuts:
    """The original shipped editable accelerators; these pin the store half."""

    def test_defaults_match_the_shell(self, tmp_path: Path) -> None:
        # A divergence between this table and shortcuts.js ACCELERATORS would mean
        # Settings shows one key while a different one is actually bound. The
        # values are platform-dependent (see _default_shortcuts), so this asserts
        # against that function rather than restating one platform's strings.
        from kiro_crew.apps.builtins.mochi.settings import _default_shortcuts

        assert load_settings(tmp_path)["shortcuts"] == _default_shortcuts()

    def test_rebinding_one_action_leaves_the_other_alone(self, tmp_path: Path) -> None:
        from kiro_crew.apps.builtins.mochi.settings import _default_shortcuts

        out = save_settings(tmp_path, {"shortcuts": {"toggleWindow": "CommandOrControl+Shift+K"}})
        assert out["shortcuts"]["toggleWindow"] == "CommandOrControl+Shift+K"
        # The untouched action keeps its PLATFORM default (Alt+Shift+H on
        # Linux/Windows) — restating the macOS string here failed CI's Linux run.
        assert out["shortcuts"]["hideAll"] == _default_shortcuts()["hideAll"]

    def test_empty_string_unbinds(self, tmp_path: Path) -> None:
        assert save_settings(tmp_path, {"shortcuts": {"hideAll": ""}})["shortcuts"]["hideAll"] == ""

    def test_none_unbinds(self, tmp_path: Path) -> None:
        assert (
            save_settings(tmp_path, {"shortcuts": {"hideAll": None}})["shortcuts"]["hideAll"] == ""
        )

    def test_unknown_action_is_dropped_not_persisted(self, tmp_path: Path) -> None:
        # `voiceInput` is the original's fourth action; push-to-talk is not ported,
        # so it must not grow the file. (screenCapture IS a real action now.)
        out = save_settings(tmp_path, {"shortcuts": {"voiceInput": "Alt+Space"}})
        assert "voiceInput" not in out["shortcuts"]

    @pytest.mark.parametrize(
        "bad",
        [
            "Shift",  # modifiers only — not a usable global shortcut
            "K",  # no modifier
            "CommandOrControl+Shift+K+J",  # two non-modifier keys
            "CommandOrControl++K",  # empty segment
        ],
    )
    def test_malformed_accelerator_is_rejected_on_write(self, tmp_path: Path, bad: str) -> None:
        # A WRITE raises rather than silently keeping the old value: the user must
        # learn their combination was refused.
        with pytest.raises(ValueError):
            save_settings(tmp_path, {"shortcuts": {"toggleWindow": bad}})

    def test_a_bad_stored_value_costs_only_that_action(self, tmp_path: Path) -> None:
        # LOAD degrades instead of raising — one corrupt entry must not take away
        # the user's other binding or the whole settings file.
        (tmp_path / "mochi-settings.json").write_text(
            '{"shortcuts": {"toggleWindow": "Shift", "hideAll": "Option+Shift+J"}}'
        )
        from kiro_crew.apps.builtins.mochi.settings import _default_shortcuts

        loaded = load_settings(tmp_path)["shortcuts"]
        assert loaded["toggleWindow"] == _default_shortcuts()["toggleWindow"]
        # Normalised on the way in: `Option` is macOS-only, `Alt` is portable.
        assert loaded["hideAll"] == "Alt+Shift+J"

    def test_non_object_shortcuts_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            save_settings(tmp_path, {"shortcuts": "CommandOrControl+Shift+M"})


class TestCrossPlatformAccelerators:
    """The accelerator defaults and normalisation must work on Windows too.

    `CommandOrControl` is already portable, but two things were not: `Option` is a
    macOS-ONLY token (a binding captured on a Mac would fail to register on
    Windows, and the settings UI reports that as "already taken"), and
    Ctrl+Shift+<letter> defaults would silently steal shortcuts that Windows
    applications use, because globalShortcut registration is exclusive.
    """

    def test_option_is_normalised_to_alt(self) -> None:
        from kiro_crew.apps.builtins.mochi.settings import validate_accelerator

        assert validate_accelerator("Option+Shift+X") == "Alt+Shift+X"

    def test_short_aliases_are_expanded(self) -> None:
        from kiro_crew.apps.builtins.mochi.settings import validate_accelerator

        assert validate_accelerator("CmdOrCtrl+Shift+M") == "CommandOrControl+Shift+M"

    def test_defaults_avoid_ctrl_shift_off_darwin(self, monkeypatch) -> None:
        from kiro_crew.apps.builtins.mochi import settings as mod

        monkeypatch.setattr(mod.sys, "platform", "win32")
        defaults = mod._default_shortcuts()
        for accelerator in defaults.values():
            # Ctrl+Shift+<letter> is heavily used by Windows applications, and
            # Ctrl+Alt+<letter> types a character on AltGr layouts.
            assert "CommandOrControl" not in accelerator
            assert accelerator.startswith("Alt+Shift+")

    def test_defaults_use_command_on_darwin(self, monkeypatch) -> None:
        from kiro_crew.apps.builtins.mochi import settings as mod

        monkeypatch.setattr(mod.sys, "platform", "darwin")
        for accelerator in mod._default_shortcuts().values():
            assert accelerator.startswith("CommandOrControl+Shift+")

    def test_screen_capture_is_a_real_action(self) -> None:
        from kiro_crew.apps.builtins.mochi.settings import (
            SHORTCUT_ACTIONS,
            _default_shortcuts,
        )

        # A shortcut action with no default (or a default with no action) is the
        # silent class of bug: the key is advertised and never bound.
        assert "screenCapture" in SHORTCUT_ACTIONS
        assert set(_default_shortcuts()) == set(SHORTCUT_ACTIONS)


class TestSettingsConcurrency:
    """The load-modify-write in ``save_settings`` must be serialised by the
    cross-process lock so two concurrent saves cannot drop each other's update.
    """

    def test_concurrent_saves_do_not_lose_updates(self, tmp_path, monkeypatch) -> None:
        import threading
        import time

        from kiro_crew.apps.builtins.mochi import settings as mod

        data_dir = tmp_path
        save_settings(data_dir, {"petName": "seed"})

        real_load = mod.load_settings

        def slow_load(dd):
            # Widen the read->write window so the two RMW sequences overlap;
            # this runs INSIDE the lock, so the lock (if held) forces them to
            # serialise instead of both reading the same stale snapshot.
            result = real_load(dd)
            time.sleep(0.2)
            return result

        monkeypatch.setattr(mod, "load_settings", slow_load)

        errors: list[Exception] = []

        def worker(key: str, val: str) -> None:
            try:
                save_settings(data_dir, {key: val})
            except Exception as exc:  # pragma: no cover - surfaced via assert
                errors.append(exc)

        t1 = threading.Thread(target=worker, args=("petName", "alice"))
        t2 = threading.Thread(target=worker, args=("bgModel", "claude"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors
        final = json.loads(settings_path(data_dir).read_text(encoding="utf-8"))
        # Without the lock the later writer would revert one key to its stale
        # snapshot value; the lock guarantees BOTH updates survive.
        assert final["petName"] == "alice"
        assert final["bgModel"] == "claude"

    def test_lock_is_a_sibling_file(self, tmp_path) -> None:
        from kiro_crew.apps.builtins.mochi.settings import (
            settings_mutation,
            settings_path,
        )

        with settings_mutation(tmp_path):
            pass
        lock = settings_path(tmp_path).with_name(settings_path(tmp_path).name + ".lock")
        # A sibling .lock, never the data file itself (flock follows the inode,
        # so locking the atomically-replaced file would guard a vanishing inode).
        assert lock.exists()
        assert lock != settings_path(tmp_path)
