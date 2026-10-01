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
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS

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
    its neighbour with another context window. EFFORT is refused on the same
    ground and needs its own rule, because ``catalog_key`` folds the effort
    suffix on purpose -- right for judging nativeness, where the dial is not
    part of the identity, and wrong for choosing a spelling to SEND. A harness
    in ``ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS`` advertises one row per model x
    effort while its ``model`` option takes only the bare id, so the pin stored
    for it names no effort and every advertised row names one: folding across
    that gap let the tie-break pick a row by LENGTH, and
    :func:`_push_model_via_effort_split` then applied that row's bracket as
    ``reasoning_effort``. A candidate whose effort half differs from the pin's
    (:func:`model_registry.split_effort_suffix`, which reports no effort for a
    ``[1m]`` WINDOW suffix) is therefore not a spelling of it. Several
    advertised spellings of the SAME model at the SAME effort can remain (a base
    and a 1M variant the registry lists as one model); the winner is
    :func:`model_registry.preferred_advertised_spelling`, the tie-break
    :func:`model_registry.resolve_wire_model_id` applies, so two candidates this
    fold admits cannot be ordered differently by the wire fold.

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
    _base, wanted_effort = model_registry.split_effort_suffix(wanted)
    folded = [
        m
        for m in ids
        if model_registry.catalog_key(m) == wanted_key
        and model_registry.split_effort_suffix(m.strip().lower())[1] == wanted_effort
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


def _pair_id_bare_spelling(model_id: str, ids: Sequence[str]) -> str:
    """The BARE model half an advertised pair row offers *model_id*, or ``""``.

    A harness in ``ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS`` keeps TWO spellings of
    one selection and advertises only one of them. ``models.availableModels`` is
    one row per model x reasoning effort (``gpt-6-astra[max]``) while the
    ``model`` config option -- the channel a switch actually goes down -- takes
    only the bare ``gpt-6-astra``, the effort travelling down a separate option.
    So on this harness "absent from the advertised list" does not mean "not
    served": the pin Crew stores is routinely the bare id, the one
    :func:`_push_model_via_effort_split` itself records whenever an effort is
    refused, and the one the adapter accepts in a single write.

    Answers with the bare half of an advertised row -- evidence from the same
    ``session/new`` the spelling fold already reads, not a spelling invented
    here -- for a row that names an EFFORT and whose model half is the pin's:

      - a model no row names under any effort resolves to ``""`` and takes the
        withhold, because there is no advertised bare half to answer with;
      - a BARE advertised row is skipped: the literal test and the spelling fold
        above own that case, and this is only the SECOND vocabulary;
      - a ``[1m]`` WINDOW suffix is not an effort
        (:func:`model_registry.split_effort_suffix`), so no comparison here ever
        sheds a window marker, and a claude-shaped id can never reach this at all
        because its harness is not a member.

    An effort the account does not advertise resolves to the bare model too --
    the degradation ``_push_model_via_effort_split`` already performs when the
    adapter refuses the effort write: the MODEL is applied and the adapter owns
    the dial. What never happens is the inverse, answering with another row's
    bracket, which would apply a reasoning effort the operator did not choose.
    """
    wanted_base, _effort = model_registry.split_effort_suffix(model_id.strip().lower())
    if "[" in wanted_base:
        # A bracket ``split_effort_suffix`` declined to take names a context WINDOW,
        # not an effort. The bare half of a pair row carries no window marker, so
        # answering with it would move the pin to its other-window neighbour --
        # exactly the swap :func:`model_registry.same_registered_model` refuses one
        # dial over, and the reason ``[1m]`` is excluded from the effort split in
        # the first place. Not this rule's to decide.
        return ""
    wanted_key = model_registry.catalog_key(wanted_base)
    if not wanted_key:
        return ""
    for candidate in ids:
        base, effort = model_registry.split_effort_suffix(candidate.strip())
        if not effort:
            continue
        if model_registry.catalog_key(base) != wanted_key:
            continue
        if not model_registry.same_registered_model(wanted_base, base):
            continue
        return base
    return ""


def resolve_pin_spelling_on(
    model_id: str, advertised: Sequence[str] | None, *, backend: str = ""
) -> str:
    """:func:`resolve_pin_spelling`, plus the second vocabulary of a pair-id harness.

    ONE home for that question rather than a copy per wire site: the startup
    application of a persisted pin, the shared-runtime substitute path, the
    warm-pool re-apply and the fallback chain's wire fold all cross it, and
    one spelling of "what does this pin resolve to here" per site would
    eventually disagree about a model the operator pinned.

    *backend* is the harness the answer will be SENT to. An empty one -- a caller
    that is not choosing a wire spelling for a live session, such as the picker
    filter -- keeps :func:`resolve_pin_spelling` verbatim,
    ADVERTISED-spelling contract and all. Only a member of
    ``ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS`` widens it, and only where the fold has
    already answered ``""``, so no resolution that succeeds today changes.
    """
    resolved = resolve_pin_spelling(model_id, advertised)
    if resolved or backend not in ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS:
        return resolved
    ids = [m.strip() for m in (advertised or []) if m and m.strip()]
    return _pair_id_bare_spelling(model_id, ids) if ids else ""


def resolve_usable_model(
    preferred: str, advertised: Sequence[str] | None, *, backend: str = ""
) -> str:
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
      - concrete, served only as the bare half of an advertised ``<model>[<effort>]``
        row, on a *backend* whose ``model`` option takes that bare half ->
        the BARE spelling (:func:`resolve_pin_spelling_on`);
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
    # *backend* additionally admits a pair-id harness's bare ``model``-option
    # spelling, which its advertised list never carries; a caller that passes
    # none keeps the fold verbatim.
    return resolve_pin_spelling_on(preferred, ids, backend=backend)


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
