"""Mint a remote KiroCrew dashboard token over SSH.

Build the shell snippet that runs ``kirocrew token`` on the remote desktop and
parse the JWT out of the URL it prints. Lifting this into the gateway lets the
``SshTunnelManager`` (Stage 4) mint a per-instance token at connect time without
reinventing the bin-candidate search.

Security (standard practices):

* The minted token is a short-lived (≤20h) bearer credential. It is **never
  logged** and is returned only to the in-memory caller.
* ``ssh`` is invoked via ``create_subprocess_exec`` with an argv list (no shell
  on the *local* side), so ``ssh_host`` cannot inject local shell syntax.
* The *remote* command is necessarily a single string the remote shell runs.
  Its only variable parts are the candidate bin paths (hard-coded literals), a
  user ``remote_bin`` (charset-validated by the registry / tunnel manager before
  reaching here), and ``ttl`` (validated by :func:`_validate_ttl`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re

from kiro_crew.config.paths import CONFIG_DIR_NAME, LEGACY_CONFIG_DIR_NAME
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

# Sentinel meaning "no custom bin path — try every candidate". Stored as-is;
# never executed locally.
DEFAULT_REMOTE_BIN = "$HOME/.local/bin/kirocrew"

# Non-interactive SSH shells don't source ~/.zshrc, so PATH may be minimal. Each
# candidate is a full path the remote shell can exec directly.
REMOTE_BIN_CANDIDATES: tuple[str, ...] = (
    "$HOME/.local/bin/kirocrew",  # install.sh / source install
    "$HOME/.kirocrew-app/.venv/bin/kirocrew",  # one-liner installer venv
)

# ttl accepted by `kirocrew token --ttl`: a positive integer with an h/m suffix
# (e.g. "20h", "30m"). Validated to keep it out of the remote command unchecked.
_TTL_RE = re.compile(r"^[1-9][0-9]{0,3}[hm]$")

# Extract the JWT from a `kirocrew token` URL: http://localhost:7777?token=eyJ...
# (also matches https://.../?token=...&foo=bar).
_TOKEN_RE = re.compile(r"[?&]token=([^\s&]+)")

# Bare KiroCrew-token / JWT shape, used to scrub a token that reached stdout
# outside a URL before a stdout tail is put into an exception message.
#
# The segment count is `{1,4}` REPEATED, not a fixed `head.payload.sig` triple:
# `generate_token` (dashboard/token_auth.py) mints `payload.signature` — only
# TWO segments — so a three-segment pattern would never match the shape this app
# actually produces, leaving the scrub layer inert against its own tokens. The
# upper bound covers a compact JWE (five segments) so a partial match cannot
# leave a trailing `.ciphertext.tag` behind. Matching is greedy, so all segments
# present are consumed.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{4,}(?:\.[A-Za-z0-9_-]+){1,4}")

# How much of a failing remote's stdout to carry in the error (tail, not head —
# the failure reason is the last thing printed).
_OUTPUT_TAIL_CHARS = 300

# How much stdout the scrubbers are allowed to SCAN. The remote's stdout is
# unbounded (whatever the far side printed) and `re` does not release the GIL,
# so scrubbing it whole blocks the gateway's event loop in proportion to its
# size — measured ~1s per MB, i.e. ~13s on a 13MB payload. Bounding the scan
# window keeps the cost flat (~10ms) regardless of what the remote emits. The
# window is 8x the carried tail so a clipped left edge normally falls in noise
# the tail would have dropped anyway. An executor hop is NOT a substitute: with
# the GIL held by `re`, the same 13MB payload still stalled the loop for ~1.1s
# in a thread.
_OUTPUT_SCAN_CHARS = _OUTPUT_TAIL_CHARS * 8

# Stand-in for the run touching the scan window's left edge: the slice may have
# cut it out of the middle of a secret, leaving a suffix the token patterns can
# no longer recognise.
#
# The floor is deliberately LOW rather than set to the window-minus-tail
# "reachability" distance. A clipped fragment 2000+ chars from the end looks
# unreachable only if the text keeps its length — but the scrubbers SHRINK it
# (each matched blob collapses to `<redacted>`), so a stdout full of redactable
# material pulls the leading fragment into the carried tail. Measured: with a
# 2100-char floor, a stdout of dense JWT-shaped blobs surfaced a raw clipped
# fragment in the error. Scrubbing the clipped run unconditionally costs nothing
# the tail truncation would have kept anyway.
_CLIPPED_RUN_MIN = 16
_CLIPPED_RUN_RE = re.compile(r"^[A-Za-z0-9_\-.=+/%%:?&]{%d,}" % _CLIPPED_RUN_MIN)
_CLIPPED_MARKER = "<clipped>"

# How long to wait for the remote `kirocrew token` to return before giving up.
_DEFAULT_MINT_TIMEOUT_SECS = 30.0


class TokenMintError(Exception):
    """Raised when minting a remote token fails."""


def _validate_ttl(ttl: str) -> str:
    """Return *ttl* if it matches the accepted ``<int>[hm]`` form, else raise."""
    if not _TTL_RE.match(ttl):
        raise TokenMintError(
            f"invalid ttl {ttl!r}: expected a positive integer with 'h' or 'm' "
            "suffix (e.g. '20h', '30m')"
        )
    return ttl


def ttl_to_seconds(ttl: str) -> int:
    """Convert a validated ``<int>[hm]`` ttl string to seconds.

    Used to schedule proactive token refresh before the cap. Raises
    :class:`TokenMintError` for a malformed ttl.
    """
    ttl = _validate_ttl(ttl)
    value, unit = int(ttl[:-1]), ttl[-1]
    return value * 3600 if unit == "h" else value * 60


def parse_token_from_stdout(stdout: str) -> str:
    """Extract the JWT from a ``kirocrew token`` URL, or ``""`` if absent."""
    match = _TOKEN_RE.search(stdout.strip())
    return match.group(1) if match else ""


def _validate_port(port: int | None) -> int | None:
    """Return *port* if it's a valid TCP port (1-65535), else raise.

    Kept out of the remote command unvalidated — the value is interpolated into
    a shell command line, so it must be a bounded integer.
    """
    if port is None:
        return None
    try:
        p = int(port)
    except (TypeError, ValueError) as e:
        raise TokenMintError(f"invalid port {port!r}: not an integer") from e
    if not (1 <= p <= 65535):
        raise TokenMintError(f"invalid port {p}: out of range 1-65535")
    return p


def _token_subcommand(
    ttl: str | None, port: int | None = None, embed_parent_port: int | None = None
) -> str:
    """Return the ``kirocrew token`` subcommand (ttl/port pre-validated).

    ``--port`` is essential when the remote gateway isn't on the default 7777:
    ``kirocrew token`` calls its own dashboard to mint, so the port must match
    the instance's ``remote_port`` or the mint fails with "connection refused".

    ``--embed-parent-port`` carries the *parent* (embedding) dashboard's port so
    the minted token authorizes exactly that loopback origin as a CSP
    frame-ancestor — how the multi-instance embed renders across ports without a
    hardcoded port or a wildcard.
    """
    cmd = "token"
    if ttl:
        cmd += f" --ttl {ttl}"
    if port:
        cmd += f" --port {port}"
    if embed_parent_port:
        cmd += f" --embed-parent-port {embed_parent_port}"
    return cmd


def build_candidate_command(
    subcommand: str,
    candidates: tuple[str, ...] = REMOTE_BIN_CANDIDATES,
    *,
    marker_port: int | None = None,
) -> str:
    """Remote shell snippet that execs the first available candidate bin.

    Generic over the kirocrew *subcommand* (e.g. ``token``, ``restart``).
    Candidates are hard-coded literals, so double-quote embedding is safe; the
    ``$HOME`` inside each expands at remote-shell parse time. *subcommand* is
    a fixed literal or pre-validated string — never raw user input.

    When *marker_port* is given, the snippet first consults the run-marker the
    *running* gateway wrote for that port (``<data-home>/run/gateway-<port>.bin``,
    probing ``$KIROCREW_HOME`` then ``$HOME/.kiro/crew`` then the legacy
    ``$HOME/.kirocrew`` — see :func:`_run_marker_clause`) and execs the launcher
    it names — so mint uses the same venv as the live gateway instead of whatever
    ``~/.local/bin/kirocrew`` happens to point at. Falls through to the candidate
    search when the marker is absent or doesn't name an executable (older
    remotes, or gateway not running), so nothing regresses.
    See :mod:`kiro_crew.instances.run_marker`.
    """
    expanded = " ".join(f'"{p}"' for p in candidates)
    prefix = f"{_run_marker_clause(subcommand, marker_port)} " if marker_port is not None else ""
    return prefix + " ".join(
        [
            f"for b in {expanded}; do",
            '  if [ -x "$b" ]; then',
            f'    exec "$b" {subcommand};',
            "  fi;",
            "done;",
            f'echo "kirocrew binary not found in any of: {", ".join(candidates)}" >&2;',
            "exit 127",
        ]
    )


def _run_marker_clause(subcommand: str, port: int) -> str:
    """Shell prelude that execs the gateway's recorded launcher for *port*.

    Probes, in priority order, the run-marker
    (``run/gateway-<port>.bin``, written by
    :func:`kiro_crew.instances.run_marker.write_marker`) under each candidate
    data home and ``exec``s the first launcher it names that is an executable
    file:

    1. ``$KIROCREW_HOME`` — an explicit override, if exported into this shell.
    2. ``$HOME/<CONFIG_DIR_NAME>`` — the current default data home.
    3. ``$HOME/<LEGACY_CONFIG_DIR_NAME>`` — the pre-move legacy home (older remotes).

    The default/legacy home segments are **interpolated from**
    :data:`kiro_crew.config.paths.CONFIG_DIR_NAME` /
    :data:`~kiro_crew.config.paths.LEGACY_CONFIG_DIR_NAME` — the same constants
    ``config_dir()`` (the marker *writer*) derives its default from — so reader
    and writer share one source of truth. A future data-home rename updates both
    sides from that single edit instead of leaving these shell literals to drift
    stale (which is precisely the read/write desync this whole mechanism guards).

    ``$HOME``/``$KIROCREW_HOME`` expand at remote-shell parse time; *port* is a
    bounded int (see :func:`_validate_port`) and the home segments are trusted
    module constants, so the path literals cannot inject shell syntax.

    Why the multi-home probe: the writer keys the marker off the gateway
    process's ``config_dir()``, which now defaults to ``~/.kiro/crew`` after the
    ``~/.kirocrew`` -> ``~/.kiro/crew`` data-home move. This prelude runs in the
    *remote* non-interactive SSH shell, which usually does NOT export
    ``KIROCREW_HOME`` — so a single ``${KIROCREW_HOME:-$HOME/.kirocrew}`` default
    (the pre-move literal) would read the wrong, now-empty legacy path on every
    migrated remote, miss the marker, and fall through to the blind candidate
    search (which lands on whatever ``~/.local/bin/kirocrew`` points at, e.g. an
    uninstalled worktree). Probing both the new default and the legacy home — and
    still honoring an explicit ``KIROCREW_HOME`` above both — restores the marker
    hit on migrated remotes while remaining a no-op on un-migrated ones. Falls
    through to the candidate search when no marker names an executable, so
    nothing regresses.
    """
    fname = f"run/gateway-{int(port)}.bin"
    # ``${KIROCREW_HOME:+...}`` expands to the value only when KIROCREW_HOME is
    # set and non-empty (else an empty word, skipped below) — so an unset
    # override never degrades to a bare ``/run/...`` absolute path. The default
    # and legacy home segments come from the shared config.paths constants (not
    # re-hardcoded here) so reader and writer never drift apart on a home move.
    homes = " ".join(
        [
            f'"${{KIROCREW_HOME:+$KIROCREW_HOME/{fname}}}"',
            f'"$HOME/{CONFIG_DIR_NAME}/{fname}"',
            f'"$HOME/{LEGACY_CONFIG_DIR_NAME}/{fname}"',
        ]
    )
    return " ".join(
        [
            f"for __mk in {homes}; do",
            '  [ -n "$__mk" ] || continue;',
            '  if [ -f "$__mk" ]; then',
            '    __kb="$(cat "$__mk" 2>/dev/null)";',
            '    if [ -n "$__kb" ] && [ -x "$__kb" ]; then',
            f'      exec "$__kb" {subcommand};',
            "    fi;",
            "  fi;",
            "done;",
        ]
    )


def build_remote_command(
    remote_bin: str,
    subcommand: str,
    candidates: tuple[str, ...] = REMOTE_BIN_CANDIDATES,
    *,
    marker_port: int | None = None,
) -> str:
    """Pick the remote command for the user's stored ``remote_bin`` (generic).

    Empty / default sentinel → candidate search (optionally run-marker-first when
    *marker_port* is given). Otherwise respect the custom path (``~/`` → ``$HOME/``)
    verbatim — an explicit ``remote_bin`` is the user's deliberate choice and is
    never overridden by the marker. *remote_bin* must already be charset-validated;
    *subcommand* is a fixed/validated literal.
    """
    if not remote_bin or remote_bin == DEFAULT_REMOTE_BIN:
        return build_candidate_command(subcommand, candidates, marker_port=marker_port)
    expanded = re.sub(r"^~/", "$HOME/", remote_bin)
    return f'"{expanded}" {subcommand}'


def build_remote_token_command(
    remote_bin: str,
    candidates: tuple[str, ...] = REMOTE_BIN_CANDIDATES,
    ttl: str | None = None,
    port: int | None = None,
    embed_parent_port: int | None = None,
) -> str:
    """Pick the remote command for the user's stored ``remote_bin``.

    Empty / default sentinel → try every candidate in order. Otherwise respect
    the customization, rewriting a leading ``~/`` to ``$HOME/`` (tilde does not
    expand inside double quotes) so kiro-cli resolves over SSH. *remote_bin*
    must already be charset-validated by the caller.
    """
    if ttl is not None:
        ttl = _validate_ttl(ttl)
    port = _validate_port(port)
    embed_parent_port = _validate_port(embed_parent_port)
    return build_remote_command(
        remote_bin,
        _token_subcommand(ttl, port, embed_parent_port),
        candidates,
        # Prefer the marker the gateway on this port wrote (its own venv) over the
        # blind PATH candidate search — keyed by the same port the mint targets.
        marker_port=port,
    )


def _build_ssh_argv(ssh_host: str, remote_command: str) -> list[str]:
    """Build the local ``ssh`` argv (no local shell) to run *remote_command*.

    ``BatchMode=yes`` fails fast instead of hanging on an interactive password
    prompt; ``ConnectTimeout`` bounds the TCP connect. ``ssh_host`` is validated
    by the caller (registry / tunnel manager) before reaching here.
    """
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "AddressFamily=inet",
        ssh_host,
        remote_command,
    ]


def _redacted_output_tail(stdout: str, limit: int = _OUTPUT_TAIL_CHARS) -> str:
    """Return a token-stripped, credential-redacted tail of *stdout*.

    Why this exists: ``kirocrew token`` on a remote that predates the
    stdout->stderr split prints its failure reasons (``❌ Could not reach gateway
    on port 5476: ...``) to **stdout**, so an error built only from stderr came
    back as ``<no stderr>`` and the operator had to SSH in to learn why. Carrying
    a stdout tail makes the real reason travel with the exception.

    Why stripping is mandatory: unlike stderr, stdout is the one stream that
    *does* carry the minted JWT on success. This tail is only ever built on a
    failure path, but a partially-successful remote (URL printed, then a
    non-zero exit) could still put a live credential in it — so the token is
    substituted out FIRST, before the generic credential/exfil redactors run,
    and the result is truncated to the last *limit* chars (the tail, because the
    reason is the last thing printed).

    Why the scan is bounded: the scrubbers cost ~1s per MB of stdout and `re`
    holds the GIL, so scanning an unbounded remote payload stalls the gateway's
    event loop (13s measured on 13MB). Only the last ``_OUTPUT_SCAN_CHARS`` are
    scanned. That slice can land mid-run, which would show the regexes a
    *fragment* of a secret and let its suffix survive into the tail — so the run
    touching the window's left edge is dropped when it is long enough to have
    been clipped out of one. A reason printed inside such an over-long unbroken
    run is sacrificed with it; a reason on its own line (what remotes actually
    print) is unaffected.
    """
    if not stdout:
        return ""
    window = stdout
    if len(window) > _OUTPUT_SCAN_CHARS:
        window = _CLIPPED_RUN_RE.sub(_CLIPPED_MARKER, window[-_OUTPUT_SCAN_CHARS:], count=1)
    stripped = _TOKEN_RE.sub(lambda m: m.group(0)[0] + "token=<redacted>", window)
    stripped = _JWT_RE.sub("<redacted>", stripped)
    safe = redact_exfiltration_urls(redact_credentials(stripped)[0])[0]
    return safe.strip()[-limit:]


def _with_stdout_tail(primary: str, safe_stdout: str) -> str:
    """Append the redacted stdout tail to *primary* when there is one.

    Keeps the original single-stream message shape for modern remotes (which
    print their reasons to stderr) and only widens the message when stdout
    actually carried something — i.e. on an older remote.
    """
    return f"{primary} | stdout tail: {safe_stdout}" if safe_stdout else primary


async def mint_remote_token(
    ssh_host: str,
    *,
    remote_bin: str = "",
    ttl: str = "20h",
    remote_port: int | None = None,
    embed_parent_port: int | None = None,
    timeout_secs: float = _DEFAULT_MINT_TIMEOUT_SECS,
) -> str:
    """SSH to *ssh_host*, run ``kirocrew token``, and return the parsed JWT.

    *remote_port* is passed to ``kirocrew token --port`` so the remote mint
    targets the gateway the instance actually runs on (not the default 7777).

    *embed_parent_port* is passed to ``kirocrew token --embed-parent-port`` so the
    minted token authorizes the parent (embedding) dashboard's loopback origin as
    a CSP frame-ancestor — how the embedded pane renders across ports.

    Raises :class:`TokenMintError` on connection failure, a non-zero remote
    exit, a timeout, or if no token can be parsed from stdout. The token itself
    is never logged.
    """
    ttl = _validate_ttl(ttl)
    remote_command = build_remote_token_command(
        remote_bin, ttl=ttl, port=remote_port, embed_parent_port=embed_parent_port
    )
    argv = _build_ssh_argv(ssh_host, remote_command)
    logger.info("Minting token on %s (ttl=%s)", ssh_host, ttl)  # no token in logs

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        raise TokenMintError(f"failed to spawn ssh for {ssh_host}: {e}") from e

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_secs)
    except asyncio.TimeoutError as e:
        proc.kill()
        try:
            await proc.wait()
        except ProcessLookupError:
            pass
        raise TokenMintError(f"timed out minting token on {ssh_host} after {timeout_secs}s") from e

    stdout = stdout_b.decode("utf-8", "replace")
    stderr = stderr_b.decode("utf-8", "replace").strip()
    # stderr is proxy-controlled (WSSH banner etc.); redact credentials/exfil URLs
    # before surfacing it in an exception that may reach logs/status. The token
    # only ever appears on stdout, never stderr.
    safe_stderr = redact_exfiltration_urls(redact_credentials(stderr)[0])[0] if stderr else ""

    if proc.returncode != 0:
        # stderr may carry the "binary not found" diagnostic — safe to log; it
        # never contains the token (token only ever appears on stdout).
        # Belt-and-suspenders for older remotes: pre-fix `kirocrew token` printed
        # its failure reasons to STDOUT, so a stderr-only error message degraded
        # to a useless "<no stderr>". The tail is built HERE, inside the failure
        # branch, so the "only ever built on a failure path" invariant that
        # _redacted_output_tail documents is enforced by control flow rather than
        # merely asserted — the success path never touches stdout holding a token.
        raise TokenMintError(
            f"remote token mint on {ssh_host} exited {proc.returncode}: "
            f"{_with_stdout_tail(safe_stderr or '<no stderr>', _redacted_output_tail(stdout))}"
        )

    token = parse_token_from_stdout(stdout)
    if not token:
        detail = _with_stdout_tail(
            f"stderr: {safe_stderr or '<none>'}", _redacted_output_tail(stdout)
        )
        raise TokenMintError(f"could not parse a token from {ssh_host} output ({detail})")
    return token


async def run_remote_kirocrew(
    ssh_host: str,
    subcommand: str,
    *,
    remote_bin: str = "",
    marker_port: int | None = None,
    timeout_secs: float = 60.0,
) -> tuple[int, str]:
    """Run ``kirocrew <subcommand>`` on *ssh_host* over SSH.

    Generic runner for non-token subcommands such as ``restart``. Returns
    ``(returncode, combined_stderr_tail)``; -1 on spawn failure / timeout.
    ``ssh_host`` must be validated by the caller; *subcommand* is a fixed
    literal (never raw user input). Never returns or logs secrets.

    *marker_port* — when the caller knows the remote dashboard port, pass it so
    this resolves the running gateway's own launcher via the run-marker first
    (same fix as token mint), instead of the blind PATH candidate search. This
    is what makes the dashboard "restart remote" action work on a host whose
    ``~/.local/bin/kirocrew`` points at an uninstalled worktree.
    """
    remote_command = build_remote_command(
        remote_bin, subcommand, marker_port=_validate_port(marker_port)
    )
    argv = _build_ssh_argv(ssh_host, remote_command)
    logger.info("Running 'kirocrew %s' on %s", subcommand, ssh_host)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        return -1, f"failed to spawn ssh: {e}"
    try:
        _out, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_secs)
    except asyncio.TimeoutError:
        proc.kill()
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()
        return -1, f"timed out after {timeout_secs}s"
    err = err_b.decode("utf-8", "replace").strip()
    # Proxy-controlled stderr (e.g. a WSSH banner) can carry credential-looking
    # text or exfil URLs; redact before returning since callers surface this tail.
    safe_err = redact_exfiltration_urls(redact_credentials(err)[0])[0] if err else ""
    return (proc.returncode if proc.returncode is not None else -1), safe_err[:300]
