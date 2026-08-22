# Requirements Document

> Code citations name a file and a symbol, never a line number: the anchors in this spec drifted twice during implementation (once across 315 commits of `main`, once across 45), and a number that is wrong reads as authoritative.

## Introduction

Kiro Crew has no way to define user-supplied key/value pairs and reference them from the text it sends to an agent. Users working across several contexts — a dev API and a prod API, two ticket queues, two service endpoints — retype those values in every prompt and every cron message, or hard-code them into a skill.

Four halves of this feature already exist in the codebase and were never joined:

- A **value store with no management**: `KiroCrewConfig.load_credentials()` (`config/loader.py`) parses *every* `KEY=VALUE` line in `~/.kiro/crew/.env`, not just the 13 hard-coded credential keys, and `os.environ.setdefault`s all of them into every child process. There is no UI, CLI, API, schema, or scrub for user-added keys.
- A **`{{TOKEN}}` expansion mechanism with no user-supplied values**: `ContextBuilder._resolve_prompt_templates` (`context.py`) resolves `{{MAX_SUBAGENTS}}`, `{{VERBOSITY_BLOCK}}` and `{{WIDGET_BLOCK}}`, all gateway-computed. `render_nudge_message` (`dashboard/handlers/autonudge.py`) resolves `{{STOP_FILE}}` the same way.
- **Scope objects with nowhere to hang values**: a workspace (`WorkspaceConfig`, `config/loader.py`) is one field wide, and a crew (`KiroCrewAgentConfig`, `config/loader.py`) binds a kiro agent, workspace, memory store and model — and is switched from a live header dropdown via a route that re-derives bindings (`dashboard/chat_handlers.py`).
- A **layered-resolution precedent applied to exactly one thing**: `resolve_memory_store_config` (`config/loader.py`) merges a named store's non-empty fields over a top-level section.

This feature introduces **Crew Variables**: user-defined pairs declared at four scopes that cascade — global, workspace, crew, session — expanded as `{{name}}` in the text layer.

**Why a cascade rather than Postman-style switchable environments.** Postman invented named environments because a collection is not a context, so a switchable bag had to be bolted alongside. Kiro Crew already has real scope objects, so values hang on them directly and no second selector is introduced. A named-set indirection at the crew scope can be added later without a breaking change, since a crew's layer is already a bag of pairs; it is not in v1.

**Vocabulary.** The user-facing label is "Environment Variables", with the top-level section presented as "Global Environment Variables". The stored keys deliberately avoid the word `env` — the file is `variables.json` and the field is `variables` at every scope — because `env` already means the process environment, `mcpServers.env`, and `~/.kiro/crew/.env` in this codebase.

**Variables are stored in their own file, not in `config.json`.** That is a storage decision with requirement-level consequences, so it is stated here rather than only in the design: `KiroCrewConfig.save()` replaces the whole config document from an explicit key list, so any behaviour it could have for a `variables` slot is wrong in some way — serializing the merged value overwrites a base value the `config.local.json` overlay shadowed and the shadowed value is unrecoverable; preserving it under the config lock stalls the event loop, because `save()` is synchronous and reached from 13 async call sites; preserving it with an unlocked read drops a variables write that already returned 200. A dedicated `variables.json` beside `config.json` removes the choice instead of picking one of the three, and with it the overlay layer, the overlay-owned-key class, and the deleted-workspace resurrection window. The costs are real and stated in Requirements 9, 13 and 14: variables fall outside whatever backs up `config.json`, a hand-edit goes to a different file, and there is no machine-local overlay for them.

**Secrets are a decided non-goal for v1**, not a deferred question — see Requirement 13. The reason is measured: the agent env-scrub list `_AGENT_DENIED_ENV_KEYS` (`sandbox.py`) is a closed set of 13 literals, so a user-defined key is already readable today with a plain `printenv` inside an agent shell. Marking a variable "secure" in the UI without extending that machinery would be a false promise. Postman's own model confirms the shape of the real fix — secret-capability there is a store with an encryption key and an enforced `vault:` namespace, plus per-consumer opt-in before a script may read one, never a checkbox on an ordinary variable.

## Glossary

- **Variables_Store**: `variables.json` beside `config.json` — the only place a user-defined pair is persisted, and the only file the variables endpoint writes.
- **Global_Layer**: the `global` map in the Variables_Store.
- **Workspace_Layer**: the `workspaces.<name>` map in the Variables_Store, applied for a workspace the config defines.
- **Crew_Layer**: the `crews.<name>` map in the Variables_Store, applied for a crew the config defines.
- **Session_Layer**: transient pairs set on one chat session, never written to config.
- **Effective_Map**: the resolved `dict[str, str]` for a session — the four layers merged per key, narrowest winning.
- **Variable_Resolver**: new logic in `config/loader.py` producing the Effective_Map.
- **Variable_Expander**: new logic performing single-pass `{{name}}` substitution.
- **Reserved_Token**: an existing built-in `{{...}}` prompt token — `MAX_SUBAGENTS`, `VERBOSITY_BLOCK`, `WIDGET_BLOCK`, `STOP_FILE`, `ALIAS` — plus the single-brace `{bot_name}`.
- **Authored_Text**: text the **operator** typed or authored in Kiro Crew — a dashboard composer message, a cron job `message`, a monitor/auto-nudge instruction, the configured agent system prompt. Inbound channel text is deliberately NOT Authored_Text, because it is authored by a channel participant rather than by the operator; see Requirement 4.4, withdrawn.
- **Imported_Text**: text loaded from a file or registry rather than typed — a `SKILL.md` body, an `@prompt` file body, a steering file. May originate outside the user's control (a cloned repo, the public skill registry).
- **Participant_Text**: text arriving on an inbound channel transport (Slack, Discord, Telegram, Webex, WeCom, Teams). Authored by any of the configured `allowed_users`, not by the operator, and therefore never expanded.
- **Machine_Surface**: a non-prose consumer of config values — `mcpServers` `command`/`args`/`env`, an agent spec JSON, an app manifest.

## Requirements

### Requirement 1: Variable Definitions at Four Scopes

**User Story:** As a Kiro Crew user, I want to declare variables at the scope they actually belong to, so that a value shared by everything is written once and a value specific to one crew lives on that crew.

#### Acceptance Criteria

1. THE `KiroCrewConfig` dataclass SHALL carry a top-level `variables` field of type `dict[str, str]`, defaulting to empty, with `_meta` field metadata consistent with the surrounding dataclasses. The field is populated from the Variables_Store at load; it is not parsed from `config.json`.
2. THE `WorkspaceConfig` dataclass SHALL carry a `variables` field of type `dict[str, str]`, defaulting to empty, populated the same way.
3. THE `KiroCrewAgentConfig` dataclass SHALL carry a `variables` field of type `dict[str, str]`, defaulting to empty, populated the same way.
4. THE Variables_Store SHALL be one JSON object beside `config.json`, holding a flat `global` map plus `workspaces` and `crews` maps of name → pairs, and `KiroCrewConfig.to_dict()` SHALL NOT emit `variables` at any scope — so a whole-config `save()` can neither delete, overwrite nor resurrect a pair, and no `save()`-time preservation, locking or read-then-write window exists to get wrong.
5. THE Config_Loader SHALL accept a variable name matching `^[A-Za-z][A-Za-z0-9_]*$` and SHALL reject any other name with a warning naming the offending key and its scope, without failing the load.
6. WHEN a value is not a string, THE Config_Loader SHALL coerce a bool, int or float to its string form and SHALL reject any other type with a warning naming the offending key and scope.
7. THE Config_Loader SHALL cap a single value at 4096 characters, SHALL reject a value containing an ASCII control character other than tab, and SHALL reject a value containing the opening delimiter `{{`, in each case with a warning naming the key and scope. Refusing the delimiter is what makes expansion idempotent rather than merely single-pass per call: a message can legitimately cross more than one expansion boundary, and two single-pass calls in series would otherwise resolve a token that arrived from a value.
8. WHEN a pair is rejected under criteria 5 through 7, THE Config_Loader SHALL omit that single pair and SHALL retain every other pair at that scope and every other scope.
9. THE same validation rules SHALL apply identically at all four scopes, from one shared code path.
10. WHEN the Variables_Store is absent, unreadable, or not a JSON object, THE Config_Loader SHALL resolve no variables and SHALL warn, without raising — a broken store must not take the gateway down over an optional feature.
11. WHEN the Variables_Store names a workspace or crew the config does not define, THE Config_Loader SHALL skip that entry rather than materializing the name, so a stale entry is inert data and not a resurrected scope.
12. WHEN a write would have to replace a container that is present but not a mapping, THE SYSTEM SHALL refuse the write and name the dotted path, and SHALL NOT coerce or discard the value — a hand-written value is the only copy there is. Read tolerates what write refuses, deliberately: a read has no way to report the problem to anyone who can fix it, and a write does.
13. WHEN the Variables_Store cannot be parsed, THE SYSTEM SHALL fail the write rather than reset the document, so servicing one patch cannot delete every pair at every scope.
14. THERE SHALL BE no migration into the Variables_Store: no released version stored variables anywhere, so a `variables` key found in `config.json` is not a prior home to import from.

### Requirement 2: Layered Resolution

**User Story:** As a Kiro Crew user, I want a narrower scope to win over a broader one, so that I can set a default globally and override it for one crew without editing the default.

#### Acceptance Criteria

1. THE Variable_Resolver SHALL produce the Effective_Map by merging, in order, Global_Layer, then Workspace_Layer, then Crew_Layer, then Session_Layer, each overriding the previous per key.
2. THE merge SHALL be keyed on **key presence, not truthiness**: an empty string at a narrower scope is an intentional override to empty and SHALL NOT fall through to a broader scope. This differs deliberately from `resolve_memory_store_config` (`config/loader.py`), where an empty field means "inherit".
3. THE Workspace_Layer applied SHALL be that of the workspace the session actually resolved to, which `resolve_agent_bindings` (`config/loader.py`) already derives from the crew's binding or `default_workspace`.
4. WHEN a crew names a workspace absent from `workspaces`, THE Variable_Resolver SHALL apply the fallback workspace's layer, matching the warning-and-fall-back behavior `resolve_agent_bindings` already has for the workspace itself.
5. THE Variable_Resolver SHALL NOT recurse, and no value can carry a token to recurse on: a value containing `{{` is rejected by Requirement 1.7 before it reaches the Effective_Map.
6. WHEN every layer is empty, THE Variable_Resolver SHALL return an empty Effective_Map and THE Variable_Expander SHALL become a no-op leaving every string byte-identical.

### Requirement 3: Provenance

**User Story:** As a Kiro Crew user, I want to see which scope supplied each effective value, so that I can tell why a value is what it is without reading three config sections.

#### Acceptance Criteria

1. THE Variable_Resolver SHALL report, per key in the Effective_Map, which scope supplied the winning value and which scopes were shadowed.
2. ~~THE Variable_Resolver SHALL report, per key, which file supplied the value — `config.json` or `config.local.json`.~~ — **WITHDRAWN.** There is one file. The Variables_Store has no overlay layer, so the supplying scope is the whole of a pair's provenance and a per-file attribution has nothing to distinguish.
3. `resolve_agent_bindings` SHALL report the resolved Effective_Map alongside the workspace and memory store it already returns.
4. THE provenance computation MAY share the resolution path rather than being a separate colder one, because winning scope and shadowing fall out of the merge itself. No re-read of a raw document is needed now that criterion 2 is withdrawn.
5. THE provenance path SHALL NOT be invoked per message.

### Requirement 4: Expansion in Authored Text

**User Story:** As a Kiro Crew user, I want `{{baseUrl}}` in what I type to be replaced by the effective value, so that I stop retyping the same values.

#### Acceptance Criteria

1. THE Variable_Expander SHALL recognize `\{\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\}\}` and SHALL replace a matched token with the Effective_Map value for that name.
2. THE SYSTEM SHALL expand tokens in the configured agent system prompt, applied where both prompt branches converge (after `_load_agent_prompt`, `context.py`) so a custom agent's prompt is covered as well as a built-in one.
3. THE SYSTEM SHALL expand tokens in a chat message the user submits from the dashboard composer.
4. ~~THE SYSTEM SHALL expand tokens in an inbound message from Slack, Discord, Telegram, Webex, WeCom or Teams.~~ — **WITHDRAWN.** THE SYSTEM SHALL NOT expand tokens in an inbound message from any channel transport (Slack, Discord, Telegram, Webex, WeCom, Teams). A variable's value is operator configuration, but inbound channel text is Participant_Text — authored by a channel participant, and `allowed_users` admits several people — so expanding it let anyone permitted to message the bot read operator config by sending `{{NAME}}` and reading the reply. The disclosure does not depend on the values being secrets: the operator never opted into publishing them. Restoring this criterion requires a trustworthy operator-vs-participant identity at the dispatch boundary, which the transport layer does not carry; that is a security design decision, not plumbing. Expansion is confined to the operator-authored surfaces in criteria 2, 3, 5 and 6 of this requirement.
5. THE SYSTEM SHALL expand tokens in a cron job's `message` at dispatch time, not registration time, so changing a variable changes what the next run receives.
6. THE SYSTEM SHALL expand tokens in a `monitor_start` loop instruction before the existing `{{STOP_FILE}}` pass (`dashboard/handlers/autonudge.py`).
7. THE Variable_Expander SHALL be single-pass: a substituted value SHALL NOT be rescanned.
8. THE SYSTEM SHALL persist the user's original text with tokens intact in session history; expansion is a send-time transformation.

### Requirement 5: Refusal to Expand in Imported Text and Machine Surfaces

**User Story:** As a Kiro Crew user, I want a skill I installed from the public registry to be unable to read my variables, so that a third-party skill cannot become an exfiltration primitive.

#### Acceptance Criteria

1. THE SYSTEM SHALL NOT expand tokens in a `SKILL.md` body, including one appended by `$skill` expansion (`dashboard/chat_runner.py`) or fetched by `skill_fetch`.
2. THE SYSTEM SHALL NOT expand tokens in an `@prompt` file body (`dashboard/chat_runner.py`).
3. THE SYSTEM SHALL NOT expand tokens in a steering file loaded by `_load_steering_resources` (`context.py`).
4. THE SYSTEM SHALL NOT expand tokens in any Machine_Surface, preserving the rule stated in the `_placeholder_values` docstring (`apps/bridges.py`) that rendered agent JSON must not draw values from a writable location.
5. WHEN Imported_Text contains a token, THE SYSTEM SHALL leave it byte-identical and SHALL NOT log the token's name at INFO or above.
6. THE SYSTEM SHALL enforce criteria 1 through 4 structurally, by expanding only an explicitly passed Authored_Text string, and SHALL NOT rely on scanning for markers.
7. THE repository SHALL carry a test asserting a variable's value is absent from the prompt assembled for a session whose only reference to it is inside a `SKILL.md` body.

### Requirement 6: Reserved Names

**User Story:** As a Kiro Crew user, I want a clear warning when I name a variable the same as a built-in token, so that I do not silently shadow gateway behavior.

#### Acceptance Criteria

1. THE Config_Loader SHALL treat `MAX_SUBAGENTS`, `VERBOSITY_BLOCK`, `WIDGET_BLOCK`, `STOP_FILE`, `ALIAS` and `bot_name` as Reserved_Tokens.
2. WHEN any scope defines a key equal to a Reserved_Token, THE Config_Loader SHALL omit that pair with a warning naming the key, the scope, and the reason.
3. THE Variable_Expander SHALL run after every Reserved_Token pass, so a user variable can never alter a built-in substitution.
4. THE Reserved_Token list SHALL live in one module-level constant, and a test SHALL assert it covers every `{{...}}` literal present in `context.py`, `dashboard/handlers/autonudge.py` and `slack_manifest.py`.

### Requirement 7: Unresolved Tokens Stay Literal

**User Story:** As a Kiro Crew user, I want a misspelled variable to be obvious rather than silently empty, so that I notice before the agent acts on a truncated instruction.

#### Acceptance Criteria

1. WHEN a token names a variable absent from the Effective_Map, THE Variable_Expander SHALL leave the token byte-identical.
2. THE Variable_Expander SHALL NOT substitute an empty string for an unknown name.
3. THE Variable_Expander SHALL return the set of unresolved names encountered.
4. WHEN at least one token is unresolved in a dashboard chat message, THE SYSTEM SHALL surface the unresolved names in the session, exactly once per message.
5. THE Variable_Expander SHALL leave a malformed token such as `{{ }}`, `{{1abc}}` or `{{a-b}}` byte-identical and SHALL NOT report it as unresolved.

### Requirement 8: No Token Escalation

**User Story:** As a Kiro Crew user, I want a variable's value treated as plain text, so that a value cannot reach in and load a skill or prompt file I did not ask for.

#### Acceptance Criteria

1. WHEN a value contains a `$name` sequence matching `_DOLLAR_SKILL_PATTERN` (`skills.py`), THE SYSTEM SHALL NOT load the named skill as a result of the substitution.
2. WHEN a value contains an `@name` sequence, THE SYSTEM SHALL NOT inline the named prompt file as a result of the substitution.
3. THE turn pipeline SHALL resolve `$skill` and `@prompt` references against the pre-expansion text, so criteria 1 and 2 hold structurally rather than by sanitizing values.
4. THE repository SHALL carry a test for each of criteria 1 and 2 naming a real installed skill and prompt and asserting it was not loaded.
5. THE Variable_Expander SHALL NOT strip or escape characters within a value, since criteria 1 through 3 remove the need to.

### Requirement 9: Variables Have No Machine-Local Overlay

**User Story:** As a Kiro Crew user working from a shared config, I want to know whether a variable can be overridden on this machine only, so that I do not plan around an override the storage does not offer.

#### Acceptance Criteria

1. ~~THE SYSTEM SHALL allow `variables`, `workspaces.<name>.variables` and `agents.<name>.variables` to be set in `config.local.json`.~~ — **WITHDRAWN.** THE SYSTEM SHALL NOT read variables from `config.json` or `config.local.json` at any scope. The overlay is half of what made an in-config `variables` map unsafe to write: because the overlay wins at load, a base value the overlay shadows is absent from the merged view, so a whole-document `save()` could neither preserve it nor recover it. The Variables_Store has one layer and one writer.
2. ~~WHEN the same variable at the same scope is defined in both files, THE SYSTEM SHALL use the `config.local.json` value.~~ — **WITHDRAWN**, following 9.1. There is no second file to win, and therefore no class of key the endpoint can read but not write.
3. ~~THE CLI SHALL accept a `--local` flag on its writing verbs.~~ — **WITHDRAWN.** A variables verb SHALL NOT offer `--local`; there is one destination.
4. ~~THE SYSTEM SHALL surface the supplying file per effective value.~~ — **WITHDRAWN**, see Requirement 3.2.
5. THE Reserved_Token rejection and every Requirement 1 validation rule SHALL apply on both the read path and the write path, because the Variables_Store is hand-editable and a hand edit reaches the reader without passing the endpoint.
6. WHEN a cron job or monitor loop resolves variables, THE SYSTEM SHALL use the same Effective_Map a dashboard session for that crew would.
7. THE cost of 9.1 SHALL be stated rather than left implicit: the shared-vs-machine-local split every other config key gets is not available for a variable. A value that must differ per machine has to differ by crew or workspace, or be hand-edited into that machine's Variables_Store.

### Requirement 10: CLI Surface

**User Story:** As a Kiro Crew user, I want to manage variables from the command line, so that I can script them and use them headlessly.

#### Acceptance Criteria

1. THE CLI SHALL provide `kirocrew vars list`, printing the Effective_Map with each value's winning scope, and accepting `--workspace` / `--agent` to resolve as that context would.
2. THE CLI SHALL provide `kirocrew vars set KEY VALUE` and `kirocrew vars unset KEY`, each accepting a scope selector — global by default, `--workspace NAME` or `--agent NAME`. There is no `--local`; see Requirement 9.3.
3. THE CLI SHALL provide `kirocrew vars show KEY`, listing the value at every scope that defines it and marking which one wins.
4. WHEN a scope selector names a workspace or crew that does not exist, THE CLI SHALL exit non-zero listing the available names and SHALL NOT write the Variables_Store.
5. THE CLI SHALL apply a per-key patch through the same single store writer the HTTP route uses, preserving every key it was not asked to change, and SHALL NOT replace a scope wholesale.

### Requirement 11: HTTP and UI Surface

**User Story:** As a Kiro Crew user, I want to edit variables in the dashboard and see which scope a value came from, so that I do not hand-edit JSON.

#### Acceptance Criteria

1. THE dashboard SHALL expose `GET /api/variables` returning the pairs the Variables_Store holds per scope, the resolved Effective_Map for the active context, and per-key winning scope and shadowed scopes. THE per-scope maps SHALL come from the store rather than from the resolved cascade, and the workspace and crew maps SHALL be keyed off the names the CONFIG defines, so a stale store entry is never advertised as editable.
2. THE dashboard SHALL expose `PUT /api/variables` applying a PER-KEY patch at a named scope — a `set` object of pairs to write and/or a `delete` array of names to remove — and SHALL NOT accept a whole-scope replacement. A key nobody names is neither read nor rewritten, so two writers touching different keys cannot lose each other's edits, and a value the client could not see cannot be dropped by echoing a map back.
3. THE `PUT` SHALL treat deletion as an explicit verb rather than absence from a map, keeping the empty string unambiguous: it is a legal value that still overrides a broader scope, so it cannot share an encoding with "unset".
4. THE `PUT` SHALL refuse rather than drop an invalid pair — a dashboard write is interactive, and silently discarding a pair the user just typed looks like a save that worked — returning 400 with the offending key named and a machine-readable `code`.
5. THE `PUT` SHALL return 400 for: malformed JSON, a non-object body, a scope outside the writable set, a missing or wrongly-typed `set`/`delete`, an invalid name or value, a key that is both set and deleted in one request, a workspace the config does not define, and a store container it would have to replace that is not a mapping. It SHALL return 500 when the store cannot be parsed, without resetting it.
6. THE writable scopes SHALL be global and workspace. Crew pairs are stored and resolved but are not yet editable through this endpoint; the crew form that would edit them is criterion 9 of this requirement.
7. THE Settings UI SHALL provide an **Environment Variables** panel whose primary section is **Global Environment Variables**, with an editable pair table and add/delete affordances.
8. THE Settings panel SHALL also present per-workspace pairs, so a user can manage a workspace's layer without visiting another page.
9. THE crew form on the Agents page SHALL provide the crew's own pairs beside the existing workspace, memory-store and model fields.
10. THE UI SHALL show, per effective value, the scope that supplied it and whether it is shadowing a broader scope.
11. Every new user-visible string SHALL be added to all locale catalogs and SHALL pass `I18N_BASE_REF=origin/main npm run i18n:check` and the render gate.
12. THE panel SHALL be reachable from the command palette, and any surface hidden from the nav SHALL carry the matching `EXTRA_PAGES` entry.
13. THE panel SHALL be keyboard operable, SHALL associate every input with a visible label, and SHALL use lucide icons rather than emoji.

### Requirement 12: Diagnostics

**User Story:** As a Kiro Crew user, I want Kiro Crew to tell me when my variables are misconfigured, so that I find out before a cron job runs with a literal token in it.

#### Acceptance Criteria

1. `kirocrew doctor` SHALL report the Effective_Map size per configured crew and every pair rejected under Requirements 1 and 6, naming key, scope and reason.
2. `kirocrew doctor` SHALL report each cron job whose `message` references a name absent from that job's crew's Effective_Map, naming the job and the token.
3. THE dashboard composer SHALL indicate an unknown `{{name}}` before submission, reusing the token-scanning approach of `website/src/components/composerTokens.ts`.
4. THE SYSTEM SHALL NOT include any variable's value in diagnostic output; it SHALL name keys and scopes only.

### Requirement 13: Secrets Are Out of Scope for v1

**User Story:** As a Kiro Crew user, I want Kiro Crew to be honest that this feature is not for secrets, so that I do not put a credential somewhere it is not protected.

#### Acceptance Criteria

1. THE feature SHALL NOT provide a secret, secure, masked or encrypted variable type.
2. THE UI SHALL state plainly, in the Environment Variables panel, that variables are not for secrets and are stored in plain text in `variables.json`.
3. THE documentation SHALL state the same, SHALL name the file, and SHALL name where a credential does belong today.
4. THE feature SHALL NOT introduce any new value into a child process environment, and SHALL NOT read from or write to `~/.kiro/crew/.env` or any credential store.
5. THE feature SHALL NOT expand a token in any Machine_Surface, so a variable cannot become an `mcpServers` credential by another route.
6. THE Variables_Store SHALL remain forward-compatible with a future secret store: because a secret would arrive under its own reference namespace and its own store, no field added by this feature needs to change to accommodate one.
7. No requirement in this document SHALL be satisfied by presenting a variable as more protected than plain text in a file the operator can edit. Note the file mode is tightened to 0600 on write as defence in depth, and a filesystem that refuses the `chmod` does not fail the write — the mode is not the boundary, and the values are declared non-secret.

### Requirement 14: Non-Functional Requirements

#### Acceptance Criteria

1. **Performance.** THE Variable_Resolver SHALL NOT read config from disk per message; it SHALL resolve from the already-loaded config object on the existing per-session path. THE Variable_Expander SHALL compile its pattern once at module level.
2. **Performance.** WHEN the Effective_Map is empty, THE Variable_Expander SHALL return the input string object without scanning it.
3. **Security.** THE threat model SHALL be that a value is equivalent in trust to text the **operator** typed themselves — which holds only because Requirement 5 confines expansion away from Imported_Text and Machine_Surfaces, Requirement 4.4 is withdrawn so no Participant_Text is expanded, and Requirement 8 prevents escalation.
4. **Backward compatibility.** WHEN no Variables_Store exists — the state of every install that predates this feature — THE Config_Loader SHALL load the config unchanged, resolve an empty Effective_Map, and leave every string byte-identical. The store is created by its first write.
5. **Backward compatibility.** THERE SHALL BE no migration, and the absence is not an omission: no released version stored variables in `config.json`, `config.local.json` or anywhere else, so there is no prior location to read and nothing a user can have written that this feature would strand. A `variables` key hand-added to `config.json` is not the Global_Layer and is not read.
6. **Durability.** THE cost of a dedicated file SHALL be stated where users see it: `variables.json` is outside `config.json`, so a backup, snapshot or sync that covers `config.json` does not cover variables unless it covers the whole data directory, and a hand-edit goes to `variables.json` rather than to the config the user already knows.
7. **Observability.** THE SYSTEM SHALL log at DEBUG, once per message, the count of tokens expanded and unresolved, and SHALL NOT log values.
