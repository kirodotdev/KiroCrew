"""The lookups and floor rule sets keyed to the built-in catalog rows.

The rule lookups by id and by pattern, the git-publish and self-protection rule
id sets the argv-structural floor enforces with the second-line notes a floor
denial prints, the interpreter-payload rule set, and the flat list of pattern
strings ``cli_commands`` imports. Each pattern table is built from the catalog
rows at import from the same rule ids as its id set, so the two cannot drift
apart. ``is_denied``'s floor branch reads these sets and notes; the argv floor
imports only the sentinel it reports for a push it cannot verify
(``_GIT_PUBLISH_UNGATED``).

:mod:`kiro_crew.security.denied_rules` is the import path of every name defined
here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .denied_rules_catalog import BUILTIN_DENIED_RULES

if TYPE_CHECKING:
    from .denied_rules_catalog import DeniedCommandRule


_RULES_BY_ID: dict[str, DeniedCommandRule] = {r.id: r for r in BUILTIN_DENIED_RULES}

# Reverse map (pattern → rule id) for SEL audit enrichment on a regex-tier match.
_RULE_ID_BY_PATTERN: dict[str, str] = {r.pattern: r.id for r in BUILTIN_DENIED_RULES}


# ── Git-publish rule patterns are NOT evaluated in the Python regex tier ──
# The ``git-publish`` category rules exist in the catalog for UI display /
# opt-out parity, but git-publish enforcement is done by the verb-anchored ``_is_git_publish``
# / ``_is_push_to_protected_branch`` floor (evaluated BEFORE the regex tiers), gated per catalog
# row: a disabled row stays disabled.  Only the ungated anti-obfuscation branch
# (``_GIT_PUBLISH_UNGATED``, reported as ``git-publish-push-brace-expansion-refspec``) ignores
# opt-out.  Their patterns were authored for
# kiro-cli's linear-time (RE2-style) engine; under Python's backtracking
# ``re`` the nested ``(?:...)*`` quantifiers are catastrophic (ReDoS) on
# pathological flag-spam input, so they must never reach ``re.search``.  The
# always-on floor already covers every case these patterns would (protected
# targets denied, feature branches allowed), so skipping them loses no coverage.
_GIT_PUBLISH_RULE_CATEGORY = "git-publish"
# Single filtered view of the catalog so the pattern set and the id set below
# cannot drift apart (both must cover exactly the git-publish rules).
_GIT_PUBLISH_RULES: tuple[DeniedCommandRule, ...] = tuple(
    r for r in BUILTIN_DENIED_RULES if r.category == _GIT_PUBLISH_RULE_CATEGORY
)
_GIT_PUBLISH_RULE_PATTERNS: frozenset[str] = frozenset(r.pattern for r in _GIT_PUBLISH_RULES)

# Tag returned by ``_git_publish_floor_tags`` for the anti-obfuscation branches
# (substitution glue, unparseable push, no clean push segment). Deliberately not
# a rule id: these are what make the gated rules non-bypassable, so no opt-out
# may reach them. The leading NUL-ish sentinel shape cannot collide with a slug.
_GIT_PUBLISH_UNGATED = "\x00git-publish-unverifiable"

# ``id -> pattern`` for the git-publish rules, so a floor denial can report the
# rule's own pattern (as the regex tier does) instead of an opaque label: the
# human string "git push" would map back to NO rule id in the SEL audit trail.
_GIT_PUBLISH_FLOOR_BY_ID: dict[str, str] = {r.id: r.pattern for r in _GIT_PUBLISH_RULES}

# Why a git-publish floor denial happened, in words. Same role as
# ``_SELF_PROTECTION_FLOOR_NOTES``: the floor routinely fires on input the
# catalog pattern does NOT literally match, because these patterns are kept out
# of ``re`` entirely (ReDoS) and the verb-anchored floor is the only enforcement.
# Presentation-only SECOND line — ``RecoveryCard.tsx`` parses the pattern from
# the first line with a per-line end-anchored regex.
_GIT_PUBLISH_FLOOR_NOTES: dict[str, str] = {
    "git-publish-push-bare": (
        "Matched structurally on the parsed push arguments, not by the pattern text above: "
        "the push names no branch, so it publishes whatever branch is checked out."
    ),
    "git-publish-push-single-arg": (
        "Matched structurally on the parsed push arguments, not by the pattern text above: "
        "the push names a remote but no branch, so it publishes to the configured upstream."
    ),
    "git-publish-push-protected-branch-name": (
        "Matched structurally on the parsed push arguments, not by the pattern text above: "
        "a refspec resolves to a protected branch after shell quoting is collapsed."
    ),
    "git-publish-push-protected-ref-path": (
        "Matched structurally on the parsed push arguments, not by the pattern text above: "
        "a refs/heads, heads/ or remotes/ ref path resolves to a protected branch."
    ),
    "git-publish-push-wildcard-refspec": (
        "Matched structurally on the parsed push arguments, not by the pattern text above: "
        "a wildcard refspec expands to many refs, which can include a protected branch."
    ),
    "git-publish-push-mirror-all": (
        "Matched structurally on the parsed push arguments, not by the pattern text above: "
        "--mirror/--all push every local ref regardless of any explicit refspec."
    ),
    "git-publish-push-ambiguous-ref": (
        "Matched structurally on the parsed push arguments, not by the pattern text above: "
        "the destination is a symbolic ref that only resolves when the push runs."
    ),
}

# The one git-publish rule whose coverage is an UNGATED branch: brace expansion
# is caught by ``_AMBIGUOUS_EXPANSION_RE`` inside the unverifiable-glue check, so
# disabling this row would change nothing and the Settings surface must keep
# rendering it locked (see ``floor_enforced_builtin_command_ids``).
_GIT_PUBLISH_UNGATED_RULE_IDS: frozenset[str] = frozenset(
    {"git-publish-push-brace-expansion-refspec"}
)

# Catalog rules whose ENFORCEMENT is an always-on floor that no opt-out reaches.
# This is the one explicit id in ``_GIT_PUBLISH_UNGATED_RULE_IDS``, NOT the whole
# git-publish category: every other git-publish row is gated per row by the floor
# and stays opt-out-able.
_FLOOR_ENFORCED_RULE_IDS: frozenset[str] = _GIT_PUBLISH_UNGATED_RULE_IDS


def floor_enforced_builtin_command_ids() -> frozenset[str]:
    """Built-in rule ids enforced by an always-on floor (not opt-out-able).

    These rules exist in the catalog for display parity, but their enforcement
    is the ungated anti-obfuscation branch of the verb-anchored git-publish floor
    (``_is_git_publish`` / ``_is_push_to_protected_branch``), evaluated before the configurable
    tiers, which consults no opt-out state (the floor's other git-publish rows are gated per
    row).  Persisting one of these ids into ``disabled_ids`` changes nothing — the Settings
    surface must render them locked/forced-on and the toggle API must reject a disable, or
    the opt-out is a silent no-op (UI reports success, the floor still denies).

    DISPLAY/API accessor only: nothing in the enforcement path reads it, so it
    cannot weaken the floor.  Pure and deterministic (a module-scope constant
    naming the ungated rule id), safe to call from any thread.
    """
    return _FLOOR_ENFORCED_RULE_IDS


# Self-protection rules that get the argv-structural floor (``_self_token_frames``
# -> a per-rule predicate), which sees the de-escaped, de-quoted argv that the
# raw-text regex tier cannot. Two enforcement stories share the mechanism:
#   * credential-mint / self-kill: floor-PRIMARY. Their catalog ``pattern`` is a
#     human-auditable SUBSET; a raw-string match cannot resolve shell quoting or
#     redirection, and a pattern loose enough to try would re-block ordinary
#     paths, so the floor carries enforcement.
#   * dev-mode confirm flag: regex+floor UNION. The regex catches the raw-text
#     literal; the floor additionally catches quote-splitting inside the token.
# All members stay in the regex tier (only git-publish is removed from ``re``);
# the floor is a union with it, never a replacement, and each predicate runs
# only while its row is in the effective set, so an operator-disabled rule stays
# disabled.
_SELF_PROTECTION_FLOOR_RULE_IDS: frozenset[str] = frozenset(
    {
        "credential-exfil-kirocrew-token",
        "self-protection-kill",
        "self-protection-dev-mode-out-of-root-confirm",
        "sandbox-escape-ssh-self",
    }
)
_SELF_PROTECTION_FLOOR_BY_ID: dict[str, str] = {
    r.id: r.pattern for r in BUILTIN_DENIED_RULES if r.id in _SELF_PROTECTION_FLOOR_RULE_IDS
}
_SELF_PROTECTION_FLOOR_PATTERNS: frozenset[str] = frozenset(_SELF_PROTECTION_FLOOR_BY_ID.values())

# The self-management SUBCOMMAND floors named in this set -- restart, update,
# gateway restart, cloud <destructive>, skills authority-retire -- have NO catalog row
# and NO opt-out.  A regex
# row (``.*kiro.?crew ... restart.*`` and siblings) opening with an unbounded any-run would
# match the product name in a worktree path plus the verb word later, denying
# ``ls ~/kirocrew-wt/restart.log``, and adds nothing to the structural predicate, which requires
# the product to be the argv's own PROGRAM (console script or ``python -m kiro_crew``) and the
# action its LEADING subcommand.  So the floor carries the whole of enforcement -- ungated, like
# the git-publish anti-obfuscation branches: with no row there is no toggle, and a floor that
# consulted an opt-out no row can express would be a silent allow (an ``is_denied`` skipping a
# predicate whose id resolved to no pattern would never fire it).  These ids are what a refusal and
# its SEL event report; they are deliberately NOT catalog ids, and the two sets
# are pinned disjoint so a re-added row cannot silently gate a floor again.
_SELF_PROTECTION_UNGATED_FLOOR_IDS: frozenset[str] = frozenset(
    {
        "self-protection-restart",
        "self-protection-update",
        "self-protection-gateway-restart",
        "self-protection-cloud",
        "self-protection-skills-authority-retire",
    }
)

# Why a floor denial happened, in words, for the rules whose floor can fire on
# input the catalog ``pattern`` provably does NOT match.
#
# The refusal's first line reports that pattern (see the floor branch in
# ``is_denied``) so the reason and the SEL event still map back to a rule id.
# That identifier is not an explanation, though, and for a floor hit it is a
# misleading one: ``python -c "import kiro_crew"`` is denied by the argv floor,
# while the pattern it names requires a ``token`` word the command does not
# contain. A reader who trusts the line looks for the wrong thing — and the
# refusal reason is now handed to the MODEL in-band on a tool deny
# (``chat_runner._steer_policy_notice``), so a wrong explanation actively
# misdirects the agent's next attempt rather than merely reading oddly in a log.
#
# Presentation only, on the refusal's SECOND line, which both consumers ignore:
# ``RecoveryCard.tsx`` extracts the pattern with a per-line end-anchored regex
# and the suite's ``_denied_by`` partitions on the first line's separator.
#
# The ``_SELF_PROTECTION_UNGATED_FLOOR_IDS`` entries are the same shape, but
# for them the first line reports the ID itself (there is no catalog pattern to
# report), the way the gated git-publish floor does.  The opening phrase is the
# anchor ``deny_guidance`` classifies a self-protection refusal by, so every entry
# keeps it verbatim.
_SELF_PROTECTION_FLOOR_NOTES: dict[str, str] = {
    "credential-exfil-kirocrew-token": (
        "Matched structurally on the command's argv, not by the pattern text above: "
        "the product CLI is invoked to mint a dashboard token, or an inline "
        "interpreter program names the mint surface -- the CLI module, the token "
        "subcommand's module, or a token-producing product import (an imported CLI can "
        "construct the token verb itself, so no 'token' argv word need appear)."
    ),
    "self-protection-kill": (
        "Matched structurally on the command's argv, not by the pattern text above: "
        "the command signals or kills this gateway's own process."
    ),
    "self-protection-restart": (
        "Matched structurally on the command's argv: the product CLI is the argv's own "
        "program and its leading subcommand restarts this gateway. This floor has no "
        "catalog row and no opt-out."
    ),
    "self-protection-update": (
        "Matched structurally on the command's argv: the product CLI is the argv's own "
        "program and its leading subcommand self-updates this gateway. This floor has "
        "no catalog row and no opt-out."
    ),
    "self-protection-gateway-restart": (
        "Matched structurally on the command's argv: the product CLI is the argv's own "
        "program and its leading subcommands restart the gateway server. This floor has "
        "no catalog row and no opt-out."
    ),
    "self-protection-cloud": (
        "Matched structurally on the command's argv: the product CLI is the argv's own "
        "program and its leading subcommand is a destructive cloud lifecycle operation. "
        "This floor has no catalog row and no opt-out."
    ),
    "self-protection-skills-authority-retire": (
        "Matched structurally on the command's argv: the product CLI is the argv's own "
        "program and its leading subcommands retire the auto-skill authority. This "
        "stopped-installation recovery must run from the operator's own terminal, so "
        "this floor has no catalog row and no opt-out."
    ),
    "self-protection-dev-mode-out-of-root-confirm": (
        "Matched structurally on the command's argv, not by the pattern text above: "
        "shell de-escaping resolves an argument to the operator's "
        "`--confirm-out-of-install-root` attestation flag, which agent commands "
        "may never carry."
    ),
    "sandbox-escape-ssh-self": (
        "Matched structurally on the command's argv, not by the pattern text above: "
        "the ssh/scp/sftp/rsync target is THIS machine or is not classified yet; reaching "
        "this machine would re-enter the host outside the agent sandbox. (1) PENDING, "
        "retry: the first command naming a new DOTTED hostname in this gateway process is "
        "refused while a background DNS check runs, on Linux a non-loopback IP address "
        "is refused until this machine's address list loads, and outside Windows a dotless "
        "name is refused while a hosts file over 64 KiB is still being read in the "
        "background. Wait a few seconds and retry "
        "this exact command. If it is still refused, wait a minute, retry, and retry again "
        "a few seconds later: a retry after that wait can start a new check and be refused "
        "while it runs. The command runs once the target is classified as remote. (2) Still "
        "refused after those retries: a name that "
        "did not resolve (a failed lookup is never trusted as an allow), so use a name DNS "
        "can resolve or the host's IP address instead of an ssh_config-only alias; an IP "
        "address while this machine's address list cannot be read, so use a resolvable "
        "name; a dotless name on Windows when the hosts file is over 64 KiB, since its "
        "content cannot be verified, so use the full hostname or an IP address; "
        "or the target is this machine (localhost, a loopback address or alias, or "
        "one of this host's own names or addresses), which is never allowed. (3) FORWARDED "
        "port: the "
        "port is part of the target, so a forwarded port on a loopback address (a container "
        "or VM behind `ssh -p 2222 localhost`) is denied like the host, since sshd here "
        "could listen on that port too. Operator recourse for container/VM workflows is "
        "the per-rule toggle in Settings until a port-scoped exemption ships."
    ),
}

# The two INTERPRETER-payload rules.  They are ordinary regex-tier rules, but an
# interpreter CONCATENATES adjacent string literals, so they are additionally matched
# against a copy of the text with those joins collapsed.
_INTERPRETER_RULE_IDS: frozenset[str] = frozenset(
    {"credential-exfil-kirocrew-token-argv", "self-protection-kill-interpreter"}
)
_INTERPRETER_RULE_PATTERNS: frozenset[str] = frozenset(
    r.pattern for r in BUILTIN_DENIED_RULES if r.id in _INTERPRETER_RULE_IDS
)
# ``'p' + 'kill'`` is ONE string by the time the interpreter runs it.
_LITERAL_CONCAT_RE = re.compile(r"""['"]\s*\+\s*['"]""")


# ── Back-compat alias ──
# Retained as a DERIVED flat string list so ``platform/security_authority`` and
# ``cli_commands`` keep importing a ``list[str]``.  Its members are now REGEX
# strings (string identity only — the match semantics moved to ``re.search``).
BUILTIN_DENY_PATTERNS: list[str] = [r.pattern for r in BUILTIN_DENIED_RULES]
