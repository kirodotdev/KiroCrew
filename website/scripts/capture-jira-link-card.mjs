/**
 * Asserting screenshot harness for the sole-link Jira card (#2582).
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server with
 * every /api/** call answered from fixtures, so the assistant bubble goes
 * through the actual remark/rehype pipeline and the real MdParagraph /
 * MdAnchor split — no mock renderer.
 *
 * The seeded assistant message carries both forms on purpose, so one frame
 * answers both halves of the fix:
 *
 *   1. A Jira issue URL that is a paragraph's ONLY content — must render as a
 *      block LinkCard (Jira mark, issue key, instance host, copy button).
 *   2. The same URL inside a sentence — must keep today's inline chip.
 *
 * The harness ASSERTS rather than merely captures:
 *   - exactly one card (the card anchor carries `data-unfurl-url`; the Jira
 *     chip does not),
 *   - the card shows the issue key and the instance host,
 *   - two Jira provider marks total (card + chip),
 *   - ZERO /api/link-meta requests — the card is synchronous by design.
 *
 * Usage: node scripts/capture-jira-link-card.mjs [outDir] [label]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/jira-link-card'
const LABEL = process.argv[3] || 'after'
const SLOT = 'chat-jira-link-card'
const PROJECT = '/home/user/workspace/KiroCrew'
const JIRA_URL = 'https://acme.atlassian.net/browse/PROJ-123'
// The inline line uses a DIFFERENT issue so the no-fetch assertion can be made
// per-URL: MdAnchor's pre-existing (discarded) unfurl probe for inline links
// fires regardless of this change, but the sole-link CARD path must never
// fetch — that is the guarantee #2582 adds.
const JIRA_URL_INLINE = 'https://acme.atlassian.net/browse/PROJ-77'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Jira link card',
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

// Sole-paragraph URL first (the card), then the same URL mid-sentence (the chip).
const CONTENT = [
  'The tracking issue for this work:',
  '',
  JIRA_URL,
  '',
  `Progress is logged in ${JIRA_URL_INLINE} as sub-tasks close.`,
].join('\n')

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 30, content: 'Where is the Jira issue for this?' },
    { role: 'assistant', ts: Date.now() / 1000 - 10, content: CONTENT },
  ],
}

/** Assertion helper: the harness must FAIL loudly, not write a lying frame. */
function check(cond, msg) {
  if (!cond) throw new Error(`ASSERTION FAILED: ${msg}`)
  console.log('ok:', msg)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1100, height: 700 },
    deviceScaleFactor: 1,
  })
  const page = await context.newPage()

  const fixedApi = makeFixedApi(PROJECT)
  const linkMetaRequests = []
  let theme = 'dark'
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/link-meta') {
      linkMetaRequests.push(route.request().url())
      return json(route, {}, 502)
    }
    // The card is gated on the link_previews opt-in, exactly like the fetched card.
    if (path === '/api/dashboard/config') return json(route, { link_previews: true })
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    return handleBootRoute(route, path, { project: PROJECT, theme, fixedApi })
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))

  await page.addInitScript(slot => {
    localStorage.clear()
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot', slot)
  }, SLOT)

  for (const t of ['dark', 'light']) {
    theme = t
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)

    const bubble = page.locator('[data-role="assistant"] .msg-content').first()
    await bubble.waitFor({ state: 'visible', timeout: 15000 })

    // --- Assertions (dark pass, "after" label only: a "before" run against a
    // dist built from origin/main photographs the defect, which cannot pass) ---
    if (t === 'dark' && LABEL === 'after') {
      const cards = bubble.locator(`a[data-unfurl-url="${JIRA_URL}"]`)
      check(await cards.count() === 1, 'exactly one card anchor (data-unfurl-url) for the sole-link paragraph')
      check((await cards.first().textContent() || '').includes('PROJ-123'), 'card shows the issue key')
      const host = new URL(JIRA_URL).host
      check(await cards.first().locator(`span:text-is("${host}")`).count() === 1, 'card shows the instance host')
      const marks = bubble.locator('[data-testid="jira-provider-mark"]')
      check(await marks.count() === 2, 'two Jira provider marks: one card + one inline chip')
      const copyBtns = bubble.locator('button[aria-label*="PROJ-123"]')
      check(await copyBtns.count() === 1, 'card carries a copy-URL button')
      // Exact comparison of the fetched target, not a substring scan: the
      // request carries the URL as a query param, so parse it back out.
      const cardFetches = linkMetaRequests.filter(u => {
        try { return new URL(u).searchParams.get('url') === JIRA_URL } catch { return false }
      })
      check(cardFetches.length === 0, `no /api/link-meta request for the sole-link card URL (saw ${cardFetches.length})`)
    }

    const path = `${OUT}/${LABEL}-${t}.png`
    await bubble.screenshot({ path })
    console.log('wrote', path)
  }

  await context.close()
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
