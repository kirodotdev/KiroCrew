/**
 * Screenshot harness for the MCP panel's OAuth sign-in states.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers the two endpoints this surface reads from the shared stub. No gateway,
 * no MCP server, and no OAuth flow is ever launched — the states below are the
 * probe results a gateway would report, which is exactly what the panel renders.
 *
 * The three rows are the three answers a remote probe can give about
 * authorization, and they must be told apart at a glance:
 *   - challenge seen, no runtime grant   -> "Sign-in required" + where to sign in
 *   - challenge seen, grant held         -> "Signed in" (the end state of that flow)
 *   - static credential rejected         -> the error, plus why a token cannot fix it
 * A fourth row carries no authorization evidence at all, which is what an older
 * gateway reports; it must keep the vaguer "Not verified" rather than being told to
 * sign in -- and it must stay distinguishable from the grant-held row, which is the
 * whole reason that row has its own wording.
 *
 * Usage: node scripts/capture-mcp-oauth-affordance.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/mcp-oauth-shots'
mkdirSync(OUT, { recursive: true })

const PRESENCE = { kirocrew: true, kiroGlobal: false }

const servers = [
  // The reported case: an OAuth-protected server nobody has signed in to yet.
  {
    name: 'higgsfield',
    command: '',
    url: 'https://mcp.higgsfield.ai/mcp',
    status: 'needs_auth',
    source: 'mcp.json',
    enabled: true,
    tools: [],
    presence: PRESENCE,
    kirocrewManaged: true,
    probedAt: 1786975675,
    authChallenge: true,
    authGrantPresent: false,
  },
  // Authorized through Kiro CLI. The probe still cannot verify the grant is live,
  // but it CAN see that one exists -- so this row reads "Signed in" and offers no
  // action, and must stay visually distinct from the no-evidence row below.
  {
    name: 'linear',
    command: '',
    url: 'https://mcp.linear.app/mcp',
    status: 'needs_auth',
    source: 'mcp.json',
    enabled: true,
    tools: [],
    presence: PRESENCE,
    kirocrewManaged: true,
    probedAt: 1786975675,
    authChallenge: true,
    authGrantPresent: true,
  },
  // A pasted static credential against an OAuth-only server.
  {
    name: 'notion',
    command: '',
    url: 'https://mcp.notion.com/mcp',
    status: 'error',
    error: 'HTTP 401',
    source: 'mcp.json',
    enabled: true,
    tools: [],
    headers: { Authorization: '[REDACTED: credential]' },
    presence: PRESENCE,
    kirocrewManaged: true,
    probedAt: 1786975675,
    authChallenge: true,
  },
  // No authorization evidence at all — an older gateway. Must NOT say sign-in.
  {
    name: 'legacy-remote',
    command: '',
    url: 'https://mcp.example.com/mcp',
    status: 'needs_auth',
    source: 'mcp.json',
    enabled: true,
    tools: [],
    presence: PRESENCE,
    kirocrewManaged: true,
    probedAt: 1786975675,
  },
  // A healthy stdio server, for contrast.
  {
    name: 'kirocrew-core',
    command: '/usr/local/bin/kirocrew-mcp-core',
    status: 'ok',
    source: 'agent',
    enabled: true,
    tools: ['spawn_run', 'learn_add', 'artifact_save'],
    presence: PRESENCE,
    probedAt: 1786975675,
  },
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1600, height: 1000 },
  deviceScaleFactor: 2,
})
const page = await context.newPage()
logPageProblems(page)

await stubDashboardApi(page, {
  extra: (path, route) => {
    if (path === '/api/mcp') return json(route, servers), true
    if (path === '/api/mcp/scopes') return json(route, { scopes: [] }), true
    return false
  },
})

const shot = async name => {
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log('wrote', `${OUT}/${name}.png`)
}

await page.goto(`${base}/capabilities?tab=mcp`, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2200)

// The Connections page opens on its Services gallery when that is enabled, so the
// MCP Servers sub-tab has to be selected explicitly rather than by query param.
const mcpTab = page.getByRole('tab', { name: /MCP Servers/i })
if (await mcpTab.count()) {
  await mcpTab.first().click()
} else {
  await page.getByText(/MCP Servers/i).first().click()
}
await page.waitForTimeout(2200)

// 1. All four authorization states in one table.
await shot('01-mcp-panel-auth-states')

// 2. The sign-in row on its own, where the guidance text is legible. Filtering is
//    also the honest way to show one row: the panel has no per-row detail view.
await page.getByPlaceholder(/Filter servers or tools/i).fill('higgsfield')
await page.waitForTimeout(900)
await shot('02-sign-in-required-guidance')

// 3. The rejected static credential, which is the other half of the story: the
//    row says why a pasted token cannot satisfy an OAuth server.
await page.getByPlaceholder(/Filter servers or tools/i).fill('notion')
await page.waitForTimeout(900)
await shot('03-static-token-rejected')

await context.close()
await browser.close()
srv.close()
console.log('done')
