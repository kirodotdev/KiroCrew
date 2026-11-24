// P20f C (D114 2, D117 2; REVIEW-47 Part 1 section 4): a press on a taken dashboard that lies over a DESK LINE, a
// Bot's words board 1.8 m or more away (`Board.tsx` `AT_DESK`, `userData.desk` set), which is drawn with no depth test
// and pressed first while it is `front`. The sample crew with words (`fixture=1&words=1`), an emulated session (iwer),
// the Aura's lens 40.7 degrees tall.
//   r6  The form at rest in its Bot's hands: every desk line stays in front (as today).
//   r1  A form taken, his look turned so the form lies over the desk line's lower part in the picture. A pinch on a
//       solid part of the form inside the overlap: the form goes back, and the board is not taken.
//   r3  The same pose, a pinch on the board 2 cm (in the room, at the board) above the form's outline: the board is taken.
//   (r2 by pixels, r4 another Bot nearer and r5 a press 100 ms after the take are not in this row.)
// Each still carries its pose line. Red before (m108: no outline, no `takenCovers`), green after.
// usage: render-slot.sh node taken-desk.mjs [out-prefix]
import { mkdirSync, writeFileSync } from 'node:fs'
import { dashOf, open, read, sleep, waitUntil } from './dash3d-lib.mjs'
import { AURA, EYE, enterAura, parkHands, emu } from './pages-lib.mjs'

const name = process.argv[2] ?? 'taken-desk'
const OUT = process.env.HAND_OUT ?? (process.env.XR_UI ?? '..') + '/iterations/iter-15/dash7/desk'
mkdirSync(OUT, { recursive: true })
let bad = 0
const check = (what, ok, got) => { if (!ok) bad += 1; console.log(`${ok ? 'PASS' : 'FAIL'} ${what}: ${JSON.stringify(got).slice(0, 900)}`) }
const note = (what, got) => console.log(`  ${what}: ${JSON.stringify(got).slice(0, 900)}`)
const DEG = 180 / Math.PI

const got = await open({ fixture: true, emulate: true, query: process.env.DESK_QUERY ?? '&words=1&unread=mira,sage&up=', viewport: { width: AURA.width, height: AURA.height } })
const { page } = got
await waitUntil(async () => { const c = (await read(page)).crew; return c.length > 0 && c.every(b => b.drawn) }, 90000, 400)
await page.evaluate(() => { window.__xrPages.stroll?.(false); window.__xrDash3d.hold(true) })
await waitUntil(async () => { const d = await dashOf(page); return d.atlas && d.mira }, 20000, 300)
await enterAura(page)
await parkHands(page)
await emu.frames(page, 20)

const head = { eye: [0, EYE, 0], yaw: 0, pitch: 0 }
const setHead = async (eye, yaw, pitch, frames = 14) => { head.eye = eye; head.yaw = yaw; head.pitch = pitch; await emu.setHead(page, { position: eye, yawDeg: -yaw, pitchDeg: pitch }); await emu.frames(page, frames) }
const aimOf = (to, from) => { const d = to.map((v, i) => v - from[i]); return { yaw: Math.atan2(d[0], -d[2]) * DEG, pitch: Math.atan2(d[1], Math.hypot(d[0], d[2])) * DEG } }
const lookAt = async (to, from = head.eye, frames = 14) => { const a = aimOf(to, from); await setHead(from, a.yaw, a.pitch, frames); return a }
/** The head turned from where it is so the lens's middle (the dot) lies on picture point `p` ([x, y], -1 to 1). */
const aimPoint = (p, frames = 14) => setHead(head.eye, head.yaw + Math.atan(p[0] * Math.tan(AURA.fovxDeg / 2 / DEG)) * DEG, head.pitch + Math.atan(p[1] * Math.tan(AURA.fovyDeg / 2 / DEG)) * DEG, frames)
const pose = () => ({ eye: head.eye.map(v => +v.toFixed(3)), yawDeg: +head.yaw.toFixed(1), pitchDeg: +head.pitch.toFixed(1), lensDeg: AURA.fovyDeg })
const shoot = async tag => { await page.screenshot({ path: `${OUT}/${name}-${tag}.png` }); note(`still ${name}-${tag}.png`, { pose: pose() }) }
const pinch = async () => {
  await emu.select(page, { hand: 'right', down: true }); await emu.frames(page, 1)
  await emu.select(page, { hand: 'right', down: false }); await emu.frames(page, 8)
}
const marks = async () => (await read(page)).boardMarks ?? {}
const boardsNow = async () => (await read(page)).boards?.boards ?? []
const boxOf = m => { const c = (m.corners ?? []).filter(Boolean); if (c.length < 4) return null; return [Math.min(...c.map(p => p[0])), Math.min(...c.map(p => p[1])), Math.max(...c.map(p => p[0])), Math.max(...c.map(p => p[1]))] }
const r3 = v => +v.toFixed(3)

// The desk lines: words boards at a Bot, far enough to be at a desk.
await setHead([0, EYE, 0], 0, 0, 30)
const deskLines = async () => Object.entries(await marks()).filter(([, m]) => m.desk)
const desks = (await waitUntil(async () => { await emu.frames(page, 4); const d = await deskLines(); return d.length ? d : null }, 15000, 200)) || []
note('all boards at Bots', Object.entries(await marks()).map(([id, m]) => ({ id, body: m.body.map(r3), desk: !!m.desk, front: m.front, dist: r3(Math.hypot(m.body[0], m.body[1] - EYE, m.body[2])) })))
note('desk lines', { desks: desks.map(([id, m]) => ({ id, body: m.body.map(r3), front: m.front, line: m.desk.line })), boards: (await boardsNow()).map(b => [b.needId, b.bot, b.kind, b.where]) })
if (!desks.length) { check('the sample with words has a desk line', false, { boards: await boardsNow() }); await got.browser.close(); process.exit(1) }

// r6: at rest every desk line is in front.
check('r6: no form taken, each desk line in front (front=true)', desks.every(([, m]) => m.front === true), desks.map(([id, m]) => [id, m.front]))

// The desk line to work on, and a form that is not its own Bot's: the nearest tray to the room's middle.
const [deskId, desk0] = desks[0]
const deskBot = (await boardsNow()).find(b => b.needId === deskId)?.bot
const trays = await dashOf(page)
const formSlug = ['atlas', 'mira', 'juno'].find(s => trays[s] && s !== deskBot) ?? Object.keys(trays)[0]
note('pick', { deskId, deskBot, form: formSlug, deskBody: desk0.body.map(r3) })

// The take: dash4-taken's own (its tries from the room's middle and from in front of the tray, 19 degrees down for Atlas).
/** The head turned from `h0` (where the picture box `nd` was read) so the lens's middle lies on `nd`'s middle. */
const aimNdc = (eye, h0, nd, frames = 14) => setHead(eye, h0.yaw + Math.atan(((nd[0] + nd[2]) / 2) * Math.tan(AURA.fovxDeg / 2 / DEG)) * DEG, h0.pitch + Math.atan(((nd[1] + nd[3]) / 2) * Math.tan(AURA.fovyDeg / 2 / DEG)) * DEG, frames)
/** The drawn boxes of a form's parts that are in the lens, the build's own (`partsNdc`). */
const partMids = d => {
  const parts = (d?.partsNdc ?? []).filter(n => n && n.every(Number.isFinite) && n[0] > -0.95 && n[2] < 0.95 && n[1] > -0.95 && n[3] < 0.95)
  // A tray with no parts (a broken layout: Mira's sample says "unknown version") has only its grey tray to aim at.
  return parts.length ? parts : d?.ndc && d.ndc.every(Number.isFinite) ? [d.ndc] : []
}
const mid = b => [(b[0][0] + b[1][0]) / 2, (b[0][1] + b[1][1]) / 2, (b[0][2] + b[1][2]) / 2]
const onWhat = async () => (await read(page)).pinch?.head?.on?.what ?? null
/** A take: the dot moved over the tray's points until the probe says it lies on the dashboard, then a pinch at once. */
const take = async (slug, downDeg = null) => {
  const tries = []
  // An eye placed so the tray's top lies `downDeg` under his eye line, on the line from the tray to the room's middle.
  const at19 = r => { const h = Math.hypot(r.at[0], r.at[2]) || 1; const far = (EYE - r.at[1]) / Math.tan(downDeg / DEG); return [r.at[0] - (r.at[0] / h) * far, EYE, r.at[2] - (r.at[2] / h) * far] }
  const front = r => { const [a, b] = r.box; const dx = b[0] - a[0], dz = b[2] - a[2]; const n = dx < dz ? [1, 0] : [0, 1]; const s = Math.sign(-(n[0] * r.at[0] + n[1] * r.at[2])) || 1; return [r.at[0] + n[0] * s * 1.2, EYE, r.at[2] + n[1] * s * 1.2] }
  const r0 = (await dashOf(page))[slug]
  if (!r0) return { t: null, tries: ['no tray'] }
  const froms = [[0, EYE, 0], [0.4, EYE, 0], [-0.4, EYE, 0], [0, EYE, 0], front(r0), front(r0)]
  if (downDeg !== null && r0.at[1] < EYE - 0.1) froms.unshift(at19(r0), at19(r0))
  for (const from of froms) {
    const r = (await dashOf(page))[slug]
    const bot = (await read(page)).crew.find(b => b.slug === slug)
    const [a, b] = r.box; const c = mid(r.box)
    const lower = bot ? [bot.feet[0], bot.feet[1] + (bot.top[1] - bot.feet[1]) * 0.2, bot.feet[2]] : c
    let on = null
    for (const p of [[r.at[0], r.at[1] - 0.004, r.at[2]], c, [c[0], a[1] + (b[1] - a[1]) * 0.25, c[2]], [a[0] + (b[0] - a[0]) * 0.25, c[1], c[2]], [a[0] + (b[0] - a[0]) * 0.75, c[1], c[2]], lower]) {
      // The dot on a dashboard, and that dashboard the one asked for: it lights (`lit`). Another Bot's tray between
      // (w1110: a pinch on Atlas's from Mira's front) is not taken.
      await lookAt(p, from); on = await onWhat(); if (/^dash/.test(on ?? '') && (await dashOf(page))[slug]?.lit) break
    }
    // Then its DRAWN middle, as the build pictures it from here: a grey tray of a broken layout is small and low, and
    // the room-box points missed it (w1127: three of the reasons).
    if (!/^dash/.test(on ?? '') || !(await dashOf(page))[slug]?.lit) {
      const h0 = { yaw: head.yaw, pitch: head.pitch }; const n = (await dashOf(page))[slug]?.ndc
      if (n && n.every(Number.isFinite)) { await aimNdc(from, h0, n); on = await onWhat() }
    }
    if (!/^dash/.test(on ?? '') || !(await dashOf(page))[slug]?.lit) { tries.push({ from, on, lit: (await dashOf(page))[slug]?.lit }); continue }
    const at = { ...head }
    // D140 D: what the build did with the click, frame by frame: where the tray stands, and the boards that sent a
    // taken tray back (`dashWork.sentBack`: a board that came up over it).
    const sent0 = await page.evaluate(() => (window.__xrDashWork?.sentBack ?? []).length)
    await pinch()
    const track = []
    for (let f = 0; f < 6; f += 1) { await emu.frames(page, 1); const x = (await dashOf(page))[slug]; track.push(x?.where ?? null) }
    let t = (await dashOf(page))[slug]
    const sent = await page.evaluate(n => (window.__xrDashWork?.sentBack ?? []).slice(n), sent0)
    tries.push({ from, last: (await read(page)).pinch?.last, track, sentBack: sent.map(s => s.board), presses: (await dashOf(page))[slug]?.presses?.slice(-4) })
    // Judged once it stands: come all the way (`fit.mix` 1) and fitted (`fit.n` 4). Read after six frames it was still
    // on its way up from the tray (w1114: its foot at -1.58 at the take).
    if (t?.where === 'taken') {
      t = (await waitUntil(async () => { await emu.frames(page, 2); const x = (await dashOf(page))[slug]; return x?.fit && x.fit.mix === 1 && x.fit.n >= 4 ? x : null }, 4000, 50)) || (await dashOf(page))[slug]
      return { t, tries, at }
    }
  }
  return { t: null, tries }
}
// The take must come from the eye 19 degrees over Atlas's tray (the take's first two tries): from another eye the form
// stands elsewhere and the pose search below misses the desk line (w1197, w1198, w1211: taken from [-0.4, 1.6, 0], 3
// misses of 3 on m113 and m112 alike; w1212 from the 19 degree eye: the pose found). Else it is sent back and taken again.
const allTries = []
const takeIt = async () => {
  for (let k = 0; k < 4; k += 1) {
    const r = await take(formSlug, formSlug === 'atlas' ? 19 : null)
    note('take: tries', r.tries)
    allTries.push(...r.tries)
    writeFileSync(`${OUT}/${name}-take-tries.json`, JSON.stringify(allTries))
    if (!r.t || formSlug !== 'atlas' || r.tries.length <= 2) return r.t
    const x = (await dashOf(page))[formSlug]
    await lookAt([x.at[0], x.at[1] - 0.004, x.at[2]]); await pinch(); await emu.frames(page, 40); await sleep(500)
  }
  return null
}
// RETAKES=N (D147): the form sent back and taken again N times first, each click logged (`take-drops.py`): the
// re-take is where a click on the tray was dropped (w1236: 11 of 16).
for (let k = 0; k < Number(process.env.RETAKES ?? 0); k += 1) {
  const t = await takeIt()
  if (!t) break
  const x = (await dashOf(page))[formSlug]
  await lookAt([x.at[0], x.at[1] - 0.004, x.at[2]]); await pinch(); await emu.frames(page, 40); await sleep(500)
}
// M140: the re-takes' bar in the row itself, as `take-drops.py` counts it: at least 20 clicks whose down and up read
// the tray, none of which left it at rest.
if (Number(process.env.RETAKES ?? 0) > 0) {
  const clicks = allTries.filter(t => t.last?.kind === 'click' && /^dash:/.test(t.last?.down ?? '') && /^dash:/.test(t.last?.up ?? '') && t.track)
  const dropped = clicks.filter(t => !t.track.includes('taken'))
  check(`retakes: ${clicks.length} clicks with down and up on the tray (at least 20), ${dropped.length} left it at rest (none)`, clicks.length >= 20 && dropped.length === 0, { clicks: clicks.length, dropped: dropped.length })
}
const t1 = await takeIt()
check('the form is taken', !!t1, { form: formSlug, at: t1?.at })
if (!t1) { await got.browser.close(); process.exit(1) }

// Turn his look so the desk line's middle is just over the form's top in the picture: the form follows his look, so
// it then lies over the board's lower part. Three tries, each read from the build's boxes.
let over = null
for (let i = 0; i < 6; i += 1) {
  const f = (await dashOf(page))[formSlug]
  const m = (await marks())[deskId]
  const b = m && boxOf(m)
  if (!f?.ndc || !b) break
  const midX = (b[0] + b[2]) / 2, midY = (b[1] + b[3]) / 2
  // Where the board's middle should be: over the form's top by a third of the board's height.
  const wantY = f.ndc[3] + (b[3] - b[1]) / 6
  const wantX = (f.ndc[0] + f.ndc[2]) / 2
  // Its LOWER part only: the form's top between the board's foot and its middle, so the eye's line to the board's
  // middle passes over the form (that line alone covered a board before `takenCovers`).
  over = { form: f.ndc.map(r3), board: b.map(r3), lower: f.ndc[3] > b[1] + 0.01 && f.ndc[3] < midY - 0.01 && f.ndc[0] < b[2] && f.ndc[2] > b[0] }
  if (over.lower && Math.abs(midX - wantX) < 0.15) break
  // A turn moves the form and the board alike while the form stands; only the eye's height moves them apart (the
  // form is near, the board far). Raised by dy, the near form sinks against the far board by dy (1/near - 1/far).
  const near = Math.hypot(...f.at.map((v, j) => v - head.eye[j])), far = Math.hypot(...m.body.map((v, j) => v - head.eye[j]))
  const want = midY - 0.04, k = Math.tan(AURA.fovyDeg / 2 / DEG)
  const dy = Math.max(-0.12, Math.min(0.12, ((f.ndc[3] - want) * k) / (1 / near - 1 / far)))
  const eye2 = [head.eye[0], head.eye[1] + dy, head.eye[2]]
  // Turn so the board moves by (want - now) in the picture: a turn of the head moves the room the other way.
  await setHead(eye2, head.yaw + Math.atan((midX - wantX) * Math.tan(AURA.fovxDeg / 2 / DEG)) * DEG, head.pitch + Math.atan((midY - wantY) * k) * DEG, 10)
  // The form comes again where he looks now: 500 ms at rest, a 300 ms glide.
  await waitUntil(async () => { await emu.frames(page, 3); const x = (await dashOf(page))[formSlug]; return x && !x.gliding && x.where === 'taken' ? x : null }, 2500, 50)
  await sleep(700); await emu.frames(page, 20)
}
const look0 = { ...head, eye: [...head.eye] }
const mOver = (await marks())[deskId]
check('the pose: the taken form lies over the desk line\'s lower part in the picture', !!over?.lower, { over, pose: pose() })
note('the desk line under the form', { front: mOver?.front, desk: !!mOver?.desk })
await shoot('over')

// OUTLINE_SCAN=1: what the dot lies on over a grid across the taken form's drawn box and a tenth past it.
if (process.env.OUTLINE_SCAN) {
  const f0 = (await dashOf(page))[formSlug], h0 = { ...head }
  const n = f0.ndc, gx = [-0.1, 0, 0.17, 0.33, 0.5, 0.67, 0.83, 1, 1.1], gy = [-0.1, 0, 0.25, 0.5, 0.75, 1, 1.1]
  const grid = []
  for (const v of gy) {
    const row = []
    for (const u of gx) {
      const p = [n[0] + (n[2] - n[0]) * u, n[1] + (n[3] - n[1]) * v]
      await setHead(h0.eye, h0.yaw, h0.pitch, 1)
      await aimPoint(p, 2)
      const on = await onWhat()
      row.push(on === null ? '.' : /^dash/.test(on) ? 'D' : on === 'board-paper' ? 'B' : on.slice(0, 1))
    }
    grid.push(row.join(''))
  }
  note('outline scan (rows from the foot up; D the form, B the board, . nothing)', { ndc: n.map(r3), still: (await dashOf(page))[formSlug]?.where, grid })
}
// r1: the dot on a solid part of the form inside the overlap, then a pinch.
const f1 = (await dashOf(page))[formSlug]
const b1 = boxOf(mOver)
const inOverlap = (f1?.partsNdc ?? []).map((n, i) => ({ n, i })).filter(({ n }) => n && n.every(Number.isFinite) && n[3] > b1[1] && n[1] < b1[3] && n[0] < b1[2] && n[2] > b1[0])
note('parts inside the overlap', inOverlap.map(({ n, i }) => ({ i, n: n.map(r3) })))
const shared = n => Math.max(0, Math.min(n[2], b1[2]) - Math.max(n[0], b1[0])) * Math.max(0, Math.min(n[3], b1[3]) - Math.max(n[1], b1[1]))
const part = inOverlap.sort((a, b) => shared(b.n) - shared(a.n))[0] ?? (f1?.ndc ? { n: f1.ndc, i: -1 } : null)
if (part) {
  // The part's top half, where it overlaps the board.
  // The middle of where the part (or, with no part there, the form's drawn box) and the board overlap.
  const p = [(Math.max(part.n[0], b1[0]) + Math.min(part.n[2], b1[2])) / 2, (Math.max(part.n[1], b1[1]) + Math.min(part.n[3], b1[3])) / 2]
  const before = (await boardsNow()).find(b => b.needId === deskId)
  // At once after the aim, as in r3: no still between (the form must not come again where he looks meanwhile).
  await aimPoint(p, 4)
  const on = await onWhat()
  const mNow = (await marks())[deskId]
  await pinch()
  note('r1 the dot', { on, part: part.i, at: p.map(r3), front: mNow?.front, pose: pose() })
  await emu.frames(page, 10)
  const after = (await boardsNow()).find(b => b.needId === deskId)
  const f = (await dashOf(page))[formSlug]
  check(`r1: the dot ${part.i >= 0 ? 'on a solid part of the form' : 'inside the form\'s drawn box'}, inside the overlap, is the form's`, /^dash/.test(on ?? ''), { on })
  check('r1: the desk line under the taken form is not in front then', mNow?.front === false, { front: mNow?.front })
  check('r1: a pinch there sends the form back, and the board is not taken', f?.where !== 'taken' && after?.kind === before?.kind && after?.where === 'bot', { form: f?.where, board: [before?.kind, before?.where, '->', after?.kind, after?.where], last: (await read(page)).pinch?.last })
  await shoot('r1-after')
  // Row 2 (D130): the same picture without the form. Back to the pose of `over`, the form in its Bot's hands; the two
  // stills and the boxes go to `r2-pixels.py`: inside the form's box the picture changes, and the board's part above
  // the form is the same with the form and without it (what the form does not cover is not hidden).
  await setHead(look0.eye, look0.yaw, look0.pitch, 20); await sleep(600); await emu.frames(page, 20)
  await shoot('r2-without')
  note('r2 boxes', { form: f1.ndc.map(r3), board: b1.map(r3), formNow: (await dashOf(page))[formSlug]?.where, size: [AURA.width, AURA.height] })
}

// r3: take it again, the same pose; the dot on the board 2 cm above the form's outline, then a pinch.
const t3 = await takeIt()
if (t3) {
  await setHead(look0.eye, look0.yaw, look0.pitch, 10)
  await waitUntil(async () => { await emu.frames(page, 3); const x = (await dashOf(page))[formSlug]; return x && !x.gliding && x.where === 'taken' ? x : null }, 2500, 50)
  await sleep(700); await emu.frames(page, 20)
  const f = (await dashOf(page))[formSlug]
  const m = (await marks())[deskId]
  const b = boxOf(m)
  // 2 cm at the board, as a share of the picture: the board's own height in metres against its height in the picture.
  const up = m.sizeM[1] > 0 ? (0.02 / m.sizeM[1]) * (b[3] - b[1]) : 0.02
  const p = [Math.max(b[0] + 0.01, Math.min(b[2] - 0.01, (f.ndc[0] + f.ndc[2]) / 2)), f.ndc[3] + up]
  // At once: a form whose dot has left it comes again where he looks after 500 ms at rest (`HELD_COME`), and it would
  // then stand under the dot. So the pinch goes before that, and the still after it.
  await aimPoint(p, 4)
  const on = await onWhat()
  await pinch()
  note('r3 the dot', { on, at: p.map(r3), formTop: r3(f.ndc[3]), boardTop: r3(b[3]), pose: pose() })
  await emu.frames(page, 10)
  const after = (await boardsNow()).find(x => x.needId === deskId)
  check('r3: a pinch on the board 2 cm above the form\'s outline takes the board', after?.where === 'held', { on, board: after && [after.kind, after.where], last: (await read(page)).pinch?.last })
  await shoot('r3-after')
} else check('r3: the form taken again', false, {})

note('errors', got.errors.slice(0, 5))
console.log(`TOTAL bad ${bad}`)
await got.browser.close().catch(() => {})
process.exit(0)
