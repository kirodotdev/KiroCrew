# User-configurable Denied Commands — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce denied commands only at KiroCrew's `hooks.py` PreToolUse gate, make the 130 built-in rules default-ON but user-disableable from Settings → Security (with a governance enterprise force-pin), and stop injecting `deniedCommands` into kiro agent files.

**Architecture:** Ported structured `BUILTIN_DENIED_RULES` in `security.py` + a pure effective-set resolver; a redefined ADD-only floor; a governance `commands`-scope pin projection; a reworked `hooks.on_tool_call` (deny-before-read-auto-approve); a REST API + Settings→Security UI. Read-only auto-approve moves from kiro-cli (`autoAllowReadonly`) into `hooks.py`.

**Tech Stack:** Python 3.9+ (stdlib, dataclasses, `re`, `fnmatch`, `asyncio`, aiohttp), React + TypeScript + Vite, `@tanstack/react-query`, lucide-react.

## Authoritative references (read before implementing)

- **Canonical contract:** `docs/superpowers/specs/2026-07-22-user-configurable-denied-commands-design.md` — the single source of truth for every signature, route, field name, and response shape. When this plan and the spec agree, follow either; if they ever differ, the spec wins.
- **Byte-exact rule data:** `/private/tmp/denied-cmd-blueprint/rules-manifest.json` — the 130 rules `[{pattern, id, category, description}]` with patterns copied verbatim from `config/defaults.json`. **`BUILTIN_DENIED_RULES` MUST be generated from this file's patterns, never re-typed** (the blueprint agents mangled regex escaping; the manifest fixes it against the real file).
- **Per-anchor edit detail:** `/private/tmp/denied-cmd-blueprint/edit-contracts.md` — current-code quotes + required new behavior per file, with exact line anchors.

## Global Constraints

- Line length 100 (black). Python ≥ 3.9; `from __future__ import annotations`. `import logging` + `logger = logging.getLogger(__name__)`.
- No hardcoded strings in business logic — constants live in designated modules.
- flake8: no unused imports (F401), pep8-naming (N806), W504. mypy must pass. Never use emojis in UI — lucide-react only.
- Route every POSIX-only call through `platform_compat` (N/A here — no process/signal work).
- Keep the generic security controls (CLAUDE.md): AKIA/ASIA redaction, sensitive-path blocking, git-publish, exfil — these stay always-on and un-disableable.
- Do NOT re-introduce Amazon-internal couplings. `ACP_BACKEND_CLAUDE` seam + `platform/` extension points stay intact; `CONTRACT_VERSION` stays 1.
- Single commit per PR is a prepare-pr concern, not this plan's — commit freely per task while building; the branch is squashed before the PR.
- Async tests carry `@pytest.mark.asyncio`. Mock kiro-cli — never spawn real processes.

---

## File structure & ownership (no two tasks edit the same file)

| File | Owner task | Responsibility |
|---|---|---|
| `src/kiro_crew/security.py` | T1 | rule model, catalog, effective-set resolver, dual-tier `is_denied`, dict accessors |
| `src/kiro_crew/platform/governance.py` | T2 | `commands`-scope pin accessors + `resolve_pinned_commands` |
| `src/kiro_crew/platform/security_authority.py` | T3 | floor redefinition, `denied_regexes` passthrough |
| `src/kiro_crew/hooks.py` | T4 | `UserDeniedPattern`, `HooksConfig` fields, `on_tool_call` deny+read-only rework |
| `src/kiro_crew/config/defaults.json` + `agent.py` + `session.py` + `config/loader.py` + `dashboard/handlers/agents.py` | T5 | retire injection + `autoAllowReadonly`; cascade cleanups |
| `src/kiro_crew/dashboard/handlers/security.py` (new) + `handlers/__init__.py` + `dashboard/server.py` + `dashboard/handlers/core.py` | T6 | REST API + stats re-source + the `core.py:993` settable-key removal (owns ALL core.py edits) |
| `website/src/api/client.ts` | T7 | TS types + client methods |
| `website/src/pages/settings/SecurityPanel.tsx` | T8 | two-card UI + confirm modal |
| `test/test_denied_commands_*.py` + updates to `test/test_agent.py`, `test/test_config_patch.py`, delete `test/test_enforce_denied_scope.py`, frontend `*.test.tsx` | T9 | test suite + parity test |
| `docs/system-specs/modules/{security,governance,platform-context}.md`, `CLAUDE.md`, `AGENTS.md`, `src/kiro_crew/docs/configuration.md` | T10 | spec + doc updates |

**Dependency waves (for parallel build):**
- **Wave 1:** T1 (foundation — everyone imports from it).
- **Wave 2 (parallel):** T2, T3, T5, T7 (T3 needs T1's `is_denied` signature; T2/T5/T7 independent).
- **Wave 3 (parallel):** T4 (needs T1+T2+T3), T6 (needs T1), T8 (needs T7).
- **Wave 4 (parallel):** T9, T10.

Git commits are owned by the orchestrator (main loop) per wave after the build gate — parallel agents implement + self-test their own files but do NOT run `git commit` (concurrent index writes corrupt).

---

## Task 1: security.py — rule model, catalog, resolver, dual-tier matching

**Files:**
- Modify: `src/kiro_crew/security.py` (replace `BUILTIN_DENY_PATTERNS` block ~47-86; extend `is_denied` ~2023, ~2079-2154)
- Test: `test/test_denied_commands_security.py` (new)

**Interfaces:**
- Consumes: nothing (foundation).
- Produces:
  - `DeniedCommandRule(id: str, pattern: str, category: str, description: str)` — `@dataclass(frozen=True)`.
  - `BUILTIN_DENIED_RULES: list[DeniedCommandRule]` (130, patterns from `rules-manifest.json`).
  - `BUILTIN_DENY_PATTERNS: list[str]` retained = `[r.pattern for r in BUILTIN_DENIED_RULES]`.
  - `compute_effective_denied(rules, disabled_ids, disable_all, user_added, governance_pins) -> list[str]`.
  - `is_denied(tool_name, extra_patterns=None, *, denied_regexes=None) -> str | None`.
  - `builtin_denied_rules() -> list[dict]`; `pinned_builtin_command_ids() -> set[str]` (calls governance via `current_context`; empty in standalone; fail-soft).

- [ ] **Step 1: Generate the catalog literal from the manifest** (do NOT hand-type patterns). Emit `_BUILTIN_DENIED_RULES_DATA` from `/private/tmp/denied-cmd-blueprint/rules-manifest.json` with `repr()`-safe pattern strings, then build `BUILTIN_DENIED_RULES`.
- [ ] **Step 2: Write failing tests** — `test_catalog_has_130_unique_ids`, `test_patterns_match_manifest_verbatim`, `test_compute_effective_disable_all_drops_all`, `test_compute_effective_per_id_disable`, `test_pin_readds_disabled_rule`, `test_pin_readds_under_disable_all`, `test_is_denied_regex_tier_matches` (e.g. `aws ec2 terminate-instances i-x` blocked), `test_is_denied_glob_tier_unchanged`, `test_malformed_user_regex_skipped_not_raised`, `test_git_publish_still_blocks_with_empty_denied_regexes`.
- [ ] **Step 3: Run tests → FAIL.**
- [ ] **Step 4: Implement** per spec "security.py" section + edit-contracts anchors (dataclass, catalog, `compute_effective_denied`, dual-tier `_matches` helper with `re.search(IGNORECASE)` for regex tier + `fnmatch` for glob tier, `try/except re.error` skip, dict accessors). Keep `_is_git_publish`, `is_sensitive_bash_command`, `audit_bash_exfiltration`, `_SENSITIVE_HOME_DIRS` untouched and evaluated before the tiers.
- [ ] **Step 5: Run tests → PASS**; `flake8 src/kiro_crew/security.py`; `mypy src/kiro_crew/security.py`.

---

## Task 2: platform/governance.py — command force-pins

**Files:**
- Modify: `src/kiro_crew/platform/governance.py` (constants ~774; helper near ~936; `GovernanceCeiling`/`Profile` methods ~873/~894; new `resolve_pinned_commands` after `gate_decision` ~1302; `__all__`)
- Test: `test/test_denied_commands_governance.py` (new)

**Interfaces:**
- Produces: `COMMANDS_SCOPE = "commands"`; `GovernanceCeiling.pinned_command_patterns() -> Tuple[str, ...]`; `Profile.pinned_command_patterns() -> Tuple[str, ...]`; `resolve_pinned_commands(ceiling, profile=None) -> Tuple[str, ...]`.

- [ ] **Step 1: Write failing tests** — `test_deny_mode_commands_ceiling_yields_pins`, `test_allow_mode_commands_yields_no_pins`, `test_resolve_pinned_commands_unions_ceiling_and_profile`, `test_ungoverned_returns_empty`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the additive accessors + `_command_deny_patterns` + `resolve_pinned_commands` (deduped union) per spec. Confirm-only (no edit) on `load_security_policy`/`gate_decision`/`assert_governance_paths_protected`.
- [ ] **Step 4: Run → PASS**; flake8; mypy.

---

## Task 3: platform/security_authority.py — floor redefinition

**Files:**
- Modify: `src/kiro_crew/platform/security_authority.py` (`BASELINE_DENY` ~40, remove `_GIT_PUBLISH_PROBE` ~46, `is_denied` ~88, `assert_security_floor` ~101)
- Test: `test/test_denied_commands_authority.py` (new); also update `test/test_platform_context.py`, `test/test_cpp_wiring_*.py` (T9 owns those).

**Interfaces:**
- Consumes: T1 `is_denied(..., *, denied_regexes=None)`.
- Produces: `BASELINE_DENY = ()`; `PolicyAuthority.is_denied(self, tool_name, extra_patterns=None, *, denied_regexes=None) -> str | None` (`@final`, forwards `denied_regexes`); `assert_security_floor` keeps structural guards only.

- [ ] **Step 1: Write failing tests** — `test_baseline_deny_is_empty`, `test_assert_floor_passes_default_authority`, `test_final_override_guard_still_rejects_subclass_override`, `test_is_denied_forwards_denied_regexes`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — `BASELINE_DENY = ()`; remove `_GIT_PUBLISH_PROBE` + git-publish probe + builtin-superset assertion from `assert_security_floor` (keep isinstance + `@final`-override guards); add `denied_regexes` passthrough to `is_denied` (overlay via `extra_patterns`, never filtered). Rewrite docstrings per spec.
- [ ] **Step 4: Run → PASS**; flake8; mypy.

---

## Task 4: hooks.py — HooksConfig fields + on_tool_call rework

**Files:**
- Modify: `src/kiro_crew/hooks.py` (`UserDeniedPattern` new dataclass ~136; `HooksConfig` fields ~140; `from_dict`/`to_dict` ~149; `on_tool_call` deny block ~387-405 + read-only step ~421-426)
- Test: `test/test_denied_commands_hooks.py` (new)

**Interfaces:**
- Consumes: T1 `compute_effective_denied`, T2 `resolve_pinned_commands`/`COMMANDS_SCOPE`, T3 `PolicyAuthority.is_denied(..., denied_regexes=)`.
- Produces: `UserDeniedPattern`; `HooksConfig.denied_commands_{disable_all,disabled_ids,user_added}`; `HooksConfig.to_dict`; `HookManager._effective_denied(ctx)`; `_governance_pinned_command_ids(ctx) -> set[str]`.

- [ ] **Step 1: Write failing tests** — `test_from_dict_reads_nested_denied_commands`, `test_to_dict_roundtrip_no_bundled_leak`, `test_disabled_builtin_falls_through`, `test_pinned_builtin_still_denied_when_disabled`, `test_readonly_autoapprove_after_deny` (a denied command that is also read-only-shaped stays denied), `test_readonly_shell_autoapproved`, `test_deny_runs_before_readonly`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** per spec "hooks.py" section: nested `hooks.denied_commands` parse, `_effective_denied`, replace `deny = self._config.auto_deny_tools` with `authority.is_denied(target, self._config.auto_deny_tools, denied_regexes=self._effective_denied(ctx))`, and the read-only auto-approve as the LAST branch before `allow()` using function-local imports of `dashboard.state.is_read_only_bash` + `slack.gateway._is_read_only_tool` + `_READ_ONLY_TOOL_KINDS = frozenset({"read","fetch"})`. `_governance_pinned_command_ids` fail-soft like `_governance_denial`.
- [ ] **Step 4: Run → PASS**; flake8; mypy. Also thread the three fields through the heartbeat-scoped `HooksConfig` construction (`slack/gateway.py` positional build) so denies carry into heartbeat sessions.

---

## Task 5: Retire agent-config injection + autoAllowReadonly

**Files:**
- Modify: `src/kiro_crew/config/defaults.json` (remove `toolsSettings`); `src/kiro_crew/agent.py` (remove `_enforce_denied_commands`, caches, overwrite blocks, `KiroCrewConfig` import line 43); `src/kiro_crew/session.py` (import + `CleanupHook` + `start_pool` block + `_enforce_denied_commands_hook`); `src/kiro_crew/config/loader.py` (`enforce_denied_commands` field + wiring); `src/kiro_crew/dashboard/handlers/agents.py` (deniedCommands injection ~392-399)
- Test: covered by T9 (`test_agent.py` updates + `test_enforce_denied_scope.py` deletion). Self-test here: `python -c "import kiro_crew.agent, kiro_crew.session, kiro_crew.config.loader"` imports clean + `build_agent_config()` returns a valid dict with no `toolsSettings.execute_bash.deniedCommands`.

**Interfaces:**
- Produces: `build_agent_config`/`_refresh_dynamic_fields`/`repair_agent_configs` no longer touch `deniedCommands`; removed symbols `_enforce_denied_commands`, `_denied_cmd_mtimes`, `_last_skipped_set`.
- NOTE: does NOT edit `core.py:993` — that belongs to T6 (which owns all core.py edits). T5 leaves a note; T6 removes the settable-key entry.

- [ ] **Step 1:** Edit `defaults.json` — remove `execute_bash.autoAllowReadonly` + both `deniedCommands` arrays → delete `toolsSettings`; validate `json.load`.
- [ ] **Step 2:** Remove the `deniedCommands` overwrite + RuntimeError guard in `build_agent_config` (~1238) and `_refresh_dynamic_fields` (~1332); drop the `_enforce_denied_commands()` call from `repair_agent_configs`; delete `_enforce_denied_commands`, `_denied_cmd_mtimes`, `_last_skipped_set`; remove the line-43 `KiroCrewConfig` import (keep line-44 `config_path`). Reword docstrings.
- [ ] **Step 3:** Cascade: `session.py` (drop import, `CleanupHook('denied_commands', …)`, `start_pool` try/except, `_enforce_denied_commands_hook`); `config/loader.py` (drop `enforce_denied_commands` field + `agent_data.get` wiring); `agents.py` (drop deniedCommands injection into PUT-saved configs).
- [ ] **Step 4:** Self-test imports + `build_agent_config()` shape; `grep -n "_enforce_denied_commands\|enforce_denied_commands\|autoAllowReadonly" src/kiro_crew/` shows zero in non-test code (except any intentional doc string); flake8 the touched files.

---

## Task 6: REST API + stats re-source (owns ALL core.py edits)

**Files:**
- Create: `src/kiro_crew/dashboard/handlers/security.py`
- Modify: `src/kiro_crew/dashboard/handlers/__init__.py` (re-export), `src/kiro_crew/dashboard/server.py` (6 routes after `/api/security/stats`), `src/kiro_crew/dashboard/handlers/core.py` (`api_security_stats` re-source + drop `build_agent_config` import (F401) + remove the `agent.enforce_denied_commands` settable-key at ~993)
- Test: `test/test_denied_commands_api.py` (new)

**Interfaces:**
- Consumes: T1 `builtin_denied_rules()`, `pinned_builtin_command_ids()`.
- Produces: 6 handlers + routes per the canonical spec API table; `count_effective_denied_commands()`, `build_denied_commands_snapshot()`.

- [ ] **Step 1: Write failing tests** — GET returns snapshot; builtin toggle happy path; **409 on disabling a pinned rule**; **200 no-op enabling a pinned rule**; 404 unknown id; disable-all; user add validates regex (400 on bad/empty/>512); user toggle/delete; SEL audit emitted on each mutation; `enabled = pinned OR (not disable_all AND id not in disabled_ids)`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `handlers/security.py` per spec (config lock + `_atomic_json_write`, mutate only `hooks.denied_commands`, SEL on success+reject, snapshot from every endpoint); register routes; re-export in `__init__.py`; rewrite `api_security_stats` to call `count_effective_denied_commands()` + drop the `build_agent_config` import; remove the `core.py:993` settable-key entry. Lazy in-function imports to avoid cycles.
- [ ] **Step 4: Run → PASS**; flake8 (confirm F401 clean on core.py); mypy.

---

## Task 7: website/src/api/client.ts — types + methods

**Files:**
- Modify: `website/src/api/client.ts` (interfaces before `export const api`; 6 members after `securityStats`)
- Test: type-checked by `tsc` (T9 adds behavior tests via SecurityPanel).

**Interfaces:**
- Produces: `DeniedCommandRule`, `DeniedUserRule`, `DeniedCommandsData`; `api.deniedCommands/toggleBuiltinDeniedCommand/setDeniedCommandsDisableAll/addUserDeniedCommand/toggleUserDeniedCommand/deleteUserDeniedCommand` per spec (routes/bodies exactly as the API table).

- [ ] **Step 1: Implement** the two interfaces + six members using `get()/patch()/post()/del()` helpers; each mutation `Promise<DeniedCommandsData>`.
- [ ] **Step 2:** `cd website && npx tsc -b` → clean.

---

## Task 8: SecurityPanel.tsx — two-card UI + confirm modal

**Files:**
- Modify: `website/src/pages/settings/SecurityPanel.tsx`
- Test: `website/src/pages/settings/SecurityPanel.test.tsx` (T9 owns the test file; this task builds the component)

**Interfaces:**
- Consumes: T7 client types/methods.
- Produces: file-local `BuiltinDenyRow`, `CustomDenyRow`, `AddDenyInput`, confirm modal, `groupByCategory`; `['denied-commands']` query key.

- [ ] **Step 1: Implement** per spec "SecurityPanel.tsx" section: `<SettingsSection title="Denied Commands">` with Card A (disable-all toggle + category-grouped built-in rows with collapsible patterns + toggles) and Card B (custom rows + add-input with `new RegExp` validation). Disable-a-builtin / disable-all opens a confirm modal with a required acknowledgment checkbox (`ack` resets on open/close); enable is immediate; pinned rows render `Lock` + disabled forced-on toggle + tooltip; `disable_all_locked`/`governance_locked` disables the disable-all toggle. Status row uses `effective_count`; drop the `agents/defaults.json` href. Invalidate `['denied-commands']` + `['security-stats']` on every mutation. lucide-react only, no emojis.
- [ ] **Step 2:** `cd website && npx tsc -b` → clean.

---

## Task 9: Tests + parity + build gate

**Files:**
- Create: the per-task `test_denied_commands_*.py` are written in their tasks; this task adds cross-cutting + fixes fallout.
- Modify: `test/test_agent.py` (drop `_enforce_denied_commands` patches; assert no deniedCommands injection), `test/test_config_patch.py` (drop `enforce_denied_commands` key), `test/test_platform_context.py` + `test/test_cpp_wiring_{standalone,amazon}.py` (BASELINE_DENY now `()`), `website/src/pages/settings/SecurityPanel.test.tsx` (new)
- Delete: `test/test_enforce_denied_scope.py`

**Interfaces:** consumes all prior tasks.

- [ ] **Step 1: Parity test** (`test_denied_commands_security.py`): assert `{r.pattern for r in BUILTIN_DENIED_RULES}` equals the frozen golden set loaded from a committed `test/fixtures/denied_commands_golden.json` (copy of the 130 exact patterns), and 130 unique ids.
- [ ] **Step 2:** Fix the pre-existing tests that assert old semantics (list above).
- [ ] **Step 3: Frontend test** — toggle calls API, confirm modal gates disable, pinned rows locked + no mutation, add validates regex, delete only on user rows, status row shows `effective_count`.
- [ ] **Step 4: Full build gate:** `black src test && isort src test && flake8 src/kiro_crew test && mypy src/kiro_crew && python -m pytest --override-ini="addopts=" -p no:cacheprovider -q` and `cd website && npx tsc -b && npx vitest run`. All green.

---

## Task 10: Docs + specs

**Files:**
- Modify: `docs/system-specs/modules/security.md`, `docs/system-specs/modules/governance.md`, `docs/system-specs/modules/platform-context.md`, `CLAUDE.md`, `AGENTS.md`, `src/kiro_crew/docs/configuration.md`

**Interfaces:** consumes final code.

- [ ] **Step 1:** Apply the doc edits from the edit-contracts "docs" section: security.md (hooks-only + `DeniedCommandRule`/`BUILTIN_DENIED_RULES`, retire `_enforce_denied_commands` + `autoAllowReadonly`, new opt-out + read-only-auto-approve subsection, threat-model row, rule 6); governance.md (Plane A gate order, Plane B reinforcement, `commands` force-pin); platform-context.md (floor redefinition); `configuration.md` (drop `agent.enforce_denied_commands`); CLAUDE.md/AGENTS.md notes (keep generic-controls guidance).
- [ ] **Step 2:** `grep -rn "_enforce_denied_commands\|autoAllowReadonly\|enforce_denied_commands" docs/ src/kiro_crew/docs CLAUDE.md AGENTS.md` shows only intentional retirement mentions.

---

## Self-review notes

- **Spec coverage:** every spec section maps to a task (rules/resolver→T1, floor→T3, governance→T2, hooks/read-only→T4, retirement→T5, API→T6, client→T7, UI→T8, tests/parity→T9, docs→T10). ✓
- **Type consistency:** `denied_regexes` (T1/T3/T4), `compute_effective_denied` args (T1↔T4), `builtin_denied_rules()`/`pinned_builtin_command_ids()` (T1↔T6), `DeniedCommandsData` (T6↔T7↔T8), `hooks.denied_commands.{disable_all,disabled_ids,user_added}` (T4↔T6) all consistent. ✓
- **No placeholders:** exact code lives in the canonical spec + `rules-manifest.json` + `edit-contracts.md`; steps reference them by path with concrete anchors. ✓
