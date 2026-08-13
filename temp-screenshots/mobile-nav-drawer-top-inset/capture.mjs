// Captures the mobile nav drawer at 390px from a running pod, plus the drawer's
// measured inset on all four sides. Usage: node capture.mjs <baseUrl> <token> <outPng>
import { chromium } from 'playwright'

const [baseUrl, token, out] = process.argv.slice(2)
if (!baseUrl || !token || !out) throw new Error('usage: capture.mjs <baseUrl> <token> <outPng>')

const b = await chromium.launch()
const ctx = await b.newContext({
  viewport: { width: 390, height: 844 },
  deviceScaleFactor: 2,
  locale: 'en-US',
})
const p = await ctx.newPage()
await p.goto(`${baseUrl}/?token=${token}`, { waitUntil: 'domcontentloaded' })
await p.waitForSelector('[data-testid="dashboard-shell"]', { timeout: 30000 })
// Settle the shell entrance animation before touching the menu.
await p.waitForTimeout(1200)

await p.getByRole('button', { name: 'Open menu' }).click()
const nav = p.getByRole('navigation', { name: 'Main navigation' })
await nav.waitFor({ state: 'visible', timeout: 10000 })
// The drawer animates in over 250ms; measure and shoot only once it has landed.
await p.waitForTimeout(900)

const geo = await p.evaluate(() => {
  const el = document.querySelector('nav[aria-label="Main navigation"]')
  const r = el.getBoundingClientRect()
  return {
    top: Math.round(r.top),
    left: Math.round(r.left),
    right: Math.round(innerWidth - r.right),
    bottom: Math.round(innerHeight - r.bottom),
    height: Math.round(r.height),
    viewport: `${innerWidth}x${innerHeight}`,
  }
})
console.log(JSON.stringify(geo))

await p.screenshot({ path: out })
await b.close()
