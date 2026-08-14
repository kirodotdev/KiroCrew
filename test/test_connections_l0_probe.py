"""Hermetic tests for the Connections L0 static-metadata probe.

Every test here is offline: the autouse fixture makes constructing a real HTTP
client an error, so a regression that reintroduces a live call fails loudly
instead of turning the suite into a network-dependent flake.
"""

import ast
import errno
import json
import os
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest
from yarl import URL

from kiro_crew.connections import get_provider, l0_drift, l0_probe, l0_record
from kiro_crew.connections.registry import (
    L0_VERIFICATION_WARN_AGE_DAYS,
    REGISTRY_PATH,
    canonical_host,
    is_local_host,
    utc_today,
)

MCP_URL = "https://mcp.example.com/mcp"
RESOURCE_URL = "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
RESOURCE_ROOT_URL = "https://mcp.example.com/.well-known/oauth-protected-resource"
# A committed issuer WITH a path: three of the seven real providers have one, and
# it is what makes the identity comparison load-bearing.
ISSUER = "https://auth.example.com/tenant-a"
OTHER_TENANT = "https://auth.example.com/tenant-b"
AUTHORIZATION_URL = "https://auth.example.com/.well-known/oauth-authorization-server/tenant-a"
OTHER_TENANT_URL = "https://auth.example.com/.well-known/oauth-authorization-server/tenant-b"


class FakeContent:
    def __init__(self, data):
        self.data = data
        self.offset = 0

    async def read(self, limit):
        chunk = self.data[self.offset : self.offset + limit]
        self.offset += len(chunk)
        return chunk


class FakeResponse:
    def __init__(self, status, payload=None, headers=None, body=None):
        self._spec = (status, payload, headers, body)
        self.status = status
        self.headers = headers or {}
        if body is None:
            body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.content = FakeContent(body)

    def fresh(self):
        """A body is read once, so probing several providers re-issues the route."""

        return FakeResponse(*self._spec)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.fetched = []

    def get(self, url, **kwargs):
        self.fetched.append(url)
        self.kwargs = kwargs
        if url not in self.routes:
            return FakeResponse(404)
        return self.routes[url].fresh()

    def post(self, url, **kwargs):  # pragma: no cover - probe must never POST
        raise AssertionError("the L0 probe must not POST to a provider")


@pytest.fixture(autouse=True)
def forbid_real_http_client(monkeypatch):
    def fail_if_constructed(*_args, **_kwargs):
        raise AssertionError("unit tests must not construct a real HTTP client")

    monkeypatch.setattr(l0_probe.aiohttp, "ClientSession", fail_if_constructed)


def provider(dcr=True, pkce=True, authorization_server=ISSUER):
    item = deepcopy(get_provider("notion"))
    assert item is not None
    item["name"] = "Example"
    item["slug"] = "example"
    item["mcp_url"] = MCP_URL
    item["l0_expectations"] = {
        "authorization_server": authorization_server,
        "dcr": dcr,
        "pkce": pkce,
        "verified_on": utc_today().isoformat(),
    }
    return item


def authorization_document(*, dcr=True, pkce=True, issuer=ISSUER):
    document = {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "code_challenge_methods_supported": ["S256"] if pkce else [],
    }
    if dcr:
        document["registration_endpoint"] = f"{issuer}/register"
    return document


def routes(*, dcr=True, pkce=True, resource=None, advertised=ISSUER, at_root=False, issuer=ISSUER):
    resource_url = RESOURCE_ROOT_URL if at_root else RESOURCE_URL
    return {
        resource_url: FakeResponse(
            200,
            {
                "resource": MCP_URL if resource is None else resource,
                "authorization_servers": [advertised],
            },
        ),
        AUTHORIZATION_URL: FakeResponse(
            200, authorization_document(dcr=dcr, pkce=pkce, issuer=issuer)
        ),
    }


# --- probe ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_validates_discovery_and_captures_what_it_observed():
    session = FakeSession(routes())

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=3.5)

    assert result["ok"] is True
    assert result["errors"] == []
    assert all(result["checks"].values())
    assert result["observed"] == {
        "authorization_server": ISSUER,
        "dcr": True,
        "pkce": True,
    }
    assert session.kwargs["timeout"].total == 3.5


@pytest.mark.asyncio
async def test_root_well_known_is_tried_when_path_insertion_misses():
    """GitLab serves one of its MCP endpoints only at the origin root."""

    session = FakeSession(routes(at_root=True))

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=1.0)

    assert result["ok"] is True
    # The earlier miss must not be reported: it is the expected first probe.
    assert result["errors"] == []
    assert session.fetched[:2] == [RESOURCE_URL, RESOURCE_ROOT_URL]


@pytest.mark.asyncio
async def test_resource_may_be_an_array_of_covered_endpoints():
    """GitLab deviates from RFC 9728 and sends a list; the probe still works."""

    session = FakeSession(routes(resource=[MCP_URL, "https://mcp.example.com/other"]))

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=1.0)

    assert result["ok"] is True


@pytest.mark.asyncio
async def test_resource_naming_a_different_host_is_rejected():
    session = FakeSession(routes(resource="https://elsewhere.example.com/mcp"))

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=1.0)

    assert result["ok"] is False
    assert any("does not cover the MCP endpoint" in error for error in result["errors"])


@pytest.mark.asyncio
async def test_every_candidate_failing_reports_the_failures():
    session = FakeSession({})

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=1.0)

    assert result["ok"] is False
    assert len(result["errors"]) == 2
    assert result["observed"] is None


# --- G3: issuer identity, not origin --------------------------------------


@pytest.mark.parametrize(
    ("committed", "advertised", "label"),
    [
        (ISSUER, "https://AUTH.example.com/tenant-a", "host case"),
        (ISSUER, "https://auth.example.com:443/tenant-a", "explicit default port"),
        (ISSUER, f"{ISSUER}/", "trailing slash"),
        (ISSUER, OTHER_TENANT, "realm substitution"),
    ],
)
def test_an_issuer_variant_is_a_mismatch_not_a_normalization(committed, advertised, label):
    """RFC 8414 §2 is simple string comparison, so none of these are equal.

    A normalizing comparison would wave the first three through. Each one is an
    authorization server answering under a string we did not commit, which is
    exactly what the check exists to notice.
    """

    assert l0_probe._validate_issuer(advertised) != l0_probe._validate_issuer(committed), label


def test_a_valid_issuer_is_returned_verbatim():
    for value in [ISSUER, "https://AUTH.example.com:443/T/", "https://x.example"]:
        assert l0_probe._validate_issuer(value) == value


def test_host_canonicalization_never_leaks_into_the_issuer_value():
    """Vetting canonicalizes the host; the compared value stays untouched.

    ``faß.de`` and its A-label ``xn--fa-hia.de`` DIAL the same host, so both pass
    the local-host screen -- but they are different issuer identifiers under RFC
    8414 §2 and must not compare equal. If canonicalization ever leaked into the
    returned value, these two would collapse into one.
    """

    unicode_form = "https://fa\u00df.de/tenant"
    a_label_form = "https://xn--fa-hia.de/tenant"

    assert l0_probe._validate_issuer(unicode_form) == unicode_form
    assert l0_probe._validate_issuer(a_label_form) == a_label_form
    assert l0_probe._validate_issuer(unicode_form) != l0_probe._validate_issuer(a_label_form)


@pytest.mark.asyncio
async def test_an_issuer_differing_only_by_idna_spelling_is_a_mismatch():
    """Same dialled host, different issuer string -- still a finding."""

    entry = provider(authorization_server="https://fa\u00df.de/tenant")
    session = FakeSession(
        {
            RESOURCE_URL: FakeResponse(
                200,
                {"resource": MCP_URL, "authorization_servers": ["https://xn--fa-hia.de/tenant"]},
            )
        }
    )

    result = await l0_probe.probe_provider(session, entry, timeout_seconds=1.0)

    assert result["ok"] is False
    assert any("differs from the committed" in error for error in result["errors"])


@pytest.mark.asyncio
async def test_a_realm_substitution_on_the_same_origin_is_flagged():
    """RFC 8414 makes the issuer an identity; /tenant-b may not answer for /tenant-a."""

    session = FakeSession(routes(issuer=OTHER_TENANT))

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=1.0)

    assert result["ok"] is False
    assert any("is not the requested issuer" in error for error in result["errors"])
    assert result["observed"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("advertised_issuer", "label"),
    [
        ("https://AUTH.example.com/tenant-a", "host case"),
        ("https://auth.example.com:443/tenant-a", "explicit default port"),
        (f"{ISSUER}/", "trailing slash"),
        (OTHER_TENANT, "realm substitution"),
    ],
)
async def test_an_issuer_variant_is_flagged_end_to_end(advertised_issuer, label):
    """Each of these would pass a normalizing comparison. None may pass this one."""

    session = FakeSession(routes(issuer=advertised_issuer))

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=1.0)

    assert result["ok"] is False, label
    assert any("is not the requested issuer" in error for error in result["errors"]), label


@pytest.mark.asyncio
async def test_a_trailing_slash_issuer_is_a_mismatch():
    """Previously normalized away; under exact comparison it is a finding."""

    session = FakeSession(routes(issuer=f"{ISSUER}/"))

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=1.0)

    assert result["ok"] is False
    assert any("is not the requested issuer" in error for error in result["errors"])


@pytest.mark.parametrize(
    "value",
    [
        "http://auth.example.com/t",
        "https://127.0.0.1/t",
        "https://10.0.0.5/t",
        "https://[::1]/t",
        "https://localhost/t",
        "https://vault.internal/t",
        "https://auth.example.com/t?tenant=a",
        # IDNA homoglyphs: YARL dials 127.0.0.1 / vault.internal for these.
        "https://127\u30020\u30020\u30021/t",
        "https://127\uff0e0\uff0e0\uff0e1/t",
        "https://vault\u3002internal/t",
        "https://LOCALHOST/t",
    ],
)
def test_an_unfetchable_issuer_is_refused(value):
    with pytest.raises(ValueError):
        l0_probe._validate_issuer(value)


# --- G1: the advertised issuer is never dereferenced ----------------------


@pytest.mark.asyncio
async def test_an_advertised_issuer_change_is_never_fetched_in_probe_mode():
    session = FakeSession(
        {
            **routes(advertised=OTHER_TENANT),
            OTHER_TENANT_URL: FakeResponse(200, authorization_document(issuer=OTHER_TENANT)),
        }
    )

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=1.0)

    assert result["ok"] is False
    assert any("differs from the committed" in error for error in result["errors"])
    assert OTHER_TENANT_URL not in session.fetched
    assert AUTHORIZATION_URL in session.fetched


@pytest.mark.asyncio
async def test_an_advertised_issuer_change_is_never_fetched_in_record_mode():
    """The SSRF seam: record mode used to dereference this value."""

    session = FakeSession(
        {
            **routes(advertised=OTHER_TENANT),
            OTHER_TENANT_URL: FakeResponse(200, authorization_document(issuer=OTHER_TENANT)),
        }
    )

    result = await l0_probe.probe_provider(
        session, provider(), timeout_seconds=1.0, record=True
    )

    assert OTHER_TENANT_URL not in session.fetched
    assert AUTHORIZATION_URL in session.fetched
    # Captured for the recorder to refuse, not silently accepted.
    assert result["observed"]["authorization_server"] == OTHER_TENANT


@pytest.mark.asyncio
async def test_an_advertised_internal_host_is_refused_outright():
    session = FakeSession(routes(advertised="https://169.254.169.254/latest"))

    result = await l0_probe.probe_provider(
        session, provider(), timeout_seconds=1.0, record=True
    )

    assert result["ok"] is False
    assert not any("169.254.169.254" in url for url in session.fetched)


@pytest.mark.asyncio
async def test_an_advertised_idna_homoglyph_host_is_refused():
    """U+3002 passes a raw-string check but YARL dials 127.0.0.1."""

    session = FakeSession(routes(advertised="https://127\u30020\u30020\u30021/latest"))

    result = await l0_probe.probe_provider(
        session, provider(), timeout_seconds=1.0, record=True
    )

    assert result["ok"] is False
    # Only the provider's own well-known candidates were touched; the homoglyph
    # host was never dialled in any spelling.
    assert all(url.startswith("https://mcp.example.com/") for url in session.fetched)
    assert not any("127" in url for url in session.fetched)


# --- F2: host vetting happens on the host that gets dialled -----------------

# Deviation characters -- where IDNA2003 and IDNA2008/UTS-46 disagree. These are
# the cases a hand-rolled canonicalizer gets wrong: the stdlib codec maps sharp-s
# to "ss" and normalizes a final sigma, while yarl (what aiohttp dials) punycodes
# them differently or refuses the label outright.
DEVIATION_HOSTS = [
    "fa\u00df.de",                          # sharp s
    "xn--fa-hia.de",                        # its IDNA2008 A-label
    "\u03c3\u03cc\u03bb\u03bf\u03c2.gr",    # Greek final sigma
    "\u03c3\u03cc\u03bb\u03bf\u03c3.gr",    # same word, non-final sigma
    "\u0938\u0940\u200d.example",           # ZWJ
    "\u0938\u0940\u200c.example",           # ZWNJ
]

AGREED_HOSTS = [
    "127\u30020\u30020\u30021",
    "127\uff0e0\uff0e0\uff0e1",
    "vault\u3002internal",
    "LOCALHOST",
    "localhost.",
    "AUTH.Example.COM",
    "example.com.",
    "m\u00fcnchen.example.com",
    "xn--mnchen-3ya.example.com",
    "auth.example.com",
    "\u0645\u0648\u0642\u0639.\u0645\u0635\u0631",
    "169.254.169.254",
    "10.0.0.5",
    "::1",
    "fe80::1",
]


@pytest.mark.parametrize(
    ("hostname", "expected"),
    [
        ("127\u30020\u30020\u30021", "127.0.0.1"),
        ("127\uff0e0\uff0e0\uff0e1", "127.0.0.1"),
        ("vault\u3002internal", "vault.internal"),
        ("LOCALHOST", "localhost"),
        ("AUTH.Example.COM", "auth.example.com"),
        ("example.com.", "example.com"),
        ("m\u00fcnchen.example.com", "xn--mnchen-3ya.example.com"),
        ("xn--mnchen-3ya.example.com", "xn--mnchen-3ya.example.com"),
        # Deviation cases: BOTH spellings must land on the same dialled host, or
        # the Unicode form and its A-label would be vetted as different hosts.
        ("fa\u00df.de", "xn--fa-hia.de"),
        ("xn--fa-hia.de", "xn--fa-hia.de"),
    ],
)
def test_a_host_canonicalizes_to_what_will_be_dialled(hostname, expected):
    assert canonical_host(hostname) == expected


@pytest.mark.parametrize("hostname", DEVIATION_HOSTS + AGREED_HOSTS)
def test_canonicalization_is_the_dialled_host_by_construction(hostname):
    """The vetted host must BE the host aiohttp connects to, not merely agree.

    aiohttp resolves ``req.url.raw_host`` (connector.py), so this compares against
    exactly that field on the real URL -- path included, since the request carries
    one. ``rstrip('.')``/``lower()`` is our own documented normalization and is
    applied to both sides, so it cannot mask a codec difference.

    Parameterized over deviation characters on purpose: an implementation that
    re-derives the encoding instead of reading it back from yarl passes on the
    agreed corpus and fails here.
    """

    url = f"https://{hostname}/.well-known/oauth-authorization-server"
    if ":" in hostname:  # IPv6 literals travel bracketed in a real URL
        url = f"https://[{hostname}]/.well-known/oauth-authorization-server"

    try:
        dialed = URL(url).raw_host
    except (ValueError, UnicodeError):
        # yarl refuses to build it, so nothing can ever be dialled: the only
        # correct answer is to refuse it too.
        assert canonical_host(hostname) is None
        return

    expected = dialed.rstrip(".").lower() if dialed else None
    assert canonical_host(hostname) == expected


def test_a_host_yarl_itself_refuses_is_refused():
    """A ZWJ label encodes fine under IDNA2003 but yarl will not build the URL."""

    with pytest.raises((ValueError, UnicodeError)):
        URL("https://\u0938\u0940\u200d.example/x")

    assert canonical_host("\u0938\u0940\u200d.example") is None
    assert is_local_host("\u0938\u0940\u200d.example") is True


@pytest.mark.parametrize(
    "hostname",
    [
        None,
        "",
        "\u30021",
        "\u3002",
        ".",
        # yarl would REINTERPRET these rather than treat them as a host.
        "user@evil.com",
        "evil.com:8443",
        "a/b",
        "evil.com/path",
        "host with space",
    ],
)
def test_a_host_that_cannot_be_vetted_is_refused(hostname):
    """A host we cannot vet is a host we must not fetch."""

    assert canonical_host(hostname) is None
    assert is_local_host(hostname) is True


@pytest.mark.parametrize(
    "hostname",
    [
        "127\u30020\u30020\u30021",
        "127\uff0e0\uff0e0\uff0e1",
        "vault\u3002internal",
        "LOCALHOST",
        "localhost.",
        "10.0.0.5",
        "169.254.169.254",
        "::1",
        "fe80::1",
    ],
)
def test_a_homoglyph_or_variant_local_host_is_still_local(hostname):
    assert is_local_host(hostname) is True


@pytest.mark.parametrize(
    "hostname",
    [
        "auth.example.com",
        "mcp.notion.com",
        "xn--mnchen-3ya.example.com",
        "fa\u00df.de",
        "xn--fa-hia.de",
    ],
)
def test_a_real_provider_host_survives_canonicalization(hostname):
    assert is_local_host(hostname) is False


@pytest.mark.asyncio
async def test_an_unusable_committed_issuer_fails_before_any_request():
    session = FakeSession(routes())
    entry = provider()
    entry["l0_expectations"]["authorization_server"] = "https://127.0.0.1/t"

    result = await l0_probe.probe_provider(session, entry, timeout_seconds=1.0)

    assert result["ok"] is False
    assert session.fetched == []
    assert any("committed baseline is unusable" in error for error in result["errors"])


# --- expectation diffing ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("advertised", "expected", "fragment"),
    [
        (False, True, "DCR advertised=False"),
        (True, False, "DCR advertised=True"),
    ],
)
async def test_dcr_mismatch_is_reported(advertised, expected, fragment):
    session = FakeSession(routes(dcr=advertised))

    result = await l0_probe.probe_provider(
        session, provider(dcr=expected), timeout_seconds=1.0
    )

    assert result["ok"] is False
    assert any(fragment in error for error in result["errors"])


@pytest.mark.asyncio
async def test_pkce_mismatch_is_reported():
    session = FakeSession(routes(pkce=False))

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=1.0)

    assert result["ok"] is False
    assert any("PKCE S256 advertised=False" in error for error in result["errors"])


@pytest.mark.asyncio
async def test_recording_ignores_the_committed_dcr_and_pkce():
    session = FakeSession(routes(dcr=False))

    result = await l0_probe.probe_provider(
        session, provider(dcr=True), timeout_seconds=1.0, record=True
    )

    assert result["ok"] is True
    assert result["observed"]["dcr"] is False


# --- transport hardening ---------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_metadata_document_is_refused():
    oversized = b'{"resource":"' + b"a" * (l0_probe._MAX_METADATA_BYTES + 64) + b'"}'
    session = FakeSession({RESOURCE_URL: FakeResponse(200, body=oversized)})

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=1.0)

    assert result["ok"] is False
    assert any("byte limit" in error for error in result["errors"])


@pytest.mark.asyncio
async def test_compressed_metadata_document_is_refused():
    session = FakeSession(
        {
            RESOURCE_URL: FakeResponse(
                200, {"resource": MCP_URL}, headers={"Content-Encoding": "gzip"}
            )
        }
    )

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=1.0)

    assert result["ok"] is False
    assert any("Content-Encoding" in error for error in result["errors"])


@pytest.mark.asyncio
async def test_redirects_are_not_followed():
    session = FakeSession(routes())

    await l0_probe.probe_provider(session, provider(), timeout_seconds=1.0)

    assert session.kwargs["allow_redirects"] is False


@pytest.mark.asyncio
async def test_non_object_metadata_document_is_refused():
    session = FakeSession({RESOURCE_URL: FakeResponse(200, body=b"[1,2,3]")})

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=1.0)

    assert result["ok"] is False
    assert any("non-object JSON" in error for error in result["errors"])


@pytest.mark.asyncio
async def test_concurrency_is_bounded():
    providers = [dict(provider(), slug=f"p{index}") for index in range(6)]
    session = FakeSession(routes())

    results = await l0_probe.probe_all(
        session, providers, concurrency=2, timeout_seconds=1.0
    )

    assert len(results) == 6
    assert all(result["ok"] for result in results)


def test_the_report_names_its_own_scope():
    """A green run must not read as challenge-shape coverage."""

    report = l0_probe.build_report([])

    assert report["scope"] == "static-metadata"


def test_report_marks_a_fatal_error_and_stays_machine_readable():
    report = l0_probe.build_report([], fatal=RuntimeError("registry unreadable"))

    assert report["ok"] is False
    assert report["fatal_error"] == "RuntimeError: registry unreadable"
    assert report["providers"] == []


# --- drift ----------------------------------------------------------------


def test_a_single_failure_does_not_report_drift():
    streaks = l0_drift.update_streaks({}, {"notion": False})
    prior = l0_drift.StateLoad({}, True, None, None)

    assert streaks == {"notion": 1}
    assert l0_drift.verdict(streaks, threshold=3, prior=prior)["ok"] is True


def test_drift_is_reported_only_at_the_threshold():
    streaks = {"notion": 0}
    for _ in range(3):
        streaks = l0_drift.update_streaks(streaks, {"notion": False})

    result = l0_drift.verdict(
        streaks, threshold=3, prior=l0_drift.StateLoad({}, True, None, None)
    )

    assert streaks == {"notion": 3}
    assert result["ok"] is False
    assert result["drifted"] == ["notion"]


def test_one_pass_clears_an_accumulated_streak():
    assert l0_drift.update_streaks({"notion": 2}, {"notion": True}) == {"notion": 0}


def test_a_provider_dropped_from_the_registry_leaves_the_state():
    assert l0_drift.update_streaks({"gone": 5, "notion": 1}, {"notion": False}) == {"notion": 2}


def test_streaks_are_clamped():
    streaks = l0_drift.update_streaks({"notion": l0_drift._MAX_STREAK}, {"notion": False})

    assert streaks == {"notion": l0_drift._MAX_STREAK}


def test_state_round_trips(tmp_path):
    path = tmp_path / "state.json"
    l0_drift.write_state(path, {"notion": 2, "github": 0})

    loaded = l0_drift.load_streaks(path)

    assert loaded.loaded is True
    assert loaded.discarded is None
    assert loaded.recorded_at
    assert loaded.streaks == {"notion": 2, "github": 0}


def test_a_write_failure_mid_state_leaves_the_prior_streak_loadable(tmp_path, monkeypatch):
    """A half-written state file is indistinguishable from a tampered one.

    Simulates the disk filling up partway through the payload. An in-place
    write_text would have truncated the file already, so the next run would
    discard the carried streak and restart at zero -- losing the third failing
    night that reports drift. The atomic path has only touched a temp file, so
    the state the artifact carries is still the previous run's.
    """

    path = tmp_path / "state.json"
    l0_drift.write_state(path, {"notion": 2})
    original = path.read_text(encoding="utf-8")

    real_write = os.write
    state = {"seen": 0}

    def failing_write(fd, data):
        # Let the first chunk land, then fail: a clean upfront failure would not
        # exercise the partially-written case this test exists for.
        state["seen"] += 1
        if state["seen"] == 1:
            return real_write(fd, data[: max(1, len(data) // 2)])
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "write", failing_write)

    with pytest.raises(OSError):
        l0_drift.write_state(path, {"notion": 3})

    monkeypatch.undo()
    assert path.read_text(encoding="utf-8") == original
    # Still a usable streak, not a discard -- the point of the atomic write.
    assert l0_drift.load_streaks(path).streaks == {"notion": 2}
    # No temp file left behind for the artifact upload to collect.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]


def test_a_successful_state_write_replaces_the_file_atomically(tmp_path, monkeypatch):
    """The destination is reached by rename, never written in place."""

    path = tmp_path / "state.json"
    l0_drift.write_state(path, {"notion": 2})
    renames = []
    real_replace = os.replace

    def tracking_replace(src, dst, **kwargs):
        renames.append((str(src), str(dst)))
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", tracking_replace)

    l0_drift.write_state(path, {"notion": 3})

    monkeypatch.undo()
    assert [dst for _src, dst in renames] == [str(path)]
    assert l0_drift.load_streaks(path).streaks == {"notion": 3}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]


def test_a_tampered_or_truncated_state_is_discarded_with_a_reason(tmp_path):
    """A silently reset streak hides the third failing night, so it must be loud."""

    path = tmp_path / "state.json"
    l0_drift.write_state(path, {"notion": 2})
    document = json.loads(path.read_text(encoding="utf-8"))
    document["streaks"]["notion"] = 0
    path.write_text(json.dumps(document), encoding="utf-8")

    loaded = l0_drift.load_streaks(path)

    assert loaded.loaded is False
    assert loaded.streaks == {}
    assert "digest" in loaded.discarded


@pytest.mark.parametrize(
    ("contents", "fragment"),
    [
        ("not json", "could not be parsed"),
        ("[]", "not an object"),
        (json.dumps({"schema_version": 999, "streaks": {}}), "not supported"),
        (
            json.dumps({"schema_version": l0_drift.STATE_SCHEMA_VERSION, "streaks": "nope"}),
            "no streaks object",
        ),
    ],
)
def test_unusable_state_names_why_it_was_discarded(tmp_path, contents, fragment):
    path = tmp_path / "state.json"
    path.write_text(contents, encoding="utf-8")

    loaded = l0_drift.load_streaks(path)

    assert loaded.loaded is False
    assert fragment in loaded.discarded


def test_an_absent_state_file_is_not_a_discard(tmp_path):
    """First run vs lost streak are different events and must not look alike."""

    assert l0_drift.load_streaks(tmp_path / "absent.json").discarded is None
    assert l0_drift.load_streaks(None).discarded is None


# --- F3: an expected-but-missing artifact is not a first run ----------------


def test_a_missing_artifact_after_a_prior_run_is_reported(tmp_path):
    """A retention-expired artifact silently reset a two-night streak before."""

    loaded = l0_drift.load_streaks(tmp_path / "absent.json", prior_run_expected=True)

    assert loaded.loaded is False
    assert loaded.discarded == "artifact_missing"


def test_a_genuine_first_run_stays_null_even_with_the_flag_off(tmp_path):
    loaded = l0_drift.load_streaks(tmp_path / "absent.json", prior_run_expected=False)

    assert loaded.discarded is None


def test_the_cli_reports_artifact_missing_when_a_prior_run_existed(monkeypatch, tmp_path):
    _stub_run_probe(monkeypatch, [failing("notion")])
    state = tmp_path / "state.json"

    l0_probe.main(
        [
            "--report",
            str(tmp_path / "report.json"),
            "--state",
            str(state),
            "--prior-run-expected",
        ]
    )

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["drift"]["prior_state_discarded"] == "artifact_missing"
    assert report["drift"]["prior_state_loaded"] is False


def test_the_cli_reports_null_on_a_genuine_first_run(monkeypatch, tmp_path):
    _stub_run_probe(monkeypatch, [failing("notion")])

    l0_probe.main(
        [
            "--report",
            str(tmp_path / "report.json"),
            "--state",
            str(tmp_path / "state.json"),
        ]
    )

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["drift"]["prior_state_discarded"] is None


def test_a_present_artifact_is_unaffected_by_the_flag(tmp_path):
    """The flag only speaks to ABSENCE; a readable artifact loads either way."""

    path = tmp_path / "state.json"
    l0_drift.write_state(path, {"notion": 2})

    for expected in (True, False):
        loaded = l0_drift.load_streaks(path, prior_run_expected=expected)
        assert loaded.loaded is True
        assert loaded.discarded is None
        assert loaded.streaks == {"notion": 2}


def test_junk_state_entries_are_dropped(tmp_path):
    path = tmp_path / "state.json"
    # Written through write_state so the digest covers the sanitized view.
    l0_drift.write_state(path, {"notion": 2})
    document = json.loads(path.read_text(encoding="utf-8"))
    document["streaks"].update({"bad": "x", "negative": -1, "boolean": True})
    path.write_text(json.dumps(document), encoding="utf-8")

    loaded = l0_drift.load_streaks(path)

    assert loaded.streaks == {"notion": 2}
    assert loaded.loaded is True


def test_threshold_must_be_positive():
    with pytest.raises(ValueError):
        l0_drift.verdict({}, threshold=0, prior=l0_drift.StateLoad({}, False, None, None))


# --- record ---------------------------------------------------------------


def test_rendering_the_committed_registry_reproduces_it_byte_for_byte():
    """The recorder must not reformat a security-relevant file to write one field."""

    original = REGISTRY_PATH.read_text(encoding="utf-8")

    assert l0_record.render_registry(json.loads(original)) == original


def test_recording_refreshes_the_stamp_and_reports_changes(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(REGISTRY_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    committed = json.loads(path.read_text(encoding="utf-8"))[0]
    observed = {
        committed["slug"]: {
            "authorization_server": committed["l0_expectations"]["authorization_server"],
            "dcr": not committed["l0_expectations"]["dcr"],
            "pkce": committed["l0_expectations"]["pkce"],
        }
    }

    outcome = l0_record.record_baselines(path, observed, date(2026, 5, 4))

    assert outcome.changed == [committed["slug"]]
    assert outcome.needs_approval == {}
    written = json.loads(path.read_text(encoding="utf-8"))[0]["l0_expectations"]
    assert written["verified_on"] == "2026-05-04"
    assert written["dcr"] is not committed["l0_expectations"]["dcr"]


def test_a_confirming_capture_on_a_later_day_still_moves_the_stamp(tmp_path):
    """Stamp churn is the intended refresh, not noise."""

    path = tmp_path / "registry.json"
    path.write_text(REGISTRY_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    committed = json.loads(path.read_text(encoding="utf-8"))[0]
    observed = {
        committed["slug"]: {
            key: committed["l0_expectations"][key]
            for key in ("authorization_server", "dcr", "pkce")
        }
    }
    later = date.fromisoformat(committed["l0_expectations"]["verified_on"])
    later = later.replace(year=later.year + 1)

    outcome = l0_record.record_baselines(path, observed, later)

    assert outcome.changed == [committed["slug"]]
    written = json.loads(path.read_text(encoding="utf-8"))[0]["l0_expectations"]
    assert written["verified_on"] == later.isoformat()


def test_a_capture_on_the_committed_day_writes_nothing(tmp_path):
    path = tmp_path / "registry.json"
    original = REGISTRY_PATH.read_text(encoding="utf-8")
    path.write_text(original, encoding="utf-8")
    committed = json.loads(original)[0]
    observed = {
        committed["slug"]: {
            key: committed["l0_expectations"][key]
            for key in ("authorization_server", "dcr", "pkce")
        }
    }
    same_day = date.fromisoformat(committed["l0_expectations"]["verified_on"])

    outcome = l0_record.record_baselines(path, observed, same_day)

    assert outcome.changed == []
    assert path.read_text(encoding="utf-8") == original


def test_an_issuer_change_is_reported_for_approval_and_never_written(tmp_path):
    path = tmp_path / "registry.json"
    original = REGISTRY_PATH.read_text(encoding="utf-8")
    path.write_text(original, encoding="utf-8")
    committed = json.loads(original)[0]
    observed = {
        committed["slug"]: {
            "authorization_server": "https://attacker.example.com/oauth",
            "dcr": True,
            "pkce": True,
        }
    }

    outcome = l0_record.record_baselines(path, observed, date(2026, 5, 4))

    assert outcome.changed == []
    assert outcome.needs_approval == {
        committed["slug"]: "https://attacker.example.com/oauth"
    }
    # Not even the stamp moves: an unapproved change must not buy more silence.
    assert path.read_text(encoding="utf-8") == original


def test_a_provider_that_failed_to_capture_keeps_its_stale_baseline(tmp_path):
    path = tmp_path / "registry.json"
    original = REGISTRY_PATH.read_text(encoding="utf-8")
    path.write_text(original, encoding="utf-8")
    before = json.loads(original)[1]["l0_expectations"]
    first = json.loads(original)[0]
    observed = {
        first["slug"]: {
            "authorization_server": first["l0_expectations"]["authorization_server"],
            "dcr": not first["l0_expectations"]["dcr"],
            "pkce": first["l0_expectations"]["pkce"],
        }
    }

    l0_record.record_baselines(path, observed, date(2026, 5, 4))

    assert json.loads(path.read_text(encoding="utf-8"))[1]["l0_expectations"] == before


def test_the_recorder_write_is_byte_exact_on_windows_too():
    """Pinned on the AST because no POSIX run can observe this.

    `atomic_write`'s default newline translates "\\n" to "\\r\\n" on Windows only.
    This module reads the registry back, edits it and rewrites it, and
    `test_rendering_the_committed_registry_reproduces_it_byte_for_byte` pins that
    round trip -- so on Windows the default would accumulate carriage returns and
    break it, while every Linux test would still pass. A behavioural test cannot
    catch that here, so assert the keyword on the CALL (a substring check would
    be satisfied by the comment that explains it).
    """

    tree = ast.parse(Path(l0_record.__file__).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "atomic_write"
    ]

    assert len(calls) == 1, "expected exactly one registry write"
    newline = {kw.arg: kw.value for kw in calls[0].keywords}.get("newline")
    assert isinstance(newline, ast.Constant) and newline.value == ""
    assert "registry_path.write_text(" not in Path(l0_record.__file__).read_text(
        encoding="utf-8"
    )


def test_an_unreadable_registry_is_a_clean_error(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(l0_record.RecordError, match="array of objects"):
        l0_record.record_baselines(path, {}, date(2026, 5, 4))


def test_a_missing_registry_is_a_clean_error(tmp_path):
    with pytest.raises(l0_record.RecordError, match="could not read"):
        l0_record.record_baselines(tmp_path / "absent.json", {}, date(2026, 5, 4))


def _changing_capture(original: str) -> dict:
    """An observation that differs from the committed baseline, so a write happens."""

    committed = json.loads(original)[0]
    return {
        committed["slug"]: {
            "authorization_server": committed["l0_expectations"]["authorization_server"],
            "dcr": not committed["l0_expectations"]["dcr"],
            "pkce": committed["l0_expectations"]["pkce"],
        }
    }


def test_a_write_failure_mid_record_leaves_the_registry_intact(tmp_path, monkeypatch):
    """The registry is parsed at import time, so a truncated one breaks startup.

    Simulates the disk filling up partway through the payload. An in-place
    write_text would have already truncated the file by this point; the atomic
    path has only touched a temp file, so the committed registry is untouched.
    """

    path = tmp_path / "registry.json"
    original = REGISTRY_PATH.read_text(encoding="utf-8")
    path.write_text(original, encoding="utf-8")

    real_write = os.write
    state = {"seen": 0}

    def failing_write(fd, data):
        # Let the first chunk land, then fail: a clean upfront failure would not
        # exercise the partially-written case this test exists for.
        state["seen"] += 1
        if state["seen"] == 1:
            return real_write(fd, data[: max(1, len(data) // 2)])
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "write", failing_write)

    with pytest.raises(l0_record.RecordError, match="could not write"):
        l0_record.record_baselines(path, _changing_capture(original), date(2026, 5, 4))

    monkeypatch.undo()
    assert path.read_text(encoding="utf-8") == original
    # No temp file left behind for a later reader to trip over.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["registry.json"]


def test_an_interrupt_mid_record_leaves_the_registry_intact(tmp_path, monkeypatch):
    """KeyboardInterrupt is not an OSError, so it propagates -- intact either way."""

    path = tmp_path / "registry.json"
    original = REGISTRY_PATH.read_text(encoding="utf-8")
    path.write_text(original, encoding="utf-8")

    def interrupt(fd, data):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "write", interrupt)

    with pytest.raises(KeyboardInterrupt):
        l0_record.record_baselines(path, _changing_capture(original), date(2026, 5, 4))

    monkeypatch.undo()
    assert path.read_text(encoding="utf-8") == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["registry.json"]


def test_a_successful_record_replaces_the_file_atomically(tmp_path, monkeypatch):
    """The destination is reached by rename, never written in place."""

    path = tmp_path / "registry.json"
    original = REGISTRY_PATH.read_text(encoding="utf-8")
    path.write_text(original, encoding="utf-8")
    renames = []
    real_replace = os.replace

    def tracking_replace(src, dst, **kwargs):
        renames.append((str(src), str(dst)))
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", tracking_replace)

    outcome = l0_record.record_baselines(
        path, _changing_capture(original), date(2026, 5, 4)
    )

    monkeypatch.undo()
    assert outcome.changed
    assert [dst for _src, dst in renames] == [str(path)]
    assert path.read_text(encoding="utf-8") != original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["registry.json"]


# --- CLI ------------------------------------------------------------------


def _stub_run_probe(monkeypatch, results):
    async def fake(*, concurrency, timeout_seconds, record=False):
        return results

    monkeypatch.setattr(l0_probe, "run_probe", fake)


def passing(slug):
    return {
        "slug": slug,
        "name": slug,
        "ok": True,
        "checks": {},
        "errors": [],
        "observed": None,
        "duration_ms": 1,
    }


def failing(slug):
    return dict(passing(slug), ok=False, errors=["boom"])


def test_cli_without_state_reports_this_run_alone(monkeypatch, tmp_path):
    _stub_run_probe(monkeypatch, [failing("notion")])

    code = l0_probe.main(["--report", str(tmp_path / "report.json")])

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert code == 1
    assert report["gate"] == "immediate"
    assert "drift" not in report


def test_cli_with_state_stays_green_on_a_first_failure(monkeypatch, tmp_path):
    _stub_run_probe(monkeypatch, [failing("notion")])
    state = tmp_path / "state.json"

    code = l0_probe.main(["--report", str(tmp_path / "report.json"), "--state", str(state)])

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert code == 0
    assert report["ok"] is False
    assert report["drift"]["streaks"] == {"notion": 1}
    assert l0_drift.load_streaks(state).streaks == {"notion": 1}


def test_cli_goes_red_once_the_streak_reaches_the_threshold(monkeypatch, tmp_path):
    _stub_run_probe(monkeypatch, [failing("notion")])
    state = tmp_path / "state.json"
    args = ["--report", str(tmp_path / "report.json"), "--state", str(state)]

    assert l0_probe.main([*args, "--drift-threshold", "2"]) == 0
    assert l0_probe.main([*args, "--drift-threshold", "2"]) == 1


def test_cli_recovers_when_a_provider_passes_again(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    args = ["--report", str(tmp_path / "report.json"), "--state", str(state)]

    _stub_run_probe(monkeypatch, [failing("notion")])
    assert l0_probe.main([*args, "--drift-threshold", "2"]) == 0
    _stub_run_probe(monkeypatch, [passing("notion")])
    assert l0_probe.main([*args, "--drift-threshold", "2"]) == 0
    _stub_run_probe(monkeypatch, [failing("notion")])
    assert l0_probe.main([*args, "--drift-threshold", "2"]) == 0


def test_a_discarded_state_is_surfaced_in_the_report(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    state.write_text("not json", encoding="utf-8")
    _stub_run_probe(monkeypatch, [failing("notion")])

    l0_probe.main(["--report", str(tmp_path / "report.json"), "--state", str(state)])

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert "could not be parsed" in report["drift"]["prior_state_discarded"]
    assert report["drift"]["prior_state_loaded"] is False


# --- G2: a fatal error is about the probe, not the providers ---------------


def test_a_fatal_probe_exits_non_zero_and_leaves_the_streaks_untouched(
    monkeypatch, tmp_path
):
    state = tmp_path / "state.json"
    l0_drift.write_state(state, {"notion": 2, "github": 0})
    before = state.read_text(encoding="utf-8")

    async def explode(*, concurrency, timeout_seconds, record=False):
        raise RuntimeError("no network")

    monkeypatch.setattr(l0_probe, "run_probe", explode)

    code = l0_probe.main(
        ["--report", str(tmp_path / "report.json"), "--state", str(state)]
    )

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert code == 1
    assert report["gate"] == "fatal"
    assert report["fatal_error"] == "RuntimeError: no network"
    assert "drift" not in report
    assert state.read_text(encoding="utf-8") == before


def test_a_fatal_probe_without_state_also_exits_non_zero(monkeypatch, tmp_path):
    async def explode(*, concurrency, timeout_seconds, record=False):
        raise RuntimeError("no network")

    monkeypatch.setattr(l0_probe, "run_probe", explode)

    assert l0_probe.main(["--report", str(tmp_path / "report.json")]) == 1


def test_a_per_provider_failure_still_advances_only_that_streak(monkeypatch, tmp_path):
    """Per-provider isolation survives the fatal-path change."""

    state = tmp_path / "state.json"
    _stub_run_probe(monkeypatch, [failing("notion"), passing("github")])

    l0_probe.main(["--report", str(tmp_path / "report.json"), "--state", str(state)])

    assert l0_drift.load_streaks(state).streaks == {"notion": 1, "github": 0}


# --- record CLI -----------------------------------------------------------


def test_record_mode_writes_the_registry_and_exits_clean(monkeypatch, tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(REGISTRY_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(l0_probe, "REGISTRY_PATH", path)
    committed = json.loads(path.read_text(encoding="utf-8"))[0]
    captured = dict(
        passing(committed["slug"]),
        observed={
            "authorization_server": committed["l0_expectations"]["authorization_server"],
            "dcr": not committed["l0_expectations"]["dcr"],
            "pkce": committed["l0_expectations"]["pkce"],
        },
    )
    _stub_run_probe(monkeypatch, [captured])

    assert l0_probe.main(["--record"]) == 0
    written = json.loads(path.read_text(encoding="utf-8"))[0]["l0_expectations"]
    assert written["verified_on"] == utc_today().isoformat()


def test_record_mode_fails_when_an_issuer_needs_approval(monkeypatch, tmp_path):
    path = tmp_path / "registry.json"
    original = REGISTRY_PATH.read_text(encoding="utf-8")
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(l0_probe, "REGISTRY_PATH", path)
    committed = json.loads(original)[0]
    captured = dict(
        passing(committed["slug"]),
        observed={
            "authorization_server": "https://attacker.example.com/oauth",
            "dcr": True,
            "pkce": True,
        },
    )
    _stub_run_probe(monkeypatch, [captured])

    assert l0_probe.main(["--record"]) == 1
    assert path.read_text(encoding="utf-8") == original


def test_record_mode_fails_when_a_provider_could_not_be_reached(monkeypatch, tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(REGISTRY_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(l0_probe, "REGISTRY_PATH", path)
    _stub_run_probe(monkeypatch, [failing("notion")])

    assert l0_probe.main(["--record"]) == 1
    assert path.read_text(encoding="utf-8") == REGISTRY_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize("argv", [["--concurrency", "0"], ["--timeout", "0"]])
def test_nonsense_bounds_are_rejected(argv):
    with pytest.raises(SystemExit):
        l0_probe.main(argv)


# --- O1: staleness lands on the nightly, never on a PR ---------------------


def test_staleness_is_always_reported_with_its_runbook(monkeypatch, tmp_path):
    _stub_run_probe(monkeypatch, [passing("notion")])

    l0_probe.main(["--report", str(tmp_path / "report.json")])

    staleness = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))["staleness"]
    assert staleness["enforced"] is False
    assert staleness["warn_after_days"] == L0_VERIFICATION_WARN_AGE_DAYS
    assert "--record" in staleness["runbook"]


def test_a_stale_baseline_fails_only_when_the_nightly_asks(monkeypatch, tmp_path):
    """The whole point of O1: a PR run must not go red on a timer nobody reset."""

    monkeypatch.setattr(
        l0_probe, "stale_l0_baselines", lambda *_a, **_k: {"notion": "2020-01-01"}
    )
    _stub_run_probe(monkeypatch, [passing("notion")])
    args = ["--report", str(tmp_path / "report.json")]

    assert l0_probe.main(args) == 0
    assert l0_probe.main([*args, "--fail-on-stale"]) == 1


def test_staleness_does_not_mask_a_clean_drift_verdict(monkeypatch, tmp_path):
    monkeypatch.setattr(l0_probe, "stale_l0_baselines", lambda *_a, **_k: {})
    _stub_run_probe(monkeypatch, [passing("notion")])

    code = l0_probe.main(
        [
            "--report",
            str(tmp_path / "report.json"),
            "--state",
            str(tmp_path / "state.json"),
            "--fail-on-stale",
        ]
    )

    assert code == 0
