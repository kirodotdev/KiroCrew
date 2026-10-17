"""Gateway-only admission of provider-generated SharePoint locators."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from kiro_crew import locator_attestations
from kiro_crew.platform.context import installed_context


def sharepoint_attestation_policy(
    mcp_server_name: str, tool_name: str
) -> tuple[frozenset[str], frozenset[str]]:
    """Return declared result fields and exact trusted hosts, or empty sets.

    The no-context answer is no attestation. Every adapter/declaration failure is
    the same answer, so URL text cannot become trust when composition is absent.
    """
    if not mcp_server_name or not tool_name:
        return frozenset(), frozenset()
    try:
        ctx = installed_context()
        if ctx is None:
            return frozenset(), frozenset()
        fields_getter = getattr(ctx.credentials, "provider_generated_sharepoint_fields", None)
        hosts_getter = getattr(ctx.credentials, "exempt_exact_hosts", None)
        if fields_getter is None or hosts_getter is None:
            return frozenset(), frozenset()
        declared = fields_getter()
        fields = (
            declared.get((mcp_server_name, tool_name), ()) if isinstance(declared, dict) else ()
        )
        hosts = hosts_getter()
        if not isinstance(fields, (set, frozenset, tuple, list)) or not isinstance(
            hosts, (set, frozenset, tuple, list)
        ):
            return frozenset(), frozenset()
        clean_fields = frozenset(field for field in fields if isinstance(field, str))
        clean_hosts = frozenset(host for host in hosts if isinstance(host, str))
        if not clean_fields or not clean_hosts:
            return frozenset(), frozenset()
        return clean_fields, clean_hosts
    except Exception:
        return frozenset(), frozenset()


def attested_sharepoint_digests(text: str) -> frozenset[str]:
    """Return the current immutable registry view without waiting for storage."""
    return locator_attestations.digests_for_text(text)


def attest_tool_result(
    *,
    update: dict[str, Any],
    raw_input: Mapping[str, Any],
    declared_fields: Iterable[str],
    trusted_hosts: Iterable[str],
) -> int:
    """Persist attestations after the event-loop boundary proved provenance."""
    if update.get("status") != "completed":
        return 0
    return locator_attestations.attest_provider_result(
        update=update,
        raw_input=raw_input,
        declared_fields=declared_fields,
        trusted_hosts=trusted_hosts,
    )
