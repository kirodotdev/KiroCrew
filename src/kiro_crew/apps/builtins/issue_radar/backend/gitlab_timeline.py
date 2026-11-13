"""GitLab timeline: the secondary resource-event streams (labels, state).

Split out of ``gitlab_client`` to keep that module under the file-length cap.
``gitlab_client._assemble_timeline`` still owns the timeline and is the import
path callers use; this module only reads the two event streams it merges in.

Every glab call goes through the ``gitlab_client`` facade AT CALL TIME (see
``_client``), so a test or extension that replaces ``gitlab_client._glab_api``,
``list_repo_labels`` or a normalizer reaches these reads too.
"""

from __future__ import annotations

from types import ModuleType

from .errors import ProviderCliError
from .gitlab_normalization import _obj, _rows


def _client() -> ModuleType:
    """``gitlab_client``, imported at call time because it imports this module."""
    from . import gitlab_client

    return gitlab_client


def resource_events(
    owner: str,
    repo: str,
    base: str,
    *,
    host: str,
    timeout: float,
) -> list[dict]:
    """Normalized label and state events for the issue or MR at ``base``.

    A failure on either stream degrades to omitting its entries rather than
    failing the whole pane: the comments are the substance, and an older GitLab
    may not serve the resource-event endpoints at all.
    """
    gl = _client()
    try:
        label_events = _rows(
            gl._glab_api(f"{base}/resource_label_events", host=host, timeout=timeout, paginate=True)
        )
    except ProviderCliError:
        label_events = []
    # Each label event embeds its label, colour included, so the project's label
    # set is only read when an event arrived without one (a deleted label, or an
    # older GitLab), not on every timeline load.
    labels_by_name: dict[str, dict] = {}
    if any(not _obj(e.get("label")).get("color") for e in label_events):
        try:
            labels_by_name = {
                lab["name"]: lab
                for lab in gl.list_repo_labels(owner, repo, host=host, timeout=timeout)
            }
        except ProviderCliError:
            labels_by_name = {}
    events = [
        e
        for e in (gl._norm_label_event(item, labels_by_name) for item in label_events)
        if e is not None
    ]
    try:
        state_events = _rows(
            gl._glab_api(f"{base}/resource_state_events", host=host, timeout=timeout, paginate=True)
        )
    except ProviderCliError:
        state_events = []
    events.extend(e for e in (gl._norm_state_event(item) for item in state_events) if e is not None)
    return events
