import { test, expect } from '@playwright/test'

/**
 * Playwright launches Chromium with --hide-scrollbars, which gives every box
 * a zero-width scrollbar. Real browsers draw the composer's 6px scrollbar
 * (index.css `::-webkit-scrollbar`) once the draft overflows. The mirror never
 * scrolls visibly, so without a reserved gutter on both boxes the textarea's
 * text box is 6px narrower, wraps a line the mirror keeps whole, and every
 * chip after that line sits one line off its token.
 *
 * The launch flag is worker-scoped, so this case lives in its own file:
 * composer-paste-highlight.spec.ts keeps the default hidden scrollbars.
 */
test.use({ launchOptions: { ignoreDefaultArgs: ['--hide-scrollbars'] } })

test('paste chip mirror wraps at the textarea width when the textarea scrolls', async ({ page }) => {
  await page.goto('/chat')
  const input = page.locator('textarea[data-composer-input]').first()
  await expect(input).toBeVisible({ timeout: 15000 })
  await input.click()
  for (let i = 0; i < 12; i += 1) {
    await page.keyboard.insertText(`typed line ${i + 1}`)
    await page.keyboard.press('Shift+Enter')
  }
  const pasted = Array.from({ length: 7 }, (_, i) => `pasted line ${i + 1}`).join('\n')
  await input.evaluate((el, text) => {
    const data = new DataTransfer()
    data.setData('text/plain', text)
    el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }))
  }, pasted)
  await expect(page.locator('[aria-hidden] [data-paste-seq]')).toHaveCount(1)
  const boxes = await input.evaluate(ta => {
    const mirror = document.querySelector('[aria-hidden] [data-paste-seq]')!.closest('[aria-hidden]') as HTMLElement
    return {
      scrollbar: ta.offsetWidth - ta.clientWidth,
      overflows: ta.scrollHeight > ta.clientHeight,
      widthGap: mirror.clientWidth - ta.clientWidth,
    }
  })
  // Guard the precondition: the scrollbar must actually take width here,
  // or this test proves nothing.
  expect(boxes.overflows).toBe(true)
  expect(boxes.scrollbar).toBeGreaterThan(0)
  expect(boxes.widthGap).toBe(0)
})

test('autosize measuring twin reserves the same scrollbar gutter as the textarea', async ({ page }) => {
  await page.goto('/chat')
  const input = page.locator('textarea[data-composer-input]').first()
  await expect(input).toBeVisible({ timeout: 15000 })
  await input.click()
  // A few short lines: the draft stays below the 140px cap, so the twin's
  // measurement is what sets the textarea's height here.
  for (let i = 0; i < 3; i += 1) {
    await page.keyboard.insertText(`typed line ${i + 1}`)
    await page.keyboard.press('Shift+Enter')
  }
  // The off-screen twin is the one textarea that is aria-hidden and readonly
  // and is not the live composer input.
  const twin = page.locator('textarea[aria-hidden="true"][readonly]:not([data-composer-input])')
  await expect(twin).toHaveCount(1)
  const widths = await input.evaluate((ta, twinEl) => ({
    gutter: ta.offsetWidth - ta.clientWidth,
    live: ta.clientWidth,
    twin: (twinEl as HTMLTextAreaElement).clientWidth,
  }), await twin.elementHandle())
  // Guard the precondition: the live textarea must reserve a gutter here, or
  // a matching twin proves nothing.
  expect(widths.gutter).toBeGreaterThan(0)
  expect(widths.twin).toBe(widths.live)
})
