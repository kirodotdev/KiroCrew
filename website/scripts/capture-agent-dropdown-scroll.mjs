/**
 * Screenshot harness for the agent picker's scroll behavior (#6375).
 *
 * Seeds enough agents that the list overflows its host's `max-h-[280px]`
 * listbox, opens the picker, and photographs it mid-scroll. Before the fix,
 * AgentDropdownList carried its own `overflow-y-auto max-h-[300px]`, so the
 * pop-up showed two nested scrollbars; after, the host listbox is the single
 * scroll owner and exactly one scrollbar appears.
 *
 * The script measures the scrollable-ancestor count of the option rows and
 * FAILS (exit 1) when it differs from the expected count, so a regressed
 * build cannot yield a citable "passing" PNG. It shoots whatever is in
 * `website/dist` — to reproduce the "before" comparison shot, build main
 * (or revert the AgentDropdownList change), rebuild, and run with
 * EXPECT_SCROLL_OWNERS=2.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server
 * and answers every /api/** call from fixtures through `stubDashboardApi`.
 *
 * Usage: [EXPECT_SCROLL_OWNERS=n] node scripts/capture-agent-dropdown-scroll.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/agent-dropdown-scroll-shots'
const EXPECT_OWNERS = Number(process.env.EXPECT_SCROLL_OWNERS || '1')

mkdirSync(OUT, { recursive: true })

const SLOT = 'chat-1'
// Enough rows that the list overflows both the host's 280px listbox and the
// component's former internal 300px cap, so nested scrollers both engage.
const AGENTS = Array.from({ length: 24 }, (_, i) => ({
  name: `crew-agent-${String(i + 1).padStart(2, '0')}`,
  source: i % 3 === 0 ? 'builtin' : 'package',
  description: `Specialist crew agent number ${i + 1} for scroll-evidence purposes`,
}))

const { srv, base } = await serveDist()
const browser = await chromium.launch()

try {
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 1 })
  const page = await context.newPage()
  logPageProblems(page)

  const extra = async (path, route) => {
    if (path === '/api/agents') {
      await json(route, { agents: AGENTS, default_agent: AGENTS[0].name })
      return true
    }
    return false
  }

  await stubDashboardApi(page, {
    slots: [{ key: SLOT, messages: 0, running: false, agent: AGENTS[0].name, mode: '' }],
    extra,
  })
  await page.addInitScript(slot => {
    localStorage.setItem('mc-active-slot', slot)
    localStorage.setItem('mc-lang', 'en')
  }, SLOT)
  // Classic (non-overlay) scrollbars so every scroll container is visible in the shot.
  await page.addInitScript(() => {
    const style = document.createElement('style')
    style.textContent = '::-webkit-scrollbar{width:12px}::-webkit-scrollbar-thumb{background:#888;border-radius:6px}::-webkit-scrollbar-track{background:#ddd}'
    document.addEventListener('DOMContentLoaded', () => document.head.appendChild(style))
  })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)

  await page.getByRole('button', { name: /^Agent: / }).first().click()
  const picker = page.getByRole('dialog', { name: 'Agent selector' })
  await picker.waitFor({ state: 'visible', timeout: 5000 })

  // Scroll the host listbox part-way so a scrollbar thumb (or two, before the
  // fix) is engaged and visible mid-track.
  await picker.getByRole('listbox').evaluate(el => { el.scrollTop = 60 })
  // Before the fix the INNER container is the taller scroller — nudge it too so
  // both thumbs sit mid-track in the "before" shot.
  await picker.getByRole('listbox').evaluate(el => {
    const inner = el.firstElementChild
    if (inner && inner.scrollHeight > inner.clientHeight) inner.scrollTop = 80
  })
  await page.waitForTimeout(300)

  // Count scroll containers among the option rows' ancestors inside the picker —
  // the measurable half of the evidence, asserted below so the shot can't lie.
  const scrollOwners = await picker.evaluate(root => {
    const option = root.querySelector('[role="option"]')
    let n = 0
    for (let el = option?.parentElement; el && root.contains(el); el = el.parentElement) {
      if (el.scrollHeight > el.clientHeight + 1 && /(auto|scroll)/.test(getComputedStyle(el).overflowY)) n++
    }
    return n
  })
  console.log('scrollable ancestors of the option rows:', scrollOwners)

  const out = join(OUT, 'picker.png')
  await picker.screenshot({ path: out })
  console.log('wrote', out)

  await context.close()

  if (scrollOwners !== EXPECT_OWNERS) {
    console.error(`FAIL: expected ${EXPECT_OWNERS} scroll owner(s), found ${scrollOwners}`)
    process.exitCode = 1
  }
} finally {
  await browser.close()
  srv.close()
}
