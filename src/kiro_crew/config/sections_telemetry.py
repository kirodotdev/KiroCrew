"""The ``telemetry`` and ``wakatime`` sections, and the usage-beacon endpoint default.

An owner of the :mod:`kiro_crew.config.sections` facade; it never imports the facade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit as _urlsplit

from kiro_crew.config.fields import _meta

logger = logging.getLogger("kiro_crew.config.loader")


# Default endpoint for the anonymous usage beacon (see kiro_crew/beacon.py).
# Lives here with the other config defaults so beacon.py adds no import edge
# into the config package. Setting the field to "" disables the beacon outright.
_DEFAULT_BEACON_ENDPOINT = "https://d175o3ylxqum0e.cloudfront.net"

# Disk bounds for the local metric recorder, which is on by default.
DEFAULT_METRICS_RETENTION_DAYS = 30
DEFAULT_METRICS_MAX_TOTAL_MB = 100


@dataclass
class TelemetryConfig:
    """Metrics telemetry settings.

    Default ON for local recording: a local-first JSONL sink under
    ``~/.kiro/crew/metrics`` records how the app is used, bounded on disk by the
    ``retention_days`` and ``max_total_mb`` defaults below. Every user is told in
    the dashboard's Privacy chapter (or by a one-line notice on a CLI or headless
    start) and can turn it off there, in Settings > Privacy, with ``kirocrew
    telemetry disable``, ``KIROCREW_TELEMETRY=0`` or ``DO_NOT_TRACK=1``. A value
    written in either config file wins over the default, and an install that
    turned the heartbeat off without setting this switch loads as off. When off,
    metric call sites are cheap no-ops and nothing is written. Remote / OTLP
    egress is a separate opt-in, and needs recording turned on explicitly (see
    ``metrics.provider.outbound_consented``).
    """

    enabled: bool = field(
        default=True,
        metadata=_meta(
            "Enabled",
            "Main switch for local usage metrics. On by default: a local-first JSONL "
            "sink under ~/.kiro/crew/metrics records how long things take and how "
            "often features are used (no prompts, files or credentials), with no "
            "network egress. Turn it off here, in Settings > Privacy, with kirocrew "
            "telemetry disable, KIROCREW_TELEMETRY=0 or DO_NOT_TRACK=1. When off, "
            "metric call sites are no-ops and nothing is written.",
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
    # Non-zero defaults because recording is on by default: an uncapped
    # recorder grows on disk without end. 30 days covers the Telemetry panel's
    # 14-day window with room to spare; 100 MB is several weeks of a heavy
    # user's shards (one measured busy host wrote about 11 MB a day).
    retention_days: int = field(
        default=DEFAULT_METRICS_RETENTION_DAYS,
        metadata=_meta(
            "Retention (days)",
            "Prune local JSONL metric shards older than this many days on each "
            f"export cycle. Default {DEFAULT_METRICS_RETENTION_DAYS}. 0 disables "
            "age-based pruning. Bounds on-disk telemetry growth.",
        ),
    )
    max_total_mb: int = field(
        default=DEFAULT_METRICS_MAX_TOTAL_MB,
        metadata=_meta(
            "Max Total Size (MB)",
            "Opportunistic directory budget for local metric shards. Closed shards "
            "are pruned oldest-first; protected active writers can temporarily exceed "
            f"the budget. Default {DEFAULT_METRICS_MAX_TOTAL_MB}. 0 disables the "
            "size cap.",
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
            "OTLP exporter from the otlp package extra to be installed.",
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
            "collection and egresses only to a configured export destination "
            "after recording was turned on explicitly.",
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
