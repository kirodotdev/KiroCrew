// Usage: node rigidle.mjs "$(cat /tmp/kc-pod-url.txt)"  (see rigphone.mjs)
// Pure-idle discriminator: warm until fully loaded+measured, then watch a
// parked viewport for 30s with NO gestures. Any jump here is NOT a landing.
import { chromium } from 'playwright'
const browser = await chromium.launch({ headless: true })
const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })
const page = await context.newPage()
await page.goto(process.argv[2], { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(9000)
await page.evaluate(() => { const el = document.querySelector('.chat-container'); if (el) el.scrollTop = el.scrollHeight })
// Warm until the walk finishes (hasMore false) + grace for the farm.
for (let i = 0; i < 40; i++) {
  await page.waitForTimeout(5000)
  const done = await page.evaluate(() => !document.body.textContent.includes('Load earlier messages'))
  if (done && i > 6) break
}
await page.waitForTimeout(10000)
const report = await page.evaluate(async () => {
  const el = document.querySelector('.chat-container')
  const events = []
  let anchor = null, base = 0, lastSt = el.scrollTop
  const pick = () => {
    const rows = [...document.querySelectorAll('[data-display-index]')]
    const mid = el.getBoundingClientRect().top + el.clientHeight / 2
    let best = 1e9, a = null
    for (const r of rows) { const rect = r.getBoundingClientRect(); const d = Math.abs((rect.top + rect.bottom) / 2 - mid); if (d < best) { best = d; a = r } }
    return a
  }
  await new Promise(res => {
    const t0 = performance.now()
    const tick = () => {
      const st = el.scrollTop
      const scrolled = st - lastSt
      if (!anchor || !anchor.isConnected) { const prev = anchor; anchor = pick(); base = anchor ? anchor.getBoundingClientRect().top : 0; if (prev) events.push({ kind: 'remount', t: Math.round(performance.now() - t0) }) }
      else {
        const t = anchor.getBoundingClientRect().top
        const dev = (t - base) + scrolled
        if (Math.abs(dev) > 24) events.push({ kind: 'shift', dev: Math.round(dev), st: Math.round(st), t: Math.round(performance.now() - t0) })
        base = t
      }
      lastSt = st
      if (performance.now() - t0 < 30000) requestAnimationFrame(tick); else res()
    }
    requestAnimationFrame(tick)
  })
  return { events: events.slice(0, 12), n: events.length }
})
console.log(JSON.stringify(report))
await browser.close()
