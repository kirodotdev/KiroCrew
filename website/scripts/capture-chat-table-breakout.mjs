import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
import { chromium } from 'playwright'

/** Which check groups a run executes.
 *  - default: table geometry AND the MCP full-screen overlay.
 *  - `--expect-bug`: the pre-breakout table baseline only (that source has no
 *    query container, so the overlay baseline does not apply to it).
 *  - `--expect-bug --overlay-only`: the overlay baseline only, against index.css
 *    with a `.chat-container` query container re-added.
 *  - `--overlay-only`: the overlay checks only.
 *  The turn-rail checks (the minimap staying static and usable while a
 *  breakout table scrolls under its band) run with the default table checks
 *  only: the pre-breakout baseline has no table to scroll past the rail. */
export function selectModes(argv) {
  const expectBug = argv.includes('--expect-bug')
  const overlayOnly = argv.includes('--overlay-only')
  return { expectBug, tables: !overlayOnly, overlay: overlayOnly || !expectBug, rail: !overlayOnly && !expectBug }
}

if (process.argv.includes('--self-test')) {
  assert.deepEqual(selectModes([]), { expectBug: false, tables: true, overlay: true, rail: true })
  assert.deepEqual(selectModes(['--expect-bug']), { expectBug: true, tables: true, overlay: false, rail: false })
  assert.deepEqual(selectModes(['--expect-bug', '--overlay-only']), { expectBug: true, tables: false, overlay: true, rail: false })
  assert.deepEqual(selectModes(['--overlay-only']), { expectBug: false, tables: false, overlay: true, rail: false })
  console.log('Mode selection self-test passed.')
  process.exit(0)
}

const base = process.argv[2]
const out = process.argv[3]
assert(base && out, 'Usage: node scripts/capture-chat-table-breakout.mjs <loopback URL> <output dir> [--expect-bug] [--overlay-only] | --self-test')
const { expectBug, tables, overlay, rail } = selectModes(process.argv)
mkdirSync(out, { recursive: true })
const { LD_LIBRARY_PATH: _ld, ...env } = process.env
const browser = await chromium.launch({ env })
const results = []
// Breakout is opt-in: a top-level table keeps the reading column's width
// until its Expand toggle is pressed. Toggle every visible top-level toggle.
const setExpanded = (page, want) => page.evaluate((want) => {
  for (const b of document.querySelectorAll('[data-role="assistant"] [data-testid="table-expand"]')) {
    if (getComputedStyle(b).display !== 'none' && (b.getAttribute('aria-expanded') === 'true') !== want) b.click()
  }
}, want)
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
      if (!expectBug) {
        // Default: the table sits in the reading column, like the prose.
        const d = await page.evaluate(() => {
          const assistant = document.querySelector('.chat-container [data-role="assistant"]')
          const outer = assistant.querySelector('table').parentElement.parentElement
          const toggle = outer.querySelector('[data-testid="table-expand"]')
          return { table: outer.getBoundingClientRect().width, prose: assistant.querySelector('p').getBoundingClientRect().width, expanded: outer.hasAttribute('data-expanded'), toggleShown: getComputedStyle(toggle).display !== 'none' }
        })
        assert(!d.expanded && Math.abs(d.table - d.prose) < 1, `Default table must keep the prose width: ${JSON.stringify(d)}`)
        assert.equal(d.toggleShown, width >= 880, `Expand toggle must show only where expanding can widen: ${JSON.stringify({ width, ...d })}`)
        if (width === 1500) {
          await page.hover('.chat-container [data-role="assistant"] [data-testid="markdown-table"]')
          await page.screenshot({ path: `${out}/default-${theme}-${width}.png` })
        }
        await setExpanded(page, true)
        await page.waitForTimeout(100)
      }
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
        const tableCode = table.querySelector('code')
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
          // Inline code inherits the message's own wrap rules in a cell exactly
          // as in prose; the first-column identifier pill stays inside the
          // wrapper's visible box at scrollLeft 0 instead of being clipped by it.
          codeWordBreak: getComputedStyle(tableCode).wordBreak,
          proseCodeWordBreak: getComputedStyle(para.querySelector('code')).wordBreak,
          firstPill: rect(tableCode), wrapperBox: rect(wrapper),
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
        // index.css: max(100%, pane - 80px), centred on the column — 40px
        // clear of each pane edge, the band the turn rail paints into.
        assert(Math.abs(m.table.width - Math.max(m.prose.width, m.clientWidth - 80)) < 1, `Table must fill the pane minus the rail band: ${JSON.stringify({ table: m.table, prose: m.prose, clientWidth: m.clientWidth })}`)
        assert(Math.abs((m.table.x + m.table.right) / 2 - (m.scroller.x + m.clientWidth / 2)) < 1, 'Table must be centred on the pane')
        // A table at the column's width sits inside the column the rail already
        // probes; a wider one must leave the rail's 40px band on both sides.
        if (m.table.width > m.prose.width + 1) assert(m.table.x - m.scroller.x >= 39 && m.scroller.x + m.clientWidth - m.table.right >= 39, `Table entered the rail band: ${JSON.stringify({ table: m.table, scroller: m.scroller, clientWidth: m.clientWidth })}`)
        for (const ancestor of m.ancestors) {
          assert(ancestor.x <= m.table.x + 1 && ancestor.right >= m.table.right - 1, `An ancestor clips the expanded table: ${JSON.stringify({ ancestor, table: m.table })}`)
        }
      }
      // Breakout widens the table; it does not change how its inline code
      // wraps. A pane-wide `word-break: normal` on the pills pushed the
      // identifier column past the phone's pane edge with no scroll cue.
      assert.equal(m.codeWordBreak, m.proseCodeWordBreak, 'Table code must wrap like inline code in prose')
      // At 390 the Signal column fits inside the pane once its pill wraps like
      // prose (the nowrap pill put its right edge ~80px past the wrapper). At
      // 320 eight columns overflow under any wrapping, so only the rule above applies.
      if (width === 390) assert(m.firstPill.right <= m.wrapperBox.right + 1, `Identifier pill clipped by its table wrapper: ${JSON.stringify({ pill: m.firstPill, wrapper: m.wrapperBox })}`)
      if (width <= 390) assert(m.scrolls, 'Wide table must still scroll locally on a phone')
      assert.equal(m.farmHeight, m.liveHeight, 'Off-screen measurement differs from the visible row')
      results.push({ host, theme, width, ...m })
      if (width === 1500 || width === 390) await page.screenshot({ path: `${out}/${expectBug ? 'before' : 'after'}-${theme}-${width}.png` })
      if (!expectBug) await setExpanded(page, false)
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

  // Turn rail: the minimap's 28px button sits 8px from the pane edge; a
  // breakout table stops 40px from it, so the rail never has to yield. On
  // either edge the same rail node stays mounted, painted at the same spot,
  // clickable and focused while the table scrolls under its band and out
  // again; the pane keeps one scrollbar state and one width throughout. The
  // pass is recorded to `<out>/video/` (the path lands in measurements.json).
  for (const side of rail ? ['left', 'right'] : []) {
    const context = await browser.newContext({ viewport: { width: 1500, height: 900 }, deviceScaleFactor: 1, recordVideo: { dir: `${out}/video`, size: { width: 1500, height: 900 } } })
    const page = await context.newPage()
    await page.goto(`${base}/capture/chat-table-breakout.html?theme=light&rail=${side}`, { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-live-fixture] table')
    await setExpanded(page, true)
    const railProbe = () => page.evaluate((side) => {
      const scroller = document.querySelector('.chat-container')
      const s = scroller.getBoundingClientRect()
      const button = document.querySelector('[data-testid="turn-navigation-minimap"] button')
      const table = document.querySelector('[data-live-fixture] .markdown-table')
      const t = table.getBoundingClientRect()
      const rect = el => el ? (({ x, y, right, bottom, width }) => ({ x, y, right, bottom, width }))(el.getBoundingClientRect()) : null
      // Where the rail's button paints: 8px in from the pane edge, 28px wide.
      const probeX = side === 'right' ? s.left + scroller.clientWidth - 8 - 14 : s.left + 8 + 14
      const tableOnScreen = t.bottom > s.top && t.top < s.bottom
      // Two probes on the rail's x: at the button's own centre (where a rail
      // click lands) and level with the table while it is on screen (where a
      // table reaching into the band would be what the point hits).
      const b = button?.getBoundingClientRect()
      const classify = hit => button && hit && button.contains(hit) ? 'rail' : hit?.closest('table') ? hit.tagName.toLowerCase() : hit?.tagName
      const hit = classify(document.elementFromPoint(probeX, b ? (b.top + b.bottom) / 2 : (s.top + s.bottom) / 2))
      const bandHit = tableOnScreen ? classify(document.elementFromPoint(probeX, Math.min(Math.max(t.top + 40, s.top + 1), s.bottom - 1))) : null
      return {
        railShown: !!button, identity: button?.dataset.identity, focused: !!button && document.activeElement === button,
        button: rect(button), table: rect(table), tableOnScreen, scrollTop: scroller.scrollTop, hit, bandHit,
        tableFree: side === 'right' ? s.left + scroller.clientWidth - t.right : t.left - s.left,
        clientWidth: scroller.clientWidth, paneScrollWidth: scroller.scrollWidth, scrollbarWidth: scroller.style.scrollbarWidth || '',
      }
    }, side)
    await page.waitForFunction(() => !!document.querySelector('[data-testid="turn-navigation-minimap"] button'))
    // Tag the node and focus it, so identity and focus can be re-checked
    // after the table has been under the rail.
    await page.evaluate(() => { const b = document.querySelector('[data-testid="turn-navigation-minimap"] button'); b.dataset.identity = 'original'; b.focus() })
    const before = await railProbe()
    assert(!before.tableOnScreen, `Fixture: turn 1's table must start below the fold: ${JSON.stringify(before)}`)
    assert(before.railShown && before.hit === 'rail' && before.focused, `Table-free viewport must keep a usable ${side} rail: ${JSON.stringify(before)}`)
    assert.equal(before.scrollbarWidth, side === 'right' ? 'none' : '', 'The rail owns the scrollbar state on the right edge only')
    await page.screenshot({ path: `${out}/after-rail-${side}-free.png` })
    // Scroll the SAME document until the table sits beside the rail.
    await page.evaluate(() => document.querySelector('[data-live-fixture] .markdown-table').scrollIntoView({ block: 'center', behavior: 'smooth' }))
    await page.waitForFunction(() => {
      const s = document.querySelector('.chat-container').getBoundingClientRect()
      const t = document.querySelector('[data-live-fixture] .markdown-table').getBoundingClientRect()
      return t.top > s.top && t.bottom < s.bottom
    })
    await page.waitForTimeout(300)
    const over = await railProbe()
    assert(over.tableOnScreen && over.tableFree >= 39, `Table must stay clear of the ${side} rail band: ${JSON.stringify(over)}`)
    assert(over.railShown && over.identity === 'original' && over.focused, `${side} rail must be the same focused node while the table is on screen: ${JSON.stringify(over)}`)
    assert.deepEqual(over.button, before.button, `${side} rail must not move while the table is on screen`)
    assert.equal(over.hit, 'rail', `A click at the rail's position must still reach the rail: ${JSON.stringify(over)}`)
    assert(!['table', 'td', 'th'].includes(over.bandHit), `The rail's band level with the table must not reach a table cell: ${JSON.stringify(over)}`)
    const overlap = side === 'right' ? over.table.right > over.button.x : over.table.x < over.button.right
    assert(!overlap, `Table and ${side} rail must not overlap: ${JSON.stringify(over)}`)
    assert(over.paneScrollWidth <= over.clientWidth + 1, 'Transcript scrolls horizontally with the table on screen')
    assert.equal(over.clientWidth, before.clientWidth, 'Pane width (and so the height-cache width bucket) must not change')
    assert.equal(over.scrollbarWidth, before.scrollbarWidth, 'Scrollbar state must not toggle')
    await page.screenshot({ path: `${out}/after-rail-${side}-table.png` })
    // Scrolling back: same node, same spot, still focused.
    await page.evaluate(() => document.querySelector('.chat-container').scrollTo({ top: 0, behavior: 'smooth' }))
    await page.waitForFunction(() => document.querySelector('.chat-container').scrollTop === 0)
    await page.waitForTimeout(300)
    const after = await railProbe()
    assert(after.railShown && after.identity === 'original' && after.focused && after.hit === 'rail', `${side} rail must survive the table leaving: ${JSON.stringify(after)}`)
    assert.deepEqual(after.button, before.button, `${side} rail must not move when the table leaves`)
    assert.equal(after.clientWidth, before.clientWidth, 'Pane width must not change when the table leaves')
    const video = page.video()
    await page.close()
    await context.close()
    results.push({ host: 'main', theme: 'light', width: 1500, rail: { side, before, over, after, video: video ? await video.path() : null } })
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
console.log(JSON.stringify(results.map(({ theme, width, table, prose, scrolls, mcpOverlay, rail }) => mcpOverlay
  ? { theme, viewport: width, mcpOverlay: { centered: mcpOverlay.centered, coversViewport: mcpOverlay.coversViewport, overSidebar: mcpOverlay.overSidebar, sidebarHit: mcpOverlay.sidebarHit } }
  : rail
    ? { theme, viewport: width, rail: { side: rail.side, tableFreeRail: rail.before.hit, overTable: { railShown: rail.over.railShown, hit: rail.over.hit, tableFree: rail.over.tableFree }, video: rail.video } }
    : { theme, viewport: width, table: table.width, prose: prose.width, scrolls }), null, 2))
console.log(expectBug ? 'Baseline reproduced.' : 'All table geometry assertions passed.')
