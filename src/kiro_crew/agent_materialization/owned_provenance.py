"""Owned stems whose file on disk must be CONFIRMED before it counts as the machine's.

Most owned specs are safe to classify by filename alone: the name was reserved before
any release let a user create a template, so nothing a user owns can be sitting at it,
and the managed installer rewrites the file on every rebuild.

A stem whose installer can DECLINE is the exception. Such a name was user-creatable
before it became owned, so the file at it may be a crew's private copy that the managed
installer will never rewrite. Reading that file as owned is what strips a user's hooks,
skips a user's auto-approvals past a ceiling tightening, or replaces a customized
template.

The exception is declared PER STEM rather than tested inline at each caller, because
only a stem's own installer knows what its last write looks like, and because the
callers that have to ask are spread across fork governance, the plumbing refresh and the
capability projection -- three places that must not drift apart. Adding a fourth
declining installer means giving it an entry here, not another branch at each caller.

Two questions, and they are not the same one:

* :attr:`OwnedProvenanceGate.confirms` -- do these bytes positively say the managed
  installer wrote them? Asked wherever a file is about to be treated as owned CONTENT:
  the origin of a plumbing refresh, a capability projection's parent.
* :attr:`OwnedProvenanceGate.install_refreshes` -- would the managed install actually
  LAND here, so an owned writer re-filters the grants on every rebuild? Asked where
  fork governance decides it may SKIP a file because something else sanitizes it.

The two come apart for a stem whose install is refused by something other than the
file's own bytes -- a user markdown sibling, a recorded fork -- where the bytes can
confirm while the install still never lands. A skip made on ``confirms`` in that case
leaves a grant list nothing re-filters.

Both answers are FAIL-CLOSED: absent, unreadable or unconfirmed is not the machine's.
Every predicate here imports its owner lazily, so this module stays importable from the
installers it describes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, NamedTuple

from kiro_crew.agent_files import DASHBOARD_AUTHOR_AGENT_FILENAME, TEAM_LEAD_AGENT_FILENAME

_DASHBOARD_AUTHOR_STEM = Path(DASHBOARD_AUTHOR_AGENT_FILENAME).stem
_TEAM_LEAD_STEM = Path(TEAM_LEAD_AGENT_FILENAME).stem


class OwnedProvenanceGate(NamedTuple):
    """How to confirm, for one declining stem, that the managed installer owns a file.

    ``pins_home`` is a PROPERTY of the stem's managed write rather than a question about
    one file: True when that writer records the instance's ``KIROCREW_HOME`` in its
    managed server entries, so a file at the stem can carry the write provenance the
    ownership probe reads. False when the writer records no pin on any branch, which
    makes every file at that stem -- the installer's own included -- unable to contribute
    a home signal, so the probe can only exclude it outright.
    """

    confirms: Callable[[Path], bool]
    install_refreshes: Callable[[Path], bool]
    pins_home: bool


def _dashboard_author_confirms(path: Path) -> bool:
    """Do the bytes at *path* reproduce the installer-recorded ownership digest?

    The digest is recorded after the install lands a write, so only a file whose exact
    bytes reproduce it is that installer's own last write. Fail-closed: an absent or
    unreadable file confirms as nothing.

    Routed to the stem's own entry point rather than reimplemented here, so this gate and
    the fork refresh ask one question through one function -- and a test that patches that
    function still reaches every caller of it.
    """
    from kiro_crew.agent_materialization import fork_refresh

    return fork_refresh._dashboard_author_file_is_installers(path)


def _dashboard_author_install_refreshes(path: Path) -> bool:
    """Would the managed dashboard-author install land on *path*?

    Narrower than :func:`_dashboard_author_confirms`: a user ``.md`` sibling blocks a
    first-time install even when nothing contradicts the ``.json``, and a file the
    install refuses has no owned writer re-filtering its grants.
    """
    from kiro_crew.agent_materialization import worker_agent

    return worker_agent._managed_dashboard_author_install_lands(path)


def _team_lead_owns(path: Path) -> bool:
    """Does the team-lead installer own the file at *path*?

    ONE predicate for both questions, because this installer's refusals all come from
    the attribution itself -- the file's marks, or a lineage record naming it a crew's
    copy -- so "these bytes are ours" and "the install lands here" are decided by the
    same test. A stem that later grows a refusal the attribution does not see needs its
    own second predicate, which is why the gate keeps two fields.
    """
    from kiro_crew.agent_materialization import team_lead_agent

    return team_lead_agent._managed_team_lead_install_lands(path)


#: The declining stems, keyed by stem. A stem absent from this map is a plain owned
#: stem whose installer writes every rebuild, and its filename is the whole answer.
_GATES: dict[str, OwnedProvenanceGate] = {
    _DASHBOARD_AUTHOR_STEM: OwnedProvenanceGate(
        confirms=_dashboard_author_confirms,
        install_refreshes=_dashboard_author_install_refreshes,
        # Its managed entry carries no home pin on either branch -- the installer's own
        # write omits it and a user leftover never had one -- so no file at this stem can
        # vouch by pin, whatever its provenance.
        pins_home=False,
    ),
    _TEAM_LEAD_STEM: OwnedProvenanceGate(
        confirms=_team_lead_owns,
        install_refreshes=_team_lead_owns,
        # Its writer pins the data home into the two servers it mounts explicitly, so a
        # file this installer wrote DOES carry the provenance the ownership probe reads
        # and keeps its pin checked. Only a file the installer declines is excluded.
        pins_home=True,
    ),
}


def owned_provenance_gate(stem: str) -> OwnedProvenanceGate | None:
    """The gate for *stem*, or ``None`` when that stem's filename is the whole answer."""
    return _GATES.get(stem)
