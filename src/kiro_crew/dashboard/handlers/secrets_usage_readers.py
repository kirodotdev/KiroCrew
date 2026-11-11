"""The integrations that read a vault entry by name, for ``GET /api/secrets/usage``.

Each scanner takes the names from its owner module's own rule, reads config only
(never a vault value) and lists an entry only while that integration is set to
read it now. A scanner that cannot read its config raises, and the caller marks
its kind incomplete.

Owners covered: Bitbucket Server host tokens, the Linear API key, the Jev
decision provider key, the OTLP trace headers, Sprites profiles and the
``kirocrew secrets authorize`` approvals. A reader added to main later shows as
no use found until it is added here.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

KIND_INTEGRATION = "integration"
KIND_APPROVED_API = "approved_api"
KIND_SPRITES_PROFILE = "sprites_profile"

Add = Callable[[str, dict[str, str]], None]


def _place(kind: str, name: str, key: str = "") -> dict[str, str]:
    return {"kind": kind, "name": name, "key": key}


def scan_bitbucket_server(config: Any, stored: frozenset[str], add: Add) -> None:
    """Per-host tokens, then the shared name for a lone host, as the provider reads them."""
    from kiro_crew.dashboard.source_providers.bitbucket_server import (
        GLOBAL_TOKEN_NAME,
        host_token_name,
    )

    hosts = sorted(set(config.dashboard.bitbucket_server_hosts))
    for host in hosts:
        names = [host_token_name(host)]
        if len(hosts) == 1:
            # The provider falls back to the shared name when the per-host entry
            # is empty or unreadable. Whether it is cannot be told without
            # reading the value, so both count while they are stored.
            names.append(GLOBAL_TOKEN_NAME)
        for name in names:
            if name in stored:
                add(name, _place(KIND_INTEGRATION, "Bitbucket Server", host))


def scan_linear(config: Any, stored: frozenset[str], add: Add) -> None:
    """The Linear key: read on every Linear link, with no switch to turn it off."""
    from kiro_crew.dashboard.source_providers.linear import API_KEY_NAME

    if API_KEY_NAME in stored:
        add(API_KEY_NAME, _place(KIND_INTEGRATION, "Linear"))


def scan_decisions(config: Any, stored: frozenset[str], add: Add) -> None:
    """The Jev key: referenced by ``decisions.provider.api_key`` and consented to."""
    from kiro_crew.decisions import consent
    from kiro_crew.decisions.impl_sagemaker import is_sagemaker_endpoint
    from kiro_crew.decisions.impl_systemone import HOSTED_TYPESAFE, VAULT_SECRET_NAME
    from kiro_crew.decisions.local_models import is_loopback_endpoint

    provider = config.decisions.provider
    if VAULT_SECRET_NAME not in stored:
        return
    # The name as ``resolve_api_key`` reads it: the prefix, then the name with
    # the spaces around it dropped.
    value = (provider.api_key or "").strip()
    if not value.startswith("secret://") or value[len("secret://") :].strip() != VAULT_SECRET_NAME:
        return
    # The endpoint the provider sends to, as it picks it. A loopback one is sent
    # no key, and a ``sagemaker://`` one goes to the SageMaker provider, which
    # signs with AWS credentials and never reads this entry.
    endpoint = str(provider.endpoint or HOSTED_TYPESAFE.endpoint)
    if is_loopback_endpoint(endpoint) or is_sagemaker_endpoint(endpoint):
        return
    if consent.permits(endpoint):
        add(VAULT_SECRET_NAME, _place(KIND_INTEGRATION, "Jev"))


def scan_otlp(config: Any, stored: frozenset[str], add: Add) -> None:
    """Vault-sourced OTLP headers of the trace destination the gateway resolves now."""
    from kiro_crew.tracing import otlp

    dest = otlp.resolve(config.telemetry)
    for header, kind, ref in dest.headers if dest is not None else ():
        if kind == otlp.FROM_VAULT and ref in stored:
            add(ref, _place(KIND_INTEGRATION, "OpenTelemetry", header))


def scan_sprites(config: Any, stored: frozenset[str], add: Add) -> None:
    """The token each configured Sprites profile names."""
    for profile in config.session_environments.sprites_profiles:
        name = str(profile.get("token_secret") or "")
        if name in stored:
            add(name, _place(KIND_SPRITES_PROFILE, str(profile.get("name") or profile.get("id"))))


def scan_sprites_resources(data_home: Path, stored: frozenset[str], add: Add) -> None:
    """The token each saved Sprites machine was made with.

    A machine keeps its token name after its profile is removed or renamed, and
    archive, reopen and the retention delete still read it, so it counts while
    the row exists. An unreadable registry raises.
    """
    from kiro_crew.session_environments.persistent_vm import SessionResourceRegistry
    from kiro_crew.sprites_session.launch import SpritesSessionEnvironments as cls

    for row in SessionResourceRegistry(data_home / cls.registry_file, cls.label).all():
        if row.token_secret in stored:
            add(row.token_secret, _place(KIND_SPRITES_PROFILE, row.profile_id, row.name))


def scan_approved_apis(config_dir: Path, stored: frozenset[str], add: Add) -> None:
    """Entries ``kirocrew secrets authorize`` approved for an origin, by the policy's own check.

    An absent policy approves nothing; one that exists but cannot be opened
    raises, so the scan is incomplete rather than "nothing approved".
    """
    from kiro_crew.secrets_mediation.policy import PolicyError, load_authorization, policy_path

    path = policy_path(config_dir)
    try:
        with open(path, "rb"):
            pass
    except FileNotFoundError:
        return
    for name in sorted(stored):
        try:
            authorization = load_authorization(config_dir, name)
        except PolicyError:
            continue
        add(name, _place(KIND_APPROVED_API, authorization.origin))
