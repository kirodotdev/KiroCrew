import { test, expect, Page, APIRequestContext } from '@playwright/test'

/**
 * The list panels' floating search dock (components/ListDock.tsx) is a
 * `pointer-events-none` band over the rows with an enumerated allowlist of
 * descendants that re-arm pointer events (the glass field, every focusable, a
 * notice's alert/status box). Two things about that only a real hit-test can
 * prove, and jsdom has no hit-testing:
 *   1. every focusable inside the dock lands on ITSELF under
 *      `elementFromPoint` — a control that fell outside the allowlist would
 *      dead-click in production with no error anywhere;
 *   2. the space beside a lone filter chip lands on the dock, not on a row the
 *      opaque shelf hides — a click on what reads as blank panel is inert
 *      rather than a surprise navigation.
 * Both run against the Sessions sidebar and the Crew Members roster, which
 * mount the same dock.
 *
 * The capture harness (website/scripts/capture-glass-search-dock.mjs) runs the
 * same two assertions for its screenshots; this spec is the one CI runs.
 *
 * SERIAL-RUN DEPENDENCY: same as sidebar-folder-alignment.spec.ts (the gate runs
 * workers: 1; session-tags-folders.spec.ts wipes slots in its beforeEach).
 */

const seeded = { slots: [] as string[] }

async function primeBrowser(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('mc-onboarded', '1')
    // The running-only filter: its chip renders on the dock's shelf whether or
    // not any session is running, which is the grown state under test.
    localStorage.setItem('mc-session-running-only', '1')
  })
  await page.setViewportSize({ width: 1400, height: 700 })
}

async function seedSlot(request: APIRequestContext) {
  const res = await request.post('/api/chat/slots', { data: { agent: 'default' } })
  expect(res.ok(), 'POST /api/chat/slots should succeed').toBe(true)
  const slot = await res.json()
  expect(slot.key, 'slot should be created').toBeTruthy()
  seeded.slots.push(slot.key)
}

test.afterEach(async ({ request }) => {
  for (const key of seeded.slots.splice(0)) await request.delete(`/api/chat/slots/${key}`)
})

/** Every focusable inside the dock must be what `elementFromPoint` returns at its centre. */
async function dockControlMisses(page: Page, dock: string) {
  return page.evaluate((sel) => {
    const root = document.querySelector<HTMLElement>(sel)!
    const misses: string[] = []
    const controls = root.querySelectorAll<HTMLElement>('button, a[href], input, select, textarea, [tabindex]:not([tabindex="-1"]), [role="button"]')
    for (const el of controls) {
      const r = el.getBoundingClientRect()
      if (r.width < 2 || r.height < 2) continue
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
      if (!hit || !(el === hit || el.contains(hit))) misses.push(`${el.tagName.toLowerCase()}[${el.getAttribute('data-testid') || el.getAttribute('aria-label') || ''}]`)
    }
    return { count: controls.length, misses }
  }, dock)
}

/** What the point just inside the chip row's right edge lands on: 'dock' or 'below'. */
async function besideChipLandsOn(page: Page, chipRow: string) {
  return page.evaluate((sel) => {
    const row = document.querySelector<HTMLElement>(sel)!
    const r = row.getBoundingClientRect()
    const hit = document.elementFromPoint(r.right - 6, r.top + r.height / 2)
    return hit && hit.closest('[data-testid="list-dock"]') ? 'dock' : 'below'
  }, chipRow)
}

test.describe('Floating search dock: controls hit, opaque shelf inert', () => {
  test('Sessions sidebar: every dock control hit-tests to itself; the space beside the filter chip is inert', async ({ page, request }) => {
    await primeBrowser(page)
    // Enough rows to scroll one under the shelf beside the chip.
    for (let i = 0; i < 12; i++) await seedSlot(request)
    await page.goto('/chat')
    const dock = page.locator('[data-testid="list-dock"]').first()
    await expect(dock).toBeVisible({ timeout: 15000 })
    await expect(page.locator('[data-testid="search-field-glass"]').first()).toBeVisible()
    const lane = page.locator('[data-testid="tree-view-lane"]')
    await expect(lane).toBeVisible()

    const probe = await dockControlMisses(page, '[data-testid="list-dock"]')
    expect(probe.count, 'the dock holds the field, its menu and the chip').toBeGreaterThanOrEqual(3)
    expect(probe.misses, 'every dock control must be what the pointer reaches').toEqual([])

    // The running-only chip row is under the field; scroll a row beneath it.
    const chipRow = page.locator('[data-testid="list-dock"] .flex-wrap').first()
    await expect(chipRow).toBeVisible()
    await lane.evaluate(el => { el.scrollTop = 60 })
    await page.waitForTimeout(150)
    expect(await besideChipLandsOn(page, '[data-testid="list-dock"] .flex-wrap'), 'beside the chip the pointer lands on the inert shelf, not on a hidden row').toBe('dock')
  })

  test('Crew Members roster: every dock control hit-tests to itself', async ({ page }) => {
    await primeBrowser(page)
    await page.goto('/members')
    const dock = page.locator('#main-content [data-testid="list-dock"]').first()
    await expect(dock).toBeVisible({ timeout: 15000 })
    await expect(page.locator('#main-content [data-testid="search-field-glass"]').first()).toBeVisible()
    const probe = await dockControlMisses(page, '#main-content [data-testid="list-dock"]')
    expect(probe.count, 'the roster dock holds the field and its menu').toBeGreaterThanOrEqual(2)
    expect(probe.misses, 'every roster dock control must be what the pointer reaches').toEqual([])
  })
})
