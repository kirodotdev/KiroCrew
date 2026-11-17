"""Which decision models the ceiling lets the Decision model picker offer.

``GET /api/decisions/provider`` (``handlers/decisions.py``) reports one answer per
decision model the picker can name, keyed by its allow-list id
(``capability.provider_id``): the hosted provider, each local preset and a SageMaker
endpoint. An endpoint the owner wrote by hand is not one of them: the list does not
govern it. Each answer is the AND of every part of the ceiling that governs that
model:

* its row: ``capabilities.decisions`` for every model, plus
  ``capabilities.decisions_local`` for a local preset (``denied_sides``);
* the ``endpoints`` list for a model that leaves the machine (the hosted default's
  host, a SageMaker runtime host in some region);
* its own entry in the ``providers`` list (``capability.permitted_providers``).

These are OFFERS: they decide what the picker draws as allowed, and they write no
SEL row per model. Every request that acts on an answer (the provider PUT, the
consent PUT, the send path) asks the same parts again through the audited checks,
so an offer can only ever be narrower in effect than what it shows.

Split from ``handlers/decisions.py`` for its size. Blocking: call off the event loop.
"""

from __future__ import annotations


def provider_offers(
    *,
    hosted_denied: bool,
    local_denied: bool,
    sagemaker_region: str = "",
) -> dict:
    """``{"hosted_permitted", "sagemaker_permitted", "provider_permitted", "listed"}``.

    *hosted_denied* / *local_denied* are the two rows' answers, already read (and
    audited) by the caller. *sagemaker_region* is the configured SageMaker region,
    asked first.
    """
    from kiro_crew.dashboard.handlers.decisions_sagemaker import hosted_offers
    from kiro_crew.decisions import capability, local_models

    listed = capability.permitted_providers()
    hosted_hosts, sagemaker_hosts = hosted_offers(hosted_denied, sagemaker_region)
    hosted = hosted_hosts and listed.get(capability.PROVIDER_HOSTED, False)
    sagemaker = sagemaker_hosts and listed.get(capability.PROVIDER_SAGEMAKER, False)
    permitted = {capability.PROVIDER_HOSTED: hosted}
    for preset in local_models.LOCAL_MODELS:
        permitted[preset.id] = not local_denied and listed.get(preset.id, False)
    permitted[capability.PROVIDER_SAGEMAKER] = sagemaker
    return {
        "hosted_permitted": hosted,
        "sagemaker_permitted": sagemaker,
        "provider_permitted": permitted,
        # The list's own answers, for the caller's configured-address check; not
        # part of the GET payload.
        "listed": listed,
    }
