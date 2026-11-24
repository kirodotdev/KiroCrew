// P20j (D156): the things a press can land on that are not drawn, or not all drawn. Each ONLY mode measures one.
//   ONLY=botbox  each Bot at rest, the eye level with its press box at 1.5 m and 0.6 m: where the box ends (by the head's
//                ray, bisected) and where the body is drawn (by pixels: a still with the Bot, against the same pose with
//                the Bot walked out of the lens), each side; then presses 1, 2 and 4 cm outside the drawn body, left,
//                right and above: what each did (lit, taps, carried, what the down and up read).
//                CASE=taken: Atlas's form taken first. CASE=board: Juno's board in his hand first.
//   ONLY=planes  each hidden plane with no handlers (the page's corner bars, Home's plane): the eye set so the ray goes
//                through it to a Bot behind; what the dot is on, what the press did.
//   ONLY=close   the close mark of the page with its edge not shown: the dot set on it and pressed at once, and again
//                after 30 frames: does the page close? Crops of the mark's place before each press.
//   ONLY=gap04   Atlas's tray from 0.4 m: the dot in the widest gap; the gap's own box (`gapsAt`) against the head's
//                ray, the crop with the box drawn on it, and where the press went.
// usage: render-slot.sh node dash11-holes.mjs <out-prefix>
import { mkdirSync, writeFileSync } from 'node:fs'
import { dashOf, open, read, sleep, waitUntil } from './dash3d-lib.mjs'
import { AURA, EYE, enterAura, parkHands, emu } from './pages-lib.mjs'

const name = process.argv[2] ?? 'holes'
const OUT = process.env.HAND_OUT ?? (process.env.XR_UI ?? '..') + '/iterations/iter-15/dash11'
const ONLY = process.env.ONLY ?? 'botbox'
const CASE = process.env.CASE ?? ''
mkdirSync(OUT, { recursive: true })
let bad = 0
const check = (what, ok, got) => { if (!ok) bad += 1; console.log(`${ok ? 'PASS' : 'FAIL'} ${what}: ${JSON.stringify(got).slice(0, 1500)}`) }
const note = (what, got) => console.log(`  ${what}: ${JSON.stringify(got).slice(0, 3000)}`)
const DEG = 180 / Math.PI
/** Pixels for one unit of tan at the lens's middle. */
const F = AURA.height / 2 / Math.tan(AURA.fovyDeg / 2 / DEG)

const got = await open({ fixture: true, emulate: true, viewport: { width: AURA.width, height: AURA.height } })
const { page } = got
await waitUntil(async () => { const c = (await read(page)).crew; return c.length > 0 && c.every(b => b.drawn) }, 90000, 400)
await page.evaluate(() => { window.__xrPages.stroll?.(false); window.__xrDash3d?.hold?.(true) })
// SIX=1: Atlas's tray of six parts, as `dash4-taken.mjs` sets it (the gap-back row's tray).
if (process.env.SIX === '1') {
  const SIX = { xr_layout: { v: 1, parts: ['Aa', 'Bb', 'Cc', 'Dd', 'Ee', 'Ff'].map((nm, i) => ({ kind: ['number', 'ring', 'bar', 'lamps', 'number', 'ring'][i], name: nm, field: i === 3 ? 'fleet' : 'good', max: 9 })) }, good: 4, fleet: [{ state: 'working' }] }
  await page.evaluate(w => window.__xrDash3d.set('atlas', w), SIX)
  await waitUntil(async () => ((await dashOf(page)).atlas?.parts?.length ?? 0) === 6, 20000, 300)
}
await enterAura(page)
await parkHands(page)
await emu.frames(page, 20)

const head = { eye: [0, EYE, 0], yaw: 0, pitch: 0 }
const setHead = async (eye, yaw, pitch, frames = 14) => { head.eye = eye; head.yaw = yaw; head.pitch = pitch; await emu.setHead(page, { position: eye, yawDeg: -yaw, pitchDeg: pitch }); await emu.frames(page, frames) }
const aimOf = (to, from) => { const d = to.map((v, i) => v - from[i]); return { yaw: Math.atan2(d[0], -d[2]) * DEG, pitch: Math.atan2(d[1], Math.hypot(d[0], d[2])) * DEG } }
const lookAt = async (to, from = head.eye, frames = 14) => { const a = aimOf(to, from); await setHead(from, a.yaw, a.pitch, frames); return a }
const pinch = async () => { await emu.select(page, { hand: 'right', down: true }); await emu.frames(page, 1); await emu.select(page, { hand: 'right', down: false }); await emu.frames(page, 8) }
const onWhat = async () => (await read(page)).pinch?.head?.on?.what ?? null
const shoot = async tag => (await page.screenshot({ path: `${OUT}/${name}-${tag}.png` })).toString('base64')
const shootB64 = async () => (await page.screenshot()).toString('base64')
/** The boards in his hand now (`boards.boards[].where === 'held'`). */
const heldBoards = () => page.evaluate(() => (window.__xrPages.read().boards?.boards ?? []).filter(b => b.where === 'held').map(b => b.needId))
/**
 * M163, M164: a board in his hand blocks every tray (`Workspace.tsx` `boardInHand`), so before each try every held
 * board is handed back the way a hand does it: a pinch on it that travels 4 cm slowly (`board.ts` `releaseKind`: past
 * `stayM` 1.5 cm, under the throw's 9 cm, gives `back`). Returns what was still held after.
 */
const putBackBoards = async () => {
  for (const id of await heldBoards()) {
    const m = (await page.evaluate(() => window.__xrPages.read().boardMarks ?? {}))[id]
    if (!m) continue
    await lookAt(m.body, head.eye, 6)
    await page.evaluate(async () => {
      const e = window.__xrEmu
      e.select({ hand: 'right', down: true }); await e.frames(2)
      for (let i = 1; i <= 8; i += 1) { e.setRay({ hand: 'right', origin: [0.3 - 0.005 * i, 1, 0.2], direction: [0.577, -0.577, 0.577] }); await e.frames(1) }
      e.select({ hand: 'right', down: false }); await e.frames(4)
    })
    await parkHands(page)
    await waitUntil(async () => !(await heldBoards()).includes(id), 2500, 100).catch(() => {})
  }
  return heldBoards()
}
const settle = async () => { await page.evaluate(() => { window.__xrPages.cardClose?.() }); await emu.frames(page, 20); await putBackBoards(); await sleep(300) }
const botOf = async slug => (await read(page)).crew.find(b => b.slug === slug)
/** The middle of a Bot's press box (`Crew.tsx`: the plate at the body's middle height, in its parts group). */
// `feet` is in the room; `parts` is the parts group's own place, in its parent (w1256 aimed at the floor with it).
const boxMid = b => [b.feet[0], b.feet[1] + 0.3 * (0.07 + 0.5), b.feet[2]]
/** On the Bot's box: the Bot lit (its box's `onPointerEnter`). The dot's word for the box is `room` (`whatIs`: an
 * unnamed mesh), the same word as for other unnamed things, so it is not used. */
const litOn = async slug => (await read(page)).crew.find(b => b.slug === slug)?.lit === true

/** Pixel crop with a box and the dot drawn on it, written as a PNG at full size (D141). */
const crop = (src, rect, marks, file) => page.evaluate(async ([s, r, mk]) => {
  const img = await new Promise((done, fail) => { const i = new Image(); i.onload = () => done(i); i.onerror = fail; i.src = `data:image/png;base64,${s}` })
  const c = document.createElement('canvas'); c.width = r.w; c.height = r.h
  const x = c.getContext('2d'); x.drawImage(img, r.x, r.y, r.w, r.h, 0, 0, r.w, r.h)
  for (const m of mk) {
    x.strokeStyle = m.color; x.lineWidth = 1
    if (m.poly) { x.beginPath(); m.poly.forEach(([px, py], i) => (i ? x.lineTo(px - r.x, py - r.y) : x.moveTo(px - r.x, py - r.y))); x.closePath(); x.stroke() }
    if (m.dot) { x.beginPath(); x.arc(m.dot[0] - r.x, m.dot[1] - r.y, 3, 0, 7); x.stroke() }
  }
  return c.toDataURL('image/png').split(',')[1]
}, [src, rect, marks]).then(b64 => writeFileSync(`${OUT}/${name}-${file}.png`, Buffer.from(b64, 'base64')))

// ONLY=litpress (D162 3): why a Bot lit 1 to 2 cm outside its body takes no tap. For Pico and Sage at 0.6 m, the head
// turned out from the box's middle in 0.25 degree steps and back in again: at each step, after 10 frames, the Bot's
// light, what the dot is on (the same head pointer: `HeadAim.tsx`), whose the light is (`pinchRoute.light`) and
// whether the pointer is on. Then presses at chosen steps, each with the same reads just before and the press's
// own record (`to`, down, up, kind) and the taps.
if (ONLY === 'litpress') {
  const rows = []
  for (const slug of (process.env.BOTS ?? 'pico,sage').split(',')) {
    const b0 = await botOf(slug)
    const c = boxMid(b0), h = Math.hypot(c[0], c[2]) || 1, out = [-c[0] / h, -c[2] / h]
    const eye = [c[0] + out[0] * 0.6, c[1], c[2] + out[1] * 0.6]
    const a0 = await lookAt(c, eye)
    const state = async () => { const r = await read(page); const b = r.crew.find(x => x.slug === slug); return { lit: b?.lit === true, on: r.pinch?.head?.on?.what ?? null, light: r.pinch?.light ?? null, enabled: r.pinch?.head?.enabled ?? null, taps: b?.taps ?? null, feet: b?.feet?.map(v => +v.toFixed(3)) ?? null, walking: b?.walking ?? null, goal: b?.goal ?? null } }
    const sweep = []
    for (const sign of [-1, 1]) {
      for (let t = 0; t <= 14.01; t += 0.25) { await setHead(eye, a0.yaw + sign * t, a0.pitch, 10); sweep.push({ dir: sign < 0 ? 'out-left' : 'out-right', t, ...(await state()) }) }
      for (let t = 14; t >= -0.01; t -= 0.25) { await setHead(eye, a0.yaw + sign * t, a0.pitch, 10); sweep.push({ dir: sign < 0 ? 'in-left' : 'in-right', t, ...(await state()) }) }
    }
    // Presses: at the box's middle, and at each step where the Bot was lit while the dot read no hit.
    const odd = [...new Set(sweep.filter(s => s.lit && s.on !== 'room').map(s => `${s.dir.endsWith('left') ? -1 : 1}:${s.t}`))].slice(0, 4)
    const presses = []
    for (const k of ['1:0', ...odd]) {
      const [sg, t] = k.split(':').map(Number)
      await settle()
      await setHead(eye, a0.yaw + sg * t, a0.pitch, 10)
      const s0 = await state()
      const seq0 = (await read(page)).pinch?.seq ?? null
      await pinch(); await emu.frames(page, 12)
      const r1 = await read(page)
      presses.push({ at: k, before: s0, last: r1.pinch?.last ?? null, seq: [seq0, r1.pinch?.seq ?? null], taps: [s0.taps, r1.crew.find(x => x.slug === slug)?.taps ?? null] })
      // A tap opens the Bot's own things; they are closed before the next press.
      await page.evaluate(() => { window.__xrPages.cardClose?.() }); await emu.frames(page, 30); await sleep(600)
    }
    rows.push({ slug, eye, a0, sweep, presses })
  }
  writeFileSync(`${OUT}/${name}-litpress.json`, JSON.stringify(rows, null, 1))
  for (const r of rows) {
    const runs = []
    const f0 = r.sweep[0]?.feet
    for (const s of r.sweep) { const key = `${s.dir} lit=${s.lit} on=${s.on} moved=${f0 && s.feet ? Math.round(Math.hypot(s.feet[0] - f0[0], s.feet[2] - f0[2]) * 100) : '?'}cm goal=${s.goal}`; const last = runs[runs.length - 1]; if (last && last.key === key) last.to = s.t; else runs.push({ key, from: s.t, to: s.t }) }
    note(`litpress ${r.slug} sweep`, runs.map(x => `${x.key} @${x.from}..${x.to}`))
    note(`litpress ${r.slug} presses`, r.presses)
  }
  check('litpress: ran', rows.length > 0, rows.length)
}

// ONLY=moment (D162 3): the dot's place on the Bot's box at the light's read (A), at the down (B) and at the up (C),
// for presses near the box's side edge. The box's middle at each moment is the Bot's own place then (`feet`, which
// drifts while it idles: `places.ts` `driftNow`, 4 cm to each side, 3 cm up and down). The dot is where the head's
// ray (`pinch.head`) meets the box's plane. Inside the box: within 10.8 cm of its middle across, 15 cm up and down.
if (ONLY === 'moment') {
  const rows = []
  if (CASE === 'taken') {
    const a = (await dashOf(page)).atlas
    await lookAt([a.at[0], a.at[1] - 0.004, a.at[2]], [0, EYE, 0]); await pinch(); await emu.frames(page, 40); await sleep(600)
    note('case taken', { atlas: (await dashOf(page)).atlas?.where })
  }
  if (CASE === 'walk') {
    // A Bot that walks: Pico and Sage sent 0.3 m to their side and back while the presses run.
    for (const s of ['pico', 'sage']) await page.evaluate(([x]) => window.__xrScene?.walk(x, 0.3, 0), [s])
  }
  for (const slug of (process.env.BOTS ?? 'pico,sage').split(',')) {
    const b0 = await botOf(slug)
    const c0 = boxMid(b0), h = Math.hypot(c0[0], c0[2]) || 1, n = [-c0[0] / h, -c0[2] / h]
    const eye = [c0[0] + n[0] * 0.6, c0[1], c0[2] + n[1] * 0.6]
    const a0 = await lookAt(c0, eye)
    const at = async () => {
      const r = await read(page), b = r.crew.find(x => x.slug === slug), ray = r.pinch?.head
      if (!b || !ray) return null
      const c = boxMid(b), o = ray.origin, d = ray.direction
      const s = ((c[0] - o[0]) * n[0] + (c[2] - o[2]) * n[1]) / (d[0] * n[0] + d[2] * n[1])
      const p = [o[0] + d[0] * s, o[1] + d[1] * s, o[2] + d[2] * s]
      const across = (p[0] - c[0]) * -n[1] + (p[2] - c[2]) * n[0]
      return { lit: b.lit === true, on: ray.on?.what ?? null, acrossCm: +(across * 100).toFixed(1), upCm: +((p[1] - c[1]) * 100).toFixed(1), inBox: Math.abs(across) <= 0.108 && Math.abs(p[1] - c[1]) <= 0.15, taps: b.taps, feet: b.feet.map(v => +v.toFixed(3)) }
    }
    for (const sign of [-1, 1]) for (const t of [9.5, 10.5, 11.5, 12.5]) for (let k = 0; k < 2; k += 1) {
      await settle()
      await setHead(eye, a0.yaw + sign * t, a0.pitch, 10)
      const A = await at()
      await emu.select(page, { hand: 'right', down: true }); await emu.frames(page, 1)
      const B = await at()
      await emu.select(page, { hand: 'right', down: false }); await emu.frames(page, 1)
      const C = await at()
      await emu.frames(page, 10)
      const D = await at()
      rows.push({ slug, deg: sign * t, A, B, C, tapped: A && D ? D.taps > A.taps : null, last: (await read(page)).pinch?.last ?? null })
      if (A && D && D.taps > A.taps) { await page.evaluate(() => { window.__xrPages.cardClose?.() }); await emu.frames(page, 30); await sleep(600) }
    }
  }
  writeFileSync(`${OUT}/${name}-moment.json`, JSON.stringify(rows, null, 1))
  for (const r of rows) note('moment', `${r.slug} ${r.deg}: A ${r.A?.lit ? 'lit' : '-'} ${r.A?.inBox ? 'in' : 'out'} ${r.A?.acrossCm} | B ${r.B?.inBox ? 'in' : 'out'} ${r.B?.acrossCm} ${r.last?.down} | C ${r.C?.inBox ? 'in' : 'out'} ${r.C?.acrossCm} ${r.last?.up} | tapped ${r.tapped}`)
  // One thing, both sides (D162 2), judged at each moment by the dot's place on the box as it stood then: a tap only
  // when the dot was in the box at the down and at the up; and in the box at both, a tap.
  const bad1 = rows.filter(r => r.tapped && !(r.B?.inBox && r.C?.inBox))
  // A page that stands nearer on the ray takes the press (w1268, w1269: the Bot's own page, opened by its tap): that is
  // the page's, not a lost tap.
  const bad2 = rows.filter(r => !r.tapped && r.B?.inBox && r.C?.inBox && r.last?.kind === 'click' && !/^(page|panel):/.test(r.last?.down ?? ''))
  check(`moment: a press taps the Bot exactly when the dot was in its box at the down and at the up (${rows.length} presses)`, rows.length > 0 && bad1.length === 0 && bad2.length === 0, { tappedOutside: bad1.map(r => [r.slug, r.deg]), inButNoTap: bad2.map(r => [r.slug, r.deg, r.B, r.C]) })
  const litOut = rows.filter(r => r.A?.lit && !r.A.inBox)
  note('lit with the dot outside the box at the same read', litOut.map(r => [r.slug, r.deg, r.A.acrossCm]))
}

// ONLY=stuck (P20k 1): a press goes down on a Bot's box and comes up off it. The dot is held on the down's ray while
// the pinch lasts (`HeadAim.tsx`); the hand's travel turns it (`heldTurn`). So the row moves the pinching hand's ray
// sideways between the down and the up, all inside one page call (a click: under `PINCH.clickMs`). Read: what the dot
// was on at the down and the up, the Bot's light and its press under way (`pressing`, the probe, xr-15 only) at once,
// with the head turned 20 degrees away, and 1.2 s later (past the 900 ms answer light). MOVE_CM: the hand's travel.
if (ONLY === 'stuck') {
  const rows = []
  const slug = process.env.BOTS ?? 'pico'
  const move = Number(process.env.MOVE_CM ?? 8) / 100
  const b0 = await botOf(slug)
  const c0 = boxMid(b0), hh = Math.hypot(c0[0], c0[2]) || 1, n = [-c0[0] / hh, -c0[2] / hh]
  const eye = [c0[0] + n[0] * 0.6, c0[1], c0[2] + n[1] * 0.6]
  const a0 = await lookAt(c0, eye)
  const read1 = async () => { const r = await read(page); const b = r.crew.find(x => x.slug === slug); return { lit: b?.lit === true, litBy: b?.litBy ?? null, pressing: b && "pressing" in b ? b.pressing : "absent", taps: b?.taps ?? null, on: r.pinch?.head?.on?.what ?? null, dir: r.pinch?.head?.direction?.map(v => +v.toFixed(3)) ?? null, boxEvents: b?.boxEvents ?? 'absent', visibility: b?.visibility ?? 'absent' } }
  // A tap opens the Bot's page in front of it (`open-1`): it is closed before each try, so the next one meets the Bot.
  const closeOpened = () => page.evaluate(() => { for (const p of window.__xrPages.read().panels ?? []) if (/^open-/.test(p.id ?? '')) window.__xrPages.close(p.id) })
  for (let k = 0; k < Number(process.env.TRIES ?? 5); k += 1) {
    await settle(); await closeOpened(); await sleep(1200); await emu.frames(page, 10)
    // The dot 2 cm inside the box's left edge (10.8 cm from its middle at 0.6 m: 8.8 cm, 8.35 degrees).
    await setHead(eye, a0.yaw - Math.atan(0.088 / 0.6) * DEG, a0.pitch, 12)
    // HOW=hop: the dot 2 cm over the box's foot (13 cm under its middle), on its middle line.
    if (process.env.HOW === 'hop') await setHead(eye, a0.yaw, a0.pitch - Math.atan(0.13 / 0.6) * DEG, 12)
    if (process.env.HOW === 'ray') {
      // The head looks at the box's middle; the right hand's ray, from beside the eye, lies 2 cm inside the left edge.
      await lookAt(c0, eye)
      const side = [-n[1], n[0]]
      const origin = [eye[0] + side[0] * 0.15, eye[1] - 0.3, eye[2] + side[1] * 0.15]
      const pt = cm => [c0[0] - side[0] * cm / 100, c0[1], c0[2] - side[1] * cm / 100]
      const norm3 = v => { const l = Math.hypot(...v); return v.map(x => x / l) }
      const on = norm3(pt(8.8).map((v, i) => v - origin[i])), off = norm3(pt(16.8).map((v, i) => v - origin[i]))
      await page.evaluate(g => { window.__rayGoal = g }, { origin, on, off })
      await emu.setRay(page, { hand: 'right', origin, direction: on }); await emu.frames(page, 10)
    }
    const A = await read1()
    // HOW=walk: the hand stays still and the Bot walks out from under the held dot (as its drift did in w1270).
    const how = process.env.HOW ?? 'hand'
    const t = await page.evaluate(async ([m, how, s]) => {
      const e = window.__xrEmu, t0 = performance.now()
      e.select({ hand: 'right', down: true }); await e.frames(2)
      const held0 = window.__xrPages.read().pinch?.head?.direction ?? null
      let at = null
      if (how === 'hop') {
        // The Bot hops (`__xrScene.hop`) while the held dot stays near its box's foot.
        window.__xrScene?.hop(s)
        for (let i = 0; i < 8; i += 1) { await e.frames(1); if (window.__xrPages.read().pinch?.head?.on?.what !== 'room') break }
      } else if (how === 'ray') {
        // The hand's own ray (not held: it follows the hand) turned from the box to 6 cm past its edge.
        const g = window.__rayGoal
        at = e.setRay({ hand: 'right', origin: g.origin, direction: g.off })
        await e.frames(2)
      } else if (how === 'walk') {
        window.__xrScene?.walk(s, 0.4, 0)
        for (let i = 0; i < 20; i += 1) { await e.frames(1); if (window.__xrPages.read().pinch?.head?.on?.what !== 'room') break }
      } else {
        at = e.setRay({ hand: 'right', origin: [0.3 - m, 1, 0.2], direction: [0.577, -0.577, 0.577] })
        await e.frames(2)
      }
      const r1 = window.__xrPages.read(), held1 = r1.pinch?.head?.direction ?? null
      const atUp = { on: r1.pinch?.head?.on?.what ?? null, lit: (r1.crew ?? []).find(b => b.slug === 'pico' || b.slug === 'sage')?.lit ?? null }
      e.select({ hand: 'right', down: false }); await e.frames(2)
      return { ms: Math.round(performance.now() - t0), at, held0, held1, atUp }
    }, [move, how, slug])
    const B = await read1()
    const last = (await read(page)).pinch?.last ?? null
    await parkHands(page)
    await setHead(eye, a0.yaw - 20, a0.pitch, 12)
    const C = await read1()
    await sleep(1200); await emu.frames(page, 10)
    const D = await read1()
    // How far the held dot turned between the down and just before the up, in degrees.
    const turn = t.held0 && t.held1 ? +(Math.acos(Math.min(1, t.held0[0] * t.held1[0] + t.held0[1] * t.held1[1] + t.held0[2] * t.held1[2])) * DEG).toFixed(2) : null
    rows.push({ k, A, ms: t.ms, turn, atUp: t.atUp, last, B, away: C, later: D })
    if (how === 'walk') {
      await page.evaluate(([s, f]) => { const b = window.__xrPages.read().crew.find(x => x.slug === s); if (b) window.__xrScene?.walk(s, f[0] - b.feet[0], f[2] - b.feet[2]) }, [slug, b0.feet])
      await waitUntil(async () => { const f = (await botOf(slug)).feet; return Math.hypot(f[0] - b0.feet[0], f[2] - b0.feet[2]) < 0.05 }, 15000, 200).catch(() => {})
      await emu.frames(page, 20)
    }
    await lookAt(c0, eye)
  }
  writeFileSync(`${OUT}/${name}-stuck.json`, JSON.stringify(rows, null, 1))
  for (const r of rows) note('stuck', `${r.k}: A lit=${r.A.lit} by ${r.A.litBy} pressing=${JSON.stringify(r.A.pressing)} | ${r.ms} ms turn ${r.turn} deg, before the up on=${r.atUp?.on} | down=${r.last?.down} up=${r.last?.up} ${r.last?.kind} | after lit=${r.B.lit} by ${r.B.litBy} pressing=${JSON.stringify(r.B.pressing)} taps ${r.A.taps}>${r.B.taps} | head away lit=${r.away.lit} on=${r.away.on} pressing=${JSON.stringify(r.away.pressing)} | 1.2 s later lit=${r.later.lit}`)
  // One side (P20k 1): a press that came up off the box leaves no light once the dot is away, nor any press under way.
  const offUp = rows.filter(r => r.last?.down !== r.last?.up)
  const stuck = offUp.filter(r => r.later.lit || (r.later.pressing && r.later.pressing !== 'absent'))
  check(`stuck: a press that went down on the box and came up off it leaves no light (${offUp.length} such presses, ${stuck.length} left lit)`, offUp.length > 0 && stuck.length === 0, stuck.map(r => [r.k, r.last, r.later]))
}

// ONLY=second (P20k 3): a tap at the box's middle, then a second one 1, 5 and 15 frames later, inside the 900 ms answer
// light (`Crew.tsx` `tap`): is each taken (the Bot's `taps`)?
if (ONLY === 'second') {
  const rows = []
  const slug = process.env.BOTS ?? 'pico'
  const b0 = await botOf(slug)
  const c0 = boxMid(b0), hh = Math.hypot(c0[0], c0[2]) || 1, n = [-c0[0] / hh, -c0[2] / hh]
  const eye = [c0[0] + n[0] * 0.6, c0[1], c0[2] + n[1] * 0.6]
  const closeOpened = () => page.evaluate(() => { for (const p of window.__xrPages.read().panels ?? []) if (/^open-/.test(p.id ?? '')) window.__xrPages.close(p.id) })
  for (const gap of [1, 5, 15]) for (let k = 0; k < 2; k += 1) {
    await settle(); await closeOpened(); await sleep(1500); await emu.frames(page, 10)
    await lookAt(c0, eye)
    const r = await page.evaluate(async ([s, g]) => {
      const e = window.__xrEmu, rd = () => window.__xrPages.read(), taps = () => rd().crew.find(b => b.slug === s)?.taps ?? null
      const t0 = taps(), at0 = performance.now()
      e.select({ hand: 'right', down: true }); await e.frames(1); e.select({ hand: 'right', down: false })
      // M165: the first up and the second down each stamped by the page's clock; the open time is between them.
      const upAt = performance.now()
      await e.frames(1)
      const t1 = taps(), last1 = rd().pinch?.last ?? null
      await e.frames(g)
      const lit = rd().crew.find(b => b.slug === s)?.lit ?? null, on = rd().pinch?.head?.on?.what ?? null
      const dt = Math.round(performance.now() - at0)
      const downAt = performance.now()
      e.select({ hand: 'right', down: true }); await e.frames(1); e.select({ hand: 'right', down: false }); await e.frames(2)
      const last2 = rd().pinch?.last ?? null
      // w1275 read the taps 2 frames after the second up: the second pinch had not ended then. Read 30 frames later.
      await e.frames(30)
      return { t0, t1, t2: taps(), last1, last2, last2Later: rd().pinch?.last ?? null, litBetween: lit, onBetween: on, msBetween: dt, openMs: Math.round(downAt - upAt) }
    }, [slug, gap])
    rows.push({ gap, k, ...r })
  }
  writeFileSync(`${OUT}/${name}-second.json`, JSON.stringify(rows, null, 1))
  for (const r of rows) note('second', `${r.gap} frames (${r.msBetween} ms): taps ${r.t0}>${r.t1}>${r.t2}; between lit=${r.litBetween} on=${r.onBetween}; second ${r.last2?.kind} ${r.last2?.down}/${r.last2?.up}`)
  note('second: [frames, ms from the first down, ms open from the first up to the second down, taps counted]', rows.map(r => [r.gap, r.msBetween, r.openMs, r.t2 - r.t0, r.last2Later?.kind]))
  check('second: the pair of clicks was counted (at least the first)', rows.length > 0 && rows.every(r => r.t2 - r.t0 >= 1), rows.map(r => [r.gap, r.t2 - r.t0]))
}

// ONLY=mira (P20k 4): Mira's take, from the eyes the takes row used (w1251): the dot on her tray's middle, a pinch.
// At the down and at the up the dot's place (the lens's middle) against her tray's drawn box in the lens (`ndc`),
// and what the dot was on; then whether it was taken 6 and 30 frames later.
if (ONLY === 'mira') {
  const rows = []
  const inN = (n, p = [0, 0]) => !!n && p[0] >= n[0] && p[0] <= n[2] && p[1] >= n[1] && p[1] <= n[3]
  // CASE=board: a board is taken into his hand first, on purpose, and kept there: every try must be refused.
  let boardTaken = null
  if (CASE === 'board') {
    const marks = await page.evaluate(() => window.__xrPages.read().boardMarks ?? {})
    for (const [id, mk] of Object.entries(marks)) {
      if (!mk.paper) continue
      await lookAt(mk.paper, [0, EYE, 0]); await pinch(); await emu.frames(page, 20)
      if ((await heldBoards()).includes(id)) { boardTaken = id; break }
    }
    note('case board: taken into his hand', boardTaken)
  }
  // With a board in his hand it stands before his eye: the board case presses from eyes beside it.
  const froms = CASE === 'board' ? [[0.4, EYE, 0], [0.7, EYE, 0], [0.4, EYE + 0.2, 0]] : [[0, EYE, 0], [0.4, EYE, 0], [-0.4, EYE, 0]]
  for (const from of froms) for (let k = 0; k < Number(process.env.TRIES ?? 3); k += 1) {
    if (CASE === 'board') { await page.evaluate(() => { window.__xrPages.cardClose?.() }); await emu.frames(page, 20) } else await settle()
    const held = await heldBoards()
    const m = (await dashOf(page)).mira
    if (!m) { rows.push({ from, mira: null }); continue }
    await lookAt([m.at[0], m.at[1] - 0.004, m.at[2]], from)
    // A board standing on the line takes the press for itself (w1276: two clicks on `board-paper` took a board into
    // his hand): such a try is not pressed.
    const on0 = await onWhat()
    if (/^board/.test(on0 ?? '')) { rows.push({ from, k, held, skipped: on0 }); continue }
    const said0 = (await read(page)).note ?? null
    const r = await page.evaluate(async () => {
      const e = window.__xrEmu, rd = () => window.__xrPages.read(), at = () => { const x = rd(); const d = x.dash3d?.mira; return { on: x.pinch?.head?.on?.what ?? null, ndc: d?.ndc ?? null, lens: d?.lens ?? null, lit: d?.lit ?? null, where: d?.where ?? null } }
      const A = at()
      e.select({ hand: 'right', down: true }); await e.frames(1)
      const B = at()
      e.select({ hand: 'right', down: false }); await e.frames(1)
      const C = at()
      // Frame by frame after the up: was it ever taken, and did the build send it back (`dashWork.sentBack`: a board
      // over the tray, `Dash3d.tsx`)?
      const w = window.__xrDashWork, sent0 = w?.sentBack?.length ?? null
      const seen = []
      for (let i = 0; i < 6; i += 1) { await e.frames(1); seen.push(rd().dash3d?.mira?.where ?? null) }
      const D = at()
      await e.frames(24)
      return { A, B, C, D, E: at(), last: rd().pinch?.last ?? null, seen, sentBack: w ? (w.sentBack?.length ?? 0) - (sent0 ?? 0) : 'no dashWork', sentLast: w?.sentBack?.slice(-1)[0] ?? null }
    })
    rows.push({ from, k, held, ...r, dotInAtDown: inN(r.B.ndc), dotInAtUp: inN(r.C.ndc), said: [said0, (await read(page)).note ?? null] })
    if (r.E.where === 'taken') { const x = (await dashOf(page)).mira; await lookAt([x.at[0], x.at[1] - 0.004, x.at[2]]); await pinch(); await emu.frames(page, 40); await sleep(500) }
  }
  writeFileSync(`${OUT}/${name}-mira.json`, JSON.stringify(rows, null, 1))
  for (const r of rows) if (r.skipped) note('mira', `${JSON.stringify(r.from)}: not pressed, the dot on ${r.skipped}; held ${JSON.stringify(r.held)}`)
  for (const r of rows.filter(x => !x.skipped && x.A)) note('mira', `${JSON.stringify(r.from)}: held ${JSON.stringify(r.held)} said ${JSON.stringify(r.said?.[1])?.slice(0, 80)} | seen ${JSON.stringify(r.seen)} sentBack ${JSON.stringify(r.sentBack)} | A on=${r.A?.on} lit=${r.A?.lit} | down ${r.last?.down} dot in=${r.dotInAtDown} | up ${r.last?.up} dot in=${r.dotInAtUp} | ${r.last?.kind} | 6f ${r.D?.where} 30f ${r.E?.where} | ndc ${JSON.stringify(r.B?.ndc?.map(v => +v.toFixed(3)))}`)
  const onTray = rows.filter(r => r.dotInAtDown && r.dotInAtUp && /dash:mira/.test(r.last?.down ?? '') && /dash:mira/.test(r.last?.up ?? '') && r.last?.kind === 'click')
  if (CASE === 'board') {
    // The other side: with a board in his hand no such click takes her tray, and each says why (`Dash3d.tsx` `press`).
    const took = onTray.filter(r => r.E?.where === 'taken')
    // M187: at least 3 such clicks, as the empty hand's side asks: with none the check could pass with nothing tried.
    check(`mira, a board in his hand (${boardTaken}): no click on her drawn tray takes it (${onTray.length} such clicks, at least 3; ${took.length} taken)`, !!boardTaken && onTray.length >= 3 && took.length === 0, onTray.map(r => [r.from, r.held, r.E?.where, r.said?.[1]]))
  } else {
    const lost = onTray.filter(r => r.E?.where !== 'taken')
    const handFull = rows.filter(r => r.held?.length)
    check(`mira, his hand empty: a click with the dot on her drawn tray at the down and the up takes it (${onTray.length} such clicks, ${lost.length} not taken; tries with a board held: ${handFull.length})`, onTray.length >= 3 && lost.length === 0 && handFull.length === 0, lost.map(r => [r.from, r.held, r.D?.where, r.E?.where]))
  }
  if (boardTaken) note('board put back after the row', await putBackBoards())
}

if (ONLY === 'botbox') {
  if (CASE === 'taken') {
    const a = (await dashOf(page)).atlas
    await lookAt([a.at[0], a.at[1] - 0.004, a.at[2]], [0, EYE, 0]); await pinch(); await emu.frames(page, 40); await sleep(600)
    note('case taken', { atlas: (await dashOf(page)).atlas?.where })
  }
  if (CASE === 'board') {
    const j = (await read(page)).boards ?? null
    note('case board: boards', j)
  }
  const rows = []
  const only = process.env.BOTS ? process.env.BOTS.split(',') : null
  const crew = (await read(page)).crew.filter(b => !b.seat && (!only || only.includes(b.slug)))
  for (const b0 of crew) {
    const slug = b0.slug
    const c = boxMid(b0), h = Math.hypot(c[0], c[2]) || 1
    // D162 5: from the room's middle first; when something stands nearer on that line (Otto: a page, a board's foot),
    // the eye goes round the Bot by 20 degree steps until the Bot lights at its box's middle from both distances.
    let out = [-c[0] / h, -c[2] / h]
    for (const turn of [0, 20, -20, 40, -40, 60, -60, 80, -80]) {
      const r = turn / DEG, o = [-c[0] / h, -c[2] / h], dir = [o[0] * Math.cos(r) - o[1] * Math.sin(r), o[0] * Math.sin(r) + o[1] * Math.cos(r)]
      let ok = true
      for (const d of [1.5, 0.6]) { await lookAt(c, [c[0] + dir[0] * d, c[1], c[2] + dir[1] * d]); if (!(await litOn(slug))) { ok = false; break } }
      if (ok) { out = dir; if (turn) note(`botbox ${slug}: the eye went ${turn} degrees round it`, { dir }); break }
    }
    const eyeAt = d => [c[0] + out[0] * d, c[1], c[2] + out[1] * d]
    const per = {}
    // 1. Where the box ends: the head turned by bisection from the box's middle (on it) to past it (off).
    for (const d of [1.5, 0.6]) {
      const eye = eyeAt(d)
      const a0 = await lookAt(c, eye)
      const mid = (await litOn(slug)) ? `lit:${slug}` : await onWhat()
      const edge = async (axis, sign) => {
        let lo = 0, hi = Math.atan(0.3 / d) * DEG
        for (let k = 0; k < 9; k += 1) {
          const t = (lo + hi) / 2
          await setHead(eye, a0.yaw + (axis === 'x' ? sign * t : 0), a0.pitch + (axis === 'y' ? sign * t : 0), 3)
          if (await litOn(slug)) lo = t; else hi = t
        }
        return +((lo + hi) / 2).toFixed(3)
      }
      const box = mid === `lit:${slug}` ? { left: await edge('x', -1), right: await edge('x', 1), top: await edge('y', 1), bottom: await edge('y', -1) } : null
      await lookAt(c, eye)
      per[d] = { eye, a0, mid, box, still: await shootB64() }
    }
    // 2. Where the body is drawn: the same poses with the Bot walked 1.6 m to its side, out of the lens.
    const side = [-out[1], out[0]]
    const feet0 = (await botOf(slug)).feet
    await page.evaluate(([s, dx, dz]) => window.__xrScene?.walk(s, dx, dz), [slug, side[0] * 1.6, side[1] * 1.6])
    await waitUntil(async () => { const f = (await botOf(slug)).feet; return Math.hypot(f[0] - feet0[0], f[2] - feet0[2]) > 1.5 }, 20000, 200).catch(() => {})
    await emu.frames(page, 30)
    const drawn = {}
    for (const d of [1.5, 0.6]) {
      await lookAt(c, per[d].eye)
      const empty = await shootB64()
      // The drawn body: pixels that differ from the empty room by more than 24 levels, inside a window round the middle.
      drawn[d] = await page.evaluate(async ([s1, s2, w, hgt]) => {
        const load = async s => { const img = await new Promise((done, fail) => { const i = new Image(); i.onload = () => done(i); i.onerror = fail; i.src = `data:image/png;base64,${s}` }); const cv = document.createElement('canvas'); cv.width = img.naturalWidth; cv.height = img.naturalHeight; const x = cv.getContext('2d', { willReadFrequently: true }); x.drawImage(img, 0, 0); return x }
        const A = await load(s1), B = await load(s2)
        const W = A.canvas.width, H = A.canvas.height
        const x0 = Math.max(0, Math.round(W / 2 - w)), x1 = Math.min(W, Math.round(W / 2 + w)), y0 = Math.max(0, Math.round(H / 2 - hgt)), y1 = Math.min(H, Math.round(H / 2 + hgt))
        const a = A.getImageData(x0, y0, x1 - x0, y1 - y0).data, bb = B.getImageData(x0, y0, x1 - x0, y1 - y0).data
        const rowsOut = []
        let top = null, bottom = null, left = Infinity, right = -Infinity
        for (let y = 0; y < y1 - y0; y += 1) {
          let l = null, r = null
          for (let x = 0; x < x1 - x0; x += 1) { const i = (y * (x1 - x0) + x) * 4; if ((Math.abs(a[i] - bb[i]) + Math.abs(a[i + 1] - bb[i + 1]) + Math.abs(a[i + 2] - bb[i + 2])) / 3 > 24) { if (l === null) l = x; r = x } }
          if (l !== null) { if (top === null) top = y + y0; bottom = y + y0; left = Math.min(left, l + x0); right = Math.max(right, r + x0) }
          rowsOut.push(l === null ? null : [l + x0, r + x0])
        }
        // The row at the lens's middle (the box's middle height) and the widest.
        const midRow = rowsOut[Math.round(H / 2) - y0]
        return { top, bottom, left: Number.isFinite(left) ? left : null, right: Number.isFinite(right) ? right : null, midRow }
      }, [per[d].still, empty, Math.round(F * Math.tan(Math.atan(0.3 / d))), Math.round(F * Math.tan(Math.atan(0.3 / d)))])
    }
    await page.evaluate(([s, dx, dz]) => window.__xrScene?.walk(s, dx, dz), [slug, -side[0] * 1.6, -side[1] * 1.6])
    await waitUntil(async () => { const f = (await botOf(slug)).feet; return Math.hypot(f[0] - feet0[0], f[2] - feet0[2]) < 0.02 }, 20000, 200).catch(() => {})
    await emu.frames(page, 40); await sleep(400)
    const back = (await botOf(slug)).feet
    // 3. Presses outside the drawn body: 1, 2, 4 cm past its edge at the middle row (left, right) and past its top.
    const deg = px => Math.atan((px - (px < 0 ? 0 : 0)) / F) * DEG
    for (const d of [1.5, 0.6]) {
      const dr = drawn[d], p = per[d]
      if (!dr || dr.left === null || !p.box) { rows.push({ slug, d, drawn: dr, box: p.box, mid: p.mid }); continue }
      const W2 = AURA.width / 2, H2 = AURA.height / 2
      const ml = dr.midRow ? dr.midRow[0] : dr.left, mr = dr.midRow ? dr.midRow[1] : dr.right
      const body = { left: +deg(W2 - ml).toFixed(3), right: +deg(mr - W2).toFixed(3), top: +deg(H2 - dr.top).toFixed(3), bottom: +deg(dr.bottom - H2).toFixed(3) }
      const pxPerDeg = F / DEG
      const past = { left: +(p.box.left - body.left).toFixed(3), right: +(p.box.right - body.right).toFixed(3), top: +(p.box.top - body.top).toFixed(3), bottom: +(p.box.bottom - body.bottom).toFixed(3) }
      const pastPx = Object.fromEntries(Object.entries(past).map(([k, v]) => [k, Math.round(v * pxPerDeg)]))
      const pastCm = Object.fromEntries(Object.entries(past).map(([k, v]) => [k, +(Math.tan(v / DEG) * d * 100).toFixed(1)]))
      const presses = []
      for (const [sideName, axis, sign, edgeDeg] of [['left', 'x', -1, body.left], ['right', 'x', 1, body.right], ['top', 'y', 1, body.top], ['bottom', 'y', -1, body.bottom]]) {
        for (const cm of [1, 2, 4]) {
          const t = edgeDeg + Math.atan(cm / 100 / d) * DEG
          await settle()
          await setHead(p.eye, p.a0.yaw + (axis === 'x' ? sign * t : 0), p.a0.pitch + (axis === 'y' ? sign * t : 0), 10)
          const r0 = await read(page), b1 = r0.crew.find(b => b.slug === slug)
          const on = r0.pinch?.head?.on?.what ?? null
          await pinch(); await emu.frames(page, 12)
          const r1 = await read(page), b2 = r1.crew.find(b => b.slug === slug)
          presses.push({ side: sideName, cm, deg: +t.toFixed(3), on, lit: b1?.lit ?? null, down: r1.pinch?.last?.down ?? null, up: r1.pinch?.last?.up ?? null, kind: r1.pinch?.last?.kind ?? null, taps: [b1?.taps ?? null, b2?.taps ?? null], carried: b2?.carried ?? null, card: r1.card ? (r1.card.open ?? r1.card.slug ?? true) : null, panels: [r0.panels?.length ?? null, r1.panels?.length ?? null] })
        }
      }
      rows.push({ slug, d, mid: p.mid, box: p.box, body, past, pastPx, pastCm, feet: [feet0, back], presses })
    }
  }
  writeFileSync(`${OUT}/${name}-botbox.json`, JSON.stringify(rows, null, 1))
  for (const r of rows) note(`botbox ${r.slug} ${r.d} m`, { mid: r.mid, box: r.box, body: r.body, pastDeg: r.past, pastPx: r.pastPx, pastCm: r.pastCm, moved: r.feet && +Math.hypot(r.feet[1][0] - r.feet[0][0], r.feet[1][2] - r.feet[0][2]).toFixed(3), presses: r.presses?.map(q => `${q.side}${q.cm}:${q.on}/${q.down}/${q.up}/${q.kind}/taps${q.taps.join('>')}`) })
  const measured = rows.filter(r => r.past)
  check(`botbox: every Bot measured at both distances (${measured.length} of ${rows.length})`, measured.length === rows.length && rows.length > 0, rows.filter(r => !r.past).map(r => [r.slug, r.d, r.mid]))
}

if (ONLY === 'planes') {
  const walk = (await page.evaluate(() => window.__xrHiddenRays?.() ?? null))
  if (!walk) { console.log('  no probe walk (__xrHiddenRays) on this build: the planes are found by it, so this mode is not run here'); console.log('TOTAL bad 1'); await got.browser.close(); process.exit(1) }
  const planes = walk.filter(x => x.geometry === 'PlaneGeometry' && !x.handlers && (/^Mesh < Group\{xrPress=home\}/.test(x.path) || /xrPanel=page/.test(x.path)))
  const crew = (await read(page)).crew.filter(b => !b.seat)
  const rows = []
  for (const pl of planes) {
    // The Bot whose box lies farthest behind the plane from the room's middle, and the eye 0.7 m before the plane on
    // the line from that Bot through it.
    const pick = crew.map(b => ({ b, c: boxMid(b) })).sort((u, v) => Math.hypot(...v.c.map((q, i) => q - pl.at[i])) - Math.hypot(...u.c.map((q, i) => q - pl.at[i])))
    const behind = pick.find(({ c }) => Math.hypot(c[0], c[2]) > Math.hypot(pl.at[0], pl.at[2]) + 0.3) ?? pick[0]
    const dir = pl.at.map((q, i) => q - behind.c[i]), n = Math.hypot(...dir)
    const eye = pl.at.map((q, i) => q + (dir[i] / n) * 0.7)
    await settle()
    await lookAt(behind.c, eye)
    const r0 = await read(page), b1 = r0.crew.find(b => b.slug === behind.b.slug)
    const on = r0.pinch?.head?.on?.what ?? null
    await pinch(); await emu.frames(page, 12)
    const r1 = await read(page), b2 = r1.crew.find(b => b.slug === behind.b.slug)
    rows.push({ plane: pl.path, at: pl.at, hiddenBy: pl.hiddenBy, bot: behind.b.slug, eye: eye.map(v => +v.toFixed(3)), on, lit: b1?.lit, down: r1.pinch?.last?.down, up: r1.pinch?.last?.up, kind: r1.pinch?.last?.kind, taps: [b1?.taps, b2?.taps] })
    await shoot(`plane-${rows.length}`)
  }
  writeFileSync(`${OUT}/${name}-planes.json`, JSON.stringify(rows, null, 1))
  for (const r of rows) note('plane', r)
  // Reached: the Bot behind lit (its box's enter), or a drawn page or form stood nearer on the ray and took it.
  const reached = r => r.lit === true || /^(page|panel|dash):/.test(r.on ?? '')
  check(`planes: a ray through each hidden plane reaches what is drawn behind it (${rows.length} planes)`, rows.length > 0 && rows.every(reached), rows.map(r => [r.plane.slice(0, 40), r.bot, r.on, r.lit, r.down]))
  note('not reached', rows.filter(r => !reached(r)).map(r => [r.plane.slice(0, 50), r.at, r.on]))
}

if (ONLY === 'close') {
  const walk = (await page.evaluate(() => window.__xrHiddenRays?.() ?? null))
  const mk = walk?.find(x => /^Mesh\{xrPress=close/.test(x.path))
  const rows = []
  for (const wait of [1, 30]) {
    if (!mk) break
    // A page there to close: the sample room's first page.
    const n0 = (await read(page)).panels?.length ?? 0
    await settle()
    await lookAt([0, EYE, -1.7], [0, EYE, 0])
    await emu.frames(page, 40)
    const before = await shootB64()
    await lookAt(mk.at, [0, EYE, 0], wait)
    const r0 = await read(page)
    const at = await shootB64()
    await pinch(); await emu.frames(page, 20)
    const r1 = await read(page)
    const q = { x: AURA.width / 2, y: AURA.height / 2 }
    await crop(at, { x: q.x - 60, y: q.y - 60, w: 120, h: 120 }, [{ color: '#ff3040', dot: [q.x, q.y] }], `close-w${wait}-at`)
    await crop(before, { x: 0, y: 0, w: AURA.width, h: AURA.height }, [], `close-w${wait}-before`)
    rows.push({ wait, on: r0.pinch?.head?.on?.what ?? null, onLit: r0.pinch?.head?.on?.lit ?? null, panels: [n0, r0.panels?.length, r1.panels?.length], down: r1.pinch?.last?.down, up: r1.pinch?.last?.up, kind: r1.pinch?.last?.kind })
    if ((r1.panels?.length ?? 0) < (r0.panels?.length ?? 0)) { note('closed; reloading the room is not done, the second try has no page', null); break }
  }
  writeFileSync(`${OUT}/${name}-close.json`, JSON.stringify({ mark: mk ?? null, rows }, null, 1))
  for (const r of rows) note('close', r)
  check('close: the close mark was found by the walk', !!mk, { mk: mk?.path ?? null })
}

if (ONLY === 'gap04') {
  const a0 = (await dashOf(page)).atlas
  const hh = Math.hypot(a0.at[0], a0.at[2]) || 1
  const rows = []
  for (const far of [0.4, 0.3]) {
    const eye = [a0.at[0] - (a0.at[0] / hh) * far, a0.at[1] + 0.02, a0.at[2] - (a0.at[2] / hh) * far]
    await settle()
    await lookAt([a0.at[0], a0.at[1], a0.at[2]], eye)
    const d = (await dashOf(page)).atlas
    const ps = (d.partsNdc ?? []).map((p, i) => ({ i, p })).filter(({ p }) => p && p.every(v => Number.isFinite(v) && Math.abs(v) < 0.98)).sort((u, w) => u.p[0] - w.p[0])
    const pairs = []
    for (let k = 0; k + 1 < ps.length; k += 1) {
      const u = ps[k].p, w = ps[k + 1].p, y0 = Math.max(u[1], w[1]), y1 = Math.min(u[3], w[3]), sep = w[0] - u[2]
      if (y1 > y0 && sep > 0) pairs.push({ sep, between: [ps[k].i, ps[k + 1].i], g: [(u[2] + w[0]) / 2, (y0 + y1) / 2], u, w })
    }
    const pick = pairs.find(p => p.sep > 0.03) ?? pairs.sort((u, w) => w.sep - u.sep)[0]
    if (!pick) { rows.push({ far, pairs: pairs.length }); continue }
    const h0 = { yaw: head.yaw, pitch: head.pitch }
    await setHead(eye, h0.yaw + Math.atan(pick.g[0] * Math.tan(AURA.fovxDeg / 2 / DEG)) * DEG, h0.pitch + Math.atan(pick.g[1] * Math.tan(AURA.fovyDeg / 2 / DEG)) * DEG, 4)
    const r = await read(page)
    const ray = r.pinch?.head ?? null
    const gaps = r.dash3d?.atlas?.gapsAt ?? null
    const still = await shootB64()
    // The head's ray in each gap's own space (its unit box): inside or not, and how far from it.
    const inv = m => { // 4x4 column-major inverse (affine)
      const a = m; const r3 = [a[0], a[1], a[2], a[4], a[5], a[6], a[8], a[9], a[10]]
      const det = r3[0] * (r3[4] * r3[8] - r3[5] * r3[7]) - r3[3] * (r3[1] * r3[8] - r3[2] * r3[7]) + r3[6] * (r3[1] * r3[5] - r3[2] * r3[4])
      const c = [(r3[4] * r3[8] - r3[5] * r3[7]) / det, -(r3[1] * r3[8] - r3[2] * r3[7]) / det, (r3[1] * r3[5] - r3[2] * r3[4]) / det, -(r3[3] * r3[8] - r3[5] * r3[6]) / det, (r3[0] * r3[8] - r3[2] * r3[6]) / det, -(r3[0] * r3[5] - r3[2] * r3[3]) / det, (r3[3] * r3[7] - r3[4] * r3[6]) / det, -(r3[0] * r3[7] - r3[1] * r3[6]) / det, (r3[0] * r3[4] - r3[1] * r3[3]) / det]
      const t = [a[12], a[13], a[14]]
      return { lin: v => [c[0] * v[0] + c[3] * v[1] + c[6] * v[2], c[1] * v[0] + c[4] * v[1] + c[7] * v[2], c[2] * v[0] + c[5] * v[1] + c[8] * v[2]], pt: v => { const q = [v[0] - t[0], v[1] - t[1], v[2] - t[2]]; return [c[0] * q[0] + c[3] * q[1] + c[6] * q[2], c[1] * q[0] + c[4] * q[1] + c[7] * q[2], c[2] * q[0] + c[5] * q[1] + c[8] * q[2]] } }
    }
    const tests = (gaps ?? []).map((g, gi) => {
      if (!ray) return { gi, on: g.on, hit: null }
      const iv = inv(g.m), o = iv.pt(ray.origin), dd = iv.lin(ray.direction)
      let t0 = -Infinity, t1 = Infinity
      for (let k = 0; k < 3; k += 1) {
        if (Math.abs(dd[k]) < 1e-12) { if (o[k] < -0.5 || o[k] > 0.5) { t0 = Infinity } continue }
        const ta = (-0.5 - o[k]) / dd[k], tb = (0.5 - o[k]) / dd[k]
        t0 = Math.max(t0, Math.min(ta, tb)); t1 = Math.min(t1, Math.max(ta, tb))
      }
      // Where the ray crosses the gap's middle plane (its local z = 0): how far, in the gap's own widths, from its box.
      const tz = Math.abs(dd[2]) > 1e-9 ? -o[2] / dd[2] : null
      const pz = tz === null ? null : [o[0] + dd[0] * tz, o[1] + dd[1] * tz]
      // The gap's box seen from the eye: its eight corners in pixels, by the row's camera (the head above).
      return { gi, on: g.on, hit: t0 <= t1 && t1 > 0, atMid: pz && pz.map(v => +v.toFixed(3)) }
    })
    const proj = p => {
      const y = head.yaw / DEG, t = head.pitch / DEG, dv = [p[0] - head.eye[0], p[1] - head.eye[1], p[2] - head.eye[2]]
      const x1 = dv[0] * Math.cos(y) + dv[2] * Math.sin(y), z1 = -dv[0] * Math.sin(y) + dv[2] * Math.cos(y)
      const y2 = dv[1] * Math.cos(t) + z1 * Math.sin(t), z2 = -dv[1] * Math.sin(t) + z1 * Math.cos(t)
      return [AURA.width / 2 + (x1 / -z2) * F, AURA.height / 2 - (y2 / -z2) * F]
    }
    const face = g => { const m = g.m; const w = v => [m[0] * v[0] + m[4] * v[1] + m[8] * v[2] + m[12], m[1] * v[0] + m[5] * v[1] + m[9] * v[2] + m[13], m[2] * v[0] + m[6] * v[1] + m[10] * v[2] + m[14]]; return [[-0.5, -0.5, 0.5], [0.5, -0.5, 0.5], [0.5, 0.5, 0.5], [-0.5, 0.5, 0.5]].map(v => proj(w(v))) }
    const facesBack = g => { const m = g.m; const w = v => [m[0] * v[0] + m[4] * v[1] + m[8] * v[2] + m[12], m[1] * v[0] + m[5] * v[1] + m[9] * v[2] + m[13], m[2] * v[0] + m[6] * v[1] + m[10] * v[2] + m[14]]; return [[-0.5, -0.5, -0.5], [0.5, -0.5, -0.5], [0.5, 0.5, -0.5], [-0.5, 0.5, -0.5]].map(v => proj(w(v))) }
    const toPx = n => [((n[0] + 1) / 2) * AURA.width, ((1 - n[1]) / 2) * AURA.height]
    const q = { x: AURA.width / 2, y: AURA.height / 2 }
    const marks = [{ color: '#ff3040', dot: [q.x, q.y] }]
    for (const g of gaps ?? []) { marks.push({ color: g.on ? '#30ff60' : '#808080', poly: face(g) }); marks.push({ color: g.on ? '#208040' : '#505050', poly: facesBack(g) }) }
    for (const pp of [pick.u, pick.w]) { const a = toPx([pp[0], pp[1]]), b = toPx([pp[2], pp[3]]); marks.push({ color: '#3080ff', poly: [[a[0], b[1]], [b[0], b[1]], [b[0], a[1]], [a[0], a[1]]] }) }
    await crop(still, { x: q.x - 160, y: q.y - 120, w: 320, h: 240 }, marks, `gap04-${far}-crop`)
    await crop(still, { x: q.x - 160, y: q.y - 120, w: 320, h: 240 }, [], `gap04-${far}-bare`)
    await pinch(); await emu.frames(page, 8)
    const last = (await read(page)).pinch?.last, after = (await dashOf(page)).atlas?.where
    rows.push({ far, sep: +pick.sep.toFixed(4), between: pick.between, on: ray?.on?.what ?? null, tests, down: last?.down, up: last?.up, kind: last?.kind, after })
    if (after === 'taken') { const x = (await dashOf(page)).atlas; await lookAt([x.at[0], x.at[1] - 0.004, x.at[2]], eye); await pinch(); await emu.frames(page, 40); await sleep(500) }
  }
  writeFileSync(`${OUT}/${name}-gap04.json`, JSON.stringify(rows, null, 1))
  for (const r of rows) note('gap04', r)
  const red = rows.filter(r => r.tests?.some(t => t.on && t.hit) && r.after !== 'taken')
  check('gap04: a press whose ray is inside an on gap\'s box takes the tray', rows.length > 0 && red.length === 0, red.map(r => [r.far, r.on, r.down, r.after]))
}

console.log(`TOTAL bad ${bad}`)
await got.browser.close()
process.exit(bad ? 1 : 0)
