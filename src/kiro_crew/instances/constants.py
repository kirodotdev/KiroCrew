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
DEFAULT_WARM_SET_CAP: int = 5

# First local loopback port handed out for an SSH ``-L`` forward. The port
# allocator increments from here, skipping ports already in use. Chosen to sit
# just above the default dashboard port (7777).
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

# How long (secs) to wait for the remote ``kirocrew token`` to return before
# giving up on the mint. This is the *second* proxy-bound ssh child a connect
# spawns: the readiness wait above bounds the ``ssh -L`` forward, and this bounds
# the separate ``ssh <host> kirocrew token`` invocation. Both cross the same
# ProxyCommand/jump host, so a host whose handshake alone eats 12-16s spends that
# cost twice — meaning a user who had to raise ``connect_timeout_secs`` can clear
# the forward and still time out here. Exposed as a user-tunable via
# ``kirocrew config set instances.mint_timeout_secs <value>`` for exactly that
# case (30s covers a direct ssh mint comfortably).
DEFAULT_MINT_TIMEOUT_SECS: float = 30.0

# SSM dispatches the mint through ``ssm send-command``, which adds the agent's
# poll interval to the remote command's own runtime before any output comes
# back — routinely slower than an ssh mint, hence the higher default. An explicit
# ``mint_timeout_secs`` override wins for both transports (same convention as
# ``connect_timeout_secs``).
DEFAULT_SSM_MINT_TIMEOUT_SECS: float = 90.0

# Upper bound (secs) on a user-configured instances.mint_timeout_secs. Higher
# than the connect ceiling because the mint budget covers a proxy handshake AND
# the remote gateway actually answering the token request, and because it must
# sit above the SSM default (90s) or that transport's own default would be
# unreachable as an explicit value. Still bounded so a pathological setting
# cannot leave a connect attempt hanging for hours.
MINT_TIMEOUT_CEILING_SECS: float = 300.0

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

# Timeout (secs) for one session-transfer request over an already-open tunnel
# (POST the bundle to the peer's import endpoint — no SSH spawn). Far larger than
# the token probe above because this carries a whole conversation: a bundle is
# capped at ~20 MB of message content, and the SSH forward it crosses can be a
# high-latency link, so a probe-sized budget would fail every large transfer. The
# request is still bounded rather than unlimited, so an unresponsive peer
# surfaces as a clean transfer error instead of hanging the caller's turn.
DEFAULT_SESSION_TRANSFER_TIMEOUT_SECS: float = 30.0


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
