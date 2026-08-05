# Implementation Plan: Subagent Session Cleanup

## Overview

Implement provider-agnostic session file cleanup for subagent sessions. The implementation proceeds bottom-up: path safety utility → provider interface → provider implementations → SessionManager integration → SubagentManager integration → persistence tracking → startup sweep → tombstone pruning enhancement.

## Tasks

- [ ] 1. Add path safety utility and cleanup_session to LLMProvider
  - [x] 1.1 Add `_is_safe_path` helper function to a shared module (`providers/cleanup.py`)
    - Implement path validation: resolve target, resolve root, verify target is under root
    - Handle OSError/ValueError gracefully
    - _Requirements: 7.2, 7.3_
  - [x] 1.2 Add `session_id` property and `cleanup_session` default method to `LLMProvider` ABC in `providers/base.py`
    - Add `@property session_id(self) -> str` returning empty string by default
    - Add `async def cleanup_session(self, session_id: str) -> None` with no-op default
    - Document that cleanup_session only operates on filesystem, not provider process
    - _Requirements: 1.1, 6.2_
  - [x] 1.3 Write property tests for `_is_safe_path`
    - **Property 5: Path traversal is blocked**
    - **Validates: Requirements 7.2**

- [ ] 2. Implement provider-specific cleanup methods
  - [x] 2.1 Implement `session_id` property and `cleanup_session` in `AcpProvider` (`providers/acp.py`)
    - Override `session_id` property to return `self._client._session_id`
    - Delete `~/.kiro/sessions/cli/{session_id}.json` and `.jsonl` files (not directories)
    - Use `_is_safe_path` before deletion
    - Use `unlink(missing_ok=True)` for idempotency
    - Catch and log all OSError exceptions
    - _Requirements: 1.2, 1.6, 7.2, 7.4_
  - [x] 2.2 Implement `session_id` property and `cleanup_session` in `ClaudeCodeProvider` (`providers/claude_code.py`)
    - Override `session_id` property to return `self._session_id`
    - Ephemeral mode: return immediately (no-op)
    - Per-session mode: delete session directory with `shutil.rmtree(ignore_errors=True)`
    - Use `_is_safe_path` before deletion
    - _Requirements: 1.3, 1.4, 1.6_
  - [x] 2.3 Write property tests for ACP cleanup
    - **Property 1: ACP cleanup deletes all session files**
    - **Validates: Requirements 1.2**
  - [x] 2.4 Write property tests for cleanup error resilience
    - **Property 3: Cleanup never raises exceptions**
    - **Property 4: Cleanup is idempotent**
    - **Validates: Requirements 1.6, 7.4**

- [ ] 3. Integrate cleanup into SessionManager
  - [x] 3.1 Add `_safe_cleanup` async method and extend `release()` with `cleanup` parameter
    - Add `cleanup: bool = False` parameter to `release()`
    - Use `provider.session_id` property directly (no `_get_provider_session_id` helper needed)
    - When `cleanup=True` and key starts with `subagent:`, get session_id and schedule cleanup
    - Use `asyncio.ensure_future` for fire-and-forget cleanup (safe because cleanup only touches filesystem)
    - _Requirements: 2.1, 2.4, 6.2, 7.1_
  - [x] 3.2 Write property test for cleanup restriction
    - **Property 6: Cleanup restricted to subagent sessions**
    - **Validates: Requirements 2.4, 7.1**

- [x] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 5. Integrate cleanup into SubagentManager
  - [x] 5.1 Update `_run()` finally block to pass `cleanup=True` to `release()`
    - Change `self._sessions.release(session_key)` to `self._sessions.release(session_key, cleanup=True)`
    - _Requirements: 2.1, 2.2_
  - [x] 5.2 Update `_force_reap()` to pass `cleanup=True` to `release()`
    - Change `self._sessions.release(session_key)` to `self._sessions.release(session_key, cleanup=True)`
    - _Requirements: 3.1_
  - [x] 5.3 Write unit tests for SubagentManager cleanup integration
    - Test completion flow calls cleanup
    - Test error completion flow calls cleanup
    - Test cleanup failure doesn't disrupt completion
    - _Requirements: 2.1, 2.2, 2.3, 3.1, 3.2_

- [ ] 6. Add session ID tracking to subagent persistence
  - [x] 6.1 Record session_id in state.json after session creation
    - In `_run_inner()`, after `get_or_create()` returns, use `provider.session_id` property
    - Call `update_state(info.id, session_id=session_id, provider=provider_type)`
    - Handle case where session_id is not available (store empty string)
    - _Requirements: 6.1, 6.2, 6.3_
  - [x] 6.2 Write property test for session ID persistence
    - **Property 7: Session ID correctly persisted**
    - **Validates: Requirements 6.1, 6.2**

- [ ] 7. Enhance tombstone pruning with session file cleanup
  - [x] 7.1 Add `_cleanup_session_files_sync` helper to `subagent_persistence.py`
    - Synchronous cleanup that reads session_id from state.json
    - Deletes corresponding session files based on provider type
    - Uses `_is_safe_path` for validation
    - Handles missing/corrupt state gracefully
    - _Requirements: 4.1, 4.2, 4.3_
  - [x] 7.2 Update `prune_stale_tombstones` to call session file cleanup before folder deletion
    - Read session_id from state.json or tombstone.json
    - Call `_cleanup_session_files_sync` before `shutil.rmtree`
    - _Requirements: 4.1, 4.2_
  - [x] 7.3 Write property test for tombstone pruning cleanup
    - **Property 8: Tombstone pruning cleans session files**
    - **Validates: Requirements 4.1**

- [ ] 8. Add startup sweep for orphaned session files
  - [x] 8.1 Extend `_reconcile_orphans()` to clean up session files for tombstoned agents
    - After writing tombstone, read session_id from state
    - Call async cleanup for the session files
    - Rate limit: process in batches of 50 with `await asyncio.sleep(0)` between batches to prevent event loop starvation
    - Handle errors per-entry without stopping the sweep
    - _Requirements: 5.1, 5.2, 5.3, 5.4_
  - [x] 8.2 Write property test for startup sweep
    - **Property 9: Startup sweep processes all tombstoned entries**
    - **Validates: Requirements 5.2, 5.4**

- [x] 9. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- All tasks including tests are required
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties
- Unit tests validate specific examples and edge cases
- Build command: `black && isort && flake8 && mypy && python -m pytest`
- Test framework: pytest + pytest-asyncio + Hypothesis
