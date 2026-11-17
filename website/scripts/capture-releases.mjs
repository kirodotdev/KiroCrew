/**
 * Screenshot harness for Settings > About: the install card, Updates with its
 * "What you'd get" list, and the release notes section (running release, then
 * the capped list of earlier ones).
 *
 * Starts its own Vite dev server bound to loopback only (127.0.0.1), or uses
 * the base URL given, and answers /api/** from the shared fixture router, so the shot goes through
 * the app's own router, Settings nav, react-query and markdown renderer rather
 * than a hand-mounted panel.
 *
 * The /api/releases bodies are not written by hand and not checked in either:
 * they are produced at capture time by running the real backend parser over the
 * repo's real CHANGELOG.md (scripts/lib/releases-fixtures.mjs). The update's
 * changelog lines are the newest section of that same file, standing in for
 * the lines a git checkout's check adds.
 *
 * Scenario, theme and locale ride QUERY PARAMS rather than being flipped in
 * place: the init script reads them on every load.
 *
 * Usage: node scripts/capture-releases.mjs [outDir] [viteBase]
 *        PYTHON=/path/to/python overrides the interpreter used for the parser.
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync, readFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { installApiFixtures, logPageFailures, json } from './lib/api-fixtures.mjs'
import { realReleasePayloads } from './lib/releases-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/about-releases'
// Loopback only: the harness serves nothing to any other machine.
const server = process.argv[3] ? null : await createServer({ server: { host: '127.0.0.1', port: 5199, strictPort: true } })
if (server) await server.listen()
const BASE = process.argv[3] || 'http://127.0.0.1:5199'
const WIDE = { width: 1440, height: 1000 }
const NARROW = { width: 390, height: 844 }

const FIXTURES = realReleasePayloads([
  ['stable', '0.12.2'],
  ['nightly', '0.13.0-nightly.20261111t060622'],
  ['behind', '0.12.1'],
])

/** What scripts/feed_release_notes.py writes into the desktop feed for `version`. */
function feedNotes(version) {
  return JSON.parse(execFileSync(process.env.PYTHON || 'python3', ['scripts/feed_release_notes.py', version], { cwd: '..', encoding: 'utf8' }))
}

/** The newest CHANGELOG.md section, as the lines a checkout's check adds. */
function newestSection() {
  const md = readFileSync('../CHANGELOG.md', 'utf8')
  const start = md.indexOf('\n## [')
  const next = md.indexOf('\n## [', start + 1)
  return md.slice(start + 1, next).trim()
}

function statusFor(rel, update, canApply = true) {
  const version = FIXTURES[rel].current_version
  const channel = rel === 'stable' ? 'stable' : rel
  return {
    sessions: 0, crons: 0, lessons: 0, uptime: 120,
    version, version_display: version, release_channel: channel, update_channel: channel,
    branch: 'main', commit: '4dd046d206',
    update_available: update, update_can_apply: canApply,
    update_command: canApply ? '' : 'kirocrew update', update_check_status: 'succeeded',
    update_latest_version: update ? '0.13.0' : version,
    update_latest_version_display: update ? '0.13.0' : version,
    update_last_checked_at: Math.floor(Date.now() / 1000) - 600,
    update_commits_ahead: 0, update_commits_behind: update ? 42 : 0,
  }
}

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()

async function open({ rel = 'stable', update = false, notes = true, canApply = true, desktop = null, theme = 'light', size = WIDE, path = '/settings/about' }) {
  const context = await browser.newContext({ viewport: size, deviceScaleFactor: size === NARROW ? 2 : 1 })
  const page = await context.newPage()
  logPageFailures(page)
  await installApiFixtures(page, {
    '/api/status': statusFor(rel, update, canApply),
    '/api/theme/boot': { mode: theme, theme: '' },
    '/api/releases': FIXTURES[rel],
    '/api/members': { members: [] },
  })
  if (desktop) {
    // A stand-in for the Electron preload: the running build's info, and one
    // live 'found' payload carrying the feed's notes.
    await page.addInitScript(d => {
      window.updateAPI = {
        getInfo: async () => d.info,
        check: async () => ({ ok: true }),
        download: async () => ({ ok: true }),
        onState: cb => { setTimeout(() => cb(d.state), 300); return () => {} },
      }
    }, desktop)
  }
  // The fixture router's `**/api/**` glob also matches dev-server module paths
  // such as /src/api/client.ts; hand those back to Vite.
  await page.route(url => !url.pathname.startsWith('/api/'), route => route.continue())
  await page.route('**/api/update/check', route => json(route, {
    check_status: 'succeeded', update_available: update, latest_version: update ? '0.13.0' : FIXTURES[rel].current_version,
    changes: update && notes ? newestSection() : '', can_apply: true, channel: rel === 'stable' ? 'stable' : rel,
    commits_ahead: 0, commits_behind: update ? 42 : 0, last_checked_at: Math.floor(Date.now() / 1000),
  }))
  await page.addInitScript(t => {
    localStorage.clear()
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-theme', t)
  }, theme)
  await page.goto(`${BASE}${path}`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-testid="releases-current"], [data-testid="releases-earlier"]', { timeout: 30_000 })
  await page.waitForTimeout(800)
  // An available update also opens the app-wide "update found" prompt; this
  // harness shoots the About page underneath it.
  for (let i = 0; i < 2 && await page.locator('div[role="presentation"].fixed').count(); i++) {
    await page.keyboard.press('Escape')
    await page.waitForTimeout(300)
  }
  await page.mouse.move(size.width - 5, size.height - 5)
  return { page, context }
}

async function shot(page, name, opts = {}) {
  const file = `${OUT}/${name}.png`
  await page.screenshot({ path: file, ...opts })
  console.log('wrote', file)
}

// 1-4. Wide and narrow, light and dark, a stable build with an update available
//      and its changes loaded.
for (const theme of ['light', 'dark']) {
  for (const [label, size] of [['wide', WIDE], ['narrow', NARROW]]) {
    const { page, context } = await open({ update: true, theme, size })
    await page.getByRole('button', { name: "Show what's in this update" }).click()
    await page.waitForSelector('[data-testid="update-changes"]')
    await page.waitForTimeout(300)
    await shot(page, `${label}-${theme}-update`, { fullPage: true })
    await context.close()
  }
}

// 4b. Up to date on stable, wide and narrow.
for (const [label, size] of [['wide', WIDE], ['narrow', NARROW]]) {
  const { page, context } = await open({ size })
  await shot(page, `${label}-light-uptodate`, { fullPage: true })
  await context.close()
}

// 4c. The old bare /settings/releases address lands on the release notes section.
{
  const { page, context } = await open({ path: '/settings/releases' })
  await page.waitForTimeout(500)
  console.log('  landed on', page.url())
  await shot(page, 'redirect-bare-releases')
  await context.close()
}

// 5. A nightly build: the running release is unreleased and badged.
{
  const { page, context } = await open({ rel: 'nightly' })
  await page.locator('[data-testid="releases-current"]').scrollIntoViewIfNeeded()
  await shot(page, 'wide-light-nightly')
  await context.close()
}

// 6. Expanded release and the next page of rows.
{
  const { page, context } = await open({ theme: 'dark' })
  await page.locator('[data-testid="releases-earlier"]').scrollIntoViewIfNeeded()
  await page.getByRole('button', { name: /^0\.11\.0/ }).click()
  await page.waitForTimeout(300)
  await shot(page, 'wide-dark-expanded')
  await context.close()
}

// 7. An old /settings/releases/<version> link lands on About with that release open.
{
  const { page, context } = await open({ path: '/settings/releases/0.9.0' })
  await page.waitForTimeout(500)
  console.log('  landed on', page.url())
  await shot(page, 'redirect-0.9.0')
  await context.close()
}

// 8. Update available before this tab has checked: the offer to load its changes.
{
  const { page, context } = await open({ update: true })
  await page.getByTestId('update-changes-load').scrollIntoViewIfNeeded()
  await shot(page, 'wide-light-update-load-offer')
  // 9. The check answered with no changelog lines: "no changelog entries".
  await context.close()
}
{
  const { page, context } = await open({ update: true, notes: false })
  await page.getByTestId('update-changes-load').click()
  await page.waitForSelector('[data-testid="update-changes-none"]')
  await shot(page, 'wide-light-update-no-notes')
  await context.close()
}

// 10. A link to a version this build does not list, and the Show N more control.
{
  const { page, context } = await open({ path: '/settings/about?release=9.9.9' })
  await page.waitForSelector('[data-testid="releases-link-missing"]')
  await page.waitForTimeout(400)
  await shot(page, 'wide-light-unknown-version-and-show-more')
  await context.close()
}

// 11. A build behind the changelog: newer versions in their own card.
{
  const { page, context } = await open({ rel: 'behind' })
  await page.locator('[data-testid="releases-newer"]').scrollIntoViewIfNeeded()
  await shot(page, 'wide-light-newer-than-yours')
  await context.close()
}

// 12. An update with no notes in reach (command install, version not in the list).
{
  const { page, context } = await open({ update: true, canApply: false })
  await page.waitForSelector('[data-testid="update-changes-empty"]')
  await shot(page, 'wide-light-update-notes-not-shown')
  await context.close()
}

// 13. Desktop: the feed's notes, the 0.12.2 update's own section, under a running 0.12.0.
{
  const desktop = {
    info: { version: '0.12.0', channel: 'stable', stampedChannel: 'stable', packaged: true, channelSwitchable: true, platform: 'darwin-arm64' },
    state: { state: 'found', version: '0.12.2', notes: feedNotes('0.12.2') },
  }
  const { page, context } = await open({ rel: 'behind', desktop })
  await page.waitForSelector('[data-testid="update-changes"]', { timeout: 15_000 })
  await page.locator('[data-testid="update-changes"]').scrollIntoViewIfNeeded()
  await shot(page, 'wide-light-desktop-update')
  await context.close()
}

// 14. A recording: an old /settings/releases/<version> link lands on About with
//    that release open, then another release is opened in place and more rows
//    are shown.
{
  const context = await browser.newContext({ viewport: WIDE, recordVideo: { dir: OUT, size: WIDE } })
  const page = await context.newPage()
  await installApiFixtures(page, {
    '/api/status': statusFor('stable', false),
    '/api/theme/boot': { mode: 'light', theme: '' },
    '/api/releases': FIXTURES.stable,
    '/api/members': { members: [] },
  })
  await page.route(url => !url.pathname.startsWith('/api/'), route => route.continue())
  await page.addInitScript(() => { localStorage.clear(); localStorage.setItem('mc-onboarded', '1'); localStorage.setItem('mc-theme', 'light') })
  await page.goto(`${BASE}/settings/releases/0.10.0`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('#release-0\\.10\\.0 [aria-expanded="true"]', { timeout: 30_000 })
  await page.waitForTimeout(2000)
  await page.getByRole('button', { name: /^0\.10\.0/ }).click()
  await page.waitForTimeout(800)
  await page.locator('[data-testid="releases-earlier"]').scrollIntoViewIfNeeded()
  await page.waitForTimeout(800)
  await page.getByRole('button', { name: /^0\.12\.0/ }).click()
  await page.waitForTimeout(1500)
  await page.getByRole('button', { name: /^0\.12\.0/ }).click()
  await page.waitForTimeout(600)
  await page.getByTestId('releases-show-more').click()
  await page.waitForTimeout(1500)
  const video = page.video()
  await context.close()
  console.log('VIDEO', await video.path())
}

await browser.close()
await server?.close()
