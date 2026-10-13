"""The native redaction gate is on by default and falls back to Python.

These cases run without ``kc_native`` built: they prove that a missing, stale,
disabled or failing native module leaves redaction on the pure-Python path with
the same answers. ``test_native_redaction_gate.py`` covers the built module.
"""

from __future__ import annotations

import sys
import types

import pytest

from kiro_crew.security import redaction, redaction_patterns

_SAMPLES = [
    "",
    "plain text",
    "token ghp_" + "a" * 36,
    "Authorization: Bearer abc.def.ghi",
    "postgres://user:pw@host/db",
    "xoxb-1234-5678-abcdef",
]


def test_default_is_on(monkeypatch):
    monkeypatch.delenv("KIROCREW_NATIVE_REDACTION", raising=False)
    fake = types.ModuleType("kc_native")
    fake.GATE_FINGERPRINT = redaction._native_gate_fingerprint()
    fake.might_contain_credential = lambda text: True
    monkeypatch.setitem(sys.modules, "kc_native", fake)
    assert redaction._load_native_gate() is fake.might_contain_credential


def test_missing_module_keeps_python(monkeypatch):
    monkeypatch.delenv("KIROCREW_NATIVE_REDACTION", raising=False)
    monkeypatch.setitem(sys.modules, "kc_native", None)  # import raises ImportError
    assert redaction._load_native_gate() is None


def test_stale_module_keeps_python(monkeypatch, caplog):
    monkeypatch.delenv("KIROCREW_NATIVE_REDACTION", raising=False)
    fake = types.ModuleType("kc_native")
    fake.GATE_FINGERPRINT = "not-this-build"
    fake.might_contain_credential = lambda text: True
    monkeypatch.setitem(sys.modules, "kc_native", fake)
    with caplog.at_level("DEBUG", logger=redaction_patterns.__name__):
        assert redaction._load_native_gate() is None
    assert "fingerprint does not match" in caplog.text


def test_env_zero_turns_it_off(monkeypatch):
    fake = types.ModuleType("kc_native")
    fake.GATE_FINGERPRINT = redaction._native_gate_fingerprint()
    fake.might_contain_credential = lambda text: True
    monkeypatch.setitem(sys.modules, "kc_native", fake)
    monkeypatch.setenv("KIROCREW_NATIVE_REDACTION", "0")
    assert redaction._load_native_gate() is None


@pytest.mark.parametrize("text", _SAMPLES)
def test_no_gate_answers_like_python(monkeypatch, text):
    monkeypatch.setattr(redaction, "_NATIVE_GATE", None)
    assert redaction._might_contain_credential(text) == redaction._might_contain_credential_py(text)


@pytest.mark.parametrize("text", _SAMPLES)
def test_gate_that_cannot_encode_falls_back(monkeypatch, text):
    def _refuse(_text):
        raise UnicodeEncodeError("utf-8", "x", 0, 1, "surrogate")

    monkeypatch.setattr(redaction, "_NATIVE_GATE", _refuse)
    assert redaction._might_contain_credential(text) == redaction._might_contain_credential_py(text)
