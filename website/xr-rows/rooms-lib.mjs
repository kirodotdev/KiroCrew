// Round 15, NAMED ROOMS (D37): what the rooms' stills, rows and film share. An emulated session on the Aura's lens,
// places on, the sample crew; three rooms kept ("Review", "Build", "Crew"), he stands in "Review".
import { mkdirSync } from 'node:fs'
import { ITER, openRoom, read, sleep, waitUntil, wholeWindow } from './chatcard-lib.mjs'
import { emu } from './xr-emu/xr-emu-lib.mjs'

export { ITER, read, sleep, waitUntil, emu }
export const DIR = `${ITER}/rooms`
mkdirSync(DIR, { recursive: true }); mkdirSync(`${ITER}/rows`, { recursive: true })
export const EYE = 1.6
export const FOVY = 40.7
export const pg = (path, yawDeg, css, pitchDeg = -4, distanceM = 1.9) => ({ path, at: { yawDeg, pitchDeg, distanceM }, css, byHand: true })
export const LAYOUTS = {
  r1: [pg('/chat', 0, [1280, 800]), pg('/schedule', -48, [900, 720]), pg('/artifacts', 48, [900, 720])],
  r2: [pg('/chat', -30, [1000, 700]), pg('/projects', 20, [1100, 760]), pg('/notifications', 58, [720, 720])],
  r3: [pg('/members', 0, [1300, 820]), pg('/sessions', -46, [900, 720])],
}
export const NAMES = { r1: 'Review', r2: 'Build', r3: 'Crew' }
/** More of a room's layout than its pages (where he put Bots), by room: a tool sets it before `start`. */
export const MORE = {}
export const storageOf = (ids = ['r1', 'r2', 'r3'], told = false) => ({
  'kirocrew:xr-room': JSON.stringify({ v: 1, room: 'r1', pages: [] }),
  'kirocrew:xr-rooms': JSON.stringify({ v: 1, now: 'r1', told, rooms: ids.map(id => ({ id, name: NAMES[id], named: true, layout: JSON.stringify({ v: 1, ...(MORE[id] ?? {}), pages: id === 'r1' ? [] : LAYOUTS[id] }) })) }),
})

export function row(name) {
  const out = { row: name, checks: [], notes: [], shots: [] }
  const check = (label, ok, value) => { out.checks.push({ name: label, ok: !!ok, value }); console.log(`${ok ? 'PASS' : 'FAIL'} ${label}: ${JSON.stringify(value ?? null).slice(0, 400)}`); return !!ok }
  const note = (label, value) => { out.notes.push({ name: label, value }); console.log(`  ${label}: ${JSON.stringify(value).slice(0, 500)}`) }
  return { out, check, note }
}

/**
 * The room open in a session, room one's pages standing and live. Room one's pages are opened under ids of this
 * tool's own: on the shared dev gateway a page of a record written into the store by hand (ids "page", "kept-n")
 * stays loading (ROOMS-1.md, "seen, cause not proven").
 * XR_ROOMS_QUERY adds words to the address for every row (`&ahead=0`: no room is warmed ahead, D46).
 *
 * `own: true` (or XR_ROOMS_OWN=1), off by default (TOOLS-15.md): room one's pages are opened the room's own way.
 * The chat page that the room opens with stays (its id is "page"), and the others are opened as the palette opens
 * them (`__xrPages.page`, the product's `openPage`), so each stands where the room itself puts it. A page opened
 * so is stood by the room, and a load stands it where it stood; a page opened by `__xrPages.open` with a place
 * named in the crew's place is stood elsewhere by the next load (RELOAD-ENTER.md). `pages`: how many of room
 * one's pages (own way only). `fixture: false`: the gateway's real Bots, not the sample crew.
 */
export async function start({ ids, told = false, video = null, query = '', viewport, own = process.env.XR_ROOMS_OWN === '1', pages = LAYOUTS.r1.length, fixture = true } = {}) {
  const got = await openRoom({ emulate: true, speech: 'fake', query: `${fixture ? '&fixture=1&crewn=7' : ''}${query}${process.env.XR_ROOMS_QUERY ?? ''}`, storage: storageOf(ids, told), video, ...(viewport ? { viewport } : {}) })
  const { page } = got
  await page.waitForFunction(() => !!window.__xrPages, null, { timeout: 180000 })
  await waitUntil(async () => { const c = (await read(page).catch(() => null))?.crew ?? []; return !fixture || (c.length > 0 && c.every(b => b.drawn && b.shown)) }, 180000, 500)
  const count = own ? pages : LAYOUTS.r1.length
  if (own) {
    if (!(await page.evaluate(() => typeof window.__xrPages.page === 'function'))) throw new Error('own: this build has no __xrPages.page (the hook word came with the warm rooms, ecb1c05517f)')
    await page.evaluate(() => window.__xrPages.stroll(false))
    // One at a time: each is stood clear of the ones that stand.
    for (const [i, k] of LAYOUTS.r1.slice(1, count).entries()) { await page.evaluate(p => window.__xrPages.page(p), k.path); await waitUntil(async () => Object.keys((await read(page)).sources).length === i + 2, 20000, 100) }
  } else {
    await page.evaluate(() => { window.__xrPages.stroll(false); window.__xrPages.close('page') })
    for (const [i, k] of LAYOUTS.r1.entries()) await page.evaluate(([id, q]) => window.__xrPages.open({ id, live: q.path, yawDeg: q.at.yawDeg, pitchDeg: q.at.pitchDeg, distanceM: q.at.distanceM, cssWidth: q.css[0], cssHeight: q.css[1] }), [`rs-${i}`, k])
  }
  await waitUntil(async () => { const r = await read(page); return Object.keys(r.sources).length === count && Object.values(r.sources).every(v => v.status === 'live') ? r : null }, 90000, 500)
  const h = hands(page)
  await page.evaluate(f => { window.__xrEmuDevice.fovy = (f * Math.PI) / 180; window.__xrEmuDevice.ipd = 0 }, FOVY)
  await emu.setInputMode(page, 'hand')
  await h.look(0, 0)
  await page.click('[data-testid="xr-enter"]')
  await page.waitForFunction(() => window.__xrPages.read().inSession, null, { timeout: 30000 })
  await wholeWindow(page)
  await h.park()
  await emu.frames(page, 8)
  await waitUntil(async () => { await emu.frames(page, 2); const r = await read(page); return Object.values(r.sources).every(v => v.status === 'live' && v.seen?.since != null) ? r : null }, 90000, 500)
  await emu.frames(page, 4)
  return { ...got, ...h }
}

/** The tab loaded again and the session entered anew, as `start` enters it. `h`: what `start` gave. */
export async function again(page, h, { fixture = true } = {}) {
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForFunction(() => !!window.__xrPages, null, { timeout: 180000 })
  await waitUntil(async () => { const c = (await read(page).catch(() => null))?.crew ?? []; return !fixture || (c.length > 0 && c.every(b => b.drawn && b.shown)) }, 180000, 500)
  await page.evaluate(() => window.__xrPages.stroll(false))
  await page.evaluate(f => { window.__xrEmuDevice.fovy = (f * Math.PI) / 180; window.__xrEmuDevice.ipd = 0 }, FOVY)
  await emu.setInputMode(page, 'hand')
  await h.look(0, 0)
  await page.click('[data-testid="xr-enter"]')
  await page.waitForFunction(() => window.__xrPages.read().inSession, null, { timeout: 30000 })
  await wholeWindow(page)
  await h.park()
  await emu.frames(page, 8)
}

export function hands(page) {
  // Where the head looks now: a point of the room is put on the screen by it (the session's own eye, the Aura's lens).
  const now = { yaw: 0, pitch: 0 }
  const look = (yawDeg, pitchDeg = 0) => { now.yaw = yawDeg; now.pitch = pitchDeg; return emu.setHead(page, { position: [0, EYE, 0], yawDeg: -yawDeg, pitchDeg }) }
  const onScreen = p => {
    const vp = page.viewportSize()
    const y = (now.yaw * Math.PI) / 180; const t = (now.pitch * Math.PI) / 180
    const d = [p[0], p[1] - EYE, p[2]]
    // Turn the world back by the head's yaw (to the right is +), then by its pitch (up is +).
    const x1 = d[0] * Math.cos(y) + d[2] * Math.sin(y); const z1 = -d[0] * Math.sin(y) + d[2] * Math.cos(y)
    const y2 = d[1] * Math.cos(t) + z1 * Math.sin(t); const z2 = -d[1] * Math.sin(t) + z1 * Math.cos(t)
    const k = Math.tan((FOVY * Math.PI) / 360)
    return { x: vp.width / 2 + (x1 / -z2 / (k * (vp.width / vp.height))) * (vp.width / 2), y: vp.height / 2 - (y2 / -z2 / k) * (vp.height / 2) }
  }
  /** A flat thing's corners on the screen, in order. */
  const quadOf = points => points.map(onScreen)
  const rectOf = points => { const at = points.map(onScreen); return { x0: Math.min(...at.map(q => q.x)), y0: Math.min(...at.map(q => q.y)), x1: Math.max(...at.map(q => q.x)), y1: Math.max(...at.map(q => q.y)) } }
  /** Home's strip: its corners in the room, from its middle and size (it faces the start head). */
  const stripCorners = async () => {
    const h = (await read(page)).home
    if (!h) return null
    const [x, yy, z] = h.center
    const far = Math.hypot(x, z) || 1
    const right = [-z / far, 0, x / far]
    return [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([sx, sy]) => [x + (right[0] * sx * h.size[0]) / 2, yy + (sy * h.size[1]) / 2, z + (right[2] * sx * h.size[0]) / 2])
  }
  const dotAt = async at => { const d = at.map((v, i) => v - [0, EYE, 0][i]); await look((Math.atan2(d[0], -d[2]) * 180) / Math.PI, (Math.atan2(d[1], Math.hypot(d[0], d[2])) * 180) / Math.PI); await emu.frames(page, 4) }
  const park = async () => { for (const hand of ['left', 'right']) await emu.setRay(page, { hand, origin: [hand === 'left' ? -0.3 : 0.3, 1, 0.2], direction: [hand === 'left' ? -0.577 : 0.577, -0.577, 0.577] }); await emu.frames(page, 3) }
  const pinchOf = async () => (await read(page)).pinch
  const down = async (hand = 'right') => { const before = JSON.stringify((await pinchOf())?.last ?? null); await emu.select(page, { hand, down: true }); await waitUntil(async () => JSON.stringify((await pinchOf())?.last ?? null) !== before, 3000, 15) }
  const up = async (hand = 'right') => { await emu.select(page, { hand, down: false }); await waitUntil(async () => (await pinchOf())?.last?.up !== '', 2000, 15); await emu.frames(page, 2) }
  /** The left palm up, turned to the face: the palette shows. `at`: where the palm is. */
  const palm = async (at = [-0.1, 1.42, -0.42]) => {
    await emu.setHandPose(page, { hand: 'left', position: at, palmTowardHead: true })
    const shown = await waitUntil(async () => { await emu.frames(page, 3); const p = (await read(page)).palette; return p?.side && p.rooms?.length ? p : null }, 6000, 50)
    // The palette is mounted in one frame and placed in the next (`Palette.tsx`): read it once it has been placed, or
    // its tiles' places are the plain matrix's, metres off (D70 3).
    if (!shown) return shown
    await emu.frames(page, 1)
    return (await read(page)).palette ?? shown
  }
  const palmAway = async () => { await emu.setHandPose(page, { hand: 'left', clear: true }); await park(); await waitUntil(async () => { await emu.frames(page, 2); return !(await read(page)).palette?.side }, 4000, 50) }
  /** The dot on a card (the palette turns to the head as the head turns: aim, read, aim again). */
  const onCard = async id => {
    for (let i = 0; i < 3; i += 1) { const p = (await read(page)).palette; const c = p?.rooms.find(r => r.id === id); if (!c) throw new Error(`no card ${id}`); await dotAt(c.at); await emu.frames(page, 3) }
    return (await pinchOf())?.head?.on?.what
  }
  const pinchCard = async id => { const on = await onCard(id); await down(); await emu.frames(page, 2); await up(); return on }
  return { look, dotAt, park, pinchOf, down, up, palm, palmAway, onCard, pinchCard, onScreen, rectOf, quadOf, stripCorners, where: () => ({ ...now }) }
}

export const meet = (a, b) => a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1
/** Whether two convex four-corners on the screen share a point: no edge of either parts them. */
export function quadsMeet(a, b) {
  for (const q of [a, b]) for (let i = 0; i < q.length; i += 1) {
    const p0 = q[i]; const p1 = q[(i + 1) % q.length]
    const n = { x: p1.y - p0.y, y: p0.x - p1.x }
    const span = pts => { const d = pts.map(p => p.x * n.x + p.y * n.y); return [Math.min(...d), Math.max(...d)] }
    const [a0, a1] = span(a); const [b0, b1] = span(b)
    if (a1 <= b0 || b1 <= a0) return false
  }
  return true
}
/** A crop for a screenshot round a rectangle, with a margin, inside the window. */
export function clipOf(page, r, margin = 60) {
  const vp = page.viewportSize()
  const x = Math.max(0, r.x0 - margin); const y = Math.max(0, r.y0 - margin)
  return { x, y, width: Math.min(vp.width, r.x1 + margin) - x, height: Math.min(vp.height, r.y1 + margin) - y }
}
