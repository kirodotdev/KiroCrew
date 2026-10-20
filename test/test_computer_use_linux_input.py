"""The Linux INPUT layer (:mod:`input_linux`): the keysym table, the AT-SPI action
ladder, keystroke synthesis, and the modifier refusals.

Runs on EVERY platform. ``linux_ffi`` is reached only through module-level seams
these tests monkeypatch, so a macOS or Windows shard exercises the same decision
logic that runs on Linux — which matters because the branches with the worst
consequences (a FAILED action followed by a different one, a chord with a modifier
stuck down, a wrong keysym typed into a live window) cannot be produced on demand
against a real desktop.

The properties pinned here are the ones whose violation is a wrong gesture that
SUCCEEDS, not a refusal:

* **Every canonical key has a keysym.** A key ``keymap`` accepts but the table
  lacks is refused at the driver AFTER the chokepoint said yes.
* **The action ladder never falls through a FAILURE.** A present-but-failed action
  stops the ladder; only an ABSENT action advances it.
* **A chord releases its modifiers in reverse**, so none is left stuck down.
* **``capslock`` and ``fn`` are refused**, with the reason and the alternative —
  ``capslock`` toggles a machine-wide lock that outlives the call.
"""

from __future__ import annotations

import pytest

from kiro_crew.computer_use import input_linux, keymap, linux_ffi
from kiro_crew.computer_use.keymap import KeySpec


# ──────────────────────────────────────────────────────────────────────────
# The keysym table covers the whole vocabulary
# ──────────────────────────────────────────────────────────────────────────
class TestTheKeysymTableCoversEveryCanonicalKey:
    def test_every_canonical_keymap_name_has_a_keysym(self) -> None:
        """A key ``keymap`` accepts but this table lacks would be refused at the
        driver AFTER the chokepoint said yes — a dead end the model cannot act on."""
        missing = sorted(set(keymap.KEY_ALIASES.values()) - set(input_linux.KEYSYMS))
        assert not missing, f"canonical keys with no X keysym: {missing}"

    def test_delete_and_backspace_resolve_to_their_own_keysyms(self) -> None:
        """macOS shares one keycode between them; X distinguishes them.

        ``delete`` must be ``XK_Delete`` (0xFFFF) and ``backspace`` ``XK_BackSpace``
        (0xFF08) — collapsing them would destroy the character BEFORE the caret for
        ``press_key("delete")`` instead of after it.
        """
        assert input_linux.KEYSYMS["delete"] == 0xFFFF
        assert input_linux.KEYSYMS["backspace"] == 0xFF08
        assert input_linux.KEYSYMS["delete"] != input_linux.KEYSYMS["backspace"]

    def test_a_letter_keysym_is_the_lowercase_glyph(self) -> None:
        """Shift is applied separately for an uppercase glyph, so the base keysym is
        the lowercase one (the ASCII code point)."""
        assert input_linux.KEYSYMS["a"] == 0x0061


# ──────────────────────────────────────────────────────────────────────────
# The AT-SPI action ladder
# ──────────────────────────────────────────────────────────────────────────
class TestTheActionLadder:
    """Specificity order, and the rule that a FAILURE is not a fall-through."""

    @staticmethod
    def _actions(monkeypatch, available, outcomes):
        """Install an element whose ``actions()`` is *available* and whose
        ``do_action(obj, i)`` returns ``outcomes[name]`` for the i-th action."""
        order: list[str] = []

        def actions(obj):
            return tuple(available)

        def do_action(obj, index):
            name = available[index]
            order.append(name)
            return outcomes.get(name, False)

        monkeypatch.setattr(linux_ffi, "actions", actions)
        monkeypatch.setattr(linux_ffi, "do_action", do_action)
        return order

    def test_click_is_tried_first_and_stops_the_ladder(self, monkeypatch) -> None:
        order = self._actions(monkeypatch, ["click", "press", "activate"], {"click": True})
        won = input_linux.perform_activate(object(), "button")
        assert won == "click"
        assert order == ["click"]

    def test_an_ABSENT_action_advances_to_the_next_candidate(self, monkeypatch) -> None:
        """``click`` not advertised, ``activate`` is: it advances past the missing one."""
        order = self._actions(monkeypatch, ["activate"], {"activate": True})
        won = input_linux.perform_activate(object(), "button")
        assert won == "activate"
        assert order == ["activate"]

    def test_a_FAILED_action_STOPS_the_ladder_rather_than_falling_through(
        self, monkeypatch
    ) -> None:
        """**The defect this test exists for — the mirror of the Windows ladder's.**

        ``click`` is advertised AND fails. Advancing to ``press`` would perform a
        DIFFERENT gesture than the one the toolkit already attempted and rejected.
        """
        order = self._actions(monkeypatch, ["click", "press"], {"click": False, "press": True})
        won = input_linux.perform_activate(object(), "button")
        assert won is None, "a failed action was followed by the next candidate"
        assert order == ["click"], "the ladder fell through a FAILURE"

    def test_a_checkbox_can_fall_to_toggle(self, monkeypatch) -> None:
        """``toggle`` is tried only for a checkbox/radio, and only AFTER the activate
        verbs, so a checkbox exposing ``toggle`` alone still flips."""
        self._actions(monkeypatch, ["toggle"], {"toggle": True})
        won = input_linux.perform_activate(object(), "checkbox")
        assert won == "toggle"

    def test_a_button_does_NOT_try_toggle(self, monkeypatch) -> None:
        """A plain button has no toggle semantics, so ``toggle`` is not a candidate
        for it — only its sole-action fallback could reach a lone ``toggle``."""
        assert "toggle" not in input_linux.activate_actions_for("button")
        assert "toggle" in input_linux.activate_actions_for("checkbox")

    def test_a_sole_unnamed_action_is_performed(self, monkeypatch) -> None:
        """A toolkit may name its only activation something unexpected; if it exposes
        exactly one action and no named one matched, perform it."""
        self._actions(monkeypatch, ["do-the-thing"], {"do-the-thing": True})
        won = input_linux.perform_activate(object(), "custom")
        assert won == "do-the-thing"

    def test_several_unnamed_actions_are_NOT_guessed(self, monkeypatch) -> None:
        """With two unrecognised actions there is no basis to pick one, so refuse."""
        order = self._actions(monkeypatch, ["foo", "bar"], {"foo": True, "bar": True})
        won = input_linux.perform_activate(object(), "custom")
        assert won is None
        assert order == [], "an ambiguous action was performed anyway"

    def test_no_actions_at_all_refuses(self, monkeypatch) -> None:
        monkeypatch.setattr(linux_ffi, "actions", lambda obj: ())
        assert input_linux.perform_activate(object(), "button") is None


# ──────────────────────────────────────────────────────────────────────────
# The modifier mapping and chord synthesis
# ──────────────────────────────────────────────────────────────────────────
class TestModifiersAndChords:
    def test_ctrl_shift_releases_in_reverse_order(self, monkeypatch) -> None:
        """**A chord must leave no modifier stuck down.**

        Press order is modifiers then key; release order is key then modifiers in
        REVERSE. Asserted on the exact XTest key events, with the keysym→keycode map
        made the identity so the sequence is readable.
        """
        events: list[tuple[int, bool]] = []
        monkeypatch.setattr(linux_ffi, "x_keysym_to_keycode", lambda k: k)
        monkeypatch.setattr(
            linux_ffi, "xtest_key", lambda code, press: events.append((code, press))
        )
        monkeypatch.setattr(linux_ffi, "x_flush", lambda: None)

        spec = keymap.parse_spec("ctrl+shift+a")
        input_linux.send_key_chord(spec)

        # modifiers sorted: ctrl (0xFFE3), shift (0xFFE1); key a (0x0061).
        ctrl, shift, a = 0xFFE3, 0xFFE1, 0x0061
        assert events == [
            (ctrl, True),
            (shift, True),
            (a, True),
            (a, False),
            (shift, False),
            (ctrl, False),
        ]

    def test_cmd_canonicalizes_to_ctrl_not_the_logo_key(self, monkeypatch) -> None:
        """``cmd+s`` means Save, which on Linux is Ctrl+S — ``keymap`` already folds
        ``cmd`` to ``ctrl``, so the chord presses Control_L, never Super_L."""
        events: list[int] = []
        monkeypatch.setattr(linux_ffi, "x_keysym_to_keycode", lambda k: k)
        monkeypatch.setattr(
            linux_ffi, "xtest_key", lambda code, press: press and events.append(code)
        )
        monkeypatch.setattr(linux_ffi, "x_flush", lambda: None)
        input_linux.send_key_chord(keymap.parse_spec("cmd+s"))
        assert 0xFFE3 in events, "cmd did not map to Control_L"
        assert 0xFFEB not in events, "cmd mapped to the Super (logo) key"

    def test_win_maps_to_super_not_ctrl(self) -> None:
        """``win`` is the one spelling that names the physical logo key, so it stays
        itself and resolves to Super_L."""
        spec = keymap.parse_spec("win+d")
        assert input_linux.modifier_keysyms(spec) == [0xFFEB]

    def test_capslock_is_refused_with_the_alternative(self) -> None:
        """**A residual-state defect: the only modifier that cannot be undone.**

        ``VK_CAPITAL``'s X analogue toggles a machine-wide lock that OUTLIVES the
        call, so ``capslock+a`` would capitalize everything the operator types next
        and releasing it would not undo it. Refused, naming ``shift+``.
        """
        spec = KeySpec(key="a", modifiers=frozenset({"capslock"}))
        with pytest.raises(input_linux.LinuxInputError) as excinfo:
            input_linux.modifier_keysyms(spec)
        assert "shift+" in str(excinfo.value)

    def test_fn_is_refused_naming_firmware(self) -> None:
        """``fn`` is handled in keyboard firmware below the OS, so there is no keysym
        to synthesize — refused rather than faked into a chord nobody asked for."""
        spec = KeySpec(key="a", modifiers=frozenset({"fn"}))
        with pytest.raises(input_linux.LinuxInputError) as excinfo:
            input_linux.modifier_keysyms(spec)
        assert "firmware" in str(excinfo.value)

    def test_capslock_and_fn_stay_in_the_shared_vocabulary(self) -> None:
        """The refusal is per-PLATFORM: macOS serves both, so they remain valid in
        the platform-free grammar rather than being dropped."""
        assert "capslock" in keymap.MODIFIERS
        assert "fn" in keymap.MODIFIER_ALIASES.values()


# ──────────────────────────────────────────────────────────────────────────
# Keystroke typing: unreachable characters are skipped, never substituted
# ──────────────────────────────────────────────────────────────────────────
class TestTypeTextViaXtest:
    def test_a_shifted_character_presses_shift_around_the_tap(self, monkeypatch) -> None:
        events: list = []
        monkeypatch.setattr(linux_ffi, "x_string_to_keysym", lambda s: int(s[1:], 16))
        monkeypatch.setattr(linux_ffi, "x_keysym_to_keycode", lambda k: k)
        monkeypatch.setattr(linux_ffi, "x_keycode_needs_shift", lambda kc, ks: ks == ord("A"))
        monkeypatch.setattr(
            linux_ffi, "xtest_key", lambda code, press: events.append((code, press))
        )
        monkeypatch.setattr(linux_ffi, "x_flush", lambda: None)

        sent = input_linux.type_text_via_xtest("A")
        assert sent == 1
        shift = 0xFFE1
        assert events[0] == (shift, True)
        assert events[-1] == (shift, False)

    def test_an_unreachable_character_is_SKIPPED_not_substituted(self, monkeypatch) -> None:
        """**Typing the wrong character into a live app is worse than typing nothing.**

        A keysym of 0 (NoSymbol) means the layout cannot reach the character; it is
        skipped, and the returned count is short so the driver refuses as a partial
        write rather than reporting a success it did not achieve.
        """
        taps: list[int] = []
        monkeypatch.setattr(
            linux_ffi, "x_string_to_keysym", lambda s: 0 if "FFFD" in s else int(s[1:], 16)
        )
        monkeypatch.setattr(linux_ffi, "x_keysym_to_keycode", lambda k: k)
        monkeypatch.setattr(linux_ffi, "x_keycode_needs_shift", lambda kc, ks: False)
        monkeypatch.setattr(linux_ffi, "xtest_key", lambda code, press: press and taps.append(code))
        monkeypatch.setattr(linux_ffi, "x_flush", lambda: None)

        # "a\uFFFDb": the middle character resolves to NoSymbol and is skipped.
        sent = input_linux.type_text_via_xtest("a\ufffdb")
        assert sent == 2, "the unreachable character was not skipped"
        assert taps == [ord("a"), ord("b")]


# ──────────────────────────────────────────────────────────────────────────
# EditableText write is focus-free
# ──────────────────────────────────────────────────────────────────────────
class TestEditableTextWrite:
    def test_write_delegates_to_set_text_contents(self, monkeypatch) -> None:
        seen: list = []
        monkeypatch.setattr(
            linux_ffi, "set_text_contents", lambda obj, s: seen.append((obj, s)) or True
        )
        assert input_linux.write_text_via_atspi("OBJ", "hello") is True
        assert seen == [("OBJ", "hello")]

    def test_a_non_editable_object_returns_False(self, monkeypatch) -> None:
        monkeypatch.setattr(linux_ffi, "set_text_contents", lambda obj, s: False)
        assert input_linux.write_text_via_atspi("OBJ", "hello") is False


# ──────────────────────────────────────────────────────────────────────────
# Negative control #1 — paste the evidence
# ──────────────────────────────────────────────────────────────────────────
# Reverting the ladder's "stop on a FAILED action" guard makes
# test_a_FAILED_action_STOPS_the_ladder_rather_than_falling_through FAIL. Verified
# on a scratch copy of input_linux.py with ``perform_activate``'s post-failure
# ``return None`` changed to ``continue`` (fall through to the next candidate):
#
#   $ env -u DISPLAY .venv/bin/python -m pytest -n0 \
#       test/test_computer_use_linux_input.py::TestTheActionLadder::\
#       test_a_FAILED_action_STOPS_the_ladder_rather_than_falling_through
#   >       assert won is None, "a failed action was followed by the next candidate"
#   E       AssertionError: a failed action was followed by the next candidate
#   E       assert 'press' is None
#   1 failed
#
# The guard restored, the test passes. (See reports/driver.md § Negative controls.)
