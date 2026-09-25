/**
 * Composer hint in NARROW split-view panes (#9016): the long placeholder used to
 * wrap and grow the empty composer a row taller, and now keeps one line with a
 * faded tail. Real built SPA behind the shared static server, /api/** from
 * fixtures. The box's own height is the proof: wrapped is 58px, one line 44.
 *
 * Usage: node scripts/capture-chatinput-placeholder-narrow.mjs [outDir] [prefix]
 *   prefix 'before' overrides the rule away and inverts the assertions, so the
 *   pair a UI PR needs comes off one build.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems } from './lib/stub-dashboard-api.mjs'
import { stubSplitPanes } from './lib/split-pane-fixture.mjs'

const OUT = process.argv[2] || '../temp-screenshots/chatinput-placeholder-narrow'
const PREFIX = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })

const [A, B] = ['pane-a', 'pane-b']
const ONE_LINE_H = 44 // min-h-[44px] on the textarea
const INPUT = '[data-chat-pane] textarea[data-composer-input]'
// A stylesheet, not a class edit: React owns className and restores it on render.
const DEFEAT = `${INPUT}::placeholder { white-space: pre-wrap !important; overflow: visible !important; mask-image: none !important; -webkit-mask-image: none !important }`
const slot = (key, title) => ({ key, title, messages: 1, running: false, agent: 'kirocrew', created: '2026-09-24T09:00:00Z', last_ts: '2026-09-25T08:00:00Z', folder_id: '' })
const reply = (mid, content) => [{ role: 'assistant', content, ts: '2026-09-25T08:00:00Z', meta: { mid } }]
// `dir: 'col'` tiles left→right (SessionGridLayout), which is what squeezes a
// composer; 1100px then puts each one under the hint's own width.
const LAYOUT = { [A]: { type: 'split', id: 'sp', dir: 'col', sizes: [0.5, 0.5], children: [{ type: 'leaf', id: 'l1', kind: 'session', slot: A }, { type: 'leaf', id: 'l2', kind: 'session', slot: B }] } }

/** What one composer's own box says about the hint it is showing. */
const readComposer = (ta) => ta.evaluate((el, floor) => {
  const cs = getComputedStyle(el)
  const ph = getComputedStyle(el, '::placeholder')
  const g = document.createElement('canvas').getContext('2d')
  g.font = cs.font
  return {
    box: Math.round(el.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)),
    hint: Math.round(g.measureText(el.placeholder).width),
    height: el.clientHeight, sliced: el.scrollHeight > el.clientHeight,
    oneLine: el.clientHeight === floor && ph.whiteSpace === 'nowrap' && (ph.maskImage || '').startsWith('linear-gradient'),
  }
}, ONE_LINE_H)

/** Re-enter the auto-size measurement: its memo keys on the element's own
 *  inputs, so a stylesheet edit alone never re-measures. Every pane needs it —
 *  one left alone keeps the one-line height and photographs a sliced row. */
async function remeasure(page, ta) {
  await ta.focus()
  await page.keyboard.type('x')
  await page.keyboard.press('Backspace')
  await ta.blur()
  await page.waitForTimeout(250)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const page = await (await browser.newContext({ viewport: { width: 1100, height: 760 }, deviceScaleFactor: 2 })).newPage()
  await stubSplitPanes(page, {
    slots: [slot(A, 'Design notes'), slot(B, 'Release checklist')],
    transcripts: { [A]: reply('a1', 'Option A keeps the sidebar fixed at every width.'), [B]: reply('b1', 'Option B collapses it under 900px.') },
    layout: LAYOUT,
  })
  logPageProblems(page)
  await page.goto(`${base}/chat/${A}`, { waitUntil: 'domcontentloaded' })
  const composers = page.locator(INPUT)
  await composers.first().waitFor({ timeout: 20_000 })
  const panes = await composers.count()
  if (panes !== 2) throw new Error(`expected 2 pane composers (split view), got ${panes} — a single-chat frame proves nothing`)
  if (PREFIX === 'before') await page.addStyleTag({ content: DEFEAT })
  for (let i = 0; i < panes; i++) {
    if (PREFIX === 'before') await remeasure(page, composers.nth(i))
    const m = await readComposer(composers.nth(i))
    if (m.hint <= m.box) throw new Error(`pane ${i}: the hint already fits (${m.hint} <= ${m.box}px) — this width proves nothing`)
    if (m.sliced) throw new Error(`pane ${i}: content taller than the box (${JSON.stringify(m)}) — the frame would show a sliced row`)
    if (PREFIX === 'before' ? m.oneLine : !m.oneLine) throw new Error(`pane ${i} is not the ${PREFIX} state: ${JSON.stringify(m)}`)
    console.log(`ok  pane ${i} ${JSON.stringify(m)}`)
  }
  await page.screenshot({ path: `${OUT}/${PREFIX}-1-narrow-panes.png` })
  const box = await composers.first().boundingBox()
  await page.screenshot({ path: `${OUT}/${PREFIX}-2-composer-closeup.png`, clip: { x: box.x - 8, y: box.y - 8, width: box.width + 16, height: box.height + 16 } })
  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}/${PREFIX}-{1-narrow-panes,2-composer-closeup}.png`)
}

main().catch(err => { console.error(err); process.exit(1) })
