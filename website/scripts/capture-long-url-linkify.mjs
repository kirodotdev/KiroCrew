/**
 * Screenshot harness for the long-URL linkify fix (#5729).
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server with
 * every /api/** call answered from fixtures, so the assistant bubble goes
 * through the actual remark/rehype pipeline — no gateway, no auth, no mock
 * renderer. The bug is a markdown TOKENIZATION artefact (CommonMark refuses a
 * raw space in an unbracketed link destination), so only the real pipeline can
 * photograph it.
 *
 * The seeded message carries three lines on purpose, because one frame has to
 * answer both "does it fix the bug" and "does it leave working URLs alone":
 *
 *   1. The reported shape: a `[text](url)` whose pre-filled destination
 *      carries raw spaces. Pre-fix the span fails to parse — the label renders
 *      as literal `[file the issue](` prose and only the URL head autolinks.
 *      Post-fix it is one anchor whose href carries every query param.
 *   2. A properly-encoded long URL (>200 chars, many `&`-separated params) as
 *      a bare literal. This works today and the frame must show it unchanged.
 *   3. A bare file path, which must stay plain text in both frames — the fix
 *      must not loosen the path-vs-URL distinction.
 *
 * Run once against a dist built from this branch (label "after") and once
 * against a dist built with origin/main's MarkdownRenderer.tsx (label
 * "before").
 *
 * Usage: node scripts/capture-long-url-linkify.mjs <outDir> [label]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/chat-long-url-linkify'
const LABEL = process.argv[3] || 'after'
const SLOT = 'chat-long-url-linkify'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Long URL linkify',
  running: false,
  last_message: 'link ready',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const PREFILL_SPACED =
  'https://github.com/kirodotdev/KiroCrew/issues/new?title=Bug: dashboard chat fails&labels=bug, area: dashboard&assignees=someone'
const LONG_ENCODED =
  'https://github.com/kirodotdev/KiroCrew/issues/new?title=Bug%3A+dashboard+chat+fails&body=%23%23+What%0A%0AURLs+are+not+clickable%0A%0A%23%23+Why%0A%0AThe+agent+generated+a+link&labels=bug%2Carea%3A+dashboard&assignees=someone&template=bug_report.md&milestone=v2.0'

// The reported shape first, then the working control cases.
const CONTENT = [
  `I drafted the issue — [file the issue](${PREFILL_SPACED}) when ready.`,
  '',
  `Prefill link (encoded): ${LONG_ENCODED}`,
  '',
  'The renderer lives at /home/user/workspace/KiroCrew/website/src/components/MarkdownRenderer.tsx — a path, not a link.',
].join('\n')

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    {
      role: 'user',
      ts: Date.now() / 1000 - 30,
      content: 'Draft a pre-filled GitHub issue link for the linkify bug.',
    },
    {
      role: 'assistant',
      ts: Date.now() / 1000 - 10,
      content: CONTENT,
    },
  ],
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  // deviceScaleFactor 1 and a modest viewport keep every frame well under the
  // 2000px cap that wedges an agent session on read.
  const context = await browser.newContext({
    viewport: { width: 1100, height: 700 },
    deviceScaleFactor: 1,
  })
  const page = await context.newPage()

  const fixedApi = makeFixedApi(PROJECT)
  // The app reads its theme from the config API, which OUTRANKS the localStorage
  // seed — hardcode it here and both "themes" come out as the same dark frame.
  let theme = 'dark'
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    return handleBootRoute(route, path, { project: PROJECT, theme, fixedApi })
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => {
    if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300))
  })

  await page.addInitScript(slot => {
    localStorage.clear()
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot', slot)
  }, SLOT)

  for (const t of ['dark', 'light']) {
    theme = t
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)

    // Photograph the assistant bubble, not the whole shell: the delta is a
    // truncated-vs-whole anchor and a full-window frame buries it in chrome.
    const bubble = page.locator('[data-role="assistant"] .msg-content').first()
    await bubble.waitFor({ state: 'visible', timeout: 15000 })
    const path = `${OUT}/${LABEL}-${t}.png`
    await bubble.screenshot({ path })
    console.log('wrote', path)
  }

  await context.close()
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
