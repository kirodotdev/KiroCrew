# Design: User-configurable denied commands (hooks-only enforcement)

**Date:** 2026-07-22
**Branch:** `feat/denied-command-opt-out`
**Status:** Approved (brainstorm) — ready for implementation plan

## Problem

KiroCrew ships a large, opinionated list of shell commands blocked from the
agent (~130 regexes). Two enforcement layers exist today:

1. **kiro-cli agent config** — `toolsSettings.execute_bash.deniedCommands` (130
   regexes) in `src/kiro_crew/config/defaults.json`, force-injected into every
   `~/.kiro/agents/*.json` by `agent._enforce_denied_commands()` (install +
   gateway startup + ~60s periodic loop). Injection **replaces** the array, so
   it silently wipes any `deniedCommands` a user authored in their own agent
   file.
2. **KiroCrew's own PreToolUse gate** — `hooks.HookManager.on_tool_call` →
   `security.is_denied` against a short `BUILTIN_DENY_PATTERNS` glob list plus
   always-on keystone controls (sensitive-path, IMDS, git-publish, exfil).

Users complain the block list is too aggressive and un-opt-out-able — neither
plain kiro-cli nor Claude Code impose it. Security (Nikita) approved letting
users **opt out with proper warnings**; product (Joe) wants: add commands in
the UI, disable-all + disable-individual, **never delete** the list or items,
**refresh append-only** (never override a user's own agent-file settings).

## Why it matters

The current model is a support-cost and adoption blocker (false positives on
routine commands), and the `_enforce_denied_commands` **replace** semantics
actively destroys user configuration — the opposite of Joe's "append-only,
never override users' agent files" requirement. Without an opt-out that is
coherent across *both* enforcement layers, a user who disables a command in one
place is still blocked by the other ("disabled but still blocked").

## Decision summary

**Enforce denied commands ONLY at KiroCrew's `hooks.py` PreToolUse gate.** Stop
injecting `deniedCommands` into kiro agent files entirely — this satisfies
"never override users' own settings in their agent files" by *never touching
them*. The canonical list becomes KiroCrew-owned structured data
(`BUILTIN_DENIED_RULES` in `security.py`), default-ON but user-disableable from
**Settings → Security**. A governance `commands`-scope policy can force-pin
rules as un-opt-out-able (enterprise lock, wins via tightest-wins).

Two prerequisites made this safe (both verified in code):

- **P1 — every command must reach the gate.** `config/defaults.json` sets
  `execute_bash.autoAllowReadonly: true`, letting kiro-cli self-approve
  read-only commands *in-process* so they never emit `session/request_permission`
  and never hit `hooks.py`. We remove `autoAllowReadonly` and re-home read-only
  auto-approve **into `hooks.py`, after the deny+governance checks**, so reads
  still don't nag but the deny list is always evaluated first.
- **P2 — coverage parity.** The gate's `BUILTIN_DENY_PATTERNS` was a tiny glob
  list; the agent-config list is 130 rich regexes. We port all 130 verbatim
  into `BUILTIN_DENIED_RULES` so nothing regresses. A parity test locks this.

## Core concepts

- **`DeniedCommandRule`** — a frozen dataclass `(id, pattern, category,
  description)`. `id` is a stable slug (the opt-out key + SEL audit key).
  `pattern` is a **Python regex** (matched `re.search`, case-insensitive).
- **`BUILTIN_DENIED_RULES`** — the canonical list of **130** rules, ported
  byte-exact from `defaults.json`'s `deniedCommands`. Default-ON.
- **Effective set** (computed per tool call):
  `enabled_builtins ∪ enabled_user_added ∪ governance_pins`, where a governance
  pin re-adds a rule even if the user disabled it or set disable-all
  (tightest-wins).
- **Two enforcement tiers in `is_denied`:** the *regex tier* (built-in rules +
  user-added patterns + governance pins, via `re.search`) and the *glob tier*
  (legacy `auto_deny_tools` + companion overlay, via `fnmatch`, unchanged).
- **Always-on keystone controls stay independent and un-disableable:**
  `is_sensitive_bash_command` (`~/.aws`, `~/.ssh`, …), `_check_imds_access`
  (169.254.169.254), `_is_git_publish` (push to protected branches),
  `audit_bash_exfiltration`, `_ENV_CRED_PATTERNS`. These are the generic
  security controls CLAUDE.md mandates keeping. They run **before** the tier
  loops, unconditionally.
- **Intentional defense-in-depth nuance:** ~45 of the 130 rules overlap a
  keystone control (sensitive-file reads, IMDS, git-publish, cred-env dumps).
  All 130 rules are user-disableable, but **disabling a rule does not disable
  the independent keystone control** — such a command stays blocked by
  defense-in-depth even after its rule is off. This is intentional and
  documented in the UI copy + specs. The ~85 opinionated destructive commands
  (AWS delete/mutate, `cdk/terraform/pulumi destroy`, `rm -rf`, `DROP DATABASE`,
  kill-kirocrew, reverse shells) have **no** keystone backup — disabling those
  fully unblocks them (the actual user ask).

## Architecture & data flow

```
config.json  hooks.denied_commands = {disable_all, disabled_ids, user_added[]}
      │  (user opt-out state, agent/operator-editable, read live via fingerprint cache)
      ▼
HookManager.on_tool_call (PreToolUse gate — the SOLE enforcement point)
  1. deny-by-default for shell tools with no recoverable command
  2. sensitive-path / sensitive-bash / exfil  ── always-on keystone, unconditional
  3. write-protected-config check              ── always-on keystone
  4. effective deny set:                        ── NEW
        pins   = governance commands-scope pins (resolve_pinned_commands)
        regex  = compute_effective_denied(BUILTIN_DENIED_RULES,
                    disabled_ids, disable_all, user_added, pins)
        authority.is_denied(target, auto_deny_tools, denied_regexes=regex)
  5. governance gate_decision (ceiling ∩ profile, incl. commands scope)
  6. read-only auto-approve  ── NEW, runs ONLY here (after every deny)
  7. user auto_approve_tools loop
  8. allow
      │
      ▼
security.is_denied(tool_name, extra_patterns, *, denied_regexes)
   _is_git_publish (always-on floor, unconditional)  → block
   regex tier: re.search(IGNORECASE) over denied_regexes  → block
   glob  tier: fnmatch over extra_patterns               → block
   → "Blocked by security policy: <pattern>" | None
```

Governance stays the enterprise force-deny: `security_policy.json`'s
`commands`-scope deny patterns are projected as pins and unioned into the
effective set, overriding user opt-out. Because that file is on the
`_SENSITIVE_HOME_DIRS` keystone (agent can't write it), a pin is un-opt-out-able
by construction.

## Canonical contract (resolves blueprint divergences)

The parallel blueprint agents produced three slightly different route/field
schemes. **This section is the single source of truth** — all implementers code
against exactly these names.

### security.py

```python
@dataclass(frozen=True)
class DeniedCommandRule:
    id: str          # stable slug, e.g. "aws-destructive-cfn-delete-stack"
    pattern: str     # Python regex, matched re.search(IGNORECASE)
    category: str    # aws-destructive | credential-exfil | sensitive-file-read |
                     #   iac-teardown | local-destructive | pipe-to-shell | sql |
                     #   self-protection | git-publish | reverse-shell
    description: str # one human sentence for the UI

BUILTIN_DENIED_RULES: list[DeniedCommandRule]   # 130 rules, patterns byte-exact from defaults.json
_RULES_BY_ID: dict[str, DeniedCommandRule]
BUILTIN_DENY_PATTERNS: list[str]                # RETAINED as derived alias = [r.pattern for r in BUILTIN_DENIED_RULES]

def compute_effective_denied(
    rules: list[DeniedCommandRule],
    disabled_ids: Iterable[str],
    disable_all: bool,
    user_added: Iterable[str],
    governance_pins: Iterable[str],   # rule ids force-pinned by governance
) -> list[str]:
    """Pure, order-preserving, deduped. Returns REGEX strings.
    Include rule.pattern if (not disable_all and id not in disabled) OR id in pins.
    Append user_added verbatim. Governance pins win (tightest-wins)."""

def is_denied(
    tool_name: str,
    extra_patterns: list[str] | None = None,   # glob tier (fnmatch) — unchanged
    *,
    denied_regexes: list[str] | None = None,   # regex tier (re.search IGNORECASE)
) -> str | None:
    """denied_regexes=None → fail closed to all built-ins enabled.
    Malformed user regex: catch re.error, log WARNING, skip that pattern."""

# API-facing accessors (dict form so handlers avoid importing the dataclass):
def builtin_denied_rules() -> list[dict]:          # [{id, pattern, category, description}, ...]
def pinned_builtin_command_ids() -> set[str]:      # governance-pinned rule ids; empty in standalone
```

`_is_git_publish` / `_is_push_to_protected_branch`, `is_sensitive_bash_command`,
`audit_bash_exfiltration`, `_check_imds_access`, `_ENV_CRED_PATTERNS`,
`_SENSITIVE_HOME_DIRS` are **unchanged** and run before the tiers.

### platform/security_authority.py (floor redefinition)

- `BASELINE_DENY: Tuple[str, ...] = ()` — name preserved (re-exported by
  `platform/__init__.py`), value now empty. Built-ins are no longer the
  un-weakenable baseline; they are the disableable tier.
- `PolicyAuthority.is_denied(self, tool_name, extra_patterns=None, *,
  denied_regexes: list[str] | None = None) -> str | None` — `@final`, forwards
  `denied_regexes` to `security.is_denied`; overlay patterns go through
  `extra_patterns` (ADD-only, never filtered by user opt-out).
- `assert_security_floor` — keeps the `isinstance` + `@final`-override
  structural guards; **removes** the builtin-superset assertion and the
  git-publish behavioral probe (git-publish is now a disableable rule enforced
  independently by the always-on `_is_git_publish` floor inside `security.py`).
  The un-weakenable floor is now (a) the companion ADD-only `SecurityOverlay`
  and (b) governance pins.

### platform/governance.py (enterprise force-pin, purely additive)

```python
COMMANDS_SCOPE = "commands"
def _command_deny_patterns(control) -> Tuple[str, ...]   # deny-mode ScopedRuleset.deny, else ()
GovernanceCeiling.pinned_command_patterns(self) -> Tuple[str, ...]
Profile.pinned_command_patterns(self) -> Tuple[str, ...]
def resolve_pinned_commands(ceiling, profile=None) -> Tuple[str, ...]   # deduped union
```

`load_security_policy`, `gate_decision`, `resolve`, `assert_governance_paths_protected`
are **unchanged** (confirm-only). Pins are an additional projection consumed by
the hooks effective-set union; ALLOW-mode command allowlists are NOT projected
as pins (stay gate-only). No new `SCOPE_CATALOG` row.

> Note: governance's `commands` matcher is `fnmatchcase` (case-sensitive) while
> the security union matches case-insensitively (broader). Safe for a deny; do
> not "fix" one to match the other. A governance pin is an independent ceiling
> that *covers the same command*, not literally the same rule string.

### hooks.py (HooksConfig lives HERE, not config/loader.py)

```python
@dataclass
class UserDeniedPattern:
    id: str = ""; pattern: str = ""; enabled: bool = True
    @classmethod
    def from_dict(cls, data: dict) -> "UserDeniedPattern": ...   # blank id → uuid4().hex[:12]
    def to_dict(self) -> dict: ...

# HooksConfig gains a nested sub-object, persisted under hooks.denied_commands:
denied_commands_disable_all: bool = False
denied_commands_disabled_ids: list[str] = field(default_factory=list)
denied_commands_user_added: list[UserDeniedPattern] = field(default_factory=list)
```

- `HooksConfig.from_dict` reads `data.get("denied_commands", {})` (nested
  sub-object) → the three fields; tolerates missing. `to_dict` writes them back
  nested and does NOT re-emit `_BUNDLED_AUTO_APPROVE_TOOLS`.
- `on_tool_call`: replace `deny = self._config.auto_deny_tools` with a resolved
  effective set. New `_effective_denied(ctx)` returns the regex-tier list via
  `compute_effective_denied(...)` using `_governance_pinned_command_ids(ctx)`
  (fail-soft like `_governance_denial`); `auto_deny_tools` stays the glob tier
  passed via `extra_patterns`. Call
  `authority.is_denied(target, self._config.auto_deny_tools, denied_regexes=regex)`.
- New read-only auto-approve step is the **last branch before `allow()`**, after
  every early-return deny + governance. Reuse existing classifiers
  (`dashboard.state.is_read_only_bash` for shell; `slack.gateway._is_read_only_tool`
  + `tool_kind in {"read","fetch"}` for non-shell) via **function-local imports**
  (top-level would cause an import cycle: `slack.gateway` imports `hooks`).
- `loader.py` needs **no change** — `hooks: dict` round-trips the nested keys
  verbatim; `schema.py` treats `hooks` as opaque (no change).

Config JSON shape:
```json
"hooks": {
  "denied_commands": {
    "disable_all": false,
    "disabled_ids": ["<builtin-rule-id>", ...],
    "user_added": [{"id": "user-xxxxxxxx", "pattern": "rm -rf /tmp/mine", "enabled": true}]
  }
}
```

### config/defaults.json

- Remove `execute_bash.autoAllowReadonly`.
- Remove `deniedCommands` from **both** `execute_bash` and `shell`. Both become
  empty ⇒ delete the whole `toolsSettings` block; fix trailing-comma
  bookkeeping. Remaining top-level keys: `name, description, model, tools,
  allowedTools, resources, includeMcpJson, hooks`.

### agent.py (retire injection — lands in lockstep with defaults.json)

Remove: `_enforce_denied_commands`, `_denied_cmd_mtimes`, `_last_skipped_set`,
the `deniedCommands` overwrite in `build_agent_config` (~1238) and
`_refresh_dynamic_fields` (~1332, incl. its RuntimeError guard), the
`KiroCrewConfig` import (line 43 only — keep `config_path` on line 44).
`repair_agent_configs` keeps `_sanitize_agent_hooks`, drops the
`_enforce_denied_commands()` call. Cross-file cleanups (same change-set):
`session.py` (import + `CleanupHook('denied_commands', …)` + `start_pool` block +
`_enforce_denied_commands_hook`), `config/loader.py` `enforce_denied_commands`
field + wiring, `dashboard/handlers/core.py:993` settable-key entry,
`dashboard/handlers/agents.py` deniedCommands injection. Delete/rewrite
`test/test_enforce_denied_scope.py`; update `test/test_agent.py`,
`test/test_config_patch.py`.

### API — new module dashboard/handlers/security.py

Config sub-object at `config.json["hooks"]["denied_commands"]`; mutations use
`_get_config_lock()` (agents.py) + `_atomic_json_write` (agent.py) and emit a
SEL audit entry (success `ok`, reject `denied`). Preserve sibling `hooks` keys —
mutate only `denied_commands`.

| Method | Route | Body | Notes |
|---|---|---|---|
| GET | `/api/security/denied-commands` | — | returns `DeniedCommandsData`; no audit (read) |
| PATCH | `/api/security/denied-commands/builtins/{id}` | `{enabled: bool}` | 404 unknown id; **409** if `enabled:false` on a pinned rule; `enabled:true` on pinned = 200 no-op |
| PATCH | `/api/security/denied-commands/disable-all` | `{value: bool}` | pinned rules stay enabled in snapshot |
| POST | `/api/security/denied-commands/user` | `{pattern: str}` | 400 empty / >512 chars / `re.compile` error; id = `user-` + uuid4().hex[:12] |
| PATCH | `/api/security/denied-commands/user/{id}` | `{enabled: bool}` | 404 unknown |
| DELETE | `/api/security/denied-commands/user/{id}` | — | 404 unknown |

Every endpoint (GET + all mutations) returns the full refreshed snapshot:

```json
{
  "builtins":   [{"id","pattern","category","description","enabled","pinned"}],
  "user_added": [{"id","pattern","enabled"}],
  "disable_all": false,
  "effective_count": 130,
  "governance_locked": false
}
```

`enabled = pinned OR (not disable_all AND id not in disabled_ids)`.
`governance_locked = len(pinned_builtin_command_ids()) > 0`.
`effective_count = #enabled builtins + #enabled user_added`.
`core.api_security_stats` re-sources its `denied_commands` count from
`count_effective_denied_commands()` (drop the `build_agent_config` import →
F401). Register 6 routes in `server.py` after `/api/security/stats`; re-export
from `handlers/__init__.py`.

### website/src/api/client.ts

```ts
export interface DeniedCommandRule {
  id: string; pattern: string; category: string; description: string;
  enabled: boolean; pinned: boolean;
}
export interface DeniedUserRule { id: string; pattern: string; enabled: boolean }
export interface DeniedCommandsData {
  builtins: DeniedCommandRule[];
  user_added: DeniedUserRule[];
  disable_all: boolean;
  effective_count: number;
  governance_locked: boolean;
}
// api members (each mutation resolves to the full refreshed DeniedCommandsData):
deniedCommands(): Promise<DeniedCommandsData>                                   // GET
toggleBuiltinDeniedCommand(id: string, enabled: boolean): Promise<...>          // PATCH builtins/{id}
setDeniedCommandsDisableAll(value: boolean): Promise<...>                       // PATCH disable-all
addUserDeniedCommand(pattern: string): Promise<...>                            // POST user
toggleUserDeniedCommand(id: string, enabled: boolean): Promise<...>            // PATCH user/{id}
deleteUserDeniedCommand(id: string): Promise<...>                              // DELETE user/{id}
```

Use `get()` / `patch()` / `post()` / `del()` helpers (carry `X-Session-Key`).

### website/src/pages/settings/SecurityPanel.tsx

Extend the existing panel (Settings → Security tab). New `<SettingsSection
title="Denied Commands">` with two cards, all lucide-react icons (no emojis),
matching existing `SettingsSection`/`SettingsCard`/`Toggle`/`Btn`/`Input` idioms:

- **Card A — Built-in denies:** primary **"Disable all built-in denies"** toggle
  (disabled + Lock when `governance_locked`); rules grouped by `category`; each
  row = description + collapsible monospace pattern (Chevron) + `Toggle`.
  Turning a rule **OFF** (or the disable-all toggle on) opens a **confirm modal**
  requiring an acknowledgment checkbox; turning **ON** is immediate. Pinned rows
  render a `Lock` + disabled forced-on toggle + tooltip ("Enforced by your
  organization's security policy"). While `disable_all`, individual toggles
  render dimmed but remain individually configurable.
- **Card B — Your custom denies:** `user_added` rows with `Toggle` + `Trash2`
  delete; an add-pattern `Input` + `Plus` `Btn` validating `new RegExp(value)`
  client-side (UX pre-check; backend re-validates with `re.compile`).
- The existing read-only "Denied Commands" status row now shows
  `effective_count` (drop the stale `agents/defaults.json` href).
- React-query key `['denied-commands']`; every mutation seeds from the returned
  snapshot and invalidates `['denied-commands']` + `['security-stats']`.
- Confirm modal copy names the specific rule and warns disabling weakens
  protection against destructive/credential-exfil commands; `ack` resets on
  open/close. No persistent banner (per approved UX).

## Warning UX (Nikita's requirement)

Confirm dialog with an explicit acknowledgment checkbox on every
disable-a-built-in and disable-all action. Every mutation writes a SEL audit
entry (durable record of a weakened state). No persistent banner.

## Error handling & edge cases

- Bad regex on add → 400, never persisted. Malformed **stored** user regex at
  match time → caught (`re.error`), logged WARNING, that one pattern skipped
  (other rules still enforce; a typo can't wedge the gate).
- Toggle/​disable a governance-pinned rule → 409; UI renders it locked so this
  is defense-in-depth.
- `config.json` corrupt → GET/snapshot tolerates as `{}`; mutations return 500
  rather than silently resetting the file.
- Migration: existing installs have no `hooks.denied_commands` → defaults (all
  built-ins on, nothing disabled). Existing `~/.kiro/agents/*.json` may retain
  stale injected `deniedCommands`; harmless (kiro-cli still honors them as a
  redundant layer) and no longer refreshed. No data migration required.
- Heartbeat-scoped `HooksConfig` (gateway.py) constructs positionally and drops
  most fields → thread the three `denied_commands_*` fields through so denies
  carry into heartbeat sessions.
- Cron `mcp_cron` `is_denied(command)` path passes no `denied_regexes` → fails
  closed to all built-ins (stricter). Acceptable; note in spec.

## Testing

**Backend (`security.py` ≥80% coverage):**
- `compute_effective_denied`: disable-all, per-id disable, user-added append,
  pin re-adds a disabled rule, pin re-adds under disable-all, dedup/order.
- **Parity test** — `{r.pattern for r in BUILTIN_DENIED_RULES}` equals a frozen
  golden set of the 130 patterns (locks no-coverage-loss); 130 unique ids.
- `is_denied` dual matching: regex tier match, glob tier match, malformed user
  regex skipped, git-publish still blocks with empty `denied_regexes`.
- `security_authority`: `assert_security_floor` passes with empty opt-out;
  `BASELINE_DENY == ()`; overlay still enforced; `@final` guard intact.
- `governance`: deny-mode `commands` ceiling → `pinned_command_patterns`;
  `resolve_pinned_commands` union; allow-mode → `()`.
- `hooks`: deny runs before read-only auto-approve; a disabled built-in falls
  through unless pinned; a pinned rule stays denied; read-only auto-approve
  never re-admits a denied/sensitive command.
- `agent.py`: `deniedCommands` no longer written; config generation valid;
  no dangling refs to removed symbols.
- API: each endpoint happy-path + 400/404/409 + SEL audit emitted.

**Frontend (vitest):** toggle calls API; confirm modal gates disable; pinned
rows locked + no mutation; add validates regex; delete only on user rows;
status row shows `effective_count`.

**Build gate (all must pass):** `pytest`, `flake8`, `mypy`, `tsc -b`, `vitest`.

## Docs to update (same commit)

- `docs/system-specs/modules/security.md` — Denied Commands section → hooks-only
  + `DeniedCommandRule`/`BUILTIN_DENIED_RULES`; retire `_enforce_denied_commands`
  + `autoAllowReadonly` notes; add opt-out + read-only-auto-approve subsection;
  threat-model row; "when writing new code" rule 6.
- `docs/system-specs/modules/governance.md` — Plane A gate order (effective
  deny-floor → gate_decision → read-only auto-approve); Plane B reinforcement
  (no agent-JSON injection at all); `commands`-scope as enterprise force-pin.
- `docs/system-specs/modules/platform-context.md` — ADD-only floor redefinition
  (`BASELINE_DENY` narrows to overlay+pins; built-ins ride in via effective set).
- `CLAUDE.md` / `AGENTS.md` — note deniedCommands are hooks-enforced + user-
  configurable (keep the "keep generic security controls" guidance intact).
- `src/kiro_crew/docs/configuration.md` — remove/retire the
  `agent.enforce_denied_commands` config-field documentation (field is deleted).

## Out of scope (YAGNI)

- "This rule would block N of your recent commands" preview.
- Editing the governance `security_policy.json` from the UI (keystone — CLI/file
  only, by design).
- Per-rule custom severities or categories in the UI beyond grouping.
- Relocating `_is_read_only_tool`/`is_read_only_bash` into a leaf module
  (function-local imports suffice; flagged as a possible follow-up).
