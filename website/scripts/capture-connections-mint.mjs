/**
 * Screenshot harness for the Connections approval-URL mint.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server, with every /api/** call answered from fixtures via Playwright route
 * interception -- gateway-free, no kiro-cli, no MCP child, no consent flow.
 *
 * The scene is the Notion card walked through one real connect: the harness
 * CLICKS Connect, the SPA writes the entry, posts /api/connections/mint, and
 * then polls GET /api/connections/mint on its own React Query interval. The
 * fixture moves that feed the way a live mint does -- `minting`, then `waiting`
 * with a URL, then `granted` once the entry probes healthy -- and the card is
 * photographed at each stop.
 *
 * The transcript is EMPTY on purpose (`slots: []`, and the harness asserts no
 * `/api/chat/slots/<key>` fetch is ever made). The card's other approval-URL
 * source is the newest `mcp_oauth` chat message, so an empty chat state is what
 * makes shot 2 load-bearing: the rendered link can only have come from the mint
 * feed, and the href is asserted equal to the fixture's URL before capture.
 *
 * Usage: node scripts/capture-connections-mint.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/connections-mint'
const SLUG = 'notion'

mkdirSync(OUT, { recursive: true })

/**
 * The minted approval URL, shaped as the drain hands it over: the provider's
 * authorize endpoint carrying the mint process's PKCE challenge and the loopback
 * redirect its own MCP child is listening on. Unique enough that asserting the
 * anchor's href against it proves the feed reached the card's approval anchor.
 */
const MINTED_URL = 'https://mcp.notion.com/authorize?response_type=code&client_id=8f2c1d40-7b9a-4e51-9c33-0a6df1e2b874&code_challenge=Rk9vQmFyQmF6UXV1eFF1dXhRdXV4UXV1eFF1dXhRdQ&code_challenge_method=S256&redirect_uri=http%3A%2F%2F127.0.0.1%3A41207%2Foauth%2Fcallback'

/** The remote entry Connect writes, before and after the grant lands. */
const entry = scene => ({
  name: SLUG,
  command: '',
  url: 'https://mcp.notion.com/mcp',
  status: scene.granted ? 'ok' : 'error',
  error: scene.granted ? '' : 'authorization required',
  tools: scene.granted ? ['search', 'fetch', 'create-pages'] : [],
  source: 'kirocrew',
  enabled: true,
  kirocrewManaged: true,
  ...(scene.granted ? { accountLabel: 'stan@example.com', connectedSince: '2026-08-12T22:41:00Z' } : {}),
})

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 880 },
    deviceScaleFactor: 1,
  })
  const page = await context.newPage()
  logPageProblems(page)

  // Mutable so the mint feed can advance between shots without re-registering
  // routes. `installed` flips on the SPA's own POST /api/mcp/custom, so /api/mcp
  // reports an empty list until the click actually wrote the entry.
  const scene = { installed: false, granted: false, mint: { slug: SLUG, state: 'idle' } }
  const seen = { mintPosts: 0, mintGets: 0, transcriptFetches: 0 }

  await stubDashboardApi(page, {
    slots: [],
    // `return json(...), true` -- the comma marks the request handled; awaiting
    // fulfil() would resolve to undefined and fall through to the boot stub.
    extra: async (path, route) => {
      if (path === '/api/config/kirocrew') return json(route, { connections_ui: true }), true
      if (path === '/api/mcp' || path === '/api/mcp/probe') {
        return json(route, scene.installed ? [entry(scene)] : []), true
      }
      if (path === '/api/mcp/custom') {
        scene.installed = true
        return json(route, { ok: true, added: [SLUG], enabled: true }), true
      }
      if (path === '/api/connections/mint') {
        if (route.request().method() === 'POST') {
          seen.mintPosts += 1
          scene.mint = { slug: SLUG, state: 'minting' }
          return json(route, { ok: true, slug: SLUG, state: 'minting' }), true
        }
        seen.mintGets += 1
        return json(route, scene.mint), true
      }
      // Counted, never served: a transcript fetch would introduce the chat
      // banner as a second possible URL source and void shot 2's claim.
      if (path.startsWith('/api/chat/slots/')) {
        seen.transcriptFetches += 1
        return json(route, { running: false, has_more: false, total: 0, queue: [], messages: [] }), true
      }
      return false
    },
  })

  const card = page.locator(`#connection-${SLUG}`)
  const shot = async name => {
    await page.waitForTimeout(500)
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }
  const stateOf = () => card.getAttribute('data-state')
  const expectState = async want => {
    await page.locator(`#connection-${SLUG}[data-state="${want}"]`).waitFor({ state: 'visible', timeout: 20000 })
    console.log(`card state: ${await stateOf()}`)
  }

  // 1. Baseline. Deep-linked, so the panel mounts straight into Connections
  //    with the flag on and no entry written yet.
  await page.goto(base + '/capabilities?tab=mcp', { waitUntil: 'domcontentloaded' })
  await expectState('not-connected')
  const connect = card.getByRole('button', { name: 'Connect', exact: true })
  await connect.waitFor({ state: 'visible', timeout: 20000 })
  if (await card.getByRole('link', { name: /Re-open approval/i }).count() !== 0) {
    throw new Error('baseline: an approval link is already rendered')
  }
  await shot('1-card-not-connected')

  // 2. The PR's whole point. A real click writes the entry and asks for the URL;
  //    the fixture answers `minting` first, so the link that appears is the one
  //    the poll picked up, not one painted with the first response.
  await connect.click()
  await expectState('waiting-for-approval')
  await card.getByText(/Waiting for the approval address/i).first()
    .waitFor({ state: 'visible', timeout: 20000 })
  if (seen.mintPosts !== 1) throw new Error(`expected exactly one mint POST, saw ${seen.mintPosts}`)
  scene.mint = { slug: SLUG, state: 'waiting', oauth_url: MINTED_URL }
  const approval = card.getByRole('link', { name: /Re-open approval/i })
  await approval.waitFor({ state: 'visible', timeout: 20000 })
  const href = await approval.getAttribute('href')
  if (href !== MINTED_URL) throw new Error(`approval href is not the minted URL: ${href}`)
  await card.getByText(/Waiting for approval/i).first()
    .waitFor({ state: 'visible', timeout: 20000 })
  // The mint opens no browser tab, so the card must NOT be telling the user to
  // finish in one -- this assertion is what keeps that copy off the minted path.
  if (await card.getByText(/Finish approving in your browser/i).count()) {
    throw new Error('minted path still renders the browser-tab copy')
  }
  if (seen.mintGets < 2) throw new Error(`expected the card to poll the feed, saw ${seen.mintGets} GETs`)
  if (seen.transcriptFetches !== 0) {
    throw new Error(`a transcript was fetched (${seen.transcriptFetches}) -- the URL source is ambiguous`)
  }
  console.log(`approval link renders the minted URL (${seen.mintGets} feed reads, no transcript)`)
  await shot('2-card-waiting-minted-approval-link')

  // 3. Terminal state. The grant lands on disk, the entry probes healthy and the
  //    feed reports `granted` -- the card leaves the wait on its own poll.
  scene.granted = true
  scene.mint = { slug: SLUG, state: 'granted' }
  await expectState('connected')
  await card.getByText('stan@example.com').first().waitFor({ state: 'visible', timeout: 20000 })
  await shot('3-card-connected-after-grant')

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
