"""Inline-media ``data:`` URI carve-out — behaviour and its CSP gate.

An MCP app emits ``<img src="data:image/webp;base64,…">``. The base64 body is a
rendered sub-resource (the browser decodes it and issues no request) but is
structurally identical to an encoded secret, so before this carve-out the
credential and exfil passes spliced a ``[REDACTED: credential]`` tag INTO the
``src`` and stranded the image.

The carve-out is default-deny and bounded twice over:

1. **Surface-scoped.** The two bare passes ``redact_credentials`` /
   ``redact_exfiltration_urls`` carry NO media awareness — every direct caller
   scans inline media in full. Only the composed facade helper
   ``redact_mcp_app_payload_text`` masks media around both passes. So the
   "survives byte-identical" tests target the FACADE, while
   :class:`TestBarePassesAreMediaUnaware` asserts the bare passes still REDACT a
   media URI.
2. **CSP-gated per payload.** An MCP-app iframe is NOT egress-free: the app's own
   ``connectDomains`` replace ``connect-src 'none'`` in ``buildMcpAppCsp``, and
   the app's JS and its CSP metadata share one author. So
   ``mcp_apps_render._payload_redactor`` hands the carve-out to a payload ONLY
   when its declared CSP grants no outbound origin, and the strict pass otherwise
   — pinned in :class:`TestCspGate`.

The MIME scope is deliberately MEDIA-ONLY: an ``image/`` or ``font/`` ``data:``
URI is eligible; a ``data:text/…`` blob is still scanned, so it cannot become a
smuggling channel. Widening the MIME scope to ``text``/``application`` would open
exactly that, and these tests fail if it does. ``blob:`` is out of scope
entirely: the consuming iframe's ``img-src 'self' data:`` cannot render one.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest

from kiro_crew.mcp_apps_render import (
    _CSP_EGRESS_KEYS,
    _csp_grants_egress,
    _payload_redactor,
    _redact_leaves,
)
from kiro_crew.security import (
    _mask_media_data_uris,
    _media_body_is_clean,
    _unmask_media_data_uris,
    redact_credentials,
    redact_exfiltration_urls,
    redact_mcp_app_payload_text,
)

# The scope primitives stay OFF the security facade — like ``_MEDIA_DATA_URI_RE``,
# they are imported from their owning module by tests.
from kiro_crew.security import redaction
from kiro_crew.security.redaction import (
    _MEDIA_DATA_URI_RE,
    _MEDIA_URI_PREFIX_RE,
    _media_head_is_plausible,
)

# A signed JWT — the exact shape the credential pass redacts. Embedding it inside
# a media body proves the mask (not luck) is what leaves the src intact.
_JWS = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0"
    ".dQw4w9WgXcQdQw4w9WgXcQdQw4w9WgXcQdQw4w9WgXc"
)

# A long, plausible webp base64 body (no dots, so not a JWT itself) — the 40+
# base64-char run that trips the base64/entropy heuristics. Built with
# ``b64encode`` rather than written as a literal because the carve-out now decodes
# the WHOLE body: a hand-written literal with a stray '=' mid-string is not valid
# base64, and the gate correctly refuses to exempt what it cannot decode. Decodes
# to the webp signature (``RIFF`` at 0, ``WEBP`` at 8) plus benign filler.
_WEBP_BODY = base64.b64encode(
    b"RIFF" + b"\x12\x00\x00\x00" + b"WEBP" + b"\x00\x01\x02\x03" * 24
).decode()
_WEBP_URI = f"data:image/webp;base64,{_WEBP_BODY}"
_IMG = f'<img src="{_WEBP_URI}" alt="Art Deco Diamond and Emerald Necklace in Platinum">'

# A webp body whose head decodes to the ``RIFF….WEBP`` container signature (so the
# plausibility gate exempts it on the opted-in surface) AND whose tail carries a
# credential shape (a dot-stripped JWS) that the media-UNAWARE bare passes redact.
# ``_WEBP_BODY`` above survives even the STRICT pass "by accident" (it does not
# trip the credential heuristics), so it cannot show the strict-vs-facade
# divergence. This body can: bare pass → redacted, facade →
# byte-identical. ``UklGRhIAAABXRUJQ`` decodes to ``b'RIFF\x12\x00\x00\x00WEBP'``.
_WEBP_HEAD_B64 = "UklGRhIAAABXRUJQ"
_JWS_NO_DOTS = _JWS.replace(".", "")
_CRED_WEBP_BODY = _WEBP_HEAD_B64 + _JWS_NO_DOTS
_CRED_WEBP_BODY += "A" * ((4 - len(_CRED_WEBP_BODY) % 4) % 4)
_CRED_WEBP_URI = f"data:image/webp;base64,{_CRED_WEBP_BODY}"
_CRED_IMG = f'<img src="{_CRED_WEBP_URI}">'

# The same shape for a font media URI: ``d09GMgABAAAA`` decodes to the woff2
# ``wOF2`` signature, and the tail carries the credential shape.
_WOFF2_HEAD_B64 = "d09GMgABAAAA"
_CRED_WOFF2_BODY = _WOFF2_HEAD_B64 + _JWS_NO_DOTS
_CRED_WOFF2_BODY += "A" * ((4 - len(_CRED_WOFF2_BODY) % 4) % 4)
_CRED_WOFF2_URI = f"data:font/woff2;base64,{_CRED_WOFF2_BODY}"

# The four CSP metadata keys that put an outbound origin in the delivered policy,
# read from the module under test so the tests cannot drift from the gate.
_EGRESS_KEYS = _CSP_EGRESS_KEYS

# The frontend CSP builder, for the coupling pin in :class:`TestCspGate`.
_MCP_APP_SRCDOC_TS = (
    Path(__file__).resolve().parents[1] / "website" / "src" / "lib" / "mcpAppSrcdoc.ts"
)


class TestBehaviour:
    """The media URI survives ONLY through the surface-scoped facade.

    The carve-out lives outside the bare passes, so these "survives
    byte-identical / is exempt" assertions target
    ``redact_mcp_app_payload_text`` — the one media-aware batch entry point.
    That helper masks the media URI around both passes and restores it
    byte-identical, so the src is untouched. The inverse (a bare pass REDACTS a
    media URI) is pinned in :class:`TestBarePassesAreMediaUnaware`.
    """

    def test_image_data_uri_survives_facade_byte_identical(self) -> None:
        result, warnings = redact_mcp_app_payload_text(_IMG)
        assert result == _IMG
        assert warnings == []

    def test_image_body_embedding_a_real_jwt_is_still_protected(self) -> None:
        # The credential-shaped body (a dot-stripped JWS run) would be redacted by
        # the media-unaware passes, corrupting the src. Through the facade the mask
        # (not luck — this body DOES trip the heuristics, unlike ``_WEBP_BODY``)
        # leaves the src byte-identical.
        result, warnings = redact_mcp_app_payload_text(_CRED_IMG)
        assert result == _CRED_IMG
        # Quick check: the bare pass proves this body really is redaction-bait.
        bare, bare_warnings = redact_credentials(_CRED_IMG)
        assert bare != _CRED_IMG
        assert bare_warnings

    def test_blob_url_is_not_in_scope(self) -> None:
        # ``blob:`` is deliberately OUT of the carve-out: the consuming iframe's
        # policy is ``img-src 'self' data:`` with no ``blob:``, so a blob URL
        # cannot render there and exempting one would widen the carve-out with no
        # defect to fix. It carries no MIME label or base64 body for the
        # plausibility gate either. The pattern must not match one.
        blob = "blob:https://dash.example.test/9f1c-4a2b-babe"
        assert _MEDIA_DATA_URI_RE.search(blob) is None
        masked, originals = _mask_media_data_uris(f'<img src="{blob}">', _media_body_is_clean)
        assert originals == []

    def test_bare_jwt_outside_a_media_uri_is_still_redacted(self) -> None:
        result, warnings = redact_credentials(f"leaked: {_JWS}")
        assert _JWS not in result
        assert warnings

    def test_text_plain_data_uri_is_still_scanned(self) -> None:
        # Media-scoped: a non-media data: URI is NOT exempt, so a secret hidden in
        # a text/plain base64 body must still be redacted — even through the
        # media-aware facade.
        uri = f"data:text/plain;base64,{_JWS}"
        result, _ = redact_mcp_app_payload_text(f"note: {uri}")
        assert _JWS not in result

    def test_application_octet_stream_data_uri_is_still_scanned(self) -> None:
        uri = f"data:application/octet-stream;base64,{_JWS}"
        result, _ = redact_mcp_app_payload_text(f"blob: {uri}")
        assert _JWS not in result


class TestBarePassesAreMediaUnaware:
    """The inverse of :class:`TestBehaviour`: the bare ``redact_credentials`` /
    ``redact_exfiltration_urls`` carry NO media awareness, so a media URI fed
    DIRECTLY to a bare pass is scanned in full — a credential-shaped body gets a
    redaction tag spliced in.

    Only the facade exempts a media URI; a bare pass never does.
    ``test_text_plain_data_uri_is_still_scanned`` and the octet-stream case above
    cover the media-UNAWARE MIME scope; these add the labelled ``image`` /
    ``font`` media URI cases, which the bare passes redact.
    """

    def test_bare_credential_pass_redacts_a_labelled_image_media_uri(self) -> None:
        # A data:image/webp;base64,<credential-shaped body> fed to the bare
        # credential pass is scanned in full: the body is redacted.
        result, warnings = redact_credentials(_CRED_IMG)
        assert result != _CRED_IMG
        assert _CRED_WEBP_BODY not in result
        assert "[REDACTED: credential]" in result
        assert warnings

    def test_bare_credential_pass_redacts_a_labelled_font_media_uri(self) -> None:
        result, warnings = redact_credentials(f'url("{_CRED_WOFF2_URI}")')
        assert _CRED_WOFF2_BODY not in result
        assert "[REDACTED: credential]" in result
        assert warnings

    def test_bare_exfil_pass_redacts_a_media_uri_carrying_a_suspicious_url(
        self,
    ) -> None:
        # The exfil pass is likewise media-unaware: a media data: URI whose body
        # is not the constraint here — an exfiltration URL adjacent to it is
        # rewritten while the media URI is NOT spared by any carve-out. Feed a
        # data: URI whose surrounding text carries a redaction-worthy secret, and
        # confirm the bare exfil pass does not treat the media URI as exempt.
        exfil = f"https://evil.example/steal?token={_JWS}"
        text = f'{exfil} <img src="{_CRED_WEBP_URI}">'
        result, warnings = redact_exfiltration_urls(text)
        # The exfil pass fires on the suspicious URL (media-unaware: no carve-out
        # short-circuits the scan).
        assert warnings
        assert _JWS not in result


class TestMaskRoundTrip:
    def test_mask_then_unmask_is_identity(self) -> None:
        masked, originals = _mask_media_data_uris(_IMG, _media_body_is_clean)
        assert _WEBP_URI not in masked
        assert originals == [_WEBP_URI]
        restored, unmask_warnings = _unmask_media_data_uris(masked, originals)
        assert restored == _IMG
        assert unmask_warnings == []

    def test_placeholder_carries_no_credential_or_base64_shape(self) -> None:
        # The whole point of the placeholder: a pass run between mask and unmask
        # must leave it untouched, or the restore would fail.
        masked, originals = _mask_media_data_uris(_IMG, _media_body_is_clean)
        scanned, warnings = redact_credentials(masked)
        assert scanned == masked
        assert warnings == []
        restored, unmask_warnings = _unmask_media_data_uris(scanned, originals)
        assert restored == _IMG
        assert unmask_warnings == []

    def test_multiple_media_uris_are_masked_independently(self) -> None:
        # Two DIFFERENT plausible containers (webp + woff2) in one input each get
        # their own placeholder index and restore to their own original.
        html = f'<img src="{_CRED_WEBP_URI}"> and url({_CRED_WOFF2_URI})'
        masked, originals = _mask_media_data_uris(html, _media_body_is_clean)
        assert originals == [_CRED_WEBP_URI, _CRED_WOFF2_URI]
        assert _CRED_WEBP_URI not in masked
        assert _CRED_WOFF2_URI not in masked
        restored, warnings = _unmask_media_data_uris(masked, originals)
        assert restored == html
        assert warnings == []

    def test_an_implausible_body_is_never_masked(self) -> None:
        # A correctly-labelled MIME whose body decodes to no container signature
        # is left RAW for the passes — the plausibility gate, not the label,
        # decides. ``QQQQ…`` decodes to ``AAA…``, which is no container.
        html = '<img src="data:image/webp;base64,' + "Q" * 60 + '">'
        masked, originals = _mask_media_data_uris(html, _media_body_is_clean)
        assert originals == []
        assert masked == html

    def test_no_media_uri_is_a_cheap_noop(self) -> None:
        text = "no media here, just prose"
        masked, originals = _mask_media_data_uris(text, _media_body_is_clean)
        assert masked == text
        assert originals == []


class TestBatchHardening:
    """Batch-helper hardening: mask strips injected sentinels, unmask fails
    closed on an unresolved index, duplicate URIs round-trip, and a suspicious
    URL coexists with a media URI through the facade.

    These target the batch helpers and the ``redact_mcp_app_payload_text``
    facade directly rather than the media-unaware bare passes.
    """

    def test_injected_placeholder_sentinel_does_not_survive_or_collide(self) -> None:
        # An attacker-supplied \x00media:0\x00 sentinel sits next to a real media
        # URI. The mask strips such a look-alike BEFORE masking, so it never
        # becomes a placeholder and cannot collide with a real placeholder index
        # (which would restore the sentinel into the real URI and leak a raw
        # \x00).
        text = f'\x00media:0\x00 and <img src="{_WEBP_URI}">'

        masked, originals = _mask_media_data_uris(text, _media_body_is_clean)
        # The only masked span is the REAL URI; the injected sentinel was stripped.
        assert originals == [_WEBP_URI]
        assert "\x00media:0\x00 " not in masked  # attacker sentinel gone
        assert _WEBP_URI not in masked  # real URI is masked

        restored, warnings = _unmask_media_data_uris(masked, originals)
        # Round-trip preserves the real URI; the raw sentinel is not leaked.
        assert _WEBP_URI in restored
        assert restored == f' and <img src="{_WEBP_URI}">'
        assert "\x00" not in restored
        assert warnings == []

    def test_unresolved_index_fails_closed_to_credential_tag(self) -> None:
        # A placeholder whose index is out of range for the originals list must
        # fail closed to the credential tag, never leak a raw \x00, and surface a
        # COUNT-ONLY warning (no secret bytes, no index value).
        text = "before \x00media:5\x00 after"
        restored, warnings = _unmask_media_data_uris(text, [])

        assert "[REDACTED: credential]" in restored
        assert "\x00" not in restored
        assert warnings  # a warning is surfaced
        # Count-only: the warning text carries neither the raw index nor bytes.
        assert all("5" not in w for w in warnings)
        assert all("\x00" not in w for w in warnings)

    def test_same_media_uri_twice_round_trips(self) -> None:
        # Duplicate-tolerant: the same URI appearing twice is masked to two
        # independent placeholders and both restore correctly.
        text = f'<img src="{_WEBP_URI}"> then again <img src="{_WEBP_URI}">'

        masked, originals = _mask_media_data_uris(text, _media_body_is_clean)
        assert originals == [_WEBP_URI, _WEBP_URI]
        assert _WEBP_URI not in masked

        restored, warnings = _unmask_media_data_uris(masked, originals)
        assert restored == text
        assert warnings == []

    def test_suspicious_url_redacted_while_media_uri_survives(self) -> None:
        # Through the facade, an exfiltration-looking URL carrying a real
        # credential is redacted while a valid media URI survives intact, and
        # warnings are surfaced.
        exfil = f"https://evil.example/?data={_JWS}"
        text = f'{exfil} next to <img src="{_WEBP_URI}">'

        result, warnings = redact_mcp_app_payload_text(text)

        # The media URI is preserved byte-identical.
        assert _WEBP_URI in result
        # The embedded credential does not survive.
        assert _JWS not in result
        # Warnings are surfaced by the facade.
        assert warnings
        # No raw sentinel leaks.
        assert "\x00" not in result


class TestWholeBodyScanIsTheBound:
    """The exemption is licensed by the exempted bytes being CLEAN, not by the
    render surface.

    An MCP app always has an exfil path regardless of its CSP: it relays
    ``tools/call`` to its own MCP server through the gateway (``McpAppFrame.tsx``
    → ``/api/mcp-apps/call`` → ``mcp_gateway/app_call.py``, which forwards
    iframe-controlled ``arguments``), and it receives the payload unredacted
    either way. So ``connect-src 'none'`` does not mean "cannot exfiltrate", and a
    signature check on the head is not enough either — a structurally valid
    container can carry a secret in a metadata chunk. These pin the whole-body
    scan that closes that.
    """

    def test_valid_png_carrying_a_secret_in_a_text_chunk_is_not_exempted(self) -> None:
        # Real PNG magic, so the head signature passes; a real JWS stored ASCII in
        # a tEXt chunk, so the body scan must refuse it. A signature check alone
        # cannot catch this, which is why the whole-body scan is the bound.
        body = base64.b64encode(
            b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x2atEXtComment\x00" + _JWS.encode()
        ).decode()
        uri = f"data:image/png;base64,{body}"

        masked, originals = _mask_media_data_uris(uri, _media_body_is_clean)
        assert originals == [], "a secret-bearing image body must not be exempted"
        assert masked == uri

        # And through the facade the secret does not survive.
        result, warnings = redact_mcp_app_payload_text(f'<img src="{uri}">')
        assert body not in result
        assert warnings

    def test_valid_png_with_a_clean_body_is_exempted(self) -> None:
        # Positive control for the test above: same container, no secret, so the
        # carve-out still does its job and the image survives byte-identical.
        body = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00\x01\x02\x03" * 40).decode()
        uri = f"data:image/png;base64,{body}"

        _, originals = _mask_media_data_uris(uri, _media_body_is_clean)
        assert originals == [uri]
        assert redact_mcp_app_payload_text(uri)[0] == uri

    def test_a_credential_shaped_base64_text_is_still_exempt_when_the_bytes_are_clean(
        self,
    ) -> None:
        # The whole point of masking survives the stricter gate: this body's
        # base64 TEXT carries a JWS shape (the bare pass redacts it, asserted in
        # TestBarePassesAreMediaUnaware) but its DECODED bytes hold no credential,
        # so it is still exempted. Scanning decoded bytes is a different question
        # from scanning the base64 text — that is why the fix is not a regression.
        _, originals = _mask_media_data_uris(_CRED_WEBP_URI, _media_body_is_clean)
        assert originals == [_CRED_WEBP_URI]

    def test_an_oversize_body_is_refused_rather_than_exempted_unscanned(self, monkeypatch) -> None:
        # Fail closed on size: skipping the scan to save the work would exempt
        # exactly the unbounded body an attacker controls. Patched small so the
        # test does not allocate the real 12M-char cap.
        monkeypatch.setattr(redaction, "_MEDIA_MAX_BODY_B64_CHARS", 16)
        body = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode()
        uri = f"data:image/png;base64,{body}"
        assert len(body) > 16, "control: body must exceed the patched cap"

        _, originals = _mask_media_data_uris(uri, _media_body_is_clean)
        assert originals == []

    def test_the_scan_predicate_is_required_not_defaulted(self) -> None:
        # A caller cannot obtain the carve-out without supplying the scan that
        # justifies it, so there is no way to reach the mask with the check off.
        with pytest.raises(TypeError):
            _mask_media_data_uris(_IMG)  # type: ignore[call-arg]


class TestCspGate:
    """The carve-out reaches a payload ONLY when its declared CSP grants the app
    iframe no outbound origin.

    This is the bound the whole exemption rests on. ``buildMcpAppCsp`` lets an
    app's own ``connectDomains`` replace ``connect-src 'none'``, and the MCP
    server authors both the app's JS and its CSP metadata — so "it renders in a
    sandboxed iframe" is NOT on its own a reason to stop redacting. An app that
    declares any outbound origin therefore keeps the strict, media-unaware pass
    and keeps the inline-image false positive with it.
    """

    def test_no_csp_declared_yields_no_egress(self) -> None:
        # Absent csp is the SEP-1865 default and yields the strictest policy.
        assert _csp_grants_egress(None) is False
        assert _csp_grants_egress({}) is False

    def test_empty_domain_lists_yield_no_egress(self) -> None:
        assert _csp_grants_egress({key: [] for key in _EGRESS_KEYS}) is False

    def test_each_declared_domain_list_is_egress(self) -> None:
        # Every one of the four lists puts an origin in the delivered policy:
        # connect/frame replace a 'none' directive, resource widens img/script/
        # style/font/media (an <img src="https://attacker/?s=…"> is egress too),
        # base-uri changes where a relative URL resolves.
        for key in _EGRESS_KEYS:
            assert _csp_grants_egress({key: ["https://attacker.example"]}) is True, key

    def test_an_unknown_csp_shape_fails_closed(self) -> None:
        # csp is untrusted server JSON of arbitrary shape. Anything that is not
        # None and not a dict cannot be shown to grant nothing, so it is egress.
        for shape in ("connect-src *", 7, ["https://attacker.example"], True):
            assert _csp_grants_egress(shape) is True, shape

    def test_an_app_declaring_connect_domains_gets_the_strict_passes(self) -> None:
        # The blocker case, end to end through the redactor the call site picks:
        # a payload from an app with a declared connect-src does NOT get the
        # carve-out, so a valid-headed webp body carrying a credential shape is
        # redacted rather than delivered intact to a page that can POST it.
        redactor = _payload_redactor({"connectDomains": ["https://attacker.example"]})
        out = _redact_leaves({"img": _CRED_IMG}, redactor)
        assert out["img"] != _CRED_IMG
        assert _CRED_WEBP_BODY not in out["img"]

    def test_an_app_declaring_nothing_gets_the_carve_out(self) -> None:
        # And the fix still works for the app the defect was reported against.
        redactor = _payload_redactor(None)
        out = _redact_leaves({"img": _CRED_IMG}, redactor)
        assert out["img"] == _CRED_IMG

    def test_the_gate_covers_every_domain_list_build_mcp_app_csp_reads(self) -> None:
        # Coupling pin: ``buildMcpAppCsp`` reads exactly four ``*Domains`` keys
        # off the csp metadata. Extract them from the frontend source and require
        # the backend gate to cover every one, so adding a fifth widenable
        # directive to the CSP builder without teaching this gate fails here
        # rather than silently handing the carve-out to an app that can egress.
        source = _MCP_APP_SRCDOC_TS.read_text(encoding="utf-8")
        declared = set(re.findall(r"csp\?\.([A-Za-z]+Domains)", source))
        assert declared, "no csp?.*Domains reads found — did the builder move?"
        assert declared == set(_EGRESS_KEYS), (
            "buildMcpAppCsp reads a domain list the backend carve-out gate does "
            f"not treat as egress: {sorted(declared - set(_EGRESS_KEYS))}"
        )


class TestScopePrimitiveCoupling:
    """Pin ``_MEDIA_URI_PREFIX_RE`` EQUAL in MIME scope to the batch
    ``_MEDIA_DATA_URI_RE`` by reading the shared shape out of each source, so a
    silent widening of one alone fails loudly.

    Same convention as
    ``test_the_two_base64_run_patterns_stay_structurally_coupled``: both patterns
    spell ``data:(?:image|font)/`` LITERALLY rather than sharing a built
    constant, so the MIME alternation is extracted from each ``.pattern`` and
    compared.
    """

    def test_the_two_media_patterns_share_one_mime_alternation(self) -> None:
        # Extract ``data:(?:image|font)/`` from BOTH patterns by source
        # inspection and assert they are equal, so widening the MIME scope of one
        # (adding ``text``/``application``, say) without the other fails here.
        alternation = re.compile(r"data:\(\?:([A-Za-z|]+)\)/")

        batch = alternation.search(_MEDIA_DATA_URI_RE.pattern)
        prefix = alternation.search(_MEDIA_URI_PREFIX_RE.pattern)
        assert batch, "batch _MEDIA_DATA_URI_RE MIME alternation not found"
        assert prefix, "streaming _MEDIA_URI_PREFIX_RE MIME alternation not found"

        assert batch.group(1) == prefix.group(1)
        # And it is exactly the media-only scope, not a widened one.
        assert set(batch.group(1).split("|")) == {"image", "font"}

        # Negative control: a widened alternation must not compare equal.
        widened = _MEDIA_URI_PREFIX_RE.pattern.replace("(?:image|font)", "(?:image|font|text)")
        assert widened != _MEDIA_URI_PREFIX_RE.pattern, "control failed to mutate"
        widened_match = alternation.search(widened)
        assert widened_match and widened_match.group(1) != batch.group(1)


class TestMediaHeadIsPlausible:
    """The plausibility gate accepts a real container head for the declared MIME,
    refuses a mislabelled secret, and refuses ``image/svg+xml`` (absent from the
    allowlist — text, script-capable).
    """

    def test_real_webp_head_passes(self) -> None:
        # RIFF at 0 AND WEBP at 8 — both checks must match for webp.
        head = b"RIFF" + b"\x12\x00\x00\x00" + b"WEBP"
        assert _media_head_is_plausible("image/webp", head) is True

    def test_real_png_head_passes(self) -> None:
        assert _media_head_is_plausible("image/png", b"\x89PNG\r\n\x1a\n") is True

    def test_mislabelled_aws_key_pair_matches_no_signature(self) -> None:
        # The decoded head of a base64'd AWS key pair matches no container
        # signature, so it is refused for ANY claimed media type — the
        # mislabelled-secret case that MUST be redacted rather than exempted.
        aws_secret = (
            b"AKIAIOSFODNN7EXAMPLE wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY "
            b"ghp_0123456789abcdefghijklmnopqrstuvwxyz"
        )
        head = aws_secret[:16]
        for mime in (
            "image/png",
            "image/webp",
            "image/jpeg",
            "image/gif",
            "font/woff2",
        ):
            assert _media_head_is_plausible(mime, head) is False, mime

    def test_svg_is_refused_even_with_xml_head(self) -> None:
        # image/svg+xml is deliberately ABSENT from the allowlist: it is text and
        # script-capable, not a binary container. It is refused even when the
        # head looks XML-ish.
        assert _media_head_is_plausible("image/svg+xml", b"<?xml version=") is False
        assert _media_head_is_plausible("image/svg+xml", b"<svg xmlns=") is False
