// Usage: node rigtop.mjs "$(cat /tmp/kc-pod-url.txt)"  (see rigphone.mjs)
// Top-park scenario: fling to the very top fast (before background prefetch
// completes), park there, and watch content-anchor integrity while the
// top-park walk lands pages above/at the reader.
import { chromium } from 'playwright'
const browser = await chromium.launch({ headless: true })
const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })
const page = await context.newPage()
await page.goto(process.argv[2], { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(9000)
const cdp = await context.newCDPSession(page)
await page.evaluate(() => { const el = document.querySelector('.chat-container'); if (el) el.scrollTop = el.scrollHeight })
await page.waitForTimeout(1500)
// Race to the top with fast flings (no reading pauses).
for (let i = 0; i < 20; i++) {
  await cdp.send('Input.synthesizeScrollGesture', { x: 195, y: 500, xDistance: 0, yDistance: 2500, speed: 8000, gestureSourceType: 'touch' })
  const atTop = await page.evaluate(() => (document.querySelector('.chat-container')?.scrollTop ?? 1) < 50)
  if (atTop) break
}
// Park at the top and watch for 40s while the walk lands pages.
const report = await page.evaluate(async () => {
  const el = document.querySelector('.chat-container')
  el.scrollTop = 0
  const events = []
  let anchor = null, base = 0, lastSt = el.scrollTop
  const pick = () => {
    const rows = [...document.querySelectorAll('[data-display-index]')]
    const vpTop = el.getBoundingClientRect().top
    let best = 1e9, a = null
    for (const r of rows) { const rect = r.getBoundingClientRect(); if (rect.bottom < vpTop) continue; const d = Math.abs(rect.top - vpTop - 100); if (d < best) { best = d; a = r } }
    return a
  }
  await new Promise(res => {
    const t0 = performance.now()
    const tick = () => {
      const st = el.scrollTop
      const scrolled = st - lastSt
      if (!anchor || !anchor.isConnected) { anchor = pick(); base = anchor ? anchor.getBoundingClientRect().top : 0 }
      else {
        const t = anchor.getBoundingClientRect().top
        const dev = (t - base) + scrolled
        if (Math.abs(dev) > 24) { events.push({ dev: Math.round(dev), st: Math.round(st), t: Math.round(performance.now() - t0) }); }
        base = t
      }
      lastSt = st
      if (performance.now() - t0 < 40000) requestAnimationFrame(tick); else res()
    }
    requestAnimationFrame(tick)
  })
  return { n: events.length, worst: events.sort((a, b) => Math.abs(b.dev) - Math.abs(a.dev)).slice(0, 5) }
})
console.log(JSON.stringify(report))
await browser.close()
