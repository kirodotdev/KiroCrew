/**
 * Screenshots of the About page's "Upcoming update" block, inline under Update.
 *
 * Serves the REAL built SPA (website/dist) against the shared gateway-free
 * stub, so no gateway and no credential are involved. The block reads one
 * status field, `update_upcoming`, which this harness stages through the
 * stub's `/api/status` route. Two scenes, each in light and dark:
 *
 * - `none`: no newer version announced or offered. The block draws nothing,
 *   and the assertion below also checks it left no empty card behind.
 * - `announced`: a published notice for a version the feed does not offer yet.
 *
 * Builds the SPA first (SKIP_BUILD=1 reuses dist): serve-dist serves what is on
 * disk, so shooting a stale dist would show the page without the block.
 *
 * Usage, from website/: node scripts/capture-about-upcoming-update.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/about-update-preview'
const VIEWPORT = { width: 1100, height: 1000 }

// A mise-managed node exports its own lib/node on LD_LIBRARY_PATH, whose
// libstdc++ predates the one Chromium's bundled libs need.
delete process.env.LD_LIBRARY_PATH

mkdirSync(OUT, { recursive: true })

const UPCOMING = {
  state: 'announced',
  version: '0.13.0',
  date: '2026-11-18',
  highlights: [
    'Faster startup on large workspaces',
    'Clearer output from the doctor command',
    'Fewer approval prompts during long tasks',
  ],
  notes_url: 'https://github.com/kirodotdev/KiroCrew/releases/tag/v0.13.0',
}

const SCENES = {
  none: { update_upcoming: null },
  announced: { update_upcoming: UPCOMING },
}

let failures = 0
const check = (label, got, want) => {
  const ok = got === want
  if (!ok) failures++
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}: got ${JSON.stringify(got)}${ok ? '' : ` want ${JSON.stringify(want)}`}`)
}

async function shoot(browser, base, scene, theme) {
  const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme,
    localStorageEntries: { 'mc-onboarded': '1', 'mc-privacy-notice-v1': '1', 'mc-lang': 'en' },
    extra: async (path, route) => {
      if (path !== '/api/status') return false
      await json(route, {
        sessions: 0, crons: 0, lessons: 0, uptime: 120, version: '0.12.2',
        update_can_apply: true, update_check_status: 'succeeded', update_available: false,
        ...SCENES[scene],
      })
      return true
    },
  })
  await page.goto(`${base}/settings/about`, { waitUntil: 'domcontentloaded' })
  const title = page.getByText('Updates', { exact: true }).first()
  await title.waitFor({ state: 'visible', timeout: 20_000 })
  const block = page.getByTestId('upcoming-update')
  if (scene === 'none') {
    // Let the status settle before asserting an absence.
    await page.locator('[data-testid^="hero-"]').first().waitFor({ state: 'visible', timeout: 20_000 })
  } else {
    await block.waitFor({ state: 'visible', timeout: 20_000 })
    check(`${scene}/${theme} state attribute`, await block.getAttribute('data-state'), scene)
    check(`${scene}/${theme} has no button of its own`, await block.getByRole('button').count(), 0)
    check(`${scene}/${theme} is not inside a dialog`, await page.locator('[role="dialog"] [data-testid="upcoming-update"]').count(), 0)
    check(`${scene}/${theme} closing line`,
      (await page.getByTestId('upcoming-update-takes-effect').textContent()).includes('restart'), true)
  }
  if (scene === 'none') check(`${scene}/${theme} draws nothing`, await block.count(), 0)
  if (scene !== 'none') check(`${scene}/${theme} opens no dialog`, await page.getByRole('dialog').count(), 0)
  // The Updates card, which holds the block when there is one.
  const card = page.locator('div').filter({ has: title }).filter({ hasText: 'View all releases' }).last()
  await card.scrollIntoViewIfNeeded()
  const file = `${OUT}/${scene}-${theme}.png`
  await card.screenshot({ path: file, animations: 'disabled' })
  console.log('wrote', file)
  await context.close()
}

async function main() {
  if (!process.env.SKIP_BUILD) {
    console.log('building dist (SKIP_BUILD=1 to reuse)...')
    execFileSync('npm', ['run', 'build'], { stdio: 'inherit', shell: process.platform === 'win32' })
  }
  const { srv, base } = await serveDist()
  const browser = await chromium.launch({ executablePath: chromiumExecutable() })
  try {
    for (const scene of Object.keys(SCENES)) {
      for (const theme of ['light', 'dark']) await shoot(browser, base, scene, theme)
    }
  } finally {
    await browser.close()
    srv.close()
  }
  if (failures) { console.error(`${failures} check(s) failed`); process.exit(1) }
}

main().catch(err => { console.error(err); process.exit(1) })
