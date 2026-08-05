"""Injection-safe validation for SSH connection inputs.

The ``SshTunnelManager`` and token-mint helper pass ``ssh_host`` and
``remote_bin`` into ``ssh`` argv lists. Even though we never use a *local* shell
(``create_subprocess_exec`` takes an argv list), two classes of attack remain:

1. **Option injection** — an ``ssh_host`` like ``-oProxyCommand=...`` is parsed
   by ssh as an *option*, not a host, and can run arbitrary local commands. We
   defend by rejecting any segment that starts with ``-``.
2. **Remote shell injection** — ``remote_bin`` is embedded (double-quoted) into
   the remote command string that the remote shell evaluates. We forbid every
   shell metacharacter (``$ ; | & ` ( ) < > \n`` quotes …) so nothing can break
   out of the quotes or trigger command substitution.

Validation lives here, with the tunnel manager, rather than in the registry:
the registry does a light early-reject charset check, but this is the
authoritative guard applied immediately before a command line is built.
"""

from __future__ import annotations

import re

# Host charset: letters, digits, dot, hyphen, underscore, and a single optional
# ``user@`` prefix. No whitespace, no shell metacharacters. Length-bounded.
_HOST_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_MAX_HOST_LEN = 255

# remote_bin: an absolute or ~/ path to the kirocrew binary. Allows letters,
# digits, dot, underscore, slash, hyphen, tilde, and spaces. Crucially excludes
# ``$`` (no command substitution / var expansion we don't control), quotes,
# and every shell control character. Length-bounded.
_REMOTE_BIN_RE = re.compile(r"^[A-Za-z0-9._/~ -]{1,512}$")


class SshValidationError(ValueError):
    """Raised when an ssh_host or remote_bin fails injection-safe validation."""


def validate_ssh_host(ssh_host: str) -> str:
    """Return *ssh_host* if safe to place in an ssh argv, else raise.

    Accepts ``host``, ``host.fqdn``, an ssh-config alias, or ``user@host``.
    Rejects empty values, anything over 255 chars, any segment beginning with
    ``-`` (option injection), and any character outside the host charset.
    """
    if not ssh_host or not isinstance(ssh_host, str):
        raise SshValidationError("ssh_host must be a non-empty string")
    host = ssh_host.strip()
    if len(host) > _MAX_HOST_LEN:
        raise SshValidationError(f"ssh_host too long (>{_MAX_HOST_LEN} chars)")
    if host.startswith("-"):
        raise SshValidationError(f"ssh_host {host!r} must not start with '-' (option injection)")

    # Split an optional single user@ prefix; validate each segment.
    if host.count("@") > 1:
        raise SshValidationError(f"ssh_host {host!r} has more than one '@'")
    segments = host.split("@")
    for seg in segments:
        if not seg:
            raise SshValidationError(f"ssh_host {host!r} has an empty user/host segment")
        if seg.startswith("-"):
            raise SshValidationError(f"ssh_host segment {seg!r} must not start with '-'")
        if not _HOST_SEGMENT_RE.match(seg):
            raise SshValidationError(
                f"ssh_host segment {seg!r} contains invalid characters "
                f"(allowed: letters, digits, '.', '_', '-')"
            )
    return host


def validate_remote_bin(remote_bin: str) -> str:
    """Return *remote_bin* if safe to embed in the remote command, else raise.

    An empty string is allowed and means "use the candidate search". A non-empty
    value must match the path charset, not start with ``-``, and contain no shell
    metacharacters.
    """
    if remote_bin is None:
        return ""
    if not isinstance(remote_bin, str):
        raise SshValidationError("remote_bin must be a string")
    rb = remote_bin.strip()
    if not rb:
        return ""
    if rb.startswith("-"):
        raise SshValidationError(f"remote_bin {rb!r} must not start with '-'")
    if not _REMOTE_BIN_RE.match(rb):
        raise SshValidationError(
            f"remote_bin {rb!r} contains invalid characters "
            f"(allowed: letters, digits, '.', '_', '/', '~', '-', space)"
        )
    return rb
