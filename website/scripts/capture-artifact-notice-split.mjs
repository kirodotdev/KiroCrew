/**
 * Screenshot harness for the artifact-frame notice split (#6489).
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * and answers every /api/** call from fixtures via Playwright route interception
 * (gateway-free — no kiro-cli, no live backend).
 *
 * The frame notice used to render ONE copy ("Couldn't render this artifact" +
 * Retry) for two states: `failed` (the mint itself failed — the claim is
 * accurate) and `docSilent` (the frame loaded a document that never reported a
 * height — which is EITHER an engine renavigation 404ing the single-use doc URL
 * or the reader deliberately following a link inside the sandbox; the two are
 * indistinguishable from outside an opaque origin, so a failure claim is wrong
 * half the time and "Retry" destroys a page the reader chose to open). The fix
 * gives docSilent cause-neutral copy ("This artifact is no longer showing" +
 * "Show artifact"); `failed` keeps its copy.
 *
 * Frames:
 *   01-failed     mint rejected — centered failure notice, no document
 *   02-docsilent  document loaded then went silent — overlay notice strip
 *
 * The point of the change is the copy split, so run against the branch (after)
 * and against main (before) to see the delta.
 *
 * Usage: node scripts/capture-artifact-notice-split.mjs [outDir] [prefix] [distDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/artifact-notice-split'
const PREFIX = process.argv[3] || 'after'
const DIST = process.argv[4] || DEFAULT_DIST

mkdirSync(OUT, { recursive: true })

const ARTIFACT = {
  slug: 'quarterly-report',
  name: 'Quarterly report',
  kind: 'widget',
  source: 'chat',
  session_title: 'Artifact notice split',
  description: 'Fixture artifact for the notice-state capture',
  tags: [],
  version: 1,
  pinned: false,
  created_at: '2026-08-20T10:00:00.000000+00:00',
  updated_at: '2026-08-27T21:00:00.000000+00:00',
  content: '<div style="padding:24px;font:14px system-ui"><h2>Quarterly report</h2><p>A rendered artifact document.</p></div>',
}

/** When false, POST /api/sandbox-doc 500s so the mint itself fails. */
let mintSucceeds = true

const extra = async (path, route) => {
  if (path === '/api/artifacts') return json(route, { artifacts: [ARTIFACT] }), true
  if (path === '/api/artifact-folders') return json(route, { folders: [] }), true
  if (path === '/api/artifacts/session-docs') return json(route, { docs: [] }), true
  if (path === '/api/sandbox-doc') {
    if (!mintSucceeds) return json(route, { error: 'mint failed' }, 500), true
    return json(route, { url: '/sandbox-doc/spent/1700000000.mac' }), true
  }

  const m = /^\/api\/artifacts\/([^/]+)(\/.*)?$/.exec(path)
  if (!m) return false
  const rest = m[2] || ''
  if (rest === '/versions') return json(route, { slug: ARTIFACT.slug, versions: [1] }), true
  if (rest === '/events') return json(route, { slug: ARTIFACT.slug, events: [] }), true
  if (rest === '/comments') return json(route, { comments: [] }), true
  if (rest === '/upstream-status') return json(route, {}), true
  if (rest === '') return json(route, ARTIFACT), true
  return false
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 1100 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await stubDashboardApi(page, { extra })
  logPageProblems(page)

  // The minted document: a page WITHOUT the injected height reporter, standing
  // in for what the frame shows after it navigated away from our document (a
  // spent-url 404, or a page the reader opened by following a link). It loads
  // fine — `load` fires — and then never reports, which is the docSilent signal.
  await page.route('**/sandbox-doc/**', route => route.fulfill({
    status: 200,
    contentType: 'text/html; charset=utf-8',
    body: '<!doctype html><html><body style="font:14px system-ui;padding:24px;color:#444">'
      + '<h3>Some other page</h3><p>The frame navigated here — this document is not ours and never reports a height.</p>'
      + '</body></html>',
  }))

  // ── Frame 1: failed — the mint itself failed, failure claim is accurate ──
  mintSucceeds = false
  await page.goto(base + `/artifacts/${ARTIFACT.slug}`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  await page.screenshot({
    path: `${OUT}/${PREFIX}-01-failed.png`,
    clip: { x: 0, y: 0, width: 1500, height: 760 },
  })
  console.log('wrote', `${OUT}/${PREFIX}-01-failed.png`)

  // ── Frame 2: docSilent — document loaded, then silence past the grace window ──
  mintSucceeds = true
  await page.goto(base + `/artifacts/${ARTIFACT.slug}`, { waitUntil: 'domcontentloaded' })
  // DOC_REPORT_GRACE_MS is 3000ms after the frame's load event; wait past it.
  await page.waitForTimeout(5500)
  await page.screenshot({
    path: `${OUT}/${PREFIX}-02-docsilent.png`,
    clip: { x: 0, y: 0, width: 1500, height: 760 },
  })
  console.log('wrote', `${OUT}/${PREFIX}-02-docsilent.png`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
