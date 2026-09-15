#!/usr/bin/env node
/**
 * capture-build.mjs — render a project's ALREADY-BUILT output to PNGs.
 *
 * Why this exists: a built SPA cannot be opened from the filesystem. Vite/CRA/Next
 * emit absolute asset paths (src="/assets/index-abc.js"), which resolve to the
 * filesystem root over file:// and never load — you get a white page. Serving the
 * same folder over http fixes it completely.
 *
 * This NEVER runs a build and NEVER runs the project's dev server or npm scripts.
 * It only serves files that already exist, from a loopback-bound static server it
 * starts and stops itself.
 *
 * Usage:
 *   node capture-build.mjs <project-dir|build-dir> [--routes=/,/agents,/settings]
 *                          [--out=<dir>] [--width=1280] [--height=900] [--full]
 *
 * Prints JSON: { buildDir, spa, served, screens:[{route,path,chars}], skipped:[…], notes:[…] }
 */
import { createServer } from 'node:http'
import { existsSync, readFileSync, statSync, mkdirSync, readdirSync } from 'node:fs'
import { join, resolve, extname, isAbsolute } from 'node:path'
import { tmpdir } from 'node:os'
import { getPlaywright } from './ensure-playwright.mjs'
import { installSsrfGuard } from './ssrf-guard.mjs'

// Build outputs worth trying, best first. storybook-static is the richest:
// every component in every state, already rendered.
const BUILD_DIRS = ['storybook-static', 'dist', 'build', 'out', 'public/dist', '.next/server/app']

const MIME = {
  '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript',
  '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif',
  '.webp': 'image/webp', '.avif': 'image/avif', '.ico': 'image/x-icon',
  '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf', '.map': 'application/json',
}

function parseArgs(argv) {
  const pos = []
  const opt = { width: 1280, height: 900, full: false, routes: null, out: null }
  for (const a of argv) {
    if (a.startsWith('--width=')) opt.width = parseInt(a.slice(8), 10)
    else if (a.startsWith('--height=')) opt.height = parseInt(a.slice(9), 10)
    else if (a === '--full') opt.full = true
    else if (a.startsWith('--routes=')) opt.routes = a.slice(9).split(',').map(s => s.trim()).filter(Boolean)
    else if (a.startsWith('--out=')) opt.out = a.slice(6)
    else pos.push(a)
  }
  return { dir: pos[0], opt }
}

/** Find the build output. The given dir may itself already be one. */
function findBuild(root, notes) {
  if (existsSync(join(root, 'index.html')) && !existsSync(join(root, 'package.json'))) {
    return { dir: root, why: 'given directory is already a built site' }
  }
  for (const cand of BUILD_DIRS) {
    const d = join(root, cand)
    if (existsSync(join(d, 'index.html'))) return { dir: d, why: cand }
  }
  // A bare index.html next to package.json is usually a dev template (script
  // pointing at /src/main.tsx), which will not render. Say so rather than trying.
  if (existsSync(join(root, 'index.html'))) {
    const html = readFileSync(join(root, 'index.html'), 'utf8')
    if (/src=["']\/?src\//.test(html)) {
      notes.push('Found index.html but it is a dev template pointing at /src — needs a build or a dev server.')
    } else {
      return { dir: root, why: 'index.html at project root' }
    }
  }
  return null
}

/**
 * Index every file under the build dir ONCE, as url-path -> absolute-path.
 *
 * The request handler then only ever *looks up* a key in this map, so no part of
 * a request string is used to build a filesystem path. That removes the class of
 * bug entirely rather than trying to sanitise it: there is no path arithmetic on
 * user input left to get wrong, and CodeQL's "uncontrolled data in a path
 * expression" no longer has a flow to report because every path handed to fs
 * originates from our own readdir walk.
 *
 * WHAT THIS ENUMERATES IS A CONTRACT with routes.py's `_served_signature`, which
 * takes the staleness token that decides whether a probe screenshot may be reused
 * for a later render. That token signs exactly the set indexed here — the Dirent
 * test below counts neither a symlinked directory nor a symlinked file, and
 * dot-entries are skipped. Widening this walk without widening that one leaves a
 * served file unsigned, so a change to it would not move the token and a stale
 * screenshot would be reused. Change both together.
 */
function indexBuildFiles(rootDir) {
  const root = resolve(rootDir)
  const byUrl = new Map()
  const walk = (dir, prefix) => {
    let entries
    try {
      entries = readdirSync(dir, { withFileTypes: true })
    } catch {
      return
    }
    for (const entry of entries) {
      if (entry.name.startsWith('.')) continue
      const abs = join(dir, entry.name)
      const url = prefix + '/' + entry.name
      if (entry.isDirectory()) walk(abs, url)
      else if (entry.isFile()) byUrl.set(url, abs)
    }
  }
  walk(root, '')
  return byUrl
}

function startServer(rootDir, isSpa) {
  const byUrl = indexBuildFiles(rootDir)
  const shell = byUrl.get('/index.html') || null
  return new Promise((res) => {
    const srv = createServer((req, response) => {
      let urlPath
      try {
        urlPath = decodeURIComponent((req.url || '/').split('?')[0])
      } catch {
        response.writeHead(400).end('bad request')
        return
      }
      // Lookups only — never a join/resolve against urlPath.
      let file = byUrl.get(urlPath)
        || byUrl.get(urlPath.replace(/\/+$/, '') + '/index.html')
        || (urlPath === '/' ? shell : undefined)
      // SPA client-side routes have no file on disk — fall back to the shell.
      if (!file && isSpa && !extname(urlPath)) file = shell || undefined
      if (!file) { response.writeHead(404).end('not found'); return }
      try {
        const body = readFileSync(file)
        response.writeHead(200, { 'content-type': MIME[extname(file).toLowerCase()] || 'application/octet-stream' })
        response.end(body)
      } catch {
        response.writeHead(500).end('error')
      }
    })
    // Loopback only, ephemeral port.
    srv.listen(0, '127.0.0.1', () => res({ srv, port: srv.address().port }))
  })
}

/** Static HTML files under a build dir, as fallback "routes". */
function htmlFiles(dir, base = '', depth = 0, acc = []) {
  if (depth > 3) return acc
  for (const name of readdirSync(dir)) {
    if (name.startsWith('.') || name === 'assets' || name === 'node_modules') continue
    const full = join(dir, name)
    if (statSync(full).isDirectory()) htmlFiles(full, base + '/' + name, depth + 1, acc)
    else if (extname(name) === '.html') acc.push(base + '/' + name)
  }
  return acc
}

/**
 * Find a modal / banner / gate covering the page. A fresh browser profile has no
 * stored state, so first-run onboarding, cookie banners and login walls appear on
 * EVERY route — capturing them yields N screenshots of one overlay and none of the
 * actual screens.
 */
function findOverlay(page) {
  return page.evaluate(() => {
    const vw = innerWidth, vh = innerHeight, viewport = vw * vh
    const explicit = document.querySelector('[aria-modal="true"], [role="dialog"], [role="alertdialog"], dialog[open]')
    const consider = explicit ? [explicit] : Array.from(document.body?.querySelectorAll('*') || [])
    let best = null
    for (const el of consider) {
      const cs = getComputedStyle(el)
      if (!explicit && cs.position !== 'fixed' && cs.position !== 'absolute') continue
      if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue
      const r = el.getBoundingClientRect()
      const area = (r.width * r.height) / viewport
      if (area < 0.05 || area > 1.6) continue
      const z = parseInt(cs.zIndex || '0', 10) || 0
      if (!explicit && z < 10) continue
      const text = (el.innerText || '').replace(/\s+/g, ' ').trim()
      if (!text) continue
      const score = (explicit ? 1e6 : 0) + z * 100 + area * 10
      if (!best || score > best.score) {
        best = { score, z, area: +area.toFixed(3), text: text.slice(0, 140), explicit: !!explicit }
      }
    }
    return best
  }).catch(() => null)
}

/**
 * Whether the page's scrollable height already fits the viewport, so a viewport
 * screenshot holds the same pixels a fullPage one would.
 *
 * THIS IS A CONTRACT with routes.py, which reuses a probe PNG for a later /render
 * only when the screen record says the PNG covers the whole page. Playwright's
 * `fullPage` extends a capture along the HEIGHT only — width stays the viewport
 * width either way — so height is the only axis on which the two modes differ.
 *
 * An unreadable page (detached frame, hostile script) counts as NOT fitting: the
 * caller then re-captures with --full rather than assuming an equivalence it could
 * not confirm. Erring this way costs one screenshot; erring the other way silently
 * drops everything below the fold from a critique.
 */
function pageFitsViewport(page) {
  return page.evaluate(() => {
    // The scrolling box is documentElement in standards mode, but a page can put
    // overflow on body instead; take the taller so neither is missed.
    const h = Math.max(document.documentElement?.scrollHeight || 0, document.body?.scrollHeight || 0)
    // scrollHeight is a rounded integer while innerHeight can be fractional under a
    // non-integral devicePixelRatio, so allow one pixel of rounding slack.
    return h > 0 && h <= innerHeight + 1
  }).catch(() => false)
}

const sig = (o) => (o && o.text ? o.text.slice(0, 60).toLowerCase() : '')

async function main() {
  const { dir: dirArg, opt } = parseArgs(process.argv.slice(2))
  if (!dirArg) {
    console.error('usage: node capture-build.mjs <project-dir> [--routes=/,/a,/b] [--out=dir] [--full]')
    process.exit(1)
  }
  const root = isAbsolute(dirArg) ? dirArg : resolve(process.cwd(), dirArg)
  if (!existsSync(root)) { console.error(`capture-build: not found: ${root}`); process.exit(2) }

  const notes = []
  const found = findBuild(root, notes)
  if (!found) {
    console.log(JSON.stringify({
      buildDir: null, usableForVisualCritique: false, askUserFor: 'screenshots or a Figma link',
      screens: [], skipped: [], blockedBy: null, notes: notes.concat([
        'No built output found. Looked for: ' + BUILD_DIRS.join(', ') + '.',
        'Nothing can be rendered from source alone — ask the user to build it, or for a running URL, or for screenshots.',
      ]),
    }, null, 2))
    return
  }

  const outDir = opt.out ? (isAbsolute(opt.out) ? opt.out : resolve(process.cwd(), opt.out)) : tmpdir()
  mkdirSync(outDir, { recursive: true })

  // Routes with :params can't be rendered meaningfully — skip them, don't fake them.
  const skipped = []
  let routes = opt.routes
  if (routes) {
    routes = routes.filter(r => {
      if (/[:*]/.test(r)) { skipped.push({ route: r, why: 'needs a real parameter value' }); return false }
      return true
    })
  }
  const isSpa = !!routes && routes.length > 0
  if (!routes || !routes.length) {
    routes = htmlFiles(found.dir)
    if (!routes.length) routes = ['/']
    notes.push('No --routes given; captured the HTML files present in the build.')
  }

  const { srv, port } = await startServer(found.dir, isSpa)
  const base = `http://127.0.0.1:${port}`
  const pw = await getPlaywright()
  if (!pw) {
    srv.close()
    console.error('capture-build: Playwright unavailable — cannot render.')
    process.exit(3)
  }
  const browser = await pw.chromium.launch({ channel: 'chrome' }).catch(() => pw.chromium.launch())
  // Guard on the CONTEXT so popups inherit it, and block service workers so a
  // built page cannot route requests through a worker the page-level interceptor
  // never sees. The served base is our own loopback server (allowed for THIS
  // origin only); the guard blocks a built page's redirect/subresource to any
  // other private or loopback address.
  const ctx = await browser.newContext({ viewport: { width: opt.width, height: opt.height }, serviceWorkers: 'block' })
  await installSsrfGuard(ctx, base)
  const screens = []
  try {
    for (const route of routes) {
      const page = await ctx.newPage()
      const url = base + (route.startsWith('/') ? route : '/' + route)
      let chars = 0
      try {
        await page.goto(url, { waitUntil: 'networkidle', timeout: 30000 })
        await page.waitForTimeout(350)
        chars = await page.evaluate(() => (document.body?.innerText || '').replace(/\s+/g, ' ').trim().length).catch(() => 0)
        const overlay = await findOverlay(page)
        // Measure before the screenshot but after the settle above, so lazy content
        // that changes the page height is already in.
        const fits = await pageFitsViewport(page)
        const safe = route.replace(/[^\w.-]+/g, '_').replace(/^_+|_+$/g, '') || 'index'
        const outPath = join(outDir, `build-${safe}-${Date.now()}.png`)
        await page.screenshot({ path: outPath, fullPage: opt.full })
        // Same rule as render.mjs: a blank page is NOT a seen screen.
        if (chars < 8) skipped.push({ route, why: 'rendered blank (' + chars + ' chars) — not counted as seen' })
        // `fullPageCoverage` describes the PNG, not the page: a --full capture always
        // covers it, and a viewport capture does when the page fits. routes.py reuses
        // a probe PNG only when this is true.
        else screens.push({
          route, path: outPath, chars, overlay: overlay || null,
          fullPageCoverage: opt.full || fits,
        })
      } catch (err) {
        skipped.push({ route, why: (err && err.message ? err.message : String(err)).slice(0, 120) })
      } finally {
        await page.close()
      }
    }
  } finally {
    await browser.close()
    srv.close()
  }

  // If the SAME overlay sits on most screens, these captures are not usable as a
  // visual critique — they all show one gate. Say so and ask for another input.
  let blockedBy = null
  const withOverlay = screens.filter(s => s.overlay && sig(s.overlay))
  if (withOverlay.length >= 2) {
    const counts = new Map()
    for (const s of withOverlay) {
      const k = sig(s.overlay)
      counts.set(k, (counts.get(k) || 0) + 1)
    }
    let topKey = '', topN = 0
    for (const [k, n] of counts) if (n > topN) { topN = n; topKey = k }
    if (screens.length && topN / screens.length >= 0.6) {
      const one = withOverlay.find(s => sig(s.overlay) === topKey)
      blockedBy = {
        onScreens: topN,
        ofScreens: screens.length,
        coversViewport: one.overlay.area,
        text: one.overlay.text,
        likely: /cookie|consent|gdpr/i.test(one.overlay.text) ? 'cookie/consent banner'
          : /sign in|log in|login|password|continue with/i.test(one.overlay.text) ? 'login wall'
          : 'first-run / onboarding gate',
      }
    }
  }

  const out = {
    buildDir: found.dir, spa: isSpa, served: base, screens, skipped, blockedBy,
    notes: notes.concat([`Served ${found.why} over loopback http; no build or dev server was run.`]),
  }
  if (blockedBy) {
    out.usableForVisualCritique = false
    out.askUserFor = 'screenshots or a Figma link'
    out.notes.push(
      `BLOCKED: the same ${blockedBy.likely} covers ${blockedBy.onScreens} of ${blockedBy.ofScreens} ` +
      `captured screens (~${Math.round(blockedBy.coversViewport * 100)}% of the viewport): ` +
      `"${blockedBy.text.slice(0, 70)}…". A fresh browser profile has no stored state, so this gate ` +
      `appears on every route.`,
      'Do NOT critique these captures — every one shows the same gate, not the screen behind it. ' +
      'Report the gate ONCE at most, then ask the user for screenshots of the real screens, or a Figma link.',
    )
  } else {
    out.usableForVisualCritique = screens.length > 0
  }
  console.log(JSON.stringify(out, null, 2))
}

main().catch((e) => { console.error('capture-build: failed:', e?.message || e); process.exit(4) })
