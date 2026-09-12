/**
 * Screenshot harness for the SESSION RECAP NOTICE.
 *
 * Seeds a transcript whose tail is a `meta.kind="recap"` assistant status row
 * (the shape `_append_recap_notice` persists: backend-composed "Recap: …"
 * text) following a real assistant turn that carries [OPTIONS:] choices, and
 * asserts BOTH halves of the feature in the REAL built SPA (website/dist):
 *
 *   1. the notice text renders in the transcript, and
 *   2. the follow-up OPTIONS of the turn BEFORE the notice still render —
 *      the shared systemNotice scan-skip is what a trailing status row must
 *      not break (the exact regression `SYSTEM_NOTICE_KINDS` guards).
 *
 * Dark + light shots land in temp-screenshots/kas-recap/ (the sanctioned
 * PR-screenshot location). Nothing in CI runs this file; the CI-enforced
 * halves are the mapping tests (test_kas_display_mapping.py), the chokepoint
 * tests (test_midless_broadcast_dedup.py), and the scan-skip vitest
 * (deriveFollowUpOptions.test.ts).
 *
 * Usage: node scripts/capture-kas-recap-notice.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/kas-recap'
const SLOT = 'chat-recap'
const PROJECT = '/home/user/workspace/notes'

mkdirSync(OUT, { recursive: true })

const OPTIONS = ['Fix the flaky test first', 'Ship the migration']
const RECAP_TEXT =
  '\u{1F9ED} Recap \u2014 session so far: Migrating the export pipeline to batch writes; the retry test is red. Next: pin the backoff clock.'

const slots = [{
  key: SLOT,
  title: 'Export pipeline migration',
  running: false,
  last_message: RECAP_TEXT,
  messages: 3,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 3,
  queue: [],
  project: PROJECT,
  messages: [
    {
      role: 'user',
      ts: Date.now() / 1000 - 7200,
      content: 'Migrate the export pipeline to batch writes.',
    },
    {
      role: 'assistant',
      ts: Date.now() / 1000 - 7000,
      content:
        'Batch writer is in and wired; the retry test is red on a timing '
        + 'assumption.\n\n'
        + `[OPTIONS: ${OPTIONS.join(' | ')}]`,
    },
    {
      // The row _append_recap_notice persists: assistant role, backend-built
      // "Recap: …" content, kind tagged in meta (survives history reload).
      role: 'assistant',
      ts: Date.now() / 1000 - 60,
      content: RECAP_TEXT,
      meta: { kind: 'recap', mid: 'recap-mid-1' },
    },
  ],
}

const failures = []
function expect(cond, label) {
  if (!cond) failures.push(label)
}

async function shoot(browser, base, theme) {
  const page = await browser.newPage({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme,
    slots,
    extra: async (path, route) => {
      if (path.startsWith('/api/chat/slots/')) {
        await json(route, detail)
        return true
      }
      return false
    },
  })
  await page.goto(`${base}/chat/${SLOT}`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(2200)
  // Chips mount after the transcript settles; make sure the tail is on-screen.
  await page.keyboard.press('End').catch(() => {})
  const body = await page.locator('body').innerText()
  expect(body.includes('Recap \u2014 session so far: Migrating the export pipeline'), `${theme}: recap notice must render in the transcript`)
  for (const opt of OPTIONS) {
    expect(body.includes(opt), `${theme}: follow-up option "${opt}" must survive the trailing recap notice`)
  }
  await page.screenshot({ path: `${OUT}/recap-notice-${theme}.png` })
  await page.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    await shoot(browser, base, 'dark')
    await shoot(browser, base, 'light')
  } finally {
    await browser.close()
    srv.close()
  }
  if (failures.length) {
    console.error('FAILURES:')
    for (const f of failures) console.error(' -', f)
    process.exit(1)
  }
  console.log('OK — 2 screenshots in', OUT)
}

main().catch((e) => {
  console.error(e)
  process.exit(1)
})
