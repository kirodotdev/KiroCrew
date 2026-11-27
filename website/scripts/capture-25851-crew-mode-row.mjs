/**
 * Screenshot harness for the simplified Conductor mode marks.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception -- gateway-free, no kiro-cli, no dashboard auth. The root row
 * carries the `crew_mode_root` stamp the gateway writes, so the row and composer
 * render from the same state they read live.
 *
 * Frames (1280x800, 2x), one per theme:
 *   <prefix>-<theme>.png        the Conductor mode Sessions row
 *   <prefix>-<theme>-full.png   the full chat and composer
 *   <prefix>-dark-tip.png          the focused composer bolt and its status
 *   <prefix>-<theme>-attach-open.png        the composer's open attach menu
 *   <prefix>-<theme>-attach-bolt-press.png  the same menu closed by a press on the composer bolt
 *   <prefix>-dark-row-tip.png      the focused Sessions bolt, its name below it
 *   <prefix>-<theme>-new.png        a new conductor chat before its first
 *                                  message: the pressed, labelled turn-off chip
 *   <prefix>-<theme>-new-hover.png  the same chip hovered
 *   <prefix>-dark-mobile.png       the narrow chat composer
 *   <prefix>-dark-mobile-tip.png   the tapped composer bolt and its status
 *   <prefix>-dark-drawer.png       the narrow Sessions drawer
 *   <prefix>-dark-drawer-tip.png   the tapped Sessions bolt and its name
 *   <prefix>-dark-drawer-tip-bubble.png  the mobile tooltip at native size
 *   <prefix>-dark-drawer-bolt-hold.png   an 800ms touch on the Sessions bolt: the row stays put, no menu
 *   <prefix>-dark-drawer-title-hold.png  the same hold on the title: the row lifts to drag
 *
 * Usage (in website/): node scripts/capture-25851-crew-mode-row.mjs <outDir> <prefix>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/25851')
const PREFIX = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })

const ROOT = 'chat-root'
const NEW = 'chat-new'
const NO_STATUS = 'chat-no-status'
const LONG_TAG = 'Context tag that must stay clipped horizontally '.repeat(8)
const STATUS = 'This chat runs in conductor mode'
const T0 = '2026-10-28T08:00:00Z'
const row = (key, title, extra = {}) => ({
  key, title, messages: 2, running: false, agent: 'kirocrew', agent_kind: 'template', mode: '', origin: 'user',
  memory_mode: 'persistent', created: T0, last_ts: T0, folder_id: '', workspace: 'default', ...extra,
})
const slots = [
  row(ROOT, 'Write three haiku about the sea, trees and rain', { agent: 'kirocrew-conductor', crew_mode_root: true, last_ts: '2026-10-28T08:12:00Z' }),
  row('chat-w1', 'Rain haiku', { agent: 'kirocrew-worker', crew_root: ROOT, created_by: ROOT, last_ts: '2026-10-28T08:11:00Z' }),
  row('chat-w2', 'Tree haiku', { agent: 'kirocrew-worker', crew_root: ROOT, created_by: ROOT, last_ts: '2026-10-28T08:09:00Z' }),
  row('chat-plain', 'Plan the release notes', { last_ts: '2026-10-28T07:50:00Z' }),
  // A conductor chat with no message yet: the composer offers the way back off.
  row(NEW, '', { agent: 'kirocrew-conductor', messages: 0, last_ts: '2026-10-28T07:40:00Z' }),
  // No timestamp, tag or status: only the 20px title contributes column height.
  row(NO_STATUS, 'A conductor with no status', { agent: 'kirocrew-conductor', crew_mode_root: true, created: '', last_ts: '' }),
  row('chat-long', 'A long title that must remain horizontally clipped '.repeat(10), { tags: ['long-tag'] }),
]
const CATALOG = {
  agents: ['kirocrew', 'kirocrew-conductor', 'kirocrew-worker'].map(name => ({ name, selection_kind: 'template', kiro_agent: '', workspace: 'default', memory_store: 'default', source: 'builtin' })),
  default_agent: 'kirocrew', crew_mode_agent: 'kirocrew-conductor', conductor_agents: ['kirocrew-conductor'],
}
const WORKERS = {
  root: ROOT,
  workers: [
    { key: 'chat-w1', title: 'Rain haiku', agent: 'kirocrew-worker', status: 'idle', group: 'done', last_activity: '2026-10-28T08:11:00Z', created_by: ROOT, dispatched_by: ROOT },
    { key: 'chat-w2', title: 'Tree haiku', agent: 'kirocrew-worker', status: 'idle', group: 'done', last_activity: '2026-10-28T08:09:00Z', created_by: ROOT, dispatched_by: ROOT },
  ],
}

const extra = async (path, route) => {
  if (path === '/api/agents/catalog') { await json(route, CATALOG); return true }
  if (path === '/api/chat/tags') { await json(route, [{ id: 'long-tag', name: LONG_TAG, color: '', order: 0 }]); return true }
  if (path === `/api/chat/slots/${NO_STATUS}/crew-workers`) { await json(route, { root: NO_STATUS, workers: [] }); return true }
  if (path === `/api/chat/slots/${ROOT}/crew-workers`) { await json(route, WORKERS); return true }
  if (path === `/api/chat/slots/${ROOT}/crew-timeline`) { await json(route, { root: ROOT, lanes: [], gaps: [] }); return true }
  if (path === `/api/chat/slots/${ROOT}`) {
    await json(route, {
      messages: [
        { role: 'user', content: 'Write three haiku about the sea, trees and rain.', ts: '2026-10-28T08:01:00Z' },
        { role: 'assistant', content: 'The crew finished all three haiku.', ts: '2026-10-28T08:12:00Z' },
      ],
      has_more: false,
      total: 2,
    })
    return true
  }
  if (path.startsWith('/api/chat/slots/') && !path.slice('/api/chat/slots/'.length).includes('/')) {
    await json(route, { messages: [], has_more: false, total: 0 }); return true
  }
  if (path === '/api/sessions') { await json(route, { sessions: [], has_more: false }); return true }
  if (path === '/api/chat/pins') { await json(route, { pins: [] }); return true }
  return false
}

let failures = 0
const assert = (ok, label) => { console.log(`ASSERT ${ok ? 'ok  ' : 'FAIL'}: ${label}`); if (!ok) failures++ }
// Slide-mode drawers are mounted during their motion; wait for their real
// destination before measuring a hit point or capturing the row.
const waitForMobileDrawer = page => page.waitForFunction(() => {
  const drawer = document.querySelector('.mobile-sessions-overlay')
  return drawer && drawer.getBoundingClientRect().left === parseFloat(getComputedStyle(drawer).left)
})

// Deliver Safari's no-focus sequence without compatibility mouse events.
async function noFocusTouch(page, mark, label) {
  await page.evaluate(() => document.activeElement?.blur())
  const tap = async () => {
    await mark.dispatchEvent('pointerdown', { pointerType: 'touch', bubbles: true })
    await mark.dispatchEvent('pointerup', { pointerType: 'touch', bubbles: true })
  }
  await tap()
  assert(await page.getByRole('tooltip').count() === 1, `${label}: first no-focus pointerup opens`)
  assert(await mark.evaluate(el => document.activeElement !== el), `${label}: no focus was delivered`)
  await mark.evaluate(el => el.parentElement.dispatchEvent(new Event('scroll')))
  assert(await page.getByRole('tooltip').count() === 0, `${label}: anchor-moving scroll closes`)
  await tap()
  assert(await page.getByRole('tooltip').count() === 1, `${label}: no-focus retap reopens`)
  await page.screenshot({ path: `${OUT}/${PREFIX}-${label}-no-focus-retap.png` })
  await page.locator('body').dispatchEvent('pointerdown', { pointerType: 'touch', bubbles: true })
  assert(await page.getByRole('tooltip').count() === 0, `${label}: outside pointerdown closes without blur`)
  await page.screenshot({ path: `${OUT}/${PREFIX}-${label}-outside-close.png` })
}

async function checkNoStatusRow(page, label) {
  const card = page.locator(`[data-session-row="${NO_STATUS}"]`)
  await card.waitFor({ state: 'visible' })
  const mark = card.getByTestId('conductor-mode-title-mark')
  // An actual keyboard event establishes :focus-visible rather than styling it.
  await page.keyboard.press('Tab')
  await mark.focus()
  const geo = await mark.evaluate(bolt => {
    const column = bolt.parentElement, b = bolt.getBoundingClientRect(), c = column.getBoundingClientRect()
    const title = bolt.nextElementSibling, t = title.getBoundingClientRect()
    const css = getComputedStyle(column), outline = getComputedStyle(bolt)
    const corners = [[b.left + 0.5, b.top + 0.5], [b.right - 0.5, b.top + 0.5], [b.left + 0.5, b.bottom - 0.5], [b.right - 0.5, b.bottom - 0.5]]
    const out = Math.max(0, parseFloat(outline.outlineOffset) + parseFloat(outline.outlineWidth))
    const ring = { left: b.left - out, top: b.top - out, right: b.right + out, bottom: b.bottom + out }
    const clips = []
    for (let el = column; el; el = el.parentElement) {
      const s = getComputedStyle(el), r = el.getBoundingClientRect()
      if (s.overflowX !== 'visible') clips.push(ring.left >= r.left && ring.right <= r.right)
      if (s.overflowY !== 'visible') clips.push(ring.top >= r.top && ring.bottom <= r.bottom)
    }
    const row = bolt.closest('[data-session-row]'), rowCss = getComputedStyle(row)
    const trailingH = Math.max(0, ...[...row.children].filter(el => el !== column && getComputedStyle(el).position !== 'absolute').map(el => el.getBoundingClientRect().height))
    return {
      columnH: c.height, titleH: t.height, boltW: b.width, boltH: b.height,
      rowH: row.getBoundingClientRect().height, trailingH,
      rowPadding: parseFloat(rowCss.paddingTop) + parseFloat(rowCss.paddingBottom),
      summary: !!column.querySelector('[data-testid="crew-root-summary"]'),
      meta: column.querySelector('.session-agent-label').textContent,
      overflowX: css.overflowX, overflowY: css.overflowY,
      corners: corners.map(([x, y]) => bolt.contains(document.elementFromPoint(x, y))),
      focusVisible: bolt.matches(':focus-visible'), outlineStyle: outline.outlineStyle, outlineWidth: outline.outlineWidth,
      ringUnclipped: clips.every(Boolean),
    }
  })
  console.log(`${label}: no-status geometry ${JSON.stringify(geo)}`)
  assert(!geo.summary && geo.meta === '' && geo.columnH === 20, `${label}: fixture has no meta or status height`)
  assert(geo.boltW === 24 && geo.boltH === 24 && geo.titleH === 20, `${label}: bolt and title sizes stay fixed`)
  assert(geo.overflowX === 'clip' && geo.overflowY === 'visible', `${label}: built overflow-x-clip preserves vertical overflow`)
  assert(geo.corners.length === 4 && geo.corners.every(Boolean), `${label}: all four bolt corners hit the bolt`)
  assert(geo.focusVisible && geo.outlineStyle !== 'none' && parseFloat(geo.outlineWidth) === 2 && geo.ringUnclipped, `${label}: full keyboard focus ring is unclipped`)
  await card.screenshot({ path: `${OUT}/${PREFIX}-${label}-no-status-focus.png` })
  await page.evaluate(() => document.activeElement?.blur())
  const long = page.locator('[data-session-row="chat-long"]')
  const horizontal = await long.evaluate(el => {
    const title = el.querySelector('[data-session-title]'), tag = el.querySelector('[data-testid="slot-tag-long-tag"]')?.parentElement
    const column = title.parentElement
    return {
      titleClipped: title.scrollWidth > title.clientWidth && getComputedStyle(title).overflowX === 'hidden',
      tagClipped: !!tag && tag.scrollWidth > tag.clientWidth && getComputedStyle(tag).overflowX === 'hidden',
      overflowX: getComputedStyle(column).overflowX, overflowY: getComputedStyle(column).overflowY,
      rowH: el.getBoundingClientRect().height,
    }
  })
  console.log(`${label}: long title/tag ${JSON.stringify(horizontal)}`)
  assert(horizontal.titleClipped && horizontal.tagClipped && horizontal.overflowX === 'clip', `${label}: long title and tag still clip horizontally`)
  const plainH = await page.locator('[data-session-row="chat-plain"]').evaluate(el => el.getBoundingClientRect().height)
  // The phone's in-flow kebab can exceed a bare title; neither row grows for the bolt.
  assert(geo.rowH === Math.max(geo.titleH, geo.trailingH) + geo.rowPadding && horizontal.rowH === plainH, `${label}: no-status and long-content row heights stay unchanged`)
  await noFocusTouch(page, mark, label)
}

async function main() {
  const { srv, base } = await serveDist()
  const { LD_LIBRARY_PATH: _mise, ...env } = process.env
  const browser = await chromium.launch({ env })
  for (const theme of ['dark', 'light']) {
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots,
      theme,
      localStorageEntries: {
        'mc-active-slot-chat': ROOT,
        'mc-lang': 'en',
        'kc-locked-controls': '[]',
      },
      extra,
    })
    await page.goto(`${base}/chat?sid=${ROOT}`, { waitUntil: 'domcontentloaded' })
    const card = page.locator('[data-crew-root="true"]').first()
    await card.waitFor({ timeout: 20_000 })
    await page.getByTestId('crew-root-done').waitFor({ state: 'visible' })
    // The fixture rows are older than the sleeping-session cutoff: show them, so
    // a plain row sits beside the conductor row and the new chat can be opened.
    await page.locator('[data-testid^="stale-expander-"]').first().click()
    await page.locator('[data-session-row$="chat-plain"]').waitFor({ timeout: 5_000 })
    // Off the expander, so its own hint does not cover the rows in the frames.
    await page.mouse.move(640, 790)
    await page.evaluate(() => { document.activeElement?.blur() })
    await page.getByRole('tooltip').waitFor({ state: 'detached', timeout: 5_000 }).catch(() => {})
    await checkNoStatusRow(page, theme)
    const mark = card.getByTestId('conductor-mode-title-mark')
    const composerMark = page.getByTestId('crew-mode-composer-chip')
    const badge = await card.getByTestId('crew-ghost-badge').count()
    const modeText = await card.getByText('Conductor mode', { exact: true }).count()
    const titleWidth = await card.locator('[data-session-title]').first().evaluate(el => el.getBoundingClientRect().width)
    console.log(`${PREFIX}/${theme}: badge=${badge} mode-text=${modeText} title-width=${titleWidth.toFixed(1)}px`)
    if (PREFIX === 'after') {
      assert(badge === 0, `${theme}: no ghost badge before the title`)
      assert(modeText === 0, `${theme}: no repeated Conductor mode text`)
      assert((await mark.count()) === 1, `${theme}: one bolt before the title`)
      // The layout the row promises: the bolt's 24px box sits outside the
      // title's 20px line box, so a conductor row is a plain row's height.
      const geo = await card.evaluate(el => {
        const title = el.querySelector('[data-session-title]')
        const bolt = el.querySelector('[data-testid="conductor-mode-title-mark"]')
        const text = [...title.childNodes].find(n => n.nodeType === 3 && n.textContent.trim())
        const range = document.createRange(); range.selectNodeContents(text)
        const t = title.getBoundingClientRect(), b = bolt.getBoundingClientRect()
        const plain = document.querySelector('[data-session-row$="chat-plain"]')
        return {
          inTitle: title.contains(bolt), titleH: t.height, rowH: el.getBoundingClientRect().height,
          plainH: plain.getBoundingClientRect().height, plainTitleH: plain.querySelector('[data-session-title]').getBoundingClientRect().height,
          // The fixture's plain row has no status line; the root has its summary.
          // So the two rows differ by exactly that line, and by nothing the bolt adds.
          summaryH: el.querySelector('[data-testid="crew-root-summary"]')?.getBoundingClientRect().height ?? 0,
          boltW: b.width, boltH: b.height, boltMid: (b.top + b.bottom) / 2, lineMid: t.top + 10,
          boltRight: b.right, textLeft: range.getClientRects()[0].left,
          hit: document.elementFromPoint(b.left + 1, b.top + 1) === bolt && document.elementFromPoint(b.right - 1, b.bottom - 1) === bolt,
        }
      })
      console.log(`${PREFIX}/${theme}: geometry ${JSON.stringify(geo)}`)
      assert(!geo.inTitle, `${theme}: bolt is outside the title's line box`)
      assert(geo.titleH === geo.plainTitleH && geo.titleH === 20, `${theme}: conductor title line is the plain 20px line`)
      assert(geo.summaryH > 0 && geo.rowH === geo.plainH + geo.summaryH, `${theme}: conductor row is a plain row plus its status line, nothing more`)
      assert(geo.boltW === 24 && geo.boltH === 24, `${theme}: Sessions bolt keeps its real 24x24 box`)
      assert(!(await mark.evaluate(el => el.classList.contains('mc-touch-hit'))), `${theme}: Sessions bolt does not expand into the title`)
      assert(await composerMark.evaluate(el => el.classList.contains('mc-touch-hit')), `${theme}: isolated composer bolt retains touch expansion`)
      assert(geo.hit, `${theme}: the whole 24px box takes the hit (no clipped corner)`)
      assert(Math.abs(geo.boltMid - geo.lineMid) <= 1, `${theme}: bolt is centred on the title line`)
      assert(geo.textLeft >= geo.boltRight, `${theme}: title text starts after the bolt's box`)
      assert((await composerMark.textContent()) === '', `${theme}: composer mark is only a bolt`)
    }
    // The row and the rows around it: the whole session-list width, a little above and below.
    const box = await card.boundingBox()
    const x = Math.max(0, box.x - 12)
    const y = Math.max(0, box.y - 40)
    await page.screenshot({ path: `${OUT}/${PREFIX}-${theme}.png`, clip: { x, y, width: box.width + 24, height: box.height + 140 } })
    await page.screenshot({ path: `${OUT}/${PREFIX}-${theme}-full.png` })
    if (theme === 'dark') {
      await mark.focus()
      const rowTip = page.getByRole('tooltip')
      await rowTip.waitFor({ timeout: 5_000 })
      assert((await rowTip.textContent()) === 'Conductor mode', 'dark: focused Sessions bolt names the mode')
      const [rowTipBox, markBox] = [await rowTip.boundingBox(), await mark.boundingBox()]
      assert(rowTipBox.y >= markBox.y + markBox.height, 'dark: Sessions tip opens below the bolt, clear of search')
      await page.screenshot({ path: `${OUT}/${PREFIX}-dark-row-tip.png` })
      await composerMark.focus()
      const tip = page.getByRole('tooltip')
      await tip.waitFor({ timeout: 5_000 })
      assert((await tip.textContent()) === STATUS, 'dark: focused composer bolt states the mode as a status')
      await page.screenshot({ path: `${OUT}/${PREFIX}-dark-tip.png` })
    }
    // The composer bolt stops no press, so a real mouse press on it is a press
    // outside the open attach menu, whose closer listens for document mousedown.
    await page.evaluate(() => { document.activeElement?.blur() })
    const plus = page.getByRole('button', { name: 'Add files & options' })
    await plus.click()
    const upload = page.getByRole('button', { name: 'Upload file' })
    await upload.waitFor({ state: 'visible', timeout: 5_000 })
    assert((await plus.getAttribute('aria-expanded')) === 'true', `${theme}: composer attach menu opens`)
    await page.screenshot({ path: `${OUT}/${PREFIX}-${theme}-attach-open.png` })
    const boltBox = await composerMark.boundingBox()
    await page.mouse.click(boltBox.x + boltBox.width / 2, boltBox.y + boltBox.height / 2)
    await upload.waitFor({ state: 'detached', timeout: 2_000 }).catch(() => {})
    assert((await plus.getAttribute('aria-expanded')) === 'false' && (await upload.count()) === 0, `${theme}: pressing the composer bolt closes the open attach menu`)
    await page.screenshot({ path: `${OUT}/${PREFIX}-${theme}-attach-bolt-press.png` })
    await page.keyboard.press('Escape')
    await page.mouse.move(0, 0)
    // A new conductor chat, before the first message: the turn-off control is
    // the labelled, pressed chip, not the read-only bolt.
    await page.evaluate(() => { document.activeElement?.blur() })
    await page.locator(`[data-session-row$="${NEW}"]`).click()
    const pressed = page.locator('[data-testid="crew-mode-chip"][aria-pressed="true"]')
    await pressed.waitFor({ timeout: 10_000 })
    assert((await page.getByTestId('crew-mode-composer-chip').count()) === 0, `${theme}: no read-only bolt before the first message`)
    assert((await pressed.textContent()).includes('Conductor mode'), `${theme}: pressed turn-off chip keeps its name`)
    const dock = await pressed.evaluate(el => {
      const r = el.closest('[data-testid="composer-dock-root"]').getBoundingClientRect()
      return { x: r.x, y: r.y, width: r.width, height: r.height }
    })
    const near = { x: Math.max(0, dock.x - 8), y: Math.max(0, dock.y - 8), width: dock.width + 16, height: dock.height + 16 }
    await page.mouse.move(0, 0)
    await page.screenshot({ path: `${OUT}/${PREFIX}-${theme}-new.png`, clip: near })
    await pressed.hover()
    // Settled hover: the pointer is on the chip and its transitions have ended.
    await pressed.evaluate(el => el.matches(':hover') && Promise.all(el.getAnimations({ subtree: true }).map(a => a.finished)))
    assert(await pressed.evaluate(el => el.matches(':hover')), `${theme}: pressed chip is hovered`)
    await page.screenshot({ path: `${OUT}/${PREFIX}-${theme}-new-hover.png`, clip: near })
    await context.close()

    if (theme === 'dark') {
      const mobileContext = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, hasTouch: true })
      const mobile = await mobileContext.newPage()
      logPageProblems(mobile)
      await stubDashboardApi(mobile, {
        slots,
        theme,
        localStorageEntries: {
          'mc-active-slot-chat': ROOT,
          'mc-lang': 'en',
          'kc-locked-controls': '[]',
        },
        extra,
      })
      await mobile.goto(`${base}/chat?sid=${ROOT}`, { waitUntil: 'domcontentloaded' })
      const mobileComposerMark = mobile.getByTestId('crew-mode-composer-chip')
      await mobileComposerMark.waitFor({ timeout: 20_000 })
      const composerTarget = await mobileComposerMark.evaluate(el => {
        const b = el.getBoundingClientRect(), pseudo = getComputedStyle(el, '::after')
        return {
          expanded: el.classList.contains('mc-touch-hit'),
          width: b.width - parseFloat(pseudo.left) - parseFloat(pseudo.right),
          height: b.height - parseFloat(pseudo.top) - parseFloat(pseudo.bottom),
        }
      })
      console.log(`${PREFIX}/dark-mobile: composer target ${JSON.stringify(composerTarget)}`)
      assert(composerTarget.expanded && composerTarget.width === 44 && composerTarget.height === 44, 'dark mobile: isolated composer bolt keeps its 44px target')
      await mobile.screenshot({ path: `${OUT}/${PREFIX}-dark-mobile.png` })
      await mobile.getByTestId('crew-mode-composer-chip').tap()
      const composerTip = mobile.getByRole('tooltip')
      await composerTip.waitFor({ timeout: 5_000 })
      assert((await composerTip.textContent()) === STATUS, 'dark mobile: tapped composer bolt states the mode')
      await mobile.screenshot({ path: `${OUT}/${PREFIX}-dark-mobile-tip.png` })
      await mobileComposerMark.evaluate(el => el.parentElement.dispatchEvent(new Event('scroll')))
      await mobile.getByRole('tooltip').waitFor({ state: 'detached' })
      await mobileComposerMark.tap()
      await composerTip.waitFor({ state: 'visible' })
      assert((await composerTip.textContent()) === STATUS, 'dark mobile: native retap after scroll reopens without a new focus')
      await mobile.locator('body').dispatchEvent('pointerdown', { pointerType: 'touch', bubbles: true })
      await mobile.getByRole('tooltip').waitFor({ state: 'detached' })
      await mobile.evaluate(() => { document.activeElement?.blur() })
      await mobile.getByTestId('mobile-topbar-sessions-toggle').click()
      await mobile.locator('[data-crew-root="true"]').first().waitFor({ timeout: 10_000 })
      await waitForMobileDrawer(mobile)
      await mobile.screenshot({ path: `${OUT}/${PREFIX}-dark-drawer.png` })
      const mobileMark = mobile.locator(`[data-session-row="${ROOT}"]`).getByTestId('conductor-mode-title-mark')
      await mobileMark.tap()
      const mobileTip = mobile.getByRole('tooltip')
      await mobileTip.waitFor({ timeout: 5_000 })
      assert((await mobileTip.textContent()) === 'Conductor mode', 'dark mobile: tapped Sessions bolt names the mode')
      const mobileTipBox = await mobileTip.boundingBox()
      assert(mobileTipBox !== null && mobileTipBox.width > 0 && mobileTipBox.height > 0, 'dark mobile: tapped Sessions tooltip has a painted box')
      assert((await mobile.locator('[data-crew-root="true"]').count()) > 0, 'dark mobile: tapping the Sessions bolt keeps the drawer open')
      await mobileTip.screenshot({ path: `${OUT}/${PREFIX}-dark-drawer-tip-bubble.png` })
      await mobile.screenshot({ path: `${OUT}/${PREFIX}-dark-drawer-tip.png` })

      // Hold one native finger past the row's TouchSensor delay (250ms). The
      // bolt stays still, and held past Radix's 700ms touch hold it opens no
      // row context menu; the same hold on the title lifts the row.
      // A tap need not focus the bolt, so close its tip by an outside press.
      const tapFocused = await mobileMark.evaluate(el => document.activeElement === el)
      console.log(`${PREFIX}/dark-mobile: tapped Sessions bolt focused=${tapFocused}`)
      await mobile.locator('body').dispatchEvent('pointerdown', { pointerType: 'touch', bubbles: true })
      await mobile.evaluate(() => { document.activeElement?.blur() })
      await mobile.getByRole('tooltip').waitFor({ state: 'detached' })
      const cdp = await mobileContext.newCDPSession(mobile)
      const lifted = () => mobile.evaluate(() => [...document.querySelectorAll('.session-row[data-crew-root="true"]')].some(el => el.classList.contains('opacity-40')))
      const menus = () => mobile.locator('[role="menu"]').count()
      const holdAt = async (box, name, ms) => {
        assert(box !== null, `dark mobile: ${name} has a painted box`)
        const point = { x: box.x + box.width / 2, y: box.y + box.height / 2 }
        await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [point] })
        await mobile.evaluate(wait => new Promise(resolve => setTimeout(resolve, wait)), ms)
        const held = { lifted: await lifted(), menus: await menus() }
        await mobile.screenshot({ path: `${OUT}/${PREFIX}-dark-drawer-${name}-hold.png` })
        await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
        // A long press can also fire a native contextmenu as the finger lifts.
        await mobile.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
        held.menusAfter = await menus()
        console.log(`${PREFIX}/dark-mobile: ${name} hold ${ms}ms ${JSON.stringify(held)}`)
        return held
      }
      const rootTitle = mobile.locator(`[data-session-row="${ROOT}"] [data-session-title]`).first()
      const boltHold = await holdAt(await mobileMark.boundingBox(), 'bolt', 800)
      assert(!boltHold.lifted, 'dark mobile: an 800ms press on the Sessions bolt does not lift the row')
      assert(boltHold.menus === 0 && boltHold.menusAfter === 0, 'dark mobile: an 800ms press on the Sessions bolt paints no row context menu')
      if (boltHold.menusAfter > 0) {
        await mobile.keyboard.press('Escape')
        await mobile.locator('[role="menu"]').waitFor({ state: 'detached', timeout: 2_000 }).catch(() => {})
      }
      const titleBox = await rootTitle.boundingBox()
      assert(titleBox !== null, 'dark mobile: the title has a painted box')
      assert((await holdAt({ x: titleBox.x + 40, y: titleBox.y, width: 20, height: titleBox.height }, 'title', 400)).lifted, 'dark mobile: the same long press on the title lifts the row')
      await mobile.waitForFunction(() => ![...document.querySelectorAll('.session-row[data-crew-root="true"]')].some(el => el.classList.contains('opacity-40')))
      await mobile.evaluate(() => new Promise(resolve => setTimeout(resolve, 100)))
      await cdp.detach()

      // Switch away first: a tap on the root's visible title must switch back,
      // not land on the bolt's invisible coarse-pointer extension.
      await mobile.evaluate(() => { document.activeElement?.blur() })
      await mobile.getByRole('tooltip').waitFor({ state: 'detached' })
      await mobile.locator('[data-testid^="stale-expander-"]').first().click()
      await mobile.locator('[data-session-row="chat-long"]').waitFor({ state: 'visible' })
      await checkNoStatusRow(mobile, 'dark-mobile')
      await mobile.locator('[data-session-row$="chat-plain"]').click()
      await mobile.waitForURL(url => url.searchParams.get('sid') === 'chat-plain')
      await mobile.getByTestId('mobile-topbar-sessions-toggle').click()
      await mobileMark.waitFor({ state: 'visible' })
      await waitForMobileDrawer(mobile)
      const touchGeo = await mobileMark.evaluate(bolt => {
        const title = bolt.nextElementSibling
        const text = [...title.childNodes].find(n => n.nodeType === 3 && n.textContent.trim())
        const range = document.createRange(); range.setStart(text, 0); range.setEnd(text, 1)
        const glyph = range.getBoundingClientRect(), b = bolt.getBoundingClientRect()
        const x = glyph.left + Math.min(2, glyph.width / 2), y = (glyph.top + glyph.bottom) / 2
        const hit = document.elementFromPoint(x, y)
        const pseudo = getComputedStyle(bolt, '::after')
        return {
          coarse: matchMedia('(pointer: coarse)').matches,
          boltW: b.width, boltH: b.height, titleH: title.getBoundingClientRect().height,
          expanded: bolt.classList.contains('mc-touch-hit'), pseudoContent: pseudo.content, pseudoLeft: pseudo.left,
          x, y, outsideBox: x > b.right, hitMark: bolt.contains(hit), hitTitle: title.contains(hit),
        }
      })
      console.log(`${PREFIX}/dark-mobile: touch geometry ${JSON.stringify(touchGeo)}`)
      assert(touchGeo.coarse, 'dark mobile: regression uses a coarse pointer')
      assert(touchGeo.boltW === 24 && touchGeo.boltH === 24 && touchGeo.titleH === 20, 'dark mobile: real 24px bolt and 20px title line stay unchanged')
      assert(!touchGeo.expanded, 'dark mobile: Sessions bolt has no touch expansion class')
      assert(touchGeo.outsideBox && touchGeo.hitTitle && !touchGeo.hitMark, 'dark mobile: visible title text hits the title, not the bolt')
      // An exact-point click isolates the authored hit box under coarse CSS:
      // Chromium can otherwise snap a near-edge touch to a neighbouring button.
      await mobile.mouse.click(touchGeo.x, touchGeo.y)
      await mobile.waitForFunction(root => new URL(location.href).searchParams.get('sid') === root || document.querySelector('[role="tooltip"]')?.textContent === 'Conductor mode', ROOT)
      const switched = new URL(mobile.url()).searchParams.get('sid') === ROOT
      assert(switched, 'dark mobile: exact visible title point switches the inactive root row under coarse CSS')
      assert(await mobile.getByRole('tooltip').count() === 0, 'dark mobile: title point opens no bolt help')
      if (switched) {
        await mobileMark.waitFor({ state: 'hidden' })
        await mobile.getByTestId('mobile-topbar-sessions-toggle').click()
        await mobile.locator('[data-session-row$="chat-plain"]').tap()
        await mobile.waitForURL(url => url.searchParams.get('sid') === 'chat-plain')
        await mobile.getByTestId('mobile-topbar-sessions-toggle').click()
        const title = mobile.locator('[data-crew-root="true"] [data-session-title]').first()
        await title.tap()
        await mobile.waitForURL(url => url.searchParams.get('sid') === ROOT)
        await mobileMark.waitFor({ state: 'hidden' })
        assert(await mobile.getByRole('tooltip').count() === 0, 'dark mobile: native touch on the visible title switches without bolt help')
      }
      await mobile.screenshot({ path: `${OUT}/${PREFIX}-dark-title-tap.png` })
      await mobileContext.close()
    }
  }
  await browser.close()
  srv.close()
  console.log(failures ? `FAILED: ${failures} assertion(s)` : 'all assertions passed')
  process.exit(failures ? 1 : 0)
}

main().catch(e => { console.error(e); process.exit(2) })
