"""The ``telemetry`` and ``wakatime`` sections, and the usage-beacon endpoint default.

An owner of the :mod:`kiro_crew.config.sections` facade; it never imports the facade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit as _urlsplit

from kiro_crew.config.fields import _meta

logger = logging.getLogger("kiro_crew.config.loader")


# Default endpoint for the anonymous usage beacon (see kiro_crew/beacon.py).
# Lives here with the other config defaults so beacon.py adds no import edge
# into the config package. Setting the field to "" disables the beacon outright.
_DEFAULT_BEACON_ENDPOINT = "https://d175o3ylxqum0e.cloudfront.net"


@dataclass
class TelemetryConfig:
    """Metrics telemetry settings (Wave 0 trunk).

    Default OFF: when disabled, metric call sites are cheap no-ops and nothing is
    written or exported (byte-identical to no telemetry), mirroring the
    ``mcp_gateway.enabled`` / ``skills.lazy_load`` opt-in convention. When
    enabled, a local-first JSONL sink under ``~/.kiro/crew/metrics`` is activated;
    remote / OTLP egress is a separate opt-in requiring ``kirocrew[otlp]``.
    """

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Main switch for Kiro Crew metrics telemetry. Off by default: metric "
            "call sites are no-ops and nothing is written. When on, a local-first "
            "JSONL sink under ~/.kiro/crew/metrics is enabled (no network egress).",
        ),
    )
    local_dir: str = field(
        default="",
        metadata=_meta(
            "Local Metrics Dir",
            "Directory for local JSONL metric shards. Empty = ~/.kiro/crew/metrics. "
            "Supports ~ expansion.",
        ),
    )
    export_interval_seconds: int = field(
        default=60,
        metadata=_meta(
            "Export Interval (s)",
            "How often the local exporter flushes aggregated metrics to disk (>=1).",
        ),
    )
    retention_days: int = field(
        default=0,
        metadata=_meta(
            "Retention (days)",
            "Prune local JSONL metric shards older than this many days on each "
            "export cycle. 0 disables age-based pruning. Bounds on-disk telemetry "
            "growth.",
        ),
    )
    max_total_mb: int = field(
        default=0,
        metadata=_meta(
            "Max Total Size (MB)",
            "Opportunistic directory budget for local metric shards. Closed shards "
            "are pruned oldest-first; protected active writers can temporarily exceed "
            "the budget. 0 disables the size cap.",
        ),
    )
    otlp_endpoint: str = field(
        default="",
        metadata=_meta(
            "OTLP Endpoint",
            "Opt-in OpenTelemetry OTLP/HTTP metrics endpoint (e.g. "
            "http://localhost:4318/v1/metrics). EMPTY = no network egress "
            "(default). When set, aggregated metrics are ALSO pushed to this "
            "collector in addition to the local JSONL sink; requires the "
            "OTLP exporter from the otlp package extra to be installed. Kept "
            "for compatibility: when set it is the first entry of the "
            "OTLP Endpoints list below.",
            sensitive=True,
        ),
    )
    otlp_endpoints: list[dict[str, Any]] = field(
        default_factory=list,
        metadata=_meta(
            "OTLP Endpoints",
            "Opt-in list of OpenTelemetry OTLP/HTTP metrics collectors, each its "
            "own destination, sent to IN ADDITION to the local JSONL sink and to "
            "OTLP Endpoint above. EMPTY = no network egress (default). Each entry "
            "is {endpoint, name, enabled, headers_env, temporality, allow, "
            "point_attributes, resource_allow}: endpoint is an http(s) URL; "
            "headers_env maps a header name to an environment variable that holds "
            "its value, so no secret is stored here (there is no key for a literal "
            "header value, and an entry with one is skipped). An entry sends only "
            "those headers: the OTEL_EXPORTER_OTLP_HEADERS variables apply to "
            "OTLP Endpoint above, not to these entries. temporality "
            "is '' (core default), 'cumulative' or 'delta'; allow is a list of "
            "metric names or the preset 'PRODUCT_METRICS', and without it the "
            "collector gets every metric; point_attributes adds fixed labels "
            "from a reviewed key set (ClientType); resource_allow keeps only the "
            "named resource keys. Set enabled to false to turn an entry off. "
            "Every entry obeys the 'enabled' switch above and the "
            "capabilities.telemetry otlp_hosts policy. A malformed entry is "
            "skipped with a warning. See docs/guides/telemetry-otlp-export.md.",
            sensitive=True,
        ),
    )
    beacon_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Anonymous Usage Beacon",
            "Anonymous daily heartbeat so maintainers can see how many "
            "copies are actively running, which versions are in use, and "
            "which distribution channels they came from. Sends "
            "EXACTLY five fields, at most once per day: a random installation "
            "id, app release (major.minor.patch only — build stamps are "
            "stripped), Python minor version, distribution channel, and a "
            "first-run bit. NEVER sends prompts, "
            "model output, file contents, paths, repo names, credentials, "
            "hostname, username, IP address, operating system, CPU "
            "architecture, release channel, or governance posture. "
            "Automatically suppressed in CI "
            "and for a non-default KIROCREW_HOME. Opt out with "
            "KIROCREW_TELEMETRY_DISABLED=1 or by turning this off; an "
            "enterprise policy can also pin it off via the "
            "capabilities.telemetry governance scope, which this switch cannot "
            "override. Independent "
            "of the 'enabled' switch above, which is local-only metrics "
            "collection and still never egresses.",
        ),
    )
    beacon_endpoint: str = field(
        default=_DEFAULT_BEACON_ENDPOINT,
        metadata=_meta(
            "Beacon Endpoint",
            "HTTPS base URL that receives the anonymous heartbeat. EMPTY = no "
            "beacon is ever sent, regardless of the toggle above. Must be "
            "https:// (a plaintext heartbeat would reveal which hosts run this "
            "software to any on-path observer); a non-https value is cleared.",
        ),
    )

    def __post_init__(self) -> None:
        if self.export_interval_seconds < 1:
            logger.warning("export_interval_seconds %d < 1, using 1", self.export_interval_seconds)
            object.__setattr__(self, "export_interval_seconds", 1)
        if self.retention_days < 0:
            logger.warning("retention_days %d < 0, using 0 (no age pruning)", self.retention_days)
            object.__setattr__(self, "retention_days", 0)
        if self.max_total_mb < 0:
            logger.warning("max_total_mb %d < 0, using 0 (no size cap)", self.max_total_mb)
            object.__setattr__(self, "max_total_mb", 0)
        # Fail CLOSED on an unusable beacon endpoint: clear it rather than send
        # the heartbeat in plaintext or defer a parse failure to the send path.
        # Enforced here so the invariant holds for every consumer of the config.
        # A startswith("https://") test is NOT sufficient — it accepts a host
        # containing whitespace, which urlopen then rejects with
        # http.client.InvalidURL from deep inside the beacon thread. Parse it the
        # same way the send path does, and require a whitespace-free netloc.
        endpoint = self.beacon_endpoint.strip()
        if endpoint:
            try:
                parts = _urlsplit(endpoint)
                usable = (
                    parts.scheme == "https"
                    and bool(parts.netloc)
                    and not any(c.isspace() for c in parts.netloc)
                )
            except ValueError:
                usable = False
            if not usable:
                logger.warning("beacon_endpoint is not a usable https:// URL; beacon disabled")
                endpoint = ""
        if endpoint != self.beacon_endpoint:
            object.__setattr__(self, "beacon_endpoint", endpoint)


@dataclass
class WakaTimeConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the WakaTime integration (send coding-activity heartbeats "
            "and read back stats). Requires the WAKATIME_API_KEY credential "
            "stored in the dashboard secrets vault.",
            tags=["wakatime"],
        ),
    )
    api_base_url: str = field(
        default="",
        metadata=_meta(
            "API Base URL",
            "Override the WakaTime API base URL for a self-hosted, "
            "API-compatible backend (Wakapi, Hackatime). Empty uses the public "
            "WakaTime API at https://wakatime.com/api/v1.",
            tags=["wakatime"],
        ),
    )
    send_heartbeats: bool = field(
        default=False,
        metadata=_meta(
            "Send coding-activity heartbeats",
            "Send a heartbeat to WakaTime after each agent turn that edited "
            "files or ran a command, so your Kiro Crew coding time shows up in "
            "WakaTime alongside your editor. Off by default: sending activity "
            "outward is a separate opt-in from reading your own stats. Requires "
            "the WakaTime integration to be enabled. Covers dashboard chat and "
            "dashboard-linked Slack threads; the other messaging channels "
            "(Telegram, Discord, Teams, WhatsApp and the rest), the task "
            "runner, subagents, installed apps, and the standalone kirocrew "
            "chat CLI each run their own turn loop and do not emit heartbeats "
            "yet. Incognito and temporary sessions never send one: they keep "
            "no record of the chat, and a heartbeat is an external record of "
            "it.",
            tags=["wakatime"],
        ),
    )
