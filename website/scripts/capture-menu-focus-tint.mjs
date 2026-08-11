/**
 * Screenshot + assertion harness for the file panel's ⋯ menu row tint.
 *
 * The menu follows the WAI-ARIA menu pattern: opening it moves real DOM focus
 * onto the first row. The tint that marks the focused row is therefore on
 * screen before the pointer has gone anywhere near the menu, so it must be
 * scoped to `:focus-visible` — which a script-moved focus matches only when a
 * keypress moved it. A bare `:focus` tint is the same colour as `:hover`, so a
 * pointer user reads the first row as selected for as long as the menu is open,
 * and sees two rows lit at once the moment they hover another.
 *
 * `:focus-visible` after a programmatic `.focus()` is a browser heuristic, so a
 * unit test cannot prove this fix works — only a real browser can. That is what
 * this harness is for, and why it ASSERTS computed backgrounds rather than only
 * photographing them:
 *
 *   1. pointer-opened  → roving focus still lands on row 0, and NO row is lit.
 *   2. pointer + hover  → exactly one row is lit, and it is the hovered one.
 *   3. keyboard-opened → row 0 IS lit, so keyboard users keep their indicator.
 *   4. keyboard + Down  → the lit row follows focus down the list.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures (gateway-free — no
 * kiro-cli, no credentials).
 *
 * Usage: node scripts/capture-menu-focus-tint.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/menu-focus-tint'
const SLOT = 'panel-menu-focus-tint'
const DOC_PATH = '/home/builder/project/docs/release-notes.md'
const DOC = [
  '# Release notes',
  '',
  'The ⋯ menu opens with keyboard focus on its first row, so the row tint has',
  'to distinguish "focused because you arrowed here" from "hovered".',
  '',
  '- A pointer user should see no row lit until they hover one.',
  '- A keyboard user should always see the row their focus is on.',
  '',
].join('\n')

/** A row counts as lit when it paints a background of its own. */
const UNLIT = 'rgba(0, 0, 0, 0)'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Release notes review',
  running: false,
  messages: 4,
  agent: 'kirocrew',
  modified: Math.floor(Date.now() / 1000),
  folder_id: '',
}]

/** File-read + library routes the panel and its ⋯ menu fetch on open.
 *
 * Must return TRUTHY to claim the route: the stub awaits this hook's result and
 * falls through to its own handler otherwise, and `route.fulfill()` resolves to
 * `undefined` — so returning it directly double-fulfils and throws.
 */
const extra = async (path, route) => {
  if (path.startsWith('/api/file-read')) {
    await route.fulfill({ status: 200, contentType: 'text/plain', body: DOC })
    return true
  }
  if (path.startsWith('/api/knowledge/sources')) { await json(route, { sources: [] }); return true }
  if (path.startsWith('/api/knowledge/config')) {
    await json(route, { enabled: true, supported_formats: ['.md', '.txt', '.pdf'] })
    return true
  }
  if (path.startsWith('/api/artifacts')) { await json(route, { artifacts: [] }); return true }
  return false
}

/** Per-row label, computed background, and whether it holds DOM focus. */
const readRows = page => page.evaluate(() => {
  const menu = [...document.querySelectorAll('[role="menu"]')].pop()
  return [...menu.querySelectorAll('[role="menuitem"]')].map(el => ({
    label: (el.textContent || '').trim(),
    bg: getComputedStyle(el).backgroundColor,
    focused: el === document.activeElement,
  }))
})

async function shoot(page, name, menu, width, height) {
  const box = await menu.boundingBox()
  const x = Math.max(0, box.x - 48)
  const y = Math.max(0, box.y - 56)
  await page.screenshot({
    path: `${OUT}/${name}.png`,
    clip: { x, y, width: width - x, height: Math.min(height - y, box.height + 80) },
  })
  console.log('wrote', `${OUT}/${name}.png`)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const width = 1400
  const height = 900
  const failures = []

  for (const theme of ['light', 'dark']) {
    const context = await browser.newContext({
      viewport: { width, height },
      deviceScaleFactor: 2, // 13px menu type renders soft at 1x
    })
    const page = await context.newPage()
    logPageProblems(page)
    // The stub's init script clears localStorage, so every seed below has to be
    // registered AFTER it.
    await stubDashboardApi(page, { slots, theme, extra })
    await page.addInitScript(([slot, docPath, doc]) => {
      localStorage.setItem('mc-active-slot', slot)
      localStorage.setItem('mc-activity-open:' + slot, 'true')
      localStorage.setItem('mc-privacy-notice-v1', '1')
      localStorage.setItem('mc-lang', 'en')
      // `loadPersisted` reads tabs verbatim, so a seeded `content` renders
      // without a fetch (the strip happens on WRITE, in serializeBucket).
      localStorage.setItem('mc-panel-tabs:' + slot, JSON.stringify({
        activeId: 'doc',
        tabs: [{ id: 'doc', kind: 'file', title: 'release-notes.md', path: docPath, content: doc }],
      }))
    }, [SLOT, DOC_PATH, DOC])

    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
    await page.getByText('release-notes.md').first().waitFor({ state: 'visible', timeout: 15000 })

    const fail = msg => failures.push(`${theme}: ${msg}`)
    const lit = rows => rows.filter(r => r.bg !== UNLIT)

    // `pages.chatSidebar.more_options` renders the SAME label on the sidebar
    // header and on every session row, so the accessible name is ambiguous on a
    // chat page. The panel's trigger carries a testid for exactly this reason.
    const trigger = page.locator('[data-testid="markdown-panel-more-options"]')
    await trigger.first().waitFor({ state: 'visible', timeout: 15000 })

    // ---- 1. Pointer-opened: focus moves in, nothing is lit. ----------------
    await trigger.click()
    const menu = page.locator('[role="menu"]').last()
    await menu.waitFor({ state: 'visible', timeout: 10000 })
    await page.mouse.move(12, 12) // park the pointer off the menu
    await page.waitForTimeout(400) // the roving-focus timeout has to land first

    let rows = await readRows(page)
    console.log(`${theme} pointer-open:`, JSON.stringify(rows.map(r => [r.label, r.bg, r.focused])))
    // Non-vacuity: a menu that rendered no rows would pass "nothing is lit".
    if (rows.length < 5) fail(`expected the full menu, got ${rows.length} rows`)
    if (rows[0]?.label !== 'Refresh') fail(`expected Refresh first, got ${rows[0]?.label}`)
    if (!rows[0]?.focused) fail('roving focus no longer lands on the first row')
    if (lit(rows).length) fail(`pointer-opened menu lit ${lit(rows).map(r => r.label).join(', ')}`)
    await shoot(page, `01-pointer-open-${theme}`, menu, width, height)

    // ---- 2. Pointer + hover: exactly the hovered row is lit. ---------------
    await menu.getByText('Copy path', { exact: true }).hover()
    await page.waitForTimeout(200)
    rows = await readRows(page)
    console.log(`${theme} pointer-hover:`, JSON.stringify(lit(rows).map(r => r.label)))
    if (lit(rows).length !== 1) fail(`hover lit ${lit(rows).length} rows, want 1`)
    else if (lit(rows)[0].label !== 'Copy path') fail(`hover lit ${lit(rows)[0].label}`)
    await shoot(page, `02-pointer-hover-${theme}`, menu, width, height)

    // ---- 3. Keyboard-opened: the focused row IS lit. -----------------------
    await page.keyboard.press('Escape')
    await page.waitForTimeout(200)
    // Park the pointer off the menu first: it is still physically over the row
    // hovered above, and re-opening would put that row back under it — a second
    // lit row that is a real hover, not a focus tint.
    await page.mouse.move(12, 12)
    await trigger.focus()
    await page.keyboard.press('Enter') // a keypress opens it, so focus is visible
    await menu.waitFor({ state: 'visible', timeout: 10000 })
    await page.waitForTimeout(400)
    rows = await readRows(page)
    console.log(`${theme} keyboard-open:`, JSON.stringify(lit(rows).map(r => r.label)))
    if (!rows[0]?.focused) fail('keyboard-opened menu did not focus the first row')
    if (lit(rows).length !== 1 || lit(rows)[0]?.label !== 'Refresh') {
      fail(`keyboard-opened menu lit ${JSON.stringify(lit(rows).map(r => r.label))}, want ["Refresh"]`)
    }
    await shoot(page, `03-keyboard-open-${theme}`, menu, width, height)

    // ---- 4. Keyboard ArrowDown: the lit row follows focus. -----------------
    await page.keyboard.press('ArrowDown')
    await page.waitForTimeout(200)
    rows = await readRows(page)
    console.log(`${theme} keyboard-down:`, JSON.stringify(lit(rows).map(r => r.label)))
    if (!rows[1]?.focused) fail('ArrowDown did not move focus to the second row')
    if (lit(rows).length !== 1 || lit(rows)[0]?.label !== rows[1]?.label) {
      fail(`after ArrowDown lit ${JSON.stringify(lit(rows).map(r => r.label))}, want ${JSON.stringify([rows[1]?.label])}`)
    }
    await shoot(page, `04-keyboard-arrowdown-${theme}`, menu, width, height)

    await context.close()
  }

  await browser.close()
  srv.close()

  if (failures.length) {
    console.error('FAIL\n  ' + failures.join('\n  '))
    process.exit(1)
  }
  console.log('OK: pointer-opened menus paint nothing; keyboard focus stays visible')
}

main().catch(err => { console.error(err); process.exit(1) })
