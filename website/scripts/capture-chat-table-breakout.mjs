import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
import { chromium } from 'playwright'

/** Which check groups a run executes.
 *  - default: table geometry AND the MCP full-screen overlay.
 *  - `--expect-bug`: the pre-breakout table baseline only (that source has no
 *    query container, so the overlay baseline does not apply to it).
 *  - `--expect-bug --overlay-only`: the overlay baseline only, against index.css
 *    with a `.chat-container` query container re-added.
 *  - `--overlay-only`: the overlay checks only. */
export function selectModes(argv) {
  const expectBug = argv.includes('--expect-bug')
  const overlayOnly = argv.includes('--overlay-only')
  return { expectBug, tables: !overlayOnly, overlay: overlayOnly || !expectBug }
}

if (process.argv.includes('--self-test')) {
  assert.deepEqual(selectModes([]), { expectBug: false, tables: true, overlay: true })
  assert.deepEqual(selectModes(['--expect-bug']), { expectBug: true, tables: true, overlay: false })
  assert.deepEqual(selectModes(['--expect-bug', '--overlay-only']), { expectBug: true, tables: false, overlay: true })
  assert.deepEqual(selectModes(['--overlay-only']), { expectBug: false, tables: false, overlay: true })
  console.log('Mode selection self-test passed.')
  process.exit(0)
}

const base = process.argv[2]
const out = process.argv[3]
assert(base && out, 'Usage: node scripts/capture-chat-table-breakout.mjs <loopback URL> <output dir> [--expect-bug] [--overlay-only] | --self-test')
const { expectBug, tables, overlay } = selectModes(process.argv)
mkdirSync(out, { recursive: true })
const { LD_LIBRARY_PATH: _ld, ...env } = process.env
const browser = await chromium.launch({ env })
const results = []
try {
  for (const host of tables ? ['sdk', 'main'] : []) {
  for (const theme of ['light', 'dark']) {
    const page = await browser.newPage({ viewport: { width: 1500, height: 900 }, deviceScaleFactor: 1 })
    await page.goto(`${base}/capture/chat-table-breakout.html?theme=${theme}&host=${host}`, { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-role="assistant"] table')
    // Resize the SAME document, so responsive geometry cannot rely on remounting.
    for (const width of [1500, 1100, 768, 390, 320, 1500]) {
      await page.setViewportSize({ width, height: 900 })
      await page.waitForTimeout(100)
      const m = await page.evaluate(() => {
        const scroller = document.querySelector('.chat-container')
        const assistant = scroller.querySelector('[data-role="assistant"]')
        const table = assistant.querySelector('table')
        const wrapper = table.parentElement
        const outer = wrapper.parentElement
        const para = assistant.querySelector('p')
        const nested = assistant.querySelector('blockquote [data-testid="markdown-table"]')
        const rect = el => { const r = el.getBoundingClientRect(); return { x: r.x, right: r.right, width: r.width } }
        wrapper.scrollLeft = 100000
        const scrolls = wrapper.scrollLeft > 0
        wrapper.scrollLeft = 0
        return {
          farmHeight: scroller.querySelector('[data-farm-fixture] > div').offsetHeight,
          liveHeight: scroller.querySelector('[data-live-fixture] > div').offsetHeight,
          scroller: rect(scroller), clientWidth: scroller.clientWidth,
          paneScrollWidth: scroller.scrollWidth, table: rect(outer), prose: rect(para),
          nested: rect(nested), quote: rect(nested.closest('blockquote')),
          userTable: rect(scroller.querySelector('[data-role="user"] table').parentElement),
          user: rect(scroller.querySelector('[data-role="user"] .message-bubble')),
          composer: rect(document.querySelector('[data-composer-fixture]')),
          scrolls, tableScrollWidth: wrapper.scrollWidth, tableClientWidth: wrapper.clientWidth,
          codeWordBreak: getComputedStyle(table.querySelector('code')).wordBreak,
          ancestors: (() => { const a = []; for (let e = outer.parentElement; e && e !== scroller; e = e.parentElement) { const style = getComputedStyle(e); if (['hidden', 'clip', 'auto', 'scroll'].includes(style.overflowX)) a.push({ ...rect(e), className: e.className, overflow: style.overflowX }) } return a })(),
        }
      })
      assert(Math.abs(m.prose.width - (Math.min(m.clientWidth, 800) - 32)) < 1, 'Prose width changed')
      assert(Math.abs(m.composer.width - Math.min(m.scroller.width, 800)) < 1, 'Composer width changed')
      assert(m.paneScrollWidth <= m.clientWidth + 1, 'Transcript scrolls horizontally')
      assert(m.nested.width <= m.quote.width + 1, 'Nested table escaped its quotation')
      assert(m.userTable.width <= m.user.width + 1, 'User table escaped its bubble')
      if (expectBug) {
        assert(Math.abs(m.table.width - m.prose.width) < 1, 'Baseline must confine the table to prose width')
      } else {
        assert(Math.abs(m.table.width - (m.clientWidth - 32)) < 1, 'Table must fill the pane minus its gutters')
        assert(Math.abs(m.table.x - (m.scroller.x + 16)) < 1, 'Left gutter incorrect')
        for (const ancestor of m.ancestors) {
          assert(ancestor.x <= m.table.x + 1 && ancestor.right >= m.table.right - 1, `An ancestor clips the expanded table: ${JSON.stringify({ ancestor, table: m.table })}`)
        }
        assert.equal(m.codeWordBreak, 'normal', 'Identifiers must not break into characters')
      }
      if (width <= 390) assert(m.scrolls, 'Wide table must still scroll locally on a phone')
      assert.equal(m.farmHeight, m.liveHeight, 'Off-screen measurement differs from the visible row')
      results.push({ host, theme, width, ...m })
      if (width === 1500 || width === 390) await page.screenshot({ path: `${out}/${expectBug ? 'before' : 'after'}-${theme}-${width}.png` })
    }
    if (!expectBug) {
      await page.goto(`${base}/capture/chat-table-breakout.html?theme=${theme}&host=${host}&tableOnly`, { waitUntil: 'networkidle' })
      if (process.argv.includes('--mutate-margin-containment')) {
        await page.addStyleTag({ content: '[data-role="assistant"] > .message-bubble { display: block !important; }' })
      }
      const bubble = page.locator('[data-live-fixture] .message-bubble')
      const before = await bubble.boundingBox()
      const rowBefore = await page.locator('[data-live-fixture]').boundingBox()
      await page.locator('[data-live-fixture] [data-role="assistant"]').hover()
      await page.locator('[data-live-fixture] [data-testid="toggle-raw-view"]').click()
      await page.waitForTimeout(100)
      const raw = await bubble.boundingBox()
      assert(before && raw && Math.abs(before.height - raw.height) < 1, 'Raw view must preserve the table-only bubble height')
      const rowRaw = await page.locator('[data-live-fixture]').boundingBox()
      assert(rowBefore && rowRaw && Math.abs(rowBefore.height - rowRaw.height) < 1, 'Raw view must preserve total row height, including table margins')
    }
    await page.close()
  }
  }

  // MCP full-screen sheet: McpAppFrame promotes its wrapper to `position: fixed`
  // IN PLACE (never portaled — reparenting the iframe reloads the app), so the
  // sheet must escape the scroller to the viewport: nothing between them may
  // establish a containing block for fixed descendants.
  for (const width of overlay ? [1500, 1100] : []) {
    const page = await browser.newPage({ viewport: { width, height: 900 }, deviceScaleFactor: 1 })
    await page.goto(`${base}/capture/chat-table-breakout.html?theme=light&host=main&mcp`, { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-live-fixture] iframe')
    if (expectBug) {
      // SIMULATED hazard, not a reproduction in this Chromium: this Chromium
      // does not trap fixed descendants in a query container on its own, so
      // the baseline adds `contain: layout` to the real query container (which
      // must come from index.css) to show what a containing block on the
      // scroller does to the sheet.
      assert.equal(await page.evaluate(() => getComputedStyle(document.querySelector('.chat-container')).containerType), 'inline-size', 'Baseline needs the query container in index.css')
      await page.addStyleTag({ content: '.chat-container { contain: layout; }' })
    }
    const app = page.frameLocator('[data-live-fixture] iframe').locator('#count')
    await app.click(); await app.click()
    assert.equal(await app.textContent(), '2', 'App state precondition')
    const tableBefore = await page.locator('[data-live-fixture] [data-testid="markdown-table"]').first().boundingBox()
    // Tag the iframe node so identity (not just presence) can be re-checked.
    await page.evaluate(() => { document.querySelector('[data-live-fixture] iframe').dataset.identity = 'original' })
    await page.locator('[data-live-fixture] [aria-label="Open app full screen"]').click()
    await page.waitForSelector('[role="dialog"][aria-modal="true"]')
    const o = await page.evaluate(() => {
      const sheet = document.querySelector('[role="dialog"][aria-modal="true"]')
      const backdrop = sheet.previousElementSibling.previousElementSibling
      const scroller = document.querySelector('.chat-container')
      const aside = document.querySelector('aside')
      const rect = el => { const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, right: r.right, bottom: r.bottom, width: r.width, height: r.height } }
      const asideRect = aside.getBoundingClientRect()
      // A point inside the sidebar but left of the sheet's 2.5% viewport inset.
      const hit = document.elementFromPoint(asideRect.x + 8, asideRect.y + asideRect.height / 2)
      return {
        viewport: { width: innerWidth, height: innerHeight },
        scrollerContainment: { containerType: getComputedStyle(scroller).containerType, contain: getComputedStyle(scroller).contain },
        sheet: rect(sheet), backdrop: rect(backdrop), scroller: rect(scroller), aside: rect(aside),
        backdropIsFixedToViewport: getComputedStyle(backdrop).position === 'fixed',
        sidebarHit: hit === backdrop ? 'backdrop' : hit === sheet || sheet.contains(hit) ? 'sheet' : hit?.tagName,
        iframeIdentity: document.querySelector('[role="dialog"] iframe')?.dataset.identity,
      }
    })
    const centered = Math.abs((o.sheet.x + o.sheet.right) / 2 - o.viewport.width / 2) < 1 && Math.abs((o.sheet.y + o.sheet.bottom) / 2 - o.viewport.height / 2) < 1
    const coversViewport = o.backdrop.x === 0 && o.backdrop.y === 0 && o.backdrop.right === o.viewport.width && o.backdrop.bottom === o.viewport.height
    const overSidebar = o.sheet.x < o.aside.right
    const clippedToPane = o.backdrop.x >= o.scroller.x && o.backdrop.width <= o.scroller.width
    await page.screenshot({ path: `${out}/${expectBug ? 'before' : 'after'}-mcp-overlay-${width}.png` })
    if (expectBug) {
      assert(clippedToPane && !coversViewport, `Baseline must trap the sheet inside the scroller: ${JSON.stringify(o)}`)
    } else {
      assert.deepEqual(o.scrollerContainment, { containerType: 'normal', contain: 'none' }, 'The scroller must not be a containing block for fixed descendants')
      assert(coversViewport, `Backdrop must cover the viewport: ${JSON.stringify(o)}`)
      assert(centered, `Sheet must be centred on the viewport: ${JSON.stringify(o)}`)
      assert(overSidebar, `Sheet must extend over the sidebar: ${JSON.stringify(o)}`)
      assert.equal(o.sidebarHit, 'backdrop', 'Backdrop must intercept pointer hits over the sidebar')
      assert.equal(o.iframeIdentity, 'original', 'Promotion must keep the same iframe node')
      assert.equal(await app.textContent(), '2', 'App state must survive promotion')
    }
    await page.keyboard.press('Escape')
    await page.waitForSelector('[role="dialog"][aria-modal="true"]', { state: 'detached' })
    const restored = await page.evaluate(() => document.querySelector('[data-live-fixture] iframe')?.dataset.identity)
    assert.equal(restored, 'original', 'Dismissal must keep the same iframe node')
    assert.equal(await app.textContent(), '2', 'App state must survive dismissal')
    const tableAfter = await page.locator('[data-live-fixture] [data-testid="markdown-table"]').first().boundingBox()
    assert(tableBefore && tableAfter && Math.abs(tableBefore.width - tableAfter.width) < 1 && Math.abs(tableBefore.x - tableAfter.x) < 1, 'Table geometry must be unchanged after the sheet closes')
    results.push({ host: 'main', theme: 'light', width, mcpOverlay: { ...o, centered, coversViewport, overSidebar, clippedToPane } })
    await page.close()
  }
} finally { await browser.close() }
writeFileSync(`${out}/measurements.json`, JSON.stringify(results, null, 2))
console.log(JSON.stringify(results.map(({ theme, width, table, prose, scrolls, mcpOverlay }) => mcpOverlay
  ? { theme, viewport: width, mcpOverlay: { centered: mcpOverlay.centered, coversViewport: mcpOverlay.coversViewport, overSidebar: mcpOverlay.overSidebar, sidebarHit: mcpOverlay.sidebarHit } }
  : { theme, viewport: width, table: table.width, prose: prose.width, scrolls }), null, 2))
console.log(expectBug ? 'Baseline reproduced.' : 'All table geometry assertions passed.')
