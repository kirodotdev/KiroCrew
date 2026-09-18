/**
 * Screenshots for #10616: the composer-toolbar pickers (agent, model, project)
 * must stay anchored to their chip when the composer moves under the open menu.
 *
 * Runs the REAL ChatPage off the isolated scroll-shell entry
 * (capture/chatpage-scroll-shell.html) with every /api/** call answered here.
 * For each picker:
 *   1. raise the composer 280px (the page root loses 280px of height, the way
 *      a software keyboard shrinks the visible page), open the picker, shoot
 *      `<picker>-01-raised`;
 *   2. give the height back (the composer drops 280px) and dispatch `resize`
 *      on `window.visualViewport` -- the one signal the iOS keyboard closing
 *      emits and `window` events miss; shoot `<picker>-02-dropped`.
 * Every frame asserts the menu's gap to its chip before it is written, so a
 * frame cannot document the wrong state. `--expect=before` flips the second
 * assertion to the pre-fix geometry (menu stranded 280px above the chip) so
 * the same script photographs the bug off the base commit.
 *
 * Usage (two shells, from website/):
 *   npx vite --host 127.0.0.1 --port 6834 --strictPort
 *   node scripts/capture-toolbar-picker-remeasure.mjs http://127.0.0.1:6834 ../temp-screenshots/toolbar-picker-remeasure [--expect=after|before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const positional = process.argv.slice(2).filter(a => !a.startsWith('--'))
const BASE = positional[0] || 'http://127.0.0.1:6834'
const OUT = positional[1] || '../temp-screenshots/toolbar-picker-remeasure'
const EXPECT = (process.argv.find(a => a.startsWith('--expect=')) || '--expect=after').slice('--expect='.length)
mkdirSync(OUT, { recursive: true })

const RAISE = 280
/** Each picker's chip (its title, as the fixture slot renders it: inherited
 *  default agent, `auto` model, no project) and how to find its open menu. */
const PICKERS = [
  { id: 'agent', chip: /default agent/, menu: (page) => page.getByRole('dialog', { name: 'Agent selector' }), gap: 4 },
  { id: 'model', chip: 'Model: auto', menu: (page) => page.getByRole('dialog', { name: 'Model list' }), gap: 4 },
  { id: 'project', chip: 'Select project', menu: (page) => page.getByRole('option', { name: /proj-x/ }).locator('xpath=ancestor::div[contains(@class,"fixed")][1]'), gap: 4 },
]

const pad = (n) => String(n).padStart(2, '0')
/** Byte-equal to the entry's `short` fixture: the mount refetch REPLACES the store. */
const MESSAGES = Array.from({ length: 16 }, (_, i) => ([
  { role: 'user', content: `Status check #${i + 1}: anything new in the queue?`, cls: '', ts: `2026-08-27T01:${pad(10 + i)}:00Z` },
  { role: 'assistant', content: `Sweep ${i + 1} done.\n\n- two issues triaged as duplicates\n- one PR moved to review-ready\n- CI green on the retry`, cls: '', ts: `2026-08-27T01:${pad(10 + i)}:30Z` },
])).flat().slice(0, 2)

const browser = await chromium.launch()
let failed = false
const check = (name, ok, detail) => { console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`); if (!ok) failed = true }

async function newPage() {
  const page = await browser.newPage({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 1 })
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    const json = (body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) {
      return json({ key: 'slot-a', title: 'scroll-shell fixture', running: false, has_more: false, total: MESSAGES.length, messages: MESSAGES, agent: 'kirocrew', model: 'claude-opus-5', project: '/home/user/proj-x' })
    }
    if (path === '/api/chat/slots') return json([{ key: 'slot-a', title: 'scroll-shell fixture', messages: MESSAGES.length, running: false, mode: '', agent: 'kirocrew', model: 'claude-opus-5', project: '/home/user/proj-x' }])
    if (path === '/api/agents') return json({ agents: [{ name: 'kirocrew' }, { name: 'reviewer' }], default_agent: 'kirocrew' })
    if (path === '/api/models') return json([{ model_name: 'auto', description: 'Models chosen by task' }, { model_name: 'claude-opus-5', description: 'Claude Opus 5' }])
    if (path === '/api/recent-projects') return json({ dirs: ['/home/user/proj-x', '/home/user/proj-y'] })
    if (path === '/api/browse-dirs') return json({ path: '/home/user', parent: '/home', dirs: [] })
    if (/\/api\/chat\/(tags|pins|folders|tag-columns)$/.test(path)) return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    const isList = /commands|skills|sessions|files|history|artifacts|folders|slots$/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/chatpage-scroll-shell.html?theme=dark&scene=short`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('Status check #1:').first().waitFor()
  await page.waitForTimeout(250)
  return page
}

/** Composer height budget: shrinking the capture root lifts the composer. */
const setRaised = (page, raised) => page.evaluate(([px, on]) => {
  document.querySelector('[data-capture-root]').style.height = on ? `calc(100vh - ${px}px)` : '100vh'
}, [RAISE, raised])

/** Gap between the menu's bottom edge and the chip's top edge, in CSS px. */
async function gap(chip, menu) {
  const [c, m] = await Promise.all([chip.boundingBox(), menu.boundingBox()])
  return { gap: c.y - (m.y + m.height), chipTop: c.y, menuBottom: m.y + m.height }
}

for (const p of PICKERS) {
  const page = await newPage()
  const chip = page.getByTitle(p.chip).first()
  await chip.waitFor()

  // 01 — composer raised (keyboard up), picker opened from the raised chip.
  await setRaised(page, true)
  await page.waitForTimeout(100)
  await chip.click()
  const menu = p.menu(page)
  await menu.waitFor()
  await page.waitForTimeout(150)
  const g1 = await gap(chip, menu)
  check(`${p.id}-01-raised: menu sits ${p.gap}px above its chip`, Math.abs(g1.gap - p.gap) < 1, JSON.stringify(g1))
  await page.screenshot({ path: `${OUT}/${p.id}-01-raised.png` })

  // 02 — keyboard closes: composer drops RAISE px; only visualViewport says so.
  await setRaised(page, false)
  await page.evaluate(() => window.visualViewport.dispatchEvent(new Event('resize')))
  await page.waitForTimeout(150)
  const g2 = await gap(chip, menu)
  check(`${p.id}-02-dropped: chip moved down ${RAISE}px`, Math.abs(g2.chipTop - g1.chipTop - RAISE) < 1, `chipTop ${g1.chipTop} -> ${g2.chipTop}`)
  if (EXPECT === 'before') {
    check(`${p.id}-02-dropped BEFORE: menu stranded where the chip was`, Math.abs(g2.gap - (p.gap + RAISE)) < 1, JSON.stringify(g2))
  } else {
    check(`${p.id}-02-dropped AFTER: menu still ${p.gap}px above its chip`, Math.abs(g2.gap - p.gap) < 1, JSON.stringify(g2))
  }
  await page.screenshot({ path: `${OUT}/${p.id}-02-dropped-${EXPECT}.png` })
  await page.close()
}

await browser.close()
if (failed) { console.error('capture: at least one frame did not match its expected state'); process.exit(1) }
console.log(`wrote frames to ${OUT}`)
