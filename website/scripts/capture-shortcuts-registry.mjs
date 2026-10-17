/**
 * Screenshot harness for the keyboard-shortcut registry (#4608).
 *
 * Runs a BUILT SPA (`website/dist`, or the path in DIST) on the repo's static
 * server with every /api/** call answered by the shared stub — no gateway, no
 * dashboard token. Captures the Alt+K shortcuts reference and Settings →
 * Shortcuts in light and dark, so a base build and a branch build can be
 * shot with the same script for a before/after.
 *
 * Usage: DIST=<dist dir> OUT_DIR=<out dir> node scripts/capture-shortcuts-registry.mjs
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.env.OUT_DIR || '/tmp/shortcuts-registry-shots'
mkdirSync(OUT, { recursive: true })

const SLOTS = [
  { key: 'chat-1-a', title: 'Shortcut registry — P1', running: false, messages: 4, agent: 'kirocrew', last_ts: new Date().toISOString() },
  { key: 'chat-1-b', title: 'Conventional defaults', running: false, messages: 2, agent: 'kirocrew', last_ts: new Date(Date.now() - 3600_000).toISOString() },
]

const { srv, base } = await serveDist(process.env.DIST || DEFAULT_DIST)
const browser = await chromium.launch({ executablePath: chromiumExecutable() })

/**
 * `host`: 'browser' is a plain tab (browser-reserved chords demoted, Win/Linux
 * glyphs); 'desktop-mac' fakes the Electron preload bridge (`window.kirocrew`)
 * and a macOS platform so the modal renders the desktop state — ⌘N / ⌘W
 * leading, ⌘ glyphs — the way the shipped app does. The SPA reads both at
 * module load, so they are seeded before any script runs.
 */
async function open(theme, host = 'browser') {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 1400 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  if (host === 'desktop-mac') {
    await page.addInitScript(() => {
      Object.defineProperty(navigator, 'platform', { value: 'MacIntel', configurable: true })
      window.kirocrew = { isElectron: true, platform: 'darwin' }
    })
  } else if (host === 'desktop-linux') {
    // Windows/Linux desktop: Ctrl chords lead, no demotion, non-Mac glyphs.
    await page.addInitScript(() => { window.kirocrew = { isElectron: true, platform: 'linux' } })
  }
  await stubDashboardApi(page, { slots: SLOTS, folders: [], theme, localStorageEntries: { 'mc-lang': 'en' } })
  return { ctx, page }
}

for (const theme of ['light', 'dark']) {
  // Alt+K reference modal, opened from the chat page with the chord itself.
  {
    const { ctx, page } = await open(theme)
    await page.goto(`${base}/chat`)
    await page.waitForSelector('[data-slot-key]', { timeout: 20000 })
    await page.waitForTimeout(600)
    await page.keyboard.press('Alt+K')
    const dialog = page.getByRole('dialog', { name: 'Keyboard shortcuts' })
    await dialog.waitFor({ timeout: 10000 })
    // The card scrolls (max-h 80vh); the rows this PR changes live under ACTIONS.
    const card = dialog.locator('> div')
    await dialog.getByText('Actions', { exact: true }).evaluate(el => el.scrollIntoView({ block: 'start' }))
    await page.waitForTimeout(400)
    await card.screenshot({ path: `${OUT}/shortcuts-modal-${theme}.png` })
    console.log(`shortcuts-modal-${theme}.png`)
    await ctx.close()
  }
  // Settings → Shortcuts, the Actions card region.
  {
    const { ctx, page } = await open(theme)
    await page.goto(`${base}/settings/shortcuts`)
    await page.getByText('Open shortcuts help').first().waitFor({ timeout: 20000 })
    await page.waitForTimeout(600)
    await page.screenshot({ path: `${OUT}/settings-shortcuts-${theme}.png` })
    console.log(`settings-shortcuts-${theme}.png`)
    await ctx.close()
  }
}

// Desktop (Windows/Linux Electron) host: Ctrl+N / Ctrl+W lead, no demotion.
for (const theme of ['light', 'dark']) {
  const { ctx, page } = await open(theme, 'desktop-linux')
  await page.goto(`${base}/chat`)
  await page.waitForSelector('[data-slot-key]', { timeout: 20000 })
  await page.waitForTimeout(600)
  await page.keyboard.press('Alt+K')
  const dialog = page.getByRole('dialog', { name: 'Keyboard shortcuts' })
  await dialog.waitFor({ timeout: 10000 })
  const card = dialog.locator('> div')
  await dialog.getByText('Actions', { exact: true }).evaluate(el => el.scrollIntoView({ block: 'start' }))
  await page.waitForTimeout(400)
  await card.screenshot({ path: `${OUT}/shortcuts-modal-desktop-linux-${theme}.png` })
  console.log(`shortcuts-modal-desktop-linux-${theme}.png`)
  await ctx.close()
}

// Desktop (macOS Electron) host: the conventional chords lead, no demotion.
for (const theme of ['light', 'dark']) {
  const { ctx, page } = await open(theme, 'desktop-mac')
  await page.goto(`${base}/chat`)
  await page.waitForSelector('[data-slot-key]', { timeout: 20000 })
  await page.waitForTimeout(600)
  await page.keyboard.press('Alt+K')
  const dialog = page.getByRole('dialog', { name: 'Keyboard shortcuts' })
  await dialog.waitFor({ timeout: 10000 })
  const card = dialog.locator('> div')
  await dialog.getByText('Actions', { exact: true }).evaluate(el => el.scrollIntoView({ block: 'start' }))
  await page.waitForTimeout(400)
  await card.screenshot({ path: `${OUT}/shortcuts-modal-desktop-mac-${theme}.png` })
  console.log(`shortcuts-modal-desktop-mac-${theme}.png`)
  // The footer ("Enable shortcuts" + the always-works chords) sits below the
  // scrolled Actions view; one capture scrolled to the end shows it uncropped.
  await card.evaluate(el => { el.scrollTop = el.scrollHeight })
  await page.waitForTimeout(300)
  await card.screenshot({ path: `${OUT}/shortcuts-modal-desktop-mac-footer-${theme}.png` })
  console.log(`shortcuts-modal-desktop-mac-footer-${theme}.png`)
  await ctx.close()
}

await browser.close()
srv.close()
console.log('DONE', OUT)
