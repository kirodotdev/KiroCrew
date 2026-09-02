"""Computer use — the KEYSTONE primary enable (``computer_use.json``).

Two properties, and both are security properties:

* **Every read fails soft to DISABLED.** A missing, unreadable, truncated or
  hand-mangled file must never mean "enabled" — this is a gate whose open position
  hands out the operator's whole desktop. Modeled on
  ``hooks.load_denied_commands_state()``, which has the identical contract.
* **The agent can neither read nor write it.** The leaf is on
  ``security._CREW_SECRET_LEAVES``, so ``is_sensitive_path`` blocks the tool path
  and ``is_sensitive_bash_command`` blocks every shell form. That is the single
  mechanism that makes the enable un-flippable by a prompt-injected agent, so it is
  asserted directly rather than assumed from the list membership.

The file deliberately lives OUTSIDE ``config.json``: that section carries
display/limit knobs only and has no ``enabled`` field, precisely so there is exactly
one place the feature can be turned on and it is not one the agent can reach.
"""

from __future__ import annotations

import json
import stat

import pytest

from kiro_crew import platform_compat, security
from kiro_crew.computer_use import enable_state
from kiro_crew.computer_use.types import (
    STATE_FILE_NAME,
    STATE_KEY_ALLOWED_APPS,
    STATE_KEY_ENABLED,
    STATE_KEY_EXTRA_DENIED_APPS,
    PolicyConfig,
    PolicyStateError,
)
from kiro_crew.config import loader as config_loader


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Redirect the keystone path at the LOADER, the way the suite does elsewhere.

    ``enable_state.computer_use_state_path`` resolves through the loader module
    attribute rather than a direct import, so this single patch redirects it — and a
    test that patched only ``config_dir`` would still work.
    """
    directory = tmp_path / "crew"
    directory.mkdir()
    monkeypatch.setattr(
        config_loader, "computer_use_state_path", lambda: directory / STATE_FILE_NAME
    )
    return directory


def _write_raw(state_dir, text: str) -> None:
    (state_dir / STATE_FILE_NAME).write_text(text, encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────────
# Fail-safe reads
# ──────────────────────────────────────────────────────────────────────────
class TestFailSafeToDisabled:
    def test_absent_file_is_disabled(self, state_dir):
        assert enable_state.load_state() == {}
        assert enable_state.is_enabled() is False

    def test_corrupt_json_is_disabled(self, state_dir):
        # A truncated write (or a killed editor) must not be interpreted generously.
        _write_raw(state_dir, '{"enabled": tr')
        assert enable_state.load_state() == {}
        assert enable_state.is_enabled() is False

    def test_empty_file_is_disabled(self, state_dir):
        _write_raw(state_dir, "")
        assert enable_state.is_enabled() is False

    @pytest.mark.parametrize("body", ["[1, 2]", '"enabled"', "true", "42", "null"])
    def test_non_object_json_is_disabled(self, state_dir, body):
        # Valid JSON that is not an object: ``.get`` would raise (or, for a list of
        # pairs, do something surprising), so the loader normalizes to ``{}``.
        _write_raw(state_dir, body)
        assert enable_state.load_state() == {}
        assert enable_state.is_enabled() is False

    def test_unreadable_file_is_disabled(self, state_dir):
        path = state_dir / STATE_FILE_NAME
        path.write_text(json.dumps({STATE_KEY_ENABLED: True}), encoding="utf-8")
        if not platform_compat.IS_POSIX:
            pytest.skip("mode bits do not deny the owner on Windows")
        platform_compat.chmod_safe(str(path), 0o000)
        try:
            assert enable_state.is_enabled() is False
        finally:
            platform_compat.chmod_safe(str(path), 0o600)

    def test_missing_directory_is_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            config_loader,
            "computer_use_state_path",
            lambda: tmp_path / "nope" / "deeper" / STATE_FILE_NAME,
        )
        assert enable_state.is_enabled() is False

    @pytest.mark.parametrize("value", ["false", "true", 1, 0, "yes", [], {}, None])
    def test_only_a_real_json_true_enables(self, state_dir, value):
        """Strict identity against ``True``, not a truthiness test.

        A hand-edited ``"enabled": "false"`` is a truthy non-empty STRING; a
        truthiness test would enable desktop control on a file whose author clearly
        meant the opposite.
        """
        _write_raw(state_dir, json.dumps({STATE_KEY_ENABLED: value}))
        assert enable_state.is_enabled() is False, value

    def test_json_true_enables(self, state_dir):
        _write_raw(state_dir, json.dumps({STATE_KEY_ENABLED: True}))
        assert enable_state.is_enabled() is True

    def test_missing_key_is_disabled(self, state_dir):
        _write_raw(state_dir, json.dumps({STATE_KEY_ALLOWED_APPS: ["Preview"]}))
        assert enable_state.is_enabled() is False

    def test_caller_supplied_state_avoids_a_second_read(self, state_dir):
        # One dispatch should not read the keystone twice.
        assert enable_state.is_enabled({STATE_KEY_ENABLED: True}) is True
        assert enable_state.is_enabled({}) is False


# ──────────────────────────────────────────────────────────────────────────
# Policy config coercion
# ──────────────────────────────────────────────────────────────────────────
class TestPolicyConfig:
    def test_lists_are_normalized(self, state_dir):
        _write_raw(
            state_dir,
            json.dumps(
                {
                    STATE_KEY_ENABLED: True,
                    STATE_KEY_ALLOWED_APPS: ["  Preview  ", "com.Apple.Pages"],
                    STATE_KEY_EXTRA_DENIED_APPS: ["Slack"],
                }
            ),
        )
        cfg = enable_state.load_policy_config()
        assert cfg.allowed_apps == ("preview", "com.apple.pages")
        assert cfg.extra_denied_apps == ("slack",)

    def test_an_absent_allow_list_is_still_the_documented_no_restriction_case(self):
        """``None``/absent is the ONE shape that legitimately yields ``()``."""
        cfg = PolicyConfig.from_state({STATE_KEY_ALLOWED_APPS: None})
        assert cfg.allowed_apps == ()

    @pytest.mark.parametrize(
        "raw",
        [
            [None, 7, {"x": 1}, "", "   "],  # every entry unusable → () → UNRESTRICTED
            [{"name": "Preview"}],  # the reviewer's shape: a list of dicts
            ["Preview", 7],  # partially usable — still refused, see below
        ],
    )
    def test_junk_INSIDE_a_list_also_REFUSES(self, raw):
        """A malformed ENTRY refuses too — the list check alone was not enough.

        The earlier fix only caught a non-list (``"Preview"`` instead of
        ``["Preview"]``). But ``[{"name": "Preview"}]`` IS a list: every entry is
        dropped, the result is ``()``, and ``check_app`` reads that as "every app
        that is not built-in denied" — the same typo-widens-the-ceiling bug one
        level down.

        Refusal is per-entry rather than only when the result would be empty. A
        partially-usable list still means the operator wrote something we cannot
        interpret in the field that decides which applications may be driven, and
        for a ceiling the loud fail-closed answer beats silently enforcing a
        narrower list than the one they think they wrote.
        """
        with pytest.raises(PolicyStateError):
            PolicyConfig.from_state({STATE_KEY_ALLOWED_APPS: raw})

    @pytest.mark.parametrize("raw", [[None, 7, {"x": 1}], [{"name": "Slack"}]])
    def test_junk_inside_a_DENY_list_still_degrades(self, raw):
        """Same input shape, opposite disposition — dropping a deny entry cannot grant."""
        cfg = PolicyConfig.from_state({STATE_KEY_EXTRA_DENIED_APPS: raw})
        assert cfg.extra_denied_apps == ()

    @pytest.mark.parametrize("raw", ["scalar", 42, {"a": 1}, True])
    def test_a_malformed_allow_list_REFUSES_rather_than_failing_open(self, raw):
        """The reviewer-found fail-open: ``()`` means UNRESTRICTED for this field.

        Writing ``"Preview"`` instead of ``["Preview"]`` used to coerce to ``()``,
        which ``check_app`` reads as "every app that is not built-in denied" — a
        typo silently converting a restriction into a grant. A present-but-malformed
        allow-list now raises, and the dispatcher turns that into a refusal.
        """
        with pytest.raises(PolicyStateError):
            PolicyConfig.from_state({STATE_KEY_ALLOWED_APPS: raw})

    @pytest.mark.parametrize("raw", ["scalar", 42, {"a": 1}])
    def test_a_malformed_DENY_list_still_degrades(self, raw):
        """Opposite disposition, on purpose: an empty deny-list denies nothing extra.

        ``extra_denied_apps`` can only ADD to the built-in floor, so coercing junk to
        empty loses an operator addition but cannot grant anything the floor denies —
        degrading is already the fail-closed direction there.
        """
        cfg = PolicyConfig.from_state({STATE_KEY_EXTRA_DENIED_APPS: raw})
        assert cfg.extra_denied_apps == ()

    def test_empty_allow_list_means_everything_not_denied(self, state_dir):
        # The allow-list is an optional NARROWING, not the primary enable.
        _write_raw(state_dir, json.dumps({STATE_KEY_ENABLED: True}))
        assert enable_state.load_policy_config().allowed_apps == ()

    def test_extra_denied_can_only_add(self):
        # There is deliberately no mechanism to REMOVE a built-in denylist entry, so
        # the floor cannot be edited away from the dashboard.
        from kiro_crew.computer_use import policy
        from kiro_crew.computer_use.types import AppRef

        # Retargeted onto ``kirocrew_self`` — the ONE entry the floor still carries
        # (driving our own Settings UI would route around the keystone that holds
        # the primary enable). The terminal / password-manager / system-settings
        # entries were removed by product decision.
        ours = AppRef(name="Kiro Crew", pid=1, bundle_id="dev.kiro.crew")
        cfg = PolicyConfig(allowed_apps=("dev.kiro.crew",), extra_denied_apps=())
        # Even an explicit operator allow-list entry cannot lift the built-in floor.
        assert policy.check_app(ours, cfg) is not None


# ──────────────────────────────────────────────────────────────────────────
# Writes
# ──────────────────────────────────────────────────────────────────────────
class TestSaveState:
    def test_save_then_load_round_trip(self, state_dir):
        enable_state.save_state({STATE_KEY_ENABLED: True, STATE_KEY_ALLOWED_APPS: ["Preview"]})
        assert enable_state.is_enabled() is True
        assert enable_state.load_policy_config().allowed_apps == ("preview",)

    def test_saved_file_is_owner_only(self, state_dir):
        enable_state.save_state({STATE_KEY_ENABLED: False})
        if not platform_compat.IS_POSIX:
            pytest.skip("POSIX mode bits")
        mode = stat.S_IMODE((state_dir / STATE_FILE_NAME).stat().st_mode)
        assert mode == 0o600

    def test_save_rejects_a_non_dict(self, state_dir):
        with pytest.raises(ValueError):
            enable_state.save_state(["enabled"])  # type: ignore[arg-type]

    def test_save_replaces_wholesale(self, state_dir):
        # Callers must pass the COMPLETE object (read-modify-write). Documented so a
        # handler cannot assume a merge and silently drop a field.
        enable_state.save_state({STATE_KEY_ENABLED: True, STATE_KEY_ALLOWED_APPS: ["Preview"]})
        enable_state.save_state({STATE_KEY_ENABLED: True})
        assert enable_state.load_state() == {STATE_KEY_ENABLED: True}

    def test_missing_parent_directory_is_created(self, tmp_path, monkeypatch):
        # ``atomic_write`` mkdirs the parent, so a first-run write into a fresh data
        # home succeeds rather than needing the dashboard to pre-create it.
        monkeypatch.setattr(
            config_loader,
            "computer_use_state_path",
            lambda: tmp_path / "fresh" / "home" / STATE_FILE_NAME,
        )
        enable_state.save_state({STATE_KEY_ENABLED: True})
        assert enable_state.is_enabled() is True

    def test_write_failure_raises_rather_than_failing_soft(self, state_dir, monkeypatch):
        """The read path is the ONLY fail-soft direction.

        A silent write failure would leave the operator believing they enabled (or
        disabled) the feature when they did not — so the HTTP handler must get a real
        error to report. Simulated at the ``atomic_write`` boundary because the real
        failures (a full volume, a read-only mount, an EPERM rename) are not
        reproducible in a tmp dir.
        """

        def _boom(*args, **kwargs):
            raise OSError("no space left on device")

        monkeypatch.setattr(enable_state, "atomic_write", _boom)
        with pytest.raises(OSError):
            enable_state.save_state({STATE_KEY_ENABLED: True})
        # And nothing was left half-written: the reader still says DISABLED.
        assert enable_state.is_enabled() is False


# ──────────────────────────────────────────────────────────────────────────
# THE keystone assertions
# ──────────────────────────────────────────────────────────────────────────
class TestKeystoneProtection:
    def test_leaf_is_registered_on_the_secret_floor(self):
        assert STATE_FILE_NAME in security._CREW_SECRET_LEAVES

    @pytest.mark.parametrize(
        "path",
        [
            "~/.kiro/crew/computer_use.json",
            "~/.kirocrew/computer_use.json",
        ],
    )
    def test_agent_cannot_read_or_write_it_on_the_tool_path(self, path):
        # BOTH gates: reading the file tells the agent whether it is being watched
        # and which apps are allow-listed; writing it flips the ceiling.
        assert security.is_sensitive_path(path) is True
        assert security.is_sensitive_write_path(path) is True

    @pytest.mark.parametrize(
        "command",
        [
            "cat ~/.kiro/crew/computer_use.json",
            "less ~/.kiro/crew/computer_use.json",
            "echo '{\"enabled\":true}' > ~/.kiro/crew/computer_use.json",
            "echo x >> ~/.kiro/crew/computer_use.json",
            "tee ~/.kiro/crew/computer_use.json",
            "cp /tmp/evil.json ~/.kiro/crew/computer_use.json",
            "python -c \"open('~/.kiro/crew/computer_use.json','w')\"",
        ],
    )
    def test_agent_cannot_reach_it_from_a_shell(self, command):
        # The shell plane needs its own matcher: a bash body has no ``path`` param
        # for the tool-path gate to inspect.
        assert security.is_sensitive_bash_command(command), command

    def test_archive_extraction_into_the_trust_root_is_blocked(self):
        # The ``tar -C`` / ``unzip -d`` drop would land a replacement file without
        # ever naming it.
        for command in (
            "tar -xf payload.tar -C ~/.kiro/crew",
            "unzip payload.zip -d ~/.kiro/crew",
        ):
            assert security.is_sensitive_bash_command(command), command

    def test_the_enable_is_not_in_config_json(self):
        """The deliberate asymmetry, asserted so a refactor cannot "tidy" it away.

        ``config.json``'s ``computer_use`` section is agent-readable and only
        WRITE-protected; putting ``enabled`` there would make the ceiling readable
        and would add a second place the feature can be turned on.
        """
        from kiro_crew.config.loader import ComputerUseConfig

        fields = {f.name for f in __import__("dataclasses").fields(ComputerUseConfig)}
        assert "enabled" not in fields

    def test_state_path_honors_the_data_home_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setattr("kiro_crew.config.paths._resolved_home", None)
        assert str(tmp_path) in str(enable_state.computer_use_state_path())

    def test_state_path_delegates_to_the_single_canonical_definition(self):
        # One definition, so a future move of the data home cannot leave the two
        # spellings pointing at different files.
        assert enable_state.computer_use_state_path() == config_loader.computer_use_state_path()

    def test_state_path_tolerates_a_string_returning_loader(self, tmp_path, monkeypatch):
        # Defensive: the loader is monkeypatched all over the suite, sometimes with a
        # lambda returning a str. A ``Path`` must come back either way.
        from pathlib import Path

        monkeypatch.setattr(
            config_loader,
            "computer_use_state_path",
            lambda: str(tmp_path / STATE_FILE_NAME),
        )
        assert isinstance(enable_state.computer_use_state_path(), Path)
