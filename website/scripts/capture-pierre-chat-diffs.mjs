/**
 * Screenshot harness for the CHAT surfaces of the Monaco → Pierre migration.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via route interception
 * (gateway-free — no kiro-cli, no live backend, no token). The client code under
 * test is unmodified: FileChangeChips renders `meta.file_changes` and DiffBlock
 * renders a fenced ```diff exactly as in production.
 *
 * Frames:
 *   01-chips-card-collapsed   the file-change card, 5 lightweight rows:
 *                             36px header band + its color-mix tint, per-row
 *                             filename + ±counts + diffstat cells, row dividers;
 *                             no Pierre surface is mounted yet
 *   01b-chips-card-320        the same closed rows at the 320px viewport floor;
 *                             an artifact row keeps its badge, counts and useful
 *                             filename while redundant diffstat cells hide;
 *                             Pierre remains unmounted
 *   02-chip-row-expanded      one row open — the inline Pierre diff INSIDE the
 *                             transcript (the headline feature)
 *   03-chip-filename-hover    collapsed filename hovered: it is the open-file
 *                             control, so it takes the accent color + pointer cursor
 *   04-chat-diff-basename     a chat diff block whose patch names
 *                             `website/src/components/DiffBlock.tsx` — the header
 *                             must read `DiffBlock.tsx`, never the full path
 *   05-chat-diff-split        the same block after toggling Split view
 *   06-collapsed-unchanged    two distant hunks, so Pierre draws its collapsed
 *                             unchanged-region separator row between them
 *   07-headerless-fallback    a patch with `@@` hunks but NO `---`/`+++` lines.
 *                             KNOWN REGRESSION, captured deliberately: Pierre's
 *                             parser yields zero files, so PierrePatchImpl falls
 *                             back to PlainCodeFallback — no file header, and
 *                             therefore no Open / Split / Copy controls either
 *                             (they are slotted into Pierre's header).
 *
 * Every frame is an ELEMENT screenshot (`locator.screenshot()`), not a full-page
 * one: the transcript is taller than the 2000px-per-edge budget for PR media
 * once deviceScaleFactor is 2. Dimensions are asserted after each write.
 *
 * Usage: node scripts/capture-pierre-chat-diffs.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/pierre-diffs'
/** Repo root, derived from this script's own location: the fixtures show a real
 *  project path in breadcrumbs and the file rail without pinning the frames to
 *  one machine's worktree. */
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')

/** Hard ceiling for a PR-attached PNG, on BOTH edges. */
const MAX_EDGE = 2000

mkdirSync(OUT, { recursive: true })

// ── Fixtures ────────────────────────────────────────────────────────────────

/* `meta.file_changes` is `{path, before, after}` per file (ChatPage passes it
 * straight through to FileChangeChips as FileChangeEntry[]), and the row's
 * ±counts are computed CLIENT-side by countLines — so the before/after pair has
 * to be real text, not a summary. Five files: enough for the header roll-up and
 * four dividers, still under COLLAPSED_COUNT (8) so no "Show N more" row steals
 * the frame. */
const change = (path, before, after) => ({ path, before, after })

const MONACO_BLOCK_BEFORE = `import { lazy, Suspense } from 'react'
import { MonacoDiffViewer } from './MonacoDiffViewer'
import { parseUnifiedDiff } from '../utils/parseUnifiedDiff'

export default function DiffBlock({ code, complete }: Props) {
  const parsed = parseUnifiedDiff(code)
  const [sideBySide, setSideBySide] = useState(false)
  return (
    <div className="diff-block">
      <Suspense fallback={<pre>{code}</pre>}>
        <MonacoDiffViewer
          original={parsed.original}
          modified={parsed.modified}
          renderSideBySide={sideBySide}
          theme={monacoTheme}
        />
      </Suspense>
    </div>
  )
}`

const MONACO_BLOCK_AFTER = `import { useMemo, useState } from 'react'
import { PierrePatch } from '../pierre'
import { basenamePatchHeaders } from '../utils/diffUtils'
import { PIERRE_COMPACT_HEADER_CSS } from '../pierre/config'

export default function DiffBlock({ code, complete }: Props) {
  const displayPatch = useMemo(() => basenamePatchHeaders(code), [code])
  const [sideBySide, setSideBySide] = useState(false)
  const options = useMemo(() => ({
    diffStyle: sideBySide ? 'split' : 'unified',
    overflow: 'wrap',
    disableFileHeader: false,
    unsafeCSS: PIERRE_COMPACT_HEADER_CSS,
  }), [sideBySide])
  return (
    <div className="diff-block group/diff rounded-xl border border-border">
      <div className="pierre-surface">
        <PierrePatch patch={displayPatch} options={options} />
      </div>
    </div>
  )
}`

const IMPL_BEFORE = `export function PierrePatchImpl({ patch, options }: Props) {
  const dark = useIsDark()
  const files = useMemo(() => parsePatchFiles(patch).flatMap(p => p.files), [patch])
  if (files.length === 0) return <PlainCodeFallback text={patch} />
  return (
    <PierreShell>
      {files.map((fileDiff, i) => (
        <FileDiff key={i} fileDiff={fileDiff} options={options} />
      ))}
    </PierreShell>
  )
}`

const IMPL_AFTER = `export function PierrePatchImpl({ patch, options, renderHeaderMetadata }: Props) {
  const dark = useIsDark()
  const resolved = useMemo(
    () => pierreDiffOptions({ themeType: pierreThemeType(dark), ...options }),
    [dark, options],
  )
  const files = useMemo(() => {
    try {
      const parsed = parsePatchFiles(normalizePatchHunks(patch)).flatMap(p => p.files)
      for (const f of parsed) {
        if (f.name?.startsWith('b/') && f.prevName?.startsWith('a/')) {
          f.name = f.name.slice(2)
          const prev = f.prevName.slice(2)
          f.prevName = prev === f.name ? undefined : prev
        }
        f.cacheKey = contentCacheKey(f.name ?? '', patch)
      }
      return parsed
    } catch {
      return []
    }
  }, [patch])
  const noHunks = files.length > 0 && files.every(f => (f.hunks?.length ?? 0) === 0)
  const looksLikeChanges = /^[+-](?![+-][+-] )/m.test(patch)
  if (files.length === 0 || (noHunks && looksLikeChanges)) return <PlainCodeFallback text={patch} />
  return (
    <PierreShell>
      {files.map((fileDiff, i) => (
        <FileDiff
          key={\`\${fileDiff.name ?? ''}:\${i}\`}
          fileDiff={fileDiff}
          options={resolved}
          renderHeaderMetadata={i === 0 ? renderHeaderMetadata : undefined}
        />
      ))}
    </PierreShell>
  )
}`

const UTILS_BEFORE = `export function parseUnifiedDiff(patch: string): ParsedDiff {
  const original: string[] = []
  const modified: string[] = []
  for (const line of patch.split('\\n')) {
    if (line.startsWith('@@') || line.startsWith('---') || line.startsWith('+++')) continue
    if (line.startsWith('-')) original.push(line.slice(1))
    else if (line.startsWith('+')) modified.push(line.slice(1))
    else { original.push(line.slice(1)); modified.push(line.slice(1)) }
  }
  return { original: original.join('\\n'), modified: modified.join('\\n') }
}`

const UTILS_AFTER = `export function basenamePatchHeaders(patch: string): string {
  const short = (tok: string): string => {
    if (tok === '/dev/null') return tok
    const m = /^([ab]\\/)(.*)$/.exec(tok)
    const prefix = m ? m[1] : ''
    const rest = m ? m[2] : tok
    return prefix + rest.slice(rest.lastIndexOf('/') + 1)
  }
  return patch.split('\\n').map(line => {
    const file = /^(--- |\\+\\+\\+ )(.*)$/.exec(line)
    if (file) {
      const [path, ...tail] = file[2].split('\\t')
      return file[1] + [short(path), ...tail].join('\\t')
    }
    return line
  }).join('\\n')
}`

const TOOLDETAILS_BEFORE = `import MonacoCodeBlock from '../../components/MonacoCodeBlock'

const body = <MonacoCodeBlock code={text} language={lang} readOnly />`

const TOOLDETAILS_AFTER = `import { PierreCode } from '../../pierre'

const body = <PierreCode file={{ name, contents: text }} langHint={lang} />`

const CONFIG_BEFORE = `export const MONACO_THEMES = { dark: 'vs-dark', light: 'vs' }
export const MONACO_WORKER_URL = '/assets/monaco/editor.worker.js'`

const CONFIG_AFTER = `export const PIERRE_THEMES: ThemesType = { dark: 'pierre-dark', light: 'pierre-light' }

export const PIERRE_COMPACT_HEADER_CSS = \`
[data-diffs-header]{--diffs-gap-block:6px;font-size:12px;line-height:18px}
[data-change-icon]{width:13px;height:13px}
\`

export const PIERRE_WORKER_POOL_SIZE = 4`

const FILE_CHANGES = [
  change('website/src/components/DiffBlock.tsx', MONACO_BLOCK_BEFORE, MONACO_BLOCK_AFTER),
  change('website/src/pierre/PierreImpl.tsx', IMPL_BEFORE, IMPL_AFTER),
  change('website/src/utils/diffUtils.ts', UTILS_BEFORE, UTILS_AFTER),
  change('website/src/pages/chat/ToolDetails.tsx', TOOLDETAILS_BEFORE, TOOLDETAILS_AFTER),
  change('website/src/pierre/config.ts', CONFIG_BEFORE, CONFIG_AFTER),
]

const ARTIFACT_PATH = 'website/src/components/DiffBlock.tsx'

/** The row expanded for frame 02 — the biggest diff of the five. */
const EXPAND_PATH = 'website/src/pierre/PierreImpl.tsx'

/* A DEEP path on purpose: the header is supposed to show `DiffBlock.tsx` only
 * (basenamePatchHeaders rewrites the copy Pierre parses), so a regression that
 * leaks the directory chain would be unmissable in the frame. */
const DEEP_DIFF = [
  '```diff',
  '--- a/website/src/components/DiffBlock.tsx',
  '+++ b/website/src/components/DiffBlock.tsx',
  '@@ -1,9 +1,10 @@',
  " import { memo, useState, useMemo } from 'react'",
  "-import { MonacoDiffViewer } from './MonacoDiffViewer'",
  "-import { parseUnifiedDiff } from '../utils/parseUnifiedDiff'",
  "+import { PierrePatch } from '../pierre'",
  "+import { basenamePatchHeaders } from '../utils/diffUtils'",
  "+import { PIERRE_COMPACT_HEADER_CSS } from '../pierre/config'",
  ' ',
  ' export default memo(function DiffBlock({ code, complete }: Props) {',
  '   const [sideBySide, setSideBySide] = useState(false)',
  '@@ -22,7 +23,7 @@',
  '   return (',
  '     <div className="diff-block group/diff rounded-xl border border-border">',
  '-      <MonacoDiffViewer original={parsed.original} modified={parsed.modified} />',
  '+      <PierrePatch patch={displayPatch} options={options} renderHeaderMetadata={headerControls} />',
  '     </div>',
  '   )',
  ' })',
  '```',
].join('\n')

/* Two hunks separated by a wide unchanged stretch (line 12 → line 240), which
 * is what makes Pierre draw its collapsed-region separator row between them.
 * `expandUnchanged: false` in PIERRE_DIFF_DEFAULTS keeps the stretch folded. */
const COLLAPSED_REGION_DIFF = [
  '```diff',
  '--- a/website/src/pierre/config.ts',
  '+++ b/website/src/pierre/config.ts',
  '@@ -8,6 +8,7 @@',
  " import type { BaseDiffOptions, ThemesType } from '@pierre/diffs'",
  ' ',
  "-export const MONACO_THEMES = { dark: 'vs-dark', light: 'vs' }",
  "+export const PIERRE_THEMES: ThemesType = { dark: 'pierre-dark', light: 'pierre-light' }",
  '+',
  "+export const PIERRE_WORKER_POOL_SIZE = 4",
  ' ',
  ' export function pierreThemeType(isDark: boolean) {',
  '@@ -240,8 +242,9 @@',
  ' export const PIERRE_DIFF_DEFAULTS: PierreDiffOptions = {',
  '   ...PIERRE_CODE_DEFAULTS,',
  "   diffStyle: 'unified',",
  "-  diffIndicators: 'gutter',",
  "-  hunkSeparators: 'none',",
  "+  diffIndicators: 'bars',",
  "+  hunkSeparators: 'line-info',",
  "+  lineDiffType: 'word',",
  '   expandUnchanged: false,',
  ' }',
  '```',
].join('\n')

/* KNOWN REGRESSION fixture: `@@` hunks with NO `---`/`+++` file headers.
 * parsePatchFiles has no file section to attach the hunks to, so
 * PierrePatchImpl gets zero files and renders PlainCodeFallback — plain
 * monospace, no file header, and no Open/Split/Copy (those live in the header's
 * metadata slot). Captured on purpose as evidence, not worked around. */
const HEADERLESS_DIFF = [
  '```diff',
  '@@ -14,6 +14,7 @@',
  ' export function usePierreTheme() {',
  '-  const theme = MONACO_THEMES[mode]',
  '+  const theme = PIERRE_THEMES[mode]',
  '+  const type = pierreThemeType(mode === \'dark\')',
  '   return theme',
  ' }',
  '```',
].join('\n')

const t0 = Math.floor(Date.now() / 1000) - 900

/** Slot + transcript pair. One slot per surface keeps each frame's page short,
 *  so an element screenshot is never fighting a virtualized transcript. */
const SURFACES = {
  chips: {
    key: 'chat-pierre-chips',
    title: 'Replace Monaco with Pierre',
    messages: [
      { role: 'user', content: 'Swap the dashboard off Monaco and onto Pierre for every code and diff surface.', ts: String(t0) },
      {
        role: 'assistant',
        ts: String(t0 + 120),
        content: 'Done — the chat diff blocks, the inline file rows and the tool-detail code views all render through Pierre now, and the Monaco worker bundle is gone from the graph.',
        meta: { file_changes: FILE_CHANGES },
      },
    ],
  },
  diff: {
    key: 'chat-pierre-diff',
    title: 'Chat diff block header',
    messages: [
      { role: 'user', content: 'Show me the DiffBlock change as a patch.', ts: String(t0) },
      {
        role: 'assistant',
        ts: String(t0 + 60),
        content: 'Here is the patch — the header renders the basename while the Open button keeps the full path:\n\n' + DEEP_DIFF,
      },
    ],
  },
  region: {
    key: 'chat-pierre-region',
    title: 'Collapsed unchanged region',
    messages: [
      { role: 'user', content: 'And the config defaults?', ts: String(t0) },
      {
        role: 'assistant',
        ts: String(t0 + 60),
        content: 'Two edits, 230 lines apart — Pierre folds the unchanged stretch between them:\n\n' + COLLAPSED_REGION_DIFF,
      },
    ],
  },
  headerless: {
    key: 'chat-pierre-headerless',
    title: 'Headerless patch fallback',
    messages: [
      { role: 'user', content: 'Just the hunk, no file headers.', ts: String(t0) },
      {
        role: 'assistant',
        ts: String(t0 + 60),
        content: 'Hunk only:\n\n' + HEADERLESS_DIFF,
      },
    ],
  },
}

const slots = Object.values(SURFACES).map(s => ({
  key: s.key,
  title: s.title,
  running: false,
  last_message: s.title,
  messages: s.messages.length,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}))

const detailByKey = Object.fromEntries(Object.values(SURFACES).map(s => [
  s.key,
  { running: false, has_more: false, total: s.messages.length, queue: [], messages: s.messages },
]))

// ── Harness ─────────────────────────────────────────────────────────────────

/** PNG width/height straight out of the IHDR chunk — no image dependency. */
function pngSize(path) {
  const b = readFileSync(path)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

/** Chromium resolution lives in ./lib/chromium-executable.mjs (shared across the capture harnesses). */

async function main() {
  const { srv, base } = await serveDist()
  const executablePath = chromiumExecutable()
  console.log('chromium:', executablePath || '(playwright default)')
  const browser = await chromium.launch({ executablePath })
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    // 12-13px header type and 7px diffstat cells render soft at 1x on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  /** Slot list + per-slot detail + the DiffBlock "Open" existence probe. */
  const extra = async (path, route) => {
    if (path === '/api/chat/slots') return json(route, slots), true
    const m = /^\/api\/chat\/slots\/([^/]+)/.exec(path)
    if (m) {
      const d = detailByKey[decodeURIComponent(m[1])]
      if (d) return json(route, d), true
    }
    // DiffBlock HEAD-probes this before it will show the Open button; 200 so the
    // affordance is present in the frame (the point of the basename change is
    // that the SHORT header and the FULL-path Open button coexist).
    if (path === '/api/file-read') return route.fulfill({ status: 200, body: '' }), true
    if (path === '/api/artifacts/session-docs') {
      return json(route, { docs: [{ path: ARTIFACT_PATH }] }), true
    }
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] }), true
    return false
  }

  await stubDashboardApi(page, { slots, extra })
  logPageProblems(page)

  /**
   * Count / read elements matching `sel` ANYWHERE, including inside Pierre's
   * shadow roots.
   *
   * Every Pierre surface renders into a shadow root, so a plain
   * `document.querySelectorAll('[data-diffs-header]')` inside `page.evaluate`
   * returns 0 even when five headers are painted — the first version of this
   * harness logged exactly that and the numbers were pure noise. Playwright's
   * own locators pierce shadow DOM; raw evaluate does not, so this walks the
   * roots explicitly.
   */
  const deepQuery = (sel, root = 'body') => page.evaluate(([sel, rootSel]) => {
    const out = []
    const walk = node => {
      if (!node) return
      out.push(...node.querySelectorAll(sel))
      for (const el of node.querySelectorAll('*')) if (el.shadowRoot) walk(el.shadowRoot)
    }
    walk(document.querySelector(rootSel))
    return out.map(el => ({
      text: (el.textContent || '').trim().slice(0, 70),
      color: getComputedStyle(el).color,
      cursor: getComputedStyle(el).cursor,
    }))
  }, [sel, root])

  const wrote = []

  /** Write an element screenshot and assert the 2000px-per-edge budget. */
  async function shot(locator, name) {
    const file = `${OUT}/${name}.png`
    await locator.screenshot({ path: file })
    const { w, h } = pngSize(file)
    const flag = w > MAX_EDGE || h > MAX_EDGE ? '  ⚠️ OVER 2000px' : ''
    console.log(`wrote ${file}  ${w}x${h}${flag}`)
    wrote.push({ file, w, h, over: !!flag })
  }

  /** Load a surface: seed localStorage (theme + onboarded + active slot) so the
   *  first-run theme modal never mounts over the transcript, then navigate.
   *
   *  The slot is selected by the `?sid=` deep link, which ChatPage activates on
   *  mount — that is deterministic where the localStorage key is not: the
   *  restore key is per-MODE (`mc-active-slot-chat`, see ChatPage's
   *  `slotStorageKey`), so seeding the bare `mc-active-slot` silently does
   *  nothing and every surface renders whichever slot the list happens to
   *  activate first. It is still seeded as a belt-and-braces fallback.
   *
   *  `pinLastPrompt` is turned OFF in the seeded chat config: it defaults ON,
   *  and the pinned-prompt banner floats over the top of the transcript — in an
   *  element screenshot of the chips card it lands on top of the first row and
   *  hides its ±counts. */
  async function load(surface) {
    await page.addInitScript(key => {
      localStorage.clear()
      localStorage.setItem('mc-theme', 'dark')
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot-chat', key)
      localStorage.setItem('mc-chat-config', JSON.stringify({
        pinLastPrompt: false,
        // 'expanded' is the default already; pinned so the card (not the
        // minimal glass pills) is what these frames capture regardless of any
        // future default flip.
        fileChipStyle: 'expanded',
        // No reveal animation to race the screenshot.
        streamMode: 'immediate',
      }))
    }, surface.key)
    await page.goto(base + '/?sid=' + encodeURIComponent(surface.key), { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
    // Escape + any close button, the way the pod-e2e runner does it: belt and
    // braces over the localStorage seed above.
    await page.keyboard.press('Escape')
    const close = page.locator('[aria-label="Close"]')
    if (await close.count()) await close.first().click().catch(() => {})
    await page.waitForTimeout(400)
  }

  // ── Frames 1-3: the file-change chips card ─────────────────────────────────
  await load(SURFACES.chips)
  const card = page.locator('div.ft-block-reveal:has([data-testid^="fcc-row-"])').first()
  await card.waitFor({ state: 'visible', timeout: 15000 })
  // Closed rows are ordinary light-DOM headers. Wait for all five, then
  // confirm the expensive Pierre shadow headers have not mounted.
  await page.waitForFunction(
    n => document.querySelectorAll('[data-testid^="fcc-header-"]').length >= n,
    FILE_CHANGES.length,
    { timeout: 15000 },
  )
  await page.waitForTimeout(300)
  const lightHeaderCount = await page.locator('[data-testid^="fcc-header-"]').count()
  const collapsedPierreHeaders = (await deepQuery('[data-diffs-header]')).length
  if (lightHeaderCount !== FILE_CHANGES.length || collapsedPierreHeaders !== 0) {
    throw new Error(`collapsed mount gate failed: light=${lightHeaderCount}, pierre=${collapsedPierreHeaders}`)
  }
  console.log('DIAG chips rows:', (await page.locator('[data-testid^="fcc-row-"]').count()),
    'lightHeaders:', lightHeaderCount,
    'pierreHeaders:', collapsedPierreHeaders,
    'titles:', JSON.stringify((await deepQuery('[data-fcc-filename]')).map(t => t.text)))
  await shot(card, '01-chips-card-collapsed')

  // Frame 1b: the supported 320px viewport floor. Artifact identity and counts
  // remain visible, while its redundant diffstat cells yield space to a useful
  // filename. Closed rows still perform no Pierre work.
  await page.setViewportSize({ width: 320, height: 900 })
  await page.waitForTimeout(300)
  const narrowLayout = await page.locator(`[data-testid="fcc-row-${ARTIFACT_PATH}"]`).evaluate(row => {
    const filename = row.querySelector('[data-fcc-filename]')
    const metadata = row.querySelector('[data-testid="fcc-metadata"]')
    const secondary = row.querySelector('[data-fcc-secondary-metadata]')
    return {
      filenameWidth: Math.round(filename.getBoundingClientRect().width),
      metadataWidth: Math.round(metadata.getBoundingClientRect().width),
      artifactBadges: row.querySelectorAll('[data-fcc-artifact-badge]').length,
      secondaryDisplay: getComputedStyle(secondary).display,
    }
  })
  const narrowPierreHeaders = (await deepQuery('[data-diffs-header]')).length
  if (
    narrowLayout.filenameWidth < 64
    || narrowLayout.artifactBadges !== 1
    || narrowLayout.secondaryDisplay !== 'none'
    || narrowPierreHeaders !== 0
  ) {
    throw new Error(
      `320px artifact layout gate failed: ${JSON.stringify({ ...narrowLayout, pierreHeaders: narrowPierreHeaders })}`,
    )
  }
  console.log(
    'DIAG 320px layout:',
    JSON.stringify({ ...narrowLayout, pierreHeaders: narrowPierreHeaders }),
  )
  await shot(card, '01b-chips-card-320')
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.waitForTimeout(300)

  // Frame 2: one row expanded — the inline Pierre diff in the transcript.
  await page.locator(`[data-testid="fcc-toggle-${EXPAND_PATH}"]`).click()
  await page.waitForTimeout(1600)
  console.log('DIAG expanded codeEls:', (await deepQuery('[data-code]')).length,
    'rowCodeHead:', JSON.stringify((await deepQuery('pre', `[data-testid="fcc-row-${EXPAND_PATH}"]`))[0]?.text ?? null),
    'pinnedBanner:', await page.locator('[data-testid="pinned-prompt"]').count())
  await shot(card, '02-chip-row-expanded')

  // Frame 3: collapse back to the lightweight header, then hover the native
  // filename control. The accent treatment must survive the Pierre-to-header
  // swap and the diff body must be gone.
  await page.locator(`[data-testid="fcc-toggle-${EXPAND_PATH}"]`).click()
  await page.waitForTimeout(700)
  const hoverRow = page.locator(`[data-testid="fcc-row-${EXPAND_PATH}"]`)
  const title = hoverRow.locator('[data-fcc-filename]').first()
  await title.hover()
  await page.waitForTimeout(500)
  console.log('DIAG hover:', JSON.stringify(
    (await deepQuery('[data-fcc-filename]', `[data-testid="fcc-row-${EXPAND_PATH}"]`))[0]))
  // Crop to the hovered row's header band, so the accent color is the subject
  // rather than a detail inside a 600px-tall card.
  const box = await hoverRow.boundingBox()
  await page.screenshot({
    path: `${OUT}/03-chip-filename-hover.png`,
    clip: { x: box.x - 8, y: box.y - 8, width: box.width + 16, height: 52 },
  })
  {
    const { w, h } = pngSize(`${OUT}/03-chip-filename-hover.png`)
    console.log(`wrote ${OUT}/03-chip-filename-hover.png  ${w}x${h}`)
    wrote.push({ file: `${OUT}/03-chip-filename-hover.png`, w, h, over: w > MAX_EDGE || h > MAX_EDGE })
  }

  // ── Frames 4-5: the chat diff block (basename header, then split) ──────────
  await load(SURFACES.diff)
  const block = page.locator('.diff-block').first()
  await block.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(1500)
  // Hover so the header controls (Open / Split / Copy) are revealed — the Open
  // button carrying the FULL path beside a basename-only header is the story.
  await block.hover()
  await page.waitForTimeout(400)
  // The strongest single assertion for this frame: the header title is the
  // BASENAME while the Open control's label still carries the FULL path.
  console.log('DIAG diff title:', JSON.stringify((await deepQuery('[data-title]', '.diff-block')).map(t => t.text)),
    'openBtnLabel:', JSON.stringify(await page.locator('.diff-block [aria-label*="in side panel"]').first().getAttribute('aria-label').catch(() => null)),
    'separators:', (await deepQuery('[data-separator]', '.diff-block')).length)
  await shot(block, '04-chat-diff-basename')

  const split = page.getByTitle('Split view')
  await split.click()
  await page.waitForTimeout(1500)
  await page.mouse.move(4, 4)
  await page.waitForTimeout(300)
  await block.hover()
  await page.waitForTimeout(300)
  await shot(block, '05-chat-diff-split')

  // ── Frame 6: collapsed unchanged region + separator row ────────────────────
  await load(SURFACES.region)
  const regionBlock = page.locator('.diff-block').first()
  await regionBlock.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(1600)
  console.log('DIAG region separators:', JSON.stringify(
    (await deepQuery('[data-separator]', '.diff-block')).map(s => s.text)))
  await shot(regionBlock, '06-collapsed-unchanged')

  // ── Frame 7: headerless patch → PlainCodeFallback (known regression) ──────
  await load(SURFACES.headerless)
  const hlBlock = page.locator('.diff-block').first()
  await hlBlock.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(1600)
  await hlBlock.hover()
  await page.waitForTimeout(400)
  console.log('DIAG headerless pierreHeaders:', (await deepQuery('[data-diffs-header]', '.diff-block')).length,
    'titles:', JSON.stringify((await deepQuery('[data-title]', '.diff-block')).map(t => t.text)),
    'plainPre:', await page.locator('.diff-block pre.font-mono').count(),
    'openBtn:', await page.locator('.diff-block [aria-label*="in side panel"]').count(),
    'splitBtn:', await page.locator('.diff-block [title="Split view"]').count())
  await shot(hlBlock, '07-headerless-fallback')

  console.log('\n── SUMMARY ─────────────────────────────')
  for (const w of wrote) console.log(`${w.over ? 'OVER' : ' ok '}  ${w.w}x${w.h}  ${w.file}`)
  const over = wrote.filter(w => w.over)
  console.log(over.length ? `FAIL: ${over.length} frame(s) exceed ${MAX_EDGE}px` : `all ${wrote.length} frames within ${MAX_EDGE}px`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
