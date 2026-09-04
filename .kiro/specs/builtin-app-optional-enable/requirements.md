# Requirements Document

## Introduction

Extend KiroCrew's builtin app system to support "default disabled" apps. The existing 5 builtin apps remain unchanged (enabled by default, locked lifecycle). New builtin apps can be configured to default to disabled, appear in the App Store Browse tab for discovery, and when enabled by the user, show in the Installed tab and sidebar. Additionally, provide a standard developer configuration interface for builtin app authors to declare app metadata and behavior until the package-separated App Store receives security approval.

## Glossary

- **Gateway**: The KiroCrew gateway service responsible for app lifecycle management and API routing
- **Builtin_App**: A feature module baked into the KiroCrew dashboard, using the host React router (no ESM bundle)
- **App_Store**: The dashboard page for app management, containing "Installed" and "Browse" tabs
- **Installed_Tab**: The App Store tab showing installed and enabled apps
- **Browse_Tab**: The App Store tab showing discoverable and installable apps
- **Sidebar**: The left navigation bar displaying page entries for enabled apps
- **InstalledApp_Metadata**: The `installed.json` metadata file for each app
- **defaultEnabled**: A field in the builtin app definition controlling the initial enabled state on first registration
- **Builtin_App_Definition**: The dictionary entry in `_BUILTIN_APPS` that declares a builtin app's metadata and configuration
- **App_Developer**: An internal developer who authors a new builtin app feature for KiroCrew

## Requirements

### Requirement 1: Builtin App Default Enabled Configuration

**User Story:** As a platform developer, I want to specify a default enabled state for new builtin apps, so that I can control which features are visible to users out of the box.

#### Acceptance Criteria

1. THE Builtin_App_Definition SHALL support a `defaultEnabled` field of boolean type
2. WHEN the `defaultEnabled` field is not specified in a Builtin_App_Definition, THE Gateway SHALL treat its value as `true`
3. WHEN a new builtin app is registered for the first time, THE Gateway SHALL use the `defaultEnabled` value from the app definition as the initial `enabled` value in InstalledApp_Metadata
4. WHEN an existing builtin app is re-registered during Gateway restart, THE Gateway SHALL preserve the user's previously set `enabled` state regardless of the `defaultEnabled` value

### Requirement 2: Backward Compatibility for Existing Builtin Apps

**User Story:** As an existing user, I want the current 5 builtin apps to behave exactly as before, so that my workflow is not disrupted.

#### Acceptance Criteria

1. THE Gateway SHALL ensure the existing 5 builtin apps (agent-worlds, channels, taskkeeper, secretary, board) have an effective `defaultEnabled` value of `true`
2. WHEN the Gateway starts after an upgrade, THE Gateway SHALL not change the `enabled` state of any existing builtin app
3. THE existing builtin apps SHALL continue to have `lifecycle="locked"` and cannot be uninstalled

### Requirement 3: Browse Tab Display of Disabled Builtin Apps

**User Story:** As a user, I want to discover disabled builtin apps in the App Store Browse tab, so that I can learn about available features.

#### Acceptance Criteria

1. WHEN a user opens the Browse_Tab, THE App_Store SHALL display all disabled builtin apps
2. WHEN displaying a disabled builtin app, THE App_Store SHALL show the app name, description, author, and tags
3. WHEN displaying a disabled builtin app, THE App_Store SHALL provide an "Enable" action button
4. THE App_Store SHALL display disabled builtin apps alongside registry apps in the Browse_Tab

### Requirement 4: Behavior After Enabling a Builtin App

**User Story:** As a user, I want an enabled builtin app to immediately appear in the installed list and sidebar, so that I can start using it.

#### Acceptance Criteria

1. WHEN a user enables a builtin app from the Browse_Tab, THE Gateway SHALL update the app's `enabled` state to `true`
2. WHEN a builtin app is enabled, THE Sidebar SHALL display the page entries defined in that app's manifest
3. WHEN a builtin app is enabled, THE Installed_Tab SHALL show the app in the installed list
4. WHEN a builtin app is enabled, THE Browse_Tab SHALL remove it from the discoverable apps display

### Requirement 5: Disabling an Enabled Builtin App

**User Story:** As a user, I want to disable builtin apps I don't need, so that I can keep my interface clean.

#### Acceptance Criteria

1. WHEN a user disables a builtin app from the Installed_Tab, THE Gateway SHALL update the app's `enabled` state to `false`
2. WHEN a builtin app is disabled, THE Sidebar SHALL remove that app's page entries
3. WHEN a builtin app is disabled, THE Browse_Tab SHALL display the app again for discovery
4. THE App_Store SHALL not allow users to uninstall any builtin app regardless of its enabled state

### Requirement 6: State Persistence

**User Story:** As a user, I want my enable/disable choices for builtin apps to persist across Gateway restarts, so that I don't need to reconfigure.

#### Acceptance Criteria

1. WHEN a user enables or disables a builtin app, THE Gateway SHALL immediately write the state change to InstalledApp_Metadata
2. WHEN the Gateway restarts, THE Gateway SHALL read and restore the user's previous enabled/disabled state from InstalledApp_Metadata
3. FOR ALL builtin apps, the `register_builtin_apps()` function SHALL only use the `defaultEnabled` value on first registration and preserve existing state on subsequent restarts

### Requirement 7: API Support

**User Story:** As a frontend developer, I want the API to provide sufficient information to distinguish discoverable builtin apps from enabled ones, so that I can render the UI correctly.

#### Acceptance Criteria

1. WHEN the frontend requests the app list, THE Gateway API SHALL include `origin`, `enabled`, and `lifecycle` fields in each app's response data
2. WHEN the frontend needs to filter Browse_Tab content, THE Gateway API SHALL support filtering the app list by `origin` and `enabled` state
3. THE Gateway API SHALL return complete manifest information (including `description`, `tags`, `ui.pages`) for disabled builtin apps

### Requirement 8: Standard Developer Configuration for Builtin Apps

**User Story:** As an App_Developer, I want a standard and documented way to configure my builtin app's metadata and default behavior, so that I can add new builtin apps without ambiguity until the package-separated App Store is security approved.

#### Acceptance Criteria

1. THE Builtin_App_Definition SHALL serve as the single source of truth for declaring a new builtin app's configuration
2. THE Builtin_App_Definition SHALL support the following fields: `name`, `version`, `displayName`, `description`, `author`, `tags`, `permissions`, `ui`, and `defaultEnabled`
3. WHEN an App_Developer adds a new entry to the `_BUILTIN_APPS` list, THE Gateway SHALL automatically register it on next startup without additional manual steps
4. IF an App_Developer provides an invalid or incomplete Builtin_App_Definition, THEN THE Gateway SHALL log a warning and skip registration of that app without affecting other apps
5. THE Builtin_App_Definition format SHALL be documented with inline code comments describing each field's purpose and valid values
