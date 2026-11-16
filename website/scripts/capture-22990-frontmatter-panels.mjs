/**
 * Screenshot + behaviour harness for #22990 on the surfaces people actually
 * use: the file SIDE PANEL and the FULLSCREEN view of `MarkdownPanel` (the
 * sibling `capture-22990-frontmatter-strip.mjs` drives the AWS drive file
 * dialog, a modal).
 *
 * Runs the REAL built SPA behind the shared loopback static server with every
 * /api/** call answered from fixtures, so no gateway is needed. Each frame is
 * written only after its named surface demonstrably rendered.
 *
 * MODE=after (default) asserts the strip renders in the side panel AND in
 * fullscreen, and that collapsing it hides the rows without unmounting them.
 * MODE=before asserts the strip is absent (run it against a dist built from
 * the base commit; point DIST_DIR at that dist).
 *
 * Usage: [DIST_DIR=<dist>] [THEME=dark|light] [MODE=before|after] \
 *        node scripts/capture-22990-frontmatter-panels.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/22990-panels'
const BEFORE = process.env.MODE === 'before'
const THEME = process.env.THEME === 'light' ? 'light' : 'dark'
const PREFIX = `${BEFORE ? 'before' : 'after'}-${THEME}`
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const SLOT = 'chat-fm-panels'
const MAX_EDGE = 2000
mkdirSync(OUT, { recursive: true })

// A file path that is only a key in the route stub below, never read from disk.
const MD_PATH = `${PROJECT}/notes/incident-retro.md`
const MD_CONTENT = `---
title: Quarterly incident retrospective
status: in-review
owner: platform-team
tags: [retrospective, reliability]
---

# Quarterly incident retrospective

The review covers three incidents and the follow-up work they produced.

## Executive summary

Two of the three incidents shared one root cause, a missing alert on queue depth.
`

const slots = [{
  key: SLOT, title: 'Retro review', running: false, last_message: 'Retro review',
  messages: 2, agent: 'kirocrew', memory_mode: 'persistent', project: PROJECT,
  modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
}]
const t0 = Math.floor(Date.now() / 1000) - 900
const slotDetail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', content: 'Open the retro doc.', ts: String(t0) },
    { role: 'assistant', content: 'Opened it in the side panel.', ts: String(t0 + 30) },
  ],
}
const fileTab = { id: `file:${MD_PATH}`, kind: 'file', title: 'incident-retro.md', path: MD_PATH, slot: SLOT, diffMode: false }

function pngSize(path) {
  const b = readFileSync(path)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

async function main() {
  const { srv, base } = await serveDist(process.env.DIST_DIR || undefined)
  const executablePath = chromiumExecutable()
  const browser = await chromium.launch({ executablePath })
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
  const page = await context.newPage()

  const extra = async (path, route) => {
    const q = new URL(route.request().url()).searchParams.get('path') || ''
    if (path === '/api/chat/slots') return json(route, slots), true
    // A local chat: the side panel withholds file tabs until this read says so.
    if (/^\/api\/chat\/slots\/[^/]+\/environment$/.test(path)) return json(route, { profile: null, profiles: [] }), true
    if (/^\/api\/chat\/slots\/[^/]+/.test(path)) return json(route, slotDetail), true
    if (path === '/api/file-read') {
      return route.fulfill(q === MD_PATH
        ? { status: 200, contentType: 'text/plain; charset=utf-8', body: MD_CONTENT }
        : { status: 404, contentType: 'text/plain', body: 'not found' }), true
    }
    if (path === '/api/file-diff') return json(route, { diff: '', original: '', status: 'clean' }), true
    if (path === '/api/project/tree') return json(route, { root: PROJECT, paths: ['notes/incident-retro.md'], repo: false, truncated: false }), true
    if (path === '/api/project/git/status') return json(route, { repo: false, files: [] }), true
    if (path === '/api/project/git') return json(route, { path: PROJECT, repo: false }), true
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] }), true
    return false
  }
  await stubDashboardApi(page, {
    slots, extra, theme: THEME,
    localStorageEntries: {
      'mc-active-slot-chat': SLOT,
      ['mc-activity-open:' + SLOT]: 'true',
      ['mc-panel-tabs:' + SLOT]: JSON.stringify({ activeId: fileTab.id, tabs: [fileTab] }),
      'mc-files-rail-open': '0',
      'mc-side-panel-width': '760',
      'kirocrew:comment-hint-dismissed': '1',
      ['mc-git-panel-opened:' + SLOT + ':' + PROJECT]: '1',
      'mc-chat-config': JSON.stringify({ pinLastPrompt: false, streamMode: 'immediate' }),
    },
  })
  logPageProblems(page)

  const fail = (msg) => { throw new Error(msg) }
  // Polls, so a render or unmount still settling is waited out rather than
  // sampled once; a count that never reaches `want` fails after the deadline.
  const expectCount = async (loc, want, what, timeout = 5000) => {
    const deadline = Date.now() + timeout
    let n = await loc.count()
    while (n !== want && Date.now() < deadline) {
      await page.waitForTimeout(100)
      n = await loc.count()
    }
    if (n !== want) fail(`${what}: expected ${want}, found ${n}`)
    console.log(`  ok  ${what} = ${n}`)
  }
  async function frame(name) {
    const file = `${OUT}/${PREFIX}-${name}.png`
    await page.screenshot({ path: file })
    const { w, h } = pngSize(file)
    if (w > MAX_EDGE || h > MAX_EDGE) fail(`${file}: ${w}x${h} over the ${MAX_EDGE}px budget`)
    console.log(`wrote ${file} ${w}x${h}`)
  }
  const resolvedTheme = () => page.evaluate(() => document.documentElement.getAttribute('data-theme') || '')

  await page.goto(base + '/?sid=' + encodeURIComponent(SLOT), { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)
  await page.getByText('Executive summary').first().waitFor({ timeout: 20000 })
  await page.waitForTimeout(600)
  // The built-in themes resolve to `kiro-dark` / `kiro-light`.
  if (!(await resolvedTheme()).endsWith(THEME)) fail(`theme did not resolve to ${THEME}`)

  // ── Side panel ────────────────────────────────────────────────────────────
  console.log(`side panel (${BEFORE ? 'before' : 'after'}, ${THEME})`)
  const sideStrip = page.getByTestId('md-frontmatter')
  if (BEFORE) {
    await expectCount(sideStrip, 0, 'side panel: no metadata strip on the base build')
    await frame('side-panel')
  } else {
    await expectCount(sideStrip, 1, 'side panel: metadata strip')
    await sideStrip.getByText('Frontmatter · 4 fields').waitFor({ timeout: 5000 })
    await expectCount(sideStrip.locator('dt'), 4, 'side panel: key rows')
    await expectCount(page.getByRole('heading', { name: 'Quarterly incident retrospective' }), 1, 'side panel: body heading (title is not repeated in the body prose)')
    await frame('side-panel')
    await sideStrip.getByRole('button').click()
    await page.waitForTimeout(300)
    await expectCount(sideStrip.locator('dl[hidden]'), 1, 'side panel: collapsed rows hidden, still mounted')
    await frame('side-panel-collapsed')
    await sideStrip.getByRole('button').click()
    await page.waitForTimeout(300)
  }

  // ── Fullscreen ────────────────────────────────────────────────────────────
  console.log(`fullscreen (${BEFORE ? 'before' : 'after'}, ${THEME})`)
  await page.getByTestId('markdown-panel-more-options').first().click()
  await page.getByRole('menuitem', { name: /full screen/i }).click()
  const fsHeader = page.getByTestId('markdown-panel-fullscreen-header')
  await fsHeader.waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(800)
  const fsStrip = page.getByTestId('md-frontmatter')
  if (BEFORE) {
    await expectCount(fsStrip, 0, 'fullscreen: no metadata strip on the base build')
    await frame('fullscreen')
  } else {
    await expectCount(fsStrip, 1, 'fullscreen: metadata strip (the side panel is unmounted while fullscreen)')
    await fsStrip.getByText('Frontmatter · 4 fields').waitFor({ timeout: 5000 })
    await expectCount(fsStrip.locator('dt'), 4, 'fullscreen: key rows')
    await frame('fullscreen')
    await fsStrip.getByRole('button').click()
    await page.waitForTimeout(300)
    await expectCount(fsStrip.locator('dl[hidden]'), 1, 'fullscreen: collapsed rows hidden, still mounted')
    await frame('fullscreen-collapsed')
  }

  await browser.close()
  srv.close()
  console.log('done')
}

main().catch((err) => { console.error(err); process.exit(1) })
