import { test, expect } from '@playwright/test'

/**
 * E2E test for the "Fork session" feature.
 *
 * Exercises the full round-trip: send message → wait for assistant reply →
 * click fork button → verify new tab.
 *
 * Tagged @needs-agent, so it runs only when an agent turn is available. The e2e
 * harness supplies one by pointing KIROCREW_KIRO_BIN at the stub ACP backend,
 * which answers deterministically and offline. A missing reply is a failure, not
 * an environment gap.
 */

test.describe('Fork Session E2E', { tag: '@needs-agent' }, () => {
  test.beforeEach(async ({ page }) => {
    // Auth is handled by the 'setup' project (playwright.config.ts), which
    // exchanges PLAYWRIGHT_TOKEN for a cookie and persists it via storageState.
    // Tests just navigate straight to /chat with the cookie already attached.
    await page.goto('/chat', { waitUntil: 'networkidle' })
    // Dismiss first-run theme picker modal if present.
    const letsGo = page.getByRole('button', { name: /let's go/i })
    if (await letsGo.isVisible({ timeout: 2000 }).catch(() => false)) {
      await letsGo.click()
    }
    // /chat shows an empty state until a slot exists. Click "New chat" first.
    const newChatButton = page.getByRole('button', { name: /new chat|\+/i }).first()
    if (await newChatButton.isVisible({ timeout: 5000 }).catch(() => false)) {
      await newChatButton.click()
    }
    await expect(page.getByPlaceholder(/message/i)).toBeVisible({ timeout: 10000 })
    // Let paint settle so the recording isn't black for the first frames.
    if (process.env.PLAYWRIGHT_VIDEO === '1') await page.waitForTimeout(500)
  })

  test('fork button appears on assistant message and creates new tab', async ({ page }) => {
    test.setTimeout(120000)
    // Send a message that should elicit a short assistant reply.
    const messageInput = page.getByPlaceholder(/message/i)
    await messageInput.fill('reply with a single word: ready')
    await page.keyboard.press('Enter')

    // A fresh chat is fully loaded, so fork is an in-place ROW button and needs no menu;
    // its visibility is the reply signal. By test id, not title: labels are i18n-driven.
    const forkButton = page.getByTestId('fork-from-here')
    await expect(forkButton).toBeVisible({ timeout: 60000 })
    // Fresh chat has nothing older, so a DISABLED fork here would mean the bounded
    // initial fetch regressed normal use rather than this spec drifting again.
    await expect(forkButton).toBeEnabled()
    await expect(page.getByTestId('assistant-more-actions')).toHaveCount(0)

    await forkButton.hover()
    // GIF-only pauses: skip in normal CI to keep tests fast.
    if (process.env.PLAYWRIGHT_VIDEO === '1') await page.waitForTimeout(1500)
    await forkButton.click()

    // New slot should appear with a "Fork of " title. The shipped fork-arrow
    // feature prefixes the title with "↳ " (↳ Fork of <parent>), so match the
    // "Fork of " substring rather than anchoring at the start of the string.
    await expect(page.getByText(/Fork of /).first()).toBeVisible({ timeout: 10000 })
    if (process.env.PLAYWRIGHT_VIDEO === '1') await page.waitForTimeout(2000)
  })
})
