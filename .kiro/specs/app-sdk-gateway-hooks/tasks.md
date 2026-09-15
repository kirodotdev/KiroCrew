# Implementation Plan: App SDK Gateway Hooks

## Overview

Implement gateway-side hooks enabling apps to register routes, manage crons, and participate in lifecycle events without modifying KiroCrew core files. Implementation is in Python (aiohttp) within the KiroCrew package.

## Tasks

- [x] 1. Extend manifest schema with backend.hooks and extended CronEntry
  - [x] 1.1 Add `HooksConfig` dataclass to `manifest.py` with `routes`, `on_startup`, `on_shutdown` fields
    - Add validation for Python module path format (`module.path:callable_name`)
    - Extend `BackendConfig` to include `hooks: HooksConfig`
    - _Requirements: 6.1, 6.2, 6.3_
  - [x] 1.2 Extend `CronEntry` dataclass with `agent_sequence`, `env`, `persistent_session`, `silent` fields
    - Update `to_dict()` and `from_dict()` for round-trip support
    - _Requirements: 6.4_
  - [x] 1.3 Write property test for manifest round-trip with hooks and extended crons
    - **Property 13: Manifest round-trip with hooks and extended crons**
    - **Validates: Requirements 6.4, 6.5**
  - [x] 1.4 Write property test for hook path validation
    - **Property 12: Hook path validation**
    - **Validates: Requirements 6.2, 6.3**

- [x] 2. Implement App Context, Cron SDK, EventBus, and AppStorage
  - [x] 2.1 Create `src/kiro_crew/apps/context.py` with `AppContext` dataclass and `AppHealthStatus`
    - Include `name`, `data_dir`, `config`, `logger`, `cron`, `events`, `storage` fields
    - Implement `AppContextFactory` that builds context based on app permissions
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_
  - [x] 2.2 Create `src/kiro_crew/apps/cron_sdk.py` with `CronSDK` class
    - Implement `add_job`, `remove_job`, `update_job`, `list_jobs`, `remove_all`
    - Enforce ownership via `created_by = "app:{app_name}"` prefix
    - Support advanced fields: `agent_sequence`, `env`, `persistent_session`, `silent`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_
  - [x] 2.3 Create `src/kiro_crew/apps/event_bus.py` with `EventBus` class
    - Implement `publish(event_type, data)` with permission check against declared events
    - Implement `publish_to_app(event_type, data)` for app-scoped broadcasts
    - Wrap existing `DashboardState.broadcast()` — no new WS infrastructure
    - Raise `PermissionError` if event_type not in allowed list
    - _Requirements: 5.2_
  - [x] 2.4 Create `src/kiro_crew/apps/app_storage.py` with `AppStorage` class
    - Implement `get(key)`, `set(key, value)`, `delete(key)`, `list_keys()`
    - Store as `data_dir/kv/{key}.json` with atomic writes
    - Validate keys to prevent path traversal (reject `..`, `/`, `\`)
    - _Requirements: 5.1_
  - [x] 2.5 Write property tests for Cron SDK ownership enforcement
    - **Property 4: Cron ownership enforcement on mutations**
    - **Property 5: Cron list filtering by owner**
    - **Validates: Requirements 2.2, 2.3, 2.4, 2.5**
  - [x] 2.6 Write property tests for Cron SDK job creation and cleanup
    - **Property 3: Cron job creation preserves ownership and fields**
    - **Property 6: Cron remove_all completeness**
    - **Validates: Requirements 2.1, 2.6, 2.7**
  - [x] 2.7 Write property tests for App Context permission mapping
    - **Property 10: App context permission mapping**
    - **Property 11: App context base fields always present**
    - **Validates: Requirements 5.1, 5.4, 5.5**
  - [x] 2.8 Write property tests for EventBus and AppStorage
    - **Property 17: EventBus permission enforcement**
    - **Property 18: AppStorage key isolation**
    - **Property 19: AppStorage key validation rejects traversal**
    - **Validates: Requirements 5.1, 5.2**

- [x] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Implement Module Loader and Route Registry
  - [x] 4.1 Create `src/kiro_crew/apps/module_loader.py` with isolated module loading
    - Implement `load_app_module(app_name, app_dir, module_path)` using `spec_from_file_location`
    - Register modules as `_kirocrew_app_{app_name}.{dotted_path}` in sys.modules
    - Implement `unload_app_modules(app_name)` to clean sys.modules on disable
    - Add path containment check (module cannot escape app directory)
    - _Requirements: 1.1, 1.4 (error resilience)_
  - [x] 4.2 Create `src/kiro_crew/apps/route_registry.py` with `RouteRegistry` class
    - Implement middleware-based soft routing (internal dict, not aiohttp UrlDispatcher)
    - Implement `register_app_routes(app_name, app_dir, hook_path, ctx)` — loads module via module_loader, adds to internal table
    - Implement `deregister_app_routes(app_name)` — removes from internal table + calls `unload_app_modules`
    - Implement `dispatch(request)` — catch-all handler that routes to registered handlers
    - Handler signature: `async def handler(request: web.Request, ctx: AppContext) -> web.Response`
    - Set `health_status="degraded"` on route load failure
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_
  - [x] 4.3 Write property tests for Route Registry
    - **Property 1: Route prefix enforcement**
    - **Property 2: Route deregistration completeness**
    - **Validates: Requirements 1.2, 1.3, 1.5**
  - [x] 4.4 Write property tests for Module Isolation
    - **Property 15: Module isolation prevents namespace collisions**
    - **Property 16: Module unload cleans sys.modules**
    - **Validates: Requirements 1.3, 1.4**

- [x] 5. Implement Lifecycle Hook Dispatcher
  - [x] 5.1 Create `src/kiro_crew/apps/lifecycle.py` with `LifecycleDispatcher` class
    - Implement `dispatch_startup(enabled_apps)` — invokes on_startup hooks sorted by app name
    - Implement `dispatch_shutdown(enabled_apps)` — invokes on_shutdown hooks in reverse order
    - Implement `dispatch_enable(app_info)` and `dispatch_disable(app_info)` for per-app hooks
    - Handle exceptions gracefully (log and continue)
    - Pass `AppContext` to all hooks
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 7.4_
  - [x] 5.2 Write property tests for lifecycle hook ordering
    - **Property 9: Lifecycle hook invocation order is deterministic**
    - **Property 14: Shell-before-Python hook ordering**
    - **Validates: Requirements 4.5, 7.4**

- [x] 6. Implement Builtin Auto-Discovery
  - [x] 6.1 Create `src/kiro_crew/apps/discovery.py` with `discover_builtin_apps()` function
    - Scan `builtins/` directory for subdirectories with valid `app.json`
    - Convert manifests to the dict format expected by `register_builtin_apps()`
    - Skip invalid/missing manifests with warning log
    - _Requirements: 3.1, 3.2, 3.4_
  - [x] 6.2 Replace `_BUILTIN_APPS` list in `manager.py` with call to `discover_builtin_apps()`
    - Ensure backward compatibility — same apps discovered, same metadata format
    - _Requirements: 3.3_
  - [x] 6.3 Write property tests for builtin discovery
    - **Property 7: Builtin discovery finds all valid manifests**
    - **Property 8: Discovered builtins have correct classification**
    - **Validates: Requirements 3.1, 3.2**

- [-] 7. Integrate hooks into gateway lifecycle
  - [x] 7.1 Wire Route Registry into `routes.py` app enable/disable flow
    - After `register_app()` succeeds, call `route_registry.register_app_routes()` if hooks declared
    - Before `deregister_app()`, call `route_registry.deregister_app_routes()`
    - Remove hardcoded `if name == "mimir": register_mimir_routes(request.app)` check
    - _Requirements: 1.1, 1.3, 7.1, 7.2_
  - [x] 7.2 Wire Lifecycle Dispatcher into `server.py` startup/shutdown
    - After gateway initialization, call `dispatcher.dispatch_startup()`
    - Before gateway shutdown, call `dispatcher.dispatch_shutdown()`
    - Ensure shell script hooks (`setup.onEnable`) run before Python hooks
    - _Requirements: 4.1, 4.2, 7.3, 7.4_
  - [x] 7.3 Wire CronSDK cleanup into app disable/uninstall flow
    - On disable: call `cron_sdk.remove_all()` for the app
    - On uninstall: call `cron_sdk.remove_all()` for the app
    - _Requirements: 2.6_

- [ ] 8. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.
  - Verify existing apps (Mochi, agent-worlds) are unaffected.

## Notes

- All tasks including property tests are required
- Each task references specific requirements for traceability
- Property tests use `hypothesis` library with minimum 100 iterations
- Mimir migration is out of scope for this CR — Mimir owner will migrate using the hooks system after merge
