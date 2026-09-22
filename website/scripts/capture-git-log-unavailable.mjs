/**
 * Screenshot harness for the Git panel's log-route outage state.
 *
 * Runs the REAL built SPA (website/dist) with /api/** answered from fixtures:
 * the status route answers a clean repository while the log route answers
 * `503 {code: "git_log_unavailable"}` -- the response the log route gives when
 * repository discovery fails for any reason other than Git's own
 * not-a-repository verdict (sandbox refusal, spawn failure, timeout, corrupt
 * metadata). `dashboard.auto_open_git_panel` opens the panel without a click.
 *
 * Two frames:
 *   log-unavailable-dark / -light   clean pill + ONE untitled log notice,
 *                                   no "No changes or commits to display."
 *
 * Every frame asserts no unexpected dialog is open before the shot: first-run
 * gates sit on top of every frame while DOM assertions still pass behind them.
 *
 * Usage: node scripts/capture-git-log-unavailable.mjs <outDir>
 */
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'
import { json } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/git-log-unavailable-10892'
const SLOT = 'chat-git-log-unavailable'
const PROJECT = '/home/user/workspace/demo-service'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Git panel log outage',
  running: false,
  last_message: 'A failed history read no longer reads as an empty history.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 1,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 60, content: 'The Git panel shows no commits but this repo has history.' },
    { role: 'assistant', ts: Date.now() / 1000 - 30, content: 'The log read failed, and the panel reported that failure as an empty commit list. It now says the history could not be read.' },
  ],
}

const h = await openTranscriptHarness({
  slot: SLOT,
  project: PROJECT,
  slots,
  detail,
  viewport: { width: 1280, height: 800 },
  deviceScaleFactor: 1,
})

const dashCfg = { auto_open_git_panel: true }
await h.page.route(/\/api\/dashboard\/config$/, async route => json(route, dashCfg))

await h.page.route(/\/api\/project\/git/, async route => {
  const path = new URL(route.request().url()).pathname
  if (path === '/api/project/git/status') {
    return json(route, { repo: true, repoRoot: PROJECT, branch: 'mainline', files: [] })
  }
  if (path === '/api/project/git/log') {
    // Kept in step with the backend's real sentence; the panel keys on `code`
    // and renders localized copy, so the string itself is never on screen.
    return json(
      route,
      { error: "Couldn't read the commit history.", code: 'git_log_unavailable' },
      503,
    )
  }
  return json(route, { repo: true, repoRoot: PROJECT, branch: 'mainline' })
})

async function shoot(theme) {
  await h.load(theme, { selector: 'textarea', settle: 1500 })
  await h.page.waitForSelector('[data-testid="git-panel-log-error"]', { timeout: 20000 })
  await h.page.waitForTimeout(600)
  const dialogs = await h.page.locator('[role="dialog"]').count()
  if (dialogs !== 0) throw new Error(`unexpected dialog open before shot (${dialogs})`)
  const facts = {
    // Untitled on purpose, like the status outage: `titles` must read 0.
    titles: await h.page.locator('[data-testid="git-panel-log-error"] strong').count(),
    message: (await h.page.locator('[data-testid="git-panel-log-error"]').first().innerText())?.trim(),
    statusNotices: await h.page.locator('[data-testid="git-panel-status-error"]').count(),
    refusalNotices: await h.page.locator('[data-testid="git-panel-filter-refused"]').count(),
    cleanPill: await h.page.locator('text=clean').count(),
    emptyState: await h.page.locator('text=No changes or commits to display.').count(),
    refreshEnabled: await h.page.getByRole('button', { name: 'Refresh' }).isEnabled(),
  }
  console.log(theme.toUpperCase(), JSON.stringify(facts))
  if (facts.titles !== 0 || facts.emptyState !== 0 || facts.cleanPill < 1 || !facts.refreshEnabled) {
    throw new Error(`frame does not show the log-unavailable state: ${JSON.stringify(facts)}`)
  }
  await h.page.screenshot({ path: join(OUT, `git-panel-log-unavailable-${theme}.png`) })
  console.log('SHOT', `git-panel-log-unavailable-${theme}`)
}

await shoot('dark')
await shoot('light')

await h.close()
console.log('DONE', OUT)
