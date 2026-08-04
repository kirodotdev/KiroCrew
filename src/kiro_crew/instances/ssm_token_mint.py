"""Mint a remote Kiro Crew dashboard token over AWS SSM (SSM transport).

The SSM-transport sibling of :mod:`kiro_crew.instances.token_mint`. Where the
SSH transport runs ``kirocrew token`` over ``ssh <host> <command>``, the SSM
transport runs the same subcommand over ``aws ssm send-command`` (via
:mod:`kiro_crew.cloud.ssm`, the launcher's existing send-command chokepoint) so
no code duplicates the AWS argv-building, polling, or output-redaction logic.

Security (standard practices, mirrors ``token_mint.py``):

* The minted token is a short-lived (≤20h) bearer credential. It is **never
  logged** and is returned only to the in-memory caller.
* ``aws ssm send-command`` is invoked via ``cloud.ssm.run_command``, itself
  routed through the ``cloud.aws.run_aws`` chokepoint — a fixed argv list, no
  shell on the local side. ``ssm_target``/``aws_profile``/``aws_region`` are
  injection-validated by the caller (tunnel manager) before reaching here.
* The *remote* command is the same ``kirocrew token``/``restart`` subcommand
  string :mod:`token_mint` builds (shared builders), so it inherits the same
  candidate-search / run-marker resolution and charset-validated
  ``remote_bin``.
* SSM send-command output transits SSM's command-invocation history (accepted
  trade-off, same as ``cloud/connect.py::mint_token`` — short TTL, loopback-only
  usability, and the launcher's chokepoint denies ``ssm:ListCommandInvocations``
  to a leaked/agent credential). This module does not change that posture; it
  reuses the existing, reviewed ``cloud.ssm.run_command`` path.
"""

from __future__ import annotations

import asyncio
import logging
import re

from kiro_crew.cloud import ssm as cloud_ssm
from kiro_crew.instances.token_mint import (
    TokenMintError,
    build_remote_command,
    parse_token_from_stdout,
)
from kiro_crew.instances.validation import (
    SsmValidationError,
    validate_aws_profile,
    validate_aws_region,
    validate_ssm_run_as,
    validate_ssm_target,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

# How long to wait for the SSM send-command invocation to finish. Generous vs.
# the SSH transport's 30s timeout: send-command has its own dispatch latency
# (agent poll interval) on top of the remote command's own runtime.
_DEFAULT_MINT_TIMEOUT_SECS = 90

# Same ttl shape token_mint.py accepts (kept local rather than importing a
# "private" helper cross-module — see module docstring).
_TTL_RE = re.compile(r"^[1-9][0-9]{0,3}[hm]$")

# How much of a failing remote's stdout/stderr to carry in an error message.
_OUTPUT_TAIL_CHARS = 300


def _validate_ttl(ttl: str) -> str:
    if not _TTL_RE.match(ttl):
        raise TokenMintError(
            f"invalid ttl {ttl!r}: expected a positive integer with 'h' or 'm' "
            "suffix (e.g. '20h', '30m')"
        )
    return ttl


def _redacted_tail(text: str, limit: int = _OUTPUT_TAIL_CHARS) -> str:
    """Credential/exfil-redact *text* and return its last *limit* chars.

    Mirrors :func:`kiro_crew.instances.token_mint._redacted_output_tail`'s
    intent (never let a token or credential-looking string reach a raised
    exception's message) but is not called on an unbounded remote payload here:
    :func:`cloud.ssm.run_command`'s ``CommandResult.stdout``/``stderr`` are
    already SSM-invocation-output-sized (not a giant blind stream), so no
    scan-window bounding is needed.
    """
    if not text:
        return ""
    safe = redact_exfiltration_urls(redact_credentials(text)[0])[0]
    return safe.strip()[-limit:]


def _validate_port(port: int | None) -> int | None:
    if port is None:
        return None
    try:
        p = int(port)
    except (TypeError, ValueError) as e:
        raise TokenMintError(f"invalid port {port!r}: not an integer") from e
    if not (1 <= p <= 65535):
        raise TokenMintError(f"invalid port {p}: out of range 1-65535")
    return p


async def mint_remote_token_ssm(
    ssm_target: str,
    *,
    aws_profile: str = "",
    aws_region: str = "",
    ssm_run_as: str = "",
    remote_bin: str = "",
    ttl: str = "20h",
    remote_port: int | None = None,
    embed_parent_port: int | None = None,
    timeout_secs: float = _DEFAULT_MINT_TIMEOUT_SECS,
) -> str:
    """Run ``kirocrew token`` on *ssm_target* over SSM and return the parsed JWT.

    Mirrors :func:`kiro_crew.instances.token_mint.mint_remote_token`'s contract
    (same subcommand shape, same ``TokenMintError`` on failure) but dispatches
    over ``aws ssm send-command`` instead of ``ssh``. Runs in a worker thread
    (``asyncio.to_thread``) since :func:`cloud.ssm.run_command` blocks
    synchronously while polling ``get-command-invocation``.
    """
    ttl = _validate_ttl(ttl)
    target = validate_ssm_target(ssm_target)
    profile = validate_aws_profile(aws_profile)
    region = validate_aws_region(aws_region)
    run_as = validate_ssm_run_as(ssm_run_as)
    port = _validate_port(remote_port)
    embed_port = _validate_port(embed_parent_port)

    subcommand = "token"
    if ttl:
        subcommand += f" --ttl {ttl}"
    if port:
        subcommand += f" --port {port}"
    if embed_port:
        subcommand += f" --embed-parent-port {embed_port}"
    remote_command = build_remote_command(remote_bin, subcommand, marker_port=port)

    # False positive (below): the message mentions "token" (this function's job)
    # but the interpolated values are the SSM target id and the ttl — never the
    # credential itself, which is returned to the in-memory caller only and is
    # never logged (a documented invariant of both mint modules). The rule keys
    # off the word in the format string, so it cannot see that.
    # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
    logger.info("Minting token on %s over SSM (ttl=%s)", target, ttl)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                cloud_ssm.run_command,
                target,
                remote_command,
                profile,
                region,
                run_as=run_as,
                total_wait=int(timeout_secs),
            ),
            timeout=timeout_secs + 15,
        )
    except asyncio.TimeoutError as e:
        raise TokenMintError(f"timed out minting token on {target} over SSM") from e
    except SsmValidationError as e:
        raise TokenMintError(f"invalid SSM settings for {target}: {e}") from e
    except Exception as e:  # AWSError etc. from the aws chokepoint
        raise TokenMintError(f"SSM send-command failed for {target}: {e}") from e

    if not result.ok:
        raise TokenMintError(
            f"remote token mint on {target} over SSM exited "
            f"status={result.status} code={result.exit_code}: "
            f"stderr: {_redacted_tail(result.stderr) or '<none>'} | "
            f"stdout tail: {_redacted_tail(result.stdout)}"
        )

    token = parse_token_from_stdout(result.stdout)
    if not token:
        raise TokenMintError(
            f"could not parse a token from {target} output over SSM "
            f"(stderr: {_redacted_tail(result.stderr) or '<none>'})"
        )
    return token


async def run_remote_kirocrew_ssm(
    ssm_target: str,
    subcommand: str,
    *,
    aws_profile: str = "",
    aws_region: str = "",
    ssm_run_as: str = "",
    remote_bin: str = "",
    marker_port: int | None = None,
    timeout_secs: float = 90.0,
) -> tuple[int, str]:
    """Run ``kirocrew <subcommand>`` on *ssm_target* over SSM.

    The SSM-transport sibling of
    :func:`kiro_crew.instances.token_mint.run_remote_kirocrew`. Returns
    ``(returncode, stderr_tail)``; ``-1`` on a validation/dispatch failure.
    """
    try:
        target = validate_ssm_target(ssm_target)
        profile = validate_aws_profile(aws_profile)
        region = validate_aws_region(aws_region)
        run_as = validate_ssm_run_as(ssm_run_as)
    except SsmValidationError as e:
        return -1, f"invalid SSM settings: {e}"

    remote_command = build_remote_command(
        remote_bin, subcommand, marker_port=_validate_port(marker_port)
    )
    logger.info("Running 'kirocrew %s' on %s over SSM", subcommand, target)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                cloud_ssm.run_command,
                target,
                remote_command,
                profile,
                region,
                run_as=run_as,
                total_wait=int(timeout_secs),
            ),
            timeout=timeout_secs + 15,
        )
    except asyncio.TimeoutError:
        return -1, f"timed out after {timeout_secs}s"
    except Exception as e:
        return -1, f"SSM send-command failed: {e}"
    return result.exit_code, _redacted_tail(result.stderr or "")
