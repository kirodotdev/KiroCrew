// Browser half of scripts/measure_send_under_latency.py (run by it; not meant to be run by hand).
//
// One page opens a big session through the delay proxy while a SECOND page of the same browser
// context (so the same 6-connections-per-origin pool) fires composer sends. Prints one JSON
// document on stdout.
import { createRequire } from 'node:module'

const cfg = JSON.parse(process.env.MEASURE_CONFIG)
const { chromium } = createRequire(`${cfg.websiteDir}/package.json`)('@playwright/test')

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const control = (path) => fetch(`${cfg.control}${path}`).then((r) => r.json())

const browser = await chromium.launch({ headless: true })

// Wire accounting for the big slot's detail route, from the browser's own network stack.
const reqs = new Map()
async function watch(context, page) {
  const cdp = await context.newCDPSession(page)
  await cdp.send('Network.enable')
  cdp.on('Network.requestWillBeSent', (e) => {
    if (/\/api\/chat\/slots\/big\?/.test(e.request.url))
      reqs.set(e.requestId, { url: e.request.url.replace(/^https?:\/\/[^/]+/, ''), t0: Date.now(), raw: 0 })
  })
  cdp.on('Network.responseReceived', (e) => {
    const r = reqs.get(e.requestId)
    if (r) r.status = e.response.status
  })
  cdp.on('Network.dataReceived', (e) => {
    const r = reqs.get(e.requestId)
    if (r) r.raw += e.dataLength
  })
  cdp.on('Network.loadingFinished', (e) => {
    const r = reqs.get(e.requestId)
    if (r) {
      r.wire = e.encodedDataLength
      r.ms = Date.now() - r.t0
    }
  })
}
const mark = () => reqs.clear()
const snapshot = () => [...reqs.values()].filter((r) => r.wire !== undefined)

// One fresh browser context per round: an empty HTTP cache, so the open is a first visit.
async function freshContext() {
  const context = await browser.newContext({ locale: 'en-US' })
  await context.addInitScript(() => {
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-nav', '0')
  })
  const page = await context.newPage()
  await watch(context, page)
  // Warm-up at full bandwidth: cookie and SPA bundle land in the cache, with the SMALL slot
  // active, so the timed part is the big session and not the 16 MB bundle.
  // The big slot is never read during warm-up, so its open below is a first visit.
  await page.route(/\/api\/chat\/slots\/big(\?|$)/, (route) => route.abort())
  await control('/throttle?mbit=0')
  await page.goto(`${cfg.base}/?token=${encodeURIComponent(cfg.token)}`, { waitUntil: 'load', timeout: 120000 })
  await page.waitForURL((u) => !u.search.includes('token'), { timeout: 30000 }).catch(() => {})
  await sleep(1000)
  await page.goto(`${cfg.base}/chat`, { waitUntil: 'load', timeout: 120000 })
  // A first-run privacy notice covers the page a few seconds after it opens; answering it once
  // (the disposable gateway stores the choice) keeps it off the session rows.
  await page.getByRole('button', { name: 'Keep on' }).click({ timeout: 8000 }).catch(() => {})
  await page.locator('[data-slot-key="small"]').first().click({ timeout: 60000 })
  await sleep(3000)
  const sender = await context.newPage()
  await sender.goto(`${cfg.base}/icon-192.png`)
  await page.unroute(/\/api\/chat\/slots\/big(\?|$)/)
  return { context, page, sender }
}

const sends = []
async function fireSends(sender, count, gapMs) {
  for (let i = 0; i < count; i++) {
    const t0 = Date.now()
    const p = sender.evaluate(
      async ({ t0, i }) => {
        const r = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: `ping ${i}`, slot: 'small' }),
        })
        await r.text()
        return Date.now() - t0
      },
      { t0, i },
    )
    sends.push({ t0, done: p })
    await sleep(gapMs)
  }
}

const out = { rounds: [] }
for (let round = 0; round < cfg.rounds; round++) {
  const { context, page, sender } = await freshContext()
  await control('/throttle?mbit=' + cfg.mbit)
  mark()
  const tOpen = Date.now()
  const burst = fireSends(sender, cfg.sends, cfg.gapMs)
  await page.locator('[data-slot-key="big"]').first().click()
  await burst
  await Promise.all(sends.map((s) => s.done)).catch(() => {})
  await sleep(cfg.settleMs)
  const open = snapshot()

  // An unchanged refresh: leave the tab and come back, nothing new in the slot.
  await page.locator('[data-slot-key="small"]').first().click()
  await sleep(1500)
  mark()
  await page.locator('[data-slot-key="big"]').first().click()
  await sleep(cfg.refreshWaitMs)
  const unchanged = snapshot()

  // A refresh after new rows: composer sends land in the big slot from the other page, and the
  // open tab re-reads the slot when each turn ends.
  mark()
  await sender.evaluate(async (n) => {
    for (let i = 0; i < n; i++) {
      const r = await fetch('/api/chat?r=new', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: `new row ${i}`, slot: 'big' }),
      })
      await r.text()
    }
  }, cfg.newRows)
  await sleep(cfg.refreshWaitMs)
  const newRows = snapshot()
  out.rounds.push({ tOpen, open, unchanged, newRows })
  await context.close()
}
out.sends = await Promise.all(sends.map(async (s) => ({ t0: s.t0, total_ms: await s.done.catch(() => null) })))
console.log('RESULT:' + JSON.stringify(out))
await browser.close()
