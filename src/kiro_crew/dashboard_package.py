"""The ONE function that reads the Model out of a member's dashboard package.

A v3 dashboard is an artifact of ``kind="dashboard"`` holding a Model (the data types
and field shapes), a View, a theme and ``bound_to``. ``dashboard_write`` validates an
agent's value against the Model **inside that current package** -- never against a
builtin template manifest, which is what it did while templates were the only source.

THIS MODULE IS THE WHOLE SEAM. :func:`read_package_model` is the only place that knows
where a Model comes from; the write path calls it and nothing else. Its signature, its
four states and the refusal each maps to are settled in ``INTERFACE.md``, agreed with
the package line, and consumed by :mod:`kiro_crew.dashboard_agentic`.

Why the reader is HERE and the write gate is not
------------------------------------------------
The package has two jobs with opposite import weight. The write gate -- validate,
canonicalize, decide the version, merge a revert -- is imported by
:mod:`kiro_crew.artifacts`, so it has to stay cheap, and it lives beside the artifact
store's other field rules at :mod:`kiro_crew.artifact_store.dashboard_package`. This
reader imports that gate AND :mod:`kiro_crew.dashboard_templates.manifest`, which
reaches the projection registry and template parity. One combined module would drag
that graph into every caller of :mod:`kiro_crew.artifacts`.

Why a state and not an exception for a content problem
------------------------------------------------------
Every refusal on the write path is RECORDED to the crewmate's mistake book before the
response is sent, which is the only reason its next cycle is cheaper than this one. An
exception raised from in here would escape that recording and become a 500: the agent
would learn nothing, and a person would be shown a server error for the ordinary fact
that no package exists yet. So "there is no package", "the Model does not parse" and
"this is not yours" are RETURNED, each as its own state, and only a failure that is
not about content -- the store refusing a path, a read that errors -- raises.

That is also exactly how :mod:`kiro_crew.dashboard_templates.instance` answers for a
stored template: a record that does not parse comes back as ``error`` with a sentence,
never as a raise. One posture, two readers.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from kiro_crew.dashboard_agentic import Instance

logger = logging.getLogger(__name__)

__all__ = [
    "PACKAGE_STATES",
    "STATE_EMPTY",
    "STATE_ERROR",
    "STATE_LIVE",
    "PackageRead",
    "PackageReadError",
    "read_package_model",
]

#: The package holds a Model this write can be checked against.
STATE_LIVE: Final[str] = "live"
#: No dashboard package is bound to this member yet.
STATE_EMPTY: Final[str] = "empty"
#: A package is there and its Model does not parse.
STATE_ERROR: Final[str] = "error"
#: A package is there and this caller may not read it.

#: Every state :func:`read_package_model` can answer with.
#:
#: Pinned against the refusal table in :data:`dashboard_agentic.PACKAGE_REFUSALS` by
#: test, because the mapping there is keyed by these STRINGS -- the write path must not
#: import this module (the Model type travels the other way), so the vocabulary is
#: shared as values and the pin is what keeps the two from drifting. A state added here
#: with no refusal beside it would be reported to an agent as a bare "no package".
#:
#: ``stale`` is deliberately absent. On a template it means "the registry moved past
#: this copy", and a package has no registry behind it to move.
PACKAGE_STATES: Final[tuple[str, ...]] = (STATE_LIVE, STATE_EMPTY, STATE_ERROR)


class PackageReadError(Exception):
    """The package could not be read at all. IO, never content.

    Distinct from every state above so the caller can separate "I read it, and here is
    what it says" from "I could not read it", which are a 400 and a 500.
    """


@dataclass(frozen=True)
class PackageRead:
    """What the write path is told about a member's current dashboard package."""

    #: The Model as the write path needs it, or ``None`` when there is none to check
    #: against -- every non-live state, and a live PACKAGE, which this build does not
    #: translate.
    #:
    #: :class:`~kiro_crew.dashboard_agentic.Instance` is REUSED rather than a new type
    #: minted beside it, and that reuse is what keeps this seam one function:
    #: ``check_write`` already accepts exactly this narrow view -- the field specs plus
    #: the version a write is stamped with -- so it is not touched by the move off
    #: templates.
    model: Instance | None
    #: One of :data:`PACKAGE_STATES`.
    state: str
    #: One sentence a person can be shown, and the one the refusal quotes.
    state_reason: str
    #: Whether a real dashboard PACKAGE was read, as opposed to the template instance
    #: this read falls through to when no package is bound.
    #:
    #: PROVENANCE, and nothing more. What it means for a write is the write route's to
    #: decide -- today that route refuses, because the page a crewmate is shown still
    #: renders the template -- and keeping the meaning there is what stops this module
    #: from having to know what any page draws.
    from_package: bool = False


#: The ``bound_to`` scope a MEMBER's package is spelled under. The other scope the
#: binding grammar admits is ``session:<slot key>``, which names a slot and not a
#: crewmate, so it is never what a member resolves to.
_CREWMATE_SCOPE: Final[str] = "crewmate:"


def read_package_model(member: str) -> PackageRead:
    """The Model inside *member*'s current dashboard package.

    Keyed by the MEMBER, which is what the write path holds. The artifact's slug is
    resolved from *member*'s binding, so no caller has to invent one.

    Content problems return a state. Only a failed read raises
    :class:`PackageReadError`.

    The template instance store stands behind the package path for EXACTLY ONE of its
    states, :data:`STATE_EMPTY`. A crewmate whose page is a builtin template has no
    ``kind="dashboard"`` artifact, and answering it "create one" would refuse every
    write it makes. :data:`STATE_ERROR` does not fall through: a package that is present
    and broken is a real answer, and a template standing behind it would mask the one
    thing the crewmate has to be told.
    """
    resolution = _from_package(member)
    if resolution.state != STATE_EMPTY:
        return resolution
    instance = _from_template_instance(member)
    # Nothing either way keeps the PACKAGE path's sentence, because "no dashboard
    # package yet" is the remedy a crewmate composing one can act on.
    return instance if instance.state != STATE_EMPTY else resolution


def _from_package(member: str) -> PackageRead:
    """Read the ``kind="dashboard"`` artifact bound to *member*."""
    from kiro_crew.artifact_store.dashboard_package import (
        BindingLookupIncomplete,
        parse_package,
        resolve_bound_slug,
    )
    from kiro_crew.artifacts import ArtifactError, get_default_store

    bound_to = f"{_CREWMATE_SCOPE}{member}"
    try:
        slug = resolve_bound_slug(bound_to)
    except BindingLookupIncomplete as exc:
        # The scan could not see every stored package, so "no package" is unproven.
        # ERROR rather than EMPTY, and the difference is the whole point: EMPTY falls
        # through to the template instance store below, which hands back a BUILTIN
        # page's Model, and a write checked against that can replace a cell with a
        # value of the wrong shape.
        return PackageRead(
            None, STATE_ERROR, f"{member}'s dashboard package cannot be located: {exc}"
        )
    except (ArtifactError, ValueError) as exc:
        # A binding this reader cannot even spell is content, not IO: an agent
        # given a 500 for a member name learns nothing it can act on.
        return PackageRead(None, STATE_ERROR, f"{member!r} is not a dashboard binding: {exc}")
    except OSError as exc:
        raise PackageReadError(f"the dashboard packages cannot be listed: {exc}") from exc
    if not slug:
        return PackageRead(None, STATE_EMPTY, f"{member} has no dashboard package yet")
    try:
        loaded = get_default_store().get(slug)
    except ArtifactError:
        # Absent, or a slug this caller named that holds nothing. EMPTY rather than
        # ERROR: there is no package here to be broken.
        return PackageRead(None, STATE_EMPTY, f"there is no dashboard package at {slug!r}")
    except OSError as exc:
        raise PackageReadError(f"the dashboard package {slug!r} cannot be read: {exc}") from exc
    try:
        package = parse_package(loaded.content or "")
    except (ArtifactError, ValueError) as exc:
        return PackageRead(
            None, STATE_ERROR, f"the dashboard package at {slug!r} does not load: {exc}"
        )
    types = (package.get("model") or {}).get("types")
    if not isinstance(types, Mapping):
        # The gate validated the package and this reader cannot find a field table in
        # it. A disagreement between two readers of one record, so it is a state with a
        # sentence rather than a raise, and it stays AHEAD of the live answer below: a
        # package that is present and malformed is told so, rather than being folded
        # into the one answer every readable package gets.
        return PackageRead(
            None, STATE_ERROR, f"the dashboard package at {slug!r} declares no field table"
        )
    version = int(getattr(loaded, "version", 0) or 0)
    # LIVE, and carrying NO Model. A package's field table is not translated into
    # manifest field specs on this base, because the one caller refuses a package-bound
    # write before it would check one: the page a crewmate is shown still renders the
    # template instance, so checking the write against the package while the page draws
    # something else is what lets an undrawable value land. The translation ships with
    # the change that makes the display read the same package, which is the first place
    # its output is used for anything.
    return PackageRead(
        None,
        STATE_LIVE,
        f"package {slug!r} version {version}, bound to {bound_to}",
        from_package=True,
    )


def _from_template_instance(member: str) -> PackageRead:
    """FALLBACK while the write gate is on the package line's own branch.

    Resolves the Model from today's template instance store so the seam is exercised
    end to end and current behaviour does not regress. This whole function goes when
    :mod:`kiro_crew.artifact_store.dashboard_package` merges; nothing outside this
    module reads it.

    Its mapping is ``read_instance``'s existing rule verbatim: ``live`` / ``stale`` use
    the stored copy's manifest, ``empty`` uses the default template's, ``error`` and
    anything unrecognised have no manifest to validate against, and no instance with no
    default answers :data:`STATE_EMPTY`.
    """
    slug = member
    # Resolved through the modules rather than imported at the top, which keeps the
    # boot-path rule the dashboard handlers follow and lets a test replace one.
    from kiro_crew.dashboard_templates import instance as instance_store
    from kiro_crew.dashboard_templates.manifest import ManifestError, parse_manifest

    try:
        record = instance_store.read(slug)
    except Exception as exc:
        # A read that FAILED, which is the one thing that is not a state: the record
        # could not be opened, so nothing is known about the Model either way. Caught
        # broadly rather than on the store's own error type, because a stub store put
        # in its place has no such type and a real one's ``read`` touches the file
        # system -- and the ANSWER is the same either way.
        raise PackageReadError(f"the dashboard package for {slug!r} cannot be read: {exc}") from exc
    # Read DEFENSIVELY off the store, each name with the literal it stands for as its
    # default, which is what ``read_instance`` did before this and for the same reason:
    # this module resolves the store at call time so a narrower stand-in can be put in
    # its place, and an attribute lookup that raised would turn a replaced store into
    # a 500.
    live = getattr(instance_store, "STATE_LIVE", "live")
    stale = getattr(instance_store, "STATE_STALE", "stale")
    empty = getattr(instance_store, "STATE_EMPTY", "empty")
    state = getattr(record, "state", "")
    reason = str(getattr(record, "state_reason", "") or "")
    if state == empty:
        try:
            fallback = instance_store.default_instance(slug)
        except Exception:
            logger.warning(
                "no default dashboard package for %s to write against", slug, exc_info=True
            )
            fallback = None
        if fallback is None:
            # ``default_instance`` ANSWERS None when the registry ships no default
            # rather than raising, so this covers both halves.
            return PackageRead(None, STATE_EMPTY, f"{member or slug} has no dashboard package yet")
        record = fallback
        reason = str(getattr(record, "state_reason", "") or "")
    elif state not in (live, stale):
        # ``error`` lands here, and so does a state this reader does not recognise.
        # BOTH are STATE_ERROR rather than STATE_EMPTY: a package the store can see
        # and this reader cannot interpret is present, and telling the agent to create
        # one would be the wrong instruction on every cycle.
        return PackageRead(
            None, STATE_ERROR, reason or f"this dashboard package is in state {state!r}"
        )
    try:
        manifest = parse_manifest(dict(record.manifest))
    except (ManifestError, TypeError, ValueError) as exc:
        # The copy loaded for the store and does not parse for this reader, which is a
        # content problem and so a state. Its own problems are the sentence, because an
        # agent told "the Model is broken" with no detail has nothing to act on.
        return PackageRead(
            None, STATE_ERROR, f"this dashboard package's Model does not load: {exc}"
        )
    return PackageRead(
        Instance(manifest=manifest, instance_version=int(getattr(record, "instance_version", 0))),
        STATE_LIVE,
        reason,
    )
