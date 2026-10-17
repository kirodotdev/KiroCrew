"""Which harness a model pin was chosen for, so one harness's pin is not sent to another.

A stored model pin -- ``agent.model`` in ``config.json``, a dashboard slot's
``model``, a kiro agent spec's ``model`` field -- records WHAT was picked and
nothing about WHERE. Switching the ACP backend changes which adapter the next
session starts, and the pin travels unchanged, so a model chosen in one harness
is handed to a harness that never served it. The adapter refuses the id and the
session lands on that harness's own default, which is the right outcome reached
the wrong way: the user gets a warning about a model they did not pick this turn,
and every surface that reads the stored value keeps naming a model no turn runs.

The missing fact is recoverable without storing it. Each backend's ids live in a
model-registry NAMESPACE (:func:`kiro_crew.agent_sdk.backends.model_registry_namespace`),
and a namespace has a catalog: the static ``model_registry.json`` index plus either
the live advertised list from the current session or the shared advertised-model
cache. Factory call sites cover every surface through the shared cache; wire call
sites additionally carry the fresh list from their session. So "was this pin
chosen for this harness?" becomes a question the catalogs can answer.

Scoping a pin needs TWO findings, and requiring both is the whole design:

* the session's own harness has said what it serves -- it has a warm
  advertised catalog -- and this pin is not in it. An absence from a warm list
  the backend itself sent is evidence. An absence from the STATIC index is not:
  that file is a snapshot, it lists ``acp`` and ``claude_code`` only, and every
  other harness appears in it never. Reading a static gap as a refusal would
  drop a pin kiro serves perfectly well the first time a fresh install starts,
  before any kiro session has filled the ``acp`` bucket.
* some OTHER namespace's catalog claims the pin. That is what makes the pin
  attributable rather than merely unrecognized, and it is what keeps a
  real-but-unlisted id -- a regional Bedrock profile, a model newer than every
  catalog -- on the wire: nobody else claims it, so nothing says it was chosen
  elsewhere, so it is sent unchanged.

Both findings hold for the case that motivated this: the harness the pin was
chosen in had advertised its list (that is where the picker's options came from,
so any pin a user can click is in some catalog by construction), and the harness
being switched TO advertises its own list in the same breath as it refuses the
id.

Nothing is rewritten on disk. The stored pin stays as the user picked it and is
simply not read by a harness that cannot claim it, so switching BACK restores it
with no migration, no second field and no schema change. A pin is scoped at every
read rather than healed once at a write, which leaves one rule and no window in
which two stores disagree about what is pinned.

This module imports :mod:`kiro_crew.model_registry` and the model sentinel from
:mod:`kiro_crew.config.sections`. The sections module does not import the loader,
which keeps the loader's model-scope import acyclic.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from kiro_crew import model_registry
from kiro_crew.config.sections import DEFAULT_MODEL

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MODEL",
    "pin_applies",
    "scoped_pin",
]


def foreign_namespaces(
    model_id: str,
    namespace: str,
) -> frozenset[str]:
    """The namespaces OTHER than *namespace* whose own vocabulary holds *model_id*.

    Excludes *namespace* itself so the answer reads as "who else owns this", which
    is what both the refusal and its log line mean. Including it would let a
    harness be reported as foreign to itself -- two namespaces can list one model
    family, so a native pin its own account cannot run would otherwise be named as
    belonging elsewhere.

    """
    if not model_id or model_id == DEFAULT_MODEL:
        return frozenset()
    return frozenset(
        other
        for other in model_registry.catalog_namespaces()
        if other != namespace and model_registry.namespace_vocabulary(model_id, other)
    )


def pin_applies(
    model_id: str,
    namespace: str,
    *,
    advertised: Sequence[str] | None = None,
) -> bool:
    """Whether a pin spelled *model_id* may be applied to a session in *namespace*.

    False in exactly one case, the conjunction the module docstring describes:
    *namespace* has advertised a list of its own and this pin is not in it, AND
    another namespace's catalog claims the pin. A non-empty *advertised* is the
    current session's list and takes precedence over the shared cache for
    *namespace*. Everything else is True -- including an empty *namespace*, which
    is the caller not knowing which harness will run, where withholding would
    trade a wrong model for a missing one.
    """
    if not model_id or model_id == DEFAULT_MODEL or not namespace:
        return True
    if model_registry.namespace_vocabulary(model_id, namespace, advertised):
        # The id is this harness's own. Whether the ACCOUNT may run it is a
        # separate question, owned by the entitlement guard downstream -- routing
        # an unentitled-but-native pin through here would report a wrong cause.
        return True
    if not (advertised or model_registry.advertised_models(namespace)):
        # This harness has not told us what it serves, so its silence is not a
        # refusal. Only the static index could object here, and a static gap is
        # a snapshot's age rather than a fact about the harness.
        return True
    # Not in this harness's vocabulary. Send it anyway unless another harness
    # owns the id: one nobody owns may still be real (a regional profile, a model
    # newer than every catalog), and an unrecognized id is the caller's to send.
    return not foreign_namespaces(model_id, namespace)


def scoped_pin(
    model_id: str,
    namespace: str,
    *,
    advertised: Sequence[str] | None = None,
    source: str = "",
    log_level: int = logging.INFO,
) -> str:
    """*model_id* when it applies to *namespace*, else ``""`` (inherit the default).

    ``""`` rather than ``auto`` because that is the spelling every model-resolution
    tier already treats as "defer to the tier below, then let the backend pick",
    so an out-of-scope pin reads to its consumers exactly like an unset one.

    A non-empty *advertised* is the current session's list for *namespace*.
    *source* names the store the pin came from (``"agent.model"``, ``"slot"``, …)
    and travels to the debug log. The scope note itself is INFO: the pin is not a
    fault and the session is about to run correctly, so this records a stale
    setting rather than something the user must act on. The WARNING it replaces
    came from the adapter refusing the id on the wire, which is the event this
    exists to stop happening.
    """
    if pin_applies(model_id, namespace, advertised=advertised):
        return model_id
    owners = foreign_namespaces(model_id, namespace)
    chosen_for = "/".join(sorted(owners)) or "another harness"
    logger.log(
        log_level,
        "model pin %s was chosen for %s, not %s; this session inherits the %s default. "
        "The pin is kept and applies again on %s.",
        model_id,
        chosen_for,
        namespace,
        namespace,
        chosen_for,
    )
    if source:
        logger.debug("out-of-scope pin %s came from %s", model_id, source)
    return ""
