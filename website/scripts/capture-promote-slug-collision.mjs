/**
 * Screenshot harness for the de-duplicated slug report on the promote path.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * and answers every /api/** call from fixtures via Playwright route interception
 * (gateway-free -- no kiro-cli, no live backend). Same shape as
 * capture-artifact-notice-split.mjs.
 *
 * Promoting a chat document creates a file-backed artifact whose slug is derived
 * from the file name. When that slug is already taken the store silently appends
 * a numeric suffix, so the corrected document lands at `<slug>-2` while the
 * canonical slug keeps serving the OLDER artifact's text. The response now names
 * the slug it collided with, and the page surfaces it as a non-fatal notice --
 * before, the user was told only the new slug and could not tell the two apart.
 *
 * Frames:
 *   01-before-promote      the unsaved chat document, star (promote) affordance
 *   02-collision-notice    promoted -> notice naming BOTH slugs
 *   03-free-slug-no-notice control: derived slug was free -> no notice at all
 *
 * Frame 03 is the negative control: it proves the notice is driven by the
 * response FIELD and not by promoting at all, which is the same pair the unit
 * tests assert.
 *
 * Usage: node scripts/capture-promote-slug-collision.mjs [outDir] [prefix] [distDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/promote-slug-collision'
const PREFIX = process.argv[3] || 'after'
const DIST = process.argv[4] || DEFAULT_DIST

mkdirSync(OUT, { recursive: true })

/** The artifact ALREADY holding the name-derived slug, with the older text. */
const HOLDER = {
  slug: 'notes-md',
  name: 'notes.md',
  kind: 'markdown',
  source: 'chat',
  session_title: 'Research session',
  description: 'Promoted earlier -- holds the canonical slug',
  tags: [],
  version: 1,
  pinned: true,
  created_at: '2026-08-20T10:00:00.000000+00:00',
  updated_at: '2026-08-20T10:00:00.000000+00:00',
  content: '# Notes\n\nThe older text.\n',
}

/** The artifact the promote lands at once the derived slug turns out taken. */
const SUFFIXED = {
  ...HOLDER,
  slug: 'notes-md-2',
  description: 'Promoted just now -- landed at a suffixed slug',
  updated_at: '2026-09-01T19:00:00.000000+00:00',
  content: '# Notes\n\nThe corrected text.\n',
}

const DOC = {
  path: '/ws/research/notes.md',
  name: 'notes.md',
  updated_at: '2026-09-01T18:55:00',
  session_key: 'dashboard_chat-1',
  session_title: 'Research session',
  message_ts: 'm1',
  saved: false,
  slug: '',
}

/** When false the promote reports no collision (frame 03's control). */
let collides = true
/** Flipped once a promote has happened, so the refetch shows the new artifact. */
let promoted = false

const extra = async (path, route) => {
  if (path === '/api/artifacts') {
    const artifacts = promoted && collides ? [HOLDER, SUFFIXED] : [HOLDER]
    return json(route, { artifacts }), true
  }
  if (path === '/api/artifact-folders') return json(route, { folders: [] }), true
  if (path === '/api/artifacts/session-docs') {
    return json(route, { docs: promoted ? [] : [DOC] }), true
  }
  if (path === '/api/artifacts/materialize') {
    promoted = true
    // The server puts `slug_collided_with` on THIS response only; it names the
    // plain derived slug when the store had to suffix it, and is empty when the
    // derived slug was free.
    return json(route, collides
      ? { ...SUFFIXED, slug_collided_with: 'notes-md' }
      : { ...HOLDER, slug: 'notes-md', slug_collided_with: '' }), true
  }
  return false
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 1000 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await stubDashboardApi(page, { extra })
  logPageProblems(page)

  // ── Frame 1: the unsaved chat document, before promoting ──────────────────
  await page.goto(base + '/artifacts', { waitUntil: 'domcontentloaded' })
  await page.getByText('notes.md').first().waitFor({ timeout: 15000 })
  await page.waitForTimeout(600)
  await page.screenshot({
    path: `${OUT}/${PREFIX}-01-before-promote.png`,
    clip: { x: 0, y: 0, width: 1500, height: 620 },
  })
  console.log('wrote', `${OUT}/${PREFIX}-01-before-promote.png`)

  // ── Frame 2: promote -> the derived slug was taken, notice names both ─────
  await page.getByLabel('Star document').first().click()
  await page.getByText(/Saved under a different address/i).waitFor({ timeout: 15000 })
  await page.waitForTimeout(600)
  await page.screenshot({
    path: `${OUT}/${PREFIX}-02-collision-notice.png`,
    clip: { x: 0, y: 0, width: 1500, height: 620 },
  })
  console.log('wrote', `${OUT}/${PREFIX}-02-collision-notice.png`)

  // ── Frame 3: control -- derived slug free, so no notice at all ────────────
  collides = false
  promoted = false
  await page.goto(base + '/artifacts', { waitUntil: 'domcontentloaded' })
  await page.getByText('notes.md').first().waitFor({ timeout: 15000 })
  await page.getByLabel('Star document').first().click()
  // Wait for the promote to have been answered, then confirm the notice is absent.
  await page.waitForTimeout(1800)
  await page.screenshot({
    path: `${OUT}/${PREFIX}-03-free-slug-no-notice.png`,
    clip: { x: 0, y: 0, width: 1500, height: 620 },
  })
  console.log('wrote', `${OUT}/${PREFIX}-03-free-slug-no-notice.png`)

  // ── Frame 4: 320px -- the buttons stack instead of overflowing the notice ──
  collides = true
  promoted = false
  await page.setViewportSize({ width: 320, height: 900 })
  await page.goto(base + '/artifacts', { waitUntil: 'domcontentloaded' })
  await page.getByText('notes.md').first().waitFor({ timeout: 15000 })
  await page.getByLabel('Star document').first().click()
  await page.getByText(/Saved under a different address/i).waitFor({ timeout: 15000 })
  await page.waitForTimeout(600)
  await page.screenshot({
    path: `${OUT}/${PREFIX}-04-narrow-viewport-320.png`,
    clip: { x: 0, y: 0, width: 320, height: 520 },
  })
  console.log('wrote', `${OUT}/${PREFIX}-04-narrow-viewport-320.png`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
