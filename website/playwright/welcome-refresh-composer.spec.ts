import { test, expect, Page, APIRequestContext } from '@playwright/test'

/**
 * New-chat welcome hero on a phone with the software keyboard open: the
 * "Refresh suggestions" row must never take a tap meant for the composer, and it
 * must be possible to scroll it clear of the composer.
 *
 * The hero scrolls UNDER the floating composer dock (ChatPage), which has no
 * z-index on purpose and wins by DOM order. Two things broke that on main:
 *   1. the refresh row's `relative z-20` escaped the welcome view and painted
 *      above the dock, so the row sat on the composer's model row, mic and Send;
 *   2. the welcome column was `min-h-0`, so its content spilled past the hero's
 *      bottom padding (the dock height) and no scroll lifted the last row clear.
 * Only a real hit test shows either; happy-dom has none.
 *
 * Keyboard-open is emulated the way Chromium handles the page's
 * `interactive-widget=resizes-content` hint: the layout viewport loses the
 * keyboard's height (390x844 with a 336px keyboard). Suggestions are stubbed so
 * the hero always holds six rows and overflows.
 *
 * The capture harness (website/scripts/capture-welcome-refresh-send.mjs) runs
 * the same checks across more viewports for screenshots; this spec is the one
 * CI runs. SERIAL-RUN DEPENDENCY: as sidebar-glass-dock.spec.ts.
 */

const seeded: string[] = []
const SUGGESTIONS = Array.from({ length: 6 }, (_, i) => ({ text: `Suggested task number ${i + 1} for the welcome hero`, kind: 'general' }))

async function seedSlot(request: APIRequestContext) {
  const res = await request.post('/api/chat/slots', { data: { agent: 'default' } })
  expect(res.ok(), `POST /api/chat/slots should succeed (HTTP ${res.status()})`).toBe(true)
  const slot = await res.json()
  seeded.push(slot.key)
  return slot.key as string
}

test.afterEach(async ({ request }) => {
  for (const key of seeded.splice(0)) await request.delete(`/api/chat/slots/${key}`)
})

/** Scroll the hero (row centre onto Send, or to the end) and hit-test both sides. */
async function probe(page: Page, where: 'send' | 'end') {
  return page.evaluate((pos) => {
    const dock = document.querySelector<HTMLElement>('[data-testid="composer-dock-root"]')!
    const row = document.querySelector<HTMLElement>('[data-testid="welcome-suggestions"] > div:last-child button')!
    const send = dock.querySelector<HTMLElement>('button[aria-label="Send"]')!
    const hero = row.closest<HTMLElement>('.overflow-y-auto')!
    hero.scrollTop = pos === 'end' ? hero.scrollHeight : 0
    if (pos === 'send') {
      const rr = row.getBoundingClientRect(), sr = send.getBoundingClientRect()
      hero.scrollTop += (rr.top + rr.height / 2) - (sr.top + sr.height / 2)
    }
    const centre = (el: HTMLElement) => { const b = el.getBoundingClientRect(); return document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2) }
    const covered = Array.from(dock.querySelectorAll<HTMLElement>('button'))
      .filter(b => b.getBoundingClientRect().width > 1)
      .filter(b => { const hit = centre(b); return !hit || !b.contains(hit) })
      .map(b => b.getAttribute('aria-label') || b.textContent?.trim() || b.tagName)
    const rowHit = centre(row)
    // The dock's visible top: the memory-mode chip when it renders, else the composer.
    const top = (dock.querySelector<HTMLElement>('[data-testid="composer-memory-chip"]') ?? dock.querySelector<HTMLElement>('textarea[data-composer-input]')!).getBoundingClientRect().top
    return {
      covered,
      rowOnSend: row.getBoundingClientRect().bottom > send.getBoundingClientRect().top,
      rowReachable: !!rowHit && row.contains(rowHit) && row.getBoundingClientRect().bottom <= top,
    }
  }, where)
}

test('phone, keyboard open: Refresh suggestions never covers the composer and scrolls clear of it', async ({ page, request }) => {
  const key = await seedSlot(request)
  await page.addInitScript(() => { localStorage.setItem('mc-onboarded', '1') })
  await page.route('**/api/suggestions*', route => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ suggestions: SUGGESTIONS, generated_at: Date.now() / 1000, stale: false }),
  }))
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`/chat?sid=${encodeURIComponent(key)}`, { waitUntil: 'domcontentloaded' })
  const input = page.locator('[data-testid="composer-dock-root"] textarea[data-composer-input]')
  await expect(input).toBeVisible({ timeout: 15000 })
  await expect(page.getByRole('button', { name: 'Refresh suggestions' })).toBeVisible()
  await input.fill('Test foo')
  await page.setViewportSize({ width: 390, height: 844 - 336 })
  await page.waitForTimeout(400)

  const onSend = await probe(page, 'send')
  expect(onSend.rowOnSend, 'the hero must be able to scroll the row onto Send, or this proves nothing').toBe(true)
  expect(onSend.covered, 'with the row scrolled onto Send, every composer control still takes its own tap').toEqual([])

  const end = await probe(page, 'end')
  expect(end.covered, 'scrolled to the end, every composer control still takes its own tap').toEqual([])
  expect(end.rowReachable, 'scrolled to the end, Refresh suggestions sits above the dock and takes its own tap').toBe(true)
})
