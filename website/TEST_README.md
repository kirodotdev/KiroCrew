# KiroCrew UI Integration Tests

This directory contains integration and E2E tests for the KiroCrew dashboard frontend.

## Test Structure

### MSW Integration Tests (`src/test/*.integration.test.tsx`)
These tests use Mock Service Worker (MSW) to intercept API calls and test full tab components with realistic data flows:

- **MemoryTab.integration.test.tsx** - Tests memory settings, preferences, projects, history, and consolidation
- **CronTab.integration.test.tsx** - Tests cron job creation, editing, toggling, and deletion
- **SkillsTab.integration.test.tsx** - Tests skill listing, expansion, editing, creation, and deletion
- **McpTab.integration.test.tsx** - Tests MCP server management, tool toggling, and server enable/disable
- **HooksPage.integration.test.tsx** - Tests hook creation, editing, deletion, toggling, and testing

### Playwright E2E Tests (`e2e/*.spec.ts`)
End-to-end tests that run against the actual running application, organized by page/feature:

- **navigation.spec.ts** - Homepage, navigation, theme toggle, responsive design
- **overview.spec.ts** - Overview page tab switching and data loading
- **cron.spec.ts** - Cron job creation, editing, deletion, validation
- **chat.spec.ts** - Chat interface, message sending, streaming responses
- **hooks.spec.ts** - Hooks page, hook creation, editing, testing
- **system.spec.ts** - System information display

## Running Tests

### Integration Tests (MSW)
```bash
# Run all integration tests
npm run test:integration

# Watch mode
npm run test:watch
```

### E2E Tests (Playwright)
```bash
# Run E2E tests headless (parallel, fast - for CI/CD)
npm run test:playwright:headless

# Run with visible browser (sequential, one test at a time - for watching)
npm run test:playwright
```

### All Tests
```bash
# Run all tests (unit + integration)
npm test

# Full check (typecheck + lint + test)
npm run check
```

## MSW Setup

MSW (Mock Service Worker) intercepts network requests at the network level, providing more realistic testing than mocking individual API functions.

### Mock Server (`src/test/mocks/server.ts`)
Defines handlers for all API endpoints used by the frontend:
- Memory endpoints (preferences, projects, history, settings, consolidation)
- Cron endpoints (list, create, update, delete, toggle)
- Skills endpoints (list, get, create, update, delete)
- MCP endpoints (list, toggle, probe, sync)
- Hooks endpoints (list, create, update, delete, toggle, test)
- Sessions and lessons endpoints

The mock server is automatically set up in `src/test/setup.ts` for all tests.

## Playwright Configuration

Playwright is configured in `playwright.config.ts`:
- Uses Chromium browser
- Runs against `http://localhost:3000`
- Auto-starts dev server before tests
- Uses dev environment (`KIROCREW_HOME=.kirocrew-dev` + `KIROCREW_PORT=6777`)

## Test Coverage

The integration tests cover the main workflows described in the Taskei tickets:

**MSW Integration Tests**
- ✅ MemoryTab: load settings, save, consolidate flow
- ✅ CronTab: load jobs, add job, toggle/delete
- ✅ SkillsTab: load skills, expand/collapse, create/edit/delete
- ✅ McpTab: list servers, toggle tools, enable/disable servers
- ✅ HooksPage: create hooks, edit, delete, toggle, test execution

**Playwright E2E Tests**
- ✅ Navigate to Overview → verify stats load
- ✅ Send chat message → see streaming response
- ✅ Create cron job → verify in jobs table
- ✅ Switch between Overview tabs → verify each tab loads data
- ✅ Navigate to Hooks page → create and manage hooks
- ⚠️  WebSocket reconnection (requires backend setup)

## Troubleshooting

### Import Issues
If you see "Failed to resolve import" errors, the issue may be with module resolution. The tests use relative imports like `../../store/chatSlice` which should work correctly.

### MSW Not Intercepting
Make sure `src/test/setup.ts` is configured in `vite.config.ts` as `setupFiles`. The server should start before all tests and clean up after.

### Playwright Server Not Starting
Check that:
- Port 3000 and 6777 are available
- Backend is configured with dev environment variables
- `KIROCREW_HOME=.kirocrew-dev` directory exists

### E2E Test Debugging
Use `npm run test:e2e:debug` to run with visible browser and step through each action. This will help you see exactly what's happening when a test fails.

## Understanding Test Types

See [TEST_TYPES_EXPLAINED.md](./TEST_TYPES_EXPLAINED.md) for a comprehensive comparison of:
- MSW Integration Tests vs Playwright E2E Tests
- When to use each type
- Differences between test commands
- File organization and naming conventions

## Future Enhancements

- Add contract tests using Pact or similar
- Add accessibility tests with axe-core
- Add visual regression tests
- Add WebSocket reconnection E2E test (requires mock WebSocket server)
- Add performance benchmarks

## Resources

- [MSW Documentation](https://mswjs.io/)
- [Playwright Documentation](https://playwright.dev/)
- [Vitest Documentation](https://vitest.dev/)
- [Testing Library Documentation](https://testing-library.com/)
