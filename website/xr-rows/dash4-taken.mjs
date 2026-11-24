// P20c (D103 2, 3): a taken dashboard is whole in the lens AT THE TAKE (his head on the rest tray, 17 to 21 degrees
// down), comes whole again when his head rests level (the held board's rule: 500 ms at rest, a 300 ms glide), and
// never moves while a pinch is held (the head turned 15 degrees) or while his dot is on it (the head 5 degrees off).
// Whole is judged two ways: the probe's box inside the lens, and by pixels: the lens's top and foot rows across the
// form's width against the same head pose with the tray sent back, the places where Bots stand left out (each Bot's
// column, projected by this row's own camera, which is checked against the build's box of the form).
// Then the cases beside: a second take while one is taken; a room switch while taken; a reload.
// Emulated session (iwer), the Aura's lens 40.7 degrees tall, the sample crew (fixture=1).
// usage: render-slot.sh node dash4-taken.mjs [out-prefix]
//   PUSH_DEG=5: the row holds a pinch on empty space and turns his head until the form's top is 5 degrees out of the
//   lens, and judges it there: that check must FAIL (D103 3).
import { mkdirSync, writeFileSync } from 'node:fs'
import { dashOf, open, read, sleep, waitUntil } from './dash3d-lib.mjs'
import { AURA, EYE, enterAura, parkHands, emu } from './pages-lib.mjs'

const name = process.argv[2] ?? 'dash4-taken'
const OUT = process.env.HAND_OUT ?? (process.env.XR_UI ?? '..') + '/iterations/iter-15/dash4'
const PUSH = Number(process.env.PUSH_DEG ?? 0)
/** ONLY=six: only the six-part tray (M71, D107 1), judged by the box and by pixels. */
const ONLY = process.env.ONLY ?? ''
mkdirSync(OUT, { recursive: true })
let bad = 0
const check = (what, ok, got) => { if (!ok) bad += 1; tally[ok ? 'pass' : 'red'].push(what.split(':')[0]); console.log(`${ok ? 'PASS' : 'FAIL'} ${what}: ${JSON.stringify(got).slice(0, 900)}`) }
const tally = { pass: [], red: [], notJudged: [] }
/**
 * D188 1: the one reason a check may be not judged: the Bot whose form is taken stands behind it, and its own place
 * covers so much of the lens's top or foot row that the row is under a quarter judged, when the row would be a quarter
 * judged without that Bot's place alone. Never a pass, never hidden: a NOT-JUDGED line, counted on its own.
 */
const OWN_BOT = 'not judged: her own Bot stands behind the row'
const notJudged = (what, got) => { tally.notJudged.push(what.split(':')[0]); console.log(`NOT-JUDGED ${what}: ${JSON.stringify(got).slice(0, 900)}`) }
const note = (what, got) => console.log(`  ${what}: ${JSON.stringify(got).slice(0, 900)}`)
const DEG = 180 / Math.PI
/** The board's rule, read from the build (`Board.tsx` `HELD_COME`): rest 500 ms, glide 300 ms. A frame's slack on top. */
const COME_MS = 500 + 300 + 400

// DESK_ROOM=1: the page's query besides the fixture, as the desk row opens it (`&words=1&unread=mira,sage&up=`: the room
// where the re-take dropped its press, D140 D).
const got = await open({ fixture: true, emulate: true, ...(process.env.DESK_ROOM === '1' ? { query: '&words=1&unread=mira,sage&up=' } : {}), viewport: { width: AURA.width, height: AURA.height } })
const { page } = got
await waitUntil(async () => { const c = (await read(page)).crew; return c.length > 0 && c.every(b => b.drawn) }, 90000, 400)
// Atlas gets a tray of six parts, the widest the kit takes (M71: the widest box reads a turned box worst).
const SIX = { xr_layout: { v: 1, parts: ['Aa', 'Bb', 'Cc', 'Dd', 'Ee', 'Ff'].map((name, i) => ({ kind: ['number', 'ring', 'bar', 'lamps', 'number', 'ring'][i], name, field: i === 3 ? 'fleet' : 'good', max: 9 })) }, good: 4, fleet: [{ state: 'working' }] }
await page.evaluate(w => { window.__xrPages.stroll?.(false); window.__xrDash3d.hold(true); window.__xrDash3d.set('atlas', w) }, SIX)
await waitUntil(async () => { const d = await dashOf(page); return d.atlas && d.mira }, 20000, 300)
await enterAura(page)
await parkHands(page)
await emu.frames(page, 20)

// The head this row has set: its place, its yaw (to the right +) and pitch (up +).
const head = { eye: [0, EYE, 0], yaw: 0, pitch: 0 }
const setHead = async (eye, yaw, pitch, frames = 14) => { head.eye = eye; head.yaw = yaw; head.pitch = pitch; await emu.setHead(page, { position: eye, yawDeg: -yaw, pitchDeg: pitch }); await emu.frames(page, frames) }
const aimOf = (to, from) => { const d = to.map((v, i) => v - from[i]); return { yaw: Math.atan2(d[0], -d[2]) * DEG, pitch: Math.atan2(d[1], Math.hypot(d[0], d[2])) * DEG } }
const lookAt = async (to, from = head.eye) => { const a = aimOf(to, from); await setHead(from, a.yaw, a.pitch); return a }
/** The head turned from `h0` (where the picture box `nd` was read) so the lens's middle lies on `nd`'s middle. */
const aimNdc = (eye, h0, nd, frames = 14) => setHead(eye, h0.yaw + Math.atan(((nd[0] + nd[2]) / 2) * Math.tan(AURA.fovxDeg / 2 / DEG)) * DEG, h0.pitch + Math.atan(((nd[1] + nd[3]) / 2) * Math.tan(AURA.fovyDeg / 2 / DEG)) * DEG, frames)
/** The drawn boxes of a form's parts that are in the lens, the build's own (`partsNdc`). */
const partMids = d => {
  const parts = (d?.partsNdc ?? []).filter(n => n && n.every(Number.isFinite) && n[0] > -0.95 && n[2] < 0.95 && n[1] > -0.95 && n[3] < 0.95)
  // A tray with no parts (a broken layout: Mira's sample says "unknown version") has only its grey tray to aim at.
  return parts.length ? parts : d?.ndc && d.ndc.every(Number.isFinite) ? [d.ndc] : []
}
/** A point of the room in the picture (pixels), by this row's camera: the head above, the Aura's lens. */
const onScreen = p => {
  const y = head.yaw / DEG, t = head.pitch / DEG
  const d = [p[0] - head.eye[0], p[1] - head.eye[1], p[2] - head.eye[2]]
  const x1 = d[0] * Math.cos(y) + d[2] * Math.sin(y); const z1 = -d[0] * Math.sin(y) + d[2] * Math.cos(y)
  const y2 = d[1] * Math.cos(t) + z1 * Math.sin(t); const z2 = -d[1] * Math.sin(t) + z1 * Math.cos(t)
  const k = Math.tan(AURA.fovyDeg / 2 / DEG)
  if (z2 >= -0.01) return null
  return { x: AURA.width / 2 + (x1 / -z2 / (k * (AURA.width / AURA.height))) * (AURA.width / 2), y: AURA.height / 2 - (y2 / -z2 / k) * (AURA.height / 2) }
}
const rectOf = points => { const at = points.map(onScreen).filter(Boolean); if (!at.length) return null; return { x0: Math.min(...at.map(q => q.x)), y0: Math.min(...at.map(q => q.y)), x1: Math.max(...at.map(q => q.x)), y1: Math.max(...at.map(q => q.y)) } }
const cornersOf = b => { const out = []; for (const x of [b[0][0], b[1][0]]) for (const y of [b[0][1], b[1][1]]) for (const z of [b[0][2], b[1][2]]) out.push([x, y, z]); return out }
/** Where each Bot stands in the picture now: its body's column (where its parts are, else its feet), half again as wide. */
/** The box of the Bot whose form is `slug`'s, as `botBoxes` draws each (D188 1: the own Bot behind its taken form). */
const boxOfBot = b => { const base = b.parts ?? b.feet; const tall = b.top[1] - b.feet[1]; const r = b.widthM * 0.75; return rectOf(cornersOf([[base[0] - r, base[1] - 0.02, base[2] - r], [base[0] + r, base[1] + tall + 0.04, base[2] + r]])) }
const ownBox = async slug => { const crew = (await read(page)).crew.filter(c => !c.seat); const b = crew.find(c => c.slug === slug); return b ? { own: boxOfBot(b), others: crew.filter(c => c.slug !== slug).map(boxOfBot).filter(Boolean) } : null }
const botBoxes = async () => (await read(page)).crew.filter(b => !b.seat).map(b => {
  const base = b.parts ?? b.feet; const tall = b.top[1] - b.feet[1]; const r = b.widthM * 0.75
  return rectOf(cornersOf([[base[0] - r, base[1] - 0.02, base[2] - r], [base[0] + r, base[1] + tall + 0.04, base[2] + r]]))
}).filter(Boolean)
/**
 * Where each page stands in the picture now (M127): the box round its four corners as it faces. Its letters differ
 * between two stills by 6 to 15 levels (REVIEW-54 Part 4), so the side columns leave them out as they leave out the
 * Bots. M140's floor stays: a side covered past three quarters is not judged, and that side is red.
 */
const pageBoxes = async () => ((await read(page)).panels ?? []).map(p => {
  // D173 2: the page by its four corners as it stands turned (the probe's `right` and `up`), a twentieth again, and
  // the picture's box round them. The box of its middle and size, unturned, missed the near edge of a page turned
  // to the side (w1288, w1291-h: `atlas-at-take`'s right side read that page's new picture, in the same place).
  const [w, h] = p.shown ?? p.size
  const hx = (w / 2) * 1.05, hy = (h / 2) * 1.05
  const right = p.right ?? [1, 0, 0], up = p.up ?? [0, 1, 0]
  const corners = [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([sx, sy]) => p.position.map((v, i) => v + right[i] * hx * sx + up[i] * hy * sy))
  // shortcut: a corner behind the eye is left out, so a page reaching past the eye's plane is boxed by its other
  // corners only; clip the page's edges at the near plane when a row stands that close to a page.
  return rectOf(corners)
}).filter(Boolean)
const inside = n => !!n && n[0] > -0.99 && n[1] > -0.99 && n[2] < 0.99 && n[3] < 0.99
const ndcToPx = n => ({ x0: ((n[0] + 1) / 2) * AURA.width, x1: ((n[2] + 1) / 2) * AURA.width, y0: ((1 - n[3]) / 2) * AURA.height, y1: ((1 - n[1]) / 2) * AURA.height })

/**
 * The lens's top 4 rows and foot 4 rows across the form's width: the share of their pixels, outside every Bot's box,
 * that changed from the same pose with the tray sent back; and the lit share of the lip (the box's lowest 40 %).
 */
const judge = (png, back, n, bots) => page.evaluate(async ([src, was, b, skip]) => {
  const load = async s => { const img = await new Promise((done, fail) => { const i = new Image(); i.onload = () => done(i); i.onerror = fail; i.src = `data:image/png;base64,${s}` }); const c = document.createElement('canvas'); c.width = img.naturalWidth; c.height = img.naturalHeight; const x = c.getContext('2d', { willReadFrequently: true }); x.drawImage(img, 0, 0); return { c, x } }
  const { c, x } = await load(src)
  const o = was ? await load(was) : null
  const x0 = Math.max(0, Math.round(((b[0] + 1) / 2) * c.width)), x1 = Math.min(c.width, Math.round(((b[2] + 1) / 2) * c.width))
  const out = (px, py) => skip.some(r => px >= r.x0 && px <= r.x1 && py >= r.y0 && py <= r.y1)
  const band = y0 => {
    const w = Math.max(1, x1 - x0); const d = x.getImageData(x0, y0, w, 4).data; const e = o ? o.x.getImageData(x0, y0, w, 4).data : null
    let n = 0, moved = 0, left = 0
    for (let i = 0; i < d.length; i += 4) {
      const px = x0 + ((i / 4) % w), py = y0 + Math.floor(i / 4 / w)
      if (out(px, py)) { left += 1; continue }
      n += 1
      // 12 levels, not 40: the lip and the tray are dark, near the room's black, and a cut lip changed under 40 (w1118).
      if (e && (Math.abs(d[i] - e[i]) + Math.abs(d[i + 1] - e[i + 1]) + Math.abs(d[i + 2] - e[i + 2])) / 3 > 12) moved += 1
    }
    return { judged: n, leftOut: left, changed: e && n ? +(moved / n).toFixed(3) : null }
  }
  const yLow = Math.round(((1 - b[1]) / 2) * c.height), yTop = Math.round(((1 - (b[1] + (b[3] - b[1]) * 0.4)) / 2) * c.height)
  let lip = null
  // The lip as far as it is in the lens; none when all of it is out (w1121: a lip wholly above the lens read row 0).
  const yA = Math.max(0, yTop), yB = Math.min(c.height, yLow)
  if (x1 > x0 && yB > yA) { const d = x.getImageData(x0, yA, x1 - x0, yB - yA).data; let lit = 0; for (let i = 0; i < d.length; i += 4) if ((d[i] + d[i + 1] + d[i + 2]) / 3 > 40) lit += 1; lip = +(lit / (d.length / 4)).toFixed(3) }
  return { top: band(0), foot: band(c.height - 4), lip }
}, [png, back, n, bots])

/** As `judge`, for the lens's left and right 4 columns over the form's height. */
const judgeSides = (png, back, n, bots) => page.evaluate(async ([src, was, b, skip]) => {
  const load = async s => { const img = await new Promise((done, fail) => { const i = new Image(); i.onload = () => done(i); i.onerror = fail; i.src = `data:image/png;base64,${s}` }); const c = document.createElement('canvas'); c.width = img.naturalWidth; c.height = img.naturalHeight; const x = c.getContext('2d', { willReadFrequently: true }); x.drawImage(img, 0, 0); return { c, x } }
  const { c, x } = await load(src)
  const o = await load(was)
  const y0 = Math.max(0, Math.round(((1 - b[3]) / 2) * c.height)), y1 = Math.min(c.height, Math.round(((1 - b[1]) / 2) * c.height))
  const out = (px, py) => skip.some(r => px >= r.x0 && px <= r.x1 && py >= r.y0 && py <= r.y1)
  const band = x0 => {
    const h = Math.max(1, y1 - y0); const d = x.getImageData(x0, y0, 4, h).data; const e = o.x.getImageData(x0, y0, 4, h).data
    let n = 0, moved = 0, left = 0
    for (let i = 0; i < d.length; i += 4) {
      const px = x0 + ((i / 4) % 4), py = y0 + Math.floor(i / 4 / 4)
      if (out(px, py)) { left += 1; continue }
      n += 1
      if ((Math.abs(d[i] - e[i]) + Math.abs(d[i + 1] - e[i + 1]) + Math.abs(d[i + 2] - e[i + 2])) / 3 > 12) moved += 1
    }
    return { judged: n, leftOut: left, changed: n ? +(moved / n).toFixed(3) : null }
  }
  // M140: a side whose last 4 columns lie mostly on a page or a Bot is judged a little further in: the first band of
  // 4 columns, at most 96 px in, of which at least a quarter is left in. `inPx` says how far in it stood.
  const side = (at, step) => {
    for (let k = 0; k <= 24; k += 1) { const x0 = at + step * 4 * k; const s = band(x0); if (s.judged >= 0.25 * (s.judged + s.leftOut)) return { ...s, inPx: 4 * k } }
    return { ...band(at), inPx: null }
  }
  return { left: side(0, 1), right: side(c.width - 4, -1) }
}, [png, back, n, bots])

const pinch = async () => {
  // D188 2: what the trays' own handlers saw of this pinch (`Dash3d.tsx` `dashWork.presses`, the build's probe), on a
  // line of its own after each pinch: which piece took the down, whether it was drawn, each leave and enter between,
  // the up, and what `trayPress` answered.
  const t0 = await page.evaluate(() => performance.now())
  for (let i = 0; i < 3; i += 1) {
    await emu.select(page, { hand: 'right', down: true }); await emu.frames(page, 1)
    await emu.select(page, { hand: 'right', down: false }); await emu.frames(page, 8)
    if ((await read(page)).pinch?.last?.kind !== 'long') break
  }
  const seen = await page.evaluate(t => (window.__xrDashWork?.presses ?? null)?.filter(x => x.ms >= t - 1), t0)
  pinchLog.push({ last: (await read(page)).pinch?.last ?? null, trays: seen })
  console.log(`  pinch ${pinchLog.length}: ${JSON.stringify(pinchLog[pinchLog.length - 1]).slice(0, 1500)}`)
}
const pinchLog = []
const mid = b => [(b[0][0] + b[1][0]) / 2, (b[0][1] + b[1][1]) / 2, (b[0][2] + b[1][2]) / 2]
const onWhat = async () => (await read(page)).pinch?.head?.on?.what ?? null
/** A take: the dot moved over the tray's points until the probe says it lies on the dashboard, then a pinch at once. */
const take = async (slug, downDeg = null, onlyFrom = null) => {
  const tries = []
  // An eye placed so the tray's top lies `downDeg` under his eye line, on the line from the tray to the room's middle.
  const at19 = r => { const h = Math.hypot(r.at[0], r.at[2]) || 1; const far = (EYE - r.at[1]) / Math.tan(downDeg / DEG); return [r.at[0] - (r.at[0] / h) * far, EYE, r.at[2] - (r.at[2] / h) * far] }
  const front = r => { const [a, b] = r.box; const dx = b[0] - a[0], dz = b[2] - a[2]; const n = dx < dz ? [1, 0] : [0, 1]; const s = Math.sign(-(n[0] * r.at[0] + n[1] * r.at[2])) || 1; return [r.at[0] + n[0] * s * 1.2, EYE, r.at[2] + n[1] * s * 1.2] }
  const r0 = (await dashOf(page))[slug]
  if (!r0) return { t: null, tries: ['no tray'] }
  // TAKE_UP (D130 3): the eye that much higher, so a far, low tray is seen from a little above, not edge-on (M100).
  const UP = Number(process.env.TAKE_UP ?? 0)
  // `onlyFrom`: the caller set the eye itself (D173: the full G5's Mira took from the room's middle in one run and
  // from her front in another, by which try first saw her tray lit); only that eye is tried, three times.
  // Six tries at that eye: her edge-on tray is about 30 by 10 px and drifts with her (w1289 missed it three times).
  const froms = onlyFrom ? [onlyFrom, onlyFrom, onlyFrom, onlyFrom, onlyFrom, onlyFrom] : [[0, EYE, 0], [0.4, EYE, 0], [-0.4, EYE, 0], [0, EYE, 0], front(r0), front(r0)].map(f => [f[0], f[1] + UP, f[2]])
  if (!onlyFrom && downDeg !== null && r0.at[1] < EYE - 0.1) froms.unshift(at19(r0), at19(r0))
  for (const from of froms) {
    const r = (await dashOf(page))[slug]
    const bot = (await read(page)).crew.find(b => b.slug === slug)
    const [a, b] = r.box; const c = mid(r.box)
    const lower = bot ? [bot.feet[0], bot.feet[1] + (bot.top[1] - bot.feet[1]) * 0.2, bot.feet[2]] : c
    let on = null, seen = { on: null, lit: false, gliding: [] }
    for (const p of [[r.at[0], r.at[1] - 0.004, r.at[2]], c, [c[0], a[1] + (b[1] - a[1]) * 0.25, c[2]], [a[0] + (b[0] - a[0]) * 0.25, c[1], c[2]], [a[0] + (b[0] - a[0]) * 0.75, c[1], c[2]], lower]) {
      // The dot on a dashboard, and that dashboard the one asked for: it lights (`lit`). Another Bot's tray between
      // (w1110: a pinch on Atlas's from Mira's front) is not taken.
      // What the dot lies on, whether the tray lights and whether a taken form glides, in ONE read (M127): read apart, a
      // form could come where he looks between the two (REVIEW-54 Part 4).
      await lookAt(p, from)
      const r1 = await read(page)
      on = r1.pinch?.head?.on?.what ?? null
      seen = { on, lit: r1.dash3d?.[slug]?.lit === true, gliding: Object.entries(r1.dash3d ?? {}).filter(([, x]) => x.gliding).map(([s]) => s) }
      if (/^dash/.test(on ?? '') && seen.lit) break
    }
    // Then its DRAWN middle, as the build pictures it from here: a grey tray of a broken layout is small and low, and
    // the room-box points missed it (w1127: three of the reasons).
    if (!/^dash/.test(on ?? '') || !seen.lit) {
      const h0 = { yaw: head.yaw, pitch: head.pitch }; const n = (await dashOf(page))[slug]?.ndc
      if (n && n.every(Number.isFinite)) {
        await aimNdc(from, h0, n)
        const r2 = await read(page)
        on = r2.pinch?.head?.on?.what ?? null
        seen = { on, lit: r2.dash3d?.[slug]?.lit === true, gliding: Object.entries(r2.dash3d ?? {}).filter(([, x]) => x.gliding).map(([s]) => s) }
      }
    }
    if (!/^dash/.test(on ?? '') || !seen.lit) { tries.push({ from, ...seen }); continue }
    const at = { ...head }
    const tookAt = Date.now()
    // What decides a pinch that the dot's both ends name as the tray (w1285: two such clicks did not take Mira's):
    // whether a board was in his hand or the keys were up (the press is refused, `trayBlock`), whether a board's
    // picture sent the taken tray back (`dashWork.sentBack`), and the tray's `where` in each of the six frames after.
    const state = async () => { const r = await read(page); return { lit: r.dash3d?.[slug]?.lit === true, where: r.dash3d?.[slug]?.where, boards: (r.boards?.boards ?? []).map(b => `${b.bot}:${b.where}`), keys: r.keys ?? null, sentBack: await page.evaluate(() => window.__xrDashWork?.sentBack?.length ?? null) } }
    const before = await state()
    await pinch()
    const frames = []
    if (process.env.SOON !== '1') for (let i = 0; i < 6; i += 1) { await emu.frames(page, 1); frames.push((await dashOf(page))[slug]?.where ?? null) }
    let t = (await dashOf(page))[slug]
    tries.push({ from, last: (await read(page)).pinch?.last, before, after: await state(), frames })
    // Each pinch on its own line: the check's line is cut at 900 characters (w1292-g5-i lost Mira's second try).
    note(`${slug}'s take, pinch ${tries.length}`, tries[tries.length - 1])
    // Judged once it stands: come all the way (`fit.mix` 1) and fitted (`fit.n` 4). Read after six frames it was still
    // on its way up from the tray (w1114: its foot at -1.58 at the take).
    // SOON=1 (M95): the row goes on at once from the take's own pinch, not once the form stands.
    if (t?.where === 'taken' && process.env.SOON === '1') return { t, tries, at, pinchedAt: tookAt }
    if (t?.where === 'taken') {
      t = (await waitUntil(async () => { await emu.frames(page, 2); const x = (await dashOf(page))[slug]; return x?.fit && x.fit.mix === 1 && x.fit.n >= 4 ? x : null }, 4000, 50)) || (await dashOf(page))[slug]
      return { t, tries, at }
    }
  }
  return { t: null, tries }
}
/** Waits until it is taken, standing (no glide), and placed again at least `moves` times; how long that took. */
const settled = async (slug, moves, ms) => {
  const t0 = Date.now()
  const d = await waitUntil(async () => { await emu.frames(page, 2); const x = (await dashOf(page))[slug]; return x && x.where === 'taken' && !x.gliding && x.moves >= moves && (!x.fit || x.fit.n >= 4) ? x : null }, ms, 50)
  return { d, ms: Date.now() - t0 }
}
const shoot = async tag => (await page.screenshot({ path: `${OUT}/${name}-${tag}.png` })).toString('base64')

// The row's camera is the build's: the form's box projected by this row lies where the probe says it is drawn.
const poses = []
const results = {}
// ONLY=m115 (D132 2): Atlas's form taken; the dot on a Bot and on another tray that show BEHIND its drawn box. Then
// the form lights and what is behind does not; a pinch sends the form back and leaves what was behind as it was; a
// second pinch, the same aim, is what was behind's (a tray is taken, a Bot is pressed).
// ONLY=juno (D140 C): Juno's tray at rest, the dot set on each drawn piece of it from in front of it (its parts' boxes,
// its drawn box, its top's middle), first with nothing taken, then with Atlas's form taken. Each aim says what the dot
// lies on, whether Juno's tray lights, and whether the dot lies in Atlas's box. Then a pinch where it lit.
if (ONLY === 'juno') {
  const aims = async (tag, eye, h0) => {
    const j0 = (await dashOf(page)).juno
    const boxes = [...(j0?.partsNdc ?? []).map((n, i) => [`part${i}`, n]), ['lens', j0?.lens], ['ndc', j0?.ndc]].filter(([, n]) => n && n.every(v => Number.isFinite(v) && Math.abs(v) < 0.98))
    const out = []
    for (const [k, n] of boxes) {
      await aimNdc(eye, h0, n, 3)
      const d = await dashOf(page), on = await onWhat(), L = d.atlas?.lens ?? null
      if (!out.length) await shoot(`juno-${tag.replace(/\W+/g, '-')}`)
      out.push({ k, box: n.map(v => +v.toFixed(3)), now: d.juno?.lens?.map(v => +v.toFixed(3)) ?? null, head: [+head.yaw.toFixed(1), +head.pitch.toFixed(1)], on, junoLit: d.juno?.lit, hidden: d.juno?.hidden?.hidden ?? null, atlas: d.atlas?.where, inAtlasBox: !!L && L[0] < 0 && L[2] > 0 && L[1] < 0 && L[3] > 0 })
    }
    // Room points on the tray itself: its top's middle (the take's first aim) and the slab 2 cm under it.
    for (const [k, dy] of [['top', -0.004], ['slab', -0.02]]) {
      const jj = (await dashOf(page)).juno
      await lookAt([jj.at[0], jj.at[1] + dy, jj.at[2]], eye)
      const d = await dashOf(page), on = await onWhat(), L = d.atlas?.lens ?? null
      await shoot(`juno-${tag.replace(/\W+/g, '-')}-${k}`)
      out.push({ k, head: [+head.yaw.toFixed(1), +head.pitch.toFixed(1)], on, junoLit: d.juno?.lit, hidden: d.juno?.hidden?.hidden ?? null, atlas: d.atlas?.where, inAtlasBox: !!L && L[0] < 0 && L[2] > 0 && L[1] < 0 && L[3] > 0 })
    }
    note(`juno aims (${tag})`, out)
    return out
  }
  const front = r => { const [a, b] = r.box; const dx = b[0] - a[0], dz = b[2] - a[2]; const n = dx < dz ? [1, 0] : [0, 1]; const s = Math.sign(-(n[0] * r.at[0] + n[1] * r.at[2])) || 1; return [r.at[0] + n[0] * s * 1.2, EYE, r.at[2] + n[1] * s * 1.2] }
  const j = (await dashOf(page)).juno
  const eye = front(j)
  const toJ = aimOf([j.at[0], j.at[1], j.at[2]], eye)
  await setHead(eye, toJ.yaw, toJ.pitch)
  const before = await aims('nothing taken', eye, { yaw: head.yaw, pitch: head.pitch })
  check('juno: with nothing taken, the dot on a drawn piece of Juno\'s tray lights it', before.some(x => /dash:juno/.test(x.on ?? '') && x.junoLit), { before })
  const t = await take('atlas')
  if (!t.t) check('juno: Atlas\'s form is taken', false, { tries: t.tries })
  else {
    await setHead(eye, toJ.yaw, toJ.pitch, 4)
    const after = await aims('Atlas taken', eye, { yaw: head.yaw, pitch: head.pitch })
    const outside = after.filter(x => !x.inAtlasBox && /dash:juno/.test(x.on ?? ''))
    check('juno: with Atlas\'s form taken, the dot on a piece of Juno\'s tray outside Atlas\'s box lights Juno\'s tray (D130 2: it is taken at once)', outside.length > 0 && outside.some(x => x.junoLit), { after })
    const lit = after.find(x => x.junoLit)
    if (lit) {
      const j2 = (await dashOf(page)).juno
      if (lit.k === 'top' || lit.k === 'slab') await lookAt([j2.at[0], j2.at[1] + (lit.k === 'top' ? -0.004 : -0.02), j2.at[2]], eye)
      else {
        await setHead(eye, toJ.yaw, toJ.pitch, 4)
        const j3 = (await dashOf(page)).juno
        await aimNdc(eye, { yaw: toJ.yaw, pitch: toJ.pitch }, lit.k === 'lens' ? j3.lens : lit.k === 'ndc' ? j3.ndc : j3.partsNdc[Number(lit.k.slice(4))], 3)
      }
      await pinch(); await emu.frames(page, 12)
      const d = await dashOf(page)
      check('juno: a pinch where Juno\'s tray lit takes it, and Atlas\'s form goes back', d.juno?.where === 'taken' && d.atlas?.where === 'rest', { down: (await read(page)).pinch?.last?.down, juno: d.juno?.where, atlas: d.atlas?.where })
    }
  }
  console.log(`TOTAL bad ${bad}`)
  await got.browser.close()
  process.exit(bad ? 1 : 0)
}
// ONLY=boardhand (D149 4): Atlas's form taken; his head turns to Juno's board and a pinch takes the board into his
// hand. Then the form goes back to its Bot (`Dash3d.tsx:653`, a board in his hand: `block`), and while he holds the
// board a tray neither lights nor is taken (`refused`).
if (ONLY === 'boardhand') {
  const t = await take('atlas')
  if (!t.t) check('boardhand: Atlas\'s form is taken', false, { tries: t.tries })
  else {
    const boards = () => page.evaluate(() => (window.__xrPages.read().boards?.boards ?? []).map(b => ({ id: b.needId, bot: b.bot, where: b.where })))
    const marks = await page.evaluate(() => window.__xrPages.read().boardMarks ?? {})
    const juno = Object.entries(marks).find(([id]) => /juno/.test(id))
    if (!juno) check('boardhand: Juno\'s board is up', false, { boards: await boards() })
    else {
      const a = aimOf(juno[1].paper ?? juno[1].body, t.at.eye)
      await setHead(t.at.eye, a.yaw, a.pitch, 1)
      const r = await read(page), on = r.pinch?.head?.on?.what ?? null
      await pinch(); await emu.frames(page, 20)
      const b1 = await boards(), held = b1.find(b => /juno/.test(b.id ?? '') && b.where === 'held')
      const d1 = await dashOf(page)
      await shoot('boardhand-held')
      check(`boardhand: a pinch on Juno's board takes it into his hand (the dot on ${on})`, !!held, { on, down: (await read(page)).pinch?.last?.down, boards: b1 })
      check('boardhand: with the board in his hand, Atlas\'s form goes back to its Bot', d1.atlas?.where === 'rest', { atlas: d1.atlas?.where })
      // A tray while he holds the board: the dot on Atlas's tray top does not light it, and a pinch does not take it.
      const x = (await dashOf(page)).atlas
      await lookAt([x.at[0], x.at[1] - 0.004, x.at[2]], t.at.eye)
      const r2 = await read(page)
      await pinch(); await emu.frames(page, 12)
      const d2 = await dashOf(page)
      check('boardhand: while he holds the board, a tray is not lit and a pinch on it takes nothing', r2.dash3d?.atlas?.lit !== true && d2.atlas?.where === 'rest', { on: r2.pinch?.head?.on?.what, lit: r2.dash3d?.atlas?.lit, down: (await read(page)).pinch?.last?.down, atlas: d2.atlas?.where, boards: await boards() })
    }
  }
  console.log(`TOTAL bad ${bad}`)
  await got.browser.close()
  process.exit(bad ? 1 : 0)
}
// ONLY=gaprest (D149): Atlas's tray at rest, the eye close in front of it so the gaps between its parts are wide
// (wider than half a degree: their press meshes are off, D56 3). A press in such a gap is not the tray's: it goes to
// what is behind. If a switched-off gap mesh takes it, the down reads `dash:atlas` and nothing happens.
if (ONLY === 'gaprest') {
  const r0 = (await dashOf(page)).atlas
  const rows = []
  for (const far of [0.6, 0.45, 0.35]) {
    const h = Math.hypot(r0.at[0], r0.at[2]) || 1
    const eye = [r0.at[0] - (r0.at[0] / h) * far, r0.at[1] + 0.15, r0.at[2] - (r0.at[2] / h) * far]
    await lookAt([r0.at[0], r0.at[1], r0.at[2]], eye)
    const d = (await dashOf(page)).atlas
    const ps = (d.partsNdc ?? []).map((p, i) => ({ i, p })).filter(({ p }) => p && p.every(v => Number.isFinite(v) && Math.abs(v) < 0.98)).sort((a, b) => a.p[0] - b.p[0])
    for (let k = 0; k + 1 < ps.length; k += 1) {
      const u = ps[k].p, w = ps[k + 1].p, y0 = Math.max(u[1], w[1]), y1 = Math.min(u[3], w[3])
      if (!(w[0] - u[2] > 0.03 && y1 > y0)) continue
      const g = [(u[2] + w[0]) / 2, (y0 + y1) / 2]
      await lookAt([r0.at[0], r0.at[1], r0.at[2]], eye)
      await aimNdc(eye, { yaw: head.yaw, pitch: head.pitch }, [g[0] - 0.001, g[1] - 0.001, g[0] + 0.001, g[1] + 0.001], 2)
      const r = await read(page), on = r.pinch?.head?.on?.what ?? null, lit = r.dash3d?.atlas?.lit
      const before = r.dash3d?.atlas?.where
      await pinch(); await emu.frames(page, 8)
      const last = (await read(page)).pinch?.last, after = (await dashOf(page)).atlas?.where
      rows.push({ far, between: [ps[k].i, ps[k + 1].i], on, lit, down: last?.down, up: last?.up, before, after })
      if (after === 'taken') { const x = (await dashOf(page)).atlas; await lookAt([x.at[0], x.at[1] - 0.004, x.at[2]], eye); await pinch(); await emu.frames(page, 30); await sleep(400) }
    }
  }
  writeFileSync(`${OUT}/${name}-gaprest.json`, JSON.stringify(rows, null, 1))
  note('gaprest', rows)
  // Dropped: the down and the up read the tray, nothing lit it, and it stayed at rest.
  const dropped = rows.filter(x => /dash:atlas/.test(x.down ?? '') && /dash:atlas/.test(x.up ?? '') && !x.lit && x.after === 'rest')
  check(`gaprest: no press in a wide gap of a tray at rest is dropped on the tray (${rows.length} presses, ${dropped.length} dropped)`, rows.length > 0 && dropped.length === 0, { dropped })
  console.log(`TOTAL bad ${bad}`)
  await got.browser.close()
  process.exit(bad ? 1 : 0)
}
// ONLY=gapback (D155, M143): a narrow gap of a tray at rest is the tray's (D56 3), before any take, after a take and a
// send-back, after two, and on Juno's tray after Juno's own; a press there takes the form. The other side: a wide gap
// (its mesh off) takes no ray, no press and no light. Beside: the form taken, the form gliding home, two forms. The
// probe's walk tells which gap meshes have a ray: an on gap is listed as hidden by `its material`, an off one by
// `itself` (it should not be listed at all).
if (ONLY === 'gapback') {
  const rows = []
  const eyeOf = (r, far, up) => { const h = Math.hypot(r.at[0], r.at[2]) || 1; return [r.at[0] - (r.at[0] / h) * far, r.at[1] + up, r.at[2] - (r.at[2] / h) * far] }
  // M151: a pack has no probe walk (`__xrHiddenRays` is not picked into one). There the counts are null, and the row
  // judges the presses alone, by the build's words and by pixels, and says so.
  const gapRays = async slug => {
    const all = await page.evaluate(() => window.__xrHiddenRays?.() ?? null)
    if (!all) return { on: null, off: null }
    const mine = all.filter(x => x.path.startsWith('dash-gap') && x.path.includes(`dash3d=${slug}`))
    return { on: mine.filter(x => x.hiddenBy === 'its material').length, off: mine.filter(x => x.hiddenBy !== 'its material').length }
  }
  const noWalk = (await gapRays('atlas')).on === null
  if (noWalk) console.log('  M151: this build has no probe walk (__xrHiddenRays); the gap rays are not counted, the presses are judged alone')
  // M153: the share of the tray's drawn place at rest (its `ndc` before the press) whose pixels changed 8 frames after
  // the press. Taken, the tray leaves that place; at rest it stays.
  const placeChanged = (a, b, n) => page.evaluate(async ([s1, s2, box]) => {
    const load = async s => { const img = await new Promise((done, fail) => { const i = new Image(); i.onload = () => done(i); i.onerror = fail; i.src = `data:image/png;base64,${s}` }); const c = document.createElement('canvas'); c.width = img.naturalWidth; c.height = img.naturalHeight; const x = c.getContext('2d', { willReadFrequently: true }); x.drawImage(img, 0, 0); return x }
    const A = await load(s1), B = await load(s2), W = A.canvas.width, H = A.canvas.height
    const x0 = Math.max(0, Math.round(((box[0] + 1) / 2) * W)), x1 = Math.min(W, Math.round(((box[2] + 1) / 2) * W)), y0 = Math.max(0, Math.round(((1 - box[3]) / 2) * H)), y1 = Math.min(H, Math.round(((1 - box[1]) / 2) * H))
    if (!(x1 > x0 && y1 > y0)) return null
    const p = A.getImageData(x0, y0, x1 - x0, y1 - y0).data, q = B.getImageData(x0, y0, x1 - x0, y1 - y0).data
    let n = 0, moved = 0
    for (let i = 0; i < p.length; i += 4) { n += 1; if ((Math.abs(p[i] - q[i]) + Math.abs(p[i + 1] - q[i + 1]) + Math.abs(p[i + 2] - q[i + 2])) / 3 > 24) moved += 1 }
    return +(moved / n).toFixed(3)
  }, [a, b, n])
  const still = async () => (await page.screenshot()).toString('base64')
  const sendBack = async (slug, settle = true) => {
    const x = (await dashOf(page))[slug]
    if (x?.where !== 'taken') return
    await lookAt([x.at[0], x.at[1] - 0.004, x.at[2]]); await pinch()
    if (settle) { await emu.frames(page, 40); await sleep(500); await emu.frames(page, 40) }
  }
  // A press in the gap between two neighbouring parts of `slug`'s tray, seen from `eye`: the narrowest gap, or with
  // `wide` the first wider than 0.03 in the lens.
  const gapPress = async (label, slug, eye, wide = false, look = true) => {
    const r0 = (await dashOf(page))[slug]
    if (look) await lookAt([r0.at[0], r0.at[1], r0.at[2]], eye)
    const d = (await dashOf(page))[slug]
    const rays = await gapRays(slug)
    const ps = (d.partsNdc ?? []).map((p, i) => ({ i, p })).filter(({ p }) => p && p.every(v => Number.isFinite(v) && Math.abs(v) < 0.98)).sort((a, b) => a.p[0] - b.p[0])
    const pairs = []
    for (let k = 0; k + 1 < ps.length; k += 1) {
      const u = ps[k].p, w = ps[k + 1].p, y0 = Math.max(u[1], w[1]), y1 = Math.min(u[3], w[3]), sep = w[0] - u[2]
      if (y1 > y0 && sep > 0) pairs.push({ sep, between: [ps[k].i, ps[k + 1].i], g: [(u[2] + w[0]) / 2, (y0 + y1) / 2] })
    }
    const pick = wide ? pairs.find(p => p.sep > 0.03) : pairs.sort((a, b) => a.sep - b.sep)[0]
    if (!pick) { rows.push({ label, slug, where: d.where, rays, gap: null }); return null }
    await aimNdc(eye, { yaw: head.yaw, pitch: head.pitch }, [pick.g[0] - 0.0005, pick.g[1] - 0.0005, pick.g[0] + 0.0005, pick.g[1] + 0.0005], 2)
    const r = await read(page), on = r.pinch?.head?.on?.what ?? null, lit = r.dash3d?.[slug]?.lit === true
    const place = r.dash3d?.[slug]?.ndc ?? null, s0 = await still()
    await pinch(); await emu.frames(page, 8)
    const last = (await read(page)).pinch?.last, after = (await dashOf(page))[slug]?.where
    const moved = place ? await placeChanged(s0, await still(), place) : null
    const row = { label, slug, wide, sep: +pick.sep.toFixed(4), between: pick.between, rays, on, lit, down: last?.down, up: last?.up, kind: last?.kind, after, moved }
    rows.push(row)
    await sendBack(slug)
    return row
  }
  const a0 = (await dashOf(page)).atlas, j0 = (await dashOf(page)).juno
  const far = eyeOf(a0, 1.4, 0.3), farJ = j0 && eyeOf(j0, 1.4, 0.3), near = eyeOf(a0, 0.4, 0.02)
  await gapPress('fresh', 'atlas', far)
  for (const n of [1, 2]) {
    const t = await take('atlas')
    rows.push({ label: `take ${n}`, taken: !!t.t, rays: await gapRays('atlas') })
    await sendBack('atlas')
    await gapPress(`after ${n} take${n > 1 ? 's' : ''}`, 'atlas', far)
  }
  // Gliding (M153): sent back, read 6 frames later while it comes home: its gaps have no ray until it stands at rest
  // (`Dash3d.tsx`: on only while `e === 0`). Then, the glide over, a narrow gap takes the press again, by pixels too.
  // The glide is the taken form's (`gliding`, `Dash3d.tsx`: `glide.current`): his head turned and held, it comes
  // whole again (500 ms at rest, a 300 ms glide). A send-back has no glide (w1260: 30 frames, never `gliding`). While
  // it glides its gaps have no ray, and a press on it sends it back (D114 2), by the build's word and by pixels.
  const tg = await take('atlas')
  if (tg.t) {
    await setHead(head.eye, head.yaw + 20, head.pitch + 8, 4)
    const t0 = Date.now()
    let g = null
    while (Date.now() - t0 < COME_MS + 1500) { await emu.frames(page, 2); g = (await dashOf(page)).atlas; if (g?.gliding) break }
    const rays = await gapRays('atlas')
    let sent = null
    if (g?.gliding && g.lens) {
      const L = g.lens, mid = [(L[0] + L[2]) / 2, (L[1] + L[3]) / 2]
      // The still and the form's drawn place are read at the press's own pose (w1261 read them before the head turned
      // to aim, so the whole picture had moved).
      await aimNdc(head.eye, { yaw: head.yaw, pitch: head.pitch }, [mid[0] - 0.001, mid[1] - 0.001, mid[0] + 0.001, mid[1] + 0.001], 1)
      const place = (await dashOf(page)).atlas?.ndc ?? null, s0 = await still()
      await pinch(); await emu.frames(page, 8)
      const s1 = await still()
      writeFileSync(`${OUT}/${name}-glide-before.png`, Buffer.from(s0, 'base64')); writeFileSync(`${OUT}/${name}-glide-after.png`, Buffer.from(s1, 'base64'))
      sent = { where: (await dashOf(page)).atlas?.where, last: (await read(page)).pinch?.last, place, moved: place ? await placeChanged(s0, s1, place) : null }
    }
    rows.push({ label: 'gliding', where: g?.where, gliding: g?.gliding === true, ms: Date.now() - t0, rays, sent })
    await sendBack('atlas')
    await gapPress('after the glide', 'atlas', far)
  }
  // Two forms: Juno's taken and sent back, then a narrow gap on each.
  if (farJ) {
    const tj = await take('juno')
    rows.push({ label: 'juno take', taken: !!tj.t, rays: { atlas: await gapRays('atlas'), juno: await gapRays('juno') } })
    await sendBack('juno')
    await gapPress('juno after its take', 'juno', farJ)
    await gapPress('atlas after juno', 'atlas', far)
  }
  // At 0.4 m (w1252) the gaps still read on (the slack keeps an on gap on to 0.55 degrees), and a press there went to
  // the room: recorded, not judged.
  await gapPress('near, on', 'atlas', near, true)
  rows[rows.length - 1].wide = false
  rows[rows.length - 1].nearSep = rows[rows.length - 1].sep
  rows[rows.length - 1].sep = undefined
  // The other side: closer in, the gaps wide and their meshes off.
  await gapPress('wide, off', 'atlas', eyeOf(a0, 0.2, 0.1), true)
  writeFileSync(`${OUT}/${name}-gapback.json`, JSON.stringify(rows, null, 1))
  note('gapback', rows.map(x => [x.label, x.sep ?? null, x.rays, x.on ?? null, x.lit ?? null, x.down ?? null, x.after ?? x.where ?? null]))
  const narrow = rows.filter(x => x.sep !== undefined && !x.wide)
  // Taken by the build's word AND by pixels (M153): at least a fifth of its rest place changed. With no walk (M151)
  // the gap ray count is not judged.
  // The pixel bar: taken, 0.39 to 0.74 of the rest place changed; left at rest, 0.02 to 0.22 (w1259). 0.3 between.
  const lost = narrow.filter(x => x.after !== 'taken' || !(x.moved >= 0.3) || (!noWalk && !(x.rays.on > 0)))
  check(`gapback: a press in a narrow gap of a tray at rest takes it, before and after takes and after a glide (${narrow.length} presses, ${lost.length} not taken, not moved by pixels, or no gap ray${noWalk ? '; no walk: rays not judged' : ''})`, narrow.length >= 4 && lost.length === 0, { lost: lost.map(x => [x.label, x.on, x.down, x.after, x.moved, x.rays]) })
  const taken = rows.filter(x => /^take|juno take/.test(x.label))
  // Judged on the taken form's own gaps: in Juno's take, Atlas stands at rest and its gaps are on.
  const takenGaps = x => (x.rays.juno ?? x.rays)
  if (noWalk) note('gapback: while taken, gaps with a ray', 'not judged: no walk on this build (M151)')
  else check('gapback: while a form is taken, none of its gaps has a ray', taken.length > 0 && taken.every(x => x.taken && takenGaps(x).on + takenGaps(x).off === 0), taken.map(x => [x.label, x.taken, x.rays]))
  const gl = rows.find(x => x.label === 'gliding')
  if (noWalk) note('gapback: while gliding, gaps with a ray', 'not judged: no walk on this build (M151)')
  else check('gapback: while the taken form glides, its gaps have no ray', !!gl && gl.gliding && gl.rays.on + gl.rays.off === 0, gl ?? null)
  check('gapback: a press on the taken form while it glides sends it back, by pixels too', !!gl && gl.gliding && gl.sent?.where === 'rest' && gl.sent.moved >= 0.15, gl ?? null)
  // The taken form's box at the press holds the page behind it too, which stays: 0.20 to 0.23 of it changed when the
  // form went (w1261-w1263, the still pair `*-glide-before.png` / `*-glide-after.png`), so its bar is 0.15.
  // All of a tray's gaps are one width, so they are on or off together: here every one must be off (none listed as
  // on), none of them may keep a ray (none listed as off), and the press is not dropped on the tray; by pixels its
  // rest place stays.
  const off = rows.filter(x => x.wide)
  check(`gapback: a wide gap (its mesh off) has no ray, and its press neither lights nor takes the tray by the gap${noWalk ? ' (no walk: rays not judged)' : ''}`, off.length === 1 && off[0].sep !== undefined && (noWalk || (off[0].rays.on === 0 && off[0].rays.off === 0)) && !(off[0].on === 'dash:atlas' && off[0].after !== 'taken') && (off[0].after === 'taken' || (off[0].moved ?? 1) < 0.3), off)
  console.log(`TOTAL bad ${bad}`)
  await got.browser.close()
  process.exit(bad ? 1 : 0)
}
// ONLY=grid (D149): Atlas's form taken, a press at each point of a grid over its box in the lens (5 across, 3 up),
// taken again before each. Every press inside the box is the form's (D114 2): it goes back. A press that reads
// `dash:atlas` at its down and its up and leaves the form taken is dropped: a switched-off gap mesh of the form, or
// another piece that is not drawn, took it.
if (ONLY === 'grid') {
  const cells = []
  for (const v of [0.2, 0.5, 0.8]) for (const u of [0.1, 0.3, 0.5, 0.7, 0.9]) {
    const t = (await dashOf(page)).atlas?.where === 'taken' ? { t: (await dashOf(page)).atlas, at: { ...head } } : await take('atlas')
    if (!t.t) { cells.push({ u, v, taken: false }); continue }
    await setHead(t.at.eye, t.at.yaw, t.at.pitch, 3)
    const L = (await dashOf(page)).atlas?.lens
    if (!L) { cells.push({ u, v, lens: null }); continue }
    const p = [L[0] + (L[2] - L[0]) * u, L[1] + (L[3] - L[1]) * v]
    await aimNdc(t.at.eye, { yaw: head.yaw, pitch: head.pitch }, [p[0] - 0.001, p[1] - 0.001, p[0] + 0.001, p[1] + 0.001], 2)
    const r = await read(page), on = r.pinch?.head?.on?.what ?? null, L2 = r.dash3d?.atlas?.lens ?? null
    const inBox = !!L2 && L2[0] < 0 && L2[2] > 0 && L2[1] < 0 && L2[3] > 0
    await pinch(); await emu.frames(page, 8)
    const last = (await read(page)).pinch?.last, after = (await dashOf(page)).atlas?.where
    cells.push({ u, v, inBox, on, down: last?.down, up: last?.up, kind: last?.kind, after })
    if (after === 'taken') { await setHead(t.at.eye, t.at.yaw, t.at.pitch, 3); const x = (await dashOf(page)).atlas; await lookAt([x.at[0], x.at[1] - 0.004, x.at[2]], t.at.eye); await pinch(); await emu.frames(page, 30) }
    await emu.frames(page, 20); await sleep(400)
  }
  writeFileSync(`${OUT}/${name}-grid.json`, JSON.stringify(cells, null, 1))
  note('grid', cells.map(c => [c.u, c.v, c.inBox, c.down, c.after]))
  const dropped = cells.filter(c => c.inBox && c.kind === 'click' && /dash:atlas/.test(c.down ?? '') && /dash:atlas/.test(c.up ?? '') && c.after === 'taken')
  const notForm = cells.filter(c => c.inBox && c.kind === 'click' && c.after === 'taken')
  check(`grid: every press inside the taken form's box sends it back (${cells.filter(c => c.inBox).length} presses in the box, ${dropped.length} dropped on the form, ${notForm.length} not the form's)`, cells.some(c => c.inBox) && notForm.length === 0, { dropped: dropped.map(c => [c.u, c.v]), notForm: notForm.map(c => [c.u, c.v, c.down]) })
  console.log(`TOTAL bad ${bad}`)
  await got.browser.close()
  process.exit(bad ? 1 : 0)
}
// ONLY=rays (D147 4): the probe's list of meshes that are not drawn and still have a ray, at rest, with Atlas's form
// taken, and after it went back (the re-take's case). Written whole to <name>-rays.json.
if (ONLY === 'rays') {
  const walk = () => page.evaluate(() => window.__xrHiddenRays?.() ?? null)
  const out = { rest: await walk() }
  const t = await take('atlas')
  out.taken = t.t ? await walk() : 'not taken'
  if (t.t) { const x = (await dashOf(page)).atlas; await lookAt([x.at[0], x.at[1] - 0.004, x.at[2]]); await pinch(); await emu.frames(page, 40); await sleep(800); await emu.frames(page, 10) }
  out.back = await walk()
  writeFileSync(`${OUT}/${name}-rays.json`, JSON.stringify(out, null, 1))
  for (const k of ['rest', 'taken', 'back']) note(`rays ${k}`, Array.isArray(out[k]) ? { count: out[k].length, first: out[k].slice(0, 4) } : out[k])
  check('rays: the probe walked the room', Array.isArray(out.rest), { rest: Array.isArray(out.rest) })
  console.log(`TOTAL bad ${bad}`)
  await got.browser.close()
  process.exit(bad ? 1 : 0)
}
// ONLY=take1 (D140 D): the take from the 19 degree eye, ten times: for each, the pinch's down and up as the build
// read them, and where the tray stands 6, 20 and 40 frames after; then it is sent back.
if (ONLY === 'take1') {
  const r0 = (await dashOf(page)).atlas
  const h = Math.hypot(r0.at[0], r0.at[2]) || 1, far = (EYE - r0.at[1]) / Math.tan(19 / DEG)
  const eye = [r0.at[0] - (r0.at[0] / h) * far, EYE, r0.at[2] - (r0.at[2] / h) * far]
  const rows = []
  for (let k = 0; k < 10; k += 1) {
    const r = (await dashOf(page)).atlas
    await lookAt([r.at[0], r.at[1] - 0.004, r.at[2]], eye)
    const on = await onWhat(), lit = (await dashOf(page)).atlas?.lit
    const seq0 = (await read(page)).pinch?.seq ?? null
    await emu.select(page, { hand: 'right', down: true }); await emu.frames(page, 1)
    const atDown = { on: await onWhat(), where: (await dashOf(page)).atlas?.where }
    await emu.select(page, { hand: 'right', down: false })
    const where = []
    for (const n of [6, 14, 20]) { await emu.frames(page, n); where.push((await dashOf(page)).atlas?.where) }
    const last = (await read(page)).pinch?.last
    rows.push({ k, on, lit, atDown, last: last && { down: last.down, up: last.up, kind: last.kind, ms: last.ms }, where, seq: [seq0, (await read(page)).pinch?.seq ?? null] })
    if ((await dashOf(page)).atlas?.where === 'taken') { const x = (await dashOf(page)).atlas; await lookAt([x.at[0], x.at[1] - 0.004, x.at[2]], eye); await pinch(); await emu.frames(page, 40); await sleep(500) }
  }
  writeFileSync(`${OUT}/${name}-take1.json`, JSON.stringify(rows, null, 1))
  const dropped = rows.filter(x => /dash:atlas/.test(x.last?.down ?? '') && /dash:atlas/.test(x.last?.up ?? '') && x.last?.kind === 'click' && !x.where.includes('taken'))
  note('take1 rows', rows.map(x => [x.k, x.on, x.lit, x.last?.down, x.last?.up, x.last?.kind, x.last?.ms, x.where.join('/')]))
  check(`take1: every click whose down and up were on Atlas's tray took it (${rows.length} tries, ${dropped.length} dropped)`, dropped.length === 0, { dropped: dropped.map(x => x.k) })
  console.log(`TOTAL bad ${bad}`)
  await got.browser.close()
  process.exit(bad ? 1 : 0)
}
if (ONLY === 'm115' && process.env.SOON === '1') {
  // Row 5 (M95, by a row): a press about 100 ms after the take's pinch, with Juno's tray behind the taken form's box
  // (the aim of w1192): it is the form's, which goes back; Juno's tray stays.
  // The take is found by the row's take; sent back; then made again from the same head, by hand, so the press can
  // follow the take's up by a few frames: the host draws a frame in some 60 to 300 ms, so the time is the page's own
  // (`performance.now` in the page) and the frames between. The head does not move: the press is where the dot is,
  // and it is the form's exactly when the dot lies in the box of what is drawn of the form now (D132 2, D137). The
  // aim at Juno's tray of w1193 to w1195 was outside the lens (w1200: Juno at x 4.7), so it judged nothing.
  const t = await take('atlas')
  if (!t.t) { check('row 5: Atlas\'s form is taken', false, { tries: t.tries }) } else {
    await setHead(t.at.eye, t.at.yaw, t.at.pitch); await pinch(); await emu.frames(page, 40); await sleep(500)
    await setHead(t.at.eye, t.at.yaw, t.at.pitch)
    const pnow = () => page.evaluate(() => performance.now())
    const GAP = Number(process.env.GAP_FRAMES ?? 0)
    await emu.select(page, { hand: 'right', down: true }); await emu.frames(page, 1)
    await emu.select(page, { hand: 'right', down: false }); const upAt = await pnow()
    // SOON_OUT=1 (B4): the other side. In the frame after the take his head turns 25 degrees up, so the dot lies
    // outside the form's box: the press is then not the form's.
    if (process.env.SOON_OUT === '1') await setHead(t.at.eye, t.at.yaw, t.at.pitch + 25, 1)
    await emu.frames(page, GAP + 1)
    // One read between the up and the press: each read takes the host some time (w1209: a still there made it 1.8 s).
    const r1 = await read(page), d1 = r1.dash3d, on = r1.pinch?.head?.on?.what ?? null, takeLast = r1.pinch?.last
    const lens = d1.atlas?.lens ?? null
    const inBox = !!lens && lens[0] < 0 && lens[2] > 0 && lens[1] < 0 && lens[3] > 0
    const downAt = await pnow()
    await emu.select(page, { hand: 'right', down: true }); await emu.frames(page, 1)
    await emu.select(page, { hand: 'right', down: false }); await emu.frames(page, 12)
    const last = (await read(page)).pinch?.last
    const d2 = await dashOf(page)
    const pressMs = Math.round(downAt - upAt)
    await shoot(`row5-after`)
    note('row 5 the press', { take: takeLast?.down, atlasAtPress: d1.atlas?.where, mix: d1.atlas?.fit?.mix ?? null, framesBetween: GAP + 1, pressMs, lens: lens?.map(v => +v.toFixed(3)), inBox, on, down: last?.down })
    const formHad = /dash:atlas/.test(last?.down ?? '') && d2.atlas?.where === 'rest'
    check(`row 5 (M95): Atlas's form is taken by the take's up`, /dash:atlas/.test(takeLast?.down ?? '') && d1.atlas?.where === 'taken', { take: takeLast?.down, atPress: d1.atlas?.where })
    check(`row 5 (M95): a press ${pressMs} ms (page clock, ${GAP + 1} frames) after the take's up, the form still coming (mix ${d1.atlas?.fit?.mix ?? '?'}): it is the form's exactly when the dot lies in the box of what is drawn now (in the box: ${inBox})`, formHad === inBox, { inBox, down: last?.down, atlas: d2.atlas?.where })
  }
  console.log(`TOTAL bad ${bad}`)
  await got.browser.close()
  process.exit(bad ? 1 : 0)
}
if (ONLY === 'm115') {
  const crewNow = async () => (await read(page)).crew
  const midOfBot = b => [b.feet[0], b.feet[1] + (b.top[1] - b.feet[1]) * 0.5, b.feet[2]]
  const cases = []
  // M115_KIND=tray, bot, lifted or out: one case a run (a second take of Atlas after a case did not come in w1191).
  const KINDS = ['tray', 'bot', 'lifted', 'page', 'half', 'out']
  const want = (process.env.M115_KIND ?? 'tray,bot').split(',')
  const done = new Set(KINDS.filter(k => !want.includes(k)))
  // The box the build presses by is the probe's `lens` (D137: only what the lens shows of the form). A box past 1.5
  // is no drawn box in the lens (w1192 read -39.8 to 88.2 on m113, with no form round the dot in its still).
  const lensOk = n => !!n && n.every(v => Number.isFinite(v) && Math.abs(v) <= 1.5)
  const inLens = (n, x, y, m = 0.04) => lensOk(n) && n[0] < x - m && n[2] > x + m && n[1] < y - m && n[3] > y + m
  const ndcAt = p => { const q = onScreen(p); return q ? [(q.x / AURA.width) * 2 - 1, 1 - (q.y / AURA.height) * 2] : null }
  for (let round = 0; round < 6 && done.size < KINDS.length; round += 1) {
    const back = await dashOf(page); for (const [s, x] of Object.entries(back)) if (x.where === 'taken' && s !== 'atlas') { await lookAt([x.at[0], x.at[1] - 0.004, x.at[2]]); await pinch(); await emu.frames(page, 30) }
    const t = (await dashOf(page)).atlas?.where === 'taken' ? { t: (await dashOf(page)).atlas, at: { ...head } } : await take('atlas')
    if (!t.t) { check('m115: Atlas\'s form is taken', false, { tries: t.tries }); break }
    const eye = t.at.eye, h0 = { yaw: t.at.yaw, pitch: t.at.pitch }
    await setHead(eye, h0.yaw, h0.pitch, 4)
    const d0 = await dashOf(page), L = d0.atlas?.lens ?? null
    const formM = Math.hypot(...d0.atlas.at.map((v, i) => v - eye[i]))
    const trays = Object.entries(d0).filter(([s, x]) => s !== 'atlas' && x.where === 'rest')
    // Points that the take's own picture shows inside the form's box and farther than the form: a turn of the head
    // onto one moves the form and the point alike, so the dot then lies on the form with the point behind it.
    const kinds = [
      ...(done.has('tray') ? [] : trays.filter(([, x]) => !x.hidden?.hidden).map(([s, x]) => ({ kind: 'tray', slug: s, p: [x.at[0], x.at[1] + 0.02, x.at[2]] }))),
      ...(done.has('lifted') ? [] : trays.filter(([, x]) => x.hidden?.hidden).map(([s, x]) => ({ kind: 'lifted', slug: s, p: [x.at[0], x.at[1] + 0.02, x.at[2]] }))),
      ...(done.has('bot') ? [] : (await crewNow()).filter(b => b.slug !== 'atlas' && !b.seat).flatMap(b => [0.5, 0.75, 0.25].map(f => ({ kind: 'bot', slug: b.slug, p: [b.feet[0], b.feet[1] + (b.top[1] - b.feet[1]) * f, b.feet[2]] })))),
    ].map(c => ({ ...c, at: ndcAt(c.p), behindM: Math.hypot(...c.p.map((v, i) => v - eye[i])) }))
    note('m115 the take\'s picture', { lens: L?.map(v => +v.toFixed(3)), formM: +formM.toFixed(2), candidates: kinds.map(c => [c.kind, c.slug, c.at?.map(v => +v.toFixed(2)), +c.behindM.toFixed(2)]) })
    // The other side (D137): the form half out of the lens keeps the box of its seen part, and a press on that part
    // sends it back. His head turns left until the form's right end leaves the lens.
    if (!done.has('half')) {
      done.add('half')
      let dh = null, turned = 0
      // Steps of 3 degrees: past the 2 degrees of a look at rest (`HELD_COME.restDeg`), so the form does not come again.
      for (let k = 3; k <= 45 && !dh; k += 3) {
        await setHead(eye, h0.yaw - k, h0.pitch, 1)
        const x = (await dashOf(page)).atlas
        if (x?.where === 'taken' && x.ndc?.[2] > 1.05 && x.lens && x.lens[2] === 1) { dh = x; turned = k }
      }
      if (!dh) { check('m115 half: the form half out of the lens', false, { turned }) } else {
        // M124: the dot in the AIR between two neighbouring parts that both lie wholly in the lens: a drawn piece takes
        // its own press with no box at all (`Dash3d.tsx` `drawn`), so only a press in the air judges the box.
        const ps = (dh.partsNdc ?? []).map((p, i) => ({ i, p })).filter(({ p }) => p && p.every(v => Number.isFinite(v) && Math.abs(v) < 0.98)).sort((u, w) => u.p[0] - w.p[0])
        let gap = null
        for (let k = 0; k + 1 < ps.length && !gap; k += 1) {
          const u = ps[k].p, w = ps[k + 1].p, y0 = Math.max(u[1], w[1]), y1 = Math.min(u[3], w[3])
          if (w[0] - u[2] > 0.02 && y1 > y0) gap = { between: [ps[k].i, ps[k + 1].i], x: (u[2] + w[0]) / 2, y: (y0 + y1) / 2 }
        }
        const n = gap ? [gap.x - 0.001, gap.y - 0.001, gap.x + 0.001, gap.y + 0.001] : null
        if (n) await aimNdc(eye, { yaw: head.yaw, pitch: head.pitch }, n, 2)
        const d1 = await dashOf(page), on = await onWhat()
        const inAir = !!gap && !(d1.atlas?.partsNdc ?? []).some(p => p && p[0] < 0 && p[2] > 0 && p[1] < 0 && p[3] > 0)
        note('m115 half, the air between two parts', { gap, inAir, partsAtDot: (d1.atlas?.partsNdc ?? []).map((p, i) => p && p[0] < 0 && p[2] > 0 && p[1] < 0 && p[3] > 0 ? i : null).filter(v => v !== null) })
        await shoot('m115-half')
        check(`m115 half: the head turned ${turned} degrees, the form's right end out of the lens: its box is its seen part (cut at 1)`, d1.atlas?.where === 'taken' && !!d1.atlas?.lens && d1.atlas.lens[2] === 1 && d1.atlas.ndc[2] > 1, { lens: d1.atlas?.lens?.map(v => +v.toFixed(3)), ndc: d1.atlas?.ndc?.map(v => +v.toFixed(2)), on })
        await pinch(); await emu.frames(page, 12)
        const last = (await read(page)).pinch?.last, d2 = await dashOf(page)
        check(`m115 half (M124): a press in the air between parts ${gap ? gap.between.join(' and ') : '?'}, inside the cut box, sends the form back`, inAir && /dash:atlas/.test(last?.down ?? '') && d2.atlas?.where === 'rest', { gap, inAir, down: last?.down, atlas: d2.atlas?.where })
        // The same pose with the form back, for the dot's crop with and without it.
        await emu.frames(page, 30); await sleep(400); await shoot('m115-half-without')
        note('m115 pixels', { with: `${name}-m115-half.png`, without: `${name}-m115-half-without.png`, kind: 'half' })
      }
      cases.push({ kind: 'half' })
      continue
    }
    // The other side (D137): a tray that the form is not in front of, taken while nothing of the form is in the lens.
    if (!done.has('out')) {
      done.add('out')
      const juno = d0.juno
      const a = aimOf([juno.at[0], juno.at[1] + 0.02, juno.at[2]], eye)
      await setHead(eye, a.yaw, a.pitch, 2)
      const d1 = await dashOf(page), on = await onWhat()
      await shoot('m115-out')
      check(`m115 out: his head turned to Juno's tray, Atlas's form has nothing in the lens and no box`, d1.atlas?.where === 'taken' && d1.atlas?.lens === null, { lens: d1.atlas?.lens, ndc: d1.atlas?.ndc?.map(v => +v.toFixed(2)), on })
      const t2 = /dash:juno/.test(on ?? '') ? (await pinch(), { tries: [{ at: 'aim' }] }) : await take('juno')
      await emu.frames(page, 12)
      const d2 = await dashOf(page)
      check(`m115 out: then a press on Juno's tray takes it at once, and Atlas's form goes back`, d2.juno?.where === 'taken' && d2.atlas?.where === 'rest', { on, down: (await read(page)).pinch?.last?.down, juno: d2.juno?.where, atlas: d2.atlas?.where, tries: t2.tries?.length })
      cases.push({ kind: 'out' })
      continue
    }
    // The dot is set on the middle of one of the form's two number blocks (parts 0 and 4 of SIX: solid; a bar's glass
    // is drawn at a fifth and a ring has a hole, w1203). What stands behind it on the dot's line is worked out by this
    // row from the probe: a Bot's column, a tray's box, a board's body, the nearest past the form. From the take's eye
    // the form stands under the Bots (w1200), so the eye is also lowered, raised and stepped aside.
    const dirOf = () => { const y = head.yaw / DEG, t = head.pitch / DEG; return [Math.sin(y) * Math.cos(t), Math.sin(t), -Math.cos(y) * Math.cos(t)] }
    const behindOn = async (e, fM) => {
      const dv = dirOf(), out = []
      for (const bb of (await crewNow()).filter(x => x.slug !== 'atlas' && !x.seat)) {
        const px = bb.feet[0] - e[0], pz = bb.feet[2] - e[2], fl = dv[0] * dv[0] + dv[2] * dv[2]
        const tt = (px * dv[0] + pz * dv[2]) / fl, cx = e[0] + dv[0] * tt - bb.feet[0], cz = e[2] + dv[2] * tt - bb.feet[2], yy = e[1] + dv[1] * tt
        if (tt > fM + 0.1 && cx * cx + cz * cz < (bb.widthM / 2) ** 2 && yy > bb.feet[1] && yy < bb.top[1]) out.push({ kind: 'bot', slug: bb.slug, t: tt })
      }
      for (const [sl, x] of Object.entries(await dashOf(page))) {
        if (sl === 'atlas' || x.where !== 'rest' || !x.box) continue
        let t0 = 0, t1 = Infinity
        for (let k = 0; k < 3; k += 1) {
          const lo = (x.box[0][k] - e[k]) / dv[k], hi = (x.box[1][k] - e[k]) / dv[k]
          t0 = Math.max(t0, Math.min(lo, hi)); t1 = Math.min(t1, Math.max(lo, hi))
        }
        if (t0 < t1 && t0 > fM + 0.1) out.push({ kind: x.hidden?.hidden ? 'lifted' : 'tray', slug: sl, t: t0 })
      }
      for (const [id, m] of Object.entries(await page.evaluate(() => window.__xrPages.read().boardMarks ?? {}))) {
        const q = m.body.map((v, i) => v - e[i]), tt = q[0] * dv[0] + q[1] * dv[1] + q[2] * dv[2]
        const miss = Math.hypot(q[0] - dv[0] * tt, q[1] - dv[1] * tt, q[2] - dv[2] * tt)
        if (tt > fM + 0.1 && miss < Math.min(...m.sizeM) / 2) out.push({ kind: 'page', slug: id, t: tt })
      }
      return out.sort((p1, p2) => p1.t - p2.t)[0] ?? null
    }
    let hit = null, eyeAt = eye, a = null
    const moves = [[0, 0]]
    for (const dx of [0, -0.2, 0.2, -0.4, 0.4]) for (const dy of [0.1, -0.1, 0.2, -0.2, 0.3, -0.3, 0.4, -0.4]) moves.push([dx, dy])
    const seenBehind = []
    for (const [dx, dy] of moves) {
      if (hit) break
      const e2 = [eye[0] + dx, eye[1] - dy, eye[2]]
      await setHead(e2, h0.yaw, h0.pitch, 3)
      const dn = await dashOf(page)
      if (dn.atlas?.where !== 'taken') break
      const fM = Math.hypot(...dn.atlas.at.map((v, i) => v - e2[i]))
      const hh = { yaw: head.yaw, pitch: head.pitch }
      for (const i of [0, 4]) {
        const n = dn.atlas?.partsNdc?.[i]
        if (!n || !n.every(v => Number.isFinite(v) && Math.abs(v) < 0.95)) continue
        await aimNdc(e2, hh, n, 2)
        if (!/dash:atlas/.test((await onWhat()) ?? '')) continue
        const b = await behindOn(e2, fM)
        seenBehind.push([dx, dy, i, b ? `${b.kind}:${b.slug}` : null])
        if (b && !done.has(b.kind)) { hit = { ...b, block: i }; eyeAt = e2; a = { yaw: head.yaw, pitch: head.pitch }; break }
      }
    }
    note('m115 what stands behind the number blocks', { seen: seenBehind.slice(0, 60) })
    // Nothing there (w1206: the dot's line through the blocks runs under the Bots): the eye is set on the line from a
    // thing of a wanted kind through a block's middle, as far from the block as the take's eye was.
    if (!hit) {
      await setHead(eye, h0.yaw, h0.pitch, 3)
      const dn = await dashOf(page)
      const fM = Math.hypot(...dn.atlas.at.map((v, i) => v - eye[i]))
      const blocks = []
      for (const i of [0, 4]) {
        // The block's middle in the room, from the probe (`partsAt`); on a build without it, on the dot's line at the form's distance.
        if (dn.atlas?.partsAt?.[i]) { blocks.push({ i, F: dn.atlas.partsAt[i] }); continue }
        const n = dn.atlas?.partsNdc?.[i]
        if (!n || !n.every(v => Number.isFinite(v) && Math.abs(v) < 0.95)) continue
        await aimNdc(eye, h0, n, 2)
        const dv = dirOf(); blocks.push({ i, F: eye.map((v, k) => v + dv[k] * fM) })
      }
      const tn = Object.entries(dn).filter(([s, x]) => s !== 'atlas' && x.where === 'rest')
      const bm = await page.evaluate(() => window.__xrPages.read().boardMarks ?? {})
      const targets = [
        // A tray's box middle, and its top's middle a little over it: from a low eye the line to the box middle meets its
        // own Bot's body first (w1225: behind was `bot:juno`).
        ...(done.has('tray') ? [] : tn.filter(([, x]) => !x.hidden?.hidden).flatMap(([, x]) => [...(x.box ? [[(x.box[0][0] + x.box[1][0]) / 2, x.box[0][1] + (x.box[1][1] - x.box[0][1]) * 0.15, (x.box[0][2] + x.box[1][2]) / 2], [(x.box[0][0] + x.box[1][0]) / 2, (x.box[0][1] + x.box[1][1]) / 2, (x.box[0][2] + x.box[1][2]) / 2]] : []), [x.at[0], x.at[1] + 0.01, x.at[2]]])),
        // A lifted tray (M72's own case): where a tray stands when a nearer Bot hides it, over its own Bot's head
        // (`REST_CLEAR_M` 0.03); the eye on the line through it is then checked for the lift itself (below).
        ...(done.has('lifted') ? [] : await Promise.all(tn.map(async ([sl, x]) => { const o = (await crewNow()).find(bb => bb.slug === sl); return o ? [x.at[0], o.top[1] + 0.05, x.at[2]] : x.at }))),
        ...(done.has('bot') ? [] : (await crewNow()).filter(bb => bb.slug !== 'atlas' && !bb.seat).map(bb => [bb.feet[0], (bb.feet[1] + bb.top[1]) / 2, bb.feet[2]])),
        ...(done.has('page') ? [] : Object.values(bm).map(m => m.body)),
      ]
      // On the same line at a few distances from the block: the thing stays behind the block, and the eye's height with
      // it (w1230: at the form's distance the eye stood at 0.6 to 0.7 m and the dot read `room`).
      for (const T of targets) for (const { i: bi, F } of blocks) for (const kd of [1, 0.75, 1.3, 1.6]) {
        if (hit) break
        const u = F.map((v, k) => v - T[k]), ul = Math.hypot(...u)
        const e2 = F.map((v, k) => v + (u[k] / ul) * fM * kd)
        if (e2[1] < 0.6 || e2[1] > 2.3) { seenBehind.push(['line', e2.map(v => +v.toFixed(2)), 'eye out of 0.6 to 2.3 m']); continue }
        const aim = aimOf(F, e2)
        await setHead(e2, aim.yaw, aim.pitch, 3)
        // The block's middle was found at the form's distance, not its own (M123: the dot then stood 56 px off it, at
        // its lower left corner): the dot is set on the block's drawn middle as the build pictures it from here.
        const nb = (await dashOf(page)).atlas?.partsNdc?.[bi]
        if (nb && nb.every(v => Number.isFinite(v) && Math.abs(v) < 0.98)) await aimNdc(e2, { yaw: head.yaw, pitch: head.pitch }, nb, 3)
        // A tray lifts only once the build's answer has held 300 ms (`HIDDEN_SLACK.holdMs`): wait past it.
        if (!done.has('lifted')) { await sleep(500); await emu.frames(page, 4) }
        const dd = await dashOf(page)
        if (dd.atlas?.where !== 'taken' || !/dash:atlas/.test((await onWhat()) ?? '')) { seenBehind.push(['line', e2.map(v => +v.toFixed(2)), (await onWhat()) ?? null]); continue }
        const b = await behindOn(e2, Math.hypot(...dd.atlas.at.map((v, i) => v - e2[i])))
        seenBehind.push(['line', e2.map(v => +v.toFixed(2)), b ? `${b.kind}:${b.slug}` : null])
        if (b && !done.has(b.kind)) { hit = { ...b, block: bi }; eyeAt = e2; a = { yaw: head.yaw, pitch: head.pitch } }
      }
      note('m115 the eye on a line through a block', { seen: seenBehind.filter(x => x[0] === 'line').slice(0, 30) })
    }
    if (!hit) { note('m115 nothing wanted behind a drawn part', { want: want.filter(k => !done.has(k)) }); break }
    hit.at = [0, 0]; hit.behindM = hit.t
    note('m115 the eye moved', { eye: eyeAt.map(v => +v.toFixed(2)), hit: [hit.kind, hit.slug, +hit.t.toFixed(2)] })
    done.add(hit.kind)
    await setHead(eyeAt, a.yaw, a.pitch, 2)
    const on = await onWhat()
    const d1 = await dashOf(page), c1 = await crewNow(), b1 = await page.evaluate(() => (window.__xrPages.read().boards?.boards ?? []).map(x => `${x.needId ?? x.bot}:${x.kind}:${x.where}`))
    await shoot(`m115-${hit.kind}-with`)
    // Which boards stand where, against the form, at this pose: a board drawn over a nearer form (w1202) is read here.
    const bm = await page.evaluate(() => window.__xrPages.read().boardMarks ?? {})
    note('m115 boards at the press', { formM: +Math.hypot(...d1.atlas.at.map((v, i) => v - eyeAt[i])).toFixed(2), boards: Object.entries(bm).map(([id, m]) => ({ id, distM: +Math.hypot(...m.body.map((v, i) => v - eyeAt[i])).toFixed(2), ndc: m.ndc?.body?.map(v => +v.toFixed(2)), front: m.front, desk: !!m.desk })) })
    const lens1 = d1.atlas?.lens ?? null
    // M123: which pieces of the form the dot's line meets, and how far the dot is from the aimed block's drawn middle,
    // in pixels of the picture. On the piece: inside the middle half of its drawn box.
    const pieces = (d1.atlas?.partsNdc ?? []).map((n, i) => ({ i, n })).filter(({ n }) => n && n.every(Number.isFinite) && n[0] < 0 && n[2] > 0 && n[1] < 0 && n[3] > 0)
    const blk = d1.atlas?.partsNdc?.[hit.block]
    const offPx = blk ? [Math.round(((blk[0] + blk[2]) / 2) * AURA.width / 2), Math.round(((blk[1] + blk[3]) / 2) * AURA.height / 2)] : null
    const halfPx = blk ? [Math.round(((blk[2] - blk[0]) / 2) * AURA.width / 2), Math.round(((blk[3] - blk[1]) / 2) * AURA.height / 2)] : null
    const onPiece = !!blk && Math.abs(offPx[0]) <= halfPx[0] / 2 && Math.abs(offPx[1]) <= halfPx[1] / 2
    note('m115 the piece at the dot', { block: hit.block, lineMeets: pieces.map(p => p.i), offPx, halfPx })
    check(`m115 piece (${hit.kind}): ${onPiece ? 'the dot is on' : 'the dot is not on the piece:'} number block ${hit.block}, ${offPx ? Math.round(Math.hypot(...offPx)) : '?'} px from its drawn middle (half size ${halfPx ? halfPx.join(' x ') : '?'} px)`, onPiece, { offPx, halfPx, lineMeets: pieces.map(p => p.i) })
    check(`m115 box (${hit.kind}): the taken form's box is what the lens shows, and the dot lies inside it`, d1.atlas?.where === 'taken' && inLens(lens1, 0, 0, 0.01), { lens: lens1?.map(v => +v.toFixed(3)), ndc: d1.atlas?.ndc?.map(v => +v.toFixed(2)), on })
    await pinch(); await emu.frames(page, 12)
    const last1 = (await read(page)).pinch?.last
    const d2 = await dashOf(page), c2 = await crewNow(), b2 = await page.evaluate(() => (window.__xrPages.read().boards?.boards ?? []).map(x => `${x.needId ?? x.bot}:${x.kind}:${x.where}`))
    // The same pose without the form: for the pixels (`m115-pixels.py`: the dot's patch must change, so the dot lay
    // on a drawn form), and for what was behind: what the dot lies on now.
    await emu.frames(page, 30); await sleep(400); await setHead(eyeAt, a.yaw, a.pitch, 6)
    await shoot(`m115-${hit.kind}-without`)
    note('m115 pixels', { with: `${name}-m115-${hit.kind}-with.png`, without: `${name}-m115-${hit.kind}-without.png`, kind: hit.kind })
    // A Bot's body reads `room` to the probe's `on` (its meshes carry no mark): the Bot that lights there is what is behind.
    const onBack = await onWhat(), litBot = (await crewNow()).find(x => x.lit && x.slug !== 'atlas')
    const behind = onBack === 'room' && litBot ? `bot:${litBot.slug}` : onBack
    const slugOf = w => (w ?? '').replace(/^(dash|bot|crew):/, '')
    const behindBot = /^(bot|crew)/.test(behind ?? '') ? slugOf(behind) : null, behindDash = /^dash/.test(behind ?? '') ? slugOf(behind) : null
    const behindLit = behindBot ? c1.find(x => x.slug === behindBot)?.lit === true : behindDash ? d1[behindDash]?.lit === true : null
    check(`m115 light (${hit.kind}): with the dot on Atlas's taken form, the form lights and what is behind it (${behind}) does not`, d1.atlas?.lit === true && behindLit !== true && /dash:atlas/.test(on ?? ''), { aimedAt: [hit.kind, hit.slug], on, behind, formLit: d1.atlas?.lit, behindLit: behindLit ?? 'a board: its light is not read' })
    const untouched = (behindBot ? c2.find(x => x.slug === behindBot)?.taps === c1.find(x => x.slug === behindBot)?.taps : true) && (behindDash ? d2[behindDash]?.where === d1[behindDash]?.where : true) && b2.join() === b1.join()
    check(`m115 press (${hit.kind}): a pinch there sends Atlas's form back, and what was behind (${behind}) is untouched`, d2.atlas?.where === 'rest' && untouched && /dash:atlas/.test(last1?.down ?? ''), { down: last1?.down, atlas: d2.atlas?.where, boards: [b1, b2] })
    // Then what was behind is what a press is on, at the same aim.
    const t2 = behindDash && behindDash !== 'atlas' ? await take(behindDash) : null
    if (!t2) { await setHead(eyeAt, a.yaw, a.pitch, 2); await pinch(); await emu.frames(page, 12) }
    const last2 = (await read(page)).pinch?.last
    const d3 = await dashOf(page)
    const tapsNow = behindBot ? (await crewNow()).find(x => x.slug === behindBot)?.taps ?? 0 : 0, tapsBefore = behindBot ? c2.find(x => x.slug === behindBot)?.taps ?? 0 : 0
    const pressed = behind && behind !== 'room' && behind !== 'nothing' && (behindDash ? d3[behindDash]?.where === 'taken' : behindBot ? tapsNow > tapsBefore : (last2?.down ?? '') === behind)
    // M140: a Bot's body reads `room` to the probe, so a Bot behind is judged by its own count of taps, which the
    // press must raise (empty air raises none).
    check(`m115 second press (${hit.kind}): then a press there is what was behind (${behind})${behindBot ? `: ${behindBot}'s taps ${tapsBefore} -> ${tapsNow}` : ''}`, !!pressed, { down: last2?.down, behind, taps: behindBot ? [tapsBefore, tapsNow] : null, tries: t2?.tries?.length ?? null })
    cases.push({ kind: hit.kind, slug: hit.slug, behind })
  }
  note('m115 cases', { cases, notFound: want.filter(k => !done.has(k)) })
  if (want.some(k => !cases.some(c => c.kind === k))) check(`m115: every case asked (${want.join(', ')}) was found`, false, { found: cases.map(c => c.kind) })
  console.log(`TOTAL bad ${bad}`)
  await got.browser.close()
  process.exit(bad ? 1 : 0)
}
// ONLY=takes (M100): only the first take of Mira's tray and of Atlas's, each try written out, then the end.
if (ONLY === 'takes') {
  for (const slug of ['mira', 'atlas']) {
    const before = (await dashOf(page))[slug]
    const bot = (await read(page)).crew.find(b => b.slug === slug)
    note(`${slug} before the take`, { at: before?.at, ndc: before?.ndc, hidden: before?.hidden, feet: bot?.feet, act: bot?.act, walking: bot?.walking, state: bot?.state })
    const { t, tries } = await take(slug, slug === 'atlas' ? 19 : null)
    check(`takes: a pinch with the dot on ${slug}'s tray takes it`, t?.where === 'taken', { tries })
    if (t) break
  }
}
/**
 * D173 3: Mira's take sets its own state first. The eye stands 1.2 m before her tray on its short axis (as `take`'s
 * `front`), looking at it; every page that stands in the lens from there is closed by the probe (`__xrPages.close`),
 * so no page lies over the lens's four sides that the pixel checks read. Returns the pose and what it closed.
 */
// Atlas's the same way (w1288: both runs' `atlas-at-take` read 4.4 % changed on the right, where a turned page
// that the page box does not cover stood over the lens's edge): its eye is `take`'s 19 degree one.
const takePose = async slug => {
  const r = (await dashOf(page))[slug]
  let eye
  if (slug === 'atlas') {
    const h = Math.hypot(r.at[0], r.at[2]) || 1, far = (EYE - r.at[1]) / Math.tan(19 / DEG)
    eye = [r.at[0] - (r.at[0] / h) * far, EYE, r.at[2] - (r.at[2] / h) * far]
  } else {
    const [a, b] = r.box; const dx = b[0] - a[0], dz = b[2] - a[2]; const n = dx < dz ? [1, 0] : [0, 1]
    const s = Math.sign(-(n[0] * r.at[0] + n[1] * r.at[2])) || 1
    eye = [r.at[0] + n[0] * s * 1.2, EYE, r.at[2] + n[1] * s * 1.2]
  }
  await lookAt([r.at[0], r.at[1] - 0.004, r.at[2]], eye)
  // A page is in the lens when any of its four corners, or its middle, lies in it, by this row's camera. Its corners
  // are taken one and a half times its size from its middle in every way it could face: the probe gives no turn, and a
  // turned page reaches past the box of its middle and size (w1288).
  const inLens = q => !!q && q.x >= 0 && q.x <= AURA.width && q.y >= 0 && q.y <= AURA.height
  const reaches = p => {
    const r2 = Math.max(p.size[0], p.size[1]) * 0.75
    const pts = [p.position]
    for (const dx of [-r2, r2]) for (const dy of [-r2, r2]) for (const dz of [-r2, r2]) pts.push([p.position[0] + dx, p.position[1] + dy, p.position[2] + dz])
    return pts.some(q => inLens(onScreen(q)))
  }
  const closed = ((await read(page)).panels ?? []).filter(reaches).map(p => p.id)
  for (const id of closed) await page.evaluate(x => window.__xrPages.close(x), id)
  if (!closed.length) return { eye: eye.map(v => +v.toFixed(3)), yaw: +head.yaw.toFixed(1), pitch: +head.pitch.toFixed(1), closed, pagesInLens: 0 }
  // A closed page lays the room's places out again, and its Bot and tray walk there (w1289: Atlas's eye, read before
  // the close, then looked 76 degrees off). So: wait until the tray stands still for a second, then read the pose again.
  let last = null, still = 0
  for (let i = 0; i < 40 && still < 5; i += 1) {
    await emu.frames(page, 6); await sleep(200)
    const at = (await dashOf(page))[slug]?.at
    still = last && at && Math.hypot(at[0] - last[0], at[1] - last[1], at[2] - last[2]) < 0.002 ? still + 1 : 0
    last = at
  }
  const again = await takePose(slug)
  return { ...again, closed: [...closed, ...again.closed], settledReads: still }
}
/**
 * How much of each lens side the pixel checks could judge from the head as it is now, by this row's camera: the
 * share of the side's 4 columns over the form's rows (the build's drawn box `ndc`) that no Bot's and no page's box
 * covers, at the first band of 4 columns, at most 96 px in, with a quarter clear (as `judgeSides` walks in).
 */
const sidesAt = async ndc => {
  {
    if (!ndc || !ndc.every(Number.isFinite)) return null
    const form = ndcToPx(ndc)
    const skip = [...(await botBoxes()), ...(await pageBoxes())]
    const y0 = Math.max(0, Math.round(form.y0)), y1 = Math.min(AURA.height, Math.round(form.y1))
    const share = x0 => { let n = 0, out = 0; for (let y = y0; y < y1; y += 1) for (let x = x0; x < x0 + 4; x += 1) { n += 1; if (skip.some(r => x >= r.x0 && x <= r.x1 && y >= r.y0 && y <= r.y1)) out += 1 } return n ? (n - out) / n : 0 }
    const side = (at, step) => { for (let k = 0; k <= 24; k += 1) { const s = share(at + step * 4 * k); if (s >= 0.25) return { clear: +s.toFixed(3), inPx: 4 * k } } return { clear: +share(at).toFixed(3), inPx: null } }
    // M206: the lens's top and foot 4 rows across the form's width, the Bots' places left out (as `judge` reads them).
    const bots = await botBoxes()
    const x0 = Math.max(0, Math.round(form.x0)), x1 = Math.min(AURA.width, Math.round(form.x1))
    const row = r0 => { let n = 0, out = 0; for (let y = r0; y < r0 + 4; y += 1) for (let x = x0; x < x1; x += 1) { n += 1; if (bots.some(r => x >= r.x0 && x <= r.x1 && y >= r.y0 && y <= r.y1)) out += 1 } return n ? +((n - out) / n).toFixed(3) : 0 }
    return { left: side(0, 1), right: side(AURA.width - 4, -1), top: row(0), foot: row(AURA.height - 4) }
  }
}
/** A taken form sent back by a pinch on its drawn box, as the row's own send-back does; whether it rests again. */
const sendBack = async slug => {
  for (const [fx, fy] of [[0.5, 0.5], [0.5, 0.3], [0.35, 0.4], [0.65, 0.4], [0.5, 0.7], [0.25, 0.5], [0.75, 0.5]]) {
    const w = (await dashOf(page))[slug]
    if (w?.where !== 'taken') break
    const nd = w.ndc; const tx = Math.tan(AURA.fovxDeg / 2 / DEG); const ty = Math.tan(AURA.fovyDeg / 2 / DEG)
    await setHead(head.eye, head.yaw + Math.atan((nd[0] + (nd[2] - nd[0]) * fx) * tx) * DEG, head.pitch + Math.atan((nd[1] + (nd[3] - nd[1]) * fy) * ty) * DEG)
    if (!(await dashOf(page))[slug]?.lit) continue
    await pinch(); await emu.frames(page, 40)
  }
  return !!(await waitUntil(async () => { await emu.frames(page, 2); const x = (await dashOf(page))[slug]; return x?.where === 'rest' && !x.gliding ? x : null }, 4000, 100))
}
/**
 * D173 (11:45): Atlas's 19 degree take from the first eye that leaves every lens side a quarter clear of the pages'
 * and the Bots' boxes, at all four poses the pixel checks read (the take's pitch, level, 9 up, 9 down). The eye keeps
 * its distance and its 19 degrees and steps round the tray from the room-middle line: 0, then 10, 20 and 30 degrees
 * each way. A step's take is a real one; a step that is not clear sends its form back before the next. Nothing is
 * closed and nothing in the room is moved. No clear step leaves the take red: never the old eye, never less judged.
 */
// M206 (D185 D): the same for Mira, whose eye stands 1.2 m before her tray on its short axis (`takePose`), and both
// count the lens's top and foot rows too: a step is clear only when those rows are a quarter clear of the Bots' places.
const stepTake = async (slug, base) => {
  const r = (await dashOf(page))[slug]
  const { u, fars, downDeg } = base(r)
  const steps = []
  for (const far of fars) for (const s of [0, 10, -10, 20, -20, 30, -30]) {
    const c = Math.cos(s / DEG), sn = Math.sin(s / DEG)
    const eye = [r.at[0] + (u[0] * c - u[1] * sn) * far, EYE, r.at[2] + (u[0] * sn + u[1] * c) * far]
    const tk = await take(slug, downDeg, eye)
    const step = { stepDeg: s, farM: +far.toFixed(2), eye: eye.map(v => +v.toFixed(3)), taken: tk.t?.where === 'taken', pinches: tk.tries.filter(x => x.last).length, eyesTried: tk.tries.length }
    steps.push(step)
    if (!step.taken) { note(`${slug}: no take from the eye ${s} degrees round, ${far.toFixed(2)} m off`, { ...step, tries: tk.tries }); continue }
    // The four poses walked for real, as the checks will walk them: at level the form may be placed again (w1293-j:
    // its box at the take, reckoned at 9 up, lay out of the lens), so each pose's box is the build's own there.
    const sides = []
    for (const p of [tk.at.pitch, 0, 9, -9]) {
      await setHead(tk.at.eye, tk.at.yaw, p, 2)
      await settled(slug, 0, 6000); await sleep(p === 0 ? 1500 : 900)
      sides.push({ pitch: +p.toFixed(1), ...(await sidesAt((await dashOf(page))[slug]?.ndc)) })
    }
    step.sides = sides
    step.clear = sides.every(x => x.left && x.right && x.left.inPx !== null && x.right.inPx !== null && x.top >= 0.25 && x.foot >= 0.25)
    note(`${slug}: the eye ${s} degrees round, ${far.toFixed(2)} m off, each pose's sides, top and foot by the row's camera`, step)
    // Sent back from his head level, where the walk left the form whole; once more from the take's own pitch (w1301:
    // from 9 down Mira's form did not go back, and every check after it read a room with her form still taken).
    await setHead(tk.at.eye, tk.at.yaw, 0, 2); await settled(slug, 0, 4000)
    step.sentBack = await sendBack(slug)
    if (!step.sentBack) { await setHead(tk.at.eye, tk.at.yaw, tk.at.pitch, 2); await sleep(900); step.sentBack = await sendBack(slug) }
    if (!step.sentBack) return { t: null, tries: tk.tries, steps, chosen: null, why: `the form taken from the eye ${s} degrees round did not go back` }
    if (!step.clear) continue
    // The checks start from a take of their own at the chosen eye: the walk above moved the form at level.
    await sleep(600)
    const real = await take(slug, downDeg, eye)
    step.again = { taken: real.t?.where === 'taken', pinches: real.tries.filter(x => x.last).length, eyesTried: real.tries.length }
    return { ...real, steps, chosen: step, ...(real.t ? {} : { why: 'the take again from the chosen eye took nothing' }) }
  }
  return { t: null, tries: [], steps, chosen: null, why: 'no step leaves every side, the top and the foot a quarter clear' }
}
/** Atlas: 19 degrees down on his low tray, on the line from it to the room's middle (`take`'s `at19`). */
const atlasBase = r => { const h = Math.hypot(r.at[0], r.at[2]) || 1; return { u: [-r.at[0] / h, -r.at[2] / h], fars: [(EYE - r.at[1]) / Math.tan(19 / DEG)], downDeg: 19 } }
// D188 1: Mira's eye is not stepped (her own Bot stands behind her taken form from every eye before it): her take is
// `takePose`'s, and a top or foot row her Bot covers is written not judged (`OWN_BOT`).
/**
 * ONLY=traypress (D188 2): make the press on a tray that takes nothing happen. From the eyes where it came (Mira 1.2 m
 * before her tray, stepped -20 and -30 degrees round it; Atlas 19 degrees down, the same steps), take the tray and
 * send it back, TRIES times each. Every pinch is logged with the build's own lines (`pinch`). A miss is a pinch whose
 * dot read the tray at both ends, as a click, after which the build called no `press` for that tray.
 */
if (ONLY === 'traypress') {
  const tries = Number(process.env.TRIES ?? 12)
  const steps = (process.env.STEPS ?? '-20,-30').split(',').map(Number)
  for (const slug of (process.env.BOTS ?? 'mira').split(',')) {
    if (slug === 'mira') note('mira: the pose the row set before', await takePose('mira'))
    const r = (await dashOf(page))[slug]
    if (!r) { check(`traypress: ${slug} has a tray`, false, {}); continue }
    const [a, b] = r.box; const n = b[0] - a[0] < b[2] - a[2] ? [1, 0] : [0, 1]; const sg = Math.sign(-(n[0] * r.at[0] + n[1] * r.at[2])) || 1
    const base = slug === 'atlas' ? atlasBase(r) : { u: [n[0] * sg, n[1] * sg], fars: [1.2], downDeg: null }
    for (const st of steps) {
      const c = Math.cos(st / DEG), sn = Math.sin(st / DEG), far = base.fars[0]
      const eye = [r.at[0] + (base.u[0] * c - base.u[1] * sn) * far, EYE, r.at[2] + (base.u[0] * sn + base.u[1] * c) * far]
      // SAMPLE=1: before the tries, the dot held on the tray as `take` first aims it (its `at`, 4 mm down), 40 frames
      // with no pinch: what the dot lies on and where the tray stands each frame.
      if (process.env.SAMPLE === '1') {
        const r0 = (await dashOf(page))[slug]
        await lookAt([r0.at[0], r0.at[1] - 0.004, r0.at[2]], eye)
        const seen = []
        for (let i = 0; i < 40; i += 1) { await emu.frames(page, 1); const rr = await read(page); const d = rr.dash3d?.[slug]; seen.push({ ms: Math.round(await page.evaluate(() => performance.now())), on: rr.pinch?.head?.on?.what ?? null, at: d?.at?.map(v => +v.toFixed(4)), ndc: d?.ndc?.map(v => +v.toFixed(3)), lit: d?.lit }) }
        writeFileSync(`${OUT}/${name}-${slug}-${st}-still.json`, JSON.stringify(seen, null, 1))
        note(`traypress ${slug} step ${st}: the dot held on the resting tray, 40 frames`, { on: [...new Set(seen.map(x => x.on))], ats: new Set(seen.map(x => JSON.stringify(x.at))).size, file: `${name}-${slug}-${st}-still.json` })
      }
      // SAMPLE=2 (D192 1, by pixels): the dot held on the resting tray where it lights, 40 frames, a crop of the lens
      // round the tray each frame, and the trays' own lines (a leave of the dot's pointer?); then the head turned 12
      // degrees off it and 40 frames more. The crops are measured by `tray-rows.py`.
      if (process.env.SAMPLE === '2') {
        // The quiet time after `takePose`'s closes lights nothing (`pinchRoute.quiet`): wait it out first (w1310: the dot
        // read nothing for its first frames).
        await waitUntil(async () => { await emu.frames(page, 2); return ((await read(page)).pinch?.quietMs ?? 0) === 0 ? true : null }, 10000, 100)
        const r0 = (await dashOf(page))[slug]
        let on = null
        for (const q of [[r0.at[0], r0.at[1] - 0.004, r0.at[2]], mid(r0.box), [r0.at[0], r0.at[1] + 0.004, r0.at[2]], null]) {
          if (q) await lookAt(q, eye)
          else { const h1 = { yaw: head.yaw, pitch: head.pitch }; const n0 = (await dashOf(page))[slug]?.ndc; if (n0) await aimNdc(eye, h1, n0) }
          await emu.frames(page, 6)
          const rr = await read(page)
          on = rr.pinch?.head?.on?.what ?? null
          if (on === `dash:${slug}` && rr.dash3d?.[slug]?.lit) break
        }
        const h0 = { eye: head.eye, yaw: head.yaw, pitch: head.pitch }
        const clipOf = nd => { const b = ndcToPx(nd); const x = Math.max(0, Math.round(b.x0) - 60), y = Math.max(0, Math.round(b.y0) - 60); return { x, y, width: Math.min(AURA.width - x, Math.round(b.x1 - b.x0) + 120), height: Math.min(AURA.height - y, Math.round(b.y1 - b.y0) + 120) } }
        const runOf = async (tag, clip) => {
          const t0 = await page.evaluate(() => performance.now())
          const rows = []
          for (let i = 0; i < 40; i += 1) {
            await emu.frames(page, 1)
            const rr = await read(page); const d = rr.dash3d?.[slug]
            const file = `${name}-${slug}${st}-${tag}-${String(i).padStart(2, '0')}.png`
            await page.screenshot({ path: `${OUT}/${file}`, clip })
            rows.push({ i, file, on: rr.pinch?.head?.on?.what ?? null, at: d?.at?.map(v => +v.toFixed(4)), ndc: d?.ndc?.map(v => +v.toFixed(4)), lit: d?.lit })
          }
          const lines = await page.evaluate(t => (window.__xrDashWork?.presses ?? []).filter(x => x.ms >= t), t0)
          return { rows, clip, leaves: lines.filter(x => x.ev === 'leave' && x.slug === slug).length, lines }
        }
        const onRun = await runOf('on', clipOf((await dashOf(page))[slug].ndc))
        await setHead(h0.eye, h0.yaw + 12, h0.pitch, 2); await emu.frames(page, 4)
        const offRun = await runOf('off', clipOf((await dashOf(page))[slug].ndc))
        writeFileSync(`${OUT}/${name}-${slug}${st}-pixels.json`, JSON.stringify({ pose: h0, onAtStart: on, on: onRun, off: offRun }, null, 1))
        note(`traypress ${slug} step ${st}: the dot held on the resting tray 40 frames, then 12 degrees off it`, { pose: { eye: h0.eye.map(v => +v.toFixed(3)), yawDeg: +h0.yaw.toFixed(1), pitchDeg: +h0.pitch.toFixed(1) }, onAtStart: on, onFrames: [...new Set(onRun.rows.map(x => x.on))], leavesOn: onRun.leaves, ats: { on: new Set(onRun.rows.map(x => JSON.stringify(x.at))).size, off: new Set(offRun.rows.map(x => JSON.stringify(x.at))).size } })
      }
      // NOTAKE=1 (D192 2, both sides): a down on the resting tray, the ray turned away within 2 frames, and the up
      // there: on the room, on another Bot, on another Bot's resting tray, on a board. None may take the tray (no
      // `press` line of either tray, the tray still at rest). And `back`: the ray leaves and comes back before the up,
      // which must take it. Each case is tried NOTAKE_N times (default 3).
      // RAY=hand: the right hand's ray is the pointer, raised by the eye; it is turned by the wrist (its direction) with
      // its origin still, so the pinch stays a click (`PINCH.stillM` counts the hand's travel only). Else the dot: a
      // held dot's ray does not follow the head in a pinch (`HeadAim.tsx`, `keep.held`), so its up lands where it began.
      if (process.env.NOTAKE === '1') {
        const viaHand = process.env.RAY === 'hand'
        await waitUntil(async () => { await emu.frames(page, 2); return ((await read(page)).pinch?.quietMs ?? 0) === 0 ? true : null }, 10000, 100)
        const crew = (await read(page)).crew.filter(c => !c.seat)
        const other = crew.find(c => c.slug !== slug && (c.top[1] - c.feet[1]) > 0.1)
        // A board: the probe gives no board's place, so its point is where Mira's send-back met Atlas's board paper
        // from her -20 eye (w1325: yaw -20.7, pitch -7, 0.194 m on). Judged as a board only if the up reads `board-paper`.
        let boardAt = null
        if (slug === 'mira' && st === -20) {
          await setHead(eye, -20.7, -7, 6); const hd = (await read(page)).pinch?.head
          if (hd) boardAt = hd.origin.map((v, i) => v + hd.direction[i] * 0.194)
        }
        const handO = [eye[0], eye[1] - 0.2, eye[2]]
        const unit = v => { const n = Math.hypot(...v); return v.map(x => x / n) }
        const rayTo = async to => { await emu.setRay(page, { hand: 'right', origin: handO, direction: unit(to.map((v, i) => v - handO[i])) }); await emu.frames(page, 2) }
        // Each turn goes in 2 frames: `lookAt` waits 14, which a held pinch would read as a long one.
        const aim = async to => { if (viaHand) await rayTo(to); else { const a = aimOf(to, eye); await setHead(eye, a.yaw, a.pitch, 2) } }
        const onNow = async () => { const rr = await read(page); return viaHand ? rr.pinch?.hands?.right?.on ?? null : rr.pinch?.head?.on?.what ?? null }
        const r00 = (await dashOf(page))[slug]
        const roomAt = [r00.at[0], r00.at[1] + 1.2, r00.at[2]]
        const away = {
          room: async () => aim(roomAt),
          bot: other ? async () => aim([other.feet[0], (other.feet[1] + other.top[1]) / 2, other.feet[2]]) : null,
          tray: slug !== 'atlas' ? async () => { const a = (await dashOf(page)).atlas; if (a) await aim([a.at[0], a.at[1] - 0.004, a.at[2]]) } : null,
          back: async to => { await aim(roomAt); await aim(to) },
          // Last and once: an up on a board at its Bot takes that board into his hand (`Board.tsx` onPointerUp, `onTake`),
          // so the page is loaded again after it (w1327: the held board then lay before every later aim).
          board: boardAt ? async () => aim(boardAt) : null,
          // DASH-16 D, with RAY=hand: a down on the room, then the up on Atlas's board at its Bot (must not take it); a
          // plain pinch on that board, and one that leaves to the room and comes back (both must take it).
          ...(viaHand && boardAt ? {
            roomboard: async () => aim(boardAt),
            boardplain: async () => {},
            // M247: what the ray lay on while it was away is logged (`midOn`): the board writes no line of its own.
            boardback: async () => { await aim(roomAt); midOn = await onNow(); await aim(boardAt) },
          } : {}),
        }
        // M47, by the dot: a long pinch on Atlas's board whose head then turns off the point of its down is let go by a
        // cancel (`releaseCancels`); that cancel is his own let-go and must still take the board.
        if (!viaHand && boardAt) away.boardlong = async () => { await page.waitForTimeout(700); await setHead(eye, head.yaw + 30, head.pitch, 2) }
        // NOTAKE_ONLY: a comma-free list of case kinds joined by '+', to run only those.
        if (process.env.NOTAKE_ONLY) for (const k of Object.keys(away)) if (!process.env.NOTAKE_ONLY.split('+').includes(k)) delete away[k]
        // M247: `roomboard` goes down where the hand's ray reads the room (the aim at another Bot's middle read `room`
        // in w1329): a hand's down on nothing at all goes to the dot (`pinchRoute.light`).
        const roomDown = other ? [other.feet[0], (other.feet[1] + other.top[1]) / 2, other.feet[2]] : roomAt
        const downTo = kind => (kind === 'roomboard' ? roomDown : kind === 'boardplain' || kind === 'boardback' || kind === 'boardlong' ? boardAt : null)
        const atlasBoard = list => (list.find(x => x.startsWith('atlas:')) ?? '').split(':')[2] ?? null
        if (viaHand) await setHead(eye, aimOf(r00.at, eye).yaw, aimOf(r00.at, eye).pitch, 6)
        const cases = []
        let midOn = null
        for (const [kind, turn] of Object.entries(away)) {
          if (!turn) { note(`notake ${slug} ${kind}`, 'not judged: nothing of that kind in the room to aim at'); continue }
          for (let n = 0; n < (kind.startsWith('board') || kind === 'roomboard' ? 1 : Number(process.env.NOTAKE_N ?? 3)); n += 1) {
            const r0 = (await dashOf(page))[slug]
            if (r0?.where !== 'rest') { note(`notake ${slug} ${kind} ${n}`, `not judged: the tray is ${r0?.where}`); break }
            let on = null, to = null
            const boardsBefore = await page.evaluate(() => (window.__xrPages.read().boards?.boards ?? []).map(x => `${x.bot}:${x.kind}:${x.where}`))
            if (downTo(kind)) { if (viaHand) await rayTo(downTo(kind)); else await lookAt(downTo(kind), eye); await emu.frames(page, 6); on = await onNow(); to = downTo(kind) }
            else for (const q of [[r0.at[0], r0.at[1] - 0.004, r0.at[2]], mid(r0.box), [r0.at[0], r0.at[1] + 0.004, r0.at[2]]]) {
              if (viaHand) await rayTo(q); else await lookAt(q, eye)
              await emu.frames(page, 6)
              on = await onNow(); to = q
              if (on === `dash:${slug}` && (await dashOf(page))[slug]?.lit) break
            }
            const t0 = await page.evaluate(() => performance.now())
            midOn = null
            await emu.select(page, { hand: 'right', down: true }); await emu.frames(page, 1)
            await turn(to)
            const upOn = await onNow()
            await emu.select(page, { hand: 'right', down: false }); await emu.frames(page, 10)
            const lines = await page.evaluate(t => (window.__xrDashWork?.presses ?? []).filter(x => x.ms >= t - 1), t0)
            const last = (await read(page)).pinch?.last ?? null
            const presses = lines.filter(x => x.ev === 'press').map(x => `${x.slug}:${x.does}`)
            const where = (await dashOf(page))[slug]?.where
            const c = { kind, n, ray: viaHand ? 'hand' : 'dot', downOn: on, upOn, midOn, last, presses, where, leaves: lines.filter(x => x.ev === 'leave' && x.slug === slug).length, lines: lines.map(x => `${x.slug}:${x.ev}:${x.pointer}:${x.piece ?? ''}${x.does ? `:${x.does}` : ''}`) }
            cases.push(c); console.log(`  notake ${slug} ${kind} ${n}: ${JSON.stringify(c).slice(0, 1100)}`)
            // A take (by `back`, or a wrong one) is put back by the probe's own way out, a reload: each case starts at rest.
            const boards = await page.evaluate(() => (window.__xrPages.read().boards?.boards ?? []).map(x => `${x.bot}:${x.kind}:${x.where}`))
            c.boards = boards
            c.boardsBefore = boardsBefore
            console.log(`  notake ${slug} ${kind} ${n}: Atlas's board ${atlasBoard(boardsBefore)} -> ${atlasBoard(boards)}`)
            if (where !== 'rest' || presses.length || kind.startsWith('board') || kind === 'roomboard') {
              await page.reload(); await waitUntil(async () => { try { const cr = (await read(page)).crew; return cr.length > 0 && cr.every(x => x.drawn) } catch { return false } }, 90000, 400)
              await page.evaluate(() => window.__xrDash3d.hold(true)); await enterAura(page); await parkHands(page); await emu.frames(page, 30)
              await waitUntil(async () => ((await dashOf(page))[slug] ? true : null), 20000, 300)
              await waitUntil(async () => { await emu.frames(page, 2); return ((await read(page)).pinch?.quietMs ?? 0) === 0 ? true : null }, 10000, 100)
              if (viaHand) await setHead(eye, aimOf(r00.at, eye).yaw, aimOf(r00.at, eye).pitch, 6)
            }
          }
        }
        if (viaHand) await parkHands(page)
        writeFileSync(`${OUT}/${name}-${slug}${st}-notake-${viaHand ? 'hand' : 'dot'}.json`, JSON.stringify(cases, null, 1))
        // A click and a long press both end in an up the tray reads (`upTakes`: only a drag takes nothing by its kind):
        // the back case's two turns pass `PINCH.clickMs` on a slow frame (w1328: 526 to 599 ms), so both are judged.
        // DASH-16 D: judged only where the down and the up read what the case names, and Atlas's board stood at its Bot.
        const dCase = (c, downOn) => c.upOn === 'board-paper' && c.downOn === downOn && atlasBoard(c.boardsBefore) === 'bot' && c.last?.kind !== 'drag'
        const dNot = cases.filter(c => (c.kind === 'board' && dCase(c, `dash:${slug}`)) || (c.kind === 'roomboard' && c.downOn === 'room' && c.upOn === 'board-paper' && atlasBoard(c.boardsBefore) === 'bot'))
        const dTake = cases.filter(c => (c.kind === 'boardplain' || (c.kind === 'boardback' && c.midOn !== null && c.midOn !== 'board-paper')) && dCase(c, 'board-paper'))
        const longs = cases.filter(c => c.kind === 'boardlong' && c.downOn === 'board-paper' && atlasBoard(c.boardsBefore) === 'bot' && c.last?.kind === 'long')
        if (!viaHand && boardAt && away.boardlong) check(`notake ${slug} step ${st} (dot, M47): a long pinch on Atlas's board, the head turned off its point before the let-go, takes it (${longs.length} judged)`, longs.length >= 1 && longs.every(c => atlasBoard(c.boards) === 'held'), cases.filter(c => c.kind === 'boardlong').map(c => [c.downOn, c.upOn, c.last?.kind, atlasBoard(c.boardsBefore), atlasBoard(c.boards)]))
        if (viaHand && boardAt) {
          check(`notake ${slug} step ${st} (hand ray, DASH-16 D): an up on Atlas's board at its Bot whose down began on her tray or the room takes nothing (${dNot.length} judged: ${dNot.map(c => c.kind).join(', ')})`, dNot.length >= 1 && dNot.every(c => atlasBoard(c.boards) === 'bot'), dNot.map(c => [c.kind, c.downOn, c.upOn, atlasBoard(c.boards)]))
          check(`notake ${slug} step ${st} (hand ray, DASH-16 D): a pinch on Atlas's board, and one that leaves and comes back, takes it (${dTake.length} judged: ${dTake.map(c => c.kind).join(', ')})`, dTake.length >= 1 && dTake.every(c => atlasBoard(c.boards) === 'held'), dTake.map(c => [c.kind, c.downOn, c.upOn, atlasBoard(c.boards), c.last?.kind]))
          note(`notake ${slug} step ${st}: DASH-16 D cases not judged`, cases.filter(c => ['board', 'roomboard', 'boardplain', 'boardback'].includes(c.kind) && !dNot.includes(c) && !dTake.includes(c)).map(c => [c.kind, c.downOn, c.upOn, atlasBoard(c.boardsBefore), c.last?.kind]))
        }
        const judged = cases.filter(c => c.downOn === `dash:${slug}` && (c.last?.kind === 'click' || c.last?.kind === 'long'))
        const off = judged.filter(c => c.kind !== 'back' && !['roomboard', 'boardplain', 'boardback'].includes(c.kind) && c.upOn !== `dash:${slug}`)
        const backs = judged.filter(c => c.kind === 'back' && c.upOn === `dash:${slug}`)
        note(`notake ${slug} step ${st}: cases not judged (not on the tray at the down, a drag, or the up's place not the case's)`, cases.filter(c => !off.includes(c) && !backs.includes(c) && !['roomboard', 'boardplain', 'boardback'].includes(c.kind)).map(c => [c.kind, c.n, c.downOn, c.upOn, c.last?.kind, c.presses]))
        if (!process.env.NOTAKE_ONLY) check(`notake ${slug} step ${st} (${viaHand ? 'hand ray' : 'dot'}): a down on the tray whose up lands off it takes nothing (${off.length} judged; the ups lay on ${[...new Set(off.map(c => `${c.kind}=${c.upOn}`))].join(', ')})`, off.length >= 3 && off.every(c => c.presses.length === 0 && c.where === 'rest'), off.filter(c => c.presses.length || c.where !== 'rest'))
        if (!process.env.NOTAKE_ONLY) check(`notake ${slug} step ${st} (${viaHand ? 'hand ray' : 'dot'}): a down on the tray that leaves and comes back before its up takes it (${backs.length} judged, ends ${[...new Set(backs.map(c => c.last?.kind))].join(', ')})`, backs.length >= 1 && backs.every(c => c.presses.includes(`${slug}:take`) && c.leaves >= 1), backs.filter(c => !c.presses.includes(`${slug}:take`) || c.leaves < 1))
      }
      for (let k = 0; k < tries; k += 1) {
        const from = pinchLog.length
        const tk = await take(slug, base.downDeg, eye)
        const took = tk.t?.where === 'taken'
        let back = null
        if (took) {
          await setHead(tk.at.eye, tk.at.yaw, 0, 2); await settled(slug, 0, 4000)
          back = await sendBack(slug)
          if (!back) { await setHead(tk.at.eye, tk.at.yaw, tk.at.pitch, 2); await sleep(900); back = await sendBack(slug) }
        }
        note(`traypress ${slug} step ${st} try ${k}`, { eye: eye.map(v => +v.toFixed(3)), took, back, pinches: pinchLog.length - from })
        // SAMPLE=1: the head held where the take's aim left it, 30 frames: what the dot lies on and where the tray
        // stands (its `at`, its drawn box `ndc`) each frame, with no pinch. Tells a tray that moves under a still dot.
        // Taken there after a send-back that failed: sampled with the head where the send-back's last aim left it.
        if (process.env.SAMPLE === '1' && took && !back) {
          const seen = []
          for (let i = 0; i < 30; i += 1) { await emu.frames(page, 1); const rr = await read(page); const d = rr.dash3d?.[slug]; seen.push({ on: rr.pinch?.head?.on?.what ?? null, at: d?.at?.map(v => +v.toFixed(4)), ndc: d?.ndc?.map(v => +v.toFixed(3)), where: d?.where }) }
          note(`traypress ${slug} step ${st} try ${k}: 30 frames, head still`, seen)
          // DASH-16 B: every hit on the dot's ray there, nearest first, with its press order and its board's `front`
          // (`__xrHeadHits`, on a build that has it), and a still of the whole view with its pose.
          const hits = await page.evaluate(() => window.__xrHeadHits?.() ?? null)
          const still = `${name}-${slug}${st}-back${k}.png`
          await page.screenshot({ path: `${OUT}/${still}` })
          note(`traypress ${slug} step ${st} try ${k}: the hits on the dot's ray, and the still`, { pose: { eye: head.eye.map(v => +v.toFixed(3)), yawDeg: +head.yaw.toFixed(1), pitchDeg: +head.pitch.toFixed(1) }, on: (await read(page)).pinch?.head?.on?.what ?? null, still, hits })
        }
        // Left taken, the next try would start from a taken tray: put it back by the probe's own way out, a reload.
        if (took && !back) {
          note(`traypress ${slug}: the form did not go back; the page is loaded again`, {})
          await page.reload(); await waitUntil(async () => { try { const cr = (await read(page)).crew; return cr.length > 0 && cr.every(x => x.drawn) } catch { return false } }, 90000, 400)
          await page.evaluate(() => window.__xrDash3d.hold(true)); await enterAura(page); await parkHands(page); await emu.frames(page, 30)
          await waitUntil(async () => ((await dashOf(page))[slug] ? true : null), 20000, 300)
        }
      }
    }
  }
  const misses = pinchLog.filter(x => x.last && x.last.kind === 'click' && /^dash:/.test(x.last.down) && x.last.down === x.last.up && !(x.trays ?? []).some(e => e.ev === 'press' && `dash:${e.slug}` === x.last.down))
  writeFileSync(`${OUT}/${name}-traypress.json`, JSON.stringify(pinchLog, null, 1))
  for (const m of misses) note('traypress MISS', m)
  check(`traypress: every click whose dot read a tray at its down and its up was answered by that tray's press (${pinchLog.length} pinches, ${misses.length} not answered)`, misses.length === 0, { misses: misses.length })
}
for (const slug of ONLY ? [] : ['mira', 'atlas']) {
  const set = slug === 'mira' ? await takePose(slug) : null
  if (set) note(`${slug}: the pose the row set before the take, and the pages it closed`, set)
  // D103 2's case: Atlas's tray rests low at its Bot, so it is taken with his head 19 degrees down on it, from the
  // first eye round it whose lens sides, top and foot a page or a Bot leaves judgeable (`stepTake`); Mira too.
  const ta = slug === 'atlas' ? await stepTake(slug, atlasBase) : null
  if (ta) note(`${slug}: the eye the row chose`, { chosen: ta.chosen, why: ta.why ?? null, steps: ta.steps.map(x => ({ stepDeg: x.stepDeg, taken: x.taken, clear: x.clear ?? null, pinches: x.pinches })) })
  const { t, tries, at } = ta ?? (await take(slug, null, set ? set.eye : null))
  // Atlas's take counts only from a chosen eye: one that leaves every side, the top and the foot a quarter clear (D173, M206).
  check(`a pinch with the dot on ${slug}'s tray takes it${ta ? ', from an eye that leaves every lens side, its top and its foot a quarter clear at all four poses' : ''}`, t?.where === 'taken' && (!ta || !!ta.chosen), { ...(ta ? { chosenStep: ta.chosen?.stepDeg ?? null, why: ta.why ?? null } : {}), tries: tries.map(x => ({ from: x.from, on: x.on, lit: x.lit, last: x.last, frames: x.frames })) })
  if (!t) continue
  const d0 = t
  // Like with like: `box` is the body's box in the room, and `press` is that same box projected by the build.
  const mine = rectOf(cornersOf(d0.box)); const its = ndcToPx(d0.press)
  const off = mine ? Math.max(Math.abs(mine.x0 - its.x0), Math.abs(mine.x1 - its.x1), Math.abs(mine.y0 - its.y0), Math.abs(mine.y1 - its.y1)) : null
  // The two cameras agree: 0.0001 px on every run that held like with like, and within 0.4 px of a magenta mark's
  // own pixels (`dash6-mark.mjs`, w1124). The 137 px of w1116 was this line holding the room box against `ndc`, a
  // tighter box since `e912fdc7ca2`, not a camera. The Bots' places that the pixel judge leaves out are drawn by
  // the row's camera, so this is a check again.
  check(`the row's camera draws ${slug}'s room box where the build does (within 2 px)`, off !== null && off <= 2, { mine, its, off })
  // 1. Whole at once, at the take: his head as it was on the tray.
  const pose1 = { slug, tag: `${slug}-at-take`, eye: at.eye, yaw: at.yaw, pitch: at.pitch, ndc: d0.ndc, bots: await botBoxes(), png: await shoot(`${slug}-at-take`), moves: d0.moves }
  poses.push(pose1)
  check(`${slug} taken with his head ${at.pitch.toFixed(1)} degrees ${at.pitch < 0 ? 'down' : 'up'} at the tray: its drawn box is in the lens at once, and it has not moved${slug === 'atlas' ? ' (the 19-degree take)' : ''}`, inside(d0.ndc) && d0.moves === 0 && (slug !== 'atlas' || at.pitch < -17), { pitch: +at.pitch.toFixed(1), ndc: d0.ndc.map(v => +v.toFixed(3)), moves: d0.moves })
  // 2. Head level, at rest: it comes whole within the board's rest and glide time.
  await setHead(at.eye, at.yaw, 0, 2)
  const cutLevel = !inside((await dashOf(page))[slug].ndc)
  const level = await settled(slug, cutLevel ? 1 : 0, cutLevel ? 6000 : 1500)
  if (!cutLevel) await sleep(1200)
  const d1 = level.d || (await dashOf(page))[slug]
  results[`${slug}-level`] = { ms: level.ms, moves: d1.moves }
  poses.push({ slug, tag: `${slug}-level`, eye: at.eye, yaw: at.yaw, pitch: 0, ndc: d1.ndc, bots: await botBoxes(), png: await shoot(`${slug}-level`), moves: d1.moves })
  check(`${slug}: his head level, the form ${cutLevel ? 'cut there' : 'whole there'}: ${cutLevel ? `at rest it comes whole within ${COME_MS} ms (rest 500 + glide 300 + slack), placed again once` : 'it does not move'}`, cutLevel ? !!level.d && level.ms <= COME_MS && inside(d1.ndc) && d1.moves === 1 : inside(d1.ndc) && d1.moves === 0, { ms: level.ms, moves: d1.moves, ndc: d1.ndc?.map(v => +v.toFixed(3)), come: d1.come, where: d1.where })
  // 3. From there, 9 degrees up and down: whole, and it does not move.
  for (const up of [9, -9]) {
    await setHead(at.eye, at.yaw, up); await sleep(900)
    const d = (await dashOf(page))[slug]
    poses.push({ slug, tag: `${slug}-${up > 0 ? 'up9' : 'down9'}`, eye: at.eye, yaw: at.yaw, pitch: up, ndc: d.ndc, bots: await botBoxes(), png: await shoot(`${slug}-${up > 0 ? 'up9' : 'down9'}`), moves: d.moves })
    check(`${slug}: the head ${up} degrees from level, at rest: its box in the lens and it did not move`, inside(d.ndc) && d.moves === d1.moves, { ndc: d.ndc.map(v => +v.toFixed(3)), moves: d.moves })
  }
  // 4. A pinch held on a part while the head turns 15 degrees, and rests there for a second: the form does not move.
  await setHead(at.eye, at.yaw, 0); await sleep(700)
  const before = (await dashOf(page))[slug]
  // The dot on a part of it (D107 5): at a PART's drawn middle (`partsNdc`, the build's picture of each part), never
  // at the form's middle, which may be a gap. Read where `before` was read (his head level toward it).
  let onPart = null
  const holdTries = []
  for (const nd of partMids(before)) {
    await aimNdc(at.eye, { yaw: at.yaw, pitch: 0 }, nd)
    onPart = await onWhat(); const lit = !!(await dashOf(page))[slug]?.lit
    holdTries.push({ nd: nd.map(v => +v.toFixed(3)), on: onPart, lit })
    if (/^dash/.test(onPart ?? '') && lit) break
  }
  note(`${slug}: the dot at each part's drawn middle, for the held pinch`, holdTries)
  // No pinch unless the dot lies on this form: a pinch that went down on a board took that board into his hand, and
  // a board in his hand sends every tray back and refuses each take (w1117: the rest of the row ran with it).
  const onForm = /^dash/.test(onPart ?? '') && !!(await dashOf(page))[slug]?.lit
  if (onForm) { await emu.select(page, { hand: 'right', down: true }); await emu.frames(page, 2) }
  const track = []
  for (let k = 1; k <= 5; k += 1) { await setHead(at.eye, head.yaw + 3, head.pitch, 4); const x = (await dashOf(page))[slug]; track.push({ at: x.at.map(v => +v.toFixed(4)), moves: x.moves }) }
  await sleep(1000); await emu.frames(page, 6)
  const held = (await dashOf(page))[slug]
  const cutHeld = !inside(held.ndc)
  if (onForm) { await emu.select(page, { hand: 'right', down: false }); await emu.frames(page, 6) }
  const still = onForm && track.every(x => Math.hypot(...x.at.map((v, i) => v - before.at[i])) < 0.001 && x.moves === before.moves) && Math.hypot(...held.at.map((v, i) => v - before.at[i])) < 0.001 && held.moves === before.moves
  check(`${slug}: a pinch held on a part (${onPart}) while the head turns 15 degrees and rests 1 s there: the form does not move`, /^dash/.test(onPart ?? '') && still, { onPart, cutWhileHeld: cutHeld, track, after: { at: held.at.map(v => +v.toFixed(4)), moves: held.moves }, stillTaken: (await dashOf(page))[slug]?.where })
  // After the release with the head 15 degrees aside: if it is cut there, it comes again (the rule, not a check).
  // It may be placed again where his head rests now (15 degrees aside): wait until it has stood still for 1.5 s.
  let after = { d: null, ms: 0 }
  { let last = null; let stillSince = Date.now(); const t0 = Date.now()
    while (Date.now() - t0 < 6000) { await emu.frames(page, 2); const x = (await dashOf(page))[slug]; const key = x ? `${x.moves}:${x.gliding}` : ''; if (key !== last) { last = key; stillSince = Date.now() } else if (Date.now() - stillSince > 1500 && !x?.gliding) { after = { d: x, ms: Date.now() - t0 }; break } await sleep(100) } }
  note(`${slug}: after the release, the head 15 degrees aside`, { cut: cutHeld, moves: after.d?.moves ?? null, where: (await dashOf(page))[slug]?.where })
  if ((await dashOf(page))[slug]?.where !== 'taken') { check(`${slug} still taken after the held pinch`, false, {}); continue }
  // 5. The dot on it, the head at rest 5 degrees off its middle, the form whole: it does not move.
  const now = (await dashOf(page))[slug]
  // The dot on a PART's drawn middle (D107 5), the part whose middle lies nearest 5 degrees from the form's middle:
  // the head turned that far off the form's middle, at rest there. The form's middle may be a gap (w1119).
  const h1 = { yaw: head.yaw, pitch: head.pitch }
  const angOf = n => [h1.yaw + Math.atan(((n[0] + n[2]) / 2) * Math.tan(AURA.fovxDeg / 2 / DEG)) * DEG, h1.pitch + Math.atan(((n[1] + n[3]) / 2) * Math.tan(AURA.fovyDeg / 2 / DEG)) * DEG]
  const [fy0, fp0] = angOf(now.ndc)
  // D108 3, the other side: a form at rest with his dot on it never starts to move. That can fail only for a CUT
  // form, so first the part farthest from the form's middle that leaves the form cut with the dot on it; else (a
  // narrow form) the part nearest 5 degrees off, the form whole.
  const parts = partMids(now).map(n => { const [y, p] = angOf(n); return { n, off: Math.hypot(y - fy0, p - fp0) } })
  let dotOn = null; let offDeg = null; let cutAtRest = false
  // The dot counts as on it when it lights (`lit`, the build's own pointer on it), not by the head probe's name alone.
  const onIt = async () => /^dash/.test((dotOn = await onWhat()) ?? '') && !!(await dashOf(page))[slug]?.lit
  for (const { n, off } of [...parts].sort((a, b) => b.off - a.off)) {
    await aimNdc(at.eye, h1, n, 4); offDeg = +off.toFixed(1)
    if (await onIt() && !inside((await dashOf(page))[slug].ndc)) { cutAtRest = true; break }
  }
  if (!cutAtRest) {
    for (const { n, off } of [...parts].sort((a, b) => Math.abs(a.off - 5) - Math.abs(b.off - 5))) {
      await aimNdc(at.eye, h1, n, 4); offDeg = +off.toFixed(1)
      if (await onIt()) break
    }
  }
  // The count starts here, with the dot on it: the search above turned the head, and a form cut on the way with no
  // dot on it is placed again by the rule; that is not what this case judges.
  const base = (await dashOf(page))[slug]
  await sleep(1500); await emu.frames(page, 6)
  const dot = (await dashOf(page))[slug]
  dotOn = await onWhat()
  check(`${slug}: the dot on a part's drawn middle (${dotOn}), the head at rest ${offDeg} degrees off the form's middle for 1.5 s, the form ${cutAtRest ? 'CUT' : 'whole'}: it does not start to move`, /^dash/.test(dotOn ?? '') && !!dot.lit && (cutAtRest || inside(dot.ndc)) && dot.moves === base.moves && Math.hypot(...dot.at.map((v, i) => v - base.at[i])) < 0.001, { on: dotOn, lit: dot.lit, offDeg, cutAtRest, ndc: dot.ndc.map(v => +v.toFixed(3)), moves: [base.moves, dot.moves] })
  // 6. D103 3: the form pushed out of the lens on purpose, held there by a pinch on empty space: the judge must fail.
  if (PUSH && slug === 'atlas') {
    await setHead(at.eye, at.yaw, 0); await sleep(600)
    const s = (await dashOf(page))[slug]
    // The push is found by the build's own picture: the head turns down a degree at a time, holding a pinch so the
    // form cannot move, until the form's drawn top crosses the lens's top; then PUSH degrees more less one. (w1116:
    // reckoned from the room box, the push took the whole form out of the lens, and the judge had nothing to see.)
    const topDeg = Math.max(...cornersOf(s.box).map(p => Math.atan2(p[1] - at.eye[1], Math.hypot(p[0] - at.eye[0], p[2] - at.eye[2])) * DEG))
    // Empty space first: the dot well over the form.
    // The pinch goes down on nothing: never on a page (a pinch there is a click into the dev gateway's page, w1116:
    // "panel:page") or a board. Up, then aside, until the dot lies on the room or on nothing.
    let sky = null
    for (const [dy, dx] of [[15, 0], [25, 0], [35, 0], [15, -25], [15, 25], [40, -40]]) {
      await setHead(at.eye, at.yaw + dx, topDeg + dy); sky = await onWhat()
      if (sky === null || sky === 'room') break
    }
    if (!(sky === null || sky === 'room')) { check('the pushed form: a place of empty sky to hold the pinch on', false, { sky }); continue }
    await emu.select(page, { hand: 'right', down: true }); await emu.frames(page, 2)
    // From 15 degrees up, so the sweep starts with the top in the lens: the form may already be cut at level
    // (w1121: it was, and the push from level took all of it out).
    let pitch = null; let wasIn = false
    for (let p = 15; p > -40; p -= 1) {
      await setHead(at.eye, at.yaw, p, 6); await sleep(120)
      const n = (await dashOf(page))[slug]?.ndc
      if (n && n[3] <= 1) wasIn = true
      else if (n && wasIn) { pitch = p - (PUSH - 1); break }
    }
    if (pitch === null) { await emu.select(page, { hand: 'right', down: false }); await emu.frames(page, 6); check('the pushed form: its drawn top crossed the lens top in the sweep', false, { wasIn }); continue }
    await setHead(at.eye, at.yaw, pitch); await sleep(800); await emu.frames(page, 4)
    const p = (await dashOf(page))[slug]
    poses.push({ slug, tag: `${slug}-pushed${PUSH}`, eye: at.eye, yaw: at.yaw, pitch, ndc: p.ndc, bots: await botBoxes(), png: await shoot(`${slug}-pushed${PUSH}`), moves: p.moves, pushed: true })
    await emu.select(page, { hand: 'right', down: false }); await emu.frames(page, 6)
    note('pushed', { sky, topDeg: +topDeg.toFixed(2), pitch: +pitch.toFixed(2), ndc: p.ndc.map(v => +v.toFixed(3)) })
  }
  // Sent back: a pinch on its tray, with the head level toward it.
  // Sent back: a pinch with the dot on a drawn piece of it (it lights), over a few points of its box (w1115: one
  // point missed, and every pose then had no frame to be judged against).
  for (const f of [[0.5, 0.2], [0.5, 0.5], [0.3, 0.4], [0.7, 0.4], [0.5, 0.8]]) {
    const w = (await dashOf(page))[slug]
    if (w?.where !== 'taken') break
    const [a0, b0] = w.box
    await lookAt([a0[0] + (b0[0] - a0[0]) * f[0], a0[1] + (b0[1] - a0[1]) * f[1], a0[2] + (b0[2] - a0[2]) * f[0]], at.eye)
    if (!(await dashOf(page))[slug]?.lit) continue
    await pinch(); await emu.frames(page, 40)
  }
  // Then over its DRAWN box, read where the head is now (w1122: every room-box point missed after the push).
  for (const [fx, fy] of [[0.5, 0.5], [0.5, 0.3], [0.35, 0.4], [0.65, 0.4], [0.5, 0.7], [0.25, 0.5], [0.75, 0.5]]) {
    const w = (await dashOf(page))[slug]
    if (w?.where !== 'taken') break
    const nd = w.ndc; const tx = Math.tan(AURA.fovxDeg / 2 / DEG); const ty = Math.tan(AURA.fovyDeg / 2 / DEG)
    await setHead(head.eye, head.yaw + Math.atan((nd[0] + (nd[2] - nd[0]) * fx) * tx) * DEG, head.pitch + Math.atan((nd[1] + (nd[3] - nd[1]) * fy) * ty) * DEG)
    if (!(await dashOf(page))[slug]?.lit) continue
    await pinch(); await emu.frames(page, 40)
  }
  const back = (await dashOf(page))[slug]?.where
  check(`${slug} sent back by a pinch on its taken tray`, back === 'rest', { where: back })
  // The pixel half: each pose again with the tray sent back, the Bots' places left out.
  for (const p of poses.filter(x => x.slug === slug)) {
    await setHead(p.eye, p.yaw, p.pitch); await sleep(300)
    const was = back === 'rest' ? await shoot(`${p.tag}-back`) : null
    const bots = [...p.bots, ...(await botBoxes())]
    // The pages are left out of the side columns only (M127): the top and foot rows judge the form's own width.
    const sideSkip = [...bots, ...(await pageBoxes())]
    const j0 = await judge(p.png, was, p.ndc, bots)
    // The lens's sides too (M81, REVIEW-43 3.3 3): the left and right 4 columns over the form's height. A column that
    // is all Bots' places judges nothing and is not counted against the form.
    const s = was ? await judgeSides(p.png, was, p.ndc, sideSkip) : { left: { changed: null, judged: 0 }, right: { changed: null, judged: 0 } }
    // M140: a side is calm only when at least a quarter of its columns are judged (REVIEW-57: at yaw 0 the side judged
    // 340 of 1,712 pixels, and a side left out whole passed).
    const sideCalm = e => e.changed !== null && e.judged >= 0.25 * (e.judged + e.leftOut) && e.changed < 0.03
    const j = { ...j0, left: s.left, right: s.right, sidesCalm: !!was && sideCalm(s.left) && sideCalm(s.right) }
    // Under 3 %, not 10 %: a form pushed 5 degrees past the top crosses the edge rows only with its rings' arcs, and
    // changed 7.5 % of them (w1120); whole forms changed at most 1.3 % (w1119, eight poses).
    // M206: the sides' floor here too: a row under a quarter judged (the rest a Bot's place) is not calm.
    const calm = e => e.changed !== null && e.changed < 0.03 && e.judged > 0 && e.judged >= 0.25 * (e.judged + e.leftOut)
    // D188 1: a top or foot row that fails only its floor, and would be a quarter judged without its own Bot's place.
    const ob = await ownBox(p.slug)
    const ownOnly = r0 => { const own = ob?.own; if (!own || !p.ndc.every(Number.isFinite)) return 0; const b = ndcToPx(p.ndc); const x0 = Math.max(0, Math.round(b.x0)), x1 = Math.min(AURA.width, Math.round(b.x1)); let n = 0; for (let y = r0; y < r0 + 4; y += 1) for (let x = x0; x < x1; x += 1) if (x >= own.x0 && x <= own.x1 && y >= own.y0 && y <= own.y1 && !ob.others.some(r => x >= r.x0 && x <= r.x1 && y >= r.y0 && y <= r.y1)) n += 1; return n }
    const byOwn = (e, r0) => e.changed !== null && e.changed < 0.03 && !calm(e) && e.judged + ownOnly(r0) >= 0.25 * (e.judged + e.leftOut)
    const rowsOk = (calm(j.top) || byOwn(j.top, 0)) && (calm(j.foot) || byOwn(j.foot, AURA.height - 4))
    const ownCovers = rowsOk && (!calm(j.top) || !calm(j.foot))
    const rest = j.sidesCalm && j.lip !== null && j.lip > 0.02
    if (ownCovers && rest && !p.pushed) {
      notJudged(`${p.tag}: ${OWN_BOT}`, { still: `${name}-${p.tag}.png`, pose: { eye: p.eye.map(v => +v.toFixed(3)), yawDeg: +p.yaw.toFixed(1), pitchDeg: +p.pitch.toFixed(1) }, ownBotPx: { top: ownOnly(0), foot: ownOnly(AURA.height - 4) }, ...j })
      continue
    }
    check(`${p.tag}: by pixels, the lens's top and foot rows across the form, the Bots' places left out, are as without it (under 3 % changed), the left and right 4 columns too, and its lip lit${p.pushed ? ' [PUSHED: this one must FAIL]' : ''}`, calm(j.top) && calm(j.foot) && j.sidesCalm && j.lip !== null && j.lip > 0.02, { still: `${name}-${p.tag}.png`, pose: { eye: p.eye.map(v => +v.toFixed(3)), yawDeg: +p.yaw.toFixed(1), pitchDeg: +p.pitch.toFixed(1) }, ndc: p.ndc.map(v => +v.toFixed(3)), boxInside: inside(p.ndc), ...j })
  }
}

// M71 (D104 2): one tray of six parts, placed again for his head facing 0 and then 45 degrees in the room: the two
// distances from his eye to the form's middle within 5 cm.
if (!ONLY || ONLY === 'six') {
  const tk = await take('atlas')
  if (tk.t) {
    const eye = tk.at.eye
    const dist = d => Math.hypot(d.at[0] - eye[0], d.at[2] - eye[2])
    const got = {}
    const sixPoses = {}
    for (const yaw of [45, 0, 45]) {
      const m0 = (await dashOf(page)).atlas?.moves ?? 0
      await setHead(eye, yaw, 0, 2)
      await settled('atlas', m0 + 1, 4000)
      await sleep(600); await emu.frames(page, 6)
      const d = (await dashOf(page)).atlas
      got[`yaw${yaw}`] = { distM: +dist(d).toFixed(3), moves: d.moves, inLens: inside(d.ndc), parts: d.parts.length, ndc: d.ndc.map(v => +v.toFixed(3)), gliding: d.gliding, pointed: d.hold?.pointed ?? d.come?.pointed ?? null, on: await onWhat(), fit: { n: d.fit?.n, k: d.fit?.k, dy: d.fit?.dy } }
      // D108 3: the cause shown. One line per facing: still gliding? his dot on it? how far it stands.
      note(`six parts facing ${yaw}: at the read`, { distM: got[`yaw${yaw}`].distM, gliding: d.gliding, pointed: d.hold?.pointed ?? d.come?.pointed ?? null, on: got[`yaw${yaw}`].on, inLens: got[`yaw${yaw}`].inLens })
      sixPoses[`yaw${yaw}`] = { eye, yaw, ndc: d.ndc, bots: await botBoxes(), png: await shoot(`six-${yaw}`) }
    }
    // D108 3, the other side: a pinch that goes down during a glide stops it where it is. His head turns to 0 again (the
    // form, placed for 45, is cut), and when it glides, his head lifts to the sky for one frame and a pinch goes
    // down there (on nothing: no press). Held 600 ms: the form stands where it was; let go, the glide ends.
    {
      // His head goes to the sky at once (the form, placed for 45 degrees level, is cut there), so the pinch can go
      // down on nothing on the very frame the glide begins (w1129: found after the sky search, the glide had ended).
      const m0 = (await dashOf(page)).atlas?.moves ?? 0
      let sky = null; let skyPitch = 40
      for (const p of [40, 55, 70]) { skyPitch = p; await setHead(eye, 0, p, 2); sky = await onWhat(); if (sky === null || sky === 'room') break }
      const g0 = (sky === null || sky === 'room') ? await waitUntil(async () => { await emu.frames(page, 1); const x = (await dashOf(page)).atlas; return x?.gliding && x.moves > m0 ? x : null }, 4000, 0) : null
      // Never a pinch on a page (a click into the dev gateway's page) or a board.
      if (!(sky === null || sky === 'room')) check('a pinch during a glide stops it: empty sky to pinch on', false, { sky })
      else if (!g0) check('a pinch during a glide stops it: a glide to catch', false, { moves: (await dashOf(page)).atlas?.moves })
      else {
        {
        await emu.select(page, { hand: 'right', down: true }); await emu.frames(page, 1)
        const a = (await dashOf(page)).atlas
        const track = []
        for (let k = 0; k < 6; k += 1) { await sleep(100); await emu.frames(page, 2); const x = (await dashOf(page)).atlas; track.push({ at: x.at.map(v => +v.toFixed(4)), gliding: x.gliding }) }
        await emu.select(page, { hand: 'right', down: false }); await emu.frames(page, 4)
        // Let go with his head still at the sky: the glide runs on to its end there.
        const end = await settled('atlas', m0 + 1, 3000)
        await setHead(eye, 0, 0, 2); await settled('atlas', m0 + 2, 3000)
        note('the glide caught by a pinch', { skyPitch })
        const stood = track.every(x => Math.hypot(...x.at.map((v, i) => v - a.at[i])) < 0.001)
        check('a pinch that goes down during a glide stops it where it is, and the glide ends after the let-go (D108 3)', track.every(x => x.gliding) && stood && !!end.d && !end.d.gliding, { sky, at: a.at.map(v => +v.toFixed(4)), track, end: end.d && { at: end.d.at.map(v => +v.toFixed(4)), gliding: end.d.gliding, inLens: inside(end.d.ndc) } })
        }
      }
    }
    // R10: the distance the width needs, from the build's numbers (`dash3d.ts` FORM): six parts of 0.13 m and two ends
    // of 0.01 m are 0.8 m wide; the near corners stand 0.05 + 0.085 sin 45 = 0.11 m nearer than the middle; the lens
    // leaves 2 x (30.7 - 2) degrees. At 0.8 m that span is 1.05 times the room, and `fitTaken` stands it 1.05 times
    // farther than the span needs: 0.8 x 1.05 x 1.05, 0.88 m at most; the near corners fill it at 0.84 m. So
    // between 0.84 and 0.88 m, each facing, and the two within 5 cm.
    const W = 6 * 0.13 + 2 * 0.01, near = 0.05 + 0.085 * Math.SQRT1_2, room = (30.7 - 2) * Math.PI / 180
    const need = W / 2 / Math.tan(room) + near
    const span = d => 2 * Math.atan(W / 2 / (d - near)) / (2 * room)
    const most = 0.8 * span(0.8) * 1.05
    got.needM = +need.toFixed(3); got.mostM = +most.toFixed(3)
    check(`a tray of six parts placed again facing 0 and 45 degrees in the room stands where its width needs (${need.toFixed(2)} to ${most.toFixed(2)} m), the two within 5 cm, whole (R10, M71)`, [got.yaw0, got.yaw45].every(x => x.distM >= need - 0.01 && x.distM <= most + 0.01 && x.inLens && x.gliding === false) && Math.abs(got.yaw0.distM - got.yaw45.distM) <= 0.05 && got.yaw0.parts === 6, got)
    const w = (await dashOf(page)).atlas; await lookAt([w.at[0], w.at[1] - 0.004, w.at[2]], eye); await pinch(); await emu.frames(page, 40)
    // By pixels too (D107 1): each facing again with the tray sent back; the lens's edge rows across the form as without it.
    const sent = (await dashOf(page)).atlas?.where
    for (const [tag, p] of Object.entries(sixPoses)) {
      if (sent !== 'rest') { check(`six parts ${tag}: by pixels (the tray was not sent back, nothing to judge against)`, false, { sent }); continue }
      await setHead(p.eye, p.yaw, 0); await sleep(300)
      const j = await judge(p.png, await shoot(`six-${p.yaw}-back`), p.ndc, [...p.bots, ...(await botBoxes())])
      // M206: the sides' floor here too.
      const calm = e => e.changed !== null && e.changed < 0.03 && e.judged > 0 && e.judged >= 0.25 * (e.judged + e.leftOut)
      check(`six parts ${tag}: by pixels, the lens's top and foot rows across the form, the Bots' places left out, are as without it (under 3 % changed), and its lip lit`, calm(j.top) && calm(j.foot) && j.lip !== null && j.lip > 0.02, { ndc: p.ndc.map(v => +v.toFixed(3)), ...j })
    }
    // The lens's sides too: the form is six parts wide, so a cut is at the sides. The left and right 4 columns over the
    // form's height, as without it.
    for (const [tag, p] of Object.entries(sixPoses)) {
      if (sent !== 'rest') continue
      await setHead(p.eye, p.yaw, 0); await sleep(300)
      const j = await judgeSides(p.png, await shoot(`six-${p.yaw}-back2`), p.ndc, [...p.bots, ...(await botBoxes())])
      check(`six parts ${tag}: by pixels, the lens's left and right 4 columns over the form's height, the Bots' places left out, are as without it (under 3 % changed)`, j.left.changed !== null && j.left.changed < 0.03 && j.right.changed !== null && j.right.changed < 0.03, j)
    }
  } else check('M71: Atlas\'s six-part tray taken', false, { tries: tk.tries })
}

// D107 3: each of the 11 reasons on Mira's taken tray. Its lines are on the lip, inside the tray's width, no taller
// than the tray's own label's letters (one label line, two at most for the reason), never over a Bot (inside the lip).
if (ONLY === 'why') {
  const P = (o = {}) => ({ kind: 'number', name: 'A', field: 'a', ...o })
  const LAYOUTS = {
    not_layout: 'ring',
    version: { v: 2, parts: [P()] },
    no_parts: { v: 1, parts: [] },
    too_many: { v: 1, parts: Array.from({ length: 7 }, () => P()) },
    part: { v: 1, parts: ['ring', P()] },
    kind: { v: 1, parts: [P({ kind: 'gauge', name: 'Load', field: 'load' }), P()] },
    name: { v: 1, parts: [{ kind: 'number', field: 'a' }, P()] },
    field: { v: 1, parts: [P({ field: '1a' }), P()] },
    path: { v: 1, parts: [P({ path: 'b.c.d.e.f' }), P()] },
    max: { v: 1, parts: [P({ kind: 'bar', max: 0 }), P()] },
    key: { v: 1, parts: [P({ kind: 'lamps', key: 'a b' }), P()] },
  }
  const eyeDist = d => Math.hypot(d.at[0] - head.eye[0], d.at[1] - head.eye[1], d.at[2] - head.eye[2])
  for (const [why, layout] of Object.entries(LAYOUTS)) {
    await setHead([0, EYE, 0], 0, 0)
    await page.evaluate(w => { window.__xrDash3d.set('mira', w) }, { xr_layout: layout, a: 3, load: 1 })
    await sleep(800)
    await waitUntil(async () => { const d = (await dashOf(page)).mira; return d && (why === 'part' || why === 'kind' || why === 'name' || why === 'field' || why === 'path' || why === 'max' || why === 'key' ? true : d.why === why) }, 8000, 200)
    // At rest first: his head toward Mira's tray from where the take is made.
    { const r = (await dashOf(page)).mira; if (r) { await lookAt(mid(r.box), [0.4, EYE, 0]); await sleep(400); await shoot(`why-${why}-rest`); note(`still why-${why}-rest: the pose`, { eye: head.eye, yawDeg: +head.yaw.toFixed(1), pitchDeg: +head.pitch.toFixed(1) }) } }
    let who = 'mira'
    let tk = await take('mira')
    // Mira's grey tray at rest is small and far, and the row's dot missed it for two reasons (w1127, w1129): the same
    // layout on Atlas, whose tray stands near.
    if (!tk.t) {
      who = 'atlas'
      await setHead([0, EYE, 0], 0, 0)
      await page.evaluate(w => { window.__xrDash3d.set('atlas', w) }, { xr_layout: layout, a: 3, load: 1 })
      await sleep(800)
      tk = await take('atlas')
    }
    if (!tk.t) { check(`${why}: Mira's or Atlas's tray taken`, false, { tries: tk.tries }); continue }
    await sleep(500); await emu.frames(page, 6)
    const d = (await dashOf(page))[who]
    const whyPng = await shoot(`why-${why}`)
    // M96: by pixels. The reason's box (the build's) on the still must lie inside the picture, and hold ink: pixels
    // far from the box's own middle colour (the lip), at least 2 % of it. A reason not drawn, or cut by the lens, fails.
    {
      const wn0 = d?.words?.whyNdc
      const px = wn0 ? ndcToPx(wn0) : null
      const inPic = !!px && px.x0 >= 0 && px.y0 >= 0 && px.x1 <= AURA.width && px.y1 <= AURA.height
      const ink = px ? await page.evaluate(async ([src, b]) => {
        const im = new Image(); im.src = `data:image/png;base64,${src}`; await im.decode()
        const c = document.createElement('canvas'); c.width = im.width; c.height = im.height
        const g = c.getContext('2d'); g.drawImage(im, 0, 0)
        const x0 = Math.max(0, Math.floor(b.x0)), y0 = Math.max(0, Math.floor(b.y0)), w = Math.max(1, Math.min(im.width, Math.ceil(b.x1)) - x0), h = Math.max(1, Math.min(im.height, Math.ceil(b.y1)) - y0)
        const p = g.getImageData(x0, y0, w, h).data, lum = []
        for (let i = 0; i < p.length; i += 4) lum.push(0.299 * p[i] + 0.587 * p[i + 1] + 0.114 * p[i + 2])
        const mid = [...lum].sort((a, z) => a - z)[lum.length >> 1]
        return { share: lum.filter(v => Math.abs(v - mid) > 60).length / lum.length, px: lum.length }
      }, [whyPng, px]) : null
      check(`${why}: by pixels, the reason's words are drawn whole in the picture (inside it, ink 2 % or more of their box)`, inPic && !!ink && ink.share >= 0.02, { box: px && Object.fromEntries(Object.entries(px).map(([k, v]) => [k, Math.round(v)])), inPic, ink: ink && { share: +ink.share.toFixed(3), px: ink.px }, still: `${name}-why-${why}.png` })
    }
    note(`still why-${why}: the pose`, { tray: who, eye: head.eye.map(v => +v.toFixed(3)), yawDeg: +head.yaw.toFixed(1), pitchDeg: +head.pitch.toFixed(1) })
    const w = d.words, b = d.ndc, wn = w.whyNdc, hn = w.headerNdc
    const lineH = hn ? hn[3] - hn[1] : null
    const whyH = wn ? wn[3] - wn[1] : null
    // On the lip: inside the drawn tray's width, and over the tray's label within the lip (`FORM.lipM` 85 mm: from the
    // label's top, 16 mm `WORDS.headerH` and 2 mm over the lip's foot, to the lip's top is 67 mm; the reason's top
    // stands 4 mm under it). The old line stood 5 cm over the tray's top, in the air: about 14 cm over the label.
    const perM = lineH ? lineH / 0.016 : 0
    const whole = !!wn && !!hn && ['not_layout', 'version', 'no_parts', 'too_many'].includes(why)
    const ok = !!wn && !!hn && w.why.length > 0 && wn[0] >= b[0] - 0.002 && wn[2] <= b[2] + 0.002 && (!whole || (wn[1] >= hn[3] - 0.004 && wn[3] - hn[3] <= 0.075 * perM))
    // His view: 40.7 degrees over 1200 px; the label's letters are `headerSize` 0.012 m at this distance.
    const dist = eyeDist(d)
    check(`${why}: "${w.why.join(' | ')}" on the lip, inside the tray's width${['not_layout', 'version', 'no_parts', 'too_many'].includes(why) ? ', just over the tray\'s label' : ''}`, ok, { whyNdc: wn?.map(v => +v.toFixed(3)), headerNdc: hn?.map(v => +v.toFixed(3)), trayNdc: b.map(v => +v.toFixed(3)), whyLines: lineH ? +(whyH / lineH).toFixed(2) : null, distM: +dist.toFixed(3), emDeg: +(Math.atan(0.015 / dist) * DEG).toFixed(2) })
    for (const f of [[0.5, 0.5], [0.5, 0.3], [0.3, 0.4], [0.7, 0.4]]) {
      const x = (await dashOf(page))[who]
      if (x?.where !== 'taken') break
      const nd = x.ndc; const tx = Math.tan(AURA.fovxDeg / 2 / DEG); const ty = Math.tan(AURA.fovyDeg / 2 / DEG)
      await setHead(head.eye, head.yaw + Math.atan((nd[0] + (nd[2] - nd[0]) * f[0]) * tx) * DEG, head.pitch + Math.atan((nd[1] + (nd[3] - nd[1]) * f[1]) * ty) * DEG)
      if (!(await dashOf(page))[who]?.lit) continue
      await pinch(); await emu.frames(page, 40)
    }
    // Atlas gets its six parts back for the next reason.
    if (who === 'atlas') await page.evaluate(w => { window.__xrDash3d.set('atlas', w) }, SIX)
  }
}

// The cases beside. (a) A second take while one is taken: one is taken at a time, the first goes back.
// ONLY=m72 (M114): the tray's sway alone, in the room the row opens with.
if (!ONLY || ONLY === 'm72') {
  if (ONLY !== 'm72') {
  // D105 4: which path put a tray back. The three paths in `Dash3d.tsx`: a press on the taken form (`trayPress`
  // 'back'), the effect that drops a take when something blocks or the view is gone, and a board that came up over
  // it (`dashWork.sentBack`). So the row logs both takes' tries, the pinch each made, the boards up, and sentBack.
  const work = () => page.evaluate(() => ({ sentBack: (window.__xrDashWork?.sentBack ?? []).length, boards: ((window.__xrPages.read().boards?.boards) ?? []).map(b => `${b.bot}:${b.where}`) }))
  const w0 = await work()
  // Atlas then Juno: Mira's far thin tray is the one the row's own aim misses (w1114, w1116: the first take failed).
  const first = await take('atlas')
  const after1 = { dash: Object.fromEntries(Object.entries(await dashOf(page)).map(([k, v]) => [k, v.where])), ...(await work()) }
  // D130 2, both sides (D140 C). From in front of Juno's tray, its top lies inside Atlas's taken box (w1222): a press
  // there is the form's, and the next press takes Juno's tray. Then, Atlas taken again, his head turned from the take's
  // eye to Juno's tray: nothing of the form is in the lens, and a take of Juno's tray is at once.
  let second = { t: null, tries: ['not tried: the first take failed'] }
  if (first.t) {
    const j = (await dashOf(page)).juno
    const [ja, jb] = j.box; const jdx = jb[0] - ja[0], jdz = jb[2] - ja[2]; const jn = jdx < jdz ? [1, 0] : [0, 1]; const js = Math.sign(-(jn[0] * j.at[0] + jn[1] * j.at[2])) || 1
    const jeye = [j.at[0] + jn[0] * js * 1.2, EYE, j.at[2] + jn[1] * js * 1.2]
    await lookAt([j.at[0], j.at[1] - 0.004, j.at[2]], jeye)
    // The form comes where he looks once his head has rested 500 ms (D103 2): the press waits until it stands there,
    // with the dot in its box (w1230: read before it came, the dot read `dash:juno` and the press went to the form).
    const came = await waitUntil(async () => { await emu.frames(page, 2); const r = await read(page), L = r.dash3d?.atlas?.lens ?? null; return r.dash3d?.atlas?.where === 'taken' && !r.dash3d.atlas.gliding && !!L && L[0] < 0 && L[2] > 0 && L[1] < 0 && L[3] > 0 ? r : null }, 5000, 50)
    const rIn = came || (await read(page)), din = rIn.dash3d, onIn = rIn.pinch?.head?.on?.what ?? null, L = din.atlas?.lens ?? null
    const inBox = !!L && L[0] < 0 && L[2] > 0 && L[1] < 0 && L[3] > 0
    await pinch(); await emu.frames(page, 12)
    const dIn = await dashOf(page), downIn = (await read(page)).pinch?.last?.down
    await emu.frames(page, 20); await sleep(400)
    await lookAt([j.at[0], j.at[1] - 0.004, j.at[2]], jeye)
    await pinch(); await emu.frames(page, 12)
    const dIn2 = await dashOf(page), downIn2 = (await read(page)).pinch?.last?.down
    check(`the second take, Juno's tray behind Atlas's box (in the box: ${inBox}): a press is the form's, it goes back and Juno's tray stays; the next press takes Juno's`, inBox && /dash:atlas/.test(onIn ?? '') && /dash:atlas/.test(downIn ?? '') && dIn.atlas?.where === 'rest' && dIn.juno?.where === 'rest' && dIn2.juno?.where === 'taken', { onIn, downIn, after: [dIn.atlas?.where, dIn.juno?.where], downIn2, after2: [dIn2.atlas?.where, dIn2.juno?.where] })
    // Juno's back, Atlas's taken again; then Juno's tray from the take's eye, with the form out of the lens.
    if (dIn2.juno?.where === 'taken') { await lookAt([dIn2.juno.at[0], dIn2.juno.at[1] - 0.004, dIn2.juno.at[2]], head.eye); await pinch(); await emu.frames(page, 30); await sleep(400) }
    const again = await take('atlas')
    if (again.t) {
      const jj = (await dashOf(page)).juno
      await setHead(again.at.eye, again.at.yaw, again.at.pitch, 4)
      // Pressed at once after the turn, in ONE read: within 500 ms the form comes where he looks (D103 2) and Juno's
      // tray is then behind it (w1230: the take's tries, 14 frames each, always saw the form arrive first).
      const a = aimOf([jj.at[0], jj.at[1] - 0.004, jj.at[2]], again.at.eye)
      await setHead(again.at.eye, a.yaw, a.pitch, 1)
      const rOut = await read(page), lensOut = rOut.dash3d?.atlas?.lens ?? null, onOut = rOut.pinch?.head?.on?.what ?? null
      await pinch(); await emu.frames(page, 6)
      const dOut = await dashOf(page)
      second = { t: dOut.juno?.where === 'taken' ? dOut.juno : null, tries: [{ at: 'aim', onOut, lit: rOut.dash3d?.juno?.lit ?? null, down: (await read(page)).pinch?.last?.down }] }
      second.lensOut = lensOut
    } else second = { t: null, tries: ['Atlas not taken again'], again: again.tries }
  }
  await emu.frames(page, 30); await sleep(600)
  const d = await dashOf(page)
  const after2 = { dash: Object.fromEntries(Object.entries(d).map(([k, v]) => [k, v.where])), ...(await work()) }
  note('the second take, step by step', { before: w0, first: { taken: !!first.t, tries: first.tries }, after1, second: { taken: !!second.t, tries: second.tries }, after2 })
  check('the second take, Juno\'s tray outside the taken form\'s picture (D130 2, his head turned so nothing of the form is in the lens): it is taken at once and the first goes back to its Bot', !!first.t && !!second.t && d.juno?.where === 'taken' && d.atlas?.where === 'rest', { atlas: d.atlas?.where, juno: d.juno?.where, firstTaken: !!first.t, secondTaken: !!second.t, secondDown: second.tries?.at?.(-1)?.last?.down ?? null, sentBack: after2.sentBack - w0.sentBack })
  // (b) A room switch while taken.
  const r0 = (await read(page)).rooms
  note('rooms before', r0 && { list: (r0.list ?? r0.rooms ?? []).map?.(x => x.id ?? x) , now: r0.now ?? r0.current ?? null })
  await page.evaluate(() => window.__xrPages.room('new'))
  await emu.frames(page, 30); await sleep(1200)
  const r1 = (await read(page)).rooms
  const d2 = await dashOf(page)
  note('a room switch (a new room) while Atlas is taken', { atlas: d2.atlas ? { where: d2.atlas.where, moves: d2.atlas.moves, inLens: inside(d2.atlas.ndc) } : null, rooms: r1 && { now: r1.now ?? r1.current ?? null } })
  await shoot('room-switch-taken')
  }
  // M72: the line from his eye to a tray at rest sways across another Bot's body edge. A Bot walking across the line
  // and his eye stepping across it make the same line; the row moves the eye, which it can do to the centimetre (the
  // Bots stand still: `stroll(false)`). It finds, by the build's own rule (`hiddenBy`: the body's column of half its
  // width, nearer than the tray), an eye where the line to a tray just touches another Bot's edge; then sways the eye
  // 1.5 cm to and fro every 100 ms for 3 s. The tray changes place at most once (lifted, or not), never bobs.
  // In the full run M72 would start after the room switch above, in a new room with a form still taken, where its
  // start is not clear (w1214). It runs in a run of its own (`ONLY=m72`), in the room the row opens with (D140 F).
  if (ONLY === 'm72') {
    const back = (await dashOf(page)).atlas; if (back?.where === 'taken') { await lookAt([back.at[0], back.at[1] - 0.004, back.at[2]], [0, EYE, 0]); await pinch(); await emu.frames(page, 40) }
    await page.evaluate(() => window.__xrPages.stroll?.(false)); await sleep(1500)
    // M114: a Bot already on a stroll walks home after `stroll(false)` (w1181: Mira's feet went 5 cm in 5 s while the
    // eye stepped), and a Bot that moves moves the line as much as the eye does. Wait until no Bot's feet move 2 mm in 1 s.
    const feetNow = async () => (await read(page)).crew.filter(b => !b.seat).map(b => b.feet)
    let moving = []
    const bodiesStill = await waitUntil(async () => {
      const a = await feetNow(); await sleep(1000); await emu.frames(page, 2); const c = (await read(page)).crew.filter(b => !b.seat)
      moving = c.filter((b, i) => Math.hypot(...b.feet.map((v, j) => v - a[i][j])) >= 0.002)
      return moving.length === 0
    }, 8000, 0)
    // The Bots of the back row still move at rest (w1183: Mira, Pico and Sage, `idle`, `rest`, not walking; Mira's feet
    // went round some 6 cm in 20 s), and only Atlas, Juno and Otto stand still, with no eye where one hides another's
    // tray. So the eye is set from where the two Bots stand at each step (`rowEdgeNow` below), not from where they stood.
    note('M72: the Bots stand still', { still: !!bodiesStill, moving: moving.map(b => ({ slug: b.slug, state: b.state, walking: !!b.walking, goal: b.goal ?? null, act: b.act ?? null })) })
    const crew = (await read(page)).crew.filter(b => !b.seat)
    const rests = Object.entries(await dashOf(page)).filter(([, x]) => x.where === 'rest')
    const hidden = (eye, at, body, slack = 0) => {
      const dx = at[0] - eye[0], dz = at[2] - eye[2], flat = dx * dx + dz * dz
      const t = ((body.feet[0] - eye[0]) * dx + (body.feet[2] - eye[2]) * dz) / flat
      if (t <= 0 || t >= 1) return false
      const px = eye[0] + dx * t - body.feet[0], pz = eye[2] + dz * t - body.feet[2]
      const y = eye[1] + (at[1] - eye[1]) * t, r = body.widthM / 2 + slack
      return px * px + pz * pz < r * r && y >= body.feet[1] && y <= body.top[1]
    }
    const midOf = (eye, at, body) => {
      const dx = at[0] - eye[0], dz = at[2] - eye[2], t = ((body.feet[0] - eye[0]) * dx + (body.feet[2] - eye[2]) * dz) / (dx * dx + dz * dz)
      const f = (eye[1] + (at[1] - eye[1]) * t - body.feet[1]) / (body.top[1] - body.feet[1])
      return f >= 0.2 && f <= 0.8
    }
    let found = null
    for (const [slug, x] of rests) {
      for (const b of crew.filter(c => c.slug !== slug)) {
        for (const z of [0, 0.5, -0.5, 1, 1.5]) for (const y of [EYE, 1.3, 1.1, 1.2, 1.0, 0.9]) for (let k = -200; k <= 200 && !found; k += 1) {
          const eye = [k / 100, y, z]; const eye2 = [(k + 1) / 100, y, z]
          // M114: only where the line meets the hider's column in the middle 60 % of its height, so that a Bot of the back
          // row that bobs (w1184: Mira's feet 3.5 cm up and down) moves the edge across, not the line over or under it.
          if (hidden(eye, x.at, b) !== hidden(eye2, x.at, b) && midOf(eye2, x.at, b)) found = { slug, by: b.slug, x: (k + 0.5) / 100, y, z }
        }
        if (found) break
      }
      if (found) break
    }
    if (!found) {
      const fOf = (eye, at, body) => { const dx = at[0] - eye[0], dz = at[2] - eye[2], t = ((body.feet[0] - eye[0]) * dx + (body.feet[2] - eye[2]) * dz) / (dx * dx + dz * dz); return +((eye[1] + (at[1] - eye[1]) * t - body.feet[1]) / (body.top[1] - body.feet[1])).toFixed(2) }
      const seen = []
      for (const [slug, x] of rests) for (const b of crew.filter(c => c.slug !== slug)) for (const y of [EYE, 1.4]) for (let k = -200; k <= 200; k += 1) { const e1 = [k / 100, y, 0], e2 = [(k + 1) / 100, y, 0]; if (hidden(e1, x.at, b) !== hidden(e2, x.at, b)) seen.push(`${slug}/${b.slug} y${y} x${k / 100} f${fOf(e2, x.at, b)}`) }
      note('M72: the edges there were, z 0', { seen: seen.slice(0, 40), trays: rests.map(([s, x]) => [s, x.at.map(v => +v.toFixed(2))]), crew: crew.map(b => [b.slug, b.feet.map(v => +v.toFixed(2)), +b.top[1].toFixed(2), b.widthM]) })
      check('M72: an eye where the line to a tray touches another Bot\'s edge', false, { rests: rests.map(([s]) => s), crew: crew.map(b => b.slug) })
    }
    else {
      // M81 (REVIEW-43 3.3 2, S8): judged by the tray's height in the room (`at[1]`), not by the build's flag; each side
      // of the sway is held 400 ms, longer than the build's 300 ms hold, so the tray can change; red when it never hid.
      // D114: the sway starts with the tray clear. The eye first stands 3 cm on the clear side of the edge (outside
      // the build's 2 cm slack) until the build says the tray is not hidden and it stands at its own height; that
      // height is the base. (Read before that, the base could be a lifted tray's, and every return read as a change.)
      const at0 = (await dashOf(page))[found.slug].at
      const clearX = [found.x - 0.03, found.x + 0.03].find(x => !hidden([x, found.y, found.z], at0, crew.find(c => c.slug === found.by), 0.02)) ?? found.x - 0.03
      const dir = Math.sign(clearX - found.x)
      // The dot is held 25 cm over the tray, off it: a tray that a pointer lies on never changes place (M72's own rule).
      const over = p => [p[0], p[1] + 0.25, p[2]]
      // Where the line from an eye at (x, found.y, found.z) to the tray at rest touches the hider's edge NOW, by the
      // row's copy of the build's rule: the tray's Bot and the hider may move (see above), and the edge with them.
      let edgeWas = found.x
      const rowEdgeNow = async () => {
        const c = (await read(page)).crew, o = c.find(b => b.slug === found.slug), hb = c.find(b => b.slug === found.by)
        const at = (await dashOf(page))[found.slug].at, p = [at[0], o.feet[1] + 0.045, at[2]]
        let best = null
        for (let k = -150; k < 150; k += 1) {
          const x1 = edgeWas + k / 1000, x2 = x1 + 0.001
          if (hidden([x1, found.y, found.z], p, hb) !== hidden([x2, found.y, found.z], p, hb) && (best === null || Math.abs(x1 - edgeWas) < Math.abs(best - edgeWas))) best = x1 + 0.0005
        }
        if (best !== null) edgeWas = best
        return { x: edgeWas, restY: o.feet[1] + 0.045, ownerFeet: o.feet.map(v => +v.toFixed(3)), y: at[1] }
      }
      // The eye `off` from that edge (+ to the clear side), set anew each 50 ms, held `ms`: what the build answers there
      // (`hidden` turns only after its 300 ms hold, dash3d.ts `hiddenStep`), and since when it has given that answer.
      const eyeAt = async (x, frames = 2) => { const at = (await dashOf(page))[found.slug].at; const a = aimOf(over(at), [x, found.y, found.z]); await setHead([x, found.y, found.z], a.yaw, a.pitch, frames) }
      const holdAt = async (off, ms = 450) => {
        let last = null, since = Date.now(), e = null
        const end = Date.now() + ms
        while (Date.now() < end) { e = await rowEdgeNow(); await eyeAt(e.x + dir * off, 1); await sleep(30); const h = (await dashOf(page))[found.slug]?.hidden?.hidden ?? null; if (h !== last) { last = h; since = Date.now() } }
        e = await rowEdgeNow()
        return { hidden: last, stoodMs: Date.now() - since, y: e.y, restY: e.restY, ownerFeet: e.ownerFeet, rowEdge: +e.x.toFixed(4) }
      }
      // M114 (D132 1). The rest height: where a tray at rest stands, its top over its own Bot's feet (`restBox().liftM`,
      // `FORM.restLiftM`, 0.045 m), read with each answer as its Bot moves; lifted, it stands over its Bot's head.
      const atRest = s => Math.abs(s.y - s.restY) < 0.01
      // (b) the start: 5 cm on the clear side, until the build has said "not hidden" for longer than its hold and the
      // tray stands at its rest height. Else the row does not start.
      let start = null
      const holds = []
      for (let k = 0; k < 8 && !start; k += 1) { const s = await holdAt(0.05, 500); holds.push({ k, hidden: s.hidden, stoodMs: s.stoodMs, y: +s.y.toFixed(3), restY: +s.restY.toFixed(3), rowEdge: s.rowEdge }); if (s.hidden === false && s.stoodMs > 300 && atRest(s)) start = s }
      // (a) the edge by the build's own answer: the eye steps 1 cm at a time towards the Bot and past, each step held
      // past the hold; the build's edge is half way between the last step it called clear and the first it hid, as an
      // offset from the row's edge.
      const steps = []
      let edge = null
      if (start) {
        for (let k = 4; k >= -5 && edge === null; k -= 1) {
          const s = await holdAt(k / 100)
          steps.push({ off: k / 100, hidden: s.hidden, stoodMs: s.stoodMs, y: +s.y.toFixed(3), restY: +s.restY.toFixed(3), rowEdge: s.rowEdge, ownerFeet: s.ownerFeet })
          if (s.hidden === true) edge = (k + 0.5) / 100
        }
      }
      note('M72: the start, the tray clear, and the edge by the build', { rowEdge: found.x, dir, start, steps, edgeOff: edge })
      // Each of the start's holds (M127): the build's answer, how long it stood, the height against the rest height.
      note('M72: the start\'s holds (up to eight)', { holds })
      note('M72: the start\'s holds (up to eight)', { holds })
      if (!start) { check(`M72: the line from his eye to ${found.slug}'s tray across ${found.by}'s edge: the start is not clear (the build did not say "not hidden" for 300 ms with the tray at its rest height)`, false, { found }) }
      if (start && edge === null) { check(`M72: the build's own edge: the tray never hid with the eye stepped from 4 cm clear to 5 cm in`, false, { found, steps }) }
      // Back to the clear side, past the 2 cm slack, until the start holds again: the sway starts with the tray at rest.
      // The slack is at the hider; at the eye it is larger by 1 / (1 - t), t the share of the line from the eye to the
      // hider (w1188: about 0.66, so 2 cm there is about 6 cm here): the eye goes back 10 cm.
      let again = null
      for (let k = 0; k < 8 && !again && start && edge !== null; k += 1) { const s = await holdAt(edge + 0.10, 500); if (s.hidden === false && s.stoodMs > 300 && atRest(s)) again = s }
      if (start && edge !== null && !again) { check(`M72: the line from his eye to ${found.slug}'s tray across ${found.by}'s edge: the start is not clear (after the edge was found)`, false, { found, edge, steps }) }
      // (c) "up" is judged against the rest height of that moment.
      const base = again ? again.restY : 0
      if (start && edge !== null && again) {
      const flips = []; let was = null; let ever = false; let most = 0
      const track = []
      const t0 = Date.now()
      for (let n = 0; n < 12; n += 1) {
        // To and fro 1.5 cm in all (0.75 cm each side of the build's edge): inside the build's 2 cm slack. A 3 cm sway,
        // with each side held past the 300 ms hold, may rightly cross both edges (w1129: 12 changes).
        const off = edge + (n % 2 ? 0.0075 : -0.0075)
        const end = Date.now() + 400
        while (Date.now() < end) {
          const e = await rowEdgeNow(); await eyeAt(e.x + dir * off, 1); await sleep(30)
          const dz = (await dashOf(page))[found.slug], y = dz?.at?.[1]
          if (y === undefined) continue
          // The track, to read a change by (w1189: 3 changes on 17c, cause not found): the edge, the build's answer, the
          // height over rest, and the hider's and the tray's Bot's feet.
          const cw = (await read(page)).crew, hb = cw.find(x => x.slug === found.by)
          track.push([Date.now() - t0, n, +e.x.toFixed(4), dz.hidden?.hidden ?? null, +(y - e.restY).toFixed(3), e.ownerFeet, hb?.feet?.map(v => +v.toFixed(3))])
          most = Math.max(most, y - e.restY)
          const up = y > e.restY + 0.02
          if (was !== null && up !== was) flips.push(Date.now() - t0)
          was = up; ever = ever || up
        }
      }
      await shoot('m72-sway')
      writeFileSync(`${OUT}/${name}-m72-track.json`, JSON.stringify({ found, edgeOff: edge, flips, track }))
      note('M72 the sway, sample by sample', { file: `${name}-m72-track.json`, samples: track.length })
      check(`M72: the line from his eye to ${found.slug}'s tray swaying 1.5 cm in all across ${found.by}'s edge, 400 ms each side, 12 sides: by its height, the tray hid and changed place at most once`, ever && flips.length <= 1, { found, edgeOff: edge, restYAtStart: +base.toFixed(3), mostOverRest: +most.toFixed(3), flips, ever })
      }
    }
  }
  // (c) A reload: nothing is taken after it (the take is not kept).
  await page.reload(); await waitUntil(async () => { try { const c = (await read(page)).crew; return c.length > 0 && c.every(b => b.drawn) } catch { return false } }, 90000, 400)
  await page.evaluate(() => window.__xrDash3d.hold(true))
  await enterAura(page); await parkHands(page); await emu.frames(page, 30)
  const d3 = await waitUntil(async () => { const x = await dashOf(page); return x.atlas ? x : null }, 20000, 300)
  check('after a reload nothing is taken: each tray rests at its Bot (a take is not kept)', !!d3 && Object.values(d3).every(x => x.where === 'rest'), d3 && Object.fromEntries(Object.entries(d3).map(([k, v]) => [k, v.where])))
}
console.log(`RESULTS ${JSON.stringify(results)}`)
// D188 1: the run's line by name: what passed, what is red, what was not judged (and only for `OWN_BOT`).
console.log(`TALLY pass ${tally.pass.length}, red ${tally.red.length}, not judged ${tally.notJudged.length}${tally.red.length ? ` | red: ${tally.red.join('; ')}` : ''}${tally.notJudged.length ? ` | not judged (${OWN_BOT}): ${tally.notJudged.join('; ')}` : ''}`)
console.log(`TOTAL bad ${bad}`)
await got.browser.close()
process.exit(bad ? 1 : 0)
