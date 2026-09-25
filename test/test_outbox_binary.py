"""Integration tests for binary file support in outbox notify + download handlers."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import file_delivery_consent
from kiro_crew.dashboard.handlers import api_outbox_download, api_outbox_notify


def _synth_flagged_png() -> bytes:
    """PNG-shaped bytes that fail UTF-8 decode and carry key-SHAPED material.

    Assembled at runtime from fragments rather than checked in as a literal, the
    same convention ``test_file_delivery_consent`` documents: a diff carrying
    literal PEM headers reads as an exfiltration recipe to a review provider, and
    the scanner sees identical bytes either way. The body is deterministic base64
    over a digest, so it is private-key-SHAPED without being a private key.

    ``\\xff\\xfe`` is what makes the UTF-8 decode raise, which is the branch under
    test; ``.png`` puts the guessed MIME type inside the allow-list so the bytes
    reach the content scan rather than stopping at the type check.
    """
    rule = "-" * 5
    begin = " ".join(["BEGIN", "RSA", "PRIVATE", "KEY"])
    end = " ".join(["END", "RSA", "PRIVATE", "KEY"])
    body = "\n".join(
        base64.b64encode(hashlib.sha256(f"kc-8779-{i}".encode()).digest() * 2).decode()
        for i in range(4)
    )
    pem = f"{rule}{begin}{rule}\n{body}\n{rule}{end}{rule}\n"
    return b"\x89PNG\r\n\x1a\n\xff\xfe" + pem.encode() + b"\x80\x81"


def _clean_png() -> bytes:
    return b"\x89PNG\r\n\x1a\n\xff\xfe" + b"\x00" * 200


#: Wide encodings a credential can be written in inside an allow-listed
#: container: both widths, both byte orders. An ID3v2 tag in ``audio/mpeg`` is
#: UTF-16 and an ``application/pdf`` text string is commonly UTF-16BE, so these
#: are what standard writers emit rather than a crafted shape.
_WIDE_ENCODINGS = ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")


def _pem_text() -> str:
    """Key-SHAPED PEM text, assembled at runtime for the reason above.

    Same convention as :func:`_synth_flagged_png`: literal PEM headers in a diff
    read as an exfiltration recipe to a review provider, and the scanner sees
    identical bytes either way.
    """
    rule = "-" * 5
    begin = " ".join(["BEGIN", "RSA", "PRIVATE", "KEY"])
    end = " ".join(["END", "RSA", "PRIVATE", "KEY"])
    body = "\n".join(
        base64.b64encode(hashlib.sha256(f"kc-8779-wide-{i}".encode()).digest() * 2).decode()
        for i in range(4)
    )
    return f"{rule}{begin}{rule}\n{body}\n{rule}{end}{rule}\n"


def _synth_wide_utf8_pdf(encoding: str) -> bytes:
    """PDF-shaped bytes that DECODE AS UTF-8 and carry the key at wide spacing.

    The distinction from :func:`_synth_flagged_png` is the whole point: that one
    carries ``\\xff\\xfe`` so the UTF-8 decode raises and the bytes take each
    gate's binary branch. Every byte here is either ASCII or NUL, and NUL is
    itself valid UTF-8, so these bytes decode cleanly and take the TEXT branch --
    where the key's characters arrive NUL-separated and no contiguous-ASCII
    detector matches them.

    Explicit ``-le``/``-be`` spellings keep a byte-order mark off the front, so
    the run starts where this function says it does and the buffer stays
    UTF-8-decodable.
    """
    return b"%PDF-1.7\n" + _pem_text().encode(encoding) + b"\n%%EOF\n"


def _make_app(state=None) -> web.Application:
    app = web.Application()
    app["state"] = state or MagicMock(_slots={})
    app.router.add_post("/api/outbox/notify", api_outbox_notify)
    app.router.add_get("/api/outbox/{filename}", api_outbox_download)
    return app


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.files._sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


@pytest.fixture(autouse=True)
def isolated_consent_store(tmp_path, monkeypatch):
    """Point the consent store at a tmp file so the host's real grant cannot leak in.

    Without this a machine that happens to hold an ``owner_dashboard`` grant would
    turn every "refused without a grant" assertion below green for the wrong
    reason.
    """
    store = tmp_path / "file_delivery_consent.json"
    monkeypatch.setattr(
        file_delivery_consent, "file_delivery_consent_path", lambda: store, raising=True
    )
    return store


def _grant() -> None:
    file_delivery_consent.record_grant(
        file_delivery_consent.CLASS_OWNER_DASHBOARD, granted_at="2026-09-05T00:00:00+00:00"
    )


@pytest.fixture
def outbox(tmp_path):
    # Not ``tmp_path``: on macOS pytest's basetemp carries the per-user
    # ``/var/folders/<..30 random chars..>/T`` segment, which trips the bare-secret
    # heuristic in redact_credentials() and has api_outbox_notify reject the path
    # with 400 before any test logic runs.
    #
    # Not a literal ``/tmp`` either: the rootdir conftest already gives the run
    # its own ``tempfile`` base -- ``/tmp/kc-pytest-<user>-<pid>-<rand>`` on macOS,
    # ``$TMPDIR/kc-pytest-...`` elsewhere -- short, low-entropy, removed at session
    # end and RESIDUE-REPORTED, whereas a directory dropped straight into the
    # shared ``/tmp`` is owned by nobody and, on hosts that reap ``/tmp``
    # mid-session, can vanish under a running test. A bare ``mkdtemp()`` lands
    # in that base; the assertion pins it so a ``dir=`` creeping back in is a
    # red test rather than residue on someone's disk. Same fixture as
    # test_outbox_notify_broadcast.py.
    import shutil
    import tempfile

    base = Path(tempfile.mkdtemp())
    assert base.is_relative_to(tempfile.gettempdir()), base
    odir = base / "outbox"
    odir.mkdir()
    try:
        with patch("kiro_crew.config.loader.outbox_dir", return_value=odir):
            yield odir
    finally:
        shutil.rmtree(base, ignore_errors=True)


class TestOutboxNotifyBinary:
    @pytest.mark.asyncio
    async def test_binary_mp3_accepted(self, outbox, mock_sel):
        """Binary MP3 file passes notify validation (in allowlist)."""
        mp3 = outbox / "test.mp3"
        mp3.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 50)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(mp3),
                    "filename": "test.mp3",
                    "description": "test audio",
                    "size": mp3.stat().st_size,
                },
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_binary_exe_rejected(self, outbox, mock_sel):
        """Binary EXE file rejected (not in allowlist)."""
        exe = outbox / "payload.exe"
        exe.write_bytes(b"\x4d\x5a\x90\x00\x03\x00\xff\xfe\x80\x81" * 10)  # non-UTF-8 PE header
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(exe),
                    "filename": "payload.exe",
                    "description": "bad file",
                    "size": exe.stat().st_size,
                },
            )
            assert resp.status == 400
            data = await resp.json()
            assert "not allowed" in data["error"]

    @pytest.mark.asyncio
    async def test_text_with_secrets_rejected(self, outbox, mock_sel):
        """Text file with AWS key is rejected."""
        txt = outbox / "secrets.txt"
        txt.write_text("key=AKIAIOSFODNN7EXAMPLE")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(txt),
                    "filename": "secrets.txt",
                    "description": "oops",
                    "size": txt.stat().st_size,
                },
            )
            assert resp.status == 400
            data = await resp.json()
            assert "sensitive" in data["error"]


class TestOutboxDownloadBinary:
    @pytest.mark.asyncio
    async def test_download_mp3_inline(self, outbox, mock_sel):
        """MP3 served with audio/mpeg content-type and inline disposition."""
        mp3 = outbox / "standup.mp3"
        mp3.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/standup.mp3")
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "audio/mpeg"
            assert "inline" in resp.headers["Content-Disposition"]
            assert resp.headers["X-Content-Type-Options"] == "nosniff"

    @pytest.mark.asyncio
    async def test_download_exe_rejected(self, outbox, mock_sel):
        """EXE file rejected by download handler (not in allowlist).

        macOS ships /etc/apache2/mime.types which classifies .exe as
        application/x-msdownload; Linux CI lacks that file and falls
        through to application/octet-stream. Compute the expected mime
        from the same call the handler makes so the assertion is
        platform-portable.
        """
        exe = outbox / "bad.exe"
        exe.write_bytes(b"\x4d\x5a\x90\x00\x03\x00\xff\xfe\x80\x81" * 10)  # non-UTF-8
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/bad.exe")
            assert resp.status == 403
            data = await resp.json()
            assert "not allowed" in data["error"]
        expected_mime = mimetypes.guess_type("bad.exe")[0] or "application/octet-stream"
        mock_sel.log_tool_invocation.assert_called_with(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="download",
            outcome="denied",
            error=f"binary_mime_not_allowed: {expected_mime}",
        )

    @pytest.mark.asyncio
    async def test_download_text_served(self, outbox, mock_sel):
        """Clean text file served normally."""
        txt = outbox / "readme.txt"
        txt.write_text("hello world")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/readme.txt")
            assert resp.status == 200
            body = await resp.read()
            assert b"hello world" in body

    @pytest.mark.asyncio
    async def test_download_video_inline(self, outbox, mock_sel):
        """MP4 served with video/mp4 and inline disposition."""
        mp4 = outbox / "clip.mp4"
        mp4.write_bytes(b"\x00\x00\x00\x1cftyp\xff\xfe\x80\x81" + b"\x90" * 100)  # non-UTF-8
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/clip.mp4")
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "video/mp4"
            assert "inline" in resp.headers["Content-Disposition"]


class TestGeneratedBinaryActuallyTrips:
    """A fixture the scanner ignores would make every case below vacuous."""

    def test_the_flagged_png_is_detected(self):
        from kiro_crew.platform import binary_content_is_flagged

        assert binary_content_is_flagged(_synth_flagged_png())

    def test_the_clean_png_is_not_detected(self):
        from kiro_crew.platform import binary_content_is_flagged

        assert not binary_content_is_flagged(_clean_png())

    def test_both_fixtures_really_are_binary(self):
        for raw in (_synth_flagged_png(), _clean_png()):
            with pytest.raises(UnicodeDecodeError):
                raw.decode("utf-8")


class TestOwnerFacingGatesScanBinaryContent:
    """An allow-listed media type carrying a credential is not waved through.

    The type allow-list answers "can the browser render these bytes safely", which
    is a different question from "do these bytes contain a secret". Both
    owner-facing HTTP gates decide the second question with the same recorded
    grant their text branch already consults, so one file cannot be refused as
    text and accepted as a PNG.
    """

    @pytest.mark.asyncio
    async def test_notify_refuses_a_flagged_media_file_without_a_grant(self, outbox, mock_sel):
        png = outbox / "shot.png"
        png.write_bytes(_synth_flagged_png())
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(png),
                    "filename": "shot.png",
                    "description": "screenshot",
                    "size": png.stat().st_size,
                },
            )
            assert resp.status == 400
            assert "embedded credentials" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_notify_delivers_the_card_under_the_owners_grant(self, outbox, mock_sel):
        png = outbox / "shot.png"
        png.write_bytes(_synth_flagged_png())
        _grant()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(png),
                    "filename": "shot.png",
                    "description": "screenshot",
                    "size": png.stat().st_size,
                },
            )
            assert resp.status == 200
            assert (await resp.json())["ok"] is True

    @pytest.mark.asyncio
    async def test_download_refuses_flagged_media_without_a_grant(self, outbox, mock_sel):
        png = outbox / "shot.png"
        png.write_bytes(_synth_flagged_png())
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/shot.png")
            assert resp.status == 400
            assert "embedded credentials" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_download_refuses_flagged_media_for_a_non_owner_holding_a_grant(
        self, outbox, mock_sel
    ):
        """The grant releases bytes to the OWNER, not to every authenticated caller.

        Same second conjunct the text branch carries: this route needs
        authentication, which a Slack allow-listed non-owner running ``!dashboard``
        also has. The collaborator is patched rather than an identity forged onto
        the request, so what is pinned is the route's decision and not the harness.
        """
        png = outbox / "shot.png"
        png.write_bytes(_synth_flagged_png())
        _grant()
        with patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=False,
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/outbox/shot.png")
                assert resp.status == 400

    @pytest.mark.asyncio
    async def test_download_serves_flagged_media_to_the_owner_under_a_grant(self, outbox, mock_sel):
        png = outbox / "shot.png"
        raw = _synth_flagged_png()
        png.write_bytes(raw)
        _grant()
        with patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=True,
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/outbox/shot.png")
                assert resp.status == 200
                assert await resp.read() == raw

    @pytest.mark.asyncio
    async def test_a_clean_media_file_needs_no_grant_on_either_gate(self, outbox, mock_sel):
        """The scan must not turn ordinary media into a consent prompt."""
        png = outbox / "clean.png"
        png.write_bytes(_clean_png())
        async with TestClient(TestServer(_make_app())) as client:
            notify = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(png),
                    "filename": "clean.png",
                    "description": "plain",
                    "size": png.stat().st_size,
                },
            )
            assert notify.status == 200
            download = await client.get("/api/outbox/clean.png")
            assert download.status == 200

    @pytest.mark.asyncio
    async def test_a_refused_media_type_is_never_scanned(self, outbox, mock_sel):
        """Type first, content second: a type this route refuses stops at the type.

        The ordering is what keeps a 50 MB executable from being decoded and
        scanned only to be refused for its type anyway, and the audited reason
        stays the accurate one.
        """
        exe = outbox / "bad.exe"
        exe.write_bytes(_synth_flagged_png())
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/bad.exe")
            assert resp.status == 403
            assert "not allowed" in (await resp.json())["error"]
        expected_mime = mimetypes.guess_type("bad.exe")[0] or "application/octet-stream"
        mock_sel.log_tool_invocation.assert_called_with(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="download",
            outcome="denied",
            error=f"binary_mime_not_allowed: {expected_mime}",
        )


class TestWideEncodedCredentialInAUtf8DecodableFile:
    """A wide-encoded credential in bytes that DECODE must still be refused.

    NUL-interleaved ASCII is itself valid UTF-8, so these bytes take each gate's
    TEXT branch rather than its binary one, and ``redact`` matches contiguous
    ASCII so it matches none of the key. A gate whose wide pass sits behind a
    decode failure therefore never scans this file at all.

    A negative answer from the scan does not degrade to "owner only" on the
    download route -- it skips the owner conjunct entirely, so the bytes leave to
    any authenticated caller, and the completed-download record notes the
    handover without undoing it.

    The first test is the control that keeps the rest honest: it asserts the
    buffer really does decode as UTF-8 and that the text pass really does miss
    the key, so a gate doing only the narrow pass fails the others.
    """

    @pytest.mark.parametrize("encoding", _WIDE_ENCODINGS)
    def test_the_container_decodes_as_utf8_and_the_text_pass_misses_it(self, encoding):
        from kiro_crew import security

        raw = _synth_wide_utf8_pdf(encoding)
        text = raw.decode("utf-8")
        assert security.redact(text) == text

    @pytest.mark.parametrize("encoding", _WIDE_ENCODINGS)
    def test_the_shared_wide_pass_answers_on_it(self, encoding):
        from kiro_crew.platform import wide_content_is_flagged

        assert wide_content_is_flagged(_synth_wide_utf8_pdf(encoding))

    @pytest.mark.parametrize("encoding", _WIDE_ENCODINGS)
    def test_innocent_wide_text_in_a_utf8_container_is_not_flagged(self, encoding):
        """The pass must discriminate on content, not on wide text existing."""
        from kiro_crew.platform import wide_content_is_flagged

        innocent = "the quick brown fox jumps over the lazy dog\n" * 3
        raw = b"%PDF-1.7\n" + innocent.encode(encoding) + b"\n%%EOF\n"
        assert raw.decode("utf-8")
        assert not wide_content_is_flagged(raw)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("encoding", _WIDE_ENCODINGS)
    async def test_notify_refuses_it_without_a_grant(self, outbox, mock_sel, encoding):
        pdf = outbox / "report.pdf"
        pdf.write_bytes(_synth_wide_utf8_pdf(encoding))
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(pdf),
                    "filename": "report.pdf",
                    "description": "report",
                    "size": pdf.stat().st_size,
                },
            )
            assert resp.status == 400
            assert "sensitive" in (await resp.json())["error"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("encoding", _WIDE_ENCODINGS)
    async def test_download_refuses_it_without_a_grant(self, outbox, mock_sel, encoding):
        pdf = outbox / "report.pdf"
        pdf.write_bytes(_synth_wide_utf8_pdf(encoding))
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/report.pdf")
            assert resp.status == 400


#: The bytes a standard baseline JPEG writes on either side of its symbol table's
#: printable tail. Both are outside printable ASCII, which is what makes the tail
#: a region of its own in a real container rather than something the surrounding
#: segment runs into: ``\x16``-``\x1a`` closes the preceding ``HUFFVAL`` entries
#: and ``\x83`` opens the following ones.
_DHT_LEAD_OUT, _DHT_LEAD_IN = b"\x16\x17\x18\x19\x1a", b"\x83\x84\x85"


def _enumerated_table() -> bytes:
    """The standard container symbol table the delivery scan has to see past.

    Taken from the product constant rather than restated, so a test cannot pass
    against a table the scanner does not actually pin itself to.
    """
    from kiro_crew.security.redaction import _BASELINE_SYMBOL_TABLE

    return _BASELINE_SYMBOL_TABLE.encode("latin-1")


def _clean_jpeg() -> bytes:
    """JPEG-shaped bytes carrying the standard table and nothing sensitive.

    ``\\xff\\xd8\\xff\\xe0`` is the SOI/APP0 head a JPEG opens with and puts the
    guessed MIME type inside the allow-list; ``\\xff\\xc4`` opens the ``DHT``
    segment, and the table sits between the same non-printable ``HUFFVAL`` entries
    a real baseline image puts around it. The high bytes are what makes the UTF-8
    decode raise, so these bytes take each gate's binary branch.
    """
    return (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"
        b"\xff\xc4\x00\xb5\x00" + _DHT_LEAD_OUT + _enumerated_table() + _DHT_LEAD_IN + b"\xff\xd9"
    )


def _telegram_shaped_token(bot_id: str) -> str:
    """A bot-token-SHAPED value, assembled at runtime.

    Same convention as :func:`_synth_flagged_png`: the scanner sees identical
    bytes either way, and a diff carrying a literal token reads as key material
    to a review provider. The body is deterministic base64 over a digest, so it
    is token-SHAPED without being a token.
    """
    body = base64.urlsafe_b64encode(hashlib.sha256(bot_id.encode()).digest()).decode().rstrip("=")
    return f"{bot_id}:{body[:35]}"


def _text_regions(raw: bytes) -> list[str]:
    """The maximal runs of text bytes in *raw*, which is what masking decides on."""
    import re

    return [match.group() for match in re.finditer(r"[\t\n\r\x20-\x7e]+", raw.decode("latin-1"))]


#: The standard baseline luminance AC Huffman ``HUFFVAL`` list, ITU-T T.81 Annex K
#: Table K.5, as the flat sequence of byte values a JPEG writer emits, sixteen to
#: a row.
#:
#: Stated here independently of the product, which carries only the printable tail
#: and carries it as code-point RANGES. That is the point: this list is the anchor
#: that proves the product constant is what a real image actually writes, so a
#: constant edited on one side and not the other is caught rather than agreed with.
#: It has to be the WHOLE list, because the claim being checked is that the tail is
#: the LONGEST printable run in it, and the shorter runs live in these early rows.
_STANDARD_AC_HUFFVAL = bytes.fromhex(
    "01020300041105122131410613516107"
    "227114328191a1082342b1c11552d1f0"
    "2433627282090a161718191a25262728"
    "292a3435363738393a43444546474849"
    "4a535455565758595a63646566676869"
    "6a737475767778797a83848586878889"
    "8a92939495969798999aa2a3a4a5a6a7"
    "a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5"
    "c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2"
    "e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8"
    "f9fa"
)


class TestContainerSymbolTablesAreNotCredentials:
    """A container's own symbol table must not spend the owner's delivery grant.

    A standard baseline JPEG writes its Huffman symbol table as a fixed list of
    byte values, and the printable tail of that list reads as six digits, a colon
    and thirty-two letters -- the shape of an unlabelled bot token. The detectors'
    entropy floor scores the character multiset and the table's characters are all
    distinct, so it scores what a generated secret scores.

    The cost is not the refusal. The three owner-facing gates read one durable
    grant, so the owner's only way past a refused photo is to record a grant that
    is class-wide: once recorded it disarms the refusal for every content kind on
    all three gates, and a genuine key afterwards leaves with an audit entry and
    no refusal. Noise on this path spends the control, which is why a table has to
    be invisible to the scan rather than merely rare.

    Masking is pinned to that one fixed constant, and the tests here are written
    around what that buys: the first is the control that keeps the rest honest,
    and the credential cases are the ones a shape-based rule got wrong.
    """

    def test_the_table_really_does_satisfy_a_credential_shape(self):
        from kiro_crew import security

        table = _enumerated_table().decode("latin-1")
        assert security.redact(table) != table

    def test_the_pinned_constant_is_what_a_real_image_writes(self):
        """The product constant is the printable tail of the standard table.

        Masking is pinned to one constant, so everything else here rests on that
        constant being the bytes a real baseline image actually carries. The
        ``HUFFVAL`` list above states those bytes independently, as a flat list
        rather than as ranges, and this is where the two are made to agree.
        """
        from kiro_crew.security.redaction import _BASELINE_SYMBOL_TABLE

        printable_runs = _text_regions(_STANDARD_AC_HUFFVAL)
        assert _BASELINE_SYMBOL_TABLE == max(printable_runs, key=len)
        assert len(_BASELINE_SYMBOL_TABLE) == 45

    def test_a_real_segment_delimits_exactly_the_table(self):
        """In a real DHT segment the table's tail is a region all by itself.

        Masking only ever touches a WHOLE region, so this is the property that
        decides whether a real container can be delivered at all. Checked against
        the standard ``HUFFVAL`` list, whose neighbouring entries are outside
        printable ASCII, and then against the fixture the rest of these tests use.
        """
        from kiro_crew.security.redaction import _BASELINE_SYMBOL_TABLE

        assert _BASELINE_SYMBOL_TABLE in _text_regions(_STANDARD_AC_HUFFVAL)
        assert _BASELINE_SYMBOL_TABLE in _text_regions(_clean_jpeg())

    def test_a_container_carrying_only_its_table_is_not_flagged(self):
        from kiro_crew.platform import binary_content_is_flagged

        raw = _clean_jpeg()
        with pytest.raises(UnicodeDecodeError):
            raw.decode("utf-8")
        assert not binary_content_is_flagged(raw)

    @pytest.mark.parametrize(
        "secret",
        [
            _telegram_shaped_token("110201543"),
            "AKIA" + "IOSFODNN7EXAMPLE",
            "gh" + "p_" + hashlib.sha256(b"kc-gate").hexdigest()[:36],
        ],
        ids=["bot-token", "access-key-id", "forge-token"],
    )
    def test_a_credential_inside_the_same_container_is_still_flagged(self, secret):
        from kiro_crew.platform import binary_content_is_flagged

        assert binary_content_is_flagged(_clean_jpeg() + secret.encode())

    def test_a_key_inside_the_same_container_is_still_flagged(self):
        from kiro_crew.platform import binary_content_is_flagged

        assert binary_content_is_flagged(_clean_jpeg() + _pem_text().encode())

    def test_a_credential_shaped_region_is_not_masked(self):
        """A value that merely LOOKS like a table keeps its whole match.

        This is the case a structural rule cannot get right. The value below is
        delimited by non-text bytes and ascends in six runs of six or more
        consecutive code points, so every structural property a table has, it has
        -- and it is forty characters of credential shape. It is not a slice of the
        pinned constant, which is the only thing masking answers to, so it is not
        masked and the file is refused.
        """
        from kiro_crew.platform import binary_content_is_flagged
        from kiro_crew.security.redaction import _BASELINE_SYMBOL_TABLE

        shaped = "123456789:ABCDEFabcdefGHIJKLmnopqrSTUVWX"
        runs, run = [], 1
        for earlier, later in zip(shaped, shaped[1:]):
            if ord(later) == ord(earlier) + 1:
                run += 1
            else:
                runs.append(run)
                run = 1
        runs.append(run)
        assert min(runs) >= 6 and len(runs) >= 3
        assert shaped not in _BASELINE_SYMBOL_TABLE
        assert binary_content_is_flagged(b"\xff\xd8\xff\xe0\x80" + shaped.encode() + b"\x80")

    def test_masking_only_ever_removes_characters_of_the_constant(self):
        """The invariant the safety argument rests on, over the cases that matter.

        Whatever masking removes is a contiguous slice of one fixed public
        constant, so no byte an uploader chose can be taken out of the scanned
        copy. Checked by character multiset rather than by eye.
        """
        from collections import Counter

        from kiro_crew.security.redaction import (
            _BASELINE_SYMBOL_TABLE,
            mask_baseline_symbol_tables,
        )

        table = _BASELINE_SYMBOL_TABLE
        token = _telegram_shaped_token("110201543")
        for probe in (
            f"\x80{table}\x80",
            f"\x80{table}{token}\x80",
            f"\x80{token}{table}\x80",
            f"\x80{table}\x00{token}\x80",
            "\x80" + "123456789:ABCDEFabcdefGHIJKLmnopqrSTUVWX" + "\x80",
            f"\x80AKIAIOSFODNN7EXAMPLE{table}\x80",
        ):
            removed = Counter(probe) - Counter(mask_baseline_symbol_tables(probe))
            assert set(removed) <= set(table), (probe, removed)

    def test_tables_on_both_sides_of_a_credential_do_not_hide_it(self):
        """A table beside a credential shares its text region, so nothing is masked.

        Padding is the one case an attacker would reach for, and the credential
        between the two tables keeps its match.
        """
        from kiro_crew.platform import binary_content_is_flagged

        table = _enumerated_table()
        secret = _telegram_shaped_token("110201543").encode()
        assert binary_content_is_flagged(b"\xff\xd8\xff\xe0" + table + secret + table + b"\x80")

    def test_a_table_before_an_ascending_id_does_not_hide_the_credential(self):
        """An ascending account identifier beside a table keeps its match.

        ``ord(":") == ord("9") + 1``, so an ascending identifier and its colon
        continue the table's own last run and a structural rule reads the pair as
        one table. The region carrying both is longer than the pinned constant, so
        it is not a slice of it and neither part is masked.
        """
        from kiro_crew.platform import binary_content_is_flagged

        assert ord(":") == ord("9") + 1
        table = _enumerated_table()
        secret = _telegram_shaped_token("123456789").encode()
        assert binary_content_is_flagged(b"\xff\xd8\xff\xe0\x80" + table + secret + b"\x80")

    def test_a_non_text_byte_between_them_does_not_hide_it_either(self):
        """The other way to split them: the table gets its own region, the value keeps its."""
        from kiro_crew.platform import binary_content_is_flagged

        table = _enumerated_table()
        secret = _telegram_shaped_token("123456789").encode()
        assert binary_content_is_flagged(
            b"\xff\xd8\xff\xe0\x80" + table + b"\x00" + secret + b"\x80"
        )

    def test_one_long_ordered_fragment_keeps_its_whole_match(self):
        """An ordered span inside an identifier is not the table, so nothing is masked."""
        from kiro_crew.platform import binary_content_is_flagged

        secret = _telegram_shaped_token("123456789")
        assert secret.startswith("123456789:")
        assert binary_content_is_flagged(b"\xff\xd8\xff\xe0\x80\x81" + secret.encode())

    def test_an_ascending_span_that_is_not_the_table_is_left_alone(self):
        from kiro_crew.security.redaction import mask_baseline_symbol_tables

        span = "".join(chr(0x41 + step) for step in range(40))
        probe = f"\x00{span}\x00"
        assert mask_baseline_symbol_tables(probe) == probe

    def test_a_region_that_is_more_than_the_table_is_left_alone(self):
        """Only a whole region equal to a slice of the constant is masked."""
        from kiro_crew.security.redaction import mask_baseline_symbol_tables

        table = _enumerated_table().decode("latin-1")
        mixed = f"{table}the quick brown fox"
        assert mask_baseline_symbol_tables(mixed) == mixed

    def test_masking_leaves_every_byte_outside_the_table_alone(self):
        from kiro_crew.security.redaction import (
            _MASKED_TABLE_FILLER,
            mask_baseline_symbol_tables,
        )

        table = _enumerated_table().decode("latin-1")
        head, tail = "the quick brown fox\x00", "\x00jumps over the lazy dog"
        masked = mask_baseline_symbol_tables(head + table + tail)
        assert masked.startswith(head)
        assert masked.endswith(tail)
        assert masked[len(head) : len(masked) - len(tail)] == _MASKED_TABLE_FILLER * len(table)

    def test_nothing_is_masked_when_there_is_no_table(self):
        """The buffer comes back ITSELF, which is what lets the gate skip a re-scan."""
        from kiro_crew.security.redaction import mask_baseline_symbol_tables

        probe = "the quick brown fox jumps over the lazy dog, twice over and then again"
        assert mask_baseline_symbol_tables(probe) is probe

    def test_no_shorter_slice_of_the_table_is_flagged(self):
        """The region floor is measured against the catalogue, not chosen.

        Masking a slice shorter than the floor could not change an answer, because
        no such slice is flagged in the first place. If a detector ever starts
        matching a shorter one, this fails and the floor has to come down with it.
        """
        from kiro_crew import security
        from kiro_crew.security.redaction import (
            _BASELINE_SYMBOL_TABLE,
            _BASELINE_SYMBOL_TABLE_MIN,
        )

        table = _BASELINE_SYMBOL_TABLE
        for length in range(1, _BASELINE_SYMBOL_TABLE_MIN):
            for start in range(len(table) - length + 1):
                candidate = table[start : start + length]
                assert security.redact(candidate) == candidate, candidate
        at_floor = [
            table[start : start + _BASELINE_SYMBOL_TABLE_MIN]
            for start in range(len(table) - _BASELINE_SYMBOL_TABLE_MIN + 1)
        ]
        assert any(security.redact(slice_) != slice_ for slice_ in at_floor)

    def test_the_filler_is_admitted_by_every_boundary_crossing_class(self):
        """What stops blanking from destroying a match that crossed the region.

        A credential's match can anchor ACROSS a masked region, because the
        non-text bytes delimiting the region sit inside the value classes that
        carry no literal label. A filler those classes reject terminates the run
        and the match vanishes from the scanned copy, so the filler must be one
        they all accept.
        """
        import re

        from kiro_crew.security.redaction import _MASKED_TABLE_FILLER

        crossing = {
            "url password": r"[^\s/]",
            "key-value value": r"[^\s\"',}]",
            "url userinfo": r"[^\s:/@]",
            "pem body span": r"[\s\S]",
        }
        rejected = [
            name
            for name, pattern in crossing.items()
            if not re.fullmatch(pattern, _MASKED_TABLE_FILLER)
        ]
        assert rejected == [], rejected

    def test_the_filler_is_admitted_by_no_contiguous_token_class(self):
        """What stops blanking from building a match the raw bytes lacked.

        The same filler must not be able to LENGTHEN a contiguous token run, and
        it is what cancels the table's own bot-token shape for the same reason.
        """
        import re

        from kiro_crew.security.redaction import _MASKED_TABLE_FILLER

        contiguous = {
            "bare secret run": r"[A-Za-z0-9+/]",
            "token body": r"[A-Za-z0-9_-]",
            "numeric id": r"[0-9]",
            "base64 pem body": r"[A-Za-z0-9+/=]",
        }
        admitted = [
            name
            for name, pattern in contiguous.items()
            if re.fullmatch(pattern, _MASKED_TABLE_FILLER)
        ]
        assert admitted == [], admitted

    def test_a_credential_anchored_across_the_table_survives_masking(self):
        """A match spanning the masked region must still be there afterwards.

        The URL userinfo class admits the non-text bytes that delimit a region, so
        a password can begin before the table and reach its ``@`` after it. The
        table is still its own maximal region and is still masked, so the only
        thing standing between this and a delivered password is the filler not
        terminating the run.
        """
        from kiro_crew.platform import binary_content_is_flagged
        from kiro_crew.security import redact
        from kiro_crew.security.redaction import (
            _BASELINE_SYMBOL_TABLE,
            mask_baseline_symbol_tables,
        )

        password = "hunter2hunter2hunter2"
        raw = (
            b"\xff\xd8\xff\xe0"
            + f"https://x:{password}\x80".encode("latin-1")
            + _BASELINE_SYMBOL_TABLE.encode("latin-1")
            + b"\x80@db.example.com/"
        )
        text = raw.decode("latin-1")
        # The premise: the match really does cross the region, and the password is
        # too short for any contiguous-run detector to catch on its own.
        assert redact(text) != text
        assert len(password) < 40
        masked = mask_baseline_symbol_tables(text)
        assert masked is not text, "the table region must actually be masked"
        assert _BASELINE_SYMBOL_TABLE not in masked
        assert redact(masked) != masked
        assert binary_content_is_flagged(raw)

    @pytest.mark.parametrize("boundary", ["\x80", "\xff", "\x00", "\x81\x82"])
    def test_a_crossing_credential_survives_at_every_boundary_spelling(self, boundary):
        """Whatever non-text bytes delimit the region, the crossing match holds."""
        from kiro_crew.platform import binary_content_is_flagged
        from kiro_crew.security.redaction import _BASELINE_SYMBOL_TABLE

        payload = (
            f"https://user:{'p' * 30}{boundary}{_BASELINE_SYMBOL_TABLE}{boundary}"
            "@host.example.com/"
        )
        assert binary_content_is_flagged(b"\xff\xd8\xff\xe0" + payload.encode("latin-1"))

    def test_masking_never_turns_a_clean_buffer_into_a_flagged_one(self):
        from kiro_crew.security import redact
        from kiro_crew.security.redaction import (
            _BASELINE_SYMBOL_TABLE,
            mask_baseline_symbol_tables,
        )

        table = _BASELINE_SYMBOL_TABLE
        for probe in (
            f"http://u:\x80{table}\x80@host",
            f"\x80{table}\x80",
            f"a/b\x80{table}\x80c",
            "{" + f"\x80{table}\x80" + "}",
            f"user@\x80{table}\x80",
        ):
            if redact(probe) == probe:
                masked = mask_baseline_symbol_tables(probe)
                assert redact(masked) == masked, probe

    def test_a_table_dense_buffer_is_returned_unmasked_and_still_refused(self):
        """Masking is bounded, and bounded towards refusing.

        The region floor is short enough that a crafted upload can carry far more
        qualifying regions than any container does, and retaining a slice per
        region amplifies the buffer several times over in memory. Past the cap the
        buffer comes back untouched, so the gate answers exactly as it did before
        masking existed -- which is to refuse.
        """
        from kiro_crew.platform import binary_content_is_flagged
        from kiro_crew.security import redact
        from kiro_crew.security.redaction import (
            _BASELINE_SYMBOL_TABLE,
            _MASKED_REGION_CAP,
            mask_baseline_symbol_tables,
        )

        unit = "\x00" + _BASELINE_SYMBOL_TABLE + "\x00"
        dense = unit * (_MASKED_REGION_CAP + 1)
        assert redact(dense) != dense
        assert mask_baseline_symbol_tables(dense) is dense
        assert binary_content_is_flagged(b"\xff\xd8\xff\xe0\x80" + dense.encode("latin-1"))

    def test_under_the_cap_the_tables_are_still_masked(self):
        from kiro_crew.security.redaction import (
            _BASELINE_SYMBOL_TABLE,
            _MASKED_REGION_CAP,
            mask_baseline_symbol_tables,
        )

        unit = "\x00" + _BASELINE_SYMBOL_TABLE + "\x00"
        sparse = unit * (_MASKED_REGION_CAP - 1)
        masked = mask_baseline_symbol_tables(sparse)
        assert masked is not sparse
        assert _BASELINE_SYMBOL_TABLE not in masked

    def test_no_container_writes_its_table_where_the_wide_pass_can_see_it(self):
        """Why the wide leg asks no masked question: a table never reaches it.

        The wide projections match printable ASCII alternating with NUL, and a
        symbol table is contiguous bytes at single-byte spacing, so a container's
        table matches none of them and the leg lifts nothing at all out of it.
        """
        from kiro_crew.platform import wide_content_is_flagged
        from kiro_crew.platform.context import _WIDE_PROJECTIONS

        raw = _clean_jpeg()
        lifted = [
            match.group()[offset::stride]
            for pattern, offset, stride in _WIDE_PROJECTIONS
            for match in pattern.finditer(raw)
        ]
        assert lifted == []
        assert not wide_content_is_flagged(raw)

    @pytest.mark.parametrize("encoding", _WIDE_ENCODINGS)
    def test_the_wide_pass_still_answers_on_a_key_beside_a_table(self, encoding):
        from kiro_crew.platform import wide_content_is_flagged

        payload = _enumerated_table().decode("latin-1") + _pem_text()
        assert wide_content_is_flagged(b"%PDF-1.7\n" + payload.encode(encoding) + b"\n%%EOF\n")

    @pytest.mark.parametrize("encoding", _WIDE_ENCODINGS)
    def test_the_wide_pass_still_answers_on_an_ascending_id_beside_a_table(self, encoding):
        """The colon case at wide spacing, where a projection starts mid-block."""
        from kiro_crew.platform import wide_content_is_flagged

        payload = _enumerated_table().decode("latin-1") + _telegram_shaped_token("123456789")
        assert wide_content_is_flagged(b"%PDF-1.7\n" + payload.encode(encoding) + b"\n%%EOF\n")

    @pytest.mark.asyncio
    async def test_notify_delivers_a_container_carrying_only_its_table(self, outbox, mock_sel):
        """The owner is never sent to the grant panel for a clean container."""
        jpeg = outbox / "photo.jpg"
        jpeg.write_bytes(_clean_jpeg())
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(jpeg),
                    "filename": "photo.jpg",
                    "description": "photo",
                    "size": jpeg.stat().st_size,
                },
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_download_serves_a_container_carrying_only_its_table(self, outbox, mock_sel):
        jpeg = outbox / "photo.jpg"
        jpeg.write_bytes(_clean_jpeg())
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/photo.jpg")
            assert resp.status == 200
