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
})
