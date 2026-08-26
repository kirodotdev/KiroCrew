/**
 * Screenshot harness for the file viewer's mode-toggle label: the header
 * button that enters/leaves markdown source mode. Source mode IS the editor
 * (there is no read-only source view for markdown), so the toggle reads
 * "Edit" / "Preview" — the same pair the fullscreen toolbar already uses —
 * rather than "View Source" / "View Preview", which described the mode as a
 * viewer and hid the edit affordance entirely.
 *
 * Same house pattern as `capture-pierre-files-tab.mjs`: the REAL built SPA
 * (`website/dist`) behind the shared in-process static server, every
 * `/api/**` answered from fixtures via Playwright route interception —
 * gateway-free. The client code under test is unmodified.
 *
 * Frames (prefix comes from LABEL_MODE):
 *   <p>-md-preview-toggle   a markdown file open in PREVIEW, the header
 *                           toggle in frame
 *   <p>-md-source-toggle    the same file after clicking the toggle: source
 *                           (edit) mode, the toggle now offering the way back
 *
 * LABEL_MODE=before probes the old "View Source"/"View Preview" labels (run
 * against a dist built from the base commit); the default probes the new
 * "Edit"/"Preview" labels. The probe set is what makes a frame trustworthy:
 * a frame is only written once the named button demonstrably rendered.
 *
 * Usage: node scripts/capture-md-edit-label.mjs [outDir]
 *        LABEL_MODE=before node scripts/capture-md-edit-label.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, existsSync, readdirSync } from 'node:fs'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { homedir } from 'node:os'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/md-edit-label'
const BEFORE = process.env.LABEL_MODE === 'before'
const PREFIX = BEFORE ? '10-before' : '20-after'
/** The labels this dist is expected to paint — asserted, never assumed. */
const PREVIEW_MODE_LABEL = BEFORE ? 'View Source' : 'Edit'
const SOURCE_MODE_LABEL = BEFORE ? 'View Preview' : 'Preview'

const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const SLOT = 'chat-md-edit-label'
const MAX_EDGE = 2000

mkdirSync(OUT, { recursive: true })

// ── Fixtures ────────────────────────────────────────────────────────────────

// Not under docs/: docs-lint treats a `docs/...` string in code as a citation
// of a real documentation file; this is a fixture path served entirely by the
// route stub above.
const MD_PATH = `${PROJECT}/notes/proposal.md`

const MD_CONTENT = `# Ship KAS inside the product

Decision proposal, draft v1. Every claim below was validated end to end.

## Executive summary

The bundle adds one platform per release and keeps the auth interface
unchanged. Distribution goes through the existing CDN.

## What needs alignment

1. Who owns the multi-platform bundle build
2. Version pinning and the upgrade policy
`

const FILE_CONTENT = { [MD_PATH]: MD_CONTENT }

const slots = [{
  key: SLOT,
  title: 'Proposal review',
  running: false,
  last_message: 'Proposal review',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const t0 = Math.floor(Date.now() / 1000) - 900
const slotDetail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', content: 'Open the proposal doc.', ts: String(t0) },
    { role: 'assistant', content: 'Opened it in the side panel.', ts: String(t0 + 30) },
  ],
}

const fileTab = {
  id: `file:${MD_PATH}`,
  kind: 'file',
  title: 'proposal.md',
  path: MD_PATH,
  slot: SLOT,
  diffMode: false,
}

// ── Harness ─────────────────────────────────────────────────────────────────

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

async function main() {
  const { srv, base } = await serveDist()
  const executablePath = chromiumExecutable()
  console.log('chromium:', executablePath || '(playwright default)')
  console.log('label mode:', BEFORE ? 'before (View Source/View Preview)' : 'after (Edit/Preview)')
  const browser = await chromium.launch({ executablePath })
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  const extra = async (path, route) => {
    const url = new URL(route.request().url())
    const q = url.searchParams.get('path') || ''
    if (path === '/api/chat/slots') return json(route, slots), true
    if (/^\/api\/chat\/slots\/[^/]+/.test(path)) return json(route, slotDetail), true
    // Cold-tab hydration reads the body as TEXT, not JSON.
    if (path === '/api/file-read') {
      const body = FILE_CONTENT[q]
      return route.fulfill(body != null
        ? { status: 200, contentType: 'text/plain; charset=utf-8', body }
        : { status: 404, contentType: 'text/plain', body: 'not found' }), true
    }
    if (path === '/api/file-diff') return json(route, { diff: '', original: '', status: 'clean' }), true
    if (path === '/api/project/tree') return json(route, { root: PROJECT, paths: ['notes/proposal.md'], repo: false, truncated: false }), true
    if (path === '/api/project/git/status') return json(route, { repo: false, files: [] }), true
    if (path === '/api/project/git') return json(route, { path: PROJECT, repo: false }), true
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] }), true
    return false
  }

  await stubDashboardApi(page, { slots, extra })
  logPageProblems(page)

  const wrote = []
  const MIN_MBPP = 15

  async function assertRendered(name, probes) {
    const found = []
    for (const { selector, locator, min = 1, attr } of probes) {
      const count = await locator.count()
      if (count < min) {
        throw new Error(`frame ${name}: probe \`${selector}\` matched ${count} node(s), need >= ${min} — surface did not render; fix the fixture, do not save the frame`)
      }
      const texts = []
      for (let i = 0; i < Math.min(count, min + 2); i++) {
        const v = attr
          ? await locator.nth(i).getAttribute(attr).catch(() => null)
          : await locator.nth(i).innerText().catch(() => '')
        const t = (v || '').trim()
        if (t) texts.push(`${attr ? `${attr}=` : ''}${t.replace(/\s+/g, ' ').slice(0, 70)}`)
      }
      if (texts.length === 0) {
        throw new Error(`frame ${name}: probe \`${selector}\` matched ${count} node(s) but every one is EMPTY — blank surface; fix the fixture, do not save the frame`)
      }
      found.push({ selector, count, text: texts.join(' ⏐ ') })
    }
    return found
  }

  function record(file, evidence) {
    const { w, h } = pngSize(file)
    const bytes = readFileSync(file).length
    const mbpp = Math.round((bytes * 1000) / (w * h))
    const over = w > MAX_EDGE || h > MAX_EDGE
    const blank = mbpp < MIN_MBPP
    console.log(`wrote ${file}  ${w}x${h}  ${bytes}B  ${mbpp} milli-bytes/px${over ? '  OVER 2000px' : ''}${blank ? `  LIKELY BLANK (< ${MIN_MBPP})` : ''}`)
    for (const e of evidence) console.log(`      asserted ${e.selector}  ×${e.count}  →  ${e.text}`)
    wrote.push({ file, w, h, over, blank })
    if (over) throw new Error(`frame ${file}: ${w}x${h} exceeds the ${MAX_EDGE}px edge budget`)
    if (blank) throw new Error(`frame ${file}: below the blank-frame floor — re-shoot, do not ship`)
  }

  async function shot(locator, name, probes) {
    const evidence = await assertRendered(name, probes)
    const file = `${OUT}/${name}.png`
    await locator.screenshot({ path: file })
    record(file, evidence)
  }

  const probe = (selector, locator, opts = {}) => ({ selector, locator, min: opts.min, attr: opts.attr })

  // Seed the persisted panel stores and load the dashboard (see the sibling
  // harness for why each key exists; the rail stays closed here — the toggle
  // under test lives in the viewer header, not the rail).
  await page.addInitScript(([slot, project, tabsJson]) => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot-chat', slot)
    localStorage.setItem('mc-activity-open:' + slot, 'true')
    localStorage.setItem('mc-panel-tabs:' + slot, tabsJson)
    localStorage.setItem('mc-files-rail-open', '0')
    localStorage.setItem('mc-side-panel-width', '760')
    localStorage.setItem('kirocrew:comment-hint-dismissed', '1')
    localStorage.setItem('mc-git-panel-opened:' + slot + ':' + project, '1')
    localStorage.setItem('mc-chat-config', JSON.stringify({ pinLastPrompt: false, streamMode: 'immediate' }))
  }, [SLOT, PROJECT, JSON.stringify({ activeId: fileTab.id, tabs: [fileTab] })])
  await page.goto(base + '/?sid=' + encodeURIComponent(SLOT), { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  const panel = () => page.locator('div:has(> .side-panel-strip)').last()
  const p = panel()
  await p.waitFor({ state: 'visible', timeout: 20000 })
  await p.getByRole('heading', { name: 'Executive summary' }).waitFor({ timeout: 20000 })
  await page.waitForTimeout(800)

  // ── Frame 1: markdown PREVIEW — the toggle offers the way into the editor ──
  await shot(p, `${PREFIX}-md-preview-toggle`, [
    probe(`header toggle "${PREVIEW_MODE_LABEL}" (aria-pressed=false)`,
      p.getByRole('button', { name: PREVIEW_MODE_LABEL, exact: true }).and(p.locator('[aria-pressed="false"]'))),
    probe('rendered markdown heading', p.getByRole('heading', { name: 'Executive summary' })),
  ])

  // ── Frame 2: SOURCE (edit) mode — the toggle offers the way back ───────────
  await p.getByRole('button', { name: PREVIEW_MODE_LABEL, exact: true }).click()
  await page.waitForTimeout(1500)
  await shot(p, `${PREFIX}-md-source-toggle`, [
    probe(`header toggle "${SOURCE_MODE_LABEL}" (aria-pressed=true)`,
      p.getByRole('button', { name: SOURCE_MODE_LABEL, exact: true }).and(p.locator('[aria-pressed="true"]'))),
    probe('Pierre editor surface mounted (source mode is the editor)', p.locator('.pierre-surface'), { attr: 'class' }),
  ])

  await browser.close()
  srv.close()
  console.log(`done — ${wrote.length} frames in ${OUT}`)
}

main().catch(err => { console.error(err); process.exit(1) })
