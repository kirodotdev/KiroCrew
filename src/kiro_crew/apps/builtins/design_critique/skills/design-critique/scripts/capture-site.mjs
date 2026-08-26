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
import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, statSync, readFileSync } from 'node:fs'
import { resolve, isAbsolute, join } from 'node:path'
import { getPlaywright } from './ensure-playwright.mjs'

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

function chromeBinary() {
  return [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
  ].find(existsSync) || null
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

  if (pw) {
    const browser = await pw.chromium.launch({ channel: 'chrome' }).catch(() => pw.chromium.launch())
    const ctx = await browser.newContext({ viewport: { width: o.width, height: o.height } })
    const page = await ctx.newPage()
    for (const [i, r] of routes.entries()) {
      // Index-prefixed: safeLabel() collapses distinct routes onto the same
      // label ('/a/b' and '/a?b' both become 'a-b'), so a label alone let the
      // second capture overwrite the first and both results then pointed at
      // one image. The index is unique per route by construction.
      const file = join(outDir, `${String(i + 1).padStart(2, '0')}-${r.label}.png`)
      let ok = false
      try {
        await page.goto(o.base + r.path, { waitUntil: 'domcontentloaded', timeout: 30000 })
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
  } else {
    const bin = chromeBinary()
    for (const [i, r] of routes.entries()) {
      // Index-prefixed: safeLabel() collapses distinct routes onto the same
      // label ('/a/b' and '/a?b' both become 'a-b'), so a label alone let the
      // second capture overwrite the first and both results then pointed at
      // one image. The index is unique per route by construction.
      const file = join(outDir, `${String(i + 1).padStart(2, '0')}-${r.label}.png`)
      let ok = false
      if (bin) {
        const res = spawnSync(bin, ['--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-sandbox',
          `--window-size=${o.width},${o.height}`, `--screenshot=${file}`, o.base + r.path], { stdio: 'ignore', timeout: 45000 })
        ok = res.status === 0 && existsSync(file)
      }
      const bytes = ok ? statSync(file).size : 0
      results.push({ route: r.path, label: r.label, file, ok: ok && bytes > 1000, bytes, engine: bin ? 'chrome-headless' : 'none' })
    }
  }

  for (const r of results) console.log(JSON.stringify(r))
  const good = results.filter(r => r.ok).length
  console.error(`capture-site: ${good}/${results.length} routes captured (engine: ${results[0]?.engine || 'none'})`)
  process.exit(good > 0 ? 0 : 3)
}

main().catch(e => { console.error('capture-site: failed:', e?.message || e); process.exit(4) })
