// Usage: node rigbottom.mjs "$(cat /tmp/kc-pod-url.txt)"  (see rigphone.mjs)
// Bottom-park scenario: land at the bottom of a LONG, partially-loaded
// session (the default landing), never scroll, and watch the BOTTOM-most
// visible row's viewport position while idle prefetch lands pages above.
// Models the field report: parked at the bottom after sending a message,
// nothing streaming, and the view bounces by itself.
import { bootPhoneRig } from './rig-lib.mjs'
const { context, page, cdp } = await bootPhoneRig(process.argv[2])
// Do NOT scroll at all: the app lands at the bottom by itself. Install the
// watcher on the BOTTOM-most visible row (the reader's fixation point when
// parked at the bottom) and record any viewport-relative movement.
await page.evaluate(() => {
  const el = document.querySelector('.chat-container')
  const state = { events: [], anchor: null, base: 0 }
  window.__rigState = state
  const pick = () => {
    const rows = [...document.querySelectorAll('[data-display-index]')]
    const vpBottom = el.getBoundingClientRect().bottom
    for (let i = rows.length - 1; i >= 0; i--) {
      const r = rows[i].getBoundingClientRect()
      if (r.top < vpBottom - 40 && r.bottom > 100) return rows[i]
    }
    return rows[rows.length - 1] || null
  }
  state.pick = pick
  const t0 = performance.now(); window.__rigT0 = t0
  state.timer = setInterval(() => {
    const s = window.__rigState
    if (!s.anchor || !s.anchor.isConnected) {
      if (s.anchor) s.events.push({ kind: 'remount', t: Math.round(performance.now() - t0) })
      s.anchor = s.pick(); s.base = s.anchor ? s.anchor.getBoundingClientRect().top : 0
    }
    if (s.anchor) {
      const dev = s.anchor.getBoundingClientRect().top - s.base
      if (Math.abs(dev) > 24) s.events.push({ kind: 'shift', dev: Math.round(dev), t: Math.round(performance.now() - t0), st: Math.round(el.scrollTop), sh: Math.round(el.scrollHeight) })
      s.base = s.anchor.getBoundingClientRect().top
    }
  }, 120)
})
// Watch for 90s with zero interaction (idle prefetch fires after ~6s quiet).
let navDrift = null
for (let c = 0; c < 18; c++) {
  await page.waitForTimeout(5000)
  const here = page.url()
  if (!here.includes('chat-197')) { navDrift = here.slice(0, 90); break }
}
const report = await page.evaluate(() => {
  const s = window.__rigState
  clearInterval(s.timer)
  const el = document.querySelector('.chat-container')
  const events = s.events
  const shifts = events.filter(e => e.kind === 'shift')
  const remounts = events.filter(e => e.kind === 'remount').length
  const atBottom = el.scrollHeight - (el.scrollTop + el.clientHeight) < 100
  return { shifts: shifts.length, remounts, atBottom, sh: Math.round(el.scrollHeight), worst: shifts.sort((a, b) => Math.abs(b.dev) - Math.abs(a.dev)).slice(0, 6), rawShifts: events }
}).catch((e) => ({ shifts: -1, err: String(e).slice(0, 120) }))
report.navDrift = navDrift
const { rawShifts, ...summary } = report
await import('fs').then(fs => fs.promises.writeFile('/tmp/kc-rig-bottom.json', JSON.stringify({ rawShifts }, null, 1)))
console.log(JSON.stringify(summary))
await context.close()
