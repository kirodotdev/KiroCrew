# Implementation Plan: Builtin App Migration

## Overview

Implement the two-phase graceful migration mechanism for builtin apps. Backend changes in Python (KiroCrew gateway), frontend changes in TypeScript/React (KiroCrewWebsite).

## Tasks

- [ ] 1. Extend InstalledApp dataclass and builtin app registration
  - [x] 1.1 Add `migratedTo` field to `InstalledApp` dataclass in `manager.py`
    - Add field with default empty string
    - Update `to_dict()` and `from_dict()` to handle the new field
    - _Requirements: 1.1, 1.2_
  - [x] 1.2 Update `register_builtin_apps()` to persist `migratedTo` from app definitions
    - When a builtin app definition has `migratedTo`, write it to installed.json
    - On re-registration, sync `migratedTo` from definition (overwrite stale values)
    - _Requirements: 1.2, 1.3_
  - [x] 1.3 Update `_validate_builtin_app()` to validate `migratedTo` format
    - Accept `"registry:{name}"` or `"standalone:{name}"` format
    - Reject invalid formats with warning (don't block registration)
    - _Requirements: 1.1_
  - [x] 1.4 Write property test for migratedTo persistence round-trip
    - **Property 2: migratedTo Persistence Round-Trip**
    - **Validates: Requirements 1.2, 1.3**
  - [x] 1.5 Write property test for registration preserves functionality
    - **Property 3: Registration Preserves Functionality**
    - **Validates: Requirements 1.4**

- [ ] 2. Implement orphan detection
  - [x] 2.1 Add `detect_orphaned_builtins()` function in `manager.py`
    - Scan apps_dir for `origin=builtin` entries not in `_BUILTIN_APPS`
    - Return list of orphaned app names
    - _Requirements: 3.1, 3.2_
  - [x] 2.2 Enhance `list_apps()` to include `orphaned` and `migratedTo` in response
    - Call `detect_orphaned_builtins()` to get orphan set
    - Add `orphaned: true` to response for orphaned apps
    - Include `migratedTo` field in all app responses where present
    - _Requirements: 3.3, 8.1, 8.2_
  - [x] 2.3 Write property test for orphan detection correctness
    - **Property 4: Orphan Detection Correctness**
    - **Validates: Requirements 3.2**
  - [x] 2.4 Write property test for data directory invariant
    - **Property 6: Data Directory Invariant**
    - **Validates: Requirements 3.4, 7.1**

- [ ] 3. Implement cleanup endpoint
  - [x] 3.1 Add `cleanup_migrated_builtin()` function in `manager.py`
    - Validate target is orphaned builtin
    - Validate standalone replacement is installed
    - Remove `installed.json` and `app.json`, preserve `data/`
    - Return AppResult with success/error
    - _Requirements: 6.1, 6.2, 7.3, 8.4_
  - [x] 3.2 Add `DELETE /api/apps/{name}/migrate-cleanup` route in `routes.py`
    - Wire to cleanup function
    - Return appropriate HTTP status codes (400, 409, 500)
    - _Requirements: 8.3, 8.4_
  - [x] 3.3 Write property test for cleanup removes metadata only
    - **Property 11: Cleanup Removes Metadata Only**
    - **Validates: Requirements 6.1, 6.2, 7.3**
  - [x] 3.4 Write property test for cleanup validation
    - **Property 13: Cleanup Validation**
    - **Validates: Requirements 8.4**
  - [x] 3.5 Write property test for cleanup failure atomicity
    - **Property 12: Cleanup Failure Atomicity**
    - **Validates: Requirements 6.4**

- [x] 4. Checkpoint - Backend complete
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 5. Frontend: MigrationBanner component (Phase 1)
  - [x] 5.1 Create `MigrationBanner.tsx` component
    - Amber warning banner with app name, migration target, install button
    - Button navigates to App Store with the standalone app pre-selected
    - Use shared UI components (Card, Btn) and Lucide icons
    - _Requirements: 2.1, 2.2, 2.3_
  - [x] 5.2 Integrate MigrationBanner into app pages
    - In the dynamic app rendering logic, check for `migratedTo` field
    - Display banner at top of builtin app pages when `migratedTo` is set
    - _Requirements: 2.1_
  - [x] 5.3 Add migration badge to Installed tab in AppsPage
    - Show badge next to apps with `migratedTo` set
    - Use Badge component with "warn" variant
    - _Requirements: 2.4_
  - [x] 5.4 Write property test for migration banner content
    - **Property 7: Migration Banner Content**
    - **Validates: Requirements 2.2**

- [ ] 6. Frontend: MigrationPage component (Phase 2)
  - [x] 6.1 Create `MigrationPage.tsx` component
    - Full page with app name, migration explanation, data preservation notice
    - "Install from App Store" button triggers install flow
    - "Migration complete ✓" state when standalone is installed
    - "Clean up old entry" button calls cleanup endpoint
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_
  - [x] 6.2 Add `/apps/migrate/:name` route in `App.tsx`
    - Route renders MigrationPage component
    - Page fetches app info from API to determine state
    - _Requirements: 4.1_
  - [x] 6.3 Write property test for migration page content
    - **Property 8: Migration Page Content**
    - **Validates: Requirements 4.2**

- [ ] 7. Frontend: Sidebar and navigation for orphaned apps
  - [x] 7.1 Update `appNavItems` logic in `App.tsx` to handle orphaned apps
    - Orphaned apps with `enabled=true` still appear in sidebar
    - Route orphaned apps to `/apps/migrate/{name}` instead of original route
    - Add visual migration indicator icon for orphaned entries
    - _Requirements: 5.1, 5.2_
  - [x] 7.2 Ensure disable removes orphaned app from sidebar
    - Existing disable_app flow already handles this via `enabled=false`
    - Verify appNavItems filters out disabled orphaned apps
    - _Requirements: 5.3_
  - [x] 7.3 Write property test for sidebar visibility
    - **Property 9: Sidebar Visibility for Enabled Orphaned Apps**
    - **Validates: Requirements 5.1**

- [x] 8. Final checkpoint - All tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- All tasks including property tests are required
- Backend uses pytest + Hypothesis for property tests
- Frontend uses fast-check for property tests
- The existing `disable_app()` and `enable_app()` functions already handle the enabled state toggle — no changes needed there
- The `list_apps()` function already scans the apps directory — we enhance its response rather than creating a new endpoint
