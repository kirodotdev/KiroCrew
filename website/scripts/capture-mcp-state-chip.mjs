/**
 * Screenshot harness for the MCP Management STATE chip (#5245).
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers only the two endpoints this surface reads from the shared stub. No
 * gateway, no dashboard auth, and no MCP server is ever launched.
 *
 * The fixture exists to photograph ONE row: a server the rewriter left unwrapped
 * (`pooling_blocked_by_env`) that is nonetheless in the stub allowlist while
 * backend sharing is ON. That combination is what used to render `shared` -- the
 * page claiming a pooled backend for an entry the broker never wrapped -- so it
 * is the row the fix is about. The other rows are the states it must NOT change:
 * a genuinely shared row, a stubbed row with sharing off, a direct row, and a
 * transport that cannot be stubbed at all.
 *
 * Names are placeholders. A verdict tier is only reachable from probe data in a
 * running gateway's memory, so a fixture must not put a real server's name next
 * to one; these rows assert nothing about any deployment.
 *
 * Usage: node scripts/capture-mcp-state-chip.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/mcp-state-chip-shots'

/** The blocked row's chip label, shared by the fixture assertions below. */
const CHIP = 'direct (env)'
mkdirSync(OUT, { recursive: true })

/** name, can_stub, in allowlist, transport, blocked-from-pooling-by-declared-env */
const ROWS = [
  // THE row. Allowlisted and sharing is on, so before the fix this read `shared`
  // while the rewriter had actually left it unwrapped.
  ['alpha-env-mcp', true, true, 'stdio', true],
  // Allowlisted with no obstacle: still `shared`, which the fix must not disturb.
  ['bravo-mcp', true, true, 'stdio', false],
  // Allowlisted but not sharing-eligible for an unrelated reason is not this
  // page's business; a plain allowlisted row is enough to hold the `shared` case.
  ['charlie-mcp', true, true, 'stdio', false],
  // Not in the allowlist: launches per session, reads `direct`.
  ['delta-mcp', true, false, 'stdio', false],
  // Blocked but NOT in the allowlist. Its state is plainly `direct`: the field is
  // forward-looking here, and the stub is not in the path at all.
  ['echo-mcp', true, false, 'stdio', true],
  // No stdio pipe to interpose on, so the question does not arise.
  ['foxtrot-mcp', false, false, 'http', false],
]

const servers = ROWS.map(([name, canStub, inAllowlist, transport, blocked]) => ({
  name,
  can_stub: canStub && transport === 'stdio',
  stub: canStub && transport === 'stdio' && inAllowlist,
  in_allowlist: inAllowlist,
  entry_poolable: false,
  pooling_blocked_by_env: blocked,
  agents: ['kirocrew'],
  transport,
  denylisted: false,
}))

const stubbed = servers.filter(s => s.stub).map(s => s.name)

/** Sharing is ON for the whole run: see the note above the shot below. */
const sharingEnabled = true

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({
  // Tall enough that the whole state matrix lands in ONE frame: the point of the
  // shot is the contrast between the blocked rows and the rows they must not
  // change, and a frame that cuts the table in half cannot show it.
  viewport: { width: 1600, height: 1460 },
  deviceScaleFactor: 2,
})
const page = await context.newPage()
logPageProblems(page)

await stubDashboardApi(page, {
  extra: (path, route) => {
    if (path === '/api/mcp-gateway/servers') {
      return json(route, { servers }), true
    }
    if (path === '/api/mcp/measure') {
      return json(route, { running: false, done: 0, measured: 0, total: 0 }), true
    }
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

/**
 * Assert on RENDERED text, not on the fixture.
 *
 * A saved PNG proves nothing on its own: a stale bundle photographs the old copy
 * while the source is correct. Reading the strings back out of the page is what
 * ties the frame to this build.
 */
const assertRendered = async label => {
  // Counted with an EXACT-text locator, not a body substring: the legend below the
  // table now opens with the same phrase, so a substring count sees the chip and
  // the legend and cannot tell which is missing.
  const chip = await page.getByText(CHIP, { exact: true }).count()
  const body = await page.locator('body').innerText()
  const reason = body.includes('Not stubbed, but your opt-in stays saved.')
  const defined = body.includes(`${CHIP} —`)
  console.log(`${label}: chips=${chip} reason=${reason} legend-defines=${defined}`)
  if (chip !== 1) throw new Error(`${label}: expected exactly 1 blocked chip, saw ${chip}`)
  if (!reason) throw new Error(`${label}: the reason line did not render -- stale bundle?`)
  if (!defined) throw new Error(`${label}: the legend does not define the term`)
}

await page.goto(`${base}/developer?tab=mcp-pool`, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2600)

/** Bring the table into frame: the rows are the subject, not the page header. */
const showTable = async () => {
  await page.getByText('alpha-env-mcp', { exact: true }).scrollIntoViewIfNeeded()
  await page.waitForTimeout(700)
}

// The one frame worth keeping. Sharing is ON because that is the only state the
// backend ever sends this field in: it computes the flag as `gw_cfg.enabled and
// <withheld env>`, so a sharing-OFF frame would photograph a wire state a real
// gateway cannot produce. The blocked row reads `direct (env)` in the muted style
// while the plain allowlisted rows read `shared` in the accent style -- text AND
// colour, since a chip that says one thing and is coloured the other reproduces
// the contradiction this fix removes.
await assertRendered('sharing-on')
await showTable()
await shot('01-state-chip-sharing-on')

// The SECOND tab renders the same chips through the same derivation, so it has to
// be able to decode them on its own. Its footer is a different catalog string, and
// the row-level note is a servers-table affordance that does not exist here -- so
// this frame is the one that proves the term is defined where it is shown, and not
// only under the other tab.
await page.getByRole('tab', { name: /sharing assessment/i }).click()
await page.waitForTimeout(1400)
{
  const body = await page.locator('body').innerText()
  const chip = await page.getByText(CHIP, { exact: true }).count()
  const defined = body.includes(`${CHIP} \u2014`)
  console.log(`assessment: chips=${chip} defines=${defined}`)
  if (chip !== 1) throw new Error(`assessment: expected exactly 1 blocked chip, saw ${chip}`)
  if (!defined) throw new Error('assessment: the term is not defined on this tab')
}
await page.getByText('alpha-env-mcp', { exact: true }).scrollIntoViewIfNeeded()
await page.waitForTimeout(700)
await shot('02-state-chip-assessment-tab')

await context.close()
await browser.close()
srv.close()
console.log('done')
