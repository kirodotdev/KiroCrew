"""The ``signal`` config section's load-boundary coercion."""

from kiro_crew.config.section_builders import _build_signal_config


def test_a_string_enabled_value_does_not_turn_the_channel_on():
    assert _build_signal_config({"enabled": "false"}).enabled is False
    assert _build_signal_config({"enabled": "true"}).enabled is False


def test_a_real_bool_enabled_value_is_kept():
    assert _build_signal_config({"enabled": True}).enabled is True
    assert _build_signal_config({}).enabled is False


def test_an_unquoted_number_gains_its_plus_and_junk_reads_as_empty():
    cfg = _build_signal_config(
        {"account": 447700900001, "allowed_numbers": [447700900002, True, 1.5, None, " +1 "]}
    )
    assert cfg.account == "+447700900001"
    assert cfg.allowed_numbers == ["+447700900002", "+1"]


def test_config_loading_and_the_channel_share_one_identifier_coercion():
    """One definition, read by both: config loading must not drift from the channel."""
    from kiro_crew.config import section_builders
    from kiro_crew.dashboard.handlers import signal_settings
    from kiro_crew.signal_cli import client, identifiers

    assert section_builders.coerce_identifier is identifiers.coerce_identifier
    assert client.coerce_identifier is identifiers.coerce_identifier
    assert signal_settings.coerce_identifier is identifiers.coerce_identifier
    assert not hasattr(section_builders, "_coerce_signal_identifier")
    for name in ("normalize_identifier", "identifier_problem", "account_identifier"):
        assert getattr(client, name) is getattr(identifiers, name), name
        assert getattr(signal_settings, name) is getattr(identifiers, name), name
    assert client.redact_identifier is identifiers.redact_identifier
