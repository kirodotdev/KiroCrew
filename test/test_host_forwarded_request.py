"""A port forward a container's host delivers into its loopback serves app web origins.

Some development containers run their SSH server on the host, so an ``ssh -L``
forward reaches the container's ``127.0.0.1`` from the host's end of a
link-local link. ``origin.is_host_forwarded_request`` admits that shape for app
web origins only; every other peer keeps being refused.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.origin import is_direct_local_request, is_host_forwarded_request
from kiro_crew.shared_crew.contracts import SSM_REQUEST_KEY

_HOST_SIDE = "169.254.255.193"


def _request(
    peer: str, sockname: object = ("127.0.0.1", 5476), headers: dict[str, str] | None = None
):
    info = {"peername": (peer, 50000), "sockname": sockname}
    transport = SimpleNamespace(get_extra_info=lambda name, default=None: info.get(name, default))
    return make_mocked_request(
        "GET",
        "/",
        headers={"Host": "demo.apps.localhost:5476", **(headers or {})},
        transport=transport,
    )


def test_a_link_local_peer_on_the_loopback_socket_is_a_host_forward() -> None:
    req = _request(_HOST_SIDE)
    assert is_host_forwarded_request(req) is True
    # The general gate is unchanged: secret reveal and config writes still refuse it.
    assert is_direct_local_request(req) is False


@pytest.mark.parametrize(
    "peer, sockname",
    [
        ("10.0.0.5", ("127.0.0.1", 5476)),  # a routable peer
        ("203.0.113.9", ("127.0.0.1", 5476)),
        (_HOST_SIDE, ("169.254.255.194", 5476)),  # a widened bind, not loopback
        (_HOST_SIDE, ("0.0.0.0", 5476)),
        (_HOST_SIDE, "/run/dashboard.sock"),  # a unix socket has no address pair
        (_HOST_SIDE, None),
        ("not-an-address", ("127.0.0.1", 5476)),
    ],
)
def test_any_other_peer_or_socket_is_refused(peer: str, sockname: object) -> None:
    assert is_host_forwarded_request(_request(peer, sockname)) is False


@pytest.mark.parametrize("header", ["Forwarded", "X-Forwarded-For", "X-Real-IP"])
def test_a_forwarding_header_is_refused(header: str) -> None:
    assert is_host_forwarded_request(_request(_HOST_SIDE, headers={header: "x"})) is False


def test_a_session_manager_request_is_refused() -> None:
    req = _request(_HOST_SIDE)
    req[SSM_REQUEST_KEY] = True
    assert is_host_forwarded_request(req) is False
