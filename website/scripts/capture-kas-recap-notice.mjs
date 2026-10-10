/**
 * Screenshot harness for the SESSION RECAP NOTICE and its Settings toggle.
 *
 * One transcript shape, shot dark + light:
 *
 *   tail      — the at-return placement, the recap's only surface: the resume
 *               prefetch appended the recap as the transcript tail before any
 *               new prompt (`_surface_resume_recap` in chat_runner.py). Asserts
 *               the notice text renders AND the follow-up OPTIONS of the turn
 *               before it still render — the shared systemNotice scan-skip is
 *               what a trailing status row must not break (the exact
 *               regression `SYSTEM_NOTICE_KINDS` guards).
 *
 * Plus the Settings -> Chat -> Sessions toggle row, dark + light, with the
 * setting on.
 *
 * Shots land in temp-screenshots/kas-recap/ (the sanctioned
 * PR-screenshot location). Nothing in CI runs this file; the CI-enforced
 * halves are the normalizer tests (test_kas_display_mapping.py), the chokepoint
 * tests (test_midless_broadcast_dedup.py), the prefetch tests
 * (test_eager_spawn.py), and the scan-skip vitest (deriveFollowUpOptions.test.ts).
 *
 * Usage: node scripts/capture-kas-recap-notice.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { KIROCREW_CONFIG_FIXTURE, logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/kas-recap'
const SLOT = 'chat-recap'
const PROJECT = '/home/user/workspace/notes'

mkdirSync(OUT, { recursive: true })

const OPTIONS = ['Fix the flaky test first', 'Ship the migration']
const RECAP_LABEL = 'Where you left off:'
const RECAP_TEXT =
  `${RECAP_LABEL} Migrating the export pipeline to batch writes; the retry test is red. Next: pin the backoff clock.`

// The sidebar preview the real slot projection shows, never the recap row: it
// skips system-notice rows, strips markdown and cuts to 80 chars.
const SIDEBAR_PREVIEW = 'Batch writer is in and wired; the retry test is red on a timing assumption.'

const RECAP_ROW = {
  // The row _append_recap_notice persists: assistant role, backend-built
  // content, kind tagged in meta (survives history reload).
  role: 'assistant',
  ts: Date.now() / 1000 - 60,
  content: RECAP_TEXT,
  meta: { kind: 'recap', mid: 'recap-mid-1' },
}

const PRIOR_TURN = [
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
]

// The resume prefetch's placement: the recap row is the transcript tail.
const TRANSCRIPT = [...PRIOR_TURN, RECAP_ROW]

const SLOTS = [{
  key: SLOT,
  title: 'Export pipeline migration',
  running: false,
  last_message: SIDEBAR_PREVIEW,
  messages: TRANSCRIPT.length, // sidebar count only; the transcript comes from the slot detail stub
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const DETAIL = {
  running: false,
  has_more: false,
  total: TRANSCRIPT.length,
  queue: [],
  project: PROJECT,
  messages: TRANSCRIPT,
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
    slots: SLOTS,
    extra: async (path, route) => {
      if (path.startsWith('/api/chat/slots/')) {
        await json(route, DETAIL)
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
  const tag = `tail/${theme}`
  expect(body.includes(`${RECAP_LABEL} Migrating the export pipeline`), `${tag}: recap notice must render in the transcript`)
  for (const opt of OPTIONS) {
    expect(body.includes(opt), `${tag}: follow-up option "${opt}" must survive the trailing recap notice`)
  }
  await page.screenshot({ path: `${OUT}/recap-notice-${theme}.png` })
  await page.close()
}

async function shootToggle(browser, base, theme) {
  const page = await browser.newPage({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
  logPageProblems(page)
  const cfg = {
    ...KIROCREW_CONFIG_FIXTURE,
    agent: { ...KIROCREW_CONFIG_FIXTURE.agent, session_recap: true },
    session_summary: { enabled: false },
  }
  await stubDashboardApi(page, {
    theme,
    slots: [],
    extra: async (path, route) => {
      if (path === '/api/config/kirocrew') {
        await json(route, cfg)
        return true
      }
      return false
    },
  })
  await page.goto(`${base}/settings/chat/sessions?highlight=key%3Aagent.session_recap`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(1800)
  const row = page.locator('[data-setting-key="agent.session_recap"]')
  const tag = `toggle/${theme}`
  expect(await row.count() === 1, `${tag}: the Session recap row must render once`)
  if (await row.count() === 1) {
    const text = await row.innerText()
    expect(text.includes('Session recap'), `${tag}: row label`)
    expect(text.includes('Shows where a chat left off when you reopen it. KAS harness only, one more model call per turn.'), `${tag}: row purpose and cost line`)
    expect(await row.getByRole('switch').getAttribute('aria-checked') === 'true', `${tag}: toggle reads the saved value`)
    await row.scrollIntoViewIfNeeded()
  }
  await page.screenshot({ path: `${OUT}/recap-toggle-${theme}.png` })
  await page.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    await shoot(browser, base, 'dark')
    await shoot(browser, base, 'light')
    await shootToggle(browser, base, 'dark')
    await shootToggle(browser, base, 'light')
  } finally {
    await browser.close()
    srv.close()
  }
  if (failures.length) {
    console.error('FAILURES:')
    for (const f of failures) console.error(' -', f)
    process.exit(1)
  }
  console.log('OK — 4 screenshots in', OUT)
}

main().catch((e) => {
  console.error(e)
  process.exit(1)
})
