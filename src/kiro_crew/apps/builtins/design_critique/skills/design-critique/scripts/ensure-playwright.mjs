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
import { existsSync, mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const PW_VERSION = '1.47.2' // last line that supports Node 18
const CACHE = process.env.DC_PW_DIR || join(homedir(), '.cache', 'kirocrew-design-critique')
const PW_ENTRY = join(CACHE, 'node_modules', 'playwright', 'index.js')

// Playwright is CJS; a dynamic import may put the API on `.default`.
function normalize(mod) {
  if (!mod) return null
  if (mod.chromium) return mod
  if (mod.default && mod.default.chromium) return mod.default
  return null
}

export async function getPlaywright({ autoInstall = true } = {}) {
  // 1. resolvable from the normal module paths?
  try { const m = normalize(await import('playwright')); if (m) return m } catch { /* not here */ }
  // 2. present in the cache dir?
  if (existsSync(PW_ENTRY)) {
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
