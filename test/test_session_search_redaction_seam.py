"""The three session-search egress passes redact through ONE owner.

``/api/sessions/search`` and ``/api/instances/search-sessions`` both return
search rows carrying LLM-authored titles and peer-supplied snippets, and each
hand-composed ``redact_exfiltration_urls`` -> ``redact_credentials``: THREE
copies across TWO modules (#3940, Design review). ``security.redact`` already
owned that composition, so the fix is that all three passes call IT, over the
one shared ``SESSION_SEARCH_TEXT_FIELDS`` tuple -- which gives the field list an
owner too, so a caller cannot redact ``title`` and forget ``snippet``.

The pins are BEHAVIOURAL rather than source-text: each replaces ``redact`` in the
module under test with a wrapping marker and requires the response to carry it.
A site that reintroduced a private copy of the chain would still redact
correctly and would still fail these tests, which is the bypass the seam exists
to prevent. The marker WRAPS its input rather than returning a constant, so each
assertion pairs a row's output with that row's OWN input -- three rows with
distinct text, because with one row every candidate key expression coincides and
a loop-variable slip is invisible.

Harness mirrors ``test_instances_search_federation.py`` (the owner of the
federated-search stubs) and ``test_dashboard_sessions_search.py``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew import security
from kiro_crew.dashboard import handlers_instances as hi
from kiro_crew.dashboard.handlers import _shared
from kiro_crew.dashboard.handlers import sessions as hs
from kiro_crew.instances.ssh_tunnel_manager import TunnelState

#: The redacted field set, spelled out HERE rather than read from
#: ``_shared.SESSION_SEARCH_TEXT_FIELDS``. Iterating the constant under test
#: made these assertions shrink with it: narrowing the tuple to ``("title",)``
#: left the pins green because they stopped checking ``snippet`` at all. A test
#: must not derive its expectations from the value it exists to pin.
REDACTED_FIELDS = ("title", "snippet")

#: Three rows with DISTINCT text in both redacted fields. Distinct per row so a
#: pairing assertion can catch a wrong-key slip; distinct per FIELD so a pass
#: that redacts `title` and skips `snippet` cannot pass by coincidence.
ROWS = [
    {"key": "k1", "title": "title-one", "snippet": "snippet-one"},
    {"key": "k2", "title": "title-two", "snippet": "snippet-two"},
    {"key": "k3", "title": "title-three", "snippet": "snippet-three"},
]


def _marker(value: str) -> str:
    """Stand-in redactor that WRAPS, so the caller's own input stays visible."""
    return f"SEAM<{value}>"


class _Log:
    """ConversationLog stub returning a fixed local ranking."""

    def __init__(self, rows):
        self._rows = rows

    def search_sessions(self, _q, _limit):
        return [dict(r) for r in self._rows]


class _Req:
    """Request stub mirroring aiohttp's mapping surface (see
    test_instances_search_federation.py, which owns this shape)."""

    def __init__(self, state, identity):
        self.app = {"state": state}
        self.match_info = {}
        self.headers = {}
        self.query = {"q": "apollo"}
        self._attrs = identity

    def get(self, key, default=""):
        return self._attrs.get(key, default)

    def __contains__(self, key):
        return key in self._attrs

    def __getitem__(self, key):
        return self._attrs[key]


def _request(state):
    # `_guard`'s owner check is POSITIVE: with no configured owner_id only the
    # local dashboard subjects pass, so "local-app" with an empty app slug is
    # the owner's own context.
    return _Req(state, {"user": "local-app", "app": ""})


def _enable_instances(monkeypatch):
    monkeypatch.setattr(
        hi.KiroCrewConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=True))),
    )


class _Mgr:
    """Instances manager stub with scriptable per-peer search replies."""

    def __init__(self, replies):
        self._replies = replies

    def status_all(self):
        return {
            iid: SimpleNamespace(state=TunnelState.CONNECTED, local_port=17777)
            for iid in self._replies
        }

    async def search_sessions_remote(self, iid, _q, _limit):
        return self._replies[iid]


def _state(local_rows, mgr=None):
    ids = list(getattr(mgr, "_replies", {})) if mgr is not None else []
    return SimpleNamespace(
        conversation_log=_Log(local_rows),
        instances_manager=mgr,
        instances_registry=SimpleNamespace(
            get=lambda iid: SimpleNamespace(id=iid, name=f"crew-{iid}"),
            list=lambda: [SimpleNamespace(id=iid, name=f"crew-{iid}") for iid in ids],
        ),
    )


async def _json_of(resp):
    return json.loads(resp.body.decode())


def _assert_every_field_paired_with_its_own_row(rows, expected_inputs):
    """Each row's every redacted field wraps THAT row's own input.

    The pairing is what a single-row test cannot check: with one row a
    ``rows[0]``-for-``row`` slip reads identically to the correct expression.
    """
    assert len(rows) == len(expected_inputs), f"expected {len(expected_inputs)} rows, got {rows}"
    for row, source in zip(rows, expected_inputs):
        for field in REDACTED_FIELDS:
            assert row[field] == f"SEAM<{source[field]}>", (
                f"row {row.get('key')} field {field!r} did not come through the shared "
                f"redactor carrying its own input: {row[field]!r}"
            )


class TestOneRedactionOwnerForThreePasses:
    def test_both_modules_redact_through_the_one_shared_owner(self):
        """Neither module composes the chain itself; both hold `security.redact`.

        `security.redact` (security.py) is the repo's single composition of the
        exfiltration-URL and credential passes, in that order. Pinning identity
        rather than behaviour is what catches a module that goes back to
        hand-composing the pair while still redacting correctly.
        """
        assert hs.redact is security.redact
        assert hi.redact is security.redact
        assert hs.SESSION_SEARCH_TEXT_FIELDS is _shared.SESSION_SEARCH_TEXT_FIELDS
        assert hi.SESSION_SEARCH_TEXT_FIELDS is _shared.SESSION_SEARCH_TEXT_FIELDS

    def test_the_redacted_field_set_is_title_and_snippet(self):
        """Pins the field set as a VALUE, against this file's own literal.

        Separate from the routing pins on purpose: narrowing the set is a
        deliberate act (it stops redacting a field that reaches the browser),
        so it should fail one test that says exactly that rather than quietly
        shrinking what the other pins check.
        """
        assert _shared.SESSION_SEARCH_TEXT_FIELDS == REDACTED_FIELDS

    @pytest.mark.asyncio
    async def test_local_search_pass_routes_every_text_field_through_the_owner(self, monkeypatch):
        """``/api/sessions/search`` -- pass 1 of 3."""
        monkeypatch.setattr(hs, "redact", _marker)

        resp = await hs.api_sessions_search(_request(_state(ROWS)))

        _assert_every_field_paired_with_its_own_row((await _json_of(resp))["sessions"], ROWS)

    @pytest.mark.asyncio
    async def test_federated_local_row_pass_routes_every_text_field_through_the_owner(
        self, monkeypatch
    ):
        """``/api/instances/search-sessions``, LOCAL rows -- pass 2 of 3.

        This endpoint calls ``conversation_log.search_sessions`` directly rather
        than going through ``api_sessions_search``, so it carries its own copy of
        the pass; before this test nothing pinned it at all.
        """
        _enable_instances(monkeypatch)
        monkeypatch.setattr(hi, "redact", _marker)

        resp = await hi.api_instances_search_sessions(_request(_state(ROWS)))

        _assert_every_field_paired_with_its_own_row((await _json_of(resp))["sessions"], ROWS)

    @pytest.mark.asyncio
    async def test_federated_peer_row_pass_routes_every_text_field_through_the_owner(
        self, monkeypatch
    ):
        """``/api/instances/search-sessions``, PEER rows -- pass 3 of 3."""
        _enable_instances(monkeypatch)
        monkeypatch.setattr(hi, "redact", _marker)
        mgr = _Mgr({"a": (True, {"sessions": [dict(r) for r in ROWS]})})

        resp = await hi.api_instances_search_sessions(_request(_state([], mgr)))

        _assert_every_field_paired_with_its_own_row((await _json_of(resp))["sessions"], ROWS)


class TestFederatedLocalRowsRedactForReal:
    """The pass that had no coverage before, driven with the REAL redactors.

    The behavioural pins above prove ROUTING; this proves the routed chain
    actually removes a credential, and asserts the secret appears NOWHERE in the
    serialized body rather than only in the field it was planted in.
    """

    @pytest.mark.asyncio
    async def test_a_credential_in_a_local_row_never_reaches_the_browser(self, monkeypatch):
        _enable_instances(monkeypatch)
        secret = "AKIAIOSFODNN7EXAMPLE"
        rows = [
            {"key": "k1", "title": f"deploy {secret}", "snippet": f"ran with {secret}"},
            {"key": "k2", "title": "clean title", "snippet": "clean snippet"},
        ]

        resp = await hi.api_instances_search_sessions(_request(_state(rows)))

        body = resp.body.decode()
        # Control: the rows really did reach the response, so an absent secret is
        # redaction rather than an empty result set.
        assert "clean title" in body
        assert secret not in body, "a credential survived the federated local-row pass"
