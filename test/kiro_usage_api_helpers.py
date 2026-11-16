"""The response and credential-candidate builders the kiro_usage_api tests share.

A dedicated ``*_helpers.py`` imported by bare name, per the repo convention: no
``test_*`` module imports another. ``test_kiro_usage_api`` and
``test_kiro_usage_api_credential_choice`` both build their fakes here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import kiro_crew.dashboard.handlers.kiro_usage_api as api


def _resp(status: int, body: object) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    if isinstance(body, Exception):
        r.json.side_effect = body
    else:
        r.json.return_value = body
    return r


def _cand(value: str, *, own: bool = True, hours: float = 1.0):
    """Build a credential candidate for tests.

    ``own`` defaults to True (kiro-cli's own auth store) so ARN-anchored tests stay
    focused on the ARN check rather than incidentally tripping the provenance one.
    Pass ``own=False`` for the JSON SSO caches / another product's store.
    """
    expiry = datetime.now(timezone.utc) + timedelta(hours=hours)
    return api._Candidate(value, expiry, own)
