"""``sky_click`` — the quarantined PRIVATE SkyLight path (``macos_skylight``).

This is the one module in the package built on undocumented Apple ABI, so the tests
are shaped differently from the rest of the suite: they pin the BYTES and the ORDER,
because those are the parts a future edit can change without any type checker or
linter noticing, and the failure mode is not an exception — it is a click delivered
to the wrong window.

Three properties matter:

* **the activation record's byte layout**, offset by offset. A shifted field means
  the window server focuses a different window (or nothing) and the click lands on
  whatever is frontmost — silently;
* **the event recipe's order and its primer pair**. The primer down/up at (-1, -1)
  is not a retry; it is what makes the following pair route to the TARGET window.
  Dropping it is a silent mis-delivery, so its presence is asserted directly;
* **fail-closed degradation.** Every entry point must refuse in prose that names
  ``app_post`` when a symbol is missing, so a future macOS that drops
  ``SLEventPostToPid`` costs the model one clear refusal rather than a crash.

Nothing here touches the real window server: the SPI is faked, so these run on
Linux CI identically.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew.computer_use import macos_skylight as sky
from kiro_crew.computer_use import policy
from kiro_crew.computer_use.types import (
    CLICK_METHOD_AUTO,
    CLICK_METHOD_GLOBAL,
    CLICK_METHOD_SKY_CLICK,
    CLICK_METHODS,
    MOUSE_BUTTON_LEFT,
    MOUSE_BUTTON_MIDDLE,
    MOUSE_BUTTON_RIGHT,
    POINTER_MOVING_METHODS,
    ComputerUseError,
)


class TestTheActivationRecordBytes:
    """Pinned offset by offset. See the module docstring for why."""

    def test_length_and_self_describing_size(self):
        record = sky.activation_record(1, focused=True)
        assert len(record) == 0xF8
        # The record carries its own length at 0x04; a mismatch is rejected.
        assert record[0x04] == 0xF8

    def test_kind_byte_marks_it_as_an_activation(self):
        assert sky.activation_record(1, focused=True)[0x08] == 0x0D

    def test_window_id_is_LITTLE_endian_at_0x3C(self):
        """Byte order is the silent one: big-endian here names a different window."""
        record = sky.activation_record(0x12345678, focused=True)
        assert record[0x3C:0x40] == b"\x78\x56\x34\x12"

    def test_focus_flag_distinguishes_activate_from_deactivate(self):
        assert sky.activation_record(1, focused=True)[0x8A] == 0x01
        assert sky.activation_record(1, focused=False)[0x8A] == 0x02

    def test_the_two_records_differ_ONLY_in_the_focus_flag(self):
        """A deactivate that differed elsewhere would leave focus stuck on."""
        on = sky.activation_record(0xABCD, focused=True)
        off = sky.activation_record(0xABCD, focused=False)
        differing = [i for i in range(len(on)) if on[i] != off[i]]
        assert differing == [0x8A]

    def test_an_oversized_window_id_is_masked_not_overflowed(self):
        """A 4-byte field must never raise or corrupt neighbouring offsets."""
        record = sky.activation_record(0x1_0000_0001, focused=True)
        assert len(record) == 0xF8
        assert record[0x3C:0x40] == b"\x01\x00\x00\x00"


class TestTheClickRecipe:
    def test_a_single_click_is_move_then_primer_pair_then_one_target_pair(self):
        steps = sky.click_recipe(1)
        assert len(steps) == 5
        # The move comes FIRST and is aimed at the target.
        assert steps[0].at_target is True
        # Then the primer pair, which is NOT at the target.
        assert [s.at_target for s in steps[1:3]] == [False, False]
        # Then exactly one target pair.
        assert [s.at_target for s in steps[3:]] == [True, True]

    def test_a_double_click_adds_a_SECOND_target_pair_with_click_state_2(self):
        """AppKit reads ``NSEvent.clickCount`` from the click-state field, so a double
        click is one sequence with states 1 then 2 — not two separate clicks."""
        steps = sky.click_recipe(2)
        assert len(steps) == 7
        target_states = [s.click_state for s in steps if s.at_target][1:]
        assert target_states == [1, 1, 2, 2]

    def test_the_primer_pair_is_always_present(self):
        """It is what routes the real pair to the TARGET window rather than the front
        one. A future edit that "optimises" it away is a silent mis-delivery."""
        for count in (1, 2):
            assert any(not s.at_target for s in sky.click_recipe(count)), count

    def test_the_last_step_has_no_trailing_delay(self):
        """A trailing sleep would add latency to every click for nothing."""
        assert sky.click_recipe(1)[-1].delay_after == 0
        assert sky.click_recipe(2)[-1].delay_after == 0

    @pytest.mark.parametrize("count", [0, 3, -1, 99])
    def test_an_unsupported_count_refuses_and_names_app_post(self, count):
        """Only 1 and 2 are in the observed recipe. Inventing a third pair would be
        guessing at ABI, so the refusal points at the public method instead."""
        with pytest.raises(ComputerUseError) as exc:
            sky.click_recipe(count)
        assert "app_post" in str(exc.value)


class TestFailClosedWhenTheSPIIsAbsent:
    """A future macOS that drops a private symbol must cost one clear refusal."""

    @staticmethod
    def _unavailable(reason: str = "missing private symbols: SLEventPostToPid"):
        return sky.SkyLightCapability(missing_symbols=("SLEventPostToPid",))

    def test_capability_reports_the_missing_symbol_by_name(self):
        cap = self._unavailable()
        assert cap.is_available is False
        assert "SLEventPostToPid" in cap.reason

    def test_a_load_failure_is_reported_separately_from_a_missing_symbol(self):
        cap = sky.SkyLightCapability(load_error="the private SkyLight framework could not be loaded")
        assert cap.is_available is False
        assert "SkyLight" in cap.reason

    def test_an_available_capability_has_an_empty_reason(self):
        assert sky.SkyLightCapability().is_available is True
        assert sky.SkyLightCapability().reason == ""

    def test_sky_click_refuses_and_names_app_post_when_unavailable(self):
        class _Spi:
            capability = sky.SkyLightCapability(missing_symbols=("SLEventPostToPid",))

        with patch.object(sky, "_get_spi", return_value=_Spi()):
            with pytest.raises(ComputerUseError) as exc:
                sky.sky_click(
                    pid=1,
                    window_id=2,
                    screen_x=10,
                    screen_y=10,
                    window_x=5,
                    window_y=5,
                    window_width=100,
                    window_height=100,
                )
        message = str(exc.value)
        assert "app_post" in message
        assert "SLEventPostToPid" in message


class TestGeometryIsValidatedBeforeAnyEventIsPosted:
    """A point outside the window would be routed by the window server to nothing —
    or worse, clamped. Refusing tells the model to re-snapshot instead."""

    @staticmethod
    def _available_spi():
        class _Spi:
            capability = sky.SkyLightCapability()

        return _Spi()

    @pytest.mark.parametrize(
        "window_x,window_y",
        [(-1, 5), (5, -1), (101, 5), (5, 101), (float("nan"), 5)],
    )
    def test_a_point_outside_the_window_refuses(self, window_x, window_y):
        with patch.object(sky, "_get_spi", return_value=self._available_spi()):
            with pytest.raises(ComputerUseError):
                sky.sky_click(
                    pid=1,
                    window_id=2,
                    screen_x=10,
                    screen_y=10,
                    window_x=window_x,
                    window_y=window_y,
                    window_width=100,
                    window_height=100,
                )

    def test_a_zero_sized_window_refuses(self):
        with patch.object(sky, "_get_spi", return_value=self._available_spi()):
            with pytest.raises(ComputerUseError):
                sky.sky_click(
                    pid=1,
                    window_id=2,
                    screen_x=1,
                    screen_y=1,
                    window_x=0,
                    window_y=0,
                    window_width=0,
                    window_height=0,
                )

    def test_a_missing_window_id_refuses_and_names_app_post(self):
        """This path routes BY window id; there is nothing to address without one."""
        with patch.object(sky, "_get_spi", return_value=self._available_spi()):
            with pytest.raises(ComputerUseError) as exc:
                sky.sky_click(
                    pid=1,
                    window_id=0,
                    screen_x=1,
                    screen_y=1,
                    window_x=0,
                    window_y=0,
                    window_width=10,
                    window_height=10,
                )
        assert "app_post" in str(exc.value)


class TestTheMethodIsShippedButNeverImplicit:
    """The security shape of adding a private-API method.

    It is available BY NAME and unreachable otherwise — the same contract ``global``
    has, for the same reason: a model that did not ask for a private-API path must
    never be given one.
    """

    def test_sky_click_is_a_shipped_method(self):
        assert CLICK_METHOD_SKY_CLICK in CLICK_METHODS

    def test_auto_NEVER_resolves_to_sky_click(self):
        from kiro_crew.computer_use import policy

        for element_index, point in [(0, None), (None, (5.0, 5.0)), (0, (5.0, 5.0))]:
            resolved = policy.resolve_click_method(
                CLICK_METHOD_AUTO, element_index=element_index, point=point
            )
            assert resolved != CLICK_METHOD_SKY_CLICK, (element_index, point)
            assert resolved != CLICK_METHOD_GLOBAL, (element_index, point)

    def test_sky_click_does_NOT_move_the_operators_pointer(self):
        """The whole point of the method: a background click that leaves the cursor
        alone. If it ever joins this set, the Settings copy becomes false."""
        assert CLICK_METHOD_SKY_CLICK not in POINTER_MOVING_METHODS

    def test_the_private_ABI_lives_in_exactly_ONE_module(self):
        """Quarantine, asserted structurally.

        ``macos_ffi`` keeps its "public frameworks only" property — that is what
        makes it reviewable against Apple's documentation — so a future author must
        not scatter SkyLight symbols into it.
        """
        import pathlib

        # The PRIVATE SYMBOLS, not the word "SkyLight": naming the framework in a
        # comment is how the trade-off gets explained where a reader needs it, and a
        # test that banned the word would push that explanation out of the code.
        # What must stay in one file is the ability to CALL the private ABI.
        private_symbols = (
            "SLEventPostToPid",
            "SLEventSetIntegerValueField",
            "SLPSPostEventRecordTo",
            "CGEventSetWindowLocation",
            "PrivateFrameworks",
        )
        package = pathlib.Path(sky.__file__).parent
        offenders = {}
        for path in sorted(package.glob("*.py")):
            if path.name == "macos_skylight.py":
                continue
            body = path.read_text(encoding="utf-8")
            hits = {sym for sym in private_symbols if sym in body}
            if hits:
                offenders[path.name] = hits
        assert not offenders, offenders


class TestSkyClickIsLeftButtonOnly:
    """GPT 5.6 BLOCKING, confirmed: this method silently downgraded right clicks.

    ``click_recipe`` took no button at all and built the LEFT-button event codes
    unconditionally, while nothing upstream refused ``mouse_button="right"`` with
    ``click_method="sky_click"``. So a right-click request through this method
    performed a left click — "open the context menu" became "activate the control",
    which can destroy data, and it happened on a BACKGROUND window the operator
    cannot see.

    Refused rather than implemented: the observed private sequence (the primer pair,
    the focus-flag record, the nine numeric fields) was reverse-engineered for a left
    click, and the button number is one field among those. A right-click variant
    would be invented, not observed — and this module's whole discipline is that
    every constant here is observed.

    Refused rather than downgraded, for the same reason ``AX_MENU_LADDER`` never
    falls back to ``AXPress``: performing a different gesture than the one requested
    is worse than performing none.
    """

    @pytest.mark.parametrize("button", [MOUSE_BUTTON_RIGHT, MOUSE_BUTTON_MIDDLE])
    def test_the_recipe_REFUSES_a_non_left_button(self, button):
        with pytest.raises(ComputerUseError) as exc:
            sky.click_recipe(1, button)
        # Actionable: names the public method that CAN do it, so the model recovers
        # in one step instead of retrying the same refusal.
        assert "app_post" in str(exc.value)
        assert button in str(exc.value)

    def test_the_left_button_still_builds_the_recipe(self):
        """Inverse guard — the refusal must not break the only supported button."""
        assert len(sky.click_recipe(1, MOUSE_BUTTON_LEFT)) == 5

    def test_the_default_is_left_so_existing_callers_are_unchanged(self):
        assert sky.click_recipe(2) == sky.click_recipe(2, MOUSE_BUTTON_LEFT)

    @pytest.mark.parametrize("button", [MOUSE_BUTTON_RIGHT, MOUSE_BUTTON_MIDDLE])
    def test_the_DISPATCH_gate_refuses_before_a_driver_is_reached(self, button):
        """The chokepoint check, which is what the model actually hits.

        The module-level raise above is defence in depth; this is the one that turns
        the pair into a legible refusal without any private framework being loaded —
        so it holds on every platform, including the Linux and Windows CI shards.
        """
        refusal = policy.check_method_button(CLICK_METHOD_SKY_CLICK, button)
        assert refusal is not None
        assert "app_post" in refusal

    def test_the_gate_permits_every_OTHER_method_with_any_button(self):
        """The constraint belongs to this method alone. Right- and middle-click on the
        public paths are shipped features, so a gate that over-refused would remove
        them."""
        for method in CLICK_METHODS:
            if method == CLICK_METHOD_SKY_CLICK:
                continue
            for button in (MOUSE_BUTTON_RIGHT, MOUSE_BUTTON_MIDDLE, MOUSE_BUTTON_LEFT):
                assert policy.check_method_button(method, button) is None, (method, button)

    def test_the_gate_permits_sky_click_with_the_left_button(self):
        assert policy.check_method_button(CLICK_METHOD_SKY_CLICK, MOUSE_BUTTON_LEFT) is None

    def test_the_driver_PASSES_the_button_rather_than_assuming_it(self):
        """The root cause was an omitted argument, so the fix is asserted at the call.

        A behavioural test alone would keep passing if someone dropped the argument
        again and the recipe's default silently took over — which is exactly the
        shape of the original bug.
        """
        import inspect

        from kiro_crew.computer_use import macos_driver

        source = inspect.getsource(macos_driver._sky_click)
        assert "button=req.button" in source
