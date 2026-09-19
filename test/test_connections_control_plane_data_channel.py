"""W01 · L09: a CONSUMER really receives the data, through the real composition.

The gap this pins was fatal and silent: every judgment in the plane was strict,
every gate was tested, and the SUCCESS path returned nothing. ``OperationResult``
was ``{status, next_cursor}``, ``neutral_decode`` threw the body away, so a caller
that authorized, routed and emitted a call correctly held a verdict and no items,
no object and no bytes. Reproduced before the fix with ``/tmp/l09c-prefix-probe.py``.

**What "proven" means here.** Not ``body is not None``. Each test below builds the
REAL production transport (:func:`build_production_transport`) over a REAL
isolated on-disk :class:`kiro_crew.secrets.SecretVault` and drives it through
:func:`~kiro_crew.connections.control_plane.executor.execute` /
:class:`~kiro_crew.connections.control_plane.executor.PageWalk`, then asserts the
VALUES a consumer holds: the item fields, the cursor string, and -- for an Office
download -- the bytes, byte for byte. A test that asserted presence rather than
content would pass just as happily on a payload carrying the wrong bytes, which is
the failure mode that makes a binary channel worthless.

The sender is injected (``http_send`` ) here rather than a loopback TLS server:
this is the FAST UNIT LANE for the data channel -- what the decode produces and
what survives to the consumer -- and it stays that way deliberately. It is NOT
the evidence that a payload travels a real sender: an injected callable cannot
prove that, and
``test_connections_control_plane_real_tls_e2e.py`` is where all three shapes are
proven end to end over a REAL TLS loopback through the unmodified
:func:`~kiro_crew.connections.control_plane.production.urllib_http_send`, in a
fresh-install posture, with a readback against the exact bytes the server served.
The wire properties themselves -- redirects, credential stripping, the body cap,
the deadline -- are proven against real sockets in
``test_connections_control_plane_production.py``.
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, get_args

import pytest

from kiro_crew.connections.control_plane.auth_modes import declare_permitted_modes
from kiro_crew.connections.control_plane.binding import Binding
from kiro_crew.connections.control_plane.executor import (
    EXECUTOR_SCHEMA_VERSION,
    PageWalk,
    execute,
)
from kiro_crew.connections.control_plane.handle import (
    DerivedHandle,
    derive_handle,
    ensure_usable,
)
from kiro_crew.connections.control_plane.lifecycle import BindingStore
from kiro_crew.connections.control_plane.operation import Effect, OperationDescriptor
from kiro_crew.connections.control_plane.policy import LayerCeilings
from kiro_crew.connections.control_plane.production import (
    BindingCustodyGate,
    HttpReply,
    HttpRequest,
    build_production_transport,
    decode_json_body,
    neutral_decode,
)
from kiro_crew.connections.control_plane.result import (
    DEFAULT_MEDIA_TYPE,
    PAYLOAD_KIND_BYTES,
    PAYLOAD_KIND_COLLECTION,
    PAYLOAD_KIND_OBJECT,
    PAYLOAD_KINDS,
    RESULT_SCHEMA_VERSION,
    BytesPayload,
    CollectionPayload,
    ObjectPayload,
    OperationPayload,
    OperationResult,
    result_with_payload,
)
from kiro_crew.secrets import SecretVault

_T0 = 1_000_000.0
_GRANTED = ("mail.read", "mail.send")

#: A byte string that is NOT valid UTF-8 and contains a NUL -- the two things a
#: text coercion destroys. Prefixed with a real ZIP local-file-header magic, which
#: is what an xlsx/docx actually starts with.
_XLSX_BYTES = (
    b"PK\x03\x04\x14\x00\x08\x08\x00\x00" + bytes(range(256)) + b"\xff\xfe\xfd\x00PK\x05\x06"
)
_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# =============================================================================
# the real control-plane composition (mirrors the production suite's fixtures)
# =============================================================================
def _verifier(*, claimed_subject: str, claimed_tenant: str, service_id: str) -> Dict[str, str]:
    return {
        "subject_ref": f"subject://verified/{claimed_subject}",
        "tenant_ref": f"tenant://verified/{claimed_tenant}",
    }


def _make_default_binding() -> Binding:
    """The default binding, minted by the real constructor -- scoped secret_ref kept.

    Built ONCE at module load so the ``real_vault`` fixture can seed the credential
    under the binding's OWN per-binding name (read off the record the constructor
    produced), never a slug name. The two share this object, so the vault holds
    exactly the name ``select_secret`` will fence-read.
    """

    from kiro_crew.connections.control_plane.binding import create_binding

    return create_binding(
        service_id="outlook",
        claimed_subject="alice",
        claimed_tenant="acme",
        credential_mode="oauth_user",
        verifier=_verifier,  # type: ignore[arg-type]
        slug="outlook",
    )


_DEFAULT_BINDING: Binding = _make_default_binding()


def _bound(*, requested: Tuple[str, ...] = ("mail.read",)) -> Tuple[Binding, DerivedHandle]:
    """A binding and a handle derived from it (the binding is what gets fenced).

    Uses the shared ``_DEFAULT_BINDING`` so its per-binding scoped secret_ref (the
    one the constructor produced) is what the ``real_vault`` fixture seeds -- the
    per-binding path is exercised, not a slug path.
    """

    binding = dict(_DEFAULT_BINDING)  # type: ignore[assignment]
    handle = derive_handle(
        binding,  # type: ignore[arg-type]
        granted_scopes=_GRANTED,
        requested_scopes=requested,
        now=_T0,
        ttl_seconds=300.0,
    )
    return binding, handle  # type: ignore[return-value]


def _gate_for(binding: Binding, handle: DerivedHandle) -> BindingCustodyGate:
    view = ensure_usable(handle, now=_T0)
    return BindingCustodyGate(binding=binding, binding_fingerprint=view.binding_fingerprint)


def _live_store(root: Path, binding: Binding) -> BindingStore:
    """A REAL on-disk BindingStore holding ``binding``, for the live-store fence."""

    store = BindingStore(root / "connections" / "control_plane_bindings.json")
    store.insert(
        binding, deployment_id="deployment://test/graph", kiro_principal="kiro://test/owner"
    )
    return store


def _descriptor(effect: Effect = "read") -> OperationDescriptor:
    return {
        "operation_id": "outlook.messages.list",
        "service_id": "outlook",
        "operation_kind": "list",
        "effect": effect,
        "credential_modes": ("oauth_user",),
    }


def _kw(**over: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = dict(
        now=_T0,
        offered_mode="oauth_user",
        permitted=declare_permitted_modes(("oauth_user",)),
        layers=LayerCeilings(),  # all None == ungoverned == permit
        governance_scope="tools",
        governance_item="messages.list",
    )
    base.update(over)
    return base


@pytest.fixture
def real_vault(tmp_path: Path) -> SecretVault:
    """A REAL, isolated, AES-256-GCM vault on disk holding the binding secret.

    Seeded under the shared default binding's OWN per-binding secret_ref name (read
    off the record ``create_binding`` produced), so the fenced ``select_secret``
    read resolves the per-binding entry -- not a slug name.
    """

    vault = SecretVault(tmp_path / "crewhome")
    vault.set_sync(_DEFAULT_BINDING["secret_ref"]["name"], "outlook-live-token")
    # Guard for the guard: a stub here would make every assertion below vacuous.
    assert (tmp_path / "crewhome" / ".vault" / "secrets.enc").is_file()
    assert (
        b"outlook-live-token" not in (tmp_path / "crewhome" / ".vault" / "secrets.enc").read_bytes()
    )
    return vault


class _Sender:
    """A controlled sender: returns a scripted reply per URL, records what it saw.

    It stands in for the network ONLY. Everything above it -- the four judgments,
    the binding-bound custody, the real vault read, the locator, the decode -- is
    the production path.
    """

    def __init__(self, replies: Mapping[str, HttpReply]) -> None:
        self._replies = dict(replies)
        self.urls: List[str] = []
        self.authorization: List[Optional[str]] = []

    def __call__(self, request: HttpRequest, *, timeout_seconds: float) -> HttpReply:
        self.urls.append(request.url)
        self.authorization.append(
            next((v for k, v in request.headers.items() if k.lower() == "authorization"), None)
        )
        return self._replies[request.url]

    @property
    def calls(self) -> int:
        return len(self.urls)


def _cursor_locator(base: str):
    """A vendor-shaped locator: the page cursor lands in the URL, filters survive."""

    def _locate(*, request_args: Mapping[str, Any], **_: Any) -> HttpRequest:
        cursor = request_args.get("cursor")
        suffix = f"?page={cursor}" if cursor else ""
        return HttpRequest(method="GET", url=f"{base}{suffix}", headers={"Accept": "*/*"})

    return _locate


# =============================================================================
# (1) COLLECTION -- items AND the cursor, across a real PageWalk
# =============================================================================
def _graph_collection_decode(reply: HttpReply) -> OperationResult:
    """A VENDOR-shaped decode: it knows this provider's cursor spelling.

    Exactly the injection seam the neutral module documents. It reads ``value``
    for the items and ``@odata.nextLink`` for the continuation, and passes that
    continuation to :func:`result_with_payload` as the explicit ``next_cursor`` --
    the ONE place a cursor lives. The payload carries items only.
    """

    body = decode_json_body(reply)
    items = tuple(item for item in body.get("value", []) if isinstance(item, Mapping))
    link = body.get("@odata.nextLink")
    cursor = link if isinstance(link, str) else None
    return result_with_payload(
        CollectionPayload(items=items),
        status="partial" if cursor else "ok",
        next_cursor=cursor,
    )


def test_a_consumer_receives_the_items_and_the_cursor_of_every_page(
    tmp_path: Path,
    real_vault: SecretVault,
) -> None:
    """The decision point for the COLLECTION shape: values, not presence.

    Three real pages through a real :class:`PageWalk` over the real production
    transport. Each page's items are asserted BY VALUE, the cursor is asserted as
    the exact string the provider sent, and the walk terminates on the page that
    sends none.
    """

    base = "https://graph.example.invalid/v1.0/me/messages"
    sender = _Sender(
        {
            base: HttpReply(
                status=200,
                headers={"Content-Type": "application/json"},
                body=b'{"value":[{"id":"m1","subject":"one"},{"id":"m2","subject":"two"}],'
                b'"@odata.nextLink":"CURSOR-P2"}',
            ),
            f"{base}?page=CURSOR-P2": HttpReply(
                status=200,
                headers={"Content-Type": "application/json"},
                body=b'{"value":[{"id":"m3","subject":"three"}],"@odata.nextLink":"CURSOR-P3"}',
            ),
            f"{base}?page=CURSOR-P3": HttpReply(
                status=200,
                headers={"Content-Type": "application/json"},
                body=b'{"value":[{"id":"m4","subject":"four"}]}',
            ),
        }
    )
    binding, handle = _bound()
    transport = build_production_transport(
        gate=_gate_for(binding, handle),
        store=_live_store(tmp_path, binding),
        vault=real_vault,
        locator=_cursor_locator(base),
        http_send=sender,
        decode=_graph_collection_decode,
    )
    walk = PageWalk(
        descriptor=_descriptor(),
        handle=handle,
        transport=transport,
        offered_mode="oauth_user",
        permitted=declare_permitted_modes(("oauth_user",)),
        layers=LayerCeilings(),
        governance_scope="tools",
        governance_item="messages.list",
        clock=lambda: _T0,
        base_args={"filter": "isRead eq false"},
    )

    collected: List[Mapping[str, Any]] = []
    cursors: List[Optional[str]] = []
    while not walk.done:
        outcome = walk.next()
        assert outcome.ok, outcome.error
        payload = outcome.payload
        # The consumer reads a COLLECTION, discriminable without isinstance too.
        assert isinstance(payload, CollectionPayload)
        assert payload.kind == "collection" and payload.kind in PAYLOAD_KINDS
        # Every page carried DATA -- this is the assertion the old path failed.
        assert payload.items, "a page arrived with no items"
        collected.extend(payload.items)
        # The ONE cursor: read off the envelope, because the payload has none.
        assert outcome.result is not None
        assert not hasattr(payload, "next_cursor")
        cursors.append(outcome.result["next_cursor"])

    # The exact records, in order, by value -- not a count, not a not-None.
    assert collected == [
        {"id": "m1", "subject": "one"},
        {"id": "m2", "subject": "two"},
        {"id": "m3", "subject": "three"},
        {"id": "m4", "subject": "four"},
    ]
    # The exact cursor strings the provider sent, terminal page included.
    assert cursors == ["CURSOR-P2", "CURSOR-P3", None]
    assert walk.pages == 3 and sender.calls == 3
    # The walk really advanced ON those cursors (the locator put them in the URL).
    assert sender.urls == [base, f"{base}?page=CURSOR-P2", f"{base}?page=CURSOR-P3"]


def test_the_cursor_lives_on_the_envelope_and_nowhere_else() -> None:
    """ITEM 1's pin: there is exactly ONE cursor, and it is the envelope's.

    A ``CollectionPayload`` carrying a second copy would invite the defence of
    "deliberate
    duplication written by one constructor". That defence only covers envelopes
    built through that constructor: an ``OperationResult`` is a ``TypedDict``, so a
    producer can build one literally and a middle layer can reassign
    ``next_cursor`` on the mapping it was handed. When the two disagree a walk
    stops early (records dropped) or repeats a page (records duplicated), and
    neither is distinguishable from a correct result at the seam. One field cannot
    disagree with itself.
    """

    # The attribute is GONE -- not None, not deprecated. Both spellings pinned,
    # because a dataclass field is visible on the class and on an instance.
    assert not hasattr(CollectionPayload, "next_cursor")
    assert not hasattr(CollectionPayload(items=({"id": "a"},)), "next_cursor")
    assert "next_cursor" not in {f.name for f in dataclasses.fields(CollectionPayload)}
    assert [f.name for f in dataclasses.fields(CollectionPayload)] == ["items"]

    # The cursor is an EXPLICIT argument, and it lands on the envelope only.
    envelope = result_with_payload(
        CollectionPayload(items=({"id": "a"},)), status="partial", next_cursor="NEXT-1"
    )
    assert envelope["next_cursor"] == "NEXT-1"
    assert isinstance(envelope["payload"], CollectionPayload)
    assert "next_cursor" not in dataclasses.asdict(envelope["payload"])

    # A terminal page: no cursor at all.
    assert result_with_payload(CollectionPayload(items=({"id": "b"},)))["next_cursor"] is None

    # The signature now HAS next_cursor -- keyword-only, defaulting to None.
    parameters = inspect.signature(result_with_payload).parameters
    assert parameters["next_cursor"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["next_cursor"].default is None


def test_a_cursor_on_a_shape_that_cannot_be_paged_is_refused() -> None:
    """A cursor with an object / bytes / nothing raises instead of being ignored.

    Quietly dropping it would lose a continuation a caller believed it returned;
    quietly keeping it would point a caller at a page that does not exist. Both are
    silent, so this is loud.
    """

    for payload in (ObjectPayload(object={"id": "m1"}), BytesPayload(data=b"PK"), None):
        with pytest.raises(ValueError, match="only meaningful for a CollectionPayload"):
            result_with_payload(payload, next_cursor="NOPE")
        # ... and with no cursor the same call is fine and reports None.
        assert result_with_payload(payload)["next_cursor"] is None


def test_a_page_walk_advances_purely_on_the_envelope_cursor(
    tmp_path: Path, real_vault: SecretVault
) -> None:
    """ITEM 1's second pin: the walk's only cursor source is the envelope.

    Driven with a decode that puts a cursor on the ENVELOPE and returns a payload
    that (by construction, since the field is gone) carries none. If ``PageWalk``
    consulted anything but ``OperationResult.next_cursor`` it would stop at page
    one; it reaches page two, and the URL proves the envelope's cursor is what it
    resumed on.
    """

    base = "https://graph.example.invalid/v1.0/me/messages"
    sender = _Sender(
        {
            base: HttpReply(
                status=200,
                headers={"Content-Type": "application/json"},
                body=b'{"value":[{"id":"m1"}],"@odata.nextLink":"ONLY-ON-THE-ENVELOPE"}',
            ),
            f"{base}?page=ONLY-ON-THE-ENVELOPE": HttpReply(
                status=200,
                headers={"Content-Type": "application/json"},
                body=b'{"value":[{"id":"m2"}]}',
            ),
        }
    )
    binding, handle = _bound()
    walk = PageWalk(
        descriptor=_descriptor(),
        handle=handle,
        transport=build_production_transport(
            gate=_gate_for(binding, handle),
            store=_live_store(tmp_path, binding),
            vault=real_vault,
            locator=_cursor_locator(base),
            http_send=sender,
            decode=_graph_collection_decode,
        ),
        offered_mode="oauth_user",
        permitted=declare_permitted_modes(("oauth_user",)),
        layers=LayerCeilings(),
        governance_scope="tools",
        governance_item="messages.list",
        clock=lambda: _T0,
    )
    first = walk.next()
    assert first.ok and first.result is not None
    assert first.result["next_cursor"] == "ONLY-ON-THE-ENVELOPE"
    assert not walk.done
    second = walk.next()
    assert second.ok and second.result is not None and second.result["next_cursor"] is None
    assert walk.done and walk.pages == 2
    assert sender.urls == [base, f"{base}?page=ONLY-ON-THE-ENVELOPE"]


# =============================================================================
# (2) SINGLE OBJECT -- the object itself
# =============================================================================
def _single_object_decode(reply: HttpReply) -> OperationResult:
    return result_with_payload(ObjectPayload(object=decode_json_body(reply)), status="ok")


def test_a_consumer_receives_the_single_object_with_its_fields(
    tmp_path: Path,
    real_vault: SecretVault,
) -> None:
    """The decision point for the SINGLE OBJECT shape."""

    url = "https://graph.example.invalid/v1.0/me/messages/m1"
    sender = _Sender(
        {
            url: HttpReply(
                status=200,
                headers={"Content-Type": "application/json"},
                body=b'{"id":"m1","subject":"Quarterly review","isRead":false,"webLink":"https://x/1"}',
            )
        }
    )
    binding, handle = _bound()
    transport = build_production_transport(
        gate=_gate_for(binding, handle),
        store=_live_store(tmp_path, binding),
        vault=real_vault,
        locator=_cursor_locator(url),
        http_send=sender,
        decode=_single_object_decode,
    )
    outcome = execute(_descriptor(), handle, transport, **_kw())

    assert outcome.ok
    payload = outcome.payload
    assert isinstance(payload, ObjectPayload) and payload.kind == "object"
    # The FIELDS, by value.
    assert payload.object["id"] == "m1"
    assert payload.object["subject"] == "Quarterly review"
    assert payload.object["isRead"] is False
    assert payload.object["webLink"] == "https://x/1"
    # A single object is not a paged shape, so no cursor is invented for it.
    assert outcome.result is not None and outcome.result["next_cursor"] is None
    # And it is NOT presented as a one-element collection.
    assert not isinstance(payload, CollectionPayload)


# =============================================================================
# (3) OFFICE BYTES -- byte-equal, never text-coerced, never dropped
# =============================================================================
def test_a_consumer_receives_office_bytes_byte_for_byte(
    tmp_path: Path, real_vault: SecretVault
) -> None:
    """The decision point for the OFFICE BYTES shape, on the NEUTRAL decode.

    No vendor decode is injected: the default
    :func:`~kiro_crew.connections.control_plane.production.neutral_decode` carries
    the bytes, which is what makes a binary download work on a provider nobody has
    written a decode for yet.
    """

    # The bytes are genuinely binary: a text coercion cannot survive them.
    with pytest.raises(UnicodeDecodeError):
        _XLSX_BYTES.decode("utf-8")

    url = "https://graph.example.invalid/v1.0/me/drive/items/w1/content"
    sender = _Sender(
        {
            url: HttpReply(
                status=200,
                headers={
                    "Content-Type": _XLSX_MEDIA_TYPE,
                    "Content-Disposition": 'attachment; filename="Q3.xlsx"',
                },
                body=_XLSX_BYTES,
            )
        }
    )
    binding, handle = _bound()
    transport = build_production_transport(
        gate=_gate_for(binding, handle),
        store=_live_store(tmp_path, binding),
        vault=real_vault,
        locator=_cursor_locator(url),
        http_send=sender,
        # the default decode -- stated explicitly so the test says what it means
        decode=neutral_decode,
    )
    outcome = execute(_descriptor(), handle, transport, **_kw())

    assert outcome.ok
    payload = outcome.payload
    assert isinstance(payload, BytesPayload) and payload.kind == "bytes"
    # THE assertion: byte-equal, not "is not None", not a length, not a prefix.
    assert payload.data == _XLSX_BYTES
    assert type(payload.data) is bytes
    assert len(payload.data) == len(_XLSX_BYTES)
    # Every byte survived, NUL and the invalid UTF-8 tail included.
    assert b"\x00" in payload.data and payload.data.endswith(b"PK\x05\x06")
    assert payload.data[:4] == b"PK\x03\x04"
    # The media type is REPORTED from the header, and the offered name carried.
    assert payload.media_type == _XLSX_MEDIA_TYPE
    assert payload.filename == "Q3.xlsx"


def test_bytes_with_no_declared_media_type_are_still_carried(
    tmp_path: Path, real_vault: SecretVault
) -> None:
    """A provider that declares nothing does not cost the caller its bytes."""

    url = "https://graph.example.invalid/v1.0/me/drive/items/w2/content"
    sender = _Sender({url: HttpReply(status=200, headers={}, body=_XLSX_BYTES)})
    binding, handle = _bound()
    transport = build_production_transport(
        gate=_gate_for(binding, handle),
        store=_live_store(tmp_path, binding),
        vault=real_vault,
        locator=_cursor_locator(url),
        http_send=sender,
    )
    payload = execute(_descriptor(), handle, transport, **_kw()).payload
    assert isinstance(payload, BytesPayload)
    assert payload.data == _XLSX_BYTES
    assert payload.media_type == DEFAULT_MEDIA_TYPE
    assert payload.filename is None


def test_a_reply_that_returned_nothing_carries_no_payload(
    tmp_path: Path, real_vault: SecretVault
) -> None:
    """The honest empty: a 204 reads ``payload is None``, not empty bytes.

    Distinguishing "returned nothing" from "returned bytes we dropped" is the
    whole point of the channel; an empty ``BytesPayload`` would have collapsed the
    two back together.
    """

    url = "https://graph.example.invalid/v1.0/me/messages/m9"
    sender = _Sender({url: HttpReply(status=204, headers={}, body=b"")})
    binding, handle = _bound()
    transport = build_production_transport(
        gate=_gate_for(binding, handle),
        store=_live_store(tmp_path, binding),
        vault=real_vault,
        locator=_cursor_locator(url),
        http_send=sender,
    )
    outcome = execute(_descriptor(), handle, transport, **_kw())
    assert outcome.ok and outcome.payload is None
    assert outcome.result == {"status": "ok", "next_cursor": None, "payload": None}


# =============================================================================
# the data channel did not weaken the gate: a denied call still emits NOTHING
# =============================================================================
def test_a_denied_gate_reaches_neither_the_sender_the_vault_nor_a_payload(
    tmp_path: Path,
    real_vault: SecretVault,
) -> None:
    """calls == 0 on deny, now measured through the composition that carries data.

    The invariant is already pinned in the executor suite against a fake
    transport; re-asserting it HERE is what proves the data channel did not move
    the gate. The denial is a governance one (an unknown scope is deny-by-default),
    so it lands at judgment 3 -- after the trusted view resolved and before
    anything is emitted.
    """

    url = "https://graph.example.invalid/v1.0/me/messages"
    sender = _Sender({url: HttpReply(status=200, body=b'{"value":[{"id":"leaked"}]}')})
    binding, handle = _bound()
    transport = build_production_transport(
        gate=_gate_for(binding, handle),
        store=_live_store(tmp_path, binding),
        vault=real_vault,
        locator=_cursor_locator(url),
        http_send=sender,
        decode=_graph_collection_decode,
    )
    outcome = execute(
        _descriptor(),
        handle,
        transport,
        **_kw(governance_scope="definitely.not.a.catalog.scope", governance_item="x"),
    )

    assert outcome.error is not None
    assert not outcome.ok
    # Nothing was emitted, and there is no payload to have leaked.
    assert sender.calls == 0
    assert outcome.payload is None and outcome.result is None
    # The trusted view still resolved, so an audit sees the routing axes.
    assert outcome.view is not None and outcome.view.service_id == "outlook"


def test_the_schema_versions_the_downstreams_pin() -> None:
    assert RESULT_SCHEMA_VERSION == 3
    assert EXECUTOR_SCHEMA_VERSION == 4


def test_the_kind_constants_match_the_kinds_the_classes_actually_carry() -> None:
    """The constants and the literals the dataclasses hold cannot drift apart.

    Each class hardcodes its own ``kind`` literal (a ``ClassVar`` annotated with
    the ``Literal`` type, which is what keeps mypy narrowing it), so the named
    constants are a SECOND spelling of the same three values. Pinned here rather
    than trusted -- the same guard the production suite applies to
    ``RESULT_STATUS_OK`` / ``RESULT_STATUS_PARTIAL``.
    """

    assert CollectionPayload.kind == PAYLOAD_KIND_COLLECTION == "collection"
    assert ObjectPayload.kind == PAYLOAD_KIND_OBJECT == "object"
    assert BytesPayload(data=b"").kind == PAYLOAD_KIND_BYTES == "bytes"
    assert PAYLOAD_KINDS == (PAYLOAD_KIND_COLLECTION, PAYLOAD_KIND_OBJECT, PAYLOAD_KIND_BYTES)
    # Exactly three shapes, and the union has exactly those three members.
    assert len(PAYLOAD_KINDS) == len(set(PAYLOAD_KINDS)) == 3
    assert set(get_args(OperationPayload)) == {CollectionPayload, ObjectPayload, BytesPayload}


def test_the_payload_types_are_reachable_on_the_control_plane_only() -> None:
    """The canonical export face, and no leak onto ``kiro_crew.connections``."""

    import kiro_crew.connections as connections
    import kiro_crew.connections.control_plane as cp

    for name in (
        "BytesPayload",
        "CollectionPayload",
        "DEFAULT_MEDIA_TYPE",
        "ObjectPayload",
        "OperationPayload",
        "PAYLOAD_KINDS",
        "PAYLOAD_KIND_BYTES",
        "PAYLOAD_KIND_COLLECTION",
        "PAYLOAD_KIND_OBJECT",
        "PayloadKind",
        "result_with_payload",
    ):
        assert hasattr(cp, name), name
        assert cp.__all__.count(name) == 1, name
        assert name not in connections.__all__, f"{name} leaked into connections.__all__"
    # ``connections/__init__.py`` is untouched by this change: the control plane
    # stays the canonical export face, exactly as it was before the payload landed.
    assert len(cp.__all__) == len(set(cp.__all__))


def test_two_bindings_of_one_provider_get_distinct_names_and_cannot_read_each_others_token(
    tmp_path: Path,
) -> None:
    """F1's real requirement: per-binding isolation from the CONSTRUCTOR's records.

    Two bindings of the SAME provider (different verified subject/tenant) get two
    DIFFERENT ``secret_ref`` names straight from ``create_binding`` -- no name is
    stamped by the test. Each transport resolves ITS OWN per-binding entry from the
    live store, so binding B reaches B's token and never A's, and vice versa. A
    slug-keyed scheme (one name for both) could not express this.
    """

    from kiro_crew.connections.control_plane.binding import binding_secret_ref, create_binding

    binding_a = create_binding(
        service_id="outlook",
        claimed_subject="alice",
        claimed_tenant="acme",
        credential_mode="oauth_user",
        verifier=_verifier,
        slug="outlook",  # type: ignore[arg-type]
    )
    binding_b = create_binding(
        service_id="outlook",
        claimed_subject="bob",
        claimed_tenant="globex",
        credential_mode="oauth_user",
        verifier=_verifier,
        slug="outlook",  # type: ignore[arg-type]
    )
    name_a = binding_a["secret_ref"]["name"]
    name_b = binding_b["secret_ref"]["name"]
    slug_name = binding_secret_ref("outlook")["name"]
    # Distinct per-binding names, neither equal to the shared slug name.
    assert name_a != name_b
    assert slug_name not in (name_a, name_b)

    vault = SecretVault(tmp_path / "crewhome")
    vault.set_sync(name_a, "token-alice")
    vault.set_sync(name_b, "token-bob")
    vault.set_sync(slug_name, "token-collapsed-by-slug")

    store = BindingStore(tmp_path / "connections" / "control_plane_bindings.json")
    store.insert(binding_a, deployment_id="deployment://t/a", kiro_principal="kiro://t/owner")
    store.insert(binding_b, deployment_id="deployment://t/b", kiro_principal="kiro://t/owner")

    handle_a = derive_handle(
        binding_a,
        granted_scopes=_GRANTED,
        requested_scopes=("mail.read",),
        now=_T0,
        ttl_seconds=300.0,
    )
    handle_b = derive_handle(
        binding_b,
        granted_scopes=_GRANTED,
        requested_scopes=("mail.read",),
        now=_T0,
        ttl_seconds=300.0,
    )

    seen: List[str] = []

    def _send(request: HttpRequest, *, timeout_seconds: float) -> HttpReply:
        seen.append(request.headers["Authorization"])
        return HttpReply(
            status=200, headers={"Content-Type": "application/json"}, body=b'{"id":"x"}'
        )

    for binding, handle in ((binding_a, handle_a), (binding_b, handle_b)):
        t = build_production_transport(
            gate=_gate_for(binding, handle),
            store=store,
            vault=vault,
            locator=_cursor_locator("https://graph.example.invalid/v1/me"),
            http_send=_send,
            decode=_single_object_decode,
        )
        assert execute(_descriptor(), handle, t, **_kw()).error is None

    # Each binding reached ITS OWN token; neither read the other's, nor the slug's.
    assert seen == ["Bearer token-alice", "Bearer token-bob"]
    assert "Bearer token-collapsed-by-slug" not in seen
