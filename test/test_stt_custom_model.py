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
import unittest.mock
import urllib.request
from pathlib import Path

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.stt import models
from kiro_crew.url_redaction import redact_model_url

_PAYLOAD = b"custom weights" * 64
_DIGEST = hashlib.sha256(_PAYLOAD).hexdigest()
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

    def _open(_url, timeout=None):
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

    def _open(url, timeout=None):
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


def _redirect_to(location: str):
    """Stage urllib's 30x path with *location*, returning what it does next.

    Returns the arguments for :meth:`_HttpsOnlyRedirectHandler.http_error_302` plus
    the list a followed redirect appends to — empty means nothing was fetched, which
    is the whole assertion for the refusal case. ``http_error_301``, ``303``, ``307``
    and ``308`` are aliases of the same method in urllib, so one entry point covers
    the family.
    """
    handler = models._HttpsOnlyRedirectHandler()
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
    """
    real = urllib.request.build_opener(models._HttpsOnlyRedirectHandler)
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
    assert seen == [models._HttpsOnlyRedirectHandler]


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
# Correcting a custom pin used to leave the OLD weights in force. All three of the
# mechanisms that failed keyed off the on-disk path, and the path was the constant
# `ggml-custom.bin` for every custom model that has ever existed, so a corrected
# URL or digest was indistinguishable from the one it replaced.


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
    # The previously-good file is untouched, so switching back needs no network.
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
    ],
)
def test_valid_custom_url_accepts_only_a_real_https_address(raw, expected):
    assert models.valid_custom_url(raw) == expected


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

        secret = "https://user:token@example.invalid/ggml-tiny.bin?X-Amz-Signature=deadbeef"
        masked = validation._mask_value(secret, sensitive=True)
        assert "token" not in masked
        assert "X-Amz-Signature" not in masked
        assert "example.invalid" not in masked

    def test_the_digest_is_not_masked_since_it_is_not_a_credential(self) -> None:
        """A negative control: over-masking would hide a field operators must read."""
        from kiro_crew.config import validation

        assert validation._is_sensitive_path(self._schema(), "stt.custom_model_sha256") is False
