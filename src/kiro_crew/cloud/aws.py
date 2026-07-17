"""The single ``aws`` CLI chokepoint for the cloud launcher.

Every AWS interaction in :mod:`kiro_crew.cloud` goes through :func:`run_aws`.
Nothing else in the package shells out to ``aws`` directly. This mirrors
``deploy_web/engine.py`` and gives the module the same testability: unit tests
monkeypatch this one function.

Credentials are resolved by the ``aws`` CLI's own provider chain — KiroCrew
passes only ``--profile <name>`` (and never a raw key). The argv is always a
fixed list (no shell), and the invocation is routed through the OS-level sandbox
(:func:`kiro_crew.sandbox.wrap_argv`, ``mode="standard"``) exactly like the
deploy-web subprocess, because the ``aws`` CLI must read ``~/.aws`` to resolve
credentials.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any, Optional

from kiro_crew.sandbox import wrap_argv

logger = logging.getLogger(__name__)

# Default subprocess timeout for a single aws call (seconds). Long operations
# (cloudformation deploy waits) pass a larger explicit timeout.
DEFAULT_TIMEOUT = 60

# Substrings that identify an authorization failure in aws CLI stderr.
_AUTH_DENIED_MARKERS = (
    "AccessDenied",
    "not authorized",
    "UnauthorizedOperation",
    "is not authorized to perform",
)


class CloudActionDenied(Exception):
    """A cloud mutation was attempted from an agent/automation context.

    Cloud provisioning/teardown is a **human/installer action**. The shell-level
    ``deniedCommands`` block the ``aws`` CLI verbs and the ``kirocrew cloud``
    wrappers, but an agent could still import ``kiro_crew.cloud`` and call a
    mutating function (``ec2.destroy()`` / ``deploy()``) directly from a Python
    snippet — bypassing the shell denylist. :func:`assert_human_action` and the
    :func:`assert_chokepoint_allowed` guard add in-layer defense-in-depth for
    that path. These are **best-effort friction, not a hard boundary**: see
    :func:`assert_chokepoint_allowed` for the honest containment model (in the
    default ``standard`` sandbox the agent can reach ``~/.aws`` regardless, so
    the real ceiling is the least-privilege scope of the operator's own
    credentials; run the agent in ``strict``/``cc`` sandbox to wall it off).
    """


def _in_agent_session() -> bool:
    """True inside an agent/ACP subprocess (the gateway injects this var)."""
    return bool(os.environ.get("KIROCREW_SESSION_KEY"))


def assert_human_action(action: str) -> None:
    """Refuse a destructive/sensitive cloud mutation from an agent session.

    The gateway injects ``KIROCREW_SESSION_KEY`` into every agent/ACP subprocess
    (and MCP tools key off it). A human running ``kirocrew cloud …`` in their own
    terminal has no such var, so we fail closed whenever it IS set. This reaches
    the path the shell ``deniedCommands`` can't (a raw ``ec2.destroy()`` call
    never spawns the denied wrapper string).

    Honest scope: this is **best-effort friction, not a containment boundary**.
    ``KIROCREW_SESSION_KEY`` is an ordinary env var the agent's own code can
    ``del os.environ[...]`` before calling in-process, and in the default
    ``standard`` sandbox ``~/.aws`` is readable so the agent could bypass this
    module entirely (raw ``aws``/boto3). The actual ceiling on what an escaped
    agent can do to the account is the least-privilege scope of the operator's
    credentials (see :mod:`kiro_crew.cloud.iam`); to truly wall the agent off
    from cloud creds, run it under the ``strict``/``cc`` sandbox (which hides
    ``~/.aws``). We keep this guard because it deterministically stops the
    honest/accidental in-process call and costs nothing.
    """
    if _in_agent_session():
        raise CloudActionDenied(
            f"cloud mutation '{action}' is a human/installer action and is refused "
            "from an agent session — run `kirocrew cloud …` yourself in a terminal."
        )


# EXACT (service, operation) pairs an agent MAY run through the chokepoint —
# only the read-only calls that `cloud list`/`status`/`doctor` actually need.
# An *exact* allowlist (not a `get-*`/`list-*` prefix) is deliberate: prefix
# matching would wave through secret reads like `secretsmanager get-secret-value`,
# `ssm get-parameter --with-decryption`, and `ssm get-command-invocation` (which
# returns a prior human `send-command`'s output — potentially a dashboard token).
_AGENT_READ_ALLOWLIST: frozenset = frozenset(
    {
        ("sts", "get-caller-identity"),
        ("ec2", "describe-instances"),
        ("ec2", "describe-instance-status"),
        ("ec2", "describe-vpcs"),
        ("ec2", "describe-subnets"),
        ("ec2", "describe-route-tables"),
        ("ec2", "describe-instance-type-offerings"),
        ("ec2", "describe-security-groups"),
        ("cloudformation", "describe-stacks"),
        ("cloudformation", "describe-stack-events"),
        ("cloudformation", "describe-stack-resources"),
        ("cloudformation", "list-stacks"),
        ("ssm", "describe-instance-information"),
        ("resourcegroupstaggingapi", "get-resources"),
    }
)


def assert_chokepoint_allowed(args: list[str]) -> None:
    """Chokepoint guard: refuse non-allowlisted ``aws`` calls from an agent session.

    Defense-in-depth (NOT the primary boundary — see below). Sits in
    :func:`run_aws` itself, so beyond the per-op :func:`assert_human_action`
    guards it also covers the token paths (``ssm send-command`` in
    ``connect.mint_token``, ``ssm start-session`` in the tunnel/login flows) and
    any future caller that imports ``run_aws`` directly. Only the exact read-only
    ``(service, operation)`` pairs in :data:`_AGENT_READ_ALLOWLIST` pass under an
    agent session — so secret-bearing reads (``secretsmanager get-secret-value``,
    ``ssm get-parameter --with-decryption``, ``ssm get-command-invocation``) are
    denied, not just mutations.

    Honest containment model (no single hard boundary in the default posture):
    (1) cloud verbs are not MCP/LLM tools, so the model isn't handed them;
    (2) the shell ``deniedCommands`` block the CLI + wrapper strings from the
    ``execute_bash``/``shell`` tools; (3) this env-keyed chokepoint stops the
    in-process ``import kiro_crew.cloud`` path. All three are bypassable by a
    determined, code-executing agent (obfuscated shell string; ``del
    os.environ['KIROCREW_SESSION_KEY']``; raw ``aws``/boto3 when ``~/.aws`` is
    readable — which it IS in the default ``standard`` sandbox). The load-bearing
    control is therefore the **least-privilege IAM scope** of the operator's own
    credentials (:mod:`kiro_crew.cloud.iam` — tag/ARN-scoped, no IAM writes), plus
    the option to run the agent under the ``strict``/``cc`` sandbox that hides
    ``~/.aws`` entirely. These layers raise the bar and stop honest/accidental
    misuse; they are not a claim that a hostile in-process agent is fully
    contained.
    """
    if _in_agent_session() and (tuple(args[:2]) not in _AGENT_READ_ALLOWLIST):
        op = " ".join(args[:2]) if args else "(empty)"
        raise CloudActionDenied(
            f"cloud AWS action '{op}' is refused from an agent session (only "
            "specific read-only status/discovery calls are allowed) — run "
            "`kirocrew cloud …` yourself in a terminal."
        )


class AWSError(Exception):
    """An ``aws`` CLI call failed.

    Carries the failing ``action`` label and, when the failure is an
    authorization error, the ``missing_action`` the user must be granted so the
    caller can render a precise remediation hint (mirrors deploy-web).
    """

    def __init__(
        self,
        message: str,
        *,
        action: str = "",
        missing_action: Optional[str] = None,
        returncode: int = 1,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.action = action
        self.missing_action = missing_action
        self.returncode = returncode
        self.stderr = stderr


def _build_argv(args: list[str], profile: str, region: str) -> list[str]:
    """Build an ``aws`` argv with optional ``--profile`` / ``--region``.

    Never appends a raw credential — only the profile *name* and region, both of
    which the caller has already charset-validated (see ``cloud.iam`` /
    ``cloud.ec2``).
    """
    cmd = ["aws", *args]
    if profile:
        cmd += ["--profile", profile]
    if region:
        cmd += ["--region", region]
    return cmd


def run_aws(
    args: list[str],
    profile: str = "",
    region: str = "",
    *,
    timeout: int = DEFAULT_TIMEOUT,
    proc_sink: Optional[Any] = None,
) -> tuple[int, str, str]:
    """Run a single ``aws`` CLI command. Returns ``(returncode, stdout, stderr)``.

    This is the one subprocess chokepoint the whole ``cloud`` package uses and
    the single function unit tests monkeypatch. It never raises on a non-zero
    exit — callers decide whether to raise (see :func:`checked`).

    ``proc_sink``, if given, is called once with the live ``Popen`` right after
    spawn. A caller running this on a background thread (e.g. the wizard's deploy
    worker) can capture the handle and terminate the child from the main thread
    on Ctrl+C — otherwise a long ``cloudformation deploy`` (up to 1800s) would be
    orphaned when the interrupt hits the main thread rather than this one.

    NB: the sandbox scrubs ``AWS_SECRET*``/``AWS_SESSION*`` from the child env
    (deliberate credential-isolation floor, applied in all modes), so
    **env-var-only credentials are not supported** — auth must come from a
    profile / ``~/.aws`` (``aws configure`` or ``aws configure sso``).
    :func:`env_credentials_hint` detects that setup for a clear error message.

    Chokepoint guard: refuses non-read-only calls from an agent session (see
    :func:`assert_chokepoint_allowed`), so importing this function from a Python
    snippet can't run mutations or mint tokens behind the shell denylist.
    """
    assert_chokepoint_allowed(args)
    argv = _build_argv(args, profile, region)
    sandboxed, cleanup = wrap_argv(argv, mode="standard")
    proc: Optional[subprocess.Popen[str]] = None
    try:
        try:
            proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell, sandbox-wrapped
                sandboxed,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if proc_sink is not None:
                try:
                    proc_sink(proc)
                except Exception:  # pragma: no cover - a sink must never break the call
                    logger.debug("proc_sink raised; ignoring", exc_info=True)
        except FileNotFoundError:
            # `aws` (or the sandbox wrapper) is not installed — return the
            # conventional 127 instead of a raw traceback so the first
            # `cloud launch` on a bare install gets an actionable message.
            return (
                127,
                "",
                (
                    "aws CLI not found — install it first "
                    "(https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) "
                    "or run cloud-install.sh / install.ps1, then retry"
                ),
            )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            return 124, out or "", f"aws call timed out after {timeout}s: {' '.join(args[:2])}"
        return proc.returncode or 0, out or "", err or ""
    except KeyboardInterrupt:
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        raise
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass


def env_credentials_hint() -> str:
    """A hint when the user relies on env-var creds the sandbox scrubs.

    The sandbox strips ``AWS_SECRET*``/``AWS_SESSION*`` from every child env
    (its credential-isolation floor — never weakened per-callsite), so
    env-var-only auth cannot work through the launcher. Detect that setup and
    say so explicitly instead of a generic "could not resolve credentials".
    """
    if os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("AWS_SESSION_TOKEN"):
        return (
            "Detected AWS credentials in environment variables — the launcher's "
            "sandbox does not pass those through. Use a profile instead: "
            "`aws configure sso` (or `aws configure`), then rerun with "
            "--profile <name>."
        )
    return ""


def is_access_denied(stderr: str) -> bool:
    """True if the stderr text is an authorization (not client-side) error."""
    return any(marker in stderr for marker in _AUTH_DENIED_MARKERS)


def map_missing_action(stderr: str) -> Optional[str]:
    """Extract the exact ``service:Action`` the user was denied, if present.

    AWS AccessDenied messages typically read
    ``... is not authorized to perform: ec2:RunInstances on resource ...``.
    Returns that ``ec2:RunInstances`` token, else ``None``.
    """
    if not is_access_denied(stderr):
        return None
    marker = "to perform: "
    idx = stderr.find(marker)
    if idx == -1:
        return None
    tail = stderr[idx + len(marker) :]
    token = tail.split()[0] if tail.split() else ""
    # An action looks like "service:Action"; guard against stray punctuation.
    token = token.strip().rstrip(".,")
    return token or None


def checked(
    args: list[str],
    profile: str = "",
    region: str = "",
    *,
    action: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Run an ``aws`` call, raising :class:`AWSError` on failure.

    ``action`` is a human label for the operation (e.g. ``"ec2:RunInstances"``);
    on an authorization failure the precise denied action is attached as
    ``missing_action`` so the UI can tell the user exactly what to grant.
    """
    rc, out, err = run_aws(args, profile, region, timeout=timeout)
    if rc != 0:
        missing = map_missing_action(err)
        hint = ""
        if missing:
            hint = f" — grant `{missing}` in your IAM policy and retry"
        elif is_access_denied(err):
            hint = " — add the missing permission (see `kirocrew cloud iam-policy`) and retry"
        raise AWSError(
            f"{action} failed: {err.strip()[:400]}{hint}",
            action=action,
            missing_action=missing,
            returncode=rc,
            stderr=err,
        )
    return out


def checked_json(
    args: list[str],
    profile: str = "",
    region: str = "",
    *,
    action: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> Any:
    """Run a ``--output json`` ``aws`` call and parse the result.

    Raises :class:`AWSError` on a non-zero exit or unparseable output.
    """
    if "--output" not in args:
        args = [*args, "--output", "json"]
    out = checked(args, profile, region, action=action, timeout=timeout)
    try:
        return json.loads(out or "null")
    except json.JSONDecodeError as exc:
        raise AWSError(f"{action}: could not parse aws JSON output: {exc}", action=action) from exc
