"""Shared helper for triggering cron jobs via the dashboard API."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from kiro_crew.instances import run_marker
from kiro_crew.loopback_http import loopback_urlopen

_JOB_ID_RE = re.compile(r"[a-f0-9]{6,12}")
_TIMEOUT_SECS = 10  # endpoint returns immediately after starting execution


def trigger_cron_job(job_id: str, port: int, secret_path: Path) -> tuple[bool, str]:
    """POST to the gateway dashboard to trigger a cron job immediately.

    Returns (success, message) tuple.
    """
    if not _JOB_ID_RE.fullmatch(job_id):
        return False, f"Invalid job ID format: {job_id}"

    url = f"http://127.0.0.1:{port}/api/crons/{job_id}/run"
    headers: dict[str, str] = {}
    # Credential for the LISTENER first: the shared file has one slot per data
    # home and a second gateway generation replaces it, so reading a home-wide
    # file while another generation owns *port* is a guaranteed 403.
    #
    # The named *secret_path* is the FALLBACK, not an override, and that order is
    # dictated by the callers: both pass the home-wide file, which is precisely the
    # file the per-port read exists to outrank. Preferring the named path would
    # reinstate the defect.
    #
    # The cost, stated rather than hidden: a crash-orphaned per-port credential is
    # preferred over a correct named path, so a caller naming another home's
    # credential for a port this home once served would send the stale one. No
    # caller does that; closing it means this parameter going away, not the order
    # flipping.
    secret = run_marker.read_secret(port)
    if not secret and secret_path.exists():
        secret = secret_path.read_text().strip()
    if secret:
        headers["X-Internal-Secret"] = secret
    try:
        # data=b"" is required by urllib to send a POST (not GET)
        req = urllib.request.Request(url, method="POST", data=b"", headers=headers)
        with loopback_urlopen(req, timeout=_TIMEOUT_SECS) as resp:
            body = json.loads(resp.read())
            if body.get("ok"):
                name = body.get("name", job_id)
                return True, f"Triggered job: {name} ({job_id})"
            return False, f"Error: {body.get('error', 'unknown')}"
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, f"Job not found: {job_id}"
        return False, f"Error: HTTP {e.code}"
    except (urllib.error.URLError, OSError):
        return False, "Error: cannot reach gateway. Is `kirocrew gateway` running?"
