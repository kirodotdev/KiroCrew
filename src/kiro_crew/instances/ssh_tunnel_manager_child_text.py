"""The forwarder child's text contract, for ``kiro_crew.instances.ssh_tunnel_manager``.

What the child is told and what it says: the loopback-bound argv it is spawned with
(``ssh -N -L ...`` or the SSM port-forward document), the ssh and SSM stderr signals
that classify why it failed, the SSM stdout anchors that name why a forward closed,
and the sanitizing of that text before a person sees it. Pure functions and
constants: nothing here starts or watches a process.
"""

from __future__ import annotations

import re

from kiro_crew.cloud import ssm as cloud_ssm
from kiro_crew.security import redact

_LOOPBACK = "127.0.0.1"


# ssh prints these benign advisory lines to stderr on connect (post-quantum KEX
# warning); they are NOT failures. Strip them from captured stderr so the real
# error (e.g. "bind: Address already in use") isn't masked in logs/status.
_BENIGN_SSH_STDERR_MARKERS = (
    "post-quantum key exchange",
    "store now, decrypt later",
    "server may need to be upgraded",
    "openssh.com/pq",
)


# Classification phrases for _exit_error / _ssm_exit_error, matched against the
# lowercased noise-stripped stderr. The first hit ALSO anchors the sanitized
# detail window (see _sanitize_banner) so the classified phrase survives the cap
# even when arbitrary benign stderr (e.g. LocalCommand output) precedes it.
_SSH_AUTH_SIGNALS = (
    "permission denied",
    "publickey",
    "authentication failed",
    "certificate has expired",
    "certificate expired",
)
_SSH_TRANSPORT_DROP_SIGNALS = (
    "timed out during banner exchange",
    "session ended unexpectedly",
    "connection timed out",
    "connection reset",
    "closed by remote host",
    "connection refused",
)
_SSH_BIND_SIGNALS = ("address already in use", "cannot listen to port")
# Appended to a transport drop ONLY when the child died before the forward ever
# came up. A drop on the first connect is most often not a drop at all: the
# instance's ``ssh_host`` names a host that does not exist under that name
# (rebuilt, renamed, decommissioned), and the proxy closes the socket before
# the ssh handshake. ssh's own text then says only "connection closed by
# <unknown>", which points nowhere, while the one field the operator can fix
# is never mentioned. A steady-state drop (idle timeout, reset after hours up)
# does NOT get this line: that host was reachable, so the name is right.
_FIRST_CONNECT_DROP_HINT = (
    "the connection dropped before the forward came up; if this host was "
    "rebuilt or renamed, check this instance's ssh_host"
)
_SSM_CREDENTIAL_SIGNALS = (
    "expired",
    "unable to locate credentials",
    "no credentials",
    "credentials not found",
)
_SSM_DENIAL_SIGNALS = ("accessdenied", "not authorized", "unauthorizedoperation")
_SSM_PLUGIN_SIGNALS = ("sessionmanagerplugin", "session-manager-plugin")
_SSM_TARGET_SIGNALS = (
    "targetnotconnected",
    "not connected",
    "invalidinstanceid",
    "invalidinstanceinformation",
)
_SSM_BIND_SIGNALS = ("address already in use", "bind")

# ---------------------------------------------------------------------------
# SSM close-reason shapes, matched against the tunnel child's STDOUT.
#
# `aws ssm start-session` prints every session-close notice to STDOUT and sends
# its own diagnostics to a rolling log FILE, so none of it reaches the stderr
# the error classifiers read. The four literals below are the only close shapes
# session-manager-plugin prints, each taken from its source:
#
#   datachannel HandleChannelClosedMessage
#     "\n\nSessionId: %s : %s\n\n"                   service gave a close reason
#     "\n\nExiting session with sessionId: %s.\n\n"  closed, no reason given
#   sessionhandler ResumeSessionHandler
#     "Session: %s timed out.\n"                     resume gave up; session gone
#   session ValidateInputAndStartSession
#     "Cannot perform start session: %v\n"           the session never opened
#
# All four paths end in exit status 0: the plugin's Stop() is os.Exit(0) and its
# main() has no non-zero exit at all. So the exit code cannot separate them and
# the anchored shape is what does. An unrecognised shape stays generic rather
# than being guessed at, because naming a cause the text does not establish is
# the same defect as naming a duration the documentation does not settle.
_SSM_CLOSED_WITH_REASON_ANCHOR = "sessionid: "
_SSM_CLOSED_NO_REASON_ANCHOR = "exiting session with sessionid: "
_SSM_RESUME_TIMED_OUT_ANCHOR = "session: "
_SSM_START_FAILED_ANCHOR = "cannot perform start session: "
# Separator the reason-carrying shape puts between the session id and the text.
_SSM_REASON_SEPARATOR = " : "

# Idle phrases matched INSIDE a service-supplied close reason. The reason text
# is generated service-side (the message gateway), which is not open source, so
# this set is deliberately narrow and anything it does not match falls back to
# the generic wording. Widening it would trade a correct generic message for a
# possibly-wrong specific one.
_SSM_IDLE_CLOSE_SIGNALS = (
    "due to inactivity",
    "idle timeout",
    "idle session timeout",
)

# C0 control characters other than tab and newline. Stripped from captured
# stdout so a bare ESC, NUL or CR cannot survive into a match or a buffer.
_C0_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _sanitize_ssm_stdout(text: str) -> str:
    """ANSI-strip, control-strip and credential-redact captured SSM stdout.

    Applied at READ time rather than per drained chunk: redaction keys on whole
    tokens, and a credential split across two reads would survive a per-chunk
    pass. The buffer is already bounded to ``_MAX_STDOUT_CHARS`` when it is
    captured, so there is no cost to sanitizing all of it at once.
    """
    cleaned = _ANSI_CSI_RE.sub("", text)
    cleaned = _C0_CONTROL_RE.sub("", cleaned)
    return redact(cleaned)


def _ssm_close_reason(stdout: str) -> str:
    """Classify SSM stdout into one anchored close shape.

    Returns ``"idle"``, ``"closed"``, ``"resume_timeout"``, ``"start_failed"``
    or ``""`` when nothing matched.

    The service-supplied reason text inside a reason-carrying notice is read
    here to pick the shape and is not returned: it is a CLASSIFICATION SIGNAL
    ONLY. The caller composes its own wording from the shape, because
    service-controlled text is not something this code can vouch for to an
    operator. Keeping it inside this function means no caller can surface or
    log it.

    Matching is line-anchored on the literal each shape starts with, so the
    session banner and per-connection lines the plugin also prints cannot be
    mistaken for a close notice.
    """
    for raw_line in _sanitize_ssm_stdout(stdout).splitlines():
        line = raw_line.strip()
        low = line.lower()
        if low.startswith(_SSM_CLOSED_WITH_REASON_ANCHOR):
            rest = line[len(_SSM_CLOSED_WITH_REASON_ANCHOR) :]
            head, sep, reason = rest.partition(_SSM_REASON_SEPARATOR)
            # No separator means the id was printed without a reason; that is
            # not a shape the plugin emits, so it is not claimed as one.
            if not sep or not reason.strip():
                continue
            if _first_hit(reason.strip().lower(), _SSM_IDLE_CLOSE_SIGNALS) is not None:
                return "idle"
            return "closed"
        if low.startswith(_SSM_CLOSED_NO_REASON_ANCHOR):
            return "closed"
        if low.startswith(_SSM_RESUME_TIMED_OUT_ANCHOR) and low.rstrip(".").endswith("timed out"):
            return "resume_timeout"
        if low.startswith(_SSM_START_FAILED_ANCHOR):
            return "start_failed"
    return ""


def _first_hit(low: str, phrases: tuple[str, ...]) -> str | None:
    """Return the first of *phrases* present in *low* (already lowercased)."""
    for phrase in phrases:
        if phrase in low:
            return phrase
    return None


def _strip_benign_ssh_noise(text: str) -> str:
    """Drop ssh's benign post-quantum KEX warning lines so a real error shows."""
    kept = [
        ln
        for ln in text.splitlines()
        if ln.strip() and not any(m in ln.lower() for m in _BENIGN_SSH_STDERR_MARKERS)
    ]
    return "\n".join(kept).strip()


# CSI/ANSI escape sequences (WSSH banners carry color + cursor moves such as
# \x1b[31m and \x1b[1G); strip them so a control sequence can't corrupt surfaced
# status text or dashboard tooltips.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


_BANNER_DETAIL_MAX_CHARS = 200


def _sanitize_banner(text: str, *, anchor: str | None = None) -> str:
    """ANSI-strip + credential/exfil-redact untrusted ssh stderr before it is
    surfaced in status/logs, capped at ``_BANNER_DETAIL_MAX_CHARS``. The banner
    is external, proxy-controlled text, so it is a redacted secondary detail
    only -- never a classification signal.

    When *anchor* names the lowercase classification phrase the exit-error
    classifier matched, the fixed-width window is centered on the first
    occurrence of that phrase instead of taken from the head, so benign stderr
    written earlier (e.g. arbitrary ``LocalCommand`` output such as repeated
    ``tput`` warnings under launchd/systemd where ``TERM`` is unset) cannot
    consume the budget and truncate the classified reason out of the surfaced
    detail. Centering on the phrase itself (never on its line, which the
    proxy-controlled buffer can make arbitrarily long) keeps the phrase inside
    the window unconditionally. Without an anchor, or when sanitization
    removed the phrase, the head slice is unchanged.
    """
    cleaned = _ANSI_CSI_RE.sub("", text)
    # Canonical exfiltration-then-credentials composition: the URL pass keys
    # partly on query length and replaces the whole URL, so a credential pass
    # run ahead of it can shorten a `?token=<long>` query below that threshold
    # and leave the destination and payload parameters on the wire.
    cleaned = redact(cleaned)
    if len(cleaned) <= _BANNER_DETAIL_MAX_CHARS:
        return cleaned
    if anchor:
        hit = cleaned.lower().find(anchor)
        if hit >= 0:
            start = hit + len(anchor) // 2 - _BANNER_DETAIL_MAX_CHARS // 2
            start = max(0, min(start, len(cleaned) - _BANNER_DETAIL_MAX_CHARS))
            return cleaned[start : start + _BANNER_DETAIL_MAX_CHARS]
    return cleaned[:_BANNER_DETAIL_MAX_CHARS]


def _build_ssh_tunnel_argv(
    ssh_host: str, local_port: int, remote_port: int, *, compression: bool = True
) -> list[str]:
    """Build the supervised ``ssh -N -L`` argv (loopback-bound, no local shell).

    ``ssh_host`` must already be validated by :func:`validate_ssh_host`.

    ``compression`` adds ``-C`` (zlib transport compression). The forwarded
    stream carries the remote dashboard SPA bundle + all API/WS traffic, which
    is highly compressible; the gateway does not gzip at the HTTP layer, so this
    is the only compression in the path. See ``instances.ssh_compression``.
    """
    # Windows: not yet supported — requires the OpenSSH client (`ssh`) on PATH,
    # which isn't guaranteed; ssh-process kill handling also needs a Windows audit.
    # Tracked as follow-on work.
    forward = f"{_LOOPBACK}:{local_port}:{_LOOPBACK}:{remote_port}"
    argv = [
        "ssh",
        "-N",  # no remote command; foreground so the gateway can supervise it
    ]
    if compression:
        argv.append("-C")  # compress the forwarded stream (bundle + API/WS)
    argv += [
        "-o",
        "BatchMode=yes",  # never prompt — fail fast if auth is needed
        "-o",
        "ExitOnForwardFailure=yes",  # exit if the local forward can't bind
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "AddressFamily=inet",  # force IPv4 loopback (dodge ::1 fallback)
        # The forward must stay owned by the child this manager supervises.
        # Multiplexing takes it away from the user's ssh_config: ssh hands the
        # forward to an existing shared connection and exits 0, leaving it alive
        # under a process the gateway never spawned, so a tunnel that is in fact
        # serving is reported as dead.
        #
        # Routing and identity (`User`, `IdentityFile`, `Port`,
        # `ProxyJump`/`ProxyCommand`) are deliberately still inherited -- the
        # registry carries no inline equivalents. See §9 of the instances spec.
        "-o",
        "ControlPath=none",  # no socket to share -- this is what disables it
        "-o",
        "ControlMaster=no",  # policy; ControlPath alone suffices  # wokeignore:rule=master
        "-L",
        forward,
        ssh_host,
    ]
    return argv


def _build_ssm_tunnel_argv(
    ssm_target: str, local_port: int, remote_port: int, *, profile: str = "", region: str = ""
) -> list[str]:
    """Build the supervised ``aws ssm start-session`` port-forward argv.

    ``ssm_target``/``profile``/``region`` must already be injection-validated
    (:func:`validate_ssm_target` / :func:`validate_aws_profile` /
    :func:`validate_aws_region`). Delegates to
    :func:`kiro_crew.cloud.ssm.build_port_forward_argv` — the launcher's
    existing, reviewed argv builder — rather than duplicating it, so the two
    features can never drift on the SSM document/parameter shape.
    """
    return cloud_ssm.build_port_forward_argv(ssm_target, remote_port, local_port, profile, region)
