/**
 * Caret alignment on the editable split diff surface (wrap mode).
 *
 * Opens a code file in the Files tab with diff mode + split + word wrap on,
 * clicks a line to place the caret, then measures the caret's top against the
 * top of the row it belongs to and captures a tight crop around both.
 *
 * The measurement is the point: it prints `delta` (caret.top - line.top), which
 * is 0 when the overlays sit on their text and equals `[data-code]`'s
 * padding-top when Pierre's `#getLineY` double-counts it.
 *
 * Usage: node scripts/capture-pierre-caret-align.mjs <outDir> <label> [split|unified]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, existsSync, readdirSync } from 'node:fs'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { homedir } from 'node:os'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/caret-align'
const LABEL = process.argv[3] || 'frame'
const SPLIT = (process.argv[4] || 'split') !== 'unified'
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const SLOT = 'chat-caret-align'
const FILE = PROJECT + '/website/src/pierre/config.ts'

const ORIGINAL = `export const PIERRE_WORKER_POOL_SIZE = 4

export function pierreFileOptions(overrides) {
  return { ...PIERRE_CODE_DEFAULTS, ...overrides }
}

export function pierreDiffOptions(overrides) {
  return { ...PIERRE_DIFF_DEFAULTS, ...overrides }
}
`

const CONTENT = `export const PIERRE_WORKER_POOL_SIZE = 4

export function pierreFileOptions(overrides) {
  return { ...PIERRE_CODE_DEFAULTS, ...overrides }
}

export function pierreDiffOptions(overrides) {
  return { ...PIERRE_DIFF_DEFAULTS, ...overrides, themeType: 'dark' }
}
`

const DIFF = `--- a/website/src/pierre/config.ts
+++ b/website/src/pierre/config.ts
@@ -6,5 +6,5 @@
 export function pierreDiffOptions(overrides) {
-  return { ...PIERRE_DIFF_DEFAULTS, ...overrides }
+  return { ...PIERRE_DIFF_DEFAULTS, ...overrides, themeType: 'dark' }
 }
`

const slots = [{
  key: SLOT, title: 'Caret alignment', running: false, last_message: 'caret',
  messages: 2, agent: 'kirocrew', memory_mode: 'persistent', project: PROJECT,
  modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
}]
const t0 = Math.floor(Date.now() / 1000) - 900
const slotDetail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', content: 'Open the config in split diff mode.', ts: String(t0) },
    { role: 'assistant', content: 'Opened.', ts: String(t0 + 60) },
  ],
}
const GIT_FILES = [{ path: 'website/src/pierre/config.ts', status: 'modified', additions: 1, deletions: 1 }]
const bucket = (tabs, activeId) => JSON.stringify({ activeId, tabs })

function pngSize(path) {
  const b = readFileSync(path)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

function chromiumExecutable() {
  if (process.env.PLAYWRIGHT_CHROMIUM) return process.env.PLAYWRIGHT_CHROMIUM
  const cache = join(homedir(), '.cache', 'ms-playwright')
  if (!existsSync(cache)) return undefined
  const rev = d => parseInt((/-(\d+)$/.exec(d) || [])[1] || '0', 10)
  return readdirSync(cache)
    .filter(d => d.startsWith('chromium_headless_shell-') || d.startsWith('chromium-'))
    .sort((a, b) => rev(b) - rev(a))
    .flatMap(d => [
      join(cache, d, 'chrome-headless-shell-linux64', 'chrome-headless-shell'),
      join(cache, d, 'chrome-linux64', 'chrome'),
      join(cache, d, 'chrome-linux', 'chrome'),
    ])
    .find(existsSync)
}

/** Shadow-piercing probe: caret rect, nearest row rect, and the padding that
 *  Pierre's `#getLineY` adds on top of an `offsetTop` that already carries it. */
const PROBE = () => {
  const all = []
  ;(function walk(r) {
    r.querySelectorAll('*').forEach(el => { all.push(el); if (el.shadowRoot) walk(el.shadowRoot) })
  })(document)
  const caret = all.find(el => el.matches?.('[data-caret]'))
  if (!caret) return { error: 'no caret' }
  const cr = caret.getBoundingClientRect()
  if (!caret.isConnected || (cr.top === 0 && cr.height === 0)) return { error: 'caret detached' }
  const rows = all.filter(el => el.matches?.('[data-line]'))
    .map(l => l.getBoundingClientRect()).filter(r => r.height > 0)
  if (!rows.length) return { error: 'no rows' }
  const row = rows.sort((a, b) => Math.abs(a.top - cr.top) - Math.abs(b.top - cr.top))[0]
  const code = all.find(el => el.matches?.('[data-code]'))
  return {
    caretTop: +cr.top.toFixed(2), caretLeft: +cr.left.toFixed(2), caretH: +cr.height.toFixed(2),
    rowTop: +row.top.toFixed(2), rowH: +row.height.toFixed(2),
    delta: +(cr.top - row.top).toFixed(2),
    codePadTop: code ? getComputedStyle(code).paddingTop : '(no [data-code])',
  }
}

async function main() {
  const { srv, base } = await serveDist()
  const executablePath = chromiumExecutable()
  console.log('chromium:', executablePath || '(playwright default)')
  const browser = await chromium.launch({ executablePath })
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

  const extra = async (path, route) => {
    const q = new URL(route.request().url()).searchParams.get('path') || ''
    if (path === '/api/chat/slots') return json(route, slots), true
    if (/^\/api\/chat\/slots\/[^/]+/.test(path)) return json(route, slotDetail), true
    if (path === '/api/project/tree') {
      return json(route, { root: PROJECT, paths: ['website/src/pierre/config.ts'], repo: true, truncated: false }), true
    }
    if (path === '/api/project/git/status') {
      return json(route, { repo: true, repoRoot: PROJECT, branch: 'fix/pierre-caret-align', ahead: 0, behind: 0, files: GIT_FILES }), true
    }
    if (path === '/api/project/git') {
      return json(route, { path: PROJECT, repo: true, repoRoot: PROJECT, branch: 'fix/pierre-caret-align', detached: false, head: 'abc1234' }), true
    }
    if (path === '/api/project/git/log') return json(route, { repo: true, commits: [] }), true
    if (path === '/api/file-read') {
      return route.fulfill(q === FILE
        ? { status: 200, contentType: 'text/plain; charset=utf-8', body: CONTENT }
        : { status: 404, contentType: 'text/plain', body: 'not found' }), true
    }
    if (path === '/api/file-diff') {
      return json(route, q === FILE
        ? { diff: DIFF, original: ORIGINAL, status: 'modified' }
        : { diff: '', original: '', status: 'clean' }), true
    }
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] }), true
    return false
  }

  await stubDashboardApi(page, { slots, extra })
  logPageProblems(page)

  const tab = {
    id: `file:${FILE}`, kind: 'file', title: 'config.ts', path: FILE, slot: SLOT, diffMode: true,
  }
  await page.addInitScript(([slot, project, tabsJson, split]) => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot-chat', slot)
    localStorage.setItem('mc-activity-open:' + slot, 'true')
    localStorage.setItem('mc-panel-tabs:' + slot, tabsJson)
    // '1'/'0' — usePersistedBool's getter is `v === '1'`.
    localStorage.setItem('mc-diff-split', split)
    localStorage.setItem('mc-file-wordwrap', '1')
    localStorage.setItem('mc-file-linenums', '1')
    localStorage.setItem('mc-file-collapse-unchanged', '0')
    localStorage.setItem('mc-files-rail-open', '0')
    localStorage.setItem('mc-side-panel-width', '900')
    localStorage.setItem('kirocrew:comment-hint-dismissed', '1')
    localStorage.setItem('mc-git-panel-opened:' + slot + ':' + project, '1')
    localStorage.setItem('mc-chat-config', JSON.stringify({ pinLastPrompt: false, streamMode: 'immediate' }))
  }, [SLOT, PROJECT, bucket([{ id: 'files', kind: 'files', title: 'Files' }, tab], tab.id), SPLIT ? '1' : '0'])

  await page.goto(base + '/?sid=' + encodeURIComponent(SLOT), { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3200)

  // Place a caret: click a changed row. Prefer the additions pane when the
  // layout has one; the fallback must walk shadow roots too, since every row
  // lives inside Pierre's and `document.querySelectorAll` cannot reach them.
  const target = await page.evaluateHandle(() => {
    const all = []
    ;(function walk(r) {
      r.querySelectorAll('*').forEach(el => { all.push(el); if (el.shadowRoot) walk(el.shadowRoot) })
    })(document)
    const add = all.find(el => el.matches?.('[data-additions]'))
    const rows = add
      ? [...add.querySelectorAll('[data-line]')]
      : all.filter(el => el.matches?.('[data-line]'))
    return rows[Math.min(3, rows.length - 1)] ?? null
  })
  const el = target.asElement()
  if (!el) { console.error('FAIL: no [data-line] to click'); await browser.close(); srv.close(); process.exit(1) }
  await el.click({ position: { x: 40, y: 6 } })
  await page.waitForTimeout(500)

  const m = await page.evaluate(PROBE)
  console.log('measurement:', JSON.stringify(m))
  if (m.error) { console.error('FAIL: ' + m.error); await browser.close(); srv.close(); process.exit(1) }

  mkdirSync(OUT, { recursive: true })
  const out = join(OUT, `${LABEL}.png`)
  // Tight crop around the caret and its row so an 8px offset is legible.
  const clip = {
    x: Math.max(0, m.caretLeft - 170),
    y: Math.max(0, Math.min(m.caretTop, m.rowTop) - 46),
    width: 620,
    height: 150,
  }
  await page.screenshot({ path: out, clip })
  const { w, h } = pngSize(out)
  console.log(`wrote ${out} (${w}x${h})  delta=${m.delta}  codePadTop=${m.codePadTop}`)

  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })
