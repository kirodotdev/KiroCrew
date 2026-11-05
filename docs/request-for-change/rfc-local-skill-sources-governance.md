---
title: Local skill sources under operator policy, an allow-only skills.local_sources key over project skill directories, extra skill roots, skill mappings and the KIROCREW_PROJECT_DIR source
status: draft
author: hkrishg-aws
created: 2026-11-05
last-audited: 2026-11-05
audited-at: 19e077655d65862aef110ce9aa8b9928b8881e97
doc-pr: null
implementation-prs: []
tracking-issues: [29686]
supersedes: []
superseded-by: []
---

# RFC: Local skill sources under operator policy

- Status: draft. Proposed for maintainer review; nothing is built, and this document changes no code.
- Tracking issue: [#29686](https://github.com/kirodotdev/KiroCrew/issues/29686).
- Design for the "Local skill sources" row of the accepted [rfc-crew-enterprise-edition](rfc-crew-enterprise-edition.md) appendix, policy term `crew_extension_local_sources`. That appendix had one "Extension sources" row mapping both halves to `external_access.registry_hosts`, which is only the registry half (proposed in [#28530](https://github.com/kirodotdev/KiroCrew/pull/28530)). The same pull request as this document splits the row in two and points the local half here, so the two documents agree.
- Related: [governance](../system-specs/modules/governance.md) (the evaluator, catalog and baseline this key joins), [memory-skills-hooks](../system-specs/modules/memory-skills-hooks.md) (the skill loader and project-skill trust), [harness-parity](../system-specs/modules/harness-parity.md).
- Measured at `origin/main` `19e077655d` (`19e07765`). Every file and line cited below was read at that commit. A symbol is given beside each line range so the symbol still locates the code after the lines move.

## Decisions requested

Each blocks Phase 1. Evidence for each is in the section named.

| # | Decision | Recommendation | Detail |
|---|---|---|---|
| D1 | Name of the key | `skills.local_sources`. Catalog rows are never renamed in place, so the name is permanent once it ships. | Design 1 |
| D2 | Which roots it covers | Covered: `<project>/.kiro/skills`, each `skills.extra_paths` root, each `skill://` mapping walk root outside every catalog and exempt root, and `<KIROCREW_PROJECT_DIR>/skills` (judged before the built-in sync copies it). Exempt: packaged built-ins, app provider roots, edition roots, and the user's global roots (`<data home>/skills`, kiro-cli's home `skills`). | Design 2 |
| D3 | Allow-only at every layer | A `skills.local_sources` control in any policy tier or any profile must be `"mode": "allow"`, in every edition. Carried as catalog data, refused at parse. | Design 4 |
| D4 | Enterprise default | Off: baseline `{"mode": "allow", "allow": []}`. The row ships in Phase 0 so operators can preview and publish an allow list; it withholds nothing until Phase 1. | Design 8, Migration plan |

## Summary

A `SKILL.md` enters the agent's context and can instruct it to run anything. Four local sources can put one there that an operator's policy cannot name today: a project's `<project>/.kiro/skills`, the roots in `skills.extra_paths`, the directories an agent spec's `skill://` mappings walk, and the `skills/` directory of `KIROCREW_PROJECT_DIR`, which the built-in sync copies into the global root. This RFC adds one governed scope, `skills.local_sources`, an allow list matched against one item per source: the canonical directory that directly contains or supplies its `SKILL.md` files.

The key only narrows. It never records, implies or replaces a trust grant: a project's skills load only when the local off switch, every governing policy and profile layer, and the directory-instance grant all admit it. Every layer must use an allow list, so a path spelling an author did not foresee is withheld rather than admitted. In `oss` nothing changes until a policy or profile names the key. In an enterprise build covered sources are off by default once Phase 1 lands, after a Phase 0 preview that reports what each host would admit and why.

The user's global roots stay outside this key. Packaged built-ins, Discover installs, earlier copies from `KIROCREW_PROJECT_DIR` and hand-authored skills all land in `<data home>/skills`, and a path policy cannot tell them apart once they share a root. This RFC therefore does not claim to control every local skill location.

## Threat model

| Actor | Controls | Trusted to |
|---|---|---|
| A repository the user opens, including its project-scope agent specs | `<project>/.kiro/skills` content; project-scope `skill://` mappings, which take precedence over a user-level spec of the same name; whether a checkout matches the markers `KIROCREW_PROJECT_DIR` detection looks for when the process starts inside it | Nothing. It may control content and mappings; it must not widen what the operator's ceiling admits. |
| The user | Local config (`skills.extra_paths`, `skills.project_skills_enabled`), user-level agent specs, the process environment, project trust grants, the user's global roots | Consent and trust decisions on their own machine. In an enterprise build, local config, environment and profiles may only narrow (enterprise RFC D3). |
| The enterprise operator | The remote policy ceiling | Widening and narrowing the ceiling. |
| The build authority | Packaged built-in skills, edition roots, the build-time edition | Everything the build ships. |

Boundaries this RFC adds a check to:

1. **Repository to catalog.** Today guarded by the off switch and the directory-instance grant. This RFC adds the policy conjunct to project trees and to mapping roots a spec names.
2. **Local config and environment to catalog.** Today guarded by local config only. This RFC adds the policy conjunct to extra roots and `KIROCREW_PROJECT_DIR`.
3. **Policy to admission.** Policy removes sources; it never admits a source another check refused.

Out of scope: a malicious local administrator (who can edit the install, the policy files and the grant store), the integrity of bytes after a source is admitted, and an agent writing into an admitted directory (that is `filesystem.write` and the sandbox). The goal is that a repository-selected or config-selected source cannot widen the operator's ceiling. It is not a guarantee about the content an admitted source serves.

## Motivation

### Current state

| Claim at `19e07765` | File | Lines | Symbol |
|---|---|---|---|
| The project-skills off switch is local config, default `True` | `src/kiro_crew/config/memory_sections.py` | 859-870 | `SkillsConfig.project_skills_enabled` |
| Extra skill roots are local config | `src/kiro_crew/config/memory_sections.py` | 846-858 | `SkillsConfig.extra_paths` |
| An unreadable or invalid config leaves the off switch off | `src/kiro_crew/config/resolution.py`; `src/kiro_crew/config/loader_pipeline.py` | 689-717; 498 | `_fail_closed_project_skills_config` |
| The off switch is read with a memo and fails closed | `src/kiro_crew/skill_trust.py` | 215-242 | `_project_skills_enabled` |
| The grant store answers nothing while the off switch is off | `src/kiro_crew/skill_trust.py` | 338-363 | `_read_store` |
| A project is identified by its `realpath`; only a path that expands to an existing absolute directory has a key | `src/kiro_crew/skill_trust.py` | 118-146 | `canonical_key` |
| A grant binds the directory instance (`st_dev`, `st_ino`, birth time where exposed) | `src/kiro_crew/skill_trust.py` | 155-207 | `_instance_identity` |
| The grant oracle, and the grant listing | `src/kiro_crew/skill_trust.py` | 379-384; 387-414; 721 | `is_project_trusted`; `is_key_trusted`; `list_trusted_projects` |
| Platforms without no-follow directory-descriptor traversal (Windows) refuse every project before resolving its path | `src/kiro_crew/skill_trust.py` | 101-110; 132-133 | `project_skill_traversal_supported`; `canonical_key` |
| The loader folds the project verdict into the catalog cache key | `src/kiro_crew/skills_body_reads.py` | 238-278 | `_trusted_project_key` |
| The enforcement record: scope `project_skills`, rule `project_skills_trust_enforced`, once per `(key, outcome)` per loader, `critical=False` | `src/kiro_crew/skills_body_reads.py`; `src/kiro_crew/skills.py` | 281-323; 442 | `_audit_project_skill_enforcement`; `_audited_projects` |
| Grant and revoke records use the same `project_skills` label | `src/kiro_crew/skill_trust.py` | 602-611; 678-687 | `grant_project_trust`; `revoke_project_trust` |
| Body reads: global root, then extra roots, then the trusted project, last | `src/kiro_crew/skills_body_reads.py` | 503-551 | `load_skill` |
| Extra-root bodies are read with no `within_root`, so only the final path component is no-follow | `src/kiro_crew/skills_body_reads.py`; `src/kiro_crew/hook_runtime/safe_reads.py` | 534-549, 643-649; 390-419 | `load_skill`, `_read_global_skill_text`; `safe_read_file_bytes_nolink` |
| A mapped row is read by its own path, fenced to `mapping_root` | `src/kiro_crew/skills_body_reads.py` | 774-780 | `_read_exact_key` |
| The walk appends the project root last, confined | `src/kiro_crew/skill_runtime/catalog.py` | 1221-1295 | `_iter_uncached` |
| The project's `.kiro/skills` is always a catalog root for the mapping walk, so a mapping into it is never re-walked past consent | `src/kiro_crew/skill_runtime/catalog.py` | 1171-1178 | `_scoped_entries` |
| A mapping outside every catalog root is walked from its literal prefix, after the UNC and sensitive-path gate, admitted under that root's `realpath` or a provider root | `src/kiro_crew/skill_runtime/catalog.py` | 1182-1217 (root 1196, gate 1197, admitted roots 1199) | `_scoped_entries` |
| The snapshot scope keys on the root set and the project key | `src/kiro_crew/skill_runtime/catalog.py` | 385-394; 423-440 | `_catalog_scope_key`; `_catalog_scope_id` |
| `skill://` accepts `~/`, absolute and workspace-relative paths | `src/kiro_crew/agent_discovery_parts/spec_fields.py` | 273-303 | `expand_skill_uri` |
| A project-scope agent spec takes precedence over a user-level one with the same name | `src/kiro_crew/agent_discovery_parts/skill_scope.py` | 255-261 | `agent_skill_globs` |
| Inherited trees reach a harness through one presence check that asks the off switch and the grant first; kiro-cli's home `skills` tree is added unasked | `src/kiro_crew/skills_discovery.py`; `src/kiro_crew/agent_discovery_parts/skill_scope.py` | 518-534; 169-201 (home tree 194-196) | `project_skill_tree_present`; `inherited_skill_globs` |
| Extra roots are resolved, sensitive-path screened and existence checked at load and on a live config change | `src/kiro_crew/skills.py`; `src/kiro_crew/skills_discovery.py` | 455-464; 792-832 | `SkillsLoader.__init__`; `_screen_extra_paths`, `_adopt_extra_paths` |
| Edition roots come from the platform context, are kept apart from configured roots and stay last | `src/kiro_crew/skills.py`; `src/kiro_crew/skills_discovery.py` | 481-492; 816-832 | `_edition_extra_paths`; `_adopt_extra_paths` |
| Provider roots a symlink in the global tree may point into: the installed package and `<data home>/apps` | `src/kiro_crew/skills_discovery.py` | 139-159 | `_trusted_skill_roots` |
| `KIROCREW_PROJECT_DIR` supplies `<dir>/skills` when it is a directory | `src/kiro_crew/skills_discovery.py` | 129-136 | `_project_skills_dir` |
| When unset, the CLI and the MCP entry set it by walking up from the working directory for `skills/` plus `src/kiro_crew/`, then the saved `project_dir` | `src/kiro_crew/cli.py`; `src/kiro_crew/mcp_entry.py`; `src/kiro_crew/project_dir.py` | 379-382; 131-136; 17, 37-61 | `main`; module setup; `detect_project_dir` |
| The built-in sync copies that source into the global root before the packaged built-ins, and the first source to ship a name owns it | `src/kiro_crew/skills_builtin_sync.py` | 447, 470-485 | `_ensure_builtin_skills` |
| The doctor's install-currency check mirrors the same two sources | `src/kiro_crew/skills_builtin_provenance.py` | 564-570 | installed-skill currency |
| Discover installs write into the global root | `src/kiro_crew/dashboard/handlers/discover.py` | 598 | `api_skills_discover_install` |
| A helper that adds `<project>/skills`, `<project>/.kiro/skills` and the global roots has no production caller (tests only) | `src/kiro_crew/agent.py` | 748, 843-865, 869 | `_all_skill_paths` and its alias |
| No catalog row for any skill source; `filesystem.read` uses the `path` matcher | `src/kiro_crew/platform/governance_parts/catalog.py` | 100-534 | `SCOPE_CATALOG` |
| `ScopeSpec` already carries per-scope parse rules as data (`always_permitted`) | `src/kiro_crew/platform/governance_parts/catalog.py` | 40-55 | `ScopeSpec` |
| An unknown governed key refuses the whole policy | `src/kiro_crew/platform/governance_parts/parsing.py` | 423, 459, 461 | `_parse_controls` |
| The allow-only refusal applies only to a policy (`is_policy`), only in an enterprise build, only to `dashboard_origins` | `src/kiro_crew/platform/governance_parts/parsing.py` | 64-67; 187-205 | `ALLOW_ONLY_IN_ENTERPRISE`; `_parse_control` |
| Profiles and the policy's `fallback` profile parse with `is_policy=False` | `src/kiro_crew/platform/governance_parts/parsing.py` | 580-605; 639, 708 | `parse_policy`; `parse_profile` |
| An invalid profile is replaced by the policy's declared `fallback` profile, or by the deny-all profile (every ruleset an empty allow list) when none is declared | `src/kiro_crew/platform/governance_profiles.py`; `src/kiro_crew/platform/governance_parts/carriers.py` | 21, 292-325, 768-794; 760-778 | Validation rule 5, `_fallback_profile`; `deny_all_profile` |
| An unnamed scope permits; in an enterprise build the policy level gets the baseline first, a profile never does | `src/kiro_crew/platform/governance_parts/evaluator.py` | 56-111 | `_policy_level`, `_query_level` |
| Every catalog scope needs a baseline row or an ungated reason | `src/kiro_crew/platform/governance_parts/baseline.py` | 1-24; 41-86; 98-111 | `BASELINE_CONTROLS`; `BASELINE_UNGATED` |
| The path matcher is lexical: the item is normalized, the pattern is matched as written | `src/kiro_crew/platform/governance_parts/matching.py` | 53-113; 282-322 | `_match_path`; `_expand`, `_norm_item` |
| Only a deny folds case, and only on Windows | `src/kiro_crew/platform/governance_parts/matching.py` | 116-125; 330-366 | `_match_denied_path`; `_match_local_path` |
| The extension-governance helper: host surface, enterprise baseline when ungoverned, fail-closed, audits every governed call | `src/kiro_crew/extension_governance.py` | 44-98 | `_denied` |
| The host surface's session key | `src/kiro_crew/platform/governance_profiles.py` | 206 | `HOST_SESSION_KEY` |
| A ceiling swap bumps a counter; a second token covers ceiling and profile changes together | `src/kiro_crew/platform/context.py`; `src/kiro_crew/platform/governance_profiles.py` | 582-590; 437-452 | `governance_generation`; `governance_answer_generation` |
| `kirocrew policy validate` load-checks the policy and every profile; `policy explain <scope> <item>` prints verdict, rule and layer for one item | `src/kiro_crew/cli_parser_admin.py`; `src/kiro_crew/cli_commands_security.py` | 241-251; 315-390 | policy subcommands |
| Governance records store the item after credential and exfiltration-URL redaction only; local-path redaction exists but the SEL writer does not apply it | `src/kiro_crew/sel.py`; `src/kiro_crew/security/redaction.py` | 822-893; 1338 | `log_governance_decision`; `redact_local_paths` |
| The SEL is a file in the data home on the owner-only startup sweep | `src/kiro_crew/sel.py`; `src/kiro_crew/owner_only_files.py` | 12, 93; 64, 882, 908-909 | `_SEL_FILE`; `STARTUP_SWEEP_STORES` |
| Sessions can run in a remote environment whose workspace server replaces the built-in file and shell tools | `src/kiro_crew/session_environment.py`; `src/kiro_crew/acp/runtime_remote_workspace.py` | 1; 1 | module docstrings |
| `CONTRACT_VERSION` is pinned at 1 | `src/kiro_crew/platform/context.py` | 76-84 | `CONTRACT_VERSION` |

### Problems

1. **Policy cannot narrow a local source.** Project skills answer only to a local switch and a local grant. `skills.extra_paths` and `KIROCREW_PROJECT_DIR` answer only to local config and the environment. A mapped root answers only to the agent spec that names it plus the sensitive-path and provider-root fences. Enterprise RFC D3 says local config may narrow a control but never widen it, and each of these widens what can enter the agent's context.
2. **`project_skills` is an audit label, not a policy key.** The records in `skills_body_reads.py` lines 311-320 and `skill_trust.py` lines 602-611 and 678-687 name it as their scope, but `SCOPE_CATALOG` has no such row, so a policy that names it is refused as an unknown governed key.
3. **The allow-only rule does not reach profiles.** The one existing allow-only scope is checked only when `is_policy` is true. A profile is parsed with `is_policy=False`, and in `oss` an unnamed policy scope permits, so a profile can be the only restriction on a scope. A deny list there admits every spelling its author missed.
4. **The enterprise baseline has nothing to turn off.** The appendix row is off in an enterprise build, and no key exists for its local half.

### Corrections to the issue's premises

The issue was measured at `cd3940f`. Three of its statements do not hold at `19e07765`:

- **`project_skills` is an existing audit label, not yet a catalog key** (problem 2). This RFC does not promote it; see Design 1.
- **`folders.read` aliases `filesystem.read`** (`_SCOPE_ALIASES`, `catalog.py` lines 559-561). It is one scope under a second spelling.
- **The issue's line numbers are stale.** At `19e07765`: `project_skills_enabled` is at `memory_sections.py` line 859 (not 861), `extra_paths` at line 846 (not 848), `_project_skills_enabled` at `skill_trust.py` line 215 (not 213), `is_project_trusted` at line 379 (not 377), and `_instance_identity` at lines 155-207 (not 156-209).

## Goals

1. An operator's policy can name which local directories may supply skills, for every covered source in Design 2.
2. Policy only narrows. It never grants project-skill trust and never replaces the directory-instance grant.
3. A path spelling the author did not foresee is withheld at every governing layer.
4. One admission predicate per source, asked at every point that admits that source, including every harness projection, so every supported harness reaches the same answer.
5. Operators can see, before enforcement, every covered source a host would load, its policy item, and which layer and rule decide it.
6. `oss` behaviour is unchanged while no policy or profile names the key, including no new audit records.
7. An enterprise build is off by default for covered sources from Phase 1, per the appendix row.
8. Fail closed on an unevaluable policy, and record decisions without a governance event per message.

## Non-goals

- **The user's global roots** (`<data home>/skills`, kiro-cli's home `skills`). They receive packaged built-ins, Discover installs, `KIROCREW_PROJECT_DIR` copies made earlier, and hand-authored skills, and a path policy cannot tell those apart once they share a root. Governing them needs a provenance signal, not a path; see open question 3.
- **Content integrity, signing and digest provenance.** Source admission answers where a skill came from, not which bytes the agent received. Signed or digest-pinned skills are a separate follow-up.
- **Remote and container workspaces.** This RFC defines the item only as a path on the host that runs the skill loader. How a remote session's skill roots are named is open question 2, and Phase 1 is blocked on it.
- **Install, Discover install and discovery registries.** Install is `capabilities.extension_install`; registries are #28530.
- **Changing which trust decision a source requires.** This RFC adds a policy conjunct; it does not add, remove or move a trust grant.
- **Governing skills by name.** A repository chooses its own skill names.
- **What the agent reads with its file tools.** That is `filesystem.read`.
- **Loading project skills on Windows.** The refusal in `project_skill_traversal_supported` stays.
- **Withdrawing a body already injected into a live conversation.**
- **A deny form of the key** (Design 4, open question 1).

## Design

### 1. The key: `skills.local_sources`

| Field | Value |
|---|---|
| Scope | `skills.local_sources` |
| Archetype | `ScopeSpec(RULESET, matcher="path", allow_only=True)` |
| Item | the canonical directory that directly contains or supplies the source's `SKILL.md` files (Design 3) |
| Policy form | allow list only, at every layer, in every edition (Design 4) |
| Enterprise baseline | `{"mode": "allow", "allow": []}`, off |
| Written as | `{"skills": {"local_sources": {"mode": "allow", "allow": [...]}}}`, or the flat key `"skills.local_sources"` |

The nested form parses with no loader edit: `_parse_controls` descends any fixed-child namespace whose dotted child is a catalog scope, as it does for `filesystem` and `network`.

Why this name (D1):

- **It names every covered source.** A name that says "project" would misdescribe three of its four items. It follows the appendix's policy term `crew_extension_local_sources`.
- **Not `project_skills`.** That label names the trust records written at grant, revoke and enforcement. Reusing it would make a trust record and a policy decision indistinguishable in the SEL.
- **Not `external_access.*`.** `external_access.registry_hosts` matches hosts; a local directory is not an external service, and one `ScopeSpec` carries one matcher.
- **Under `skills`.** The local settings this key bounds (`skills.project_skills_enabled`, `skills.extra_paths`) live in the `skills` config section.
- **Append-only.** `SCOPE_CATALOG` rows are never renamed in place (`catalog.py` lines 91-99).

### 2. Covered and exempt sources (D2)

| Source | Covered | Why |
|---|---|---|
| `<project>/.kiro/skills` | yes | Arrives with a repository. |
| `skills.extra_paths` roots | yes | Local config that widens what enters context. |
| `skill://` mapping roots walked from outside every catalog root and every exempt root | yes | An agent spec, possibly project-scope, names the directory. |
| `<KIROCREW_PROJECT_DIR>/skills` | yes, judged before the built-in sync copies it | Chosen by the environment or by the working directory the process starts in; after the copy its skills are indistinguishable from the global root's and own their names over packaged built-ins. |
| Packaged built-in skills; app skills under the provider roots (installed package, `<data home>/apps`) | no | Shipped by the build, or brought by an app whose install and activation `apps` and `app_versions` govern. |
| Edition roots (`McpToolingProvider.extra_skills()`) | no | Composed by the edition build, the same authority as the build. |
| `<data home>/skills` and kiro-cli's home `skills` | no, stated residual | Shared by packaged, installed, copied and hand-authored content; see Non-goals. |
| A mapping whose walk root lies inside a catalog root or an exempt root | no extra check | Its rows already passed that root's own admission (`_scoped_entries`, lines 1171-1195). |

`_all_skill_paths` in `agent.py` assembles project and global roots with no trust check but has no production caller at `19e07765`. Phase 1 re-checks its callers; any new caller of a covered root must go through the admission predicates.

### 3. The policy item: one semantic

Every item is the absolute canonical directory that directly contains or supplies `SKILL.md` files, produced by resolution each source already performs:

| Source | Item | Produced by |
|---|---|---|
| Project | `<canonical_key(project)>/.kiro/skills`, joined lexically. No `realpath` inside the project before consent; the no-follow descriptor walk refuses a linked `.kiro` or `skills` component, so the joined path is the directory that is read. | `canonical_key`, then a join |
| Extra root | the resolved root | `Path(p).expanduser().resolve()` in `SkillsLoader.__init__` and `_screen_extra_paths` |
| Mapping root | `realpath` of the literal walk root | `_scoped_entries`, after the UNC and sensitive-path gate at line 1197 |
| `KIROCREW_PROJECT_DIR` | `realpath` of `<dir>/skills`, after a sensitive-path gate this design adds (the source has none today) | `_project_skills_dir`, then `validate_file_path` and `os.path.realpath` |

The directory-instance grant is a separate AND check keyed on the project directory itself, not on this item. The policy is written once for many hosts and cannot know an inode; the grant store is per host and binds the instance the user reviewed. Each check keeps the identity it can verify, and both must admit.

One semantic means a mapping that names `<project>/.kiro/skills` from outside the catalog roots, and the project tree itself, are asked about the same string.

Matching follows from the path matcher (`_norm_item`, `_expand`): the item is normalized lexically, the pattern is `~`/`$VAR`-expanded on the evaluating host and otherwise matched as written.

- **Symlinks.** The item is resolved; the pattern is not. An entry written through a link admits nothing. On macOS `/tmp/work/*` never matches, because the item is `/private/tmp/work/...`; the same holds for `/var` and a symlinked home.
- **Case.** An allow entry matches case-sensitively on every platform. On a case-insensitive volume one directory reached under two spellings is two items, and the unmatched one is withheld.
- **Windows.** Project items never reach the policy on Windows (`canonical_key` returns `None` first). Other items do: a pattern separator meets either separator, a drive letter meets either case, `\\?\C:\x` folds to `C:\x`, UNC spellings stay as written (`_match_local_path`, `_norm_item`).
- **Trailing separators and relative entries.** An item carries no trailing separator and is always absolute, so such an entry admits nothing.
- **Globs.** `fnmatch` semantics. A bare directory admits exactly that directory. `*` crosses separators, so `/srv/repos/*/.kiro/skills` admits every project skills directory below `/srv/repos` at any depth.
- **Namespace.** An item is a path on the host that runs the skill loader. Nothing here names a path inside a remote or container workspace (open question 2).

### 4. Allow-only at every layer (D3)

A deny list over paths is not safe: one directory has several spellings (a link the caller resolved differently, a bind mount, another case on a case-insensitive volume), and a deny names only the spellings its author thought of. An allow list fails closed on the same aliases.

Restricting only the policy is not enough. In `oss` a policy that does not name the scope permits it, so a profile can be the only restriction, and a profile deny list that misses a spelling admits it. Governance composes every layer with AND, so the rule must hold at each one:

| Layer | Parsed by | Today | Phase 0 |
|---|---|---|---|
| Remote and local policy tiers | `parse_policy` (`is_policy=True`) | allow-only only for `dashboard_origins`, enterprise only | refused at load unless `allow`, every edition |
| The policy's `fallback` profile | `_parse_controls(..., is_policy=False)` | no allow-only rule | refused at load unless `allow` |
| Per-surface profiles | `parse_profile` (`is_policy=False`) | no allow-only rule | the profile is invalid and is replaced by the policy's `fallback` profile, or the deny-all profile when none is declared (Validation rule 5) |

Parser and data-model change: `ScopeSpec` gains `allow_only: bool = False`, appended last so existing positional and keyword construction keeps working. `_parse_control` refuses a non-`allow` ruleset for an `allow_only` scope regardless of `is_policy` and edition, with a message naming the scope and the reason. `ALLOW_ONLY_IN_ENTERPRISE` stays as it is for `dashboard_origins`, whose enterprise-only scope and exact-origin rules are unchanged. The deny-all profile already writes an empty allow list for every ruleset scope, so it needs no edit.

Replacing an invalid profile is the existing rule, applied to every scope that profile governs. Either replacement is itself allow-only for this scope: a declared `fallback` is parsed under the same rule, and when it does not name the scope the policy level (or, in an enterprise build, the baseline) decides. `kirocrew policy validate` already reports the file as `INVALID→deny-all` and exits non-zero. No profile can name `skills.local_sources` today (it is an unknown key), so no existing file changes meaning.

Shapes that admit nothing but are already safe (a trailing separator, a relative entry, an alias spelling) are not refused, because refusing would block boot for a document that already fails closed. Phase 2 makes `policy validate` warn about them.

### 5. The predicates

- **Grant oracle, unchanged: `skill_trust.is_key_trusted(project_key)`.** It already returns `False` while the off switch is off.
- **Policy query, new: `extension_governance.local_skill_source_denied(item) -> str | None`.** Asks `skills.local_sources` on the host surface (`HOST_SESSION_KEY`) the way `_denied` does: enterprise baseline when nothing governs, denial on any evaluation error. It never audits. It is memoized per `(item, governance_answer_generation())`, the token covering both ceiling and profile changes; the ceiling counter alone would miss a profile tightening. That token is a pure read and sees a profile file edit only after `poll_profiles_fresh()` runs, so each admission point polls it off the event loop before reading the token, as other governance readers already do.
- **Admission, new: `project_skills_admitted(project_key)`** = `project_skills_enabled() and local_skill_source_denied(<key>/.kiro/skills) is None and is_key_trusted(project_key)`. For the other sources the admission is `local_skill_source_denied(item) is None` on top of each source's existing fences. These live in a new module, not in `skill_trust.py`, so the consent record stays a consent record.
- **Inventory, new in Phase 0.** One function that enumerates the covered sources a host would load and returns, per source, its kind, item and decision, built from the same item producers and the same query the admission points use.

Where they are asked. Each is a seam every harness already runs through, so no caller tests harness identity:

| Admission point | Today | Phase 1 |
|---|---|---|
| `SkillsLoader._trusted_project_key` | `is_key_trusted` | `project_skills_admitted`; covers `load_skill`, exact-key reads, `_scoped_entries` and the catalog scope key |
| `project_skill_tree_present` | off switch and `is_project_trusted` | `project_skills_admitted`; covers `inherited_skill_globs` and every harness projection that calls it |
| Extra roots in the walk and in `load_skill` | every root in `_extra_paths` | configured roots filtered by the policy query; edition roots unfiltered; each admitted root's body reads pass it as `within_root` with `within_root_is_canonical=True` |
| The mapping walk in `_scoped_entries`, and a row's `mapping_root` | provider-root and sensitive-path fences | the same fences, then the policy query on the resolved walk root before it is walked |
| `_ensure_builtin_skills` and the doctor's currency mirror | iterate `_project_skills_dir()` first | ask the policy query on the `KIROCREW_PROJECT_DIR` item before reading it; a withheld source is skipped, so packaged built-ins supply their own names |
| Every harness projection that turns a skill scope into harness configuration (modules under `src/kiro_crew/acp/`) | reads the scope through the seams above | takes every root it emits from the seams above, never from an unfiltered glob; Phase 1 lists them by a caller grep |

The dashboard's trust readers (`_shared_skill_listing` line 494, `prompts_skill_catalog._trust_snapshot` lines 459-478) report state and admit nothing. Phase 1 adds the policy verdict to what they report, so a chat with a policy-withheld project is told so rather than offered a grant that would not load it.

### 6. When a change binds

- **Crew-served reads bind at the next catalog or read decision.** `_trusted_project_key` folds the verdict into the cache key, so a changed verdict selects a different catalog scope on the next message. The admitted extra-root set and the answer token join `_catalog_scope_id`'s material, so a narrowed policy is never served a snapshot built under a wider one. Mapping rows are produced per request by `_scoped_entries` and never snapshotted, so mapping admission is decided on each request.
- **One generation per catalog build.** A build reads the answer token once, decides every source under it, and stores its snapshot under that token. If the token has changed when the build finishes, the snapshot is not served for the new token; the next request builds again.
- **`KIROCREW_PROJECT_DIR` binds at the next sync.** A copy made under an earlier answer stays in the global root, which is exempt; see Security considerations.
- **Harness projections bind at the next projection** (a launch or respawn).
- **Bodies already injected stay** in their conversation.

### 7. Audit without per-message records, and privacy

The existing record stays as it is: scope `project_skills`, rule `project_skills_trust_enforced`, once per `(key, outcome)` per loader, `critical=False`.

The policy decision gets its own record, written by the admission point, never by the hot-path query:

- scope `skills.local_sources`, item the canonical item, outcome, the decision's `rule` and `layer`, `critical=False`;
- once per `(item, outcome, rule)` per loader, extending the `_audited_projects` marker, and once per sync run for `KIROCREW_PROJECT_DIR`, whose module-level sync has no loader and keeps its own marker; a second message about an unchanged decision writes nothing;
- not written for an ungoverned permit, matching `_denied`, so an `oss` host with no key writes nothing new.

`_denied(..., audit=True)` is not called from the read path, because it writes one record per call.

Privacy. The record stores an absolute path. Project records already do, so the new record broadens collection to extra roots, mapping roots and `KIROCREW_PROJECT_DIR`, which can carry user names and project names. The SEL writer applies credential and exfiltration-URL redaction to the item, not `redact_local_paths`, and the SEL sits on the owner-only startup sweep. Phase 1 reuses those rules and adds no export. Whether governance items should be path-minimized is a residual for the privacy review the Phase 1 pull request must name. The Phase 0 inventory prints to the operator's terminal and writes no SEL record.

### 8. Enterprise and `oss` (D4)

- **`oss`, key not named anywhere:** the query answers ungoverned, nothing is recorded, behaviour is identical to today.
- **`oss`, key named in a policy or profile:** enforced as in an enterprise build from Phase 1.
- **Enterprise:** `BASELINE_CONTROLS` gains `"skills.local_sources": {"mode": "allow", "allow": []}` in Phase 0. From then on any `resolve` caller, `policy show` and `policy explain` report it, but no admission point reads it. From Phase 1, with no remote rule, covered sources are withheld. The remote policy widens it; local config, environment, profiles and dashboard settings cannot.

## Migration plan

### Phase 0: preview, no admission change

One pull request: the catalog row with `allow_only`, the parse rule at every layer, the baseline row, the item producers, the inventory, and its report. No admission point calls the new query.

The report reuses the existing surfaces rather than adding a command:

- `kirocrew policy validate` gains a "Local skill sources (preview, not enforced)" section. Each row: source kind (`project`, `extra_path`, `mapping`, `project_dir`), the item exactly as a policy must spell it, `admitted` or `withheld`, and the deciding layer and rule. For a project the row also names a withholding off switch or a missing grant. Detected sources are the configured extra roots, the `KIROCREW_PROJECT_DIR` source the gateway would read, every granted project from `list_trusted_projects` plus the working directory, and the mapping roots of user-level specs and of project-scope specs in those projects. The section never changes the exit code.
- `kirocrew policy explain skills.local_sources <item>` works once the row exists. For this scope it evaluates on the host surface, as admission does, rather than the `--session-key` default.
- Both, and a one-time warning when a loaded policy or profile names the key, say the control is not enforced in this build.

Phase 0 is needed before enforcement for a second reason: a build without the row refuses a whole policy that names the key, so an operator can publish an allow list only once every host that fetches it runs a Phase 0 build.

Exit criteria:

1. **Parse.** The key loads in the nested and the flat form with the `path` matcher, in a policy tier, the `fallback` profile and a per-surface profile.
2. **Allow-only at every layer.** `"mode": "deny"` is refused at load in a policy and in the `fallback` profile in both editions, naming the scope; a per-surface profile with it is replaced by the declared `fallback` profile, or the deny-all profile when none is declared, and `policy validate` reports it `INVALID→deny-all`. `dashboard_origins` behaves as before.
3. **No admission change.** With an `allow: []` policy in both editions, every existing project-skill, extra-root, mapping and built-in sync test passes unchanged and no `skills.local_sources` record is written.
4. **Inventory.** For a fixture with one source of each kind, the report lists each with the item Design 3 defines, and its verdict, rule and layer equal `local_skill_source_denied` for that item; with no policy in `oss` each row reads ungoverned, and in an enterprise build each reads withheld, layer `policy`, with the `enterprise baseline:` reason.
5. **Baseline row.** `test_governance_enterprise_baseline.py` finds the new row.

### Enterprise upgrade recipe

1. Roll every host to a Phase 0 build before any policy names the key.
2. On representative hosts run `kirocrew policy validate`. Each covered source shows `withheld`, layer `policy`, reason prefixed `enterprise baseline:`: what Phase 1 would withhold.
3. Build the allow list from the item column. Copy an item verbatim to admit exactly that directory, or generalize to a glob such as `/srv/repos/*/.kiro/skills`. Write resolved spellings (`/private/tmp/...` on macOS) and the case the item shows.
4. Publish the allow list in the remote policy. A profile may narrow it further, with an allow list.
5. Re-run `policy validate`; the sources you meant to keep show `admitted`, layer `policy` (or `both` when a profile also names the scope). Use `policy explain skills.local_sources <item>` for a single path.
6. Roll to the Phase 1 build. Trust grants are kept, so an admitted project whose directory instance is unchanged loads without a new grant.

`oss` hosts need no step.

### Phase 1: enforcement at every admission point

Blocked on: D1 to D4; Phase 0 released; open question 2 answered or the remote path pinned by a test (criterion 22).

One pull request wires every admission point in Design 5, removes the preview marking, and updates the governance and memory-skills-hooks specs in the same commit. If it must split, every partial pull request keeps the preview marking and says which sources are enforced, and none claims coverage of a source it has not wired. A test pins the covered-source table in Design 2 against the admission points.

Exit criteria, each an executable assertion:

1. **Allow semantics.** With `allow: ["/a/b/.kiro/skills"]`, a granted project at `/a/b` loads and `/a/c` does not; with `allow: ["/a/*/.kiro/skills"]`, `/a/b/c` loads; `allow: []` admits nothing.
2. **Policy cannot grant.** A project the policy names with no grant loads nothing; a grant with no instance identity, or bound to a replaced directory instance, loads nothing; no policy outcome changes the grant store's bytes.
3. **Three-way AND.** With policy admitting and a grant present, `skills.project_skills_enabled: false` loads nothing.
4. **Profile only.** In `oss`, with no policy naming the key and a host-surface profile allowing `/a/b/.kiro/skills`, the project at `/a/b` reached through any other spelling loads nothing.
5. **Symlink and alias.** A project reached through a symlink is matched by its resolved path; an entry written through the link admits nothing; on macOS `/private/tmp/...` admits and `/tmp/...` does not.
6. **Case.** An entry whose case differs from the item's in any letter other than a Windows drive letter admits nothing.
7. **Windows.** Under a simulated Windows path mode an extra root matches across separators and drive-letter case and not across other letters' case; a project item never reaches the policy.
8. **Trailing separator and relative entry.** Each admits nothing and the policy still loads.
9. **`skills.extra_paths`.** A configured root the policy does not name contributes no catalog row and no `load_skill` body; an edition root is unaffected.
10. **`skill://` mapping.** A mapping walk root outside every catalog and exempt root is walked only when admitted; a mapping into the global root is unaffected; `mapping_root` reads obey the same answer; a mapping naming a withheld project's `.kiro/skills` gets the project's answer.
11. **`KIROCREW_PROJECT_DIR` withheld before copy.** With the source not admitted, the sync creates and updates nothing in the global root from it, packaged built-ins supply their own names, the doctor mirror reports the source withheld, and one record is written.
12. **No shadowing.** A withheld project, extra root or `KIROCREW_PROJECT_DIR` source defining skill `x` does not displace an admitted global `x` in the catalog, in `load_skill` or in an exact-key read.
13. **No mixed generation.** A policy refresh between the start and the end of a catalog build yields no snapshot served under the new answer token with rows decided under the old one; the next request rebuilds.
14. **Swap after decision.** A covered root replaced by a symlink, or a nested directory swapped for one, between the decision and the read serves no byte from outside the admitted canonical root, for project, extra-root, mapping and `KIROCREW_PROJECT_DIR` reads.
15. **Enterprise off.** In an enterprise build with no remote rule the four covered sources load nothing; the global root and built-ins still load.
16. **`oss` unchanged.** In `oss` with no key, every existing project-skill, extra-root, mapping and sync test passes unchanged and the SEL gains no `skills.local_sources` record.
17. **Fail closed.** An evaluation error in the query withholds every covered source; an unreadable or malformed config still leaves project skills off.
18. **Audit dedup.** Many reads for one item write one record per `(item, outcome, rule)`; a changed outcome writes one more; `project_skills_trust_enforced` is unchanged.
19. **Binding.** After a ceiling swap or a profile change that removes an item, the next catalog read and the next `load_skill` withhold it; a body injected before is still in the conversation.
20. **Inventory equals enforcement.** For one fixture and policy, the set of `(kind, item, verdict)` the Phase 0 report prints equals the set the admission points decide.
21. **Harness parity.** For one fixture and policy, every id in `acp_backends.BASELINE_SELECTABLE_BACKENDS` yields the same admitted root set, and `scripts/check_harness_parity.py` passes on the diff.
22. **Remote path.** A session in a remote environment either hands no remote-namespace path to any admission point (asserted), or Phase 1 does not merge until open question 2 adds a namespace.

### Phase 2: operator documentation and lint

One pull request after Phase 1: the "Project-skill trust" section of `src/kiro_crew/docs/skills.md` and the operator governance guides gain the key, the three-way AND, the item rule with the macOS `/private` example, the upgrade recipe and a worked allow list; `policy validate` warns on an entry that can admit nothing (trailing separator, relative path, a spelling that does not resolve to itself on this host).

Exit criteria: `./scripts/docs-lint.sh` passes; `policy validate` prints one warning per such entry and keeps its exit code.

## Backward compatibility

- **`oss`:** no change while nothing names the key. A policy that names it, refused today as an unknown key, loads.
- **Enterprise:** breaking at Phase 1 for an existing install. Project skills, configured extra roots, mapped roots outside the exempt roots and `KIROCREW_PROJECT_DIR` are withheld until the remote policy names them. Phase 0 and the upgrade recipe exist so operators see this first. Trust grants are not deleted. This is a release-notes item for both phases.
- **Profiles:** none can name the key today, so the allow-only rule changes no existing profile.
- **Audit:** `project_skills` records keep their scope, rules and cadence. `skills.local_sources` records appear only when the key is governed.
- **`CONTRACT_VERSION`:** unchanged at 1. The change is a catalog row, a defaulted `ScopeSpec` field, a baseline row, a parse rule and core helpers. No `PlatformContext` field, Protocol or seam signature changes, and the version is pinned at 1 before launch.
- **Writer and reader sweep:** readers are the admission points in Design 5, the inventory and the two dashboard trust readers. No new writer of the grant store exists.

## Security considerations

- **Narrow only.** The key is a conjunct under `POLICY ∩ PROFILE`; it can only remove a source another check admitted, and it never writes the grant store (Phase 1 criterion 2).
- **Fail closed at every layer.** Allow-only at each policy tier and profile; an evaluation error denies; the enterprise baseline is off from Phase 1.
- **Lexical match over canonical items.** The pattern is never resolved, so matching adds no filesystem lookup on a path the policy names. Items come from paths the existing fences already gate.
- **Admission and the read.** The decision covers the canonical item; the existing descriptor-pinned readers keep the bytes inside it, and Phase 1 passes the admitted root to extra-root reads so a swapped intermediate directory cannot escape it.
- **Residuals.**
  - The user's global roots are not covered. Content that reaches them by any path, including a `KIROCREW_PROJECT_DIR` copy made before Phase 1 or under an earlier answer, loads as global content.
  - Source admission says nothing about the bytes an admitted source serves (no signing or digest).
  - A body already injected stays in its conversation; a harness projection built before a change keeps it until the next projection.
  - On a case-insensitive volume one directory can be withheld under one spelling and admitted under the spelling the policy names; that is the allow direction, never the reverse.
  - The new record stores more absolute paths in the SEL (Design 7).
  - Remote and container workspaces are undefined until open question 2.
  - The grant's own instance-identity residuals (`_instance_identity` docstring) are unchanged.

## Alternatives considered

- **Make `skills.project_skills_enabled` policy-owned.** All or nothing, and it reaches neither extra roots, mappings nor `KIROCREW_PROJECT_DIR`.
- **Promote `project_skills` to the catalog key.** It names one of four sources and would merge trust records with policy decisions in the SEL.
- **Use the project directory as the project item.** Then a project and a mapping into its `.kiro/skills` would be asked about different strings for the same files.
- **Allow-only at the policy layer only.** A profile can be the only restriction in `oss` (problem 3).
- **Bind the policy to the directory instance.** A central policy cannot name an inode; the grant already binds it per host.
- **Govern the global roots by path.** Built-ins, installs, copies and hand-authored skills share them; a path cannot separate them.
- **A new preview command, or a candidate-policy flag.** `policy validate` and `policy explain` already exist; and the allow list has to be published in the real policy anyway, which needs the row.
- **Deny lists with alias-aware matching.** Would still miss bind mounts and case; open question 1.
- **Leave it to `filesystem.read`.** That governs the agent's tool reads, not what the loader puts into context.
- **One key for registries and local sources.** One `ScopeSpec` has one matcher.

## Open questions

The four decisions at the top are separate; these are what remains.

1. **Deny mode later.** Is a deny form worth a matcher that resolves the pattern's literal prefix on the host, as `skill_runtime/catalog.py` does for mapping globs (`_canonical_prefix`)? It would still not cover bind mounts or case.
2. **Remote and container workspaces (blocks Phase 1).** The code at `19e07765` does not show how a remote session's project directory and mapping roots relate to the paths the local skill loader sees. Either Phase 1 proves, by test, that no remote-namespace path reaches an admission point, or the item gains a namespace (for example a host or environment prefix).
3. **Global roots.** Should a later design govern the user's global roots by provenance (an install record, a sync marker that names its source) rather than by path?
4. **Path minimization in the SEL.** Should governance items for local paths be stored hashed or home-relative, given that forensics and `policy explain` want the literal path?
