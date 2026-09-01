"""Tunable constants for the Instances feature.

Isolated in this module so resource limits and defaults can be adjusted in one
place without hunting through the registry / tunnel-manager code.

These values are the *defaults* for the corresponding ``InstancesConfig`` fields
in ``kiro_crew.config.loader``; a user can override them via
``kirocrew config set instances.<key> <value>``. Keeping the canonical default
here (and referencing it from the dataclass) means the constant and the config
default can never drift apart.
"""

from __future__ import annotations

# Maximum number of remote instances kept "warm" (iframe mounted + tunnel +
# WebSocket live) at once. Each warm instance is a full dashboard SPA, so this
# bounds memory/socket usage; least-recently-used instances beyond the cap are
# lazily evicted and reconnected on demand.
#
# ``WARM_SET_CAP_AUTO`` (0) is the default and means "as many as are connected":
# the cap is resolved per request from the live connected count (see
# ``kiro_crew.instances.warm_set.resolve_warm_set_cap``), so a crew the operator
# deliberately connected is never evicted.
#
# Auto is the default because eviction is INDISTINGUISHABLE FROM A DISCONNECT at
# the pane: the iframe is unmounted, the token is re-minted and the remote SPA
# cold-boots on the next click (surfacing the error panel outright if readiness
# misses its timeout). A fixed cap below the connected count therefore turns
# ordinary tab switching into an apparent connection flap, and the operator has
# no way to attribute it -- the tunnel is up the whole time. Tracking the
# connected count removes that class of misconfiguration rather than asking
# anyone to keep two numbers in sync by hand.
WARM_SET_CAP_AUTO: int = 0
DEFAULT_WARM_SET_CAP: int = WARM_SET_CAP_AUTO

# Upper bound on the AUTO-resolved warm set. Auto follows the connected count,
# which is a statement of user intent and not a resource budget -- a fleet of 30
# connected crews would otherwise mount 30 dashboard SPAs in one renderer.
# Beyond this many connected crews eviction resumes, so the worst case stays
# bounded while the common small-fleet case (the reason auto exists) never
# evicts. An EXPLICIT integer cap is honoured verbatim and is deliberately not
# clamped by this: an operator who names a number has made the budget decision
# themselves, including a number larger than this.
#
# 8 is a judgement, not a measurement: comfortably above the 5 this default used
# to be (so no install gets a tighter warm set than it had), and still in the
# range a single renderer has been seen to carry. The per-pane cost that bounds
# it is CPU and worker threads rather than heap -- each pane is a full SPA with
# its own polling and WebSocket, and a pane the user opens a diff in spawns its
# own highlighter worker pool (see website/src/main.tsx on why those are no
# longer spawned eagerly).
WARM_SET_CAP_AUTO_CEILING: int = 8

# First local loopback port handed out for an SSH ``-L`` forward. The port
# allocator increments from here, skipping ports already in use and ports the
# registry has already assigned. Sits well clear of the default dashboard port
# so a stock gateway's own port is never the first candidate.
DEFAULT_TUNNEL_BASE_PORT: int = 7778

# Enable SSH transport compression (``ssh -C``) on instance tunnels. The whole
# remote dashboard travels over this single forwarded stream: the SPA bundle on
# first connect plus every subsequent API/WebSocket frame. That payload is
# JS/HTML/JSON — highly compressible (typically 3-5x), and the gateway does not
# gzip its HTTP responses, so nothing is double-compressed. Default on because
# the dominant deployment is a dedicated remote gateway host where spare CPU to
# save bandwidth on a high-latency/low-throughput link is the right trade. On a
# fast/local link compression can be marginally slower, so it stays tunable via
# ``kirocrew config set instances.ssh_compression false``. See §5.2.
DEFAULT_SSH_COMPRESSION: bool = True

# Health-probe cadence/threshold for a connected tunnel. Poll every interval,
# and after this many *consecutive* failures treat the tunnel as unhealthy
# (Stage 2 self-heal hooks the existing exit seam). interval <= 0 disables the
# probe.
DEFAULT_PROBE_INTERVAL_SECS: int = 30
DEFAULT_PROBE_FAILURE_THRESHOLD: int = 3

# Max consecutive self-heal attempts before giving up on an unhealthy tunnel
# (2-tier recovery). Reset to 0 once a rebuild succeeds, so a tunnel that
# flaps-then-recovers isn't permanently capped. With the capped-exponential
# backoff below, this many attempts span the total recovery window (~2 min at
# the default 8 attempts / 30s cap) before the tunnel is left disconnected.
DEFAULT_MAX_RECOVERY_ATTEMPTS: int = 8

# Upper bound on a user-configured instances.max_recovery_attempts. A value above
# this is clamped down to it (with a warning) so a pathological setting can't turn
# the bounded self-heal into a near-infinite retry loop on a dead connection. Kept
# generous (~47 min recovery window at the 30s backoff cap) so only extreme values
# trip it.
MAX_RECOVERY_ATTEMPTS_CEILING: int = 100

# Cap (secs) on the per-attempt backoff between self-heal attempts. The backoff
# is min(base * 2**(attempt-1), this), so the inter-attempt wait grows 1, 2, 4,
# 8, 16 then holds at this cap for the remaining attempts.
DEFAULT_RECOVER_BACKOFF_MAX_SECS: float = 30.0

# Upper bound (secs) on a user-configured instances.recover_backoff_max_secs. A
# larger value is clamped down to it (with a warning) so a pathological pacing
# (e.g. a 1-day backoff) can't stretch the bounded self-heal into a multi-day
# wall-clock window even with the attempt count capped. At this ceiling the worst
# case is ~MAX_RECOVERY_ATTEMPTS_CEILING * this (~8h).
RECOVER_BACKOFF_MAX_CEILING_SECS: float = 300.0

# How long (secs) to wait for the local forward port to start accepting
# connections before declaring a connect attempt failed. A direct ``ssh -L``
# needs only a TCP handshake, so 15s is generous for most hosts. However, hosts
# behind a ProxyCommand (jump host, WSSH, corporate proxy) routinely spend
# 12-16s on the proxy handshake alone before ssh even begins the forward, so
# this timeout becomes the binding constraint. Exposed as a user-tunable via
# ``kirocrew config set instances.connect_timeout_secs <value>`` so operators on
# slow-proxy hosts can raise it without patching the installed package.
DEFAULT_CONNECT_TIMEOUT_SECS: float = 15.0

# SSM's ``session-manager-plugin`` completes a WebSocket handshake with the SSM
# service before it binds the local port — routinely slower than a direct ssh
# TCP connect. This higher default mirrors that reality. When the user supplies
# an explicit ``connect_timeout_secs`` override, it wins for both transports.
DEFAULT_SSM_CONNECT_TIMEOUT_SECS: float = 25.0

# Upper bound (secs) on a user-configured instances.connect_timeout_secs. Keeps
# a pathological value from making the connect path hang indefinitely. 120s is
# generous enough for any realistic proxy chain while still bounding the wait.
CONNECT_TIMEOUT_CEILING_SECS: float = 120.0

# Cap on the ssh ConnectTimeout the diagnostics probes (_probe_ssh,
# _probe_remote_dashboard) borrow from instances.connect_timeout_secs. The
# tunable above is sized for how long a slow-proxy CONNECT should be allowed
# to take — a diagnosis is a different use case with its own UX budget: a user
# who tuned connect_timeout_secs up to, say, 90s for a genuinely slow proxy
# still wants a diagnosis to resolve in well under a minute, not silently
# inherit the full tunable. Diagnostics use min(configured, this).
DIAGNOSTICS_CONNECT_TIMEOUT_CAP_SECS: float = 15.0

# How long (secs) to wait for the remote `kirocrew token` to return before
# giving up on a mint attempt. The mint runs over the same ssh transport as the
# tunnel itself, so a host behind a ProxyCommand or jump host pays the proxy
# handshake again here (the connect flow spawns two proxy-bound ssh children;
# ``connect_timeout_secs`` above budgets the first, this budgets the second —
# an operator who raised one typically needs to raise both). Exposed as a
# user-tunable via ``kirocrew config set instances.mint_timeout_secs <value>``.
DEFAULT_MINT_TIMEOUT_SECS: float = 30.0

# The SSM mint dispatches ``aws ssm send-command`` and polls
# ``get-command-invocation``: send-command has its own dispatch latency (agent
# poll interval) on top of the remote command's runtime, so its default is
# higher than the direct-ssh mint's. When the user supplies an explicit
# (non-None) ``mint_timeout_secs`` override, it wins for both transports.
DEFAULT_SSM_MINT_TIMEOUT_SECS: float = 90.0

# Bounds on a user-configured instances.mint_timeout_secs. Below the floor
# falls back to the default (a mint that can't finish in under 10s of budget
# would fail every realistic proxy chain anyway, so a tiny value is a
# misconfiguration, not a tuning choice); above the ceiling is clamped down
# (with a warning) so a pathological value can't make a failed mint hang the
# connect flow indefinitely.
MINT_TIMEOUT_FLOOR_SECS: float = 10.0
MINT_TIMEOUT_CEILING_SECS: float = 120.0

# Proactively re-mint each instance's dashboard token at this fraction of its
# TTL, before the 20h cap. 0.8 = refresh at 80% elapsed.
DEFAULT_TOKEN_REFRESH_FRACTION: float = 0.8

# Timeout (secs) for the loopback liveness probe that validates a *stored* token
# before the API hands it to the browser on (re)connect. A stored token can go
# stale while the tunnel stays CONNECTED (a failed self-heal re-mint, or a remote
# `kirocrew restart` that invalidates tokens); an iframe loaded with a stale
# token gets a server-rendered 403 page, so the SPA never boots to fire the
# reactive `mc-auth-expired` recovery. The probe (GET /api/status?token=... over
# the existing tunnel — no SSH) closes that initial-load gap. It is
# deny-by-default: anything but a positive 2xx (including a timeout/connection
# error) is treated as invalid and forces a fresh mint; if that mint also fails
# the link is genuinely down and the caller returns an error rather than serving
# an unconfirmed token. Kept tight so a tab activation never blocks perceptibly.
DEFAULT_TOKEN_PROBE_TIMEOUT_SECS: float = 2.0

# Connect timeout (secs) for one generic chat-proxy request over an already-open
# tunnel (see SshTunnelManager.proxy_request — no SSH spawn). Connect-phase only:
# the forward terminates on the local loopback, so a healthy tunnel accepts in
# milliseconds and anything slower means the forward is dead, not busy.
DEFAULT_PROXY_CONNECT_TIMEOUT_SECS: float = 10.0

# Read-IDLE timeout (secs) for a chat-proxy response. Deliberately NOT a total
# timeout: a proxied chat turn streams SSE for minutes, so any total budget
# either kills live turns or is meaninglessly huge. Idle is the right axis —
# the peer's SSE drain loop emits a keepalive comment every ~30s even when the
# model is silent, so 120s of true silence means the stream is dead, and the
# caller gets a clean error instead of a connection that never closes.
DEFAULT_PROXY_READ_IDLE_TIMEOUT_SECS: float = 120.0

# Cap (bytes) on an inbound request body forwarded through the chat proxy. A
# chat message plus attachments metadata is a few KB; anything MB-sized headed
# for a peer is either abuse or a bug, and the hub must not buffer unbounded
# input on behalf of either side. Mirrors the reply-side discipline of
# SEARCH_REPLY_MAX_BYTES: bound before buffering.
PROXY_REQUEST_BODY_MAX_BYTES: int = 2 * 1024 * 1024

# How many times the chat proxy will percent-decode a caller-supplied path
# before refusing it. The path is decoded to a FIXED POINT so the string the
# policy inspects is the string the peer will resolve — one decode pass is not
# enough, because the router already consumed one and `%252e%252e` therefore
# arrives as `%2e%2e` and reads as clean. Real paths need zero or one pass;
# a chain deeper than this is only ever an attempt to outrun the decoder, so
# the bound is a refusal (not a truncation) and keeps the loop finite.
PROXY_PATH_MAX_DECODE_PASSES: int = 4

# Timeout (secs) for one session-transfer request over an already-open tunnel
# (POST the bundle to the peer's import endpoint — no SSH spawn). Far larger than
# the token probe above because this carries a whole conversation: a bundle is
# capped at ~20 MB of message content, and the SSH forward it crosses can be a
# high-latency link, so a probe-sized budget would fail every large transfer. The
# request is still bounded rather than unlimited, so an unresponsive peer
# surfaces as a clean transfer error instead of hanging the caller's turn.
DEFAULT_SESSION_TRANSFER_TIMEOUT_SECS: float = 30.0

# Timeout (secs) for one federated session-search request over an already-open
# tunnel (GET the peer's /api/sessions/search — no SSH spawn). Sized between the
# token probe (2s, a bare status ping) and the transfer (30s, a ~20 MB bundle):
# a search reply is a small JSON page but the peer does real scanning work
# (bounded by its own _SEARCH_SCAN_WINDOW), so the probe budget would produce
# false "unreachable" verdicts on a loaded peer, while anything transfer-sized
# would let one dead tunnel stall an interactive, keystroke-driven search. The
# fan-out runs peers concurrently, so this is also the worst-case latency a
# slow peer adds to the aggregated response.
DEFAULT_SEARCH_PROXY_TIMEOUT_SECS: float = 6.0

# Byte ceiling for one peer's federated-search reply, enforced BEFORE JSON
# decoding (resp.json() buffers the whole body first, so a hostile/broken peer
# streaming an unbounded reply could exhaust hub memory before any per-field
# clamp runs). Sized generously above any honest reply: the aggregator caps
# limit at 200 rows and every string field is clamped to 2 KiB downstream, so
# a truthful worst case is well under 1 MiB; 4 MiB only ever bites on garbage.
SEARCH_REPLY_MAX_BYTES: int = 4 * 1024 * 1024

# Timeout (secs) for one peer capability read over an already-open tunnel (GET
# the peer's /api/version, /api/agents, /api/models, /api/effort-levels or
# /api/workspaces — no SSH spawn). Larger than the token probe (2s) because the
# peer does real work for some of these (the model list can round-trip to its
# own provider), and kept as short as that work allows because a capability read
# blocks a chat header from rendering: a user watching an empty model picker is
# better served by a fast "peer did not answer" than by a long wait. It is the
# one peer budget ABOVE the federated-search timeout (6s), which fans out reads
# that a partial result set can absorb; a missing capability read has no partial
# form — the picker is simply empty — so it is the one worth waiting out.
# The reads run concurrently, so this is the worst-case latency for the set.
DEFAULT_CAPABILITY_PROXY_TIMEOUT_SECS: float = 8.0

# Byte ceiling for one peer capability reply, enforced BEFORE JSON decoding for
# the same reason as the search cap above. Sized for the largest honest payload
# by a wide margin: the agent roster and the model list are the big ones and are
# tens of KiB each even on a heavily-configured gateway, so 2 MiB only ever
# bites on a hostile or broken peer.
CAPABILITY_REPLY_MAX_BYTES: int = 2 * 1024 * 1024


# Accepted shape for a dashboard-token lifetime: a positive integer of at most
# four digits followed by ``h`` or ``m``. Canonical here because three layers
# need the SAME answer — the registry that persists it and both token minters
# that spend it. A value one layer accepts and another rejects is stored happily
# and then fails at the next connect, blaming the tunnel for a bad edit.
#
# Anchored with ``\Z`` rather than ``$``: Python's ``$`` also matches just BEFORE
# a trailing newline, so a ``"20h\n"`` would pass a ``$``-anchored check and then
# reach the mint argument list carrying an embedded newline.
TTL_PATTERN = r"^[1-9][0-9]{0,3}[hm]\Z"
