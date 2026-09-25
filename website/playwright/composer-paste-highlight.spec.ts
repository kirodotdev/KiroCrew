import { test, expect, type Page } from '@playwright/test'

/**
 * The collapsed-paste chip in the textarea composer is painted by a backdrop
 * mirror (PasteHighlightLayer) that must line up with the token text the
 * textarea draws. The mirror fills its wrapper (`absolute inset-0`), so the
 * wrapper must be exactly as tall as the textarea. An inline-block textarea
 * leaves a line-box descender gap under itself, the wrapper grows by ~7px, the
 * mirror's scroll range grows with it, and once the draft scrolls the mirror
 * clamps to a different scrollTop than the textarea: the chip background
 * drifts off the `[ Paste #N · M lines ]` text. These tests fail on that.
 * CLI UI mode widens only the textarea's left padding, so the mirror needs the
 * same override or every chip sits 8px left of its text; the cli case pins it.
 *
 * Where the textarea draws the token is measured independently of the
 * mirror: a hidden probe div copies the textarea's COMPUTED style and lays
 * out the same value, so a regression in the mirror's own classes is caught.
 */
async function chipOffset(page: Page) {
  return page.evaluate(() => {
    const ta = document.querySelector('textarea[data-composer-input]') as HTMLTextAreaElement
    const chip = document.querySelector('[aria-hidden] [data-paste-seq]') as HTMLElement
    const mirror = chip.closest('[aria-hidden]') as HTMLElement
    const style = getComputedStyle(ta)
    const box = ta.getBoundingClientRect()
    const probe = document.createElement('div')
    for (const prop of Array.from(style)) probe.style.setProperty(prop, style.getPropertyValue(prop))
    Object.assign(probe.style, {
      position: 'fixed', left: `${box.left}px`, top: `${box.top}px`, width: `${box.width}px`,
      height: 'auto', overflow: 'hidden', visibility: 'hidden', whiteSpace: 'pre-wrap',
    })
    const value = ta.value
    const start = value.indexOf('[ Paste')
    const end = value.indexOf(']', start) + 1
    const mark = document.createElement('span')
    mark.textContent = value.slice(start, end)
    probe.append(document.createTextNode(value.slice(0, start)), mark, document.createTextNode(value.slice(end)))
    document.body.appendChild(probe)
    const expected = mark.getBoundingClientRect()
    probe.remove()
    const actual = chip.getBoundingClientRect()
    return {
      dy: actual.top - (expected.top - ta.scrollTop),
      dx: actual.left - expected.left,
      heightGap: mirror.getBoundingClientRect().height - box.height,
      scrollGap: mirror.scrollTop - ta.scrollTop,
    }
  })
}

// 0 typed lines never scrolls (catches the height gap); 20 pushes the draft
// past the composer height so the textarea scrolls, where the drift showed.
// CLI UI mode widens the textarea's left padding, which the mirror must follow.
const cases = [
  { typedLines: 0, ui: 'chat' },
  { typedLines: 20, ui: 'chat' },
  { typedLines: 20, ui: 'cli' },
]
for (const { typedLines, ui } of cases) {
  test(`paste chip highlight stays on the token after ${typedLines} typed lines (${ui} UI)`, async ({ page }) => {
    await page.addInitScript(mode => window.localStorage.setItem('mc-ui', mode), ui)
    await page.goto('/chat')
    const input = page.locator('textarea[data-composer-input]').first()
    await expect(input).toBeVisible({ timeout: 15000 })
    await input.click()
    for (let i = 0; i < typedLines; i += 1) {
      await page.keyboard.type(`typed line ${i + 1}`)
      await page.keyboard.press('Shift+Enter')
    }
    const pasted = Array.from({ length: 7 }, (_, i) => `pasted line ${i + 1}`).join('\n')
    await input.evaluate((el, text) => {
      const data = new DataTransfer()
      data.setData('text/plain', text)
      el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }))
    }, pasted)
    await expect(page.locator('[aria-hidden] [data-paste-seq]')).toHaveCount(1)
    // The mirror's scroll sync runs in a rAF after the value change.
    await expect.poll(async () => Math.abs((await chipOffset(page)).dy)).toBeLessThan(1.5)
    const offset = await chipOffset(page)
    expect(Math.abs(offset.dx)).toBeLessThan(1.5)
    expect(Math.abs(offset.heightGap)).toBeLessThan(1)
    expect(offset.scrollGap).toBe(0)
  })
}
