/**
 * Screenshots of the surfaces whose colors moved from raw palette classes and
 * hex literals to theme tokens when `shadcn/no-raw-colors` went blocking.
 *
 * Drives the isolated capture entry (website/capture/design-token-classes.html),
 * which mounts the REAL LogEntry, CollapsibleToolGroup and DagView in the
 * states the change touches.
 *
 * Each frame ASSERTS the state before writing the file, so a frame cannot
 * silently document the wrong build:
 *   after  (default): the manual-trigger pill carries `bg-accent-subtle`, the
 *     approval dot `bg-warn`, no DagView `<rect>`/`<circle>` carries a `#hex`
 *     fill or stroke, and the approval-pending node shows its warn ring and
 *     both Approve / Deny buttons.
 *   --before: the pill carries `bg-purple-100`, the dot `bg-amber-400`, and
 *     at least one DagView shape carries a `#hex` color (the pre-change
 *     checkout).
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort   # in another shell
 *   node scripts/capture-design-token-classes.mjs http://127.0.0.1:6841 ../temp-screenshots/design-token-classes [--before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/design-token-classes'
const BEFORE = process.argv.includes('--before')
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { name: 'design-token-classes-dark', theme: 'dark' },
  { name: 'design-token-classes-light', theme: 'light' },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 760, height: 900 }, deviceScaleFactor: 2 })

let failed = false
for (const s of SCENES) {
  await page.goto(`${BASE}/capture/design-token-classes.html?theme=${s.theme}`)
  await page.waitForSelector('[data-capture-section="dag-view"] svg rect')
  // Pulsing dots are animated; hold them at full opacity so the frame is deterministic.
  await page.addStyleTag({ content: '*, *::before, *::after { animation: none !important; transition: none !important; }' })

  const pillClass = await page
    .locator('[data-capture-section="log-entry"] span', { hasText: /^manual$/ })
    .first()
    .getAttribute('class')
  const dotClasses = await page
    .locator('[data-capture-section="tool-group"] span[aria-label] > span.relative')
    .evaluateAll(els => els.map(e => e.className))
  const hexShapes = await page
    .locator('[data-capture-section="dag-view"] svg rect, [data-capture-section="dag-view"] svg circle, [data-capture-section="dag-view"] svg polygon, [data-capture-section="dag-view"] svg stop')
    .evaluateAll(els => els.filter(e => /#[0-9a-f]{3,6}\b/i.test((e.getAttribute('fill') || '') + (e.getAttribute('stroke') || '') + (e.getAttribute('stop-color') || ''))).length)
  const nodeCount = await page.locator('[data-capture-section="dag-view"] svg g.cursor-pointer').count()
  // The approval controls exist only for a running node with a pending approval.
  const approveButtons = await page.locator('[data-capture-section="dag-view"] svg foreignObject button').count()
  // The approval ring is the only warn-stroked rect drawn at 3px (node borders are 2 / 2.5).
  const warnRings = await page.locator('[data-capture-section="dag-view"] svg rect[stroke="var(--warn)"][stroke-width="3"]').count()

  const ok = BEFORE
    ? /bg-purple-100/.test(pillClass || '') && dotClasses.some(c => /bg-amber-400/.test(c)) && hexShapes > 0 && nodeCount === 6
    : /bg-accent-subtle/.test(pillClass || '') && dotClasses.some(c => /bg-warn/.test(c)) && hexShapes === 0 && nodeCount === 6 && approveButtons === 2 && warnRings === 1
  console.log(`${s.name}${BEFORE ? ' (before)' : ''}: pill=${JSON.stringify(pillClass)} dots=${JSON.stringify(dotClasses)} hexShapes=${hexShapes} nodes=${nodeCount} approveButtons=${approveButtons} warnRings=${warnRings} ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) { failed = true; continue }

  const suffix = BEFORE ? '-before' : ''
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${s.name}${suffix}.png` })
}

await browser.close()
process.exit(failed ? 1 : 0)
