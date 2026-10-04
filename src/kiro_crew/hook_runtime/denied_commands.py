"""The denied-command tier's config resolution: the keystone opt-out state, the
governance force-pin, and the effective regex set and operator notes the gate
passes to ``PolicyAuthority.is_denied``.

Composed onto ``kiro_crew.hooks``; see :mod:`kiro_crew.hook_runtime`.
"""

from __future__ import annotations

import json
from dataclasses import replace as dataclasses_replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.hooks import (
        HooksConfig,
        logger,
        security,
    )


def _governance_pinned_command_ids(ctx: object) -> set[str]:
    """Return built-in command rule ids force-pinned by the active governance ceiling.

    Reads the boot-frozen ceiling (``ctx.governance``) ``commands``-scope deny
    patterns and maps the ones that pin a built-in rule to that rule's id, so
    ``_effective_denied`` can force-re-enable them even when the user opted out
    (tightest-wins). Returns ``set()`` on a standalone/ungoverned host.

    Fail-soft, mirroring ``_governance_denial``: a ``PlatformCompositionError``
    (a non-standalone host that could not compose) propagates fail-closed; any
    other error degrades to an empty set so a transient governance glitch cannot
    wedge every tool call out of ``_effective_denied``. The enterprise force-pin
    is also independently enforced by ``_governance_denial``'s commands-scope
    deny plane, so pins here are belt-and-suspenders.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        return security.pinned_builtin_command_ids()
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug("governance pin resolution failed", exc_info=True)
        return set()


def load_denied_commands_state() -> dict:
    """Read the keystone ``denied_commands.json`` opt-out state (fail-soft to {}).

    The opt-out state (``{disable_all, disabled_ids, user_added}``) lives in a
    keystone trust-root file the agent cannot write, NOT in ``config.json``.
    Returns ``{}`` (= no opt-out, all built-ins enforced) if the file is absent,
    unreadable, or not a JSON object — fail-safe for a deny gate.
    """
    try:
        from kiro_crew.config.loader import denied_commands_path

        raw = json.loads(denied_commands_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.debug("denied_commands.json load failed; treating as no opt-out", exc_info=True)
        return {}


def hooks_config_from_config_dict(hooks_section: dict) -> HooksConfig:
    """Build a ``HooksConfig`` for the gateway boot path.

    Parses the config.json ``hooks`` section for the flat hook keys, then
    OVERLAYS the denied-command opt-out state from the keystone
    ``denied_commands.json`` file (config.json's ``hooks.denied_commands`` is
    ignored — the keystone file is the sole source, so an agent that edits
    config.json cannot affect the deny ceiling).
    """
    merged = dict(hooks_section) if isinstance(hooks_section, dict) else {}
    merged["denied_commands"] = load_denied_commands_state()
    return HooksConfig.from_dict(merged)


def splice_denied_commands(base: HooksConfig, denied_state: dict | None = None) -> HooksConfig:
    """Return *base* with only its denied-command opt-out fields taken from the keystone.

    The deny ceiling and the flat hook keys come from different files -- the
    agent-unwritable ``denied_commands.json`` and operator-editable
    ``config.json`` -- so whichever one changed, the other's contribution must
    survive. Both live-reload paths route through here: a Settings>Security write
    splices fresh keystone state onto the running config, and a ``config.json``
    hooks reload splices the CURRENT keystone state onto the freshly parsed flat
    keys. Without it, one write silently reverts the other half.

    *denied_state* defaults to reading the keystone.
    """
    state = load_denied_commands_state() if denied_state is None else denied_state
    parsed = HooksConfig.from_dict({"denied_commands": state})
    return dataclasses_replace(
        base,
        denied_commands_disabled_ids=parsed.denied_commands_disabled_ids,
        denied_commands_disable_all=parsed.denied_commands_disable_all,
        denied_commands_user_added=parsed.denied_commands_user_added,
    )


def resolve_effective_denied_regexes(
    config: "HooksConfig", ctx: object = None, *, include_governance_pins: bool = True
) -> list[str]:
    """Effective regex-tier denied set from a HooksConfig (module-level).

    Same resolution as ``HookManager._effective_denied`` but usable by callers
    that hold a config rather than a HookManager (e.g. cron command vetting in
    ``mcp_cron``). Honors the user opt-out (disable_all / disabled_ids /
    user_added) with governance pins force-re-added (tightest-wins).

    ``include_governance_pins=False`` resolves the set the USER's own opt-out
    state would produce on its own. Enforcement must never use it — dropping
    pins is exactly the opt-out a pin exists to refuse. It answers a different
    question: comparing a deny against both sets tells a caller whether the
    match came ONLY from a pin, i.e. whether the block is policy state (which a
    later loosening reverses) or a rule the user is enforcing themselves.
    """
    return security.compute_effective_denied(
        list(security.BUILTIN_DENIED_RULES) + security.edition_denied_rules(),
        config.denied_commands_disabled_ids,
        config.denied_commands_disable_all,
        [p.pattern for p in config.denied_commands_user_added if p.enabled],
        _governance_pinned_command_ids(ctx) if include_governance_pins else (),
    )


def resolve_denied_notes(config: "HooksConfig") -> dict[str, str]:
    """Map each annotated, enabled user pattern to its operator note.

    The note is what the refusal shows INSTEAD of leaving the agent to infer
    intent from a raw regex — e.g. "use --maxdepth, or rg/fd" rather than a
    40-character character-class soup. Keyed by pattern because that is the only
    identity the matcher carries into ``security.is_denied``; ids are not
    threaded through the regex tier.

    Only enabled rules with a non-blank note appear. Built-in rules are absent
    on purpose: their ``description`` is catalog documentation aimed at the
    Settings reader, not remediation aimed at the caller, so promoting it into
    every refusal would change the text of rules the operator never annotated.

    A note containing :data:`security.DENY_REASON_MATCH_PREFIX` is DROPPED. The
    note is emitted on its own line, and ``RecoveryCard.tsx`` parses refusals with
    a GLOBAL per-line regex, so such a note would be read as a second, fabricated
    deny pattern. The guard uses the COLON-terminated form, not the emitted prefix:
    the regex treats the space after the colon as optional, so
    ``"Blocked by security policy:forged"`` parses as a refusal line without
    containing the emitted prefix. The add endpoint rejects this at write time;
    this guard is the one that holds for a keystone file the operator edited by
    hand. Fail-safe direction: lose the note, keep the pattern.
    """
    return {
        p.pattern: p.note.strip()
        for p in config.denied_commands_user_added
        if p.enabled
        and p.pattern
        and p.note.strip()
        and security.DENY_REASON_MATCH_PREFIX not in p.note
    }


def effective_denied_regexes_from_config() -> list[str]:
    """Resolve the effective denied set from on-disk state.

    Convenience for surfaces with neither a HookManager nor a parsed config in
    hand (cron vetting). The denied-command opt-out state comes from the keystone
    ``denied_commands.json`` (NOT config.json). Fail-soft: on any load error,
    falls back to the full built-in set (fail-closed — safer for a deny gate) so
    a glitch can never silently drop enforcement.
    """
    try:
        cfg = HooksConfig.from_dict({"denied_commands": load_denied_commands_state()})
        return resolve_effective_denied_regexes(cfg)
    except Exception:
        logger.debug("effective denied-set load failed; failing closed", exc_info=True)
        return security.compute_effective_denied(security.BUILTIN_DENIED_RULES, (), False, (), ())
