"""OTLP/HTTP span export: where spans may go, and the exporter they leave through.

Nothing here runs unless ``telemetry.traces_enabled`` is on: ``provider`` calls
:func:`resolve` only while it is, so an ``OTEL_EXPORTER_OTLP_*`` variable set for
another tool on this host never starts egress by itself.

**Where.** ``telemetry.traces_otlp_endpoint`` (the full ``/v1/traces`` URL) wins.
Without it, ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``, then ``OTEL_EXPORTER_OTLP_ENDPOINT``
with ``/v1/traces`` appended (the SDK's rule). An LTS build ignores all
three (:mod:`kiro_crew.edition_locks`), as it ignores the local metrics endpoints.
The URL must be ``https://`` unless its host is loopback, and may carry no
``user:password``, query or fragment: a credential goes in a header, where it
is read from the vault and never logged. The host is held to the
``capabilities.telemetry`` ``otlp_hosts`` policy at build and again on every
export, with the metrics signal's SEL audit (``metrics.provider``).

**Headers.** Each source's headers go to that source's endpoint only, so a
credential meant for one collector never reaches another:

- a config endpoint sends ``traces_otlp_headers_secret`` (header -> vault entry
  NAME) and ``traces_otlp_headers_env`` (header -> environment variable), and
  never the process-wide OTel header variables;
- an environment endpoint sends ``OTEL_EXPORTER_OTLP_TRACES_HEADERS``, else
  ``OTEL_EXPORTER_OTLP_HEADERS``.

A header that cannot be read (no such entry, an unset variable, a config value
that is not an entry name, i.e. a literal) turns export off with one warning
naming the header, never a value.

**What.** :class:`RemoteSpans` drops every ``kirocrew.local.*`` key before a
span reaches the wire, and its target can be swapped or cleared on a config
write without a new processor or thread.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from kiro_crew.tracing.correlation import LOCAL_PREFIX

logger = logging.getLogger(__name__)

ENV_TRACES_ENDPOINT = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
ENV_ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
ENV_TRACES_HEADERS = "OTEL_EXPORTER_OTLP_TRACES_HEADERS"
ENV_HEADERS = "OTEL_EXPORTER_OTLP_HEADERS"
ENV_SERVICE_NAME = "OTEL_SERVICE_NAME"

#: The non-secret label logs and SEL rows name; the endpoint itself never is.
DEST_NAME = "telemetry.traces_otlp_endpoint"

#: Seconds one export may take, retries included (the SDK default is 10 too,
#: pinned here so an ``OTEL_EXPORTER_OTLP_*TIMEOUT`` variable cannot raise it).
EXPORT_TIMEOUT_SECS = 10.0

_SERVICE_NAME_MAX = 128

# Character checks are sets plus length bounds, never regexes: a whole-string
# check cannot be weakened into a prefix match by an edit, and ``$`` cannot let a
# final newline through.
_DIGITS = frozenset("0123456789")
_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_ALNUM = _LETTERS | _DIGITS
#: RFC 9110 field-name ``token`` characters (``tchar``).
_TCHARS = _ALNUM | frozenset("!#$%&'*+-.^_`|~")
_HEADER_NAME_MAX = 64
#: A vault entry name, as cron secret grants spell it: a literal header value
#: (``Bearer …``, ``Basic …=``) fails it, and is refused rather than sent.
_VAULT_NAME_FIRST = _ALNUM
_VAULT_NAME_REST = _ALNUM | frozenset("._-")
_ENV_NAME_FIRST = _LETTERS | frozenset("_")
_ENV_NAME_REST = _ALNUM | frozenset("_")
_REF_NAME_MAX = 128
#: Header value characters: visible ASCII, space and tab. No CR, LF or NUL, so
#: ``requests`` never refuses one with an ``InvalidHeader`` that quotes it, and
#: no non-ASCII, which ``http.client`` cannot encode.
_HEADER_VALUE_CHARS = frozenset(chr(c) for c in range(0x20, 0x7F)) | frozenset("\t")
_HEADER_VALUE_MAX = 8192


def _is_name(value: object, first: frozenset[str], rest: frozenset[str], limit: int) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= limit
        and value[0] in first
        and all(c in rest for c in value[1:])
    )


def _is_header_name(value: object) -> bool:
    return _is_name(value, _TCHARS, _TCHARS, _HEADER_NAME_MAX)


def _is_vault_name(value: object) -> bool:
    return _is_name(value, _VAULT_NAME_FIRST, _VAULT_NAME_REST, _REF_NAME_MAX)


def _is_env_name(value: object) -> bool:
    return _is_name(value, _ENV_NAME_FIRST, _ENV_NAME_REST, _REF_NAME_MAX)


def _is_header_value(value: object) -> bool:
    """Non-empty, no leading space or tab (``requests`` refuses one), allowed characters."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= _HEADER_VALUE_MAX
        and value[0] not in " \t"
        and all(c in _HEADER_VALUE_CHARS for c in value)
    )


#: Where a header's value is read from.
FROM_VAULT = "vault"
FROM_ENV = "env"
FROM_OTEL_ENV = "otel_env"


@dataclass(frozen=True)
class Destination:
    """One trace collector. Holds where each header comes from, never a value."""

    endpoint: str
    #: ``"config"`` or ``"env"``: which source named the endpoint.
    source: str
    #: ``(header, FROM_*, reference)``; for ``FROM_OTEL_ENV`` the variable name.
    headers: tuple[tuple[str, str, str], ...] = ()
    name: str = DEST_NAME


def service_name(environ: Mapping[str, str] = os.environ) -> str:
    """``OTEL_SERVICE_NAME`` for the trace resource, else ``kirocrew``."""
    value = str(environ.get(ENV_SERVICE_NAME, "") or "").strip()
    if value and len(value) <= _SERVICE_NAME_MAX and value.isprintable():
        return value
    return "kirocrew"


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _endpoint_problem(endpoint: str) -> str | None:
    """Why *endpoint* is refused, in words that never quote it; ``None`` if usable."""
    try:
        parts = urlsplit(endpoint)
        host = parts.hostname or ""
    except ValueError:
        return "it is not a URL"
    if parts.scheme not in ("http", "https") or not host:
        return "it is not an http(s) URL"
    if any(c.isspace() for c in parts.netloc):
        return "its host contains whitespace"
    if parts.username is not None or parts.password is not None:
        return "it carries user:password (put the credential in a vault header)"
    if parts.query or parts.fragment:
        return "it carries a query or fragment"
    if parts.scheme == "http" and not _is_loopback(host):
        return "it is http:// to a host that is not loopback (https:// is required)"
    return None


def _config_headers(cfg: Any) -> tuple[tuple[str, str, str], ...] | None:
    """The config header references, or ``None`` when one is unusable."""
    refs: list[tuple[str, str, str]] = []
    for field_name, kind, is_ref in (
        ("traces_otlp_headers_secret", FROM_VAULT, _is_vault_name),
        ("traces_otlp_headers_env", FROM_ENV, _is_env_name),
    ):
        rows = getattr(cfg, field_name, None) or {}
        if not isinstance(rows, dict):
            logger.warning("telemetry.%s is not an object; trace export off", field_name)
            return None
        for header, ref in rows.items():
            if not _is_header_name(header):
                logger.warning(
                    "telemetry.%s has a key that is not a header name; trace export off",
                    field_name,
                )
                return None
            if not is_ref(ref):
                # The value may BE the credential pasted in by mistake: name the
                # header only.
                logger.warning(
                    "telemetry.%s[%r] is not a %s name (a literal header value is "
                    "never read from config); trace export off",
                    field_name,
                    header,
                    "Settings -> Secrets entry" if kind == FROM_VAULT else "variable",
                )
                return None
            refs.append((header, kind, ref))
    return tuple(refs)


def resolve(cfg: Any, environ: Mapping[str, str] = os.environ) -> Destination | None:
    """The trace collector *cfg* and *environ* name, or ``None`` for none."""
    if getattr(cfg, "traces_enabled", None) is not True:
        return None
    from kiro_crew.edition_locks import local_enable_locked

    if local_enable_locked("telemetry.otlp_hosts"):
        return None
    configured = getattr(cfg, "traces_otlp_endpoint", "")
    endpoint = configured.strip() if isinstance(configured, str) else ""
    source = "config"
    if not endpoint:
        source = "env"
        endpoint = str(environ.get(ENV_TRACES_ENDPOINT, "") or "").strip()
        if not endpoint:
            base = str(environ.get(ENV_ENDPOINT, "") or "").strip()
            endpoint = f"{base.rstrip('/')}/v1/traces" if base else ""
    if not endpoint:
        return None
    problem = _endpoint_problem(endpoint)
    if problem is not None:
        logger.warning("trace OTLP endpoint (%s) refused: %s; trace export off", source, problem)
        return None
    if source == "config":
        refs = _config_headers(cfg)
        if refs is None:
            return None
        return Destination(endpoint, source, refs)
    var = ENV_TRACES_HEADERS if environ.get(ENV_TRACES_HEADERS) else ENV_HEADERS
    if environ.get(var):
        return Destination(endpoint, source, (("", FROM_OTEL_ENV, var),))
    return Destination(endpoint, source)


def _vault_value(name: str) -> str | None:
    from kiro_crew.config.paths import config_dir
    from kiro_crew.secrets.vault import SecretVault

    value = SecretVault(config_dir()).get(name)
    return value.reveal() if value is not None else None


def _otel_env_headers(text: str) -> dict[str, str] | None:
    """``name=value,…`` (values URL-encoded or not), or ``None`` if any entry is malformed.

    Parsed here rather than by the SDK's ``parse_env_headers``, which logs a
    malformed entry verbatim: an ``Authorization: Basic …`` written in the
    wrong shape would put the credential in the log.
    """
    from urllib.parse import unquote

    out: dict[str, str] = {}
    for entry in text.split(","):
        if not entry.strip():
            continue
        name, sep, value = entry.partition("=")
        name, value = unquote(name).strip().lower(), unquote(value).strip()
        if not (sep and _is_header_name(name) and _is_header_value(value)):
            return None
        out[name] = value
    return out


def read_headers(
    dest: Destination, environ: Mapping[str, str] = os.environ
) -> dict[str, str] | None:
    """*dest*'s header values, read now; ``None`` (and one warning) when one is unusable.

    No warning quotes a value, and none names a vault reference.
    """
    out: dict[str, str] = {}
    for header, kind, ref in dest.headers:
        if kind == FROM_OTEL_ENV:
            parsed = _otel_env_headers(str(environ.get(ref, "")))
            if parsed is None:
                logger.warning(
                    "%s holds an entry that is not name=value with a valid header "
                    "name and value; trace export off",
                    ref,
                )
                return None
            out.update(parsed)
            continue
        try:
            value = _vault_value(ref) if kind == FROM_VAULT else environ.get(ref)
        except Exception:  # an unreadable vault: no export, and no detail to log
            value = None
        if value is None or not _is_header_value(value):
            # Neither the vault entry nor the variable is named: a credential
            # pasted in place of a name can pass either name check.
            logger.warning(
                "trace OTLP header %r: its %s is missing, empty or unusable; trace export off",
                header,
                "Settings -> Secrets entry" if kind == FROM_VAULT else "environment variable",
            )
            return None
        out[header] = value
    return out


def _export_session(requests: Any) -> Any:
    """The exporter's HTTP session: no redirects, and nothing it reports quotes a header.

    Our own, so the OTel credential-provider variable is not read. It follows no
    redirect: the policy gate and the https rule checked this endpoint only, and
    a 307/308 would resend the batch, and every header but ``Authorization``, to
    a host neither checked; a redirect fails the export.

    The SDK logs a failed request's exception text and the response's reason
    phrase. Header validation already refuses what ``requests`` would reject,
    but either text can still carry a header value (a library message quoting
    the request, a collector echoing a header in its status line). So every
    ``requests`` error is re-raised as the same class with fixed text, keeping
    the SDK's retry decision, and the reason phrase is replaced by the standard
    one for the status code.
    """
    from http import HTTPStatus

    class _Session(requests.Session):  # type: ignore[misc, name-defined]
        def request(self, *args: Any, **kwargs: Any) -> Any:
            failed: type | None = None
            try:
                response = super().request(*args, **kwargs)
            except requests.exceptions.RequestException as exc:
                failed = type(exc)
            if failed is not None:
                # Raised outside the handler, so no __context__ keeps the original.
                raise failed(f"{failed.__name__} (request detail withheld)")
            try:
                response.reason = HTTPStatus(response.status_code).phrase
            except ValueError:
                response.reason = ""
            return response

    # The metrics signal's no-redirect rule, in one place: a copy of this
    # session, same class, with max_redirects = 0.
    from kiro_crew.metrics import otlp_session

    session = otlp_session.export_session(_Session(), own_only=True)
    # A set ``auth`` stops requests reading ``~/.netrc`` (or ``$NETRC``) for the
    # collector's host, which would replace the configured Authorization header
    # with a credential nobody chose for tracing. Proxy and CA settings from the
    # environment still apply.
    session.auth = _no_auth
    return session


def _no_auth(request: Any) -> Any:
    return request


def build_exporter(dest: Destination) -> Any | None:
    """A policy-gated ``OTLPSpanExporter`` for *dest*, or ``None``; never raises."""
    headers = read_headers(dest)
    if headers is None:
        return None
    from kiro_crew.metrics import provider as metrics_provider

    if not metrics_provider.trace_destination_permitted(dest):
        # Audited by the policy check; its own warning is once per host for the
        # whole process, metrics included, so this one always says why.
        logger.warning(
            "trace OTLP endpoint refused by the security policy "
            "(capabilities.telemetry otlp_hosts); trace export off"
        )
        return None
    try:
        import requests
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError:
        logger.warning(
            "trace OTLP endpoint is set but opentelemetry-exporter-otlp-proto-http "
            "is not installed (the otlp extra); trace export off"
        )
        return None
    session = _export_session(requests)
    try:
        exporter = OTLPSpanExporter(
            endpoint=dest.endpoint,
            # Always truthy, so the exporter never falls back to the process-wide
            # OTel header variables for a config endpoint.
            headers=metrics_provider._OwnHeadersOnly(headers),
            timeout=EXPORT_TIMEOUT_SECS,
            session=session,
        )
        metrics_provider.gate_trace_exporter(exporter, dest.endpoint)
    except Exception:
        # A constructor error can echo the endpoint: fixed text only.
        logger.warning("trace OTLP exporter init failed; trace export off")
        return None
    return exporter


def _without_local(attributes: Any) -> dict[str, Any] | None:
    """*attributes* minus ``kirocrew.local.*``, or ``None`` when none was there."""
    if not attributes or not any(str(k).startswith(LOCAL_PREFIX) for k in attributes):
        return None
    return {k: v for k, v in attributes.items() if not str(k).startswith(LOCAL_PREFIX)}


def strip_local(span: Any) -> Any:
    """*span* as it may leave the host: the same span when it has no local key."""
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import Link

    attrs = _without_local(span.attributes)
    links = list(span.links or ())
    link_attrs = [_without_local(link.attributes) for link in links]
    if attrs is None and all(a is None for a in link_attrs):
        return span
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=attrs if attrs is not None else span.attributes,
        events=span.events,
        links=[
            Link(link.context, a) if a is not None else link for link, a in zip(links, link_attrs)
        ],
        kind=span.kind,
        status=span.status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class RemoteSpans:
    """The span exporter behind the OTLP batch processor; never raises.

    Its target is the gated ``OTLPSpanExporter`` for the current destination, or
    ``None``, which drops each batch: the processor and its thread outlive a
    switch-off or an endpoint change, and a turn still open keeps ending spans.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._target: Any = None
        self._dest: Destination | None = None
        #: Whether a batch processor reads from this object yet.
        self.attached = False

    @property
    def destination(self) -> Destination | None:
        return self._dest

    def swap(self, dest: Destination | None, target: Any) -> None:
        """Send later batches to *target* (``None`` drops them); shut the old one down."""
        with self._lock:
            old, self._target, self._dest = self._target, target, dest
        if old is not None:
            try:
                old.shutdown()
            except Exception:
                logger.warning("previous trace OTLP exporter did not shut down cleanly")

    def export(self, spans: Sequence[Any]) -> Any:
        from opentelemetry.sdk.trace.export import SpanExportResult

        target = self._target
        if target is None:
            return SpanExportResult.SUCCESS
        try:
            return target.export([strip_local(span) for span in spans])
        except Exception:
            logger.warning("trace OTLP export failed")
            return SpanExportResult.FAILURE

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def shutdown(self) -> None:
        self.swap(None, None)
