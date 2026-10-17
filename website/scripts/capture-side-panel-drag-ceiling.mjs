/**
 * Screenshot harness for the chat side panel's drag ceiling: how wide the
 * panel can be dragged before the resize handle stops.
 *
 * Same house pattern as `capture-md-edit-label.mjs`: the REAL built SPA
 * (`website/dist`) behind the shared in-process static server, every
 * `/api/**` answered from fixtures via Playwright route interception —
 * gateway-free. The client code under test is unmodified.
 *
 * Each frame is taken AFTER dragging the handle as far left as it will go
 * (well past any plausible ceiling, in small steps, so the clamp is what
 * stops it). The console prints the resulting panel width and its share of
 * the viewport; the same numbers land in `<prefix>-measurements.txt`.
 *
 * Frames (1920x1000, full page):
 *   <p>-rail-expanded    nav rail expanded, panel dragged to its ceiling
 *   <p>-rail-collapsed   nav rail collapsed, panel dragged to its ceiling
 *   <p>-rail-expanded-light  the first frame in light mode
 *
 * DIST=<dir> points the server at another build (e.g. a base-commit dist) so
 * a "before" run uses the same harness; PREFIX names the frames.
 *
 * Usage: node scripts/capture-side-panel-drag-ceiling.mjs [outDir]
 *        DIST=../../KiroCrew-main/website/dist PREFIX=10-before node scripts/capture-side-panel-drag-ceiling.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/side-panel-drag-ceiling'
const PREFIX = process.env.PREFIX || '20-after'
const DIST = process.env.DIST ? resolve(process.env.DIST) : undefined

const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const SLOT = 'chat-side-panel-ceiling'
const VIEW = { width: 1920, height: 1000 }
const MAX_EDGE = 2000
const MIN_MBPP = 15

mkdirSync(OUT, { recursive: true })

// ── Fixtures ────────────────────────────────────────────────────────────────

const MD_PATH = `${PROJECT}/notes/wide-table.md`
const MD_CONTENT = `# Release checklist

| Area | Owner | Status | Notes |
|---|---|---|---|
| Build | platform | done | reproducible on both runners |
| Docs | writers | in review | glossary pass outstanding |
| Security | appsec | done | threat model refreshed |
| Rollout | release | pending | canary at 5% first |

## Notes

Wide content is what the panel is for: a table or a diff that needs the room.
`
const FILE_CONTENT = { [MD_PATH]: MD_CONTENT }

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
const fileTab = { id: `file:${MD_PATH}`, kind: 'file', title: 'wide-table.md', path: MD_PATH, slot: SLOT, diffMode: false }

// ── Harness ─────────────────────────────────────────────────────────────────

function pngSize(path) {
  const b = readFileSync(path)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const executablePath = chromiumExecutable()
  console.log('chromium:', executablePath || '(playwright default)')
  console.log('dist:', DIST || '(worktree website/dist)', ' prefix:', PREFIX)
  const browser = await chromium.launch({ executablePath })
  const measurements = []
  const wrote = []

  const extra = async (path, route) => {
    const q = new URL(route.request().url()).searchParams.get('path') || ''
    if (path === '/api/chat/slots') return json(route, slots), true
    if (/^\/api\/chat\/slots\/[^/]+/.test(path)) return json(route, slotDetail), true
    if (path === '/api/file-read') {
      const body = FILE_CONTENT[q]
      return route.fulfill(body != null
        ? { status: 200, contentType: 'text/plain; charset=utf-8', body }
        : { status: 404, contentType: 'text/plain', body: 'not found' }), true
    }
    if (path === '/api/file-diff') return json(route, { diff: '', original: '', status: 'clean' }), true
    if (path === '/api/project/tree') return json(route, { root: PROJECT, paths: ['notes/wide-table.md'], repo: false, truncated: false }), true
    if (path === '/api/project/git/status') return json(route, { repo: false, files: [] }), true
    if (path === '/api/project/git') return json(route, { path: PROJECT, repo: false }), true
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] }), true
    return false
  }

  function record(file, evidence) {
    const { w, h } = pngSize(file)
    const bytes = readFileSync(file).length
    const mbpp = Math.round((bytes * 1000) / (w * h))
    const over = w > MAX_EDGE || h > MAX_EDGE
    const blank = mbpp < MIN_MBPP
    console.log(`wrote ${file}  ${w}x${h}  ${bytes}B  ${mbpp} milli-bytes/px${over ? '  OVER 2000px' : ''}${blank ? '  LIKELY BLANK' : ''}`)
    for (const e of evidence) console.log(`      asserted ${e}`)
    wrote.push(file)
    if (over) throw new Error(`frame ${file}: ${w}x${h} exceeds the ${MAX_EDGE}px edge budget`)
    if (blank) throw new Error(`frame ${file}: below the blank-frame floor — re-shoot, do not ship`)
  }

  async function openDashboard(theme, railCollapsed) {
    const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 1 })
    const page = await context.newPage()
    await stubDashboardApi(page, {
      slots, extra, theme,
      localStorageEntries: {
        'mc-active-slot-chat': SLOT,
        ['mc-activity-open:' + SLOT]: 'true',
        ['mc-panel-tabs:' + SLOT]: JSON.stringify({ activeId: fileTab.id, tabs: [fileTab] }),
        'mc-files-rail-open': '0',
        'mc-side-panel-width': '460',
        'mc-nav': railCollapsed ? '1' : '0',
        'kirocrew:comment-hint-dismissed': '1',
        ['mc-git-panel-opened:' + SLOT + ':' + PROJECT]: '1',
        'mc-chat-config': JSON.stringify({ pinLastPrompt: false, streamMode: 'immediate' }),
      },
    })
    logPageProblems(page)
    await page.goto(base + '/?sid=' + encodeURIComponent(SLOT), { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
    const panel = page.locator('div:has(> .side-panel-strip)').last()
    await panel.waitFor({ state: 'visible', timeout: 20000 })
    await panel.getByRole('heading', { name: 'Release checklist' }).waitFor({ timeout: 20000 })
    await page.waitForTimeout(600)
    return { context, page, panel }
  }

  const handle = page => page.locator('[role="separator"][aria-orientation="vertical"][aria-label="Resize panel"]').first()

  async function dragToCeiling(page) {
    const sep = handle(page)
    await sep.waitFor({ state: 'visible', timeout: 10000 })
    const box = await sep.boundingBox()
    const x = box.x + box.width / 2
    const y = box.y + box.height / 2
    await page.mouse.move(x, y)
    await page.mouse.down()
    const steps = 48
    for (let i = 1; i <= steps; i++) await page.mouse.move(x - (x - 12) * (i / steps), y)
    await page.mouse.up()
    await page.waitForTimeout(400)
  }

  async function measure(name, page) {
    const box = await handle(page).boundingBox()
    const panelW = Math.round(VIEW.width - box.x)
    const pct = Math.round((100 * panelW) / VIEW.width)
    const dialogs = await page.locator('[role="dialog"]:visible').count()
    if (dialogs > 0) throw new Error(`frame ${name}: ${dialogs} dialog(s) open — fix the fixture, do not save the frame`)
    const line = `${name}: panel ${panelW}px = ${pct}% of ${VIEW.width}px`
    console.log(line)
    measurements.push(line)
    return [`resize handle at x=${Math.round(box.x)}`, line]
  }

  async function frame(name, theme, railCollapsed) {
    const { context, page } = await openDashboard(theme, railCollapsed)
    await dragToCeiling(page)
    const evidence = await measure(name, page)
    const file = `${OUT}/${name}.png`
    await page.screenshot({ path: file })
    record(file, evidence)
    await context.close()
  }

  await frame(`${PREFIX}-rail-expanded`, 'dark', false)
  await frame(`${PREFIX}-rail-collapsed`, 'dark', true)
  await frame(`${PREFIX}-rail-expanded-light`, 'light', false)

  writeFileSync(`${OUT}/${PREFIX}-measurements.txt`, `viewport ${VIEW.width}x${VIEW.height}\n${measurements.join('\n')}\n`)
  await browser.close()
  srv.close()
  console.log(`done — ${wrote.length} frames in ${OUT}`)
}

main().catch(err => { console.error(err); process.exit(1) })
