import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { check, openMembersDm, podInfo } from './lib/crew-pod-harness.mjs'

// Real-pod capture for #10005 PR-1: a Crew Members DM whose thread holds more
// rows than PANE_HYDRATE_LIMIT opens on the newest page with a load-earlier
// bar, and one press widens the window by a page.
const OUT = process.argv[2]
const MEMBER = process.env.MEMBER || 'default'
if (!OUT) throw new Error('usage: <outDir>')
mkdirSync(OUT, { recursive: true })
const { BASE, authed } = podInfo(readFileSync)

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2, timezoneId: 'UTC', locale: 'en' })
const page = await context.newPage()
await openMembersDm(page, authed, BASE, MEMBER)

const bar = page.getByTestId('load-earlier-messages')
await bar.waitFor({ state: 'visible', timeout: 20000 })
const rows = () => page.evaluate(() => document.querySelectorAll('[data-display-index]').length)
const scroller = page.locator('.chat-container').first()
await scroller.evaluate((el) => { el.scrollTop = 0 })
await page.waitForTimeout(500)
const before = await rows()
check('newest page is bounded (fewer display rows than the 200-row thread)', before > 0 && before <= 60, `rows=${before}`)
const f1 = join(OUT, '01-members-dm-newest-page-load-earlier.png')
await page.screenshot({ path: f1 })
console.log('wrote', f1)

await bar.click()
await page.waitForFunction((n) => document.querySelectorAll('[data-display-index]').length > n, before, { timeout: 30000 })
await page.waitForTimeout(800)
await scroller.evaluate((el) => { el.scrollTop = 0 })
await page.waitForTimeout(300)
const after = await rows()
check('one press widens the window by roughly a page', after > before, `${before} -> ${after}`)
const f2 = join(OUT, '02-members-dm-after-load-earlier.png')
await page.screenshot({ path: f2 })
console.log('wrote', f2)
console.log(JSON.stringify({ rowsBefore: before, rowsAfter: after }))
await context.close()
await browser.close()
