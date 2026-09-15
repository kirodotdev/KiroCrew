#!/usr/bin/env node
/**
 * capture-site.mjs — Approach A: screenshot every route of a RUNNING app.
 *
 * Given a base URL and a list of routes, visit each and save a full-page PNG,
 * then the critic fs_reads each shot. Prefers Playwright (full-page + networkidle
 * + animation settle); falls back to headless Chrome (viewport shot only).
 *
 * Usage:
 *   node capture-site.mjs --base=http://localhost:3000 \
 *        --routes=/,/about,/login --out=./shots [--width=1280 --height=900] [--full]
 *   node capture-site.mjs --base=... --routes-file=routes.json --out=./shots
 *
 * routes.json: [{ "path": "/", "label": "Home" }, ...]  (or a bare ["/", "/about"])
 * Prints one JSON line per shot: {"route","label","file","ok","bytes","engine"}
 */
import { existsSync, mkdirSync, statSync, readFileSync } from 'node:fs'
import { resolve, isAbsolute, join } from 'node:path'
import { getPlaywright } from './ensure-playwright.mjs'
import { installSsrfGuard } from './ssrf-guard.mjs'

function args(argv) {
  const o = { base: 'http://localhost:3000', out: './shots', width: 1280, height: 900, full: false, routes: '', routesFile: '' }
  for (const a of argv) {
    const m = a.match(/^--([^=]+)=(.*)$/)
    if (m) o[m[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = m[2]
    else if (a === '--full') o.full = true
  }
  o.width = +o.width; o.height = +o.height
  return o
}

function slug(p) {
  const s = p.replace(/^https?:\/\/[^/]+/, '').replace(/[/?#:&=]+/g, '-').replace(/^-|-$/g, '')
  return s === '' ? 'home' : s
}

/**
 * Collapse a route label to ONE safe filename segment.
 *
 * A label reaches the filesystem as `<outDir>/<label>.png`, and with
 * `--routes-file` it is whatever the caller (or a model) put in the JSON. A label
 * of `../../victim` would walk out of `outDir` and overwrite an existing file, so
 * keep only the last segment, allow a conservative character set, and refuse a
 * leading dot so `..` cannot survive.
 */
function safeLabel(v) {
  const last = String(v == null ? '' : v).split(/[/\\]+/).filter(Boolean).pop() || ''
  const cleaned = last.replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^[.-]+/, '').replace(/-+$/, '')
  return cleaned === '' ? 'screen' : cleaned.slice(0, 80)
}

function loadRoutes(o) {
  if (o.routesFile) {
    const raw = JSON.parse(readFileSync(o.routesFile, 'utf8'))
    return raw.map(r => (typeof r === 'string'
      ? { path: r, label: safeLabel(slug(r)) }
      : { path: r.path, label: safeLabel(r.label || slug(r.path)) }))
  }
  return (o.routes || '/').split(',').map(p => p.trim()).filter(Boolean).map(p => ({ path: p, label: safeLabel(slug(p)) }))
}

async function tryPlaywright() {
  return await getPlaywright()
}

// Robust content-wait sequence (Playwright path), condensed from capture-designs SOP.
async function settle(page, o) {
  await page.waitForLoadState('domcontentloaded').catch(() => {})
  await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {})
  // scroll to trigger reveal animations, then force entry-animations to end state
  await page.evaluate(async (vh) => {
    const h = document.body.scrollHeight
    for (let y = 0; y < h; y += vh / 2) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 120)) }
    window.scrollTo(0, 0)
    document.querySelectorAll('[class*="animate-"],[class*="fade"],[class*="slide"],[class*="reveal"],[data-aos]').forEach(el => {
      el.style.animation = 'none'; el.style.opacity = '1'; el.style.transform = 'none'; el.style.transition = 'none'
    })
  }, o.height).catch(() => {})
  await page.waitForTimeout(500)
}

async function main() {
  const o = args(process.argv.slice(2))
  const routes = loadRoutes(o)
  const outDir = isAbsolute(o.out) ? o.out : resolve(process.cwd(), o.out)
  mkdirSync(outDir, { recursive: true })

  const pw = await tryPlaywright()
  const results = []

  // Playwright is REQUIRED: it is the only render path that can address-validate
  // every request and redirect (installSsrfGuard). A raw headless-Chrome
  // screenshot follows redirects with no per-request hook, so it is not a safe
  // fallback for a public URL — refuse rather than render unguarded.
  if (!pw) {
    console.error('capture-site: Playwright unavailable — cannot render safely.')
    process.exit(3)
  }
  // Pin Chromium's DNS for the vetted base host to the address the backend
  // already validated (passed as --resolve=host:ip1,ip2), so a rebinding name
  // cannot resolve to an internal IP on the browser's OWN lookup. Mirrors the
  // git-clone curloptResolve pin; the per-request installSsrfGuard still guards
  // any other host a redirect/subresource reaches.
  const launchArgs = []
  if (o.resolve) {
    const idx = o.resolve.indexOf(':')
    const host = idx > 0 ? o.resolve.slice(0, idx) : ''
    const ip = idx > 0 ? o.resolve.slice(idx + 1).split(',')[0] : ''
    if (host && ip) launchArgs.push(`--host-resolver-rules=MAP ${host} ${ip}`)
  }
  const browser = await pw.chromium.launch({ channel: 'chrome', args: launchArgs }).catch(() => pw.chromium.launch({ args: launchArgs }))
  const ctx = await browser.newContext({ viewport: { width: o.width, height: o.height }, serviceWorkers: 'block' })
  let baseOrigin = null
  try { baseOrigin = new URL(o.base).origin } catch { baseOrigin = null }
  await installSsrfGuard(ctx, baseOrigin)
  const page = await ctx.newPage()
  for (const [i, r] of routes.entries()) {
    // Index-prefixed: safeLabel() collapses distinct routes onto the same
    // label ('/a/b' and '/a?b' both become 'a-b'), so a label alone let the
    // second capture overwrite the first and both results then pointed at
    // one image. The index is unique per route by construction.
    const file = join(outDir, `${String(i + 1).padStart(2, '0')}-${r.label}.png`)
    let ok = false
    try {
      // A synthetic "/" route means "the base URL itself" (a full URL with a
      // query becomes base + "/" upstream); appending "/" would corrupt it
      // (…?view=cart/), so navigate to o.base unchanged for the root route.
      const target = (r.path === '/' || r.path === '') ? o.base : o.base + r.path
      await page.goto(target, { waitUntil: 'domcontentloaded', timeout: 30000 })
      await settle(page, o)
      await page.screenshot({ path: file, fullPage: !!o.full })
      ok = existsSync(file)
    } catch (e) { /* record failure below */ }
    const bytes = ok ? statSync(file).size : 0
    // Low byte-floor only catches truly empty files; real blank/skeleton
    // detection is the visual fs_read check the critic does afterward.
    results.push({ route: r.path, label: r.label, file, ok: ok && bytes > 2000, bytes, engine: 'playwright' })
  }
  await browser.close()

  for (const r of results) console.log(JSON.stringify(r))
  const good = results.filter(r => r.ok).length
  console.error(`capture-site: ${good}/${results.length} routes captured (engine: ${results[0]?.engine || 'none'})`)
  process.exit(good > 0 ? 0 : 3)
}

main().catch(e => { console.error('capture-site: failed:', e?.message || e); process.exit(4) })
