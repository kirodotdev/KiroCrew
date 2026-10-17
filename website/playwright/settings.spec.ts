import { test, expect, type Page } from '@playwright/test'
import { MAX_RECENT_TINT_COUNT } from '../src/utils/recencyTint'

// The stepper's upper bound comes from the source of truth rather than a copy,
// so raising or lowering it there cannot leave this spec asserting a stale
// bound. Playwright transpiles specs with its own pipeline, so importing across
// the playwright/ and src/ boundary resolves even though src/ is a separate
// tsconfig project. recencyTint.ts is a dependency-free module of pure helpers,
// so this pulls in no component or DOM code.
const MAX_TINT = MAX_RECENT_TINT_COUNT

/**
 * Settings page (/settings) — route-level Playwright coverage.
 *
 * Covers:
 *  (a) Page load with tab strip rendering
 *  (b) Tab navigation + /settings/<tab> path round-trip (desktop first tab
 *      stays the bare /settings; legacy ?tab= links translate to the path)
 *  (c) Config mutation round-trip via the "Highlight recent sessions" stepper
 *      (dashboard.recent_tint_count — server-persisted, cosmetic, test-safe)
 *  (d) Redirect contracts: /overview → /settings/overview,
 *      /instances → /settings/instances
 *  (e) The two-pane section list stays in view while the settings scroll,
 *      and a list taller than the scroll area scrolls on its own
 */
test.describe('Settings Page', () => {
  test('loads and renders the tab strip with known tabs', async ({ page }) => {
    await page.goto('/settings', { waitUntil: 'domcontentloaded' })

    // The page title rendered by SidePanelLayout inside the nav sidebar
    await expect(page.getByText('Settings').first()).toBeVisible({ timeout: 10000 })

    // Verify a selection of real tab labels exist as clickable buttons
    await expect(page.getByRole('button', { name: 'Overview', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Display', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Chat', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Remote Crew', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'About', exact: true })).toBeVisible()
  })

  test('defaults to the overview tab at the bare /settings path', async ({ page }) => {
    await page.goto('/settings', { waitUntil: 'domcontentloaded' })
    // Overview is the first tab and renders when no segment is present.
    // The content area heading should show "Overview"
    await expect(page.getByRole('heading', { name: 'Overview' }).or(
      page.locator('div').filter({ hasText: /^Overview$/ }).first()
    )).toBeVisible({ timeout: 10000 })
    // Desktop first tab is the segment-less base path — no /overview segment,
    // and never a legacy ?tab= param.
    expect(page.url()).not.toContain('tab=')
    expect(new URL(page.url()).pathname).toBe('/settings')
  })

  test('navigating between tabs updates the /settings/<tab> path', async ({ page }) => {
    await page.goto('/settings', { waitUntil: 'domcontentloaded' })

    // Click Display tab button
    const displayTab = page.getByRole('button', { name: 'Display', exact: true })
    await expect(displayTab).toBeVisible({ timeout: 10000 })
    await displayTab.click()

    // URL should update to the path segment — navigation state lives in the
    // path now, never in ?tab=. Display is a rail, so its first item's segment
    // may follow the tab (as Chat's does), e.g. /settings/display/view.
    await expect(page).toHaveURL(/\/settings\/display(?:\/[a-z-]+)?(?:[?#]|$)/)

    // Click About tab button
    await page.getByRole('button', { name: 'About', exact: true }).click()
    await expect(page).toHaveURL(/\/settings\/about(?:[?#]|$)/)
  })

  test('direct navigation to /settings/chat renders the Chat panel', async ({ page }) => {
    await page.goto('/settings/chat', { waitUntil: 'domcontentloaded' })

    // The Chat panel should render its content — check for a known setting label
    // ChatPanel contains the "Timestamps" toggle and other settings
    await expect(page.locator('[data-setting-label]').first()).toBeVisible({ timeout: 10000 })
    // The Chat rail writes its default group into the path, as Channels does.
    expect(new URL(page.url()).pathname).toMatch(/^\/settings\/chat(\/transcript)?$/)
  })

  test('a legacy ?tab= link translates to the path form and still renders the panel', async ({ page }) => {
    // Old bookmarks and docs keep landing: SettingsPage replace-navigates the
    // legacy query form onto the canonical path.
    await page.goto('/settings?tab=chat', { waitUntil: 'domcontentloaded' })
    await page.waitForURL('**/settings/chat**', { timeout: 10000 })
    await expect(page.locator('[data-setting-label]').first()).toBeVisible({ timeout: 10000 })
    expect(page.url()).not.toContain('tab=')
  })

  test('config mutation round-trip: recent_tint_count via Display stepper', async ({ page, request }) => {
    // Read the current value via API
    const before = await (await request.get('/api/config/kirocrew')).json()
    const originalCount: number = before?.dashboard?.recent_tint_count ?? 0

    // Navigate to the Display tab's Sidebar Colors rail item, where the
    // "Highlight recent sessions" stepper now lives after the rail split.
    await page.goto('/settings/display/sidebar', { waitUntil: 'domcontentloaded' })

    // Find the "Highlight recent sessions" stepper
    const field = page.locator('[data-setting-label="Highlight recent sessions"]')
    await expect(field).toBeVisible({ timeout: 10000 })

    // Step in whichever direction is actually available. clampTintCount
    // (recencyTint.ts:21) floors the value into [0, MAX_RECENT_TINT_COUNT],
    // and DisplayPanel passes no `disabled` prop, so at the maximum the
    // Increase button is still clickable but the persisted value stays at the
    // bound -- an assertion of originalCount + 1 would then poll for a value
    // one past the bound until it times out. The harness always starts at the 0
    // default so increment is the normal path, but a developer running this
    // against a live gateway may sit at either bound.
    const atMax = originalCount >= MAX_TINT
    const delta = atMax ? -1 : 1
    const btn = field.getByRole('button', { name: atMax ? 'Decrease' : 'Increase' })
    await btn.click()

    // Read back via API to verify server-side persistence. expect.poll owns the
    // wait for the mutation to reach the server -- no fixed sleep needed.
    await expect.poll(async () => {
      const cfg = await (await request.get('/api/config/kirocrew')).json()
      return cfg?.dashboard?.recent_tint_count
    }, { timeout: 5000 }).toBe(originalCount + delta)

    // Restore original value to not pollute other tests
    await request.fetch('/api/config/kirocrew', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify({ path: 'dashboard.recent_tint_count', value: originalCount }),
    })
  })

  test('/overview redirects to /settings/overview', async ({ page }) => {
    await page.goto('/overview', { waitUntil: 'domcontentloaded' })
    // React Router Navigate with replace — the redirect mints the PATH form,
    // and nothing rewrites it afterwards (the tab-sync effect short-circuits
    // on a present segment).
    await page.waitForURL('**/settings/overview', { timeout: 10000 })
    expect(new URL(page.url()).pathname).toBe('/settings/overview')
    // The Overview panel content should render (health status)
    await expect(page.getByText(/All systems running|Connecting|Reconnecting/)).toBeVisible({ timeout: 10000 })
  })

  test('/instances redirects to /settings/instances', async ({ page }) => {
    await page.goto('/instances', { waitUntil: 'domcontentloaded' })
    await page.waitForURL('**/settings/instances', { timeout: 10000 })
    // Remote Crew panel should render — check for the tab being active
    await expect(page.getByRole('button', { name: 'Remote Crew', exact: true })).toBeVisible({ timeout: 5000 })
  })

  test.describe('Chat section list', () => {
    const TOLERANCE = 1

    interface Rect { top: number; bottom: number; left: number }
    interface Probe {
      area: Rect & { scrollTop: number; scrollHeight: number; clientHeight: number }
      list: Rect & { height: number; options: number }
      nav: Rect
      scroller: Rect & { scrollTop: number; scrollHeight: number; clientHeight: number }
      cue: (Rect & { hidden: boolean }) | null
      header: Rect
      firstSetting: Rect
      lastOption: Rect
    }

    // One evaluate reads every rect in the same frame, so the values compare
    // like with like. The scroll area is the parent of the pane, which is the
    // box the settings scroll in. The pane is read through a locator, so the
    // read waits for it instead of racing the render.
    async function probe(page: Page): Promise<Probe> {
      return page.getByTestId('side-panel-pane').evaluate(pane => {
        const rect = (el: Element) => {
          const r = el.getBoundingClientRect()
          return { top: r.top, bottom: r.bottom, left: r.left }
        }
        const area = pane.parentElement as HTMLElement
        const list = document.querySelector('[role="listbox"][aria-label="Chat settings"]') as HTMLElement
        const nav = list.closest('nav') as HTMLElement
        const scroller = list.parentElement as HTMLElement
        const cue = document.querySelector('[data-testid="settings-subnav-more"]')
        const options = list.querySelectorAll('[role="option"]')
        return {
          area: {
            ...rect(area),
            scrollTop: area.scrollTop,
            scrollHeight: area.scrollHeight,
            clientHeight: area.clientHeight,
          },
          list: { ...rect(list), height: list.getBoundingClientRect().height, options: options.length },
          nav: rect(nav),
          scroller: {
            ...rect(scroller),
            scrollTop: scroller.scrollTop,
            scrollHeight: scroller.scrollHeight,
            clientHeight: scroller.clientHeight,
          },
          cue: cue && { ...rect(cue), hidden: getComputedStyle(cue).visibility === 'hidden' },
          header: rect(document.querySelector('[data-testid="side-panel-header"]') as Element),
          firstSetting: rect(document.querySelector('[data-setting-label]') as Element),
          lastOption: rect(options[options.length - 1]),
        }
      })
    }

    // Wait until two consecutive probes agree so a smooth-scroll or a late
    // layout pass cannot leak into the readings.
    async function settled(page: Page): Promise<Probe> {
      let previous = ''
      await expect.poll(async () => {
        const current = JSON.stringify(await probe(page))
        const same = current === previous
        previous = current
        return same
      }).toBe(true)
      return probe(page)
    }

    // Set the settings scroll position, then settle.
    async function scrollAndSettle(page: Page, top: number): Promise<Probe> {
      await page.getByTestId('side-panel-pane').evaluate((pane, t) => {
        ;(pane.parentElement as HTMLElement).scrollTop = t
      }, top)
      return settled(page)
    }

    // The same for the capped list's own scroll position.
    async function scrollListAndSettle(page: Page, top: number): Promise<Probe> {
      await page.getByRole('listbox', { name: 'Chat settings' }).evaluate((list, t) => {
        ;(list.parentElement as HTMLElement).scrollTop = t
      }, top)
      return settled(page)
    }

    async function openChatSettings(page: Page, viewport: { width: number; height: number }) {
      await page.setViewportSize(viewport)
      await page.goto('/settings/chat/transcript', { waitUntil: 'domcontentloaded' })
      const list = page.getByRole('listbox', { name: 'Chat settings' })
      await expect(list).toBeVisible({ timeout: 10000 })
      await expect(list.getByRole('option', { name: 'Advanced' })).toBeVisible()
      await expect(page.locator('[data-setting-label]').first()).toBeVisible()
    }

    test('the Chat section list stays pinned while the settings scroll', async ({ page }) => {
      await openChatSettings(page, { width: 1366, height: 768 })
      const rest = await scrollAndSettle(page, 0)

      // The page must really scroll and the list must really exist, or the
      // claims below pass without testing anything.
      const range = rest.area.scrollHeight - rest.area.clientHeight
      expect(range).toBeGreaterThan(300)
      expect(rest.list.options).toBeGreaterThanOrEqual(7)

      const a = await scrollAndSettle(page, Math.floor(range / 2))
      const b = await scrollAndSettle(page, range)

      // Positive controls: the area scrolled, the title scrolled away, and the
      // settings rows moved up with the scroll.
      expect(a.area.scrollTop).toBeGreaterThan(0)
      expect(b.area.scrollTop).toBeGreaterThan(a.area.scrollTop + 100)
      expect(b.header.bottom).toBeLessThan(b.area.top)
      expect(Math.abs((rest.firstSetting.top - b.firstSetting.top) - (b.area.scrollTop - rest.area.scrollTop))).toBeLessThanOrEqual(TOLERANCE)

      for (const scrolled of [a, b]) {
        expect(scrolled.list.top).toBeGreaterThanOrEqual(scrolled.area.top - TOLERANCE)
        expect(scrolled.list.top).toBeLessThanOrEqual(scrolled.area.top + 24)
        expect(scrolled.list.bottom).toBeLessThanOrEqual(scrolled.area.bottom + TOLERANCE)
        expect(Math.abs(scrolled.list.left - rest.list.left)).toBeLessThanOrEqual(TOLERANCE)
      }
      expect(Math.abs(a.list.top - b.list.top)).toBeLessThanOrEqual(TOLERANCE)
    })

    test('a section list taller than the scroll area scrolls on its own', async ({ page }) => {
      await openChatSettings(page, { width: 1366, height: 280 })
      const rest = await scrollAndSettle(page, 0)
      const range = rest.area.scrollHeight - rest.area.clientHeight
      const middle = await scrollAndSettle(page, Math.floor(range / 2))
      const end = await scrollAndSettle(page, range)

      // Guards: the list really is taller than the area, the area scrolled,
      // and the middle reading is past the title.
      expect(end.list.height).toBeGreaterThan(end.area.clientHeight)
      expect(middle.area.scrollTop).toBeGreaterThan(0)
      expect(end.area.scrollTop).toBeGreaterThan(middle.area.scrollTop + 100)
      expect(middle.header.bottom).toBeLessThan(middle.area.top)

      // The whole capped list stays inside the area, at the end of the scroll
      // as well as while it is pinned.
      for (const scrolled of [middle, end]) {
        expect(scrolled.nav.top).toBeGreaterThanOrEqual(scrolled.area.top - TOLERANCE)
        expect(scrolled.nav.bottom).toBeLessThanOrEqual(scrolled.area.bottom + TOLERANCE)
        expect(scrolled.scroller.scrollHeight).toBeGreaterThan(scrolled.scroller.clientHeight)
      }

      // The "More below" cue marks the visible bottom edge of the capped list:
      // visible while rows remain below, inside the capped box under the
      // scrolling rows, and still on that edge once the list has scrolled.
      expect(middle.scroller.scrollTop).toBe(0)
      expect(middle.cue).not.toBeNull()
      expect(middle.cue!.hidden).toBe(false)
      expect(middle.cue!.top).toBeGreaterThanOrEqual(middle.scroller.bottom - TOLERANCE)
      expect(Math.abs(middle.cue!.bottom - middle.nav.bottom)).toBeLessThanOrEqual(TOLERANCE)

      const listRange = middle.scroller.scrollHeight - middle.scroller.clientHeight
      const listMiddle = await scrollListAndSettle(page, Math.floor(listRange / 2))
      expect(listMiddle.scroller.scrollTop).toBeGreaterThan(0)
      expect(listMiddle.scroller.scrollTop).toBeLessThan(listRange - 1)
      expect(listMiddle.cue).not.toBeNull()
      expect(listMiddle.cue!.hidden).toBe(false)
      expect(Math.abs(listMiddle.cue!.bottom - listMiddle.nav.bottom)).toBeLessThanOrEqual(TOLERANCE)

      const bottom = await scrollListAndSettle(page, listRange)
      expect(bottom.cue).not.toBeNull()
      expect(bottom.cue!.hidden).toBe(true)
      expect(bottom.lastOption.bottom).toBeLessThanOrEqual(bottom.area.bottom + TOLERANCE)
      expect(bottom.lastOption.bottom).toBeGreaterThanOrEqual(bottom.area.top)
    })
  })
})
