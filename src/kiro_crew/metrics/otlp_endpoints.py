"""OTLP endpoint entries: ``telemetry.otlp_endpoints`` and the legacy string.

Telemetry can export to several OTLP collectors at once. Each one is an ENTRY, and
each entry becomes one :class:`~kiro_crew.platform.interfaces.OtlpDestination`, so
everything the core already does per destination (the consent gate, the policy
``otlp_hosts`` check, the per-export re-check, the narrowing wrapper) applies to
every entry with no second code path.

Entries come from config, in this order:

1. ``telemetry.otlp_endpoint`` -- the legacy single-string setting. When it is a
   non-empty string it becomes the FIRST entry, named ``telemetry.otlp_endpoint``,
   with no narrowing: byte-identical to the one destination it produced before
   the list existed.
2. ``telemetry.otlp_endpoints`` -- the operator's list, in ``config.json``.

A build that ships a collector of its own does not add a config entry: its
``TelemetryProvider.otlp_destinations`` returns that destination next to
:func:`config_destinations`. Config cannot remove or turn off a destination that
code returns, which is what "locked on" means here; every process-wide gate still
applies to it. :func:`label_sources` is how the Privacy panel tells the two apart.

Entry keys (all optional except ``endpoint``)::

    {"name": "ops", "endpoint": "https://collector.example.com/v1/metrics",
     "enabled": true,
     "headers_env": {"Authorization": "OPS_OTLP_AUTH", "X-Scope": "OPS_OTLP_SCOPE"},
     "temporality": "cumulative",
     "allow": ["kirocrew.session.started"],
     "point_attributes": {"ClientType": "desktop"},
     "resource_allow": ["service.name", "service.version"]}

``headers_env`` maps a header name to an ENVIRONMENT VARIABLE name, so a secret is
read when the exporter is built and never has to sit in the config file. The value
is never stored on the entry or the destination, and never logged. There is no key
for a literal header value: an entry that carries ``headers`` is REFUSED with a
warning that points at ``headers_env``, because a value written into
``config.json`` is readable by everything that reads the config.

The process-wide ``OTEL_EXPORTER_OTLP_METRICS_HEADERS`` / ``OTEL_EXPORTER_OTLP_HEADERS``
variables (and the OTel credential-provider variable) apply to the legacy
``telemetry.otlp_endpoint`` entry only. A list entry sends the headers it names
in ``headers_env`` and nothing else, so a credential set for one collector never
reaches a host added to the list later (``OtlpDestination.process_env_credentials``).

``allow`` is a list of full metric names, or the name of a reviewed preset
(:data:`ALLOW_PRESETS`). An entry that names an unknown preset is REFUSED rather
than read as "no allowlist": the safe failure of an allowlist is to send nothing.

``temporality`` is ``""`` (the core default, unchanged: see
:mod:`kiro_crew.metrics.temporality`), ``"cumulative"`` or ``"delta"``.

Every malformed entry is dropped with one warning that names the entry and the
field, never a value. Nothing here raises for bad input, and nothing here imports
the OpenTelemetry SDK, so the Privacy panel's posture read stays cheap.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from kiro_crew.platform.interfaces import OtlpDestination

logger = logging.getLogger(__name__)

#: The name the legacy ``telemetry.otlp_endpoint`` entry carries.
LEGACY_NAME = "telemetry.otlp_endpoint"

#: Where an entry came from. Reported by the Privacy panel.
SOURCE_LEGACY = "otlp_endpoint"
SOURCE_CONFIG = "otlp_endpoints"
#: A destination the telemetry provider returns from code, not from config.
SOURCE_PROVIDER = "provider"

#: Accepted ``temporality`` values; ``""`` keeps the core default.
TEMPORALITIES = frozenset({"", "cumulative", "delta"})

#: Named allowlists an entry may ask for by name instead of listing metrics.
ALLOW_PRESETS = frozenset({"PRODUCT_METRICS"})

#: Upper bound on entries read from the list. Each entry runs its own export
#: thread, so this keeps a pasted or generated list from starting hundreds of
#: them. An arbitrary safety bound, not a tuned value; so are the per-field
#: bounds below, which keep one entry from holding an unbounded string or map.
MAX_ENTRIES = 16
MAX_ENDPOINT_LEN = 2048
MAX_HEADERS = 32

_ENTRY_KEYS = frozenset(
    {
        "name",
        "endpoint",
        "enabled",
        "headers_env",
        "temporality",
        "allow",
        "point_attributes",
        "resource_allow",
    }
)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# RFC 9110 token characters.
_HEADER_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_POINT_VALUE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_METRIC_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._/-]{0,254}$")
_RESOURCE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,127}$")
_MAX_LIST = 256


class EntryError(ValueError):
    """An entry that cannot be used. The message names fields, never values."""


@dataclass(frozen=True)
class EndpointEntry:
    """One checked OTLP endpoint entry."""

    name: str
    endpoint: str
    source: str
    headers_env: "Mapping[str, str]" = field(default_factory=lambda: MappingProxyType({}))
    temporality: str = ""
    allow: "Optional[frozenset[str]]" = None
    point_attributes: "Mapping[str, str]" = field(default_factory=lambda: MappingProxyType({}))
    resource_allow: "Optional[frozenset[str]]" = None


# ── warnings, once each ─────────────────────────────────────────────────────

#: Most warning keys remembered at once. Each config revision with a new malformed
#: entry adds one, so without a bound a long-lived gateway would keep every
#: discarded revision's key. When full the memory is cleared, which costs at most
#: one repeat of a warning that is still true. An arbitrary bound, not a tuned one.
MAX_WARNED = 256

#: Fixed-size digests of the keys, so a long field message is not what is kept.
_warned: "set[bytes]" = set()
_warned_lock = threading.Lock()


def _warn_once(key: str, message: str, *args: object) -> None:
    """Warn the first time *key* is seen in this process, then stay quiet.

    Entries are resolved on every recorder build and on every Privacy panel read,
    so a bad entry would otherwise log on each of them. What is remembered is a
    16-byte digest of *key*, at most :data:`MAX_WARNED` of them.
    """
    digest = hashlib.sha256(key.encode("utf-8", "replace")).digest()[:16]
    with _warned_lock:
        first = digest not in _warned
        if first:
            if len(_warned) >= MAX_WARNED:
                _warned.clear()
            _warned.add(digest)
    if first:
        logger.warning(message, *args)


def reset_warnings() -> None:
    """Forget which entries have warned. Test helper."""
    with _warned_lock:
        _warned.clear()


# ── parsing ─────────────────────────────────────────────────────────────────


def _str_map(raw: object, where: str, key_re: "re.Pattern[str]", value_check: Any) -> "dict":
    if raw is None:
        return {}
    if not isinstance(raw, Mapping) or len(raw) > MAX_HEADERS:
        raise EntryError(f"{where} must be an object with at most {MAX_HEADERS} keys")
    out: "dict[str, str]" = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key_re.match(key):
            raise EntryError(f"{where} has an invalid key")
        if not isinstance(value, str) or not value_check(value):
            raise EntryError(f"{where}[{key}] has an invalid value")
        out[key] = value
    return out


def _name_set(raw: object, where: str, item_re: "re.Pattern[str]") -> "frozenset[str]":
    if not isinstance(raw, list) or len(raw) > _MAX_LIST:
        raise EntryError(f"{where} must be a list of at most {_MAX_LIST} names")
    if any(not isinstance(item, str) or not item_re.match(item) for item in raw):
        raise EntryError(f"{where} has an invalid name")
    return frozenset(raw)


def resolve_preset(name: str) -> "frozenset[str]":
    """The metric names a preset stands for. ``KeyError`` for an unknown one."""
    if name not in ALLOW_PRESETS:
        raise KeyError(name)
    # Deferred: product_metrics is stdlib-only, but this keeps config-only
    # readers of this module from importing the metrics catalog at all.
    from kiro_crew.metrics.product_metrics import PRODUCT_METRICS

    return PRODUCT_METRICS


def _allow(raw: object, where: str) -> "Optional[frozenset[str]]":
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            return resolve_preset(raw)
        except KeyError:
            raise EntryError(
                f"{where} names an unknown preset; known presets: {sorted(ALLOW_PRESETS)}"
            ) from None
    return _name_set(raw, where, _METRIC_RE)


def _endpoint(raw: object, where: str) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_ENDPOINT_LEN:
        raise EntryError(f"{where} must be a URL of 1 to {MAX_ENDPOINT_LEN} characters")
    value = raw.strip()
    try:
        parts = urlsplit(value)
    except ValueError:
        raise EntryError(f"{where} is not a URL") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise EntryError(f"{where} must be an http:// or https:// URL")
    if any(c.isspace() for c in value):
        raise EntryError(f"{where} contains whitespace")
    return value


def parse_entry(
    raw: object, *, label: str, source: str, default_name: str = "entry"
) -> EndpointEntry:
    """Check one raw entry and return it as an :class:`EndpointEntry`.

    *label* names the entry in error messages before its ``name`` is known (for
    example ``telemetry.otlp_endpoints[2]``); *default_name* is its name when it
    gives none. Raises :class:`EntryError`. ``enabled`` is not read here: a caller
    skips a disabled entry before parsing it, so a disabled entry never has to be
    valid.
    """
    if not isinstance(raw, Mapping):
        raise EntryError(f"{label} must be an object")
    unknown = sorted(str(k) for k in raw if k not in _ENTRY_KEYS)
    # Only boolean false skips an entry before this point, so any other value here
    # (a string "false", 0) is refused rather than read as on: a mistyped opt-out
    # must not export.
    if not isinstance(raw.get("enabled", True), bool):
        raise EntryError(f"{label}.enabled must be true or false")
    # A literal header value would be a credential in config.json, which the
    # dashboard and the agent can read. Refused with its own message, ahead of the
    # unknown-key check, so the warning says where the value should go instead.
    if "headers" in raw:
        raise EntryError(
            f"{label}.headers is not supported; put each value in an environment "
            "variable and name it in headers_env"
        )
    if unknown:
        raise EntryError(f"{label} has unknown key(s) {unknown}")
    name = raw.get("name", default_name)
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise EntryError(f"{label}.name must match {_NAME_RE.pattern}")
    temporality = raw.get("temporality", "")
    if not isinstance(temporality, str) or temporality not in TEMPORALITIES:
        raise EntryError(f"{name}.temporality must be one of {sorted(TEMPORALITIES)}")
    point_attributes = _str_map(
        raw.get("point_attributes"),
        f"{name}.point_attributes",
        _NAME_RE,
        lambda v: bool(_POINT_VALUE_RE.match(v)),
    )
    # The reviewed key set is checked here as well as where the reader is built,
    # so a refused key shows up as a refused ENTRY in the log and the panel.
    from kiro_crew.metrics.destination_filter import validate_point_attributes

    try:
        validate_point_attributes(point_attributes)
    except ValueError as exc:
        raise EntryError(f"{name}.point_attributes: {exc}") from None
    resource_raw = raw.get("resource_allow")
    return EndpointEntry(
        name=name,
        endpoint=_endpoint(raw.get("endpoint"), f"{name}.endpoint"),
        source=source,
        headers_env=MappingProxyType(
            _str_map(
                raw.get("headers_env"),
                f"{name}.headers_env",
                _HEADER_RE,
                lambda v: bool(_ENV_RE.match(v)),
            )
        ),
        temporality=temporality,
        allow=_allow(raw.get("allow"), f"{name}.allow"),
        point_attributes=MappingProxyType(point_attributes),
        resource_allow=(
            None
            if resource_raw is None
            else _name_set(resource_raw, f"{name}.resource_allow", _RESOURCE_KEY_RE)
        ),
    )


def _is_disabled(raw: object) -> bool:
    return isinstance(raw, Mapping) and raw.get("enabled", True) is False


def _parse_list(rows: object, *, source: str, prefix: str, short: str) -> "list[EndpointEntry]":
    """Parse *rows*, dropping each bad or disabled one; names stay unique."""
    if rows is None:
        return []
    if not isinstance(rows, (list, tuple)):
        # Reached by a TelemetryConfig built in code. A config.json value of the
        # wrong type never gets here: the schema check on load warns about it by
        # path and drops it, as it does for every other mistyped field.
        _warn_once(f"{prefix}:type", "%s is not a list; ignored", prefix)
        return []
    if len(rows) > MAX_ENTRIES:
        _warn_once(
            f"{prefix}:count",
            "%s has %d entries; only the first %d are used",
            prefix,
            len(rows),
            MAX_ENTRIES,
        )
    out: "list[EndpointEntry]" = []
    for index, raw in enumerate(list(rows)[:MAX_ENTRIES]):
        if _is_disabled(raw):
            continue
        label = f"{prefix}[{index}]"
        try:
            entry = parse_entry(raw, label=label, source=source, default_name=f"{short}.{index}")
        except EntryError as exc:
            _warn_once(f"{label}:{exc}", "OTLP endpoint entry ignored: %s", exc)
            continue
        except Exception:
            # A shape no check above anticipated. Skipped like any malformed entry
            # rather than raised: one bad entry must not stop collection or the
            # other entries. The message names no value.
            _warn_once(f"{label}:unexpected", "OTLP endpoint entry ignored: %s is malformed", label)
            continue
        out.append(entry)
    return out


def _dedupe(entries: "Iterable[EndpointEntry]") -> "list[EndpointEntry]":
    seen: "set[str]" = set()
    out: "list[EndpointEntry]" = []
    for entry in entries:
        if entry.name in seen:
            _warn_once(
                f"dup:{entry.source}:{entry.name}",
                "OTLP endpoint entry %r appears more than once; the first one is used",
                entry.name,
            )
            continue
        seen.add(entry.name)
        out.append(entry)
    return out


def config_entries(cfg: object) -> "tuple[EndpointEntry, ...]":
    """The operator's entries: the legacy string first, then the list."""
    entries: "list[EndpointEntry]" = []
    legacy = str(getattr(cfg, "otlp_endpoint", "") or "").strip()
    if legacy:
        # Not validated: the legacy value has always been passed to the exporter
        # as written, and refusing one that worked yesterday would be a silent
        # egress change. It carries no narrowing, exactly as before.
        entries.append(EndpointEntry(name=LEGACY_NAME, endpoint=legacy, source=SOURCE_LEGACY))
    entries.extend(
        _parse_list(
            getattr(cfg, "otlp_endpoints", None),
            source=SOURCE_CONFIG,
            prefix="telemetry.otlp_endpoints",
            short="otlp_endpoints",
        )
    )
    return tuple(_dedupe(entries))


def to_destination(entry: EndpointEntry) -> "OtlpDestination":
    """The descriptor the core builds a reader for. Metrics only."""
    from kiro_crew.platform.interfaces import OtlpDestination

    return OtlpDestination(
        name=entry.name,
        endpoint=entry.endpoint,
        signals=frozenset({"metrics"}),
        allow=entry.allow,
        point_attributes=entry.point_attributes,
        resource_allow=entry.resource_allow,
        headers_env=entry.headers_env,
        temporality=entry.temporality,
        # The process-wide OTel header and credential variables belong to the one
        # collector they were set for, which before the list existed was the
        # legacy entry. A list entry gets only what it names in headers_env.
        process_env_credentials=entry.source == SOURCE_LEGACY,
    )


def config_destinations(cfg: object) -> "tuple[OtlpDestination, ...]":
    """One destination per operator entry, legacy string first. Empty = no egress."""
    return tuple(to_destination(entry) for entry in config_entries(cfg))


@dataclass(frozen=True)
class Resolved:
    """One destination, with what the Privacy panel shows about it."""

    destination: "OtlpDestination"
    source: str
    #: True when config cannot turn it off: the provider returned it from code.
    locked: bool


def label_sources(supplied: "Iterable[OtlpDestination]", cfg: object) -> "tuple[Resolved, ...]":
    """Say where each of the provider's destinations came from.

    A destination EQUAL to the descriptor an operator entry produces came from
    config, and each entry is matched once. Any other one was returned by the
    provider's own code, so config cannot remove it -- including one that only
    shares an entry's name: matching by name alone would label a build's collector
    as the operator's, and promise an off switch that does not stop it.
    """
    pending = [(to_destination(entry), entry.source) for entry in config_entries(cfg)]
    out: "list[Resolved]" = []
    for dest in supplied:
        source = SOURCE_PROVIDER
        for index, (descriptor, entry_source) in enumerate(pending):
            if descriptor == dest:
                source = entry_source
                del pending[index]
                break
        out.append(Resolved(dest, source, source == SOURCE_PROVIDER))
    return tuple(out)
