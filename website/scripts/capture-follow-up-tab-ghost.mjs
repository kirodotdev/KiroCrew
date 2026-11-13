/**
 * Frames for the composer's Tab-to-fill follow-up suggestion (issue #28788),
 * shot off capture/follow-up-tab-ghost.html (the REAL ChatPage). Every frame
 * asserts its state before it is written:
 *   01-suggestion   desktop, empty box: the first option is the placeholder
 *   02-after-tab    Tab pressed: the option is the draft, focus stays in the box
 *   03-typed        a typed character instead: the suggestion is gone, Tab does nothing
 *   04-mobile       390px touch: no suggestion, the chips stay
 *   (x2 themes: dark, light)
 *
 * Usage (from website/):
 *   npx vite --host 127.0.0.1 --port 6833 --strictPort     # another shell
 *   node scripts/capture-follow-up-tab-ghost.mjs http://127.0.0.1:6833 ../temp-screenshots/follow-up-tab-ghost
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6833'
const OUT = process.argv[3] || '../temp-screenshots/follow-up-tab-ghost'
mkdirSync(OUT, { recursive: true })
const FIRST = 'Open an issue and show screenshots'

const browser = await chromium.launch({ executablePath: chromiumExecutable(), env: { ...process.env, LD_LIBRARY_PATH: '' } })
let failed = false
const check = (name, ok, detail = '') => { console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`); if (!ok) failed = true }

async function newPage(theme, mobile) {
  const ctx = await browser.newContext(mobile
    ? { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true }
    : { viewport: { width: 1280, height: 720 }, deviceScaleFactor: 1 })
  const page = await ctx.newPage()
  await page.route(u => new URL(u).pathname.startsWith('/api/'), async route => {
    const path = new URL(route.request().url()).pathname
    if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) {
      const msgs = await page.evaluate(() => window.__CAPTURE_MESSAGES__)
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ key: 'slot-a', title: 'tab ghost fixture', running: false, has_more: false, total: msgs.length, messages: msgs }) })
    }
    if (/\/api\/chat\/(tags|pins|folders|tag-columns)$/.test(path)) return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    const isList = /commands|skills|agents$|sessions|files|history|models|artifacts|folders|slots$/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/follow-up-tab-ghost.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByRole('button', { name: /Open an issue only/ }).first().waitFor()
  await page.waitForTimeout(300)
  return page
}
const box = page => page.locator('[data-capture-root] textarea[data-composer-input]')

for (const theme of ['dark', 'light']) {
  {
    const page = await newPage(theme, false)
    await box(page).focus()
    const ph = await box(page).getAttribute('placeholder')
    check(`01-${theme} suggestion is the placeholder`, ph === `${FIRST} - Press Tab to fill`, `placeholder=${ph}`)
    await page.screenshot({ path: `${OUT}/01-suggestion-${theme}.png` })

    await page.keyboard.press('Tab')
    await page.waitForTimeout(150)
    const v = await box(page).inputValue()
    check(`02-${theme} Tab fills the draft`, v === FIRST, `value=${v}`)
    const focused = await page.evaluate(() => document.activeElement?.hasAttribute('data-composer-input') ?? false)
    check(`02-${theme} focus stays in the box`, focused)
    const caret = await box(page).evaluate(el => [el.selectionStart, el.selectionEnd])
    check(`02-${theme} caret after the text`, caret[0] === FIRST.length && caret[1] === FIRST.length, JSON.stringify(caret))
    await page.screenshot({ path: `${OUT}/02-after-tab-${theme}.png` })
    await page.context().close()
  }
  {
    const page = await newPage(theme, false)
    await box(page).focus()
    await page.keyboard.type('w')
    const ph = await box(page).getAttribute('placeholder')
    check(`03-${theme} no suggestion once typed`, !ph?.includes(FIRST), `placeholder=${ph}`)
    await page.keyboard.press('Tab')
    const v = await box(page).inputValue()
    check(`03-${theme} Tab leaves the draft alone`, v === 'w', `value=${v}`)
    await box(page).focus()
    await page.screenshot({ path: `${OUT}/03-typed-${theme}.png` })
    await page.context().close()
  }
  {
    const page = await newPage(theme, true)
    const ph = await box(page).getAttribute('placeholder')
    check(`04-${theme} no suggestion on touch`, !ph?.includes(FIRST), `placeholder=${ph}`)
    await page.screenshot({ path: `${OUT}/04-mobile-${theme}.png` })
    await page.context().close()
  }
}
await browser.close()
for (const f of ['01-suggestion', '02-after-tab', '03-typed', '04-mobile']) {
  const same = readFileSync(`${OUT}/${f}-dark.png`).equals(readFileSync(`${OUT}/${f}-light.png`))
  check(`${f} dark differs from light`, !same)
}
if (failed) { console.error('CAPTURE FAILED'); process.exit(1) }
console.log(`frames verified in ${OUT}`)
