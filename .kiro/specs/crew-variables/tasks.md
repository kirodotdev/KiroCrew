# Implementation Plan

## Status

Shipped: the lexical core, the dedicated variables store (`variables.json` beside
`config.json`) with the three scope containers and its single per-key writer, the
four-scope resolution that reads it, the four operator-authored expansion boundaries
(agent system prompt, dashboard composer message, cron dispatch, monitor loop), the
caller-controlled assembly that keeps imported bodies out of expansion, the security
ratchets, `GET`/`PUT /api/variables`, and the Settings panel for the global and
per-workspace layers.

Storage: variables are NOT in `config.json`. `KiroCrewConfig.to_dict()` emits no
`variables` at any scope, so `save()` cannot delete, overwrite or resurrect a pair —
which is why the save-neutrality machinery, the overlay-owned-key refusal
(`variables_overlay_owned`), the deleted-workspace resurrection guard, the
malformed-workspace-entry refusal and the `_subtract_overlay` exemption are all gone
rather than reimplemented. Malformed-CONTAINER refusal survives and lives in the
store. The costs: variables fall outside a backup that covers only `config.json`, a
hand-edit goes to `variables.json`, there is no machine-local (`config.local.json`)
layer for them, and there is no migration because no released version stored them
anywhere.

Withdrawn: inbound channel message expansion (task 10, Requirement 4.4). A variable's
value is operator configuration, but inbound channel text is authored by a channel
participant and `allowed_users` admits several people, so expanding it let anyone
permitted to message the bot read operator config by sending `{{NAME}}`. No transport
expands, on any channel; a ratchet test holds the refusal.

Also withdrawn: per-file provenance (Requirement 3.2) and the `config.local.json`
overlay for variables (Requirement 9.1-9.4). Both existed only because the pairs lived
in a two-layer config document.

Deferred, each independent of the above: the CLI verb group (task 14), the crew-form
pairs and composer hint (17), `doctor` reporting (18), the user-facing docs page (19),
and the session layer (20).


> Code citations name a file and a symbol, never a line number: the anchors in this spec drifted twice during implementation (once across 315 commits of `main`, once across 45), and a number that is wrong reads as authoritative.

Each task is one focused, verifiable step that builds on the previous ones. A task is not
complete until its verification passes and each new behavior has been revert-verified
(break it, watch the test fail, restore).

Backend gates at CI parity from the repo root: `isort --check-only src/kiro_crew test`,
`flake8 src/kiro_crew test`, `mypy src/kiro_crew/`. Frontend gates from `website/`:
`npx tsc -b`, `npx vitest run --no-coverage`, and after `git fetch origin`,
`I18N_BASE_REF=origin/main npm run i18n:check` plus the render gate with the same base ref.

## Phase 1 — Lexical core

- [x] 1. Create `src/kiro_crew/variables.py` as a leaf module
  - `RESERVED_TOKENS`, `NAME_RE`, `TOKEN_RE`, `MAX_VALUE_LEN`.
  - `validate_pair(key, value)` returning `(key, coerced)` or `(None, reason)`, covering invalid name, reserved name, non-coercible type, oversize, and control characters other than tab.
  - `expand(text, values) -> tuple[str, frozenset[str]]` using a single `TOKEN_RE.sub` with a replacement **callable**, returning the input object unchanged when `values` is empty.
  - Import nothing from `kiro_crew`, so `config/loader.py`, `context.py`, `dashboard/chat_runner.py`, `cron.py` and the autonudge handler can all import it without a cycle.
  - _Requirements: 1.5, 1.6, 1.7, 2.5, 4.1, 4.7, 6.1, 7.1, 7.2, 7.3, 7.5, 14.1, 14.2_

- [x] 2. Unit-test the lexical core
  - Grammar: valid names, whitespace inside braces, and `{{ }}` / `{{1abc}}` / `{{a-b}}` left byte-identical and not reported unresolved.
  - Single pass: a value containing `{{other}}` where `other` is also defined stays literal.
  - Replacement safety: values containing `\1` and `\g<0>` inserted verbatim.
  - Empty mapping returns the identical object (assert with `is`).
  - One test per `validate_pair` rejection reason.
  - A test scanning `src/kiro_crew/context.py`, `src/kiro_crew/dashboard/handlers/autonudge.py` and `src/kiro_crew/slack_manifest.py` for `{{...}}` literals, asserting every name found is in `RESERVED_TOKENS`.
  - _Requirements: 6.4, 7.5, 14.1_

## Phase 2 — Schema and resolution

- [x] 3. Add the variables store and the three-scope schema
  - New `src/kiro_crew/config/variables_store.py`: `variables.json` beside `config.json`, holding `global` plus `workspaces`/`crews` maps; path derived from `config_path()` so a relocated or test-redirected root carries the store with it; `loader` imported lazily since `loader` imports this module.
  - `read_store()` never raises — missing file, bad JSON, OS error or non-object all resolve to no variables with a warning, because a broken store must not fail a gateway boot over an optional feature.
  - `patch_store()` as the ONLY writer: a per-key patch through `update_config_locked(..., stamp_meta=False, on_corrupt="fail")`, inheriting its lock, atomic replace, mode preservation and symlink handling; raises `MalformedStore(path)` for a present-but-non-mapping container; tightens the file to 0600 and does not fail the write if `chmod` is refused.
  - `variables: dict[str, str]` on `KiroCrewConfig`, `WorkspaceConfig` and `KiroCrewAgentConfig` (`config/loader.py`), each with `_meta` metadata, populated by `_apply_variables_store` after load — skipping any store entry naming a workspace or crew the config does not define.
  - `to_dict()` emits no `variables` at any scope (`_no_vars` filter on each nested `asdict`), and `save()` carries no variables handling at all: nothing to preserve, no lock to take, no read-then-write window. This is the fix for the three-round defect, not a mitigation of it.
  - Route every pair at every scope through `variables.validate_pair` from one shared code path, dropping a rejected pair with a WARNING naming key, scope and reason while retaining the rest.
  - Regenerate `config-baseline.json` and confirm the new `_meta` entries appear.
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.8, 1.9, 1.10, 1.11, 1.12, 1.13, 1.14, 14.4, 14.5_

- [x] 4. Implement layered resolution
  - `VariableResolution` (`values`, `winning_scope`, `shadowed`, `rejected`) and `resolve_variables(config, agent=None, session_overrides=None)`.
  - Merge global → workspace → crew → session, keyed on **key presence, not truthiness**, so an empty string at a narrow scope wins over a non-empty broad one.
  - Take the workspace layer from the workspace the session actually resolved to, reusing `resolve_agent_bindings` (`config/loader.py`) including its existing warn-and-fall-back for a crew naming a missing workspace.
  - Extend `resolve_agent_bindings` to report the resolution alongside workspace and memory store.
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.6, 3.3, 14.1_

- [ ] 5. Test resolution and the store
  - **Outstanding at the current head.** `test/test_variables_scopes.py` still asserts the deleted in-config machinery: `TestAWholeConfigSaveIsVariablesNeutral` reaches for `loader._preserve_base_variables` and for the documented read-then-write residual, `TestWorkspaceMigrationCarriesVariables` expects `_migrate_workspaces` to carry `variables`, and `TestCrewVariablesRoundTrip` expects `to_dict()` to emit it. Those classes describe a design that no longer ships and are replaced by the assertions below, not repaired.
  - `variables` is REMOVED from `_KNOWN_CONFIG_SECTIONS` (`config/loader.py`), so this core treats it as an unmodelled section: a `variables` key still present in a hand-edited `config.json` is captured into `_extra_sections` and round-tripped verbatim rather than deleted. An earlier revision kept the entry and documented the deletion as intended; that was wrong. Nothing parses the key any more, so preserving it is inert, whereas deleting it is silent data loss for anyone who followed earlier `config.json` guidance. Pinned by `test_a_legacy_variables_section_round_trips_instead_of_vanishing`.
  - Each layer alone; each adjacent pair overriding; a stack where all layers define the same key.
  - Empty string at crew scope beating a non-empty global.
  - Missing-workspace fallback with the warning asserted; an unknown crew name falling back to the default crew with the warning asserted.
  - Empty config resolves empty and leaves strings byte-identical.
  - Store round trip; a first write creating an absent container; a per-key patch leaving unnamed keys untouched; a `delete` removing only its key; `MalformedStore` raised with the right dotted path and nothing changed on disk; an unparseable store failing a write rather than being reset; an unreadable store resolving to no variables without raising; a store entry for an undefined workspace skipped rather than materialized.
  - Assert `to_dict()` emits no `variables` at any scope and that a whole-config `save()` leaves `variables.json` byte-identical — the assertion that would catch a regression back into the save-time trilemma.
  - Derive every path from `tmp_path`; never hard-code a bare absolute path such as `/x`, which resolves to a different drive on Windows CI.
  - _Requirements: 1.4, 1.10–1.14, 2.1–2.6, 14.4_

- [ ] 6. Report provenance from the merge
  - Winning scope and shadowed scopes come from the merge itself and are already reported on `VariableResolution`; expose them wherever a surface shows a value (CLI, `doctor`).
  - The per-file half is WITHDRAWN with Requirement 3.2: there is one store file and no overlay, so `variable_sources()` and its re-read of two raw config documents are not built.
  - Test shadowing lists, and that a Reserved_Token is rejected on the read path and the write path alike.
  - _Requirements: 3.1, 3.2 (withdrawn), 3.4, 3.5, 9.5_

## Phase 3 — Expansion at each boundary

- [x] 7. Expand the agent system prompt
  - Apply expansion in `src/kiro_crew/context.py` where both prompt branches converge (after `_load_agent_prompt`, at the existing `_resolve_prompt_templates` / `_substitute_bot_name` call site), so a custom agent's prompt is covered too.
  - Order it after every Reserved_Token pass.
  - Test: a variable expands for both a built-in and a custom agent; a variable named after a Reserved_Token cannot change that token's value.
  - _Requirements: 4.2, 6.3_

- [x] 8. Refactor `chat_runner` to caller-controlled assembly
  - Split `_expand_prompt_mention` (`dashboard/chat_runner.py`) and `_expand_dollar_skills` (`:2743-2812`) into parts-returning forms — `(authored, imported_blocks, status_or_count)` — leaving their resolution logic and existing gates unchanged.
  - Assemble at the call site: resolve `@prompt`, then `$skill`, then expand variables over the authored segment only, then join.
  - Preserve the existing `prompt_expanded` / `is_slash` / `_prompt_depth` gating and the SEL `skill_dollar_expansion` audit call.
  - Test that the assembled message is byte-identical to today's output when no variable is defined, proving the refactor behavior-preserving.
  - _Requirements: 4.3, 4.8, 5.1, 5.2, 5.6, 8.3_

- [x] 9. The three security tests
  - A value is absent from the assembled prompt when the only reference is inside a `SKILL.md` body.
  - A value of `$<a real installed skill>` does not cause that skill to load.
  - A value of `@<a real prompt file>` does not cause that file to be inlined.
  - Each asserts on the assembled text, not an internal flag. Revert-verify all three.
  - _Requirements: 5.1, 5.2, 5.7, 8.1, 8.2, 8.4, 8.5_

- [ ] 10. ~~Expand inbound channel messages~~ — WITHDRAWN, and the reversal is the point
  - Inbound channel text is NOT expanded, on any transport. A variable's value is OPERATOR configuration; inbound text is authored by a channel participant, and `allowed_users` admits several people. Expanding it lets anyone permitted to message the bot read operator config by sending `{{NAME}}` and reading the reply — a disclosure that does not depend on the values being secrets, because the operator never opted into publishing them.
  - Requirement 4.4 is therefore withdrawn rather than satisfied. Restoring it needs a trustworthy operator-vs-participant identity at the dispatch boundary, which this layer does not carry; that is a security design decision, not plumbing.
  - Expansion is confined to operator-authored text: the dashboard composer, the agent system prompt, a cron message, a monitor instruction.
  - `test_variables_channels.py::TestNoInboundTransportExpands` is the ratchet. It enumerates all five modules and fails if any regains an expander — the earlier version of this task asserted the opposite, and widening coverage to Discord and Telegram widened the disclosure.
  - _Requirements: 4.4 (withdrawn)_

- [x] 11. Expand cron messages and monitor instructions
  - Expand a cron job's `message` at dispatch in `cron.py` using that job's crew's Effective_Map, leaving the stored job unchanged.
  - Expand a monitor loop instruction in `dashboard/handlers/autonudge.py` before the `{{STOP_FILE}}` replace.
  - Test: editing a variable changes what the next cron run receives; the stored `message` still contains the token; `{{STOP_FILE}}` still resolves.
  - _Requirements: 4.5, 4.6, 6.3, 9.6_

- [x] 12. Add the source guard
  - A test enumerating the expansion boundaries that fails when a new `build_message` caller appears without one, following the countable-guard pattern already used for armed-resource release paths.
  - The inbound transports are guarded in the OPPOSITE direction by task 10's ratchet (`test_variables_channels.py::TestNoInboundTransportExpands`), which fails if a transport dispatch ever *gains* an expander. A newly added transport dispatch must be added to that ratchet, not given a boundary.
  - _Requirements: 5.6, 13.5_

- [x] 13. Add "leave Imported_Text alone" coverage
  - Assert no expansion in a steering file loaded by `_load_steering_resources`, and none in `mcpServers` `command`/`args`/`env`, an agent spec JSON, or an app manifest.
  - Assert an unresolved token in Imported_Text is not logged at INFO or above.
  - _Requirements: 5.3, 5.4, 5.5, 13.4, 13.5_

## Phase 4 — Surfaces

- [ ] 14. CLI verb group
  - `kirocrew vars list|show|set|unset` with `--workspace` / `--agent` scope selectors (global by default), modeled on the `workspace` group (`cli.py`, `cli_commands.py`). No `--local`: the store has one layer, so there is no machine-local destination to route a write to.
  - `vars list` prints the effective map with each value's winning scope; `vars show KEY` lists the value at every scope and marks the winner.
  - A selector naming a nonexistent workspace or crew exits non-zero listing available names and writes nothing.
  - Write through `variables_store.patch_store` rather than any config writer, so the CLI and the endpoint share one writer and one lock.
  - Test each verb, the unknown-scope paths, and that keys the verb did not name survive a write.
  - _Requirements: 9.3 (withdrawn), 10.1, 10.2, 10.3, 10.4, 10.5_

- [x] 15. HTTP routes
  - `GET /api/variables` and `PUT /api/variables` beside the existing config routes, under the same auth.
  - `GET` reports the STORE's pairs per scope (not the resolved cascade), keying the workspace and crew maps off the CONFIG's names so a stale store entry is never advertised as editable, plus the effective map, winning scope and shadowed scopes.
  - `PUT` is a PER-KEY patch — `set` object and/or `delete` array at one scope — never a whole-scope replace: the replace form drew a data-loss finding in three consecutive rounds because a client had to echo back a map it had read. Writable scopes are global and workspace.
  - Refuse rather than drop an invalid pair, with a machine-readable `code` on every 400 (`variables_invalid_json`, `_invalid_body`, `_invalid_scope`, `_invalid_values`, `_invalid_pair`, `_conflicting_change`, `_unknown_workspace`, `_malformed_container`) and `config_corrupt` 500 for an unparseable store, which is failed rather than reset. Log every refusal through the SEL helper, including the delete-side one.
  - Dispatch the write through `run_config_write` so the store's advisory lock is taken inside the worker thread and never on the event loop.
  - Test each route including every rejection shape, a set/delete round trip, and that a key not named in the request is unchanged.
  - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6_

- [x] 16. Settings Environment Variables panel
  - New `website/src/pages/settings/VariablesPanel.tsx`, registered in `website/src/pages/SettingsPage.tsx` (one import, one registry entry in `GROUP_PREFERENCES` beside Skills, one switch line), leading with a **Global Environment Variables** section and also listing per-workspace pairs.
  - Show each row's winning scope and whether it shadows a broader scope.
  - State plainly in the panel that variables are not for secrets and are stored in plain text in `variables.json`. The locale string `variables.plain_text_note` names `variables.json` in all 12 hand-maintained catalogs and in `en.context.json`; `en-XA` is regenerated. The filename is kept verbatim in every language.
  - Use lucide icons, no emoji. Add every string to all locale catalogs, preserving each catalog's key order and appending new keys at the end.
  - Ensure command-palette reachability, adding an `EXTRA_PAGES` entry if the surface is hidden from the nav.
  - Call any new api client method defensively at mount, since many test files partially mock `../src/api/client`.
  - Test render, add/edit/delete at global and workspace scope, validation surfacing, scope indicators, keyboard operation and label association. Run the full frontend suite.
  - _Requirements: 11.7, 11.8, 11.10, 11.11, 11.12, 11.13, 13.2_

- [ ] 17. Crew-form pairs and composer hint
  - Add the crew's own pairs to the crew form in `website/src/pages/KiroCrewAgentsPage.tsx`, beside the existing workspace, memory-store and model fields. This needs `PUT /api/variables` to accept the crew scope first — the endpoint's writable set is global and workspace today, while the store and the resolver already handle crew pairs.
  - Flag an unknown `{{name}}` in the composer before submission, reusing `website/src/components/composerTokens.ts`, and surface unresolved names once per submitted message.
  - Test both, including that a known variable produces no warning.
  - _Requirements: 7.4, 11.6, 11.9, 12.3_

- [ ] 18. Doctor diagnostics
  - Report the Effective_Map size per configured crew and every rejected pair with key, scope and reason.
  - Report each cron job whose `message` references a name absent from that job's crew's Effective_Map, naming job and token.
  - Never print a value. Test both reports and assert no value appears in the output.
  - _Requirements: 12.1, 12.2, 12.4, 14.7_

## Phase 5 — Close out

- [ ] 19. Documentation
  - Document the three persisted scopes, the cascade order, the CLI verbs, and the expansion surfaces — including, explicitly, the surfaces that do not expand and why.
  - Name `variables.json` as the file, say that it is not `config.json` and therefore not covered by a backup of that file alone, and say that a hand-edit goes there. Say there is no machine-local overlay for variables, and that there is no migration because no released version stored them anywhere.
  - State that variables are not for secrets, name where a credential does belong today, and record that a future secret store would arrive under its own reference namespace rather than as a flag here.
  - _Requirements: 5.4, 9.7, 13.1, 13.3, 13.6, 13.7, 14.6_

- [ ] 20. Session layer (last, and droppable)
  - Slot-scoped override dict plus `PUT /api/chat/slots/{slot}/variables`, transient and never written to config, modeled on the per-session model override; a small chat-header control to set it.
  - Ordered last deliberately: it is the mitigation for the cascade having no one-click context flip, and cutting it removes no other layer's behavior.
  - Test that a session override wins over the crew layer and does not persist across sessions.
  - _Requirements: 2.1, 11.1_

- [ ] 21. Full gate run and revert-verification sweep
  - Run every backend and frontend gate listed at the top of this plan, at CI parity with the base-ref environment variables set, plus `HARNESS_BASE_REF=origin/main python3 scripts/check_harness_parity.py`.
  - Re-confirm each security test from task 9 and the source guard from task 12 by reverting its fix and watching it fail.
  - Verify an install with no `variables.json` loads unchanged and leaves every assembled string byte-identical, and that a `variables` key hand-added to `config.json` changes nothing.
  - Verify a whole-config `save()` leaves `variables.json` byte-identical, and that `to_dict()` emits no `variables` at any scope — the one assertion that would catch a regression back into the save-time trilemma.
  - Verify no new value reaches a child process environment and no credential store is read or written.
  - _Requirements: 1.4, 13.4, 14.4, 14.5, and every requirement in 5 and 8_
