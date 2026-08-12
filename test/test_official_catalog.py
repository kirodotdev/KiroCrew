"""The official catalog: what it annotates, and what it refuses outright.

Three capabilities are deliberately absent (signature verification, tombstone
resolution, new inventory). Each is a FAIL-CLOSED gate rather than an ignored
field, so the first time one matters it surfaces loudly. These tests pin the
gates, because an omission that degrades silently is indistinguishable from an
omission nobody remembered.
"""

from __future__ import annotations

import http.client
import http.server
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from kiro_crew.apps import official_catalog as oc


def doc(**over):
    base = {
        "schemaVersion": 1,
        "generatedAt": "2026-01-01T00:00:00Z",
        "revision": "2026-01-01T00:00:00Z-abcdef1",
        "apps": [{"name": "demo-app", "source": {"type": "builtin"}}],
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _no_cache(tmp_path, monkeypatch):
    """Point the cache at a scratch path so a real one cannot satisfy a test."""
    monkeypatch.setattr(oc, "_cache_path", lambda: tmp_path / "official-catalog.json")


class TestLoadRefusals:
    def test_accepts_a_well_formed_document(self):
        assert oc.load_official_catalog(lambda: doc()) == [
            {"name": "demo-app", "source": {"type": "builtin"}}
        ]

    def test_a_failed_fetch_degrades_to_empty(self):
        """The store already works from the seed index, so a fetch failure is a
        degradation, never an error."""
        assert oc.load_official_catalog(lambda: None) == []

    @pytest.mark.parametrize("version", [2, 0, "1", None, 1.0])
    def test_an_unknown_schema_version_is_refused(self, version):
        """The document's MEANING is what a version bump changes. Reading the
        fields we still recognise would act on a contract we do not know."""
        assert oc.load_official_catalog(lambda: doc(schemaVersion=version)) == []

    @pytest.mark.parametrize("key", ["removed", "reinstated"])
    def test_a_non_empty_history_refuses_the_whole_catalog(self, key):
        """This is the load-bearing gate. Tombstone resolution is date-ordered
        with a fail-closed tie rule; implementing half of it would be worse than
        not implementing it. Rendering the other entries while skipping a
        withdrawal is the single outcome this catalog exists to prevent."""
        payload = doc(**{key: [{"name": "gone", "reason": "malicious", "since": "2026-01-01"}]})
        assert oc.load_official_catalog(lambda: payload) == []

    @pytest.mark.parametrize("key", ["removed", "reinstated"])
    def test_an_empty_history_is_not_a_refusal(self, key):
        """Publish omits these when empty, but an explicit empty list means
        'nothing withdrawn' and must not lock the catalog out."""
        assert oc.load_official_catalog(lambda: doc(**{key: []})) != []

    @pytest.mark.parametrize("apps", [None, {}, "nope", 5])
    def test_a_malformed_apps_list_is_refused(self, apps):
        assert oc.load_official_catalog(lambda: doc(apps=apps)) == []

    def test_entries_without_a_string_name_are_dropped(self):
        """A name reaches a dict key and a filesystem path elsewhere; anything
        that is not a string is not an app."""
        payload = doc(apps=[{"name": "ok"}, {"name": 5}, {}, "string", {"name": None}])
        assert oc.load_official_catalog(lambda: payload) == [{"name": "ok"}]


class TestCache:
    def test_a_second_load_does_not_refetch(self, tmp_path, monkeypatch):
        calls = []

        def fetch():
            calls.append(1)
            return doc()

        assert oc.load_official_catalog(fetch)
        assert oc.load_official_catalog(fetch)
        assert len(calls) == 1

    def test_a_stale_cache_is_refetched(self, tmp_path, monkeypatch):
        import os
        import time

        oc.load_official_catalog(lambda: doc())
        path = oc._cache_path()
        old = time.time() - oc.CACHE_TTL - 10
        os.utime(path, (old, old))
        calls = []
        oc.load_official_catalog(lambda: (calls.append(1), doc())[1])
        assert len(calls) == 1

    def test_a_corrupt_cache_is_ignored_rather_than_raised(self, tmp_path):
        path = oc._cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        assert oc.load_official_catalog(lambda: doc()) != []

    def test_a_refused_document_is_still_cached_and_still_refused(self):
        """Caching happens before validation, so a bad document is not refetched
        every call -- but it must stay refused on the cached path too."""
        payload = doc(schemaVersion=99)
        assert oc.load_official_catalog(lambda: payload) == []
        assert oc.load_official_catalog(lambda: doc()) == [], "cache still holds the bad doc"


class TestResolveRef:
    def test_an_absolute_ref_is_a_client_local_path(self):
        assert oc._resolve_ref("/app-assets/x/icon.svg") == "/app-assets/x/icon.svg"

    def test_a_relative_ref_resolves_against_the_catalog(self):
        """NOT against the app's repository -- the whole point of the catalog
        hosting the bytes is that the publisher's repo leaves the render path."""
        assert oc._resolve_ref("assets/icons/abc.png") == (
            "https://apps.crew.kiro.dev/assets/icons/abc.png"
        )

    @pytest.mark.parametrize(
        "ref",
        [
            "https://evil.example/x.png",
            "//evil.example/x.png",
            "javascript:alert(1)",
            "data:image/svg+xml;base64,AA",
            "",
            None,
            5,
            {},
        ],
    )
    def test_anything_that_is_not_a_plain_path_is_dropped(self, ref):
        """The ref contract forbids a URL. Honouring one would let the document
        choose the host an <img> loads from."""
        assert oc._resolve_ref(ref) == ""


class TestAnnotate:
    def rows(self):
        return [
            {"name": "demo-app", "displayName": "From Manifest", "author": "Demo Labs"},
            {"name": "other-app", "displayName": "Untouched"},
        ]

    def test_curated_copy_wins_over_the_manifest(self):
        """That is what curation means, for COPY: the catalog is ours, the
        manifest belongs to the app.

        `version` is excluded on purpose and is asserted unchanged here rather
        than in a separate test, so the line that used to overlay it cannot come
        back without this test noticing.
        """
        rows = self.rows()
        rows[0]["version"] = "1.0.0"
        oc.annotate(rows, [{
            "name": "demo-app",
            "displayName": "Curated Name",
            "summary": "One line of list copy.",
            "version": "9.9.9",
            "tags": ["curated"],
        }])
        assert rows[0]["displayName"] == "Curated Name"
        assert rows[0]["description"] == "One line of list copy."
        assert rows[0]["tags"] == ["curated"]
        assert rows[0]["version"] == "1.0.0", "version is a fact, not curated copy"

    def test_summary_lands_on_description(self):
        """`summary` IS the store row's one-line copy; there is no other field
        for it, and leaving `description` alone would render the manifest's
        400-character body in a list."""
        rows = self.rows()
        oc.annotate(rows, [{"name": "demo-app", "summary": "Short."}])
        assert rows[0]["description"] == "Short."

    def test_a_curated_author_is_applied_for_display(self):
        rows = self.rows()
        oc.annotate(rows, [{"name": "demo-app", "author": {"name": "Kiro Crew", "kind": "org"}}])
        assert rows[0]["author"] == "Kiro Crew"

    def test_a_curated_author_does_not_reach_the_verified_snapshot(self):
        """With the signature unchecked, the catalog is trusted only as far as
        TLS -- and TLS to a CDN is not evidence for a first-party badge. Wiring
        the author into `_index_author` is the step AFTER verification lands."""
        rows = self.rows()
        oc.annotate(rows, [{"name": "demo-app", "author": {"name": "Kiro Crew"}}])
        assert "_index_author" not in rows[0]

    def test_an_entry_matching_no_row_is_ignored(self):
        """Annotate-only: a published git source is pinned to a COMMIT and the
        install path clones with --branch, so new inventory needs that path
        changed first."""
        rows = self.rows()
        oc.annotate(rows, [{"name": "not-listed", "displayName": "Ghost"}])
        assert [r["name"] for r in rows] == ["demo-app", "other-app"]

    def test_unmatched_rows_are_left_alone(self):
        rows = self.rows()
        oc.annotate(rows, [{"name": "demo-app", "displayName": "Curated"}])
        assert rows[1]["displayName"] == "Untouched"

    def test_absent_curated_fields_do_not_erase_manifest_values(self):
        """An entry carrying only identity must not blank the row: publish omits
        a field it could not derive, and absence means 'no opinion'."""
        rows = self.rows()
        oc.annotate(rows, [{"name": "demo-app"}])
        assert rows[0]["displayName"] == "From Manifest"
        assert rows[0]["author"] == "Demo Labs"

    def test_icon_refs_become_loadable_urls(self):
        rows = self.rows()
        oc.annotate(rows, [{
            "name": "demo-app",
            "iconRef": "assets/icons/a.png",
            "iconRefDark": "assets/icons/b.png",
            "heroRef": "/app-assets/demo/hero.svg",
        }])
        assert rows[0]["iconUrl"] == "https://apps.crew.kiro.dev/assets/icons/a.png"
        assert rows[0]["iconUrlDark"] == "https://apps.crew.kiro.dev/assets/icons/b.png"
        assert rows[0]["heroImage"] == "/app-assets/demo/hero.svg"

    def test_a_url_shaped_icon_ref_is_not_applied(self):
        rows = self.rows()
        rows[0]["iconUrl"] = "/app-assets/demo/icon.svg"
        oc.annotate(rows, [{"name": "demo-app", "iconRef": "https://evil.example/x.png"}])
        assert rows[0]["iconUrl"] == "/app-assets/demo/icon.svg"


def test_the_catalog_url_is_https_and_under_the_documented_host():
    """A plaintext or third-party host here would silently move where every
    client's app list comes from."""
    assert oc.OFFICIAL_CATALOG_BASE.startswith("https://apps.crew.kiro.dev/")
    assert oc.OFFICIAL_CATALOG_URL == oc.OFFICIAL_CATALOG_BASE + "official-registry.json"


def test_cache_write_survives_an_unwritable_directory(tmp_path, monkeypatch):
    """A read-only cache dir must degrade to 'fetch every time', not raise."""
    monkeypatch.setattr(oc, "_cache_path", lambda: tmp_path / "nope" / "x" / "c.json")
    monkeypatch.setattr(
        oc.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only"))
    )
    assert oc.load_official_catalog(lambda: doc()) != []


def test_an_oversized_body_is_refused(monkeypatch):
    """Read a bounded amount from a host we do not control at parse time."""
    assert oc.MAX_BYTES > 0
    payload = json.dumps(doc()).encode()
    assert len(payload) < oc.MAX_BYTES


class TestHostileFieldTypes:
    """A fetched document's TYPES are as untrusted as its contents.

    `annotate` runs inside the `GET /api/apps/registry` handler with no
    try/except above it, so one malformed entry raising here is an HTTP 500 for
    the whole store -- the exact opposite of this module's promise that an
    unusable catalog degrades to the seed.
    """

    #: Every one of these is truthy, so a bare `if entry.get(...)` lets it
    #: through. `[]` and `{}` are absent on purpose: they are falsy and so were
    #: never the risk.
    HOSTILE = [5, True, 1.5, ["a"], {"k": "v"}]

    @pytest.mark.parametrize("bad", HOSTILE)
    @pytest.mark.parametrize(
        "field", ["displayName", "summary", "version", "tags", "author"]
    )
    def test_a_hostile_type_never_raises(self, field, bad):
        rows = [{"name": "demo-app", "displayName": "From Manifest"}]
        oc.annotate(rows, [{"name": "demo-app", field: bad}])

    def test_a_non_list_tags_is_dropped_rather_than_iterated(self):
        """`list(5)` raised TypeError; `list("ab")` silently made two tags."""
        rows = [{"name": "demo-app", "tags": ["from-manifest"]}]
        oc.annotate(rows, [{"name": "demo-app", "tags": 5}])
        assert rows[0]["tags"] == ["from-manifest"]

    def test_a_string_tags_does_not_become_one_tag_per_character(self):
        rows = [{"name": "demo-app"}]
        oc.annotate(rows, [{"name": "demo-app", "tags": "ab"}])
        assert "tags" not in rows[0]

    def test_non_string_tag_members_are_dropped_individually(self):
        """A bad member degrades that member, not the whole list."""
        rows = [{"name": "demo-app"}]
        oc.annotate(rows, [{"name": "demo-app", "tags": ["keep", 5, None, "also"]}])
        assert rows[0]["tags"] == ["keep", "also"]

    def test_a_non_string_display_field_does_not_reach_the_row(self):
        """It would be sorted and lowercased in the browser."""
        rows = [{"name": "demo-app", "displayName": "From Manifest"}]
        oc.annotate(rows, [{"name": "demo-app", "displayName": 5, "summary": 7}])
        assert rows[0]["displayName"] == "From Manifest"
        assert "description" not in rows[0]

    def test_a_non_string_author_name_does_not_reach_the_row(self):
        rows = [{"name": "demo-app", "author": "Demo Labs"}]
        oc.annotate(rows, [{"name": "demo-app", "author": {"name": 5}}])
        assert rows[0]["author"] == "Demo Labs"


class TestSchemaVersionIsExact:
    """`1.0 == 1` and `True == 1` are both true in Python, so `!=` alone let a
    document through whose version field was not an integer at all."""

    @pytest.mark.parametrize("version", [1.0, True, "1", [1], None])
    def test_a_version_that_is_not_an_int_is_refused(self, version):
        assert oc.load_official_catalog(lambda: doc(schemaVersion=version)) == []

    def test_the_exact_int_is_accepted(self):
        assert oc.load_official_catalog(lambda: doc(schemaVersion=1)) != []


class TestSchemeGuard:
    """urllib honours `file://`, so the scheme is checked where the URL becomes
    a fetch rather than trusted from whoever supplied it."""

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "http://apps.crew.kiro.dev/official-registry.json",
            "ftp://example.invalid/x.json",
            "//apps.crew.kiro.dev/x.json",
        ],
    )
    def test_a_non_https_url_is_refused(self, url):
        with pytest.raises(ValueError):
            oc._https_request(url)

    def test_https_is_built_with_a_json_accept_header(self):
        req = oc._https_request(oc.OFFICIAL_CATALOG_URL)
        assert req.get_full_url() == oc.OFFICIAL_CATALOG_URL
        assert req.get_header("Accept") == "application/json"

    def test_the_shipped_url_is_https(self):
        assert oc.OFFICIAL_CATALOG_URL.startswith("https://")


class TestTransportFailuresDegrade:
    """The fetch may not raise into the registry handler.

    `http.client.HTTPException` is NOT an `OSError` subclass, so a tuple listing
    only `URLError`/`OSError` lets a whole family through. `RemoteDisconnected`
    is the misleading member: it is caught only because it also inherits
    `ConnectionResetError`, which makes the gap look like one exception rather
    than a family.
    """

    #: Every one of these is raised by `http.client` while reading a response.
    #: `IncompleteRead` is a truncated chunked body -- the one a real CDN
    #: produces under load rather than under attack.
    HTTP_FAILURES = [
        http.client.IncompleteRead(b"partial"),
        http.client.BadStatusLine("garbage"),
        http.client.LineTooLong("header line"),
        http.client.InvalidURL(),
        http.client.UnknownProtocol("spdy/1"),
        http.client.ResponseNotReady(),
        http.client.CannotSendRequest(),
    ]

    @pytest.mark.parametrize(
        "exc", HTTP_FAILURES, ids=lambda e: type(e).__name__
    )
    def test_a_transport_failure_degrades_to_the_seed(self, exc, monkeypatch):
        def boom(*a, **k):
            raise exc

        # Patch THIS MODULE's seam, not `urllib.request.urlopen`. Patching the
        # stdlib function is how these tests silently stopped intercepting
        # anything the moment the fetch switched to an opener -- they began
        # making real requests to the live CDN and still passed.
        monkeypatch.setattr(oc, "_open_catalog", boom)
        assert oc._download() is None

    def test_every_member_is_an_http_exception(self):
        """Pins WHY the family is the right catch: naming the base covers all of
        them, so a future member needs no code change here."""
        for exc in self.HTTP_FAILURES:
            assert isinstance(exc, http.client.HTTPException)

    def test_the_family_is_not_reachable_through_oserror(self):
        """If this ever becomes true, the catch above is redundant -- and if it
        silently became true the test would say so rather than the tuple rotting
        unnoticed."""
        assert not issubclass(http.client.HTTPException, OSError)


class TestVersionIsNotCurated:
    """`version` is a FACT about the app, not curated copy.

    `annotate` runs BEFORE `_enrich_with_install_status`, so a version written
    onto the row reaches `_version_newer` and decides whether the store shows an
    update badge. Overlaying it would let the catalog fabricate an update the app
    never published, or mask one it did -- and it would couple the document to
    every app's release cadence forever.
    """

    def test_a_curated_version_does_not_reach_the_row(self):
        rows = [{"name": "demo-app", "version": "1.0.0"}]
        oc.annotate(rows, [{"name": "demo-app", "version": "9.9.9"}])
        assert rows[0]["version"] == "1.0.0"

    def test_the_rest_of_the_overlay_still_applies(self):
        """Pins that the exclusion is surgical, not the overlay being skipped."""
        rows = [{"name": "demo-app", "version": "1.0.0"}]
        oc.annotate(rows, [{
            "name": "demo-app", "version": "9.9.9",
            "displayName": "Curated", "summary": "Curated copy.",
        }])
        assert rows[0]["displayName"] == "Curated"
        assert rows[0]["description"] == "Curated copy."
        assert rows[0]["version"] == "1.0.0"


class TestNameSquattingCannotInheritCuratedCopy:
    """Matching on name alone is an impersonation surface.

    An app from a user-added registry that takes the name of a not-yet-seeded
    official app would inherit that app's curated description, author and
    CDN-hosted icon -- impersonation assembled out of OUR signed document.
    `_apply_trust_fields` refuses such a row the verified mark, but it runs AFTER
    this overlay, so the copy and the icon would land regardless.
    """

    ENTRY = {
        "name": "official-app",
        "displayName": "The Real One",
        "summary": "Curated copy that belongs to the official app.",
        "author": {"name": "Kiro Crew", "kind": "org"},  # brand-ok: squat fixture
        "iconRef": "assets/icons/abc.png",
    }

    def test_a_registry_tagged_row_is_skipped_entirely(self):
        rows = [{
            "name": "official-app", "_registry": "labs",
            "displayName": "Squatter", "description": "Its own words.",
        }]
        oc.annotate(rows, [self.ENTRY])
        assert rows[0]["displayName"] == "Squatter"
        assert rows[0]["description"] == "Its own words."
        assert "iconUrl" not in rows[0], "a squatting row must not get catalog-hosted bytes"
        assert "author" not in rows[0]

    def test_the_same_name_on_a_trusted_row_is_still_annotated(self):
        """The skip must key on the registry tag, not on the name."""
        rows = [{"name": "official-app", "displayName": "From Manifest"}]
        oc.annotate(rows, [self.ENTRY])
        assert rows[0]["displayName"] == "The Real One"


class TestFailedFetchIsRemembered:
    """An outage must not cost every store load a fresh timeout.

    `load_official_catalog` runs on `GET /api/apps/registry`. Without a negative
    cache, an unreachable CDN adds up to `FETCH_TIMEOUT` seconds to every page
    load for as long as the outage lasts, which is not the "degrade to the seed"
    this module promises.
    """

    def test_a_second_call_does_not_refetch_after_a_failure(self):
        calls = []

        def failing():
            calls.append(1)
            return None

        assert oc.load_official_catalog(failing) == []
        assert oc.load_official_catalog(failing) == []
        assert len(calls) == 1, "the second call must be answered from the failure cache"

    def test_the_failure_cache_expires(self, monkeypatch):
        calls = []

        def failing():
            calls.append(1)
            return None

        assert oc.load_official_catalog(failing) == []
        # Age the entry past FAILURE_TTL without sleeping. `oc.time` IS the time
        # module, so the real function has to be captured BEFORE patching it --
        # calling time.time() inside the replacement would recurse into itself.
        real_time = time.time
        monkeypatch.setattr(time, "time", lambda: real_time() + oc.FAILURE_TTL + 5)
        assert oc.load_official_catalog(failing) == []
        assert len(calls) == 2, "an expired failure must be retried"

    def test_the_failure_ttl_is_far_shorter_than_the_success_ttl(self):
        """Remembering a failure as long as a success would keep the store stale
        after the CDN recovered."""
        assert oc.FAILURE_TTL < oc.CACHE_TTL / 10

    def test_a_success_after_a_failure_is_cached_as_a_document(self, monkeypatch):
        """The failure entry must not be sticky once it ages out.

        Freshness comes from the cache file's MTIME, not from the timestamp
        inside it -- so a test cannot "expire" the entry by rewriting the file,
        which would refresh the very clock it meant to advance.
        """
        assert oc.load_official_catalog(lambda: None) == []
        real_time = time.time
        monkeypatch.setattr(time, "time", lambda: real_time() + oc.FAILURE_TTL + 5)
        assert oc.load_official_catalog(lambda: doc()) != []
        # And the document replaced the failure marker rather than sitting beside it.
        monkeypatch.setattr(time, "time", real_time)
        assert oc._FAILED_KEY not in (oc._read_cache() or {})


class TestRedirectsAreRefused:
    """A redirect silently changes ORIGIN, and this module's whole trust basis is
    "TLS to our own domain".

    `urlopen` follows 3xx automatically and `HTTPRedirectHandler` permits `http`
    as a target, so a `302` to `http://127.0.0.1/...` is followed and the gateway
    issues an unauthenticated request to a service on the user's own machine on
    behalf of a remote document. `_https_request` cannot catch that: it validates
    the URL we ASK for, not the one we arrive at.

    These tests drive a REAL loopback server rather than mocking the handler,
    because the property under test is what urllib does with a live 3xx, not what
    our own code thinks it does.
    """

    @staticmethod
    def _serve(handler_cls):
        srv = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv

    def test_a_redirect_to_loopback_http_is_not_followed(self, monkeypatch):
        reached = []

        class Target(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                reached.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"schemaVersion": 1, "apps": []}')

            def log_message(self, *a):  # noqa: D102
                pass

        target = self._serve(Target)
        port = target.server_address[1]

        class Redirector(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{port}/secret")
                self.end_headers()

            def log_message(self, *a):  # noqa: D102
                pass

        redirector = self._serve(Redirector)
        try:
            url = f"http://127.0.0.1:{redirector.server_address[1]}/official-registry.json"
            # Point the fetch at the redirector. The https guard is bypassed for
            # this test on purpose: the property under test is the REDIRECT
            # refusal, and a test that could not reach a 3xx would prove nothing.
            monkeypatch.setattr(oc, "OFFICIAL_CATALOG_URL", url)
            monkeypatch.setattr(oc, "_https_request", lambda u: urllib.request.Request(u, method="GET"))
            assert oc._download() is None, "a 3xx must be a fetch failure"
            assert reached == [], f"the redirect target was contacted: {reached}"
        finally:
            redirector.shutdown()
            target.shutdown()

    def test_the_refusal_is_caught_as_an_ordinary_fetch_failure(self):
        """`HTTPError` subclasses `URLError`, so the existing tuple catches it --
        asserted rather than assumed, because if it did not the raise would
        escape into the registry handler as a 500."""
        assert issubclass(urllib.error.HTTPError, urllib.error.URLError)

    def test_the_handler_is_installed_on_the_opener(self):
        opener = urllib.request.build_opener(oc._NoRedirects)
        assert any(isinstance(h, oc._NoRedirects) for h in opener.handlers)
