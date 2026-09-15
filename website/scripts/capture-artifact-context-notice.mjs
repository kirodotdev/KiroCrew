/**
 * Screenshot harness for the artifact companion-chat CONTEXT-FAILURE notice.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server and
 * answers every /api/** call from fixtures via Playwright route interception
 * (gateway-free -- no kiro-cli, no live backend).
 *
 * Opening the companion chat silently POSTs /api/chat/slots/<slot>/context so the
 * agent knows which artifact it is looking at. That POST used to report its failure
 * through the page's SAVE-error channel, so a background enqueue failure told a user
 * with a dirty draft that their WORK had not been written -- and the 429 the queue
 * now answers with was rewritten by `friendlyErrText` into the tunnel rate-limit
 * string, naming the wrong cause and promising a retry nothing performs. The notice
 * is now its own surface, titled for what actually happened, with copy that states
 * the consequence and the remedy instead of the transport text.
 *
 * Frames:
 *   01-generic     the enqueue failed outright -- generic consequence + remedy
 *   02-queue-full  429 context_not_queued -- same copy, so this pins the refusal PATH
 *
 * This ASSERTS as well as photographs: each scene exits non-zero unless the notice
 * rendered with its own title rather than the save-failure title, so a regression
 * that reroutes it back into the save channel fails the run instead of producing a
 * screenshot nobody re-reads.
 *
 * Usage: node scripts/capture-artifact-context-notice.mjs [outDir] [prefix] [distDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/artifact-context-notice'
const PREFIX = process.argv[3] || 'after'
const DIST = process.argv[4] || DEFAULT_DIST

mkdirSync(OUT, { recursive: true })

const SLOT = 'chat-bound-artifact'

const ARTIFACT = {
  slug: 'quarterly-report',
  name: 'Quarterly report',
  kind: 'widget',
  source: 'chat',
  session_title: 'Quarterly report',
  description: 'Fixture artifact for the context-notice capture',
  tags: [],
  version: 3,
  pinned: false,
  created_at: '2026-08-20T10:00:00.000000+00:00',
  // NEWER than the slot's last activity below, which is what makes the resumed
  // companion chat send the freshness nudge this notice reports on.
  updated_at: '2026-09-05T21:00:00.000000+00:00',
  content: '<div style="padding:24px;font:14px system-ui"><h2>Quarterly report</h2>'
    + '<p>A rendered artifact document.</p></div>',
}

const slots = [{
  key: SLOT,
  title: 'Quarterly report',
  running: false,
  last_message: 'Ready when you are.',
  messages: 4,
  agent: 'default',
  memory_mode: 'persistent',
  project: '',
  folder_id: '',
  artifact: ARTIFACT.slug,
  last_activity_ts: '2026-08-21T09:00:00.000000+00:00',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
}]

/** Which failure the context POST answers with: 'generic' or 'queue-full'. */
let contextFailure = 'generic'

const extra = async (path, route) => {
  if (path === '/api/artifacts') return json(route, { artifacts: [ARTIFACT] }), true
  if (path === '/api/artifact-folders') return json(route, { folders: [] }), true
  if (path === '/api/artifacts/session-docs') return json(route, { docs: [] }), true
  if (path === '/api/sandbox-doc') {
    return json(route, { url: '/sandbox-doc/spent/1700000000.mac' }), true
  }
  if (/\/api\/chat\/slots\/[^/]+\/context$/.test(path)) {
    if (contextFailure === 'queue-full') {
      return json(route, { error: 'context queue is full', code: 'context_not_queued' }, 429), true
    }
    return json(route, { error: 'upstream write failed' }, 500), true
  }
  if (path === '/api/chat/slots' && route.request().method() === 'POST') {
    // The unbound scene creates its session on the sparkle click; the shared stub answers
    // this path with the LIST, which carries no `key` for the optimistic bind to read.
    return json(route, { key: SLOT, title: ARTIFACT.name, artifact: ARTIFACT.slug }), true
  }
  if (/\/api\/chat\/slots\/[^/]+$/.test(path)) {
    return json(route, { key: SLOT, messages: [], artifact: ARTIFACT.slug }), true
  }

  const m = /^\/api\/artifacts\/([^/]+)(\/.*)?$/.exec(path)
  if (!m) return false
  const rest = m[2] || ''
  if (rest === '/versions') return json(route, { slug: ARTIFACT.slug, versions: [1, 2, 3] }), true
  if (rest === '/events') return json(route, { slug: ARTIFACT.slug, events: [] }), true
  if (rest === '/comments') return json(route, { comments: [] }), true
  if (rest === '/upstream-status') return json(route, {}), true
  if (rest === '') return json(route, ARTIFACT), true
  return false
}

/**
 * The notice must be its OWN surface. Asserting on the save-failure title being
 * absent is what discriminates: routing this back through `setSaveError` would still
 * render an alert, still screenshot, and still look fine.
 *
 * The TITLE is scene-specific. This fixture's slot is already bound and its artifact is
 * newer than the slot's last activity, so every scene here takes the resume freshness
 * nudge, where an earlier injection already succeeded and only the latest version is
 * missing. Asserting the first-injection title would pass only if that scoping regressed.
 */
async function assertOwnNotice(page, scene, expectedTitle, forbiddenTitle) {
  const body = await page.locator('body').innerText()
  if (!body.includes(expectedTitle)) {
    throw new Error(`${scene}: the context notice did not render "${expectedTitle}"`)
  }
  if (forbiddenTitle && body.includes(forbiddenTitle)) {
    throw new Error(`${scene}: rendered "${forbiddenTitle}" on the refresh path`)
  }
  if (body.includes('Save failed')) {
    throw new Error(`${scene}: rendered through the SAVE-error channel`)
  }
  if (/Failed to fetch|upstream write failed|rate limit/i.test(body)) {
    throw new Error(`${scene}: leaked transport text into the notice copy`)
  }
}

const REFRESH_TITLE = "Couldn't share the latest version with the agent"
const FIRST_TITLE = "Couldn't share this artifact with the agent"

async function scene(
  page, base, name, expected, title = REFRESH_TITLE, forbidden = FIRST_TITLE, capture = true,
) {
  await page.goto(base + `/artifacts/${ARTIFACT.slug}`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2000)
  await page.getByLabel('Toggle agent chat').click()
  await page.waitForTimeout(2500)
  await assertOwnNotice(page, name, title, forbidden)
  const body = await page.locator('body').innerText()
  if (!body.includes(expected)) {
    throw new Error(`${name}: expected copy not found -- ${expected}`)
  }
  // ASSERTED BUT NOT WRITTEN when capture is false: a variant that renders pixel-identical to a
  // sibling still needs its assertion, but a second copy of the same frame documents nothing.
  if (!capture) {
    console.log('asserted (no frame)', name)
    return
  }
  await page.screenshot({
    path: `${OUT}/${PREFIX}-${name}.png`,
    clip: { x: 0, y: 0, width: 1500, height: 820 },
  })
  console.log('wrote', `${OUT}/${PREFIX}-${name}.png`)
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 1100 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await stubDashboardApi(page, { slots, extra })
  logPageProblems(page)

  await page.route('**/sandbox-doc/**', route => route.fulfill({
    status: 200,
    contentType: 'text/html; charset=utf-8',
    body: '<!doctype html><html><body style="font:14px system-ui;padding:24px;color:#444">'
      + '<h3>Quarterly report</h3><p>A rendered artifact document.</p></body></html>',
  }))

  contextFailure = 'generic'
  await scene(page, base, '01-generic', 'Mention the artifact in your next message')

  // A 429 capacity refusal and an outright failure render the SAME body: the notice states
  // the one remedy that applies either way, so this scene pins the 429 PATH, not distinct copy.
  contextFailure = 'queue-full'
  await scene(page, base, '02-queue-full', 'Mention the artifact in your next message')

  // UX: the FIRST-injection title had no frame at all. Emptying the slot list in place --
  // the stub closes over this array -- leaves no bound slot, so the sparkle click CREATES
  // the session and injects immediately, which is the only path that renders that variant.
  slots.length = 0
  contextFailure = 'generic'
  await scene(
    page, base, '03-first-injection',
    'Mention the artifact in your next message', FIRST_TITLE, REFRESH_TITLE,
  )

  // The first-injection title paired with the 429 path renders PIXEL-IDENTICAL to 03 (the notice
  // copy does not vary by cause), so it is asserted without committing a duplicate frame.
  contextFailure = 'queue-full'
  await scene(
    page, base, '04-first-injection-queue-full',
    'Mention the artifact in your next message', FIRST_TITLE, REFRESH_TITLE, false,
  )

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
