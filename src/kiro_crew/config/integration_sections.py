"""Sections for the external processes, hosts and sources Kiro Crew reaches.

Owns the DTOs and defaults for ``mcp`` and ``mcp_gateway`` (including the MCP stub
roster readers the gateway seed shares), ``instances``, ``tunnel``, ``publish``,
``computer_use`` and the external app ``registries``. The computer-use and
instance bounds come from ``computer_use.types`` and ``instances.constants``.
``config.sections`` re-exports every name; this module never imports it, the
loader, schema or validation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from kiro_crew.computer_use.types import DEFAULT_ATTACH_SCREENSHOT as _CU_DEFAULT_ATTACH_SCREENSHOT
from kiro_crew.computer_use.types import DEFAULT_MAX_TREE_DEPTH as _CU_DEFAULT_MAX_TREE_DEPTH
from kiro_crew.computer_use.types import DEFAULT_MAX_TREE_NODES as _CU_DEFAULT_MAX_TREE_NODES
from kiro_crew.computer_use.types import (
    DEFAULT_SCREENSHOT_JPEG_QUALITY as _CU_DEFAULT_SCREENSHOT_JPEG_QUALITY,
)
from kiro_crew.computer_use.types import DEFAULT_SCREENSHOT_MAX_PX as _CU_DEFAULT_SCREENSHOT_MAX_PX
from kiro_crew.computer_use.types import DEFAULT_TEXT_LIMIT as _CU_DEFAULT_TEXT_LIMIT
from kiro_crew.config.fields import _meta, _safe_bool, _safe_dict, _safe_list
from kiro_crew.instances.constants import CONNECT_TIMEOUT_CEILING_SECS as _CONNECT_TIMEOUT_CEILING
from kiro_crew.instances.constants import DEFAULT_MAX_RECOVERY_ATTEMPTS as _DEFAULT_MAX_RECOVERY
from kiro_crew.instances.constants import DEFAULT_PROBE_FAILURE_THRESHOLD as _DEFAULT_PROBE_FAILS
from kiro_crew.instances.constants import DEFAULT_RECOVER_BACKOFF_MAX_SECS as _DEFAULT_BACKOFF_MAX
from kiro_crew.instances.constants import DEFAULT_SSH_COMPRESSION as _DEFAULT_SSH_COMPRESSION
from kiro_crew.instances.constants import DEFAULT_TUNNEL_BASE_PORT as _DEFAULT_TUNNEL_BASE_PORT
from kiro_crew.instances.constants import DEFAULT_WARM_SET_CAP as _DEFAULT_WARM_SET_CAP
from kiro_crew.instances.constants import MAX_RECOVERY_ATTEMPTS_CEILING as _MAX_RECOVERY_CEILING
from kiro_crew.instances.constants import MINT_TIMEOUT_CEILING_SECS as _MINT_TIMEOUT_CEILING
from kiro_crew.instances.constants import MINT_TIMEOUT_FLOOR_SECS as _MINT_TIMEOUT_FLOOR
from kiro_crew.instances.constants import (
    RECOVER_BACKOFF_MAX_CEILING_SECS as _RECOVER_BACKOFF_CEILING,
)
from kiro_crew.instances.constants import WARM_SET_CAP_AUTO as _WARM_SET_CAP_AUTO

logger = logging.getLogger("kiro_crew.config.loader")


def _resolve_stub_roster(mcp_gateway_data: dict) -> list[str]:
    """The stub set as CONFIGURED, before the operator's own deviations.

    This is the layer a distribution owns: an edition that wants its known
    servers stubbed out of the box ships them here, and keeps shipping them as
    the roster grows. Operator deviations live in ``stub_overrides`` and are
    applied over this by :func:`_resolve_stub_servers` — which is what lets the
    two move independently. Read this directly ONLY to answer "what does the
    roster say"; everything that wants the set actually in effect wants
    :func:`_resolve_stub_servers`.

    ``poolable_servers`` is the deprecated spelling and is consulted ONLY when
    ``stub_servers`` is absent from the file. Key presence, not truthiness, is
    the test: an operator who wrote ``stub_servers: []`` chose to stub nothing,
    and silently falling back to a stale ``poolable_servers`` would re-stub
    servers they had just cleared.

    The migration reproduces the stub set the operator was ALREADY RUNNING, which
    is why it is also conditional on ``enabled``. Before the stub became its own
    per-server decision, the broker was gated on ``enabled`` alone, so a config
    with ``enabled: false`` produced no broker, no overlay and no stub no matter
    what ``poolable_servers`` held. Migrating that list unconditionally would
    hand such an install a daemon and a stub process per server on upgrade —
    inventing the very topology change this design exists to make optional. An
    operator whose gateway was off keeps nothing running and opts in per server.
    """
    if "stub_servers" in mcp_gateway_data:
        source = mcp_gateway_data.get("stub_servers")
    elif _safe_bool(mcp_gateway_data.get("enabled", False), False):
        source = mcp_gateway_data.get("poolable_servers")
    else:
        source = None
    return [s for s in _safe_list(source) if isinstance(s, str) and s]


def _resolve_stub_overrides(mcp_gateway_data: dict) -> dict[str, bool]:
    """The operator's per-server stub DECISIONS — what they changed, not the result.

    Sparse by construction, and that is the whole point. A flat resulting list
    can only be REPLACED: an operator who unstubs one server out of a shipped
    roster would have to restate the survivors, and that restated list then
    shadows the roster permanently — the next name the distribution adds never
    reaches them, because their file already answers the question. Recording the
    DECISION instead leaves every server they did not speak about following the
    roster.

    Absent means "no opinion", which is why a key whose value equals the roster's
    answer is pruned on write rather than stored: an override that agrees with
    its base is indistinguishable from silence in effect, but not in future —
    stored, it would freeze that server against a later roster change, which is
    the shadowing this map exists to avoid.

    Non-bool values are dropped rather than coerced. A truthy string here would
    be an operator's typo, and guessing which way they meant it is worse than
    leaving that server on the roster's answer.
    """
    raw = _safe_dict(mcp_gateway_data.get("stub_overrides"))
    return {
        name: value
        for name, value in raw.items()
        if isinstance(name, str) and name and isinstance(value, bool)
    }


def _resolve_stub_servers(mcp_gateway_data: dict) -> list[str]:
    """Which MCP servers are given a stub, roster and operator decisions together.

    The set in EFFECT: :func:`_resolve_stub_roster` supplies the configured base
    and :func:`_resolve_stub_overrides` the operator's deviations from it, so a
    distribution can grow the roster without overwriting a choice the operator
    made, and the operator can turn any single server off without pinning
    themselves to today's roster.

    Roster order is preserved (the resolver has always handed back what the file
    held, duplicates included, and ``_freeze_stub_servers`` is what normalizes on
    write); servers added by an override are appended in sorted order, because
    they have no position in the file to preserve.
    """
    roster = _resolve_stub_roster(mcp_gateway_data)
    overrides = _resolve_stub_overrides(mcp_gateway_data)
    if not overrides:
        return roster
    resolved = [name for name in roster if overrides.get(name, True)]
    already = set(resolved)
    resolved.extend(name for name, on in sorted(overrides.items()) if on and name not in already)
    return resolved


@dataclass
class PublishConfig:
    """Operator-facing controls for artifact publishing.

    Publishing an artifact to an external destination is provided by a
    ``publish_provider`` registered through the ``platform`` CPP seam
    (``PublishRegistry``). The public edition registers NO provider, so
    publishing is unavailable regardless of these settings; a companion edition
    registers a concrete destination.

    This ``allowed_destinations`` list is the STANDALONE operator's narrowing
    knob (default-open, mirroring ``SlackConfig.allowed_enterprise_ids``): empty
    means "allow every registered destination". It is enforced at the publish
    handler chokepoint IN ADDITION TO the governance ceiling
    (``capabilities.publish``) — like the Slack allowlist, config can only
    NARROW, never widen: a destination denied by the enterprise policy cannot be
    re-permitted here (the security policy is never merged from ``config.json``).
    """

    allowed_destinations: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Publish Destinations",
            "Publish-provider ids the operator permits (registry keys). "
            "Empty list allows all registered destinations (default-open). "
            "Cannot widen past the enterprise governance ceiling.",
            tags=["publish"],
        ),
    )
    #: Extra filesystem roots (beyond the user's home dir) that an artifact may
    #: be relocated to point at (``artifact_relocate`` / the ``artifact_move`` MCP
    #: tool). Relocate is confined to the user home by default so an agent cannot
    #: aim an artifact at ``/etc/passwd`` or another user's files and exfiltrate
    #: them via a later artifact GET; each entry here widens the allowed set to an
    #: additional absolute root (e.g. a shared project dir). Paths are expanded +
    #: realpath-resolved; a relocate target must resolve under the home dir OR one
    #: of these roots (AND still pass the sensitive-path denylist).
    relocate_roots: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Artifact Relocate Roots",
            "Extra absolute filesystem roots an artifact may be relocated into, "
            "beyond your home directory. Empty = home-only (the secure default). "
            "The sensitive-path denylist (~/.aws, ~/.ssh, ~/.kiro/crew, …) still "
            "applies inside every allowed root.",
            tags=["artifacts"],
        ),
    )


@dataclass
class ExternalRegistryConfig:
    """An external app registry source (org-owned repo with app.json files)."""

    name: str = field(
        default="",
        metadata=_meta("Name", "Human-readable registry name (e.g. 'identityservices')."),
    )
    repo: str = field(
        default="",
        metadata=_meta("Repo", "Git URL of the repo containing apps (https or ssh)."),
    )
    branch: str = field(
        default="main",
        metadata=_meta("Branch", "Git branch to read from."),
    )
    label: str = field(
        default="",
        metadata=_meta(
            "Label",
            "Display name shown instead of the registry id (e.g. 'Community apps' "
            "for the id 'community'). DISPLAY ONLY: the id in `name` stays the "
            "identity every cache path and every installed app's `_registry` tag "
            "is keyed by, so a label change never moves an app or re-fetches an "
            "index. Empty means the id is shown as-is. Setting it HERE has no "
            "effect, for the same reason `trust` does not: this file is "
            "agent-writable, so only a build-pinned row may claim one.",
        ),
    )
    review: str = field(
        default="",
        metadata=_meta(
            "Review",
            "How thoroughly the listings in this registry were reviewed before "
            "being published, which is what the UI tells the user. 'curated' "
            "means the owning team reviewed each listing; 'community' means "
            "contributors listed apps after a lighter review, so nothing here is "
            "vetted; empty (the default) makes no claim either way and renders "
            "exactly as it did before this field existed. It changes NO security "
            "posture: `trust` alone selects the credential posture for cloning, "
            "so a 'curated' registry at the untrusted index tier still clones "
            "credential-free. Setting it HERE has no effect (see `label`): only a "
            "build-pinned row may claim a tier.",
            enum=["", "curated", "community"],
        ),
    )
    trust: str = field(
        default="index",
        metadata=_meta(
            "Trust",
            "How much a registry's INDEX is trusted, which selects the credential "
            "posture for cloning the apps it lists. 'index' (the default) treats the "
            "index as untrusted content: every app it lists is cloned credential-free "
            "so a hostile entry cannot read a private sibling repo with this machine's "
            "git identity. 'owner' means the index is under change control the build "
            "owns, so its apps may clone with this machine's credentials. Setting it "
            "HERE has no effect: the trusted tier is honoured only for registries the "
            "build supplies, because this file is agent-writable and a tier read from "
            "it would not be your assertion. A value other than 'index' on a "
            "configured registry is read as 'index'.",
        ),
    )


@dataclass
class ComputerUseConfig:
    """Computer-use DISPLAY and LIMIT knobs — deliberately no ``enabled`` field.

    The primary enable is NOT here. It lives on the keystone
    ``computer_use.json`` (see :func:`computer_use_state_path`) because turning
    computer use on grants full desktop observation plus input synthesis, which
    is a security ceiling rather than a preference: ``config.json`` is writable
    by an auto-approved agent shell (``is_sensitive_bash_command`` does NOT block
    ``echo … > config.json``), so an enable stored here could be flipped by
    prompt injection. Adding an ``enabled`` field to this dataclass would
    silently re-open that hole — do not.

    Everything modelled here is safe for the agent to read and, at worst,
    annoying for it to change: how many accessibility nodes one walk returns, how
    deep it goes, how much text per node, and the screenshot's size/quality. The
    ceilings (``*_LIMIT`` in ``computer_use.types``) are enforced independently by
    the MCP tool schemas, so a hand-edited config cannot ask for an unbounded
    walk.
    """

    max_tree_nodes: int = field(
        default=_CU_DEFAULT_MAX_TREE_NODES,
        metadata=_meta(
            "Max Tree Nodes",
            "Accessibility nodes one window walk may return before truncating.",
        ),
    )
    max_tree_depth: int = field(
        default=_CU_DEFAULT_MAX_TREE_DEPTH,
        metadata=_meta("Max Tree Depth", "How deep one accessibility walk descends."),
    )
    text_limit: int = field(
        default=_CU_DEFAULT_TEXT_LIMIT,
        metadata=_meta("Text Limit", "Characters kept per element title/value."),
    )
    attach_screenshot: bool = field(
        default=_CU_DEFAULT_ATTACH_SCREENSHOT,
        metadata=_meta(
            "Attach Screenshots",
            "Capture the target window and relay the image path alongside the tree. "
            "The accessibility tree is always the primary channel.",
        ),
    )
    screenshot_max_px: int = field(
        default=_CU_DEFAULT_SCREENSHOT_MAX_PX,
        metadata=_meta(
            "Screenshot Width",
            "Longest edge of the downscaled screenshot, in pixels.",
        ),
    )
    screenshot_jpeg_quality: int = field(
        default=_CU_DEFAULT_SCREENSHOT_JPEG_QUALITY,
        metadata=_meta("Screenshot Quality", "JPEG quality 1-100 for the screenshot."),
    )
    cursor_motion: bool = field(
        default=False,
        metadata=_meta(
            "Cursor Motion",
            "Draw a visible cursor gliding to each target before a real-pointer "
            "click, so the operator can see what the agent is doing. macOS only; "
            "purely visual and never a permit — the drawn cursor is not the pointer, "
            "and turning this on grants no new capability.",
        ),
    )


@dataclass
class McpGatewayConfig:
    """Sidecar MCP broker daemon — shares MCP backends across sessions."""

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Share MCP Backends",
            "Let sessions with an identical server configuration share one MCP "
            "server process instead of each getting its own. Off, every session "
            "gets its own backend — the same process topology as running without "
            "the broker. Either this or MCP Apps starts the broker; see "
            "docs/architecture/design-notes/mcp-stub-decoupling.md. "
            "Default False — opt-in.",
        ),
    )
    apps_enabled: bool = field(
        default=True,
        metadata=_meta(
            "MCP Apps (retired, opt-out still honoured)",
            "RETIRED GOING FORWARD, but a stored `false` KEEPS ITS OPT-OUT. Nothing "
            "writes this key any more and MCP Management does not surface it: MCP "
            "Apps capability follows whether a server gets a stub, because the stub "
            "is what carries the render and callback path, so a preference cannot "
            "grant it. It can still WITHHOLD it — a released version treated "
            "`false` here as a trustworthy opt-out, so an operator who turned MCP "
            "Apps off stays off (tightest-wins: it beats KIROCREW_MCP_APPS=1, and an "
            "unreadable config fails closed). Absent defaults True, so 'not "
            "configured' is not an opt-out. To GET server-authored UI, turn on the "
            "server's stub in MCP Management — and clear a stored `false` here if "
            "you have one. The only other MCP Apps preference is where it renders "
            "(dashboard.mcp_app_panel). "
            "See docs/architecture/design-notes/mcp-stub-decoupling.md.",
        ),
    )
    forward_declared_env: bool = field(
        default=True,
        metadata=_meta(
            "Forward Declared Env",
            "Apply a pooled server's declared env (mcpServers.<name>.env) to the "
            "shared backend. Only non-secret keys are forwarded — rotating-secret "
            "and credential-prefixed keys are never applied to a shared backend, "
            "and gatewayd re-hashes the sidecar at spawn and forwards nothing on "
            "mismatch, so every forwarded key is one all co-tenants of that "
            "backend declared identically. Turn it OFF to make an env-declaring "
            "server run unwrapped (no stub, no pooling) instead.",
            restart=True,
        ),
    )
    socket_path: str = field(
        default="",
        metadata=_meta(
            "Socket Path",
            "Local endpoint for the broker. Empty -> "
            "$KIROCREW_HOME/mcp-gateway/gateway.sock. A unix socket at this path "
            "on POSIX; on Windows the path is not created, it only derives the "
            "named-pipe name and locates the lock file beside it.",
            restart=True,
        ),
    )
    overlay_dir: str = field(
        default="",
        metadata=_meta(
            "Overlay Dir",
            "Directory of rewritten agent JSON. Broker stubs from these specs are "
            "injected into each kiro-cli session via ACP session/new. "
            "Empty -> $KIROCREW_HOME/mcp-gateway/agents.",
            restart=True,
        ),
    )
    idle_timeout_secs: int = field(
        default=300,
        metadata=_meta(
            "Idle Timeout",
            "Seconds a refcount=0 MCP backend is kept before drain. Sizes the "
            "daemon's idle sweeper at startup, so it rides the broker's command "
            "line and a change needs a broker restart.",
            restart=True,
        ),
    )
    resolve_once_refresh_hours: int = field(
        default=24,
        metadata=_meta(
            "Pre-resolve Refresh",
            "Hours before an UNPINNED npm-launcher MCP server (an npx spec at "
            "@latest, a range, or no version) is re-resolved from the registry. "
            "Pre-resolving lets a launch exec the installed tree directly, so "
            "session start does no dependency resolution and needs no network; "
            "this is how often that resolution is refreshed so such a spec still "
            "tracks upstream. A spec pinned to an exact version ignores this -- "
            "re-asking about an exact version cannot change the answer. 0 "
            "re-resolves on every prefetch pass; a server with no resolution yet "
            "simply launches the way it does today.",
        ),
    )
    max_backends: int = field(
        default=64,
        metadata=_meta(
            "Max Backends",
            "Max concurrent pooled MCP backends before the pool refuses a new one. "
            "Must be >= the number of distinct (agent x server) backends that can be "
            "live at once: each agent keeps its own backend per server, so N concurrent "
            "agents with ~S servers each need N*S slots. Bounded by design: idle "
            "backends drain after idle_timeout_secs, so steady-state RAM tracks real "
            "concurrency, not this ceiling.",
            restart=True,
        ),
    )
    spawn_concurrency_initial: int = field(
        default=4,
        metadata=_meta(
            "Spawn Concurrency",
            "How many MCP backend spawn+initialize windows the broker runs at once, "
            "across every server and session (pooled, private and respawned alike). "
            "Further spawns wait their turn in FIFO order and the waiting stub is "
            "kept informed, so a burst of new sessions cold-starts its servers a few "
            "at a time instead of forking hundreds of processes against one disk. "
            "This is the starting value the adaptive controller moves between "
            "spawn_concurrency_min and spawn_concurrency_max. Distinct from "
            "max_backends, which bounds how many backends stay RESIDENT.",
            restart=True,
        ),
    )
    spawn_concurrency_min: int = field(
        default=1,
        metadata=_meta(
            "Spawn Concurrency Floor",
            "Lowest value the adaptive controller may cut spawn concurrency to "
            "under host pressure. At least 1: something always makes progress.",
            restart=True,
        ),
    )
    spawn_concurrency_max: int = field(
        default=8,
        metadata=_meta(
            "Spawn Concurrency Ceiling",
            "Highest value the adaptive controller may raise spawn concurrency to "
            "when spawns keep succeeding without pressure.",
            restart=True,
        ),
    )
    spawn_queue_wait_secs: int = field(
        default=600,
        metadata=_meta(
            "Spawn Queue Wait",
            "Longest a session's stub is held in the broker's spawn queue before it "
            "is refused for capacity. The default matches the stub's own reconnect "
            "budget (a constant, mcp_gateway/stub.py _RECONNECT_TOTAL_BUDGET_SECS): "
            "for that long kiro-cli's transport stays open and the server's tools "
            "stay listed. This is a ceiling on the wait, never the wait itself -- "
            "the daemon waits the smaller of this and the budget the stub asked "
            "for, less a margin, so the refusal always reaches a stub that is still "
            "listening. Raising this above 600 therefore buys a queued stub no "
            "extra wait, because what the stub asked for caps it first; raise that "
            "constant to wait longer. A spent wait is reported to the session as a "
            "typed error naming the class and a retry hint, never as a crashed "
            "server.",
            restart=True,
        ),
    )
    initialize_timeout_secs: int = field(
        default=10,
        metadata=_meta(
            "Initialize Timeout",
            "Seconds a freshly spawned backend has to answer its first MCP "
            "initialize once the session sends it. A backend that stays silent is "
            "failed and reaped so its slot frees; the broker holds the spawn "
            "permit for this same window. Raise it for servers whose startup is "
            "legitimately slow (large runtimes, remote resolution).",
            restart=True,
        ),
    )
    host_budget_max_procs: int = field(
        default=0,
        metadata=_meta(
            "Host Budget: Processes",
            "Ceiling on MCP backend processes the broker is answerable for on this "
            "host -- pooled, private and the per-session exec a stub runs when the "
            "broker cannot serve it, charged identically. 0 (default) derives it "
            "from available memory at broker start, never below max_backends.",
            restart=True,
        ),
    )
    host_budget_max_rss_mb: int = field(
        default=0,
        metadata=_meta(
            "Host Budget: Memory (MiB)",
            "Ceiling on the summed per-backend memory estimate the broker admits. "
            "0 (default) leaves memory to the process ceiling above.",
            restart=True,
        ),
    )
    host_budget_max_fds: int = field(
        default=0,
        metadata=_meta(
            "Host Budget: Descriptors",
            "Ceiling on the file descriptors the broker itself holds for backends "
            "(three pipes each). 0 (default) derives it from the broker's own "
            "open-file limit, leaving room for stub connections.",
            restart=True,
        ),
    )
    stub_servers: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Routed Servers",
            "MCP server names given a stub. The stub interposes a "
            "stub, which is what makes server-authored UI (MCP Apps) and backend "
            "sharing possible for that server — so it is the one per-server "
            "decision. Empty by default: an unstubbed server is launched by the "
            "session itself, the same process topology as running without the "
            "broker, and an empty list means no broker runs at all. Whether "
            "stubbed servers SHARE one backend is the separate global switch "
            "(mcp_gateway.enabled). Managed from MCP Management.",
            restart=True,
        ),
    )
    poolable_servers: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Poolable Servers (deprecated)",
            "DEPRECATED alias for stub_servers. Read only when stub_servers "
            "is absent, so a config written before the stub became the per-server "
            "decision keeps working: a server that was pooled already had a stub, "
            "so migrating it to the stub set preserves its behaviour. There is no "
            "per-server sharing switch any more — sharing is global over the "
            "stub set.",
            restart=True,
        ),
    )
    stub_overrides: dict[str, bool] = field(
        default_factory=dict,
        metadata=_meta(
            "Stub Overrides",
            "Per-server deviations from stub_servers: a name mapped to true is "
            "stubbed even when the roster omits it, false leaves it direct even "
            "when the roster carries it. Holds what you CHANGED, not the result, "
            "so a name you never touched keeps following the roster — which is "
            "what lets an edition that ships its own stub_servers grow that list "
            "without overwriting your choices, and lets you turn one server off "
            "without pinning yourself to today's roster. Written by MCP "
            "Management when a toggle disagrees with the roster, and dropped "
            "again when you toggle it back to agree. Empty by default.",
            restart=True,
        ),
    )
    #: The roster EXACTLY as the file states it, carried so a full-file rewrite
    #: can put it back.
    #:
    #: :attr:`stub_servers` above holds the EFFECTIVE set, because that is what all
    #: seven of its consumers want (routing, the page's rows, ``stub_count``, the
    #: doctor). But ``save()`` round-trips this dataclass through ``asdict``, so a
    #: field whose value differs from the file's is a landmine: emitting the
    #: effective set would rewrite ``stub_servers`` without the servers the operator
    #: opted out of, turning a reversible deviation into a permanent deletion from a
    #: layer that is not ours to edit -- and it would happen on any unrelated
    #: ``save()``. Carrying the roster lets :meth:`KiroCrewConfig.to_dict` emit the
    #: file's own value instead.
    #:
    #: Excluded from serialization (``repr=False``, popped by ``to_dict``) -- it is
    #: not a config key and must never be written back as one. The leading
    #: underscore keeps it out of the config schema/baseline machinery, which skips
    #: private fields (same convention as ``_degraded_sections``); consumers read
    #: the :attr:`stub_roster` property.
    _stub_roster: list[str] = field(
        default_factory=list,
        repr=False,
        compare=False,
    )

    @property
    def stub_roster(self) -> list[str]:
        """The stub roster as configured, before operator deviations."""
        return self._stub_roster

    pool_identity_env: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Pool Identity Env Keys",
            "Env variable NAMES whose value is part of a shared backend's "
            "identity. Names listed here are folded into the backend's env hash "
            "even when they look like a rotating secret (AWS_SECRET*, "
            "AWS_SESSION*, OAUTH*), which is what makes them safe to apply to a "
            "shared backend: two sessions declaring different values get "
            "different backends instead of colliding onto one. Use it to let a "
            "server that authenticates from such a variable be shared at all — "
            "by default it declares one, so nothing is forwarded and the server "
            "runs unwrapped. The cost is the reason the exclusion exists: "
            "rotating a named value re-partitions that server's pool, so the "
            "next session cold-starts a backend. Exact names, not prefixes. "
            "Names the daemon's own credential scrub removes (AWS_ACCESS*, "
            "AWS_SECRET*, AWS_SESSION*, SSH_AUTH_SOCK*, GNUPGHOME*, "
            "GIT_ASKPASS*) are ignored here — that scrub is a separate, broader "
            "guard this setting does not lift. Empty by default.",
            restart=True,
        ),
    )
    prewarm_count: int = field(
        default=0,
        metadata=_meta(
            "Prewarm Count",
            "Number of hottest observed (agent x server x channel) MCP backends "
            "to spawn at gateway startup, before the first session connects. "
            "Removes the cold-start latency on the first new-chat after a "
            "gateway restart or after all backends have idled out — the steady "
            "state already reuses warm backends within the idle timeout. The "
            "hot set is learned from prior registers and persisted beside the "
            "socket; channel_id is a stable id, so a prewarmed backend is "
            "reused by every later new-chat in that channel. 0 (default) "
            "disables prewarming — no hot-key file is read or written.",
            restart=True,
        ),
    )
    read_buffer_limit_bytes: int = field(
        default=64 * 1024 * 1024,
        metadata=_meta(
            "Read Buffer Limit",
            "Maximum bytes for a single MCP response line before asyncio drops it. "
            "Default 64 MiB. Responses exceeding this are fast-failed with -32000. "
            "Env override: KIROCREW_MCP_READ_LIMIT.",
            restart=True,
        ),
    )
    response_spill_threshold_bytes: int = field(
        default=256 * 1024,
        metadata=_meta(
            "Response Spill Threshold",
            "Tool-call responses larger than this (bytes) have their text content "
            "written to ~/.kiro/crew/mcp_spill/ and truncated inline to 16 KiB + "
            "a file path marker. Default 256 KiB. Set 0 to disable spilling. "
            "Env override: KIROCREW_MCP_SPILL_THRESHOLD. Read by the MCP broker "
            "when it starts, like every other field of this section.",
            restart=True,
        ),
    )


# The forwarding default assumed when config omits
# ``mcp_gateway.forward_declared_env``. Read from the dataclass default so the
# field and every parse-site fallback cannot drift apart: this default is read
# in three places (the field, the loader's ``_safe_bool`` fallback, and the
# dashboard stub-batch reader), and a reader disagreeing with the field makes the
# batch skip servers the rewrite pools perfectly well.
FORWARD_DECLARED_ENV_DEFAULT = bool(
    McpGatewayConfig.__dataclass_fields__["forward_declared_env"].default  # type: ignore[arg-type]
)


@dataclass
class McpConfig:
    """MCP server settings that apply whether or not the broker is enabled.

    Distinct from :class:`McpGatewayConfig`, which configures the sharing broker
    itself: these settings govern how MCP servers are FOUND and launched, so
    they matter equally with the broker off.
    """

    extra_path_dirs: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Extra MCP Binary Directories",
            "Additional directories to search for MCP server binaries, ahead of "
            "the built-in locations. Add one when a package manager installs its "
            "MCP launchers somewhere Kiro Crew does not know about: a server "
            "declared by bare name that resolves nowhere never starts, and the "
            "session just comes up short of tools. Each entry must be a single "
            "absolute directory (``~`` is expanded); anything else is ignored "
            "with a warning. These directories are prepended to the search path "
            "used by the MCP probe, the agent-config command resolver, and the "
            "broker's rewriter alike, so a binary found here is found "
            "everywhere. They also join the PATH of the broker daemon and every "
            "pooled MCP backend it spawns, so a wrapper script found here can "
            "exec a bare tool name; the daemon reads this when it starts, so a "
            "change reaches it only once it is replaced. They do NOT join the "
            "search for the agent runtime itself, which must not be shadowable "
            "by a configured directory.",
            restart=True,
        ),
    )
    honour_auto_approve: bool = field(
        default=True,
        metadata=_meta(
            "Honour MCP autoApprove",
            "Keep an ``autoApprove`` list you wrote yourself -- one hand-added to "
            "``mcp.json`` or to an agent file -- in the agent config Kiro Crew "
            "writes. On by default: an ``autoApprove`` is a deliberate choice about "
            "your own tools and is respected, so those verbs run without an approval "
            "card. Know what it costs before writing one: the agent runtime approves "
            "such a call locally and emits no permission request, so Kiro Crew's "
            "own tool gate never runs for it. Turn this OFF to drop every verb no "
            "server spec declares, which puts those tools back through the gate. A "
            "governance ceiling strips the key whatever this says, and a verb a spec "
            "declares is kept either way. Applies at restart, when the spec is "
            "rebuilt, so a change does not retract or restore a grant already in the "
            "file.",
            restart=True,
        ),
    )


@dataclass
class InstancesConfig:
    """Multi-instance management (the *Instances* feature).

    Gates and tunes the gateway's ability to manage/switch between several
    remote Kiro Crew instances over SSH tunnels. Off by default — opt-in only,
    since enabling it allows the gateway to open SSH ``-L`` forwards and relaxes
    the dashboard CSP ``frame-src`` for the active loopback tunnel ports.

    Numeric transport defaults and bounds live in
    ``kiro_crew.instances.constants`` so their canonical values cannot drift
    from this dataclass.
    """

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable multi-instance management — lets this gateway open SSH tunnels "
            "to remote Kiro Crews and embed their dashboards. Default off (opt-in). "
            "Enabling also scopes a CSP frame-src relaxation to active tunnel ports.",
            restart=True,
        ),
    )
    warm_set_cap: int = field(
        default=_DEFAULT_WARM_SET_CAP,
        metadata=_meta(
            "Warm Set Cap",
            "Max number of remote instances kept warm (iframe mounted + tunnel live) "
            "at once. Least-recently-used instances beyond this are evicted and "
            "reconnected on demand. Bounds memory/socket use (each warm instance is a "
            "full dashboard SPA). 0 (the default) is automatic: the cap follows how "
            "many crews are configured, so up to an internal ceiling no crew you added "
            "is evicted and the cap widens by itself when you add one -- eviction "
            "cold-boots the pane and reads as a disconnect, so a cap below the number "
            "of crews in use makes tab switching look like a connection flap. Past that "
            "ceiling eviction resumes; an explicit value is honoured exactly, including "
            "one below the number of configured crews.",
        ),
    )
    tunnel_base_port: int = field(
        default=_DEFAULT_TUNNEL_BASE_PORT,
        metadata=_meta(
            "Tunnel Base Port",
            "First local loopback port used for an SSH -L forward. The allocator "
            "increments from here, skipping ports already in use.",
            restart=True,
        ),
    )
    ssh_compression: bool = field(
        default=_DEFAULT_SSH_COMPRESSION,
        metadata=_meta(
            "SSH Compression",
            "Enable SSH transport compression (ssh -C) on instance tunnels. The "
            "remote dashboard SPA bundle plus all API/WebSocket traffic travel over "
            "this forwarded stream and are highly compressible; the gateway does not "
            "gzip HTTP responses, so this is the only compression in the path. "
            "Default on (best for a dedicated remote host over a slow link); turn off "
            "on a fast/local link where compression CPU outweighs the bandwidth win.",
        ),
    )
    connect_timeout_secs: float | None = field(
        default=None,
        metadata=_meta(
            "Connect Timeout (secs)",
            "How long to wait for the local forward port to accept connections "
            "before declaring a connect attempt failed. When unset, SSH uses "
            "15s and SSM uses 25s. Fifteen seconds is sufficient for a direct "
            "ssh TCP connect, but hosts behind a "
            "ProxyCommand or jump host routinely need longer (the proxy handshake "
            "runs before ssh begins the forward). Raise this if connecting a "
            "remote instance times out while the same ssh forward succeeds by hand. "
            "An explicit value applies to both transports. Clamped to [1, 120].",
        ),
    )
    mint_timeout_secs: float | None = field(
        default=None,
        metadata=_meta(
            "Mint Timeout (secs)",
            "How long to wait for the remote `kirocrew token` mint to return "
            "before failing a connect. When unset, SSH uses 30s and SSM uses "
            "90s (its dispatch latency is higher). The mint runs over the same "
            "ssh transport as the tunnel, so a host behind a ProxyCommand or "
            "jump host pays the proxy handshake here too. An explicit value "
            "applies to both transports, so size it for the slowest transport "
            "you use. Clamped to [10, 120].",
        ),
    )
    max_recovery_attempts: int = field(
        default=_DEFAULT_MAX_RECOVERY,
        metadata=_meta(
            "Max Recovery Attempts",
            "Consecutive self-heal attempts before a dropped tunnel is left "
            "disconnected. With the capped-exponential backoff, the default 8 spans a "
            "~2 min recovery window, enough to outlast a transient drop (screen lock, "
            "proxy warmup) before giving up.",
        ),
    )
    recover_backoff_max_secs: float = field(
        default=_DEFAULT_BACKOFF_MAX,
        metadata=_meta(
            "Recover Backoff Cap (secs)",
            "Cap on the per-attempt backoff between self-heal attempts. The wait grows "
            "1, 2, 4, 8, 16 then holds at this cap; raising it spaces retries further "
            "across a slow reconnect.",
        ),
    )
    probe_failure_threshold: int = field(
        default=_DEFAULT_PROBE_FAILS,
        metadata=_meta(
            "Probe Failure Threshold",
            "Consecutive health-probe failures before a connected-but-not-forwarding "
            "(zombie) tunnel is torn down to trigger self-heal.",
        ),
    )

    def __post_init__(self) -> None:
        if self.warm_set_cap < 0:
            # 0 is meaningful here (automatic -- track the connected count), so
            # only a negative value is a misconfiguration, and it falls back to
            # automatic rather than to 1: a caller who wrote a nonsense number
            # wanted "enough", not the tightest possible cap.
            logger.warning(
                "instances.warm_set_cap %d < 0, using 0 (automatic: track the connected count)",
                self.warm_set_cap,
            )
            object.__setattr__(self, "warm_set_cap", _WARM_SET_CAP_AUTO)
        if not (1 <= self.tunnel_base_port <= 65535):
            logger.warning(
                "instances.tunnel_base_port %d out of range [1, 65535], using %d",
                self.tunnel_base_port,
                _DEFAULT_TUNNEL_BASE_PORT,
            )
            object.__setattr__(self, "tunnel_base_port", _DEFAULT_TUNNEL_BASE_PORT)
        if self.connect_timeout_secs is not None and self.connect_timeout_secs < 1.0:
            logger.warning(
                "instances.connect_timeout_secs %s < 1, using the transport default",
                self.connect_timeout_secs,
            )
            object.__setattr__(self, "connect_timeout_secs", None)
        elif (
            self.connect_timeout_secs is not None
            and self.connect_timeout_secs > _CONNECT_TIMEOUT_CEILING
        ):
            logger.warning(
                "instances.connect_timeout_secs %s > %s, clamping to %s",
                self.connect_timeout_secs,
                _CONNECT_TIMEOUT_CEILING,
                _CONNECT_TIMEOUT_CEILING,
            )
            object.__setattr__(self, "connect_timeout_secs", _CONNECT_TIMEOUT_CEILING)
        if self.mint_timeout_secs is not None and self.mint_timeout_secs < _MINT_TIMEOUT_FLOOR:
            logger.warning(
                "instances.mint_timeout_secs %s < %s, using the transport default",
                self.mint_timeout_secs,
                _MINT_TIMEOUT_FLOOR,
            )
            object.__setattr__(self, "mint_timeout_secs", None)
        elif self.mint_timeout_secs is not None and self.mint_timeout_secs > _MINT_TIMEOUT_CEILING:
            logger.warning(
                "instances.mint_timeout_secs %s > %s, clamping to %s",
                self.mint_timeout_secs,
                _MINT_TIMEOUT_CEILING,
                _MINT_TIMEOUT_CEILING,
            )
            object.__setattr__(self, "mint_timeout_secs", _MINT_TIMEOUT_CEILING)
        if self.max_recovery_attempts < 1:
            logger.warning(
                "instances.max_recovery_attempts %d < 1, using %d",
                self.max_recovery_attempts,
                _DEFAULT_MAX_RECOVERY,
            )
            object.__setattr__(self, "max_recovery_attempts", _DEFAULT_MAX_RECOVERY)
        elif self.max_recovery_attempts > _MAX_RECOVERY_CEILING:
            logger.warning(
                "instances.max_recovery_attempts %d > %d, clamping to %d "
                "(guards against a near-infinite self-heal loop on a dead connection)",
                self.max_recovery_attempts,
                _MAX_RECOVERY_CEILING,
                _MAX_RECOVERY_CEILING,
            )
            object.__setattr__(self, "max_recovery_attempts", _MAX_RECOVERY_CEILING)
        if self.recover_backoff_max_secs <= 0:
            logger.warning(
                "instances.recover_backoff_max_secs %s <= 0, using %s",
                self.recover_backoff_max_secs,
                _DEFAULT_BACKOFF_MAX,
            )
            object.__setattr__(self, "recover_backoff_max_secs", _DEFAULT_BACKOFF_MAX)
        elif self.recover_backoff_max_secs > _RECOVER_BACKOFF_CEILING:
            logger.warning(
                "instances.recover_backoff_max_secs %s > %s, clamping to %s "
                "(guards against a multi-day self-heal window on a dead connection)",
                self.recover_backoff_max_secs,
                _RECOVER_BACKOFF_CEILING,
                _RECOVER_BACKOFF_CEILING,
            )
            object.__setattr__(self, "recover_backoff_max_secs", _RECOVER_BACKOFF_CEILING)
        if self.probe_failure_threshold < 1:
            logger.warning(
                "instances.probe_failure_threshold %d < 1, using %d",
                self.probe_failure_threshold,
                _DEFAULT_PROBE_FAILS,
            )
            object.__setattr__(self, "probe_failure_threshold", _DEFAULT_PROBE_FAILS)


@dataclass
class TunnelConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled", "Enable a tunnel to expose the dashboard for remote access.", restart=True
        ),
    )
    name_mode: str = field(
        default="username",
        metadata=_meta(
            "Name Mode",
            "Tunnel naming: 'username' uses 'kirocrew', "
            "'hash' uses 'kirocrew-<hostHash>' for multi-host disambiguation.",
            enum=["username", "hash"],
            restart=True,
        ),
    )
    name_override: str = field(
        default="",
        metadata=_meta(
            "Name Override",
            "Explicit tunnel name (overrides name_mode). "
            "Note: some tunnel providers prefix your username (e.g. 'foo' becomes '<user>-foo').",
            restart=True,
        ),
    )
