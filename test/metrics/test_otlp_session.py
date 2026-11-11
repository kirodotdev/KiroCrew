"""``metrics.otlp_session`` without the optional ``otlp`` extra.

The loopback tests in ``test_otlp_no_redirect.py`` drive the real exporter and
skip when the extra is absent, as it is in CI. These cover every branch of the
session choice with the SDK module stubbed, so they always run.
"""

from __future__ import annotations

import logging
import sys
import types

import pytest
import requests

_SDK_MODULE = "opentelemetry.exporter.otlp.proto.http.metric_exporter"
_GENERIC_VAR = "OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER"
_METRICS_VAR = "OTEL_PYTHON_EXPORTER_OTLP_HTTP_METRICS_CREDENTIAL_PROVIDER"


@pytest.fixture
def otlp_session():
    """The module under test, imported per test so collection never depends on it."""
    from kiro_crew.metrics import otlp_session as module

    return module


@pytest.fixture
def sdk(monkeypatch):
    """A stand-in SDK exporter module; tests add or omit the session loader."""
    for var in (_GENERIC_VAR, _METRICS_VAR):
        monkeypatch.delenv(var, raising=False)
    mod = types.ModuleType(_SDK_MODULE)
    monkeypatch.setitem(sys.modules, _SDK_MODULE, mod)
    return mod


def test_a_supplied_session_is_copied_with_its_class_and_state(otlp_session):
    class _Signing(requests.Session):
        pass

    source = _Signing()
    source.key = "k"  # subclass state
    source.headers["x-api-key"] = "v"
    before = source.max_redirects

    used = otlp_session.export_session(source, own_only=False)
    used.headers["User-Agent"] = "sdk"

    assert type(used) is _Signing and used is not source
    assert used.key == "k" and used.headers["x-api-key"] == "v"
    assert used.max_redirects == 0 and source.max_redirects == before
    assert source.headers.get("User-Agent") != "sdk"


def test_a_list_entry_gets_a_plain_session_and_never_the_loader(otlp_session, sdk):
    def _never(_variable):  # pragma: no cover - asserted not called
        raise AssertionError("an otlp_endpoints entry must not load the provider session")

    sdk._load_session_from_envvar = _never
    used = otlp_session.export_session(None, own_only=True)
    assert type(used) is requests.Session and used.max_redirects == 0


@pytest.mark.parametrize("loaded", [True, False])
def test_the_legacy_entry_gets_the_loaded_session_else_a_plain_one(otlp_session, sdk, loaded):
    provided = requests.Session()
    asked: list[str] = []

    def _load(variable):
        asked.append(variable)
        return provided if loaded else None

    sdk._load_session_from_envvar = _load
    used = otlp_session.export_session(None, own_only=False)
    assert asked == [_METRICS_VAR]
    assert (used is provided) is loaded
    assert type(used) is requests.Session and used.max_redirects == 0


@pytest.mark.parametrize("var", ["", _GENERIC_VAR, _METRICS_VAR])
def test_a_missing_loader_keeps_the_legacy_entry_on_a_plain_session(
    otlp_session, sdk, monkeypatch, caplog, var
):
    if var:
        monkeypatch.setenv(var, "edition-provider")
    with caplog.at_level(logging.WARNING, logger=otlp_session.__name__):
        used = otlp_session.export_session(None, own_only=False)
    assert type(used) is requests.Session and used.max_redirects == 0
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1
    assert ("was NOT loaded" in messages[0]) is bool(var)
    if var:
        assert var in messages[0] and "edition-provider" not in messages[0]
