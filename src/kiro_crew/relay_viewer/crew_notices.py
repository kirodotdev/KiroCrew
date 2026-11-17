"""Tell the person once when a team Crew first shows up in their relay Crew list.

The dashboard reads ``GET /api/relay-viewer/crews`` every minute. After a read that
succeeded, :func:`reconcile` compares its team Crews with a state file under the
data home naming every team Crew already announced, by endpoint ID (the one field
the control plane keeps stable across renames). A Crew not in the file is
recorded first and announced after, so a write that fails sends nothing and
cannot turn into a note on every read. One new Crew gets "You've been added to
<name>" with an Open action; two or more in one read get one summary note, which
is also what a first sign-in with several Crews already granted produces.

Each recorded Crew also carries ``seen``: false until the person opens it or opens
the Crew switcher that lists it (``POST /api/relay-viewer/crews/seen``), which is
what the switcher's dot reads. The file lives on the gateway, not in browser
storage, because a new desktop window starts with empty storage.

What it does not do:

* A Crew that leaves the list stays in the file. The list can be cut at the page
  limit and drops entries it can't use, so a missing Crew is no proof of lost
  access; the open window's own lost-access notice covers that case.
* A personal Crew (the person's own machine) is never announced.
* A failed read, or the viewer turned off, never reaches this module.

Every helper here but :func:`publish` blocks on the file: call them off the loop.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

if TYPE_CHECKING:
    from kiro_crew.relay_viewer.cps import CrewEndpoint

logger = logging.getLogger(__name__)

#: Under the data home, not ``cache/``: dropping a cache is a supported repair, and
#: dropping this file would announce every team Crew again.
STATE_FILENAME = "relay-viewer-crews.json"
CHANNEL = "system.relay"
NOTE_KIND = "agent"
#: The Crew switcher reads this query on the chat page and opens that Crew.
OPEN_URL = "/chat?relay_crew={endpoint_id}"
#: One read, check and write at a time, so two dashboards polling together
#: announce a Crew once.
_LOCK = threading.Lock()


def _state_path() -> Path:
    return config_dir() / STATE_FILENAME


def _read() -> dict[str, dict[str, Any]] | None:
    """The recorded Crews; ``{}`` when the file is missing, ``None`` when it is unreadable."""
    path = _state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, ValueError):
        logger.warning("relay viewer: %s can't be read; rebuilding it", path, exc_info=True)
        return None
    crews = data.get("crews") if isinstance(data, dict) else None
    if not isinstance(crews, dict):
        logger.warning("relay viewer: %s is malformed; rebuilding it", path)
        return None
    return {k: v for k, v in crews.items() if isinstance(k, str) and isinstance(v, dict)}


def _write(crews: dict[str, dict[str, Any]]) -> bool:
    try:
        atomic_write(
            _state_path(), json.dumps({"version": 1, "crews": crews}, sort_keys=True) + "\n"
        )
    except OSError:
        logger.warning("relay viewer: could not write %s", _state_path(), exc_info=True)
        return False
    return True


def _is_team(crew: CrewEndpoint) -> bool:
    return crew.crew_type.upper() == "TEAM"


def reconcile(crews: Iterable[CrewEndpoint]) -> tuple[list[CrewEndpoint], frozenset[str]]:
    """``(to announce, unseen endpoint IDs)`` for one successful list read. BLOCKING.

    The Crews to announce are already recorded when this returns. A file that
    can't be read is rebuilt from this read with every Crew seen and nothing
    announced: a broken file must not ring the bell for every Crew.
    """
    team = [c for c in crews if _is_team(c)]
    with _LOCK:
        recorded = _read()
        if recorded is None:
            _write({c.endpoint_id: {"seen": True} for c in team})
            return [], frozenset()
        new = [c for c in team if c.endpoint_id not in recorded]
        if new:
            stored = {k: dict(v) for k, v in recorded.items()}
            for crew in new:
                recorded[crew.endpoint_id] = {"seen": False}
            if not _write(recorded):
                # Announce nothing, but keep the dots the file already holds.
                recorded, new = stored, []
    unseen = frozenset(
        c.endpoint_id for c in team if recorded.get(c.endpoint_id, {}).get("seen") is False
    )
    return new, unseen


def mark_seen(endpoint_ids: Iterable[str]) -> bool:
    """Record each of *endpoint_ids* the file holds as seen. BLOCKING.

    ``False`` only when a change was due and could not be written, so the caller
    can say the dot will come back.
    """
    wanted = set(endpoint_ids)
    with _LOCK:
        recorded = _read()
        if recorded is None:
            # Unreadable, so nothing can be saved: say so rather than report success.
            return False
        if not recorded:
            return True
        changed = False
        for endpoint_id in wanted & recorded.keys():
            if recorded[endpoint_id].get("seen") is not True:
                recorded[endpoint_id]["seen"] = True
                changed = True
        return _write(recorded) if changed else True


def publish(state: Any, new: list[CrewEndpoint]) -> None:
    """Ring the bell once for *new*: one Crew by name, two or more as one count."""
    if not new:
        return
    if len(new) == 1:
        crew = new[0]
        state.notify(
            NOTE_KIND,
            f"You've been added to {crew.name}",
            "Open it from Team Crews in the Crew switcher.",
            actions=[
                {
                    "id": "open",
                    "label": "Open",
                    "url": OPEN_URL.format(endpoint_id=crew.endpoint_id),
                }
            ],
            channel=CHANNEL,
            i18n={"id": "relay_crew_added", "params": {"name": crew.name}},
        )
        return
    state.notify(
        NOTE_KIND,
        f"You have access to {len(new)} Team Crews",
        "Open them from Team Crews in the Crew switcher.",
        channel=CHANNEL,
        i18n={"id": "relay_crews_available", "params": {"count": len(new)}},
    )
