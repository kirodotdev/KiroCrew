# Requirements Document

## Introduction

Subagent spawns (cron notifications, watchlist checks, Mochi pet actions, user-initiated spawns) create LLM provider session files on disk that persist indefinitely after the subagent completes. The gateway's `SubagentManager` cleans up its own metadata (`~/.kirocrew/subagents/` folders with tombstones and 7-day pruning), but the underlying LLM provider session files (e.g., `~/.kiro/sessions/cli/{session_id}/`) have no cleanup mechanism. This leads to unbounded disk growth — one user accumulated 26,000+ orphan session files in two weeks.

This feature adds a provider-agnostic session file cleanup mechanism that integrates with the existing subagent lifecycle, ensuring session files are deleted when subagents complete, are reaped, or are pruned during tombstone cleanup.

## Glossary

- **Session_Cleanup_Interface**: An abstract method on `LLMProvider` that each provider implements to delete its own on-disk session files given a session identifier.
- **SessionManager**: The component (`session.py`) that maps session keys to `LLMProvider` instances and manages their lifecycle.
- **SubagentManager**: The component (`subagent.py`) that spawns, tracks, and reaps isolated background agents.
- **ACP_Provider**: The LLM provider wrapping kiro-cli (JSON-RPC over stdio), storing sessions in `~/.kiro/sessions/cli/{session_id}/`.
- **Claude_Code_Provider**: The LLM provider wrapping the `claude` CLI, operating in `per_session` or `ephemeral` mode.
- **Bedrock_Provider**: The text-only LLM provider using AWS Bedrock `converse_stream()` with no persistent session files.
- **Tombstone**: A JSON file (`tombstone.json`) written to a subagent's folder on abnormal exit, marking it for eventual pruning.
- **Reaper**: The periodic loop in `SubagentManager` that force-kills subagents exceeding the timeout deadline.
- **Startup_Sweep**: A one-time scan on gateway startup that identifies and cleans up session files belonging to completed or tombstoned subagents from a prior gateway run.

## Requirements

### Requirement 1: Provider-Agnostic Cleanup Interface

**User Story:** As a gateway developer, I want a provider-agnostic cleanup method on the LLM provider interface, so that each backend can implement deletion of its own session files without coupling the cleanup logic to specific providers.

#### Acceptance Criteria

1. THE Session_Cleanup_Interface SHALL define an async method `cleanup_session(session_id: str) -> None` on the `LLMProvider` abstract base class
2. WHEN `cleanup_session` is called on the ACP_Provider, THE ACP_Provider SHALL delete the session directory at `~/.kiro/sessions/cli/{session_id}/` including all contained files
3. WHEN `cleanup_session` is called on the Claude_Code_Provider in ephemeral mode, THE Claude_Code_Provider SHALL perform no file deletion (no-op)
4. WHEN `cleanup_session` is called on the Claude_Code_Provider in per_session mode, THE Claude_Code_Provider SHALL delete the session state files associated with that session
5. WHEN `cleanup_session` is called on the Bedrock_Provider, THE Bedrock_Provider SHALL perform no file deletion (no-op)
6. IF `cleanup_session` encounters a filesystem error, THEN THE Session_Cleanup_Interface SHALL log a warning and return without raising an exception

### Requirement 2: Cleanup on Subagent Completion

**User Story:** As a system operator, I want subagent session files to be automatically deleted when a subagent completes successfully, so that disk space is reclaimed immediately after the session is no longer needed.

#### Acceptance Criteria

1. WHEN a subagent completes successfully, THE SubagentManager SHALL call `cleanup_session` for that subagent's session before releasing the session key
2. WHEN a subagent completes with an error, THE SubagentManager SHALL call `cleanup_session` for that subagent's session before releasing the session key
3. WHEN `cleanup_session` fails during subagent completion, THE SubagentManager SHALL log a warning and continue the normal completion flow without interruption
4. THE SubagentManager SHALL only call `cleanup_session` for sessions with the `subagent:` prefix, preserving long-lived dashboard and Slack sessions

### Requirement 3: Cleanup on Reaper Force-Kill

**User Story:** As a system operator, I want session files to be cleaned up when the reaper force-kills a timed-out subagent, so that abandoned sessions do not accumulate on disk.

#### Acceptance Criteria

1. WHEN the Reaper force-kills a timed-out subagent, THE Reaper SHALL call `cleanup_session` for that subagent's session after terminating the process
2. IF `cleanup_session` fails during reaping, THEN THE Reaper SHALL log a warning and continue the reaping flow without interruption

### Requirement 4: Cleanup During Tombstone Pruning

**User Story:** As a system operator, I want session files associated with tombstoned subagents to be cleaned up during the 7-day tombstone pruning cycle, so that session files from abnormally exited subagents are eventually reclaimed.

#### Acceptance Criteria

1. WHEN `prune_stale_tombstones` deletes a tombstoned subagent folder, THE pruning logic SHALL also attempt to delete the corresponding LLM provider session files
2. THE pruning logic SHALL read the session identifier from the tombstoned subagent's `state.json` to determine which session files to delete
3. IF the session identifier is missing or the session files do not exist, THEN THE pruning logic SHALL skip cleanup for that entry without error

### Requirement 5: Startup Sweep

**User Story:** As a system operator, I want the gateway to clean up orphaned session files from prior runs on startup, so that session files from subagents that completed while the gateway was down are eventually reclaimed.

#### Acceptance Criteria

1. WHEN the gateway starts, THE Startup_Sweep SHALL scan all tombstoned subagent folders for associated session identifiers
2. WHEN a tombstoned subagent has a recorded session identifier, THE Startup_Sweep SHALL delete the corresponding LLM provider session files if they still exist on disk
3. THE Startup_Sweep SHALL execute asynchronously without blocking gateway startup
4. IF the Startup_Sweep encounters errors for individual entries, THEN THE Startup_Sweep SHALL log warnings and continue processing remaining entries

### Requirement 6: Session Identifier Tracking

**User Story:** As a gateway developer, I want the subagent's LLM session identifier to be persisted in the subagent's state, so that cleanup can locate the correct session files even after the in-memory session is released.

#### Acceptance Criteria

1. WHEN a subagent session is created, THE SubagentManager SHALL record the LLM provider's session identifier in the subagent's `state.json`
2. THE session identifier SHALL be the provider-specific identifier used to locate session files on disk (e.g., the kiro-cli session UUID for ACP_Provider)
3. WHEN the session identifier is not available (provider does not use persistent files), THE SubagentManager SHALL record an empty string

### Requirement 7: Safety Constraints

**User Story:** As a system operator, I want the cleanup mechanism to only delete subagent session files and never affect long-lived sessions, so that active dashboard chats and Slack conversations are protected.

#### Acceptance Criteria

1. THE SessionManager SHALL only invoke `cleanup_session` for session keys with the `subagent:` prefix
2. THE cleanup logic SHALL validate that the session directory path is under the expected provider session root before deletion
3. IF a path traversal or unexpected path is detected, THEN THE cleanup logic SHALL refuse deletion and log an error
4. THE cleanup logic SHALL handle the case where session files have already been deleted (idempotent operation)
