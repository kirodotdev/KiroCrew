/**
 * Screenshot harness for the artifact page's label + value strings.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server and
 * answers every /api/** call from fixtures via Playwright route interception
 * (gateway-free -- no kiro-cli, no live backend).
 *
 * Each label and the value it names come from one catalog string, so a language
 * puts the value where its grammar wants it, and the footer dates go through the
 * shared date formatter instead of showing raw ISO timestamps.
 *
 * Frames:
 *   en  footer "Created <date> · Updated <date>", timeline "by user", "v1 → v3"
 *   de  the same page in German: "Erstellt am <date>", "von user" (a Latin-script
 *       language, so the frame is readable without CJK fonts on the capture host)
 *
 * This ASSERTS as well as photographs: each frame exits non-zero unless the page
 * shows the expected phrase and no raw ISO timestamp.
 *
 * Usage: node scripts/capture-artifact-label-values.mjs [outDir] [prefix] [distDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/28856'
const PREFIX = process.argv[3] || 'after'
const DIST = process.argv[4] || DEFAULT_DIST

mkdirSync(OUT, { recursive: true })

const ARTIFACT = {
  slug: 'quarterly-report',
  name: 'Quarterly report',
  kind: 'markdown',
  source: 'chat',
  description: 'Fixture artifact for the label capture',
  tags: [],
  version: 3,
  pinned: false,
  created_at: '2026-08-20T10:00:00.000000+00:00',
  updated_at: '2026-09-05T21:00:00.000000+00:00',
  content: '# Quarterly report\n\nA rendered artifact document.',
}

const EVENTS = [
  { ts: '2026-08-20T10:00:00.000Z', type: 'created', by: 'agent', version: 1 },
  { ts: '2026-09-05T21:00:00.000Z', type: 'reverted', by: 'user', version: 3, from_version: 1 },
]

const extra = async (path, route) => {
  if (path === '/api/artifacts') return json(route, { artifacts: [ARTIFACT] }), true
  if (path === '/api/artifact-folders') return json(route, { folders: [] }), true
  if (path === '/api/artifacts/session-docs') return json(route, { docs: [] }), true
  const m = /^\/api\/artifacts\/([^/]+)(\/.*)?$/.exec(path)
  if (!m) return false
  const rest = m[2] || ''
  if (rest === '/versions') return json(route, { slug: ARTIFACT.slug, versions: [1, 2, 3] }), true
  if (rest === '/events') return json(route, { slug: ARTIFACT.slug, events: EVENTS }), true
  if (rest === '/comments') return json(route, { comments: [] }), true
  if (rest === '/upstream-status') return json(route, {}), true
  if (rest === '') return json(route, ARTIFACT), true
  return false
}

async function frame(browser, base, lang, expected) {
  const context = await browser.newContext({ viewport: { width: 1300, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, { slots: [], extra, localStorageEntries: { 'mc-lang': lang } })
  logPageProblems(page)
  await page.goto(base + `/artifacts/${ARTIFACT.slug}`, { waitUntil: 'domcontentloaded' })
  await page.getByText(ARTIFACT.name).first().waitFor()
  for (const text of expected) await page.getByText(text).first().waitFor({ timeout: 15000 })
  const body = await page.locator('body').innerText()
  if (body.includes(ARTIFACT.created_at) || body.includes(ARTIFACT.updated_at)) {
    throw new Error(`${lang}: a raw ISO timestamp is still on the page`)
  }
  await page.screenshot({ path: `${OUT}/${PREFIX}-${lang}.png`, fullPage: true })
  console.log('wrote', `${OUT}/${PREFIX}-${lang}.png`)
  const timeline = page.locator('ul', { has: page.getByText('v1 → v3') }).first()
  await timeline.scrollIntoViewIfNeeded()
  await timeline.screenshot({ path: `${OUT}/${PREFIX}-${lang}-timeline.png` })
  console.log('wrote', `${OUT}/${PREFIX}-${lang}-timeline.png`)
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch()
  await frame(browser, base, 'en', ['by user', 'v1 → v3', /Created .*2026/])
  await frame(browser, base, 'de', ['von user', 'v1 → v3', /Erstellt am .*2026/])
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
