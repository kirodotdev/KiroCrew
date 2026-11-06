"""Tunable constants for the Instances feature.

These are the defaults for the matching ``InstancesConfig`` fields in
``kiro_crew.config.loader`` (override with ``kirocrew config set instances.<key>
<value>``). The dataclass references them, so constant and config default cannot drift.
"""

from __future__ import annotations

# Maximum number of remote instances kept "warm" (iframe + tunnel + WebSocket live)
# at once. Each is a full dashboard SPA; least-recently-used ones past the cap are
# evicted and reconnected on demand.
#
# ``WARM_SET_CAP_AUTO`` (0, the default) means "as many as are registered, up to
# ``WARM_SET_CAP_AUTO_CEILING``", resolved per request by
# ``kiro_crew.instances.warm_set.resolve_warm_set_cap``. Auto is the default because
# at the pane an eviction looks exactly like a disconnect (unmount, re-mint, cold
# boot), so a cap below the crews in use turns tab switching into an apparent flap.
#
# REGISTERED, not connected: a connected count races tunnel startup, and a crew that
# connects just after the poll is evicted -- a different one on each restart.
WARM_SET_CAP_AUTO: int = 0
DEFAULT_WARM_SET_CAP: int = WARM_SET_CAP_AUTO

# Upper bound on the AUTO-resolved warm set: the registered count is user intent,
# not a resource budget, so 30 crews would otherwise mount 30 SPAs in one renderer.
# An EXPLICIT integer cap is honoured verbatim, even above this.
#
# 10 is a product target (the fleet size Remote Crew is designed around), not a
# measurement. The per-pane cost is CPU and worker threads rather than heap: each pane
# polls and holds a WebSocket, and opening a diff spawns a highlighter worker pool.
WARM_SET_CAP_AUTO_CEILING: int = 10

# First local loopback port for an SSH ``-L`` forward; the allocator counts up from
# here. Clear of the default dashboard port, so a stock gateway's port is never first.
DEFAULT_TUNNEL_BASE_PORT: int = 7778

# SSH transport compression (``ssh -C``) on instance tunnels. The whole remote
# dashboard (JS/HTML/JSON, typically 3-5x compressible) rides this one stream, and the
# gateway does not gzip its responses. On by default for high-latency remote links; turn
# off on a fast link with ``kirocrew config set instances.ssh_compression false``.
DEFAULT_SSH_COMPRESSION: bool = True

# Health-probe cadence and threshold: after this many *consecutive* failures the
# tunnel is unhealthy; interval <= 0 disables the probe. A forward that relays nothing
# (a silently dead SSM session) is torn down after 3 x 10s = 30s.
DEFAULT_PROBE_INTERVAL_SECS: int = 10
DEFAULT_PROBE_FAILURE_THRESHOLD: int = 3

# Total timeout (secs) for one end-to-end liveness probe: `GET /api/health` through
# the forward must return an HTTP response. A TCP connect alone passes on a zombie
# forward (a session-manager-plugin holding the socket, relaying nothing). 4x the 1.0s
# `_port_reachable` budget, so one slow round trip does not tear down a good tunnel.
# Not user-tunable: the per-transport connect timeouts are the knob for a slow proxy.
DEFAULT_PROBE_HEALTH_TIMEOUT_SECS: float = 4.0

# Max consecutive self-heal attempts on an unhealthy tunnel. Reset to 0 only once a
# rebuilt forward answers the end-to-end health probe (not on a local rebind), so a
# dead far end still climbs to the cap. A rebuild that fails outright spends ~2 min of
# backoff at the defaults; one that re-binds but never answers also spends a 30s probe
# window per attempt, ~6 min. Size instances.max_recovery_attempts against the longer.
DEFAULT_MAX_RECOVERY_ATTEMPTS: int = 8

# Ceiling for instances.max_recovery_attempts; larger values clamp (with a warning)
# so self-heal stays bounded. Worst case 100 x (30s probe + 30s backoff) ~= 1.7h.
MAX_RECOVERY_ATTEMPTS_CEILING: int = 100

# Cap (secs) on the self-heal backoff, min(base * 2**(attempt-1), this): 1, 2, 4, 8,
# 16, then this cap.
DEFAULT_RECOVER_BACKOFF_MAX_SECS: float = 30.0

# Ceiling (secs) for instances.recover_backoff_max_secs; larger values clamp (with a
# warning). Worst case 100 * (30s + 300s) ~= 9.2h rather than days.
RECOVER_BACKOFF_MAX_CEILING_SECS: float = 300.0

# How long (secs) to wait for the local forward port to accept connections. Generous
# for a direct ``ssh -L``, but a ProxyCommand host (jump host, WSSH, corporate proxy)
# can spend 12-16s on the proxy handshake alone; raise it with
# ``kirocrew config set instances.connect_timeout_secs <value>``.
DEFAULT_CONNECT_TIMEOUT_SECS: float = 15.0

# SSM's ``session-manager-plugin`` does a WebSocket handshake before binding the
# local port, so it gets a higher default. An explicit ``connect_timeout_secs`` wins
# for both transports.
DEFAULT_SSM_CONNECT_TIMEOUT_SECS: float = 25.0

# Ceiling (secs) for instances.connect_timeout_secs, so connect can never hang.
CONNECT_TIMEOUT_CEILING_SECS: float = 120.0

# Cap on the ssh ConnectTimeout the diagnostics probes (_probe_ssh,
# _probe_remote_dashboard) borrow from instances.connect_timeout_secs: a diagnosis
# should resolve well under a minute even when connect is tuned to 90s.
# Diagnostics use min(configured, this).
DIAGNOSTICS_CONNECT_TIMEOUT_CAP_SECS: float = 15.0

# How long (secs) to wait for the remote `kirocrew token` mint. It is the second
# proxy-bound ssh child of a connect (``connect_timeout_secs`` budgets the first), so
# raising one usually means raising both: ``instances.mint_timeout_secs``.
DEFAULT_MINT_TIMEOUT_SECS: float = 30.0

# The SSM mint (``aws ssm send-command`` + ``get-command-invocation`` polling) adds
# the agent's dispatch latency, so its default is higher. An explicit (non-None)
# ``mint_timeout_secs`` wins for both transports.
DEFAULT_SSM_MINT_TIMEOUT_SECS: float = 90.0

# Bounds on instances.mint_timeout_secs. Below the floor falls back to the default
# (under 10s fails every realistic proxy chain); above the ceiling clamps (with a
# warning) so a failed mint cannot hang connect.
MINT_TIMEOUT_FLOOR_SECS: float = 10.0
MINT_TIMEOUT_CEILING_SECS: float = 120.0

# Proactively re-mint each instance's dashboard token at this fraction of its
# TTL, before the 20h cap. 0.8 = refresh at 80% elapsed.
DEFAULT_TOKEN_REFRESH_FRACTION: float = 0.8

# Ceiling on the lifetime of a credential minted for ANOTHER gateway's pane (the
# hub-lending mint). A crew's own row TTL governs this gateway's own pane and is
# left alone; only the lent credential is capped, and the cap is taken as a
# MINIMUM against the row so a row already shorter stays shorter.
#
# It is a ceiling on an EXPOSURE WINDOW rather than a tuning knob. A lent port is
# held by this gateway for the life of the lease, and a socket cannot outlive the
# process holding it: a gateway exit releases every hold while the credential
# naming that port stays valid, because the credential was issued by the remote
# crew and nothing here can invalidate it. So the window between this gateway
# exiting and the credential dying IS one of these TTLs, and its length is the
# only part of that window this gateway gets to choose.
#
# 30m rather than something smaller because the refresh loop re-mints at
# DEFAULT_TOKEN_REFRESH_FRACTION of the lifetime, which leaves 20% of it as the
# margin a re-mint has to complete in. At 30m that margin is 360s, against a
# worst case of MINT_TIMEOUT_CEILING_SECS + 15 for a chained mint and
# DEFAULT_SSM_MINT_TIMEOUT_SECS for an SSM one -- so the slowest mint in the tree
# finishes inside it with room over. A cap low enough to eat that margin would
# expire the token mid-mint and the hub's pane would reload on every cycle.
LENT_HOP_TTL_CAP: str = "30m"

# The retained-field bounds for the hop-lease map, which `a-bound-bounds-every-field-it-
# retains` requires of every field the registry keeps. Both are enforced twice: at
# ADMISSION in `lend_hop`, which refuses rather than trims because a refused mint is a
# credential never issued, and at LOAD, which cannot refuse (a foreign or corrupted write
# is already on disk) and so clamps instead.
#
# 64 live leases through one gateway. A lease exists only while a chained credential
# against that port is valid, so this bounds concurrently-chained crews, not crews: the
# warm-set cap is a single digit and nobody chains 64 crews behind one parent. Startup
# binds one listening socket per non-in-use lease, so this is also the ceiling on that
# descriptor burst.
HOP_LEASE_MAX: int = 64

# Must equal ``ttl_to_seconds(LENT_HOP_TTL_CAP)``; pinned by a test rather than computed
# here, because `ttl_to_seconds` lives in ``token_mint`` and the registry must not import
# it (the registry is below the mint in the dependency order). A stored deadline further
# out than this cannot have come from this gateway's writer, which already clamps to the
# cap, so clamping at load bounds what a foreign write can reserve.
HOP_LEASE_DEADLINE_CAP_SECS: int = 30 * 60

# Timeout (secs) for the loopback probe (GET /api/status?token=..., no SSH) that
# validates a *stored* token before it reaches the browser. A stale token gets a
# server-rendered 403, so the SPA never boots to run its `mc-auth-expired` recovery.
# Deny-by-default: anything but a 2xx forces a fresh mint. Tight so tab switches stay
# instant.
DEFAULT_TOKEN_PROBE_TIMEOUT_SECS: float = 2.0

# Connect timeout (secs) for one chat-proxy request over an open tunnel
# (SshTunnelManager.proxy_request). The forward is on loopback, so a slow connect means
# the forward is dead, not busy.
DEFAULT_PROXY_CONNECT_TIMEOUT_SECS: float = 10.0

# Read-IDLE timeout (secs) for a chat-proxy response, not a total: a chat turn
# streams SSE for minutes. The peer sends a keepalive every ~30s, so 120s of silence
# means the stream is dead.
DEFAULT_PROXY_READ_IDLE_TIMEOUT_SECS: float = 120.0

# Cap (bytes) on a request body forwarded through the chat proxy. A real one is a
# few KB; bound before buffering, like SEARCH_REPLY_MAX_BYTES.
PROXY_REQUEST_BODY_MAX_BYTES: int = 2 * 1024 * 1024

# Cap (bytes) on what the chat proxy holds to redact one peer reply: a whole
# JSON body, or one SSE event. Redaction needs a complete string, so the proxy
# buffers up to this much; a reply past it is refused rather than forwarded
# unredacted. Sized above the largest real reply (a 200-row slot page, the
# peer's full `slots` broadcast, itself bounded by PEER_SLOTS_REPLY_MAX_BYTES).
PROXY_REDACT_BUFFER_MAX_BYTES: int = 8 * 1024 * 1024

# How many times the chat proxy will percent-decode a caller-supplied path
# before refusing it. The path is decoded to a FIXED POINT so the string the
# policy inspects is the string the peer will resolve — one decode pass is not
# enough, because the router already consumed one and `%252e%252e` therefore
# arrives as `%2e%2e` and reads as clean. Real paths need zero or one pass;
# a chain deeper than this is only ever an attempt to outrun the decoder, so
# the bound is a refusal (not a truncation) and keeps the loop finite.
PROXY_PATH_MAX_DECODE_PASSES: int = 4

# Timeout (secs) for one session-transfer POST to the peer's import endpoint over an
# open tunnel. It bounds each connect and each read, not the whole request: a bundle
# has no size ceiling, so a total budget would fail every slow upload.
DEFAULT_SESSION_TRANSFER_TIMEOUT_SECS: float = 30.0

# How long (secs) an arriving session waits for the host to have memory to
# parse it before the importer answers a retryable 429. Shared with the sender,
# whose wait for the importer's reply has to outlast it.
SESSION_IMPORT_MEMORY_WAIT_SECS: float = 300.0

# Cap (bytes) on a peer's reply to a session transfer, enforced before decoding:
# with no total timeout, nothing else bounds this read. An honest reply is a few
# hundred bytes of JSON.
SESSION_TRANSFER_REPLY_MAX_BYTES: int = 256 * 1024

# Timeout (secs) for one federated GET /api/sessions/search to a peer. The peer
# scans (bounded by its _SEARCH_SCAN_WINDOW), so 2s gives false "unreachable", while
# 30s would let one dead tunnel stall a keystroke-driven search. Peers run
# concurrently, so this is also the worst-case added latency.
DEFAULT_SEARCH_PROXY_TIMEOUT_SECS: float = 6.0

# Byte ceiling for one peer's federated-search reply, enforced BEFORE JSON decoding
# (resp.json() buffers the whole body before any per-field clamp). An honest reply
# (at most 200 rows, 2 KiB per string) is well under 1 MiB.
SEARCH_REPLY_MAX_BYTES: int = 4 * 1024 * 1024

# Timeout (secs) for one peer capability read (/api/version, /api/agents,
# /api/effort-levels, /api/workspaces; /api/models has its own below). Short because
# it blocks a chat header from rendering, but above search's 6s: search can absorb a
# partial result, while a missing capability read leaves the picker empty. The reads
# run concurrently, so the slowest budget is the worst case.
DEFAULT_CAPABILITY_PROXY_TIMEOUT_SECS: float = 8.0

# Timeout (secs) for the peer's /api/models capability read specifically. The
# other four reads answer from state the peer already holds, but the model list
# is the one read whose COLD path runs real subprocess work on the peer: up to
# 5s of sandbox-backend detection (_SANDBOX_BACKEND_PROBE_TIMEOUT_SECS in
# sandbox_probe.py) plus up to 10s of `kiro-cli chat --list-models`
# (_LIST_MODELS_SUBPROCESS_TIMEOUT_SECS in dashboard/handlers/agents.py), plus
# up to 3s of entitlement revalidation
# (_READ_PATH_PROBE_DEADLINE_SECS in acp/session_handle_model_selection.py, bounding the
# read-path probe before the picker narrows) before the first reply is cached,
# ~18s worst case end to end (5 + 10 + 3 < 20). Each term is a named production
# bound, and the proxy test sums those names.
# At the shared 8s a healthy peer's cold read is cut off and reported as
# `capability_unreachable`, so every fresh remote chat's model picker opens empty.
DEFAULT_MODELS_CAPABILITY_PROXY_TIMEOUT_SECS: float = 20.0

# Byte ceiling for one peer capability reply, enforced BEFORE JSON decoding. The
# biggest honest payloads (agent roster, model list) are tens of KiB.
CAPABILITY_REPLY_MAX_BYTES: int = 2 * 1024 * 1024

# Timeout (secs) for asking a parent crew to mint a token for a crew chained
# behind it. The parent answers by running `kirocrew token` over ITS OWN hop to
# that crew, so the budget has to cover the parent's whole remote mint plus the
# round trip through the hub's forward to the parent -- which is why it is not
# the 8s capability budget, whose reads answer from state the peer already holds.
# It sits ABOVE the widest mint the parent can arm. Not above SSM's DEFAULT alone:
# `mint_timeout_secs` is operator-settable up to MINT_TIMEOUT_CEILING_SECS, so the
# ceiling plus relay margin is the only bound that holds for every configuration.
# That ordering is the point: the parent's own timeout fires first, so a slow crew
# is reported as a mint failure carrying the parent's reason rather than as an
# unreachable parent. The budget spans the WHOLE call including its single retry,
# not each attempt, so the worst case here is what a caller holding a lock waits for.
DEFAULT_CHAINED_MINT_TIMEOUT_SECS: float = MINT_TIMEOUT_CEILING_SECS + 15.0

# Byte ceiling for one chained-mint reply, enforced BEFORE JSON decoding. The honest
# payload is one token and one port, with no list in it, hence far tighter.
CHAINED_MINT_REPLY_MAX_BYTES: int = 64 * 1024

# Byte ceiling for one peer's live-slots reply, enforced BEFORE JSON decoding. A
# slot projection is a few KiB, so a hundred open sessions stay under 1 MiB.
#
# Its own constant, and looser than CAPABILITY_REPLY_MAX_BYTES, because it bounds a
# different shape: like search, a list whose size scales with the peer's workload,
# while a capability reply is fixed by configuration. One shared symbol would let a
# change sized for one endpoint silently move the others.
PEER_SLOTS_REPLY_MAX_BYTES: int = 4 * 1024 * 1024


# The crew-scoped session reads (search / list / read) get their own bound, sized
# against a FULL TRANSCRIPT: ``/api/crew-sessions/read`` returns a whole conversation,
# which can reach several megabytes. Per-field clamps (``_PEER_FIELD_MAX_CHARS``)
# still apply inside it.
PEER_SESSIONS_REPLY_MAX_BYTES: int = 8 * 1024 * 1024


# Accepted dashboard-token lifetime: a positive integer of at most four digits plus
# ``h`` or ``m``. Shared by the registry and both token minters, so a value one accepts
# cannot fail in another at the next connect.
#
# ``\Z``, not ``$``: Python's ``$`` also matches before a trailing newline, so
# ``"20h\n"`` would reach the mint argv with an embedded newline.
TTL_PATTERN = r"^[1-9][0-9]{0,3}[hm]\Z"
