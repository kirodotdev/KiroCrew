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

    // The assistant reply carries a stable server id, so Fork is immediately
    // available from one overflow trigger outside the crowded inline action row.
    const target = page.locator('[data-role="assistant"]').last()
    await expect(target).toBeVisible({ timeout: 60000 })
    await target.hover({ force: true })
    const more = target.getByTestId('assistant-more-actions')
    await expect(more).toBeVisible()
    await more.click({ force: true })
    const forkButton = page.getByTestId('fork-from-here')
    await expect(forkButton).toBeVisible()
    await expect(forkButton).toBeEnabled()
    await expect(page.getByTestId('fork-unavailable-reason')).toHaveCount(0)
    // The overflow trigger is Share's permanent home, so it renders even when
    // fork is a ROW button; fork itself must not have moved inside it.
    await expect(page.getByTestId('assistant-more-actions')).toHaveCount(1)

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


const HARNESS_GATEWAY = process.env.KIROCREW_E2E_EPHEMERAL === '1'

test.describe('Forking from long timestamped sessions', () => {
  test('forks immediately without loading earlier messages in the client', async ({ page, request }) => {
    test.skip(!HARNESS_GATEWAY, 'the 1,000-message fixture requires the ephemeral E2E gateway')
    test.setTimeout(120_000)

    const messages = Array.from({ length: 1_000 }, (_, index) => ({
      role: index % 2 === 0 ? 'user' : 'assistant',
      content: `Long-chat fork reproduction ${String(index).padStart(4, '0')}`,
      ts: new Date(Date.UTC(2026, 0, 1, 0, 0, 0, index)).toISOString(),
    }))
    const imported = await request.post('/api/chat/slots/import', {
      data: {
        bundle_version: 1,
        title: 'Long timestamped fork reproduction',
        origin: 'playwright e2e',
        agent: '',
        messages,
      },
    })
    expect(imported.status(), `session import failed: ${await imported.text()}`).toBeLessThan(300)
    const { key } = await imported.json() as { key: string }
    let forkKey = ''

    try {
      const olderPageRequests: number[] = []
      await page.route('**/api/chat/slots/**', async route => {
        const url = new URL(route.request().url())
        if (route.request().method() === 'GET' && url.searchParams.has('before')) {
          olderPageRequests.push(Number(url.searchParams.get('before')))
          await new Promise(resolve => setTimeout(resolve, 250))
        }
        await route.continue()
      })

      await page.goto(`/chat/long-timestamped-fork-reproduction?sid=${encodeURIComponent(key)}`, {
        waitUntil: 'domcontentloaded',
      })
      const dismissUpdate = page.getByRole('button', { name: 'Dismiss' })
      if (await dismissUpdate.isVisible({ timeout: 5_000 }).catch(() => false)) {
        await dismissUpdate.click()
        await expect(dismissUpdate).toBeHidden()
      }
      await expect(page.getByPlaceholder(/message/i)).toBeVisible({ timeout: 10_000 })

      const target = page.locator('[data-role="assistant"]').last()
      await expect(target).toBeVisible({ timeout: 10_000 })
      await target.hover({ force: true })

      // A server-issued message identity makes the target actionable immediately:
      // one overflow trigger, no disabled countdown walk, and no client history fetch.
      const more = target.getByTestId('assistant-more-actions')
      await expect(more).toBeVisible()
      await more.click({ force: true })
      const fork = page.getByTestId('fork-from-here')
      await expect(fork).toBeVisible()
      await expect(fork).toBeEnabled()
      await expect(page.getByTestId('fork-unavailable-reason')).toHaveCount(0)
      expect(olderPageRequests).toEqual([])

      const forkResponse = page.waitForResponse(response =>
        response.request().method() === 'POST' && response.url().endsWith('/fork'),
      )
      await fork.click({ force: true })
      const forkBody = await (await forkResponse).json() as { key?: string }
      forkKey = forkBody.key || ''
      await expect(page.getByText(/Fork of /).first()).toBeVisible({ timeout: 10_000 })
      expect(olderPageRequests).toEqual([])
    } finally {
      if (forkKey) await request.delete(`/api/chat/slots/${encodeURIComponent(forkKey)}`)
      await request.delete(`/api/chat/slots/${encodeURIComponent(key)}`)
    }
  })
})