"""Guards on ``PRODUCT_METRICS``: every name is a real counter, gauge or histogram.

``PRODUCT_METRICS`` is the allowlist a product destination exports under. It is
spelled as literal strings so the module stays import-light (``metrics.turns``
imports ``metrics.provider`` at module scope, and the recorder reads this set), so
a typo or a renamed instrument cannot be caught at import. These tests are where
it is caught instead:

* a name with no declaring constant fails ``test_every_name_is_a_declared_instrument``;
* an up/down counter or an unknown name fails
  ``test_every_name_is_an_exportable_kind``.

Both run off the SAME resolver the selftest at the bottom feeds deliberate bad
input to, so "nothing failed" here means the checks can actually see a failure
rather than that they looked at nothing.
"""

from __future__ import annotations

import importlib

from kiro_crew.metrics import inventory_gauges as ig
from kiro_crew.metrics import process_gauges as pg
from kiro_crew.metrics.product_metrics import PRODUCT_METRICS
from kiro_crew.metrics.provider import histogram_bounds

#: Modules that DECLARE instrument names as module constants. An instrument is
#: created lazily by the recorder at its first emit, so there is no runtime
#: registry to read; the declaring constant is the closest thing to one, and
#: every emit site in the tree passes one of these rather than a literal.
_DECLARING_MODULES = (
    "kiro_crew.metrics.events",
    "kiro_crew.metrics.sessions",
    "kiro_crew.metrics.turns",
    "kiro_crew.metrics.http_metrics",
    "kiro_crew.metrics.db_metrics",
    "kiro_crew.metrics.tool_calls",
    "kiro_crew.metrics.inventory_gauges",
    "kiro_crew.metrics.process_gauges",
)

#: Up/down counter instrument names. EMPTY, and that is a fact about the tree
#: rather than an omission: ``MetricsRecorder.up_down_counter`` exists but no call
#: site in the repo uses it, because "how many right now" is an observable gauge
#: read from live state. Kept as a named set so the kind resolver below has a
#: branch for it and the selftest can prove that branch rejects.
_UPDOWN_INSTRUMENTS: "frozenset[str]" = frozenset()


def _declared_instrument_names() -> "dict[str, str]":
    """Every declared instrument name -> the constant that declares it."""
    found: "dict[str, str]" = {}
    for module_name in _DECLARING_MODULES:
        module = importlib.import_module(module_name)
        for attr, value in vars(module).items():
            if attr.startswith("_") or not isinstance(value, str):
                continue
            if value.startswith("kirocrew."):
                found.setdefault(value, f"{module_name.rsplit('.', 1)[1]}.{attr}")
    return found


def _gauge_names() -> "frozenset[str]":
    return frozenset(ig.ALL_METRIC_NAMES) | frozenset(pg.ALL_METRIC_NAMES)


def _instrument_kind(
    name: str,
    *,
    declared: "dict[str, str] | None" = None,
    gauges: "frozenset[str] | None" = None,
    histograms: "frozenset[str] | None" = None,
    updown: "frozenset[str] | None" = None,
) -> "str | None":
    """Name the instrument kind behind *name*, or None when there is none.

    Order matters: histogram and up/down counter are checked BEFORE the counter
    fallback, because the fallback is what every declared non-gauge name lands on
    and an unchecked kind would read as an exportable counter.

    Every population is injectable so the selftest can drive each branch. The
    histogram oracle is ``provider.histogram_bounds()``, which
    ``test_provider_bucket_views.py`` already pins as the COMPLETE histogram set
    in both directions -- so a histogram cannot hide from this by not having
    bounds.
    """
    declared = _declared_instrument_names() if declared is None else declared
    gauges = _gauge_names() if gauges is None else gauges
    histograms = frozenset(histogram_bounds()) if histograms is None else histograms
    updown = _UPDOWN_INSTRUMENTS if updown is None else updown

    if name not in declared:
        return None
    if name in histograms:
        return "histogram"
    if name in updown:
        return "up_down_counter"
    if name in gauges:
        return "gauge"
    return "counter"


#: Histograms are exported as native OTLP histograms (the product destination's
#: reader is delta), so a distribution needs no bucket-labelled counter in its
#: place. Up/down counters stay out: a current-state reading is a gauge.
_EXPORTABLE_KINDS = frozenset({"counter", "gauge", "histogram"})


def test_every_name_is_a_declared_instrument():
    """A name nothing declares would export nothing while reading as if it did."""
    declared = _declared_instrument_names()
    assert declared, "no instrument constants were collected; the resolver is broken"
    undeclared = sorted(name for name in PRODUCT_METRICS if name not in declared)
    assert not undeclared, (
        f"in PRODUCT_METRICS with no declaring instrument constant: {undeclared}. "
        "Either the instrument is not on this branch (list it in the PENDING "
        "comment instead) or the name is misspelled -- mind the kirocrew. prefix."
    )


def test_every_name_is_an_exportable_kind():
    """Up/down counters are not exportable; counters, gauges and histograms are.

    A histogram leaves as a native OTLP histogram, explicit buckets and all, so
    the kind check accepts it; an up/down counter is excluded because a
    current-state reading is a gauge.
    """
    declared = _declared_instrument_names()
    gauges = _gauge_names()
    histograms = frozenset(histogram_bounds())
    offenders = {}
    for name in sorted(PRODUCT_METRICS):
        kind = _instrument_kind(name, declared=declared, gauges=gauges, histograms=histograms)
        if kind not in _EXPORTABLE_KINDS:
            offenders[name] = kind
    assert not offenders, f"not an exportable instrument kind: {offenders}"


#: Names deliberately held OUT of the allowlist because an attribute value on
#: them is not a closed enum. Being a counter with a real instrument is not
#: enough to export: an open value is a cardinality and privacy decision that
#: the kind check above cannot see, so each one is named here with its reason and
#: asserted absent. Putting one back then means deleting a line a reader has to
#: read, rather than quietly adding a name to a set.
_HELD_ON_OPEN_ATTRIBUTES = {
    "kirocrew.turn.tokens": (
        "`model` is the configured model id -- on a non-Kiro backend it can be a "
        "provider resource id -- and `provider` is configured the same way, so "
        "neither is drawn from a set this repo controls"
    ),
    "kirocrew.turn.duration": (
        "carries the same configured `model` and `provider` values as "
        "turn.tokens; kirocrew.session.duration is the exported histogram"
    ),
}


def test_names_held_on_an_open_attribute_stay_out():
    """A counter can be real, wanted, and still not exportable.

    ``turn.tokens`` is the case: the instrument exists, the number is wanted, and
    its `model` / `provider` values are whatever the person configured. It waits
    for a bucketed or enumerated stand-in for those two.
    """
    present = {
        name: reason for name, reason in _HELD_ON_OPEN_ATTRIBUTES.items() if name in PRODUCT_METRICS
    }
    assert not present, (
        f"held out of PRODUCT_METRICS but present: {present}. Export needs every "
        "attribute value to come from a closed enum; close the attribute first, "
        "then remove the entry here in the same change."
    )


def test_the_held_names_are_real_instruments_so_this_guard_is_not_vacuous():
    """Selftest: a typo here would assert the absence of a name nobody has.

    The guard above passes trivially if its keys are misspelled, so each one is
    resolved against a declaring constant. That also keeps the entry honest: it
    names something that genuinely could be exported today.
    """
    declared = _declared_instrument_names()
    unknown = sorted(name for name in _HELD_ON_OPEN_ATTRIBUTES if name not in declared)
    assert not unknown, f"held names with no declaring instrument constant: {unknown}"


def test_process_gauges_are_not_product_metrics():
    """Machine readings stay off the allowlist.

    ``process_gauges`` publishes this process's own thread, descriptor, memory,
    CPU and GC readings. The guide lists them as deliberately not exported, so one
    of them appearing in PRODUCT_METRICS would be a reading allowed through the
    allowlist against a written promise.
    """
    overlap = sorted(set(pg.ALL_METRIC_NAMES) & set(PRODUCT_METRICS))
    assert not overlap, f"process gauges in PRODUCT_METRICS: {overlap}"


def test_the_resolver_can_see_each_failure_it_is_asked_to_catch():
    """Selftest: prove the checks above are not passing by looking at nothing.

    Each branch the kind check relies on is driven here with deliberate input.
    Remove any one from ``_instrument_kind`` and the matching line fails, so "no
    offenders" above is a real observation.
    """
    declared = {"kirocrew.a.counter": "x.A", "kirocrew.a.hist": "x.H"}
    gauges = frozenset({"kirocrew.a.gauge"})
    histograms = frozenset({"kirocrew.a.hist"})
    updown = frozenset({"kirocrew.a.updown"})
    kind = lambda name: _instrument_kind(  # noqa: E731 - one call shape, four rows
        name, declared=declared, gauges=gauges, histograms=histograms, updown=updown
    )

    # An unknown name: no instrument at all.
    assert kind("kirocrew.not.declared") is None
    # A histogram, declared, resolved as one (and exportable, natively).
    assert kind("kirocrew.a.hist") == "histogram"
    assert "histogram" in _EXPORTABLE_KINDS
    # An up/down counter, declared, rejected on kind. Driven through the injected
    # set because the tree declares none -- the branch is still live code.
    assert kind("kirocrew.a.updown") is None, "an undeclared updown name is unknown first"
    assert (
        _instrument_kind(
            "kirocrew.a.updown",
            declared={**declared, "kirocrew.a.updown": "x.U"},
            gauges=gauges,
            histograms=histograms,
            updown=updown,
        )
        == "up_down_counter"
    )
    assert "up_down_counter" not in _EXPORTABLE_KINDS
    # And the plain kinds that pass, so the resolver is not simply rejecting all.
    assert kind("kirocrew.a.counter") == "counter"
    assert (
        _instrument_kind(
            "kirocrew.a.gauge",
            declared={**declared, "kirocrew.a.gauge": "x.G"},
            gauges=gauges,
            histograms=histograms,
            updown=updown,
        )
        == "gauge"
    )


def test_a_real_histogram_name_resolves_as_a_histogram():
    """The same proof against the live populations, not injected ones.

    Picks an actual histogram off ``histogram_bounds()`` and asserts the resolver
    calls it a histogram. If the oracle ever came back empty, every histogram
    would silently resolve to "counter" and the kind check could not tell the two
    apart -- this line is what makes that visible.
    """
    real_histograms = sorted(histogram_bounds())
    assert real_histograms, "histogram_bounds() is empty; the histogram oracle is dead"
    declared = _declared_instrument_names()
    checked = [n for n in real_histograms if n in declared]
    assert checked, "no histogram name resolves to a declaring constant"
    for name in checked:
        assert _instrument_kind(name) == "histogram", name
