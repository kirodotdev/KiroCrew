"""The built-in denied-command catalog, its linear matcher and the evaluator seam.

The catalog is the configurable tier: every row is DEFAULT-ON, user-disableable
from the security settings surface, and force-pinnable by a governance policy.
That makes it the opposite of the keystone in ``paths`` -- a row here can be
turned off, so nothing that must always hold may live here alone.

Three things in this module are load bearing beyond their own tier.

The floor id sets and pattern tables are DERIVED from catalog categories rather
than hand-maintained, so a new row in a floor-enforced category is covered
automatically and the pattern set and id set cannot drift apart. They are read
by the argv-structural floor, which owns no catalog knowledge of its own.

The matcher exists because the rule patterns were authored for a linear-time
(RE2-style) engine and are not safe to hand to a backtracking engine verbatim.
It is an EVALUATION-layer rewrite only: the catalog rows, and the golden fixture
the parity test pins to, stay byte-for-byte what they were, and a refusal still
reports the original pattern.

The refusal producer is the single place refusal text is formed. Its first line
is frozen because two consumers parse it, one of them with a per-line
end-anchored regex; an operator note therefore goes on a second line, which both
consumers ignore.

The catalog and the tables derived from it live in owner modules beside this
one, and this module is the import path of every name they define:
``denied_rules_catalog`` holds the row type and the built-in rows, and
``denied_rules_tables`` the lookups by id and by pattern, the floor rule id
sets with their notes, and the flat pattern list. The pin resolvers, the
effective-list resolver, the exception surface, the matcher, the refusal
producer and the environment tier stay here. A read through this module
answers with the owner's object, and a write reaches every owner that binds
the name (:mod:`kiro_crew._facade_mirror`).
"""

from __future__ import annotations

import fnmatch
import importlib as _importlib
import logging
import re
import sys as _sys
from typing import TYPE_CHECKING

from .denied_rules_catalog import BUILTIN_DENIED_RULES, DeniedCommandRule
from .denied_rules_tables import (  # noqa: F401
    _GIT_PUBLISH_FLOOR_BY_ID,
    _GIT_PUBLISH_FLOOR_NOTES,
    _GIT_PUBLISH_RULE_PATTERNS,
    _GIT_PUBLISH_UNGATED,
    _INTERPRETER_RULE_PATTERNS,
    _LITERAL_CONCAT_RE,
    _RULE_ID_BY_PATTERN,
    _RULES_BY_ID,
    _SELF_PROTECTION_FLOOR_BY_ID,
    _SELF_PROTECTION_FLOOR_NOTES,
    _SELF_PROTECTION_FLOOR_PATTERNS,
    BUILTIN_DENY_PATTERNS,
)
from .diagnostics import RefusalDiagnostic, annotate_refusal, refusal_diagnostic
from .shell_normalizer import _SHELL_ACTIVE_CHARS

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)


# ── Governance pins ("commands"-scope ceiling and profile pins) ──
# Prior spellings of rules whose patterns were later rewritten.  A governance
# policy persists the pattern STRING it pinned, and the pin resolvers treat a
# pattern as pinning a built-in rule only when it maps back to a rule id — so a
# ceiling or profile written against an older catalog must keep resolving to
# the rule id after an upgrade (upgrade monotonicity), or a stale pin falls out
# of the id map and a user opt-out drops a rule the administrator pinned.
# LOOKUP-ONLY: consulted by :func:`_rule_id_for_pattern` (the pin resolvers),
# never merged into ``_RULE_ID_BY_PATTERN`` — the legacy spellings must not
# count as built-ins for ``_DenyMatcher``'s fast-path election or SEL
# enrichment, and they never enter ``BUILTIN_DENY_PATTERNS`` or the golden
# manifest.
#
# Only a row that still EXISTS may have an alias here.  The pre-widening
# spellings of the ``restart`` / ``update`` / ``cloud`` / ``gateway restart``
# rows are deliberately absent: those rows do not exist, their enforcement is
# the ungated argv floor (``_SELF_PROTECTION_UNGATED_FLOOR_IDS``), which no
# opt-out can reach, so there is nothing left for such a pin to force back on.
# A persisted pin in either of those spellings resolves to ``None`` and is
# reported by ``_resolved_pin_ids`` as pinning nothing -- which is the truth,
# and preferable to resolving it onto an id the catalog cannot display or
# toggle.
_LEGACY_RULE_ID_BY_PATTERN: dict[str, str] = {
    # The bare-substring spelling of ``reverse-shell-nc`` that a policy written
    # against an older catalog pins; the row itself is anchored to the command
    # token (``_NETCAT_EXEC_PATTERN``) and stays force-pinnable under that pin.
    "nc -e.*": "reverse-shell-nc",
}


def _rule_id_for_pattern(pattern: str) -> "str | None":
    """Resolve a governance-pinned pattern string to a built-in rule id.

    Current catalog spellings first, then the legacy (pre-widening) spellings,
    so a persisted policy keeps its pin across a pattern change.
    """
    return _RULE_ID_BY_PATTERN.get(pattern) or _LEGACY_RULE_ID_BY_PATTERN.get(pattern)


def _resolved_pin_ids(pins: "Iterable[str]", component: str) -> set[str]:
    """Rule ids for *pins*, naming every pin that resolves to none.

    A pin that maps to no rule id used to leave in a set comprehension's filter,
    which is the same defect a refusal naming no rule has, one layer up: an
    administrator writes a ceiling, the pin silently pins nothing, and the policy
    still reads as valid everywhere it is displayed. So an unresolvable pin is
    reported -- by SHAPE, through the same diagnostic record a refusal carries,
    because a persisted pin is a PATTERN an operator authored and a log line
    quoting it would put a policy body into the log for every failed lookup.
    ``rule`` therefore names the failure, not the pin, and the shape is what tells
    an administrator which of their entries it was.

    Reporting only -- the resolved set is exactly what the comprehension produced,
    so no pin becomes more or less enforced by being named. Widening resolution
    itself (a trimmed form, an id-shaped pin resolved directly) is the governance
    cluster's own change and is deliberately not done here.
    """
    resolved: set[str] = set()
    for pin in pins:
        rule_id = _rule_id_for_pattern(pin)
        if rule_id is None:
            logger.warning(
                "governance pin resolves to no built-in rule and pins nothing; %s",
                refusal_diagnostic("governance-pin-unresolved", component, pin).as_line(),
            )
            continue
        resolved.add(rule_id)
    return resolved


def pinned_builtin_command_ids() -> set[str]:
    """Return built-in rule ids force-pinned by the ACTIVE governance ceiling.

    A governance ``commands``-scope deny policy can pin a built-in rule as
    un-opt-out-able.  A pattern is treated as pinning a built-in rule when it is
    string-identical to that rule's regex.

    Scope: the **active** Level-1 ceiling (``current_context().governance``)
    ONLY.  This is the ENFORCEMENT accessor (the hooks gate force-re-adds these
    ids so a user opt-out cannot weaken a ceiling pin, tightest-wins).  It does
    NOT union other profiles' pins — a rule pinned only for profile A must not be
    force-enforced for profile B or a no-profile session (that would break
    profile-scoped governance).  Per-profile command enforcement is handled
    separately by the gate's ``_governance_denial`` commands-scope deny plane,
    which resolves the *bound* profile.  For the surface-agnostic Settings
    snapshot (which must over-lock across all profiles) use
    :func:`pinned_builtin_command_ids_for_snapshot`.

    Fail-soft: returns an empty set on a standalone/ungoverned host or if
    governance resolution fails (mirrors the degrade discipline elsewhere in this
    module; ``PlatformCompositionError`` still propagates fail-closed).
    """
    from kiro_crew.platform import governance as _governance
    from kiro_crew.platform.context import PlatformCompositionError, current_context

    try:
        ceiling = current_context().governance
        if ceiling is None:
            return set()
        # ``resolve_pinned_commands`` is provided by the governance module (a
        # sibling change-set); resolve it dynamically so this module composes
        # regardless of build order.  Missing symbol → no pins (fail-soft).
        resolver = getattr(_governance, "resolve_pinned_commands", None)
        if resolver is None:
            return set()
        pins = resolver(ceiling)
        return _resolved_pin_ids(pins, "commands-ceiling-pin")
    except PlatformCompositionError:
        raise
    except Exception:
        return set()


def pinned_builtin_command_ids_for_snapshot() -> set[str]:
    """Built-in rule ids pinned by the ceiling OR by ANY loaded profile.

    DISPLAY accessor for the surface-agnostic Settings > Security snapshot, which
    has no session/agent/app to resolve a single *active* profile.  It unions the
    active ceiling pins (:func:`pinned_builtin_command_ids`) with the pins from
    ALL loaded profiles, so a rule pinned by ANY profile renders locked and is
    never presented as freely disableable — otherwise a profile-pinned rule would
    surface as a no-op opt-out (UI reports success, but the bound-profile gate
    still denies).  Conservative by design (over-locks, never under-locks).

    This is DISPLAY-only: the ENFORCEMENT gate uses the ctx-scoped
    :func:`pinned_builtin_command_ids` (active ceiling) + the bound-profile deny
    plane, so unioning all profiles here does NOT widen enforcement.

    Fail-soft like :func:`pinned_builtin_command_ids`.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        ids = pinned_builtin_command_ids()
    except PlatformCompositionError:
        raise
    except Exception:
        ids = set()
    try:
        from kiro_crew.platform.governance_profiles import all_profile_pinned_commands

        ids |= _resolved_pin_ids(all_profile_pinned_commands(), "profile-pin")
    except Exception:
        pass
    return ids


class EffectiveDenied(list[str]):
    """The effective regex-tier deny list, plus which entries are shipped.

    ``shipped`` holds the patterns present only because a shipped rule is enabled
    or pinned, never one the operator also authored. Pattern text is not
    provenance: an operator rule typed identical to a shipped one gets no
    shipped-only narrowing. A plain list records nothing, so it narrows nothing.
    """

    def __init__(self, patterns: "Iterable[str]", shipped: "Iterable[str]") -> None:
        super().__init__(patterns)
        self.shipped: frozenset[str] = frozenset(shipped)


def compute_effective_denied(
    rules: "list[DeniedCommandRule]",
    disabled_ids: "Iterable[str]",
    disable_all: bool,
    user_added: "Iterable[str]",
    governance_pins: "Iterable[str]",
) -> list[str]:
    """Resolve the effective regex-tier deny list (pure, deterministic).

    Returns the ordered, de-duplicated list of REGEX strings to enforce:

    1. For each rule in ``rules`` (input order), include ``rule.pattern`` if
       ``(not disable_all and rule.id not in disabled_ids) or rule.id in
       governance_pins``.  A governance pin re-adds a rule even when the user
       individually disabled it OR set disable-all — tightest-wins: an
       enterprise pin cannot be opted out.
    2. Append every entry of ``user_added`` verbatim (the user's own regexes).
    3. De-duplicate preserving first-seen order.

    The result is an :class:`EffectiveDenied` recording which entries came only
    from step 1, so a caller that passes it on passes their provenance too.

    No I/O, no config reads, no globals mutated — callers (the hooks gate) own
    where ``disabled_ids`` / ``disable_all`` / ``user_added`` / ``governance_pins``
    come from.
    """
    disabled = set(disabled_ids)
    pins = set(governance_pins)
    shipped: list[str] = []
    for rule in rules:
        if (not disable_all and rule.id not in disabled) or rule.id in pins:
            shipped.append(rule.pattern)
    authored = list(user_added)
    own = set(authored)
    return EffectiveDenied(dict.fromkeys(shipped + authored), (p for p in shipped if p not in own))


def enabled_rule_ids(denied_regexes: "list[str] | None") -> "frozenset[str] | None":
    """Resolve an effective REGEX list to the set of enabled built-in rule ids.

    The always-on gates (``audit_bash_exfiltration``, ``_check_imds_access``,
    ``is_sensitive_path_for_agent``) are keyed by rule id, while the hooks gate
    holds the effective set as patterns. This is the one translation, done once
    per tool call.

    ``None`` in, ``None`` out — and ``None`` means "all enabled" to every consumer,
    so the fail-closed default survives the round trip. A pattern with no catalog
    id (a user-added regex) contributes nothing, which is correct: those rules have
    no always-on branch to gate.
    """
    if denied_regexes is None:
        return None
    ids = {_RULE_ID_BY_PATTERN.get(p) for p in denied_regexes}
    return frozenset(rid for rid in ids if rid is not None)


def builtin_denied_rules() -> list[dict]:
    """Return the built-in rule catalog as plain dicts for API serialization.

    Each entry has exactly ``{id, pattern, category, description}``.  Handlers
    consume this so they never need to import the ``DeniedCommandRule`` dataclass.
    """
    return [
        {
            "id": r.id,
            "pattern": r.pattern,
            "category": r.category,
            "description": r.description,
        }
        for r in BUILTIN_DENIED_RULES
    ]


def edition_denied_rules() -> list[DeniedCommandRule]:
    """Denied-command rules contributed by the composed edition, validated.

    Reads ``current_context().denied_rules.denied_rules()`` (the
    ``DeniedRuleProvider`` seam) and returns only entries safe to union into the
    DISABLEABLE regex tier.  Unlike the ``SecurityOverlay`` floor these rules ARE
    user-disableable — the whole point of the seam — so they are resolved by
    ``compute_effective_denied`` exactly like a built-in and honour
    ``disabled_ids`` / ``disable_all``.

    Rejected (skipped with a warning, never raised):

    * a non-:class:`DeniedCommandRule` entry, or one with a blank ``id`` /
      ``pattern`` — there would be nothing to key an opt-out on;
    * an ``id`` colliding with a BUILT-IN rule id — ``disabled_ids`` is one flat
      set, so a collision would make one rule's toggle silently move the other.
      The built-in wins, mirroring the ADD-only de-dupe the skill-discovery seam
      uses for provider names;
    * a duplicate ``id`` within the edition's own list (first occurrence wins).

    Fail-soft: an ungoverned/standalone host, a provider that does not implement the
    protocol, or one that raises all yield ``[]`` — the built-in catalog stands on
    its own and the un-weakenable overlay floor is untouched either way, so
    degrading here loses only an additive rule.  ``PlatformCompositionError``
    still propagates fail-closed, as everywhere else in this module.
    """
    # Function-local by necessity, not style: importing ``kiro_crew.platform.context``
    # at module scope executes ``kiro_crew.platform.__init__``, which imports
    # ``platform.security_authority``, which imports THIS module — a genuine cycle.
    # The pre-existing local import in ``installed_context``'s caller below has the
    # same cause. (``top-level-imports``, documented exception.)
    from kiro_crew.platform.context import PlatformCompositionError, current_context

    try:
        ctx = current_context()
        contributed = list(ctx.denied_rules.denied_rules())
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug("edition denied_rules lookup failed; using built-ins only", exc_info=True)
        return []

    out: list[DeniedCommandRule] = []
    seen: set[str] = set()
    for rule in contributed:
        rid = getattr(rule, "id", None)
        pattern = getattr(rule, "pattern", None)
        if not isinstance(rid, str) or not rid.strip():
            logger.warning("edition denied rule with no id skipped")
            continue
        if not isinstance(pattern, str) or not pattern.strip():
            logger.warning("edition denied rule %s has no pattern; skipped", rid)
            continue
        if rid in _RULES_BY_ID:
            logger.warning(
                "edition denied rule %s collides with a built-in rule id; built-in wins", rid
            )
            continue
        if rid in seen:
            logger.warning("edition denied rule %s is a duplicate; first occurrence wins", rid)
            continue
        if not is_safe_user_regex(pattern):
            # The matcher DISABLES a malformed or ReDoS-prone pattern and only logs
            # (see ``_DeniedMatcher.__init__``). Publishing it anyway would put a
            # row in Settings → Security that reads enabled and toggles cleanly
            # while matching nothing — a control that looks present and is not,
            # which is the exact failure this seam exists to remove. Skip it so the
            # panel's enabled set and the matcher's cannot disagree.
            logger.warning(
                "edition denied rule %s has an unsafe or malformed pattern; skipped", rid
            )
            continue
        if not _matches_full_input(pattern):
            # The matcher would route this one to the length-bounded window (see
            # ``_DENY_FALLBACK_SCAN_MAX_CHARS``), so padding the command past the
            # cap defeats it. Same reasoning as the check above: a rule that scans
            # only a prefix is bypassable, and publishing it as enforcing would
            # make the panel claim a guarantee the matcher does not give. An
            # edition wanting this pattern rewrites it without a top-level ``.*``
            # (a bounded gap such as ``[^;&|\n]*`` keeps it one fragment), or uses
            # the un-weakenable overlay if it truly needs the loose form.
            logger.warning(
                "edition denied rule %s would only scan the first %d chars "
                "(top-level '.*' or an over-consuming gap); skipped",
                rid,
                _DENY_FALLBACK_SCAN_MAX_CHARS,
            )
            continue
        seen.add(rid)
        out.append(
            DeniedCommandRule(
                id=rid,
                pattern=pattern,
                category=str(getattr(rule, "category", "") or "edition"),
                description=str(getattr(rule, "description", "") or ""),
            )
        )
    return out


# Exceptions keyed by the deny pattern they apply to. If an input matches
# a deny pattern AND one of that pattern's exceptions, the deny is skipped.
# This avoids a blanket allowlist that could bypass unrelated deny rules.
# Exceptions are NOT applied when the input contains command separators
# (;, &&, ||, |, newlines) to prevent chaining bypasses.
#
# Scoped carve-out for INERT MENTIONS of a destructive literal.
#
# A read-only search verb cannot execute its own operands, so a destructive
# string handed to it as a pattern is text, not an action:
#
#     grep -rn "rm -rf /" tests/     <- searching FOR the rule, not running it
#
# Denying those identically to the real command prevents nothing (the same work
# completes by moving the payload into a file, which is not scanned) while
# blocking anyone working ON these rules, and surfaces to the agent as
# ``User denied tool execution`` -- indistinguishable from a human cancelling.
#
# Two conditions make this safe, and BOTH are load-bearing -- getting either one
# wrong re-allows a real wipe:
#
#   1. The glob must be ANCHORED AT THE VERB. ``fnmatch`` is a full match but
#      ``*`` crosses spaces, so a LEADING ``*`` is an unanchored substring
#      test: ``*/grep *`` matches ``rm -rf / /bin/grep x`` -- a genuine
#      root-wipe with a ``/grep `` fragment anywhere in its operand list --
#      and would exonerate it. Only the bare ``<verb> *`` form is safe, because
#      it forces the segment to BEGIN with the verb. Path-qualified
#      invocations (``/usr/bin/grep ...``) are therefore NOT exonerated: a
#      glob cannot express "the first token's basename is the verb", and
#      losing a denial is a worse outcome than a search that still needs
#      rewording.
#
#   2. The view must contain NO shell-active character at all, enforced by
#      :func:`_exception_eligible`. Blocklisting only the openers already
#      known is defeated by the next one -- a
#      bash 5.3 funsub, ``grep x ${ rm -rf /;}``, which the splitter cuts only
#      at the ``;`` so the destructive command stays glued to the search verb.
#      Enumerating opener SPELLINGS is the losing side of that game (the same
#      point applies to any spelling-based recognizer), so the guard is
#      inverted: an eligible view may contain none of ``$`` ``(`` ``)`` ``{``
#      ``}`` `` ` `` ``<`` ``>``. That covers command substitution, process
#      substitution, funsubs, subshells and redirection as a CLASS rather than
#      one opener at a time. Gating the EXCEPTION rather than widening
#      ``_CMD_SPLIT_RE`` keeps the blast radius to this carve-out; the splitter
#      feeds every other rule, which were measured against its behaviour.
#
# The verb list is confined to the ``grep`` family for the same reason. The
# premise of this whole carve-out is that the verb CANNOT execute its operands,
# and that is a property of the specific tool: ``rg --pre <cmd>`` runs a
# preprocessor and ``ack --pager <cmd>`` runs a pager, so
# ``rg --pre sh "rm -rf /tmp/victim" payload.sh`` really does execute. ``grep``
# / ``egrep`` / ``fgrep`` have no flag that spawns a helper, so for them the
# premise holds rather than merely being asserted.
#
# With all of this in force the chaining cases stay denied on the destructive
# SEGMENT in Pass 2 (``grep "x" && <destructive>``); the Pass 1 whole-string
# exception only ever defers to that pass.
#
# Because nothing in an eligible segment executes, whether the literal was
# quoted is irrelevant -- so this needs no quote awareness, which is what keeps
# it compatible with quote-NORMALIZED matching (quoting must never exculpate a
# command that does run).
#
# Deliberately NOT included, pending a maintainer decision:
# ``echo``/``printf`` (inert to execute, but ``>`` is not a segment
# separator, so an exoneration there also covers writing the string to a file)
# and ``git commit -m`` (the arguable non-search verb).
_INERT_SEARCH_VERBS = ("grep", "egrep", "fgrep")
#: Verb-anchored ONLY -- see condition 1 above. A leading ``*`` here would be a
#: bypass, not a convenience.
_INERT_SEARCH_GLOBS: list[str] = [f"{verb} *" for verb in _INERT_SEARCH_VERBS]


def _exception_eligible(view: str) -> bool:
    """Whether a deny-exception may be consulted for ``view`` at all.

    An eligible view must be a SINGLE PLAIN COMMAND: no character from
    :data:`_SHELL_ACTIVE_CHARS`, which covers command substitution, process
    substitution, funsubs, subshells, redirection AND chaining.

    Two independent reasons, each of which was a reachable bypass during review:

    * ``_CMD_SPLIT_RE`` is not a complete execution-boundary oracle -- it does
      not treat ``<(``, ``>(``, ``${`` or a bare ``(`` as a boundary -- so such a
      construct stays glued INSIDE a segment instead of being isolated into its
      own command position, and an exception keyed to the segment's leading verb
      would exonerate the command hiding in it (``grep x <(rm -rf /tmp/v)``,
      ``grep x ${ rm -rf /;}``).
    * A pipeline's later stage can EXECUTE what the search emitted
      (``grep '<destructive>' payload.py | python``).  The pipe is a splitter
      boundary, so the Pass 2 grep segment looks innocent on its own; refusing
      the separators here keeps the Pass 1 whole-string match denying outright
      instead of deferring to that segment.

    Deliberately a character-class test rather than a list of opener spellings:
    the spelling list lost twice during review, once to ``<(`` and once to a
    bash 5.3 funsub.

    Fails CLOSED: an unrecognised construct means no exception, i.e. the deny
    stands.  This gates only the exception path, so no other rule's matching
    behaviour changes.
    """
    return not _SHELL_ACTIVE_CHARS.intersection(view)


#: kiro-cli's display prefix on a shell call's title, lowercased like the views.
_SHELL_TITLE_PREFIX = "running: "


def _exception_applies(view: str, exceptions: list[str]) -> bool:
    """Whether one of *exceptions* exonerates the lowercased *view*.

    The view must be eligible (:func:`_exception_eligible`), and the globs are
    matched against the command behind ONE exact leading ``running: ``. kiro-cli
    titles a shell call ``Running: <command>`` and the deny tiers judge that
    title as a subject of its own, so a verb-anchored glob would otherwise never
    match it and the title would be refused for a command that is exonerated.
    Reading through the prefix keeps condition 1 above intact: the glob still
    has to start at the command's first word, and the title gets the verdict of
    the command it displays, never a weaker one.
    """
    if not exceptions or not _exception_eligible(view):
        return False
    if view.startswith(_SHELL_TITLE_PREFIX):
        view = view[len(_SHELL_TITLE_PREFIX) :]
    return any(fnmatch.fnmatch(view, e.lower()) for e in exceptions)


# Maps a deny pattern to the globs that exonerate it.  When an input matches
# a deny pattern AND one of that pattern's exceptions, the deny is skipped.
# This avoids a blanket allowlist that could bypass unrelated deny rules.
#
# Scoped to the two ``local-destructive`` rm rules: they are plain literal
# strings, so they are the ones an ordinary search for their own subject
# matter trips over.
_DENY_EXCEPTIONS: dict[str, list[str]] = {
    "rm -rf /.*": list(_INERT_SEARCH_GLOBS),
    "rm -rf ~.*": list(_INERT_SEARCH_GLOBS),
}


# ── Permission-verb rules that an INERT MENTION may narrow (argv-structural) ──
#
# The rules below are the ``chmod``/``chown`` rows -- most naming a system
# path, one naming the world-writable mode.  They are authored as
# ``<verb>.*<target>.*`` and evaluated with ``re.search`` over
# the WHOLE command text, which cannot tell a verb in PROGRAM position from the
# same word handed to a search tool as a pattern.  So an ordinary audit of these
# very rules is refused:
#
#     git show origin/main:src/x.py | grep -nE 'chmod|chown|/etc/' | head -50
#     grep -rnE 'os\.chown|/etc/cron' src/ 2>/dev/null | head -60
#     grep -n 'chmod 777' src/kiro_crew/security/denied_rules.py
#
# Neither changes a permission.  Denying them prevents nothing (the same search
# completes by spelling the verb some other way) and surfaces to the agent as
# ``User denied tool execution``, indistinguishable from a human cancelling.
#
# ``_DENY_EXCEPTIONS`` cannot reach these.  It is a TEXT glob gated by
# :func:`_exception_eligible`, which requires the view to hold no shell-active
# character at all -- and both commands above carry ``|`` or ``>``.  Widening
# that glob table is the wrong instrument twice over: a glob cannot express
# "this word sits at an argument position", and relaxing
# ``_exception_eligible`` would relax it for the ``rm`` carve-out too.
#
# The narrowing therefore lives at the ARGV layer
# (:func:`~.perm_verb_mention._perm_verb_mention_only`) and is consulted ONLY
# for the patterns named here, so no other rule's behaviour can change.
#
# Derived, never hand-listed: the catalog is the single source of truth for
# which rows exist, and a hand-maintained copy of those regex literals would
# silently stop covering a row that is renamed or added.  The selector is
# "``local-destructive`` row whose pattern BEGINS with a permission verb".
#
# It is deliberately blind to what the row matches AFTER the verb.  An earlier
# revision of this selector required a rooted path, which excluded the mode row
# and left an ordinary search for that very rule denied.  The mode row's own
# pattern is also under revision to admit flag spellings, so a selector keyed on
# the pattern's TAIL would drop the row on that rebase with no test noticing.
# The verb anchor is the one part a row cannot change and still be the same rule.
_PERM_VERB_RULE_RE = re.compile(r"^(ch(?:mod|own|grp))\b")

_PERM_VERB_MENTION_RULES: tuple[DeniedCommandRule, ...] = tuple(
    rule
    for rule in BUILTIN_DENIED_RULES
    if rule.category == "local-destructive" and _PERM_VERB_RULE_RE.match(rule.pattern)
)

#: Patterns whose deny an inert mention may narrow.  Membership is checked by
#: ``is_denied`` before the argv predicate is consulted at all.
_PERM_VERB_MENTION_PATTERNS: frozenset[str] = frozenset(
    rule.pattern for rule in _PERM_VERB_MENTION_RULES
)

#: The verbs those rules are anchored on, taken from the same match.  The argv
#: predicate looks for exactly these words, so a catalog row for a new verb
#: brings its own vocabulary with it.
_PERM_VERB_MENTION_VERBS: frozenset[str] = frozenset(
    match.group(1)
    for match in (_PERM_VERB_RULE_RE.match(rule.pattern) for rule in _PERM_VERB_MENTION_RULES)
    if match is not None
)


# ── ReDoS mitigation for the regex deny tier ──
# The 137 built-in rule patterns were authored for kiro-cli's linear-time
# (RE2-style) engine.  Under Python's backtracking ``re`` two independent
# pathologies appear on hostile input, so the raw patterns must never be fed to
# ``re.search`` verbatim.  ``_DenyMatcher`` compiles each pattern into a
# behaviourally-identical but linear-time matcher and matches against the FULL
# (untruncated) string, so a destructive needle at any offset is always found.
# All of this is EVALUATION-LAYER only — ``BUILTIN_DENIED_RULES`` (and the
# golden fixture the parity test pins to) stay byte-for-byte unchanged, and the
# human-readable denial reason / SEL audit still report the ORIGINAL pattern.
#
# Pathology 1 — catastrophic (exponential) backtracking.
#   The 46 ``aws-*`` patterns embed the nested-star flag run
#   ``(?:\s+--?[a-z-]+(?:[= ]\S+)?)*``.  Two internal ambiguities make it
#   exponential: (a) ``--?`` and ``[a-z-]+`` can both claim the leading dashes
#   of a flag; (b) a space-separated value ``[= ]\S+`` can equally be read as
#   the next flag.  On input like ``aws -x -x -x …`` (only ~40 repeats / ~124
#   chars) the engine explores 2ⁿ parses before failing — a length bound does
#   NOT help because the blow-up happens well below any sane bound.  We rewrite
#   the run to ``_LINEARIZED_AWS_FLAG_RUN`` which removes BOTH ambiguities
#   (``--?``→``-`` for the flag name, and a negative lookahead so a space value
#   cannot itself be a flag token).  This is provably language-equivalent — see
#   ``test_denied_commands_security`` ReDoS tests and the exhaustive
#   brute-force/directional equivalence checks documented there.
#
# Pathology 2 — polynomial (O(n²)/O(n³)) backtracking.
#   Every ``.*``-prefixed pattern (~50 of them) and the multi-``.*`` chains
#   (e.g. ``python.*open.*/\.ssh/``) are linear/polynomial per pattern but scan
#   the whole string, so across ~123 effective patterns a 20k-char input costs
#   seconds.  These ``.*`` occur ONLY at the TOP LEVEL of the ported patterns
#   (none has a top-level alternation or a top-level ``.+``), so we SPLIT each
#   pattern on its top-level ``.*`` into fixed fragments and existence-match
#   them in order with a monotonically-advancing ``re.search(text, pos)``.  A
#   top-level ``.*`` matches "anything", so "fragment₀ then fragment₁ then …"
#   at leftmost advancing positions is exactly equivalent to the whole regex —
#   verified by exhaustive brute-force + a 40k-input equivalence harness — but
#   runs in O(n) with NO backtracking across the gaps and NO length bound, so a
#   padded needle inside a single un-separated segment (the bypass this fixes)
#   is still caught.  A pattern that is NOT safe to split this way (a top-level
#   alternation, only possible via a user-supplied custom regex — no built-in
#   has one) falls back to a length-bounded ``re.search`` on the linearized
#   form (``_DENY_FALLBACK_SCAN_MAX_CHARS``): correct for short commands and
#   ReDoS-safe, at the cost of not scanning a needle past the bound in such an
#   exotic custom pattern (built-ins are unaffected).
_DENY_FALLBACK_SCAN_MAX_CHARS = 2000

# The dangerous nested-star flag run as it appears (raw) in the aws-* patterns.
_DANGEROUS_AWS_FLAG_RUN = r"(?:\s+--?[a-z-]+(?:[= ]\S+)?)*"
# Linear, language-equivalent replacement (see Pathology 1 above).
_LINEARIZED_AWS_FLAG_RUN = r"(?:\s+-[a-z-]+(?:=\S+| (?!-[a-z-]+(?:[= ]|$))\S+)?)*"


def _linearize_deny_pattern(pattern: str) -> str:
    """Rewrite the exponential aws flag-run into its linear-time equivalent.

    Pure / idempotent.  Only touches Pathology 1 (the nested-star flag run);
    the top-level ``.*`` gaps are handled structurally by ``_split_deny_frags``.
    """
    return pattern.replace(_DANGEROUS_AWS_FLAG_RUN, _LINEARIZED_AWS_FLAG_RUN)


# ── ReDoS-safety gate for USER-supplied deny regexes ──
# The 137 built-in patterns are ReDoS-safe by construction (the one dangerous
# construct — the aws flag run — is rewritten by ``_linearize_deny_pattern``,
# and the git-publish patterns never reach the regex tier).  But a USER can add
# an ARBITRARY regex via ``POST /api/security/denied-commands/user``; a
# catastrophic-backtracking pattern such as ``(a+)+$`` would then run inside the
# synchronous PreToolUse gate on the event loop and could freeze the gateway
# (2ⁿ backtracking is NOT bounded by scanning a length-limited prefix — the
# blow-up happens far below any byte bound).  ``is_safe_user_regex`` is a
# conservative, stdlib-only STRUCTURAL check used both at the add boundary
# (reject with HTTP 400) and as runtime defense-in-depth in ``_DenyMatcher``
# (an already-stored unsafe pattern is skipped, never executed).
#
# Heuristic (the classic exponential family): a pattern is UNSAFE if it contains
# a QUANTIFIED GROUP — one whose quantifier permits >1 repetitions (``*``,
# ``+``, ``{m,}``, ``{m,n}`` with n>1, ``{n}`` with n>1) — whose body itself
# contains EITHER (a) another quantifier (nested quantifier: ``(X+)+``,
# ``(X*)*``, ``(X?)*`` …) OR (b) a top-level alternation (branch-overlap risk:
# ``(a|a)+``, ``(ab|a)+``).  We deliberately err toward REJECTING a suspicious
# user pattern: built-ins are unaffected (they are added programmatically, never
# through this gate), and a user who hits a false positive can rephrase without
# the nested quantifier.  We first strip the known-safe linearized aws flag run
# so the (harmless) built-in construct is never mistaken for the dangerous
# signature if this ever runs over the effective set.


def _redos_prone(pattern: str) -> bool:
    """Structural exponential-ReDoS heuristic (see the section comment above).

    Robust to malformed / unbalanced input — never raises; returns ``False`` for
    a structure it cannot reason about (``re.compile`` is validated separately by
    callers, and the runtime fallback is length-bounded regardless).
    """
    n = len(pattern)
    i = 0
    # One frame per open group; base frame is the whole pattern.
    stack: list[dict] = [{"has_inner_quant": False, "has_alt": False}]

    def read_quantifier(idx: int) -> "tuple[str | None, int]":
        """Return (kind, new_idx): kind is ``"multi"`` (>1 repetitions possible),
        ``"opt"`` (``?`` or ``{0,1}``), or ``None`` (no quantifier at ``idx``)."""
        if idx >= n:
            return None, idx
        ch = pattern[idx]
        if ch in "*+":
            j = idx + 1
            if j < n and pattern[j] in "?+":  # lazy / possessive-style modifier
                j += 1
            return "multi", j
        if ch == "?":
            j = idx + 1
            if j < n and pattern[j] in "?+":
                j += 1
            return "opt", j
        if ch == "{":
            k = idx + 1
            body: list[str] = []
            while k < n and pattern[k] != "}":
                body.append(pattern[k])
                k += 1
            if k >= n:  # unterminated ``{`` — treat as a literal, no quantifier
                return None, idx + 1
            k += 1  # consume ``}``
            spec = "".join(body)
            if "," in spec:
                _, _, hi = spec.partition(",")
                hi = hi.strip()
                multi = hi == "" or not hi.isdigit() or int(hi) > 1
            else:
                multi = not spec.isdigit() or int(spec) > 1
            return ("multi" if multi else "opt"), k
        return None, idx

    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
            kind, i = read_quantifier(i)
            if kind is not None:
                stack[-1]["has_inner_quant"] = True
            continue
        if c == "[":
            i += 1
            if i < n and pattern[i] == "^":
                i += 1
            if i < n and pattern[i] == "]":
                i += 1
            while i < n and pattern[i] != "]":
                if pattern[i] == "\\":
                    i += 1
                i += 1
            i += 1  # consume ``]``
            kind, i = read_quantifier(i)
            if kind is not None:
                stack[-1]["has_inner_quant"] = True
            continue
        if c == "(":
            i += 1
            if i < n and pattern[i] == "?":
                nxt = pattern[i + 1] if i + 1 < n else ""
                if nxt in "=!":  # (?= (?! lookahead — a normal group frame
                    i += 2
                elif nxt == "<" and i + 2 < n and pattern[i + 2] in "=!":
                    i += 3  # (?<= (?<! lookbehind
                else:
                    # (?: , (?i: , (?P<name> … — skip the prefix up to ``:``/``>``
                    j = i + 1
                    while j < n and pattern[j] not in ":>)":
                        j += 1
                    i = j + 1 if j < n and pattern[j] in ":>" else j
            stack.append({"has_inner_quant": False, "has_alt": False})
            continue
        if c == ")":
            grp = stack.pop() if len(stack) > 1 else {"has_inner_quant": False, "has_alt": False}
            i += 1
            kind, i = read_quantifier(i)
            if kind == "multi" and (grp["has_inner_quant"] or grp["has_alt"]):
                return True
            if kind is not None:
                # A quantified group is itself a quantifier in the parent frame.
                stack[-1]["has_inner_quant"] = True
            continue
        if c == "|":
            stack[-1]["has_alt"] = True
            i += 1
            continue
        if c in "*+?{":
            kind, i = read_quantifier(i)
            if kind is not None:
                stack[-1]["has_inner_quant"] = True
            continue
        i += 1
    return False


def is_safe_user_regex(pattern: str) -> bool:
    """Return ``True`` if a USER-supplied deny regex is safe to run on the gate.

    A pattern is safe when it (a) compiles and (b) is NOT flagged by the
    structural exponential-ReDoS heuristic (``_redos_prone``).  Callers — the
    dashboard ``POST /denied-commands/user`` handler and ``_DenyMatcher`` —
    reject/skip a pattern that fails this check so a catastrophic user regex can
    never freeze the synchronous PreToolUse gate.

    The known-safe aws flag runs are stripped before the structural check only
    when the complete pattern is a built-in.  A user pattern wrapping the same
    fragment receives no exemption.

    A pattern with a TOP-LEVEL alternation (``a|b``) is also rejected: it cannot
    be split on ``.*`` for the linear full-length fragment matcher, so it would
    fall back to a length-bounded whole-string scan — which a padded command
    (a needle beyond the bound in one segment) could slip past. No built-in rule
    has top-level alternation; a user can express the same intent as separate
    rules, so rejecting it here closes the truncation-bypass with no coverage
    loss.
    """
    try:
        re.compile(pattern)
    except re.error:
        return False
    scrubbed = pattern
    if pattern in BUILTIN_DENY_PATTERNS:
        scrubbed = pattern.replace(_DANGEROUS_AWS_FLAG_RUN, "").replace(
            _LINEARIZED_AWS_FLAG_RUN, ""
        )
    if _redos_prone(scrubbed):
        return False
    return not _has_top_level_alternation(scrubbed)


def _polynomial_backtracking_prone(pattern: str) -> bool:
    """True if ``pattern`` has two ADJACENT quantified units at one nesting level.

    ``_redos_prone`` catches the EXPONENTIAL family (a quantified group whose
    body quantifies or alternates). This catches the POLYNOMIAL one it lets
    through — ``a+a+$``, ``(a+)(a+)$``, ``\\w+\\d+$``, ``.*.*!`` — where the engine
    redistributes one input run across two greedy units, O(n) ways per start
    position.

    That family is harmless on a length-capped window and NOT harmless without
    one: measured on CPython, ``a+a+$`` against 2,000 ``a``s takes ~3.5s, 4,000
    ~27s, 8,000 ~228s, and the grouped spelling ``(a+)(a+)$`` ~4.1s / ~35s. So
    this predicate gates ONLY the unbounded full-input path. A pattern it flags is
    still enforced, on the bounded engine — the behaviour every such pattern
    already had. It is deliberately not folded into ``is_safe_user_regex``:
    refusing these outright would drop rules that work today, and a rule silently
    not published is the defect this module is fighting, not a fix for it.

    A GROUP is a unit, and counts as quantified when it carries its own
    quantifier (``(ab)+``) OR when its content merely ENDS in one (``(a+)``):
    parentheses do not change how the engine redistributes the run, so
    ``(a+)(a+)$`` backtracks exactly like ``a+a+$``. Resetting state at a group
    boundary — treating a group as opaque — is what let the grouped spelling
    through.

    Conservative and syntactic: adjacency is judged on quantified units with no
    literal between them, so ``a+b+`` (disjoint runs, linear) is flagged too.
    Cheap over-rejection costs a fast path, never enforcement.
    """
    # One frame per nesting level. ``end`` is where the frame's most recent unit
    # ended; ``quantified`` says whether that unit was quantified.
    stack: list[dict] = [{"end": None, "quantified": False, "start": 0}]
    i, n = 0, len(pattern)

    def read_multi_quantifier(idx: int) -> "int | None":
        """Index past a >1-repetition quantifier at ``idx``, or None if absent."""
        if idx >= n:
            return None
        if pattern[idx] in "*+":
            j = idx + 1
            if j < n and pattern[j] in "?+":  # lazy / possessive modifier
                j += 1
            return j
        if pattern[idx] == "{":
            close = pattern.find("}", idx)
            if close != -1:
                body = pattern[idx + 1 : close]
                if body and all(c.isdigit() or c == "," for c in body):
                    hi = body.split(",")[-1] or "inf"
                    if hi == "inf" or (hi.isdigit() and int(hi) > 1):
                        return close + 1
        return None

    def record(frame: dict, start: int, end: int, quantified: bool) -> bool:
        """Add a unit to ``frame``; True if it abuts a quantified predecessor."""
        adjacent = quantified and frame["quantified"] and frame["end"] == start
        frame["end"] = end
        frame["quantified"] = quantified
        return adjacent

    while i < n:
        ch = pattern[i]
        if ch == "(":
            stack.append({"end": None, "quantified": False, "start": i})
            i += 1
            # Skip the group's opening construct — (?:, (?=, (?P<name>, …
            if i < n and pattern[i] == "?":
                i += 1
                while i < n and pattern[i] not in ":)":
                    i += 2 if pattern[i] == "\\" else 1
                if i < n and pattern[i] == ":":
                    i += 1
            continue
        if ch == ")" and len(stack) > 1:
            frame = stack.pop()
            after = read_multi_quantifier(i + 1)
            quantified = after is not None or frame["quantified"]
            end = after if after is not None else i + 1
            if record(stack[-1], frame["start"], end, quantified):
                return True
            i = end
            continue
        # A plain unit: an escape, a bracket class, or a single character.
        start = i
        if ch == "\\":
            i += 2
        elif ch == "[":
            i += 1
            if i < n and pattern[i] == "^":
                i += 1
            if i < n and pattern[i] == "]":  # literal ']' first in the class
                i += 1
            while i < n and pattern[i] != "]":
                i += 2 if pattern[i] == "\\" else 1
            i += 1
        else:
            i += 1
        after = read_multi_quantifier(i)
        end = after if after is not None else i
        if record(stack[-1], start, end, after is not None):
            return True
        i = end
    return False


def _has_top_level_alternation(pattern: str) -> bool:
    """True if ``pattern`` has a ``|`` at nesting depth 0.

    A top-level alternation binds looser than concatenation (``a.*b|c`` is
    ``(a.*b)|(c)``), so splitting on top-level ``.*`` would be INCORRECT — such
    a pattern must use the bounded-scan fallback instead.  Bracket classes and
    escapes are skipped so a ``|`` inside ``[...]`` or a literal ``\\|`` does
    not count.
    """
    depth = 0
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            i += 1
            if i < n and pattern[i] == "^":
                i += 1
            if i < n and pattern[i] == "]":
                i += 1
            while i < n and pattern[i] != "]":
                if pattern[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth == 0:
            return True
        i += 1
    return False


def _split_deny_frags(pattern: str) -> list[str]:
    """Split ``pattern`` on its TOP-LEVEL ``.*`` gaps into fixed fragments.

    Only an unescaped ``.`` immediately followed by ``*`` at nesting depth 0 is
    treated as a gap (a ``.?`` / ``.+`` / a nested ``.*`` inside ``(...)`` stays
    inside its fragment and is matched by the real engine).  A lazy (``.*?``) or
    possessive (``.*+``) modifier on the gap is consumed with it — all three
    spellings mean "any run of characters" for an ordered existence-match split,
    and leaving the dangling ``?`` / ``+`` behind would produce a fragment that
    starts with a bare quantifier and fails to compile, silently disabling an
    otherwise-valid user rule.  Empty fragments (from a leading/trailing/adjacent
    ``.*``) are dropped — a leading/trailing ``.*`` is redundant under
    ``re.search`` and an interior empty cannot occur because two adjacent
    top-level ``.*`` collapse to one gap.
    """
    frags: list[str] = []
    cur: list[str] = []
    depth = 0
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            cur.append(pattern[i : i + 2])
            i += 2
            continue
        if c == "[":
            j = i + 1
            if j < n and pattern[j] == "^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                if pattern[j] == "\\":
                    j += 1
                j += 1
            j += 1
            cur.append(pattern[i:j])
            i = j
            continue
        if c == "(":
            depth += 1
            cur.append(c)
            i += 1
            continue
        if c == ")":
            depth -= 1
            cur.append(c)
            i += 1
            continue
        if c == "." and depth == 0 and i + 1 < n and pattern[i + 1] == "*":
            frags.append("".join(cur))
            cur = []
            i += 2
            # Absorb a lazy/possessive modifier on the gap (``.*?`` / ``.*+``);
            # otherwise the dangling ``?`` / ``+`` becomes a fragment-leading
            # quantifier that fails to compile and disables the whole rule.
            if i < n and pattern[i] in "?+":
                i += 1
            continue
        cur.append(c)
        i += 1
    frags.append("".join(cur))
    return [f for f in frags if f]


def _frags_can_underconsume(frags: list[str]) -> bool:
    """True if the forward-only fragment matcher could MISS a real match.

    The linear matcher searches each fragment in order with an advancing
    ``re.search(text, pos)`` and cannot backtrack across a ``.*`` gap boundary.
    So if a NON-FINAL fragment ends in a greedy, variable-width quantifier
    (``.+`` / ``x*`` / ``\\S+`` / ``(...)+`` / ``a{2,}``), that fragment greedily
    consumes characters the NEXT fragment needs — e.g. ``rm .+`` in
    ``rm .+ .* --no-preserve-root`` eats ``x--no-preserve-root`` so the tail
    fragment never matches, a FALSE NEGATIVE that lets a denied command through.
    Real ``re.search`` would backtrack; the linear matcher won't.

    A lazy (``*?`` / ``+?`` / ``{m,}?``) trailing quantifier consumes minimally,
    so it CANNOT over-consume — those are safe. Only the FINAL fragment's greedy
    tail is harmless (nothing follows it). When this returns True the matcher
    routes to the bounded whole-regex path (exact ``re.search`` semantics on a
    length-capped window) instead of the linear split.
    """
    for frag in frags[:-1]:
        s = frag.rstrip()
        if not s:
            continue
        last = s[-1]
        # A lazy modifier (``*?`` / ``+?`` / ``}?``) consumes minimally → safe.
        if last == "?" and len(s) >= 2 and s[-2] in "*+}":
            continue
        if last in "*+":
            # Count preceding backslashes: an odd count means the quantifier is
            # escaped (a literal ``\*``/``\+``), which does not over-consume.
            j = len(s) - 2
            bs = 0
            while j >= 0 and s[j] == "\\":
                bs += 1
                j -= 1
            if bs % 2 == 0:
                return True
        elif last == "}":
            # ``{m,}`` / ``{m,n}`` — an open-ended (``,``-bearing) count is
            # variable-width and greedy; ``{m}`` (exact) is fixed-width, safe.
            open_idx = s.rfind("{")
            if open_idx >= 0 and "," in s[open_idx + 1 : -1]:
                return True
    return False


def _matches_full_input(pattern: str) -> bool:
    """True when :class:`_DenyMatcher` scans the WHOLE input for ``pattern``.

    A pattern reaches the length-bounded window (``_DENY_FALLBACK_SCAN_MAX_CHARS``)
    unless it is a parity-tested built-in OR splits into exactly one fragment. One
    fragment means no top-level ``.*``, hence no gap the forward-only matcher could
    fail to backtrack across, so its single ``re.search`` is full-input and exactly
    equivalent to the bounded path's semantics without the cap.

    This exists so a caller can ask the question BEFORE publishing a rule, rather
    than discovering after the fact that the row it advertised as enforcing is
    bypassable by padding the command past the cap. ``_DenyMatcher.__init__``
    decides the same thing from the fragments it already computed;
    ``test_deny_matcher_full_input_agreement`` pins the two to the same answer so
    this predicate cannot drift away from the matcher it describes.
    """
    linear = _linearize_deny_pattern(pattern)
    if _has_top_level_alternation(linear):
        return False
    try:
        frags = _split_deny_frags(linear)
    except re.error:
        return False
    return len(frags) == 1 and not _frags_can_underconsume(frags)


class _DenyMatcher:
    """A ReDoS-safe, full-length matcher for a single deny regex.

    Built once per pattern (memoized in ``_DENY_MATCHER_CACHE``).  ``match``
    returns whether the ORIGINAL pattern would match anywhere in ``text``:

    * Fragment path (BUILT-INS ONLY) — the pattern is split on its top-level
      ``.*`` gaps and the fragments are searched in order with an advancing
      ``re.search(text, pos)`` (equivalent to ``frag0.*frag1.*…`` but linear-time,
      no length bound). The 137 built-ins were authored for kiro-cli's RE2-style
      engine and are parity-tested to be fragment-safe (no backtracking-dependent
      construct — no ``(a|b)`` before a ``.*``, no greedy variable-width tail on a
      non-final fragment).
    * Bounded path (USER CUSTOM REGEXES + any built-in with a top-level
      alternation) — compiled whole and matched against a length-bounded prefix,
      giving EXACT ``re.search`` semantics (backtracking preserved). A
      user-supplied pattern is NEVER run through the forward-only fragment
      matcher: that matcher commits to each fragment's first match and cannot
      backtrack across a ``.*`` gap, so a pattern like ``(ab|a).*b`` (or a greedy
      ``rm .+.*x``) would UNDER-match and let a denied command through. Routing
      all user patterns to the exact bounded engine closes that fidelity class
      entirely — and it is ReDoS-safe because ``is_safe_user_regex`` already
      rejected catastrophic-backtracking patterns at add-time and here.

    Defense-in-depth: a pattern that fails ``is_safe_user_regex`` (a
    catastrophic-backtracking construct, only reachable via an already-stored
    USER custom regex — built-ins are safe by construction) is DISABLED — the
    matcher never runs it and never matches, logged once.  This guarantees the
    synchronous PreToolUse gate cannot be frozen even if such a pattern slipped
    into the config before the add-time check existed.  A malformed pattern
    (``re.error``) is likewise disabled so one bad rule cannot wedge the gate.
    """

    __slots__ = ("_frag_res", "_whole_re", "_bounded", "_disabled")

    def __init__(self, pattern: str) -> None:
        self._frag_res: "list[re.Pattern[str]]" = []
        self._whole_re: "re.Pattern[str] | None" = None
        self._bounded = False
        self._disabled = False
        if not is_safe_user_regex(pattern):
            # Either malformed or ReDoS-prone — refuse to run it (built-ins never
            # reach this branch; they are safe by construction).
            logger.warning("Disabling unsafe/malformed denied-command regex %r", pattern)
            self._disabled = True
            return
        linear = _linearize_deny_pattern(pattern)
        # A USER custom pattern (not one of the built-ins) is matched by the exact
        # bounded engine, never the forward-only fragment matcher — the latter
        # cannot faithfully emulate ``re.search`` backtracking (``(ab|a).*b``,
        # greedy ``.+`` before ``.*``, etc.), which would UNDER-match and let a
        # denied command through.  Built-ins are RE2-authored + parity-tested, so
        # they keep the fast fragment path.
        is_builtin = pattern in _RULE_ID_BY_PATTERN
        try:
            frags = None if _has_top_level_alternation(linear) else _split_deny_frags(linear)
            # A pattern with NO top-level ``.*`` splits into exactly one fragment,
            # and a one-fragment match IS ``re.search`` over the whole input: there
            # is no gap to fail to backtrack across, and ``_frags_can_underconsume``
            # inspects only ``frags[:-1]``, which is empty. So the under-match risk
            # that restricts the fragment path to parity-tested built-ins cannot
            # arise, and such a pattern gets FULL-INPUT matching whoever authored
            # it. This is what keeps an edition-contributed or user-added rule from
            # being silently capped at ``_DENY_FALLBACK_SCAN_MAX_CHARS`` — a rule
            # that only scans a 2000-char prefix is bypassed by padding, which is
            # not a control the Settings panel should show as enforcing.
            #
            # Gated on ``_polynomial_backtracking_prone``: removing the length cap
            # also removes what made POLYNOMIAL backtracking harmless. ``a+a+$``
            # passes ``is_safe_user_regex`` (it is not the exponential shape) and
            # measures ~3.5s against 2,000 characters, which is a stall of the
            # synchronous gate. Such a pattern keeps the bounded engine it already had;
            # only patterns that are free to run unbounded get the full-input path.
            single_fragment = (
                frags is not None and len(frags) == 1 and not _polynomial_backtracking_prone(linear)
            )
            if (
                frags is None
                or _frags_can_underconsume(frags)
                or not (is_builtin or single_fragment)
            ):
                # Bounded whole-regex: exact ``re.search`` semantics on a
                # length-capped window.  ReDoS-safe because ``is_safe_user_regex``
                # above already rejected catastrophic patterns.
                self._whole_re = re.compile(linear, re.IGNORECASE)
                self._bounded = True
            else:
                self._frag_res = [re.compile(f, re.IGNORECASE) for f in frags]
        except re.error:
            logger.warning("Skipping malformed denied-command regex %r", pattern)
            self._disabled = True

    def match(self, text: str) -> bool:
        if self._disabled:
            return False
        if self._bounded:
            if self._whole_re is None:
                return False
            # DOCUMENTED TRADE-OFF: the bounded path scans only the first
            # ``_DENY_FALLBACK_SCAN_MAX_CHARS`` chars. Python's backtracking ``re``
            # cannot give exact ``re.search`` semantics AND full-input AND
            # ReDoS-safety at once — a polynomial (non-catastrophic, so
            # is_safe_user_regex-accepted) user pattern like ``(ab|a).*b`` is
            # O(n²), which would freeze the gate on a large input without this
            # cap (true full-input would need a linear RE2 engine — a dependency
            # the project deliberately avoids). Scope of the residual: this path
            # is reached only by a pattern that NEEDS the exact engine — one with
            # a top-level alternation, or whose fragments can over-consume across
            # a ``.*`` gap. A pattern that splits into ONE fragment takes the
            # full-input path whoever authored it (built-in, edition-contributed
            # or user-added), because with no gap the single ``re.search`` already
            # has exact semantics; and an edition rule that WOULD land here is not
            # published at all (``edition_denied_rules``), since a rule enforced
            # only over a prefix is bypassable by padding. See security.md.
            return self._whole_re.search(text[:_DENY_FALLBACK_SCAN_MAX_CHARS]) is not None
        # An empty fragment list means the pattern reduced to ``.*`` (matches
        # everything).  No built-in does this, but stay fail-open-safe: only a
        # literal ``.*`` custom rule would, and it legitimately matches all.
        pos = 0
        for frag_re in self._frag_res:
            m = frag_re.search(text, pos)
            if m is None:
                return False
            pos = m.end()
        return True


_DENY_MATCHER_CACHE: dict[str, _DenyMatcher] = {}


def _deny_matcher(pattern: str) -> _DenyMatcher:
    """Return the memoized :class:`_DenyMatcher` for ``pattern``."""
    matcher = _DENY_MATCHER_CACHE.get(pattern)
    if matcher is None:
        matcher = _DenyMatcher(pattern)
        _DENY_MATCHER_CACHE[pattern] = matcher
    return matcher


#: The refusal prefix, exported so guards cannot drift from the producer.
#: ``RecoveryCard.tsx`` parses refusals with
#: ``/Blocked by security policy:\s*(.+?)\s*$/gm`` — GLOBAL and per-line — so any
#: line carrying this literal is read as a deny pattern.  An operator note is
#: emitted on its own line, which means a note containing this literal would be
#: parsed as a SECOND, fabricated pattern.  Callers that accept operator text
#: reject or drop it on this constant (see ``hooks.resolve_denied_notes`` and the
#: dashboard add handler) rather than hardcoding the string again.
DENY_REASON_PREFIX = "Blocked by security policy: "

#: The form to GUARD against, which is NOT the form we emit. ``RecoveryCard``'s
#: regex is ``Blocked by security policy:\s*`` — the whitespace after the colon is
#: optional — so ``"Blocked by security policy:forged"`` parses as a refusal line
#: while NOT containing :data:`DENY_REASON_PREFIX` (which carries a trailing
#: space). Guarding on the full prefix therefore leaves a bypass. Derived from the
#: same string so the two can never drift apart.
DENY_REASON_MATCH_PREFIX = DENY_REASON_PREFIX.rstrip()


# Suspicious bash patterns to flag during audit
SUSPICIOUS_BASH_PATTERNS: list[str] = [
    "curl * | bash",
    "curl * | sh",
    "wget * | bash",
    "| bash",
    "| sh",
    "| python",
    "| perl",
    "rm -rf /",
    "rm -rf ~",
    "rm -rf /*",
    "find * -delete",
    "find * -exec rm",
    "find * -exec shred",
    "xargs rm",
    "git clean -f",
    "shred ",
    "truncate ",
    "> /dev/sd",
    "mkfs.",
    "dd if=",
    "chmod 777",
    "chmod */usr/",
    "chmod */etc/",
    "chmod */sbin/",
    "chmod */boot/",
    "chmod */lib/",
    "chmod */lib64/",
    "chown */usr/",
    "chown */etc/",
    "chown */sbin/",
    "chown */boot/",
    "chown */lib/",
    "chown */lib64/",
    "eval $(",
    "base64 -d",
    "nc -e",
    "ncat -e",
    "/dev/tcp/",
    "xp_cmdshell",
    "GRANT ALL",
    "DROP DATABASE",
    "DROP TABLE",
    "TRUNCATE TABLE",
    "aws iam create-access-key",
    "aws sts assume-role",
    "export AWS_SECRET",
    "export AWS_ACCESS",
    "curl * -d @",
    "curl * --data @",
    "curl * -F file=@",
    "curl -d @",
    "curl --data @",
    "curl -F file=@",
    "wget --post-file",
    "nc * < ",
]


def _deny_pattern_matches(pattern: str, text: str, is_regex: bool) -> bool:
    """Match ``text`` (already lowercased) against a deny ``pattern``.

    Regex tier: matched via ``_deny_matcher`` (a memoized, ReDoS-safe
    *linear-time* matcher for the raw pattern — see the ReDoS-mitigation notes
    on ``_DenyMatcher``).  The matcher scans the FULL string, so a destructive
    needle at any offset (e.g. after a long benign prefix inside one un-split
    shell segment) is found — no length truncation.  A malformed stored pattern
    (``re.error``) is treated as a non-match so a single bad custom rule cannot
    wedge the whole gate — other rules still enforce.  Glob tier: ``fnmatch``
    (case-insensitive), unchanged.
    """
    if is_regex:
        return _deny_matcher(pattern).match(text)
    return fnmatch.fnmatch(text, pattern.lower())


def _deny_reason(
    matched: str,
    reason_notes: "dict[str, str] | None",
    *,
    note_override: str = "",
    diagnostic: "RefusalDiagnostic | None" = None,
) -> str:
    """Refusal text for *matched*, with the operator note on a SECOND line.

    The first line is byte-for-byte what it has always been. That is load bearing,
    not stylistic: ``RecoveryCard.tsx`` extracts the pattern with
    ``/Blocked by security policy:\\s*(.+?)\\s*$/gm`` -- per-line and end-anchored --
    so anything appended to the SAME line is captured as part of the pattern, and
    ``_denied_by`` in the test suite partitions on the exact
    ``"Blocked by security policy: "`` separator.  A note therefore goes on its own
    line, where both readers ignore it.

    Built-in rules never carry a note (the map holds user patterns only), so for them
    this returns exactly the historical string -- unless the caller passes
    *note_override*, which the argv-structural floor uses to say why a pattern the
    input does not literally match was still the rule that fired.  Without it the
    reported pattern is the rule's catalog regex, which for that path provably
    cannot match the input, so the reason names a cause the reader can disprove.

    *diagnostic* appends a THIRD line naming the rule id, the deciding component and
    the matched span's offsets and character-class shape (see
    :mod:`kiro_crew.security.diagnostics`).  It is opt-in per call site rather than
    always-on, and the split is not cosmetic: a pattern-tier denial's first line
    already IS the accurate cause, so a diagnostic there would add a line to every
    ordinary refusal for no information, while a STRUCTURAL denial reports a pattern
    the input cannot match and is the case an agent cannot diagnose at all.  So the
    tiers that decide on argv shape pass one and the pattern tiers do not, which is
    also why a plain catalog refusal stays exactly one line.

    Module-level rather than a closure because EVERY tier that can refuse must emit
    the identical micro-format: a second producer would be free to drift from the
    three consumers that parse it.
    """
    head = f"{DENY_REASON_PREFIX}{matched}"
    note = (note_override or (reason_notes or {}).get(matched, "")).strip()
    reason = f"{head}\n{note}" if note else head
    if diagnostic is None:
        return reason
    return annotate_refusal(reason, diagnostic)


# ── Environment Credential Exfiltration Detection ──
# Attackers can read AWS credentials from environment variables without
# touching the filesystem, bypassing is_sensitive_path/bash checks.
# Block: declare -p AWS_SECRET*, env | grep AWS_, printenv AWS_,
#         awk 'ENVIRON["AWS_*"]', export -p | grep AWS_

_ENV_CRED_PATTERNS: list[re.Pattern[str]] = [
    # declare -p AWS_SECRET_ACCESS_KEY / declare -p AWS_SESSION_TOKEN
    re.compile(
        r"declare\s+(?:-[a-zA-Z]+\s+)*-?p\s+AWS_(?:SECRET|SESSION|SECURITY)",
        re.IGNORECASE,
    ),
    # echo $AWS_SECRET* / echo ${AWS_SECRET*}
    re.compile(
        r"(?:echo|printf|cat)\s+.*\$\{?AWS_(?:SECRET|SESSION|SECURITY)",
        re.IGNORECASE,
    ),
    # awk ENVIRON["AWS_SECRET*"] / awk ENVIRON["AWS_SESSION*"]
    re.compile(
        r"awk\s+.*ENVIRON\s*\[\s*[\"']AWS_(?:SECRET|SESSION|SECURITY)",
        re.IGNORECASE,
    ),
    # python/ruby/node reading os.environ for AWS secrets
    re.compile(
        r"(?:python|ruby|node|perl)\S*\s+.*(?:os\.environ|ENV|process\.env)"
        r".*AWS_(?:SECRET|SESSION|SECURITY)",
        re.IGNORECASE,
    ),
]

# Two intents this tier and the deny catalog express identically: an environment
# dump piped through grep/awk/sed for AWS variables, and ``printenv`` naming a
# secret-bearing variable directly. They are held as CATALOG RULE IDS and resolved
# from ``BUILTIN_DENIED_RULES`` -- never from the user's effective set -- so this
# tier runs exactly the regex the catalog publishes and still refuses when the
# catalog rule is opted out. Naming the rule rather than keeping a second reference
# to its pattern is what makes "one regex per intent" structural: there is no
# parallel constant here that could be edited alone, and the two had already
# drifted in the dangerous direction once (the keystone covered three full variable
# names while the catalog covered every secret-bearing prefix, so the tier that
# cannot be switched off was the weaker of the two).
_ENV_CRED_SHARED_RULE_IDS: tuple[str, ...] = (
    "credential-exfil-env-grep-aws",
    "credential-exfil-env-awk-sed-aws",
    "credential-exfil-printenv-aws",
)

# Resolved eagerly and without a default, so a renamed rule id fails loudly at
# import instead of silently shrinking the tuple and retiring the always-on block.
_ENV_CRED_SHARED_RULES: tuple[DeniedCommandRule, ...] = tuple(
    next(rule for rule in BUILTIN_DENIED_RULES if rule.id == rule_id)
    for rule_id in _ENV_CRED_SHARED_RULE_IDS
)

_ENV_CRED_DENIAL_REASON = "Blocked: command reads AWS credentials from environment variables"


def _check_env_credential_access(command: str) -> str | None:
    """Detect attempts to read AWS credentials from environment variables.

    Returns denial reason if env credential access detected, None otherwise.

    The shared rules run through the same ``_deny_matcher`` the catalog tier uses,
    not a raw ``re.search``. Sharing the regex TEXT alone is not enough: this tier
    applies no length cap, and an ordered-existence pattern
    (``dump .* | .* filter .* selector``) under Python's backtracking engine is
    superlinear in the number of candidate pipes and filter words -- seconds on a
    few thousand characters, against milliseconds on the linear fragment matcher --
    so a raw search here would hand a long crafted command a stall of the
    synchronous PreToolUse gate that the catalog tier is already immune to.
    """
    for rule in _ENV_CRED_SHARED_RULES:
        if _deny_matcher(rule.pattern).match(command):
            return _ENV_CRED_DENIAL_REASON
    for pattern in _ENV_CRED_PATTERNS:
        if pattern.search(command):
            return _ENV_CRED_DENIAL_REASON
    return None


# This module is the import path of every name the rule catalog offers. The resolvers, the
# matcher and the refusal producer above stay here; the catalog and the tables keyed to it
# live in the owner modules below, and a write through this module (a patch) reaches every
# module holding the name (:mod:`kiro_crew._facade_mirror`). The header binds the tables
# ``deny_gate`` reads through this module on every call, so those reads stay plain lookups.
# Owners never import this module. New work goes to the owner of its responsibility:
#   ``denied_rules_catalog`` -- the row type and the built-in rows
#   ``denied_rules_tables`` -- the lookups, floor rule id sets and notes keyed to the rows
from kiro_crew import _facade_mirror  # noqa: E402

#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.security.denied_rules_catalog",
    "kiro_crew.security.denied_rules_tables",
)  # the loop below imports them in this order
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.security.denied_rules_catalog import (  # noqa: F401
        _AWS_SECRET_VAR_NAMES,
        _AWS_SECRET_WORD_PREFIXES,
        _AWS_SECRET_WORDS,
        _AWS_TEXT_VAR_SELECTOR,
        _AWS_VAR_SELECTOR,
        _ENV_DUMP_AWK_SED_AWS_PATTERN,
        _ENV_DUMP_GREP_AWS_PATTERN,
        _ENV_DUMP_VERBS,
        _NETCAT_EXEC_PATTERN,
        _PRINTENV_AWS_SECRET_PATTERN,
        _aws_secret_word_prefix_alternation,
    )
    from kiro_crew.security.denied_rules_tables import (  # noqa: F401
        _FLOOR_ENFORCED_RULE_IDS,
        _GIT_PUBLISH_RULE_CATEGORY,
        _GIT_PUBLISH_RULES,
        _GIT_PUBLISH_UNGATED_RULE_IDS,
        _INTERPRETER_RULE_IDS,
        _SELF_PROTECTION_FLOOR_RULE_IDS,
        _SELF_PROTECTION_UNGATED_FLOOR_IDS,
        floor_enforced_builtin_command_ids,
    )
