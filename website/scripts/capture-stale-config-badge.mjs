/**
 * Screenshot harness for the stale-config badge.
 *
 * Frames, against the built SPA with the gateway stubbed:
 *   01 a chat whose agent process runs on outdated config: the "stale config"
 *      badge in the chat header and the icon on its sidebar row, beside a
 *      current chat that shows neither;
 *   02 the badge's tooltip. Headless Chromium paints no native `title`
 *      tooltip, so the capture draws the badge's own title text beside it;
 *      the words are the ones the browser shows on hover;
 *   03 the session menu opened from the badge: the Reload row's note, naming
 *      what changed, "stale config (<file>) · choose Reload session to apply";
 *   04 the same row while a turn runs, blocked and stale at once:
 *      "stale config (<file>) — reload when the turn ends";
 *   05 the generic tooltip of a stale chat whose changed files have no
 *      label (empty `config_stale_inputs`);
 *   06 the same row while sub-agents are working:
 *      "stale config (<file>) — reload when sub-agents finish";
 *   07 the sidebar row's icon tooltip, which names the remedy without
 *      promising a menu (a click on the row opens the chat);
 *   08 a phone-width viewport (390px) with a long session title: the header
 *      badge beside the title, which keeps its room;
 *   09 the session menu of the empty-inputs chat from 05: the Reload row's
 *      generic note, "stale config · choose Reload session to apply";
 *   10 that chat's sidebar icon tooltip, the generic compact wording.
 *
 * Exits non-zero, writing nothing further, when an element does not render.
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
const TOOLTIP = `Stale config: ${INPUTS} changed since this session started. Click to open the session menu, then choose Reload session.`
const COMPACT_TOOLTIP = `Stale config: ${INPUTS} changed since this session started. Reload the session to apply.`
const NOTE_INPUTS = `stale config (${INPUTS})`
const GENERIC_NOTE = 'stale config · choose Reload session to apply'
const COMPACT_GENERIC_TOOLTIP = "Stale config: this session's config changed since it started. Reload the session to apply."

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

async function scene(browser, base, slots = [STALE, CURRENT], onSocket = null, viewport = { width: 1280, height: 820 }) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, {
    slots,
    extra: async (path, route) => {
      if (path === '/api/chat/slots/chat-1') {
        await json(route, { messages: MESSAGES, has_more: false, total: MESSAGES.length })
        return true
      }
      return false
    },
  })
  let socket = null
  await page.routeWebSocket(/\/api\/ws/, ws => { socket = ws })
  logPageProblems(page)
  await page.goto(`${base}/chat?sid=chat-1`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[aria-label="Chat messages"]', { timeout: 20_000 })
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
    const badges = page.locator('[data-testid="stale-config-badge"]')
    await badges.first().waitFor({ state: 'visible', timeout: 10_000 })
    // One in the header, one on the stale chat's sidebar row; none on the current chat.
    if ((await badges.count()) !== 2) throw new Error(`expected 2 stale badges, saw ${await badges.count()}`)
    const header = page.locator('[data-testid="stale-config-badge"]:has-text("stale config")')
    if ((await header.count()) !== 1) throw new Error('the header badge did not render')
    const title = await header.getAttribute('title')
    if (title !== TOOLTIP) throw new Error(`unexpected tooltip: ${title}`)
    await page.screenshot({ path: `${OUT}/01-badge-header-and-sidebar.png` })
    console.log('captured 01-badge-header-and-sidebar.png')

    // 02: draw the title text the browser shows on hover.
    await header.hover()
    await page.evaluate(text => {
      const badge = [...document.querySelectorAll('[data-testid="stale-config-badge"]')].find(el => el.textContent?.includes('stale config'))
      if (!badge) throw new Error('badge gone')
      const r = badge.getBoundingClientRect()
      const tip = document.createElement('div')
      tip.textContent = text
      Object.assign(tip.style, {
        position: 'fixed', left: `${r.left}px`, top: `${r.bottom + 6}px`, maxWidth: '420px', zIndex: '99999',
        background: '#f5f5f5', color: '#111', font: '12px system-ui, sans-serif', padding: '4px 7px',
        border: '1px solid #999', borderRadius: '3px', boxShadow: '0 2px 6px rgba(0,0,0,.35)',
      })
      tip.setAttribute('data-capture-tooltip', '')
      document.body.appendChild(tip)
    }, title)
    await page.waitForTimeout(200)
    await page.screenshot({ path: `${OUT}/02-badge-tooltip.png` })
    console.log('captured 02-badge-tooltip.png')
    await page.evaluate(() => document.querySelector('[data-capture-tooltip]')?.remove())

    // 03: a click on the badge opens the session menu, whose Reload row carries the note.
    await header.click()
    const note = page.locator('[data-testid="reload-stale-note"]').first()
    await note.waitFor({ state: 'visible', timeout: 10_000 })
    const noteText = (await note.textContent()) ?? ''
    if (noteText !== `${NOTE_INPUTS} · choose Reload session to apply`) throw new Error(`unexpected note: ${noteText}`)
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/03-session-menu-note.png` })
    console.log('captured 03-session-menu-note.png')
    await context.close()

    // 04: a turn is running, so Reload is blocked; the note states both facts.
    {
      const { context: ctx4, page: p4 } = await scene(browser, base, [{ ...STALE, running: true }, CURRENT])
      await p4.locator('[data-testid="stale-config-badge"]:has-text("stale config")').click()
      const blocked = p4.locator('[data-testid="reload-stale-note"]').first()
      await blocked.waitFor({ state: 'visible', timeout: 10_000 })
      const blockedText = (await blocked.textContent()) ?? ''
      if (blockedText !== `${NOTE_INPUTS} — reload when the turn ends`) throw new Error(`unexpected note: ${blockedText}`)
      await p4.waitForTimeout(300)
      await p4.screenshot({ path: `${OUT}/04-session-menu-stale-while-blocked.png` })
      console.log('captured 04-session-menu-stale-while-blocked.png')
      await ctx4.close()
    }

    // 05: no changed file named, so the tooltip is the generic one.
    {
      const { context: ctx5, page: p5 } = await scene(browser, base, [{ ...STALE, config_stale_inputs: '' }, CURRENT])
      const generic = p5.locator('[data-testid="stale-config-badge"]:has-text("stale config")')
      await generic.waitFor({ state: 'visible', timeout: 10_000 })
      const genericTitle = await generic.getAttribute('title')
      if (!genericTitle?.startsWith("Stale config: this session's config changed")) throw new Error(`unexpected tooltip: ${genericTitle}`)
      await generic.hover()
      await p5.evaluate(text => {
        const badge = [...document.querySelectorAll('[data-testid="stale-config-badge"]')].find(el => el.textContent?.includes('stale config'))
        const r = badge.getBoundingClientRect()
        const tip = document.createElement('div')
        tip.textContent = text
        Object.assign(tip.style, {
          position: 'fixed', left: `${r.left}px`, top: `${r.bottom + 6}px`, maxWidth: '420px', zIndex: '99999',
          background: '#f5f5f5', color: '#111', font: '12px system-ui, sans-serif', padding: '4px 7px',
          border: '1px solid #999', borderRadius: '3px', boxShadow: '0 2px 6px rgba(0,0,0,.35)',
        })
        document.body.appendChild(tip)
      }, genericTitle)
      await p5.waitForTimeout(200)
      await p5.screenshot({ path: `${OUT}/05-badge-generic-tooltip.png` })
      console.log('captured 05-badge-generic-tooltip.png')
      await ctx5.close()
    }

    // 09: the same empty-inputs chat's session menu: the Reload row's generic note.
    {
      const { context: ctx9, page: p9 } = await scene(browser, base, [{ ...STALE, config_stale_inputs: '' }, CURRENT])
      await p9.locator('[data-testid="stale-config-badge"]:has-text("stale config")').click()
      const note9 = p9.locator('[data-testid="reload-stale-note"]').first()
      await note9.waitFor({ state: 'visible', timeout: 10_000 })
      const note9Text = (await note9.textContent()) ?? ''
      if (note9Text !== GENERIC_NOTE) throw new Error(`unexpected note: ${note9Text}`)
      await p9.waitForTimeout(300)
      await p9.screenshot({ path: `${OUT}/09-session-menu-generic-note.png` })
      console.log('captured 09-session-menu-generic-note.png')
      await ctx9.close()
    }

    // 10: the same empty-inputs chat's sidebar icon: the generic compact tooltip.
    {
      const { context: ctx10, page: p10 } = await scene(browser, base, [{ ...STALE, config_stale_inputs: '' }, CURRENT])
      const compact10 = p10.locator('[data-testid="stale-config-badge"]:not(:has-text("stale config"))')
      if ((await compact10.count()) !== 1) throw new Error('the sidebar badge did not render')
      const compact10Title = await compact10.getAttribute('title')
      if (compact10Title !== COMPACT_GENERIC_TOOLTIP) throw new Error(`unexpected sidebar tooltip: ${compact10Title}`)
      await drawTooltip(p10, compact10, compact10Title)
      await p10.screenshot({ path: `${OUT}/10-sidebar-generic-tooltip.png` })
      console.log('captured 10-sidebar-generic-tooltip.png')
      await ctx10.close()
    }

    // 06: sub-agents are working, so Reload is blocked; the note states both facts.
    {
      const { context: ctx6, page: p6 } = await scene(browser, base, [STALE, CURRENT], ws => ws.send(JSON.stringify({
        type: 'subagent_spawn', data: { slot: 'chat-1', id: 'sa-1', task: 'Review the diff', agent: 'kirocrew' },
      })))
      await p6.locator('[data-testid="stale-config-badge"]:has-text("stale config")').click()
      const note6 = p6.locator('[data-testid="reload-stale-note"]').first()
      await note6.waitFor({ state: 'visible', timeout: 10_000 })
      const note6Text = (await note6.textContent()) ?? ''
      if (note6Text !== `${NOTE_INPUTS} — reload when sub-agents finish`) throw new Error(`unexpected note: ${note6Text}`)
      await p6.waitForTimeout(300)
      await p6.screenshot({ path: `${OUT}/06-session-menu-stale-while-subagents.png` })
      console.log('captured 06-session-menu-stale-while-subagents.png')
      await ctx6.close()
    }

    // 07: the sidebar row's icon carries the remedy alone, no click promise.
    {
      const { context: ctx7, page: p7 } = await scene(browser, base)
      const compact = p7.locator('[data-testid="stale-config-badge"]:not(:has-text("stale config"))')
      if ((await compact.count()) !== 1) throw new Error('the sidebar badge did not render')
      const compactTitle = await compact.getAttribute('title')
      if (compactTitle !== COMPACT_TOOLTIP) throw new Error(`unexpected sidebar tooltip: ${compactTitle}`)
      await drawTooltip(p7, compact, compactTitle)
      await p7.screenshot({ path: `${OUT}/07-sidebar-badge-tooltip.png` })
      console.log('captured 07-sidebar-badge-tooltip.png')
      await ctx7.close()
    }

    // 08: a phone-width viewport and a long title: the badge must not squeeze it.
    {
      const LONG = { ...STALE, title: 'Wire up the GitHub MCP server and move the release checklist into the shared agent config' }
      const { context: ctx8, page: p8 } = await scene(browser, base, [LONG, CURRENT], null, { width: 390, height: 844 })
      const narrow = p8.locator('[data-testid="stale-config-badge"][aria-label^="Stale config"]').first()
      await narrow.waitFor({ state: 'visible', timeout: 10_000 })
      await p8.screenshot({ path: `${OUT}/08-badge-narrow-viewport.png` })
      console.log('captured 08-badge-narrow-viewport.png')
      await ctx8.close()
    }
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
