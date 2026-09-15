"""The whisper.cpp model catalog, and a sha256-pinned downloader for it.

Speech recognition needs weights, and shipping them in the wheel is not an
option: the smallest useful one is 148 MB. So the first use of voice input
fetches one model, once, and every later session loads it from disk.

**The sha256 pin is the trust anchor, and not only for the network fetch.** A
model is written to its final path only after the digest computed while streaming
matches the pinned one, so a tampered mirror, a truncated transfer or a
captive-portal HTML body can only fail verification.

A file ALREADY on disk is verified too, on every load (see
``ModelStore._verified_on_disk``), because provenance is not enough: a same-size
file dropped over the weights would otherwise be trusted forever. Not cached
against the file's metadata either -- ``os.utime`` is available to anything that can
write the file, so a size-and-mtime memo is forgeable by the same actor it is meant
to catch.

The digest is the second line, not the first. Verifying and then handing a PATH to a
native loader leaves a window in which the bytes can be swapped, and re-hashing
cannot close it because the loader re-opens by name. What closes it is that
``<data home>/models`` is WRITE-PROTECTED from the agent
(``security._WRITE_PROTECTED_HOME_PATHS`` for the file tools; the agent's shell is
confined by the OS sandbox, not by a matcher over command text), so the verified
bytes are the loaded bytes. Adding a row to :data:`CATALOG` therefore needs no edit
in ``security``: the directory entry covers any file beneath it. Kiro Crew's own
downloader writes here directly and does not route through the tool gate, so a
first-run fetch and a re-download after a failed check both still work. That is a
deliberate divergence from ``embeddings.ModelDownloadManager``, which documents the
same size-only trade for its own GGUF -- that reasoning weighed a corrupted
download, not a writable directory and an agent with a shell.

This deliberately does not reuse that manager. It is bound to one pinned
artifact through ``default_model_path()``, ``_GGUF_SHA256`` and the
``memory.embed_model_path`` custom-model escape hatch, so sharing it would mean
generalising the memory subsystem's download path to carry a catalog. The shape,
the doctrine and the ``KIROCREW_SKIP_MODEL_DOWNLOAD`` escape hatch are mirrored
instead, which keeps the two subsystems independent.

Digests here were obtained by downloading each file and hashing it, not by
trusting an upstream header: HuggingFace's ``X-Linked-Etag`` for these objects is
not the content digest. Two of them (``base``, ``small``) independently match the
digests kiro-cli pins for the same models, which is a second source on the same
bytes.

A model outside this catalog is reachable through ``stt.model = "custom"`` plus
``stt.custom_model_url`` and ``stt.custom_model_sha256``, mirroring
``memory.embed_model_path``. It is NOT a weaker path: the caller supplies the
digest instead of this module pinning it, and everything downstream is identical
-- https only, digested while streaming, PUBLISHED at the path a loader reads only
after the digest matches, and re-hashed on every load. Unverified bytes are staged
inside the models directory in a ``.part`` file no loader looks at (see
:data:`_STAGING_SUFFIX`), and the final path appears in one atomic rename once the
digest has matched, so what a reader can open has always passed the pin.

Two things a custom model does differently, both forced by having no published
artifact behind it: it has no SIZE, so the pre-check that bounds a transfer falls
back to an absolute ceiling (:data:`_CUSTOM_MAX_BYTES`) rather than being dropped;
and its digest is part of its FILENAME (:attr:`WhisperModel.filename`), because a
constant name over changing bytes is the one thing that would break this module's
invariant that a path holds bytes which passed the pin.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import logging
import os
import re
import socket
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Callable

from kiro_crew.atomic_write import replace_with_retry
from kiro_crew.config.paths import config_dir
from kiro_crew.link_unfurl import address_is_not_public
from kiro_crew.url_redaction import redact_model_url

logger = logging.getLogger(__name__)

#: Upstream for the ggml conversions of OpenAI's Whisper weights. This is the
#: canonical publisher that whisper.cpp itself points at. Unlike the embedding
#: GGUF, Kiro Crew serves no mirror of these, so the pin below is doing the whole
#: job of establishing what we accept.
MODELS_BASE_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"

#: Operator override for the base URL, for an air-gapped or mirrored install. The
#: pin still applies, so a mirror can serve the bytes but cannot substitute them.
MODEL_URL_ENV = "KIROCREW_WHISPER_MODEL_BASE_URL"

#: Shared with the embedding downloader on purpose: one switch means "this
#: process must not pull model weights over the network", and a test run wants
#: that to hold for every subsystem, not one of them.
SKIP_DOWNLOAD_ENV = "KIROCREW_SKIP_MODEL_DOWNLOAD"

#: Lets ``stt.custom_model_url`` name a NON-PUBLIC address again -- the air-gapped
#: mirror case, which is the only way a custom model exists on such an install.
#:
#: An ENVIRONMENT variable and deliberately not a config key, because the whole
#: finding this screen answers is that ``config.json`` is agent-reachable: a value an
#: injected agent can write cannot also be the thing that authorises writing it. The
#: process environment is set by whoever starts the gateway, so this is the operator
#: saying "that private address is mine", which a configured URL cannot say for
#: itself. :data:`MODEL_URL_ENV` is the same escape hatch for the catalog path.
#:
#: Relaxes ONLY the configured first hop. A redirect is an address the far end chose,
#: so it stays screened however this is set -- there is no operator statement about a
#: `Location` nobody has seen yet.
CUSTOM_MODEL_ALLOW_PRIVATE_ENV = "KIROCREW_STT_CUSTOM_MODEL_ALLOW_PRIVATE"

#: Read size while streaming a download. Large enough that the digest update and
#: the progress callback are not per-packet work, small enough that a cancelled
#: download stops promptly.
_CHUNK_BYTES = 1 << 20

#: The ``stt.model`` value that selects a user-supplied model instead of a catalog
#: row. Not a member of :data:`CATALOG`: it names no fixed artifact, so it has no
#: size to advertise and no digest to state here — both come from configuration.
CUSTOM_MODEL = "custom"

#: Ceiling on a custom model's download, standing in for the pinned size a catalog
#: row carries. A digest cannot bound a transfer, because it is only known to be
#: wrong once the last byte has arrived; the size pre-check is what stops an
#: oversized response before it is stored. The largest weights whisper.cpp
#: publishes are ~3.1 GB, so this admits any real model while still bounding what
#: a hostile or misconfigured URL can write to the disk.
_CUSTOM_MAX_BYTES = 8 * (1 << 30)

#: Length of a hex-encoded sha256, which is the whole shape a digest has to have
#: before it is worth comparing anything against.
_SHA256_HEX_LEN = 64

#: Suffix for the staging file. Staged inside the TARGET directory so the final
#: step is a same-filesystem ``os.replace`` and therefore atomic; a staging area
#: under the system temp dir can land on a different device, where the rename
#: degrades into a copy that a reader can observe half-finished.
_STAGING_SUFFIX = ".part"

#: Per-socket-operation ceiling on the model download, NOT a ceiling on the whole
#: transfer: `urlopen`'s timeout bounds each individual read, so a 1.6 GB model on a
#: slow line still completes while a connection that stops delivering bytes fails
#: instead of hanging. Without it a stalled TCP connection held its worker for the
#: life of the process, and the only visible symptom was a download progress bar
#: frozen at some percentage -- indistinguishable from the slow-but-working case the
#: byte counter exists to prove apart.
_NETWORK_STALL_TIMEOUT_SECS = 60.0


@dataclass(frozen=True)
class WhisperModel:
    """One model the recogniser can load — a catalog row, or a configured custom one.

    ``size_bytes`` is carried for two reasons: the UI states the download cost
    before asking for it, and a file whose size does not match cannot be the
    pinned artifact, which is a free pre-check before any expensive work. **Zero
    means the size is unknown**, which only a custom model can be: nobody has
    published a size for a URL the user supplied. The sha256 is what establishes
    identity in that case, and it is checked on download and on every load either
    way.

    ``url`` empty means "compose the address from the catalog publisher and
    :attr:`filename`"; a non-empty value is the whole address, used verbatim.
    """

    name: str
    size_bytes: int
    sha256: str
    url: str = ""

    @property
    def filename(self) -> str:
        """The on-disk name. A custom model's carries its digest.

        This module's invariant is that a PATH holds bytes which passed that
        model's sha256. A catalog row upholds it for free: ``base`` names one fixed
        artifact forever, so ``ggml-base.bin`` is an identity. ``custom`` names
        whatever the operator's URL serves today, so the NAME is a constant while
        the bytes behind it are not — and a constant path broke the invariant three
        ways at once, all of them on the "operator corrects the pin" path:

        - ``WhisperEngine.ensure_loaded`` keys residency on this path, so the OLD
          weights stayed resident and the store was never asked to verify the new
          ones. The correction took effect only after a restart.
        - :func:`is_present` has no pinned size to check a custom model against, so
          any non-empty file at the shared path reported the NEW model as already
          downloaded.
        - ``ModelStore._verified_on_disk`` deletes a file whose digest does not
          match. One mistyped-but-well-formed digest therefore destroyed weights
          that were fine, which on a machine that is offline is unrecoverable.

        Putting the digest in the name fixes all three at the source rather than
        patching each: a different pin is a different path, so it is a different
        LoadedKey, it is correctly absent, and it cannot delete the file the
        previous pin still names.

        The WHOLE digest, not a prefix. A prefix is ample against collision, but
        collision is not the risk being managed here — a typo is, and a prefix
        leaves a typo in any character past it landing back on the good file's path
        and deleting it. 64 characters make the mapping one-to-one, so no
        single-character error can reach another pin's file.

        Stays inside ``security._WHISPER_WEIGHT_NAME``, the shell fence over these
        files, which admits any ``ggml-<alnum>[alnum._-]*.bin``.
        """
        if self.name == CUSTOM_MODEL:
            return f"ggml-{CUSTOM_MODEL}-{self.sha256}.bin"
        return f"ggml-{self.name}.bin"


#: The offered models, smallest first. Deliberately short. Every extra row is a
#: choice the user has to make before they can dictate a sentence, and the
#: accuracy ladder here already spans the useful range: ``tiny`` for a slow
#: machine, ``base`` for everyone, ``small`` when accents or jargon need it, and
#: ``large-v3-turbo`` for the accuracy ceiling. The English-only (``.en``)
#: variants are left out because they are a trap for a multilingual user and buy
#: little for an English one.
CATALOG: tuple[WhisperModel, ...] = (
    WhisperModel(
        "tiny",
        77_691_713,
        "be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21",
    ),
    WhisperModel(
        "base",
        147_951_465,
        "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe",
    ),
    WhisperModel(
        "small",
        487_601_967,
        "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b",
    ),
    WhisperModel(
        "large-v3-turbo",
        1_624_555_275,
        "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69",
    ),
)

#: The default balances download size and multilingual recognition. Inference
#: latency still depends on the native backend and competing host workloads;
#: model size alone does not establish real-time performance.
DEFAULT_MODEL = "base"

_BY_NAME: dict[str, WhisperModel] = {m.name: m for m in CATALOG}

#: Names accepted from superseded configuration, mapped to the entry that best
#: honours what the user actually asked for. Without this table every one of them
#: falls back to the default, which for someone who deliberately picked the
#: accuracy ceiling is a silent downgrade to the second-smallest model.
#:
#: Two mappings deserve their reasoning stated, because they look like
#: substitutions and are not:
#:
#: - The full-size ``large`` lineage resolves to ``large-v3-turbo``. Turbo is the
#:   same encoder with a distilled decoder, so it keeps the accuracy the user was
#:   asking for while decoding several times faster.
#: - ``medium`` also resolves to ``large-v3-turbo``, which is an upgrade rather
#:   than a compromise: turbo is both more accurate and faster than medium, so
#:   there is no reading of "medium" that turbo does not satisfy better.
#:
#: The English-only (``.en``) names drop to their multilingual sibling of the same
#: size. That loses a little English accuracy and gains every other language,
#: which is the right default for a config value nobody will revisit.
_ALIASES: dict[str, str] = {
    "turbo": "large-v3-turbo",
    "large": "large-v3-turbo",
    "large-v1": "large-v3-turbo",
    "large-v2": "large-v3-turbo",
    "large-v3": "large-v3-turbo",
    "large-v3-turbo-q5_0": "large-v3-turbo",
    "large-v3-turbo-q8_0": "large-v3-turbo",
    "medium": "large-v3-turbo",
    "medium.en": "large-v3-turbo",
    "small.en": "small",
    "base.en": "base",
    "tiny.en": "tiny",
}


#: Characters that make a URL unusable as an HTTP request target. ``http.client``
#: refuses exactly this set (its ``_contains_disallowed_url_pchar_re``) — and its
#: refusal is an ``InvalidURL`` whose message QUOTES the whole URL, which is how a
#: signed query reached a log and a status field. Refused here instead, before the
#: value is ever stored, so that exception cannot be raised at all.
_DISALLOWED_URL_CHARS = re.compile(r"[\x00-\x20\x7f]")


def valid_custom_url(value: object) -> str:
    """Return *value* if it is a usable custom model URL, else ``""``.

    https only, for the reason the catalog's own check states: the digest bounds
    what we accept, but plaintext would still leak which model an operator runs
    and let a network attacker waste the transfer. Empty is the normal "not
    configured" answer and is not an error.

    Validated structurally rather than by prefix. ``startswith("https://")`` alone
    accepted values no HTTP request can carry — an embedded space, a tab, a NUL, a
    port that is not a number — and the refusal then came from ``http.client``,
    whose ``InvalidURL`` message quotes the URL VERBATIM. That text is an exception
    string, not one of this module's redacted messages, so it travelled unredacted
    into the log and into ``ModelStore.status["error"]``, which Settings > Voice
    reads back over ``GET /api/stt/status`` — carrying a pre-signed signature with
    it. A URL rejected here never reaches :func:`_urlopen`, so that exception has no
    way to be raised.

    Surrounding whitespace is trimmed rather than refused, because a pasted value
    carries it, but the trimmed value must contain none. The order matters:
    ``urlsplit`` silently DELETES ASCII newlines and tabs, so the same check made
    after parsing would pass a string ``urlopen`` still refuses.
    """
    if not isinstance(value, str):
        return ""
    url = value.strip()
    # A lower-case literal, not ``urlsplit().scheme == "https"``: ``urlsplit``
    # lower-cases the scheme it reports, so accepting ``HTTPS://…`` here would store
    # a value that ``_download_blocking``'s own prefix check then refuses.
    if not url.startswith("https://"):
        return ""
    if _DISALLOWED_URL_CHARS.search(url) or any(ch.isspace() for ch in url):
        return ""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        # Read inside the guard: both attributes parse the authority lazily, so an
        # unclosed IPv6 literal or a non-numeric port raises HERE, not above.
        port = parts.port
    except ValueError:
        return ""
    # The prefix above already pinned the scheme, so what is left to require is an
    # authority: a host to connect to, and a port that is a usable number if given.
    if not host or port == 0:
        return ""
    # Userinfo is refused rather than carried. `urlsplit` reports `hostname` with the
    # `user:pass@` part stripped, so a screen reading `hostname` and a connection
    # reading the authority disagree about which host this is -- and urllib does not
    # turn userinfo into an `Authorization` header, so the request would fail anyway
    # after the credential had been written into a config value and a status field.
    # Refusing names the problem while the operator is still looking at the field.
    if parts.username is not None or parts.password is not None:
        return ""
    # A query or a fragment is refused for the same reason, one layer on. A model
    # artifact is a static file, so neither component carries anything the fetch
    # needs, while a pre-signed URL puts its entire bearer credential in exactly
    # those two places. The accepted value is persisted verbatim into
    # ``config.json``, which ``security.paths`` deliberately leaves agent-READABLE
    # (only writes are fenced), so whatever is stored here an untrusted agent can
    # read and exfiltrate. Refusing the two credential-only components keeps a
    # signature out of that file. A token hidden in a PATH segment is
    # indistinguishable from a filename and stays the operator's own residual.
    if parts.query or parts.fragment:
        return ""
    return url


def valid_custom_sha256(value: object) -> str:
    """Return *value* lower-cased if it is a hex sha256, else ``""``.

    Shape-checked here so a typo is caught while reading configuration rather
    than after a multi-hundred-megabyte transfer: a digest that is not 64 hex
    characters cannot match anything, so accepting it would guarantee the
    download is thrown away.
    """
    if not isinstance(value, str):
        return ""
    digest = value.strip().lower()
    if len(digest) != _SHA256_HEX_LEN:
        return ""
    return digest if all(c in "0123456789abcdef" for c in digest) else ""


def custom_model(url: object, sha256: object) -> WhisperModel | None:
    """A :class:`WhisperModel` for a user-supplied artifact, or ``None`` if unusable.

    ``None`` means the pair is absent or malformed, which callers turn into the
    same degrade-to-default the catalog uses for an unknown name. Both halves are
    required: a URL with no digest is an unpinned download, which this module does
    not do, and a digest with no URL names nothing to fetch.

    The size is deliberately ``0``. The digest is the trust anchor, so the only
    thing a size would add is the pre-check the ceiling in
    :func:`_download_blocking` provides instead.
    """
    verified_url = valid_custom_url(url)
    digest = valid_custom_sha256(sha256)
    if not verified_url or not digest:
        return None
    return WhisperModel(CUSTOM_MODEL, 0, digest, verified_url)


def _configured_custom_model() -> WhisperModel | None:
    """The custom model current configuration names, or ``None``.

    The import is deferred because ``config.sections`` imports THIS module at
    module scope to derive the accepted ``stt.model`` values, so a top-level
    import here would be a cycle.

    Reading configuration from inside :func:`resolve` cannot recurse:
    ``config``'s own validation returns :data:`CUSTOM_MODEL` unchanged instead of
    resolving it (see ``_validated_stt_model``), which is the one path that could
    have called back into here during a load.
    """
    try:
        from kiro_crew.config import KiroCrewConfig

        stt = KiroCrewConfig.load().stt
        return custom_model(stt.custom_model_url, stt.custom_model_sha256)
    except Exception:
        # Degrading, not swallowing: the caller logs the model it is falling back
        # to, and this names why the custom pair could not be read at all. Guarded
        # because this function is reached from a live voice session, whose
        # contract (see `resolve`) is to keep working on a default rather than
        # raise out of the websocket handler that read the setting.
        logger.warning("Could not read the configured custom whisper model", exc_info=True)
        return None


def resolve(name: str) -> WhisperModel:
    """Return the model *name* selects, falling back to the default.

    Falls back with a warning rather than raising: this value arrives from
    ``config.json``, and an unrecognised model must degrade to a working default
    the way an unusable backend does, not fail the voice session that read it.
    That covers :data:`CUSTOM_MODEL` with no usable URL/digest pair too — a
    half-configured custom model is an unusable selection, not an error to raise.
    """
    if name == CUSTOM_MODEL:
        model = _configured_custom_model()
        if model is not None:
            return model
        logger.warning(
            "stt.model is %r but stt.custom_model_url and stt.custom_model_sha256 are "
            "not both usable (https URL, 64 hex characters); using %r.",
            CUSTOM_MODEL,
            DEFAULT_MODEL,
        )
        return _BY_NAME[DEFAULT_MODEL]
    canonical = _ALIASES.get(name, name)
    model = _BY_NAME.get(canonical)
    if model is not None:
        return model
    logger.warning(
        "Unknown whisper model %r; using %r. Known models: %s",
        name,
        DEFAULT_MODEL,
        ", ".join(_BY_NAME),
    )
    return _BY_NAME[DEFAULT_MODEL]


def models_dir() -> Path:
    """Where whisper weights live. Respects ``KIROCREW_HOME``.

    A subdirectory of the shared ``models/`` tree rather than a sibling of it, so
    the embedding GGUF and these cannot collide on a filename and an operator
    clearing one does not take the other with it.
    """
    return config_dir() / "models" / "whisper"


def model_path(model: WhisperModel) -> Path:
    return models_dir() / model.filename


def is_present(model: WhisperModel) -> bool:
    """Whether *model* is on disk and the right size.

    The size check is what makes an interrupted download visible. A staging file
    is never at this path, so a wrong size here means a truncated or replaced
    file, and treating it as absent lets the next download overwrite it.

    A custom model has no pinned size, so presence is only "a non-empty file is
    here". That is not the weaker check it reads as: a custom model's path carries
    its digest (see :attr:`WhisperModel.filename`), so the file being AT this path
    is already the statement that it was fetched for this pin. A different pin
    asks about a different path and is correctly absent. What still guarantees the
    bytes is ``ModelStore._verified_on_disk``, which hashes the file on every load
    and deletes it when it does not match.
    """
    try:
        size = model_path(model).stat().st_size
    except OSError:
        return False
    if model.size_bytes:
        return size == model.size_bytes
    return size > 0


def _model_url(model: WhisperModel) -> str:
    if model.url:
        # A custom model's URL is the whole address, not a filename appended to a
        # base: MODEL_URL_ENV mirrors the catalog publisher's own layout, which a
        # user-supplied artifact has no reason to follow.
        return model.url
    base = os.environ.get(MODEL_URL_ENV, "").strip() or MODELS_BASE_URL
    return f"{base.rstrip('/')}/{model.filename}"


ProgressFn = Callable[[int, int], None]


class ModelDownloadError(RuntimeError):
    """A download did not produce a verified model file."""


def _public_addresses(host: str, port: int, label: str) -> list[str]:
    """Resolve *host* and return its addresses, or raise if ANY is not public.

    The screen is :func:`kiro_crew.link_unfurl.address_is_not_public`, reused rather
    than restated: it already folds the alternate IPv4 encodings the OS resolver
    accepts (``0x7f000001``, ``127.1``), the IPv4-mapped and 6to4 wrappers, and the
    two ranges a naive check misses -- ``100.64.0.0/10``, which only ``is_global``
    rejects, and ``fec0::/10``, which ``is_global`` wrongly approves.

    EVERY resolved address is checked, not just the one a socket would pick: a host
    answering with both a public and a private address is a rebind attempt whichever
    is tried first.

    Fails CLOSED. A host that does not resolve is one whose addresses were never
    checked, which is the same answer as a host that resolved to a private one.

    *label* is the already-redacted text to name in a refusal. A caller must never
    pass a raw URL: a ``Location`` is the far end's to spell, so it can put a
    credential in the userinfo, the path or the query and have this refusal write it
    to the log.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        # UnicodeError is listed because it is NOT an OSError: getaddrinfo raises it
        # for a host carrying a lone surrogate, which an OSError-only catch would let
        # escape this gate entirely.
        raise ModelDownloadError(f"refusing an unresolvable host in: {label}") from None
    addresses = [str(info[4][0]) for info in infos]
    if not addresses or any(address_is_not_public(address) for address in addresses):
        raise ModelDownloadError(f"refusing a non-public address for: {label}")
    return addresses


class _PinnedAddressHTTPSConnection(http.client.HTTPSConnection):
    """An https connection that screens its host and then connects to what it screened.

    Resolving in one place and connecting in another is a TOCTOU: a DNS server can
    answer a public address to the check and a private one to the connect, and the
    request lands on the internal service anyway. Nothing a pre-check can do closes
    that, because the second lookup is the one that decides where the packet goes --
    so the screen has to happen at the socket, and the socket has to be opened to the
    exact address the screen approved.

    Only the socket call is replaced, via the ``_create_connection`` hook
    :meth:`http.client.HTTPConnection.connect` already routes through. Reimplementing
    ``connect`` instead would have to restate the rest of it, and an earlier revision
    of this class did, silently dropping three things: the ``http.client.connect``
    audit event, the ``TCP_NODELAY`` setsockopt, and -- the one that is a defect
    rather than a regression -- ``server_hostname`` derived from ``_tunnel_host``
    when a proxy tunnel is in use, so through a tunnel it presented the wrong SNI.

    Pinning the address changes what we connect to WITHOUT changing who we claim to
    be talking to: the TLS handshake still carries the NAME (so a certificate for the
    hostname must verify), and urllib derives the ``Host`` header from the request
    rather than from the socket.

    A proxy TUNNEL cannot be pinned at all, and the hook refuses one rather than
    screening the proxy and calling that a screen -- see
    :meth:`_connect_to_a_screened_address`.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # `HTTPConnection.__init__` stores `_create_connection` as an INSTANCE
        # attribute, which shadows a subclass method of the same name -- so the hook
        # has to be installed here; declaring it as a method would never be called.
        self._create_connection = self._connect_to_a_screened_address  # type: ignore[method-assign]

    def _connect_to_a_screened_address(
        self,
        address: tuple[str, int],
        timeout: float | None = None,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        host, port = address
        # `_tunnel_host` is private to `http.client` and absent from typeshed, so the
        # read is ignored rather than routed through `getattr` with a default: if the
        # stdlib ever renames it, this must fail to build rather than quietly answer
        # None and re-open the bypass below.
        tunnel_host: str | None = self._tunnel_host  # type: ignore[attr-defined]
        if tunnel_host:
            # Through an https proxy this hook is handed the PROXY's address, and the
            # target is reached by `CONNECT` inside the tunnel -- resolved by the
            # proxy, on the proxy's DNS view. Screening what we are given would
            # approve the proxy and say nothing about the destination, so on a
            # split-horizon resolver the private address the screen exists to refuse
            # is reached anyway. Nothing at this socket can bind the far end of a
            # tunnel it does not open, so the honest answer is to refuse rather than
            # to run a check that cannot hold.
            #
            # A CATALOG download never reaches this class, so a proxied install still
            # fetches the default model: that path uses the plain connection, tunnel
            # and all. What is refused is a CUSTOM download through a proxy -- every
            # hop of one is pinned, the configured first hop included, and a tunnel's
            # far end is resolved by the proxy, so there is no address here to bind.
            # A named refusal is the honest answer; running a check that cannot hold
            # and calling the result screened is not.
            raise ModelDownloadError(
                "refusing a screened redirect through an https proxy: the address "
                f"of {redact_model_url(f'https://{tunnel_host}')} is resolved "
                "by the proxy, so it cannot be screened at the socket"
            )
        approved = _public_addresses(host, port, redact_model_url(f"https://{host}"))
        return socket.create_connection((approved[0], port), timeout, source_address)


class _ScreenedHTTPSHandler(urllib.request.HTTPSHandler):
    """Uses the pinned-address connection for a request that is marked as screened.

    Marked on every hop of a CUSTOM model download, the configured first one
    included -- see :func:`_urlopen`. A catalog download marks nothing, so screening
    does not reach it: an internal mirror is the documented purpose of
    :data:`MODEL_URL_ENV` and resolves non-public by definition.

    The context is kept on this object rather than read back out of the base class:
    ``HTTPSHandler`` stores it privately, and a subclass reaching into that is one
    stdlib refactor away from an ``AttributeError`` at the moment a download fails.
    """

    def __init__(self, context: ssl.SSLContext | None = None) -> None:
        super().__init__(context=context)
        self._screen_context = context

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        connection = (
            _PinnedAddressHTTPSConnection
            if getattr(req, "screen_address", False)
            else http.client.HTTPSConnection
        )
        return self.do_open(connection, req, context=self._screen_context)


def _private_custom_url_is_allowed(url: str) -> bool:
    """True when *url* is the exact private origin the operator declared.

    The value is an ORIGIN, not a boolean: ``https://mirror.internal:8443``. A
    boolean read (the earlier ``"1"``) authorised the operator's mirror by turning
    the screen OFF, which authorised every other private address at the same time --
    and the address being screened arrives from ``stt.custom_model_url`` in
    ``config.json``, which an injected agent can rewrite. So an operator who mirrors
    weights internally could be made to fetch an arbitrary internal service instead,
    which is the reachability this screen exists to close. Naming the origin keeps
    the air-gapped install working and leaves every other private address refused.

    Compared on :func:`_origin`, so ``https://h`` and ``https://h:443`` are the same
    declaration and ``mirror -> mirror:8443`` is not. Anything that is not a parseable
    https origin -- ``"1"``, ``"true"``, a bare hostname, an empty string left behind
    by a shell -- matches no URL and therefore disables nothing.
    """
    declared = (os.environ.get(CUSTOM_MODEL_ALLOW_PRIVATE_ENV) or "").strip()
    if not declared:
        return False
    scheme, host, _port = _origin(declared)
    if scheme != "https" or not host:
        return False
    return _origin(url) == _origin(declared)


def _refuse_non_public_host(url: str) -> None:
    """Raise :class:`ModelDownloadError` unless *url*'s host is on the public internet.

    The https-only redirect policy below is a check about the TRANSPORT, not about
    the destination, so it admitted an internal address as readily as a public one:
    a mirror that answers ``302 https://<internal service>/`` had the gateway issue
    an unauthorized request there. That is a blind SSRF -- the body is sha256-pinned
    and discarded, and the URL is redacted out of every error -- but the request is
    still made, and the reachable set is whatever this host can route to.

    Applied to a CUSTOM model download: the configured URL itself, and every redirect
    hop that leaves the host it named. A ``stt.custom_model_url`` is a value in
    ``config.json``, which is agent-reachable and write-protected only at the
    file-edit tool gate, so it is NOT an operator-only address the way
    :data:`MODEL_URL_ENV` is -- an injected agent that writes a private https URL
    there had the gateway issue an unauthorized internal GET on the next
    transcription, before the digest (any 64 hex characters) was ever consulted.

    Deliberately NOT applied to the catalog path, whose address comes from
    :data:`MODEL_URL_ENV`. Pointing that at an internal mirror is its documented
    purpose ("a mirrored or air-gapped install"), such a mirror is non-public by
    definition, and the environment is not writable by the same seam as the config
    file. What protects that path instead is the sha256 pin, which a hostile mirror
    cannot satisfy. :data:`CUSTOM_MODEL_ALLOW_PRIVATE_ENV` is the escape hatch for a
    custom URL, so an air-gapped install keeps the feature -- naming the one private
    origin it is allowed to reach, rather than switching the screen off.

    This is the EAGER half of the screen, kept because it produces a clear refusal
    before a socket is opened. The half that makes the guarantee is
    :class:`_PinnedAddressHTTPSConnection`, which screens again at connect time and
    connects to the address it approved; this one alone would leave the rebind window
    open.
    """
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").strip().rstrip(".")
    if not host:
        raise ModelDownloadError(f"refusing a URL with no host: {redact_model_url(url)}")
    try:
        port = parts.port or 443
    except ValueError:
        raise ModelDownloadError(f"refusing an unusable port in: {redact_model_url(url)}") from None
    _public_addresses(host, port, redact_model_url(url))


def _origin(url: str) -> tuple[str, str, int]:
    """The comparable origin of *url*: lowercased scheme, host, and EFFECTIVE port.

    The same-host redirect exemption below compares origin, not ``hostname`` alone,
    which would make ``mirror -> mirror:8443`` read as "same host" and hand it both the eager
    screen's exemption and the socket pinning's. A co-located internal service on
    another port of the operator's own mirror host is an ordinary deployment shape,
    so that would be a live way into the SSRF this screen exists to close.

    The port is EFFECTIVE, not literal: ``https://h/`` and ``https://h:443/`` are the
    same origin, and a comparison on the literal ``parts.port`` (``None`` versus
    ``443``) would call them different and screen a hop that never left. An
    unparseable port is reported as ``-1`` rather than raised on, so the caller sees
    an origin that matches nothing and screens the hop instead of crashing; the
    screen itself refuses such a URL with a named error.
    """
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "").lower()
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        port = -1
    return scheme, (parts.hostname or "").strip().rstrip(".").lower(), port


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only while it stays on https, to a public address.

    urllib's default handler admits an ``http``, ``https`` or ``ftp`` ``Location``
    without distinction, so an https address that answers 30x with an ``http://``
    one is followed silently and the weights then cross the network in cleartext.
    The https check on the address we were GIVEN cannot see that: it inspects the
    FIRST hop, and every hop after it is chosen by whoever answered. The sha256 pin
    is no help either — it establishes that the BYTES are the pinned ones, which is
    a different claim from the transfer having been private, and it is only settled
    once the last plaintext byte has already arrived.

    Raises rather than returning ``None``. Both stop the redirect, but a ``None``
    falls through to urllib's default error handler and surfaces an opaque
    ``HTTP Error 302: Found``, which reads like a broken server instead of a
    refusal made on purpose.

    The scheme floor above applies to every download. The ADDRESS screen -- the eager
    public-address check and the socket pin -- applies only to a CUSTOM model
    download, and there it covers the configured URL as well as every hop after it.
    The catalog download and the decoder wheel keep the transport they already had,
    because screening them changed behaviour no part of this feature needs: the
    publisher's own address answers 30x into a CDN, so that hop is cross-origin, and
    on an install with ``https_proxy`` set the socket pin cannot bind the far end of a
    tunnel and refuses -- which would deny the whole ``local`` STT provider on
    installs that worked before, with no override.
    """

    def __init__(self, *, screen_addresses: bool) -> None:
        # Keyword-only and REQUIRED: `build_opener` instantiates a handler CLASS with
        # no arguments, so a default here would let a construction site silently pick
        # a screening policy it never chose. An instance has to be passed, which is
        # what makes the choice appear at the seam.
        super().__init__()
        self._screen_addresses = screen_addresses

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        # Scheme-parsed rather than a `startswith`, because this string is the
        # server's to choose: a scheme is case-insensitive, so `HTTPS://` is a
        # legitimate hop that a literal prefix test would refuse.
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https":
            # Redacted, not interpolated: a `Location` is the far end's to spell, so
            # it can put a credential in the userinfo, the path or the query of the
            # address it sends us to and have this refusal write it to the log.
            raise ModelDownloadError(
                f"refusing a non-https redirect to: {redact_model_url(newurl)}"
            )
        if not self._screen_addresses:
            # The catalog path and the decoder wheel: scheme floor only, and the plain
            # connection, which is the transport they had before this feature existed.
            # What guards them is the sha256 pin -- a mirror that answers a `Location`
            # of its own choosing still cannot satisfy it.
            return super().redirect_request(req, fp, code, msg, headers, newurl)
        # A hop is chosen by whoever answered, so the destination is screened too:
        # `https://<internal service>/` is a legitimate https address and the scheme
        # check above has nothing to say about it.
        #
        # Every hop of a screened chain is PINNED at the socket, with no exemption:
        # the configured URL was itself screened before the first request, so there is
        # no origin in the chain that has been trusted without a check, and dropping
        # back to the plain connection for one hop would reopen the rebind window on
        # exactly the host the chain is already on.
        #
        # What the same-origin case still buys is skipping the redundant EAGER lookup
        # -- an artifact store answering `302` to its own blob path is the ordinary
        # internal-mirror topology, and its address was resolved and approved moments
        # ago. The socket screens it again regardless, which is the half that binds.
        #
        # ORIGIN, not hostname: a same-name hop onto another PORT reaches a different
        # service, and comparing the name alone let `mirror -> mirror:8443` claim the
        # exemption -- a co-located internal service on the operator's own mirror host
        # is an ordinary deployment shape, so that was a way past the eager refusal
        # rather than a corner. The scheme is compared for the same reason, even
        # though the https check above already narrows it.
        #
        # `left_operator_origin` is sticky and is NOT `screen_address`: the pin marker
        # is now present from the very first request, so reading it here would report
        # every chain as having left. Once a hop crosses origins, no later hop can
        # claim the eager exemption again -- otherwise `operator -> other ->
        # other//second` read the second hop as same-origin and skipped the lookup on
        # the one host we have no reason to trust.
        current = _origin(req.full_url)
        target = _origin(newurl)
        chain_has_left_the_operators_host = bool(getattr(req, "left_operator_origin", False))
        crosses_origin = target != current or chain_has_left_the_operators_host
        if crosses_origin:
            _refuse_non_public_host(newurl)
        hop = super().redirect_request(req, fp, code, msg, headers, newurl)
        if hop is not None:
            # Read by `_ScreenedHTTPSHandler.https_open`, which is the only consumer.
            # An attribute on the hop rather than a handler flag: a redirect chain
            # makes one Request per hop, and each has to carry its own answer.
            hop.screen_address = True  # type: ignore[attr-defined]
            if crosses_origin:
                hop.left_operator_origin = True  # type: ignore[attr-defined]
        return hop


def _urlopen(url: str, timeout: float, *, screen_addresses: bool = False) -> IO[bytes]:
    """Open *url* for reading, refusing any redirect that leaves https.

    A private opener rather than :func:`urllib.request.urlopen`, whose process-wide
    one carries the permissive redirect handler above and accepts no policy
    argument. Built per call: instantiating a handful of handlers is nothing beside
    a transfer measured in hundreds of megabytes, and it leaves no opener shared
    between two concurrent downloads.

    This is the module's one network seam, so the redirect policy covers the
    catalog download as well as a custom one. The catalog needs it just as much:
    :data:`MODEL_URL_ENV` points that path at an arbitrary mirror, and the
    publisher's own address redirects to a CDN, which is why the answer here is
    "https only" and not "no redirects".

    ``screen_addresses`` adds the address screen on top of that floor -- to the URL
    passed in as well as to every redirect after it -- and defaults OFF because only
    a CUSTOM model download asks for it. Off is the transport every caller had before
    the custom-model feature, so a path that does not opt in cannot be broken by it.
    On, an ``https_proxy`` install cannot fetch a custom model at all: the pin has no
    address to bind through a tunnel and refuses by name -- see
    :meth:`_PinnedAddressHTTPSConnection._connect_to_a_screened_address`.

    It is also the one place a transport exception can quote the URL, so every one
    is re-raised with the address REDACTED. ``valid_custom_url`` already refuses the
    values that make ``http.client`` raise an ``InvalidURL`` naming the whole URL,
    but that check guards one of three ways an address arrives here (a config value;
    :data:`MODEL_URL_ENV`; a value stored before the validator existed), and an
    exception's own text is not one of this module's redacted messages — it travels
    verbatim into the log and into ``ModelStore.status["error"]``. Re-raising is what
    makes the redaction a property of the seam rather than of the caller.
    """
    opener = urllib.request.build_opener(
        _ScreenedHTTPSHandler,
        _HttpsOnlyRedirectHandler(screen_addresses=screen_addresses),
    )
    target: str | urllib.request.Request = url
    if screen_addresses and not _private_custom_url_is_allowed(url):
        # The CONFIGURED address is screened too, not just the hops after it. It
        # arrives from `stt.custom_model_url` in `config.json` -- agent-reachable, and
        # write-protected only at the file-edit tool gate -- so it is not the
        # operator-only value `MODEL_URL_ENV` is: an injected agent that wrote a
        # private https address there had the gateway issue an unauthorized internal
        # GET on the next transcription. The sha256 pin does not stop that, because
        # the request is made before any digest is computed and the side effect on the
        # internal service has already happened by the time the body is discarded.
        #
        # BOTH halves, for the reason `_refuse_non_public_host` states: the eager
        # lookup produces a clear refusal before a socket is opened, and the pin is
        # what closes the DNS-rebind window between the lookup and the connect.
        #
        # An operator who genuinely mirrors weights internally names that origin in
        # `CUSTOM_MODEL_ALLOW_PRIVATE_ENV` and gets the previous transport back for
        # that origin on this hop only -- not for any other private address, which a
        # boolean switch would also have admitted through the same config value.
        # Redirect screening is unaffected either way.
        _refuse_non_public_host(url)
        request = urllib.request.Request(url)
        # Read by `_ScreenedHTTPSHandler.https_open`. Set here rather than inside the
        # handler because only this seam knows whether the caller opted in; the
        # handler must not infer a policy from the request it is handed.
        request.screen_address = True  # type: ignore[attr-defined]
        target = request
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- https enforced by the caller and by the handler above, and the payload is sha256-pinned
        return opener.open(target, timeout=timeout)
    except ModelDownloadError:
        # The redirect handler's own refusal, raised from inside `open`. Already
        # redacted, and re-wrapping it would replace the reason with "download
        # failed (ModelDownloadError)".
        raise
    except urllib.error.HTTPError as exc:
        # The status code is the diagnostic here and it is an integer, so it is kept
        # while `str(exc)` — which an error page or a proxy can shape — is not.
        raise ModelDownloadError(
            f"download failed (HTTP {exc.code}) from {redact_model_url(url)}"
        ) from exc
    except Exception as exc:
        # The class name, never `str(exc)`: `InvalidURL` quotes the whole URL, and
        # what another transport error interpolates is not this module's to predict.
        raise ModelDownloadError(
            f"download failed ({type(exc).__name__}) from {redact_model_url(url)}"
        ) from exc


def _sha256_file(path: Path) -> str:
    """Digest a file on disk. Blocking; callers run it off the loop.

    Read in `_CHUNK_BYTES` pieces rather than whole: the largest model is 1.6 GB, and
    reading that into memory to hash it would cost more than the hash.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def stream_pinned_payload(
    url: str,
    *,
    label: str,
    expected_size: int,
    expected_sha256: str,
    write: Callable[[bytes], object],
    max_bytes: int = 0,
    on_progress: ProgressFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
    screen_addresses: bool = False,
) -> None:
    """Stream *url* into *write*, refusing anything but the pinned payload.

    Shared by the whisper weights and by the ffmpeg decoder wheel
    (:mod:`kiro_crew.stt.decoder`), because every property below is a property of
    "fetching a sha256-pinned artifact over the network" rather than of either
    artifact: the https floor, the per-socket stall timeout, the digest computed
    while streaming, and the size enforced as a CEILING before each write. A
    second copy of this is a second place for one of them to be missing.

    *write* is a callable rather than a path, so the caller owns where the bytes
    land -- an exclusively-created staging descriptor in both cases -- and this
    function never opens or names a file.

    ``expected_size`` 0 means the artifact's LENGTH is not pinned, which is the
    custom-model case: an operator supplies a URL and a digest, and no published
    row states a size. Then ``max_bytes`` is the ceiling instead and the exact
    length check is skipped -- but the transfer is still bounded, because the
    digest is the trust anchor and a digest is only known to be wrong once the
    last byte has arrived, far too late to stop a mirror from filling the disk.
    A short or empty unpinned body is then caught by the digest rather than by a
    length check. Passing neither bound is a programming error and raises, so no
    call can produce an unbounded transfer.

    Raises :class:`ModelDownloadError` on a refusal. A transport failure (an
    unreachable host, an HTTP status, the stall timeout) arrives as one too, raised
    by :func:`_urlopen` with the address redacted and the original attached as
    ``__cause__`` — the class is not a claim that the payload was rejected, and a
    caller that needs to tell the two apart reads ``__cause__`` rather than the type.
    """
    if not url.startswith("https://"):
        # The pin bounds what we accept, but plaintext would still leak which
        # artifact an operator uses and let a network attacker waste the transfer.
        # Redacted, not interpolated: for a custom model this address is an
        # operator-supplied config value, so it can carry a credential in its
        # userinfo, path or query, and this refusal is written to the log.
        raise ModelDownloadError(f"{label}: refusing a non-https URL: {redact_model_url(url)}")
    ceiling = expected_size or max_bytes
    if ceiling <= 0:
        # Raised rather than defaulted: a silent fallback ceiling here would be a
        # number nobody chose, applied to a transfer nobody bounded.
        raise ValueError(f"{label}: expected_size or max_bytes must bound the transfer")
    digest = hashlib.sha256()
    written = 0
    # `_urlopen`, never `urllib.request.urlopen`: the module-wide opener follows a
    # redirect OFF https and quotes the URL verbatim in transport errors. Both
    # matter to every caller of this helper, not just the whisper one -- the
    # decoder wheel is fetched from a mirror that redirects too -- so the seam
    # belongs here rather than around one call site.
    with _urlopen(
        url,
        timeout=_NETWORK_STALL_TIMEOUT_SECS,
        screen_addresses=screen_addresses,
    ) as response:
        while True:
            if should_cancel is not None and should_cancel():
                raise ModelDownloadError("cancelled")
            chunk = response.read(_CHUNK_BYTES)
            if not chunk:
                break
            # Refused BEFORE the write, because the ceiling is a bound on what we
            # are willing to store and not merely something to check afterwards.
            # Streaming to EOF first and comparing the total lets a hostile or
            # misconfigured mirror fill the disk: nothing about an HTTPS response
            # bounds its length, and `Content-Length` is not consulted (it is the
            # server's claim, not the pin). Failing on the first excess chunk caps
            # the damage at one `_CHUNK_BYTES` over the size we agreed to.
            if written + len(chunk) > ceiling:
                bound = (
                    f"pinned {expected_size} bytes"
                    if expected_size
                    else f"unpinned-length ceiling of {ceiling} bytes"
                )
                raise ModelDownloadError(
                    f"{label}: response exceeds the {bound}; refusing to keep writing"
                )
            write(chunk)
            digest.update(chunk)
            written += len(chunk)
            if on_progress is not None:
                on_progress(written, expected_size)
    # Only a SHORT response can reach this now; the oversized case fails inside the
    # loop. Kept as a distinct check because a truncated transfer is the common
    # failure (a dropped connection) and deserves its own message. With no pinned
    # length there is nothing to compare against, so a truncated custom download --
    # an empty body included -- is left to the digest below, which catches every
    # one of them. A separate emptiness check here would be a second way to say
    # "the bytes are not the pinned artifact".
    if expected_size and written != expected_size:
        raise ModelDownloadError(f"{label}: expected {expected_size} bytes, received {written}")
    actual = digest.hexdigest()
    if actual != expected_sha256:
        raise ModelDownloadError(
            f"{label}: sha256 mismatch (got {actual[:16]}…, expected {expected_sha256[:16]}…)"
        )


def _download_blocking(
    model: WhisperModel,
    on_progress: ProgressFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Path:
    """Fetch and verify *model*, returning its final path. Blocks; never on the loop.

    The digest is computed while streaming rather than by re-reading the finished
    file, so a tampered payload is rejected without ever having been written to
    the path a loader would pick up.
    """
    target = model_path(model)
    target.parent.mkdir(parents=True, exist_ok=True)
    url = _model_url(model)

    # `mkstemp`, not a name this function composes. Two properties matter and neither
    # is available from `open(path, "wb")`:
    #
    # * It opens with ``O_CREAT | O_EXCL``, so it CANNOT follow a symlink. This
    #   directory is agent-writable, and a predictable staging path (a fixed
    #   ``.bin.part``, or one derived from a PID that is trivially observable) let an
    #   agent pre-plant a symlink there and have the download truncate and overwrite
    #   whatever it pointed at.
    # * The name is unpredictable and unique, which also settles the collision the
    #   fixed name caused: nothing serialises across processes -- a gateway, an MCP
    #   server and a `kirocrew` CLI each run their own store over the same data home --
    #   so two transfers interleaved their writes into one file and the sha256 pin then
    #   failed BOTH of them.
    #
    # Created in the TARGET directory so the finishing rename stays same-filesystem
    # and therefore atomic; a system-temp staging area can land on another device,
    # where the rename degrades into a copy a reader can observe half-finished.
    staging_fd, staging_name = tempfile.mkstemp(
        dir=target.parent, prefix=f"{target.name}.", suffix=_STAGING_SUFFIX
    )
    staging = Path(staging_name)
    try:
        # The descriptor is adopted FIRST, and the order is load-bearing even
        # though opening the connection first reads more naturally. `mkstemp`
        # hands back an unowned fd and nothing else here closes one -- the
        # `finally` below unlinks the PATH -- so with the transfer in front, every
        # unreachable host, HTTP error and stall timeout leaked one descriptor per
        # attempt, because a context manager that never gets evaluated never gets
        # exited. On Windows it also stranded the partial file, since the unlink
        # cannot remove a file that still has a live handle.
        #
        # Written through the DESCRIPTOR `mkstemp` returned, never reopened by
        # name: reopening would reintroduce the very race the exclusive create
        # closed, since the path is public the moment it exists.
        with os.fdopen(staging_fd, "wb") as fh:
            stream_pinned_payload(
                url,
                label=model.name,
                expected_size=model.size_bytes,
                expected_sha256=model.sha256,
                write=fh.write,
                max_bytes=_CUSTOM_MAX_BYTES,
                on_progress=on_progress,
                should_cancel=should_cancel,
                # Only a CUSTOM url opts into the address screen. `model.url` is set
                # exactly when the operator supplied the address themselves, which is
                # the path this feature adds; the catalog path keeps the transport it
                # had, so an install behind an https proxy still fetches the default.
                screen_addresses=bool(model.url),
            )
        # replace_with_retry, not os.replace: on Windows the rename fails with a
        # PermissionError while ANY other handle is open on either path, and a
        # just-written 148MB file is exactly what an AV scanner or the Search
        # indexer reaches for.
        replace_with_retry(staging, target)
        logger.info("Whisper model %s verified and installed at %s", model.name, target)
        return target
    finally:
        # A failed or cancelled attempt must not leave a partial file behind. On
        # the success path the rename already consumed it, so missing is normal.
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not remove staging file %s", staging, exc_info=True)


class ModelStore:
    """Serialises model downloads and exposes their progress.

    One store per process. Concurrent callers (a voice session that needs the
    model and a settings panel that asked for it) share one in-flight transfer
    through ``_lock`` instead of both pulling the same gigabyte.
    """

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        #: Strong references to detached decoder fetches. A task nobody references
        #: can be collected mid-await, and the set is per-store so a test with its
        #: own store cannot be handed another one's pending work.
        self._decoder_tasks: set[asyncio.Task[None]] = set()
        self.status: dict[str, object] = {
            "step": "idle",
            "model": "",
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "error": "",
        }

    def _start_decoder_autofetch(self) -> None:
        """Fetch the ffmpeg decoder in the background, if this install needs one.

        Started here because a completed model download is the one moment a second
        transfer reads as part of the same setup: the user has just committed to
        local speech-to-text, and a source install with no system FFmpeg cannot
        decode a browser recording or a voice memo at all. The alternative is
        discovering that at the first upload -- a failure the settings page can only
        answer with a shell command.

        Detached rather than awaited: the caller is a voice session or a settings
        poll waiting on the WEIGHTS, and live PCM never touches the decoder.
        Failures are the decoder store's to report through its own status, so this
        only has to keep the task alive and its exception retrieved.
        """
        # Imported here, not at module scope: decoder imports this module, so a
        # module-scope import back would be a cycle.
        from kiro_crew.stt import decoder as stt_decoder

        try:
            task = asyncio.ensure_future(stt_decoder.maybe_autofetch())
        except RuntimeError:
            # No running loop. `ensure` is always awaited, so this is not reachable
            # from production; it keeps a synchronous caller in a test from turning
            # a successful model download into a failure.
            logger.debug("No event loop for the decoder autofetch", exc_info=True)
            return
        self._decoder_tasks.add(task)
        task.add_done_callback(self._decoder_task_done)

    def _decoder_task_done(self, task: asyncio.Task[None]) -> None:
        """Drop the reference and retrieve the exception, so neither is a warning."""
        self._decoder_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("Background ffmpeg decoder fetch failed: %s", exc)

    async def ensure(self, model: WhisperModel) -> Path | None:
        """Return the on-disk path of *model*, downloading it if absent.

        Returns ``None`` when the model is not available and cannot be fetched,
        so a caller degrades to reporting voice as unavailable rather than
        raising into a websocket handler.

        A file already on disk is verified against the pin (see
        :meth:`_verified_on_disk`) rather than trusted on its size alone. BOTH
        already-present branches go through :meth:`_accept_existing`, which is the
        point: an earlier revision gated only the pre-lock one and left the post-lock
        "a concurrent caller finished while we waited" branch returning the file
        unverified, which is the same hole through the other door.
        """
        accepted = await self._accept_existing(model)
        if accepted is not None:
            return accepted
        if os.environ.get(SKIP_DOWNLOAD_ENV) == "1":
            logger.info("%s=1, not downloading whisper model %s", SKIP_DOWNLOAD_ENV, model.name)
            self._set(step="skipped", model=model.name, error="model download disabled")
            return None
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            # A concurrent caller may have completed it while we waited, and its file
            # gets the same check ours would have.
            accepted = await self._accept_existing(model)
            if accepted is not None:
                return accepted
            self._set(step="downloading", model=model.name, total=model.size_bytes)
            try:
                path = await asyncio.to_thread(
                    _download_blocking,
                    model,
                    lambda done, total: self._set(
                        step="downloading", model=model.name, done=done, total=total
                    ),
                )
            except Exception as exc:
                logger.warning("Whisper model %s download failed: %s", model.name, exc)
                self._set(step="failed", model=model.name, error=str(exc))
                return None
            self._set(step="ready", model=model.name, total=model.size_bytes)
            self._start_decoder_autofetch()
            return path

    async def _accept_existing(self, model: WhisperModel) -> Path | None:
        """The on-disk path if *model* is present AND matches its pin, else ``None``.

        The single gate for "there is already a usable file here". Both of `ensure`'s
        already-present branches ask this rather than `is_present` directly, so a
        digest check cannot be added to one door and forgotten at the other.

        ``None`` means "carry on to the download": either nothing is there, or what was
        there failed the pin and has been deleted.
        """
        if not is_present(model):
            return None
        if not await self._verified_on_disk(model):
            logger.warning("Re-downloading whisper model %s after a failed check", model.name)
            return None
        self._set(step="ready", model=model.name, total=model.size_bytes)
        return model_path(model)

    async def _verified_on_disk(self, model: WhisperModel) -> bool:
        """Check an already-present model against its pin, once per process.

        `is_present` deliberately tests only the size: it answers "must this be
        downloaded", and it is on the path of a UI poll. But size alone is not a
        trust check, and the models directory is fenced by nothing -- neither
        `is_sensitive_path` nor `is_sensitive_write_path` covers it, and a plain
        ``cp`` over the weights is an allowed bash command. So an agent could replace
        them with a same-size file and every later session would transcribe the user's
        speech through weights of its choosing, persistently and with nothing to show
        it had happened.

        Verified on EVERY call, with no metadata cache. A first version memoised the
        result against the file's size and mtime, which does not hold: `os.utime` is
        available to anything that can write the file, so a same-size overwrite with a
        restored mtime satisfied the memo and the next load trusted it. Anything cheap
        enough to check is also cheap enough to forge.

        Affordable because `WhisperEngine.ensure_loaded` decides residency BEFORE
        asking the store, so this runs once per model LOAD rather than once per
        session. That is also precisely when an unverified file would take effect: a
        resident model is already loaded from bytes that passed, and the next thing
        that could load different ones is the next load.

        A file that fails is deleted, so the caller's download path replaces it rather
        than reporting voice as broken.

        Deliberately diverging from `embeddings`, which documents the same size-only
        trade for its own GGUF. That reasoning ("a sha256 over ~600MB on every boot
        buys almost nothing") weighed a corrupted download, not a writable directory
        and an agent with a shell.
        """
        path = model_path(model)
        if not path.is_file():
            return False
        actual = await asyncio.to_thread(_sha256_file, path)
        if actual == model.sha256:
            return True
        logger.error(
            "Whisper model %s at %s does not match its pinned digest "
            "(got %s…, expected %s…). Removing it: a model of the right SIZE but the "
            "wrong CONTENT would transcribe every later utterance through weights "
            "nobody verified.",
            model.name,
            path,
            actual[:16],
            model.sha256[:16],
        )
        try:
            path.unlink()
        except OSError:
            logger.warning("Could not remove the unverified model at %s", path, exc_info=True)
            return False
        return False

    def _set(
        self,
        *,
        step: str,
        model: str,
        done: int = 0,
        total: int = 0,
        error: str = "",
    ) -> None:
        self.status = {
            "step": step,
            "model": model,
            "downloaded_bytes": done,
            "total_bytes": total,
            "error": error,
        }


_store: ModelStore | None = None


def store() -> ModelStore:
    """The process-wide model store.

    A module global holding shared, caller-independent state (which model files
    exist and whether a transfer is running) rather than per-caller data, so it is safe
    in the MCP servers that serve many sessions from one process.
    """
    global _store
    if _store is None:
        _store = ModelStore()
    return _store
