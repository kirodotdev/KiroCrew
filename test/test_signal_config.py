"""The ``signal`` config section's load-boundary coercion."""

from kiro_crew.config.section_builders import _build_signal_config


def test_a_string_enabled_value_does_not_turn_the_channel_on():
    assert _build_signal_config({"enabled": "false"}).enabled is False
    assert _build_signal_config({"enabled": "true"}).enabled is False


def test_a_real_bool_enabled_value_is_kept():
    assert _build_signal_config({"enabled": True}).enabled is True
    assert _build_signal_config({}).enabled is False
