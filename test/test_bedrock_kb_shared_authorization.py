"""One consent probe and one client per (profile, region) per remote search.

Every attested ``bedrock_kb`` row holds the same target, so a probe per row
spent the 5-second budget before the later rows were reached. These tests run
``search_remote_sources`` over several sources on a fake clock that charges
each consent probe and each client setup a realistic cold cost, so the budget
arithmetic is the real one and nothing sleeps.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from test_bedrock_kb_connector import FakeClient, FakeClientError, _add_source, _hit

from kiro_crew.knowledge.connectors import bedrock_kb

# Cold costs the module's own comment gives: the AWS CLI STS probe takes
# ~0.5-1.5s, the boto STS verify a round trip more.
_PROBE_SECS = 1.5
_CLIENT_SECS = 0.5


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture()
def store():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE sources (id TEXT PRIMARY KEY, name TEXT, source_type TEXT, "
        "uri TEXT, properties TEXT)"
    )
    yield SimpleNamespace(db=conn)
    conn.close()


@pytest.fixture()
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(bedrock_kb, "time", SimpleNamespace(monotonic=fake.monotonic))
    # The per-Retrieve rechecks read the real consent store; the rows are
    # attested for real by _add_source, so only the grant recheck (which the
    # fake client's None grant cannot pass) is held open here.
    monkeypatch.setattr(bedrock_kb, "_grant_still_current", lambda expected, current: True)
    monkeypatch.setattr(
        bedrock_kb, "_registration_still_attested", lambda registration, attested: True
    )
    return fake


@pytest.mark.parametrize("n_sources", [3, 4])
def test_every_source_is_searched_inside_the_budget(monkeypatch, store, clock, n_sources):
    kb_ids = [f"KBAAA1111{i}" for i in range(n_sources)]
    for i, kb in enumerate(kb_ids):
        _add_source(store, sid=f"src{i}", kb_ids=kb, name=f"KB {i}")
    client = FakeClient(results_by_kb={kb: [_hit(f"hit-{kb}", 0.9)] for kb in kb_ids})
    probes: list[tuple[str, str]] = []
    setups: list[tuple[str, str]] = []

    def consent(profile, region, timeout=None):
        probes.append((profile, region))
        clock.now += _PROBE_SECS
        return True

    def get_client(region, profile):
        setups.append((profile, region))
        clock.now += _CLIENT_SECS
        return client, None

    monkeypatch.setattr(bedrock_kb, "_consent_allows", consent)
    monkeypatch.setattr(bedrock_kb, "_get_client", get_client)
    deadline = clock.now + bedrock_kb.REMOTE_SEARCH_TIMEOUT_SECS

    results = bedrock_kb.search_remote_sources(store, "q", 10, deadline=deadline)

    # A probe per source would cost n * 2s and skip the last sources.
    assert probes == [("team", "us-east-1")]
    assert setups == [("team", "us-east-1")]
    assert sorted(c["knowledgeBaseId"] for c in client.calls) == kb_ids
    assert sorted(r["content"] for r in results) == [f"hit-{kb}" for kb in kb_ids]
    assert clock.now < deadline


def test_a_refused_target_is_asked_once_and_bills_nothing(monkeypatch, store, clock):
    for i in range(3):
        _add_source(store, sid=f"src{i}", kb_ids=f"KBAAA1111{i}")
    probes: list[str] = []

    def consent(profile, region, timeout=None):
        probes.append(region)
        return False

    def get_client(region, profile):
        raise AssertionError("no client may be built for a refused target")

    monkeypatch.setattr(bedrock_kb, "_consent_allows", consent)
    monkeypatch.setattr(bedrock_kb, "_get_client", get_client)

    assert bedrock_kb.search_remote_sources(store, "q", 10, deadline=clock.now + 5.0) == []
    assert probes == ["us-east-1"]


def test_a_failed_client_setup_is_not_retried_for_each_source(monkeypatch, store, clock):
    for i in range(3):
        _add_source(store, sid=f"src{i}", kb_ids=f"KBAAA1111{i}")
    setups: list[str] = []

    def get_client(region, profile):
        setups.append(region)
        raise RuntimeError("retrieval refused")

    monkeypatch.setattr(bedrock_kb, "_consent_allows", lambda profile, region, timeout=None: True)
    monkeypatch.setattr(bedrock_kb, "_get_client", get_client)

    assert bedrock_kb.search_remote_sources(store, "q", 10, deadline=clock.now + 5.0) == []
    assert setups == ["us-east-1"]


def test_a_spent_budget_is_not_recorded_as_a_refusal(monkeypatch, store, clock):
    """Out of budget before the check is silence, not a "no" for the target."""
    _add_source(store, sid="src0", kb_ids="KBAAA11110")
    monkeypatch.setattr(bedrock_kb, "_consent_allows", lambda profile, region, timeout=None: True)
    monkeypatch.setattr(bedrock_kb, "_get_client", lambda region, profile: (FakeClient(), None))
    row = dict(store.db.execute(bedrock_kb._BOUNDED_ROW_SELECT).fetchone())
    authorized: dict = {}

    out = bedrock_kb.BedrockKBConnector().search(
        row, "q", 5, deadline=clock.now - 1.0, authorized=authorized
    )

    assert out == []
    assert authorized == {}


# ---------------------------------------------------------------- last search per source


@pytest.fixture()
def last_searches(monkeypatch):
    fresh: dict = {}
    monkeypatch.setattr(bedrock_kb, "_last_searches", fresh)
    return fresh


@pytest.mark.parametrize(
    ("consent", "client_error", "retrieve_error", "expected"),
    [
        (False, None, None, "refused"),
        (True, RuntimeError("no AWS credentials resolve"), None, "error"),
        (True, None, FakeClientError("AccessDeniedException"), "error"),
        (True, None, None, "ok"),
    ],
)
def test_each_source_records_how_its_last_search_went(
    monkeypatch, store, clock, last_searches, consent, client_error, retrieve_error, expected
):
    for i in range(2):
        _add_source(store, sid=f"src{i}", kb_ids=f"KBAAA1111{i}")

    def get_client(region, profile):
        if client_error is not None:
            raise client_error
        return FakeClient(error=retrieve_error), None

    monkeypatch.setattr(
        bedrock_kb, "_consent_allows", lambda profile, region, timeout=None: consent
    )
    monkeypatch.setattr(bedrock_kb, "_get_client", get_client)
    deadline = clock.now + bedrock_kb.REMOTE_SEARCH_TIMEOUT_SECS

    bedrock_kb.search_remote_sources(store, "q", 10, deadline=deadline)  # flake-ok: fake clock

    # The second source reuses the first one's answer for the shared target.
    assert {sid: e["outcome"] for sid, e in last_searches.items()} == {
        "src0": expected,
        "src1": expected,
    }
    assert all(e["at"] for e in last_searches.values())


@pytest.mark.parametrize("stage", ["before the source", "in the consent probe", "between KBs"])
def test_a_spent_budget_records_no_search(monkeypatch, store, clock, last_searches, stage):
    _add_source(store, sid="src0", kb_ids="KBAAA11110,KBAAA11111")
    deadline = clock.now
    if stage != "before the source":
        deadline += bedrock_kb.REMOTE_SEARCH_TIMEOUT_SECS

    def consent(profile, region, timeout=None):
        if stage == "in the consent probe":
            clock.now = deadline  # the probe times out on the budget's last second
            return False
        return True

    class _SlowClient(FakeClient):
        def retrieve(self, **kwargs):
            clock.now = deadline  # the first KB answers as the budget ends
            return super().retrieve(**kwargs)

    monkeypatch.setattr(bedrock_kb, "_consent_allows", consent)
    monkeypatch.setattr(bedrock_kb, "_get_client", lambda region, profile: (_SlowClient(), None))

    bedrock_kb.search_remote_sources(store, "q", 10, deadline=deadline)  # flake-ok: fake clock

    assert last_searches == {}


def test_a_search_forgets_the_rows_no_longer_attested(store, clock, last_searches):
    _add_source(store, sid="src0", kb_ids="KBAAA11110")
    last_searches["src0"] = {"outcome": "ok", "at": "2026-11-01T00:00:00+00:00"}
    last_searches["deleted"] = {"outcome": "ok", "at": "2026-11-01T00:00:00+00:00"}
    bedrock_kb.search_remote_sources(store, "q", 10, deadline=clock.now)  # flake-ok: fake clock
    assert list(last_searches) == ["src0"]


# ---------------------------------------------------------------- clipped ids in logs

_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
_PLANTED_ID = "p" * 24 + " " + _SECRET


def _plant(store):
    """A row written straight into the agent-writable table, id over the SQL bound."""
    store.db.execute(
        "INSERT INTO sources (id, name, source_type, uri, properties) "
        "VALUES (?, 'Planted', 'bedrock_kb', 'bedrock-kb://us-east-1/KBTEST0001', ?)",
        (_PLANTED_ID, '{"kb_ids": "KBTEST0001", "region": "us-east-1", "profile": "team"}'),
    )
    store.db.commit()


def _logged(caplog, audited):
    return caplog.text + "\n".join(audited)


@pytest.fixture()
def audited(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(
        bedrock_kb.aws_consent,
        "audit_decision",
        lambda service, *, outcome, detail="": lines.append(detail),
    )
    return lines


def test_a_spent_budget_never_logs_a_planted_id(store, clock, caplog, audited):
    _plant(store)
    with caplog.at_level("WARNING", logger=bedrock_kb.__name__):
        bedrock_kb.search_remote_sources(store, "q", 5, deadline=clock.now - 1.0)
    assert "budget spent before source <clipped id>" in caplog.text
    assert _SECRET[:20] not in _logged(caplog, audited)


@pytest.mark.parametrize("stage", ["consent", "client setup"])
def test_the_per_target_budget_warnings_never_log_a_planted_id(
    monkeypatch, store, clock, caplog, audited, stage
):
    _plant(store)
    row = dict(store.db.execute(bedrock_kb._BOUNDED_ROW_SELECT).fetchone())
    deadline = clock.now + 1.0

    def consent(profile, region, timeout=None):
        clock.now += 2.0  # the probe spends the rest of the budget
        return True

    if stage == "consent":
        deadline = clock.now - 1.0
    monkeypatch.setattr(bedrock_kb, "_consent_allows", consent)
    monkeypatch.setattr(bedrock_kb, "_get_client", lambda region, profile: (FakeClient(), None))
    with caplog.at_level("WARNING", logger=bedrock_kb.__name__):
        assert bedrock_kb.BedrockKBConnector().search(row, "q", 5, deadline=deadline) == []
    assert f"budget spent before {stage} for <clipped id>" in caplog.text
    assert _SECRET[:20] not in caplog.text


def test_a_failed_source_search_never_logs_a_planted_id(monkeypatch, store, clock, caplog, audited):
    """Even a row that got past attestation is named by the placeholder."""
    _plant(store)
    monkeypatch.setattr(bedrock_kb, "_row_is_attested", lambda attested, source: True)
    monkeypatch.setattr(bedrock_kb, "_consent_allows", lambda profile, region, timeout=None: True)

    def get_client(region, profile):
        raise RuntimeError("retrieval refused")

    monkeypatch.setattr(bedrock_kb, "_get_client", get_client)
    with caplog.at_level("WARNING", logger=bedrock_kb.__name__):
        assert bedrock_kb.search_remote_sources(store, "q", 5, deadline=clock.now + 5.0) == []
    assert "bedrock_kb source <clipped id> search failed" in caplog.text
    assert _SECRET[:20] not in _logged(caplog, audited)
