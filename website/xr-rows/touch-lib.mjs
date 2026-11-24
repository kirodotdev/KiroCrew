// Iteration 10, step 8b: a phone-sized Chromium with a touch screen, on the real /xr?view=pages of the dev gateway.
// The fingers are real touch input: the browser itself makes them (Input.dispatchTouchEvent), so the browser's own
// scrolling, zooming and bouncing are in play as on a phone. WebKit, which an iPhone's web view is, does not start
// on this host (its libraries are missing), so nothing here ran in WebKit.
// Every tool that uses this writes under the round's folder only (iterations/iter-11/ now).
import { createRequire } from 'node:module'
import { existsSync } from 'node:fs'
import { BASE } from './v3-lib.mjs'
import { installEmulator } from './xr-emu/xr-emu-lib.mjs'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.XR_PLAYWRIGHT ?? 'playwright')
const STATE = process.env.XR_STATE_FILE ?? (process.env.XR_SCRATCH ?? '.') + '/devstate.json'
const IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1'
// `desk` is no phone: a desktop window with a mouse and no touch screen, for checking that the mouse still works.
export const SHAPES = { upright: { width: 390, height: 844 }, sideways: { width: 844, height: 390 }, desk: { width: 1440, height: 900 }, film: { width: 960, height: 600 }, wide: { width: 2400, height: 800 }, big: { width: 2000, height: 1400 } }
/** The shapes that are a desk's: a mouse, and one pixel for each CSS pixel. */
// `big`: a desk's window so large that the first page is drawn at over 1.4 pixels for a CSS pixel, and its panel asks for detail 2.
// `film`: a smaller desk window, for a film of an emulated session that the software renderer must draw fast enough.
const DESKS = ['desk', 'wide', 'big', 'film']
export const sleep = ms => new Promise(done => setTimeout(done, ms))

/** Open the preview on a phone of this shape. `video` names a folder to record into. `dot` draws where a finger is. */
/**
 * `start: true` leaves the preview at its start picture (the 50 degree lens, the head tipped down at the first page
 * and the crew). Without it the head is level, the frame is not shifted and the eye is stepped back to the whole first page, after every
 * load and after every press on the home button: the rows that were written before the start picture changed are about a press, a scroll and typing, and not
 * about where the picture starts. `fixture: true` opens with the land's sample crew, which has a Bot of every state.
 */
export async function phone(shape, { video = null, page: path = '/chat', dot = false, scale = 2, start = false, fixture = false, still = false, extraBot = null, bots = 0, query = '', bin = null, emulate = false } = {}) {
  const viewport = SHAPES[shape]
  // XR_BIN and XR_QUERY_ADD run any row in another Chrome, with words added to the address (`&source=dom`).
  bin = bin ?? process.env.XR_BIN ?? null
  query = `${query}${process.env.XR_QUERY_ADD ?? ''}`
  // `bin`: another Chrome's binary (Chrome for Testing 158 for the page drawn by the XR tab itself), headless in its new mode.
  // A page whose document a route fulfilled (XR_DIST) counts to Chrome as not from the gateway's own address, and its
  // socket to the gateway is then refused as a local-network request: as in chatcard-lib, those checks are off then.
  const lna = process.env.XR_DIST ? ['--disable-features=LocalNetworkAccessChecks,BlockInsecurePrivateNetworkRequests'] : []
  const browser = await chromium.launch({ ...(bin ? { executablePath: bin, headless: false } : {}), args: [...(bin ? ['--headless=new', '--noerrdialogs', '--no-first-run'] : []), '--ignore-gpu-blocklist', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', ...lna] })
  const context = await browser.newContext({
    viewport, ...(DESKS.includes(shape) ? { deviceScaleFactor: 1 } : { deviceScaleFactor: scale, hasTouch: true, isMobile: true, userAgent: IPHONE }),
    ...(existsSync(STATE) ? { storageState: STATE } : {}),
    // `still`: the person asked the system for less motion.
    ...(still ? { reducedMotion: 'reduce' } : {}),
    ...(video ? { recordVideo: { dir: video, size: { width: viewport.width * 2, height: viewport.height * 2 } } } : {}),
  })
  // XR_DIST=<a built dist folder>: the tab's code is read from it (chatcard-lib's `codeFromDir`); the shared
  // gateway's dist link is left alone.
  if (process.env.XR_DIST) await (await import('./chatcard-lib.mjs')).codeFromDir(context, process.env.XR_DIST)
  // XR_KEEP_BUFFER=1: every WebGL canvas keeps its drawing buffer after each frame, so that a row can read the
  // pixels of the frame just drawn (`first-frame.mjs`). Nothing else changes in what is drawn.
  if (process.env.XR_KEEP_BUFFER === '1') await context.addInitScript(() => {
    const get = HTMLCanvasElement.prototype.getContext
    HTMLCanvasElement.prototype.getContext = function (kind, opts) { return get.call(this, kind, /^webgl/.test(kind) ? { ...(opts || {}), preserveDrawingBuffer: true } : opts) }
  })
  // XR_WALLS=1: the drawn room is switched on from the start, by the record that keeps it, so that a check's rows run with it.
  if (process.env.XR_WALLS) await context.addInitScript(() => {
    try {
      const key = 'kirocrew:xr-room'
      const record = JSON.parse(localStorage.getItem(key) || '{"v":1,"pages":[]}')
      if (record && record.v === 1 && record.walls !== true) { record.walls = true; localStorage.setItem(key, JSON.stringify(record)) }
    } catch { /* no store: the room stays off */ }
  })
  // XR_NO_CREW=1: the room has no crew, for a check that times the page's pictures on a host that draws by software.
  if (process.env.XR_NO_CREW) await context.route('**/v1/crew/owner/bots', async route => { const res = await route.fetch(); const j = await res.json().catch(() => null); await route.fulfill({ response: res, json: { ...(j ?? {}), members: [] } }) })
  // `extraBot`: the page's own roster answer gets one more Bot of this name, which the gateway does not have: a way to see what a panel does when the gateway refuses a Bot's page.
  if (extraBot) await context.route('**/v1/crew/owner/bots', async route => { const res = await route.fetch(); const j = await res.json().catch(() => null); const rows = j?.members ?? []; await route.fulfill({ response: res, json: { ...(j ?? {}), members: rows.length ? [...rows, { ...rows[0], name: extraBot, display_name: extraBot, slug: extraBot.toLowerCase(), slot_key: `member-${extraBot.toLowerCase()}`, running: false }] : rows } }) })
  // `bots`: the page's own roster answer is filled up to so many Bots with copies of the gateway's own under new names: a picture and measuring aid for a larger crew. The copies have no page.
  if (bots) await context.route('**/v1/crew/owner/bots', async route => { const res = await route.fetch(); const j = await res.json().catch(() => null); const rows = j?.members ?? []; const members = rows.slice(0, bots); for (let i = 0; rows.length && members.length < bots; i += 1) { const name = `Bot ${members.length + 1}`; members.push({ ...rows[i % rows.length], name, display_name: name, slug: `bot-${members.length + 1}`, slot_key: `member-bot-${members.length + 1}`, running: false }) } await route.fulfill({ response: res, json: { ...(j ?? {}), members } }) })
  // XR_SLOT_EXTRA=<file>: fields put on session rows as the gateway's socket brings them, by session key (the file
  // is read again at each frame, so a row can change it as it goes). For fields the dev stand-in cannot make a real
  // session carry: a PR it linked with its state and CI (`source_links`), and its todo list (`todo`). P3's done sheet.
  if (process.env.XR_SLOT_EXTRA) {
    const { readFileSync, appendFileSync } = await import('node:fs')
    const extraOf = () => { try { return JSON.parse(readFileSync(process.env.XR_SLOT_EXTRA, 'utf8')) } catch { return {} } }
    const patch = (text) => {
      if (typeof text !== 'string' || !text.startsWith('{')) return text
      let f
      try { f = JSON.parse(text) } catch { return text }
      // XR_WS_LOG=<file>: each frame's type and slot, for a row that asks what reached the page.
      if (process.env.XR_WS_LOG && f.type !== 'slots') { try { appendFileSync(process.env.XR_WS_LOG, `${Date.now()} ${f.type} ${f.data?.slot ?? f.data?.key ?? ''} ${f.data?.role ?? ''}\n`) } catch { /* no log */ } }
      const extra = extraOf()
      const put = row => (row && extra[row.key] ? Object.assign(row, extra[row.key]) : row)
      if (f.type === 'slots' && Array.isArray(f.data)) f.data.forEach(put)
      else if (f.type === 'slots_diff') (f.data?.rows ?? []).forEach(r => put(r.row))
      else if (f.type === 'slot_patch') (f.data?.slots ?? []).forEach(put)
      else return text
      return JSON.stringify(f)
    }
    await context.routeWebSocket(/\/api\/ws/, ws => {
      const server = ws.connectToServer()
      ws.onMessage(m => server.send(m))
      server.onMessage(m => ws.send(patch(m)))
    })
  }
  // `emulate`: the emulated headset runtime (xr-emu), for rows in a session.
  if (emulate) await installEmulator(context)
  await (await import('./pages-show.mjs')).showPages(context)
  // XR_PIN_BUNDLE=1 (off by default): the tab is given one constant bundle id, so a dist swapped under the shared
  // gateway does not make the tab load itself again in the middle of a row (taps-lib.mjs, TAPS-2.md).
  if (process.env.XR_PIN_BUNDLE === '1') await (await import('./taps-lib.mjs')).pinBundle(context)
  const page = await context.newPage()
  // A recording starts with the page.
  const openedAt = Date.now()
  const errors = []
  page.on('pageerror', e => errors.push('pageerror: ' + String(e.message).slice(0, 200)))
  // Everything the page sends to the hub for a panel, as it goes out.
  const sent = []
  page.on('request', r => {
    if (!r.url().includes('/api/browser/panel/input')) return
    const body = r.postDataJSON()
    const e = body?.event
    if (e) sent.push({ ...e, at: Date.now(), panel: body.panel })
  })
  // What the hub answered to each of them, as the page's own fetch got it: `answered()` reads the list.
  await context.addInitScript(() => {
    const plain = window.fetch.bind(window)
    window.__inputAnswers = []
    // When a finger or the mouse button last came up, by the page's clock of the day.
    window.__ups = []
    addEventListener('pointerup', () => window.__ups.push(Date.now()), true)
    window.fetch = async (what, init) => {
      const out = Date.now()
      const r = await plain(what, init)
      const url = typeof what === 'string' ? what : String(what?.url ?? what)
      if (url.includes('/api/browser/panel/input')) {
        let body = null
        try { body = JSON.parse(String(init?.body ?? 'null')) } catch { /* not JSON */ }
        r.clone().json().then(j => window.__inputAnswers.push({ event: body?.event, panel: body?.panel, answer: j, out, at: Date.now() }), () => {})
      }
      return r
    }
  })
  const answered = () => page.evaluate(() => window.__inputAnswers ?? [])
  const outside = []
  page.on('request', r => { const u = new URL(r.url()); if (!['localhost', '127.0.0.1'].includes(u.hostname)) outside.push(r.url().slice(0, 120)) })
  if (!start) await context.addInitScript(() => {
    const whole = () => window.__xrPages?.look?.({ yawDeg: 0, pitchDeg: 0, lens: 'page', shift: 0 })
    const first = setInterval(() => { const hook = window.__xrPages; if (hook?.look && hook.read().view.width > 1) { clearInterval(first); whole() } }, 30)
    // The home button goes to the start picture. For the older rows it goes on to their view.
    addEventListener('click', e => { if (e.target?.closest?.('[data-testid="xr-start-view"]')) setTimeout(whole, 0) }, true)
  })
  await page.goto(`${BASE}/xr?view=pages&page=${encodeURIComponent(path)}&probe=1${fixture ? '&fixture=1' : ''}${query}`, { waitUntil: 'domcontentloaded', timeout: 120000 })
  await page.waitForSelector('[data-testid="xr-canvas"]', { timeout: 120000 })
  const skip = page.getByText('Skip all').first()
  if (await skip.waitFor({ state: 'visible', timeout: 6000 }).then(() => true, () => false)) await skip.click({ timeout: 2500 }).catch(() => {})
  if (dot) await page.evaluate(() => {
    // One dot for each finger that is down, drawn by this tool and by nothing in the product.
    const dots = new Map()
    const show = e => {
      if (e.pointerType !== 'touch' || !e.isTrusted) return
      let d = dots.get(e.pointerId)
      if (!d) {
        d = document.createElement('div')
        d.style.cssText = 'position:fixed;left:0;top:0;width:34px;height:34px;margin:-17px 0 0 -17px;border-radius:50%;background:rgba(255,255,255,.35);border:2px solid #fff;box-shadow:0 0 0 2px rgba(0,0,0,.5);z-index:2147483647;pointer-events:none'
        document.body.appendChild(d)
        dots.set(e.pointerId, d)
      }
      d.style.transform = `translate(${e.clientX}px,${e.clientY}px)`
    }
    const hide = e => { if (!e.isTrusted) return; dots.get(e.pointerId)?.remove(); dots.delete(e.pointerId) }
    addEventListener('pointerdown', show, true)
    addEventListener('pointermove', show, true)
    addEventListener('pointerup', hide, true)
    addEventListener('pointercancel', hide, true)
  })
  if (!start) await page.waitForFunction(() => !!window.__xrPages?.look && window.__xrPages.read().view.width > 1 && window.__xrPages.read().view.shift === 0, null, { timeout: 60000 }).catch(() => {})
  const cdp = DESKS.includes(shape) ? null : await context.newCDPSession(page)
  return { browser, context, page, cdp, errors, sent, answered, outside, viewport, openedAt }
}

export const read = page => page.evaluate(() => window.__xrPages.read())
export const seen = async (page, id = 'page') => (await read(page)).sources[id]?.seen ?? null
export async function until(page, test, ms = 30000, id = 'page') {
  const end = Date.now() + ms
  while (Date.now() < end) {
    const s = await seen(page, id)
    if (s && test(s)) return s
    await sleep(120)
  }
  return null
}
export const boxOf = (page, testid) => page.evaluate(t => { const el = document.querySelector(`[data-testid="${t}"]`); if (!el) return null; const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height } }, testid)

/** Where a point of a panel's page is on the phone's screen, in CSS pixels of the screen. `x`, `y` are CSS pixels of the page. */
export async function onScreen(page, x, y, id = 'page') {
  const r = await read(page)
  const panel = r.panels.find(p => p.id === id)
  const canvas = await boxOf(page, 'xr-canvas')
  const [w, h] = panel.size
  const [cw, ch] = panel.css
  const a = (x / cw - 0.5) * w
  const b = (0.5 - y / ch) * h
  const d = panel.position.map((p, i) => p + panel.right[i] * a + panel.up[i] * b - r.head[i])
  const yaw = (r.turn.yawDeg * Math.PI) / 180
  const pitch = (r.turn.pitchDeg * Math.PI) / 180
  const vx = d[0] * Math.cos(yaw) + d[2] * Math.sin(yaw)
  const vz = -d[0] * Math.sin(yaw) + d[2] * Math.cos(yaw)
  const cy = d[1] * Math.cos(pitch) + vz * Math.sin(pitch)
  const cz = -d[1] * Math.sin(pitch) + vz * Math.cos(pitch)
  const t = Math.tan((r.view.fovDeg * Math.PI) / 360)
  return { x: canvas.x + ((vx / -cz / (t * (r.view.width / r.view.height)) + 1) * r.view.width) / 2, y: canvas.y + ((1 - (r.view.shift ?? 0) - cy / -cz / t) * r.view.height) / 2, ahead: cz < 0 }
}

/** Where a point of the room is on the screen, in CSS pixels of the screen. */
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

/* ---------------- fingers ---------------- */

const touch = (cdp, type, points) => cdp.send('Input.dispatchTouchEvent', { type, touchPoints: points.map(([x, y], i) => ({ x: Math.round(x), y: Math.round(y), id: i, radiusX: 8, radiusY: 8, force: 1 })) })
/** A short tap with one finger. */
export async function tap(cdp, x, y, holdMs = 70) {
  await touch(cdp, 'touchStart', [[x, y]])
  await sleep(holdMs)
  await touch(cdp, 'touchEnd', [])
}
/** One finger down at `from`, dragged to `to` in steps, then lifted (or held, with `lift: false`). */
export async function drag(cdp, from, to, { steps = 16, ms = 600, lift = true, settle = 120 } = {}) {
  await touch(cdp, 'touchStart', [from])
  await sleep(60)
  for (let i = 1; i <= steps; i += 1) {
    await touch(cdp, 'touchMove', [[from[0] + ((to[0] - from[0]) * i) / steps, from[1] + ((to[1] - from[1]) * i) / steps]])
    await sleep(ms / steps)
  }
  await sleep(settle)
  if (lift) await touch(cdp, 'touchEnd', [])
}
/**
 * Two fingers, the first down at `a` and then the second at `b`, both dragged by `by`, then lifted one after the
 * other (the second first). `rest` is called between the two lifts, with one finger still down.
 */
export async function drag2(cdp, a, b, by, { steps = 16, ms = 600, between = 150, rest = null } = {}) {
  await touch(cdp, 'touchStart', [a])
  await sleep(between)
  await touch(cdp, 'touchStart', [a, b])
  await sleep(60)
  let at = [a, b]
  for (let i = 1; i <= steps; i += 1) {
    at = [a, b].map(([x, y]) => [x + (by[0] * i) / steps, y + (by[1] * i) / steps])
    await touch(cdp, 'touchMove', at)
    await sleep(ms / steps)
  }
  await sleep(120)
  await touch(cdp, 'touchEnd', [at[0]])
  if (rest) await rest(at[0])
  await touch(cdp, 'touchEnd', [])
}
/**
 * Two fingers about the point `at`, `from` pixels each side of it, moved until they are `to` pixels each side: apart
 * when `to` is more. With `by` the middle moves that far as they go. They are lifted one after the other.
 */
export async function pinch(cdp, at, from, to, { by = [0, 0], steps = 14, ms = 600, lean = 0.35, hold = 350 } = {}) {
  // The fingers lie on a slanted line, as a thumb and a finger do.
  const dx = Math.cos(lean), dy = -Math.sin(lean)
  const pts = (r, k) => [[at[0] + by[0] * k - dx * r, at[1] + by[1] * k - dy * r], [at[0] + by[0] * k + dx * r, at[1] + by[1] * k + dy * r]]
  const first = pts(from, 0)
  await touch(cdp, 'touchStart', [first[0]])
  await sleep(90)
  await touch(cdp, 'touchStart', first)
  await sleep(60)
  let now = first
  for (let i = 1; i <= steps; i += 1) {
    now = pts(from + ((to - from) * i) / steps, i / steps)
    await touch(cdp, 'touchMove', now)
    await sleep(ms / steps)
  }
  await sleep(hold)
  await touch(cdp, 'touchEnd', [now[0]])
  await sleep(40)
  await touch(cdp, 'touchEnd', [])
}
/** How many CSS pixels of the screen one CSS pixel of the first page covers now (1 at the nearest point). */
export async function pageScale(page) {
  const r = await read(page)
  const mPerPx = (0.868 / 1000) * 1.75
  return { scale: (r.view.height / 2 / Math.tan((r.view.fovDeg * Math.PI) / 360)) * (mPerPx / 1.75), near: r.near, fovDeg: r.view.fovDeg }
}
/** Two fingers that move apart or together about a point: what a pinch to zoom is on a phone. */
export async function spread(cdp, at, from = 40, to = 140, steps = 10) {
  const pts = r => [[at[0] - r, at[1]], [at[0] + r, at[1]]]
  await touch(cdp, 'touchStart', pts(from))
  for (let i = 1; i <= steps; i += 1) { await touch(cdp, 'touchMove', pts(from + ((to - from) * i) / steps)); await sleep(30) }
  await touch(cdp, 'touchEnd', [])
}

/** What the dashboard round the canvas did: whether anything scrolled, zoomed or moved. */
export const pageState = page => page.evaluate(() => {
  const moved = []
  for (const el of document.querySelectorAll('*')) if (el.scrollTop || el.scrollLeft) moved.push(`${el.tagName.toLowerCase()}.${String(el.className).slice(0, 40)} ${el.scrollLeft},${el.scrollTop}`)
  return { scrollX, scrollY, scale: visualViewport.scale, offsetTop: visualViewport.offsetTop, offsetLeft: visualViewport.offsetLeft, moved, inner: [innerWidth, innerHeight] }
})

/* ---------------- what the gateway says is on the page ---------------- */

export async function ask(page, question, x = 0, y = 0, id = 'page') {
  const s = await seen(page, id)
  return page.evaluate(async ([handle, q, px, py]) => {
    const r = await fetch('/api/browser/panel/ask', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ panel: handle, question: q, x: px, y: py }) })
    return r.ok ? (await r.json()).answer : { error: r.status }
  }, [s.handle, question, x, y])
}
/** Every thing a press could take in a part of the page, once each, with its label, rectangle and whether it is guarded. */
export async function things(page, { x0 = 0, y0 = 0, x1 = 1024, y1 = 720, step = 24 } = {}, id = 'page') {
  const s = await seen(page, id)
  return page.evaluate(async ([handle, ax, ay, bx, by, st]) => {
    const points = []
    for (let y = by - 6; y >= ay; y -= st) for (let x = ax + 6; x <= bx; x += st) points.push([x, y])
    const found = new Map()
    const one = async ([x, y]) => {
      const r = await fetch('/api/browser/panel/ask', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ panel: handle, question: 'target', x, y }) })
      const answer = r.ok ? (await r.json()).answer : null
      if (answer) found.set(answer.rect.join(','), answer)
    }
    for (let i = 0; i < points.length; i += 24) await Promise.all(points.slice(i, i + 24).map(one))
    return [...found.values()]
  }, [s.handle, x0, y0, x1, y1, step])
}
export async function find(page, label, region = {}, id = 'page') {
  const hit = (await things(page, region, id)).find(t => label.test(t.label))
  if (!hit) return null
  const [left, top, w, h] = hit.rect
  return { x: Math.round(left + w / 2), y: Math.round(top + h / 2), ...hit }
}
export async function waitFor(page, label, region = {}, { ms = 30000, gone = false } = {}) {
  const end = Date.now() + ms
  for (;;) {
    const hit = await find(page, label, region)
    if (gone ? !hit : hit) return gone ? true : hit
    if (Date.now() > end) return null
    await sleep(700)
  }
}
/** What the page's field holds after these typing messages, when it was empty before: text is added, Backspace takes one character. */
export function played(events) {
  let text = []
  for (const e of events) {
    if (e.type === 'text') text.push(...e.text)
    else if (e.type === 'key' && e.key === 'Backspace') text.pop()
    else if (e.type === 'key' && e.key === 'Enter') text = [...text, '\n']
  }
  return text.join('')
}
/** The page's own button for a new chat, as the gateway names it, or null. A message typed into a new chat names it. */
export const newChat = (page, id = 'page') => find(page, /^\+?\s*new\b/i, { y1: 130, x1: 640, step: 10 }, id)
/** Wait until `test()` holds. */
export async function waitUntil(test, ms = 8000, step = 120) {
  const end = Date.now() + ms
  for (;;) {
    const got = await test()
    if (got) return got
    if (Date.now() > end) return null
    await sleep(step)
  }
}
/** Type into whatever has the focus of the page: the script stands in for the keyboard, which is parked. */
export const type = (page, text, id = 'page') => page.evaluate(([i, t]) => window.__xrPages.text(i, t), [id, text])
export const key = (page, k, id = 'page') => page.evaluate(([i, t]) => window.__xrPages.key(i, t), [id, k])

/* ---------------- what the picture itself shows (part 0: a picture has its page's shape) ---------------- */

/** Whether a picture of `w` by `h` has the shape of a page of `cssW` by `cssH`, to within one pixel (the gateway's own rule). */
export const hasShape = (w, h, cssW, cssH) => w > 0 && h > 0 && cssW > 0 && cssH > 0 && Math.abs(w * cssH - h * cssW) <= Math.max(cssW, cssH)

/**
 * For every panel that has had a picture: the last picture's size beside its page's CSS size, and how many of ALL
 * the pictures that arrived since the panel was made had another shape (`offShape`, counted by the page as each
 * arrives: a moving picture can be wrong while the still that follows is right, so the last one alone says too
 * little). `dropped` is the gateway's own count of frames it did not send. `state` is what the probe's `read()` gave.
 */
export function shapesOf(state) {
  const rows = []
  for (const p of state.panels) {
    const s = state.sources[p.id]?.seen
    if (!s || !s.picture || !(s.picture[0] > 0)) { rows.push({ id: p.id, none: true }); continue }
    rows.push({ id: p.id, picture: s.picture, css: s.css, pictures: s.pictures, off: s.offShape ?? null, ok: hasShape(s.picture[0], s.picture[1], s.css[0], s.css[1]) && s.offShape === 0, dropped: s.misshapen ?? null })
  }
  const bad = rows.filter(r => !r.none && !r.ok)
  return { ok: bad.length === 0, rows, detail: rows.length ? rows.map(r => (r.none ? `${r.id}: no picture yet` : `${r.id}: ${r.picture.join('x')} for ${r.css.join('x')}, ${r.off} of ${r.pictures} pictures of another shape${r.ok ? '' : ' WRONG SHAPE'}${r.dropped ? `, the gateway dropped ${r.dropped}` : ''}`)).join('; ') : 'no panel' }
}
export const shapes = async page => shapesOf(await page.evaluate(() => window.__xrPages.read()))

/** The row at the end of every block: every picture that arrived on every panel has the shape of its page. */
export const shapeRow = async (page, check, name) => { const sh = await shapes(page); check(name, sh.ok, sh.detail); return sh }

/** Where a panel's page is on the screen, in CSS pixels of the screen: its top left corner and its size. */
export async function pageBox(page, id = 'page') {
  const s = (await read(page)).sources[id]?.seen
  const a = await onScreen(page, 0, 0, id)
  const b = await onScreen(page, s.css[0], s.css[1], id)
  return { x: a.x, y: a.y, width: b.x - a.x, height: b.y - a.y, css: s.css }
}

/** A picture of a part of the screen, as the browser's own screenshot, base64. */
export const shotOf = async (page, clip) => (await page.screenshot({ clip: { x: Math.round(clip.x), y: Math.round(clip.y), width: Math.round(clip.width), height: Math.round(clip.height) } })).toString('base64')

/** Decode two pictures in the page and say what share of their pixels differ by more than `by` in a channel. */
export const looksApart = (page, a, b, by = 40) => page.evaluate(async ([pa, pb, bound]) => {
  const load = src => new Promise((done, fail) => { const i = new Image(); i.onload = () => done(i); i.onerror = fail; i.src = `data:image/png;base64,${src}` })
  const [ia, ib] = await Promise.all([load(pa), load(pb)])
  const w = Math.min(ia.naturalWidth, ib.naturalWidth)
  const h = Math.min(ia.naturalHeight, ib.naturalHeight)
  const data = img => { const c = document.createElement('canvas'); c.width = w; c.height = h; const x = c.getContext('2d', { willReadFrequently: true }); x.drawImage(img, 0, 0); return x.getImageData(0, 0, w, h).data }
  const da = data(ia)
  const db = data(ib)
  let apart = 0
  for (let i = 0; i < da.length; i += 4) if (Math.abs(da[i] - db[i]) > bound || Math.abs(da[i + 1] - db[i + 1]) > bound || Math.abs(da[i + 2] - db[i + 2]) > bound) apart += 1
  return { share: apart / (w * h), size: [w, h], sameSize: ia.naturalWidth === ib.naturalWidth && ia.naturalHeight === ib.naturalHeight }
}, [a, b, by])

/**
 * Find a thing by its colour in a picture: the middle of the pixels whose channels lie in `range` ([lo, hi] for r,
 * g, b), as a share of the picture's width and height, and how many there are. Decoded in the page.
 */
export const findColour = (page, png, range) => page.evaluate(async ([src, [r, g, b]]) => {
  const img = await new Promise((done, fail) => { const i = new Image(); i.onload = () => done(i); i.onerror = fail; i.src = `data:image/png;base64,${src}` })
  const c = document.createElement('canvas')
  c.width = img.naturalWidth
  c.height = img.naturalHeight
  const ctx = c.getContext('2d', { willReadFrequently: true })
  ctx.drawImage(img, 0, 0)
  const d = ctx.getImageData(0, 0, c.width, c.height).data
  let n = 0, sx = 0, sy = 0
  for (let y = 0; y < c.height; y += 1) for (let x = 0; x < c.width; x += 1) {
    const i = (y * c.width + x) * 4
    if (d[i] >= r[0] && d[i] <= r[1] && d[i + 1] >= g[0] && d[i + 1] <= g[1] && d[i + 2] >= b[0] && d[i + 2] <= b[1]) { n += 1; sx += x; sy += y }
  }
  return n ? { count: n, x: (sx / n + 0.5) / c.width, y: (sy / n + 0.5) / c.height } : { count: 0, x: 0, y: 0 }
}, [png, range])


/**
 * Decode two pictures of the same part of the screen in the page and say where they differ: the box of the pixels
 * that differ by more than `by` in a channel, as shares of the picture's width and height, and how many there are.
 */
export const changedBox = (page, a, b, by = 24) => page.evaluate(async ([pa, pb, bound]) => {
  const load = src => new Promise((done, fail) => { const i = new Image(); i.onload = () => done(i); i.onerror = fail; i.src = `data:image/png;base64,${src}` })
  const [ia, ib] = await Promise.all([load(pa), load(pb)])
  const w = Math.min(ia.naturalWidth, ib.naturalWidth)
  const h = Math.min(ia.naturalHeight, ib.naturalHeight)
  const data = img => { const c = document.createElement('canvas'); c.width = w; c.height = h; const x = c.getContext('2d', { willReadFrequently: true }); x.drawImage(img, 0, 0); return x.getImageData(0, 0, w, h).data }
  const da = data(ia)
  const db = data(ib)
  let n = 0, x0 = w, y0 = h, x1 = -1, y1 = -1
  for (let y = 0; y < h; y += 1) for (let x = 0; x < w; x += 1) {
    const i = (y * w + x) * 4
    if (Math.abs(da[i] - db[i]) > bound || Math.abs(da[i + 1] - db[i + 1]) > bound || Math.abs(da[i + 2] - db[i + 2]) > bound) { n += 1; if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y }
  }
  return n ? { count: n, x: x0 / w, y: y0 / h, width: (x1 - x0 + 1) / w, height: (y1 - y0 + 1) / h } : { count: 0, x: 0, y: 0, width: 0, height: 0 }
}, [a, b, by])
