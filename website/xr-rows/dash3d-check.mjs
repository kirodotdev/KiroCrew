// Round 15, the 3D dashboard's first slice (DASH3D-1.md): its rows, in the real /xr room of pages of the throwaway
// dev gateway (the crew, the pages, the boards and the socket are the gateway's own), flat preview, mouse. The page's
// files are the dash3d worktree's build (dash3d-lib.mjs). No Bot of the dev gateway has a Dashboard page of its own,
// and its Bots run a scripted stand-in model, so THE TWO READS OF A BOT'S DASHBOARD ARE ANSWERED BY THE ROW (the page
// read and the whole read of the values), and a "frame" is handed to the page's own frame handlers by the probe.
// Everything after those answers is the product's code.
// Rows:
//   forms    a Bot with a layout has a rest form, a Bot without has none; a press takes it (the Bot's name, a number
//            over each part, a name under each); a second press sends it back; its colours are not the room's state
//            colours; at rest it is low and outside the ring; one page read for each Bot.
//   moves    a value that changes moves the part within 2 s of the frame; a cleared layout takes the thing away.
//   broken   a document that is no layout: a grey plate, the room goes on; taken, it says why in one line. One part
//            wrong: a socket, the others stand; taken, its line under it.
//   taps     a press on the Bot still opens its page; a press on the thing never reaches the page behind it.
//   boards   (a real ask of Mira) a press on a placard still takes the board; with a placard up the taken form
//            stands over the placards; a board that comes up lifts it; taking a board sends it back.
//   clear    from the room's middle the rest form is in front of no Bot and no page, with 1, 6 and 12 Bots.
//   gone     the gateway holds no live page (409): the page read is made again and the values come back; with no
//            frame at all the values are read again within the fallback time.
//   rate     three pages and three rest forms, a Bot that writes 10 times a second for 20 s: frame times, how many
//            whole reads and page reads went out.
// usage: render-slot.sh node dash3d-check.mjs <row>     (writes iterations/iter-15/rows/dash3d-<row>.json)
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { BASE } from './v3-lib.mjs'
import { roomOnScreen } from './touch-lib.mjs'
import { click, data, dashOf, onScreen, open, read, sleep, waitUntil } from './dash3d-lib.mjs'

/**
 * Where to click a dashboard: on its tray, low in the box the probe draws it in (the picture's own box, by the page's
 * camera). Its press is what it draws (`e7da1f2d072`, D55 5), so the box's middle can be air between parts, and the
 * room's middle by this row's own projection missed (9/15, 5/6 in w1066).
 */
const trayAt = async (page, d, up = 0.12) => {
  const rect = await page.evaluate(() => { const r = document.querySelector('[data-testid="xr-canvas"]').getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height } })
  const s = onScreen(d.press, rect)
  return { x: rect.x + s.x, y: rect.y + s.bottom - up * (s.bottom - s.top) }
}

const row = process.argv[2] ?? 'forms'
const ROOT = (process.env.XR_UI ?? '..') + '/iterations/iter-15'
const shots = `${ROOT}/dash3d`
mkdirSync(`${ROOT}/rows`, { recursive: true }); mkdirSync(shots, { recursive: true })
const out = { row, base: BASE, checks: [], notes: [] }
const check = (name, ok, value) => { out.checks.push({ name, ok: !!ok, value }); console.log(`${ok ? 'PASS' : 'FAIL'} ${name}: ${JSON.stringify(value ?? null).slice(0, 400)}`); return !!ok }
const note = (name, value) => { out.notes.push({ name, value }); console.log(`  ${name}: ${JSON.stringify(value).slice(0, 500)}`) }
const load = () => Number(readFileSync('/proc/loadavg', 'utf8').split(' ')[0])

const LAYOUT = { v: 1, parts: [
  { kind: 'ring', name: 'Aura checks', field: 'checks_done', max: 20 },
  { kind: 'bar', name: 'Spend', field: 'spend_today', max: 'spend_budget', unit: '$' },
  { kind: 'lamps', name: 'Workers', field: 'workers' },
  { kind: 'number', name: 'Open PRs', field: 'open_prs' },
] }
const VALUES = () => ({ xr_layout: structuredClone(LAYOUT), checks_done: 7, spend_today: 12, spend_budget: 40, open_prs: 3, workers: [{ state: 'working' }, { state: 'done' }, { state: 'idle' }, { state: 'failed' }, { state: 'needs' }] })

/** The Bots whose Dashboard reads the row answers: slug -> { name, dashboard, fields, gone, gets, posts }. */
const bots = new Map()
const give = (slug, name, fields = VALUES()) => bots.set(slug, { name, dashboard: `pkg-${slug}`, fields, gone: false, gets: [], posts: [] })
/** `crew`: fill the roster's answer up to so many Bots with copies of the gateway's own under new names. */
const standIn = (crew = 0) => async context => {
  if (crew) await context.route('**/v1/crew/owner/bots', async route => {
    const res = await route.fetch(); const j = await res.json().catch(() => null)
    const rows = j?.members ?? []; const members = rows.slice(0, crew)
    for (let i = 0; rows.length && members.length < crew; i += 1) { const from = rows[i % rows.length]; members.push({ ...from, name: `${from.name}${i + 2}`, slug: `${from.slug}${i + 2}`, slot_key: '', display_name: `${from.name}${i + 2}` }) }
    await route.fulfill({ response: res, json: { ...(j ?? {}), members } })
  })
  await context.route(/\/api\/members\/[^/]+\/dashboard(\/refetch)?(\?|$)/, async route => {
    const url = new URL(route.request().url())
    const [, slug, refetch] = /\/api\/members\/([^/]+)\/dashboard(\/refetch)?$/.exec(url.pathname) ?? []
    const b = bots.get(slug)
    if (!b) { if (!refetch) (plain[slug] ??= []).push(Date.now()); return route.fallback() }
    if (!refetch) {
      b.gets.push(Date.now()); b.gone = false
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ instance_version: 3, template: { id: b.dashboard, version: 3 }, dashboard: b.dashboard, html: '', manifest: {}, state: 'live', fields: Object.keys(b.fields).sort(), scripts: false, locale: 'en' }) })
    }
    b.posts.push(Date.now())
    if (b.gone) return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ error: 'no dashboard page is armed for this package', code: 'no_live_page' }) })
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ dashboard: b.dashboard, read: { fields: b.fields, agentic: ['xr_layout'], seq: b.posts.length, missing: [], written_at: {}, locale: 'en', subject: 'crewmate', last_user_at: '', version: b.posts.length, generation: 'row' } }) })
  })
}
/** The Bot wrote: its values change in the row's answer, and its frames are handed to the page as the socket hands them. */
const frame = (page, slug, also) => page.evaluate(([s, d, a]) => window.__xrDash3d.frame(s, d, a ?? undefined), [slug, bots.get(slug).dashboard, also === true ? 'value' : also ?? null])
/** Every page read (GET) of a Bot whose Dashboard the row does not answer, as the browser sent it: slug -> times. */
const plain = {}
/**
 * What a rest form is against its Bot, from the probe: how wide its tray is and how wide the body; how far under the
 * Bot's eyes (at 0.55 of its height) its highest point ends; how far over the line from the eye to the near edge
 * of the Bot's ring (0.11 m) its tray's lowest near edge is; how far in front of the feet its far edge is; and the
 * least angle between it and a neighbour's body, as the eye sees them.
 */
function restFacts(m, bot, r) {
  const eye = r.head
  const wideM = Math.hypot(m.far[0][0] - m.far[1][0], m.far[0][2] - m.far[1][2])
  const farMid = [(m.far[0][0] + m.far[1][0]) / 2, (m.far[0][2] + m.far[1][2]) / 2]
  const flat = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1])
  const feet = [bot.feet[0], bot.feet[2]]
  const e = [eye[0], eye[2]]
  const dFeet = flat(feet, e)
  const toEye = [(e[0] - feet[0]) / dFeet, (e[1] - feet[1]) / dFeet]
  const fromFeetM = (farMid[0] - feet[0]) * toEye[0] + (farMid[1] - feet[1]) * toEye[1]
  // The tray's near lower edge: its far edge, the tray's depth nearer, the tray's thickness lower.
  const front = dFeet - fromFeetM - 0.1 * m.scale
  const bottom = m.at[1] - 0.012 * m.scale
  const sight = eye[1] - ((eye[1] - bot.feet[1]) / (dFeet - 0.11)) * front
  const yawOf = p => (Math.atan2(p[0] - eye[0], -(p[1] - eye[2])) * 180) / Math.PI
  const halfTray = (Math.atan2(wideM / 2, front + 0.05 * m.scale) * 180) / Math.PI
  const gaps = r.crew.filter(b => b.slug !== bot.slug).map(b => { let d = Math.abs(yawOf([b.feet[0], b.feet[2]]) - yawOf(farMid)); if (d > 180) d = 360 - d; return d - halfTray - (Math.atan2(b.widthM / 2, flat([b.feet[0], b.feet[2]], e)) * 180) / Math.PI })
  return { wideM, bodyM: bot.widthM, underEyesM: bot.feet[1] + 0.55 * (bot.top[1] - bot.feet[1]) - m.box[1][1], overRingSightM: bottom - sight, fromFeetM, neighbourGapDeg: gaps.length ? Math.min(...gaps) : 999 }
}

let got = null
const finish = async code => {
  out.errors = (got?.errors ?? []).filter(e => !/api\/instances|sessions\/usage|Failed to load resource/.test(e)).slice(0, 10)
  writeFileSync(`${ROOT}/rows/dash3d-${row}.json`, JSON.stringify(out, null, 1))
  await got?.browser.close().catch(() => {})
  process.exit(code ?? (out.checks.every(c => c.ok) ? 0 : 1))
}
const start = async ({ crew = 0, query = '', pitch = -22 } = {}) => {
  got = await open({ standIn: standIn(crew), query })
  const { page } = got
  await waitUntil(async () => { const c = (await read(page)).crew; return c.length > 0 && c.every(b => b.drawn && b.shown) }, 90000, 400)
  await page.evaluate(() => window.__xrPages.stroll?.(false))
  await sleep(2500)
  await page.evaluate(p => window.__xrPages.look({ yawDeg: 0, pitchDeg: p, lens: 'start' }), pitch)
  await page.mouse.move(5, 5)
  await sleep(800)
  return page
}
const botOf = async (page, slug) => (await read(page)).crew.find(b => b.slug === slug)
const mid = box => [(box[0][0] + box[1][0]) / 2, (box[0][1] + box[1][1]) / 2, (box[0][2] + box[1][2]) / 2]
const face = async (page, point, pitch = -22) => { const r = await read(page); const yaw = (Math.atan2(point[0] - r.head[0], -(point[2] - r.head[2])) * 180) / Math.PI; await page.evaluate(([y, p]) => window.__xrPages.look({ yawDeg: y, pitchDeg: p, lens: 'start' }), [yaw, pitch]); await sleep(500) }
/** Press a dashboard: on the middle of its drawn parts. */
const pressDash = async (page, slug) => {
  const d = (await dashOf(page))[slug]
  const p = mid(d.box)
  // The picture is turned to it first, so that it is in view wherever it stands.
  if (d.where === 'taken') {
    // A level picture toward it: the form is placed for a level head (D71), and a picture still pitched down at the
    // rest tray cut it at its top (w1098, w1103 `forms-taken.png`). Its press box must then lie in the picture.
    const r = await read(page)
    const yaw = (Math.atan2(p[0] - r.head[0], -(p[2] - r.head[2])) * 180) / Math.PI
    let seen = null
    for (const y of [r.turn?.yawDeg, yaw, -yaw]) {
      if (typeof y !== 'number') continue
      await page.evaluate(v => window.__xrPages.look({ yawDeg: v, pitchDeg: 0 }), y); await sleep(500)
      const n = (await dashOf(page))[slug]?.press
      if (n && n[0] > -1 && n[1] > -1 && n[2] < 1 && n[3] < 1) { seen = { yawDeg: +y.toFixed(1), press: n.map(v => +v.toFixed(3)) }; break }
    }
    note('the taken form in a level picture', seen)
  } else {
    const r = await read(page)
    const down = (Math.atan2(r.head[1] - p[1], Math.hypot(p[0] - r.head[0], p[2] - r.head[2])) * 180) / Math.PI
    await face(page, p, -Math.max(0, Math.min(40, down - 4)))
  }
  // Low in its box first (the tray); when the form did not change hands, its box's middle, then a third of the way up
  // (a grey plate has no parts and its box is the plate alone: w1103 broken's low click met no plate).
  const was = d.where
  let at = null
  for (const up of [0.12, 0.5, 0.3]) {
    at = await trayAt(page, (await dashOf(page))[slug], up)
    await click(page, at.x, at.y)
    await sleep(900)
    if ((await dashOf(page))[slug]?.where !== was) break
    note('the press did not change hands; again higher in its box', { up, at })
  }
  return at
}
/**
 * A taken form's top and foot as the eye sees them at a level head, against the lens less `TAKEN_LOOK_DEG` (9) at each
 * end: `fovDeg` is the page's lens, tall; the Aura's 40.7 when the probe has none.
 */
const spare = (m, head, fovDeg = 40.7) => {
  const far = Math.hypot(m.at[0] - head[0], m.at[2] - head[2])
  const deg = y => (Math.atan2(y - head[1], far) * 180) / Math.PI
  const half = (fovDeg ?? 40.7) / 2 - 9
  const topDeg = deg(m.box[1][1]), footDeg = deg(m.box[0][1])
  return { ok: topDeg <= half + 0.5 && footDeg >= -half - 0.5, topDeg: +topDeg.toFixed(2), footDeg: +footDeg.toFixed(2), halfDeg: +half.toFixed(2), farM: +far.toFixed(3) }
}
const hueOf = gl => { const r = ((gl >> 16) & 255) / 255, g = ((gl >> 8) & 255) / 255, b = (gl & 255) / 255; const hi = Math.max(r, g, b), lo = Math.min(r, g, b), d = hi - lo; if (!d) return null; const h = hi === r ? ((g - b) / d) % 6 : hi === g ? (b - r) / d + 2 : (r - g) / d + 4; return (h * 60 + 360) % 360 }
const apart = (a, b) => { const d = Math.abs(a - b) % 360; return Math.min(d, 360 - d) }
const overlap = (a, b) => a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3]
const frames = async (page, ms) => {
  await page.evaluate(() => { const w = window; w.__ft = []; let last = performance.now(); const t = n => { w.__ft?.push(n - last); last = n; if (w.__ft) requestAnimationFrame(t) }; requestAnimationFrame(t) })
  await sleep(ms)
  const s = (await page.evaluate(() => { const f = window.__ft; window.__ft = null; return f })).sort((a, b) => a - b)
  return { frames: s.length, medianMs: +s[Math.floor(s.length / 2)].toFixed(2), p95Ms: +s[Math.floor(s.length * 0.95)].toFixed(2), perSecond: +(s.length / (ms / 1000)).toFixed(1) }
}

if (row === 'forms') {
  give('mira', 'Mira')
  const page = await start()
  const d0 = await waitUntil(async () => { const d = await dashOf(page); return d.mira ? d : false }, 20000, 300)
  check('Mira, whose page declares the layout, has a rest form', !!d0 && d0.mira.where === 'rest', d0 ? { where: d0.mira.where, parts: d0.mira.parts.map(p => p.kind) } : null)
  // The gateway the row stands on has Bots of its own whose real pages declare a layout (taps-xr, w1098): the row
  // answers for the Bots it gave a page to, and among those only Mira's declares one.
  check('no other Bot the row gave a page has one', !!d0 && Object.keys(d0).filter(s => bots.has(s)).join() === 'mira', d0 ? { all: Object.keys(d0), ours: Object.keys(d0).filter(s => bots.has(s)) } : null)
  const mira = await botOf(page, 'mira')
  await face(page, mira.feet)
  await page.screenshot({ path: `${shots}/forms-rest.png` })
  const rest = (await dashOf(page)).mira
  check('at rest it has no words', rest.words.header === '' && rest.words.numbers.length === 0 && rest.words.names.length === 0, rest.words)
  // At rest its Bot holds it in front of its own body: as wide as the body at most, under the eyes, over the ring's sight.
  const f = restFacts(rest, mira, await read(page))
  check('at rest it is no wider than its Bot\'s body', f.wideM <= f.bodyM + 1e-6 && f.wideM >= 0.12, { wideM: +f.wideM.toFixed(3), bodyM: +f.bodyM.toFixed(3), scale: +rest.scale.toFixed(3) })
  check('at rest it ends under its Bot\'s eyes', f.underEyesM > 0, { underEyesM: +f.underEyesM.toFixed(3) })
  check('at rest the ring at the Bot\'s feet is seen under it: the line from the eye to the ring\'s near edge passes under the tray', f.overRingSightM > 0, { overRingSightM: +f.overRingSightM.toFixed(3) })
  check('at rest it stands by its Bot: its far edge is within 12 cm of the feet, in front of them', f.fromFeetM > 0.05 && f.fromFeetM < 0.12, { fromFeetM: +f.fromFeetM.toFixed(3) })
  // Must 1: the parts' colours against the colours of the Bots' states, read off the page's own theme tokens.
  const state = await page.evaluate(() => { const probe = document.createElement('span'); document.body.appendChild(probe); const read = v => { probe.style.color = `var(${v})`; const m = /(\d+)[, ]+(\d+)[, ]+(\d+)/.exec(getComputedStyle(probe).color); return m ? (Number(m[1]) << 16) | (Number(m[2]) << 8) | Number(m[3]) : null }; const o = { accent: read('--accent'), warn: read('--warn'), danger: read('--danger') }; probe.remove(); return o })
  const mine = Object.values(rest.hues)
  const gaps = mine.flatMap(c => Object.values(state).filter(s => s !== null && hueOf(s) !== null).map(s => Math.round(apart(hueOf(c), hueOf(s)))))
  check('no part colour is a state colour of the room, and each stands at least 33 degrees of hue from all three', mine.every(c => !Object.values(state).includes(c)) && gaps.every(g => g >= 33), { parts: mine.map(c => c.toString(16)), state: Object.fromEntries(Object.entries(state).map(([k, v]) => [k, v?.toString(16)])), leastGapDeg: Math.min(...gaps) })
  check('a lamp of a thing that "needs" is grey, like any word outside the list', rest.parts[2].lamps.join() === 'working,done,idle,failed,other', rest.parts[2].lamps)
  // Must 5: one page read for each Bot.
  note('page reads and whole reads of Mira so far', { gets: bots.get('mira').gets.length, posts: bots.get('mira').posts.length })
  // Two queries of one Bot would start their reads together. The dashboard's own frames ask for the one read again
  // (one at a time), so more than one read over the seconds is the product's refresh, not a second query.
  const g = bots.get('mira').gets
  const pairGap = Math.min(Infinity, ...g.slice(1).map((t, i) => t - g[i]))
  check('the page reads of Mira never start in pairs: there is one query for a Bot', g.length >= 1 && pairGap >= 150, { reads: g.length, leastGapMs: pairGap === Infinity ? null : pairGap })
  await pressDash(page, 'mira')
  const taken = (await dashOf(page)).mira
  check('a press takes it: it stands 0.8 m ahead, within 5 cm (Q8, M71)', taken.where === 'taken' && Math.abs(Math.hypot(taken.at[0], taken.at[2]) - 0.8) <= 0.05, { where: taken.where, aheadM: +Math.hypot(taken.at[0], taken.at[2]).toFixed(3), y: +taken.at[1].toFixed(3) })
  check('taken, the lip says whose it is', taken.words.header === 'Mira\'s numbers', taken.words.header)
  check('taken, under each part, on the lip, is its number: one rule for all four', taken.words.numbers.join('|') === '7/20|12 $|5|3', taken.words.numbers)
  check('taken, and under the number its name', taken.words.names.join('|') === 'Aura checks|Spend|Workers|Open PRs', taken.words.names)
  const head = (await read(page)).head
  // D103 2: placed for his head as it is at the take (the picture pitched down at the tray), whole in the picture then.
  check('taken, it stands whole in the picture at the take (D103 2)', !!taken.ndc && taken.ndc[0] > -1 && taken.ndc[1] > -1 && taken.ndc[2] < 1 && taken.ndc[3] < 1, { ndc: taken.ndc, level: spare(taken, head, (await read(page)).view?.fovDeg) })
  {
    // What it covers, as the eye sees it: the band it takes, and each Bot whose eyes lie in that band.
    const r = await read(page)
    const ang = p => ({ yaw: (Math.atan2(p[0] - r.head[0], -(p[2] - r.head[2])) * 180) / Math.PI, down: (Math.atan2(r.head[1] - p[1], Math.hypot(p[0] - r.head[0], p[2] - r.head[2])) * 180) / Math.PI })
    const corners = []; for (const x of [0, 1]) for (const y of [0, 1]) for (const z of [0, 1]) corners.push(ang([taken.box[x][0], taken.box[y][1], taken.box[z][2]]))
    const band = { yaw: [Math.min(...corners.map(c => c.yaw)), Math.max(...corners.map(c => c.yaw))], down: [Math.min(...corners.map(c => c.down)), Math.max(...corners.map(c => c.down))] }
    const eyes = r.crew.map(b => ({ slug: b.slug, ...ang([b.feet[0], b.feet[1] + 0.55 * (b.top[1] - b.feet[1]), b.feet[2]]) }))
    const feet = r.crew.map(b => ({ slug: b.slug, ...ang(b.feet) }))
    const inBand = e => e.yaw >= band.yaw[0] && e.yaw <= band.yaw[1] && e.down >= band.down[0] && e.down <= band.down[1]
    note('what the taken form covers: its band in degrees (yaw, down), and the Bots whose eyes or feet lie in it', { band: { yaw: band.yaw.map(v => +v.toFixed(1)), down: band.down.map(v => +v.toFixed(1)) }, eyesIn: eyes.filter(inBand).map(e => e.slug), feetIn: feet.filter(inBand).map(e => e.slug), eyesDown: eyes.map(e => [e.slug, +e.yaw.toFixed(1), +e.down.toFixed(1)]) })
  }
  check('taken, every word is on the lip: nothing is written higher than the tray\'s top', taken.wordsTopY <= taken.at[1] + 0.001, { wordsTopY: +taken.wordsTopY.toFixed(3), trayTopY: +taken.at[1].toFixed(3) })
  await page.evaluate(() => window.__xrPages.look({ yawDeg: 0, pitchDeg: -30, lens: 'start' }))
  await sleep(600)
  await page.screenshot({ path: `${shots}/forms-taken.png` })
  await pressDash(page, 'mira')
  const back = (await dashOf(page)).mira
  check('a second press sends it back to the Bot', back.where === 'rest' && back.words.header === '', { where: back.where })
  await finish()
}

if (row === 'moves') {
  give('mira', 'Mira')
  const page = await start()
  await waitUntil(async () => (await dashOf(page)).mira, 20000, 300)
  const before = (await dashOf(page)).mira.parts[0]
  bots.get('mira').fields.checks_done = 15
  bots.get('mira').fields.workers = [{ state: 'done' }, { state: 'done' }]
  const t0 = Date.now()
  await frame(page, 'mira')
  const after = await waitUntil(async () => { const p = (await dashOf(page)).mira?.parts; return p && p[0].number === 15 ? p : false }, 6000, 50)
  const ms = Date.now() - t0
  check('a value that changed is drawn within 2 s of the frame', !!after && ms <= 2000 + 300, { before: before.number, after: after ? after[0].number : null, ms, lamps: after ? after[2].lamps : null })
  check('the share and the lamps moved with it', !!after && Math.abs(after[0].share - 0.75) < 1e-9 && after[2].lamps.join() === 'done,done', after ? { share: after[0].share, lamps: after[2].lamps } : null)
  await pressDash(page, 'mira')
  bots.get('mira').fields.open_prs = 1234567
  await frame(page, 'mira')
  const big = await waitUntil(async () => { const w = (await dashOf(page)).mira?.words.numbers; return w && w[3] !== '3' ? w : false }, 6000, 50)
  check('taken, the number over a part changes too, and is at most 6 characters', !!big && big[3].length <= 6, big)
  await page.evaluate(() => window.__xrPages.look({ yawDeg: 0, pitchDeg: -18, lens: 'start' })); await sleep(500)
  await page.screenshot({ path: `${shots}/moves-taken.png` })
  // The Bot takes it away: a clear writes an empty record.
  bots.get('mira').fields.xr_layout = {}
  await frame(page, 'mira')
  const gone = await waitUntil(async () => !(await dashOf(page)).mira, 6000, 50)
  check('a cleared layout takes the thing away, taken or not', gone, Object.keys(await dashOf(page)))
  await finish()
}

if (row === 'broken') {
  give('mira', 'Mira', { ...VALUES(), xr_layout: { v: 1, parts: Array.from({ length: 7 }, () => LAYOUT.parts[0]) } })
  give('juno', 'Juno', { ...VALUES(), xr_layout: { v: 1, parts: [LAYOUT.parts[3], { kind: 'gauge', name: 'Load', field: 'load' }, { kind: 'ring', name: '<img src=x onerror=alert(1)>', field: 'checks_done', max: 20 }, { ...LAYOUT.parts[0], name: 'Checks' }] } })
  const page = await start()
  const d = await waitUntil(async () => { const x = await dashOf(page); return x.mira && x.juno ? x : false }, 20000, 300)
  check('a document that is no layout still stands as a plate, and says why to the probe', !!d && d.mira.why === 'too_many' && d.mira.parts.length === 0, d ? { why: d.mira.why } : null)
  const r = await read(page)
  check('the room goes on: the Bots are drawn and the first page stands', r.crew.every(b => b.drawn) && r.panels.length >= 1 && got.errors.every(e => !/pageerror/.test(e)), { bots: r.crew.length, panels: r.panels.length, pageErrors: got.errors.filter(e => /pageerror/.test(e)) })
  const mira = await botOf(page, 'mira'); await face(page, mira.feet)
  await page.screenshot({ path: `${shots}/broken-rest.png` })
  await pressDash(page, 'mira')
  const t = (await dashOf(page)).mira
  check('taken, the grey plate says why in one short line, under the Bot\'s name', t.where === 'taken' && t.words.why.join() === 'layout: too many parts' && t.words.header === 'Mira\'s numbers', t.words)
  await page.evaluate(() => window.__xrPages.look({ yawDeg: 0, pitchDeg: -18, lens: 'start' })); await sleep(500)
  await page.screenshot({ path: `${shots}/broken-taken.png` })
  await pressDash(page, 'mira')
  check('one part wrong: it is a socket, and the others stand', d.juno.why === '' && d.juno.parts.map(p => p.kind).join() === 'number,socket,socket,ring', d.juno.parts.map(p => p.kind))
  await pressDash(page, 'juno')
  const j = (await dashOf(page)).juno
  check('taken, each socket has its line under it, and the good parts their names', j.words.why.join('|') === 'layout: unknown part|layout: bad name' && j.words.names.join('|') === 'Open PRs|Checks', j.words)
  check('markup in a name is never run: the page made no dialog and no image request', !got.errors.some(e => /onerror|alert/.test(e)) && (await page.evaluate(() => !document.querySelector('img[src="x"]'))), true)
  await page.screenshot({ path: `${shots}/broken-socket-taken.png` })
  await finish()
}

if (row === 'taps') {
  give('mira', 'Mira')
  const page = await start()
  await waitUntil(async () => (await dashOf(page)).mira, 20000, 300)
  const pagesNow = async () => Object.values((await read(page)).sources).map(s => s.path).filter(Boolean)
  const mira = await botOf(page, 'mira')
  await face(page, mira.feet)
  const m = await botOf(page, 'mira')
  // The Bot holds its tray in front of its lower body: the press is on its face, at eight tenths of its height.
  const body = await roomOnScreen(page, [m.feet[0], m.feet[1] + 0.8 * (m.top[1] - m.feet[1]), m.feet[2]])
  const taps0 = m.taps
  await click(page, body.x, body.y)
  const opened = await waitUntil(async () => (await pagesNow()).some(p => /\/members/.test(p)), 15000, 200)
  check('a press on the Bot still opens its page', opened && (await botOf(page, 'mira')).taps === taps0 + 1 && (await dashOf(page)).mira.where === 'rest', { pages: await pagesNow(), taps: (await botOf(page, 'mira')).taps - taps0 })
  // A press on the thing at rest: it is taken, and nothing else in the room was pressed.
  await face(page, (await botOf(page, 'mira')).feet)
  const before = { pages: await pagesNow(), taps: (await botOf(page, 'mira')).taps }
  await pressDash(page, 'mira')
  check('a press on the rest form takes it and presses nothing else', (await dashOf(page)).mira.where === 'taken' && (await botOf(page, 'mira')).taps === before.taps && (await pagesNow()).join() === before.pages.join(), { where: (await dashOf(page)).mira.where })
  // A page stood right behind the taken form, 1.75 m away: a press on the form must not reach it.
  const t = (await dashOf(page)).mira
  const r0 = await read(page)
  const p = mid(t.box)
  const down = (Math.atan2(r0.head[1] - p[1], 1.0) * 180) / Math.PI
  await page.evaluate(([y, d]) => window.__xrPages.open({ id: 'behind', live: '/schedule', yawDeg: y, pitchDeg: -d }), [(Math.atan2(p[0] - r0.head[0], -(p[2] - r0.head[2])) * 180) / Math.PI, down])
  await waitUntil(async () => (await read(page)).sources.behind?.status === 'live', 30000, 300)
  await sleep(1500)
  await face(page, p, -Math.max(0, down - 4))
  await page.screenshot({ path: `${shots}/taps-behind.png` })
  // The page behind does take a press beside the form (so the row would see one that reached it).
  const panel = (await read(page)).panels.find(x => x.id === 'behind')
  // A point of the page over the Bots' heads: the Bots stand in front of its lower part.
  const beside = await roomOnScreen(page, [panel.position[0], panel.position[1] + 0.3, panel.position[2]])
  got.sent.length = 0
  await click(page, beside.x, beside.y)
  await sleep(1200)
  const control = got.sent.filter(e => e.panel === 'behind' || true).length
  check('the page behind takes a press over the form (the row can see a press that reaches a page)', control > 0, { sent: got.sent.slice(0, 6) })
  // The pointer rests on the form for a while, moving a little: the page behind gets no move of it.
  const at = await trayAt(page, (await dashOf(page)).mira)
  await page.mouse.move(at.x, at.y); await sleep(500)
  got.sent.length = 0
  for (const dx of [3, -3, 5, 0]) { await page.mouse.move(at.x + dx, at.y); await sleep(200) }
  check('a pointer that lies on the taken form sends the page behind nothing', got.sent.length === 0, { sent: got.sent.slice(0, 4) })
  await click(page, at.x, at.y)
  const pressed = got.sent.filter(e => e.action !== 'move' && e.type !== 'leave')
  await sleep(1200)
  check('a press on the taken form sends it back and never reaches the page behind it', (await dashOf(page)).mira.where === 'rest' && pressed.length === 0, { where: (await dashOf(page)).mira.where, pressesSentToPages: pressed.length, sent: pressed.slice(0, 4) })
  await finish()
}

if (row === 'boards') {
  const { dashboard } = await import('./ask-lib.mjs')
  give('mira', 'Juno')
  // Trays at other Bots too: the far case takes the one farthest from Juno.
  for (const [slug, name] of [['pico', 'Pico'], ['otto', 'Otto'], ['atlas', 'Atlas']]) give(slug, name)
  const dash = await dashboard()
  await dash.ready('Juno'); await dash.settle('Juno').catch(() => {})
  const page = await start()
  await waitUntil(async () => (await dashOf(page)).mira, 20000, 300)
  const boards = async () => (await read(page)).boards.boards
  const marks = async () => (await read(page)).boardMarks
  // Taken with no board up: at its usual place.
  await pressDash(page, 'mira')
  const usual = (await dashOf(page)).mira
  const head = (await read(page)).head
  const fov = (await read(page)).view?.fovDeg
  check('with no board up the taken form stands whole in the picture at the take (D103 2)', usual.where === 'taken' && !!usual.ndc && usual.ndc[0] > -1 && usual.ndc[1] > -1 && usual.ndc[2] < 1 && usual.ndc[3] < 1, { ndc: usual.ndc, level: spare(usual, head, fov) })
  // A board comes up while it is taken.
  await dash.ask('Juno')
  const up = await waitUntil(async () => (await boards()).find(b => b.where === 'bot'), 60000, 300)
  check('Juno holds a board up (a real ask)', !!up, up ? { bot: up.bot, command: up.command } : null)
  const wentBack = await waitUntil(async () => (await dashOf(page)).mira?.where === 'rest', 5000, 100)
  check('a board that comes up sends the taken form back to its Bot', wentBack, { where: (await dashOf(page)).mira?.where })
  await sleep(1200)
  const noteNow = async () => (await page.locator('[data-testid="xr-note"]').innerText().catch(() => '')).trim()
  const saidBefore = await noteNow()
  await pressDash(page, 'mira')
  check('a board held up at a Bot does not stop a take: a press on the rest form takes it, and no line is said', (await dashOf(page)).mira.where === 'taken' && !/first/i.test(await noteNow()), { where: (await dashOf(page)).mira.where, before: saidBefore, said: await noteNow() })
  // A board that comes up at a Bot far to the side of the taken form, whose picture does not meet the form's: the
  // form stays. The form taken is the dashboard of the Bot farthest from Juno; Juno's board comes up anew.
  {
    await dash.settle('Juno').catch(() => {})
    await waitUntil(async () => !(await boards()).some(b => b.bot === up.bot), 20000, 300)
    const r = await read(page)
    const yawOf = p => (Math.atan2(p[0] - r.head[0], -(p[2] - r.head[2])) * 180) / Math.PI
    const juno = r.crew.find(b => b.slug === up.bot)
    const d = await dashOf(page)
    const far = Object.keys(d).filter(slug => slug !== 'mira').map(slug => ({ slug, apart: Math.abs(yawOf(mid(d[slug].box)) - yawOf(juno.feet)) })).sort((x, y) => y.apart - x.apart)[0]
    const gone = !(await boards()).some(b => b.bot === up.bot)
    // Mira's taken form goes back first: it stands in front of the far one's place, and a press on it sends it back.
    if ((await dashOf(page)).mira?.where === 'taken') await pressDash(page, 'mira')
    const at = await pressDash(page, far.slug)
    const taken = (await dashOf(page))[far.slug]?.where === 'taken'
    // The look turns on, away from Juno, so far that Juno's place leaves the taken form's picture: at 0.7 m the form
    // covers the middle of the view, and any Bot inside it is in front of it.
    const turn = (await read(page)).turn
    const away = Math.sign(yawOf(mid(d[far.slug].box)) - yawOf(juno.feet)) || 1
    await page.evaluate(([y, p]) => window.__xrPages.look({ yawDeg: y, pitchDeg: p, lens: 'start' }), [turn.yawDeg + away * 25, turn.pitchDeg])
    await sleep(800)
    note('the look turned away from Juno', { before: turn, after: (await read(page)).turn, away, tray: (await dashOf(page))[far.slug]?.ndc })
    note('the press on the far form', { gone, at, where: (await dashOf(page))[far.slug]?.where, ndc: (await dashOf(page))[far.slug]?.ndc, lit: (await dashOf(page))[far.slug]?.lit, boards: (await boards()).map(b => `${b.bot}:${b.where}`), on: (await read(page)).pinch?.head?.on ?? null })
    await dash.ask('Juno')
    const again = await waitUntil(async () => (await boards()).find(b => b.where === 'bot' && b.bot === up.bot), 60000, 300)
    await sleep(2500)
    const m = again ? (await marks())[again.needId] : null
    const now = (await dashOf(page))[far.slug]
    check(`a board that comes up at Juno, ${far.apart.toFixed(0)} degrees from ${far.slug}'s taken form and outside its picture, leaves that form taken`, taken && !!again && now?.where === 'taken', { taken, board: again?.needId ?? null, sentBack: await page.evaluate(() => window.__xrDashWork?.sentBack ?? null), where: now?.where, placard: m?.ndc ?? null, tray: now?.ndc })
    // Mira's form is taken again for the rows below; Juno's board, up already, does not send it back.
    await pressDash(page, 'mira')
  }
  // The card's keyboard comes up: a taken tray goes back; with it out, no tray lights and a press says why.
  const { cardOut } = await import('./chatcard-lib.mjs')
  const card = await cardOut(page).catch(() => null)
  // The flat page has Chromium's recognizer, which hears nothing here: the keyboard comes after about 8 s of listening.
  const keys = await waitUntil(async () => (await read(page)).card?.keys, 14000, 100)
  check('the card\'s keyboard is out', !!keys, { card: !!card?.card, mic: (await read(page)).card?.card?.mic ?? null })
  check('the card\'s keyboard coming up sends a taken tray back', !!(await waitUntil(async () => (await dashOf(page)).mira?.where === 'rest', 5000, 100)), { where: (await dashOf(page)).mira?.where })
  {
    const d = (await dashOf(page)).mira
    const at = await trayAt(page, d)
    await page.mouse.move(at.x, at.y, { steps: 4 }); await sleep(400)
    check('with the keyboard out, the tray does not light under the pointer', (await dashOf(page)).mira.lit === false, { lit: (await dashOf(page)).mira.lit })
    await pressDash(page, 'mira')
    const said = await noteNow()
    check('with the keyboard out, a press takes nothing and one line says to put the keyboard away', (await dashOf(page)).mira.where === 'rest' && /keyboard away/i.test(said), { where: (await dashOf(page)).mira.where, said })
  }
  // The card goes (its close: Escape is not one; the probe closes it as its close mark would).
  await page.evaluate(() => window.__xrPages.cardClose())
  await sleep(600)
  await page.evaluate(() => window.__xrPages.look({ yawDeg: 0, pitchDeg: -10, lens: 'start' })); await sleep(700)
  const bm = await marks()
  const placards = Object.entries(bm).map(([id, m]) => ({ id, ndc: m.ndc.body, sizeM: m.sizeM }))
  await page.screenshot({ path: `${shots}/boards-raised.png` })
  // A press on the placard still takes the board, and taking a board sends the dashboard back.
  const id = Object.keys(bm)[0]
  const at = await roomOnScreen(page, bm[id].body)
  await click(page, at.x, at.y)
  const held = await waitUntil(async () => (await boards()).find(b => b.where === 'held'), 8000, 150)
  check('a press on a placard still takes the board', !!held, held ? { where: held.where } : await boards())
  await sleep(600)
  {
    const d = (await dashOf(page)).mira
    const at = await trayAt(page, d)
    await page.mouse.move(at.x, at.y, { steps: 4 }); await sleep(400)
    check('while a board is in his hand, the tray does not light under the pointer', (await dashOf(page)).mira.lit === false, { lit: (await dashOf(page)).mira.lit })
  }
  await pressDash(page, 'mira')
  const saidHeld = await noteNow()
  check('while a board is in his hand, a press takes nothing and one line says to answer the board first', (await dashOf(page)).mira.where === 'rest' && /answer the board/i.test(saidHeld), { where: (await dashOf(page)).mira.where, said: saidHeld })
  await page.screenshot({ path: `${shots}/boards-held.png` })
  note('placards', placards)
  await dash.settle('Juno').catch(() => {})
  await dash.close().catch(() => {})
  await finish()
}

if (row === 'clear') {
  const results = []
  for (const crew of [1, 6, 12]) {
    bots.clear()
    got = await open({ standIn: async context => {
      // Every Bot of the room has a dashboard: the row answers for whichever slug is asked.
      await standIn(crew)(context)
      // Added last, so it is asked first: it names the Bot and hands the request on to the answer above.
      await context.route(/\/api\/members\/[^/]+\/dashboard(\/refetch)?(\?|$)/, async route => {
        const url = new URL(route.request().url())
        const [, slug] = /\/api\/members\/([^/]+)\/dashboard/.exec(url.pathname) ?? []
        if (!bots.has(slug)) give(slug, url.searchParams.get('member') ?? slug)
        return route.fallback()
      })
    } })
    const page = got.page
    await waitUntil(async () => { const c = (await read(page)).crew; return c.length === crew && c.every(b => b.drawn && b.shown) }, 90000, 400)
    await page.evaluate(() => window.__xrPages.stroll?.(false)); await sleep(2500)
    await page.evaluate(() => window.__xrPages.look({ yawDeg: 0, pitchDeg: -22, lens: 'start' })); await sleep(800)
    const d = await waitUntil(async () => { const x = await dashOf(page); return Object.keys(x).length === crew ? x : false }, 30000, 400)
    const r = await read(page)
    // As the eye at the room's middle sees it: the angle down to a point, and its angle to the right.
    const eye = r.head
    const ang = p => ({ yaw: (Math.atan2(p[0] - eye[0], -(p[2] - eye[2])) * 180) / Math.PI, down: (Math.atan2(eye[1] - p[1], Math.hypot(p[0] - eye[0], p[2] - eye[2])) * 180) / Math.PI })
    let worstPage = Infinity
    const lowestPage = Math.max(...r.panels.map(p => { const half = (p.size?.[1] ?? p.heightM ?? 1.09) / 2; return ang([p.position[0], p.position[1] - half, p.position[2]]).down }))
    const facts = Object.entries(d ?? {}).map(([slug, m]) => restFacts(m, r.crew.find(b => b.slug === slug), r))
    for (const m of Object.values(d ?? {})) worstPage = Math.min(worstPage, ang([mid(m.box)[0], m.box[1][1], mid(m.box)[2]]).down - lowestPage)
    const least = k => +Math.min(...facts.map(f => f[k])).toFixed(3)
    await page.screenshot({ path: `${shots}/clear-${crew}.png` })
    results.push({ crew, forms: facts.length, widestM: +Math.max(...facts.map(f => f.wideM)).toFixed(3), leastUnderEyesM: least('underEyesM'), leastOverRingSightM: least('overRingSightM'), leastNeighbourGapDeg: crew > 1 ? least('neighbourGapDeg') : null, underLowestPageDeg: +worstPage.toFixed(2) })
    check(`${crew} Bots: every rest form is no wider than its Bot's body`, !!d && facts.every(f => f.wideM <= f.bodyM + 1e-6), results.at(-1))
    check(`${crew} Bots: every rest form ends under its Bot's eyes`, !!d && facts.every(f => f.underEyesM > 0), { m: least('underEyesM') })
    check(`${crew} Bots: the line from the eye to the near edge of every Bot's ring passes under its tray`, !!d && facts.every(f => f.overRingSightM > 0), { m: least('overRingSightM') })
    check(`${crew} Bots: no rest form is in front of a neighbour's body, as the eye sees them`, !!d && (crew === 1 || facts.every(f => f.neighbourGapDeg > 0)), { deg: crew > 1 ? least('neighbourGapDeg') : null })
    check(`${crew} Bots: every rest form is in front of no page`, !!d && worstPage > 0, { deg: +worstPage.toFixed(2) })
    await got.browser.close().catch(() => {})
  }
  note('by crew', results)
  got = null
  await finish()
}

if (row === 'gone') {
  // Pico: a Bot that no other row on this gateway talks to, so every read here is this row's own.
  give('pico', 'Pico')
  const page = await start()
  await waitUntil(async () => (await dashOf(page)).pico, 20000, 300)
  const b = bots.get('pico')
  const gets0 = b.gets.length
  // The gateway put the live page out: the next whole read answers 409 until a page read arms one again.
  b.gone = true; b.fields.checks_done = 11
  await frame(page, 'pico')
  const back = await waitUntil(async () => (await dashOf(page)).pico?.parts[0].number === 11, 12000, 100)
  const d = await data(page)
  check('after a 409 the page read is made again, once, and the values come back', back && b.gets.length === gets0 + 1, { gets: b.gets.length - gets0, last: d.reads.pico.last, stale: d.reads.pico.stale, number: (await dashOf(page)).pico?.parts[0].number })
  // A gateway that keeps answering 409 is not asked in a loop.
  const g1 = b.gets.length; const p1 = b.posts.length
  const always = setInterval(() => { b.gone = true }, 50)
  b.gone = true
  await frame(page, 'pico')
  await sleep(9000)
  clearInterval(always)
  const d2 = await data(page)
  check('a gateway that keeps answering 409 gets one more page read and no loop; the values are marked old', b.gets.length - g1 <= 1 && b.posts.length - p1 <= 3 && d2.reads.pico.stale === true, { gets: b.gets.length - g1, posts: b.posts.length - p1, stale: d2.reads.pico.stale, last: d2.reads.pico.last })
  await page.screenshot({ path: `${shots}/gone-stale.png` })
  // No frame at all: the fallback read brings the values within its time (60 s).
  b.gone = false; b.fields.checks_done = 19
  const t0 = Date.now()
  const fell = await waitUntil(async () => (await dashOf(page)).pico?.parts[0].number === 19, 70000, 500)
  check('with no frame at all the values are read again within the fallback time (60 s)', fell && Date.now() - t0 <= 62000, { ms: Date.now() - t0, stale: (await data(page)).reads.pico.stale })
  await finish()
}

if (row === 'rate') {
  // Bots that no other row on this gateway talks to, so every read counted here is this row's own.
  for (const [slug, name] of [['pico', 'Pico'], ['otto', 'Otto'], ['atlas', 'Atlas']]) give(slug, name)
  const page = await start()
  await waitUntil(async () => Object.keys(await dashOf(page)).length === 3, 30000, 300)
  // Three pages in all.
  await page.evaluate(() => { window.__xrPages.open({ id: 'second', live: '/members', yawDeg: -38, pitchDeg: 0 }); window.__xrPages.open({ id: 'third', live: '/schedule', yawDeg: 38, pitchDeg: 0 }) })
  await sleep(9000)
  const r = await read(page)
  note('pages', Object.fromEntries(Object.entries(r.sources).map(([id, s]) => [id, { path: s.path, status: s.status }])))
  await page.screenshot({ path: `${shots}/rate-room.png` })
  const quiet = await frames(page, 10000)
  note('frame time, three pages and three rest forms, nothing written (10 s)', { ...quiet, load: load() })
  // Pico writes 10 times a second for 20 s: each write is a field frame and the frame that says her page moved.
  const b = bots.get('pico')
  const g0 = b.gets.length; const p0 = b.posts.length
  const t0 = Date.now()
  let n = 0
  const writer = setInterval(() => { n += 1; b.fields.checks_done = n % 21; b.fields.spend_today = n % 48; frame(page, 'pico', 'value').catch(() => {}) }, 100)
  const busy = await frames(page, 20000)
  clearInterval(writer)
  const during = { posts: b.posts.slice(p0).map(t => t - t0), gets: b.gets.length - g0 }
  await sleep(5000)
  const posts = b.posts.slice(p0).map(t => t - t0)
  const gaps = posts.slice(1).map((t, i) => t - posts[i])
  note('frame time while a Bot writes 10 times a second (20 s)', { ...busy, load: load(), writes: n })
  check('under 10 writes a second the whole reads start at most once in 2 s', gaps.every(g => g >= 1900) && during.posts.length <= 11, { wholeReads: during.posts.length, leastGapMs: Math.min(...gaps), startsMs: posts })
  check('one more whole read follows the end of the burst, and then none', posts.length === during.posts.length + 1 || posts.length === during.posts.length, { after: posts.length - during.posts.length })
  note('page reads of Pico during the burst (the dashboard\'s own refresh, one at a time)', { gets: b.gets.length - g0 })
  check('the last value written is the one drawn', (await dashOf(page)).pico.parts[0].number === n % 21, { drawn: (await dashOf(page)).pico.parts[0].number, written: n % 21 })
  // What the three rest forms cost: the same room with them and without them, by draw calls (which the host's load
  // does not move) and by frame time (which it does).
  const withForms = { calls: (await read(page)).calls, textures: (await read(page)).textures }
  for (const slug of ['pico', 'otto', 'atlas']) { bots.get(slug).fields.xr_layout = {}; await frame(page, slug) }
  await waitUntil(async () => Object.keys(await dashOf(page)).length === 0, 15000, 300)
  await sleep(1000)
  const noForms = { calls: (await read(page)).calls, textures: (await read(page)).textures }
  const bare = await frames(page, 10000)
  note('the same room with no dashboard (10 s)', { ...bare, load: load() })
  note('draw calls and textures of the room, with the three rest forms and without', { withForms, noForms, formsCostCalls: withForms.calls - noForms.calls })
  // The page read (a GET that makes the gateway put the Bot's whole page together): none for a Bot that writes.
  // The room's clock asks for it every 60 s, so over this row's minutes a read may come from the clock alone.
  const getsAt = b.gets.slice(g0).map(t => t - t0)
  const allGaps = b.gets.slice(1).map((t, i) => t - b.gets[i])
  check('a Bot that writes for 20 s causes no page read: the only ones are the 60 s clock\'s', allGaps.every(g => g >= 55000) && getsAt.filter(t => t >= 0 && t <= 20000).length <= 1, { pageReadsInTheBurst: getsAt.filter(t => t >= 0 && t <= 20000).length, allPageReads: b.gets.length, leastGapMs: allGaps.length ? Math.min(...allGaps) : null })
  // The Bot applies a page: the frame that says so causes one page read, at once.
  const before = b.gets.length
  const tA = Date.now()
  await frame(page, 'pico', 'apply')
  await waitUntil(async () => b.gets.length > before, 5000, 50)
  const afterApply = b.gets.length - before
  await sleep(3000)
  check('an apply causes one page read, at once', afterApply === 1 && b.gets.length - before === 1 && b.gets.at(-1) - tA < 2000, { reads: b.gets.length - before, ms: b.gets.at(-1) - tA })
  // A Bot with no package of its own is read once and not again by the clock; an apply of that Bot reads it again.
  check('a Bot with no package of its own was read once in the whole row, not again by the clock', Object.keys(plain).length > 0 && Object.values(plain).every(a => a.length === 1) && Date.now() - b.gets[0] > 60000, { reads: Object.fromEntries(Object.entries(plain).map(([k, v]) => [k, v.length])), rowMs: Date.now() - b.gets[0] })
  check('the room keeps its frames while a Bot writes fast: the median frame is no slower than with nothing written, by more than a frame', busy.medianMs <= quiet.medianMs + 17, { quiet, busy })
  await finish()
}

if (row === 'session') {
  // An emulated session with scripted hands (iwer), as one eye of the Aura sees it: not a person's hands and not
  // the glasses. No raised hand: the head's dot aims, and a pinch of a hand that rests presses.
  const { AURA, EYE, enterAura, look, parkHands, emu } = await import('./pages-lib.mjs')
  give('mira', 'Mira')
  got = await open({ standIn: standIn(), emulate: true, viewport: { width: AURA.width, height: AURA.height } })
  const page = got.page
  await waitUntil(async () => { const c = (await read(page)).crew; return c.length > 0 && c.every(b => b.drawn && b.shown) }, 90000, 400)
  await page.evaluate(() => window.__xrPages.stroll?.(false))
  await waitUntil(async () => (await dashOf(page)).mira, 20000, 300)
  await enterAura(page)
  await parkHands(page)
  // A pinch as short as the emulator makes one: down, one frame, up. On this host a frame can take 150 ms, so a pinch
  // of 3 frames was read as a long press (530 ms) and did nothing: that one is made again, once, and noted.
  const longs = []
  const pinch = async () => {
    for (let i = 0; i < 2; i += 1) {
      await emu.select(page, { hand: 'right', down: true }); await emu.frames(page, 1)
      await emu.select(page, { hand: 'right', down: false }); await emu.frames(page, 8)
      const last = (await read(page)).pinch?.last
      if (last?.kind !== 'long') return last
      longs.push(last)
    }
    return (await read(page)).pinch?.last
  }
  await emu.frames(page, 20)
  const aim = async p => { const yaw = (Math.atan2(p[0], -p[2]) * 180) / Math.PI; const pitch = -(Math.atan2(EYE - p[1], Math.hypot(p[0], p[2])) * 180) / Math.PI; await look(page, yaw, pitch); await emu.frames(page, 12); return { yaw: +yaw.toFixed(1), pitch: +pitch.toFixed(1) } }
  const rest = (await dashOf(page)).mira
  check('in a session the rest form stands at the Bot', !!rest && rest.where === 'rest', rest ? { where: rest.where, at: rest.at.map(v => +v.toFixed(2)) } : null)
  const to = await aim(mid(rest.box))
  await page.screenshot({ path: `${shots}/session-rest.png` })
  // A pinch held long ends with no press: the form stays with its Bot.
  if (!process.env.NO_LONG) { await emu.select(page, { hand: 'right', down: true }); await emu.frames(page, 45) }
  await emu.select(page, { hand: 'right', down: false }); await emu.frames(page, 30)
  const longOne = (await read(page)).pinch?.last
  check('a pinch held long on the rest form takes nothing', longOne?.kind === 'long' && (await dashOf(page)).mira.where === 'rest', { kind: longOne?.kind, ms: longOne?.ms, where: (await dashOf(page)).mira.where })
  note('the first pinch, as the pinch book has it', await pinch())
  await emu.frames(page, 40)
  const taken = (await dashOf(page)).mira
  check('the head\'s dot on the rest form and a pinch of a hand that rests take it', taken.where === 'taken' && taken.words.header === 'Mira\'s numbers', { head: to, where: taken.where, words: taken.words })
  check('it stands 0.8 m ahead of where the head looked, within 5 cm (Q8, M71)', Math.abs(Math.hypot(taken.at[0], taken.at[2]) - 0.8) <= 0.05, { aheadM: +Math.hypot(taken.at[0], taken.at[2]).toFixed(3), y: +taken.at[1].toFixed(3) })
  const to2 = await aim(mid(taken.box))
  await page.screenshot({ path: `${shots}/session-taken.png` })
  const taps = (await read(page)).crew.reduce((n, b) => n + b.taps, 0)
  note('what the head\'s dot lies on before the second pinch', (await read(page)).pinch?.head?.on)
  note('the second pinch, as the pinch book has it', await pinch())
  note('pinches the slow host made long, made again', longs)
  await emu.frames(page, 40)
  const back = (await dashOf(page)).mira
  check('the dot on the taken form and a pinch send it back, and no Bot behind it was tapped', back.where === 'rest' && (await read(page)).crew.reduce((n, b) => n + b.taps, 0) === taps, { head: to2, where: back.where })
  await finish()
}

if (row === 'drag') {
  give('mira', 'Mira')
  const page = await start()
  await waitUntil(async () => (await dashOf(page)).mira, 20000, 300)
  const mira = await botOf(page, 'mira')
  await face(page, mira.feet)
  const at = await trayAt(page, (await dashOf(page)).mira)
  const turn0 = (await read(page)).turn
  // A press that moves: down on the tray, 120 px to the side in steps, up.
  await page.mouse.move(at.x, at.y); await sleep(80)
  await page.mouse.down(); await sleep(60)
  for (let i = 1; i <= 12; i += 1) { await page.mouse.move(at.x + i * 10, at.y); await sleep(25) }
  await sleep(100)
  const turnHeld = (await read(page)).turn
  await page.mouse.up(); await sleep(900)
  const after = (await dashOf(page)).mira
  const turn1 = (await read(page)).turn
  check('a drag that begins on a tray does not take it', after.where === 'rest', { where: after.where })
  check('the drag turns the picture, as a drag of empty space does', JSON.stringify(turnHeld) !== JSON.stringify(turn0) && JSON.stringify(turn1) !== JSON.stringify(turn0), { before: turn0, held: turnHeld, after: turn1 })
  // A press that moves less than a drag does (4 px) is still a press.
  const mira2 = await botOf(page, 'mira')
  await face(page, mira2.feet)
  const at2 = await trayAt(page, (await dashOf(page)).mira)
  await page.mouse.move(at2.x, at2.y); await sleep(80)
  await page.mouse.down(); await sleep(60); await page.mouse.move(at2.x + 4, at2.y); await sleep(60); await page.mouse.up(); await sleep(900)
  check('a press that moves 4 px still takes it', (await dashOf(page)).mira.where === 'taken', { where: (await dashOf(page)).mira.where })
  // And a drag that begins on the taken form does not send it back.
  const t = (await dashOf(page)).mira
  await page.evaluate(() => window.__xrPages.look({ yawDeg: 0, pitchDeg: -30, lens: 'start' })); await sleep(600)
  const at3 = await roomOnScreen(page, mid(t.box))
  await page.mouse.move(at3.x, at3.y); await sleep(80)
  await page.mouse.down(); await sleep(60)
  for (let i = 1; i <= 8; i += 1) { await page.mouse.move(at3.x - i * 10, at3.y); await sleep(25) }
  await page.mouse.up(); await sleep(900)
  check('a drag that begins on the taken form does not send it back', (await dashOf(page)).mira.where === 'taken', { where: (await dashOf(page)).mira.where })
  await finish()
}

if (row === 'reconnect') {
  // Pico has no package of its own when the room opens. The socket goes down; while it is down Pico applies its
  // first page, so the frame that says so is never seen. The socket comes back.
  const sockets = { down: false, open: [], opened: 0, refused: 0 }
  got = await open({ standIn: async context => {
    await standIn()(context)
    await context.routeWebSocket(/\/api\/ws/, ws => {
      if (sockets.down) { sockets.refused += 1; ws.close({ code: 1012, reason: 'row: down' }); return }
      sockets.opened += 1
      ws.connectToServer()
      sockets.open.push(ws)
    })
  } })
  const page = got.page
  await waitUntil(async () => { const c = (await read(page)).crew; return c.length > 0 && c.every(b => b.drawn && b.shown) }, 90000, 400)
  await page.evaluate(() => window.__xrPages.stroll?.(false))
  await sleep(4000)
  const slugs = (await read(page)).crew.map(b => b.slug)
  const before = Object.fromEntries(slugs.map(sl => [sl, (plain[sl] ?? []).length]))
  check('every Bot\'s page was read once, and no Bot has a dashboard', slugs.length > 1 && slugs.every(sl => before[sl] === 1) && Object.keys(await dashOf(page)).length === 0, { before, opened: sockets.opened })
  const who = slugs.includes('pico') ? 'pico' : slugs[0]
  sockets.down = true
  for (const ws of sockets.open.splice(0)) await ws.close({ code: 1012, reason: 'row: down' }).catch(() => {})
  await waitUntil(async () => sockets.refused > 0, 30000, 200)
  // The first apply, with the socket down: from here the Bot's page answers with a package that declares the field.
  give(who, who)
  await sleep(1500)
  check('while the socket is down nothing is read again', (bots.get(who).gets.length === 0) && slugs.filter(sl => sl !== who).every(sl => (plain[sl] ?? []).length === before[sl]), { refused: sockets.refused })
  const opened = sockets.opened
  sockets.down = false
  const t0 = Date.now()
  await waitUntil(async () => sockets.opened > opened, 60000, 200)
  const seen = await waitUntil(async () => (await dashOf(page))[who], 20000, 200)
  check('when the socket is back, the Bot that applied its first page while it was down has its dashboard', !!seen, { who, ms: Date.now() - t0, where: seen ? seen.where : null })
  await sleep(3000)
  const after = Object.fromEntries(slugs.filter(sl => sl !== who).map(sl => [sl, (plain[sl] ?? []).length - before[sl]]))
  check('every Bot\'s page was read again once for the reconnect, not more', Object.values(after).every(n => n === 1) && bots.get(who).gets.length === 1, { again: after, [who]: bots.get(who).gets.length, socketsOpened: sockets.opened - opened })
  await page.screenshot({ path: `${shots}/reconnect.png` })
  await finish()
}

if (row === 'cost') {
  // What twelve trays cost: a room of 12 Bots that each hold one, against the same room with none, by turns, so
  // that the host's load weighs on both alike. Draw calls and the trays' own work each frame do not move with the
  // load; frames a second do, and are given beside it. usage: dash3d-check.mjs cost <tag>
  const tag = process.argv[3] ?? 'now'
  const crew = 12
  got = await open({ standIn: async context => {
    await standIn(crew)(context)
    await context.route(/\/api\/members\/[^/]+\/dashboard(\/refetch)?(\?|$)/, async route => {
      const url = new URL(route.request().url())
      const [, slug] = /\/api\/members\/([^/]+)\/dashboard/.exec(url.pathname) ?? []
      if (!bots.has(slug)) give(slug, url.searchParams.get('member') ?? slug)
      return route.fallback()
    })
  } })
  const page = got.page
  await waitUntil(async () => { const c = (await read(page)).crew; return c.length === crew && c.every(b => b.drawn && b.shown) }, 90000, 400)
  await page.evaluate(() => window.__xrPages.stroll?.(false)); await sleep(2500)
  await page.evaluate(() => window.__xrPages.look({ yawDeg: 0, pitchDeg: -22, lens: 'start' })); await sleep(800)
  await waitUntil(async () => Object.keys(await dashOf(page)).length === crew, 40000, 400)
  await page.screenshot({ path: `${shots}/cost-${tag}.png` })
  const work = () => page.evaluate(() => ({ ...window.__xrDashWork }))
  const set = async on => {
    for (const [slug, b] of bots) { b.fields.xr_layout = on ? structuredClone(LAYOUT) : {}; await frame(page, slug) }
    await waitUntil(async () => Object.keys(await dashOf(page)).length === (on ? crew : 0), 30000, 300)
    await sleep(1200)
  }
  const turns = []
  for (let i = 0; i < 3; i += 1) {
    for (const on of [true, false]) {
      await set(on)
      const w0 = await work()
      const f = await frames(page, 8000)
      const w1 = await work()
      turns.push({ trays: on ? crew : 0, calls: (await read(page)).calls, ...f, traysWorkMsAFrame: on ? +((w1.ms - w0.ms) / Math.max(1, w1.frames - w0.frames)).toFixed(3) : 0, load: load() })
    }
  }
  const of = on => turns.filter(t => (t.trays > 0) === on)
  const mean = (list, k) => +(list.reduce((s, t) => s + t[k], 0) / list.length).toFixed(2)
  const sum = { tag, callsWith: mean(of(true), 'calls'), callsWithout: mean(of(false), 'calls'), callsATray: +((mean(of(true), 'calls') - mean(of(false), 'calls')) / crew).toFixed(1), traysWorkMsAFrame: mean(of(true), 'traysWorkMsAFrame'), framesASecondWith: mean(of(true), 'perSecond'), framesASecondWithout: mean(of(false), 'perSecond'), medianMsWith: mean(of(true), 'medianMs'), medianMsWithout: mean(of(false), 'medianMs'), loadMean: mean(turns, 'load') }
  note('turns', turns)
  note('sum', sum)
  check('twelve trays stand, and were measured against none three times each', turns.length === 6, sum)
  out.row = `cost-${tag}`
  writeFileSync(`${ROOT}/rows/dash3d-cost-${tag}.json`, JSON.stringify(out, null, 1))
  await got.browser.close().catch(() => {})
  process.exit(0)
}

console.log('no such row: ' + row)
process.exit(2)
