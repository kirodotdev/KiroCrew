/**
 * Screenshot harness for #37165: the activity panel in an embedded chat frame
 * (`/embed/chat/<slug>?sid=<key>`) about 900px wide, after the main dashboard
 * has left its default rail and pinned 260px sidebar in this browser.
 *
 * Same house pattern as `capture-side-panel-drag-ceiling.mjs`: the REAL built
 * SPA behind the shared in-process static server, every `/api/**` answered
 * from fixtures via Playwright route interception, gateway-free and with no
 * dashboard token. The client code under test is unmodified.
 *
 * Frames (900x700):
 *   <p>-embed-900        the frame with the panel open, as it lands
 *   <p>-embed-900-drag   the same frame after dragging the panel's edge left
 *                        by 200px (only when a resize edge exists)
 *
 * The console prints whether the panel has a resize edge and its width; the
 * same lines land in `<prefix>-measurements.txt`.
 *
 * DIST=<dir> points the server at another build (a base-commit dist) so a
 * "before" run uses the same harness; PREFIX names the frames.
 *
 * Usage: node scripts/capture-37165-embed-panel-resize.mjs [outDir]
 *        DIST=/tmp/dist-before PREFIX=10-before node scripts/capture-37165-embed-panel-resize.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/37165-embed-panel-resize'
const PREFIX = process.env.PREFIX || '20-after'
const DIST = process.env.DIST ? resolve(process.env.DIST) : undefined

const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const SLOT = 'chat-embed-panel'
const VIEW = { width: 900, height: 700 }
const MIN_MBPP = 15

mkdirSync(OUT, { recursive: true })

const MD_PATH = `${PROJECT}/notes/release.md`
const MD_CONTENT = `# Release checklist

| Area | Owner | Status |
|---|---|---|
| Build | platform | done |
| Docs | writers | in review |
| Rollout | release | pending |
`
const slots = [{
  key: SLOT, title: 'Release checklist', running: false, last_message: 'Release checklist',
  messages: 2, agent: 'kirocrew', memory_mode: 'persistent', project: PROJECT,
  modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
}]
const t0 = Math.floor(Date.now() / 1000) - 900
const slotDetail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', content: 'Open the release checklist.', ts: String(t0) },
    { role: 'assistant', content: 'Opened it in the side panel.', ts: String(t0 + 30) },
  ],
}
const fileTab = { id: `file:${MD_PATH}`, kind: 'file', title: 'release.md', path: MD_PATH, slot: SLOT, diffMode: false }

function pngSize(path) {
  const b = readFileSync(path)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch({ executablePath: chromiumExecutable() })
  console.log('dist:', DIST || '(worktree website/dist)', ' prefix:', PREFIX)
  const lines = []
  const note = line => { console.log(line); lines.push(line) }

  const extra = async (path, route) => {
    const q = new URL(route.request().url()).searchParams.get('path') || ''
    if (path === '/api/chat/slots') return json(route, slots), true
    if (path.startsWith('/api/chat/slots/')) return json(route, slotDetail), true
    if (path === '/api/file-read') {
      return route.fulfill(q === MD_PATH
        ? { status: 200, contentType: 'text/plain; charset=utf-8', body: MD_CONTENT }
        : { status: 404, contentType: 'text/plain', body: 'not found' }), true
    }
    if (path === '/api/file-diff') return json(route, { diff: '', original: '', status: 'clean' }), true
    if (path === '/api/project/tree') return json(route, { root: PROJECT, paths: ['notes/release.md'], repo: false, truncated: false }), true
    if (path === '/api/project/git/status') return json(route, { repo: false, files: [] }), true
    if (path === '/api/project/git') return json(route, { path: PROJECT, repo: false }), true
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] }), true
    return false
  }

  function shoot(file) {
    const { w, h } = pngSize(file)
    const mbpp = Math.round((readFileSync(file).length * 1000) / (w * h))
    if (mbpp < MIN_MBPP) throw new Error(`frame ${file}: below the blank-frame floor, re-shoot`)
    console.log(`wrote ${file}  ${w}x${h}`)
  }

  const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 1 })
  const page = await context.newPage()
  await stubDashboardApi(page, {
    slots, extra, theme: 'dark',
    localStorageEntries: {
      // What the main dashboard leaves in this browser: the rail at its
      // default and the session sidebar pinned at its default width.
      'mc-nav': '1',
      'mc-sidebar-pinned': 'true',
      'mc-sidebar-width': '260',
      ['mc-activity-open:' + SLOT]: 'true',
      ['mc-panel-tabs:' + SLOT]: JSON.stringify({ activeId: fileTab.id, tabs: [fileTab] }),
      'mc-side-panel-width': '360',
      'kirocrew:comment-hint-dismissed': '1',
      ['mc-git-panel-opened:' + SLOT + ':' + PROJECT]: '1',
      'mc-chat-config': JSON.stringify({ pinLastPrompt: false, streamMode: 'immediate' }),
    },
  })
  logPageProblems(page)
  await page.goto(`${base}/embed/chat/${SLOT}?sid=${SLOT}`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)
  const panel = page.locator('div:has(> .side-panel-strip)').last()
  await panel.waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(600)

  const sep = page.locator('[role="separator"][aria-orientation="vertical"][aria-label="Resize panel"]').first()
  const hasEdge = await sep.count() > 0 && await sep.isVisible()
  const width = async () => Math.round((await panel.boundingBox()).width)
  note(`${PREFIX}: viewport ${VIEW.width}px, resize edge ${hasEdge ? 'present' : 'absent'}, panel ${await width()}px`)
  await page.screenshot({ path: `${OUT}/${PREFIX}-embed-900.png` })
  shoot(`${OUT}/${PREFIX}-embed-900.png`)

  if (hasEdge) {
    const box = await sep.boundingBox()
    const x = box.x + box.width / 2
    const y = box.y + box.height / 2
    await page.mouse.move(x, y)
    await page.mouse.down()
    for (let i = 1; i <= 20; i++) await page.mouse.move(x - 10 * i, y)
    await page.mouse.up()
    await page.waitForTimeout(400)
    note(`${PREFIX}: after a 200px drag left, panel ${await width()}px`)
    await page.screenshot({ path: `${OUT}/${PREFIX}-embed-900-drag.png` })
    shoot(`${OUT}/${PREFIX}-embed-900-drag.png`)
  }

  writeFileSync(`${OUT}/${PREFIX}-measurements.txt`, lines.join('\n') + '\n')
  await context.close()
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
