/**
 * Screenshot harness for the "Connect your phone" sidebar entry + dialog.
 *
 * Runs the REAL built SPA (website/dist) behind the shared fixture stub. Three
 * shots pin the feature's three states:
 *   1. rail-row.png       — the bottom-rail entry, visible because the governed
 *                           methods endpoint returned both methods
 *   2. modal-qr.png       — the dialog with a (fixture) QR minted via the
 *                           explicit click, plus the one-time-link section
 *   3. hidden-by-policy   — methods endpoint returns an empty list → the rail
 *                           row is ABSENT (asserted, not screenshotted: absence
 *                           of a row does not photograph well)
 *
 * Usage: node scripts/capture-mobile-connect.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '/tmp/mobile-connect-shots'

mkdirSync(OUT, { recursive: true })

/* A recognisable fixture QR: deterministic checker pattern PNG, generated as a
   data URI the same way the server hands one out. Not a real credential. */
const QR_DATA_URI =
  'data:image/svg+xml;base64,' +
  Buffer.from(
    (() => {
      let cells = ''
      let seed = 42
      const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff
      const finder = (x, y) => (x < 7 && y < 7) || (x >= 18 && y < 7) || (x < 7 && y >= 18)
      for (let y = 0; y < 25; y++)
        for (let x = 0; x < 25; x++) {
          if (finder(x, y)) continue
          if (rnd() > 0.52) cells += `<rect x="${x * 7}" y="${y * 7}" width="7" height="7"/>`
        }
      const fp = (ox, oy) =>
        `<rect x="${ox * 7}" y="${oy * 7}" width="49" height="49"/>` +
        `<rect fill="#fff" x="${(ox + 1) * 7}" y="${(oy + 1) * 7}" width="35" height="35"/>` +
        `<rect x="${(ox + 2) * 7}" y="${(oy + 2) * 7}" width="21" height="21"/>`
      return `<svg xmlns="http://www.w3.org/2000/svg" width="175" height="175" fill="#000"><rect width="175" height="175" fill="#fff"/>${cells}${fp(0, 0)}${fp(18, 0)}${fp(0, 18)}</svg>`
    })(),
  ).toString('base64')

async function makePage(context, { methodsEnabled }) {
  const page = await context.newPage()
  const extra = async (path, route) => {
    if (path === '/api/mobile-connect/methods') {
      await json(
        route,
        methodsEnabled
          ? { methods: [{ id: 'tailnet_qr', kind: 'tailnet_qr' }, { id: 'login_link', kind: 'login_link' }] }
          : { methods: [] },
      )
      return true
    }
    if (path === '/api/tailnet/mobile') {
      await json(route, { step: 'ready', host: 'crew-zezhexu.tail1a2b.ts.net', published: true })
      return true
    }
    if (path === '/api/tailnet/mobile/qr') {
      await json(route, {
        url: 'https://crew-zezhexu.tail1a2b.ts.net/?token=FIXTURE',
        image: QR_DATA_URI,
        ttl_secs: 3600,
        link_window_secs: 300,
        host: 'crew-zezhexu.tail1a2b.ts.net',
      })
      return true
    }
    return false
  }
  logPageProblems(page)
  await stubDashboardApi(page, { extra })
  return page
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })

// Shot 1+2: methods available → row renders; click through to the minted QR.
const page = await makePage(context, { methodsEnabled: true })
await page.goto(base + '/capabilities', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2400)
await page.screenshot({ path: join(OUT, 'rail-row.png') })
console.log('shot rail-row.png')

await page.getByRole('button', { name: 'Connect your phone', exact: true }).click()
await page.getByRole('button', { name: 'Show QR code', exact: true }).click()
await page.waitForSelector('img[alt="QR code for mobile access"]')
await page.waitForTimeout(400)
await page.screenshot({ path: join(OUT, 'modal-qr.png') })
console.log('shot modal-qr.png')
await page.close()

// State 3: policy/edition returns no methods → the row must be ABSENT.
const page2 = await makePage(context, { methodsEnabled: false })
await page2.goto(base + '/capabilities', { waitUntil: 'domcontentloaded' })
await page2.waitForTimeout(2400)
const count = await page2.getByRole('button', { name: 'Connect your phone', exact: true }).count()
if (count !== 0) {
  console.error('FAIL: connect row rendered despite empty methods')
  process.exit(1)
}
console.log('hidden-by-policy: row absent as required')
await page2.close()

await browser.close()
srv.close()
