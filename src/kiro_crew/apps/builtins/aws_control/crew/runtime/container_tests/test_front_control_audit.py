"""The control-authorization decision is audited, both ways, or it is not acted on.

Three properties, and the third is the one that makes the other two worth having:

1. A grant produces a record.
2. A deny produces a record -- the half that answers "who tried" after the fact, and the
   half a granted-only trail leaves out.
3. A decision that cannot be recorded is not acted on at all.

The container mirrors ``kiro_crew.sel`` rather than calling it, so these tests also pin
the mirror: SEL's ``event_type`` vocabulary, its field names, and the audit-or-deny
behaviour of ``log_tool_invocation(critical=True)``.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest
from container import common
from container.common import audit
from container.front.app import build_app
from fastapi.testclient import TestClient


def _strip_audit_handlers() -> None:
    """Put the audit logger into the state production starts in: nothing listening.

    Two steps, because pytest is not production. Removing the handler this module attaches
    is the first; cutting ``propagate`` is the second, and it is needed because pytest's
    own log-capture handler sits on the ROOT logger at level 0. A record that propagates
    there IS delivered, so with propagation left on, the sink is genuinely live and the
    inert arrangement cannot be reproduced inside a pytest session at all. Production has
    no such root handler -- that is what the reproduction of uvicorn's configuration in
    ``test_uvicorns_own_logging_configuration_does_not_provide_a_sink`` shows.
    """
    log = audit.sink()
    log.handlers[:] = [
        h for h in log.handlers if not getattr(h, "_crew_container_audit_sink", False)
    ]
    log.propagate = False


@pytest.fixture(autouse=True)
def _restore_audit_sink():
    """Leave the process's logging state as it was found.

    These tests deliberately break the sink, and a leaked broken sink would fail unrelated
    tests later in the session with a 503 that looks like a real defect.
    """
    log = audit.sink()
    saved_handlers = list(log.handlers)
    saved_level = log.level
    saved_propagate = log.propagate
    yield
    log.handlers[:] = saved_handlers
    log.setLevel(saved_level)
    log.propagate = saved_propagate


def make_settings(tmp_path: Path, *, control_secret: str | None) -> common.Settings:
    data_home = tmp_path / "data"
    data_home.mkdir(parents=True, exist_ok=True)
    return common.Settings(
        backend_port=8765,
        backend_run_dir=data_home / "run",
        front_port=8080,
        route_prefix="",
        control_secret=control_secret,
        data_home=data_home,
        config_dir=data_home / "config",
        crew_name="frontdesk",
        backup_bucket=None,
        backup_prefix="",
        single_principal=True,
    )


def _records(caplog) -> list[dict]:
    """Every SEL-shaped record in the captured log, parsed."""
    out = []
    for message in caplog.messages:
        marker = "sel_shaped_audit "
        if marker in message:
            out.append(json.loads(message.split(marker, 1)[1]))
    return out


def test_a_denied_control_request_is_recorded(tmp_path: Path, caplog) -> None:
    settings = make_settings(tmp_path, control_secret="right")
    with caplog.at_level("INFO"):
        with TestClient(build_app(settings)) as client:
            resp = client.post("/control/anything", headers={"X-SMC-Control-Secret": "wrong"})

    assert resp.status_code == 403
    records = _records(caplog)
    assert len(records) == 1, f"expected one record, got {records}"
    assert records[0]["event_type"] == audit.EVENT_DENIED
    assert records[0]["outcome"] == "denied"
    assert records[0]["metadata"]["control_header_present"] is True


def test_a_granted_control_request_is_recorded(tmp_path: Path, caplog) -> None:
    """The grant, too.

    A trail that records only denials cannot answer what an authorised caller did, which
    is the question an incident actually starts from.
    """
    settings = make_settings(tmp_path, control_secret="right")
    with caplog.at_level("INFO"):
        with TestClient(build_app(settings)) as client:
            resp = client.post("/control/anything", headers={"X-SMC-Control-Secret": "right"})

    assert resp.status_code == 404, "authorised, and no control route is served yet"
    records = _records(caplog)
    assert len(records) == 1, f"expected one record, got {records}"
    assert records[0]["event_type"] == audit.EVENT_GRANTED
    assert records[0]["outcome"] == "granted"


def test_a_missing_header_is_recorded_as_missing(tmp_path: Path, caplog) -> None:
    """ "No header" and "wrong secret" are different attempts, so they read differently."""
    settings = make_settings(tmp_path, control_secret="right")
    with caplog.at_level("INFO"):
        with TestClient(build_app(settings)) as client:
            client.post("/control/anything")

    records = _records(caplog)
    assert records[0]["metadata"]["control_header_present"] is False


def test_the_record_never_carries_the_secret(tmp_path: Path, caplog) -> None:
    """An audit log that carries the credential makes every reader a holder of it."""
    settings = make_settings(tmp_path, control_secret="sup3r-s3cret-value")
    with caplog.at_level("INFO"):
        with TestClient(build_app(settings)) as client:
            client.post(
                "/control/anything",
                headers={"X-SMC-Control-Secret": "sup3r-s3cret-value", "Cookie": "a=b"},
            )

    assert "sup3r-s3cret-value" not in caplog.text
    record = _records(caplog)[0]
    assert "sup3r-s3cret-value" not in json.dumps(record)
    assert "a=b" not in json.dumps(record), "no other header rides along either"


def test_a_decision_that_cannot_be_recorded_is_not_acted_on(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """The property the other tests rest on.

    A correct caller with the correct secret is REFUSED when the record cannot be written,
    because serving it would be the unaudited grant the record exists to prevent. 503, not
    403: what failed is this container's ability to record, not the caller's authorisation.
    """
    from container.front import app as app_mod

    def _explode(event, *, log=None):
        raise audit.AuditUnavailable("the sink is gone")

    monkeypatch.setattr(app_mod.audit, "emit", _explode)
    settings = make_settings(tmp_path, control_secret="right")
    with caplog.at_level("ERROR"):
        with TestClient(app_mod.build_app(settings)) as client:
            resp = client.post("/control/anything", headers={"X-SMC-Control-Secret": "right"})

    assert resp.status_code == 503
    assert resp.json()["code"] == "control_audit_unavailable"
    assert "the sink is gone" in caplog.text


def test_a_denial_that_cannot_be_recorded_is_also_refused_that_way(
    tmp_path: Path, monkeypatch
) -> None:
    """Same answer for the deny.

    Returning the 403 anyway would look harmless and would leave a denial nobody can
    account for, which is the same gap in the trail from the other direction.
    """
    from container.front import app as app_mod

    def _explode(event, *, log=None):
        raise audit.AuditUnavailable("the sink is gone")

    monkeypatch.setattr(app_mod.audit, "emit", _explode)
    settings = make_settings(tmp_path, control_secret="right")
    with TestClient(app_mod.build_app(settings)) as client:
        resp = client.post("/control/anything", headers={"X-SMC-Control-Secret": "wrong"})

    assert resp.status_code == 503


def test_the_customer_surface_is_not_a_control_decision(tmp_path: Path, caplog) -> None:
    """Non-vacuity for the emit site.

    The customer surface is not a control decision, so it must produce NO record -- both
    because a per-request audit line is noise and because a record implying an
    authorisation decision was made would be false.
    """
    settings = make_settings(tmp_path, control_secret="right")
    with caplog.at_level("INFO"):
        with TestClient(build_app(settings)) as client:
            client.get("/health")

    assert _records(caplog) == []


def test_the_event_uses_sels_own_type_names() -> None:
    """The mirror is only useful if the reader does not need a second vocabulary.

    ``kiro_crew.sel.SecurityEvent`` documents its ``event_type`` values as
    "tool_invocation, tool_approval, tool_denial, mcp_call, api_access". Drifting to a
    private spelling here would mean an operator's SEL query silently misses these.
    """
    assert audit.EVENT_GRANTED == "tool_approval"
    assert audit.EVENT_DENIED == "tool_denial"


def test_the_record_carries_no_chain_fields() -> None:
    """An unchained line must not claim SEL's integrity fields.

    ``prev_hash`` and ``entry_hash`` are what make a SEL entry tamper-evident, and they
    are computed as the entry is written into that chain. Emitting either here would
    assert an integrity property this record does not have.
    """
    event = audit.control_decision_event(
        granted=True, method="POST", path="/control/x", header_present=True, crew_name="frontdesk"
    )
    assert "prev_hash" not in event
    assert "entry_hash" not in event


def test_an_unserialisable_record_raises_rather_than_logging_nothing() -> None:
    """The failure mode of a JSON sink, caught where it can still deny.

    The sink is made live first, so the refusal is attributable to the record rather than
    to the sink -- two different failures with the same consequence, and an operator
    reading the message has to be able to tell them apart.
    """
    log = logging.getLogger("crew_container.audit.test_serialise")
    log.setLevel(logging.INFO)
    handler = logging.StreamHandler(io.StringIO())
    handler.setLevel(logging.INFO)
    log.addHandler(handler)
    try:
        assert audit.sink_is_live(log), "the sink must be live for this to test the record"
        with pytest.raises(audit.AuditUnavailable, match="serialised"):
            audit.emit({"bad": object()}, log=log)
    finally:
        log.removeHandler(handler)


def test_the_sink_is_live_before_any_request_can_be_served(tmp_path: Path) -> None:
    """Ordering, not just presence.

    ``build_app`` configures the sink, so an app that exists has one -- checked here
    BEFORE any request is issued. Configuring lazily on the first emit would leave
    whichever request arrives first deciding whether the audit works, and a request during
    startup would get the inert path.
    """
    _strip_audit_handlers()
    assert not audit.sink_is_live(audit.sink()), "precondition: no sink yet"

    build_app(make_settings(tmp_path, control_secret="right"))

    assert audit.sink_is_live(audit.sink()), "building the app must configure the sink"


def test_uvicorns_own_logging_configuration_does_not_provide_a_sink() -> None:
    """The production arrangement, reproduced rather than assumed.

    ``uvicorn.run`` applies its own ``LOGGING_CONFIG``, which configures the ``uvicorn``
    loggers and leaves the root at WARNING with no handler. A module logger under that
    configuration reports an effective level of WARNING and reaches no handler, so an INFO
    audit call writes zero bytes and raises nothing. This is the environment the audit has
    to work in, and the reason the sink is this module's own rather than ambient.
    """
    import logging.config

    import uvicorn.config

    saved = {
        name: (logging.getLogger(name).level, list(logging.getLogger(name).handlers))
        for name in ("", "container.front.app")
    }
    try:
        logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
        ambient = logging.getLogger("container.front.app")
        assert ambient.getEffectiveLevel() == logging.WARNING
        assert not audit.sink_is_live(ambient)
        # And the audit's own sink is unaffected by that configuration, which is the point.
        audit.configure_sink()
        assert audit.sink_is_live(audit.sink())
    finally:
        for name, (level, handlers) in saved.items():
            log = logging.getLogger(name)
            log.setLevel(level)
            log.handlers[:] = handlers


def test_a_handler_that_only_takes_warnings_is_not_an_info_sink() -> None:
    """``hasHandlers()`` is not the question.

    A logger with a WARNING-level handler attached answers ``hasHandlers()`` with True
    while still dropping every INFO record, which is precisely the arrangement that makes
    an audit inert. The handler's own level has to be part of the check.
    """
    log = logging.getLogger("crew_container.audit.test_warning_only")
    log.setLevel(logging.INFO)
    log.propagate = False
    handler = logging.StreamHandler(io.StringIO())
    handler.setLevel(logging.WARNING)
    log.addHandler(handler)
    try:
        assert log.hasHandlers(), "the misleading answer"
        assert not audit.sink_is_live(log), "an INFO record would be dropped by the handler"
    finally:
        log.removeHandler(handler)
        log.propagate = True


def test_a_logger_cut_off_from_handlers_is_not_live() -> None:
    """``propagate = False`` with no local handler reaches nothing."""
    log = logging.getLogger("crew_container.audit.test_cut_off")
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        assert not audit.sink_is_live(log)
    finally:
        log.propagate = True


def test_last_resort_does_not_count_as_a_sink() -> None:
    """``logging.lastResort`` is at WARNING, so it carries no INFO record.

    Counting it would report a live sink for exactly the case this check exists to catch:
    a process with no logging configuration at all.
    """
    assert logging.lastResort is not None
    assert logging.lastResort.level == logging.WARNING
    log = logging.getLogger("crew_container.audit.test_last_resort")
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        assert not audit.sink_is_live(log)
    finally:
        log.propagate = True


def test_an_inert_sink_refuses_the_decision(tmp_path: Path) -> None:
    """The whole finding, end to end.

    With no INFO-capable handler, a control request that would otherwise be authorised is
    refused instead of being served unaudited. Before this check the same arrangement
    served the request and wrote nothing, because a dropped log record raises nothing and
    the audit-or-deny path never fired.
    """
    settings = make_settings(tmp_path, control_secret="right")
    app = build_app(settings)
    _strip_audit_handlers()

    with TestClient(app) as client:
        resp = client.post("/control/anything", headers={"X-SMC-Control-Secret": "right"})

    assert resp.status_code == 503
    assert resp.json()["code"] == "control_audit_unavailable"


def test_configuring_the_sink_twice_does_not_double_the_record(tmp_path: Path) -> None:
    """``build_app`` runs more than once in a process, and a handler per call multiplies."""
    _strip_audit_handlers()
    audit.configure_sink()
    audit.configure_sink()
    audit.configure_sink()
    owned = [h for h in audit.sink().handlers if getattr(h, "_crew_container_audit_sink", False)]
    assert len(owned) == 1
