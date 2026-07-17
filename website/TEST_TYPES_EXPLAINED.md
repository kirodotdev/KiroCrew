# Test Types Explained

## 1. MSW Integration Tests vs Playwright E2E Tests

### MSW Integration Tests (`npm run test:integration`)

**What they test:**
- Individual React components with their API integration
- Component behavior when data is fetched and displayed
- User interactions within a single component/page
- Form validation and error handling

**Technology:**
- **Vitest** - test runner (like Jest)
- **React Testing Library** - renders components in jsdom
- **MSW (Mock Service Worker)** - intercepts HTTP requests at the network layer
- **jsdom** - simulated browser environment in Node.js

**Requirements:**
- ❌ No backend required
- ❌ No real browser required
- ✅ Just Node.js

**Speed:** Fast (seconds) - runs ~50 tests in 5-10 seconds

**What's real vs mocked:**
```
✅ Real: Component code, Redux store, API client functions, fetch() calls
❌ Mocked: Network responses (MSW intercepts and returns fake data)
```

**Example test:**
```typescript
// MemoryTab.integration.test.tsx
test('saves preferences', async () => {
  render(<Provider><MemoryTab /></Provider>)
  
  // Real fetch happens, MSW intercepts and returns fake data
  await waitFor(() => {
    expect(screen.getByText('My Preferences')).toBeInTheDocument()
  })
  
  // Real user interaction
  await user.type(textarea, 'New preferences')
  await user.click(saveButton)
  
  // Real API call, MSW intercepts PUT request
  await waitFor(() => {
    expect(saveButton).toHaveTextContent('✓ Saved')
  })
})
```

**Use cases:**
- Does MemoryTab correctly fetch and display preferences?
- Does the Save button send the right API request?
- Does the UI update after saving?
- Does error handling work?

---

### Playwright E2E Tests (`npm run test:e2e`)

**What they test:**
- Complete user workflows across multiple pages
- Real backend API integration
- WebSocket connections
- Browser-specific behavior (CSS, JavaScript, rendering)
- Navigation between pages

**Technology:**
- **Playwright** - browser automation framework
- **Chromium** - real Chrome browser
- Runs against actual running application (frontend + backend)

**Requirements:**
- ✅ Backend must be running (`KIROCREW_HOME=.kirocrew-dev KIROCREW_PORT=6777`)
- ✅ Frontend dev server must be running (or built static files served)
- ✅ Database must be accessible
- ✅ Real browser launches

**Speed:** Slow (minutes) - runs ~50 tests in 5-15 minutes

**What's real vs mocked:**
```
✅ Real: Everything - browser, frontend, backend, database, network
❌ Mocked: Nothing (unless you explicitly mock external APIs)
```

**Example test:**
```typescript
// cron.spec.ts
test('creates cron job', async ({ page }) => {
  // Real browser navigates to real URL
  await page.goto('http://localhost:3000')
  await page.getByText('Overview').click()
  
  // Real click in real browser
  await page.getByRole('tab', { name: /cron/i }).click()
  
  // Real API call to real backend
  await page.getByPlaceholder(/job name/i).fill('Test Job')
  await page.getByRole('button', { name: /add/i }).click()
  
  // Real database write, real backend response
  await expect(page.getByText('Test Job')).toBeVisible()
})
```

**Use cases:**
- Can a user actually create a cron job from start to finish?
- Does navigation between pages work?
- Do WebSocket messages appear correctly?
- Does the entire system work together?

---

## Side-by-Side Comparison

| Aspect | MSW Integration | Playwright E2E |
|--------|----------------|----------------|
| **Test scope** | Single component/page | Entire application |
| **Backend** | Not needed (mocked) | Must be running |
| **Browser** | jsdom (simulated) | Chromium (real) |
| **Speed** | Fast (seconds) | Slow (minutes) |
| **Reliability** | Very stable | Can be flaky |
| **Debugging** | Easy (console.log) | Harder (browser DevTools) |
| **Cost** | Cheap to run | Expensive to run |
| **What it catches** | Component bugs | Integration bugs |
| **CI/CD** | Run on every commit | Run before deploy |

---

## 2. Playwright Test Commands

### `npm run test:e2e` (Headless - Default)
**What it does:**
- Runs tests with **no visible browser window**
- Browser runs invisibly in the background
- You only see text output in the terminal

**When to use:**
- ✅ Running tests in CI/CD (GitHub Actions, Jenkins)
- ✅ Quick local test runs
- ✅ When you trust the tests work

**Output:**
```bash
$ npm run test:e2e

Running 25 tests using 1 worker

✓ [chromium] › overview.spec.ts:3:3 › navigates to Overview (1.2s)
✓ [chromium] › cron.spec.ts:3:3 › creates new cron job (2.1s)
✓ [chromium] › chat.spec.ts:3:3 › sends a chat message (3.5s)
...

25 passed (45.2s)
```

---

### `npm run test:e2e:debug` (Headed + Debug Mode)
**What it does:**
- Browser window **opens visibly** on your screen
- Playwright **pauses before each action**
- You can **step through** the test manually
- See browser DevTools, network requests, console logs

**When to use:**
- ❌ NOT for CI/CD (too slow, requires human interaction)
- ✅ When a test is failing and you don't know why
- ✅ When writing a new test
- ✅ When you need to inspect the DOM at a specific moment

**What you see:**
1. Chrome window opens
2. Test pauses at each step with green highlight
3. Playwright Inspector window shows:
   - Current line of code
   - "Resume" button to continue
   - "Step over" button for next action
   - Network requests
   - Console logs
   - Screenshots

**Example workflow:**
```bash
$ npm run test:e2e:debug

# Chrome opens
# Playwright Inspector opens
# Test pauses at: await page.getByText('Overview').click()
# You click "Step over"
# Test pauses at: await page.getByRole('tab', { name: /cron/i }).click()
# You click "Step over"
# ... etc
```

---

## Why Only Two Commands?

### Before (3 commands):
```json
"test:e2e": "playwright test",              // headless
"test:e2e:headed": "playwright test --headed", // visible browser
"test:e2e:ui": "playwright test --ui"          // interactive UI
```

**Problems:**
- **Confusing:** Which one do I use?
- **Overlapping:** `--headed` and `--ui` both show the browser
- **Rarely needed:** Most people only need headless (CI) or debug (local)

### After (2 commands):
```json
"test:e2e": "playwright test",                    // headless - for CI and quick runs
"test:e2e:debug": "playwright test --headed --debug" // debug mode - for troubleshooting
```

**Benefits:**
- **Clear purpose:** "Do I want to debug? Use debug command. Otherwise, default."
- **Covers all use cases:**
  - Fast runs → `test:e2e`
  - Debugging → `test:e2e:debug`
- **No confusion**

---

## The `--ui` Flag We Removed

**What `playwright test --ui` did:**
- Opened a web-based UI in your browser
- Showed a list of all tests with checkboxes
- Let you pick which tests to run
- Showed timeline and screenshots

**Why we removed it:**
- **Playwright Inspector (`--debug`) does the same thing** but better for debugging
- **UI is for exploration**, not debugging
- Most developers just want: "Run all tests" or "Debug this failing test"
- If you really want it: `npx playwright test --ui` (doesn't need a script)

---

## Quick Decision Tree

```
Are you writing/fixing code?
├─ Yes → Use MSW integration tests (`npm run test:integration`)
│         - Fast feedback loop
│         - No backend needed
│
└─ No, verifying the full system works?
   └─ Are all tests passing?
      ├─ Yes → Use headless E2E (`npm run test:e2e`)
      │         - Quick verification
      │         - CI-ready
      │
      └─ No, something is failing?
         └─ Use debug mode (`npm run test:e2e:debug`)
            - See what's happening
            - Step through the test
            - Inspect DOM/Network
```

---

## File Organization

### Integration Tests (MSW)
```
src/test/
├── MemoryTab.integration.test.tsx    - Tests MemoryTab component
├── CronTab.integration.test.tsx      - Tests CronTab component
├── SkillsTab.integration.test.tsx    - Tests SkillsTab component
├── McpTab.integration.test.tsx       - Tests McpTab component
├── HooksPage.integration.test.tsx    - Tests HooksPage component
└── mocks/
    └── server.ts                      - MSW mock server setup
```

**Naming:** `{Component}.integration.test.tsx`

### E2E Tests (Playwright)
```
e2e/
├── overview.spec.ts     - Tests Overview page (Memory/Cron/Skills/MCP tabs)
├── cron.spec.ts         - Tests Cron tab workflows
├── chat.spec.ts         - Tests Chat page
├── hooks.spec.ts        - Tests Hooks page
├── system.spec.ts       - Tests System page
└── navigation.spec.ts   - Tests navigation and theme
```

**Naming:** `{page}.spec.ts` - One file per page/feature

---

## Running Tests

### Development workflow:
```bash
# 1. Write component code
# 2. Write integration test
npm run test:integration

# 3. Test passes? Commit.
git add . && git commit -m "feat: add cron job creation"

# 4. Before pushing, verify E2E
npm run test:e2e

# 5. E2E fails? Debug it
npm run test:e2e:debug
```

### CI/CD workflow:
```yaml
# .github/workflows/test.yml
- run: npm run test:integration  # Fast unit/integration tests
- run: npm run test:e2e          # Full E2E tests (headless)
```

---

## Summary

**Integration tests (MSW):**
- Test one component at a time
- Mock network, real everything else
- Fast, reliable, easy to debug
- Run constantly during development

**E2E tests (Playwright):**
- Test entire user flows
- Nothing mocked, everything real
- Slow, can be flaky, harder to debug
- Run before deploying to production

**Two commands are enough:**
- `test:e2e` - Headless for speed
- `test:e2e:debug` - Visible + inspector for debugging
