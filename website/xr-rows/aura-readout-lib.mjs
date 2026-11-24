// Round 15, the Aura's readout (AURA-READOUT-1.md): the built XR page of the readout's worktree, opened with NO
// server at all. The test browser itself answers every request of one made-up origin from the build's folder, and
// answers the gateway's routes with "no gateway" (or a small stand-in). Nothing listens on any port for this.
import { createRequire } from 'node:module'
import { existsSync, readFileSync, statSync } from 'node:fs'
import { extname, join, normalize } from 'node:path'
import { installEmulator } from './xr-emu/xr-emu-lib.mjs'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.XR_PLAYWRIGHT ?? 'playwright')

export const DIST = process.env.XR_DIST ?? 'website/dist'
/** A made-up origin: `localhost` is a secure context, which WebXR needs. No request to it leaves the browser. */
export const ORIGIN = 'http://localhost:6795'
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.png': 'image/png', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf', '.glb': 'model/gltf-binary', '.wasm': 'application/wasm', '.ico': 'image/x-icon', '.webp': 'image/webp', '.jpg': 'image/jpeg', '.map': 'application/json', '.txt': 'text/plain', '.bin': 'application/octet-stream' }

/** `api`: path -> JSON body, for the gateway's routes a run stands in for. `init`: scripts run before the page's own. */
/** `clock`: the page's clock is Playwright's (`context.clock`) from before the page's first line, so a row can stop it and step it. */
export async function open(query, { viewport = { width: 1440, height: 900 }, api = {}, init = [], video = null, clock = false } = {}) {
  const browser = await chromium.launch({ args: ['--ignore-gpu-blocklist', '--enable-unsafe-swiftshader', '--use-angle=swiftshader'] })
  const context = await browser.newContext({ viewport, deviceScaleFactor: 1, ...(video ? { recordVideo: { dir: video, size: viewport } } : {}) })
  if (clock) await context.clock.install()
  await installEmulator(context)
  for (const content of init) await context.addInitScript({ content })
  const asked = { api: [], outside: [] }
  await context.route('**/*', route => {
    const url = new URL(route.request().url())
    if (url.origin !== ORIGIN) { asked.outside.push(url.href.slice(0, 120)); return route.abort() }
    const path = decodeURIComponent(url.pathname)
    if (path.startsWith('/api/') || path.startsWith('/v1/')) {
      asked.api.push(path)
      const body = api[path]
      return body !== undefined ? route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) }) : route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"no gateway in this run"}' })
    }
    const file = normalize(join(DIST, path))
    if (file.startsWith(DIST) && existsSync(file) && statSync(file).isFile()) return route.fulfill({ status: 200, contentType: TYPES[extname(file)] ?? 'application/octet-stream', body: readFileSync(file) })
    // The dashboard is one page: every other path is its index.
    return extname(path) ? route.fulfill({ status: 404, body: 'not in the build' }) : route.fulfill({ status: 200, contentType: 'text/html', body: readFileSync(join(DIST, 'index.html')) })
  })
  // No gateway answers here. On the twelfth's main (bdc65269728) a socket that never opens, or never sends a frame,
  // makes the dashboard cover its content with the "host is not running" view (`host-offline`) and make it inert, over
  // the XR page's own buttons. These rows are about the readout, not the socket: the socket is stood in for, open,
  // with one frame of a type that nothing reads.
  await context.routeWebSocket(/\/api\/ws/, ws => { ws.send(JSON.stringify({ _type: 'xr-readout-stand-in' })) })
  await (await import('./pages-show.mjs')).showPages(context)
  const page = await context.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e.message).slice(0, 300)))
  await page.goto(`${ORIGIN}/xr?${query}`, { waitUntil: 'domcontentloaded', timeout: 120000 })
  return { browser, context, page, errors, asked }
}
export const sleep = ms => new Promise(done => setTimeout(done, ms))
