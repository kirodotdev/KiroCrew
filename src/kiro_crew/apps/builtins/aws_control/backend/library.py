"""Library — cloud copies of artifacts on the drive's ``artifacts/`` prefix.

Push-shaped in this PR: an artifact's current version is uploaded as
``artifacts/<slug>/v<N>`` plus a ``meta.json`` sidecar, and a local sync
ledger records what was pushed when. Artifact versions map onto both the
version-named keys AND the bucket's object versioning, so history survives
even a same-key overwrite. Pull stays a download (share/presign or Drive
download) — merging a cloud copy back into the local store is future work.

The ledger (``<app data dir>/library.json``) is display state, not truth:
truth is the bucket listing; the ledger only makes "synced · 2h ago" cheap.

CALLER CONTRACT: same as storage.py — handlers hold the consent gate; sync
functions, call via ``asyncio.to_thread``.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.aws_control.backend import storage
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.artifacts import get_default_store
from kiro_crew.atomic_write import atomic_write
from kiro_crew.platform_compat import file_lock

logger = logging.getLogger(__name__)

APP_NAME = "aws-control"

#: Artifact kind → pushed file extension (content is text for all of these).
_KIND_EXT = {
    "widget": ".html",
    "html": ".html",
    "markdown": ".md",
    "svg": ".svg",
    "json": ".json",
    "text": ".txt",
    "webapp": ".html",
}


def _ledger_path() -> Path:
    return app_data_dir(APP_NAME) / "library.json"


def read_ledger() -> dict[str, Any]:
    try:
        data = json.loads(_ledger_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _write_ledger(ledger: dict[str, Any]) -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(ledger, indent=1))


def list_pushable(account: str) -> list[dict[str, Any]]:
    """Local artifacts with their push state for ONE account.

    Push state is per account (ledger keyed account -> slug): the same
    artifact can be synced to two drives, and neither console may report
    the other's state.

    Names are redacted on the way out: an LLM-authored artifact NAME can
    quote a secret as easily as its body can, and this listing is a display
    surface (the same both-pass chain the push path applies to metadata).
    """
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    def _clean(text: str) -> str:
        text, _ = redact_credentials(text)
        text, _ = redact_exfiltration_urls(text)
        return text

    ledger = read_ledger().get(account, {})
    if not isinstance(ledger, dict):
        # A corrupted per-account entry reads as empty rather than crashing
        # the Library list/push routes.
        ledger = {}
    rows: list[dict[str, Any]] = []
    for artifact in get_default_store().list():
        pushed = ledger.get(artifact.slug) or {}
        if not isinstance(pushed, dict):
            pushed = {}
        rows.append(
            {
                "slug": artifact.slug,
                "name": _clean(artifact.name),
                "kind": artifact.kind,
                "version": artifact.version,
                "updatedAt": artifact.updated_at,
                "pushedVersion": pushed.get("version"),
                "pushedAt": pushed.get("pushedAt"),
            }
        )
    rows.sort(key=lambda r: r.get("updatedAt") or "", reverse=True)
    return rows


def push_artifact(
    profile: str, region: str, bucket: str, account: str, slug: str
) -> dict[str, Any]:
    """Upload one artifact's current content + metadata sidecar.

    Image artifacts are excluded in this PR (binary asset plumbing); the
    caller surfaces the refusal as a plain message, not an error wall.
    """
    store = get_default_store()
    artifact = store.get(slug)  # raises ArtifactNotFoundError for an unknown slug
    ext = _KIND_EXT.get(artifact.kind)
    if ext is None:
        raise ValueError(f"artifact kind {artifact.kind!r} is not pushable yet")
    content = artifact.content or ""

    # Same discipline deploy-web applies before ITS artifact uploads: a
    # credential-bearing artifact is hard-blocked (the drive is private, but
    # a pushed copy is one presigned share away from anyone), and the
    # metadata sidecar runs both redaction passes — an LLM-authored name or
    # description can quote a secret as easily as the body can.
    from kiro_crew.deploy.scan import is_credential_finding, scan_content
    from kiro_crew.security import (
        redact_credentials,
        redact_exfiltration_urls,
        scan_exfiltration_urls,
    )

    if any(is_credential_finding(f) for f in scan_content(content)):
        raise ValueError(
            "this artifact contains credential-like content and will not be "
            "uploaded — remove the secret and push again"
        )
    # LLM-authored content can carry a beacon: a suspicious URL that exfiltrates
    # whatever page context it is embedded in once the pushed copy is shared.
    # The scanner is targeted (heuristic hosts, exemption list) — ordinary links
    # pass; a flagged one blocks the push rather than being silently rewritten.
    if scan_exfiltration_urls(content):
        raise ValueError(
            "this artifact links to a suspicious external endpoint and will "
            "not be uploaded — remove the URL and push again"
        )

    def _clean(text: str) -> str:
        text, _ = redact_credentials(text)
        text, _ = redact_exfiltration_urls(text)
        return text

    meta = {
        "slug": artifact.slug,
        "name": _clean(artifact.name),
        "kind": artifact.kind,
        "version": artifact.version,
        "description": _clean(artifact.description),
        "tags": [_clean(t) for t in (artifact.tags or [])],
        "pushedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    with tempfile.TemporaryDirectory(prefix="kc-library-") as tmp:
        content_path = Path(tmp) / f"v{artifact.version}{ext}"
        content_path.write_text(content, encoding="utf-8")
        meta_path = Path(tmp) / "meta.json"
        meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")
        storage.put_file(
            profile,
            region,
            bucket,
            "library",
            f"{slug}/v{artifact.version}{ext}",
            str(content_path),
            account=account,
        )
        storage.put_file(
            profile,
            region,
            bucket,
            "library",
            f"{slug}/meta.json",
            str(meta_path),
            account=account,
        )

    # Locked read-modify-write: two concurrent pushes of different slugs
    # would otherwise each rewrite the whole ledger from a stale snapshot,
    # and the later atomic write would silently drop the earlier record.
    lock_path = _ledger_path().with_suffix(".lock")
    _ledger_path().parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fd:
        with file_lock(fd.fileno(), exclusive=True, required=True):
            ledger = read_ledger()
            entry = {
                "version": artifact.version,
                "kind": artifact.kind,
                "pushedAt": meta["pushedAt"],
            }
            bucket_entry = ledger.setdefault(account, {})
            if not isinstance(bucket_entry, dict):
                bucket_entry = ledger[account] = {}
            bucket_entry[slug] = entry
            _write_ledger(ledger)
    return entry | {"slug": slug, "account": account}
