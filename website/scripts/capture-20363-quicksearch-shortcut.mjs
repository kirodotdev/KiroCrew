/**
 * Screenshot harness for issue #20363: Ctrl+K stops opening Search Everywhere
 * once the shortcut is switched away from Ctrl+K.
 *
 * The user-visible change in this PR is the Alt+K Search Everywhere reference
 * row: in double-Shift mode it no longer advertises the ⌘K alias (which no
 * longer fires there), while mod-k still shows the Ctrl/⌘ + K gesture. Each
 * frame asserts its own DOM text before it is written, so a still frame is real
 * evidence.
 *
 * Runs the REAL built SPA (website/dist) gateway-free behind the shared
 * stubDashboardApi fixtures, like capture-nav-shortcut-hint.mjs.
 *
 * Usage: node scripts/capture-20363-quicksearch-shortcut.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/20363-quicksearch-shortcut'

const slots = [
  { key: 'chat-1', title: 'Command search shortcut', messages: 4, running: false, agent: 'kirocrew', mode: '', created: '2026-08-05T01:00:00Z', last_ts: '2026-08-05T04:00:00Z', folder_id: '' },
]

mkdirSync(OUT, { recursive: true })

let base

async function openChat(browser, config) {
  const context = await browser.newContext({ viewport: { width: 1400, height: 940 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, {
    slots,
    theme: 'dark',
    localStorageEntries: {
      'mc-color-theme': 'kiro-dark',
      'mc-privacy-notice-v1': '1',
      'mc-nav': '1',
      'mc-quicksearch-shortcut': JSON.stringify(config),
    },
  })
  logPageProblems(page)
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(t => document.documentElement.getAttribute('data-theme') === t, 'kiro-dark', { timeout: 15000 })
  return { context, page }
}

/** Capture the Search Everywhere row in the Alt+K reference for one mode. */
async function shootShortcutsRow(browser, config, name, expectIncludes, expectExcludes) {
  const { context, page } = await openChat(browser, config)
  await page.keyboard.press('Alt+K')
  const dialog = page.getByRole('dialog', { name: 'Keyboard shortcuts' })
  await dialog.waitFor({ state: 'visible', timeout: 15000 })
  // The Search Everywhere row container (the flex row holding label + caps).
  const label = dialog.getByText('Search Everywhere', { exact: true }).last()
  await label.waitFor({ state: 'visible', timeout: 15000 })
  const row = label.locator('xpath=..')
  await row.scrollIntoViewIfNeeded()
  await page.waitForTimeout(200)
  const text = (await row.innerText()).trim()
  if (!text.includes(expectIncludes)) {
    throw new Error(`${name}: row "${text}" does not include "${expectIncludes}" — refusing to write a frame that does not show what it claims`)
  }
  for (const ex of expectExcludes) {
    if (text.includes(ex)) {
      throw new Error(`${name}: row "${text}" still contains "${ex}" — refusing to write a frame that does not show what it claims`)
    }
  }
  const inView = await row.evaluate(el => {
    const r = el.getBoundingClientRect()
    return r.top >= 0 && r.bottom <= window.innerHeight && r.height > 0
  })
  if (!inView) {
    throw new Error(`${name}: the Search Everywhere row is not within the viewport — refusing to write a frame that does not show it`)
  }
  const path = `${OUT}/${name}.png`
  await row.screenshot({ path })
  console.log(`wrote ${path} row=${JSON.stringify(text)}`)
  await context.close()
}

async function main() {
  const served = await serveDist()
  base = served.base
  const srv = served.srv
  const browser = await chromium.launch()
  try {
    // double-Shift: the row shows only the ⇧⇧ key caps — no Ctrl/⌘ + K alias,
    // because ⌘K no longer opens the palette in this mode.
    await shootShortcutsRow(browser, { mode: 'double-shift' }, 'shortcuts-row-double-shift', 'Search Everywhere', ['Ctrl', '⌘'])
    // mod-k: the row still shows the Ctrl/⌘ + K gesture.
    await shootShortcutsRow(browser, { mode: 'mod-k' }, 'shortcuts-row-mod-k', 'Search Everywhere', [])
  } finally {
    await browser.close()
    srv.close()
  }
}

await main()
