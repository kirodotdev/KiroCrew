/**
 * Screenshot harness for the config-changed signal.
 *
 * Frames, against the built SPA with the gateway stubbed:
 *   01 a chat whose agent process runs on outdated config: the "Config
 *      changed · Reload" button right after the session title, and the alert
 *      mark on its sidebar row, beside a current chat that shows neither. The
 *      title is hovered: while stale it reveals no Rename or Auto-title
 *      button, so the row holds two controls (session menu, Reload);
 *   02 the button's tooltip. Headless Chromium paints no native `title`
 *      tooltip, so the capture draws the button's own title text beside it;
 *      the words are the ones the browser shows on hover;
 *   03 the session menu, the second path: the Reload row's note, naming
 *      what changed, "config changed (<file>) · reload to apply";
 *   04 the same while a turn runs: the header button disabled, and the row's
 *      note "config changed (<file>) — reload when the turn ends";
 *   05 the generic tooltip of a stale chat whose changed files have no
 *      label (empty `config_stale_inputs`);
 *   06 the same while sub-agents are working: the button disabled, and
 *      "config changed (<file>) — reload when sub-agents finish";
 *   07 the sidebar row's alert mark and its tooltip;
 *   08 a phone-width viewport (390px) with a long session title: the bar's
 *      title/menu trigger carrying the alert glyph and "Config · Reload"
 *      before the title, and no Reload button beside it;
 *   09 the session menu of the empty-inputs chat from 05: the Reload row's
 *      generic note, "config changed · reload to apply";
 *   10 that chat's sidebar mark tooltip, the generic wording;
 *   11 the session menu at a 320px phone width, opened from the cue-carrying
 *      title trigger, in English: the Reload row's
 *      note stacked under the label, the remedy wrapping rather than running
 *      off-screen;
 *   12 the same at 320px in Japanese, the longest rendering of that row;
 *   13 the header button after a reload the gateway refused (a stubbed 409,
 *      sub-agents still running): the failure in the chat's action-error
 *      notice above the composer, with its agent hand-off, no notice of its
 *      own beside the button (that would be a third control in the row), and
 *      no alert dialog.
 *
 * Exits non-zero, writing nothing further, when an element does not render,
 * when the header button is not right after the title, or when a click on it
 * does not POST the session's reload.
 *
 * Usage:
 *   npm run build
 *   node scripts/capture-stale-config-badge.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/stale-config-badge'
mkdirSync(OUT, { recursive: true })

const now = Date.now()
const iso = ms => new Date(now - ms).toISOString()
// Two changed inputs, long enough that the Reload row's list truncates: the
// frames then show the remedy clause after it staying whole.
const INPUTS = '~/.kiro/agents/kirocrew.json, ~/.kiro/settings/mcp.json'
const STALE = {
  key: 'chat-1', title: 'Wire up the GitHub MCP server', messages: 2, running: false,
  agent: 'kirocrew', created: iso(3_600_000), last_ts: iso(30_000), folder_id: '',
  config_stale: true, config_stale_inputs: INPUTS,
}
const CURRENT = {
  key: 'chat-2', title: 'Triage the flaky test', messages: 2, running: false,
  agent: 'kirocrew', created: iso(7_200_000), last_ts: iso(600_000), folder_id: '',
  config_stale: false, config_stale_inputs: '',
}
const MESSAGES = [
  { role: 'user', content: 'Enable the GitHub MCP server for this chat.', ts: iso(120_000), meta: { mid: 'm-1' } },
  { role: 'assistant', content: 'Done — the server is enabled in the agent config.', ts: iso(110_000), meta: { mid: 'm-2' } },
]
const TOOLTIP = `${INPUTS} changed since this session started. Reload the session to apply.`
const GENERIC_TOOLTIP = "This session's config changed since it started. Reload the session to apply."
const NOTE_INPUTS = `config changed (${INPUTS})`
const GENERIC_NOTE = 'config changed · reload to apply'
const BUTTON = '[data-testid="stale-config-reload"]'
const MARK = '[data-testid="stale-config-badge"]'

/** The phone bar: the cue sits inside the title trigger, inside the viewport,
 *  and the bar renders no Reload button beside it. */
async function assertPhoneCue(page, width, text) {
  const trigger = page.locator('[data-testid="session-title-menu"]')
  await trigger.waitFor({ state: 'visible', timeout: 10_000 })
  const cue = trigger.locator('[data-testid="stale-config-cue"]')
  if ((await cue.count()) !== 1) throw new Error('the phone title trigger carries no stale-config cue')
  if ((await cue.locator('.lucide-circle-alert').count()) !== 1) throw new Error('the cue has no alert glyph')
  if (text && (await cue.innerText()).trim() !== text) throw new Error(`unexpected cue text: ${await cue.innerText()}`)
  const box = await cue.boundingBox()
  if (!box || box.x < 0 || box.x + box.width > width) throw new Error(`the cue is outside the ${width}px viewport`)
  if ((await page.locator(BUTTON).count()) !== 0) throw new Error('the phone bar renders a Reload button beside the title')
}

/** Open the session menu from the desktop row's chevron. */
async function openMenu(page) {
  await page.getByRole('button', { name: 'Session options' }).first().click()
}

/** Draw *text* below *el*, as the browser's native `title` tooltip would show it. */
async function drawTooltip(page, el, text) {
  await el.hover()
  const box = await el.boundingBox()
  if (!box) throw new Error('nothing to draw a tooltip under')
  await page.evaluate(({ text, left, top }) => {
    const tip = document.createElement('div')
    tip.textContent = text
    Object.assign(tip.style, {
      position: 'fixed', left: `${left}px`, top: `${top}px`, maxWidth: '420px', zIndex: '99999',
      background: '#f5f5f5', color: '#111', font: '12px system-ui, sans-serif', padding: '4px 7px',
      border: '1px solid #999', borderRadius: '3px', boxShadow: '0 2px 6px rgba(0,0,0,.35)',
    })
    document.body.appendChild(tip)
  }, { text, left: box.x, top: box.y + box.height + 6 })
  await page.waitForTimeout(200)
}

async function scene(browser, base, slots = [STALE, CURRENT], onSocket = null, viewport = { width: 1280, height: 820 }, lang = null, extraRoute = null) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, {
    slots,
    localStorageEntries: lang ? { 'mc-lang': lang } : null,
    extra: async (path, route) => {
      if (path === '/api/chat/slots/chat-1') {
        await json(route, { messages: MESSAGES, has_more: false, total: MESSAGES.length })
        return true
      }
      return extraRoute ? extraRoute(path, route) : false
    },
  })
  let socket = null
  await page.routeWebSocket(/\/api\/ws/, ws => { socket = ws })
  logPageProblems(page)
  await page.goto(`${base}/chat?sid=chat-1`, { waitUntil: 'domcontentloaded' })
  // The list's aria-label is translated; another locale waits on the message text alone.
  if (!lang) await page.waitForSelector('[aria-label="Chat messages"]', { timeout: 20_000 })
  await page.getByText('Done — the server is enabled', { exact: false }).first().waitFor({ state: 'visible', timeout: 15_000 })
  if (onSocket) {
    for (let i = 0; i < 100 && !socket; i++) await page.waitForTimeout(100)
    if (!socket) throw new Error('the dashboard never opened its websocket')
    await onSocket(socket)
    await page.waitForTimeout(300)
  }
  await page.mouse.move(900, 700)
  await page.waitForTimeout(400)
  return { context, page }
}

async function main() {
  const { srv, base } = await serveDist()
  const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
  const browser = await chromium.launch({ env: browserEnv })
  try {
    const { context, page } = await scene(browser, base)
    const button = page.locator(BUTTON)
    await button.waitFor({ state: 'visible', timeout: 10_000 })
    if ((await button.count()) !== 1) throw new Error(`expected 1 header button, saw ${await button.count()}`)
    if ((await page.locator(MARK).count()) !== 1) throw new Error('expected one sidebar mark, on the stale chat only')
    if ((await button.innerText()).trim() !== 'Config changed · Reload') throw new Error(`unexpected label: ${await button.innerText()}`)
    // Right after the title: its left edge past the title's right edge, on the same row.
    const titleBox = await page.locator('[aria-label="Session options"]').first().evaluate(el => {
      const row = el.closest('.group\\/header')
      const r = (row ?? el).getBoundingClientRect()
      return { right: r.right, top: r.top, bottom: r.bottom }
    })
    const buttonBox = await button.boundingBox()
    if (!buttonBox || buttonBox.x < titleBox.right || buttonBox.y > titleBox.bottom) throw new Error('the button is not right after the title')
    const title = await button.getAttribute('title')
    if (title !== TOOLTIP) throw new Error(`unexpected tooltip: ${title}`)
    // Hovering the title reveals no Rename / Auto-title button while stale: the
    // row stays at the session menu and Reload.
    const cluster = page.locator('[aria-label="Session options"]').first().locator('xpath=ancestor::*[contains(@class, "group/header")][1]')
    await cluster.locator('[data-ui-location="chat.session-title"]').hover()
    await page.waitForTimeout(300)
    if ((await page.getByRole('button', { name: /Regenerate title/ }).count()) !== 0) throw new Error('the stale chat title reveals an Auto-title button on hover')
    if ((await cluster.locator('.lucide-pen, .lucide-sparkles').count()) !== 0) throw new Error('the stale chat title reveals a Rename or Auto-title control on hover')
    const rowButtons = await cluster.getByRole('button').evaluateAll(els => els.filter(el => !el.hasAttribute('data-ui-location')).length)
    if (rowButtons !== 1) throw new Error(`expected the session menu alone beside the title, saw ${rowButtons} buttons`)
    await page.screenshot({ path: `${OUT}/01-header-button-and-sidebar.png` })
    console.log('captured 01-header-button-and-sidebar.png')

    await page.mouse.move(900, 700)
    await page.waitForTimeout(200)

    // 02: draw the title text the browser shows on hover.
    await drawTooltip(page, button, title)
    await page.screenshot({ path: `${OUT}/02-button-tooltip.png` })
    console.log('captured 02-button-tooltip.png')
    await page.evaluate(() => document.querySelectorAll('body > div[style*="z-index: 99999"]').forEach(el => el.remove()))

    // One click reloads: the button POSTs the session's reload route.
    const reloaded = page.waitForRequest(req => req.method() === 'POST' && req.url().includes('/api/chat/slots/chat-1/reload'), { timeout: 5_000 })
    await button.click()
    await reloaded
    await page.waitForTimeout(300)

    // 03: the second path, the session menu's Reload row.
    await openMenu(page)
    const note = page.locator('[data-testid="reload-stale-note"]').first()
    await note.waitFor({ state: 'visible', timeout: 10_000 })
    const noteText = (await note.textContent()) ?? ''
    if (noteText !== `${NOTE_INPUTS} · reload to apply`) throw new Error(`unexpected note: ${noteText}`)
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/03-session-menu-note.png` })
    console.log('captured 03-session-menu-note.png')
    await context.close()

    // 04: a turn is running, so Reload is blocked in both places.
    {
      const { context: ctx4, page: p4 } = await scene(browser, base, [{ ...STALE, running: true }, CURRENT])
      if (!(await p4.locator(BUTTON).isDisabled())) throw new Error('the header button is enabled while a turn runs')
      await openMenu(p4)
      const blocked = p4.locator('[data-testid="reload-stale-note"]').first()
      await blocked.waitFor({ state: 'visible', timeout: 10_000 })
      const blockedText = (await blocked.textContent()) ?? ''
      if (blockedText !== `${NOTE_INPUTS} — reload when the turn ends`) throw new Error(`unexpected note: ${blockedText}`)
      await p4.waitForTimeout(300)
      await p4.screenshot({ path: `${OUT}/04-blocked-while-turn-runs.png` })
      console.log('captured 04-blocked-while-turn-runs.png')
      await ctx4.close()
    }

    // 05: no changed file named, so the tooltip is the generic one.
    {
      const { context: ctx5, page: p5 } = await scene(browser, base, [{ ...STALE, config_stale_inputs: '' }, CURRENT])
      const generic = p5.locator(BUTTON)
      await generic.waitFor({ state: 'visible', timeout: 10_000 })
      const genericTitle = await generic.getAttribute('title')
      if (genericTitle !== GENERIC_TOOLTIP) throw new Error(`unexpected tooltip: ${genericTitle}`)
      await drawTooltip(p5, generic, genericTitle)
      await p5.screenshot({ path: `${OUT}/05-button-generic-tooltip.png` })
      console.log('captured 05-button-generic-tooltip.png')
      await ctx5.close()
    }

    // 09: the same empty-inputs chat's session menu: the Reload row's generic note.
    {
      const { context: ctx9, page: p9 } = await scene(browser, base, [{ ...STALE, config_stale_inputs: '' }, CURRENT])
      await openMenu(p9)
      const note9 = p9.locator('[data-testid="reload-stale-note"]').first()
      await note9.waitFor({ state: 'visible', timeout: 10_000 })
      const note9Text = (await note9.textContent()) ?? ''
      if (note9Text !== GENERIC_NOTE) throw new Error(`unexpected note: ${note9Text}`)
      await p9.waitForTimeout(300)
      await p9.screenshot({ path: `${OUT}/09-session-menu-generic-note.png` })
      console.log('captured 09-session-menu-generic-note.png')
      await ctx9.close()
    }

    // 10: the same empty-inputs chat's sidebar mark: the generic tooltip.
    {
      const { context: ctx10, page: p10 } = await scene(browser, base, [{ ...STALE, config_stale_inputs: '' }, CURRENT])
      const mark10 = p10.locator(MARK)
      if ((await mark10.count()) !== 1) throw new Error('the sidebar mark did not render')
      const mark10Title = await mark10.getAttribute('title')
      if (mark10Title !== GENERIC_TOOLTIP) throw new Error(`unexpected sidebar tooltip: ${mark10Title}`)
      await drawTooltip(p10, mark10, mark10Title)
      await p10.screenshot({ path: `${OUT}/10-sidebar-generic-tooltip.png` })
      console.log('captured 10-sidebar-generic-tooltip.png')
      await ctx10.close()
    }

    // 06: sub-agents are working, so Reload is blocked in both places.
    {
      const { context: ctx6, page: p6 } = await scene(browser, base, [STALE, CURRENT], ws => ws.send(JSON.stringify({
        type: 'subagent_spawn', data: { slot: 'chat-1', id: 'sa-1', task: 'Review the diff', agent: 'kirocrew' },
      })))
      if (!(await p6.locator(BUTTON).isDisabled())) throw new Error('the header button is enabled while sub-agents work')
      await openMenu(p6)
      const note6 = p6.locator('[data-testid="reload-stale-note"]').first()
      await note6.waitFor({ state: 'visible', timeout: 10_000 })
      const note6Text = (await note6.textContent()) ?? ''
      if (note6Text !== `${NOTE_INPUTS} — reload when sub-agents finish`) throw new Error(`unexpected note: ${note6Text}`)
      await p6.waitForTimeout(300)
      await p6.screenshot({ path: `${OUT}/06-blocked-while-subagents.png` })
      console.log('captured 06-blocked-while-subagents.png')
      await ctx6.close()
    }

    // 07: the sidebar row's alert mark and its tooltip.
    {
      const { context: ctx7, page: p7 } = await scene(browser, base)
      const mark = p7.locator(MARK)
      if ((await mark.count()) !== 1) throw new Error('the sidebar mark did not render')
      if ((await mark.locator('.lucide-circle-alert').count()) !== 1) throw new Error('the sidebar mark is not the alert glyph')
      const markTitle = await mark.getAttribute('title')
      if (markTitle !== TOOLTIP) throw new Error(`unexpected sidebar tooltip: ${markTitle}`)
      await drawTooltip(p7, mark, markTitle)
      await p7.screenshot({ path: `${OUT}/07-sidebar-mark-tooltip.png` })
      console.log('captured 07-sidebar-mark-tooltip.png')
      await ctx7.close()
    }

    // 08: a phone-width viewport and a long title: the cue rides inside the
    // title/menu trigger, and the bar adds no Reload button of its own.
    {
      const LONG = { ...STALE, title: 'Wire up the GitHub MCP server and move the release checklist into the shared agent config' }
      const { context: ctx8, page: p8 } = await scene(browser, base, [LONG, CURRENT], null, { width: 390, height: 844 })
      await assertPhoneCue(p8, 390, 'Config · Reload')
      await p8.screenshot({ path: `${OUT}/08-cue-in-title-narrow-viewport.png` })
      console.log('captured 08-cue-in-title-narrow-viewport.png')
      await ctx8.close()
    }

    // 11/12: the session menu at 320px, where the Reload row's note must stack
    // and wrap inside the viewport. Fails when the menu or the note's remedy
    // reaches past the screen edge.
    for (const [frame, lang] of [['11-session-menu-320px-en', null], ['12-session-menu-320px-ja', 'ja']]) {
      const { context: ctxN, page: pN } = await scene(browser, base, [STALE, CURRENT], null, { width: 320, height: 720 }, lang)
      await assertPhoneCue(pN, 320, lang ? null : 'Config · Reload')
      await pN.locator('[data-testid="session-title-menu"]').click()
      const noteN = pN.locator('[data-testid="reload-stale-note"]').first()
      await noteN.waitFor({ state: 'visible', timeout: 10_000 })
      await pN.waitForTimeout(300)
      for (const sel of ['[role="menu"]', '[data-testid="reload-stale-clause"]']) {
        const box = await pN.locator(sel).first().boundingBox()
        if (!box || box.x < 0 || box.x + box.width > 320) throw new Error(`${frame}: ${sel} overflows the 320px viewport (${JSON.stringify(box)})`)
      }
      await noteN.scrollIntoViewIfNeeded()
      await pN.screenshot({ path: `${OUT}/${frame}.png` })
      console.log(`captured ${frame}.png`)
      await ctxN.close()
    }

    // 13: the gateway refuses the reload; the failure lands in the chat's
    // action-error notice, the header row gains no control, and no alert
    // dialog opens.
    {
      const refuse = async (path, route) => {
        if (path !== '/api/chat/slots/chat-1/reload') return false
        await json(route, { error: 'sub-agents are running', code: 'slot_subagents_running' }, 409)
        return true
      }
      const { context: ctx13, page: p13 } = await scene(browser, base, [STALE, CURRENT], null, undefined, null, refuse)
      let dialogs = 0
      p13.on('dialog', d => { dialogs += 1; void d.dismiss() })
      const button13 = p13.locator(BUTTON)
      await button13.waitFor({ state: 'visible', timeout: 10_000 })
      await button13.click()
      const notice = p13.locator('[data-testid="action-error"]')
      await notice.waitFor({ state: 'visible', timeout: 10_000 })
      const noticeText = (await notice.innerText()).trim()
      if (!noticeText.includes('Sub-agents are still working on this session.')) throw new Error(`unexpected notice: ${noticeText}`)
      if ((await notice.getByRole('button', { name: /Ask the agent/ }).count()) !== 1) throw new Error('the notice offers no agent hand-off')
      if ((await p13.locator('[data-testid="stale-config-reload-error"]').count()) !== 0) throw new Error('the header row renders a notice of its own beside the button')
      if (await button13.isDisabled()) throw new Error('the button stays disabled after a failed reload')
      await p13.waitForTimeout(300)
      if (dialogs !== 0) throw new Error(`a failed reload opened ${dialogs} alert dialog(s)`)
      await p13.screenshot({ path: `${OUT}/13-header-reload-failed.png` })
      console.log('captured 13-header-reload-failed.png')
      await ctx13.close()
    }
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
