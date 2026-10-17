/**
 * Screenshot harness for the chat header's concurrency-cut chip, the
 * per-session queued marker, and their phone home in the top bar's menu.
 *
 * Every byte on screen is fixture data: the session title, the agent name, the
 * channel label and the controller reading are invented here, the API is
 * stubbed, and the queued depth arrives as the `subagent_queued` frame the
 * gateway would send.
 *
 * Desktop (1440 px), dark and light:
 *   01 exec cut with a queue, this session's own starts waiting (chip + marker)
 *   02 exec cut, nothing queued (chip only)
 *   03 spawn gate paused, exec cap untouched (gate chip, no marker)
 *   04 no cut (the header as on main)
 *   05 scene 01 with a two-way channel link, so the inbound-link chip shares
 *      the row (the marker folds away on that crowded row)
 *   07 spawn gate reduced, exec cap untouched (the "new agents start 2 at a time" sentence)
 *   08 every read fails (the read-error notice, no cut on screen)
 *   09 a cut, then a refresh and its retry fail (the notice takes the chip's
 *      place; the marker goes with it)
 * Narrow window (900 px, sidebar pinned): 01 and 05.
 * Bengali (12), dark, at 1440 px and 900 px: 01 and 07, every number on the
 * chip and the marker in Bengali numerals.
 * Phone (390 px and 320 px): 01 with the top bar's overflow menu open, the cut
 * row carrying the reassurance as a visible second line; at 390 px also 09,
 * with the trigger's error-toned dot (11) and then the menu holding the error
 * rows in place of the cut row (10).
 *
 * Verified by DOM asserts and measured geometry; the captures themselves are
 * only checked for existence and size.
 *
 * Usage: node scripts/capture-concurrency-cut-chip.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/concurrency-cut-chip'
const ACTIVE = 'chat-active'
const TITLE = 'Refactor the billing module'
const CARD = '/developer?tab=system&plane=services'
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const baseSlot = {
  key: ACTIVE, title: TITLE, running: true, messages: 4,
  agent: 'kirocrew', modified: now, last_ts: new Date().toISOString(), folder_id: '',
  last_message: 'Starting three agents for the split.',
}
const linkedSlot = { ...baseSlot, links: [{ channel: 'discord', label: 'Discord DM', target: '…42', direction: 'both', paused: false }] }
const detail = { running: true, has_more: false, total: 0, queue: [], messages: [] }

const EXEC_CUT = {
  cut: true,
  exec: { cap: 2, configured: 8, cut: true },
  gate: { cap: 4, configured: 4, cut: false, paused: false },
  queued: 5,
}
const FULL = 'Under load: 2 of 8 agents at once · 5 queued'
const SCENES = [
  { id: '01-exec-cut-queued', throttle: EXEC_CUT, sessionQueued: 3, chip: FULL, marker: 'This session: 3 queued' },
  { id: '02-exec-cut', throttle: { ...EXEC_CUT, queued: 0 }, sessionQueued: 0, chip: 'Under load: 2 of 8 agents at once', marker: null },
  { id: '03-gate-paused', throttle: { cut: true, exec: { cap: 8, configured: 8, cut: false }, gate: { cap: 1, configured: 4, cut: true, paused: true }, queued: 0 },
    sessionQueued: 3, chip: 'Under load: new agents wait to start', marker: null },
  { id: '04-no-cut', throttle: { cut: false }, sessionQueued: 0, chip: null, marker: null },
  // A crowded row keeps the chip's sentence and folds the marker below 64 rem
  // (this one is ~59 rem with the sidebar pinned).
  { id: '05-with-inbound-link', throttle: EXEC_CUT, sessionQueued: 3, linked: true, chip: FULL, marker: null },
  { id: '07-gate-reduced', throttle: { cut: true, exec: { cap: 8, configured: 8, cut: false }, gate: { cap: 2, configured: 4, cut: true, paused: false }, queued: 0 },
    sessionQueued: 0, chip: 'Under load: new agents start 2 at a time, down from 4', marker: null },
  // `failAfter` good reads, then every read answers 502. A failure is shown
  // once its one retry fails too: ~5 s for 08, ~20 s (a refresh, then its
  // retry) for 09.
  { id: '08-read-failed', throttle: EXEC_CUT, failAfter: 0, sessionQueued: 0, chip: null, marker: null, error: true },
  { id: '09-read-failed-kept-cut', throttle: EXEC_CUT, failAfter: 1, sessionQueued: 3, chip: FULL, marker: null, error: true },
]
const LOAD_FAILED = 'Could not check for queued agents'
const QUEUE_KEPT = 'Queued agents keep their place and start as room frees up.'
const byId = Object.fromEntries(SCENES.map(s => [s.id, s]))

/** Per width: which scenes run, and what the chip and marker are expected to show there. */
const PASSES = [
  { tag: '', width: 1440, scenes: SCENES.map(s => s.id) },
  // 900 px with the sidebar pinned leaves the header row ~25 rem: the chip
  // shows the reduced cap alone and the marker folds away. Beside the
  // inbound-link chip (a row main already fills) the chip is the gauge alone.
  { tag: '-narrow', width: 900, scenes: ['01-exec-cut-queued', '05-with-inbound-link'],
    expectChip: s => (s.linked ? '' : '2/8'), expectMarker: () => null },
]
const PHONES = [390, 320]

function assert(label, got, want) {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    throw new Error(`${label}:\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`)
  }
}

/** What a sighted reader sees in the chip: rendered text, with what CSS hides
 *  (display:none, or the 1px sr-only clip) left out. */
const visibleChipText = link => link.evaluate(a => {
  const copy = a.cloneNode(true)
  const live = [...a.querySelectorAll('*')]
  const dead = [...copy.querySelectorAll('*')]
  live.forEach((n, i) => {
    const cs = getComputedStyle(n)
    const srOnly = cs.position === 'absolute' && n.getBoundingClientRect().width <= 1
    if (cs.display === 'none' || srOnly) dead[i].setAttribute('data-gone', '')
  })
  copy.querySelectorAll('[data-gone]').forEach(n => n.remove())
  return copy.textContent.replace(/\s+/g, ' ').trim()
})

async function openPage(browser, base, { width, height, theme, scene, mobile, lang = 'en' }) {
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 2, isMobile: !!mobile, hasTouch: !!mobile })
  const page = await context.newPage()
  logPageProblems(page)
  let ws = null
  let reads = 0
  const extra = (path, route) => {
    if (path === '/api/spawn/adaptive') {
      reads += 1
      if (scene.failAfter != null && reads > scene.failAfter) return json(route, { error: 'bad gateway' }, 502), true
      return json(route, { adaptive: {}, throttle: scene.throttle }), true
    }
    if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
    return false
  }
  await stubDashboardApi(page, { slots: [scene.linked ? linkedSlot : baseSlot], theme, extra })
  // After the shared stub, which swallows /api/ws: this socket carries the
  // queued-depth frame.
  await page.routeWebSocket(/\/api\/ws/, socket => { ws = socket })
  await page.addInitScript(({ slot, lang }) => {
    localStorage.setItem('mc-active-slot', slot)
    localStorage.setItem('mc-privacy-notice-v1', '1')
    localStorage.setItem('mc-sidebar-pinned', 'true')
    localStorage.setItem('mc-lang', lang)
  }, { slot: ACTIVE, lang })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  if (scene.sessionQueued > 0) {
    if (!ws) throw new Error('websocket route never bound')
    ws.send(JSON.stringify({ type: 'subagent_queued', data: { slot: ACTIVE, queued: scene.sessionQueued } }))
    await page.waitForTimeout(600)
  }
  return { context, page, reads: () => reads }
}

/** The platform fonts that draw the visible text under `selector`, as the
 *  renderer reports them, limited to faces whose name says Bengali (or the
 *  Windows face that carries it). Empty when the host has no such face. */
async function bengaliFaces(page, selector) {
  const cdp = await page.context().newCDPSession(page)
  try {
    await cdp.send('DOM.enable')
    await cdp.send('CSS.enable')
    const { root } = await cdp.send('DOM.getDocument', { depth: -1 })
    const { nodeIds } = await cdp.send('DOM.querySelectorAll', { nodeId: root.nodeId, selector: `${selector} *` })
    const names = new Set()
    for (const nodeId of nodeIds) {
      const { fonts } = await cdp.send('CSS.getPlatformFontsForNode', { nodeId })
      for (const f of fonts) if (f.glyphCount > 0 && /bengali|nirmala/i.test(f.familyName)) names.add(f.familyName)
    }
    return [...names]
  } finally {
    await cdp.detach()
  }
}

/** Opens the phone top bar's overflow menu and waits for its open animation
 *  to finish, so the capture shows the settled, opaque menu. */
async function openPhoneMenu(page, label) {
  await page.getByTestId('mobile-topbar-more').click()
  const menu = page.getByRole('menu')
  await menu.waitFor({ state: 'visible', timeout: 5_000 })
  await menu.evaluate(m => Promise.all(m.getAnimations({ subtree: true }).map(a => a.finished)))
  await page.waitForTimeout(300)
  const { opacity, background } = await menu.evaluate(m => ({ opacity: getComputedStyle(m).opacity, background: getComputedStyle(m).backgroundColor }))
  assert(`${label} menu opacity`, opacity, '1')
  // An opaque surface: nothing under the menu shows through its rows.
  const alpha = /rgba?\(([^)]+)\)/.exec(background)?.[1].split(/[ ,/]+/).filter(Boolean)[3]
  if (alpha != null && Number(alpha) < 1) throw new Error(`${label}: the menu background is translucent (${background})`)
  return menu
}

/** Waits for the header's read-error notice and checks its text and hand-off. */
async function expectReadError(page, label) {
  const notice = page.getByTestId('concurrency-cut-error')
  await notice.waitFor({ state: 'visible', timeout: 45_000 })
  const text = (await notice.textContent()).replace(/\s+/g, ' ').trim()
  if (!text.includes(LOAD_FAILED)) throw new Error(`${label}: the notice says ${JSON.stringify(text)}`)
  if (!(await notice.getByRole('button').count())) throw new Error(`${label}: the notice has no agent hand-off`)
  return text
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const written = []
  const shot = async (page, name, clip) => {
    const path = join(OUT, name)
    await page.screenshot({ path, ...(clip ? { clip } : {}) })
    written.push({ path, bytes: statSync(path).size })
  }
  try {
    for (const pass of PASSES) {
      for (const theme of ['dark', 'light']) {
        for (const id of pass.scenes) {
          const scene = byId[id]
          const label = `${id} ${theme}${pass.tag}`
          const { context, page } = await openPage(browser, base, { width: pass.width, height: 700, theme, scene })
          const chip = page.getByTestId('concurrency-cut-chip')
          if (scene.chip) await chip.waitFor({ state: 'visible', timeout: 15_000 })
          else if (!scene.error) await page.waitForTimeout(1500)
          const errorText = scene.error ? await expectReadError(page, label) : null
          if (!scene.error && (await page.getByTestId('concurrency-cut-error').count())) {
            throw new Error(`${label}: a read-error notice shows with every read answering`)
          }
          // A reported failure takes the chip's place, whatever was read before.
          const chipNow = scene.error ? null : scene.chip
          const wantChip = chipNow && pass.expectChip ? pass.expectChip(scene) : chipNow
          const chipText = (await chip.count()) ? await visibleChipText(chip) : null
          assert(`${label} chip`, chipText, wantChip)
          if (chipNow) {
            assert(`${label} chip link`, await chip.getAttribute('href'), CARD)
            // Whatever the width, the link's name is the whole sentence.
            const name = await chip.evaluate(a => a.textContent)
            if (!name.includes(scene.chip)) throw new Error(`${label}: the chip's accessible text lost its sentence`)
          }
          const marker = page.getByTestId('session-queued-marker')
          const markerShown = (await marker.count()) && (await marker.first().isVisible())
          const markerText = markerShown
            ? await marker.first().evaluate(el => {
              const copy = el.cloneNode(true)
              copy.querySelectorAll('.sr-only').forEach(n => n.remove())
              return copy.textContent.trim()
            })
            : null
          const wantMarker = pass.expectMarker ? pass.expectMarker(scene) : scene.marker
          assert(`${label} marker`, markerText, wantMarker)
          if (scene.linked) await page.getByText('Driven from Discord DM').first().waitFor({ state: 'visible', timeout: 5_000 })
          const layout = await page.evaluate(() => {
            const title = document.querySelector('.group\\/header')
            const row = title?.parentElement
            const trail = document.querySelector('.ml-auto.flex.shrink-0.items-center')
            const label = title?.querySelector('span')
            return {
              rowWidth: row ? Math.round(row.getBoundingClientRect().width) : null,
              titleWidth: title ? Math.round(title.getBoundingClientRect().width) : null,
              titleTextShown: label ? Math.round(label.clientWidth) : null,
              chipWidth: Math.round(document.querySelector('[data-testid="concurrency-cut-chip"]')?.getBoundingClientRect().width ?? 0),
              trailRight: trail ? Math.round(trail.getBoundingClientRect().right) : null,
              rowBottom: row ? Math.ceil(row.getBoundingClientRect().bottom) : 120,
            }
          })
          console.log(`${label} layout: ${JSON.stringify(layout)}`)
          if (layout.trailRight != null && layout.trailRight > pass.width) {
            throw new Error(`${label}: trailing controls end at ${layout.trailRight}px past ${pass.width}px`)
          }
          // The title text's floor, against main at the same width: main shows
          // 188 px at 1440 and 120 px at 900; with the inbound-link chip, main
          // already leaves it 0 px at 900, so only the trailing controls are
          // held there.
          const titleFloor = pass.tag ? (scene.linked ? 0 : 50) : 150
          if (layout.titleTextShown != null && layout.titleTextShown < titleFloor) {
            throw new Error(`${label}: the session title is squeezed to ${layout.titleTextShown}px`)
          }
          await page.mouse.move(pass.width - 20, 690)
          await shot(page, `${id}-${theme}${pass.tag}.png`, { x: 0, y: 0, width: pass.width, height: layout.rowBottom + 40 })
          console.log(`${label} OK: chip=${JSON.stringify(chipText)} marker=${JSON.stringify(markerText)}${errorText ? ` error=${JSON.stringify(errorText)}` : ''}`)
          await context.close()
        }
      }
    }
    // Bengali, 1440 px wide and 900 px narrow: the sentence, the narrow "2/8"
    // and the marker all carry Bengali numerals, never Latin ones.
    for (const [width, tag] of [[1440, ''], [900, '-narrow']]) {
      for (const id of ['01-exec-cut-queued', '07-gate-reduced']) {
        const scene = byId[id]
        const label = `${id} bn${tag}`
        const { context, page } = await openPage(browser, base, { width, height: 700, theme: 'dark', scene, lang: 'bn' })
        const chip = page.getByTestId('concurrency-cut-chip')
        await chip.waitFor({ state: 'visible', timeout: 15_000 })
        const texts = { chip: await visibleChipText(chip) }
        const marker = page.getByTestId('session-queued-marker')
        if (scene.sessionQueued > 0 && (await marker.count()) && (await marker.first().isVisible())) {
          texts.marker = await marker.first().evaluate(el => {
            const copy = el.cloneNode(true)
            copy.querySelectorAll('.sr-only').forEach(n => n.remove())
            return copy.textContent.trim()
          })
        }
        if (!tag && scene.sessionQueued > 0 && !texts.marker) throw new Error(`${label}: no session marker at 1440 px`)
        for (const [what, text] of Object.entries(texts)) {
          if (!/[\u09e6-\u09ef]/.test(text) || /[0-9]/.test(text)) throw new Error(`${label}: the ${what} mixes numeral systems: ${JSON.stringify(text)}`)
        }
        // The digits are drawn with a face that has Bengali glyphs, not the
        // empty boxes a host without one renders: the capture is only evidence
        // if a reader can see the digits in it.
        const faces = await bengaliFaces(page, '[data-testid="concurrency-cut-chip"]')
        if (!faces.length) {
          throw new Error(`${label}: no installed font draws Bengali here; install one (e.g. Noto Sans Bengali) and re-run`)
        }
        const rowBottom = await page.evaluate(() => Math.ceil(document.querySelector('.group\\/header')?.parentElement?.getBoundingClientRect().bottom ?? 120))
        await page.mouse.move(width - 20, 690)
        await shot(page, `12-${id}-bn${tag}.png`, { x: 0, y: 0, width, height: rowBottom + 40 })
        console.log(`${label} OK: ${JSON.stringify(texts)} drawn with ${faces.join(', ')}`)
        await context.close()
      }
    }
    // Phone: the header row is not rendered; the overflow menu carries the cut.
    for (const width of PHONES) {
      for (const theme of ['dark', 'light']) {
        const scene = byId['01-exec-cut-queued']
        const label = `phone-${width} ${theme}`
        const { context, page } = await openPage(browser, base, { width, height: 760, theme, scene, mobile: true })
        const more = page.getByTestId('mobile-topbar-more')
        await more.waitFor({ state: 'visible', timeout: 15_000 })
        if (!(await more.getByTestId('concurrency-cut-dot').count())) throw new Error(`${label}: no cut dot on the menu trigger`)
        assert(`${label} dot tone`, await more.getByTestId('concurrency-cut-dot').getAttribute('data-tone'), 'cut')
        const region = await page.getByRole('status').filter({ hasText: 'Under load:' }).count()
        if (!region) throw new Error(`${label}: the cut is not in a live region`)
        const menu = await openPhoneMenu(page, label)
        const row = page.getByTestId('concurrency-cut-menu-item')
        await row.waitFor({ state: 'visible', timeout: 5_000 })
        assert(`${label} menu row`, (await page.getByTestId('concurrency-cut-menu-label').textContent()).trim(), FULL)
        // The hover reassurance, drawn as the row's second line inside the menu.
        const hint = page.getByTestId('concurrency-cut-menu-hint')
        assert(`${label} menu hint`, (await hint.textContent()).trim(), QUEUE_KEPT)
        if (!(await hint.isVisible())) throw new Error(`${label}: the menu row's hint line is not visible`)
        const box = await menu.boundingBox()
        if (!box || box.x < 0 || box.x + box.width > width + 1) throw new Error(`${label}: the menu does not fit the screen: ${JSON.stringify(box)}`)
        const hintBox = await hint.boundingBox()
        if (!hintBox || hintBox.x < box.x || hintBox.x + hintBox.width > box.x + box.width + 1) {
          throw new Error(`${label}: the hint line spills out of the menu: ${JSON.stringify(hintBox)}`)
        }
        await shot(page, `06-phone-${width}-menu-${theme}.png`, { x: 0, y: 0, width, height: Math.min(760, Math.ceil(box.y + box.height + 24)) })
        console.log(`${label} OK: dot, live region, menu row ${JSON.stringify(FULL)}, hint ${JSON.stringify(QUEUE_KEPT)} (${Math.round(hintBox.width)}x${Math.round(hintBox.height)} px)`)
        await context.close()
      }
    }
    // Phone, a refresh and its retry failing after a cut: the menu holds the
    // passive notice and the hand-off item in place of the cut row.
    for (const theme of ['dark', 'light']) {
      const width = 390
      const scene = byId['09-read-failed-kept-cut']
      const label = `phone-${width} read-failed ${theme}`
      const { context, page, reads } = await openPage(browser, base, { width, height: 760, theme, scene, mobile: true })
      const dot = page.getByTestId('concurrency-cut-dot')
      await dot.waitFor({ state: 'attached', timeout: 15_000 })
      const cutColor = await dot.evaluate(d => getComputedStyle(d).backgroundColor)
      // The good read, the failed refresh and its failed retry.
      const deadline = Date.now() + 45_000
      while (reads() < scene.failAfter + 2) {
        if (Date.now() > deadline) throw new Error(`${label}: only ${reads()} reads in 45 s`)
        await page.waitForTimeout(500)
      }
      // The trigger's dot stays, turned from the cut's tone to the error tone.
      await page.locator('[data-testid="concurrency-cut-dot"][data-tone="error"]').waitFor({ state: 'attached', timeout: 5_000 })
      const errorColor = await dot.evaluate(d => getComputedStyle(d).backgroundColor)
      if (errorColor === cutColor) throw new Error(`${label}: the error dot keeps the cut's colour ${cutColor}`)
      // Non-text contrast (WCAG 1.4.11, 3:1) of the dot against the bar behind it.
      const contrast = await dot.evaluate(d => {
        const rgb = s => (/rgba?\(([^)]+)\)/.exec(s)?.[1].split(/[ ,/]+/).filter(Boolean).map(Number)) ?? [0, 0, 0, 0]
        const lum = ([r, g, b]) => {
          const ch = v => { const c = v / 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4 }
          return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)
        }
        let el = d.parentElement
        let bg = [0, 0, 0, 0]
        while (el) {
          bg = rgb(getComputedStyle(el).backgroundColor)
          if (bg.length < 4 || bg[3] > 0) break
          el = el.parentElement
        }
        const [a, b] = [lum(rgb(getComputedStyle(d).backgroundColor)), lum(bg)]
        return Math.round(((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)) * 100) / 100
      })
      if (contrast < 3) throw new Error(`${label}: the error dot's contrast is ${contrast}:1, under 3:1`)
      const bar = await page.getByTestId('mobile-topbar-more').boundingBox()
      await shot(page, `11-phone-${width}-read-failed-dot-${theme}.png`, { x: 0, y: 0, width, height: Math.ceil(bar.y + bar.height + 16) })
      console.log(`${label} dot: ${cutColor} -> ${errorColor}, contrast ${contrast}:1`)
      const menu = await openPhoneMenu(page, label)
      if (await page.getByTestId('concurrency-cut-menu-item').count()) throw new Error(`${label}: the stale cut row is still in the menu`)
      const menuText = (await menu.textContent()).replace(/\s+/g, ' ')
      if (!menuText.includes(LOAD_FAILED)) throw new Error(`${label}: the menu does not say the read failed`)
      if (!(await menu.locator('[aria-describedby="concurrency-cut-menu-error"]').count())) {
        throw new Error(`${label}: the hand-off is not a menu item described by the notice`)
      }
      const box = await menu.boundingBox()
      if (!box || box.x < 0 || box.x + box.width > width + 1) throw new Error(`${label}: the menu does not fit the screen: ${JSON.stringify(box)}`)
      await shot(page, `10-phone-${width}-menu-read-failed-${theme}.png`, { x: 0, y: 0, width, height: Math.min(760, Math.ceil(box.y + box.height + 24)) })
      console.log(`${label} OK: notice and hand-off item in place of the cut row after ${reads()} reads`)
      await context.close()
    }
    for (const w of written) {
      if (w.bytes < 4000) throw new Error(`${w.path}: only ${w.bytes} bytes`)
      console.log(`${w.path} ${w.bytes} bytes`)
    }
    console.log(`\nOK — ${written.length} screenshots in ${OUT}`)
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
