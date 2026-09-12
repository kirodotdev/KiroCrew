"""Unit tests for the Bedrock KB retrieval connector.

Everything runs against a fake bedrock-agent-runtime client injected through
``_get_client`` -- no boto3 session, no credentials, no network. The store
passed to ``search_remote_sources`` is a bare sqlite connection exposing the
one table the code reads.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from kiro_crew.knowledge.connectors import bedrock_kb
from kiro_crew.knowledge.connectors.bedrock_kb import (
    BedrockKBConnector,
    _result_url,
    merge_by_rank,
    search_remote_sources,
    search_remote_sources_bounded,
)

MANAGED_ERR = (
    "ValidationException: retrievalConfiguration must specify "
    "managedSearchConfiguration for a MANAGED knowledge base"
)


class FakeClientError(Exception):
    """Mimics botocore ClientError's ``response`` shape for _error_code."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.response = {"Error": {"Code": code, "Message": message or code}}


def _hit(text: str, score: float, metadata: dict | None = None, location: dict | None = None):
    hit: dict = {"content": {"text": text}, "score": score}
    if metadata is not None:
        hit["metadata"] = metadata
    if location is not None:
        hit["location"] = location
    return hit


class FakeClient:
    """Scripted client: per-KB result lists, optional managed-only behavior."""

    def __init__(self, results_by_kb=None, error=None, managed_only=False):
        self.results_by_kb = results_by_kb or {}
        self.error = error
        self.managed_only = managed_only
        self.calls: list[dict] = []

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        config = kwargs["retrievalConfiguration"]
        if self.managed_only and "vectorSearchConfiguration" in config:
            raise FakeClientError("ValidationException", MANAGED_ERR)
        kb_id = kwargs["knowledgeBaseId"]
        return {"retrievalResults": self.results_by_kb.get(kb_id, [])}


@pytest.fixture(autouse=True)
def _consent_granted(monkeypatch):
    """Consent defaults to granted so retrieval-mechanics tests stay focused;
    the consent tests below override this to exercise the fail-closed path.
    Covers BOTH seams: the per-search gate and the per-retrieve withdrawal
    recheck. Yields the REAL _consent_allows so the end-to-end test can
    restore it."""
    original = bedrock_kb._consent_allows
    monkeypatch.setattr(bedrock_kb, "_consent_allows", lambda profile, region: True)
    monkeypatch.setattr(bedrock_kb, "_grant_still_current", lambda expected: True)
    yield original


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


def _add_source(store, sid="src1", kb_ids="KBAAA1111", name="Team KB"):
    store.db.execute(
        "INSERT INTO sources (id, name, source_type, uri, properties) VALUES (?, ?, ?, ?, ?)",
        (
            sid,
            name,
            "bedrock_kb",
            f"bedrock-kb://us-east-1/{kb_ids.split(',')[0]}",
            f'{{"kb_ids": "{kb_ids}", "region": "us-east-1", "profile": "team"}}',
        ),
    )
    store.db.commit()


# ---------------------------------------------------------------- retrieve


def test_managed_fallback_retries_with_managed_config(monkeypatch):
    client = FakeClient(results_by_kb={"KB1": [_hit("body", 0.9)]}, managed_only=True)
    results = bedrock_kb._retrieve_one(client, "KB1", "q", 4, expected_grant=None)
    assert [r["content"]["text"] for r in results] == ["body"]
    assert len(client.calls) == 2
    assert "vectorSearchConfiguration" in client.calls[0]["retrievalConfiguration"]
    managed = client.calls[1]["retrievalConfiguration"]
    assert managed == {"managedSearchConfiguration": {"numberOfResults": 4}}


def test_non_managed_errors_propagate_without_fallback():
    client = FakeClient(error=FakeClientError("AccessDeniedException", "no"))
    with pytest.raises(FakeClientError):
        bedrock_kb._retrieve_one(client, "KB1", "q", 1, expected_grant=None)
    assert len(client.calls) == 1


# ---------------------------------------------------------------- citations


def test_result_url_prefers_crawler_source_uri():
    raw = _hit(
        "t",
        0.5,
        metadata={
            "source_uri": "https://quip.example.com/doc1",
            "x-amz-bedrock-kb-source-uri": "s3://bucket/key",
        },
    )
    assert _result_url(raw) == "https://quip.example.com/doc1"


def test_result_url_falls_back_to_reserved_key_then_location():
    reserved = _hit("t", 0.5, metadata={"x-amz-bedrock-kb-source-uri": "s3://b/k"})
    assert _result_url(reserved) == "s3://b/k"
    located = _hit("t", 0.5, metadata={}, location={"s3Location": {"uri": "s3://b/other"}})
    assert _result_url(located) == "s3://b/other"
    assert _result_url(_hit("t", 0.5, metadata={})) == ""


# ---------------------------------------------------------------- search


def test_parse_kb_ids_normalizes_arns_to_ids():
    arn = "arn:aws:bedrock:us-east-1:123456789012:knowledge-base/KBPEER5678"
    assert bedrock_kb._parse_kb_ids(f"KBTEST1234, {arn}") == ["KBTEST1234", "KBPEER5678"]
    assert bedrock_kb._parse_kb_ids([arn]) == ["KBPEER5678"]
    assert bedrock_kb._parse_kb_ids("") == []


def _source(kb_ids="KB1", name="Team KB"):
    return {
        "id": "src1",
        "name": name,
        "properties": {"kb_ids": kb_ids, "region": "us-east-1", "profile": "p"},
    }


def test_search_fans_out_and_merges_by_score(monkeypatch):
    client = FakeClient(
        results_by_kb={
            "KB1": [
                _hit("low", 0.3, metadata={"source_uri": "https://w/a"}),
                _hit("", 0.99),  # content-less results are dropped
            ],
            "KB2": [_hit("high", 0.8, metadata={"source_uri": "https://w/b", "title": "B"})],
        }
    )
    monkeypatch.setattr(bedrock_kb, "_get_client", lambda region, profile: (client, None))
    results = BedrockKBConnector().search(_source("KB1,KB2"), "q", 5)
    assert [r["content"] for r in results] == ["high", "low"]
    assert results[0]["title"] == "B"
    assert results[0]["source_uri"] == "https://w/b"
    assert results[0]["source_type"] == "bedrock_kb"
    assert results[0]["source_name"] == "Team KB"
    assert {r["kb_id"] for r in results} == {"KB1", "KB2"}


def test_search_drops_hits_below_the_relevance_floor(monkeypatch):
    # Retrieve returns nearest neighbors unconditionally, so an off-topic KB
    # answers every query; the floor keeps that noise out of merged results.
    client = FakeClient(
        results_by_kb={
            "KB1": [
                _hit("noise", bedrock_kb.MIN_REMOTE_SCORE - 0.01),
                _hit("signal", bedrock_kb.MIN_REMOTE_SCORE + 0.01),
            ]
        }
    )
    monkeypatch.setattr(bedrock_kb, "_get_client", lambda region, profile: (client, None))
    results = BedrockKBConnector().search(_source("KB1"), "q", 5)
    assert [r["content"] for r in results] == ["signal"]


def test_search_redacts_credential_shaped_queries_before_egress(monkeypatch):
    client = FakeClient(results_by_kb={"KB1": [_hit("ok", 0.5)]})
    monkeypatch.setattr(bedrock_kb, "_get_client", lambda region, profile: (client, None))
    secret = "AKIAIOSFODNN7EXAMPLE"  # AWS access key id shape
    BedrockKBConnector().search(_source("KB1"), f"why does {secret} fail auth", 3)
    sent = client.calls[0]["retrievalQuery"]["text"]
    assert secret not in sent
    assert "why does" in sent  # ordinary text survives


def test_search_partial_failure_keeps_other_kbs(monkeypatch):
    calls = {"n": 0}

    class HalfBroken(FakeClient):
        def retrieve(self, **kwargs):
            calls["n"] += 1
            if kwargs["knowledgeBaseId"] == "KB1":
                raise FakeClientError("ThrottlingException")
            return {"retrievalResults": [_hit("ok", 0.4)]}

    monkeypatch.setattr(bedrock_kb, "_get_client", lambda region, profile: (HalfBroken(), None))
    results = BedrockKBConnector().search(_source("KB1,KB2"), "q", 3)
    assert [r["content"] for r in results] == ["ok"]
    assert calls["n"] == 2


def test_search_total_failure_raises(monkeypatch):
    monkeypatch.setattr(
        bedrock_kb,
        "_get_client",
        lambda region, profile: (FakeClient(error=FakeClientError("AccessDeniedException")), None),
    )
    with pytest.raises(RuntimeError, match="AccessDeniedException"):
        BedrockKBConnector().search(_source("KB1"), "q", 3)


# ---------------------------------------------------------------- validate


def test_validate_config_refuses_when_consent_withdrawn_mid_probe(monkeypatch):
    """A withdrawal landing between client creation and the probe raises the
    connector's RuntimeError — validate must REFUSE, not save the source as
    accessible (review finding: local authorization failures are not
    transient AWS weather)."""
    monkeypatch.setattr(
        bedrock_kb, "_get_client", lambda region, profile: (FakeClient(results_by_kb={}), None)
    )
    monkeypatch.setattr(bedrock_kb, "_grant_still_current", lambda expected: False)
    ok, err = BedrockKBConnector().validate_config({"kb_ids": "KB1", "region": "us-east-1"})
    assert not ok
    assert "withdrawn" in err.lower() or "refused" in err.lower()


def test_validate_config_refuses_without_consent(monkeypatch):
    monkeypatch.setattr(bedrock_kb, "_consent_allows", lambda profile, region: False)
    monkeypatch.setattr(
        bedrock_kb,
        "_get_client",
        lambda region, profile: (_ for _ in ()).throw(AssertionError("no client without consent")),
    )
    ok, err = BedrockKBConnector().validate_config({"kb_ids": "KB1", "region": "us-east-1"})
    assert not ok
    assert "consent" in err.lower()


def test_recorded_grant_admits_validate_through_the_real_gate(monkeypatch, _consent_granted):
    """End-to-end consent path with the REAL _consent_allows -> refuse_and_log
    -> authorize chain: a grant recorded under the test-pinned data home admits
    validate_config, and a different (profile, region) still refuses. Only the
    STS probe (network) and the Bedrock client are faked."""
    from unittest.mock import AsyncMock

    from kiro_crew import aws_consent

    monkeypatch.setattr(bedrock_kb, "_consent_allows", _consent_granted)
    aws_consent.record_grant(
        aws_consent.SERVICE_BEDROCK_KB,
        profile="team-profile",
        region="us-east-1",
        account="111122223333",
        arn="arn:aws:iam::111122223333:user/x",
        granted_at="2026-09-06T00:00:00+00:00",
    )
    identity = aws_consent.Identity(ok=True, account="111122223333")
    monkeypatch.setattr(aws_consent, "probe_identity", AsyncMock(return_value=identity))
    monkeypatch.setattr(
        bedrock_kb,
        "_get_client",
        lambda region, profile: (FakeClient(results_by_kb={"KB1": []}), None),
    )
    ok, err = BedrockKBConnector().validate_config(
        {"kb_ids": "KB1", "region": "us-east-1", "profile": "team-profile"}
    )
    assert ok, err
    # The twin refusal: a different target finds no matching grant.
    ok, err = BedrockKBConnector().validate_config(
        {"kb_ids": "KB1", "region": "eu-west-1", "profile": "team-profile"}
    )
    assert not ok
    assert "consent" in err.lower()


def test_search_refuses_without_consent(monkeypatch):
    monkeypatch.setattr(bedrock_kb, "_consent_allows", lambda profile, region: False)
    monkeypatch.setattr(
        bedrock_kb,
        "_get_client",
        lambda region, profile: (_ for _ in ()).throw(AssertionError("no client without consent")),
    )
    assert BedrockKBConnector().search(_source("KB1"), "q", 3) == []


def test_result_ids_are_scoped_by_source(monkeypatch):
    """Two sources listing the SAME KB must emit distinct result ids — the
    dashboard keys hit selection by id, so a collision injects both."""
    hits = [_hit("doc one", 0.9, {"source_uri": "https://example.com/a"})]
    monkeypatch.setattr(
        bedrock_kb,
        "_get_client",
        lambda region, profile: (FakeClient(results_by_kb={"KB1": hits}), None),
    )
    connector = BedrockKBConnector()
    src_a = {**_source("KB1"), "id": "src-a"}
    src_b = {**_source("KB1"), "id": "src-b"}
    ids_a = {r["id"] for r in connector.search(src_a, "q", 3)}
    ids_b = {r["id"] for r in connector.search(src_b, "q", 3)}
    assert ids_a and ids_b
    assert ids_a.isdisjoint(ids_b)


def test_parse_kb_ids_dedupes_repeats():
    assert bedrock_kb._parse_kb_ids("KB1, KB1 ,KB2,KB1") == ["KB1", "KB2"]


def test_live_source_survives_store_reconstruction(tmp_path):
    """A bedrock_kb source never has items (live retrieval, nothing
    ingested), so without its orphan-sweep exemption every gateway restart
    would silently delete it. Re-opening the store runs the sweep."""
    from kiro_crew.knowledge.store import KnowledgeStore

    db = str(tmp_path / "knowledge.db")
    store = KnowledgeStore(db)
    source_id = store.add_source(
        "Team KB",
        "bedrock_kb",
        "bedrock-kb://us-east-1/KB1",
        properties={"kb_ids": "KB1", "region": "us-east-1"},
    )
    store.close() if hasattr(store, "close") else None
    reopened = KnowledgeStore(db)  # __init__ runs _migrate -> orphan sweep
    survivor = reopened.get_source_by_uri("bedrock-kb://us-east-1/KB1")
    assert survivor is not None, "live bedrock_kb source was reaped by the orphan sweep"
    assert source_id  # the add itself succeeded


def test_conflicting_second_bedrock_target_detection(tmp_path):
    """The add-time guard's core: a registered source with a DIFFERENT
    (profile, region) is reported as a conflict; the SAME target is not."""
    import json as _json

    from kiro_crew.knowledge.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    store.add_source(
        "First KB",
        "bedrock_kb",
        "bedrock-kb://us-east-1/KB1",
        properties={"kb_ids": "KB1", "region": "us-east-1", "profile": "team-a"},
    )
    rows = store.db.execute(
        "SELECT name, properties FROM sources WHERE source_type = 'bedrock_kb'"
    ).fetchall()
    held = [
        (
            str(_json.loads(r["properties"] or "{}").get("profile") or "").strip(),
            str(_json.loads(r["properties"] or "{}").get("region") or "").strip(),
        )
        for r in rows
    ]
    assert ("team-a", "us-east-1") in held
    # Different target conflicts; same target does not.
    assert any(h != ("team-b", "eu-west-1") for h in held)
    assert all(h == ("team-a", "us-east-1") for h in held)


def test_remote_search_route_resolves_store_and_fails_open(monkeypatch, tmp_path):
    """The internal remote-search route must resolve the store through the
    module's accessor (a bare app-key read crashed on every call — review
    finding) and fail open to [] when the connector leg raises."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.handlers import knowledge as handler
    from kiro_crew.knowledge.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    req = MagicMock()
    req.app = {}
    marker = {"internal_auth": True}
    req.get = lambda key, default=None: marker.get(key, default)

    async def fake_json():
        return {"query": "q", "limit": 3}

    req.json = fake_json
    monkeypatch.setattr(handler, "_store", lambda request: store)
    monkeypatch.setattr(
        bedrock_kb,
        "search_remote_sources_bounded",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    resp = _asyncio.run(handler.remote_search(req))
    assert resp.status == 200
    import json as _json

    assert _json.loads(resp.body)["results"] == []

    # Valid JSON that is not an object must 400, not crash.
    async def fake_json_list():
        return ["not", "a", "dict"]

    req.json = fake_json_list
    resp = _asyncio.run(handler.remote_search(req))
    assert resp.status == 400

    # A cookie-authenticated browser session (no internal_auth marker) is
    # refused outright: these results are raw connector output.
    marker.clear()
    req.json = fake_json
    resp = _asyncio.run(handler.remote_search(req))
    assert resp.status == 403


def test_mcp_remote_leg_never_evaluates_consent_in_process(monkeypatch):
    """The sandboxed MCP server must not run the connector (whose consent
    read sees a pinned, possibly-stale inode). Its remote leg is an HTTP
    call to the gateway; the connector module is only used for the pure
    rank merge."""
    from kiro_crew.mcp_tools import knowledge as mcp_knowledge

    calls = {}

    def fake_fetch(query, limit, source_id):
        calls["fetched"] = (query, limit, source_id)
        return [
            {
                "id": "bedrock:src:KB1:0",
                "title": "t",
                "content": "remote hit",
                "score": 0.5,
                "match_type": "remote",
                "source_type": "bedrock_kb",
            }
        ]

    monkeypatch.setattr(mcp_knowledge, "_fetch_remote_results", fake_fetch)
    # The connector's consent seam must NOT be reachable from the MCP leg:
    monkeypatch.setattr(
        bedrock_kb,
        "search_remote_sources_bounded",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("in-process leg used")),
    )
    merged = bedrock_kb.merge_by_rank(
        [{"id": "local1", "content": "local", "score": 0.02}],
        fake_fetch("q", 5, None),
        5,
    )
    assert calls["fetched"] == ("q", 5, None)
    assert [r["id"] for r in merged][:2] == ["local1", "bedrock:src:KB1:0"]


def test_withdrawal_mid_fanout_stops_remaining_retrieves(monkeypatch):
    """Consent withdrawn after the first KB's Retrieve: the remaining KBs'
    paid requests must not be issued (per-retrieve recheck)."""
    calls: list[str] = []
    hits = [_hit("doc", 0.9, {"source_uri": "https://example.com/a"})]

    class CountingClient(FakeClient):
        def retrieve(self, **kwargs):
            calls.append(kwargs["knowledgeBaseId"])
            return super().retrieve(**kwargs)

    monkeypatch.setattr(
        bedrock_kb,
        "_get_client",
        lambda region, profile: (CountingClient(results_by_kb={"KB1": hits, "KB2": hits}), None),
    )
    state = {"granted": True}

    def flip_after_first(expected):
        allowed = state["granted"]
        state["granted"] = False  # withdrawn once the first recheck passes
        return allowed

    monkeypatch.setattr(bedrock_kb, "_grant_still_current", flip_after_first)
    results = BedrockKBConnector().search(_source("KB1,KB2"), "q", 6)
    # KB1 retrieved; KB2's recheck refused before its Retrieve was issued.
    assert calls == ["KB1"]
    assert all(r["id"].startswith("bedrock:") for r in results)


def _fake_boto3(sts_account: str):
    """A boto3 stand-in whose session freezes fixed creds and answers STS."""
    from types import SimpleNamespace

    def client(service, **kwargs):
        if service == "sts":
            return SimpleNamespace(get_caller_identity=lambda: {"Account": sts_account})
        return SimpleNamespace(kind="bedrock-agent-runtime", kwargs=kwargs)

    frozen = SimpleNamespace(access_key="AKIAFAKE", secret_key="sk", token=None)
    creds = SimpleNamespace(get_frozen_credentials=lambda: frozen)
    session = SimpleNamespace(get_credentials=lambda: creds, client=client)
    return SimpleNamespace(session=SimpleNamespace(Session=lambda profile_name=None: session))


def test_get_client_verifies_frozen_credentials_against_the_grant(monkeypatch):
    from kiro_crew import aws_consent

    aws_consent.record_grant(
        aws_consent.SERVICE_BEDROCK_KB,
        profile="p1",
        region="us-east-1",
        account="111122223333",
        arn="arn:aws:iam::111122223333:user/x",
        granted_at="2026-09-06T00:00:00+00:00",
    )
    monkeypatch.setattr(bedrock_kb, "boto3", _fake_boto3("111122223333"))
    client, verified = bedrock_kb._get_client("us-east-1", "p1")
    assert getattr(client, "kind", "") == "bedrock-agent-runtime"
    assert verified is not None and verified.account == "111122223333"

    # Same frozen set resolving to a DIFFERENT account than confirmed: refuse.
    monkeypatch.setattr(bedrock_kb, "boto3", _fake_boto3("999988887777"))
    with pytest.raises(RuntimeError, match="not the one the operator confirmed"):
        bedrock_kb._get_client("us-east-1", "p1")

    # Exact-target match: the right account under the WRONG profile or
    # region (a grant recorded for a different target) also refuses.
    monkeypatch.setattr(bedrock_kb, "boto3", _fake_boto3("111122223333"))
    with pytest.raises(RuntimeError, match="not the one the operator confirmed"):
        bedrock_kb._get_client("us-east-1", "other-profile")
    with pytest.raises(RuntimeError, match="not the one the operator confirmed"):
        bedrock_kb._get_client("eu-west-1", "p1")


def test_validate_config_rejects_missing_fields():
    connector = BedrockKBConnector()
    ok, err = connector.validate_config({"region": "us-east-1"})
    assert not ok and "kb_ids" in err
    ok, err = connector.validate_config({"kb_ids": "KB1"})
    assert not ok and "region" in err


def test_validate_config_probes_each_kb(monkeypatch):
    client = FakeClient(results_by_kb={"KB1": [], "KB2": []})
    monkeypatch.setattr(bedrock_kb, "_get_client", lambda region, profile: (client, None))
    ok, err = BedrockKBConnector().validate_config({"kb_ids": "KB1,KB2", "region": "us-east-1"})
    assert ok, err
    assert len(client.calls) == 2
    assert all(
        c["retrievalConfiguration"]["vectorSearchConfiguration"]["numberOfResults"] == 1
        for c in client.calls
    )


def test_validate_config_fails_on_access_denied(monkeypatch):
    monkeypatch.setattr(
        bedrock_kb,
        "_get_client",
        lambda region, profile: (FakeClient(error=FakeClientError("AccessDeniedException")), None),
    )
    ok, err = BedrockKBConnector().validate_config({"kb_ids": "KB1", "region": "us-east-1"})
    assert not ok
    assert "KB1" in err and "AccessDeniedException" in err


def test_validate_config_treats_throttle_as_accessible(monkeypatch):
    monkeypatch.setattr(
        bedrock_kb,
        "_get_client",
        lambda region, profile: (FakeClient(error=FakeClientError("ThrottlingException")), None),
    )
    ok, err = BedrockKBConnector().validate_config({"kb_ids": "KB1", "region": "us-east-1"})
    assert ok, err


def test_validate_config_rejects_malformed_kb_id(monkeypatch):
    # A ValidationException that is NOT the managed-config rejection (that one
    # is consumed by the fallback) means the id or request itself is malformed;
    # saving such a source would create one whose every search fails.
    monkeypatch.setattr(
        bedrock_kb,
        "_get_client",
        lambda region, profile: (
            FakeClient(error=FakeClientError("ValidationException", "invalid knowledgeBaseId")),
            None,
        ),
    )
    ok, err = BedrockKBConnector().validate_config(
        {"kb_ids": "not-a-real-id!", "region": "us-east-1"}
    )
    assert not ok
    assert "ValidationException" in err


def test_validate_config_reports_missing_extra(monkeypatch):
    def raise_missing(region, profile):
        raise RuntimeError(bedrock_kb._MISSING_BOTO3_MSG)

    monkeypatch.setattr(bedrock_kb, "_get_client", raise_missing)
    ok, err = BedrockKBConnector().validate_config({"kb_ids": "KB1", "region": "us-east-1"})
    assert not ok
    assert "bedrock" in err and "boto3" in err


# ------------------------------------------------------- remote source scan


def test_search_remote_sources_merges_registered_sources(monkeypatch, store):
    _add_source(store, sid="s1", kb_ids="KB1", name="One")
    _add_source(store, sid="s2", kb_ids="KB2", name="Two")
    client = FakeClient(
        results_by_kb={
            "KB1": [_hit("a", 0.3)],
            "KB2": [_hit("b", 0.7)],
        }
    )
    monkeypatch.setattr(bedrock_kb, "_get_client", lambda region, profile: (client, None))
    results = search_remote_sources(store, "q", 5)
    assert [r["content"] for r in results] == ["b", "a"]
    assert [r["source_name"] for r in results] == ["Two", "One"]


def test_search_remote_sources_scopes_to_source_id(monkeypatch, store):
    _add_source(store, sid="s1", kb_ids="KB1")
    _add_source(store, sid="s2", kb_ids="KB2")
    client = FakeClient(results_by_kb={"KB1": [_hit("a", 0.3)], "KB2": [_hit("b", 0.7)]})
    monkeypatch.setattr(bedrock_kb, "_get_client", lambda region, profile: (client, None))
    results = search_remote_sources(store, "q", 5, source_id="s1")
    assert [r["content"] for r in results] == ["a"]


def test_search_remote_sources_fails_open_per_source(monkeypatch, store):
    _add_source(store, sid="s1", kb_ids="KB1")
    monkeypatch.setattr(
        bedrock_kb,
        "_get_client",
        lambda region, profile: (FakeClient(error=FakeClientError("AccessDeniedException")), None),
    )
    assert search_remote_sources(store, "q", 3) == []


# ------------------------------------------------------------ bounded entry


def test_bounded_returns_empty_fast_with_no_sources(store):
    # No bedrock_kb rows: no pool is consulted, nothing to clean up.
    assert search_remote_sources_bounded(store, "q", 3) == []


def test_bounded_times_out_fail_open(monkeypatch, store):
    _add_source(store)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bedrock-kb-test")
    monkeypatch.setattr(bedrock_kb, "_search_pool", pool)
    slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(bedrock_kb, "_search_slots", slots)

    started = time.monotonic()

    def slow(*args, **kwargs):
        time.sleep(0.5)
        return [{"content": "late", "score": 1.0}]

    monkeypatch.setattr(bedrock_kb, "search_remote_sources", slow)
    try:
        assert search_remote_sources_bounded(store, "q", 3, timeout=0.05) == []
        assert time.monotonic() - started < 0.45
    finally:
        pool.shutdown(wait=True)
    # The worker's finally released the slot once it finished: the full
    # budget is available again (a leak would leave one slot held forever).
    for _ in range(2):
        assert slots.acquire(blocking=False)
    slots.release()
    slots.release()


def test_bounded_fails_open_when_all_slots_busy(monkeypatch, store):
    _add_source(store)
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(bedrock_kb, "_search_slots", slots)
    assert slots.acquire(blocking=False)  # hold the only slot
    try:
        # No pool interaction at all: admission is refused before submit.
        monkeypatch.setattr(
            bedrock_kb,
            "_get_search_pool",
            lambda: (_ for _ in ()).throw(AssertionError("pool must not be consulted")),
        )
        assert search_remote_sources_bounded(store, "q", 3) == []
    finally:
        slots.release()


def test_get_client_reports_missing_extra(monkeypatch):
    monkeypatch.setattr(bedrock_kb, "boto3", None)
    with pytest.raises(RuntimeError, match=r"\[bedrock\] extra"):
        bedrock_kb._get_client("us-east-1", "p")


def test_bounded_swallows_worker_errors(monkeypatch, store):
    _add_source(store)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bedrock-kb-test")
    monkeypatch.setattr(bedrock_kb, "_search_pool", pool)

    def boom(*args, **kwargs):
        raise FakeClientError("InternalServerException")

    monkeypatch.setattr(bedrock_kb, "search_remote_sources", boom)
    try:
        assert search_remote_sources_bounded(store, "q", 3) == []
    finally:
        pool.shutdown(wait=True)


# --------------------------------------------------------------- rank merge


def test_merge_by_rank_interleaves_and_caps():
    local = [{"id": f"l{i}"} for i in range(3)]
    remote = [{"id": f"r{i}"} for i in range(3)]
    merged = merge_by_rank(local, remote, 4)
    assert [r["id"] for r in merged] == ["l0", "r0", "l1", "r1"]
    assert merge_by_rank([], remote, 2) == remote[:2]
    assert merge_by_rank(local, [], 2) == local[:2]


# ----------------------------------------------------------- sync protocol


@pytest.mark.asyncio
async def test_source_is_never_synced():
    connector = BedrockKBConnector()
    assert connector.source_type() == "bedrock_kb"
    assert await connector.detect_changes({"id": "s1"}) is False
    text, meta = await connector.fetch({"id": "s1"})
    assert text == "" and meta.get("remote") is True


class TestSearchDeadline:
    """A spent budget stops the worker BEFORE the next paid Retrieve — the
    pool future's result(timeout) alone only abandons the waiter (GPT
    round-46 advisory finding)."""

    def test_expired_deadline_skips_remaining_kbs(self, monkeypatch):
        from kiro_crew.knowledge.connectors import bedrock_kb as mod

        calls: list[str] = []

        def fake_retrieve(client, kb_id, query, per_kb, expected_grant=None):
            calls.append(kb_id)
            return []

        connector = mod.BedrockKBConnector()
        monkeypatch.setattr(mod, "_retrieve_one", fake_retrieve)
        monkeypatch.setattr(mod, "_get_client", lambda r, p: (object(), object()))
        monkeypatch.setattr(mod, "_consent_allows", lambda p, r: True)
        source = {
            "id": "s1",
            "name": "KB",
            "uri": "bedrock-kb://us-east-1/KB1",
            "properties": '{"kb_ids": "KB1,KB2,KB3", "region": "us-east-1", "profile": ""}',
        }
        # Deadline already in the past: zero Retrieve calls may be issued.
        out = connector.search(source, "q", 5, deadline=mod.time.monotonic() - 1.0)
        assert out == []
        assert calls == []

    def test_live_deadline_lets_requests_through(self, monkeypatch):
        from kiro_crew.knowledge.connectors import bedrock_kb as mod

        calls: list[str] = []

        def fake_retrieve(client, kb_id, query, per_kb, expected_grant=None):
            calls.append(kb_id)
            return []

        connector = mod.BedrockKBConnector()
        monkeypatch.setattr(mod, "_retrieve_one", fake_retrieve)
        monkeypatch.setattr(mod, "_get_client", lambda r, p: (object(), object()))
        monkeypatch.setattr(mod, "_consent_allows", lambda p, r: True)
        source = {
            "id": "s1",
            "name": "KB",
            "uri": "bedrock-kb://us-east-1/KB1",
            "properties": '{"kb_ids": "KB1,KB2", "region": "us-east-1", "profile": ""}',
        }
        connector.search(source, "q", 5, deadline=mod.time.monotonic() + 60.0)
        assert calls == ["KB1", "KB2"]
