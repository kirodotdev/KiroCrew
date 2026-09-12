"""The custom whisper model escape hatch: ``stt.model = "custom"`` plus a URL and digest.

The whole question these answer is whether letting the CALLER supply the sha256
weakens the pin. It does not, and each test below pins one half of that claim: a
matching digest installs the file, a wrong one is refused and removed on the
download path AND on the load path, an incomplete pair degrades to a catalog model
instead of running something unverified, and the missing published size is replaced
by an absolute ceiling rather than dropped.
"""

from __future__ import annotations

import email.message
import hashlib
import http.client
import io
import json
import re
import threading
import unittest.mock
import urllib.request
from pathlib import Path

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.stt import models
from kiro_crew.url_redaction import redact_model_url

_PAYLOAD = b"custom weights" * 64
_DIGEST = hashlib.sha256(_PAYLOAD).hexdigest()
#: A globally routable literal. NOT a documentation address (192.0.2.0/24,
#: 198.51.100.0/24, 203.0.113.0/24): IANA marks those not-globally-reachable, so
#: ``is_global`` reports False and the screen would refuse the stand-in for a
#: legitimate host.
_PUBLIC_ADDRESS = "93.184.216.34"
#: The addresses a model host must never be allowed to resolve to. The last two are
#: the ones a naive screen misses: shared address space that ``is_private`` calls
#: public, and deprecated IPv6 site-local that ``is_global`` calls public.
_NON_PUBLIC_ADDRESSES = ("127.0.0.1", "10.0.0.7", "100.64.0.1", "fec0::1", "::1")
_URL = "https://models.example/ggml-my-model.bin"


def _stub_urlopen(payload: bytes):
    """A urlopen replacement yielding *payload* in one chunk."""

    class _Response:
        def __init__(self) -> None:
            self._data = payload

        def read(self, _n: int) -> bytes:
            data, self._data = self._data, b""
            return data

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def _open(_url, timeout=None, *, screen_addresses=False):
        assert timeout, "the model download must pass a socket timeout"
        return _Response()

    return _open


def _load_stt_config(tmp_path: Path, **stt) -> KiroCrewConfig:
    """Load a real config whose ``stt`` section is *stt*.

    Goes through ``KiroCrewConfig.load`` rather than constructing ``SttConfig``
    directly, because the degrade-to-default behaviour under test lives in the
    LOADER's validation, not in the dataclass.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    cfg_file = home / "config.json"
    cfg_file.write_text(json.dumps({"stt": stt}), encoding="utf-8")
    with (
        unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=home),
        unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
    ):
        return KiroCrewConfig.load()


def _with_config(monkeypatch, cfg: KiroCrewConfig) -> None:
    """Make ``models.resolve`` see *cfg*.

    ``_configured_custom_model`` imports the config lazily and calls ``load()``,
    so the substitute has to sit on the class the deferred import resolves to.
    """
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda _cls: cfg))


# ── resolve ──


def test_a_configured_custom_model_carries_its_url_and_its_digest(monkeypatch, tmp_path):
    cfg = _load_stt_config(
        tmp_path, model="custom", custom_model_url=_URL, custom_model_sha256=_DIGEST
    )
    assert cfg.stt.model == "custom"
    _with_config(monkeypatch, cfg)
    model = models.resolve("custom")
    assert model.name == models.CUSTOM_MODEL
    assert model.url == _URL
    assert model.sha256 == _DIGEST
    # No published size, which is what the download ceiling stands in for.
    assert model.size_bytes == 0
    # The digest is IN the name, so a path identifies the bytes that passed the pin
    # rather than merely "the custom slot". Still an ordinary ggml weight filename,
    # so it stays inside the shell fence `security._WHISPER_WEIGHT_NAME` puts
    # around these files.
    assert model.filename == f"ggml-custom-{_DIGEST}.bin"
    assert re.fullmatch(r"ggml-[A-Za-z0-9][A-Za-z0-9._-]*\.bin", model.filename)


@pytest.mark.parametrize(
    "url, digest",
    [
        ("", _DIGEST),  # digest with nothing to fetch
        (_URL, ""),  # a URL with no pin is an unverified download
        ("http://models.example/m.bin", _DIGEST),  # plaintext
        (_URL, "not-a-digest"),  # cannot match anything
        (_URL, _DIGEST[:-1]),  # one character short
        (_URL, _DIGEST[:-1] + "z"),  # right length, not hex
    ],
)
def test_an_unusable_custom_pair_degrades_to_the_default_model(
    monkeypatch, tmp_path, caplog, url, digest
):
    """A half-configured custom model must never fail the session that read it.

    Same contract as an unknown catalog name: ``config.json`` is hand-editable, so
    a typo has to leave voice input working on a model that exists.
    """
    cfg = _load_stt_config(
        tmp_path, model="custom", custom_model_url=url, custom_model_sha256=digest
    )
    # The loader already degraded the SELECTION, so nothing downstream sees `custom`.
    assert cfg.stt.model == models.DEFAULT_MODEL
    _with_config(monkeypatch, cfg)
    with caplog.at_level("WARNING"):
        # Asked directly, because a stored `custom` predating a cleared pair reaches
        # `resolve` without passing through the loader again.
        assert models.resolve("custom").name == models.DEFAULT_MODEL
    assert "custom" in caplog.text


def test_an_unreadable_config_degrades_rather_than_raising(monkeypatch, caplog):
    """`resolve` is on a live voice session's path and may not raise out of it."""

    def _explode(_cls):
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_explode))
    with caplog.at_level("WARNING"):
        assert models.resolve("custom").name == models.DEFAULT_MODEL
    assert "custom whisper model" in caplog.text


def test_custom_is_not_reachable_through_the_catalog_or_an_alias():
    """It names no artifact, so it must not appear where a real model is expected."""
    assert models.CUSTOM_MODEL not in {m.name for m in models.CATALOG}
    assert models.CUSTOM_MODEL not in models._ALIASES.values()


@pytest.mark.asyncio
async def test_resolving_the_custom_model_is_kept_off_the_event_loop(monkeypatch):
    """The config read this feature adds must not run on the gateway's loop.

    ``resolve(CUSTOM_MODEL)`` reads and validates ``config.json`` synchronously, and
    it is reached from five async paths -- the voice websocket's ``ensure_loaded``,
    the transport's ``pending_download``, the settings panel's ``ensure_model``, and
    the two polled endpoints ``GET /api/stt/status`` and ``POST /api/stt/prepare``
    (those two are guarded in ``TestSttResolveStaysOffTheLoop``). Called inline, that
    stat-and-parse ran on the single event loop and stalled every other session,
    which is what the repository's ``no-blocking-call-on-event-loop`` rule forbids.

    Asserted on the THREAD rather than on a timing, because a timing assertion for
    something this fast is a flake: a resolve that lands on the loop's own thread is
    the defect, whatever it cost this time.
    """
    from kiro_crew.stt import session as session_mod

    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []

    def _resolve(name):
        seen.append(threading.current_thread())
        return models._BY_NAME[models.DEFAULT_MODEL]

    class _Store:
        async def ensure(self, _model):
            return Path("already-there")

    monkeypatch.setattr(session_mod.models, "resolve", _resolve)
    monkeypatch.setattr(session_mod.models, "store", _Store)
    assert await session_mod.ensure_model(models.CUSTOM_MODEL) is True
    assert seen, "resolve was never called"
    assert all(thread is not loop_thread for thread in seen), seen


# ── the download path ──


def _custom(monkeypatch, tmp_path, digest: str = _DIGEST) -> models.WhisperModel:
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    model = models.custom_model(_URL, digest)
    assert model is not None
    return model


def test_a_matching_digest_installs_the_custom_model(monkeypatch, tmp_path):
    model = _custom(monkeypatch, tmp_path)
    monkeypatch.setattr(models, "_urlopen", _stub_urlopen(_PAYLOAD))
    path = models._download_blocking(model)
    assert path == tmp_path / model.filename
    assert path.read_bytes() == _PAYLOAD
    assert not list(tmp_path.glob("*.part")), "staging file must not survive"


def test_a_wrong_digest_is_refused_and_leaves_no_file(monkeypatch, tmp_path):
    """The caller supplies the pin; it is enforced exactly as a catalog pin is."""
    model = _custom(monkeypatch, tmp_path, hashlib.sha256(b"other weights").hexdigest())
    monkeypatch.setattr(models, "_urlopen", _stub_urlopen(_PAYLOAD))
    with pytest.raises(models.ModelDownloadError, match="sha256 mismatch"):
        models._download_blocking(model)
    assert not (tmp_path / model.filename).exists()
    assert not list(tmp_path.glob("*.part"))


def test_the_custom_url_is_fetched_verbatim(monkeypatch, tmp_path):
    """The catalog base URL must not be prepended to an address the user gave.

    ``MODEL_URL_ENV`` mirrors the catalog publisher's layout; a user-supplied
    artifact has no reason to sit under it, and composing one would fetch a path
    that does not exist while looking like a network failure.
    """
    model = _custom(monkeypatch, tmp_path)
    monkeypatch.setenv(models.MODEL_URL_ENV, "https://mirror.example/whisper")
    seen: list[str] = []

    def _open(url, timeout=None, *, screen_addresses=False):
        seen.append(url)
        return _stub_urlopen(_PAYLOAD)(url, timeout=timeout)

    monkeypatch.setattr(models, "_urlopen", _open)
    models._download_blocking(model)
    assert seen == [_URL]


def test_a_response_past_the_custom_ceiling_is_refused_mid_stream(monkeypatch, tmp_path):
    """A digest cannot bound a transfer, so an absolute ceiling replaces the pinned size.

    The digest is only known to be wrong once the last byte has arrived, which is
    too late: a hostile URL could stream until the disk filled. Asserted on what
    reached the disk, because an error raised after a 40 GB write is the same
    outage.
    """
    monkeypatch.setattr(models, "_CUSTOM_MAX_BYTES", 32)
    model = _custom(monkeypatch, tmp_path)
    monkeypatch.setattr(models, "_CHUNK_BYTES", 8)
    monkeypatch.setattr(models, "_urlopen", _stub_urlopen(b"w" * 4096))
    with pytest.raises(models.ModelDownloadError, match="ceiling"):
        models._download_blocking(model)
    assert not (tmp_path / model.filename).exists()
    for staged in tmp_path.glob("*.part"):
        raise AssertionError(f"staging file survived: {staged}")


def test_an_empty_response_is_refused(monkeypatch, tmp_path):
    """A sizeless model has no length to check, so the digest is what refuses it."""
    model = _custom(monkeypatch, tmp_path)
    monkeypatch.setattr(models, "_urlopen", _stub_urlopen(b""))
    with pytest.raises(models.ModelDownloadError, match="sha256 mismatch"):
        models._download_blocking(model)
    assert not (tmp_path / model.filename).exists()


# ── the transport: an https URL must stay https for every hop ──


def _resolves_to(monkeypatch, *addresses: str) -> None:
    """Make every host in this test resolve to *addresses*.

    ``_refuse_non_public_host`` does a LIVE ``getaddrinfo``, so without this the
    redirect tests would depend on ``models.example`` being resolvable and would fail
    CLOSED on a machine with no DNS -- asserting the fail-closed branch instead of the
    case they are about. Patching the resolver is also what makes the address screen
    itself testable: no test here may touch the network.
    """
    monkeypatch.setattr(
        models.socket,
        "getaddrinfo",
        lambda host, port, *a, **kw: [(0, 0, 0, "", (address, port)) for address in addresses],
    )


@pytest.fixture(autouse=True)
def _no_live_dns(monkeypatch):
    """No test in this module may touch DNS.

    ``_refuse_non_public_host`` resolves for real, so without this the download and
    redirect tests would depend on ``models.example`` existing and would fail CLOSED
    on a machine with no resolver -- asserting the fail-closed branch instead of the
    case they are about. A test that is about the screen overrides this by calling
    :func:`_resolves_to` with the addresses it needs.
    """
    _resolves_to(monkeypatch, _PUBLIC_ADDRESS)


def _redirect_to(location: str, *, screen: bool = True):
    """Stage urllib's 30x path with *location*, returning what it does next.

    Returns the arguments for :meth:`_HttpsOnlyRedirectHandler.http_error_302` plus
    the list a followed redirect appends to — empty means nothing was fetched, which
    is the whole assertion for the refusal case. ``http_error_301``, ``303``, ``307``
    and ``308`` are aliases of the same method in urllib, so one entry point covers
    the family.

    *screen* is the address-screen switch the handler now requires. It defaults to
    ON because that is the case most of these tests are about (a CUSTOM model URL);
    the catalog path passes ``screen=False`` and asserts the hop is followed.
    """
    handler = models._HttpsOnlyRedirectHandler(screen_addresses=screen)
    opened: list[str] = []

    class _Parent:
        def open(self, req, timeout=None):
            opened.append(req.full_url)
            return "the redirected response"

    handler.parent = _Parent()
    request = urllib.request.Request(_URL)
    request.timeout = None
    headers = email.message.Message()
    headers["Location"] = location
    return handler, request, io.BytesIO(b""), headers, opened


def test_a_redirect_off_https_is_refused_before_any_byte_is_fetched():
    """A 30x to ``http://`` must not be followed, however the first hop was spelled.

    This is the hole the initial-URL check cannot close: the address under test IS
    https, and the plaintext one is chosen by whoever answered it. The sha256 pin
    does not substitute for this — it decides whether the bytes were the pinned
    ones, not whether anyone on the path could read them, and it can only say so
    after the last byte has already crossed the network in the clear.

    Asserted on ``opened`` being empty rather than on the exception alone, because
    an error raised after the redirected fetch already happened is the same leak.
    """
    handler, request, fp, headers, opened = _redirect_to("http://models.example/ggml-my.bin")
    with pytest.raises(models.ModelDownloadError, match="non-https redirect"):
        handler.http_error_302(request, fp, 302, "Found", headers)
    assert opened == [], "the plaintext address must never be opened"


def test_a_same_host_hop_onto_another_port_is_screened_not_exempted(monkeypatch):
    """The mirror exemption is per ORIGIN, so another port is another service.

    The exemption exists for the ordinary internal-mirror topology -- an artifact
    store answering ``302`` to its own blob path -- and comparing ``hostname`` alone
    made ``mirror -> mirror:8443`` look like exactly that. It is not: a co-located
    internal service on another port of the operator's own mirror host is an ordinary
    deployment shape, and the port-blind exemption handed that hop both the eager
    screen's pass and the socket pinning's, which is the SSRF this screen exists to
    close.

    Asserted in both directions, because refusing the same-origin hop would retire the
    feature rather than secure it: the host resolves NON-publicly here, which is what
    a configured internal mirror does, so the same-origin hop must still be followed
    while the other-port hop must refuse.
    """
    _resolves_to(monkeypatch, "10.0.0.7")

    handler, request, fp, headers, opened = _redirect_to("https://models.example/blob/1")
    assert handler.http_error_302(request, fp, 302, "Found", headers) == "the redirected response"
    assert opened == ["https://models.example/blob/1"], opened

    handler, request, fp, headers, opened = _redirect_to("https://models.example:8443/blob/1")
    with pytest.raises(models.ModelDownloadError, match="non-public address"):
        handler.http_error_302(request, fp, 302, "Found", headers)
    assert opened == [], opened


def test_an_explicit_default_port_is_the_same_origin(monkeypatch):
    """``https://h/`` and ``https://h:443/`` are one origin, so the hop is exempt.

    The comparison uses the EFFECTIVE port. Comparing the literal ``parts.port``
    (``None`` against ``443``) would call these different and screen a hop that never
    left the operator's mirror -- refusing the internal-mirror install this feature
    documents, which is the failure the port fix must not introduce.
    """
    _resolves_to(monkeypatch, "10.0.0.7")
    handler, request, fp, headers, opened = _redirect_to("https://models.example:443/blob/1")
    assert handler.http_error_302(request, fp, 302, "Found", headers) == "the redirected response"
    assert opened == ["https://models.example:443/blob/1"], opened


def test_a_redirect_that_stays_on_https_is_still_followed():
    """Refusing every redirect would break the download instead of securing it.

    The publisher's own address answers 30x into a CDN, and ``MODEL_URL_ENV`` lets
    an operator point the catalog path at a mirror that does the same. The policy is
    "https only", not "no redirects".
    """
    handler, request, fp, headers, opened = _redirect_to("https://cdn.example/ggml-my.bin")
    assert handler.http_error_302(request, fp, 302, "Found", headers) == "the redirected response"
    assert opened == ["https://cdn.example/ggml-my.bin"]


def test_the_download_opener_carries_the_https_only_policy_and_drops_the_default(monkeypatch):
    """The handler has to be INSTALLED to be worth anything, so pin the wiring.

    Two claims, and the second is the one a reader would assume rather than check:
    ``build_opener`` REPLACES urllib's ``HTTPRedirectHandler`` when handed a
    subclass of it, so the permissive handler is gone rather than sitting alongside
    ours where handler order would decide which one ran.

    An INSTANCE is passed, not the class, because the handler requires its screening
    policy — ``build_opener`` instantiates a class with no arguments and so could not
    state one.
    """
    real = urllib.request.build_opener(models._HttpsOnlyRedirectHandler(screen_addresses=False))
    assert any(isinstance(h, models._HttpsOnlyRedirectHandler) for h in real.handlers)
    assert not [h for h in real.handlers if type(h) is urllib.request.HTTPRedirectHandler]

    seen: list[object] = []

    class _Opener:
        def open(self, url, timeout=None):
            return f"opened {url} with timeout {timeout}"

    def _build(*handlers):
        seen.extend(handlers)
        return _Opener()

    # Patched last: `models.urllib` IS the urllib module, so this replacement is
    # visible to every caller in the process, this test's own included.
    monkeypatch.setattr(models.urllib.request, "build_opener", _build)
    assert models._urlopen(_URL, timeout=7.0) == f"opened {_URL} with timeout 7.0"
    assert seen[0] is models._ScreenedHTTPSHandler
    assert isinstance(seen[1], models._HttpsOnlyRedirectHandler)
    assert len(seen) == 2, seen


def test_the_catalog_download_follows_a_cross_origin_hop_without_screening_it(monkeypatch):
    """The default model keeps the transport it had, or a proxied install breaks.

    huggingface answers 30x into a CDN, so the catalog hop is CROSS-ORIGIN. Screening
    it marks the hop for the address pin, and the pin refuses through an
    ``https_proxy`` tunnel because it cannot bind the far end of a tunnel it does not
    open -- denying the whole ``local`` provider on an install that worked before,
    with no override. The marker's absence is asserted too: it is what selects the
    pinned connection, so a hop followed while still carrying it fails at the socket.
    """
    _resolves_to(monkeypatch, "10.0.0.7")
    handler, request, fp, headers, opened = _redirect_to("https://cdn.example/b.bin", screen=False)
    hop = handler.redirect_request(request, fp, 302, "Found", headers, "https://cdn.example/b.bin")
    assert hop is not None and hop.full_url == "https://cdn.example/b.bin"
    assert not getattr(hop, "screen_address", False)
    assert handler.http_error_302(request, fp, 302, "Found", headers) == "the redirected response"
    assert opened == ["https://cdn.example/b.bin"]


def test_the_catalog_download_still_refuses_a_hop_off_https():
    """The scheme floor stays unconditional even where the address screen is off."""
    handler, request, fp, headers, opened = _redirect_to("http://cdn.example/x.bin", screen=False)
    with pytest.raises(models.ModelDownloadError, match="non-https redirect"):
        handler.http_error_302(request, fp, 302, "Found", headers)
    assert opened == []


# ── the load path ──


@pytest.mark.asyncio
async def test_a_present_custom_model_is_returned_when_its_digest_matches(monkeypatch, tmp_path):
    model = _custom(monkeypatch, tmp_path)
    (tmp_path / model.filename).write_bytes(_PAYLOAD)
    assert models.is_present(model)

    def _explode(_url, timeout=None):
        raise AssertionError("must not download a model already on disk")

    monkeypatch.setattr(models, "_urlopen", _explode)
    monkeypatch.delenv(models.SKIP_DOWNLOAD_ENV, raising=False)
    store = models.ModelStore()
    assert await store.ensure(model) == tmp_path / model.filename
    assert store.status["step"] == "ready"


@pytest.mark.asyncio
async def test_a_present_custom_model_is_deleted_when_its_digest_does_not_match(
    monkeypatch, tmp_path
):
    """Re-verified on every load, exactly like a catalog model.

    This is the case a caller-supplied pin has to cover to be worth anything: the
    models directory is agent-writable, and the loader re-opens the file by name
    after the check, so a same-name overwrite would otherwise transcribe every
    later utterance through weights nobody verified.
    """
    model = _custom(monkeypatch, tmp_path)
    path = tmp_path / model.filename
    path.write_bytes(b"weights nobody pinned")

    def _refuse(_url, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(models, "_urlopen", _refuse)
    monkeypatch.delenv(models.SKIP_DOWNLOAD_ENV, raising=False)
    store = models.ModelStore()
    # None because the re-download then failed -- the point is that the unverified
    # file was not returned, and is gone.
    assert await store.ensure(model) is None
    assert not path.exists()


# ── the digest is part of the path ──
#
# Correcting a custom pin must not leave the OLD weights in force. All three of the
# mechanisms below key off the on-disk path, so a single constant name such as
# `ggml-custom.bin` shared by every custom model would make a corrected URL or
# digest indistinguishable from the one it replaces.


_OTHER_PAYLOAD = b"different weights" * 64
_OTHER_DIGEST = hashlib.sha256(_OTHER_PAYLOAD).hexdigest()


def test_a_different_digest_is_a_different_path(monkeypatch, tmp_path):
    """The identity that everything downstream keys off.

    ``WhisperEngine.ensure_loaded`` builds its ``LoadedKey`` from
    ``models.model_path``, so two custom models sharing a path are one model as far
    as residency is concerned: correcting the pin kept serving the weights already
    loaded and never asked the store to verify the new ones.
    """
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    first = models.custom_model(_URL, _DIGEST)
    second = models.custom_model(_URL, _OTHER_DIGEST)
    assert first is not None and second is not None
    assert models.model_path(first) != models.model_path(second)
    # And the same pin is the same path, or a download would repeat on every load.
    assert models.model_path(first) == models.model_path(models.custom_model(_URL, _DIGEST))


def test_correcting_the_digest_reports_the_new_model_as_absent(monkeypatch, tmp_path):
    """`is_present` has no size to check a custom model against, so a shared path
    made any non-empty file answer for a model whose bytes nobody had fetched --
    reported to the panel as "already on this machine"."""
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    installed = models.custom_model(_URL, _DIGEST)
    corrected = models.custom_model(_URL, _OTHER_DIGEST)
    assert installed is not None and corrected is not None
    models.model_path(installed).write_bytes(_PAYLOAD)
    assert models.is_present(installed)
    assert not models.is_present(corrected)


@pytest.mark.asyncio
async def test_correcting_the_digest_forces_a_fresh_verified_download(monkeypatch, tmp_path):
    """The end-to-end shape of the fix: the corrected pin fetches and verifies its
    own bytes, and does so without the old file being consulted."""
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    installed = models.custom_model(_URL, _DIGEST)
    corrected = models.custom_model(_URL, _OTHER_DIGEST)
    assert installed is not None and corrected is not None
    models.model_path(installed).write_bytes(_PAYLOAD)
    monkeypatch.setattr(models, "_urlopen", _stub_urlopen(_OTHER_PAYLOAD))
    monkeypatch.delenv(models.SKIP_DOWNLOAD_ENV, raising=False)

    store = models.ModelStore()
    assert await store.ensure(corrected) == models.model_path(corrected)
    assert models.model_path(corrected).read_bytes() == _OTHER_PAYLOAD
    # The already-installed file is untouched, so switching back needs no network.
    assert models.model_path(installed).read_bytes() == _PAYLOAD


@pytest.mark.asyncio
@pytest.mark.parametrize("position", [0, 1, 31, 62, 63])
async def test_a_mistyped_digest_cannot_delete_the_working_weights(
    monkeypatch, tmp_path, position: int
):
    """The data-loss half, and the reason the WHOLE digest is in the name.

    ``_verified_on_disk`` deletes a file whose contents do not match the pin, which
    is right — a same-size substitution must not be trusted. With one path per
    custom slot it also meant that making a typo in the digest, or fixing one,
    pointed that deletion at weights that were fine. On a machine with no network
    that is unrecoverable, and the user's only symptom is that dictation stopped
    working.

    Parametrised across positions because a digest PREFIX in the filename passes
    this test only for a typo inside the prefix: a wrong character after it maps
    back onto the good file's path and deletes it, which is 48 of these 64
    positions. It is the whole reason the name carries all 64 characters.
    """
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    good = models.custom_model(_URL, _DIGEST)
    # Well-formed -- 64 hex characters -- so every shape check upstream passes it.
    wrong_char = "0" if _DIGEST[position] != "0" else "1"
    mistyped = _DIGEST[:position] + wrong_char + _DIGEST[position + 1 :]
    assert len(mistyped) == 64 and mistyped != _DIGEST
    typo = models.custom_model(_URL, mistyped)
    assert good is not None and typo is not None
    good_path = models.model_path(good)
    good_path.write_bytes(_PAYLOAD)

    def _refuse(_url, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(models, "_urlopen", _refuse)
    monkeypatch.delenv(models.SKIP_DOWNLOAD_ENV, raising=False)
    store = models.ModelStore()
    # The typo resolves nothing and cannot be fetched, which is the honest outcome.
    assert await store.ensure(typo) is None
    # What matters: the weights that DO match their pin are still there.
    assert good_path.read_bytes() == _PAYLOAD


# ── the validators the config surface and the dashboard share ──


@pytest.mark.parametrize(
    "raw, expected",
    [
        (_URL, _URL),
        (f"  {_URL}  ", _URL),
        ("http://models.example/m.bin", ""),
        ("https://", ""),
        ("", ""),
        (None, ""),
        (["https://models.example/m.bin"], ""),
        # The two components that exist only to carry a credential. A model
        # artifact is a static file, so refusing them costs nothing a fetch needs.
        ("https://models.example/m.bin?X-Amz-Signature=deadbeef", ""),
        ("https://models.example/m.bin?token=abc", ""),
        ("https://models.example/m.bin#sig=abc", ""),
        ("https://user:tok@models.example/m.bin", ""),
    ],
)
def test_valid_custom_url_accepts_only_a_real_https_address(raw, expected):
    assert models.valid_custom_url(raw) == expected


def test_a_presigned_url_cannot_be_persisted_into_agent_readable_config():
    """The stored value is whatever this validator returns, so the credential
    shape has to die here.

    ``config.json`` is agent-READABLE by design — ``security.paths`` fences writes
    to it, not reads — so a value this validator accepts is a value an untrusted
    agent can read. A pre-signed URL carries its whole bearer credential in the
    query (or the fragment), so accepting one would put a live credential in a file
    the agent may read. Both config writers (the settings PUT and the config loader)
    store exactly ``valid_custom_url(...)``, so refusing here covers both.
    """
    presigned = (
        f"{_URL}?X-Amz-Credential=AKIAEXAMPLE&X-Amz-Signature="
        "5f2a1c9e8b7d6f4a3c2e1b0d9f8a7c6b5e4d3f2a1c0b9e8d7f6a5c4b3e2d1f0a"
    )
    assert models.valid_custom_url(presigned) == ""
    # Not merely stripped down to the bare URL: a caller that took a trimmed value
    # would fetch a URL the signature is required for and store a dead link.
    assert models.valid_custom_url(presigned) != _URL
    # The pair therefore cannot be built from it either, which is what keeps the
    # selection degrading to the catalog default instead of half-configuring.
    assert models.custom_model(presigned, _DIGEST) is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        (_DIGEST, _DIGEST),
        (_DIGEST.upper(), _DIGEST),
        (f" {_DIGEST}\n", _DIGEST),
        (_DIGEST[:-1], ""),
        (_DIGEST + "a", ""),
        (_DIGEST[:-1] + "g", ""),
        ("", ""),
        (None, ""),
        (0, ""),
    ],
)
def test_valid_custom_sha256_accepts_only_64_hex_characters(raw, expected):
    assert models.valid_custom_sha256(raw) == expected


def test_custom_model_needs_both_halves():
    assert models.custom_model(_URL, "") is None
    assert models.custom_model("", _DIGEST) is None
    assert models.custom_model(_URL, _DIGEST) is not None


# ── a refused URL must not carry its credential into the message ──
#
# Every one of these is on a FAILURE path, which is the point: the value only
# reaches a message when it is rejected, so the diagnostic was the leak. Three kinds
# of credential ride in a URL — `userinfo`, the signature of a pre-signed URL (in
# the query or the fragment), and a tokenised PATH segment — and all were
# interpolated verbatim.

#: Carries a credential in every place at once, so one value pins the whole claim:
#: userinfo, a tokenised path segment, a signed query, and a fragment. Not https,
#: which is what makes it reach the refusals under test.
_SECRET_URL = "http://reader:s3cr3t@models.example:8443/tok-9f3b2c/ggml-my.bin?token=SIGNED#frag"

#: What every message about `_SECRET_URL` is allowed to say. The authority and
#: nothing else: the host is what tells an operator which mirror was refused, and
#: the path is dropped because a mirror can tokenise it.
_SECRET_URL_REDACTED = "http://models.example:8443"

#: The substrings that must appear in no message, log record or status field. The
#: path is in here: a `/tok-9f3b2c/` segment is a credential, and its filename is
#: recoverable from the pinned catalog anyway.
_SECRETS = ("s3cr3t", "reader:", "SIGNED", "token=", "frag", "tok-9f3b2c", "ggml-my.bin")


def _assert_redacted(text: str) -> None:
    """*text* names the refused URL without any credential it carried."""
    assert _SECRET_URL_REDACTED in text, text
    for secret in _SECRETS:
        assert secret not in text, f"{secret!r} leaked into: {text}"


def test_the_stt_refusals_use_the_one_shared_redactor():
    """One redactor, not a fourth copy of it.

    This reduction had been written four times over — the embedding download, two
    app engine downloads, and once more here — and the copies drifted on the
    question that matters: two kept the PATH, which is how a path-tokenised mirror
    got its token logged. Asserting IDENTITY rather than equal output is deliberate:
    equal output is what four copies had right up until one of them changed.
    """
    assert models.redact_model_url is redact_model_url
    from kiro_crew import embeddings

    assert embeddings.redact_model_url is redact_model_url


@pytest.mark.parametrize(
    "raw, expected",
    [
        # userinfo, path, query and fragment all dropped; scheme, host and port kept.
        (_SECRET_URL, _SECRET_URL_REDACTED),
        ("https://user:tok@models.example/m.bin", "https://models.example"),
        ("https://models.example/m.bin?sig=abc", "https://models.example"),
        ("https://models.example/m.bin#tok", "https://models.example"),
        ("https://models.example/artifactory/tok-9f3b2c/m.bin", "https://models.example"),
        # Nothing to strip beyond the path: the authority a clean URL names survives,
        # so the message still says which mirror was refused.
        (_URL, "https://models.example"),
        ("  " + _URL + "  ", "https://models.example"),
        ("https://models.example", "https://models.example"),
        # No authority to split off, so EVERY character is in `path` — including the
        # userinfo of a scheme-less value. None of it may be emitted.
        ("user:s3cr3t@models.example/m.bin", "<unparseable URL>"),
        ("not a url at all", "<unparseable URL>"),
        ("", "<unparseable URL>"),
        # `hostname`/`port` parse the authority lazily, so a malformed one raises
        # from inside the redactor. It must refuse, not propagate a ValueError onto
        # a path that is already handling a failure.
        ("https://models.example:notaport/m.bin", "<unparseable URL>"),
        ("https://[::1/m.bin", "<unparseable URL>"),
        # Not a string: describe the shape, never the content.
        (None, "<NoneType>"),
        (123, "<int>"),
        ([_SECRET_URL], "<list>"),
    ],
)
def test_redact_model_url_keeps_only_the_scheme_and_authority(raw, expected):
    assert redact_model_url(raw) == expected


def test_a_rejected_custom_model_url_is_redacted_in_the_config_warning(tmp_path, caplog):
    """`config.json` is where a signed URL is pasted, and rejection logs it.

    The warning named the value with `%r`, so a URL an operator had to paste to
    configure the model at all was written to the log the moment it was refused —
    and a refusal is the common case, since pasting an `http://` or pre-signed
    address is exactly the mistake this validator exists to catch.
    """
    with caplog.at_level("WARNING"):
        cfg = _load_stt_config(
            tmp_path, model="custom", custom_model_url=_SECRET_URL, custom_model_sha256=_DIGEST
        )
    # The value is still dropped and the selection still degrades — redaction
    # changed what is SAID about the rejection, not the rejection.
    assert cfg.stt.custom_model_url == ""
    assert cfg.stt.model == models.DEFAULT_MODEL
    assert "stt.custom_model_url" in caplog.text
    _assert_redacted(caplog.text)


def test_a_rejected_custom_model_digest_is_described_not_quoted(tmp_path, caplog):
    """The digest field is a second place a pasted credential lands.

    Its sibling above was redacted for the log ring `/api/logs` serves with no owner
    gate; quoting the digest left the pair asymmetric, and a value mis-pasted into
    the wrong field of a hand-edited `config.json` is an ordinary operator error.
    """
    pasted = "a-pasted-credential-not-a-digest"
    with caplog.at_level("WARNING"):
        cfg = _load_stt_config(
            tmp_path, model="custom", custom_model_url=_URL, custom_model_sha256=pasted
        )
    assert cfg.stt.custom_model_sha256 == ""
    assert "stt.custom_model_sha256" in caplog.text
    assert pasted not in caplog.text
    assert f"str of length {len(pasted)}" in caplog.text


def test_only_a_custom_models_url_turns_the_redirect_screen_on(monkeypatch, tmp_path):
    """The switch has to be WIRED to `model.url`, so read it at the seam.

    Both branches in one test: asserting only the custom one would still pass with
    the argument hardcoded to True, which is the state this scoping exists to leave.
    """
    seen: list[bool] = []

    def _record(url, timeout=None, *, screen_addresses=False):
        seen.append(screen_addresses)
        raise models.ModelDownloadError("stop here; the flag is what is under test")

    monkeypatch.setattr(models, "_urlopen", _record)
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)

    with pytest.raises(models.ModelDownloadError):
        models._download_blocking(models.resolve(models.DEFAULT_MODEL))
    with pytest.raises(models.ModelDownloadError):
        models._download_blocking(_custom(monkeypatch, tmp_path))

    assert seen == [False, True], seen


def test_a_refused_redirect_does_not_log_the_location_it_refused(caplog):
    """A ``Location`` is the far end's to spell, credential included.

    The first hop can be a clean https address the operator typed and the 30x can
    still point at `http://user:pass@…`, so this is the one credential in this file
    that the config surface never saw and could not have validated.
    """
    handler, request, fp, headers, opened = _redirect_to(_SECRET_URL)
    with caplog.at_level("WARNING"):
        with pytest.raises(models.ModelDownloadError) as raised:
            handler.http_error_302(request, fp, 302, "Found", headers)
    assert opened == [], "the plaintext address must never be opened"
    _assert_redacted(str(raised.value))


def test_a_non_https_model_url_is_redacted_in_the_download_refusal(monkeypatch, tmp_path):
    """The initial-URL refusal in `_download_blocking`, reached past the validator.

    Constructed directly rather than through `custom_model`, which would refuse the
    pair first: this branch guards the addresses that arrive another way —
    `MODEL_URL_ENV`, or a stored value predating the validator.
    """
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    model = models.WhisperModel(
        name=models.CUSTOM_MODEL, size_bytes=0, sha256=_DIGEST, url=_SECRET_URL
    )
    with pytest.raises(models.ModelDownloadError) as raised:
        models._download_blocking(model)
    _assert_redacted(str(raised.value))


@pytest.mark.asyncio
async def test_the_download_status_the_dashboard_reads_carries_no_credential(monkeypatch, tmp_path):
    """`ModelStore.status['error']` is `str(exc)`, and it is served over the API.

    Sanitising at the raise sites is what makes this hold, so it is asserted at the
    sink rather than trusted: a message built safely and then re-formatted with the
    raw URL somewhere downstream would pass every test above and still leak here.
    """
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    monkeypatch.delenv(models.SKIP_DOWNLOAD_ENV, raising=False)
    model = models.WhisperModel(
        name=models.CUSTOM_MODEL, size_bytes=0, sha256=_DIGEST, url=_SECRET_URL
    )
    store = models.ModelStore()
    assert await store.ensure(model) is None
    assert store.status["step"] == "failed"
    _assert_redacted(str(store.status["error"]))


# ── a malformed URL must be refused before it can be requested ──
#
# `startswith("https://")` was the whole validator, and it accepted values no HTTP
# request can carry. The refusal then came from `http.client`, whose `InvalidURL`
# QUOTES the URL it was handed — an exception string, so nothing in this module
# redacted it on its way to the log and to `/api/stt/status`.

#: An https URL an operator could paste, carrying a pre-signed query, that `urlopen`
#: cannot request. One entry per way the old prefix test was passed: a space, the
#: newline and tab `urlsplit` silently DELETES (so a check made after parsing would
#: not see them), a NUL, a DEL, a non-numeric port, a port of zero, and no host.
_MALFORMED_SIGNED_URLS = (
    "https://models.example/tok-9f3b2c/m bin?token=SIGNED",
    "https://models.example/tok-9f3b2c/m\nbin?token=SIGNED",
    "https://models.example/tok-9f3b2c/m\tbin?token=SIGNED",
    "https://models.example/tok-9f3b2c/m\x00bin?token=SIGNED",
    "https://models.example/tok-9f3b2c/m\x7fbin?token=SIGNED",
    "https://models.example\u00a0/tok-9f3b2c/m.bin?token=SIGNED",
    "https://models.example:notaport/tok-9f3b2c/m.bin?token=SIGNED",
    "https://models.example:0/tok-9f3b2c/m.bin?token=SIGNED",
    "https:///tok-9f3b2c/m.bin?token=SIGNED",
)


@pytest.mark.parametrize("raw", _MALFORMED_SIGNED_URLS)
def test_a_malformed_custom_url_is_refused_by_the_validator(raw):
    """Rejected exactly as a non-https value is: stored as `""`, no near-miss repair.

    This is what keeps the leak in the next two tests unreachable rather than merely
    redacted — a URL that never passes validation is never handed to `urlopen`, so
    `InvalidURL` cannot be raised to carry the query anywhere.
    """
    assert models.valid_custom_url(raw) == ""
    assert models.custom_model(raw, _DIGEST) is None


@pytest.mark.parametrize("raw", _MALFORMED_SIGNED_URLS)
def test_a_malformed_custom_url_leaves_no_credential_in_the_config_warning(raw, tmp_path, caplog):
    """The rejection is logged, and a rejection is when the signed value is present."""
    with caplog.at_level("WARNING"):
        cfg = _load_stt_config(
            tmp_path, model="custom", custom_model_url=raw, custom_model_sha256=_DIGEST
        )
    assert cfg.stt.custom_model_url == ""
    assert cfg.stt.model == models.DEFAULT_MODEL
    assert "stt.custom_model_url" in caplog.text
    for secret in ("SIGNED", "token=", "tok-9f3b2c"):
        assert secret not in caplog.text, f"{secret!r} leaked into: {caplog.text}"


@pytest.mark.asyncio
async def test_a_transport_exception_cannot_carry_the_url_to_the_status_field(
    monkeypatch, tmp_path, caplog
):
    """The belt to the validator's braces: `_urlopen` re-raises with the URL redacted.

    `http.client.InvalidURL` quotes the whole URL, and the validator above cannot be
    the only defence — a URL also arrives from `MODEL_URL_ENV` and from a value
    stored before that validator existed. Asserted at the sink the dashboard reads,
    with the real exception type the real library raises.
    """
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    monkeypatch.delenv(models.SKIP_DOWNLOAD_ENV, raising=False)

    class _QuotingOpener:
        def open(self, url, timeout=None):
            raise http.client.InvalidURL(f"URL can't contain control characters. {url!r}")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_a, **_k: _QuotingOpener())
    model = models.WhisperModel(
        name=models.CUSTOM_MODEL,
        size_bytes=0,
        sha256=_DIGEST,
        url="https://reader:s3cr3t@models.example:8443/tok-9f3b2c/ggml-my.bin?token=SIGNED",
    )
    store = models.ModelStore()
    with caplog.at_level("WARNING"):
        assert await store.ensure(model) is None
    assert store.status["step"] == "failed"
    for text in (str(store.status["error"]), caplog.text):
        assert "InvalidURL" in text, text
        assert _SECRET_URL_REDACTED.replace("http://", "https://") in text, text
        for secret in _SECRETS:
            assert secret not in text, f"{secret!r} leaked into: {text}"


@pytest.mark.asyncio
async def test_a_refused_redirect_keeps_its_own_reason_through_the_transport_seam(
    monkeypatch, tmp_path
):
    """`_urlopen`'s wrapper must not swallow the redirect refusal raised inside it.

    Both are `ModelDownloadError` from the same call, so a blanket re-wrap would
    replace "refusing a non-https redirect" with "download failed
    (ModelDownloadError)" and lose the only thing that says what went wrong.
    """
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    monkeypatch.delenv(models.SKIP_DOWNLOAD_ENV, raising=False)

    class _RefusingOpener:
        def open(self, url, timeout=None):
            raise models.ModelDownloadError(
                f"refusing a non-https redirect to: {redact_model_url(_SECRET_URL)}"
            )

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_a, **_k: _RefusingOpener())
    model = models.WhisperModel(name=models.CUSTOM_MODEL, size_bytes=0, sha256=_DIGEST, url=_URL)
    store = models.ModelStore()
    assert await store.ensure(model) is None
    error = str(store.status["error"])
    assert "refusing a non-https redirect to" in error, error
    _assert_redacted(error)


class TestCustomModelUrlIsSensitive:
    """The URL must be masked by the GENERIC config dump, not just the STT routes.

    Every other surface this PR adds is owner-gated, but `GET /api/config/kirocrew`
    is not: it masks by schema metadata alone, via `_is_sensitive_path`. A model URL
    can legitimately carry a credential -- `https://user:token@host/x` and a
    pre-signed `…?X-Amz-Signature=…` both pass `valid_custom_url` -- so without
    `sensitive=True` a non-owner dashboard session reads it verbatim out of that
    dump, defeating the owner gate and `redact_model_url` everywhere else.
    """

    def _schema(self) -> dict:
        from kiro_crew.config.schema import JSON_SCHEMA

        return JSON_SCHEMA

    def test_the_url_is_marked_sensitive_in_the_real_schema(self) -> None:
        from kiro_crew.config import validation

        assert validation._is_sensitive_path(self._schema(), "stt.custom_model_url") is True

    def test_a_credential_bearing_url_is_masked_not_returned(self) -> None:
        """The property that matters, asserted on the value rather than the flag."""
        from kiro_crew.config import validation

        credential_bearing_url = (
            "https://user:token@example.invalid/ggml-tiny.bin?X-Amz-Signature=deadbeef"
        )
        masked = validation._mask_value(credential_bearing_url, sensitive=True)
        assert "token" not in masked
        assert "X-Amz-Signature" not in masked
        assert "example.invalid" not in masked

    def test_the_digest_is_not_masked_since_it_is_not_a_credential(self) -> None:
        """A negative control: over-masking would hide a field operators must read."""
        from kiro_crew.config import validation

        assert validation._is_sensitive_path(self._schema(), "stt.custom_model_sha256") is False


def test_an_unresolvable_host_fails_closed(monkeypatch):
    """A host whose addresses were never read is not a host that passed the screen."""

    def _no_dns(*a, **kw):
        raise OSError("nxdomain")

    monkeypatch.setattr(models.socket, "getaddrinfo", _no_dns)
    with pytest.raises(models.ModelDownloadError, match="unresolvable host"):
        models._refuse_non_public_host(_URL)


def test_a_host_answering_with_a_public_and_a_private_address_is_refused(monkeypatch):
    """EVERY resolved address is screened, not the one a socket happens to pick.

    A host that answers with both is a rebind attempt whichever entry is tried first,
    so approving on "one of them is public" would approve exactly the attack.
    """
    _resolves_to(monkeypatch, _PUBLIC_ADDRESS, "127.0.0.1")
    with pytest.raises(models.ModelDownloadError, match="non-public address"):
        models._refuse_non_public_host(_URL)


def test_an_operator_configured_internal_mirror_is_opened(monkeypatch):
    """An air-gapped mirror is the documented use of ``MODEL_URL_ENV``, not an attack.

    ``stt-streaming.md`` states the variable "repoints the base URL for a mirrored or
    air-gapped install without weakening the pin". Such a mirror resolves to a private
    address BY DEFINITION, so screening the address the operator configured would
    refuse the feature on installs that already worked, with no override -- while
    stopping no attacker, because the operator chose that address.

    The pin is what protects this path: whatever the mirror serves still has to match
    the recorded sha256, so a hostile mirror cannot substitute weights.
    """
    _resolves_to(monkeypatch, "10.0.0.7")
    reached: list[str] = []

    class _Opener:
        def open(self, url, timeout=None):
            reached.append(url)
            return "opened"

    monkeypatch.setattr(models.urllib.request, "build_opener", lambda *h: _Opener())
    assert models._urlopen(_URL, timeout=7.0) == "opened"
    assert reached == [_URL], "the operator's own mirror must be reachable"


def test_a_redirect_to_an_internal_address_is_still_refused(monkeypatch):
    """The hop the FAR END chose is the one SSRF turns on, so it stays screened.

    This is the half that matters: a public host answering `302 Location:
    https://169.254.169.254/...` is asking this process to fetch an address it never
    chose. Removing the first-hop screen must not weaken this, so pin it directly on
    the handler rather than trusting the two to be tested together.
    """
    _resolves_to(monkeypatch, "10.0.0.7")
    handler = models._HttpsOnlyRedirectHandler(screen_addresses=True)
    with pytest.raises(models.ModelDownloadError, match="non-public address"):
        handler.redirect_request(
            urllib.request.Request(_URL),
            io.BytesIO(b""),
            302,
            "Found",
            email.message.Message(),
            "https://internal.example/weights.bin",
        )


# ── the rebind window: screen at the socket, connect to what was screened ──


def _pinned(monkeypatch, host: str = "models.example"):
    """A pinned connection with its socket and TLS layers recorded, not made.

    Returns the connection plus the list ``socket.create_connection`` was called with
    and the ``server_hostname`` values the TLS wrap saw. Nothing here touches the
    network: what the class under test decides is WHICH address the socket is opened
    to, and that is observable without opening one.
    """
    connection = models._PinnedAddressHTTPSConnection(host)
    connected: list[tuple] = []
    wrapped: list[str | None] = []

    class _Sock:
        """Only what ``HTTPConnection.connect`` actually calls on a fresh socket.

        ``setsockopt`` is here because the connection now reuses that ``connect``
        rather than restating it, and the real one sets ``TCP_NODELAY``. A fake
        without it is not a smaller socket, it is a socket that would have hidden
        the omission -- which is exactly what the earlier hand-rolled ``connect``
        did.
        """

        def setsockopt(self, *_args: object) -> None:
            return None

    def _create_connection(address, timeout=None, source_address=None):
        connected.append(address)
        return _Sock()

    class _Context:
        def wrap_socket(self, sock, server_hostname=None):
            wrapped.append(server_hostname)
            return sock

    monkeypatch.setattr(models.socket, "create_connection", _create_connection)
    connection._context = _Context()
    return connection, connected, wrapped


def test_a_rebinding_resolver_cannot_move_the_connection_off_the_screened_address(monkeypatch):
    """The screen and the connect must read the SAME lookup, or neither is worth much.

    A resolver that answers a public address to a check and a private one to the
    connect defeats any screen made somewhere other than the connection, because the
    second lookup is the one that decides where the packet goes. Modelled exactly that
    way -- this resolver answers public once and private afterwards -- and the
    assertion is on the address the socket was opened to, not on the absence of an
    exception: a refusal raised after the wrong connection was made is the same SSRF.
    """
    answers = iter((_PUBLIC_ADDRESS, "10.0.0.7", "10.0.0.7"))

    def _rebinding(host, port, *a, **kw):
        return [(0, 0, 0, "", (next(answers), port))]

    monkeypatch.setattr(models.socket, "getaddrinfo", _rebinding)
    connection, connected, _wrapped = _pinned(monkeypatch)
    connection.connect()
    assert connected == [(_PUBLIC_ADDRESS, 443)], "the socket must use the vetted address"


@pytest.mark.parametrize("address", _NON_PUBLIC_ADDRESSES)
def test_the_pinned_connection_refuses_a_non_public_answer_without_connecting(monkeypatch, address):
    """Every range the screen covers is refused at the socket too, and before it opens."""
    _resolves_to(monkeypatch, address)
    connection, connected, _wrapped = _pinned(monkeypatch)
    with pytest.raises(models.ModelDownloadError, match="non-public address"):
        connection.connect()
    assert connected == [], "nothing may be opened to an address that failed the screen"


def test_the_pinned_connection_keeps_the_hostname_for_tls(monkeypatch):
    """Pinning the ADDRESS must not turn into pinning the IDENTITY.

    Connecting to a literal while presenting it as the peer name would drop SNI and
    verify the certificate against an address, so a valid certificate for the host
    would stop matching and the fix would have broken every download it was meant to
    protect. ``server_hostname`` carries the name; urllib derives ``Host`` from the
    request, which this connection never rewrites.
    """
    _resolves_to(monkeypatch, _PUBLIC_ADDRESS)
    connection, connected, wrapped = _pinned(monkeypatch)
    connection.connect()
    assert connected == [(_PUBLIC_ADDRESS, 443)]
    assert wrapped == ["models.example"], "TLS must still verify the NAME"


def test_a_proxy_tunnel_cannot_launder_a_screened_redirect_past_the_address_pin(monkeypatch):
    """Through an https proxy the pin is handed the PROXY, so it must refuse, not pass.

    ``ProxyHandler`` is in ``build_opener``'s default set, so with ``https_proxy`` set
    the hook's ``address`` argument is the proxy's and the real destination is reached
    by ``CONNECT`` inside the tunnel -- resolved on the PROXY's DNS view. Screening
    what we were handed would approve the proxy, report success, and say nothing at
    all about the destination, so a split-horizon resolver reaches the private
    address the screen exists to refuse.

    The proxy here resolves PUBLIC, which is the whole point: a screen that reads the
    proxy's address passes, and the assertion is that the connection is refused
    anyway. Asserting on ``connected`` as well as on the exception, because a refusal
    raised after the socket was opened is the same bypass.
    """
    _resolves_to(monkeypatch, _PUBLIC_ADDRESS)
    connection, connected, _wrapped = _pinned(monkeypatch, host="proxy.example")
    # What `AbstractHTTPHandler.do_open` does for a proxied request, in the same order:
    # the connection is built for the proxy, then the real host is set as the tunnel.
    connection.set_tunnel("internal.example")
    with pytest.raises(models.ModelDownloadError, match="through an https proxy"):
        connection.connect()
    assert connected == [], "nothing may be opened when the destination cannot be screened"


def test_an_unproxied_screened_redirect_still_connects(monkeypatch):
    """The tunnel refusal must be about the TUNNEL, not about screened hops as such.

    Without a proxy there is no ``_tunnel_host``, the pin binds the destination
    itself, and the hop is followed. Paired with the test above so that "refuses a
    tunnel" cannot be satisfied by refusing every screened redirect, which would
    delete the feature instead of hardening it.
    """
    _resolves_to(monkeypatch, _PUBLIC_ADDRESS)
    connection, connected, wrapped = _pinned(monkeypatch, host="cdn.example")
    assert connection._tunnel_host is None
    connection.connect()
    assert connected == [(_PUBLIC_ADDRESS, 443)]
    assert wrapped == ["cdn.example"]


def test_a_same_host_redirect_on_an_internal_mirror_is_still_followed(monkeypatch):
    """An artifact store answering 302 to its own blob path is a topology, not an attack.

    A hop that stays on the origin the operator named adds no reachability they did
    not already grant, so it is not refused. What it does NOT get any more is the
    plain connection: the configured URL is screened now, so every hop of the chain
    is pinned at the socket and there is no origin in it that was trusted without a
    check. Only the redundant eager lookup is skipped, which is what this asserts by
    resolving to a private address and still expecting the hop to be followed.
    """
    _resolves_to(monkeypatch, "10.0.0.7")
    handler, request, fp, headers, opened = _redirect_to("https://models.example/blob/abc")
    assert handler.http_error_302(request, fp, 302, "Found", headers) == "the redirected response"
    assert opened == ["https://models.example/blob/abc"]


def _hop(handler, req, newurl):
    """One `redirect_request` step, returning the Request urllib would issue next."""
    headers = email.message.Message()
    headers["Location"] = newurl
    return handler.redirect_request(req, io.BytesIO(b""), 302, "Found", headers, newurl)


def test_the_screen_marker_survives_a_same_host_hop_taken_after_leaving(monkeypatch):
    """A chain cannot un-screen itself by ending on a same-host hop.

    The same-host exemption exists for the OPERATOR's origin, and what it grants is
    skipping the redundant EAGER lookup. Anchored to the current request instead, it
    re-appeared on every host the chain reached, so ``operator -> other ->
    other//second`` let the second hop skip the address check on the one host in the
    chain we have no reason to trust.

    Asserted by making the resolver answer a PRIVATE address only once the chain has
    left: a hop that still runs the lookup is refused, and a hop that wrongly claims
    the exemption is followed. The pin is not what this test distinguishes -- every
    hop of a screened chain carries it -- so the refusal is the signal.
    """
    _resolves_to(monkeypatch, _PUBLIC_ADDRESS)
    handler = models._HttpsOnlyRedirectHandler(screen_addresses=True)

    first = urllib.request.Request(_URL)
    first.timeout = None
    left = _hop(handler, first, "https://other.example/a")
    assert getattr(left, "screen_address", False) is True, "an off-host hop must be pinned"
    assert (
        getattr(left, "left_operator_origin", False) is True
    ), "crossing origins must be recorded on the hop"

    _resolves_to(monkeypatch, "10.0.0.7")
    with pytest.raises(models.ModelDownloadError, match="non-public address"):
        _hop(handler, left, "https://other.example/b")


def test_a_same_host_hop_from_the_operators_own_url_is_still_followed(monkeypatch):
    """The control: the mirror topology must stay reachable, now pinned rather than plain.

    If this starts REFUSING, the fix above has over-reached and an internal artifact
    store answering `302` to its own blob path is being rejected. The hop is pinned
    (the configured origin was screened, so pinning costs nothing) but no second
    eager lookup runs, which is why a private address still gets through here.
    """
    _resolves_to(monkeypatch, "10.0.0.7")
    handler = models._HttpsOnlyRedirectHandler(screen_addresses=True)

    first = urllib.request.Request(_URL)
    first.timeout = None
    same = _hop(handler, first, _URL.rsplit("/", 1)[0] + "/blob/abc")
    assert same is not None, "the operator's own mirror must still be followed"
    assert getattr(same, "screen_address", False) is True, "every hop of the chain is pinned"
    assert (
        getattr(same, "left_operator_origin", False) is False
    ), "a same-origin hop has not left the operator's host"


# ── the configured URL is screened too, because config.json is agent-reachable ──


def _opener_target(monkeypatch) -> list:
    """Capture what `_urlopen` hands to `opener.open`, without opening anything."""
    seen: list = []

    class _Opener:
        def open(self, target, timeout=None):
            seen.append(target)

            class _Resp:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *_exc):
                    return False

            return _Resp()

    monkeypatch.setattr(models.urllib.request, "build_opener", lambda *_h: _Opener())
    return seen


def test_a_custom_url_naming_a_private_address_is_refused_before_the_request(monkeypatch):
    """The blocking finding: a config-written private URL had the gateway fetch it.

    ``stt.custom_model_url`` lives in ``config.json``, which is agent-reachable and
    write-protected only at the file-edit tool gate, so an injected agent could point
    it at an internal service and the next transcription reached that service. The
    sha256 pin does not answer it -- the request is made before any digest is
    computed, so the effect on the internal service has already happened by the time
    the body is discarded.

    Asserted on nothing being opened, not on the exception alone: a refusal raised
    after the request went out leaves the same reachability.
    """
    seen = _opener_target(monkeypatch)
    _resolves_to(monkeypatch, "10.0.0.7")
    with pytest.raises(models.ModelDownloadError, match="non-public address"):
        models._urlopen(_URL, timeout=1.0, screen_addresses=True)
    assert seen == [], "the private address must never be requested"


def test_the_configured_custom_url_is_pinned_at_the_socket_too(monkeypatch):
    """The eager lookup alone would leave the DNS-rebind window open.

    The resolver that answers the check is not the one that decides where the packet
    goes, so the configured hop carries ``screen_address`` and reaches
    ``_PinnedAddressHTTPSConnection``, which resolves again at connect time and
    connects to the address it approved.
    """
    seen = _opener_target(monkeypatch)
    _resolves_to(monkeypatch, _PUBLIC_ADDRESS)
    with models._urlopen(_URL, timeout=1.0, screen_addresses=True):
        pass
    assert len(seen) == 1
    assert getattr(seen[0], "screen_address", False) is True, "the first hop must be pinned"
    assert seen[0].full_url == _URL


def test_a_catalog_download_still_reaches_a_private_mirror_unscreened(monkeypatch):
    """The control for `MODEL_URL_ENV`: the catalog path must not gain a screen.

    An internal mirror is that variable's documented purpose and resolves non-public
    by definition, so screening this path would refuse the feature on installs that
    worked before. It is also not the surface the finding is about -- the environment
    is set by whoever starts the gateway, not by a config write.

    Asserted on the URL being passed as a plain string: that is what leaves the hop
    unmarked, so `_ScreenedHTTPSHandler` uses the ordinary connection.
    """
    seen = _opener_target(monkeypatch)
    _resolves_to(monkeypatch, "10.0.0.7")
    with models._urlopen(_URL, timeout=1.0):
        pass
    assert seen == [_URL], "the catalog hop must stay an unmarked plain URL"


def test_an_operator_can_re_allow_a_private_custom_mirror_by_environment(monkeypatch):
    """The air-gapped case keeps the feature, through the one surface an agent lacks.

    A config value cannot authorise itself, which is the whole finding, so the
    override is an ENVIRONMENT variable: set by whoever starts the gateway, which is
    the operator saying "that private address is mine".
    """
    seen = _opener_target(monkeypatch)
    _resolves_to(monkeypatch, "10.0.0.7")
    monkeypatch.setenv(models.CUSTOM_MODEL_ALLOW_PRIVATE_ENV, "https://models.example")
    with models._urlopen(_URL, timeout=1.0, screen_addresses=True):
        pass
    assert seen == [_URL], "the operator's private mirror is reached unscreened"


def test_the_override_authorises_one_origin_and_not_private_addresses_at_large(monkeypatch):
    """The override names the operator's mirror; it is not a switch on the screen.

    Read as a boolean it authorised the operator's own address by turning the screen
    OFF, which authorised every other private address through the same
    agent-writable ``stt.custom_model_url``. So an air-gapped install was the one
    install where a rewritten config value could reach an arbitrary internal
    service.
    """
    _resolves_to(monkeypatch, "10.0.0.7")
    monkeypatch.setenv(models.CUSTOM_MODEL_ALLOW_PRIVATE_ENV, "https://models.example")
    with pytest.raises(models.ModelDownloadError, match="non-public address"):
        models._urlopen("https://internal.example/secret", timeout=1.0, screen_addresses=True)


def test_the_override_matches_an_origin_and_not_a_host_on_another_port(monkeypatch):
    """A co-located internal service on another port of the mirror host is not the mirror.

    Compared on origin rather than hostname, for the reason ``_origin`` states: the
    operator declared one address, and ``mirror:8443`` is a different one.
    """
    _resolves_to(monkeypatch, "10.0.0.7")
    monkeypatch.setenv(models.CUSTOM_MODEL_ALLOW_PRIVATE_ENV, "https://models.example")
    with pytest.raises(models.ModelDownloadError, match="non-public address"):
        models._urlopen(
            "https://models.example:8443/ggml-my-model.bin",
            timeout=1.0,
            screen_addresses=True,
        )


def test_the_private_custom_mirror_override_takes_only_a_parseable_https_origin(monkeypatch):
    """A value that is not an https origin disables nothing.

    ``"1"`` and ``"true"`` are the values a user would guess for a switch; a bare
    hostname is the origin written without its scheme, and an empty string is what a
    shell leaves behind. None of them can match a URL, so the screen stays on rather
    than depending on a truthy parse.
    """
    _resolves_to(monkeypatch, "10.0.0.7")
    for value in ("1", "true", "0", "", "yes", "models.example", "http://models.example"):
        monkeypatch.setenv(models.CUSTOM_MODEL_ALLOW_PRIVATE_ENV, value)
        with pytest.raises(models.ModelDownloadError, match="non-public address"):
            models._urlopen(_URL, timeout=1.0, screen_addresses=True)


def test_the_override_does_not_relax_a_redirect_the_far_end_chose(monkeypatch):
    """The override is a statement about the operator's OWN address, nothing further.

    A ``Location`` is spelled by whoever answered, so no operator has seen it and the
    override cannot speak for it. Were it read at this seam too, allowing a private
    mirror would also re-open the reachability through any mirror that answers 30x.
    """
    monkeypatch.setenv(models.CUSTOM_MODEL_ALLOW_PRIVATE_ENV, "https://models.example")
    _resolves_to(monkeypatch, "10.0.0.7")
    handler, request, fp, headers, opened = _redirect_to("https://internal.example/secret")
    with pytest.raises(models.ModelDownloadError, match="non-public address"):
        handler.http_error_302(request, fp, 302, "Found", headers)
    assert opened == [], "a redirect stays screened however the override is set"
