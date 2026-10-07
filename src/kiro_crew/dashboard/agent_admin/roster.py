"""The ``GET /api/agents`` row: the per-value mask, the guard that keeps a crew PUT from persisting it, the avatar projection, the row serializer and the ``?project_path=`` root resolver that scopes the roster."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.agents import (
        KiroCrewAgentConfig,
        _link_chain_refused,
        _redact_external,
        _safe_avatar,
        _unc_refused,
        is_sensitive_path,
    )


def _roster_mask(value: object) -> str:
    """Render ONE agent-record value for a roster row, masking what cannot be shown.

    Every record value is agent- or package-writable: an agent can edit
    ``config.json`` directly, and ``_do_agents_sync`` copies ``description``
    straight off a discovered agent spec, so a third-party package controls that
    string. A value the redactors would alter -- credential- or
    exfiltration-URL-shaped text -- is therefore replaced WHOLESALE by
    ``_SENSITIVE_MASK``, the sentinel ``_masked_config_dict`` already uses for
    the same job on ``GET /api/config/kirocrew``. A non-string (the loader lets
    an object through five declared-``str`` fields) is masked too: it is not
    renderable, so there is nothing to show. Benign content is byte-identical.

    **A fixed sentinel rather than redacting in place, and that is the whole
    design.** An in-place scrub makes the browser's view a FUNCTION of the
    stored value, so the write-side rule that keeps a read-modify-write from
    persisting that view (``_carries_mask``) has to recognise it by
    recomputing the transform -- which breaks in two ways a sentinel does not:

    * **Redaction-chain drift.** This same response is also wrapped in
      ``redact_record_strings``, whose order differs from ``_redact_external``'s.
      A recomputed-equality rule would stop matching and silently persist the
      redacted text; an exact sentinel survives, because scrubbing
      ``_SENSITIVE_MASK`` leaves it unchanged.
    * **Stale-view skew.** If the stored value changes between the GET and the
      PUT (an agent editing ``config.json``, a second dashboard tab), a
      recomputed rule compares the old view against the NEW value, fails to
      match, and writes ``[REDACTED ...]`` text into the config as though the
      operator had typed it. The sentinel does not depend on the stored value at
      all, so this cannot happen.

    Named cost: a value containing one credential-shaped token is masked
    entirely, so the owner loses the benign remainder of that string rather than
    seeing it partially redacted. That is the same trade ``_masked_config_dict``
    already makes, and it is the price of a view that cannot be mistaken for
    content.
    """
    # Function-local for the reason recorded at the ``_validate_role_model``
    # import in ``handlers.agents``: ``handlers.core`` resolves ``_get_config_lock``
    # from that module, so a module-level import here would close the cycle.
    from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

    if not isinstance(value, str):
        return _SENSITIVE_MASK
    return value if _redact_external(value) == value else _SENSITIVE_MASK


def _carries_mask(incoming: object) -> bool:
    """True when *incoming* still CARRIES the mask, so it is not real content.

    The write-side half of ``_roster_mask``. A client that read a roster row and
    echoed it back sends the mask; persisting it would destroy the operator's
    stored value. Such a field is treated as UNCHANGED instead.

    **Containment, not equality.** An exact-match rule closes only the
    echo-it-back case. The editor renders the mask into a text input, so an
    operator who APPENDS to it submits ``"<mask> and also X"`` -- not equal to the
    sentinel, so an equality rule would persist the redaction glyphs plus the
    addition, replacing the stored original. Any string still containing the
    sentinel is therefore refused as content.

    Consequence, stated because it is a real limitation and not a free win: a
    genuine replacement must OMIT the sentinel entirely -- clear the field, then
    type the new value. An edit that keeps the mask and adds to it is dropped
    rather than half-applied. That is lossy in the operator's INTENT, but it never
    destroys what is stored, and the alternative writes redaction glyphs into
    ``config.json`` over the real value.

    This is the remedy ``_masked_config_dict``'s docstring prescribes -- "MUST
    treat ``_SENSITIVE_MASK`` as 'unchanged' and keep the stored value" -- read
    the strict way. Because the comparison is against a FIXED sentinel and never
    against a recomputation of the stored value, it is immune to which redaction
    chain produced the view and to the stored value having changed since the read.

    Accepted residual, identical in kind to the config endpoint's: an operator
    cannot store a value containing the mask string. It is eight U+2022 bullets.

    **Recursive, because one shipped field is STRUCTURED.** ``avatar`` is a dict
    whose ``traits`` values are masked (``_roster_avatar``), so an echoed avatar
    carries the sentinel one level DOWN. A top-level-only check sees a ``dict``,
    answers "not a mask", and lets ``_safe_avatar`` persist the sentinel over the
    stored trait -- the exact corruption this predicate exists to prevent, one
    level deeper than the flat fields. Any string anywhere inside the value
    therefore counts.

    Cost of the recursive form, stated because it is sharper than the flat one: if
    an avatar carries ANY masked trait, the whole avatar field is treated as
    unchanged, so an edit to a DIFFERENT trait in the same avatar is dropped too.
    A partial merge would be the alternative, and it is worse: it would have to
    decide field-by-field which half of a structured value is authoritative, and
    getting that wrong writes glyphs into stored config. Refusing the whole field
    never destroys what is stored.
    """
    from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

    if isinstance(incoming, str):
        return _SENSITIVE_MASK in incoming
    if isinstance(incoming, dict):
        return any(_carries_mask(val) for val in incoming.values())
    if isinstance(incoming, (list, tuple)):
        return any(_carries_mask(val) for val in incoming)
    return False


def _roster_avatar(value: object) -> dict:
    """The one STRUCTURED field a roster row ships: shape-allowlisted, not masked.

    ``avatar`` is a ``dict``, so ``_roster_mask``'s non-string rule would blank it
    wholesale -- and the dashboard needs it: ``AgentSelector.tsx`` declares
    ``avatar?: unknown`` commented "verbatim from the backend", and ``main``'s
    roster still ships it via the ``asdict`` spread this PR replaces. Withholding
    it would REGRESS a live feature rather than narrow a disclosure, so it is
    named in the allowlist like every other shipped field.

    **A shape allowlist, with masking confined to the leaves that can carry user
    text.** ``_safe_avatar`` is the config's own validator, so only
    ``{"kind": "ghost", "traits": {...}, "motions": {...}, "sounds": {...},
    "expressions": {...}}``,
    ``{"kind": "image", "v": ..., "file": "<digest>.<ext>", "sounds": {...}}`` and
    ``{"kind": "pack", "id": "<pack id>", "sounds": {...}}`` survive and junk
    collapses to ``{}``. Within that, ONLY ``traits`` values are masked:

    - ``kind`` and ``v`` are structural. Mask ``kind`` and the dashboard can no
      longer tell a ghost from an uploaded picture.
    - ``file`` is PINNED by ``_AVATAR_FILE_PIN_RE`` to ``<16-hex>.<ext>``, so it is
      not arbitrary text. A value constrained by a regex is safer than a masked
      one: the pin REFUSES a bad value where masking would destroy a good one, and
      a masked ``file`` makes the per-crew avatar endpoint resolve nothing --
      silently breaking the image.
    - ``traits`` values, and the ``eyes``/``mouth`` values of each ghost
      ``expressions`` state, are the only user-authored strings here, so they go
      through ``_roster_mask`` like any other roster string. The renderer resolves
      an unrecognized trait to absent (``EYES[k] ?? ''``), so a masked trait
      degrades that axis rather than breaking the face.
    - ``motions`` and ``sounds`` values are constrained by ``_safe_motions`` and
      ``_safe_sounds`` to a shipped animation or preset name, so they are pinned
      rather than masked -- the same reason ``file`` is.
    - ``id`` (on ``kind: "pack"``) is pinned by
      ``appearance_packs.safe_pack_id`` to letters, digits, dash and underscore,
      so it is not arbitrary text either — and masking it would make the pack
      routes resolve nothing, silently blanking the face for the same reason a
      masked ``file`` breaks the image.

    Honest limit on how far the two rules can be told apart: because
    ``_safe_avatar`` already pins every non-``traits`` leaf to a shape the redactors
    do not alter (a literal ``kind``, a digest ``file``, a hex ``tile``), a blanket
    mask over the validated dict would behave the SAME as this targeted one today.
    The targeted rule is chosen for intent and for the day that pin loosens, not
    because a live defect separates them -- and no test can pin the difference
    while the shape validator holds.

    No host path is disclosed by any of this. The picture's bytes live under the
    data home's agent-fenced ``run/avatars/`` dir and are served by the per-crew
    avatar endpoint; the config field only marks the choice.
    """
    safe = _safe_avatar(value)
    traits = safe.get("traits")
    if isinstance(traits, dict):
        safe = dict(safe)
        safe["traits"] = {
            axis: (_roster_mask(val) if isinstance(val, str) else val)
            for axis, val in traits.items()
        }
    expressions = safe.get("expressions")
    if isinstance(expressions, dict):
        safe = dict(safe)
        safe["expressions"] = {
            state: {
                axis: (_roster_mask(val) if isinstance(val, str) else val)
                for axis, val in axes.items()
            }
            for state, axes in expressions.items()
            if isinstance(axes, dict)
        }
    return safe


def _name_would_be_masked(name: str) -> bool:
    """True when *name* is credential-shaped, so a roster row would mask it.

    Keyed on ``_roster_mask`` itself rather than on a second detector, so the
    create-time rule and the read-time rule cannot drift apart: a name that would
    arrive masked is a name that can never be stored in the first place.
    """
    return _roster_mask(name) != name


def _agent_roster_row(
    name: str, scope: str, agent_cfg: KiroCrewAgentConfig, *, redact: bool
) -> dict[str, object]:
    """Serialize ONE ``GET /api/agents`` roster row.

    **Key half.** Explicit allowlist -- never a ``dataclasses.asdict`` spread,
    mirroring the rule ``handlers/members.py`` already documents for
    ``GET /api/members``. The response is a network-boundary contract, and a
    spread makes that contract "every field ``KiroCrewAgentConfig`` has now, plus
    every field anyone adds later", automatically -- so a field added by someone
    who never looked at this endpoint (internal bookkeeping, a filesystem path, a
    capability hint, a credential-shaped one) ships to the browser by omission.
    Naming each field inverts the default: nothing leaves unless it is added here
    deliberately. Both row sources go through this one function, so the
    ``cfg.agents`` rows and the project-scope rows cannot drift into different
    key sets.

    **Value half.** Every record value goes through ``_roster_mask``, for every
    caller, uniformly -- see there for why they are all untrusted and why the
    mask is a fixed sentinel. ``_carries_mask`` is its write-side half in
    ``api_kirocrew_agent_update``; neither is correct alone, and an end-to-end
    test does the GET then the PUT to prove the pair.

    Uniform rather than per-field on purpose: exempting the fields the agents
    page happens to write back would encode a claim about the CLIENT that this
    side cannot enforce -- and a false one, because
    ``api_kirocrew_agent_update`` accepts ``description`` and ``source`` too.

    ``name`` is the single exception, and only for the owner: it is the row's
    IDENTITY, addressing ``/api/agents/{name}`` for edit and delete and keying
    the usage sort, and it travels in the URL rather than the body so the
    write-side rule cannot protect it. Masking it would make the row
    unaddressable. An ``app`` token cannot reach those owner-gated routes, so the
    exemption buys it nothing and ``name`` is masked there. A credential-shaped
    name is refused at CREATION (``_name_would_be_masked``), closing the hazard
    at its source rather than at this one read site -- but only for names arriving
    through that route, so an already-stored one still reaches here and is still
    masked for every caller but the owner. Named cost: an app that feeds a roster
    name to another route sees the mask, which happens only for a name containing
    credential- or URL-shaped text.

    ``scope`` is never masked: it is a literal written here, not record content.
    The annotation is ``dict[str, object]`` rather than ``dict[str, str]`` because
    of ONE field: ``avatar`` is a structured ``dict`` the dashboard needs verbatim
    (see ``_roster_avatar``). Every other value is a ``str`` -- ``_roster_mask``
    returns one for every input, including the non-strings the loader lets
    through.

    Excluded on purpose, each verified to have NO consumer in ``website/src``:
    ``watchdog_tool_stall_suspect_secs`` and ``watchdog_tool_stall_hard_cap_secs``
    (per-agent watchdog windows -- backend scheduling knobs the roster does not
    render) and ``telegram_account`` (deprecated and inert, and the one record
    field naming an external messaging binding). Adding any of them back is a
    one-line change plus the pinned key set.
    """
    return {
        # ``name`` is masked for an app token (which can address nothing) and for
        # every PROJECT row (which nothing can address either: both
        # ``api_kirocrew_agent_update`` and ``api_kirocrew_agent_delete`` 404 on a
        # name absent from ``cfg.agents``, and a scanned project agent never is).
        # A GLOBAL row's name survives for a non-app caller because it is that
        # row's only handle -- it addresses ``/api/agents/{name}`` for edit and
        # delete and keys the usage sort -- and masking it there would buy
        # nothing: the same names are readable unmasked from
        # ``GET /api/config/kirocrew``, where they are the ``agents`` map's KEYS
        # and ``_masked_config_dict`` masks only schema-``sensitive`` VALUES.
        # That last argument does NOT extend to project rows, whose names come
        # from a filesystem scan and appear in no config, which is why they are
        # masked here rather than reasoned away.
        #
        # Named cost: a project agent whose FILENAME is credential- or
        # URL-shaped is not selectable, because the picker dispatches by
        # this value (``AgentSelector.tsx:127`` ``onChange(a.name)``). That is
        # confined to names the redactors would alter; an ordinary project agent
        # name is byte-identical.
        "name": _roster_mask(name) if (redact or scope == "project") else name,
        # The project-scope tag: "project" rows dispatch only from the
        # slot whose project they were scanned from. Handler-added, not a
        # record field.
        "scope": scope,
        "kiro_agent": _roster_mask(agent_cfg.kiro_agent),
        "workspace": _roster_mask(agent_cfg.workspace),
        "memory_store": _roster_mask(agent_cfg.memory_store),
        "model": _roster_mask(agent_cfg.model),
        "reasoning_effort": _roster_mask(agent_cfg.reasoning_effort),
        # Presentation label only — masked like every other user-authored string.
        # The picker and roster render it in place of ``name`` when non-empty;
        # ``name`` above stays the row's identity and dispatch handle.
        "display_name": _roster_mask(agent_cfg.display_name),
        "description": _roster_mask(agent_cfg.description),
        "triggers": _roster_mask(agent_cfg.triggers),
        "source": _roster_mask(agent_cfg.source),
        "session_color": _roster_mask(agent_cfg.session_color),
        # The one STRUCTURED value a row carries -- shape-allowlisted by
        # ``_safe_avatar`` with masking confined to user-authored ``traits``
        # values, so ``kind``/``v``/``file`` survive as the pinned shapes the
        # dashboard and the per-crew avatar endpoint need. See ``_roster_avatar``.
        "avatar": _roster_avatar(getattr(agent_cfg, "avatar", {})),
    }


def _resolve_roster_project_path(raw: str) -> tuple[str, bool]:
    """Resolve+validate a ``?project_path=`` query value for roster scoping.

    Returns ``(resolved_path, denied)``:

    * ``denied=True`` — the spelling names an untrusted UNC share, or the
      realpath'd target is sensitive (a protected tree such as a credential
      home). ``resolved_path`` is ``""``; the CALLER logs the SEL denial,
      because this runs in a worker thread and must not touch ``sel()`` there.
    * ``resolved_path=""`` with ``denied=False`` — the path is neither sensitive
      nor an existing directory (a half-typed draft, a typo). The caller ships
      GLOBAL rows only rather than scanning a non-directory or falling back to
      a different project's roster.
    * ``resolved_path=<dir>`` — a safe, existing directory to scan.

    Blocking (realpath + stat), so callers run it off the event loop. The UNC
    gate runs before resolving the raw spelling because resolving an untrusted
    UNC host is itself an outbound SMB probe; it runs again on the resolved
    spelling before any sensitivity or directory probe. The
    ``is_sensitive_path`` check is on the RESOLVED root so a symlink into a
    protected tree cannot slip past. ``~`` is expanded first, because the folder
    project-dir field accepts ``~/repo``-style paths (as the chat project picker
    does) and ``realpath`` does NOT expand a leading ``~`` — without this an
    existing ``~/repo`` resolves to a bogus literal-tilde path, fails the isdir
    check, and the folder's project agents silently never appear. Deliberately
    no ``~``/absolute-path bar beyond expansion: a read-only roster scan of an
    existing directory is not held to the cron create-time write bar, and the
    sensitivity gate is the real protection.

    This resolver validates only the ROOT it is handed. The value actually
    scanned is the ``.kiro``/``.kiro/agents`` SUBDIR, pinned and
    sensitivity-checked by the scan machinery that enumerates it
    (:func:`agent_discovery._pinned_scan_dir_fd`, reached through
    ``project_agent_names``/``project_agent_files``) at the moment it opens
    them — not re-validated or held here. That machinery is what refuses a
    redirecting ancestor or a resolved-sensitive target, and what raises
    :class:`kiro_crew.agent_discovery.ScanUnverifiable` when it cannot pin a
    directory descriptor. Its caller, ``api_kirocrew_agents`` in
    ``handlers.agents``, always asks for that signal
    (``raise_unverifiable=True``) for an owner-supplied path, on every
    platform: a directory an HTTP query named is not one the caller already
    trusts, so a platform that cannot pin it answers 503 rather than walking
    it by name, where a swapped component could redirect the read. Folder-level
    project-agent selection is therefore unavailable on a platform that cannot
    pin a directory descriptor — a stated platform limitation, not a failure.
    That handler's slot-derived fallback is a different caller with no UI state for
    "could not be checked"; it keeps the by-name fallback unconditionally and
    is unaffected by this.
    """
    if _unc_refused(raw):
        return "", True
    try:
        expanded = os.path.expanduser(raw)
    except (OSError, ValueError):
        return "", False
    if _link_chain_refused(expanded):
        return "", True
    try:
        resolved = os.path.realpath(expanded)
    except (OSError, ValueError):
        return "", False
    if _unc_refused(resolved):
        return "", True
    if is_sensitive_path(resolved):
        return "", True
    if not os.path.isdir(resolved):
        return "", False
    return resolved, False
