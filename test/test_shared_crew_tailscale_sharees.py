"""People rows for users the local Tailscale daemon lists (``tailscale_share``)."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from kiro_crew.dashboard import tailnet_localapi
from kiro_crew.dashboard.tailnet_direct import (
    MAX_TAILNET_USERS,
    parse_status,
    parse_tailnet_users,
)
from kiro_crew.shared_crew import access_set, owner, people, tailscale_sharees
from kiro_crew.shared_crew.contracts import Principal, Provenance
from kiro_crew.shared_crew.identity import local_operator_principal, tailnet_principal

OWNER_UID = 100
SHAREE_UID = 200
MEMBER_UID = 300


def _status() -> dict:
    return {
        "BackendState": "Running",
        "Self": {
            "HostName": "Studio",
            "DNSName": "studio.tail1.ts.net.",
            "UserID": OWNER_UID,
            "TailscaleIPs": ["100.64.0.1", "fd7a:115c:a1e0::1"],
        },
        "Peer": {
            "k1": {"UserID": SHAREE_UID, "ShareeNode": True, "Tags": None},
            "k2": {"UserID": SHAREE_UID, "ShareeNode": True, "Tags": None},
            "k3": {"UserID": OWNER_UID, "Tags": None},
            "k4": {"UserID": MEMBER_UID, "Tags": None},
            "k5": {"UserID": 400, "Tags": ["tag:server"]},
            "k6": {"UserID": 500, "Tags": None},
            "k7": "not-an-object",
        },
        "User": {
            str(OWNER_UID): {"LoginName": "boss@example.com", "DisplayName": "Boss"},
            str(SHAREE_UID): {
                "LoginName": "Guest@Example.com",
                "DisplayName": "Guest",
                "ProfilePicURL": "https://pics.example/g.png",
            },
            str(MEMBER_UID): {"LoginName": "mate@example.com", "DisplayName": ""},
            "400": {"LoginName": "tagged-devices"},
            "500": {"LoginName": "bad login with spaces"},
        },
    }


@pytest.fixture(autouse=True)
def _fresh():
    owner.reset_for_tests()
    access_set.reset_for_tests()
    tailscale_sharees.reset_for_tests(read=lambda: tailnet_localapi.Answer(_status(), "x"))
    people.reset_for_tests()
    yield
    tailscale_sharees.reset_for_tests()
    owner.reset_for_tests()
    access_set.reset_for_tests()
    people.reset_for_tests()


def test_the_parser_lists_one_row_per_other_user_and_skips_the_owner_and_tagged_nodes():
    users = parse_tailnet_users(_status())
    assert [u.user_id for u in users] == [str(SHAREE_UID), str(MEMBER_UID)]
    guest, mate = users
    assert guest.login == "Guest@Example.com" and guest.display_name == "Guest"
    assert guest.profile_pic_url == "https://pics.example/g.png"
    # A blank display name falls back to the login.
    assert mate.display_name == "mate@example.com" and mate.profile_pic_url == ""


def test_the_parser_caps_users_and_reports_the_omitted_count_once(caplog):
    total = MAX_TAILNET_USERS + 1
    status = {
        "Self": {"UserID": total + 1},
        "Peer": {f"node-{i}": {"UserID": i} for i in range(1, total + 1)},
        "User": {str(i): {"LoginName": f"user-{i}@example.com"} for i in range(1, total + 1)},
    }

    with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.tailnet_direct"):
        users = parse_tailnet_users(status)

    assert [user.user_id for user in users] == [str(i) for i in range(1, MAX_TAILNET_USERS + 1)]
    reports = [record for record in caplog.records if "snapshot truncated" in record.getMessage()]
    assert len(reports) == 1
    assert reports[0].args == (1, MAX_TAILNET_USERS)


@pytest.mark.parametrize("data", [None, [], {}, {"Peer": [], "User": {}}, {"Peer": {}, "User": 1}])
def test_the_parser_returns_nothing_for_an_unexpected_shape(data):
    assert parse_tailnet_users(data) == []


def test_the_self_node_host_name_is_kept():
    assert parse_status(_status()).host_name == "Studio"


def test_the_peers_read_is_a_separate_daemon_question(monkeypatch):
    asked: list = []
    monkeypatch.setattr(tailnet_localapi, "_ask", lambda path, args: asked.append((path, args)))
    tailnet_localapi.status_with_peers()
    tailnet_localapi.status()
    assert asked[0] == ("/localapi/v0/status", ["status", "--json"])
    assert asked[1] == ("/localapi/v0/status?peers=false", ["status", "--json", "--peers=false"])


def test_a_sharee_principal_is_the_one_a_direct_visit_would_get():
    user = parse_tailnet_users(_status())[0]
    visit = tailnet_principal(
        "guest@example.com", "laptop.ts.net", Provenance.TAILSCALE_LISTENER, display_name="Guest"
    )
    assert tailscale_sharees.sharee_principal(user).id == visit.id


@pytest.mark.asyncio
async def test_the_read_is_cached_and_a_failure_yields_no_rows():
    reads: list = []
    now = [0.0]

    def read():
        reads.append(1)
        return tailnet_localapi.Answer(_status(), "x")

    tailscale_sharees.reset_for_tests(read=read, clock=lambda: now[0])
    first = await tailscale_sharees.read_sharees()
    await tailscale_sharees.read_sharees()
    assert len(first) == 2 and len(reads) == 1
    now[0] += tailscale_sharees.CACHE_TTL_SECS + 1
    await tailscale_sharees.read_sharees()
    assert len(reads) == 2

    def boom():
        raise RuntimeError("daemon down")

    tailscale_sharees.reset_for_tests(read=boom)
    assert await tailscale_sharees.read_sharees() == []
    tailscale_sharees.reset_for_tests(read=lambda: tailnet_localapi.Answer(None, "none"))
    assert await tailscale_sharees.read_sharees() == []


@pytest.mark.asyncio
async def test_a_slow_daemon_times_out_without_failing_the_read(monkeypatch):
    monkeypatch.setattr(tailscale_sharees, "READ_TIMEOUT_SECS", 0.01)
    release = threading.Event()

    def slow():
        # Held until the awaiting side has given up; bounded in case the test dies first.
        release.wait(timeout=5)
        return tailnet_localapi.Answer(None, "none")

    tailscale_sharees.reset_for_tests(read=slow)
    try:
        assert await tailscale_sharees.read_sharees() == []
    finally:
        release.set()


def test_login_for_reads_the_cache_only():
    assert tailscale_sharees.login_for(str(SHAREE_UID)) == ""
    asyncio.run(tailscale_sharees.read_sharees())
    assert tailscale_sharees.login_for(str(SHAREE_UID)) == "guest@example.com"
    assert tailscale_sharees.login_for("999") == ""


# -- People merge ------------------------------------------------------------ #


async def _view(viewer: Principal, rows=(), **kw):
    sharees = await tailscale_sharees.read_sharees()
    return people.people_view(viewer, list(rows), sharees=sharees, **kw)


def _by_login(body):
    return {r["principal"]["display"]["login"] or r["principal"]["id"]: r for r in body["people"]}


@pytest.mark.asyncio
async def test_unseen_sharees_get_rows_with_no_sightings():
    viewer = local_operator_principal("local-app")
    body = await _view(viewer)
    rows = _by_login(body)
    guest = rows["Guest@Example.com"]
    assert guest["sources"] == ["tailscale_share"]
    assert guest["first_seen_at"] is None and guest["last_seen_at"] is None
    assert guest["devices"] == 0 and guest["sessions_created"] == 0
    assert guest["is_owner"] is False and guest["is_viewer"] is False
    assert guest["tailscale_user_id"] == str(SHAREE_UID)
    assert guest["member"]["profile_pic_url"] == "https://pics.example/g.png"
    assert "mate@example.com" in rows


@pytest.mark.asyncio
async def test_a_later_observation_merges_into_the_sharee_row():
    visit = tailnet_principal(
        "guest@example.com", "laptop.ts.net", Provenance.TAILSCALE_LISTENER, display_name="Guest"
    )
    people.observe(visit, "node:laptop")
    body = await _view(local_operator_principal("local-app"))
    guests = [r for r in body["people"] if r["principal"]["id"] == visit.id]
    assert len(guests) == 1
    assert guests[0]["sources"] == ["observed"]
    assert guests[0]["last_seen_at"] is not None


@pytest.mark.asyncio
async def test_the_owner_row_is_always_present_for_the_owner_viewer():
    viewer = local_operator_principal("local-app")
    body = await _view(viewer)
    owners = [r for r in body["people"] if r["is_owner"]]
    assert len(owners) == 1
    assert owners[0]["principal"]["id"] == viewer.id and owners[0]["is_viewer"] is True
    # Not for a viewer who is somebody else.
    other = tailnet_principal("guest@example.com", "n", Provenance.TAILSCALE_LISTENER)
    assert not any(r["is_owner"] for r in (await _view(other))["people"])


@pytest.mark.asyncio
async def test_owner_rows_under_several_local_subjects_fold_into_one():
    startup = local_operator_principal("local-startup")
    people.observe(startup, "local")
    people.observe(local_operator_principal("local-app"), "local")
    legacy = [
        {"attribution": {"creator": owner.owner_principal().to_public_dict()}} for _ in range(2)
    ]
    body = await _view(startup, legacy)
    owners = [r for r in body["people"] if r["is_owner"]]
    assert len(owners) == 1
    assert owners[0]["principal"]["id"] == startup.id
    assert owners[0]["sessions_created"] == 2
    assert owners[0]["sources"] == ["observed", "session_creator"]


@pytest.mark.asyncio
async def test_the_owner_is_never_listed_as_a_sharee():
    access_set.set_owner("Guest@Example.com", str(SHAREE_UID))
    body = await _view(local_operator_principal("local-app"))
    assert not any(r["sources"] == ["tailscale_share"] and r["is_owner"] for r in body["people"])
    assert "Guest@Example.com" not in _by_login(body)


@pytest.mark.asyncio
async def test_a_legacy_owner_creator_folds_into_the_owner_row_by_id():
    # Public attribution names the creator without provenance; the fold still sees it.
    startup = local_operator_principal("local-startup")
    people.observe(startup, "local")
    legacy = [{"attribution": {"creator": owner.owner_principal().to_public_dict()}}]
    body = await _view(startup, legacy)
    owners = [r for r in body["people"] if r["is_owner"]]
    assert len(owners) == 1
    assert owners[0]["principal"]["id"] == startup.id
    assert owners[0]["sessions_created"] == 1


@pytest.mark.asyncio
async def test_only_the_owner_is_shown_sharees_who_never_came_in():
    collaborator = tailnet_principal("mate@example.com", "n", Provenance.TAILSCALE_LISTENER)
    body = await _view(collaborator)
    assert not any("tailscale_share" in r["sources"] for r in body["people"])
    owner_body = await _view(local_operator_principal("local-app"))
    assert any("tailscale_share" in r["sources"] for r in owner_body["people"])
