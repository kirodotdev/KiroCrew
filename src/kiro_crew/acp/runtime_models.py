"""The model catalog an ACP harness advertises, and how a model is chosen from it.

Pure helpers over the advertised model list: whether a model id is usable on this
account, how a pinned spelling maps onto the catalog's spelling of the same model,
which model a session falls back to, and how a harness's model-substitution advisory
is read. The per-transport model push -- ``set_model`` and its config-option ladder --
stays on ``AcpClient`` and ``AcpSessionHandle``.

``kiro_crew.acp.client`` re-exports every name defined here.
"""

from __future__ import annotations

import re
from typing import Sequence

from kiro_crew import model_registry

DEFAULT_MODEL = "auto"


def advertised_model_ids(entries: object) -> list[str]:
    """Model ids out of an ``availableModels``-shaped list, defensively.

    The advertised list is remote input reshaped by several backends, so this
    tolerates anything that is not a list of ``{"modelId": ...}`` dicts and
    returns what it can. Shared by the three call sites that pre-flight a model
    so none of them re-derives the shape — and so a surprising payload degrades
    to "entitlement unknown" (empty list -> :func:`model_is_unusable` allows the
    send) instead of raising inside session startup.
    """
    if not isinstance(entries, (list, tuple)):
        return []
    ids: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("modelId") or entry.get("value") or ""
        if isinstance(model_id, str) and model_id.strip():
            ids.append(model_id)
    return ids


def model_is_unusable(model_id: str, advertised: Sequence[str] | None) -> bool:
    """True when *advertised* is known and excludes *model_id*.

    The counterpart to :func:`_model_is_unentitled`, moved BEFORE the wire: that
    one explains a rejection after the fact, this one declines to send a model
    the backend already told us the account cannot run. Deliberately ONE shared
    predicate rather than a copy per call site — the same reason that keeps the
    formatter and the retry classifier share a discriminator: two spellings of
    "can this account use it" would eventually disagree.

    Returns False — allow the send — whenever entitlement is unknowable: an
    empty/None advertised set (no session yet, or a backend that omits
    ``models``) must not be read as "nothing is allowed", which would withhold
    every model on a backend that simply does not advertise.

    Only meaningful where the advertised ids share a namespace with *model_id*,
    and callers gate on that. kiro-cli's advertised ids are exactly the ids
    ``session/set_model`` accepts, so an id absent from the list is genuinely
    unusable. The claude backend advertises BARE ids (``claude-opus-4-8[1m]``)
    while the configured model is the prefixed provider id
    (``global.anthropic.claude-opus-4-8[1m]``), so comparing those two
    namespaces would call every legitimate model unusable; that backend
    announces its own substitutions through the ``session/new`` advisory
    instead (see ``_new_session_following_substitution``).

    A pin carrying a stale ``<namespace>::<bare-id>`` qualifier (stored when a
    catalog advertised the qualified spelling, judged against one advertising
    the bare id) is the one comparable mismatch, and it is deliberately NOT
    folded here: this predicate's permissive answer feeds the wire sites, and
    a still-qualified id on the wire is a spelling the backend never
    advertised. :func:`resolve_pin_spelling` is the shared fold for that case —
    it answers with the advertised spelling, so a caller can compare AND send
    one consistent id.
    """
    if not advertised:
        return False
    wanted = model_id.strip().lower()
    return wanted not in {m.strip().lower() for m in advertised if m and m.strip()}


def resolve_pin_spelling(model_id: str, advertised: Sequence[str] | None) -> str:
    """The advertised spelling *model_id* resolves to, or ``""`` when none.

    The companion to :func:`model_is_unusable` for values that arrive from
    storage rather than from the live picker: a persisted pin can carry a
    ``<namespace>::<bare-id>`` qualifier from the catalog that advertised it
    (for example ``openrouter::z-ai/glm-5.3-flash``), while the session being
    judged advertises the BARE id. The literal membership test then misses for
    a model the backend fully serves. This fold recovers the match without
    per-provider exemptions: the full id is tried first, and only on a miss is
    ONE leading ``<namespace>::`` qualifier peeled and the tail retried — so a
    pin the backend genuinely does not serve still resolves to ``""`` under
    either spelling, and an id advertised verbatim (qualifier and all) never
    gets peeled at all.

    When the peel misses too, both sides are folded with
    :func:`model_registry.catalog_key` — the same fold
    :func:`model_registry.namespace_vocabulary` judges nativeness with. That
    fold strips an inference-profile prefix, the ``[1m]`` window marker and an
    effort suffix, so a pin spelled in another namespace's provider-id form
    (``global.anthropic.claude-opus-4-8[1m]``) meets the bare id this harness
    advertises for the same model (``claude-opus-4.8``). Without it the two
    sides fold with different functions: the vocabulary side calls the pin
    native, this side finds no spelling, and the cold start reports an
    entitlement problem for what is a spelling one. The fold is a SPELLING fold,
    not a model fold: a candidate the static registry places as a DIFFERENT
    canonical model from the pin (``claude-opus-4-8`` at 200K against a pin
    naming the 1M ``claude-opus-4.8``) is rejected even though ``catalog_key``
    folds the window marker away -- see
    :func:`model_registry.same_registered_model` -- so a pin never resolves to
    its neighbour with another context window. Several advertised spellings of
    the SAME model can remain (a base and a 1M variant the registry lists as one
    model); the winner is :func:`model_registry.preferred_advertised_spelling`,
    the tie-break :func:`model_registry.resolve_wire_model_id` applies, so the
    two folds cannot prefer different spellings.

    Returns the ADVERTISED spelling of the match, not the caller's: the result
    is meant to be sent on the wire (``session/set_model`` accepts advertised
    ids), and it keeps the display verdict and the wire withhold answering from
    one fold so the two cannot disagree about what "usable" means. Matching is
    case/whitespace-insensitive on both sides, mirroring
    :func:`model_is_unusable`.

    An empty/unknown *advertised* set returns ``""`` — NOT as a "withheld"
    answer, but as "nothing to resolve against": callers must keep routing the
    withhold decision itself through :func:`model_is_unusable`, whose
    empty-set-means-allow contract (harness-parity H12) this function does not
    replace.
    """
    ids = [m.strip() for m in (advertised or []) if m and m.strip()]
    if not ids:
        return ""
    by_key = {m.lower(): m for m in ids}
    wanted = model_id.strip().lower()
    if wanted in by_key:
        return by_key[wanted]
    namespace, sep, bare = wanted.partition("::")
    if sep and namespace and bare in by_key:
        return by_key[bare]
    wanted_key = model_registry.catalog_key(wanted)
    if not wanted_key:
        return ""
    folded = [
        m
        for m in ids
        if model_registry.catalog_key(m) == wanted_key
        and model_registry.same_registered_model(model_id, m)
    ]
    return model_registry.preferred_advertised_spelling(folded)


def catalog_row_would_drop(model_id: str, advertised: Sequence[str] | None) -> bool:
    """True when the picker filter drops catalog row *model_id* against *advertised*.

    The one keep/drop verdict the dashboard model picker applies to a
    ``--list-models`` row, shared with the read-path revalidation that decides
    whether a snapshot is worth probing, so the two cannot disagree about which
    rows a snapshot hides. A row is KEPT when it is the ``auto`` sentinel
    (``auto`` or ``default``), when *advertised* lists it
    (:func:`model_is_unusable` is False — which includes an unknown/empty
    advertised set), or when :func:`resolve_pin_spelling` folds it onto an
    advertised spelling (a ``<namespace>::<bare-id>`` row the picker rewrites
    to the bare id). Every other row — an empty id included — drops.

    This is the per-row verdict only. The picker additionally de-duplicates
    rows that resolve to one advertised spelling, and shows the whole catalog
    when no non-``auto`` row survives against a set that does not advertise
    ``auto`` (a namespace mismatch); those are decisions about the list, made
    by the caller.
    """
    wanted = (model_id or "").strip().lower()
    if wanted in ("auto", "default"):
        return False
    if not model_is_unusable(model_id or "", advertised):
        return False
    return not resolve_pin_spelling(model_id or "", advertised)


def resolve_usable_model(preferred: str, advertised: Sequence[str] | None) -> str:
    """Resolve a SUBSTITUTE (non-explicit) model choice to what the account can
    run, mirroring the interactive path's reset-to-default (``_wire_model_id``).

    Returns ``""`` to mean **"do NOT override — inherit the session's backend
    default"** (the served model ``session/new`` already assigned), so the wire
    never receives a model the partition does not serve. Rules:

      - empty ``preferred``           -> ``""`` (already inheriting the default);
      - ``advertised`` unknown/empty  -> ``""`` for the ``"auto"`` sentinel (never
        send a literal ``"auto"`` we cannot verify — some partitions do not
        serve it), else trust a concrete caller-supplied id (nothing to check
        it against);
      - ``"auto"``                    -> ``"auto"`` IFF the backend advertises it,
        else ``""`` — exactly ``_wire_model_id``'s
        ``"auto" if "auto" in advertised else ""``;
      - concrete + usable             -> that id;
      - concrete, served only under its peeled spelling -> the ADVERTISED
        spelling. A persisted pin can carry a stale ``<namespace>::<bare-id>``
        qualifier while the session advertises the bare id; the literal miss is
        retried through
        :func:`resolve_pin_spelling`, and the fold's answer — not the caller's
        spelling — goes on the wire, because the qualified spelling is one the
        backend never advertised;
      - concrete + not served under either spelling -> ``""`` (inherit the
        served default rather than substituting a possibly-unavailable
        ``"auto"``).

    The EXPLICIT user-pick paths do NOT use this: they ``raise``
    (``model_is_unusable``) so a user who chose a model sees an error, not a swap.
    A reactive retry (``run_bg_oneliner``) remains a thin backstop for the
    fail-open case where ``advertised`` was unknown at send time.
    """
    if not preferred:
        return ""
    if not advertised:
        return "" if preferred == "auto" else preferred
    ids = [m for m in advertised if m and m.strip()]
    if preferred == "auto":
        return "auto" if not model_is_unusable("auto", ids) else ""
    if not model_is_unusable(preferred, ids):
        return preferred
    # Literal miss: the pin may only differ by a stale ``<namespace>::``
    # qualifier. The fold answers with the advertised spelling on a hit and
    # ``""`` when the model is absent under both spellings — exactly the
    # inherit-the-default answer this path wants.
    return resolve_pin_spelling(preferred, ids)


def pick_served_default(current: str, advertised: Sequence[str] | None) -> str:
    """The served model a session on *current* must switch to, or ``""``.

    ``session/new`` picks the model itself and reports it as
    ``currentModelId``, and that choice is the backend's own default rather
    than anything Crew asked for. A partition does not have to serve the model
    its backend defaults to: an account whose region omits ``"auto"`` can be
    handed ``"auto"`` at birth, and then every ``session/prompt`` dies with
    "your account does not have access to model 'auto'". So inheriting the
    backend default is only safe when the inherited model is one the same
    response advertised, and this answers which served model to move to when it
    is not.

    Returns ``""`` — nothing to do, keep inheriting — whenever the question
    cannot be answered or the answer is already right:

      - ``advertised`` empty/None: entitlement is unknowable, exactly
        :func:`model_is_unusable`'s empty-set-means-allow contract. Reading an
        absent list as "nothing is served" would switch every session on a
        backend that simply does not advertise;
      - empty ``current``: the backend echoed no model, so there is no evidence
        it picked an unserved one. Fail open rather than override a default we
        cannot see;
      - ``current`` served, under its own spelling or under a peeled
        ``<namespace>::`` one (:func:`resolve_pin_spelling`, the shared fold):
        the session is already on a model the account can run.

    Otherwise the default is genuinely unserved and the session needs a real
    model: ``"auto"`` when the backend advertises it — the same
    "let the backend choose" id ``resolve_usable_model`` and the dashboard's
    ``_wire_model_id`` send — else the FIRST advertised id, because a served
    model chosen for the user beats a session that cannot answer a single
    prompt.
    """
    ids = [m for m in (advertised or []) if m and m.strip()]
    if not ids:
        return ""
    if not current.strip():
        return ""
    if not model_is_unusable(current, ids):
        return ""
    if resolve_pin_spelling(current, ids):
        return ""
    return "auto" if not model_is_unusable("auto", ids) else ids[0]


# Matches claude-agent-acp policy-substitution advisories:
#   Model "X" is restricted by your organization's settings. Using Y instead.
# Emitted when admin-tier policy (managed-settings / policyHelper) or the
# Bedrock headless tier substitutes the requested model. The substitute is
# already in effect and the session is live; claude-agent-acp wraps it as a
# JSON-RPC -32603 error frame only because requested != applied. Informational,
# not fatal -- so we keep the session instead of raising.
_MODEL_SUBSTITUTION_ADVISORY_RE = re.compile(
    r"is\s+restricted\b.+\bUsing\s+\S+\s+instead",
    re.IGNORECASE | re.DOTALL,
)


def _extract_advisory_detail(error: object) -> str:
    """Pull the ``data.details`` (or plain string ``data``) out of an ACP error.

    Centralizes the shape-handling for the model-substitution advisory so the
    detector, the substitute-extractor, and the runtime advisory handler all
    move in lockstep when the advisory format evolves. Returns an empty string
    if the error is not a dict, has no data, or carries no details.
    """
    if not isinstance(error, dict):
        return ""
    data = error.get("data")
    if isinstance(data, dict):
        return str(data.get("details", "") or "")
    if isinstance(data, str):
        return data
    return ""


def _is_model_substitution_advisory(error: object) -> bool:
    """True iff *error* is a claude-agent-acp model-substitution advisory.

    The adapter emits this on session/new (and session/load /
    set_config_option) when policy substitutes the requested model. The
    substitution is already applied -- the session is live on the substitute
    -- but the response carries an error frame rather than a warning. Treat it
    as non-fatal: log and continue. The match is deliberately narrow (code
    -32603 AND a detail string containing BOTH 'is restricted' and 'Using X
    instead') so genuine -32603 internal errors, invalid params, malformed
    sessions, throttles, etc. still raise.
    """
    if not isinstance(error, dict):
        return False
    if error.get("code") != -32603:
        return False
    detail = _extract_advisory_detail(error)
    if not detail:
        return False
    return bool(_MODEL_SUBSTITUTION_ADVISORY_RE.search(detail))


# Captures the model id the backend says it will serve instead, from the same
# advisory: "... Using <model-id> instead." The substitute id is whitespace-free
# (e.g. ``global.anthropic.claude-sonnet-4-6[1m]``), so ``\S+`` lifts it cleanly.
_MODEL_SUBSTITUTE_RE = re.compile(
    r"\bUsing\s+(?P<model>\S+)\s+instead\b",
    re.IGNORECASE,
)


def _substitute_model_from_advisory(error: object) -> str | None:
    """Return the model id the gateway substituted to, or None.

    Parses the ``-32603`` advisory ("Model X is restricted ... Using Y
    instead.") and returns ``Y`` -- the model the gateway will actually serve.
    The caller adopts it and re-issues ``session/new`` so a real session is
    created (the advisory itself returns no sessionId). Returns None when the
    error is not a substitution advisory or the substitute can't be parsed.
    """
    if not _is_model_substitution_advisory(error):
        return None
    detail = _extract_advisory_detail(error)
    match = _MODEL_SUBSTITUTE_RE.search(detail)
    if not match:
        return None
    # Strip surrounding quotes/trailing punctuation a variant phrasing might add.
    model = match.group("model").strip().strip("\"'").rstrip(".,;")
    return model or None
