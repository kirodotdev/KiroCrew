/**
 * Screenshot harness + behavior check for the `[OPTION-ACTIONS:]` marker's TRANSCRIPT
 * rendering.
 *
 * The visible delta of this PR lives in what the transcript STOPS showing. Before it,
 * `stripOptionMarkers` keyed on the content head alone (`"[OPTION-ACTIONS:"` does not
 * start with `"[OPTIONS:"` — they diverge at `S` vs `-`), and `parseOptions` early-returned
 * raw content whenever no CONTENT marker matched. So an action-only hand-back rendered its
 * own marker as literal prose, while `searchableText` already excluded that span: text on
 * screen that in-chat search could not find. A same-line pair was worse — only the trailing
 * marker could reach the end anchor, so `[OPTIONS: …]` beside an action marker leaked its
 * raw text AND dropped its pills, leaving the destructive affordance as the one that
 * survived.
 *
 * This asserts as well as photographs, against the REAL built SPA (website/dist): each
 * scene seeds one assistant row and exits non-zero unless the marker text is gone (or, for
 * the control, still present). Nothing in CI runs this file — the CI-enforced half is
 * src/test/optionActions.test.ts and src/test/AssistantMessage.test.tsx.
 *
 * Usage: node scripts/capture-option-actions-marker.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/option-actions-marker'
const SLOT = 'chat-option-actions'
const PROJECT = '/home/user/workspace/notes'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Release checklist',
  running: false,
  last_message: 'All set.',
  messages: 2,
  agent: 'default',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const now = Date.now() / 1000

/** One scene: a two-row transcript whose assistant reply is `reply`. */
const detailFor = (reply) => ({
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: now - 600, content: 'Anything else outstanding?' },
    { role: 'assistant', ts: now - 590, content: reply },
  ],
})

async function scene(context, base, reply) {
  const extra = async (path, route) => {
    if (path.startsWith('/api/chat/slots/')) { await json(route, detailFor(reply)); return true }
    if (path === '/api/slash-commands') { await json(route, []); return true }
    return false
  }
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots, theme: 'dark', extra })
  await page.addInitScript((slot) => localStorage.setItem('mc-active-slot', slot), SLOT)
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  return page
}

async function main() {
  const { srv, base } = await serveDist()
  // `LD_LIBRARY_PATH` is cleared for the browser child only. A mise-managed node puts its
  // own `lib/node` on that path, and Chromium's GL stack then resolves the system
  // `libgallium`/`libLLVM` against node's older `libstdc++` and dies on a missing
  // `GLIBCXX_3.4.29` before the first page. Same class of portability fix as
  // `chromiumExecutable()` above: the harness stays runnable, nothing about the subject changes.
  const browser = await chromium.launch({
    executablePath: chromiumExecutable(),
    env: { ...process.env, LD_LIBRARY_PATH: '' },
  })
  const context = await browser.newContext({
    viewport: { width: 1400, height: 950 },
    deviceScaleFactor: 2,
    locale: 'en-US',
  })

  const failures = []

  /* ── Scene 1: action-only hand-back — the marker no longer renders as prose ── */
  let page = await scene(context, base, 'All set. Nothing further from me.\n[OPTION-ACTIONS: close=Nothing else, close this session]')
  if (await page.getByText('OPTION-ACTIONS').count() !== 0) {
    failures.push('scene 1: the action marker still renders as literal text')
  }
  if (await page.getByText('All set. Nothing further from me.').count() !== 1) {
    failures.push('scene 1: the prose the marker sat behind is missing')
  }
  await page.screenshot({ path: `${OUT}/1-action-only-marker-stripped.png` })
  console.log('wrote', `${OUT}/1-action-only-marker-stripped.png`)
  await page.close()

  /* ── Scene 2: same-line pair — the content marker's pills appear, neither leaks ── */
  page = await scene(context, base, 'Two ways to finish. [OPTIONS: Ship it | Hold for review] [OPTION-ACTIONS: close=Nothing else]')
  if (await page.getByText('OPTION-ACTIONS').count() !== 0) failures.push('scene 2: the action marker leaked as text')
  if (await page.getByText('[OPTIONS:').count() !== 0) failures.push('scene 2: the content marker leaked as text')
  for (const label of ['Ship it', 'Hold for review']) {
    if (await page.getByRole('button', { name: label, exact: true }).count() < 1) {
      failures.push(`scene 2: no follow-up pill for ${label}`)
    }
  }
  await page.screenshot({ path: `${OUT}/2-same-line-pair-both-recognised.png` })
  console.log('wrote', `${OUT}/2-same-line-pair-both-recognised.png`)
  await page.close()

  /* ── Scene 3: CONTROL — prose ABOUT the syntax is untouched, and offers nothing ── */
  page = await scene(context, base, 'Write [OPTION-ACTIONS: close=X] on its own line, and the row offers a close chip.')
  if (await page.getByText('[OPTION-ACTIONS: close=X]').count() !== 1) {
    failures.push('scene 3: prose discussing the marker was stripped — the strip is indiscriminate')
  }
  if (await page.getByRole('button', { name: 'X', exact: true }).count() !== 0) {
    failures.push('scene 3: prose discussing the marker produced a live affordance')
  }
  await page.screenshot({ path: `${OUT}/3-control-prose-about-syntax-intact.png` })
  console.log('wrote', `${OUT}/3-control-prose-about-syntax-intact.png`)
  await page.close()

  await browser.close()
  srv.close()

  if (failures.length) {
    for (const f of failures) console.error('FAIL:', f)
    process.exit(1)
  }
  console.log('PASS: both marker kinds leave the rendered prose, and prose discussing them does not')
}

main().catch(err => { console.error(err); process.exit(1) })
