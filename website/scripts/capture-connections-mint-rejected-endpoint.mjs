/**
 * Screenshot harness for the Connections card's rejected-URL failure copy.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server, with every /api/** call answered from fixtures via Playwright route
 * interception -- gateway-free, no kiro-cli, no MCP child, no consent flow.
 *
 * The scene is the Notion card walked through one real connect whose mint ends
 * in `failed` / `mint_url_rejected`. Two frames, each from a fresh page:
 *
 *   1. `rejected_endpoint` present -- the error line names the endpoint the
 *      gate refused, a second line carries the oauth_endpoints.json remedy,
 *      and a Documentation link points at the allowlist guide section;
 *   2. `rejected_endpoint` absent  -- the card keeps its unnamed message, with
 *      no remedy line and no link.
 *
 * Before each shot the harness asserts the copy it expects is on screen, that
 * the link (when expected) resolves to the guide anchor, that no first-run
 * dialog covers the card, and that nothing from the rejected URL beyond
 * host+path (scheme, query, token) ever reached the page text.
 *
 * Usage: node scripts/capture-connections-mint-rejected-endpoint.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/connections-mint-rejected-endpoint'
const SLUG = 'notion'
const ENDPOINT = 'auth.example-idp.com/realms/dev/authorize'
const GUIDE_URL = 'https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/connecting-remote-oauth-mcp-server.md#if-the-host-is-not-recognized-the-oauth-endpoint-allowlist'
/** Strings from the rejected URL that must NEVER appear on the card. */
const NEVER_ON_SCREEN = ['https://', 'access_token', 'AKIAIOSFODNN7EXAMPLE', 'code_challenge', '?']

mkdirSync(OUT, { recursive: true })

const entry = () => ({
  name: SLUG,
  command: '',
  url: 'https://mcp.notion.com/mcp',
  status: 'error',
  error: 'authorization required',
  tools: [],
  source: 'kirocrew',
  enabled: true,
  kirocrewManaged: true,
})

async function frame(browser, base, { name, endpoint, expectText, expectAlso = [], forbidText, expectLink }) {
  const context = await browser.newContext({ viewport: { width: 1400, height: 880 }, deviceScaleFactor: 1 })
  const page = await context.newPage()
  logPageProblems(page)

  const scene = { installed: false, mint: { slug: SLUG, state: 'idle' } }
  await stubDashboardApi(page, {
    slots: [],
    extra: async (path, route) => {
      if (path === '/api/config/kirocrew') return json(route, { connections_ui: true }), true
      if (path === '/api/mcp' || path === '/api/mcp/probe') {
        return json(route, scene.installed ? [entry()] : []), true
      }
      if (path === '/api/mcp/custom') {
        scene.installed = true
        return json(route, { ok: true, added: [SLUG], enabled: true }), true
      }
      if (path === '/api/connections/mint') {
        if (route.request().method() === 'POST') {
          scene.mint = { slug: SLUG, state: 'minting' }
          return json(route, { ok: true, slug: SLUG, state: 'minting' }), true
        }
        return json(route, scene.mint), true
      }
      if (path.startsWith('/api/chat/slots/')) {
        return json(route, { running: false, has_more: false, total: 0, queue: [], messages: [] }), true
      }
      return false
    },
  })

  const card = page.locator(`#connection-${SLUG}`)
  await page.goto(base + '/capabilities?tab=mcp', { waitUntil: 'domcontentloaded' })
  await page.locator(`#connection-${SLUG}[data-state="not-connected"]`).waitFor({ state: 'visible', timeout: 20000 })
  const connect = card.getByRole('button', { name: 'Connect', exact: true })
  await connect.waitFor({ state: 'visible', timeout: 20000 })
  await connect.click()
  await page.locator(`#connection-${SLUG}[data-state="waiting-for-approval"]`).waitFor({ state: 'visible', timeout: 20000 })

  // The terminal verdict: the gate refused the URL twice, so the row failed.
  scene.mint = {
    slug: SLUG,
    state: 'failed',
    reason: 'mint_url_rejected',
    ...(endpoint ? { rejected_endpoint: endpoint } : {}),
  }
  await card.getByText(expectText, { exact: false }).first().waitFor({ state: 'visible', timeout: 20000 })

  if (await page.getByRole('dialog').count()) throw new Error(`${name}: a dialog covers the card`)
  const text = await card.innerText()
  for (const never of [...NEVER_ON_SCREEN, ...forbidText]) {
    if (text.includes(never)) throw new Error(`${name}: card text carries ${JSON.stringify(never)}`)
  }
  for (const want of [expectText, ...expectAlso]) {
    if (!text.includes(want)) throw new Error(`${name}: card text lacks ${JSON.stringify(want)}`)
  }
  const docLinks = card.getByRole('link', { name: /Documentation/ })
  if (expectLink) {
    await docLinks.first().waitFor({ state: 'visible', timeout: 20000 })
    const href = await docLinks.first().getAttribute('href')
    if (href !== expectLink) throw new Error(`${name}: Documentation link points at ${href}`)
  } else if (await docLinks.count()) {
    throw new Error(`${name}: a Documentation link rendered without an endpoint to act on`)
  }
  await page.waitForTimeout(400)
  await card.screenshot({ path: `${OUT}/${name}.png` })
  console.log('wrote', `${OUT}/${name}.png`)
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    await frame(browser, base, {
      name: '1-card-rejected-endpoint-named',
      endpoint: ENDPOINT,
      expectText: `The approval address from ${ENDPOINT} looked like it carried a credential`,
      expectAlso: [`If ${ENDPOINT} is a trusted identity provider`, 'oauth_endpoints.json'],
      forbidText: ['containing credential-like data'],
      expectLink: GUIDE_URL,
    })
    await frame(browser, base, {
      name: '2-card-rejected-endpoint-unnamed-fallback',
      endpoint: undefined,
      expectText: 'containing credential-like data, so it was not displayed',
      forbidText: [ENDPOINT, 'oauth_endpoints.json', 'identity provider'],
    })
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
