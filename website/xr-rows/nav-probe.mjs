// DASH-16 (lead 14:41, ruling 2): open `/xr` as the room rows do, press NOTHING, and record every change of the tab's
// address with the stack that made it (`history.pushState` / `replaceState`, and each navigation of the frame), and
// what the first-run gate showed. Read only: no click, no key, no write of ours.
// usage: XR_NO_SKIP=1 node nav-probe.mjs <out.json>
import { writeFileSync } from 'node:fs'
import { open, sleep } from './dash3d-lib.mjs'

const out = process.argv[2] ?? '/tmp/nav-probe.json'
const got = { navs: [], frames: [], docs: [], console: [], skipVisible: null, end: null, threw: null }
let page = null
let browser = null
try {
  const r = await open({
    emulate: true,
    // The room's own address, returned at its canvas: none of the room's waits, so the page is kept whatever it does.
    address: '/xr?view=pages&page=%2Fchat&probe=1',
    standIn: async context => {
      context.on('page', p => {
        p.on('framenavigated', f => { if (f === p.mainFrame()) got.frames.push({ url: f.url(), at: Date.now() }) })
        p.on('request', r => { if (r.resourceType() === 'document') got.docs.push({ url: r.url(), redirectedFrom: r.redirectedFrom()?.url() ?? null, at: Date.now() }) })
        p.on('console', m => { if (got.console.length < 40) got.console.push(`${m.type()}: ${m.text().slice(0, 200)}`) })
      })
      await context.addInitScript(() => {
        window.__navLog = []
        const wrap = name => {
          const was = history[name].bind(history)
          history[name] = (state, title, url) => { window.__navLog.push({ how: name, from: location.pathname + location.search, url: String(url), body: document.body?.innerText?.slice(0, 160) ?? '', at: Math.round(performance.now()), stack: new Error().stack?.split('\n').slice(2, 14).join('\n') }); return was(state, title, url) }
        }
        wrap('pushState'); wrap('replaceState')
      })
    },
  }).catch(e => { got.threw = String(e.message); return null })
  if (r) { page = r.page; browser = r.browser }
} catch (e) { got.threw = String(e.message) }
if (page) {
  await sleep(Number(process.env.NAV_MS ?? 15000))
  got.skipVisible = await page.getByText('Skip all').first().isVisible().catch(() => null)
  got.navs = await page.evaluate(() => window.__navLog ?? [])
  got.end = page.url()
}
writeFileSync(out, JSON.stringify(got, null, 1))
console.log(JSON.stringify({ end: got.end, threw: got.threw, frames: got.frames, docs: got.docs, navs: got.navs.map(n => `${n.how} ${n.from} -> ${n.url} @${n.at}`) }))
await browser?.close()
process.exit(0)
