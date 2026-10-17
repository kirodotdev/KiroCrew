#!/usr/bin/env node
/**
 * ensure-playwright.mjs — resolve Playwright ON DEMAND, never vendored in the app.
 *
 * Resolution order:
 *   1. a normally-resolvable `playwright` (globally / already on the path)
 *   2. a cached copy under ~/.cache/kirocrew-design-critique (override: $DC_PW_DIR)
 *   3. install it into that cache dir on first use, then load it
 *
 * Falls back to null so callers can use headless Chrome instead. Uses your
 * installed Chrome via channel:'chrome', so no browser download is needed.
 *
 *   import { getPlaywright } from './ensure-playwright.mjs'
 *   const pw = await getPlaywright()          // { chromium, ... } or null
 *   const pw = await getPlaywright({ autoInstall: false })  // never install
 */
import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const PW_VERSION = '1.58.2' // matches website/package.json; must be >= 1.48 for routeWebSocket (ssrf-guard.mjs refuses without it) and needs a modern Node (repo docs say >= 22)
const CACHE = process.env.DC_PW_DIR || join(homedir(), '.cache', 'kirocrew-design-critique')
const PW_ENTRY = join(CACHE, 'node_modules', 'playwright', 'index.js')

// ssrf-guard.mjs refuses to run without routeWebSocket (Playwright >= 1.48), so
// resolving an older build would only trade a silent fail-open for a permanent
// refusal. Gate every resolution path on the version actually found: too old
// (or unreadable) falls through to the pinned install instead of returning a
// build the guard must reject. A host that cached the old 1.47.2 pin therefore
// self-heals -- the install step below upgrades the cache in place.
const MIN_PW = [1, 48]
function pwVersionUsable(pkgJsonPath) {
  try {
    const v = JSON.parse(readFileSync(pkgJsonPath, 'utf8')).version || ''
    const m = /^(\d+)\.(\d+)/.exec(v)
    if (!m) return false
    const maj = Number(m[1]), min = Number(m[2])
    return maj > MIN_PW[0] || (maj === MIN_PW[0] && min >= MIN_PW[1])
  } catch {
    return false
  }
}

// Playwright is CJS; a dynamic import may put the API on `.default`.
function normalize(mod) {
  if (!mod) return null
  if (mod.chromium) return mod
  if (mod.default && mod.default.chromium) return mod.default
  return null
}

export async function getPlaywright({ autoInstall = true } = {}) {
  // 1. resolvable from the normal module paths (only when it can provide the
  //    routeWebSocket the SSRF guard requires)?
  try {
    const pkg = createRequire(import.meta.url).resolve('playwright/package.json')
    if (pwVersionUsable(pkg)) { const m = normalize(await import('playwright')); if (m) return m }
  } catch { /* not here */ }
  // 2. present in the cache dir? An old cached pin (e.g. the former 1.47.2)
  //    fails the version gate and falls through, so the install below
  //    replaces it rather than leaving capture permanently refused.
  if (existsSync(PW_ENTRY) && pwVersionUsable(join(CACHE, 'node_modules', 'playwright', 'package.json'))) {
    try { const m = normalize(await import(pathToFileURL(PW_ENTRY).href)); if (m) return m } catch { /* fallthrough */ }
  }
  if (!autoInstall) return null
  // 3. install into the cache dir on demand (one-time)
  console.error(`ensure-playwright: installing playwright@${PW_VERSION} into ${CACHE} (one-time, needs network)…`)
  try { mkdirSync(CACHE, { recursive: true }) } catch { /* ignore */ }
  const r = spawnSync('npm', ['install', '--prefix', CACHE, `playwright@${PW_VERSION}`, '--no-audit', '--no-fund', '--silent'],
    { stdio: 'inherit', timeout: 240000 })
  if (r.status !== 0 || !existsSync(PW_ENTRY)) {
    console.error('ensure-playwright: install failed — falling back to headless Chrome.')
    return null
  }
  // The npm package ships no browser binaries, so installing it alone leaves
  // every launch rejecting on a machine with no Chrome of its own (a bare Linux
  // box is the normal case). Download the Chromium build too. The env is
  // inherited untouched so it lands in Playwright's own home-directory cache —
  // the same place the launch side will look, and never inside the project.
  const cli = join(CACHE, 'node_modules', 'playwright', 'cli.js')
  if (existsSync(cli)) {
    console.error('ensure-playwright: downloading the Chromium build (one-time)…')
    const b = spawnSync(process.execPath, [cli, 'install', 'chromium'],
      { stdio: 'inherit', timeout: 600000 })
    if (b.status !== 0) {
      // Not fatal: a machine that already has Chrome installed still works via
      // the channel fallback, so report and continue rather than refusing.
      console.error('ensure-playwright: Chromium download failed — will try a system browser.')
    }
  }
  try { return normalize(await import(pathToFileURL(PW_ENTRY).href)) } catch { return null }
}
