// P20b (D99 5, D100 7): `/xr?view=dash&slug=<slug>&camera=<name>`, one Bot's 3D dashboard alone in an empty room,
// shot from each of its fixed cameras, for the three layouts of DASH-2 (small, at every limit, wrong parts) through
// the samples (fixture=1). Each still is read by the view's own probe (`__xrDashView`) and by pixels (lit share of
// the dashboard's box). And a made-up slug: an empty room, no dashboard.
// usage: render-slot.sh node dash3-view.mjs [out-prefix]
import { mkdirSync } from 'node:fs'
import { open, sleep, waitUntil } from './dash3d-lib.mjs'
import { BASE } from './v3-lib.mjs'

const name = process.argv[2] ?? 'dash3-view'
const OUT = process.env.HAND_OUT ?? (process.env.XR_UI ?? '..') + '/iterations/iter-15/dash3'
mkdirSync(OUT, { recursive: true })
let bad = 0
const check = (what, ok, got) => { if (!ok) bad += 1; console.log(`${ok ? 'PASS' : 'FAIL'} ${what}: ${JSON.stringify(got).slice(0, 700)}`) }

const LAYOUTS = {
  small: { xr_layout: { v: 1, parts: [{ kind: 'ring', name: 'Checks', field: 'checks_done', max: 20 }, { kind: 'number', name: 'Open PRs', field: 'open_prs' }] }, checks_done: 7, open_prs: 3 },
  limits: {
    xr_layout: { v: 1, parts: [
      { kind: 'number', name: 'Requests per sec', field: 'rate', unit: '/s' },
      { kind: 'bar', name: 'Over budget', field: 'spend', max: 10, unit: '$' },
      { kind: 'ring', name: 'Deep', field: 'tree', path: 'a.b.c.d', max: 'goal' },
      { kind: 'lamps', name: 'Fleet', field: 'fleet', key: 'phase' },
      { kind: 'number', name: 'p99', field: 'p99', unit: 'ms' },
      { kind: 'bar', name: 'Done', field: 'done_share', max: 100, unit: '%' },
    ] },
    rate: 123456789, spend: 50, tree: { a: { b: { c: { d: 9 } } } }, goal: 19, p99: 245, done_share: 64,
    fleet: Array.from({ length: 20 }, (_v, i) => ({ phase: ['working', 'done', 'idle', 'failed', 'needs'][i % 5] })),
  },
  wrong: { xr_layout: { v: 1, parts: [
    { kind: 'number', name: 'Good', field: 'good' },
    { kind: 'gauge', name: 'Load', field: 'load' },
    { kind: 'number', name: 'Seventeen letters', field: 'good' },
    { kind: 'ring', name: 'Deep', field: 'tree', path: 'a.b.c.d.e', max: 5 },
    { kind: 'bar', name: 'No limit', field: 'good' },
  ] }, good: 4, load: 3 },
}
const CAMERAS = ['rest', 'taken', 'taken-aside']
const at = (slug, camera) => `/xr?view=dash&slug=${slug}&camera=${camera}&fixture=1&probe=1`

const got = await open({ address: at('atlas', 'rest'), viewport: { width: 1920, height: 1200 } })
const { page, errors } = got
const lit = async ndc => {
  const png = (await page.screenshot()).toString('base64')
  return page.evaluate(async ([src, b]) => {
    const img = await new Promise((done, fail) => { const i = new Image(); i.onload = () => done(i); i.onerror = fail; i.src = `data:image/png;base64,${src}` })
    const c = document.createElement('canvas'); c.width = img.naturalWidth; c.height = img.naturalHeight
    const x = c.getContext('2d', { willReadFrequently: true }); x.drawImage(img, 0, 0)
    const X = v => Math.max(0, Math.min(c.width - 1, Math.round(((v + 1) / 2) * c.width))); const Y = v => Math.max(0, Math.min(c.height - 1, Math.round(((1 - v) / 2) * c.height)))
    const x0 = X(b[0]), x1 = X(b[2]), y0 = Y(b[3]), y1 = Y(b[1])
    if (x1 <= x0 || y1 <= y0) return 0
    const d = x.getImageData(x0, y0, x1 - x0, y1 - y0).data
    let n = 0; for (let i = 0; i < d.length; i += 4) if ((d[i] + d[i + 1] + d[i + 2]) / 3 > 40) n += 1
    return +(n / (d.length / 4)).toFixed(3)
  }, [png, ndc])
}
for (const [layout, fields] of Object.entries(LAYOUTS)) {
  for (const camera of CAMERAS) {
    await page.goto(`${BASE}${at('atlas', camera)}`, { waitUntil: 'domcontentloaded', timeout: 120000 })
    await page.waitForSelector('[data-testid="xr-dash-view"] canvas', { timeout: 60000 })
    await waitUntil(async () => page.evaluate(() => !!window.__xrDash3d && !!window.__xrDashView), 30000, 200)
    await page.evaluate(([f]) => { window.__xrDash3d.hold(true); window.__xrDash3d.set('atlas', f) }, [fields])
    const seen = await waitUntil(async () => { const r = await page.evaluate(() => window.__xrDashView.read()); const d = r.dash.atlas; return d && d.parts.length === fields.xr_layout.parts.length && d.where === (camera === 'rest' ? 'rest' : 'taken') ? r : null }, 20000, 200)
    await sleep(800)
    const r = seen || (await page.evaluate(() => window.__xrDashView.read()))
    const d = r.dash.atlas
    await page.screenshot({ path: `${OUT}/${name}-${layout}-${camera}.png` })
    const share = d ? await lit(d.ndc) : 0
    console.log(`SHOT ${layout} ${camera} ${JSON.stringify({ where: d?.where, ndc: d?.ndc?.map(v => +v.toFixed(3)), parts: d?.parts.map(p => p.broken ?? p.kind), words: d?.words, lit: share })}`)
    check(`${layout}, camera ${camera}: the dashboard stands ${camera === 'rest' ? 'at rest' : 'taken'}, drawn in the still (lit pixels in its box)`, !!seen && share > 0.03, { where: d?.where, lit: share })
    if (camera === 'taken' && layout === 'wrong') check('wrong parts, taken: each socket\'s line on the lip', d?.words.why.length === 4 && d.words.why.every(w => /^layout: /.test(w)), d?.words)
    if (camera === 'taken' && layout === 'limits') check('every limit, taken: six numbers and the 16-letter name', d?.words.numbers.length === 6 && d.words.names.includes('Requests per sec'), d?.words)
  }
}
// A made-up slug: the room is empty, and the page asked for it by the room's two reads only.
const asked = []
page.on('request', q => { const u = new URL(q.url()); if (u.pathname.startsWith('/api/')) asked.push(`${q.method()} ${u.pathname}`) })
await page.goto(`${BASE}/xr?view=dash&slug=no-such-bot&camera=taken&probe=1`, { waitUntil: 'domcontentloaded', timeout: 120000 })
await page.waitForSelector('[data-testid="xr-dash-view"] canvas', { timeout: 60000 })
await sleep(4000)
const none = await page.evaluate(() => window.__xrDashView?.read() ?? null)
await page.screenshot({ path: `${OUT}/${name}-no-such-bot.png` })
const bot = asked.filter(a => a.includes('no-such-bot'))
check('a made-up slug: no dashboard, and the only asks that name it are the room\'s page read', !!none && Object.keys(none.dash).length === 0 && bot.every(a => a === 'GET /api/members/no-such-bot/dashboard'), { dash: none && Object.keys(none.dash), asked: bot, all: [...new Set(asked)].slice(0, 20) })
// A slug that is no slug: nothing is asked for it at all.
asked.length = 0
await page.goto(`${BASE}/xr?view=dash&slug=..%2Fmembers&camera=taken&probe=1`, { waitUntil: 'domcontentloaded', timeout: 120000 })
await sleep(3000)
check('a slug that is no slug names nothing in any ask', !asked.some(a => a.includes('members/..') || a.includes('%2F')), [...new Set(asked)].slice(0, 20))
console.log(`ERRORS ${JSON.stringify(errors.slice(0, 6))}`)
console.log(`TOTAL bad ${bad}`)
await got.browser.close()
process.exit(bad ? 1 : 0)
