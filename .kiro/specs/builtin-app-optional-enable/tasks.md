# Implementation Plan: Builtin App Optional Enable

## Overview

Minimal changes to support `defaultEnabled` in builtin app definitions. Backend reads the field during first-time registration; frontend merges disabled builtins into the Browse tab. Existing apps and behavior are unchanged.

## Tasks

- [x] 1. Backend: Add defaultEnabled support to register_builtin_apps()
  - [x] 1.1 Add `_validate_builtin_app()` function to `manager.py`
    - Validate required fields: name, version, displayName, description, author
    - Validate `defaultEnabled` is boolean if present
    - Validate name is path-safe
    - Return list of error strings (empty = valid)
    - Add inline docstring documenting the Builtin_App_Definition schema with all supported fields
    - _Requirements: 8.2, 8.4, 8.5_

  - [x] 1.2 Update `register_builtin_apps()` to use `defaultEnabled` and validation
    - Before registering each app, call `_validate_builtin_app()`; if errors, log warning and skip
    - In the `else` branch (new app), read `app_data.get("defaultEnabled", True)` and use as `enabled` value
    - Existing apps branch remains unchanged (preserves user state)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 8.3, 8.4_

  - [x] 1.3 Write property tests for registration logic
    - **Property 1: First-time registration respects defaultEnabled**
    - **Property 2: Re-registration preserves user state**
    - **Property 3: All builtin apps have lifecycle=locked**
    - **Property 8: Invalid definitions are skipped without affecting others**
    - **Validates: Requirements 1.1, 1.2, 1.3, 1.4, 2.3, 5.4, 8.4**

- [x] 2. Checkpoint - Backend tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Frontend: Show disabled builtins in Browse tab
  - [x] 3.1 Update Browse tab in `AppsPage.tsx` to include disabled builtins
    - Derive `disabledBuiltins` from the `apps` state: filter by `origin === 'builtin' && !enabled`
    - Map to `RegistryApp` shape with name, displayName, description, version, author, tags, origin, lifecycle
    - Merge with `registry` array for the browse display list
    - Apply the existing `registryFilter` search to the merged list
    - _Requirements: 3.1, 3.2, 3.4_

  - [x] 3.2 Add "Enable" button for disabled builtin app cards in Browse tab
    - In the card footer, check if `app.origin === 'builtin' && app.installed && !app.enabled`
    - If so, render an "Enable" button that calls `handleAction(app.name, 'enable')`
    - After enable, dispatch `mc:apps-changed` event (already done by handleAction)
    - _Requirements: 3.3, 4.1, 4.4_

  - [x] 3.3 Write property tests for browse filter logic
    - **Property 4: Browse filter shows exactly disabled builtins**
    - **Property 5: Sidebar filter shows only enabled apps with pages**
    - **Validates: Requirements 3.1, 4.2, 4.4, 5.2, 5.3**

- [x] 4. Checkpoint - Frontend and backend tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. End-to-end verification
  - [x] 5.1 Add a test builtin app with `defaultEnabled: false` to `_BUILTIN_APPS`
    - Add a commented-out example entry showing the full schema with `defaultEnabled: false`
    - This serves as developer documentation and can be uncommented for manual testing
    - _Requirements: 8.2, 8.5_

  - [x] 5.2 Write unit tests for enable/disable round-trip and API completeness
    - **Property 6: Enable/disable round-trip persists state**
    - **Property 7: API returns complete manifest for all builtins**
    - **Validates: Requirements 4.1, 5.1, 6.1, 7.1, 7.3**

- [x] 6. Final checkpoint - All tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- All tasks including property tests are required
- The sidebar and installed tab require zero code changes — existing filter logic handles everything
- The existing `enable_app()` / `disable_app()` functions work as-is for builtin apps
- Property tests use `hypothesis` library for generation
