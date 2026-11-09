import { test, expect, type Page } from '@playwright/test'

/**
 * Session summary panel.
 *
 * The summary endpoint is stubbed with `page.route` rather than generated. Two
 * reason: generating a real summary costs a model call, which the
 * credential-less CI gateway cannot make and which would make the spec's
 * assertions depend on model output. Stubbing keeps the spec about the panel —
 * tab registration, the states a reader will actually hit, and the disclosure
 * behaviour — which is the part this change owns.
 *
 * Not tagged @needs-agent: no agent turn is driven, so these run in the default
 * gating pass.
 */

const SUMMARY_ROUTE = '**/api/chat/slots/*/summary'

const populated = {
  enabled: true,
  stale: false,
  generated_at: Date.now() / 1000 - 300,
  user_turns: 42,
  last_activity: new Date().toISOString(),
  constraints: ['Feature work ships behind a flag that defaults to off'],
  intents: [
    {
      title: 'Session summary panel',
      initial_intent: 'Show me what this session was about',
      progress: ['Backend landed', 'Panel built'],
      next_steps: [
        { what: 'Compare the panel to the mockup', why: 'Never rendered', expect: 'They match' },
      ],
      ranges: [[1, 14], [30, 42]],
      status: 'active',
      verified: null,
      state: 'in-progress',
      last_touched_turn: 42,
      origin_turn: null,
    },
    {
      title: 'Per-app dev server isolation',
      initial_intent: 'Two apps should not fight over a port',
      progress: ['Allocator leases a port per app'],
      next_steps: [
        { what: 'Run two real apps', why: 'Only unit-tested', expect: 'Neither steals the port' },
      ],
      ranges: [[15, 29]],
      status: 'completed',
      verified: false,
      state: 'needs-you',
      last_touched_turn: 29,
      origin_turn: 12,
    },
  ],
}

async function stubSummary(page: Page, body: unknown, status = 200) {
  await page.route(SUMMARY_ROUTE, route =>
    route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify(body),
    }),
  )
}

/** Open the Summary tab through the panel's "+" menu, as a user would. */
async function openAddMenu(page: Page) {
  await page.goto('/chat', { waitUntil: 'domcontentloaded' })
  // The side panel — and with it the `+` strip carrying "Open side panel tab" —
  // is render-gated on `activityOpen` (see sidePanelMount.ts), so on a fresh
  // profile that button does not exist at all and clicking it times out.
  //
  // Open the panel through the seam ChatPage documents for exactly this: the
  // top-bar Activity button dispatches a `toggle-activity-panel` window event,
  // and ChatPage listens for it. Firing the event is stabler than driving a
  // header button by accessible name, and it does not depend on the panel's
  // persisted per-slot flag (whose key includes a slot id a spec cannot know
  // before the page has loaded). Opening with no tabs shows the launcher grid,
  // which is what carries the strip.
  const plus = page.getByRole('button', { name: 'Open side panel tab' }).first()
  await expect(async () => {
    // The event TOGGLES, so firing it at an already-open panel closes it and the
    // strip unmounts. ChatPage opens the Activity panel on its own when the
    // slot's project dir is a git repo, off an async query that can resolve at
    // any point after load — so dispatch only when the strip is genuinely
    // absent, and retry the open-and-click together: an auto-open landing
    // between a separate check and click leaves nothing to click.
    if (!(await plus.isVisible())) {
      await page.evaluate(() => window.dispatchEvent(new Event('toggle-activity-panel')))
    }
    await plus.click({ timeout: 2000 })
    // 12s of retries sits inside the 30s per-test budget alongside the goto
    // above; the auto-open this races resolves in well under a second, so the
    // ceiling is headroom rather than an expected wait.
  }).toPass({ timeout: 12000 })
}

async function openSummaryTab(page: Page) {
  await openAddMenu(page)
  await page.getByRole('menuitem', { name: /Summary/ }).click()
}

test.describe('Session summary panel', () => {
  test('opens from the + menu and renders the summary', async ({ page }) => {
    await stubSummary(page, populated)
    await openSummaryTab(page)

    // The tab is registered and its body mounted, not just the menu entry.
    await expect(page.getByText('Session summary panel')).toBeVisible({ timeout: 10000 })
    await expect(page.getByText('Where it stands')).toBeVisible()
    // Interpolation resolves against the catalog rather than printing a raw key
    // or dropping the values.
    await expect(page.getByText(/turns 1–14, 30–42/)).toBeVisible()
  })

  test('hoists what needs the reader and counts it with the right plural', async ({ page }) => {
    await stubSummary(page, populated)
    await openSummaryTab(page)

    await expect(page.getByText('Needs you').first()).toBeVisible({ timeout: 10000 })
    // Two open steps across the two intents -> the plural form.
    await expect(page.getByText('2 open items')).toBeVisible()
    // Collapsed by default, each hoisted item is one headline. Expanding it
    // names the intent it came from, so lifting an item out of its card does
    // not sever the context that makes it decidable.
    await expect(page.getByText('Part of Per-app dev server isolation')).toBeHidden()
    await page.getByText('Run two real apps').first().click()
    await expect(page.getByText('Part of Per-app dev server isolation')).toBeVisible()
  })

  test('uses the singular form for a single open item', async ({ page }) => {
    await stubSummary(page, {
      ...populated,
      intents: [{ ...populated.intents[1] }],
    })
    await openSummaryTab(page)

    await expect(page.getByText('1 open item')).toBeVisible({ timeout: 10000 })
  })

  test('collapses an intent card and remembers it across a reload', async ({ page }) => {
    await stubSummary(page, populated)
    await openSummaryTab(page)

    // The most recent card starts open, so its body is present.
    await expect(page.getByText('You asked for')).toBeVisible({ timeout: 10000 })
    await page.getByRole('button', { expanded: true }).first().click()
    await expect(page.getByText('You asked for')).toBeHidden()

    // Disclosure is persisted per slot: a reload must not silently reopen it.
    // The tab STRIP persists on a trailing 300ms debounce with no unload flush
    // (usePanelTabs), so reloading immediately can drop the Summary tab and the
    // panel would return without it — a strip race, not a disclosure bug. Wait
    // for the write this assertion depends on rather than sleeping. The slot id
    // is not knowable from here, so match any slot's bucket.
    await page.waitForFunction(
      () => {
        for (let i = 0; i < localStorage.length; i++) {
          const k = localStorage.key(i)
          if (k?.startsWith('mc-panel-tabs:') && (localStorage.getItem(k) ?? '').includes('summary')) {
            return true
          }
        }
        return false
      },
      undefined,
      { timeout: 10000 },
    )
    await page.reload({ waitUntil: 'domcontentloaded' })
    await expect(page.getByText('Session summary panel')).toBeVisible({ timeout: 10000 })
    await expect(page.getByText('You asked for')).toBeHidden()
  })

  test('pins the project notes outside the scrolling list', async ({ page }) => {
    await stubSummary(page, populated)
    await openSummaryTab(page)

    const notes = page.getByText('How this project works')
    await expect(notes).toBeVisible({ timeout: 10000 })
    // Collapsed by default — the header advertises them, the reader opts in.
    await expect(page.getByText('Feature work ships behind a flag that defaults to off')).toBeHidden()
    await notes.click()
    await expect(page.getByText('Feature work ships behind a flag that defaults to off')).toBeVisible()

    // Pinned means it survives scrolling the intent list — the durable facts are
    // the thing a reader should not have to hunt for.
    await page.mouse.wheel(0, 600)
    await expect(notes).toBeVisible()
  })

  test('does not offer Summary while the feature is off', async ({ page }) => {
    await stubSummary(page, { ...populated, enabled: false, intents: [], constraints: [] })
    await openAddMenu(page)

    // Assert a sibling row FIRST. "Summary is not visible" passes trivially if
    // the menu never opened, which would make this spec inert.
    await expect(page.getByRole('menuitem', { name: /Workflows/ })).toBeVisible({ timeout: 10000 })
    await expect(page.getByRole('menuitem', { name: /Summary/ })).toHaveCount(0)

    // The off-state panel copy itself is asserted in sessionSummaryTab.test.tsx.
    // It is deliberately NOT re-asserted here: with the row gated away there is
    // no route to the panel through the menu, so a spec that navigated there
    // would be testing a path this gate makes unreachable.
  })

  test('says no summary yet when nothing has been generated', async ({ page }) => {
    await stubSummary(page, {
      ...populated,
      intents: [],
      constraints: [],
      generated_at: null,
    })
    await openSummaryTab(page)

    await expect(page.getByText('No summary yet')).toBeVisible({ timeout: 10000 })
  })

  test('reports a failed load with a Retry that recovers the panel', async ({ page }) => {
    // SidePanel gates the + menu row on the SAME query key as the tab
    // (`['session-summary', slot]`), so it — not the tab — issues the first
    // request. Counting calls would hand the tab the success and the error
    // state would never render. Fail every request until the spec flips the
    // flag, which is what "Retry refetches" actually needs to be proven.
    let failing = true
    await page.route(SUMMARY_ROUTE, route => {
      if (failing) {
        return route.fulfill({ status: 500, contentType: 'application/json', body: '{}' })
      }
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(populated),
      })
    })
    await openSummaryTab(page)

    await expect(page.getByText('Could not load the summary')).toBeVisible({ timeout: 10000 })
    failing = false
    await page.getByRole('button', { name: /Try again/i }).click()
    await expect(page.getByText('Session summary panel')).toBeVisible({ timeout: 10000 })
  })

  test('serves a stale summary and states it in ONE freshness line', async ({ page }) => {
    await stubSummary(page, { ...populated, stale: true })
    await openSummaryTab(page)

    // Withholding a stale summary reads as broken; marking it reads as true.
    await expect(page.getByText('Session summary panel')).toBeVisible({ timeout: 10000 })
    // Staleness lives in the FOOTER, folded into the same sentence as the
    // timestamp: two markers at opposite corners make the reader reconcile
    // them, and a marker in the header wraps and pushes the count chip out of
    // position.
    await expect(page.getByText(/Updated .* — behind the conversation/)).toBeVisible()
  })
})
