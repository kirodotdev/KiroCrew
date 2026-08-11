/**
 * Screenshot harness for the card-owned mcp_oauth banner suppression.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server, with every /api/** call answered from fixtures via Playwright route
 * interception -- gateway-free, no kiro-cli, no MCP server, no consent flow.
 *
 * The scene is ONE transcript holding one `mcp_oauth` message shaped exactly as
 * `_emit_mcp_oauth_request` writes it for a provider a Connections card owns
 * (`meta.card_owned: true`). The backend annotation is NOT flag-gated, so the
 * SAME persisted message is photographed three times and only the flag moves:
 *
 *   1. flag OFF, chat            -> banner renders (the unchanged path)
 *   2. flag ON,  chat            -> transcript clean, banner gone
 *   3. flag ON,  Connections tab -> the notion card holds the approval action
 *
 * Shot 3 is reached by CLICKING the nav rail rather than a fresh `goto`, because
 * the card reads its approval URL out of the Redux chat state that the slot
 * fetch populated -- a reload would empty it and photograph a lie.
 *
 * Usage: node scripts/capture-connections-banner-suppression.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/connections-banner-suppression'
const SLOT = 'chat-1'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const iso = secondsAgo => new Date(Date.now() - secondsAgo * 1000).toISOString()

const slots = [{
  key: SLOT,
  title: 'Draft the launch note',
  running: false,
  last_message: 'Which Notion page should it land on?',
  messages: 3,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

/**
 * The transcript. The mcp_oauth row carries exactly the meta the drain emits:
 * `server_name` + `oauth_url` (always) and `card_owned` (because a rendered
 * Notion card owns that server). `cls` mirrors the slot append.
 */
const detail = {
  running: false,
  has_more: false,
  total: 3,
  queue: [],
  project: PROJECT,
  messages: [
    {
      role: 'user',
      cls: '',
      ts: iso(240),
      content: 'Draft the launch note and put it in Notion.',
    },
    {
      role: 'mcp_oauth',
      cls: 'msg msg-info',
      ts: iso(200),
      content: '\u{1F510} notion requires authentication.',
      meta: {
        server_name: 'notion',
        oauth_url: 'https://mcp.notion.com/authorize?response_type=code&client_id=8f2c1d40-7b9a-4e51-9c33-0a6df1e2b874&code_challenge=Rk9vQmFyQmF6UXV1eFF1dXhRdXV4UXV1eFF1dXhRdQ&code_challenge_method=S256&redirect_uri=http%3A%2F%2F127.0.0.1%3A36389%2Foauth%2Fcallback',
        card_owned: true,
        mid: 'm-4c1f7a90d3e28b56',
      },
    },
    {
      role: 'assistant',
      cls: '',
      ts: iso(190),
      content: 'Which Notion page should it land on?',
    },
  ],
}

/** The MCP entry Connect created for the provider, still awaiting its token. */
const mcpServers = [{
  name: 'notion',
  command: '',
  url: 'https://mcp.notion.com/mcp',
  status: 'error',
  error: 'authorization required',
  tools: [],
  source: 'kirocrew',
  enabled: true,
  kirocrewManaged: true,
}]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1440, height: 620 },
    deviceScaleFactor: 2, // 13px banner type renders soft at 1x on GitHub
  })
  const page = await context.newPage()
  logPageProblems(page)

  // Mutable so the flag can move between shots without re-registering routes.
  const scene = { connectionsUi: false }

  await stubDashboardApi(page, {
    slots,
    // `return json(...), true` -- the comma marks the request handled; awaiting
    // fulfil() would resolve to undefined and fall through to the boot stub.
    extra: async (path, route) => {
      if (path === '/api/config/kirocrew') {
        // Flag OFF is served as `{}` -- absent, exactly as an instance that never
        // opted in reports it, not `false`.
        return json(route, scene.connectionsUi ? { connections_ui: true } : {}), true
      }
      if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
      if (path === '/api/mcp') return json(route, mcpServers), true
      return false
    },
  })

  // Pre-select the slot so the SPA boots straight into the transcript. Added
  // AFTER stubDashboardApi's init script, whose localStorage.clear() would
  // otherwise wipe it.
  await page.addInitScript(s => localStorage.setItem('mc-active-slot-chat', s), SLOT)

  const shot = async name => {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  const banner = page.locator('#main-content').getByRole('link', { name: /Authorize notion/i })
  const loadChat = async () => {
    // Short viewport for the chat shots: the transcript is top-anchored and the
    // composer is pinned to the bottom, so a tall frame is mostly dead space and
    // the delta a reviewer is looking for renders small.
    await page.setViewportSize({ width: 1440, height: 620 })
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    // Transcript is rendered once the assistant's reply is on screen -- wait on
    // that, never on the banner, so the absent case is a real absence and not a
    // shot taken before the transcript painted.
    await page.locator('#main-content').getByText('Which Notion page should it land on?')
      .first().waitFor({ state: 'visible', timeout: 20000 })
    await page.waitForTimeout(600)
  }

  // 1. Flag OFF -- the unchanged path. Chat is the only surface that can
  //    authorize, so the banner must be there.
  scene.connectionsUi = false
  await loadChat()
  if (await banner.count() !== 1) throw new Error('flag OFF: expected exactly one Authorize banner')
  console.log('flag OFF: banner present')
  await shot('1-chat-flag-off-banner-renders')

  // 2. Flag ON -- same transcript, same message, banner suppressed.
  scene.connectionsUi = true
  await loadChat()
  if (await banner.count() !== 0) throw new Error('flag ON: banner should be suppressed in chat')
  console.log('flag ON: banner suppressed')
  await shot('2-chat-flag-on-banner-suppressed')

  // 3. Flag ON -- the surviving surface. Navigated by CLICKING the rail, not by
  //    a fresh goto: the card reads its approval URL out of the Redux chat state
  //    the slot fetch populated, and a reload would empty it. The rail item is a
  //    div[role=button], so getByRole finds it while a `button` selector does not.
  //    Taller frame here: the card grid needs the room the transcript did not.
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.getByRole('button', { name: 'Agent Capabilities' }).first().click()
  const connectionsTab = page.locator('#main-content').getByRole('button', { name: 'Connections', exact: true })
  // Wait for the capabilities panel to mount before clicking its tab -- clicking
  // into a still-rendering panel lands on the default Crews tab and the shot
  // times out on a card that was never asked for.
  await connectionsTab.first().waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(800)
  await connectionsTab.first().click()
  // The card in `waiting-for-approval` states it in prose and offers the approval
  // URL as a link (not a button) -- assert both, so the shot proves the state and
  // the surviving action rather than just that a card exists.
  const cardWaiting = page.locator('#main-content').getByText(/Finish approving in your browser/i)
  const reopen = page.locator('#main-content').getByRole('link', { name: /Re-open approval/i })
  await cardWaiting.first().waitFor({ state: 'visible', timeout: 20000 })
  await reopen.first().waitFor({ state: 'visible', timeout: 20000 })
  console.log('flag ON: Connections card holds the approval action')
  await page.waitForTimeout(600)
  await shot('3-connections-card-flag-on-approval-survives')

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
