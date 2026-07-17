# Requirements Document

## Introduction

Provide a graceful migration mechanism for KiroCrew builtin apps when they are extracted into standalone packages. Users must never suddenly lose an app they were using. The migration spans at least two releases: the first marks the app as "transitioning" with warnings, and the second removes the builtin code while preserving a helpful migration page instead of a 404.

## Glossary

- **Gateway**: The KiroCrew gateway service responsible for app lifecycle management and API routing
- **Builtin_App**: A feature module baked into the KiroCrew dashboard, using the host React router (no ESM bundle)
- **Standalone_App**: An independent app package extracted from a builtin app, installable via the App Store
- **Migration_Phase**: The migration stage — Phase 1 (deprecation warning) or Phase 2 (code removed + fallback view)
- **migratedTo**: A field in the builtin app definition pointing to the replacement standalone app identifier
- **Migration_Page**: A fallback page shown when builtin code has been removed, guiding users to install the standalone version
- **InstalledApp_Metadata**: The `installed.json` metadata file for each app
- **App_Store**: The dashboard page for app management, containing "Installed" and "Browse" tabs
- **Sidebar**: The left navigation bar displaying page entries for enabled apps
- **Orphaned_Builtin**: A builtin app whose `installed.json` still exists on disk but whose entry has been removed from the `_BUILTIN_APPS` list

## Requirements

### Requirement 1: Phase 1 Deprecation Marking

**User Story:** As a platform developer, I want to mark a builtin app as migrating to a standalone package, so that the system can warn users before the code is removed.

#### Acceptance Criteria

1. THE Builtin_App definition SHALL support an optional `migratedTo` string field with format `"registry:{app-name}"` or `"standalone:{app-name}"`
2. WHEN the `migratedTo` field is present in a Builtin_App definition, THE Gateway SHALL persist that value into InstalledApp_Metadata during registration
3. WHEN the Gateway starts and registers builtin apps, THE Gateway SHALL sync the `migratedTo` field to `installed.json`
4. WHEN the `migratedTo` field is set, THE Builtin_App SHALL continue to function normally without any degradation

### Requirement 2: Phase 1 Deprecation Warning Display

**User Story:** As a user, I want to see clear warnings when a builtin app is about to be migrated, so that I have time to install the standalone version.

#### Acceptance Criteria

1. WHEN a builtin app has its `migratedTo` field set, THE frontend SHALL display a persistent migration warning banner at the top of that app's page
2. WHEN displaying the migration warning banner, THE banner SHALL include: the app name, the migration target, and a prompt to install the standalone version before the next update
3. WHEN displaying the migration warning banner, THE banner SHALL provide a button that navigates to the App Store to install the standalone version
4. WHEN a builtin app has its `migratedTo` field set, THE Installed_Tab SHALL display a migration badge next to that app's entry

### Requirement 3: Phase 2 Orphaned Builtin Detection

**User Story:** As the system, I need to detect builtin apps that have been removed from code but still have user data on disk, so that I can provide migration guidance.

#### Acceptance Criteria

1. WHEN the Gateway starts, THE Gateway SHALL scan the `~/.kirocrew/apps/` directory for apps with `origin=builtin`
2. WHEN an app with `origin=builtin` exists on disk but is not in the `_BUILTIN_APPS` list, THE Gateway SHALL mark it as an Orphaned_Builtin
3. WHEN listing apps, THE Gateway API SHALL include an `orphaned: true` field in the response data for Orphaned_Builtin entries
4. WHEN an Orphaned_Builtin is detected, THE Gateway SHALL preserve its `installed.json` and `data/` directory without modification

### Requirement 4: Phase 2 Migration Page

**User Story:** As a user, when I click on a removed builtin app, I want to see a helpful migration page instead of a 404 error.

#### Acceptance Criteria

1. WHEN a user clicks a Sidebar entry for an Orphaned_Builtin, THE frontend SHALL display a Migration_Page instead of a 404 page
2. WHEN displaying the Migration_Page, THE page SHALL include: the app name, a migration explanation, and an "Install from App Store" button
3. WHEN the Migration_Page "Install" button is clicked, THE system SHALL trigger the App Store install flow for the app specified in `migratedTo`
4. WHEN the standalone version is already installed, THE Migration_Page SHALL display a "Migration complete ✓" status and offer an option to clean up the old entry
5. WHEN the Migration_Page is displayed, THE page SHALL inform the user that their data (`data/` directory) has been preserved and is accessible to the standalone version

### Requirement 5: Sidebar Continued Visibility

**User Story:** As a user, I want the app entry to remain visible in the Sidebar even after the builtin code is removed, so that I notice the change and can take action.

#### Acceptance Criteria

1. WHILE an Orphaned_Builtin's `installed.json` exists with `enabled=true`, THE Sidebar SHALL continue to display that app's entry
2. WHEN displaying an Orphaned_Builtin's Sidebar entry, THE Sidebar SHALL add a visual indicator (such as a migration icon) to signal that the app needs migration
3. WHEN a user disables an Orphaned_Builtin from the Installed_Tab, THE Sidebar SHALL remove that app's entry

### Requirement 6: Post-Migration Cleanup

**User Story:** As a user, after I successfully install the standalone version, I want the system to clean up the old builtin app entry.

#### Acceptance Criteria

1. WHEN a user confirms cleanup of an Orphaned_Builtin, THE Gateway SHALL remove its `installed.json` file
2. WHEN cleaning up an Orphaned_Builtin, THE Gateway SHALL preserve the `data/` directory for use by the standalone version
3. WHEN the standalone version is installed and the user triggers cleanup, THE Sidebar SHALL remove the old Orphaned_Builtin entry
4. IF the cleanup operation fails, THEN THE Gateway SHALL return an error message and not modify any files

### Requirement 7: Data Preservation and Sharing

**User Story:** As a user, I want my data to be preserved when migrating to the standalone version.

#### Acceptance Criteria

1. THE Gateway SHALL ensure the Orphaned_Builtin's `~/.kirocrew/apps/{name}/data/` directory remains unchanged throughout the entire migration process
2. WHEN the standalone version is installed, THE Standalone_App SHALL use the same app name as the builtin it replaces, ensuring it accesses the same `~/.kirocrew/apps/{name}/data/` directory path
3. WHEN cleaning up the old entry, THE Gateway SHALL only remove `installed.json` and `app.json`, preserving the `data/` directory
4. THE `migratedTo` field SHALL reference the same app name as the builtin (format: `"registry:{same-name}"`), ensuring data directory path identity without symlinks

### Requirement 8: API Support

**User Story:** As a frontend developer, I need the API to provide sufficient information to distinguish normal apps, migrating apps, and orphaned apps.

#### Acceptance Criteria

1. WHEN the frontend requests the app list, THE Gateway API SHALL include the `migratedTo` field (if present) in each app's response data
2. WHEN the frontend requests the app list, THE Gateway API SHALL include `orphaned: true` and `migratedTo` fields in Orphaned_Builtin response data
3. THE Gateway API SHALL provide a cleanup endpoint `DELETE /api/apps/{name}/migrate-cleanup` for removing Orphaned_Builtin metadata
4. WHEN the cleanup endpoint is called, THE Gateway API SHALL verify that the target app is indeed an Orphaned_Builtin and that the standalone version is installed before executing cleanup
