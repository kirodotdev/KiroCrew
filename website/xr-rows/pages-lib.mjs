// Shared helpers for the checks of /xr?view=pages (iteration 8): open the workspace of pages on the throwaway dev
// gateway, hand it stills of the real dashboard through its test hook, read its probe, and stand in for the Aura.
import { readFileSync } from 'node:fs'
import { launch, BASE } from './v3-lib.mjs'
import { installEmulator, emu } from './xr-emu/xr-emu-lib.mjs'

export { emu, BASE }
export const STILLS = process.env.XR_STILLS ?? (process.env.XR_UI ?? '..') + '/iterations/iter-08/stills'
export const INDEX = JSON.parse(readFileSync(`${STILLS}/index.json`, 'utf8'))

/** What one eye of the XREAL Aura shows: 1920 x 1200 pixels over 61.4 x 40.7 degrees (70 degrees diagonal, flat, 16:10). */
export const AURA = { width: 1920, height: 1200, fovyDeg: 40.7, fovxDeg: 61.4 }

/** The page's own numbers, kept here too so a tool can aim: metres for one CSS pixel, and where a panel opens. */
export const M_PER_PX = 0.000868 * 1.75
export const DISTANCE = 1.75
export const EYE = 1.6

/**
 * A browser whose window is one eye of the Aura, with the emulated runtime in it. The emulator draws one eye over the
 * whole window (it is not in stereo), so a picture of the window IS one eye's picture, pixel for pixel.
 * `scale` below 1 draws that eye with fewer pixels: 0.707 stands in for the loss of a second resampling.
 */
export async function launchAura({ scale = 1, video = null } = {}) {
  const w = Math.round(AURA.width * scale)
  const h = Math.round(AURA.height * scale)
  const opened = await launch({ viewport: { width: w, height: h }, deviceScaleFactor: 1, ...(video ? { recordVideo: { dir: video, size: { width: w, height: h } } } : {}) })
  await installEmulator(opened.context)
  // Stills are test content: they are answered from the folder they were taken into, under this page's own origin.
  await opened.context.route('**/xr-stills/**', route => {
    const name = decodeURIComponent(new URL(route.request().url()).pathname.split('/').pop())
    route.fulfill({ path: `${STILLS}/${name}`, contentType: 'image/png' }).catch(() => route.abort())
  })
  const outside = []
  opened.page.on('request', r => { if (/^https?:/.test(r.url()) && new URL(r.url()).host !== new URL(BASE).host) outside.push(r.url()) })
  return { ...opened, outside, width: w, height: h }
}

/** Open the workspace of pages with its test hook, and empty it. */
export async function openPages(page, query = '') {
  await page.goto(`${BASE}/xr?view=pages&probe=1${query ? `&${query}` : ''}`, { waitUntil: 'domcontentloaded', timeout: 120000 })
  await page.waitForSelector('[data-testid="xr-canvas"]', { timeout: 120000 })
  const skip = page.getByText('Skip all').first()
  if (await skip.waitFor({ state: 'visible', timeout: 4000 }).then(() => true, () => false)) {
    await skip.click({ timeout: 2500 }).catch(() => {})
    await skip.waitFor({ state: 'hidden', timeout: 5000 }).catch(() => {})
  }
  await page.waitForFunction(() => !!window.__xrPages, null, { timeout: 60000 })
  await page.evaluate(() => window.__xrPages.clear())
}

export const read = page => page.evaluate(() => window.__xrPages.read())
export const hook = (page, name, arg) => page.evaluate(([n, a]) => window.__xrPages[n](a), [name, arg ?? null])

/** The stills of one page in one theme at one sharpness, as the test hook takes them. `only` keeps one size. */
export function stillsOf(name, theme, dpr, only = null) {
  return INDEX.stills.filter(s => s.page === name && s.theme === theme && s.dpr === dpr && (!only || s.cssWidth === only))
    .map(s => ({ url: `/xr-stills/${encodeURIComponent(s.file)}`, cssWidth: s.cssWidth, cssHeight: s.cssHeight, dpr: s.dpr }))
}

/** Open one panel with a still of the real dashboard, and wait until its picture is there. */
export async function openStill(page, id, { name = 'chat', theme = 'dark', dpr = 2, cssWidth = 1280, only = null, yawDeg = 0, pitchDeg = 0, distanceM = DISTANCE } = {}) {
  await hook(page, 'open', { id, stills: stillsOf(name, theme, dpr, only), cssWidth, yawDeg, pitchDeg, distanceM })
  await page.waitForFunction(i => { const s = window.__xrPages.read().sources[i]; return !!s?.shown?.loaded }, id, { timeout: 60000 })
}

/** Start the immersive session as the page's own button does, as the Aura sees: one eye, 61.4 x 40.7 degrees. */
export async function enterAura(page) {
  await readyAura(page)
  await page.click('[data-testid="xr-enter"]')
  await inAura(page)
}
/** What the emulator is told before a session starts: the Aura's eye, hands, and a level head. */
export async function readyAura(page) {
  // The emulator draws one eye, set a little to the left of the head. With no distance between the eyes that eye is at
  // the head, so the middle of the picture is straight ahead.
  await page.evaluate(f => { window.__xrEmuDevice.fovy = (f * Math.PI) / 180; window.__xrEmuDevice.ipd = 0 }, AURA.fovyDeg)
  await emu.setInputMode(page, 'hand')
  await emu.setHead(page, { position: [0, EYE, 0], yawDeg: 0, pitchDeg: 0 })
}
/** After the page's own button was pressed: wait for the session, and make the window show the eye's picture alone. */
export async function inAura(page) {
  await page.waitForFunction(() => window.__xrPages.read().inSession, null, { timeout: 30000 })
  // The emulator lifts the session's canvas out of the page and over the whole window. The dashboard's own chrome is
  // hidden for the run, so a picture of the window is the eye's picture and nothing else. Black is what glasses that
  // add light show as clear.
  await page.evaluate(() => {
    const root = document.getElementById('root')
    if (root) root.style.visibility = 'hidden'
    document.documentElement.style.background = '#000'
    document.body.style.background = '#000'
    // The session's canvas has the eye's own pixel count, but it keeps the box it had on the page (a little smaller
    // than the window, and not the same shape). Laid over the whole window, one pixel of it is one pixel of a picture.
    const style = document.createElement('style')
    style.id = 'xr-emu-whole-window'
    // Since the emulator keeps the canvas in its place on the page (xr-emu, onBaseLayerSet), it is inside the hidden
    // root: it is shown by itself.
    style.textContent = 'canvas{position:fixed!important;left:0!important;top:0!important;width:100vw!important;height:100vh!important;margin:0!important;visibility:visible!important}'
    document.head.appendChild(style)
  })
  await emu.frames(page, 6)
}

/** End the session as the glasses do when they are taken off, and give the window back to the dashboard's own page. */
export async function leaveAura(page) {
  await emu.end(page)
  await page.waitForFunction(() => !window.__xrPages.read().inSession, null, { timeout: 30000 })
  await page.evaluate(() => {
    const root = document.getElementById('root')
    if (root) root.style.visibility = ''
    document.getElementById('xr-emu-whole-window')?.remove()
  })
}

/** Turn the head: `yawDeg` to the right of straight ahead, `pitchDeg` up. (The emulator counts a turn to the left as positive.) */
export const look = (page, yawDeg, pitchDeg = 0) => emu.setHead(page, { position: [0, EYE, 0], yawDeg: -yawDeg, pitchDeg })

const norm = d => { const l = Math.hypot(...d); return d.map(x => x / l) }
/** Aim a hand's ray from `origin` at a point of the room. */
export async function aimAt(page, target, hand = 'right', origin = hand === 'right' ? [0.18, 1.3, -0.25] : [-0.18, 1.3, -0.25]) {
  await emu.setRay(page, { hand, origin, direction: norm(target.map((v, i) => v - origin[i])) })
  await emu.frames(page, 2)
  return origin
}
export const pinchDown = async (page, hand = 'right') => { await emu.select(page, { hand, down: true }); await emu.frames(page, 3) }
/** Both hands out of the way: their rays point at nothing. */
export async function parkHands(page) {
  for (const hand of ['left', 'right']) await emu.setRay(page, { hand, origin: [hand === 'left' ? -0.3 : 0.3, 1, 0.2], direction: norm([hand === 'left' ? -1 : 1, -1, 1]) })
  await emu.frames(page, 3)
}
export const pinchUp = async (page, hand = 'right') => { await emu.select(page, { hand, down: false }); await emu.frames(page, 8) }
/**
 * A short pinch by its own clock: the down, two XR frames, and the up, all inside one call in the page, so no round
 * trip of the row lies between them; the page's own clock times it. A pinch longer than `shortMs` (under the room's
 * long-press time, `PINCH.clickMs` 500 ms) is no click: it is said as `long`, and the caller may make it again.
 * Returns { ms, long }.
 */
export const shortPinch = (page, hand = 'right', shortMs = 400) => page.evaluate(async ([h, most]) => {
  const e = window.__xrEmu
  const t0 = performance.now()
  e.select({ hand: h, down: true })
  await e.frames(2)
  e.select({ hand: h, down: false })
  const ms = performance.now() - t0
  await e.frames(8)
  return { ms: Math.round(ms), long: ms >= most }
}, [hand, shortMs])

/** How many frames a second the page draws right now, over `ms`. */
export const frameRate = (page, ms = 3000) => page.evaluate(t => new Promise(done => {
  let n = 0
  const t0 = performance.now()
  const tick = () => { n += 1; if (performance.now() - t0 >= t) done(Math.round((n * 10000) / (performance.now() - t0)) / 10); else requestAnimationFrame(tick) }
  requestAnimationFrame(tick)
}), ms)
