/**
 * Screenshot harness for MCP Management's two sub-views.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers the boot fixtures from the shared stub, so only the two endpoints this
 * surface actually reads are declared here. No gateway, no dashboard auth, and no
 * MCP server is ever launched.
 *
 * The fixture puts backend sharing ON across the whole stub set, which is the
 * state that makes the assessment view worth having: rows already sharing a
 * backend on evidence that does not support it.
 *
 * Usage: node scripts/capture-mcp-sharing-assessment.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/mcp-assessment-shots'
mkdirSync(OUT, { recursive: true })

const rec = (strength, share, reasons) => ({
  strength,
  recommendShare: share,
  reasons,
})

/**
 * Fixture rows use PLACEHOLDER names on purpose.
 *
 * Three of the five evidence tiers are only reachable from probe data that lives
 * in the running gateway's memory, so a fixture cannot honestly put a real
 * server's name next to one of those verdicts: it would assert something about
 * that server that nothing here measured. The tiers still have to be visible to
 * be reviewed, so the rows are anonymous and the screenshot makes no claim about
 * any particular deployment. One deliberately long name exercises the wrapping.
 *
 * name, stubbed, transport, verdict
 */
const ROWS = [
  ['alpha-mcp', true, 'stdio', rec('disqualified', false, [{ code: 'first_party_session_scoped', detail: '' }])],
  ['bravo-mcp', true, 'stdio', rec('no_objection', false, [
    { code: 'no_objection_found', detail: '' },
    { code: 'no_tool_annotations', detail: '2025-06-18' },
  ])],
  ['a-rather-long-example-server-name-mcp', true, 'stdio', rec('no_objection', false, [
    { code: 'no_objection_found', detail: '' },
  ])],
  ['charlie-mcp', true, 'stdio', rec('no_objection', false, [
    { code: 'no_objection_found', detail: '' },
    { code: 'all_tools_read_only', detail: '' },
  ])],
  ['delta-mcp', true, 'stdio', rec('disqualified', false, [
    { code: 'per_client_capability', detail: 'logging_level' },
  ])],
  ['echo-mcp', true, 'stdio', rec('disqualified', false, [
    { code: 'rotating_secret_env', detail: 'AWS_SESSION_TOKEN' },
  ])],
  // Refutation: the strongest tier, reached only by watching a shared server
  // actually misbehave.
  ['foxtrot-mcp', true, 'stdio', rec('refuted', false, [
    { code: 'observed_hazard', detail: 'unroutable_notification' },
  ])],
  ['golf-mcp', true, 'stdio', rec('declared', true, [
    { code: 'declares_caller_identity', detail: '' },
    { code: 'preflight_passed', detail: '' },
  ])],
  ['hotel-mcp', false, 'stdio', rec('no_objection', false, [{ code: 'no_objection_found', detail: '' }])],
  ['india-mcp', false, 'stdio', rec('no_objection', false, [
    { code: 'no_objection_found', detail: '' },
    { code: 'no_tools_listed', detail: '' },
  ])],
  // Probe never succeeded: unknown, and deliberately NOT flagged as unsafe.
  ['juliett-mcp', false, 'stdio', rec('unknown', false, [{ code: 'not_probed', detail: '' }])],
  // No verdict at all, as an older gateway would answer.
  ['kilo-mcp', true, 'stdio', null],
  // Not stdio: the question does not apply rather than the answer being no.
  ['lima-mcp', false, 'http', rec('disqualified', false, [{ code: 'not_stdio', detail: '' }])],
]

const servers = ROWS.map(([name, stub, transport, recommendation]) => ({
  name,
  can_stub: transport === 'stdio',
  stub: transport === 'stdio' ? stub : false,
  in_allowlist: stub,
  entry_poolable: false,
  agents: ['kirocrew'],
  transport,
  denylisted: false,
  ...(recommendation ? { recommendation } : {}),
}))

const stubbed = servers.filter(s => s.stub).map(s => s.name)

/** Flipped between shots so the warning can be shown appearing and gone. */
let sharingEnabled = true

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
    if (path === '/api/mcp-gateway/servers') return json(route, { servers }), true
    if (path === '/api/mcp-gateway/status') {
      return json(route, {
        enabled: sharingEnabled,
        stub: stubbed,
        stub_count: stubbed.length,
        running: true,
        ping_ok: true,
        supported: true,
      }), true
    }
    return false
  },
})

const shot = async name => {
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log('wrote', `${OUT}/${name}.png`)
}

const openAssessment = async () => {
  await page.getByRole('tab', { name: /sharing assessment/i }).click()
  await page.waitForTimeout(1200)
}

await page.goto(`${base}/developer?tab=mcp-pool`, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2600)

// 1. The existing decisions view, now with a tab rail above it.
await shot('01-servers-view')

// 2. The new read-only evidence view.
await openAssessment()
await shot('02-sharing-assessment')

// 3. Same verdicts with sharing OFF: the warning must disappear, because nothing
//    is co-tenanted any more.
sharingEnabled = false
await page.reload({ waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2400)
await openAssessment()
await shot('03-sharing-off-no-warning')

await context.close()
await browser.close()
srv.close()
console.log('done')
