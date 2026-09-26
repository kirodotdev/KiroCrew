/**
 * Screenshot harness for the SESSION RECAP NOTICE.
 *
 * Two transcript shapes, each shot dark + light:
 *
 *   tail   — the recap row is the transcript tail after an assistant turn
 *            that carries [OPTIONS:] choices (a resume with no new prompt
 *            yet). Asserts the notice text renders AND the follow-up
 *            OPTIONS of the turn before it still render — the shared
 *            systemNotice scan-skip is what a trailing status row must not
 *            break (the exact regression `SYSTEM_NOTICE_KINDS` guards).
 *   drain  — the drain-capture placement: the user's new prompt is already
 *            appended when the recap arrives, so the row renders AFTER that
 *            prompt and BEFORE the turn's first output (`_append_recap_notice`
 *            in chat_utils.py). Asserts that order in the rendered
 *            transcript, which is the ordering the "session so far" anchor
 *            exists for.
 *
 * Shots land in temp-screenshots/kas-recap/ (the sanctioned
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
  'Recap \u2014 session so far: Migrating the export pipeline to batch writes; the retry test is red. Next: pin the backoff clock.'

const slots = [{
  key: SLOT,
  title: 'Export pipeline migration',
  running: false,
  last_message: RECAP_TEXT,
  messages: 3, // sidebar count only; the transcript comes from the slot detail stub
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const RECAP_ROW = {
  // The row _append_recap_notice persists: assistant role, backend-built
  // "Recap: …" content, kind tagged in meta (survives history reload).
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

const NEW_PROMPT = 'Where did we leave the retry test?'
const FIRST_OUTPUT =
  'Red on the backoff clock: the test reads wall time where the writer uses '
  + 'the monotonic clock. Pinning the clock in the fixture next.'

const shapes = {
  // A resume with no new prompt yet: the recap row is the transcript tail.
  tail: [...PRIOR_TURN, RECAP_ROW],
  // The drain-capture path: the prompt row is already appended when the
  // recap arrives, so the row sits between the prompt and the first output.
  drain: [
    ...PRIOR_TURN,
    { role: 'user', ts: Date.now() / 1000 - 90, content: NEW_PROMPT },
    RECAP_ROW,
    { role: 'assistant', ts: Date.now() / 1000 - 30, content: FIRST_OUTPUT },
  ],
}

function detailFor(shape) {
  return {
    running: false,
    has_more: false,
    total: shapes[shape].length,
    queue: [],
    project: PROJECT,
    messages: shapes[shape],
  }
}

const failures = []
function expect(cond, label) {
  if (!cond) failures.push(label)
}

async function shoot(browser, base, theme, shape) {
  const page = await browser.newPage({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
  logPageProblems(page)
  const detail = detailFor(shape)
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
  const tag = `${shape}/${theme}`
  expect(body.includes('Recap \u2014 session so far: Migrating the export pipeline'), `${tag}: recap notice must render in the transcript`)
  if (shape === 'tail') {
    for (const opt of OPTIONS) {
      expect(body.includes(opt), `${tag}: follow-up option "${opt}" must survive the trailing recap notice`)
    }
  } else {
    // The sidebar's session preview repeats the recap text, so measure order
    // from the prompt row onward: that is the transcript.
    const promptAt = body.indexOf(NEW_PROMPT)
    const recapAt = promptAt < 0 ? -1 : body.indexOf('Recap \u2014 session so far', promptAt)
    const outputAt = recapAt < 0 ? -1 : body.indexOf('Red on the backoff clock', recapAt)
    expect(promptAt >= 0 && recapAt > promptAt, `${tag}: recap row must render AFTER the user's new prompt`)
    expect(outputAt > recapAt, `${tag}: recap row must render BEFORE the turn's first output`)
  }
  const suffix = shape === 'tail' ? '' : `-${shape}`
  await page.screenshot({ path: `${OUT}/recap-notice${suffix}-${theme}.png` })
  await page.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    for (const shape of Object.keys(shapes)) {
      await shoot(browser, base, 'dark', shape)
      await shoot(browser, base, 'light', shape)
    }
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
