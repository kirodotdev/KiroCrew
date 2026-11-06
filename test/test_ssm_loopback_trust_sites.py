"""An SSM guest is never trusted as "this machine's own user".

A Session Manager port forward reaches the gateway from 127.0.0.1 with no
forwarding header, which is what the owner's own browser looks like. Two things
keep the guest out of every host-owner capability:

* the predicate most gates use, ``origin.is_direct_local_request``, answers
  False for a request the SSM listener admitted (and so does every raw
  loopback test that could otherwise be the only check);
* this file lists every place that tests a peer address as "local" and says
  why each one is safe. A new site fails ``test_every_loopback_trust_site_is_classified``
  until someone classifies it here.
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.origin import is_direct_local_request, is_ssm_request
from kiro_crew.shared_crew.contracts import SSM_GUEST_REQUEST_KEY, SSM_REQUEST_KEY
from kiro_crew.shared_crew.ssm_invites import SsmGuest

SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"


def _request(**extra: object):
    transport = SimpleNamespace(
        get_extra_info=lambda name, default=None: (
            ("127.0.0.1", 50000) if name == "peername" else default
        )
    )
    req = make_mocked_request("GET", "/", headers={"Host": "localhost"}, transport=transport)
    for key, value in extra.items():
        req[key] = value
    return req


def test_a_plain_loopback_request_is_still_direct_local() -> None:
    assert is_direct_local_request(_request()) is True


@pytest.mark.parametrize(
    "marker",
    [{SSM_REQUEST_KEY: True}, {SSM_GUEST_REQUEST_KEY: SsmGuest("abc123", "Dana")}],
    ids=["admitted-mark", "guest-mark"],
)
def test_an_ssm_request_is_never_direct_local(marker: dict[str, object]) -> None:
    req = _request(**marker)
    assert is_ssm_request(req) is True
    assert is_direct_local_request(req) is False


def test_the_marker_keys_are_the_ones_the_gate_sets() -> None:
    from kiro_crew.dashboard import ssm_gate

    source = (SRC / "dashboard" / "ssm_gate.py").read_text()
    assert "SSM_REQUEST_KEY" in source and "SSM_GUEST_REQUEST_KEY" in source
    assert ssm_gate.SSM_REQUEST_KEY == SSM_REQUEST_KEY == "auth_ssm"


def test_the_raw_loopback_checks_on_update_and_frame_routes_refuse_an_ssm_request() -> None:
    from kiro_crew.dashboard.handlers.updates_step_up import _loopback_peer

    assert _loopback_peer(_request()) is True
    assert _loopback_peer(_request(**{SSM_REQUEST_KEY: True})) is False
    # Even an internal-auth mark does not rescue a request the SSM gate admitted.
    assert _loopback_peer(_request(**{SSM_REQUEST_KEY: True, "internal_auth": True})) is False


def test_an_ssm_request_mints_no_bearer_link() -> None:
    """A token minted for a guest would outlive a revoke and work on the dashboard port."""
    from kiro_crew.dashboard.handlers._shared_access import _caller_bounds

    plain = _request(auth_token="")
    assert _caller_bounds(plain)[1] > 0
    for marker in ({SSM_REQUEST_KEY: True}, {SSM_GUEST_REQUEST_KEY: SsmGuest("abc123", "Dana")}):
        assert _caller_bounds(_request(auth_token="", **marker)) == ({"no_refresh": "1"}, 0)


# ── every site that trusts a peer address ──

#: ``is_direct_local_request(...)`` call sites per file. Each is covered by the
#: predicate's SSM refusal above.
PREDICATE_SITES = {
    "dashboard/file_api/desktop.py": 1,
    "dashboard/handlers/core.py": 2,
    "dashboard/handlers/external_terminal.py": 1,
    "dashboard/handlers/knowledge_sources.py": 1,
    "dashboard/handlers/secrets.py": 1,
    "dashboard/handlers/weixin_qr.py": 4,
    "dashboard/handlers/webapp_preview.py": 1,
    "dashboard/handlers/whatsapp_setup.py": 5,
    "dashboard/messaging_api/channel_folder_backfill.py": 1,
    "dashboard/messaging_api/discord_settings.py": 2,
    "dashboard/messaging_api/feishu_settings.py": 2,
    "dashboard/messaging_api/imessage_settings.py": 2,
    "dashboard/messaging_api/slack_settings.py": 2,
    "dashboard/messaging_api/teams_settings.py": 2,
    "dashboard/messaging_api/telegram_settings.py": 2,
    "dashboard/messaging_api/webex_settings.py": 2,
    "dashboard/messaging_api/wecom_settings.py": 2,
}

#: ``is_loopback(request.remote ...)`` call sites per file, and why each is safe.
RAW_LOOPBACK_SITES = {
    # The predicate itself (SSM refusal first), the https-proto read, the CSRF
    # no-Origin allowance and the empty-Host allowance. The last two widen
    # nothing: a request still needs its own credential.
    "dashboard/origin.py": 4,
    # Internal-secret paths. The SSM gate refuses X-Internal-Secret and strict
    # internal paths before any of this runs.
    "dashboard/token_auth.py": 1,
    # Each of these also requires the internal secret, X-Local-Secret or
    # ``internal_auth``, none of which an SSM guest holds.
    "apps/builtins/dev_fleet/agent_pod_api.py": 1,
    "dashboard/handlers/core.py": 2,
    "dashboard/handlers/core_local_token.py": 1,
    "dashboard/handlers/external_terminal.py": 1,
    "dashboard/handlers/search_segment.py": 1,
    # The same, and they also read the SSM marker.
    "dashboard/handlers/computer_use.py": 1,
    "dashboard/handlers/updates_channel.py": 1,
    "dashboard/handlers/updates_step_up.py": 1,
    # Naming a peer through the Tailscale daemon or the relay; grants nothing.
    "dashboard/tailnet.py": 2,
    "dashboard/kiro_relay/ingress.py": 1,
}


def _call_sites() -> tuple[dict[str, int], dict[str, int]]:
    direct: collections.Counter[str] = collections.Counter()
    raw: collections.Counter[str] = collections.Counter()
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == "is_direct_local_request":
                direct[rel] += 1
            elif (
                name == "is_loopback"
                and node.args
                and "request.remote" in ast.unparse(node.args[0])
            ):
                raw[rel] += 1
    return dict(direct), dict(raw)


def test_every_loopback_trust_site_is_classified() -> None:
    direct, raw = _call_sites()
    assert direct == PREDICATE_SITES, (
        "is_direct_local_request is called from a place this list does not know. "
        "An SSM guest passes a plain loopback test, so a new gate must use the "
        "predicate (which refuses SSM requests) and be added to PREDICATE_SITES."
    )
    assert raw == RAW_LOOPBACK_SITES, (
        "A raw is_loopback(request.remote) test changed. Classify it in "
        "RAW_LOOPBACK_SITES: it must require a secret or internal_auth, read "
        "origin.is_ssm_request, or grant nothing."
    )


def test_the_raw_sites_that_grant_something_read_the_marker_or_a_secret() -> None:
    """The secret-or-marker claim above is checked, not just stated."""
    needs_secret_or_marker = {
        "apps/builtins/dev_fleet/agent_pod_api.py",
        "dashboard/handlers/core.py",
        "dashboard/handlers/core_local_token.py",
        "dashboard/handlers/external_terminal.py",
        "dashboard/handlers/search_segment.py",
        "dashboard/handlers/computer_use.py",
        "dashboard/handlers/updates_channel.py",
        "dashboard/handlers/updates_step_up.py",
    }
    tokens = ("internal_auth", "X-Local-Secret", "is_ssm_request", "local_secret")
    for rel in needs_secret_or_marker:
        text = (SRC / rel).read_text(encoding="utf-8")
        assert any(token in text for token in tokens), rel
