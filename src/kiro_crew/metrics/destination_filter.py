"""Per-destination export narrowing: metric allowlist, point attributes, resource.

One ``MeterProvider`` feeds several readers: the built-in local JSONL sink plus
one per destination an edition declared. The SDK gives every one of them the same
collected view, so a destination that may receive only PART of what the core
collects needs its own exporter-level filter. That is this module.

Four narrowings, all declared on ``OtlpDestination`` and all one-directional:

* ``allow`` -- a metric whose name is not in the set never reaches the encoder.
* ``point_attributes`` -- fixed pairs added to every exported data point.
* ``resource_allow`` -- the exported Resource reduced to the named keys.
* ``named_entity_namespaces`` -- the namespaces whose ``<namespace>.<app>`` names this
  destination may see. Any other namespaced ``Entity`` is sent as ``private``, with
  its ``entity_version``; points that then share attributes are merged into one.

Three properties this module exists to guarantee, in the order they matter:

1. **Containment.** The wrapper only ever touches batches handed to ITS exporter.
   The local sink and every other destination read their own, and a destination
   that narrows nothing is not wrapped at all.
2. **No shared mutation.** A collected batch reaches the wrapper as SDK
   dataclasses whose nested objects a sibling exporter may also hold. So nothing
   is assigned in place: every narrowed level is rebuilt with
   ``dataclasses.replace``, which also keeps this correct as the SDK adds fields
   (``NumberDataPoint.exemplars`` arrived that way).
3. **Fail closed.** If the narrowing itself raises, the batch is DROPPED rather
   than forwarded. Forwarding the unfiltered batch is the one outcome that turns
   a bug here into an egress of data the destination was never allowed.

A refused point-attribute KEY is refused at construction (``ValueError``), not
dropped with a log. The caller is ``provider._build_otlp_reader``, whose
degrade path is "this destination gets no reader, local collection continues", so
a refusal costs one destination's egress and nothing else. Dropping instead would
publish a series the receiving side cannot route, with only a debug line to say
why -- the failure nobody notices. Keys are a reviewed, non-secret set and are
safe to name in a log; VALUES are never logged, and never logged with the
endpoint.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Any, Mapping, Optional

if TYPE_CHECKING:
    from kiro_crew.platform.interfaces import OtlpDestination

logger = logging.getLogger(__name__)

#: Point-attribute keys a destination may ask for: the client label a receiving
#: side routes on, and ``machineId``, the hashed install id the built-in Kiro
#: telemetry destination already sends in its request header (``kiro_export``).
#: Deliberately a short reviewed list rather than a shape check -- an attribute
#: that lands on every exported point of a destination is a privacy and
#: cardinality decision, so it is made here, once, and not at the destination
#: that wants it.
ALLOWED_POINT_ATTRIBUTE_KEYS = frozenset({"ClientType", "machineId"})

#: What a hidden ``Entity`` and its ``entity_version`` are sent as:
#: ``feature_usage.APP_PRIVATE``, spelled here so this module stays import-light.
PRIVATE = "private"

#: The metrics whose ``Entity`` is an app's public id (``feature_usage.APP_*``).
#: Another metric's ``Entity`` (a tip's document name) may hold a ``.`` too.
APP_METRIC_PREFIX = "kirocrew.app."


def validate_point_attributes(attributes: Optional[Mapping[str, str]]) -> "dict[str, str]":
    """Return *attributes* as a plain dict, or raise on an unreviewed key.

    Raises ``ValueError`` naming the refused KEYS (never their values). An empty
    or missing mapping is valid and means "add nothing".
    """
    if not attributes:
        return {}
    pairs = dict(attributes)
    refused = sorted(key for key in pairs if key not in ALLOWED_POINT_ATTRIBUTE_KEYS)
    if refused:
        raise ValueError(
            "unreviewed point attribute key(s) "
            f"{refused}; allowed: {sorted(ALLOWED_POINT_ATTRIBUTE_KEYS)}"
        )
    return {str(key): str(value) for key, value in pairs.items()}


def narrows_export(dest: "OtlpDestination", operator_owned: bool = False) -> bool:
    """Whether *dest* declared any narrowing, i.e. whether it needs a wrapper.

    Read off the descriptor with ``getattr`` defaults so a destination built by
    an edition that predates these fields answers False rather than raising.
    """
    return (
        getattr(dest, "allow", None) is not None
        or bool(getattr(dest, "point_attributes", None))
        or getattr(dest, "resource_allow", None) is not None
        or hides_entity_names(dest, operator_owned)
    )


def hides_entity_names(dest: "OtlpDestination", operator_owned: bool = False) -> bool:
    """True when *dest* is sent ``private`` for an app the edition named.

    The rule: the local sink and the operator's own ``telemetry.otlp_endpoint*``
    entries (*operator_owned*) see every name. Any other destination sees only the
    namespaces it lists, and one that lists none sees none. It is False while no
    registry row declares ``appNamespace``, so a build that names no apps wraps
    nothing.
    """
    if operator_owned:
        return False
    from kiro_crew.apps import store_origin

    return store_origin.edition_names_apps()


class FilteredMetricExporter:
    """Wrap a metric exporter so one destination sees only what it may see.

    Duck-typed against ``opentelemetry.sdk.metrics.export.MetricExporter`` rather
    than subclassed, so this module imports no SDK symbol at module scope and
    stays loadable on a host without the optional exporter extra.
    ``PeriodicExportingMetricReader`` reads ``_preferred_temporality`` and
    ``_preferred_aggregation`` off its exporter at construction, so both are
    mirrored from the wrapped one: temporality is a CORE decision and a wrapper
    must not become a second place that answers it.
    """

    def __init__(
        self,
        exporter: Any,
        *,
        name: str = "",
        allow: "Optional[frozenset[str]]" = None,
        point_attributes: Optional[Mapping[str, str]] = None,
        resource_allow: "Optional[frozenset[str]]" = None,
        named_entity_namespaces: "Optional[frozenset[str]]" = None,
        hide_entity_names: bool = False,
    ) -> None:
        self._exporter = exporter
        self._name = name
        self._allow = None if allow is None else frozenset(allow)
        self._point_attributes = validate_point_attributes(point_attributes)
        self._resource_allow = None if resource_allow is None else frozenset(resource_allow)
        # An app name holds no ``.``, so on an app metric an ``Entity`` with one is
        # ``<namespace>.<app>``, and the namespace is all before the LAST ``.``: a
        # namespace may hold a ``.`` itself, and ``a`` is not ``a.b`` or ``ab``.
        self._hide_names = hide_entity_names
        self._named = frozenset(named_entity_namespaces or ())
        # One warning for the first narrowing failure, then debug. A per-minute
        # reader that kept warning would write one line per interval forever.
        self._warned = False
        self._preferred_temporality = getattr(exporter, "_preferred_temporality", None)
        self._preferred_aggregation = getattr(exporter, "_preferred_aggregation", None)

    # -- the MetricExporter surface -----------------------------------------

    def export(self, metrics_data: Any, timeout_millis: float = 10_000, **kwargs: Any) -> Any:
        """Narrow *metrics_data* for this destination, then hand it on.

        Returns the wrapped exporter's own result when something is sent, SUCCESS
        when the allowlist left nothing to send (an empty POST carries no
        information and still costs a request), and FAILURE when the narrowing
        itself raised -- in which case nothing is sent at all.
        """
        from opentelemetry.sdk.metrics.export import MetricExportResult

        try:
            narrowed = self._narrow(metrics_data)
        except Exception:
            # Never forward the unnarrowed batch: that would export exactly the
            # data this destination is not allowed to receive.
            if self._warned:
                logger.debug(
                    "metric narrowing failed for destination %r; batch dropped",
                    self._name,
                    exc_info=True,
                )
            else:
                self._warned = True
                logger.warning(
                    "metric narrowing failed for destination %r; batch dropped "
                    "(further occurrences at debug)",
                    self._name,
                    exc_info=True,
                )
            return MetricExportResult.FAILURE
        if narrowed is None:
            return MetricExportResult.SUCCESS
        return self._exporter.export(narrowed, timeout_millis=timeout_millis, **kwargs)

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return self._exporter.force_flush(timeout_millis)

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        self._exporter.shutdown(timeout_millis, **kwargs)

    def __getattr__(self, item: str) -> Any:
        # Anything the SDK or a test reads off a real exporter and this wrapper
        # does not define itself. Named methods above are NOT reached through
        # here, so the narrowing cannot be bypassed by attribute lookup.
        return getattr(self._exporter, item)

    # -- narrowing ----------------------------------------------------------

    def _narrow(self, metrics_data: Any) -> Any:
        """Rebuild *metrics_data* for this destination, or None when it is empty.

        Rebuilt top-down with ``dataclasses.replace`` at every level that
        changes, so no object a sibling exporter may hold is written to.
        """
        resource_metrics = []
        for rm in metrics_data.resource_metrics or ():
            scope_metrics = []
            for sm in rm.scope_metrics or ():
                metrics = [m for m in (self._narrow_metric(m) for m in sm.metrics or ()) if m]
                if metrics:
                    scope_metrics.append(dataclasses.replace(sm, metrics=metrics))
            if not scope_metrics:
                continue
            resource_metrics.append(
                dataclasses.replace(
                    rm,
                    scope_metrics=scope_metrics,
                    resource=self._narrow_resource(rm.resource),
                )
            )
        if not resource_metrics:
            return None
        return dataclasses.replace(metrics_data, resource_metrics=resource_metrics)

    def _narrow_resource(self, resource: Any) -> Any:
        """Reduce *resource* to ``resource_allow``, building a NEW Resource.

        A key the shared Resource does not carry is absent from the result; no
        value is invented. The schema_url is carried over, because it describes
        the keys that survived rather than the set that was dropped.
        """
        if self._resource_allow is None or resource is None:
            return resource
        from opentelemetry.sdk.resources import Resource

        kept = {
            key: value
            for key, value in dict(resource.attributes or {}).items()
            if key in self._resource_allow
        }
        return Resource(kept, getattr(resource, "schema_url", None))

    def _narrow_metric(self, metric: Any) -> Any:
        """Drop *metric* when the allowlist excludes it, else hide and label its points."""
        if self._allow is not None and metric.name not in self._allow:
            return None
        metric = self._hide_entities(metric)
        if not self._point_attributes:
            return metric
        data = getattr(metric, "data", None)
        points = getattr(data, "data_points", None)
        if data is None or not points:
            return metric
        labelled = [
            dataclasses.replace(point, attributes=self._merged_attributes(point))
            for point in points
        ]
        return dataclasses.replace(metric, data=dataclasses.replace(data, data_points=labelled))

    def _hide_entities(self, metric: Any) -> Any:
        """*metric* with each hidden ``Entity`` sent as ``private``, or *metric* itself.

        A point whose ``Entity`` is namespaced and not named by this destination gets ``Entity`` and
        ``entity_version`` set to ``private``. It can then share its attributes
        with a point that was ``private`` already; on a counter the two are summed
        into one, so the destination never sees two points of one series.
        """
        if not self._hide_names or not metric.name.startswith(APP_METRIC_PREFIX):
            return metric
        data = getattr(metric, "data", None)
        points = getattr(data, "data_points", None)
        if data is None or not points:
            return metric
        out = []
        hidden = False
        for point in points:
            attrs = getattr(point, "attributes", None) or {}
            entity = attrs.get("Entity")
            if not (
                isinstance(entity, str)
                and "." in entity
                and entity.rpartition(".")[0] not in self._named
            ):
                out.append(point)
                continue
            new = dict(attrs)
            new["Entity"] = PRIVATE
            if "entity_version" in new:
                new["entity_version"] = PRIVATE
            out.append(dataclasses.replace(point, attributes=new))
            hidden = True
        if not hidden:
            return metric
        # Only a Sum (a counter) adds: every metric that carries Entity is one
        # (``test_product_metrics``). shortcut: another kind keeps its points
        # unmerged; merge it too if a gauge ever carries Entity.
        if hasattr(data, "is_monotonic"):
            out = _summed_by_attributes(out)
        return dataclasses.replace(metric, data=dataclasses.replace(data, data_points=out))

    def _merged_attributes(self, point: Any) -> "dict[str, str]":
        """Point attributes with this destination's fixed pairs laid over them.

        The destination's pairs WIN on a key collision. The value is a constant
        fixed at construction and the receiving side routes on it, so a call site
        that managed to emit the same key must not be able to change where this
        destination's data lands.
        """
        existing = dict(getattr(point, "attributes", None) or {})
        existing.update(self._point_attributes)
        return existing


def _summed_by_attributes(points: "list[Any]") -> "list[Any]":
    """*points* with those of equal attributes added into one, in first-seen order.

    The sum spans both points' windows. Exemplars are kept from both.
    """
    merged: "dict[frozenset[Any], Any]" = {}
    for point in points:
        key = frozenset((point.attributes or {}).items())
        first = merged.get(key)
        if first is None:
            merged[key] = point
            continue
        merged[key] = dataclasses.replace(
            first,
            value=first.value + point.value,
            start_time_unix_nano=min(first.start_time_unix_nano, point.start_time_unix_nano),
            time_unix_nano=max(first.time_unix_nano, point.time_unix_nano),
            exemplars=[*(first.exemplars or ()), *(point.exemplars or ())],
        )
    return list(merged.values())


def wrap_if_narrowed(dest: "OtlpDestination", exporter: Any, operator_owned: bool = False) -> Any:
    """Return *exporter* wrapped for *dest*, or *exporter* itself unchanged.

    A destination that narrows nothing is handed back untouched, so a build with
    today's descriptors produces today's exporter object -- no wrapper in the
    path, nothing to go wrong, and nothing new for an existing destination's
    payload to differ by.

    Raises ``ValueError`` for an unreviewed point-attribute key; the caller
    degrades that destination to no reader.
    """
    if not narrows_export(dest, operator_owned):
        return exporter
    return FilteredMetricExporter(
        exporter,
        name=dest.name,
        allow=getattr(dest, "allow", None),
        point_attributes=getattr(dest, "point_attributes", None),
        resource_allow=getattr(dest, "resource_allow", None),
        named_entity_namespaces=getattr(dest, "named_entity_namespaces", None),
        hide_entity_names=hides_entity_names(dest, operator_owned),
    )
