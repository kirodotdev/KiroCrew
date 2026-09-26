import { test, expect } from '@playwright/test'
import type { APIRequestContext } from '@playwright/test'

/**
 * This test needs two sessions to be meaningful: it proves the SELECTED session
 * is restored rather than the list falling back to the first row. It used to
 * `test.skip` when fewer than two existed, which reported green while verifying
 * nothing. It now seeds the precondition instead.
 *
 * Seeding is additive (POST /api/chat/slots), never destructive, so unlike the
 * tag-column specs this needs no KIROCREW_E2E_EPHEMERAL guard. Only the missing
 * slots are created, and each gets a distinct title so the restore assertion
 * cannot pass by comparing two identical strings.
 */
async function seedTwoSessions(request: APIRequestContext) {
  const existing = await (await request.get('/api/chat/slots')).json()
  const stamp = Date.now()
  for (let i = existing.length; i < 2; i++) {
    const slot = await (
      await request.post('/api/chat/slots', { data: { agent: 'default' } })
    ).json()
    await request.patch(`/api/chat/slots/${slot.key}/title`, {
      data: { title: `active-slot-seed-${stamp}-${i}` },
    })
  }
}

test.describe('Active slot persistence across surface switches', () => {
  test.beforeEach(async ({ page, request }) => {
    await seedTwoSessions(request)
    await page.goto('/chat', { waitUntil: 'domcontentloaded' })
    await expect(page.locator('.session-row').first()).toBeVisible({ timeout: 10000 })
  })

  test('remembers selected session when leaving /chat and returning', async ({ page }) => {
    const rows = page.locator('.session-row')
    // Seeded above, so a shortfall is a seeding regression, not a reason to skip.
    await expect(rows.nth(1)).toBeVisible({ timeout: 10000 })

    // Click the second session (not the first, which is the default fallback).
    // Identity comes from the row's data-slot-key wrapper, not from rendered
    // title text: the title element's class has already churned (.font-mono no
    // longer exists inside .session-row) and a slot key is a stronger identity
    // than a display string.
    const selectedKey = await rows
      .nth(1)
      .locator('xpath=ancestor-or-self::*[@data-slot-key][1]')
      .getAttribute('data-slot-key')
    expect(selectedKey).toBeTruthy()
    await rows.nth(1).click()
    await expect(rows.nth(1)).toHaveClass(/session-active/, { timeout: 2000 })

    const nav = page.locator('nav[aria-label="Main navigation"]')
    // The original round-trip went via an "Autopilot" nav surface. That surface
    // no longer exists: /orchestrated is now a redirect to /chat (App.tsx
    // OrchestratedRedirect) and 'chat' is the only builtin declaring a slotMode,
    // so there is no slot-mode counterpart left to switch to. The guard that
    // skipped when Autopilot was absent was therefore dead code that could never
    // pass. Settings is a always-present builtin, and leaving /chat and returning
    // still exercises the behaviour under test: the sidebar must restore the
    // selected slot instead of falling back to the first row.
    await nav.getByText('Settings').click()
    await page.waitForURL('**/settings**')

    // Switch back to Sessions (the chat surface)
    await nav.getByText('Sessions').click()
    await page.waitForURL('**/chat**')
    await expect(page.locator('.session-row').first()).toBeVisible({ timeout: 10000 })

    // The previously selected session should still be active, and be the ONLY
    // active row: a fallback to the first row shows up either as the wrong key
    // or as a second active row.
    await expect(page.locator('.session-row.session-active')).toHaveCount(1, { timeout: 5000 })
    await expect(
      page.locator(`[data-slot-key="${selectedKey}"] .session-row.session-active`),
    ).toBeVisible({ timeout: 5000 })
  })

  test('keeps scrolled session tabs clear of the collapsed sidebar toggle', async ({ page, request }) => {
    const seededKeys: string[] = []
    const stamp = Date.now()

    try {
      for (let i = 0; i < 8; i++) {
        const slot = await (
          await request.post('/api/chat/slots', { data: { agent: 'default' } })
        ).json()
        seededKeys.push(slot.key)
        await request.patch(`/api/chat/slots/${slot.key}/title`, {
          data: { title: `overlap-regression-${stamp}-${i}` },
        })
      }

      await page.setViewportSize({ width: 900, height: 700 })
      await page.evaluate(() => localStorage.setItem('mc-sidebar-pinned', 'true'))
      await page.reload({ waitUntil: 'domcontentloaded' })

      const seededRows = seededKeys.map(key => page.locator(`[data-slot-key="${key}"] .session-row`))
      await expect(seededRows[0]).toBeVisible({ timeout: 10000 })
      await seededRows[0].click()
      for (const row of seededRows.slice(1)) await row.click({ button: 'middle' })

      const strip = page.getByTestId('session-tab-strip')
      await expect(strip.getByRole('tab')).toHaveCount(8)
      await page.getByRole('button', { name: 'Hide sessions sidebar', exact: true }).click()
      const toggle = page.getByRole('button', { name: 'Show sessions sidebar', exact: true })
      await expect(toggle).toBeVisible()

      // The strip's inset glides on the same 240ms curve as the sidebar morph
      // and the aria-label flips at the START of that slide, so a measurement
      // taken then reads mid-transition geometry. As in the sidebar alignment
      // specs, poll until two consecutive frames measure identically, then
      // assert against the settled numbers.
      type Geometry = {
        toggle: { x: number; width: number }
        strip: { x: number; width: number; scrollLeft: number; maxScroll: number }
        tabs: { left: number; right: number }[]
      }
      const measure = (): Promise<Geometry> =>
        page.evaluate(() => {
          const toggleEl = document.querySelector<HTMLElement>('button[aria-label="Show sessions sidebar"]')
          const stripEl = document.querySelector<HTMLElement>('[data-testid="session-tab-strip"]')
          if (!toggleEl || !stripEl) throw new Error('toggle or strip missing')
          const t = toggleEl.getBoundingClientRect()
          const s = stripEl.getBoundingClientRect()
          return {
            toggle: { x: t.x, width: t.width },
            strip: { x: s.x, width: s.width, scrollLeft: stripEl.scrollLeft, maxScroll: stripEl.scrollWidth - stripEl.clientWidth },
            tabs: Array.from(stripEl.querySelectorAll<HTMLElement>('[role="tab"]')).map(tab => {
              const r = tab.getBoundingClientRect()
              return { left: r.left, right: r.right }
            }),
          }
        })
      const settleAndMeasure = async (): Promise<Geometry> => {
        const frames: { prev: Geometry | null; cur: Geometry | null } = { prev: null, cur: null }
        await expect
          .poll(async () => {
            frames.prev = frames.cur
            frames.cur = await measure()
            return frames.prev !== null && JSON.stringify(frames.cur) === JSON.stringify(frames.prev) ? 'settled' : 'moving'
          }, { timeout: 15000, message: 'toggle and tab strip geometry should settle after the sidebar collapses' })
          .toBe('settled')
        return frames.cur as Geometry
      }

      const assertClearance = (geometry: Geometry) => {
        const toggleRight = geometry.toggle.x + geometry.toggle.width
        const stripRight = geometry.strip.x + geometry.strip.width
        const firstPaintedTabLeft = Math.min(
          ...geometry.tabs
            .filter(tab => tab.right > geometry.strip.x && tab.left < stripRight)
            .map(tab => Math.max(tab.left, geometry.strip.x)),
        )
        expect(firstPaintedTabLeft - toggleRight).toBeGreaterThanOrEqual(4)
      }

      const atStart = await settleAndMeasure()
      expect(atStart.strip.scrollLeft).toBe(0)
      assertClearance(atStart)

      // maxScroll is read from the settled geometry: the strip's clientWidth
      // shrinks as the inset animates in, so a value read earlier would be stale.
      expect(atStart.strip.maxScroll).toBeGreaterThan(0)
      await strip.evaluate((element, left) => { element.scrollLeft = left }, atStart.strip.maxScroll)
      const atEnd = await settleAndMeasure()
      expect(atEnd.strip.scrollLeft).toBe(atStart.strip.maxScroll)
      assertClearance(atEnd)
    } finally {
      for (const key of seededKeys) await request.delete(`/api/chat/slots/${key}`)
    }
  })
})
