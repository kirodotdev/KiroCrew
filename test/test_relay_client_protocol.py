"""The relay client's frames against the protocol v1 fixtures, field by field.

The fixtures under ``test/fixtures/relay_protocol_v1`` are copied unchanged from the
contract, so the relay (built from the same files) and this client cannot drift
apart without one of these tests failing.
"""

from __future__ import annotations

import json

import pytest
from relay_client_helpers import CONTRACT, FIXTURES, fixture

from kiro_crew.relay_client import protocol
from kiro_crew.relay_client.protocol import FrameError, parse_inbound

#: Invalid examples a receiver accepts or never sees, and why.
_NOT_REFUSED_ON_RECEIPT = {
    # The relay itself rewrites a Crew close code in its block; receiving one is legal.
    ("frames/ws-close", "code in the relay's block"),
    # An unknown reason is read as max-age.
    ("frames/reconnect", "unknown reason"),
}

#: The frames the relay sends to a host.
_INBOUND = [
    path.relative_to(FIXTURES).with_suffix("").as_posix()
    for path in sorted((FIXTURES / "frames").glob("*.json"))
    if json.loads(path.read_text(encoding="utf-8"))["direction"] in ("relay-to-host", "both")
]


@pytest.mark.parametrize(
    "url",
    ["ws://127.0.0.2:9/v1/host", "ws://[0:0:0:0:0:0:0:1]:9/v1/host"],
)
def test_host_url_accepts_loopback_address_identity(url: str) -> None:
    assert protocol.host_url_problem(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "ws://10.0.0.1/v1/host",
        "ws://localhost.example/v1/host",
        "ws://127.0.0.1.example/v1/host",
        "ws://[::ffff:127.0.0.1]/v1/host",
    ],
)
def test_host_url_refuses_non_loopback_without_tls(url: str) -> None:
    assert protocol.host_url_problem(url) is not None
    assert protocol.host_url_problem(url.replace("ws://", "wss://", 1)) is None


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost", True),
        ("LOCALHOST", True),
        ("127.0.0.1", True),
        ("127.0.0.2", True),
        ("::1", True),
        ("[::1]", True),
        ("0:0:0:0:0:0:0:1", True),
        ("10.0.0.1", False),
        ("0.0.0.0", False),
        ("::", False),
        ("::ffff:127.0.0.1", False),
        ("localhost.example", False),
        ("127.0.0.1.example", False),
        ("my-host", False),
        ("[::1", False),
        ("", False),
    ],
)
def test_is_loopback_host_checks_identity(host: str, expected: bool) -> None:
    assert protocol.is_loopback_host(host) is expected


def _all_fixtures() -> list[str]:
    return sorted(
        p.relative_to(FIXTURES).with_suffix("").as_posix() for p in FIXTURES.rglob("*.json")
    )


@pytest.mark.parametrize("name", _all_fixtures())
def test_every_copied_fixture_is_the_v1_contract_under_its_own_name(name: str) -> None:
    data = fixture(name)
    assert data["contract"] == CONTRACT
    assert data["protocolVersion"] == protocol.PROTOCOL_VERSION == 1
    assert data["fixture"] == name


def test_the_copied_set_covers_every_frame_the_host_leg_uses() -> None:
    frames = {n.split("/", 1)[1] for n in _all_fixtures() if n.startswith("frames/")}
    assert frames >= {"hello", "ready", "token", "ping", "pong", "reconnect", "error"}
    assert frames >= {"request", "request-chunk", "request-end", "request-cancel"}
    assert frames >= {"response", "response-chunk", "response-end"}
    assert frames >= {"ws-upgrade", "ws-frame", "ws-close", "flow-credit", "ws-flow", "opaque"}


@pytest.mark.parametrize("name", [n for n in _INBOUND if n != "frames/opaque"])
def test_every_valid_inbound_frame_parses(name: str) -> None:
    for case in fixture(name)["valid"]:
        parsed = parse_inbound(json.dumps(case["message"]))
        assert parsed["type"] == case["message"]["type"], case["name"]


@pytest.mark.parametrize("name", _INBOUND)
def test_every_invalid_inbound_frame_is_refused(name: str) -> None:
    for case in fixture(name).get("invalid", []):
        if (name, case["name"]) in _NOT_REFUSED_ON_RECEIPT:
            parse_inbound(json.dumps(case["message"]))
            continue
        with pytest.raises(FrameError) as caught:
            parse_inbound(json.dumps(case["message"]))
        expect = case["expect"]
        if "unsupported_frame" in expect:
            assert caught.value.code == "unsupported_frame", case["name"]
        elif "invalid_message" in expect:
            assert caught.value.code == "invalid_message", case["name"]
        if "dropped and logged" in expect:
            assert caught.value.answer is False


def test_opaque_is_never_agreed_in_version_1() -> None:
    for case in fixture("frames/opaque")["valid"]:
        with pytest.raises(FrameError) as caught:
            parse_inbound(json.dumps(case["message"]))
        assert caught.value.code == "unsupported_frame"
        assert caught.value.stream_id == case["message"]["id"]


def test_a_stream_error_names_its_stream() -> None:
    message = fixture("frames/request")["invalid"][0]["message"]
    with pytest.raises(FrameError) as caught:
        parse_inbound(json.dumps(message))
    assert caught.value.stream_id == message["id"]


def test_the_hello_the_client_builds_matches_the_fixture() -> None:
    shape = fixture("frames/hello")
    hello = protocol.build_hello(client_version="9.9.9")
    required = {k for k, v in shape["fields"].items() if v["required"]}
    assert required <= set(hello) <= set(shape["fields"])
    assert hello["features"] == ["flow-credit", "ws-flow", "request-credit"]
    assert "opaque" not in hello["features"]
    # Every feature offered is one the hello fixture names as known in version 1.
    known = shape["fields"]["features"]["notes"]
    assert all(feature in known for feature in hello["features"])


def test_request_credit_numbers_match_the_fixture() -> None:
    numbers = fixture("frames/flow-credit")["numbers"]
    assert protocol.REQUEST_CREDIT_INITIAL_BYTES == numbers["requestCreditInitialBytes"]
    assert protocol.MAX_CREDIT_BYTES == numbers["maxCreditBytes"]
    # A relay that spent all its credit is always answered once the Crew takes it.
    assert 1 <= protocol.REQUEST_CREDIT_GRANT_BYTES <= protocol.REQUEST_CREDIT_INITIAL_BYTES


def test_the_request_body_credit_the_client_builds_matches_the_fixture() -> None:
    (case,) = [
        c
        for c in fixture("frames/flow-credit")["valid"]
        if c["name"] == "Crew grants request-body credit"
    ]
    message = case["message"]
    assert protocol.build_flow_credit(message["id"], message["credit"]) == message
    for credit in (0, -1, protocol.MAX_CREDIT_BYTES + 1):
        with pytest.raises(ValueError):
            protocol.build_flow_credit(message["id"], credit)


def test_the_response_heads_the_client_checks() -> None:
    for case in fixture("frames/response")["valid"]:
        assert protocol.response_head_problem(case["message"]) is None, case["name"]
    for case in fixture("frames/response")["invalid"]:
        assert protocol.response_head_problem(case["message"]) is not None, case["name"]


def test_a_crew_close_code_never_reaches_the_wire_inside_the_relays_block() -> None:
    assert "code" not in protocol.build_ws_close(9, 4404)
    assert "code" not in protocol.build_ws_close(9, 1006)
    assert protocol.build_ws_close(9, 4001)["code"] == 4001
    assert protocol.build_ws_close(9, 1000)["code"] == 1000
    long_reason = protocol.build_ws_close(9, 1000, "é" * 100)["reason"]
    assert len(long_reason.encode("utf-8")) <= 123


def test_size_limits_match_the_fixture() -> None:
    sizes = fixture("limits/sizes")["sizes"]
    assert protocol.MAX_TEXT_FRAME_BYTES == sizes["maxTextFrameBytes"]
    assert protocol.WS_PIECE_BYTES == sizes["wsFramePieceBytes"]
    assert protocol.BODY_CHUNK_RAW_BYTES == sizes["bodyChunkRawBytes"]
    assert protocol.WS_MESSAGE_MAX_BYTES == sizes["wsMessageReassembledBytes"]
    assert protocol.WS_REASSEMBLY_PER_CONNECTION_BYTES == sizes["wsReassemblyPerConnectionBytes"]
    assert protocol.WS_REASSEMBLY_CONCURRENT == sizes["wsReassemblyConcurrent"]
    assert protocol.WS_REASSEMBLY_PIECE_TIMEOUT_SECONDS == sizes["wsReassemblyPieceTimeoutSeconds"]
    assert protocol.SET_COOKIES_MAX == sizes["setCookiesMax"]
    assert protocol.SET_COOKIE_MAX_CHARS == sizes["setCookieMaxChars"]
    assert protocol.WS_UPGRADE_HEADERS_MAX == sizes["wsUpgradeHeadersMax"]
    assert protocol.HEADER_NAME_MAX_CHARS == sizes["headerNameMaxChars"]
    assert protocol.HEADER_VALUE_MAX_CHARS == sizes["headerValueMaxChars"]
    assert protocol.WS_CLOSE_REASON_BYTES == sizes["wsCloseReasonBytes"]


def test_identifier_grammars_match_the_fixture() -> None:
    ids = fixture("limits/identifiers")["identifiers"]
    assert protocol.USER_ID_RE.pattern.strip("\\AZ") == ids["kiroUserId"]["pattern"].strip("^$")
    for value in ids["kiroUserId"]["valid"]:
        assert protocol.USER_ID_RE.match(value)
    for value in ids["kiroUserId"]["invalid"]:
        assert not protocol.USER_ID_RE.match(value)
    for value in ids["endpointId"]["valid"]:
        assert protocol.ENDPOINT_ID_RE.match(value)
    for value in ids["endpointId"]["invalid"]:
        assert not protocol.ENDPOINT_ID_RE.match(value)


def test_the_caller_header_never_comes_from_the_relayed_headers() -> None:
    from kiro_crew.dashboard.kiro_relay.caller import RELAY_USER_HEADER, CrewKind, HostBinding
    from kiro_crew.dashboard.kiro_relay.ingress import RelayIngress
    from kiro_crew.dashboard.relay_headers import is_relay_header
    from kiro_crew.relay_client.ingress import LoopbackIngress

    stamp = fixture("headers/caller-stamp")
    # The contract's caller header is the Crew's one shared definition of it.
    assert stamp["header"] == RELAY_USER_HEADER
    assert is_relay_header(stamp["header"])

    gateway = RelayIngress(enabled=True)
    ingress = LoopbackIngress("127.0.0.1", 9, gateway=gateway, max_ws_message_bytes=1)
    gateway.bind_host(
        HostBinding(crew_kind=CrewKind.PERSONAL, owner_user_id="u-0001.example-owner")
    )
    case = stamp["cases"][0]
    relayed = [*case["head"]["headers"].items(), (RELAY_USER_HEADER, "u-0003.spoofed")]
    out = ingress._stamped(relayed, case["head"]["user"])
    # The stamp overwrites a client value rather than sitting beside it.
    assert out.getall(RELAY_USER_HEADER) == [case["toCrewWebServer"][RELAY_USER_HEADER]]
    assert out["accept"] == case["toCrewWebServer"]["accept"]


def test_a_team_crew_binds_with_no_owner_and_a_personal_one_needs_one() -> None:
    from kiro_crew.relay_client.token_source import HostCredential, host_binding

    assert host_binding(HostCredential("a.b.c", crew_kind="team")) == ("team", "")
    assert host_binding(HostCredential("a.b.c", crew_kind="personal")) is None
    assert host_binding(HostCredential("a.b.c", crew_kind="personal", owner_user_id="u-1")) == (
        "personal",
        "u-1",
    )


def test_the_binding_reads_the_host_token_claims_when_the_source_names_none() -> None:
    import base64

    from kiro_crew.relay_client.token_source import HostCredential, host_binding

    upgrade = fixture("headers/host-upgrade")
    token = upgrade["cases"][0]["request"]["headers"]["authorization"].split(" ", 1)[1]
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert host_binding(HostCredential(token)) == (claims["kind"], claims["sub"])


def test_the_host_upgrade_uses_a_bearer_host_token() -> None:
    upgrade = fixture("headers/host-upgrade")
    assert "Authorization: Bearer" in upgrade["rule"]
    assert upgrade["cases"][0]["request"]["url"].startswith("wss://kiro-relay.")


@pytest.mark.parametrize(
    ("char", "count"),
    [("x", 200_000), ("é", 60_000), ("\x00", 20_000), ("😀", 30_000), ("short", 1)],
    ids=["ascii", "two-byte", "escaped-control", "astral", "one-frame"],
)
def test_large_ws_messages_split_into_frames_under_the_limit(char: str, count: int) -> None:
    data = char * count
    pieces = protocol.split_ws_data(data)
    assert "".join(pieces) == data
    for piece in pieces:
        frame = {"type": "ws-frame", "id": 9, "data": piece, "partial": True}
        assert len(protocol.encode(frame).encode("utf-8")) <= protocol.WS_PIECE_BYTES


@pytest.mark.parametrize(
    "url",
    [
        "ws://relay.example/v1/host",
        "http://relay.example/v1/host",
        "wss:///v1/host",
        "not a url",
    ],
    ids=["clear-text-remote", "not-websocket", "no-host", "garbage"],
)
def test_a_credential_never_carries_an_unsafe_dial_url(url: str) -> None:
    from kiro_crew.relay_client.token_source import HostCredential

    with pytest.raises(ValueError):
        HostCredential("a.b.c", host_url=url)


@pytest.mark.parametrize(
    "url",
    [
        "http://k7qm3x2vtzr4hbn6cwapfe5dj3.kiro-relay.us-east-1.on.aws",
        "https://k7qm3x2vtzr4hbn6cwapfe5dj3.kiro-relay.us-east-1.on.aws/path",
        "https://user@k7qm3x2vtzr4hbn6cwapfe5dj3.kiro-relay.us-east-1.on.aws",
        "https://k7qm3x2vtzr4hbn6cwapfe5dj3.kiro-relay.us-east-1.on.aws?x=1",
        "https://*.example",
    ],
    ids=["clear-text", "path", "userinfo", "query", "wildcard"],
)
def test_a_credential_never_carries_a_crew_url_that_is_not_an_origin(url: str) -> None:
    from kiro_crew.relay_client.token_source import HostCredential

    with pytest.raises(ValueError):
        HostCredential("a.b.c", crew_url=url)


def test_the_lease_shapes_of_both_urls_are_accepted() -> None:
    from kiro_crew.relay_client.token_source import HostCredential

    HostCredential(
        "a.b.c",
        host_url="wss://kiro-relay.us-east-1.api.aws/v1/host",
        crew_url="https://k7qm3x2vtzr4hbn6cwapfe5dj3.kiro-relay.us-east-1.on.aws/",
    )
    HostCredential("a.b.c", host_url="ws://127.0.0.1:9/v1/host")


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://k7qm3x2vtzr4hbn6cwapfe5dj3.kiro-relay.us-east-1.on.aws/", None),
        ("https://K7QM3X2VTZR4HBN6CWAPFE5DJ3.Kiro-Relay.us-east-1.on.aws", None),
        ("https://k7qm3x2vtzr4hbn6cwapfe5dj3.kiro-relay.us-east-1.on.aws:443/", None),
        (
            "https://k7qm3x2vtzr4hbn6cwapfe5dj3.kiro-relay.us-east-1.on.aws:8443",
            "https://k7qm3x2vtzr4hbn6cwapfe5dj3.kiro-relay.us-east-1.on.aws:8443",
        ),
    ],
    ids=["trailing-slash", "upper-case", "default-port", "other-port"],
)
def test_the_crew_origin_is_what_a_browser_sends(url: str, origin: str) -> None:
    expected = origin or "https://k7qm3x2vtzr4hbn6cwapfe5dj3.kiro-relay.us-east-1.on.aws"
    assert protocol.crew_url_problem(url) is None
    assert protocol.crew_origin(url) == expected


def test_a_crew_url_with_a_port_that_is_not_a_number_is_refused() -> None:
    assert protocol.crew_url_problem("https://crew.example:http") is not None


def _request_with(headers: dict) -> dict:
    message = dict(fixture("frames/request")["valid"][1]["message"])
    message["headers"] = headers
    return message


@pytest.mark.parametrize(
    "headers",
    [
        {"content-length": "5x"},
        {"content-length": " 5"},
        {"content-length": "-1"},
        {"content-length": "5, 5"},
        {"content-length": ""},
        {"content-length": "5", "Content-Length": "5"},
        {"content-length": "5", "transfer-encoding": "chunked"},
    ],
    ids=["suffix", "space", "negative", "list", "empty", "repeated", "with-chunked"],
)
def test_a_body_length_that_reads_two_ways_is_refused(headers: dict) -> None:
    with pytest.raises(FrameError) as caught:
        parse_inbound(json.dumps(_request_with(headers)))
    assert caught.value.code == "invalid_message"
    assert caught.value.stream_id == 8


def test_a_plain_body_length_is_read_from_the_head() -> None:
    parsed = parse_inbound(json.dumps(_request_with({"content-length": "52428800"})))
    assert protocol.declared_content_length(parsed["headers"]) == 52428800
    assert protocol.declared_content_length({"accept": "*/*"}) is None


def test_expect_never_crosses_to_the_crew() -> None:
    from kiro_crew.relay_client.ingress import forwardable_request_headers

    out = forwardable_request_headers(
        [("Expect", "100-continue"), ("content-length", "5"), ("accept", "*/*")]
    )
    assert out == [("content-length", "5"), ("accept", "*/*")]


@pytest.mark.asyncio
async def test_each_hand_off_gets_its_own_loopback_connection() -> None:
    from kiro_crew.dashboard.kiro_relay.ingress import RelayIngress
    from kiro_crew.relay_client.ingress import LoopbackIngress

    ingress = LoopbackIngress(
        "127.0.0.1", 9, gateway=RelayIngress(enabled=True), max_ws_message_bytes=1
    )
    try:
        assert ingress._client().connector.force_close is True
    finally:
        await ingress.aclose()


@pytest.mark.asyncio
async def test_loopback_connector_has_no_cap_besides_host_stream_admission() -> None:
    from kiro_crew.dashboard.kiro_relay.ingress import RelayIngress
    from kiro_crew.relay_client.ingress import LoopbackIngress

    ingress = LoopbackIngress(
        "127.0.0.1", 9, gateway=RelayIngress(enabled=True), max_ws_message_bytes=1
    )
    try:
        connector = ingress._client().connector
        assert connector.limit == 0
        assert connector.force_close is True
    finally:
        await ingress.aclose()


@pytest.mark.parametrize("kind", [[], {}, 1, None], ids=["list", "object", "int", "null"])
@pytest.mark.parametrize("sid", [0, 7, -1, True, "7", [], {}, None])
def test_non_string_frame_types_are_refused_without_losing_stream_id(kind, sid) -> None:
    with pytest.raises(FrameError) as caught:
        parse_inbound(json.dumps({"type": kind, "id": sid}))
    assert caught.value.code == "unsupported_frame"
    assert caught.value.stream_id == (sid if type(sid) is int and sid >= 0 else None)


def test_json_past_the_decoder_recursion_limit_is_a_frame_error() -> None:
    import gc

    text = "[" * 20000 + "]" * 20000
    assert len(text.encode("utf-8")) < protocol.MAX_TEXT_FRAME_BYTES
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        with pytest.raises(FrameError) as caught:
            parse_inbound(text)
        assert caught.value.code == "invalid_message"
        assert caught.value.stream_id is None
    finally:
        if was_enabled:
            gc.enable()
        gc.collect()


@pytest.mark.parametrize("error_type", [TypeError, ValueError, RecursionError])
@pytest.mark.parametrize("sid", [0, 7, -1, True, "7", [], {}, None])
def test_validator_exceptions_are_typed_frame_errors(monkeypatch, error_type, sid) -> None:
    # Current checks guard their field shapes; inject at the shared validator seam.
    def broken_check(frame):
        raise error_type("validator failed")

    monkeypatch.setitem(protocol._CHECKS, "ping", broken_check)
    with pytest.raises(FrameError) as caught:
        parse_inbound(json.dumps({"type": "ping", "id": sid}))
    assert caught.value.code == "invalid_message"
    assert caught.value.frame_type == "ping"
    assert caught.value.stream_id == (sid if type(sid) is int and sid >= 0 else None)


def test_validator_frame_errors_keep_their_existing_refusal(monkeypatch) -> None:
    original = FrameError("unsupported_frame", "unchanged", stream_id=9, answer=False)

    def refused_check(frame):
        raise original

    monkeypatch.setitem(protocol._CHECKS, "ping", refused_check)
    with pytest.raises(FrameError) as caught:
        parse_inbound('{"type":"ping"}')
    assert caught.value is original
    assert (original.code, original.message, original.stream_id, original.answer) == (
        "unsupported_frame",
        "unchanged",
        9,
        False,
    )
    assert original.frame_type == "ping"
