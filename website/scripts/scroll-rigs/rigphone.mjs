// Usage: node rigphone.mjs "$(cat /tmp/kc-pod-url.txt)"
//   (mint the URL first: python3 mint-pod-url.py <pod-worktree> <session-key>)
// Env: RIGPROFILE=<dir>  persistent browser profile -- models a RETURNING
//      phone whose persisted height cache is warm; omit for a first visit.
// Run from website/ or this directory (playwright resolves from website's
// node_modules).
// Phone-fidelity rig: 4x CPU throttle + Fast3G-ish network + narrow viewport.
// Scenario: race to top, park, watch the walk land pages for 60s.
import { bootPhoneRig } from './rig-lib.mjs'
// Persistent profile when RIGPROFILE is set: models a RETURNING phone (the
// persisted height cache is warm), vs the default fresh profile (first visit).
const { context, page, cdp } = await bootPhoneRig(process.argv[2])
await page.addStyleTag({ content: process.env.NOANCHOR ? '* { overflow-anchor: none !important; }' : '/* native anchoring on */' })
await page.evaluate(() => { const el = document.querySelector('.chat-container'); if (el) el.scrollTop = el.scrollHeight })
await page.waitForTimeout(1500)
for (let i = 0; i < 25; i++) {
  await cdp.send('Input.synthesizeScrollGesture', { x: 195, y: 500, xDistance: 0, yDistance: 2500, speed: 8000, gestureSourceType: 'touch' })
  const atTop = await page.evaluate(() => (document.querySelector('.chat-container')?.scrollTop ?? 1) < 50)
  if (atTop) break
}
// Install the collector once; poll it in chunks so a mid-run navigation
// surfaces as a report field instead of killing the evaluate.
await page.evaluate(() => {
  const el = document.querySelector('.chat-container')
  el.scrollTop = 0
  const state = { events: [], lastSt: el.scrollTop, anchor: null, base: 0 }
  window.__rigState = state
  const pick = () => {
    const rows = [...document.querySelectorAll('[data-display-index]')]
    const vpTop = el.getBoundingClientRect().top
    for (const r of rows) { const t = r.getBoundingClientRect().top - vpTop; if (t > -20 && t < 500) return r }
    return rows[0] || null
  }
  state.pick = pick
  const t0 = performance.now(); window.__rigT0 = t0
  state.timer = setInterval(() => {
    const s2 = window.__rigState
    if (!s2.anchor || !s2.anchor.isConnected) {
      if (s2.anchor) s2.events.push({ kind: 'remount', t: Math.round(performance.now() - t0) })
      s2.anchor = s2.pick(); s2.base = s2.anchor ? s2.anchor.getBoundingClientRect().top : 0
    }
    if (s2.anchor) {
      const dev = s2.anchor.getBoundingClientRect().top - s2.base
      if (Math.abs(dev) > 24) s2.events.push({ kind: 'shift', dev: Math.round(dev), t: Math.round(performance.now() - t0), st: Math.round(el.scrollTop), sh: Math.round(el.scrollHeight) })
      s2.base = s2.anchor.getBoundingClientRect().top
    }
  }, 120)
})
let navDrift = null
for (let c = 0; c < 12; c++) {
  await page.waitForTimeout(5000)
  const here = page.url()
  if (!here.includes('chat-197')) { navDrift = here.slice(0, 90); break }
}
const report = await page.evaluate(() => {
  const s2 = window.__rigState
  clearInterval(s2.timer)
  const el = document.querySelector('.chat-container')
  const events = s2.events
  const walkDone = !document.body.textContent.includes('Load earlier messages')
  const shifts = events.filter(e => e.kind === 'shift')
  const remounts = events.filter(e => e.kind === 'remount').length
  const dbgMap = (window.__virtDbg || {})
  const winT0 = window.__rigT0 || 0
  const evs = (window.__virtDbgEv || [])
  const bigs = shifts.filter(e => Math.abs(e.dev) > 400).map(e => {
    const near = evs.filter(v => Math.abs(v.t - (e.t + winT0)) < 800).map(v => `${v.k}${v.d !== undefined ? ':' + v.d : ''}${v.n !== undefined ? ':n' + v.n : ''}${v.stick ? ':S' : ''}`)
    return { dev: e.dev, t: e.t, near }
  })
  return { shifts: shifts.length, remounts, walkDone, dbg: dbgMap, bigs: bigs.slice(0, 8), rawEv: evs, rawShifts: events }
}).catch((e) => ({ shifts: -1, remounts: -1, walkDone: false, dbg: {}, bigs: [], rawEv: [], rawShifts: [], err: String(e).slice(0, 120) }))
report.navDrift = navDrift
const { rawEv, rawShifts, ...summary } = report
await import('fs').then(fs => fs.promises.writeFile('/tmp/kc-rig-events.json', JSON.stringify({ rawEv, rawShifts }, null, 1)))
console.log(JSON.stringify(summary))
await context.close()
