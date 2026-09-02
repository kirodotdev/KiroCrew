#!/usr/bin/env node
/**
 * render.mjs — render a renderable design to a PNG so the critic can SEE it.
 *
 * The Design Critique skill calls this, then reads the PNG (fs_read) to run a
 * real *visual* critique. Reading source code is never a visual evaluation —
 * this is what turns "code" input into pixels.
 *
 * Usage:
 *   node render.mjs <input> [outPath] [--width=1280] [--height=900] [--full] [--wait=ms]
 *
 *   <input>   a URL (http/https) OR a local file path (.html, .htm, .svg)
 *   outPath   defaults to a temp file; the resolved path is printed on the last stdout line
 *
 * Strategy: prefer Playwright (channel: 'chrome' — uses installed Chrome, no
 * browser download; full-page + network-idle waiting). Fall back to headless
 * Chrome's built-in --screenshot when Playwright isn't installed.
 */
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { resolve, isAbsolute } from 'node:path'
import { pathToFileURL } from 'node:url'
import { getPlaywright } from './ensure-playwright.mjs'

function parseArgs(argv) {
  const pos = []
  const opt = { width: 1280, height: 900, full: false, wait: 0, allowBlank: false }
  for (const a of argv) {
    if (a.startsWith('--width=')) opt.width = parseInt(a.slice(8), 10)
    else if (a.startsWith('--height=')) opt.height = parseInt(a.slice(9), 10)
    else if (a === '--full') opt.full = true
    else if (a === '--allow-blank') opt.allowBlank = true
    else if (a.startsWith('--wait=')) opt.wait = parseInt(a.slice(7), 10)
    else pos.push(a)
  }
  return { input: pos[0], out: pos[1], opt }
}

function toTarget(input) {
  if (/^https?:\/\//i.test(input)) return input
  const abs = isAbsolute(input) ? input : resolve(process.cwd(), input)
  if (!existsSync(abs)) {
    console.error(`render: input file not found: ${abs}`)
    process.exit(2)
  }
  return pathToFileURL(abs).href
}

function chromeBinary() {
  const candidates = [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
  ]
  return candidates.find(existsSync) || null
}

// A screenshot of a blank page is worse than no screenshot: the critic can't tell
// and will "critique" nothing. Inspect the live DOM before trusting the PNG.
async function inspectEmptiness(page) {
  return page.evaluate(() => {
    const text = (document.body?.innerText || '').replace(/\s+/g, ' ').trim()
    let painted = 0
    for (const el of Array.from(document.body?.querySelectorAll('*') || [])) {
      const r = el.getBoundingClientRect()
      if (r.width >= 8 && r.height >= 8) painted++
      if (painted > 12) break
    }
    return { textLen: text.length, painted, sample: text.slice(0, 80) }
  }).catch(() => ({ textLen: -1, painted: -1, sample: '' }))
}

async function renderWithPlaywright(target, out, opt) {
  const pw = await getPlaywright()
  if (!pw) return { ok: false } // not available and couldn't install — signal fallback
  const chromium = pw.chromium
  const browser = await chromium.launch({ channel: 'chrome' }).catch(() => chromium.launch())
  try {
    const page = await browser.newPage({ viewport: { width: opt.width, height: opt.height } })
    // Track assets that never arrived — the usual cause of a white SPA shell.
    const deadAssets = []
    page.on('requestfailed', (r) => { if (deadAssets.length < 6) deadAssets.push(r.url()) })
    page.on('response', (r) => { if (r.status() >= 400 && deadAssets.length < 6) deadAssets.push(r.url() + ' → ' + r.status()) })
    await page.goto(target, { waitUntil: 'networkidle', timeout: 30000 }).catch(() => {})
    if (opt.wait) await page.waitForTimeout(opt.wait)
    const empty = await inspectEmptiness(page)
    await page.screenshot({ path: out, fullPage: opt.full })
    return { ok: true, empty, deadAssets }
  } finally {
    await browser.close()
  }
}

function renderWithChrome(target, out, opt) {
  const bin = chromeBinary()
  if (!bin) return false
  const args = [
    '--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-sandbox',
    `--window-size=${opt.width},${opt.height}`,
    `--screenshot=${out}`,
    target,
  ]
  const r = spawnSync(bin, args, { stdio: 'ignore', timeout: 45000 })
  return r.status === 0 && existsSync(out)
}

async function main() {
  const { input, out: outArg, opt } = parseArgs(process.argv.slice(2))
  if (!input) {
    console.error('usage: node render.mjs <url|file> [outPath] [--width=] [--height=] [--full] [--wait=ms]')
    process.exit(1)
  }
  const target = toTarget(input)
  const out = outArg
    ? (isAbsolute(outArg) ? outArg : resolve(process.cwd(), outArg))
    : resolve(tmpdir(), `design-critique-${Date.now()}.png`)

  const res = await renderWithPlaywright(target, out, opt)
  let ok = res.ok
  let engine = 'playwright'
  if (!ok) { ok = renderWithChrome(target, out, opt); engine = 'chrome-headless' }

  if (!ok) {
    console.error('render: no renderer available. Install Playwright (`npm i playwright`) or Google Chrome.')
    process.exit(3)
  }

  // Refuse to hand back a blank page pretending to be a design.
  const e = res.empty
  const blank = e && e.textLen >= 0 && e.textLen < 8 && e.painted <= 1
  if (blank && !opt.allowBlank) {
    console.error('render: BLANK PAGE — nothing rendered (no text, no laid-out elements).')
    if (res.deadAssets && res.deadAssets.length) {
      console.error('render: assets that failed to load:')
      for (const u of res.deadAssets) console.error('  - ' + u)
    }
    if (/^file:\/\//.test(target)) {
      console.error('render: this looks like a built app opened from the filesystem. Absolute asset')
      console.error('render: paths (src="/assets/…") cannot resolve over file://. Serve the folder')
      console.error('render: over http instead — use capture-build.mjs, which does this for you.')
    }
    console.error('render: treat this screen as NOT SEEN. Do not critique it. (--allow-blank overrides.)')
    process.exit(5)
  }

  // Machine-readable last line: the screenshot path the critic should read.
  const note = blank ? ', blank (allowed)' : e && e.textLen > 0 ? `, ${e.textLen} chars visible` : ''
  console.error(`render: ok via ${engine} (${opt.width}x${opt.height}${opt.full ? ', full-page' : ''}${note})`)
  console.log(out)
}

main().catch((e) => { console.error('render: failed:', e?.message || e); process.exit(4) })
