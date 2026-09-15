/**
 * Screenshot harness for the composer footer's git-status badge.
 *
 * Runs the REAL built SPA (website/dist) with /api/** answered from fixtures.
 * The two git endpoints are fixtured to a working tree with 3 uncommitted
 * files, 1 commit ahead and 2 behind upstream, so the footer renders
 * `mainline · [3] ↑1 ↓2` — every segment of the badge in one frame.
 *
 * Usage: node scripts/capture-footer-git-status.mjs <outDir>
 */
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'
import { json } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/footer-git-status'
const SLOT = 'chat-git-footer'
const PROJECT = '/home/user/workspace/demo-service'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Show git status in the footer',
  running: false,
  last_message: 'Working tree summary now sits beside the branch.',
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
    { role: 'user', ts: Date.now() / 1000 - 60, content: 'Can the bottom bar show more detailed git status?' },
    { role: 'assistant', ts: Date.now() / 1000 - 30, content: 'Done — the footer now shows the working-tree summary beside the branch: uncommitted file count, plus commits ahead/behind upstream. The Git panel in the sidebar has the full per-file view.' },
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

// Registered after the harness's own catch-all, so (reverse registration
// order) these answer first for the git endpoints only.
// Mutable so the strip shots run with the side panel closed (full-width
// composer, branch label visible) and only the panel shot flips the git-panel
// auto-open on — the badge is a passive readout, so the panel opens via the
// dashboard.auto_open_git_panel opt-in rather than a click.
const dashCfg = { auto_open_git_panel: false }
await h.page.route(/\/api\/dashboard\/config$/, async route => {
  return json(route, dashCfg)
})
await h.page.route(/\/api\/project\/git/, async route => {
  const path = new URL(route.request().url()).pathname
  if (path === '/api/project/git/status') {
    return json(route, {
      repo: true, repoRoot: PROJECT, branch: 'mainline', ahead: 1, behind: 2,
      files: [
        { path: 'src/parser.py', status: 'M', staged: false, additions: 14, deletions: 3 },
        { path: 'src/utils.py', status: 'M', staged: true, additions: 2, deletions: 2 },
        { path: 'scratch.txt', status: '?', staged: false },
      ],
    })
  }
  if (path === '/api/project/git/log') {
    return json(route, {
      repo: true,
      commits: [
        { sha: 'e19c2ab', message: 'local work', author: 'Demo', date: new Date(Date.now() - 3600e3).toISOString(), isHead: true },
        { sha: '4f6d1c0', message: 'init', author: 'Demo', date: new Date(Date.now() - 86400e3).toISOString(), isHead: false },
      ],
    })
  }
  return json(route, { repo: true, repoRoot: PROJECT, branch: 'mainline' })
})

await h.load('dark', { selector: 'textarea', settle: 1500 })

// Full composer area, plus a tight clip of the footer strip itself.
await h.page.screenshot({ path: join(OUT, 'footer-git-status-full.png') })
const badge = h.page.locator('span[role="status"][title*="uncommitted"]')
await badge.waitFor({ timeout: 10000 })
const box = await badge.boundingBox()
const title = await badge.getAttribute('title')
console.log('BADGE', JSON.stringify({ box, title }))
await h.page.screenshot({
  path: join(OUT, 'footer-git-status-strip.png'),
  clip: { x: 0, y: box.y - 40, width: 1280, height: 90 },
})

// The Git panel carries the full per-file status the badge summarizes; flip
// the auto-open opt-in on and reload so it opens on sight of the repo (the
// badge is a passive readout, not a click target — max-two-buttons-per-row).
dashCfg.auto_open_git_panel = true
await h.load('dark', { selector: 'textarea', settle: 1500 })
await h.page.waitForSelector('text=src/parser.py', { timeout: 10000 })
await h.page.waitForTimeout(600)
await h.page.screenshot({ path: join(OUT, 'footer-git-status-panel-open.png') })
console.log('PANEL-OPEN shot taken')

// Light-theme variant of the strip (template asks for meaningful variants).
dashCfg.auto_open_git_panel = false
await h.load('light', { selector: 'textarea', settle: 1200 })
const badgeLight = h.page.locator('span[role="status"][title*="uncommitted"]')
await badgeLight.waitFor({ timeout: 10000 })
const lightBox = await badgeLight.boundingBox()
await h.page.screenshot({
  path: join(OUT, 'footer-git-status-strip-light.png'),
  clip: { x: 0, y: lightBox.y - 40, width: 1280, height: 90 },
})
console.log('LIGHT strip taken')

await h.close()
console.log('DONE', OUT)
