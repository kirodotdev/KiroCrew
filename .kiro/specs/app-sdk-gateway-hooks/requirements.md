# Requirements Document

## Introduction

This feature extends the KiroCrew App SDK and gateway to provide backend integration hooks, enabling apps (both built-in and external) to register API routes, manage cron jobs, and hook into gateway lifecycle without modifying KiroCrew core files. The goal is to eliminate the pattern where every new app must hardcode itself into `routes.py`, `server.py`, and `manager.py`.

## Glossary

- **Gateway**: The KiroCrew aiohttp server process that hosts the dashboard, API routes, and app lifecycle management.
- **App_Manifest**: The `app.json` file declaring an app's identity, resources, permissions, and capabilities.
- **Route_Registry**: A gateway-side component that discovers and mounts app-provided HTTP route handlers at startup and on app enable/disable.
- **Cron_SDK**: A programmatic API exposed to apps for creating, updating, and deleting scheduled jobs without directly accessing DashboardState.
- **Lifecycle_Hook**: A Python entry point declared in the app manifest that the gateway invokes at specific lifecycle stages (startup, shutdown, enable, disable).
- **App_Context**: A scoped object passed to lifecycle hooks and route handlers providing access to permitted gateway services (cron, storage, events) without exposing raw DashboardState.
- **Builtin_App**: An app bundled with KiroCrew source code, located in `src/kiro_crew/apps/builtins/`.
- **DashboardState**: The central gateway state object holding cron service, session state, and configuration.

## Requirements

### Requirement 1: Declarative Route Registration

**User Story:** As an app developer, I want to declare HTTP route handlers in my app manifest and implementation, so that KiroCrew automatically mounts them without requiring changes to core gateway files.

#### Acceptance Criteria

1. WHEN an app manifest declares a `backend.hooks.routes` entry point, THE Route_Registry SHALL discover and load the route module at app enable time.
2. WHEN the Route_Registry loads a route module, THE Gateway SHALL mount all routes returned by the module's registration function under the app's scoped path prefix (`/api/apps/{app_name}/`).
3. WHEN an app is disabled, THE Route_Registry SHALL remove all routes previously registered by that app.
4. IF a route module fails to load or returns invalid routes, THEN THE Gateway SHALL log the error and continue operating without the failed app's routes.
5. WHEN multiple apps register routes, THE Route_Registry SHALL ensure no path collisions occur by enforcing the app-scoped prefix.

### Requirement 2: Programmatic Cron Management

**User Story:** As an app developer, I want to programmatically create, update, and delete cron jobs through a scoped SDK API, so that I do not need to directly access DashboardState internals.

#### Acceptance Criteria

1. WHEN an app calls `App_Context.cron.add_job(...)`, THE Cron_SDK SHALL create a new cron job scoped to that app with the provided configuration.
2. WHEN an app calls `App_Context.cron.remove_job(job_id)`, THE Cron_SDK SHALL remove the specified job only if it belongs to the calling app.
3. WHEN an app calls `App_Context.cron.list_jobs()`, THE Cron_SDK SHALL return only jobs owned by the calling app.
4. WHEN an app calls `App_Context.cron.update_job(job_id, ...)`, THE Cron_SDK SHALL update the specified job only if it belongs to the calling app.
5. IF an app attempts to modify a cron job owned by a different app, THEN THE Cron_SDK SHALL reject the operation and return a permission error.
6. WHEN an app is disabled or uninstalled, THE Cron_SDK SHALL remove all cron jobs owned by that app.
7. WHEN a cron job is created via the Cron_SDK, THE Cron_SDK SHALL support advanced fields including agent_sequence, env variables, persistent_session, and silent mode.

### Requirement 3: Builtin App Auto-Discovery

**User Story:** As a platform maintainer, I want builtin apps to be automatically discovered from their directory structure, so that adding a new builtin app does not require modifying the `_BUILTIN_APPS` list in `manager.py`.

#### Acceptance Criteria

1. WHEN the Gateway starts, THE App_Manager SHALL scan the `builtins/` directory for subdirectories containing a valid `app.json` manifest.
2. WHEN a valid manifest is found in a builtin directory, THE App_Manager SHALL register it as a builtin app with origin "builtin" and lifecycle "locked".
3. WHEN the `_BUILTIN_APPS` list is removed, THE App_Manager SHALL produce identical behavior to the previous hardcoded list by discovering the same apps from the filesystem.
4. IF a builtin directory contains an invalid or missing manifest, THEN THE App_Manager SHALL log a warning and skip that directory.

### Requirement 4: Gateway Lifecycle Hooks

**User Story:** As an app developer, I want to hook into gateway startup and shutdown events, so that my app can initialize resources and clean up without modifying `server.py`.

#### Acceptance Criteria

1. WHEN an app manifest declares `backend.hooks.on_startup`, THE Gateway SHALL invoke the specified entry point after the gateway is fully initialized.
2. WHEN an app manifest declares `backend.hooks.on_shutdown`, THE Gateway SHALL invoke the specified entry point before the gateway shuts down.
3. WHEN a lifecycle hook is invoked, THE Gateway SHALL pass an App_Context object providing access to permitted services.
4. IF a lifecycle hook raises an exception, THEN THE Gateway SHALL log the error and continue startup or shutdown without aborting.
5. WHEN multiple apps declare startup hooks, THE Gateway SHALL invoke them in a deterministic order based on app name.

### Requirement 5: App Context Scoping

**User Story:** As a platform architect, I want app route handlers and lifecycle hooks to receive a scoped context object, so that apps cannot access arbitrary gateway internals.

#### Acceptance Criteria

1. THE App_Context SHALL expose only the services declared in the app's permissions (cron, storage, events).
2. WHEN an app has `permissions.cron: true`, THE App_Context SHALL include the Cron_SDK interface.
3. WHEN an app does not have a required permission, THE App_Context SHALL omit the corresponding service interface.
4. THE App_Context SHALL provide the app's name, data directory path, and configuration.
5. THE App_Context SHALL provide a logger scoped to the app's namespace.
6. WHEN an app has `permissions.events` with one or more event types, THE App_Context SHALL include an EventBus interface that allows publishing only those declared event types.
7. WHEN an app publishes an event not in its declared events list, THE EventBus SHALL reject the publish and raise a permission error.
8. WHEN an app has `permissions.storage: true`, THE App_Context SHALL include an AppStorage interface providing key-value persistence scoped to the app's data directory.
9. THE AppStorage SHALL validate keys to prevent path traversal and reject keys containing `..`, `/`, or `\`.

### Requirement 6: Manifest Schema Extension

**User Story:** As an app developer, I want to declare backend hooks in my app.json manifest, so that the gateway knows which entry points to invoke for routes and lifecycle events.

#### Acceptance Criteria

1. THE App_Manifest SHALL support a `backend.hooks` object with optional fields: `routes`, `on_startup`, `on_shutdown`.
2. WHEN `backend.hooks.routes` is specified, THE App_Manifest SHALL validate that the value is a valid Python module path string.
3. WHEN `backend.hooks.on_startup` or `backend.hooks.on_shutdown` is specified, THE App_Manifest SHALL validate that each value is a valid Python callable path string.
4. THE App_Manifest SHALL support an extended `crons` schema allowing `agent_sequence`, `env`, `persistent_session`, and `silent` fields per cron entry.
5. WHEN the manifest is serialized and deserialized, THE App_Manifest SHALL preserve all hook declarations through the round trip.

### Requirement 7: Backward Compatibility

**User Story:** As an existing app developer, I want my current apps to continue working without modification, so that this feature is purely additive.

#### Acceptance Criteria

1. WHEN an app manifest does not declare `backend.hooks`, THE Gateway SHALL operate identically to the current behavior.
2. WHEN the Mimir app is migrated to use the new hooks system, THE Gateway SHALL produce identical runtime behavior to the current hardcoded registration.
3. THE Gateway SHALL continue to support the existing `setup.onEnable` and `setup.onDisable` shell script hooks alongside the new Python lifecycle hooks.
4. WHEN both shell script hooks and Python lifecycle hooks are declared, THE Gateway SHALL execute shell scripts first, then Python hooks.
