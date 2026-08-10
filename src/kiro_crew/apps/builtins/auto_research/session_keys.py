"""Canonical Research Lab campaign and worker-session identifiers."""

from __future__ import annotations

import re

AUTO_RESEARCH_APP = "auto-research"
RESEARCH_SLOT_PREFIX = "research-"
_CAMPAIGN_ID_RE = re.compile(r"[a-f0-9]{8}")


def is_campaign_id(value: str) -> bool:
    """Return whether *value* is a canonical Research Lab campaign id."""
    return _CAMPAIGN_ID_RE.fullmatch(value) is not None


def research_slot_key(campaign_id: str) -> str:
    """Build the worker-slot key for a validated campaign id."""
    if not is_campaign_id(campaign_id):
        raise ValueError("invalid Research Lab campaign id")
    return f"{RESEARCH_SLOT_PREFIX}{campaign_id}"


def is_research_slot_key(value: str) -> bool:
    """Return whether *value* names a canonical Research Lab worker slot."""
    if not value.startswith(RESEARCH_SLOT_PREFIX):
        return False
    return is_campaign_id(value[len(RESEARCH_SLOT_PREFIX) :])


def is_owned_research_slot(value: str, app_name: str) -> bool:
    """Return whether app provenance owns a canonical Research Lab slot."""
    return app_name == AUTO_RESEARCH_APP and is_research_slot_key(value)
