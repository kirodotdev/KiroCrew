// Round 15, the new chat card (CARD-1.md): helpers for its rows in the real /xr page of the card worker's own dev
// server (127.0.0.1:6791, the worktree KiroCrew-card, its API calls handed on to the dev gateway on 6777).
// A desk-sized Chromium with a mouse (the flat preview), or with `emulate` the emulated headset runtime (xr-emu).
// Speech: this host has no microphone. `speech: 'fake'` puts a stand-in for the browser's recognizer in the page
// before it loads (window.SpeechRecognition), and `say()` fires its results; `speech: 'none'` takes the recognizer
// away; `speech: 'real'` leaves Chromium's own, which has no microphone here.
// Sends: `catchSends` answers the card's writes in the browser and counts them, so that no Bot of the shared dev
// gateway is talked to; without it they go to the dev gateway.
import { createRequire } from 'node:module'
import { existsSync } from 'node:fs'
import { emu, installEmulator } from './xr-emu/xr-emu-lib.mjs'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.XR_PLAYWRIGHT ?? 'playwright')
// The dev gateway takes a browser's requests from its own address only (and from port 3000, which another worker's
// dev server has). So the row's browser opens the gateway's address, and this tool answers every request that is
// not one of the gateway's own (its API) from the card worker's dev server: the page's code is the worktree's, and
// the gateway sees a browser on its own address. Nothing of the gateway is changed.
export const BASE = process.env.XR_BASE ?? 'http://localhost:6777'
const DEV = process.env.XR_DEV ?? 'http://127.0.0.1:6791'
const GATEWAYS = /^\/(api|v1\/crew)\/|^\/apps\/[^/]+\/(ui|api)\/|^\/logo\.png$|^\/static\/kirocrew-logo\.png$/
const TYPES = { '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp', '.ico': 'image/x-icon', '.woff2': 'font/woff2', '.woff': 'font/woff', '.wasm': 'application/wasm', '.txt': 'text/plain', '.webmanifest': 'application/manifest+json', '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.glb': 'model/gltf-binary', '.ktx2': 'image/ktx2' }
/**
 * XR_DIST=<a built dist folder>: the tab's code is read from that folder, file by file, and no server or dist link is
 * needed (the shared gateway's dist link is left alone; its API answers as ever). A path with no file is the app's
 * own page (`index.html`), as the gateway serves it.
 */
export async function codeFromDir(context, dir) {
  const { readFileSync, statSync } = await import('node:fs')
  const { extname, join, normalize } = await import('node:path')
  const root = normalize(dir)
  await context.route(url => url.origin === BASE && !GATEWAYS.test(url.pathname), async route => {
    const u = new URL(route.request().url())
    const file = normalize(join(root, decodeURIComponent(u.pathname)))
    let path = join(root, 'index.html')
    try { if (file.startsWith(root) && statSync(file).isFile()) path = file } catch { /* not a file: the app's page */ }
    await route.fulfill({ status: 200, headers: { 'content-type': TYPES[extname(path)] ?? 'application/octet-stream', 'cache-control': 'no-store' }, body: readFileSync(path) })
  })
}

export async function codeFromDev(context) {
  if (process.env.XR_DIST) return codeFromDir(context, process.env.XR_DIST)
  // XR_DEV=gateway: the gateway's own build is the code (a merged tree that the dev gateway serves).
  if (DEV === 'gateway') return
  await context.route(url => url.origin === BASE && !GATEWAYS.test(url.pathname), async route => {
    const u = new URL(route.request().url())
    try {
      const res = await fetch(DEV + u.pathname + u.search, { headers: { accept: route.request().headers().accept ?? '*/*' } })
      const headers = Object.fromEntries([...res.headers].filter(([k]) => !['content-encoding', 'content-length', 'transfer-encoding', 'connection'].includes(k)))
      await route.fulfill({ status: res.status, headers, body: Buffer.from(await res.arrayBuffer()) })
    } catch (e) { await route.abort() }
  })
}
const STATE = process.env.XR_STATE_FILE ?? (process.env.XR_SCRATCH ?? '.') + '/devstate.json'
export const ITER = (process.env.XR_UI ?? '..') + '/iterations/iter-15'
export const sleep = ms => new Promise(done => setTimeout(done, ms))
export async function waitUntil(test, ms = 10000, every = 100) {
  const end = Date.now() + ms
  for (;;) {
    const v = await test()
    if (v) return v
    if (Date.now() > end) return null
    await sleep(every)
  }
}

export async function openRoom({ clock = false, desk = false, speech = 'fake', catchSends = true, emulate = false, video = null, viewport = { width: 1440, height: 900 }, query = '', failSends = 0, cutReply = 0, storage = null, standIn = null } = {}) {
  const browser = await chromium.launch({ args: ['--ignore-gpu-blocklist', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--disable-features=LocalNetworkAccessChecks,BlockInsecurePrivateNetworkRequests'] })
  const context = await browser.newContext({
    viewport, deviceScaleFactor: 1,
    ...(existsSync(STATE) ? { storageState: STATE } : {}),
    ...(video ? { recordVideo: { dir: video, size: viewport } } : {}),
  })
  // `clock`: the page's clock is Playwright's from its first line, so a row can stop it and step it (`context.clock`).
  if (clock) await context.clock.install()
  await codeFromDev(context)
  // XR_KEEP_BUFFER=1: as touch-lib's (every WebGL canvas keeps its drawing buffer, so a row reads the frame just drawn).
  if (process.env.XR_KEEP_BUFFER === '1') await context.addInitScript(() => {
    const get = HTMLCanvasElement.prototype.getContext
    HTMLCanvasElement.prototype.getContext = function (kind, opts) { return get.call(this, kind, /^webgl/.test(kind) ? { ...(opts || {}), preserveDrawingBuffer: true } : opts) }
  })
  // XR_DROP_HOOK=1: a row may drop the pictures of a gateway handle (`window.__dropPanels`, a Set of handles): the
  // binary frames the socket brings for it never reach the page, as if the gateway sent none.
  if (process.env.XR_DROP_HOOK === '1') await context.addInitScript(() => {
    window.__dropPanels = new Set()
    window.__dropped = 0
    const panelOf = data => { try { if (!(data instanceof ArrayBuffer) || data.byteLength < 6) return null; const n = new DataView(data).getUint32(0, false); return JSON.parse(new TextDecoder().decode(new Uint8Array(data, 4, Math.min(n, data.byteLength - 4)))).panel ?? null } catch { return null } }
    // `window.__dropAll` true: every panel's pictures are dropped (a row that needs a page to land none).
    window.__dropAll = false
    const drop = e => { const p = panelOf(e.data); if (p && (window.__dropAll || window.__dropPanels.has(p))) { window.__dropped += 1; return true } return false }
    // `window.__delayRefusal` ms: a refusal as one too many reaches the page that late (a gateway whose opens queue).
    window.__delayRefusal = 0
    const pass = (self, fn, e) => { if (drop(e)) return; if (window.__delayRefusal > 0 && typeof e.data === 'string' && e.data.includes('"too_many"')) { setTimeout(() => fn.call(self, e), window.__delayRefusal); return } fn.call(self, e) }
    const add = WebSocket.prototype.addEventListener
    WebSocket.prototype.addEventListener = function (type, fn, opts) { if (type !== 'message' || typeof fn !== 'function') return add.call(this, type, fn, opts); return add.call(this, type, e => pass(this, fn, e), opts) }
    const desc = Object.getOwnPropertyDescriptor(WebSocket.prototype, 'onmessage')
    Object.defineProperty(WebSocket.prototype, 'onmessage', { configurable: true, get() { return desc.get.call(this) }, set(fn) { desc.set.call(this, typeof fn === 'function' ? e => pass(this, fn, e) : fn) } })
  })
  // `storage`: keys of the browser's own store, set before the page loads (the room's record, the card's language).
  if (storage) await context.addInitScript(kv => { if (sessionStorage.getItem('__seeded')) return; sessionStorage.setItem('__seeded', '1'); for (const [k, v] of Object.entries(kv)) { if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v) } }, storage)
  if (speech === 'fake') await context.addInitScript(() => {
    // A stand-in for the browser's recognizer: it starts at once, and a row fires its results.
    class Stand {
      start() {
        window.__rec = this; window.__recStarts = (window.__recStarts ?? 0) + 1; (window.__recLangs ??= []).push(this.lang)
        // `window.__recRefuse`: the languages that this recognizer does not know, as Chrome says it.
        if ((window.__recRefuse ?? []).includes(this.lang)) { setTimeout(() => { this.onerror?.({ error: 'language-not-supported' }); this.onend?.() }, 0); return }
        window.__recOn = true; setTimeout(() => this.onstart?.(), 0)
      }
      stop() { window.__recStops = (window.__recStops ?? 0) + 1; if (window.__rec === this) window.__recOn = false; setTimeout(() => this.onend?.(), 0) }
      abort() { this.stop() }
    }
    window.SpeechRecognition = Stand
    window.__say = (final, interim) => {
      const results = []
      if (final) results.push(Object.assign([{ transcript: final }], { isFinal: true }))
      if (interim) results.push(Object.assign([{ transcript: interim }], { isFinal: false }))
      window.__rec?.onresult?.({ resultIndex: 0, results })
    }
  })
  if (speech === 'none') await context.addInitScript(() => { delete window.SpeechRecognition; delete window.webkitSpeechRecognition; try { Object.defineProperty(window, 'webkitSpeechRecognition', { value: undefined }) } catch { /* gone */ } })
  // The card's writes, as they go out: the new chat's slot, and the message.
  const sends = []
  const slots = []
  let fail = failSends
  let cut = cutReply
  // What the page sends to a panel's page (a press, a key): a row counts them to see that a press reached no page.
  const inputs = []
  context.on('request', r => { if (r.url().includes('/api/browser/panel/input')) inputs.push({ at: Date.now(), event: r.postDataJSON()?.event?.type ?? null, action: r.postDataJSON()?.event?.action ?? null, panel: r.postDataJSON()?.panel ?? null }) })
  if (catchSends) {
    await context.route('**/api/chat/slots', async route => {
      if (route.request().method() !== 'POST') return route.continue()
      slots.push({ at: Date.now(), body: route.request().postDataJSON() })
      // `catchSends: 'bots'`: a new chat is the dev gateway's own (it talks to no Bot); only a send to a Bot's thread is answered here.
      if (catchSends === 'bots') return route.continue()
      await route.fulfill({ json: { key: `cardtest${slots.length}`, title: 'New chat' } })
    })
    await context.route('**/api/chat?ws=1', async route => {
      if (route.request().method() !== 'POST') return route.continue()
      const body = route.request().postDataJSON()
      sends.push({ at: Date.now(), slot: body?.slot, message: body?.message, key: route.request().headers()['idempotency-key'] ?? null })
      // `cutReply`: the request reaches the gateway, and its reply is lost on the way back (so many times).
      if (cut > 0) { cut -= 1; const res = await route.fetch().catch(() => null); sends[sends.length - 1].landed = res?.status() ?? null; await route.abort('connectionreset'); return }
      if (catchSends === 'bots' && !String(body?.slot ?? '').startsWith('member-')) return route.continue()
      if (fail > 0) { fail -= 1; await route.fulfill({ status: 503, json: { ok: false } }); return }
      await route.fulfill({ json: { ok: true } })
    })
  } else {
    context.on('request', r => {
      if (r.method() !== 'POST') return
      const u = new URL(r.url())
      if (u.pathname === '/api/chat/slots') slots.push({ at: Date.now(), body: r.postDataJSON() })
      if (u.pathname === '/api/chat') { const body = r.postDataJSON(); sends.push({ at: Date.now(), slot: body?.slot, message: body?.message, key: r.headers()['idempotency-key'] ?? null }) }
    })
  }
  // `standIn(context)`: a row's own answers for some of the gateway's routes, set before the page loads.
  if (standIn) await standIn(context)
  if (emulate) await installEmulator(context)
  await (await import('./pages-show.mjs')).showPages(context)
  // XR_PIN_BUNDLE=1 (off by default): as in touch-lib.mjs.
  if (process.env.XR_PIN_BUNDLE === '1' && standIn !== (await import('./taps-lib.mjs')).pinBundle) await (await import('./taps-lib.mjs')).pinBundle(context).catch(() => undefined)
  const page = await context.newPage()
  // A recording starts with the page.
  const openedAt = Date.now()
  const errors = []
  page.on('pageerror', e => errors.push('pageerror: ' + String(e.message).slice(0, 300)))
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text().slice(0, 300)) })
  await page.goto(`${BASE}/xr?view=pages&page=/chat&probe=1${desk ? '&desk=1' : ''}${query}`, { waitUntil: 'domcontentloaded', timeout: 120000 })
  await page.waitForSelector('[data-testid="xr-canvas"]', { timeout: 120000 })
  const skip = page.getByText('Skip all').first()
  if (await skip.waitFor({ state: 'visible', timeout: 5000 }).then(() => true, () => false)) await skip.click({ timeout: 2500 }).catch(() => {})
  await page.waitForFunction(() => !!window.__xrPages && window.__xrPages.read().view.width > 1, null, { timeout: 60000 })
  await waitUntil(async () => { const c = (await read(page)).crew; return c.length > 0 && c.every(b => b.drawn) }, 60000, 300)
  // The shared dev gateway's Bots raise notices while other workers' rows run; the dashboard shows each as a toast
  // over the bar's right end, where the room's buttons are. They are not drawn in a row's browser.
  await page.addStyleTag({ content: '.notif-material{display:none!important}' })
  // No Bot strolls: a throw's target stands still.
  await page.evaluate(() => window.__xrPages.stroll(false))
  return { browser, context, page, errors, sends, slots, inputs, openedAt }
}

// The hook is set anew when the room renders: a read that falls into that moment waits for it.
export const read = async page => { for (let i = 0; i < 60; i += 1) { const r = await page.evaluate(() => (window.__xrPages ? window.__xrPages.read() : null)); if (r) return r; await sleep(50) } throw new Error('the room\'s hook is not there') }
export const cardOf = async page => (await read(page)).card
export const boxOf = (page, testid) => page.evaluate(t => { const el = document.querySelector(`[data-testid="${t}"]`); if (!el) return null; const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height } }, testid)

/** Where a point of the room is on the screen, in CSS pixels of the screen (the flat preview). */
export async function roomOnScreen(page, point) {
  const r = await read(page)
  const canvas = await boxOf(page, 'xr-canvas')
  const d = point.map((p, i) => p - r.head[i])
  const yaw = (r.turn.yawDeg * Math.PI) / 180
  const pitch = (r.turn.pitchDeg * Math.PI) / 180
  const vx = d[0] * Math.cos(yaw) + d[2] * Math.sin(yaw)
  const vz = -d[0] * Math.sin(yaw) + d[2] * Math.cos(yaw)
  const cy = d[1] * Math.cos(pitch) + vz * Math.sin(pitch)
  const cz = -d[1] * Math.sin(pitch) + vz * Math.cos(pitch)
  const t = Math.tan((r.view.fovDeg * Math.PI) / 360)
  return { x: canvas.x + ((vx / -cz / (t * (r.view.width / r.view.height)) + 1) * r.view.width) / 2, y: canvas.y + ((1 - (r.view.shift ?? 0) - cy / -cz / t) * r.view.height) / 2, ahead: cz < 0 }
}

/** The stand-in recognizer hears words. */
export const say = (page, final, interim = '') => page.evaluate(([f, i]) => window.__say(f, i), [final, interim])

/** The new chat button of the bar brings the card out; waits until it stands at its place. */
export async function cardOut(page) {
  await page.click('[data-testid="xr-new-chat"]')
  await waitUntil(async () => { const c = await cardOf(page); return c.card && c.at && c.home && Math.hypot(...c.at.map((v, i) => v - c.home[i])) < 0.005 }, 5000, 50)
  return cardOf(page)
}

/** The middle of a Bot's body in the room. */
export const bodyOf = b => [(b.feet[0] + b.top[0]) / 2, (b.feet[1] + b.top[1]) / 2, (b.feet[2] + b.top[2]) / 2]

/**
 * A throw with the mouse: down on the card, moved to a point of the screen in steps, let go there.
 * `hold`: called while the card is held at the target, before the let-go.
 */
export async function throwTo(page, to, { steps = 14, ms = 500, hold = null } = {}) {
  const c = await cardOf(page)
  const from = await roomOnScreen(page, c.at)
  await page.mouse.move(from.x, from.y)
  await sleep(120)
  await page.mouse.down()
  await sleep(80)
  for (let i = 1; i <= steps; i += 1) { await page.mouse.move(from.x + ((to.x - from.x) * i) / steps, from.y + ((to.y - from.y) * i) / steps); await sleep(ms / steps) }
  await sleep(150)
  if (hold) await hold()
  await page.mouse.up()
}

/** A press with the mouse on one key of the room's keyboard, by what the key types. */
export async function pressKey(page, key) {
  const c = await cardOf(page)
  const at = c.keys?.centers?.[key]
  if (!at) throw new Error(`no key ${JSON.stringify(key)} on layer ${c.keys?.layer}`)
  const p = await roomOnScreen(page, at)
  await page.mouse.move(p.x, p.y)
  await sleep(60)
  await page.mouse.down()
  await sleep(50)
  await page.mouse.up()
  await sleep(60)
}

/* ---------------- an emulated session ---------------- */

/** Both hands at rest, low and to the sides: under the line over which a hand points. */
export async function parkHands(page) {
  for (const hand of ['left', 'right']) await emu.setRay(page, { hand, origin: [hand === 'left' ? -0.3 : 0.3, 1, 0.2], direction: [hand === 'left' ? -0.577 : 0.577, -0.577, 0.577] })
  await emu.frames(page, 3)
}
/** Turn the head: `yawDeg` to the right of straight ahead, `pitchDeg` up. */
export const lookTo = (page, yawDeg, pitchDeg = 0) => emu.setHead(page, { position: [0, 1.6, 0], yawDeg: -yawDeg, pitchDeg })
/** Turn the head so that the dot lies on a point of the room. */
export async function dotAt(page, at) {
  const d = at.map((v, i) => v - [0, 1.6, 0][i])
  await lookTo(page, (Math.atan2(d[0], -d[2]) * 180) / Math.PI, (Math.atan2(d[1], Math.hypot(d[0], d[2])) * 180) / Math.PI)
  await emu.frames(page, 4)
}
/** The page's own button starts the session; the window then shows the eye's picture alone (the Aura's lens, hands tracked). */
export async function enterSession(page) {
  await page.evaluate(f => { window.__xrEmuDevice.fovy = (f * Math.PI) / 180; window.__xrEmuDevice.ipd = 0 }, 40.7)
  await emu.setInputMode(page, 'hand')
  await lookTo(page, 0, 0)
  await page.click('[data-testid="xr-enter"]')
  await page.waitForFunction(() => window.__xrPages.read().inSession, null, { timeout: 30000 })
  await page.evaluate(() => {
    const root = document.getElementById('root')
    if (root) root.style.visibility = 'hidden'
    document.documentElement.style.background = '#000'
    document.body.style.background = '#000'
    const style = document.createElement('style')
    style.id = 'xr-emu-whole-window'
    style.textContent = 'canvas{position:fixed!important;left:0!important;top:0!important;width:100vw!important;height:100vh!important;margin:0!important;visibility:visible!important}'
    document.head.appendChild(style)
  })
  await parkHands(page)
  await emu.frames(page, 8)
}

/** In a session: the window shows the eye's picture alone, one pixel of the canvas for one of the picture. */
export async function wholeWindow(page) {
  await page.evaluate(() => {
    const root = document.getElementById('root')
    if (root) root.style.visibility = 'hidden'
    document.documentElement.style.background = '#000'
    document.body.style.background = '#000'
    if (document.getElementById('xr-emu-whole-window')) return
    const style = document.createElement('style')
    style.id = 'xr-emu-whole-window'
    style.textContent = 'canvas{position:fixed!important;left:0!important;top:0!important;width:100vw!important;height:100vh!important;margin:0!important;visibility:visible!important}'
    document.head.appendChild(style)
  })
}
