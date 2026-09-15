/**
 * Evidence capture for the Group C settings-redirect copy fixes — the surfaces
 * whose prose pointed at a Settings destination that does not exist (Settings →
 * MCP / Deploy / the Skills settings page) and now names the real one
 * (Agent Capabilities → Connections / Knowledge / Skills).
 *
 * Runs a BUILT dist (any dist — the same script is run against the BEFORE and
 * the AFTER build so the pairs line up by file name) on the shared static
 * server with every /api/** call answered from fixtures. No gateway, no token.
 *
 * Each shot is an element screenshot cropped to the containing card / dialog /
 * panel, taken only after a distinctive sentence fragment — one that is
 * OUTSIDE the changed part, so it exists in both builds — is visible. A surface
 * that cannot be reached prints `SKIPPED <id>: <reason>` and never fakes it.
 *
 *   c4-aws-control-empty.png        AWS Control zero-accounts empty state
 *   c5-aws-control-unsupported.png  AWS Control "can't list profiles" notice
 *   c1-mochi-mcp-empty.png          Mochi Settings → MCP with no servers
 *   c2-mochi-mcp-disabled.png       Mochi MCP row whose discover answered 409 disabled
 *   c3-mochi-mcp-probe-failed.png   Mochi MCP row whose probe reached the server and failed
 *   c6-md-notebook-connect-vault.png md-notebook ConnectVault (knowledge opt-in help)
 *   c7-project-skills-trust.png     ProjectSkillsTrustDialog (chat composer)
 *
 * Usage: node scripts/capture-settings-redirects-c.mjs <distDir> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'

const DIST = process.argv[2]
const OUT = process.argv[3]
if (!DIST || !OUT) {
  console.error('usage: node scripts/capture-settings-redirects-c.mjs <distDir> <outDir>')
  process.exit(2)
}
mkdirSync(OUT, { recursive: true })

const PAD = 16
const json = (route, body, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

// ---- fixtures --------------------------------------------------------------
// AWS Control: zero accounts (the onboarding empty state) and an UNSUPPORTED
// profile listing, so both Group C sentences render on one page load.
const CONSENT = (service) => ({
  service, serviceLabel: service, granted: false, region: 'us-west-2', credentialSource: '',
  account: '', identityResolved: false, revokedOnAccountChange: false, grant: null,
})

// Mochi: the panel's flat settings store, one enabled extra server per error
// case, and a core inventory that lists both.
let MOCHI_MCP_INVENTORY = []
let MOCHI_EXTRA = []
const mochiSettings = () => ({
  mode: 'active', petName: 'Mochi', activeAppearance: 'p1', shortcuts: {}, chatAlwaysOnTop: true,
  extraMcpServers: MOCHI_EXTRA, notifications: true, backgroundEnabled: false, model: '',
})
const MOCHI_STATS = { messages: 0, screenshots: 0, walks: 0, reminders: 0 }
// discover-tools answers, keyed by server name.
const MCP_TOOLS_ANSWER = {
  // 409 + machine-readable code → the panel's "This server is disabled…" line.
  'github-mcp': { status: 409, body: { code: 'server_disabled', error: 'disabled' } },
  // 200 with status != ok → probe_failed → "This server didn't respond…".
  'jira-mcp': { status: 200, body: { status: 'error', tools: [] } },
}

// md-notebook: no vaults → the page IS the ConnectVault form.
const MDNB_VAULTS = { vaults: [], hasPat: false, hasGhAuth: false }

const unmatched = new Set()
async function answer(route) {
  const req = route.request()
  const url = new URL(req.url())
  const path = url.pathname

  if (path === '/api/aws/consent') return json(route, CONSENT(url.searchParams.get('service') || 's3'))

  // ---- Mochi settings window
  if (path === '/api/apps/mochi/settings') return json(route, mochiSettings())
  if (path === '/api/apps/mochi/stats' || path === '/api/apps/mochi/stat') return json(route, MOCHI_STATS)
  if (path === '/api/apps/mochi/manifest') return json(route, { name: 'mochi', display_name: 'Mochi', enabled: true })
  if (path === '/api/apps/mochi/pet-state') return json(route, { mood: 'idle', x: 0, y: 0 })
  if (path.startsWith('/api/apps/mochi/mcp-tools/')) {
    const name = decodeURIComponent(path.split('/').pop())
    const a = MCP_TOOLS_ANSWER[name]
    if (a) return json(route, a.body, a.status)
    return json(route, { status: 'ok', tools: [] })
  }
  if (path.startsWith('/api/apps/mochi/')) return json(route, {})
  if (path === '/api/mcp') return json(route, MOCHI_MCP_INVENTORY)
  if (path === '/api/chat/slots' || path.startsWith('/api/chat/slots')) return json(route, [])
  if (path === '/api/chat/mode') return json(route, { mode: 'normal' })

  // ---- md-notebook
  // md-notebook's API_BASE is /apps/md-notebook/api (app-local), not /api/apps/…
  if (path === '/apps/md-notebook/api/vaults') return json(route, MDNB_VAULTS)
  if (path === '/apps/md-notebook/api/health') return json(route, { ok: true })
  if (path === '/apps/md-notebook/api/settings') return json(route, { autoSync: false, autoSyncMins: 5 })
  if (path.startsWith('/apps/md-notebook/api/')) return json(route, {})
  if (path.startsWith('/api/knowledge/sources')) return json(route, { sources: [] })

  // ---- dashboard shell (same set capture-aws-control.mjs uses — several are
  // consumed as ARRAYS and a blanket {} crashes the shell's error boundary).
  if (path === '/api/apps') return json(route, [])
  if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
  if (path === '/api/status') return json(route, { sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0, uptime: 1, version: '0.1.0' })
  if (path === '/api/kiro-prerequisite') return json(route, { installed: true, authenticated: true, ready: true, initial_setup_complete: true, setup_allowed: true })
  if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro Crew', avatar: '' })
  if (path === '/api/theme/boot') return json(route, { mode: 'dark', theme: '' })
  if (path === '/api/themes') return json(route, { themes: [], installed: [] })
  if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
  if (path === '/api/models') return json(route, { models: [], default: 'auto' })
  if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
  const objectish = /(config|tips|voice|autonudge|branding|status|themes|system|settings|prefs|profile)/.test(path)
  unmatched.add(path)
  return json(route, objectish ? {} : [])
}

// ---- helpers ---------------------------------------------------------------
const { srv: server, base } = await serveDist(resolve(DIST))
const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 1 })
await ctx.route('**/api/**', answer)
await ctx.route('**/api/ws', (route) => route.abort())
await ctx.addInitScript(() => {
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-import-onboarded', '1')
  localStorage.setItem('mc-privacy-acked', '1')
  localStorage.setItem('mc-theme-mode', 'dark')
})

const results = []
const skip = (id, reason) => { console.log(`SKIPPED ${id}: ${reason}`); results.push({ id, skipped: reason }) }

/** Screenshot `container` (a Locator) cropped with padding, after asserting
 *  `fragment` is visible inside the page. */
async function shoot(page, id, file, fragment, container) {
  const text = page.getByText(fragment, { exact: false }).first()
  await text.waitFor({ state: 'visible', timeout: 15_000 })
  await container.scrollIntoViewIfNeeded()
  await page.waitForTimeout(250)
  const box = await container.boundingBox()
  if (!box) throw new Error(`${id}: container has no bounding box`)
  const vp = page.viewportSize()
  const clip = {
    x: Math.max(0, box.x - PAD),
    y: Math.max(0, box.y - PAD),
    width: Math.min(vp.width, box.x + box.width + PAD) - Math.max(0, box.x - PAD),
    height: Math.min(vp.height, box.y + box.height + PAD) - Math.max(0, box.y - PAD),
  }
  const out = `${OUT}/${file}`
  await page.screenshot({ path: out, clip })
  console.log(`captured ${out} (asserted: ${fragment})`)
  results.push({ id, file: out })
}

async function newPage() {
  const page = await ctx.newPage()
  page.on('pageerror', (err) => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 300)))
  return page
}

// ---- C1 / C2 / C3: Mochi Settings → MCP ------------------------------------
// The panel is Mochi's own settings WINDOW, a separate Vite entry shipped in the
// dist at /src/apps/mochi/settings.html (not a dashboard route).
const MOCHI_SETTINGS_URL = `${base}/src/apps/mochi/settings.html`
async function openMochiMcp(page) {
  await page.goto(MOCHI_SETTINGS_URL, { waitUntil: 'domcontentloaded' })
  // The MCP nav entry is the button whose label is the section title.
  const nav = page.getByRole('button', { name: /^MCP Servers$/ }).first()
  await nav.waitFor({ state: 'visible', timeout: 15_000 })
  await nav.click()
  await page.waitForTimeout(400)
}
// The section card: the <Section> wrapper around the "MCP Servers" heading.
const mcpSection = (page) =>
  page.locator('h3, h2, div').filter({ hasText: /^MCP Servers$/ }).first().locator('xpath=..')

let c1ok = false
{
  const page = await newPage()
  try {
    MOCHI_MCP_INVENTORY = []
    MOCHI_EXTRA = []
    await openMochiMcp(page)
    await shoot(page, 'C1', 'c1-mochi-mcp-empty.png',
      'No MCP servers are configured yet',
      mcpSection(page))
    c1ok = true
  } catch (e) { skip('C1', String(e.message || e).split('\n')[0]) }
  await page.close()
}

if (!c1ok) {
  skip('C2', 'C1 (the Mochi MCP panel) was unreachable')
  skip('C3', 'C1 (the Mochi MCP panel) was unreachable')
} else {
  const page = await newPage()
  MOCHI_MCP_INVENTORY = [
    { name: 'github-mcp', tools: [] },
    { name: 'jira-mcp', tools: [] },
  ]
  MOCHI_EXTRA = [
    { name: 'github-mcp', agents: ['chat'], autoApprove: [], disabledTools: [] },
    { name: 'jira-mcp', agents: ['chat'], autoApprove: [], disabledTools: [] },
  ]
  try {
    await openMochiMcp(page)
    // Expanding a row fires discover-tools for it; the 409 lands in the row's
    // live region.
    await page.getByRole('button', { name: /github-mcp/ }).first().click()
    await shoot(page, 'C2', 'c2-mochi-mcp-disabled.png',
      'This server is disabled',
      mcpSection(page))
  } catch (e) { skip('C2', String(e.message || e).split('\n')[0]) }
  try {
    await page.getByRole('button', { name: /jira-mcp/ }).first().click()
    await shoot(page, 'C3', 'c3-mochi-mcp-probe-failed.png',
      "This server didn't respond",
      mcpSection(page))
  } catch (e) { skip('C3', String(e.message || e).split('\n')[0]) }
  await page.close()
}

// ---- C6: md-notebook ConnectVault -----------------------------------------
{
  const page = await newPage()
  try {
    await page.goto(`${base}/md-notebook`, { waitUntil: 'domcontentloaded' })
    const help = page.getByText('so agents can search your notes', { exact: false }).first()
    await help.waitFor({ state: 'visible', timeout: 15_000 })
    // The dialog card: the ConnectVault form is the closest <form> around the
    // subfolder field; fall back to a few ancestors of the help text.
    const form = page.locator('#mdnb-subfolder').locator('xpath=ancestor::form[1]')
    const container = (await form.count()) ? form.first() : help.locator('xpath=ancestor::div[3]')
    await shoot(page, 'C6', 'c6-md-notebook-connect-vault.png',
      'so agents can search your notes', container)
  } catch (e) { skip('C6', String(e.message || e).split('\n')[0]) }
  await page.close()
}

// ---- C7: ProjectSkillsTrustDialog -------------------------------------------
// Opened only from ChatInput's skill picker when an UNTRUSTED project skill is
// selected (trustPrompt state); that needs a live chat slot with a project
// whose skills the picker lists, which these fixtures do not provide.
skip('C7', 'ProjectSkillsTrustDialog opens only via ChatInput skill-picker onSelect of an untrusted project skill (trustPrompt state); no fixture-reachable path without a live chat slot + project-skills listing')

if (unmatched.size) console.log('unmatched /api paths:', [...unmatched].join(', '))
await browser.close()
server.close()
console.log('done', JSON.stringify(results))
