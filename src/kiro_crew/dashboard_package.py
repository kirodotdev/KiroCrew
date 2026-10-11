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
from typing import TYPE_CHECKING, Any, Final

from kiro_crew.dashboard_agentic import Instance

if TYPE_CHECKING:  # the manifest reaches the projection registry, so not at run time
    from kiro_crew.dashboard_templates.manifest import TemplateManifest

logger = logging.getLogger(__name__)

__all__ = [
    "PACKAGE_MANIFEST_SOURCE",
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
class _ReadModel:
    """The three values :func:`_manifest_of` reads, and nothing else.

    A local holder rather than the gate's fuller Model view, because this reader needs
    the field table, the slug and the version, and none of the binding, the layout
    fingerprint or the block subscriptions. Keeping it local also keeps the gate free to
    shape its own view without this module following it.
    """

    slug: str
    version: int
    fields: dict[str, Any]


@dataclass(frozen=True)
class PackageRead:
    """What the write path is told about a member's current dashboard package."""

    #: The Model as the write path needs it, or ``None`` for every non-live state.
    #:
    #: :class:`~kiro_crew.dashboard_agentic.Instance` is REUSED rather than a new type
    #: minted beside it, and that reuse is what keeps this seam one function:
    #: ``check_write`` already accepts exactly this narrow view -- the field specs plus
    #: the version a write is stamped with -- so it is not touched by the move off
    #: templates. ``instance_version`` carries the PACKAGE's version here.
    model: Instance | None
    #: One of :data:`PACKAGE_STATES`.
    state: str
    #: One sentence a person can be shown, and the one the refusal quotes.
    state_reason: str


#: The ``bound_to`` scope a MEMBER's package is spelled under. The other scope the
#: binding grammar admits is ``session:<slot key>``, which names a slot and not a
#: crewmate, so it is never what a member resolves to.
_CREWMATE_SCOPE: Final[str] = "crewmate:"

#: The ``source`` a synthesized manifest declares. See :data:`manifest.SOURCES`: its own
#: word, so the render gate and the catalogue scan can still tell a package from a
#: template a person wrote.
PACKAGE_MANIFEST_SOURCE: Final[str] = "package"

#: One package data type -> the manifest type it is checked as.
#:
#: EXPLICIT and declarative, which is the shape this has to be while it is the only
#: spelling. ``timestamp`` and ``enum`` both land on ``string``, so this is information
#: the package catalogue does not carry yet and this table is the second place the
#: mapping is written down. The package line has offered to put ``manifest_type`` and
#: ``manifest_shape`` on its ``FieldType``; when that lands, a table lookup off the
#: catalogue SUPERSEDES this dict and
#: ``test_every_package_data_type_has_a_manifest_type`` is what names a type added
#: upstream with no row here in the meantime.
_PACKAGE_TYPE_TO_MANIFEST: Final[dict[str, str]] = {
    "number": "number",
    "text": "string",
    "bool": "boolean",
    # An instant is checked as the ISO string it is written as. The manifest has no
    # temporal type, and inventing one here would be a type no page knows how to draw.
    "timestamp": "string",
    # A label out of a fixed set: a string, plus the choices as the field's own Shape,
    # which is what makes a value outside the set refusable rather than merely odd.
    "enum": "string",
}


class _ModelUntranslatable(Exception):
    """A validated package this reader cannot express as manifest field specs."""


def _manifest_of(model: object) -> "TemplateManifest":
    """A package's Model as the ``TemplateManifest`` the write path checks against.

    The translation lives in this module because the write path must not learn the
    package's vocabulary -- that is the whole point of one seam. Every row of it is
    recorded in ``INTERFACE.md``.

    ``id`` is the SLUG. A package is not a template and has no template id, and this
    string is read aloud to the agent in two refusal sentences ("is not a field of
    template 'report'"), so it has to name something the agent can actually go and
    look at. Rewording those two sentences for v3 belongs to the agent line.
    """
    from kiro_crew.dashboard_templates.manifest import (
        FieldSpec,
        Shape,
        TemplateManifest,
    )

    slug = str(getattr(model, "slug", "") or "")
    version = int(getattr(model, "version", 0) or 0)
    specs: dict[str, FieldSpec] = {}
    for name, spec in getattr(model, "fields", {}).items():
        declared = str(spec.get("type") or "")
        mapped = _PACKAGE_TYPE_TO_MANIFEST.get(declared)
        if mapped is None:
            raise _ModelUntranslatable(
                f"this dashboard package declares field {name!r} as type {declared!r}, "
                "which this gateway cannot check a write against -- it is newer than "
                "this build's data-type mapping"
            )
        source = spec.get("source") or {}
        agentic = source.get("agentic") is True
        choices = spec.get("choices")
        shape = (
            Shape(type=mapped, enum=tuple(choices))
            if agentic and declared == "enum" and isinstance(choices, list) and choices
            # A fold-backed field declares no shape: the manifest refuses one there,
            # because the fold writes the value and a shape would promise something no
            # write path checks.
            else None
        )
        specs[str(name)] = FieldSpec(
            name=str(name),
            type=mapped,
            fold=str(source.get("fold")) if not agentic else None,
            path=str(source.get("path")) if not agentic else None,
            agentic=agentic,
            shape=shape,
        )
    return TemplateManifest(
        id=slug,
        # A positive int or the manifest refuses it, and version 0 is what an artifact
        # with no version reads as.
        version=max(version, 1),
        title=f"dashboard package {slug}" if slug else "dashboard package",
        description="a dashboard composed by this crewmate",
        source=PACKAGE_MANIFEST_SOURCE,
        fields=specs,
    )


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
    # The three values ``_manifest_of`` reads, taken off the parsed package rather than
    # through a projection of it: the field table IS ``model.types``, and the slug and
    # version are this read's own. Nothing here needs the binding, the layout
    # fingerprint or the block subscriptions a fuller Model view would carry.
    types = (package.get("model") or {}).get("types")
    if not isinstance(types, Mapping):
        # The gate validated the package and this reader cannot find a field table in
        # it. A disagreement between two readers of one record, so it is a state with a
        # sentence rather than a raise -- the same answer an untranslatable type gets.
        return PackageRead(
            None, STATE_ERROR, f"the dashboard package at {slug!r} declares no field table"
        )
    model = _ReadModel(
        slug=slug,
        version=int(getattr(loaded, "version", 0) or 0),
        fields=dict(types),
    )
    try:
        manifest = _manifest_of(model)
    except _ModelUntranslatable as exc:
        # The gate validated the package and this reader cannot express it as field
        # specs -- a data type the mapping below has no row for. A disagreement between
        # two readers, so it is a state with a sentence rather than a raise.
        return PackageRead(None, STATE_ERROR, str(exc))
    return PackageRead(
        Instance(manifest=manifest, instance_version=model.version),
        STATE_LIVE,
        f"package {slug!r} version {model.version}, bound to {bound_to}",
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
