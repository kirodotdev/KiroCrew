"""Wire-level tests for Teams file support in both directions.

Three seams:

* ``TeamsClient.download_inbound_file`` -- where the credential decision, the URL
  vetting and the read ceiling live. Getting any of these wrong is a credential
  leak, an SSRF proxy, or an unbounded read.
* ``TeamsRenderer.on_done`` -- the single outbound seal: extraction, the inline
  ``data:`` URI activity, and the refusals that keep a path visible.
* ``TeamsTransport.receive`` -> ``TeamsDispatcher.handle_message`` -- that a
  file-only message still runs, that nothing is fetched until the personal-scope
  and allow-list gates have passed, and that a temp file outlives the turn that
  reads it whether that turn runs now or after the mid-turn queue drains.

The policy layer (envelope mapping, format allow-list, budgets) is in
``test_teams_attachments.py``.
"""

from __future__ import annotations

import base64
import os
import time
from typing import Any

import pytest

from kiro_crew.teams.attachments import TEAMS_FILE_DOWNLOAD_INFO
from kiro_crew.teams.client import (
    TEAMS_MAX_DOWNLOAD_BYTES,
    TeamsClient,
    TeamsInbound,
    TeamsSendError,
)
from kiro_crew.teams.client import resolve_addresses as _real_resolve_addresses
from kiro_crew.teams.renderer import TeamsRenderer
from kiro_crew.teams.transport import TEAMS_CAPABILITIES, TeamsTransport

#: A genuinely globally-routable address for the resolver stub. NOT a
#: documentation range (192.0.2/198.51.100/203.0.113): Python classifies those as
#: private, so using one would make every stubbed host refuse.
_PUBLIC_ADDR = "93.184.216.34"

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


# ── Download fakes ──


class _FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, size: int):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = _FakeContent(chunks or [])

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeSession:
    closed = False

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((url, dict(kwargs.get("headers") or {})))
        return self._responses.pop(0)


def _client(responses: list[_FakeResponse]) -> tuple[TeamsClient, _FakeSession]:
    client = TeamsClient(app_id="app", app_password="pw")
    session = _FakeSession(responses)
    # Both seams: the Connector session and the download session (which in production
    # carries the pin-only resolver, so the socket dials the vetted address).
    client._session = session  # type: ignore[assignment]
    client._download_session = session  # type: ignore[assignment]
    # Pre-seed the app token so no attempt reaches the token endpoint.
    client._token = "app-token"
    client._token_expiry = time.monotonic() + 3600
    return client, session


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch) -> None:
    """Answer every attachment-host lookup from a table, never from the network.

    The SSRF vet resolves each hop, and a test that reached a real resolver would be
    both slow and dependent on whoever owns ``contoso.sharepoint.com`` today. The
    resolver itself is exercised directly in :class:`TestResolvedAddressVetting`.
    """

    async def _fake(host: str, port: int = 443) -> list[str]:
        return [_PUBLIC_ADDR]

    monkeypatch.setattr("kiro_crew.teams.client.resolve_addresses", _fake)


class TestInboundDownloadAuth:
    @pytest.mark.asyncio
    async def test_preauthorized_download_url_never_sees_the_bot_token(self, tmp_path) -> None:
        """The Connector token is credential-equivalent and this host is arbitrary.

        Microsoft documents the personal-chat ``downloadUrl`` as fetchable with a
        plain GET, so attaching the token would hand a secret to whoever owns the
        host for no benefit at all.
        """
        client, session = _client([_FakeResponse(chunks=[PNG_BYTES])])
        dest = tmp_path / "f.png"
        await client.download_inbound_file(
            "https://contoso.sharepoint.com/dl", str(dest), authenticated=False
        )
        assert dest.read_bytes() == PNG_BYTES
        assert session.calls == [("https://contoso.sharepoint.com/dl", {})]

    @pytest.mark.asyncio
    async def test_token_goes_only_to_a_recognized_bot_framework_host(self, tmp_path) -> None:
        client, session = _client([_FakeResponse(chunks=[PNG_BYTES])])
        await client.download_inbound_file(
            "https://smba.trafficmanager.net/amer/img", str(tmp_path / "a.png"), authenticated=True
        )
        assert session.calls[0][1] == {"Authorization": "Bearer app-token"}

    @pytest.mark.asyncio
    async def test_unknown_host_is_fetched_anonymously_even_when_authenticated(
        self, tmp_path
    ) -> None:
        """Fail closed on the host: Microsoft does not document which host serves an
        inline image, so an unrecognized one gets no credential."""
        client, session = _client([_FakeResponse(chunks=[PNG_BYTES])])
        await client.download_inbound_file(
            "https://cdn.attacker.example/img", str(tmp_path / "a.png"), authenticated=True
        )
        assert session.calls == [("https://cdn.attacker.example/img", {})]

    @pytest.mark.asyncio
    async def test_a_lookalike_host_is_not_trusted(self, tmp_path) -> None:
        """The suffix match is dot-anchored, so a substring cannot satisfy it."""
        client, session = _client([_FakeResponse(chunks=[PNG_BYTES])])
        await client.download_inbound_file(
            "https://botframework.com.evil.example/img",
            str(tmp_path / "a.png"),
            authenticated=True,
        )
        assert session.calls[0][1] == {}

    @pytest.mark.asyncio
    async def test_a_401_with_the_token_falls_back_to_anonymous(self, tmp_path) -> None:
        """Microsoft's guidance for the inline-image case contradicts itself, so both
        documented orders are attempted rather than picking one and being wrong."""
        client, session = _client([_FakeResponse(status=401), _FakeResponse(chunks=[PNG_BYTES])])
        dest = tmp_path / "a.png"
        await client.download_inbound_file(
            "https://smba.trafficmanager.net/img", str(dest), authenticated=True
        )
        assert dest.read_bytes() == PNG_BYTES
        assert [headers for _url, headers in session.calls] == [
            {"Authorization": "Bearer app-token"},
            {},
        ]

    @pytest.mark.asyncio
    async def test_a_redirect_off_a_trusted_host_drops_the_token(self, tmp_path) -> None:
        client, session = _client(
            [
                _FakeResponse(status=302, headers={"Location": "https://cdn.other.example/x"}),
                _FakeResponse(chunks=[PNG_BYTES]),
            ]
        )
        await client.download_inbound_file(
            "https://smba.trafficmanager.net/img", str(tmp_path / "a.png"), authenticated=True
        )
        assert session.calls[0][1] == {"Authorization": "Bearer app-token"}
        assert session.calls[1] == ("https://cdn.other.example/x", {})


class TestInboundDownloadBounds:
    @pytest.mark.asyncio
    async def test_the_cap_is_on_bytes_read_not_content_length(self, tmp_path) -> None:
        """A header that understates the body must not smuggle an unbounded read."""
        client, _session = _client(
            [_FakeResponse(headers={"Content-Length": "4"}, chunks=[b"x" * 64, b"x" * 64])]
        )
        with pytest.raises(ValueError, match="exceeds"):
            await client.download_inbound_file(
                "https://contoso.sharepoint.com/dl", str(tmp_path / "big"), max_bytes=100
            )

    @pytest.mark.asyncio
    async def test_a_body_exactly_at_the_cap_is_accepted(self, tmp_path) -> None:
        client, _session = _client([_FakeResponse(chunks=[b"y" * 100])])
        dest = tmp_path / "ok"
        await client.download_inbound_file(
            "https://contoso.sharepoint.com/dl", str(dest), max_bytes=100
        )
        assert dest.read_bytes() == b"y" * 100

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "http://contoso.sharepoint.com/dl",
            "https://localhost/dl",
            "https://box.internal/dl",
            "https://127.0.0.1/dl",
            "https://[::1]/dl",
            "https://contoso.sharepoint.com:8443/dl",
            # A trailing FQDN root dot is the SAME host to every resolver, so a
            # blocklist that compares the raw name admits it while refusing the
            # bare spelling.
            "https://localhost./dl",
            "https://metadata.google.internal./dl",
            "https://box.internal./dl",
        ],
    )
    async def test_unfetchable_urls_are_refused_before_any_request(self, tmp_path, url) -> None:
        client, session = _client([])
        with pytest.raises(ValueError):
            await client.download_inbound_file(url, str(tmp_path / "f"))
        assert session.calls == []

    @pytest.mark.asyncio
    async def test_a_redirect_loop_is_bounded(self, tmp_path) -> None:
        loop = [
            _FakeResponse(status=302, headers={"Location": "https://a.example/next"})
            for _ in range(6)
        ]
        client, _session = _client(loop)
        with pytest.raises(ValueError, match="redirects"):
            await client.download_inbound_file("https://a.example/start", str(tmp_path / "f"))

    def test_the_default_ceiling_matches_the_widest_neutral_class_cap(self) -> None:
        from kiro_crew.messaging.attachments import IngestLimits

        assert TEAMS_MAX_DOWNLOAD_BYTES == IngestLimits().max_document_bytes


class TestResolvedAddressVetting:
    """A NAME that resolves inward is refused, not just a name that looks inward."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",  # nip.io-style wildcard, no control of any zone needed
            "169.254.169.254",  # the cloud instance-metadata endpoint
            "10.1.2.3",
            "192.168.0.9",
            "::1",
            "fe80::1",
            "0.0.0.0",
        ],
    )
    async def test_a_public_name_resolving_inward_is_refused(
        self, tmp_path, monkeypatch, address
    ) -> None:
        async def _resolves_inward(host: str, port: int = 443) -> list[str]:
            return [address]

        monkeypatch.setattr("kiro_crew.teams.client.resolve_addresses", _resolves_inward)
        client, session = _client([_FakeResponse(chunks=[PNG_BYTES])])
        with pytest.raises(ValueError):
            await client.download_inbound_file("https://harmless.example/dl", str(tmp_path / "f"))
        assert session.calls == [], "refused BEFORE the request, not after"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "address",
        [
            "100.64.0.1",  # RFC 6598 CGNAT -- what a tailnet and carrier NAT hand out
            "100.127.255.254",
            "fec0::1",  # deprecated IPv6 site-local
            "::ffff:127.0.0.1",  # v4-mapped loopback
            "2002:0a00:0001::1",  # 6to4 naming 10.0.0.1
        ],
    )
    async def test_ranges_the_old_category_flag_vet_approved_are_refused(
        self, tmp_path, monkeypatch, address
    ) -> None:
        """These five passed the six-flag check this vet replaced.

        `100.64.0.0/10` is not in CPython's `is_private` table and `fec0::/10`
        reports `is_global=True`, so an enumerated-category check approved both --
        and on a host attached to a tailnet, that range IS the private network. The
        mapped and 6to4 forms were evaluated as written rather than unwrapped, so
        `::ffff:127.0.0.1` passed a check whose entire purpose was refusing
        loopback. `link_unfurl.address_is_not_public` closes all five.
        """

        async def _resolves_inward(host: str, port: int = 443) -> list[str]:
            return [address]

        monkeypatch.setattr("kiro_crew.teams.client.resolve_addresses", _resolves_inward)
        client, session = _client([_FakeResponse(chunks=[PNG_BYTES])])
        with pytest.raises(ValueError):
            await client.download_inbound_file("https://harmless.example/dl", str(tmp_path / "f"))
        assert session.calls == [], "refused BEFORE the request, not after"

    @pytest.mark.asyncio
    async def test_an_unparseable_resolved_address_still_refuses(
        self, tmp_path, monkeypatch
    ) -> None:
        """The shared vet fails closed on a literal it cannot read.

        The local `ipaddress.ip_address` guard that used to do this was dropped
        because the delegation subsumes it -- so the property needs its own test,
        or removing that guard would look like a regression.
        """

        async def _garbage(host: str, port: int = 443) -> list[str]:
            return ["not-an-address"]

        monkeypatch.setattr("kiro_crew.teams.client.resolve_addresses", _garbage)
        client, session = _client([_FakeResponse(chunks=[PNG_BYTES])])
        with pytest.raises(ValueError):
            await client.download_inbound_file("https://harmless.example/dl", str(tmp_path / "f"))
        assert session.calls == []

    @pytest.mark.asyncio
    async def test_one_bad_answer_among_several_refuses_the_whole_host(
        self, tmp_path, monkeypatch
    ) -> None:
        """Which address aiohttp picks is not ours to predict, so any hit refuses."""

        async def _mixed(host: str, port: int = 443) -> list[str]:
            return [_PUBLIC_ADDR, "127.0.0.1"]

        monkeypatch.setattr("kiro_crew.teams.client.resolve_addresses", _mixed)
        client, session = _client([_FakeResponse(chunks=[PNG_BYTES])])
        with pytest.raises(ValueError):
            await client.download_inbound_file("https://mixed.example/dl", str(tmp_path / "f"))
        assert session.calls == []

    @pytest.mark.asyncio
    async def test_an_unresolvable_host_refuses_rather_than_raising_oserror(
        self, tmp_path, monkeypatch
    ) -> None:
        async def _fails(host: str, port: int = 443) -> list[str]:
            raise OSError("nodename nor servname provided")

        monkeypatch.setattr("kiro_crew.teams.client.resolve_addresses", _fails)
        client, _session = _client([])
        with pytest.raises(ValueError, match="unresolvable"):
            await client.download_inbound_file("https://gone.example/dl", str(tmp_path / "f"))

    @pytest.mark.asyncio
    async def test_a_redirect_hop_is_vetted_too(self, tmp_path, monkeypatch) -> None:
        """The bytes come from the LAST hop, so the first hop's verdict is not enough."""
        seen: list[str] = []

        async def _second_hop_is_internal(host: str, port: int = 443) -> list[str]:
            seen.append(host)
            return ["127.0.0.1"] if host == "inside.example" else [_PUBLIC_ADDR]

        monkeypatch.setattr("kiro_crew.teams.client.resolve_addresses", _second_hop_is_internal)
        client, session = _client(
            [_FakeResponse(status=302, headers={"Location": "https://inside.example/x"})]
        )
        with pytest.raises(ValueError):
            await client.download_inbound_file("https://outside.example/dl", str(tmp_path / "f"))
        assert seen == ["outside.example", "inside.example"]
        assert len(session.calls) == 1, "the inward hop is never requested"

    @pytest.mark.asyncio
    async def test_the_resolver_seam_reaches_a_real_lookup(self) -> None:
        """Loopback resolves locally, so this needs no network and no DNS server.

        Bound at import, before ``_no_real_dns`` rebinds the module symbol: the point
        is that the stub every other test uses stands in for something real.
        """
        assert "127.0.0.1" in await _real_resolve_addresses("127.0.0.1")


# ── Outbound seal ──


class _RenderClient:
    def __init__(self, *, image_fails: bool = False) -> None:
        self.sent: list[str] = []
        self.images: list[dict[str, Any]] = []
        self.cards: list[dict[str, Any]] = []
        self.image_fails = image_fails

    async def send_typing(self, conversation_id: str, service_url: str) -> None:
        return None

    async def send_message(self, conversation_id: str, content: str, service_url: str):
        self.sent.append(content)
        return f"mid-{len(self.sent)}"

    async def update_message(self, conversation_id, activity_id, content, service_url) -> bool:
        self.sent.append(content)
        return True

    async def send_card(self, conversation_id: str, card: dict, service_url: str):
        self.cards.append(card)
        return "card-1"

    async def send_inline_image(self, conversation_id: str, attachment: dict, service_url: str):
        if self.image_fails:
            raise TeamsSendError("HTTP 413")
        self.images.append(attachment)
        return "img-1"


class _FakeSel:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log_api_access(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


def _renderer(client: _RenderClient, root: str = "", session_key: str = "teams:a:direct:u"):
    return TeamsRenderer(
        client,
        "conv-1",
        "https://smba.trafficmanager.net/",
        TEAMS_CAPABILITIES,
        session_key=session_key,
        upload_root=root,
    )


async def _seal(renderer: TeamsRenderer, text: str) -> None:
    await renderer.on_text_chunk(text)
    await renderer.on_done()


class TestOutboundSeal:
    @pytest.mark.asyncio
    async def test_a_referenced_png_is_delivered_and_its_markup_removed(self, tmp_path) -> None:
        chart = tmp_path / "chart.png"
        chart.write_bytes(PNG_BYTES)
        client = _RenderClient()
        renderer = _renderer(client, str(tmp_path))
        await _seal(renderer, f"Here is the chart:\n\n![revenue]({chart})")
        assert client.sent == ["Here is the chart:"]
        assert len(client.images) == 1
        attachment = client.images[0]
        assert attachment["contentType"] == "image/png"
        assert attachment["contentUrl"].startswith("data:image/png;base64,")
        assert attachment["name"] == "revenue.png"
        # The raw path never reaches the conversation.
        assert str(chart) not in client.sent[0]

    @pytest.mark.asyncio
    async def test_an_image_only_reply_sends_no_placeholder_text(self, tmp_path) -> None:
        chart = tmp_path / "c.png"
        chart.write_bytes(PNG_BYTES)
        client = _RenderClient()
        await _seal(_renderer(client, str(tmp_path)), f"![c]({chart})")
        assert client.sent == []
        assert len(client.images) == 1

    @pytest.mark.asyncio
    async def test_a_non_raster_keeps_its_markup_and_says_why(self, tmp_path) -> None:
        """The neutral extractor refuses a script named ``.png`` by its bytes, and a
        refused reference keeps its markup so the path stays visible."""
        fake = tmp_path / "evil.png"
        fake.write_text("#!/bin/sh\nrm -rf /\n")
        client = _RenderClient()
        await _seal(_renderer(client, str(tmp_path)), f"look ![x]({fake})")
        assert client.images == []
        assert f"![x]({fake})" in client.sent[0]
        assert "not sent" in client.sent[0]

    @pytest.mark.asyncio
    async def test_a_raster_teams_cannot_inline_is_refused_by_path(self, tmp_path) -> None:
        """A WebP passes the neutral sniff but is not in Teams' inline set. The
        byte-level type is only knowable after the read, by which point the markup
        was cut, so the refusal names the resolved path instead."""
        webp = tmp_path / "pic.webp"
        webp.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
        client = _RenderClient()
        await _seal(_renderer(client, str(tmp_path)), f"see ![p]({webp})")
        assert client.images == []
        assert str(webp) in client.sent[0]
        assert "PNG and JPEG" in client.sent[0]

    @pytest.mark.asyncio
    async def test_a_reference_outside_the_approved_root_is_refused(self, tmp_path) -> None:
        outside = tmp_path / "outside" / "secret.png"
        outside.parent.mkdir()
        outside.write_bytes(PNG_BYTES)
        root = tmp_path / "workspace"
        root.mkdir()
        client = _RenderClient()
        await _seal(_renderer(client, str(root)), f"![s]({outside})")
        assert client.images == []
        assert str(outside) in client.sent[0]

    @pytest.mark.asyncio
    async def test_no_approved_root_fails_closed_and_keeps_the_markup(self, tmp_path) -> None:
        """With no boundary there is nothing to check a reference against, and the
        renderer must not fall back to reading the file anyway."""
        chart = tmp_path / "c.png"
        chart.write_bytes(PNG_BYTES)
        client = _RenderClient()
        # session_key empty => the persisted lookup is skipped entirely.
        await _seal(_renderer(client, "", session_key=""), f"![c]({chart})")
        assert client.images == []
        assert f"![c]({chart})" in client.sent[0]

    @pytest.mark.asyncio
    async def test_a_dashboard_keyed_turn_never_uploads(self, tmp_path) -> None:
        """A dashboard slot can be incognito; the renderer cannot resolve that, so it
        denies rather than shipping bytes out of a session meant to leave no trace."""
        chart = tmp_path / "c.png"
        chart.write_bytes(PNG_BYTES)
        client = _RenderClient()
        renderer = _renderer(client, "", session_key="dashboard:abc")
        await _seal(renderer, f"![c]({chart})")
        assert client.images == []

    @pytest.mark.asyncio
    async def test_an_undelivered_image_is_reported_not_dropped(self, tmp_path) -> None:
        chart = tmp_path / "c.png"
        chart.write_bytes(PNG_BYTES)
        client = _RenderClient(image_fails=True)
        await _seal(_renderer(client, str(tmp_path)), f"chart:\n\n![c]({chart})")
        assert client.images == []
        # The answer landed, then a follow-up named the picture that did not.
        assert client.sent[0] == "chart:"
        assert str(chart) in client.sent[1]

    @pytest.mark.asyncio
    async def test_alt_text_is_display_redacted_before_it_becomes_a_name(self, tmp_path) -> None:
        chart = tmp_path / "c.png"
        chart.write_bytes(PNG_BYTES)
        client = _RenderClient()
        await _seal(
            _renderer(client, str(tmp_path)),
            f"![AKIAIOSFODNN7EXAMPLE]({chart})",
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in client.images[0]["name"]

    @pytest.mark.asyncio
    async def test_an_empty_alt_falls_back_to_the_path_and_STILL_redacts(self, tmp_path) -> None:
        """With no alt text the name comes from the PATH, which the LLM also wrote.

        Extraction has already cut the path out of the answer body, so the
        attachment name is the only surviving sink -- and ``_SAFE_NAME_RE`` keeps
        every character a key id needs.
        """
        chart = tmp_path / "AKIAIOSFODNN7EXAMPLE.png"
        chart.write_bytes(PNG_BYTES)
        client = _RenderClient()
        await _seal(_renderer(client, str(tmp_path)), f"![]({chart})")
        assert client.images, "the image must still be delivered"
        assert client.images[0]["name"] == "image.png"

    def test_a_secret_the_length_cap_would_slice_is_still_caught(self) -> None:
        """The cap can cut a token down to a prefix the scanner no longer matches.

        Scanning only the finished name would ship most of the secret; the source is
        scanned first, while the token is still intact.
        """
        from kiro_crew.messaging.outbound_files import OutboundFile
        from kiro_crew.teams.attachments import _MAX_INLINE_NAME_CHARS, inline_image_name

        token = "ghp_" + "a1b2c3d4e5" * 4
        stem = "q" * 40 + token
        assert len(stem) > _MAX_INLINE_NAME_CHARS, "the cap must actually bite"
        name = inline_image_name(
            "", OutboundFile(path=f"/w/{stem}.png", data=PNG_BYTES, mime="image/png", alt="")
        )
        assert name == "image.png"
        assert "ghp_" not in name

    @pytest.mark.asyncio
    async def test_a_plain_answer_never_touches_the_filesystem(self, tmp_path, monkeypatch) -> None:
        """A reply with no image markup must not pay for a root lookup or a scan."""
        called: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.teams.renderer._persisted_upload_root",
            lambda key: called.append(key) or "",
        )
        client = _RenderClient()
        await _seal(_renderer(client, ""), "just prose")
        assert called == []
        assert client.sent == ["just prose"]

    @pytest.mark.asyncio
    async def test_the_audit_carries_counts_and_reason_codes_only(
        self, tmp_path, monkeypatch
    ) -> None:
        fake = _FakeSel()
        monkeypatch.setattr("kiro_crew.teams.renderer.sel", lambda: fake)
        chart = tmp_path / "chart.png"
        chart.write_bytes(PNG_BYTES)
        missing = tmp_path / "gone.png"
        client = _RenderClient()
        await _seal(_renderer(client, str(tmp_path)), f"![a]({chart}) ![b]({missing})")
        uploads = [e for e in fake.events if e["operation"] == "teams_renderer.upload_files"]
        outcomes = {e["outcome"] for e in uploads}
        assert outcomes == {"allowed", "denied"}
        for event in uploads:
            payload = " ".join(str(v) for v in event.values())
            assert str(chart) not in payload
            assert str(missing) not in payload
            assert "chart.png" not in payload
        denied = next(e for e in uploads if e["outcome"] == "denied")
        assert denied["error"] == "missing"
        assert denied["resources"] == "1 rejection(s)"


# ── Inbound transport gating ──


def _inbound(**overrides: Any) -> TeamsInbound:
    base: dict[str, Any] = dict(
        conversation_id="conv-1",
        conversation_type="personal",
        service_url="https://smba.trafficmanager.net/",
        text="",
        user_email="alice@example.com",
        aad_object_id="aad-1",
        activity_id="act-1",
        attachments=[
            {
                "contentType": TEAMS_FILE_DOWNLOAD_INFO,
                "name": "shot.png",
                "content": {"downloadUrl": "https://contoso.sharepoint.com/dl", "fileType": "png"},
            }
        ],
    )
    base.update(overrides)
    return TeamsInbound(**base)


class _IngestClient:
    def __init__(self) -> None:
        self.fetched: list[str] = []
        self.sent: list[str] = []

    async def download_inbound_file(self, url: str, dest: str, **kwargs: Any) -> None:
        self.fetched.append(url)
        with open(dest, "wb") as fh:
            fh.write(PNG_BYTES)

    # The queue receipt and the typing indicator go through the same client.
    async def send_message(self, conversation_id: str, content: str, service_url: str) -> str:
        self.sent.append(content)
        return f"mid-{len(self.sent)}"

    async def update_message(
        self, conversation_id: str, activity_id: str, content: str, service_url: str
    ) -> bool:
        self.sent.append(content)
        return True

    async def send_typing(self, conversation_id: str, service_url: str) -> None:
        return None


class _TurnRecorder:
    """Stands in for ``drive_turn``, capturing the prompt and the files' liveness."""

    def __init__(self, tmp_root: str) -> None:
        self._root = tmp_root
        self.prompts: list[str] = []
        self.paths: list[str] = []

    async def __call__(self, turn: Any, **_: Any) -> None:
        self.prompts.append(turn.user_text)
        seen = [line for line in turn.user_text.splitlines() if line.startswith(self._root)]
        self.paths.extend(seen)
        # A path handed to the model MUST still be a readable file at this moment;
        # the ACP encoder silently skips one that is not, so a dead path reaches
        # the model as prose and the turn answers about nothing.
        assert seen and all(os.path.exists(p) for p in seen), turn.user_text


def _files_dispatcher(sessions: Any, client: Any) -> Any:
    from test_teams_midturn import _dispatcher

    return _dispatcher(sessions, client)


class TestInboundGating:
    def test_capabilities_declare_both_directions(self) -> None:
        assert TEAMS_CAPABILITIES.files_inbound is True
        assert TEAMS_CAPABILITIES.files_outbound is True

    @pytest.mark.asyncio
    async def test_the_transport_hands_the_dispatcher_raw_descriptors(self) -> None:
        """Ingest belongs to the DISPATCHER, so nothing is fetched in this frame.

        Downloading here would delete the temp files before a mid-turn arrival's
        queued turn ever opened them (see ``TestMidTurnFiles``).
        """
        client = _IngestClient()
        seen: list[TeamsInbound] = []

        async def _dispatch(msg: TeamsInbound) -> None:
            seen.append(msg)

        transport = TeamsTransport(
            client,  # type: ignore[arg-type]
            allowed_emails=["alice@example.com"],
            dispatch=_dispatch,
        )
        await transport.receive(_inbound())
        assert client.fetched == []
        assert seen[0].attachments and seen[0].attachments[0]["name"] == "shot.png"

    @pytest.mark.asyncio
    async def test_a_group_scope_is_denied_before_any_fetch(self, monkeypatch) -> None:
        fake = _FakeSel()
        monkeypatch.setattr("kiro_crew.teams.transport.sel", lambda: fake)
        client = _IngestClient()
        dispatched: list[TeamsInbound] = []

        async def _dispatch(msg: TeamsInbound) -> None:
            dispatched.append(msg)

        transport = TeamsTransport(
            client,  # type: ignore[arg-type]
            allowed_emails=["alice@example.com"],
            dispatch=_dispatch,
        )
        await transport.receive(_inbound(conversation_type="groupChat"))
        # Never dispatched is what keeps it un-fetched: the ingest is one layer in.
        assert dispatched == []
        assert client.fetched == []
        assert fake.events[0]["outcome"] == "denied_non_personal_scope"

    @pytest.mark.asyncio
    async def test_an_unauthorized_sender_is_denied_before_any_fetch(self, monkeypatch) -> None:
        fake = _FakeSel()
        monkeypatch.setattr("kiro_crew.teams.transport.sel", lambda: fake)
        client = _IngestClient()
        dispatched: list[TeamsInbound] = []

        async def _dispatch(msg: TeamsInbound) -> None:
            dispatched.append(msg)

        transport = TeamsTransport(
            client,  # type: ignore[arg-type]
            allowed_emails=["bob@example.com"],
            dispatch=_dispatch,
        )
        await transport.receive(_inbound())
        assert dispatched == []
        assert client.fetched == []
        assert any(e["outcome"] == "denied" for e in fake.events)


class TestMidTurnFiles:
    """The ingest runs in the frame that awaits the turn -- including a drained one."""

    @pytest.mark.asyncio
    async def test_a_file_only_message_reaches_the_turn(self, tmp_path, monkeypatch) -> None:
        """A Teams upload carries attachments and NO text; requiring text would
        discard the whole message, and a caption is never read as a command."""
        from test_teams_midturn import _Provider, _Sessions

        monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
        recorder = _TurnRecorder(str(tmp_path))
        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.drive_turn", recorder)
        client = _IngestClient()
        dispatcher = _files_dispatcher(_Sessions(_Provider(), busy=False), client)

        await dispatcher.handle_message(_inbound(user_email="me@example.com"))

        assert client.fetched == ["https://contoso.sharepoint.com/dl"]
        assert recorder.paths, "the image must reach the prompt as a path line"
        # …and be gone once the turn that read it returned.
        assert not any(os.path.exists(p) for p in recorder.paths)

    @pytest.mark.asyncio
    async def test_a_caption_that_looks_like_a_command_is_not_one(
        self, tmp_path, monkeypatch
    ) -> None:
        from test_teams_midturn import _Provider, _Sessions

        monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
        recorder = _TurnRecorder(str(tmp_path))
        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.drive_turn", recorder)
        sessions = _Sessions(_Provider(), busy=False)
        dispatcher = _files_dispatcher(sessions, _IngestClient())

        await dispatcher.handle_message(_inbound(user_email="me@example.com", text="/stop this"))

        assert sessions.cleared == [], "an upload caption must not cancel the turn"
        assert recorder.prompts and "/stop this" in recorder.prompts[0]

    @pytest.mark.asyncio
    async def test_a_picture_sent_mid_turn_survives_until_the_drained_turn_reads_it(
        self, tmp_path, monkeypatch
    ) -> None:
        """The regression: a queued upload must be fetched for the turn that runs.

        Ingesting at arrival and unlinking in that frame left the drained turn a
        prompt naming a file that no longer existed -- the encoder skips it, so the
        model answered about nothing with nothing logged.
        """
        from test_teams_midturn import _Provider, _Sessions

        monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
        recorder = _TurnRecorder(str(tmp_path))
        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.drive_turn", recorder)
        client = _IngestClient()
        sessions = _Sessions(_Provider(), busy=True)
        dispatcher = _files_dispatcher(sessions, client)
        key = dispatcher._session_key("me@example.com")

        # Arrives mid-turn: queued, and NOT downloaded -- a queued message may wait
        # minutes, and steering would have carried the text and dropped the file.
        await dispatcher.handle_message(_inbound(user_email="me@example.com"))
        assert client.fetched == []
        assert sessions.queues[key][0][2]["attachments"], "descriptors must ride the entry"

        # The turn ends and the queue drains: NOW it is fetched, for the turn that
        # reads it.
        sessions._busy = False
        await dispatcher._drain_queue(key, _inbound(user_email="me@example.com"))

        assert client.fetched == ["https://contoso.sharepoint.com/dl"]
        assert recorder.paths, "the drained turn must receive the image"
        assert not any(os.path.exists(p) for p in recorder.paths)

    @pytest.mark.asyncio
    async def test_a_body_echo_only_activity_runs_no_turn(self, monkeypatch) -> None:
        """Teams attaches rich text as ``text/html``; that is not a prompt."""
        from test_teams_midturn import _Provider, _Sessions

        turns: list[Any] = []

        async def _drive(turn: Any, **_: Any) -> None:
            turns.append(turn)

        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.drive_turn", _drive)
        dispatcher = _files_dispatcher(_Sessions(_Provider(), busy=False), _IngestClient())

        await dispatcher.handle_message(
            _inbound(
                user_email="me@example.com",
                attachments=[{"contentType": "text/html", "content": "<p>hi</p>"}],
            )
        )
        assert turns == []


class TestTheSocketDialsTheVettedAddress:
    """Vetting a NAME cannot close DNS rebinding; only vetting what is dialed can.

    aiohttp resolves a URL's host itself, so a pre-fetch vet and the connect are two
    lookups: the first answers publicly, the second -- microseconds before the socket
    opens -- can answer ``169.254.169.254``. The download session's connector therefore
    resolves through a pin-only resolver, so there is no second lookup at all.
    """

    @pytest.mark.asyncio
    async def test_the_resolver_serves_only_what_was_pinned(self) -> None:
        from kiro_crew.teams.client import _VettedResolver

        resolver = _VettedResolver()
        resolver.pin("contoso.sharepoint.com", [_PUBLIC_ADDR])

        results = await resolver.resolve("contoso.sharepoint.com", 443)

        assert [r["host"] for r in results] == [_PUBLIC_ADDR]
        # The hostname is preserved, which is what keeps TLS SNI and Host correct --
        # dialing by IP instead would fail certificate validation.
        assert results[0]["hostname"] == "contoso.sharepoint.com"
        assert results[0]["port"] == 443

    @pytest.mark.asyncio
    async def test_an_unpinned_host_is_refused_not_resolved(self) -> None:
        """Fail CLOSED: a path that reaches this session without vetting cannot fetch."""
        from kiro_crew.teams.client import _VettedResolver

        resolver = _VettedResolver()

        with pytest.raises(OSError, match="unvetted host"):
            await resolver.resolve("attacker.example", 443)

    @pytest.mark.asyncio
    async def test_a_trailing_dot_resolves_to_the_same_pin(self) -> None:
        """``host.`` and ``host`` are the same name; only one of them was vetted."""
        from kiro_crew.teams.client import _VettedResolver

        resolver = _VettedResolver()
        resolver.pin("contoso.sharepoint.com.", [_PUBLIC_ADDR])

        for spelling in ("contoso.sharepoint.com", "CONTOSO.sharepoint.com."):
            assert [r["host"] for r in await resolver.resolve(spelling, 443)] == [_PUBLIC_ADDR]

    @pytest.mark.asyncio
    async def test_the_pin_map_is_bounded(self) -> None:
        """A long-lived gateway that fetched from many hosts must not grow it forever."""
        from kiro_crew.teams.client import _PINNED_HOSTS_MAX, _VettedResolver

        resolver = _VettedResolver()
        for index in range(_PINNED_HOSTS_MAX + 5):
            resolver.pin(f"h{index}.example", [_PUBLIC_ADDR])

        assert len(resolver._pinned) == _PINNED_HOSTS_MAX
        # The newest survive, and an evicted entry costs a fresh lookup, never a
        # weaker check: the next fetch vets again before it pins again.
        assert await resolver.resolve(f"h{_PINNED_HOSTS_MAX + 4}.example", 443)
        with pytest.raises(OSError):
            await resolver.resolve("h0.example", 443)

    @pytest.mark.asyncio
    async def test_a_fetch_pins_every_hop_it_vetted(self, tmp_path, monkeypatch) -> None:
        """Per hop, because a redirect is a new host with its own vet and its own pin."""
        client, _session = _client(
            [
                _FakeResponse(status=302, headers={"Location": "https://cdn.example/final"}),
                _FakeResponse(chunks=[b"payload"]),
            ]
        )

        await client.download_inbound_file(
            "https://contoso.sharepoint.com/a", str(tmp_path / "f.bin")
        )

        pinned = client._resolver._pinned
        assert pinned["contoso.sharepoint.com"] == [_PUBLIC_ADDR]
        assert pinned["cdn.example"] == [_PUBLIC_ADDR]

    @pytest.mark.asyncio
    async def test_a_host_the_vet_refuses_is_never_pinned(self, tmp_path, monkeypatch) -> None:
        async def _inward(host: str, port: int = 443) -> list[str]:
            return ["169.254.169.254"]

        monkeypatch.setattr("kiro_crew.teams.client.resolve_addresses", _inward)
        client, _session = _client([_FakeResponse(chunks=[b"secrets"])])

        with pytest.raises(ValueError):
            await client.download_inbound_file(
                "https://attacker.example/a", str(tmp_path / "f.bin")
            )

        assert client._resolver._pinned == {}
