# Requirements Document

## Introduction

Kiro Crew users who keep a shared standards repository (steering, checklists, planning
documents) that applies to many other repositories have no way to make that steering reach
every chat without repeating it in each prompt. The IDE answers this with "workspace
folders"; Crew's analogue is the sidebar folder tree, which already carries `project_dir`
and `default_agent` that chats inherit (GitHub issue #631).

This feature adds a per-folder list of **additional steering directories**. Every dashboard
chat filed anywhere under such a folder receives the `always`-inclusion markdown from those
directories as session-start context, in addition to the project's own steering.

The delivery MUST be universal. Kiro Crew runs chats on several providers today (kiro-cli,
Claude Code, Codex, KAS) and is adding config-authored harnesses so new providers arrive
without code changes. Delivery is therefore performed once, by the gateway's own context
builder — the one layer every provider passes through — and never inside a per-provider
harness. Per-harness delivery would leave one silent hole per provider, which is exactly
the shape the current working tree has (the kiro harness declares the documents as
"native" launch documents, a dedup record the runtime never transmits, so today no provider
receives them).

Out of scope: steering for subagent runs spawned from a folder chat, for messaging-channel
sessions (Slack, Discord, etc.) that have no folder, and any UI beyond the folder
configuration modal.

## Glossary

- **Folder_Store**: the dashboard's persisted folder tree (`folders.json`) and its create /
  update endpoints in `chat_folders.py`.
- **Folder**: one record in the Folder_Store; has an optional `parent_id`, and optional
  `project_dir`, `default_agent`, and (new) `steering_dirs`.
- **Steering_Dir**: one absolute directory path listed in a Folder's `steering_dirs`.
- **Steering_Document**: a `*.md` file found recursively under a Steering_Dir whose
  frontmatter `inclusion` is `always` or absent.
- **Steering_Resolver**: the function that computes a chat's Effective_Steering_Dirs from
  its folder chain (`_resolve_folder_steering_dirs`).
- **Effective_Steering_Dirs**: the ordered, deduplicated list of Steering_Dirs a chat
  inherits from its Folder and every ancestor Folder.
- **Context_Builder**: `kiro_crew.context.ContextBuilder`, whose `build_message` /
  `build_session_context` assemble the prompt for every provider.
- **Chat_Runner**: the dashboard turn path (`chat_runner.py`) that calls the
  Context_Builder for a chat slot.
- **Chat_Slot**: one dashboard chat, with `folder_id`, `project`, `mode`, `agent`.
- **Provider**: the backend running a chat: kiro-cli, Claude Code (CC), Codex, KAS, or any
  config-authored harness.
- **Harness**: the per-Provider spawn adapter under `acp/harness/` that builds a
  `SpawnPlan` from a `SpawnContext`.
- **Fresh_Session**: the first prompt of a new provider session (`is_new_session` and not
  `resumed`).
- **Reinjection_Turn**: a turn flagged `needs_reinjection` after the provider compacted its
  context.
- **Member_Chat**: a Chat_Slot in `member` mode (private memory), whose session-start
  context is the member essentials envelope.
- **Folder_Config_Modal**: the website's folder create/edit dialog
  (`FolderConfigModal.tsx`).
- **Steering_Cap**: the existing steering section character cap
  (`caps.steering` when `skills.lazy_load` is on; the shared context ceiling otherwise).
- **SEL**: the security event log.

## Requirements

### Requirement 1: Folder Steering Directories

**User Story:** As a dashboard user, I want to attach one or more steering directories to a
folder, so that standards kept outside a project can be declared once for a group of chats.

#### Acceptance Criteria

1. WHEN a folder create or update request carries `steering_dirs`, THE Folder_Store SHALL accept it only as a list of strings, and SHALL reject any other shape with HTTP 400 and error code `steering_dirs_invalid`.
2. WHEN a folder create or update request carries `steering_dirs`, THE Folder_Store SHALL validate every entry with the same contract as `project_dir`: absolute or `~`-prefixed, `expanduser` then `realpath`, and an existing directory.
3. IF an entry resolves to a sensitive path, THEN THE Folder_Store SHALL reject the request with HTTP 400 and SHALL record the refusal in the SEL exactly as a sensitive `project_dir` is recorded.
4. IF `steering_dirs` lists more than 16 entries, THEN THE Folder_Store SHALL reject the request with HTTP 400.
5. IF two entries resolve to the same realpath, THEN THE Folder_Store SHALL reject the request with HTTP 400.
6. WHEN a folder create or update request omits `steering_dirs` or carries null or an empty list, THE Folder_Store SHALL store an empty list for that folder and treat the folder as contributing no steering.
7. WHEN a folder is persisted, THE Folder_Store SHALL store the resolved realpath spelling of each entry, and SHALL return it in the folder record read by the website.

### Requirement 2: Accumulative Inheritance

**User Story:** As a dashboard user, I want steering directories to accumulate down the
folder tree, so that an organisation-standards folder above a per-repository folder
contributes both sets to the chats inside.

#### Acceptance Criteria

1. WHEN the Steering_Resolver computes Effective_Steering_Dirs for a folder, THE Steering_Resolver SHALL walk the `parent_id` chain to the root and SHALL include every ancestor's `steering_dirs`.
2. WHEN assembling the result, THE Steering_Resolver SHALL order entries root-first (an ancestor's directories precede a descendant's).
3. WHEN two folders in the chain list the same realpath, THE Steering_Resolver SHALL keep the first occurrence only.
4. IF the `parent_id` chain contains a cycle or names a missing folder, THEN THE Steering_Resolver SHALL stop the walk at that point and return the directories collected from the folders visited before it, matching the walk contract of the existing project-directory resolver.
5. WHEN the Steering_Resolver reads a stored `steering_dirs` value, THE Steering_Resolver SHALL re-validate it with the Requirement 1 contract rather than trusting the stored value.
6. IF a stored value fails re-validation, THEN THE Steering_Resolver SHALL return an error naming the offending directory and no directories.
7. FOR ALL folder trees, the Effective_Steering_Dirs of a folder with no `steering_dirs` anywhere in its chain SHALL be the empty list.

### Requirement 3: Universal Delivery

**User Story:** As a dashboard user, I want the steering from my folder to reach the model
no matter which provider or agent the chat runs on, so that I never have to know or care
which backend a chat uses.

#### Acceptance Criteria

1. WHEN a Chat_Slot with non-empty Effective_Steering_Dirs begins a Fresh_Session, THE Context_Builder SHALL include the body of every Steering_Document under those directories in the session-start context it returns to the Chat_Runner.
2. WHILE assembling that context, THE Context_Builder SHALL include folder Steering_Documents for every Provider (kiro-cli, Claude Code, Codex, KAS, and any config-authored harness) with no Provider-specific branch on the folder-steering path.
3. WHILE assembling that context, THE Context_Builder SHALL include folder Steering_Documents whether the chat's agent is the default `kirocrew` agent or a custom agent.
4. WHEN a Member_Chat begins a Fresh_Session, THE Context_Builder SHALL include folder Steering_Documents inside the member essentials envelope with their bodies present (not stripped as host-native documents).
5. WHEN reading a Steering_Document, THE Context_Builder SHALL admit the file against its own Steering_Dir as the trust base using the existing steering admissibility check, and SHALL skip any file that fails it.
6. WHEN a `*.md` file under a Steering_Dir carries frontmatter `inclusion` of `manual`, `auto`, or `fileMatch`, THE Context_Builder SHALL skip it.
7. WHEN the assembled folder steering on the non-member path exceeds the Steering_Cap, THE Context_Builder SHALL truncate it at the cap and append the existing `[steering truncated]` marker; on the Member_Chat path THE Context_Builder SHALL apply the member envelope's existing document-count and byte bounds instead.
8. WHEN a Steering_Document's realpath lies under the chat project's `.kiro/steering` or under `~/.kiro/steering`, THE Context_Builder SHALL skip it, because every Provider path already delivers project and global steering.
9. FOR ALL Steering_Documents reachable from more than one Effective_Steering_Dir, THE Context_Builder SHALL include each document at most once per session-start context.
10. WHILE a turn is not a Fresh_Session and not a Reinjection_Turn, THE Context_Builder SHALL NOT include folder Steering_Documents.

### Requirement 4: Single Delivery Seam

**User Story:** As a Kiro Crew maintainer, I want folder steering delivered at the one layer
every provider passes through, so that adding a provider (by code or by config) can never
silently drop it.

#### Acceptance Criteria

1. FOR ALL Providers, THE Harness SHALL produce an identical `SpawnPlan` for a chat with Effective_Steering_Dirs and for the same chat without them.
2. THE `SpawnContext` SHALL NOT carry a folder-steering field, and THE `AcpRuntime` constructor SHALL NOT accept one.
3. THE kiro Harness SHALL NOT compute or declare folder Steering_Documents as native launch documents.
4. WHEN the member native-launch declaration (`kiro_launch_documents`) is computed, THE Context_Builder SHALL NOT declare folder Steering_Documents as host-native, so their bodies are never stripped from the member envelope.
5. WHEN the Chat_Runner builds a turn, THE Chat_Runner SHALL pass the chat's Effective_Steering_Dirs to the Context_Builder through `build_message`, and THE Context_Builder SHALL thread them to `build_session_context` and the member essentials path.

### Requirement 5: Live Resolution From The Folder Tree

**User Story:** As a dashboard user, I want edits to a folder's steering directories to
apply to the chats already in that folder, so that I do not have to recreate chats after
changing the standards.

#### Acceptance Criteria

1. WHEN the Chat_Runner builds a Fresh_Session or Reinjection_Turn, THE Chat_Runner SHALL resolve Effective_Steering_Dirs from the Folder_Store's current committed tree using the slot's current `folder_id`, not from a value cached on the Chat_Slot.
2. THE Chat_Slot SHALL NOT persist a `steering_dirs` field, and THE Folder_Store's slot-create and agent-switch endpoints SHALL NOT compute or store one.
3. WHEN a Chat_Slot's `folder_id` changes to a different folder, THE Chat_Runner SHALL use that folder's Effective_Steering_Dirs at the chat's next Fresh_Session.
4. WHEN a Chat_Slot has no `folder_id`, THE Chat_Runner SHALL pass no Effective_Steering_Dirs.

### Requirement 6: Compaction Reinjection

**User Story:** As a dashboard user, I want folder steering to survive provider context
compaction, so that a long session keeps following the standards it started with.

#### Acceptance Criteria

1. WHEN a Reinjection_Turn is built for a Chat_Slot with non-empty Effective_Steering_Dirs, THE Context_Builder SHALL re-include the folder Steering_Documents under a header that reads `[REINJECTED AFTER COMPACTION — folder steering]`.
2. WHEN re-including on a Reinjection_Turn, THE Context_Builder SHALL apply the same admissibility, inclusion, dedup, and Steering_Cap rules as the Fresh_Session path.

### Requirement 7: Graceful Degradation

**User Story:** As a dashboard user, I want a folder whose steering directory has moved or
become unreadable to keep working, so that one stale path never breaks my chats.

#### Acceptance Criteria

1. IF an Effective_Steering_Dir does not exist at build time, THEN THE Context_Builder SHALL contribute nothing for it, SHALL log at debug level only, and SHALL complete the turn normally.
2. IF a Steering_Document cannot be read (permission or I/O error), THEN THE Context_Builder SHALL skip that document, SHALL log at debug level only, and SHALL complete the turn normally.
3. IF the Steering_Resolver returns an error for the slot's folder chain at build time, THEN THE Chat_Runner SHALL build the turn with no folder steering and SHALL log a warning naming the slot and the error.
4. IF a stored Steering_Dir has become a sensitive path since it was saved, THEN THE Steering_Resolver SHALL reject it at re-validation, AND THE Context_Builder SHALL independently apply its own steering admissibility check to every file before reading it, so a directory that bypassed the resolver is still never read.

### Requirement 8: Folder Dialog

**User Story:** As a dashboard user, I want to manage a folder's steering directories in the
folder dialog, so that I can add and remove them without editing files.

#### Acceptance Criteria

1. WHEN the Folder_Config_Modal opens for a folder, THE Folder_Config_Modal SHALL show an "Additional steering" section listing that folder's own `steering_dirs`.
2. WHEN the user activates "Add directory", THE Folder_Config_Modal SHALL open the existing directory picker and SHALL append the chosen directory to the list; WHEN the picker closes without a selection, THE Folder_Config_Modal SHALL leave the list unchanged.
3. WHEN the user removes an entry, THE Folder_Config_Modal SHALL drop it from the list before save.
4. WHEN the folder has ancestors with `steering_dirs`, THE Folder_Config_Modal SHALL display the inherited directories read-only and distinct from the folder's own entries.
5. WHEN the server rejects a save for any reason, including `steering_dirs_invalid`, THE Folder_Config_Modal SHALL show the server's error message in the modal's error area, falling back to the generic save-failed string when the response carries none.
6. WHEN saving, THE Folder_Config_Modal SHALL send `steering_dirs` as a list of strings alongside the existing folder fields.
7. FOR ALL supported locales, THE website SHALL provide translations for every new "Additional steering" string, and the i18n parity check SHALL pass.

### Requirement 9: System Specs

**User Story:** As a Kiro Crew maintainer, I want the system specs to describe folder
steering accurately, so that the documented architecture matches the code.

#### Acceptance Criteria

1. WHEN the feature's change set is complete, THE `docs/system-specs/modules/config.md` SHALL describe the folder `steering_dirs` field, its validation contract, and accumulative inheritance.
2. WHEN the feature's change set is complete, THE `docs/system-specs/modules/providers.md` SHALL state that folder steering is delivered by the Context_Builder for every Provider and SHALL NOT describe any per-Harness launch-document delivery for it.
