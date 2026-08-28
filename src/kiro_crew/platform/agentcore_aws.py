"""Optional AWS AgentCore adapter — installed by IaC or on configure.

The public ``DefaultAgentIdentityProvider`` stays a no-op. Bootstrap
imports this module on standalone boot and attaches the adapter only
when :func:`opted_in` is true (composed-ceiling posture
``workload``/``login``, or — with no ceiling — env posture or a
named workload plus ``KIROCREW_AGENTCORE_AWS=1``) **and**
``kirocrew[agentcore]`` is already installed. Boot does not pip;
:func:`ensure_extra` stays on the Settings PUT / install.sh path.
``boto3`` is loaded inside methods so
``import kiro_crew.platform.agentcore_aws`` does not pull AWS into a
process that never opted in.

A workload access token is first-party Identity material. It is never the
Gateway inbound credential and never appears in ``status()``. Workload
``gateway_mcp_spec()`` returns a localhost SigV4 proxy URL, not the
unsigned Gateway hostname.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
import os
import subprocess
import sys
import sysconfig
import threading
import time
from pathlib import Path
from typing import Any

from kiro_crew.constants import env_flag_enabled
from kiro_crew.platform.agentcore_schema import (
    normalize_agentcore_gateway_url,
    normalize_agentcore_workload_name,
)
from kiro_crew.platform.interfaces import (
    AgentIdentityProvider,
    InboundToken,
    SessionPrincipal,
    WorkloadIdentity,
)
from kiro_crew.platform_compat import is_bundled_interpreter

logger = logging.getLogger(__name__)

ENV_AWS = "KIROCREW_AGENTCORE_AWS"
ENV_WORKLOAD = "KIROCREW_AGENTCORE_WORKLOAD_NAME"
ENV_GATEWAY_URL = "KIROCREW_AGENTCORE_GATEWAY_URL"
ENV_POSTURE = "KIROCREW_AGENTCORE_POSTURE"
ENV_PROJECT_DIR = "KIROCREW_PROJECT_DIR"
# RFC hand-rolled / Settings-only default when no systemd name is set.
DEFAULT_WORKLOAD_NAME = "kirocrew"
EXTRA_CODE_OK = "ok"
EXTRA_CODE_NO_CHANNEL = "no_install_channel"
EXTRA_CODE_FAILED = "install_failed"
EXTRA_REQ_WHEEL = "kirocrew[agentcore]"
# boto3 client name (lazy). Not the ``bedrock-agentcore`` SDK package.
_CLIENT = "bedrock-agentcore"
# Catalog identity probe — vend-and-discard. Never a snapshot field.
IDENTITY_PROBE_OK = "ok"
IDENTITY_PROBE_SKIP_LOGIN = "login_needs_sign_in"
IDENTITY_PROBE_NOT_NAMED = "not_named"
IDENTITY_PROBE_SERVICE_LINKED = "service_linked"
IDENTITY_PROBE_DENIED = "identity_denied"
IDENTITY_PROBE_NOT_FOUND = "identity_not_found"
IDENTITY_PROBE_ERROR = "identity_error"
IDENTITY_PROBE_EXTRA = "extra_missing"
_JWT_FALLBACK_TTL_SECS = 300.0
_PIP_TIMEOUT_SECS = 180
_CONFIGURED_POSTURES = frozenset({"workload", "login"})
_ENSURE_LOCK = threading.Lock()


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def extra_available() -> bool:
    """True when the ``agentcore`` extra (boto3) can be imported."""
    try:
        import boto3  # noqa: F401
    except ImportError:
        return False
    return True


def pip_install_channel_available() -> bool:
    """True when ``<gateway python> -m pip install`` can plausibly succeed.

    Same three dead-ends as the voice extra: a bundled desktop interpreter
    (writes break the code-signed tree), a missing ``pip`` module, and a
    PEP 668 externally-managed interpreter outside a venv. Duplicated here
    so the platform layer never imports the dashboard.
    """
    if is_bundled_interpreter():
        return False
    if importlib.util.find_spec("pip") is None:
        return False
    if sys.prefix != sys.base_prefix:
        return True
    return not (Path(sysconfig.get_path("stdlib")) / "EXTERNALLY-MANAGED").exists()


def authored_agentcore_row() -> dict[str, Any] | None:
    """Enabled ``capabilities.agentcore`` object from the standalone home file.

    Peek only — do not parse_policy. Bootstrap and Settings need to see a
    just-written file even when the running ceiling is still boot-frozen.
    """
    from kiro_crew.platform.governance import _policy_home_path

    path = _policy_home_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    caps = data.get("capabilities")
    if not isinstance(caps, dict):
        return None
    row = caps.get("agentcore")
    if not isinstance(row, dict) or not row.get("enabled"):
        return None
    return row


def authored_posture() -> str | None:
    """Posture authored in the standalone home file, if any."""
    row = authored_agentcore_row()
    if row is None:
        return None
    posture = str(row.get("posture") or "").strip().lower()
    return posture if posture in _CONFIGURED_POSTURES else None


def authored_gateway_url() -> str:
    """Gateway MCP URL authored in the standalone home file, if any."""
    row = authored_agentcore_row()
    if row is None:
        return ""
    raw = row.get("gateway_url")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        return normalize_agentcore_gateway_url(raw)
    except ValueError:
        return ""


def authored_workload_name() -> str:
    """Workload identity name authored in the standalone home file, if any."""
    row = authored_agentcore_row()
    if row is None:
        return ""
    raw = row.get("workload_name")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        return normalize_agentcore_workload_name(raw)
    except ValueError:
        return ""


def resolved_posture() -> str:
    """Effective ceiling first; home/env only when no ceiling exists.

    A loaded fleet / central / home document is the only posture source
    once present — a home-file peek must not outrank a ceiling that
    disabled AgentCore or pinned a different posture.
    """
    from kiro_crew.platform.governance import agentcore_posture

    ceiling = _effective_governance_ceiling()
    if ceiling is not None:
        stored = agentcore_posture(ceiling)
        return stored if stored in _CONFIGURED_POSTURES else ""
    authored = authored_posture()
    if authored:
        return authored
    env = _env(ENV_POSTURE).lower()
    return env if env in _CONFIGURED_POSTURES else ""


def resolved_gateway_url() -> str:
    """Effective ceiling first; home/env only when no ceiling exists."""
    from kiro_crew.platform.governance import agentcore_gateway_url

    ceiling = _effective_governance_ceiling()
    if ceiling is not None:
        return agentcore_gateway_url(ceiling)
    authored = authored_gateway_url()
    if authored:
        return authored
    raw = _env(ENV_GATEWAY_URL)
    if not raw:
        return ""
    try:
        return normalize_agentcore_gateway_url(raw)
    except ValueError:
        logger.warning("KIROCREW_AGENTCORE_GATEWAY_URL is not a usable https URL")
        return ""


def _effective_governance_ceiling() -> Any:
    """Composed security ceiling, or ``None`` when no policy document loaded.

    ``load_security_policy`` already walks fleet env → central → home.
    Boot opt-in must use that document, not a home-file peek that can
    enable AgentCore after a fleet ceiling disabled it.
    """
    from kiro_crew.platform.governance import load_security_policy

    try:
        return load_security_policy()
    except Exception:
        return None


def opted_in() -> bool:
    """True when the effective ceiling or launch env configured AgentCore.

    A loaded policy document is the only opt-in source: fleet
    ``KIROCREW_SECURITY_POLICY`` / central distribution outrank the home
    file, so a home ``enabled: true`` cannot install the extra when the
    administrator disabled it. Launch env (CFN systemd) is consulted only
    when there is no ceiling. A leftover ``KIROCREW_AGENTCORE_AWS=1``
    still requires a workload name.
    """
    from kiro_crew.platform.governance import agentcore_posture

    ceiling = _effective_governance_ceiling()
    if ceiling is not None:
        return agentcore_posture(ceiling) in _CONFIGURED_POSTURES
    if _env(ENV_POSTURE).lower() in _CONFIGURED_POSTURES:
        return True
    return env_flag_enabled(ENV_AWS) and bool(_env(ENV_WORKLOAD))


def resolved_workload_name() -> str:
    """Effective ceiling first; home/env only when no ceiling exists.

    A crew that names ``kirocrew-e2e`` in the ceiling must not vend
    against leftover ``KIROCREW_AGENTCORE_WORKLOAD_NAME=kirocrew``.
    Ceiling-present but unnamed stays unnamed (catalog ``not_named``).
    Launch env posture still uses the RFC default when CFN omitted the
    systemd name and no ceiling is loaded.
    """
    from kiro_crew.platform.governance import agentcore_workload_name

    ceiling = _effective_governance_ceiling()
    if ceiling is not None:
        return agentcore_workload_name(ceiling)
    authored = authored_workload_name()
    if authored:
        return authored
    name = _env(ENV_WORKLOAD)
    if name:
        return name
    # Settings-only posture without a name stays unnamed (catalog
    # ``not_named``). Inventing ``kirocrew`` here is how a named identity
    # in the account is never the one we vend. Launch env posture still
    # uses the RFC default when CFN omitted the systemd name.
    if authored_posture() in _CONFIGURED_POSTURES:
        return ""
    if _env(ENV_POSTURE).lower() in _CONFIGURED_POSTURES:
        return DEFAULT_WORKLOAD_NAME
    return ""


def probe_workload_identity() -> dict[str, object]:
    """Vend-and-discard a WAT so Settings can see a wrong identity name.

    Never returns the token. Login posture skips: this page has no user
    JWT and a login instance role only has ``GetWorkloadAccessTokenForJWT``.
    """
    name = resolved_workload_name()
    if resolved_posture() == "login":
        return {"ok": True, "detail": IDENTITY_PROBE_SKIP_LOGIN, "name": name}
    if not name:
        return {"ok": False, "detail": IDENTITY_PROBE_NOT_NAMED, "name": ""}
    client = _client()
    if client is None:
        return {"ok": False, "detail": IDENTITY_PROBE_EXTRA, "name": name}
    try:
        resp = client.get_workload_access_token(workloadName=name)
    except Exception as exc:
        return {"ok": False, "detail": _classify_identity_error(exc), "name": name}
    token = resp.get("workloadAccessToken") if isinstance(resp, dict) else None
    if isinstance(token, str) and token:
        return {"ok": True, "detail": IDENTITY_PROBE_OK, "name": name}
    return {"ok": False, "detail": IDENTITY_PROBE_ERROR, "name": name}


def _classify_identity_error(exc: BaseException) -> str:
    """Map a WAT failure to a machine code. Never include the exception text."""
    name = type(exc).__name__
    code = ""
    raw = getattr(exc, "response", None)
    if isinstance(raw, dict):
        err = raw.get("Error")
        if isinstance(err, dict):
            code = str(err.get("Code") or "")
    blob = f"{name} {code} {exc}".lower()
    if "linked to a service" in blob:
        return IDENTITY_PROBE_SERVICE_LINKED
    if "accessdenied" in blob or "unauthorized" in blob or "forbidden" in blob:
        return IDENTITY_PROBE_DENIED
    if "resourcenotfound" in blob or "notfound" in blob:
        return IDENTITY_PROBE_NOT_FOUND
    return IDENTITY_PROBE_ERROR


def extra_snapshot(*, last_code: str | None = None) -> dict[str, object]:
    """Identity API fields for whether the extra is importable.

    GET never pips. ``last_code`` is the result of a just-ran
    :func:`ensure_extra` (PUT / bootstrap).
    """
    installed = extra_available()
    if last_code is not None:
        code: str | None = last_code
    elif installed:
        code = EXTRA_CODE_OK
    elif not pip_install_channel_available():
        code = EXTRA_CODE_NO_CHANNEL
    else:
        code = None
    return {"extra_installed": installed, "extra_code": code}


def _extra_install_argv() -> list[str]:
    """Install into this interpreter. Checkout extra when the tree is present."""
    root = _env(ENV_PROJECT_DIR)
    if root:
        setup = Path(root) / "setup.cfg"
        if setup.is_file():
            return [sys.executable, "-m", "pip", "install", "-e", f"{root}[agentcore]"]
    return [sys.executable, "-m", "pip", "install", EXTRA_REQ_WHEEL]


def ensure_extra() -> str:
    """Install ``kirocrew[agentcore]`` into the gateway interpreter if needed.

    Returns ``ok``, ``no_install_channel``, or ``install_failed``. Does not
    uninstall when posture is later turned off. Never raises — a failed
    pip must not block the policy write that triggered it.
    """
    with _ENSURE_LOCK:
        if extra_available():
            return EXTRA_CODE_OK
        if not pip_install_channel_available():
            logger.warning(
                "AgentCore extra is configured but this interpreter cannot "
                "pip-install (bundled, no pip, or PEP 668)"
            )
            return EXTRA_CODE_NO_CHANNEL
        argv = _extra_install_argv()
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_PIP_TIMEOUT_SECS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("AgentCore extra install failed to run", exc_info=True)
            return EXTRA_CODE_FAILED
        if result.returncode != 0 or not extra_available():
            logger.warning(
                "AgentCore extra install failed (rc=%s): %s",
                result.returncode,
                (result.stderr or result.stdout or "").strip()[-500:],
            )
            return EXTRA_CODE_FAILED
        return EXTRA_CODE_OK


def apply_agentcore_runtime() -> bool:
    """Hot-apply a Settings/home-file AgentCore write onto the running context.

    Catalog already reads the file. Inject, login-withhold, and
    ``_identity_on`` still key on the boot-frozen ceiling and the
    boot-swapped adapter. Owner-dashboard PUT is the operator's
    out-of-band write of that keystone — reload both so Save is
    sufficient. Returns False when the extra is missing or reload fails;
    the UI then keeps ``restart_required``.
    """
    from dataclasses import replace

    from kiro_crew.platform.context import current_context, set_context
    from kiro_crew.platform.defaults import DefaultAgentIdentityProvider
    from kiro_crew.platform.governance import load_security_policy

    try:
        ceiling = load_security_policy()
    except Exception:
        logger.warning("AgentCore runtime apply: policy reload failed", exc_info=True)
        return False
    ctx = current_context()
    adapter: AgentIdentityProvider
    if opted_in():
        aws_adapter = try_aws_agent_identity()
        if aws_adapter is None:
            set_context(replace(ctx, governance=ceiling))
            return False
        adapter = aws_adapter
    else:
        adapter = DefaultAgentIdentityProvider()
    set_context(replace(ctx, governance=ceiling, agent_identity=adapter))
    return True


def try_aws_agent_identity() -> "AwsAgentIdentityProvider | None":
    """Return the AWS adapter when opted in and boto3 is installed, else None."""
    if not opted_in():
        return None
    if not extra_available():
        logger.warning(
            "AgentCore identity is configured but boto3 is missing; "
            "install kirocrew[agentcore] (Settings save and the EC2 template do this)"
        )
        return None
    return AwsAgentIdentityProvider()


class AwsAgentIdentityProvider:
    """AgentIdentityProvider backed by instance-role boto3 calls."""

    def enabled(self) -> bool:
        return bool(resolved_workload_name())

    def workload_identity(self) -> WorkloadIdentity | None:
        name = resolved_workload_name()
        if not name:
            return None
        return WorkloadIdentity(name=name, arn=_workload_arn(name))

    def status(self) -> dict[str, object]:
        posture = resolved_posture()
        kind = "m2m" if posture == "workload" else "user"
        return {
            "credentialKind": kind,
            "vaultedOwnerToken": False,
            "gatewayUrlConfigured": bool(resolved_gateway_url()),
            "adapter": "aws",
        }

    def gateway_mcp_spec(self) -> dict[str, object] | None:
        url = resolved_gateway_url()
        if not url.startswith("https://"):
            return None
        # Workload inbound is IAM. kiro-cli cannot SigV4, so the spec URL
        # is the localhost proxy — never the unsigned Gateway hostname.
        from kiro_crew.platform.agentcore_sigv4 import ensure_workload_proxy

        if resolved_posture() == "workload":
            listen = ensure_workload_proxy(url)
            if not listen:
                return None
            return {"url": listen}
        return {"url": url}

    async def annotate_principal(self, principal: SessionPrincipal) -> SessionPrincipal:
        return principal

    async def vend_workload_access_token(self, principal: SessionPrincipal) -> str | None:
        name = resolved_workload_name()
        if not name:
            return None
        client = _client()
        if client is None:
            return None
        try:
            if principal.user_jwt:
                resp = client.get_workload_access_token_for_jwt(
                    workloadName=name, userToken=principal.user_jwt
                )
            else:
                resp = client.get_workload_access_token(workloadName=name)
        except Exception:
            logger.warning("GetWorkloadAccessToken failed; no token", exc_info=True)
            return None
        token = resp.get("workloadAccessToken") if isinstance(resp, dict) else None
        return token if isinstance(token, str) and token else None

    async def vend_gateway_inbound_token(self, principal: SessionPrincipal) -> InboundToken | None:
        # WAT is first-party only. Login inbound is the operator IdP JWT
        # Gateway's CUSTOM_JWT authorizer already accepts.
        jwt = principal.user_jwt
        if not jwt:
            return None
        return InboundToken(
            scheme="bearer",
            token=jwt,
            expires_at=_jwt_exp(jwt),
            audience=resolved_gateway_url(),
        )


def _client() -> Any:
    try:
        import boto3
    except ImportError:
        return None
    return boto3.client(_CLIENT)


def _workload_arn(name: str) -> str:
    """Best-effort ARN from the instance session. Empty account stays explicit."""
    try:
        import boto3
    except ImportError:
        return ""
    session = boto3.session.Session()
    region = session.region_name or _env("AWS_REGION") or _env("AWS_DEFAULT_REGION") or "us-east-1"
    account = ""
    try:
        account = str(boto3.client("sts").get_caller_identity().get("Account") or "")
    except Exception:
        logger.debug("STS account lookup failed; ARN omits account", exc_info=True)
    if not account:
        account = "unknown"
    return (
        f"arn:aws:bedrock-agentcore:{region}:{account}:"
        f"workload-identity-directory/default/workload-identity/{name}"
    )


def _jwt_exp(token: str) -> float:
    """Read ``exp`` from an unverified JWT payload. Fallback is a short TTL."""
    try:
        payload = token.split(".")[1]
        pad = "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload + pad))
        exp = data.get("exp")
        if isinstance(exp, (int, float)):
            return float(exp)
    except Exception:
        logger.debug("inbound JWT exp unreadable; using fallback TTL", exc_info=True)
    return time.time() + _JWT_FALLBACK_TTL_SECS
