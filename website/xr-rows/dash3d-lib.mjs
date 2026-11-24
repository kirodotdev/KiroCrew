// Round 15, the 3D dashboard's first slice (DASH3D-1.md): the built XR page of the dash3d worktree, shown at the
// throwaway dev gateway's own address with NO server of its own. The test browser answers the page's files from the
// worktree's build folder; every other request (the gateway's routes, its socket) goes to the dev gateway as it is.
// Nothing listens on any port for this, and nothing of the gateway is changed.
import { existsSync, readFileSync, statSync } from 'node:fs'
import { extname, join, normalize } from 'node:path'
import { BASE, chromium } from './v3-lib.mjs'

export const DIST = process.env.XR_DIST ?? 'dist'
// The signed-in browser state of the THROWAWAY gateway, as the other rows' tools use it (v3-lib.mjs keeps it fresh).
const STATE = process.env.XR_STATE_FILE ?? (process.env.XR_SCRATCH ?? '.') + '/devstate.json'
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.png': 'image/png', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.woff': 'font/woff', '.wasm': 'application/wasm', '.webp': 'image/webp', '.glb': 'model/gltf-binary', '.ico': 'image/x-icon', '.map': 'application/json' }
const GATEWAY = /^\/(api|v1|ws|media|artifacts-raw|healthz)(\/|$)/

export const sleep = ms => new Promise(done => setTimeout(done, ms))
export async function waitUntil(test, ms = 8000, step = 120) {
  const end = Date.now() + ms
  for (;;) { const got = await test().catch(() => false); if (got) return got; if (Date.now() > end) return false; await sleep(step) }
}

/**
 * Open the room of pages. `fixture`: the sample crew and its sample dashboards (nothing of a Bot is read).
 * `standIn(context)`: a row's own answers for the gateway's routes, set before the page loads.
 */
// `address`: a whole `/xr?...` address of its own instead of the room of pages (`view=dash`); then the room's own
// waits are not waited for, only the canvas.
export async function open({ query = '', fixture = false, video = null, viewport = { width: 1440, height: 900 }, still = false, standIn = null, path = '/chat', emulate = false, address = null } = {}) {
  // The page's document is answered by this tool, so the browser has no address to place it at and would refuse it
  // the gateway's socket on this machine (its local-network check). The check is switched off in this test browser only.
  const browser = await chromium.launch({ args: ['--ignore-gpu-blocklist', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--disable-features=LocalNetworkAccessChecks,BlockInsecurePrivateNetworkRequests,PrivateNetworkAccessSendPreflights'] })
  const context = await browser.newContext({
    viewport, deviceScaleFactor: 1, serviceWorkers: 'block',
    ...(existsSync(STATE) ? { storageState: STATE } : {}),
    ...(still ? { reducedMotion: 'reduce' } : {}),
    ...(video ? { recordVideo: { dir: video, size: viewport } } : {}),
  })
  const origin = new URL(BASE).origin
  const served = { files: 0, index: 0 }
  await context.route(url => url.origin === origin, route => {
    const url = new URL(route.request().url())
    const p = decodeURIComponent(url.pathname)
    if (GATEWAY.test(p)) return route.fallback()
    const file = normalize(join(DIST, p))
    if (file.startsWith(DIST) && existsSync(file) && statSync(file).isFile()) { served.files += 1; return route.fulfill({ status: 200, contentType: TYPES[extname(file)] ?? 'application/octet-stream', body: readFileSync(file) }) }
    // The dashboard is one page: a path with no file type is its index. Anything else is the gateway's.
    if (!extname(p) && route.request().resourceType() === 'document') { served.index += 1; return route.fulfill({ status: 200, contentType: 'text/html', body: readFileSync(join(DIST, 'index.html')) }) }
    return route.fallback()
  })
  if (standIn) await standIn(context)
  // `emulate`: the emulated headset runtime (xr-emu), for a row in a session.
  if (emulate) { const { installEmulator } = await import('./xr-emu/xr-emu-lib.mjs'); await installEmulator(context) }
  await (await import('./pages-show.mjs')).showPages(context)
  const page = await context.newPage()
  const errors = []
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text().slice(0, 300)) })
  page.on('pageerror', e => errors.push('pageerror: ' + String(e.message).slice(0, 300)))
  // D192 (lead 14:41, ruling 5): a room row runs only from the room. The sign-in's refresh chain revoked sends the tab
  // to the dashboard's root (`hooks/useRefreshScheduler.ts`: `window.location.assign(withBase('/'))`), and from there to
  // a chat: the row is then NOT RUN, with the place it found, never red.
  const away = { revoked: false }
  page.on('console', m => { if (/\[refresh\] chain revoked/.test(m.text())) away.revoked = true })
  const notRun = why => { console.log(`NOT-RUN ${why}: the tab stands on ${page.url()}, not /xr`); process.exit(0) }
  const guard = () => { if (away.revoked) notRun('the sign-in was refused (refresh chain revoked)'); if (!address && new URL(page.url()).pathname !== '/xr') notRun('the tab left the room') }
  // Everything the page sends to the gateway for a panel (a press on a page), as it goes out.
  const sent = []
  page.on('request', r => { if (r.url().includes('/api/browser/panel/input')) { const body = r.postDataJSON(); if (body?.event) sent.push({ ...body.event, panel: body.panel, at: Date.now() }) } })
  page.on('response', r => { if (r.status() >= 400) errors.push(`${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`) })
  await page.goto(address ? `${BASE}${address}` : `${BASE}/xr?view=pages&page=${encodeURIComponent(path)}&probe=1${fixture ? '&fixture=1' : ''}${query}`, { waitUntil: 'domcontentloaded', timeout: 120000 })
  await page.waitForSelector('[data-testid="xr-canvas"]', { timeout: 120000 })
  if (address) return { browser, context, page, errors, served, sent }
  // XR_NO_SKIP=1: a read-only open presses nothing of the page, the first run's "Skip all" included (DASH-16).
  const skip = page.getByText('Skip all').first()
  if (process.env.XR_NO_SKIP !== '1' && await skip.waitFor({ state: 'visible', timeout: 5000 }).then(() => true, () => false)) await skip.click({ timeout: 2500 }).catch(() => {})
  await page.waitForFunction(() => !!window.__xrPages?.look && window.__xrPages.read().view.width > 1, null, { timeout: 60000 }).catch(async () => {
    guard(); console.log('NO VIEW after 60 s: ' + JSON.stringify({ url: page.url(), errors: errors.slice(0, 12), text: (await page.evaluate(() => document.body.innerText).catch(() => '')).slice(0, 300) })); throw new Error('the room did not open') })
  // The refresh comes a few seconds after the page loads (w1317: 8.8 s): wait that long before the row's first step.
  await waitUntil(async () => away.revoked || null, 10000, 250)
  guard()
  return { browser, context, page, errors, served, sent }
}

export const read = page => page.evaluate(() => window.__xrPages.read())
export const dashOf = async page => (await read(page)).dash3d
export const data = page => page.evaluate(() => window.__xrDash3d?.read() ?? null)
/** Where a box of the picture (least and most x and y, from -1 to 1) is on the screen, in px: its middle and its corners. */
export function onScreen(box, viewport) {
  const x = v => ((v + 1) / 2) * viewport.width
  const y = v => ((1 - v) / 2) * viewport.height
  return { x: x((box[0] + box[2]) / 2), y: y((box[1] + box[3]) / 2), left: x(box[0]), right: x(box[2]), top: y(box[3]), bottom: y(box[1]) }
}
/** A click as a hand makes one: down, a short hold, up, at one place. */
export async function click(page, x, y, hold = 80) { await page.mouse.move(x, y); await sleep(60); await page.mouse.down(); await sleep(hold); await page.mouse.up(); await sleep(60) }
