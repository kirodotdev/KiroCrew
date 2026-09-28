/**
 * Screenshot + measurement harness for issue #12480: Mermaid node and edge-label
 * text clipped because the labels were MEASURED in the fallback face and PAINTED
 * in the swap-loaded body face.
 *
 * The body face (Space Grotesk, Google Fonts, `display=swap`) arrives over the
 * network. A diagram drawn while that load is in flight has every label box
 * sized for the fallback glyphs; when the face lands, `swap` repaints the text
 * in it -- the SVG's <foreignObject> labels included -- but the boxes keep their
 * measured widths, and every long label is cut at its right edge.
 *
 * The harness reproduces the cold-load timing deterministically: it runs the
 * REAL built SPA through the shared transcript harness (every /api/** call
 * answered from fixtures, no gateway, no token) and DELAYS one of the two things
 * the face needs, chosen with `--hold`:
 *
 *   --hold files (default)  the Google Fonts stylesheet loads normally and the
 *                           font FILES (`fonts.gstatic.com`) arrive late -- the
 *                           face is declared and pending when the transcript
 *                           renders, so `document.fonts.ready` is unsettled
 *   --hold css              the STYLESHEET (`fonts.googleapis.com/css2`) arrives
 *                           late -- no face is declared when the transcript
 *                           renders, `ready` is already settled, and the face is
 *                           declared, loaded and swapped in only afterwards.
 *                           The realistic shape for a dashboard served from a
 *                           local gateway where the font origin is the one slow
 *                           resource
 *
 * It then measures every label: the <foreignObject> box mermaid sized against
 * the width of the text painted inside it. A label whose text is wider than its
 * box is clipped. A second, warm pass reloads the page with the face cached.
 *
 * Frames (in outDir):
 *   01-<label>-cold.png   the diagram after the face landed, cold load
 *   02-<label>-warm.png   the same diagram on a warm reload
 *   report-<label>.json   per-label widths, face statuses, timings
 *
 * Point two runs at two builds to get a before/after pair:
 *   node scripts/capture-mermaid-fonts-ready.mjs <outDir> --label before --dist <old dist> --expect clipped
 *   node scripts/capture-mermaid-fonts-ready.mjs <outDir> --label after  --expect clean
 * `--expect` makes the run fail when the cold pass does not show that outcome,
 * so a stale bundle fails the run rather than producing a frame of the old UI.
 * `--font-delay-ms` (default 4000) is how late the held resource arrives.
 */
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const argv = process.argv.slice(2)
const flag = (name, fallback) => {
  const i = argv.indexOf(name)
  return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1] : fallback
}
const positional = []
for (let i = 0; i < argv.length; i++) {
  if (argv[i].startsWith('--')) { i++; continue } // a flag and its value
  positional.push(argv[i])
}
const OUT = positional[0] || process.env.PROBE_OUT || '../temp-screenshots/mermaid-fonts-12480'
const LABEL = flag('--label', 'run')
const DIST = flag('--dist', undefined)
const EXPECT = flag('--expect', undefined) // 'clipped' | 'clean' | undefined
const HOLD = flag('--hold', 'files') // 'files' | 'css'
const FONT_DELAY_MS = Number(flag('--font-delay-ms', '4000'))
const PROJECT = '/home/user/workspace/KiroCrew'
if (HOLD !== 'files' && HOLD !== 'css') throw new Error(`--hold must be files or css, got ${HOLD}`)

mkdirSync(OUT, { recursive: true })

// The report's reproduction, verbatim.
const MERMAID_SOURCE = [
  'flowchart TB',
  '  subgraph ACQ["Acquisition and assembly"]',
  '    C0["cameras 0..3 L/W"] -->|"/cameras/N/images"| SYNC["Synchronizer"]',
  '    C4["camera 4 height"] -->|"/cameras/4/images"| SYNC',
  '    C4 -->|"/cameras/4/images"| DET["ItemDetector"]',
  '    SYNC -->|"/synchronized_frames"| ASM["Assembler"]',
  '    DET -->|"/item_signal"| ASM',
  '  end',
  '  ACQ -->|"/item_pack"| OUT',
  '  subgraph OUT["Measurement and output"]',
  '    IP["ItemPack"] --> P["Processor core: decides which algorithm to run"]',
  '    STORE[("Config Store: file IO, cache, hot reload")] -.->|"Bundle"| P',
  '    P -->|"4 L/W frames + 4 LUT + params"| LW["LWMeasurer: pure function, returns D1 and D2"]',
  '    P -->|"cam4 frames + cam4 LUT"| H["HeightMeasurer: pure function, returns D3"]',
  '    LW --> P',
  '    H --> P',
  '    P -->|"Row"| SINK["Sinks: SSE / csv / jsonl / png"]',
  '    P -->|"/measurement"| GW["Gateway: HTTP/SSE to Web"]',
  '  end',
].join('\n')

const now = Math.floor(Date.now() / 1000)
const slot = 'chat-mermaid-12480'
const title = 'Mermaid label widths'
const md = ['The measurement pipeline, end to end:', '', '```mermaid', MERMAID_SOURCE, '```'].join('\n')
const slots = [{
  key: slot, title, running: false, last_message: 'Drew the pipeline.',
  messages: 2, agent: 'kirocrew', project: PROJECT, updated: now,
}]
const detail = {
  key: slot, title, agent: 'kirocrew', project: PROJECT, running: false,
  messages: [
    { role: 'user', content: 'Draw the measurement pipeline.', ts: now - 60 },
    { role: 'assistant', content: md, ts: now - 30 },
  ],
}

const harness = await openTranscriptHarness({
  slot, project: PROJECT, slots, detail,
  viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2, dist: DIST,
})
const { page } = harness

const timeline = []
const t0 = Date.now()
const mark = (what) => { timeline.push({ ms: Date.now() - t0, what }); console.log(`  +${Date.now() - t0}ms ${what}`) }

// `files`: the stylesheet comes through untouched so the @font-face rules exist
// from the start and only the font FILES are late -- the shape a slow connection
// has when the CSS is a few KB and preloaded and the woff2 files are not.
// `css`: the stylesheet is the late one, so no face exists until it lands.
const FONT_FILES = /fonts\.gstatic\.com/
const FONT_CSS = /fonts\.googleapis\.com\/css/
const HELD = HOLD === 'css' ? FONT_CSS : FONT_FILES
await page.route(HELD, async route => {
  mark(`${HOLD === 'css' ? 'stylesheet' : 'font file'} requested, holding ${FONT_DELAY_MS}ms: ${new URL(route.request().url()).pathname.slice(-24)}`)
  await new Promise(r => setTimeout(r, FONT_DELAY_MS))
  await route.continue()
})
page.on('requestfinished', req => {
  if (FONT_CSS.test(req.url())) mark('stylesheet landed')
  if (FONT_FILES.test(req.url())) mark('font file landed')
})

/** Poll for the rendered diagram and say when it appeared relative to the fonts. */
async function waitForDiagram(timeoutMs = 45000) {
  const svg = page.locator('figure svg[aria-roledescription]').first()
  await svg.waitFor({ state: 'attached', timeout: timeoutMs })
  mark('diagram SVG in the DOM')
  // Tagged so a later pass can tell a redraw (a new SVG element) from the first.
  await svg.evaluate(el => el.setAttribute('data-harness-first-draw', '1'))
  return svg
}

/** Resolves once the font FILE has landed -- in `css` mode that is after the
 *  late stylesheet declared the face and the browser fetched it. */
function faceLanded(timeoutMs = FONT_DELAY_MS + 20000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('the face never landed -- is the font origin reachable?')), timeoutMs)
    page.on('requestfinished', function onDone(req) {
      if (!FONT_FILES.test(req.url())) return
      clearTimeout(timer)
      page.off('requestfinished', onDone)
      resolve(undefined)
    })
  })
}

async function fontsSettled() {
  const status = await page.evaluate(() => document.fonts.ready.then(() => document.fonts.status))
  mark(`document.fonts.ready settled (status=${status})`)
  // The swap repaint follows the load on the next frame, and a redraw the block
  // starts on `loadingdone` takes a render; give both a moment.
  await page.waitForTimeout(1500)
}

/** Every label's box (the <foreignObject> mermaid sized from its measurement)
 *  against the text painted in it. Overflow > 0.5px means the text is clipped
 *  at the box's edge -- foreignObject clips by default. */
async function measure() {
  return page.evaluate(() => {
    const svg = document.querySelector('figure svg[aria-roledescription]')
    if (!svg) return { error: 'no diagram' }
    const rows = Array.from(svg.querySelectorAll('foreignObject')).map(fo => {
      const span = fo.querySelector('span.nodeLabel, span.edgeLabel') || fo.querySelector('div')
      const box = fo.getBoundingClientRect().width
      const text = span ? span.getBoundingClientRect().width : 0
      return {
        text: (span?.textContent || '').trim(),
        boxPx: Math.round(box * 10) / 10,
        textPx: Math.round(text * 10) / 10,
        overflowPx: Math.round((text - box) * 10) / 10,
      }
    })
    const faces = Array.from(document.fonts)
      .filter(f => /space grotesk/i.test(f.family))
      .map(f => `${f.family.replace(/["']/g, '')} ${f.weight}: ${f.status}`)
    return {
      bodyFontFamily: getComputedStyle(document.body).fontFamily,
      spaceGroteskFaces: faces,
      fontsStatus: document.fonts.status,
      redrawnAfterFirstDraw: !svg.hasAttribute('data-harness-first-draw'),
      labels: rows,
      clipped: rows.filter(r => r.overflowPx > 0.5),
    }
  })
}

const shot = async (locator, name) => {
  await locator.evaluate(el => el.scrollIntoView({ block: 'center' }))
  await page.waitForTimeout(300)
  await locator.screenshot({ path: join(OUT, name) })
  console.log('captured', name)
}

const report = { label: LABEL, dist: DIST ?? 'website/dist', hold: HOLD, fontDelayMs: FONT_DELAY_MS }

// ── Cold load: the face lands AFTER the transcript rendered ─────────────────
console.log(`[${LABEL}] cold load, ${HOLD === 'css' ? 'stylesheet' : 'font files'} delayed ${FONT_DELAY_MS}ms`)
const landed = faceLanded()
await harness.load('dark', { selector: 'figure', settle: 100 })
mark('page loaded (figure present)')
await waitForDiagram()
const diagramAtMs = timeline.at(-1).ms
await landed
await fontsSettled()
report.cold = { diagramAppearedMs: diagramAtMs, ...(await measure()), timeline: [...timeline] }
if (report.cold.error) throw new Error(`cold: ${report.cold.error}`)
if (report.cold.labels.length === 0) throw new Error('cold: the diagram has no labels -- stale bundle or mermaid did not resolve?')
console.log(`[${LABEL}] cold: ${report.cold.clipped.length} of ${report.cold.labels.length} labels clipped;`,
  `redrawn after the first draw: ${report.cold.redrawnAfterFirstDraw};`,
  `faces: ${report.cold.spaceGroteskFaces.join(', ') || 'none declared'}`)
for (const r of report.cold.clipped) console.log(`    clipped: "${r.text}" text ${r.textPx}px > box ${r.boxPx}px (+${r.overflowPx}px)`)
const figure = page.locator('figure').first()
await shot(figure, `01-${LABEL}-cold.png`)

// ── Warm reload: the face is in the browser cache ───────────────────────────
await page.unroute(HELD)
timeline.length = 0
console.log(`[${LABEL}] warm reload`)
await page.reload({ waitUntil: 'domcontentloaded' })
await page.waitForSelector('figure', { timeout: 20000 })
await waitForDiagram()
await fontsSettled()
report.warm = { ...(await measure()), timeline: [...timeline] }
console.log(`[${LABEL}] warm: ${report.warm.clipped.length} of ${report.warm.labels.length} labels clipped`)
for (const r of report.warm.clipped) console.log(`    clipped: "${r.text}" text ${r.textPx}px > box ${r.boxPx}px (+${r.overflowPx}px)`)
await shot(page.locator('figure').first(), `02-${LABEL}-warm.png`)

writeFileSync(join(OUT, `report-${LABEL}.json`), JSON.stringify(report, null, 2))
await harness.close()

if (EXPECT === 'clipped' && report.cold.clipped.length === 0) {
  console.error(`[${LABEL}] expected the cold load to CLIP labels and it did not -- is the face loading at all?`)
  process.exit(1)
}
if (EXPECT === 'clean' && report.cold.clipped.length > 0) {
  console.error(`[${LABEL}] expected the cold load to be CLEAN and ${report.cold.clipped.length} labels are clipped -- stale bundle?`)
  process.exit(1)
}
