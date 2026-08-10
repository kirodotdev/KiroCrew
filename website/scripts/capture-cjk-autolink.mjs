/**
 * Screenshot harness for the CJK autolink-boundary fix.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server with
 * every /api/** call answered from fixtures, so the assistant bubble goes
 * through the actual remark/rehype pipeline — no gateway, no auth, no mock
 * renderer. The bug is a markdown TOKENIZATION artefact, so only the real
 * pipeline can photograph it.
 *
 * The seeded message carries three lines on purpose, because one frame has to
 * answer both "does it fix the bug" and "does it leave real URLs alone":
 *
 *   1. The reported line. Pre-fix, the bare URL swallows `，`96ed647b`）：` and
 *      eats the OPENING backtick of the code span, so every later backtick
 *      pairing in the line shifts: prose renders as inline code and the real
 *      code renders with literal backticks.
 *   2. The bracket rule. A URL inside `（…）` — pre-fix the closing `）` lands
 *      inside the href, post-fix it stays in the prose.
 *   3. A real article URL whose path legitimately CONTAINS `（公司）`, terminated
 *      by a space so GFM's own run ends there. Nothing in the prose opened that
 *      bracket, so there is no evidence to cut on and the href must come out
 *      identical in both frames.
 *
 * The space in line 3 is load-bearing for the frame, not a typo. Without it GFM
 * runs to end-of-line and swallows the whole sentence, which this pass also
 * leaves alone (no evidence) — a real limitation, but it would read as "the fix
 * did nothing" in a screenshot instead of "a real URL survived".
 *
 * Not pictured, and deliberately so: `…/wiki/モーニング娘。`紹介``. A sentence-ender
 * directly before a backtick is left alone (a real title can end in `。`), so
 * that line renders the same pre- and post-fix — the trade is stated in the PR
 * body and locked by unit tests rather than photographed.
 *
 * Run once against a dist built from this branch (label "after") and once
 * against a dist built with origin/main's MarkdownRenderer.tsx (label "before").
 *
 * Usage: node scripts/capture-cjk-autolink.mjs <outDir> [label]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/cjk-autolink-boundary'
const LABEL = process.argv[3] || 'after'
const SLOT = 'chat-cjk-autolink'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'CJK punctuation after a bare URL',
  running: false,
  last_message: 'readiness: passed',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

// The reported transcript line verbatim, then the bracket rule, then a real
// article URL that must survive untouched.
const CONTENT = [
  '**#2137 — review-ready**（https://github.com/kirodotdev/KiroCrew/pull/2137，`96ed647b`）：`readiness: passed`',
  '',
  '（详见 https://github.com/kirodotdev/KiroCrew/pull/2137）后面还有正文。',
  '',
  '条目地址 https://zh.wikipedia.org/wiki/苹果（公司） — 括号属于路径，必须保持完整。',
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
      content: '#2137 现在什么状态？',
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

    // Photograph the assistant bubble, not the whole shell: the delta is a few
    // characters wide and a full-window frame buries it in chrome (and in a
    // "gateway offline" composer that has nothing to do with the change).
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
