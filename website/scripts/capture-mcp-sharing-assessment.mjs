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

const rec = (strength, share, reasons, stub = true) => ({
  strength,
  // Two axes, because the bulk action reads whichever one the global sharing
  // switch makes true of a click. A fixture that sent only ``recommendShare``
  // would leave every row ineligible and photograph a disabled button.
  recommendStub: stub,
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
  // Session-bound by construction: still a disqualifier, because the consumer
  // treats an EMPTY session key as allow (mcp_cron._check_cron_job_ownership), so
  // a pooled backend skips ownership rather than merely losing a feature -- and no
  // routing-shaped hazard code would ever record it.
  ['alpha-mcp', true, 'stdio', rec('disqualified', false, [
    { code: 'session_bound_by_construction', detail: '' },
  ], false)],
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
  // Advertises logging. Pooling it costs log volume and some dropped call-scoped
  // log lines, and both are gaps in OUR broker rather than properties of the
  // server -- a proxy can emit at the finest level any tenant asked for and filter
  // down per stub. So it is reported and gates nothing.
  ['delta-mcp', true, 'stdio', rec('measured', false, [
    { code: 'preflight_passed', detail: '' },
    { code: 'degrades_when_shared', detail: 'logging_level' },
  ])],
  // Declares a rotating secret env key. No longer a disqualification: such a key
  // is never forwarded into a shared backend at all, so the pooled backend gets
  // NOBODY's secret rather than the wrong session's. A server reading its
  // credential from disk -- the documented pattern -- declares the key without
  // needing it and pools fine.
  ['echo-mcp', true, 'stdio', rec('declared', false, [
    { code: 'declares_caller_identity', detail: '' },
    { code: 'preflight_passed', detail: '' },
    { code: 'rotating_secret_env', detail: 'AWS_SESSION_TOKEN' },
  ])],
  // Refutation: the strongest tier, and now the ONLY durable disqualification.
  // Reached only by watching a shared server actually misbehave, which is also
  // what makes the gateway serve it a private backend from here on.
  ['foxtrot-mcp', true, 'stdio', rec('refuted', false, [
    { code: 'observed_hazard', detail: 'unroutable_notification' },
  ], false)],
  ['golf-mcp', false, 'stdio', rec('declared', true, [
    { code: 'declares_caller_identity', detail: '' },
    { code: 'preflight_passed', detail: '' },
  ])],
  ['hotel-mcp', false, 'stdio', rec('no_objection', false, [{ code: 'no_objection_found', detail: '' }])],
  ['india-mcp', false, 'stdio', rec('no_objection', false, [
    { code: 'no_objection_found', detail: '' },
    { code: 'no_tools_listed', detail: '' },
  ])],
  // Probe never succeeded: unknown, and deliberately NOT flagged as unsafe.
  ['juliett-mcp', false, 'stdio', rec('unknown', false, [{ code: 'not_probed', detail: '' }], false)],
  // No verdict at all, as an older gateway would answer.
  ['kilo-mcp', true, 'stdio', null],
  // Not stdio: the question does not apply rather than the answer being no.
  ['lima-mcp', false, 'http', rec('disqualified', false, [{ code: 'not_stdio', detail: '' }], false)],
  // Measured: provoked as two callers, the handshake replayed identically. That
  // rules out a caller-sensitive handshake but says nothing about state a tool
  // call would create, so it recommends the stub and NOT sharing -- which is why
  // the bulk action leaves both of these alone while sharing is on.
  ['mike-mcp', false, 'stdio', rec('measured', false, [
    { code: 'preflight_passed', detail: '' },
    { code: 'all_tools_read_only', detail: '' },
  ])],
  // Provoked, and the handshake did NOT replay. That is reported without being
  // frozen: it does not claim MEASURED (nothing was ruled out), it does not
  // claim preflight_passed, and it is never written to the verdict store -- so
  // the button keeps offering this row and the next press can clear it.
  ['november-mcp', false, 'stdio', rec('no_objection', false, [
    { code: 'no_objection_found', detail: '' },
    { code: 'handshake_not_reproducible', detail: '' },
  ])],
]

//: Names the fixture reports as blocked from pooling by their declared env.
const ENV_BLOCKED = new Set(['november-mcp'])

const servers = ROWS.map(([name, stub, transport, recommendation]) => ({
  name,
  can_stub: transport === 'stdio',
  stub: transport === 'stdio' ? stub : false,
  in_allowlist: stub,
  entry_poolable: false,
  pooling_blocked_by_env: ENV_BLOCKED.has(name),
  agents: ['kirocrew'],
  transport,
  denylisted: false,
  ...(recommendation ? { recommendation } : {}),
}))

const stubbed = servers.filter(s => s.stub).map(s => s.name)

/** Flipped between shots so the warning can be shown appearing and gone. */
let sharingEnabled = true

/**
 * What ``GET /api/mcp/measure`` answers, swapped between shots.
 *
 * The idle answer is what a page that has never run a pass sees, so the control
 * is captured in the state a first-time reader meets it in.
 */
let measureProgress = { running: false, done: 0, measured: 0, total: 0 }

/**
 * What ``POST /api/mcp/measure`` answers, which is NOT the same value.
 *
 * The button is disabled while a pass runs, so a frame that needs the press has
 * to meet an IDLE progress read and get "running" back from the start call --
 * exactly the real sequence. One variable for both makes the button unclickable.
 */
let measureStartReply = { ok: true, running: true, done: 0, measured: 0, total: 0 }

/** Flipped for the shot where nothing is left to measure. */
let everythingMeasured = false

/**
 * Flipped for the shot where a pass measured ONE of the two servers it tried.
 *
 * Without this the frame would claim one measurement beside a button still
 * offering two, which is a state the product cannot reach — a measured row gets a
 * verdict, and the button counts rows without one.
 */
let oneNowMeasured = false

const measured = rec('no_objection', false, [{ code: 'no_objection_found', detail: '' }])

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
    if (path === '/api/mcp-gateway/servers') {
      // With everything measured the control has nothing to offer, which is the
      // steady state a healthy fleet ends up in.
      const rows = everythingMeasured
        ? servers.map(s => ({ ...s, recommendation: s.recommendation ?? measured }))
          .map(s => (s.recommendation.strength === 'unknown' ? { ...s, recommendation: measured } : s))
        : oneNowMeasured
          // The row that had no verdict at all now carries one, leaving exactly
          // one row still unmeasured. `preflight_passed` is what a clean
          // measurement with nothing to object to reads as.
          ? servers.map(s => (s.name === 'kilo-mcp'
            ? { ...s, recommendation: rec('no_objection', false, [{ code: 'preflight_passed', detail: '' }]) }
            : s))
          : servers
      return json(route, { servers: rows }), true
    }
    if (path === '/api/mcp/measure') {
      const starting = route.request().method() === 'POST'
      return json(route, starting ? measureStartReply : measureProgress), true
    }
    if (path === '/api/mcp-gateway/servers/stub') {
      // The batch form. Answering it lets the bulk action reach its own result
      // line, which is the part of the flow worth photographing: a frame of the
      // button alone cannot show what it reports having done. The server decides
      // eligibility, so the reply carries what it wrote rather than what was asked.
      return json(route, {
        ok: true,
        names: ['golf-mcp', 'mike-mcp', 'november-mcp'],
        stub: true,
        stubbed: ['golf-mcp'],
        skipped: [
          { name: 'mike-mcp', reason: 'evidence_insufficient' },
          { name: 'november-mcp', reason: 'pooling_blocked_by_env' },
        ],
        applied: true,
      }), true
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

// 4. The measurement control, in the state a first-time reader meets it: the
//    button says what it does and how many rows still have no verdict.
sharingEnabled = true
await page.reload({ waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2400)
await openAssessment()
await shot('04-measure-button-labelled')

// 5. A pass in flight. Progress is a readout next to the button rather than a
//    modal, because the operator can keep reading the table while it runs. The
//    total agrees with the two unmeasured rows above: a frame whose numbers
//    contradict its own button documents a state the product cannot reach.
measureProgress = { running: true, done: 1, measured: 1, total: 2 }
await page.reload({ waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2400)
await openAssessment()
await shot('05-measure-running')

// 6. Nothing left to measure: the same button reports the state and is disabled,
//    rather than inviting a press that would spawn nothing.
measureProgress = { running: false, done: 0, total: 0 }
everythingMeasured = true
await page.reload({ waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2400)
await openAssessment()
await shot('06-measure-nothing-left')

// 7. A pass that reached NOTHING. Every server was attempted and none produced a
//    verdict, which is what a host where the probe cannot spawn does to every
//    pass. The readout says nothing at all rather than closing on the attempt
//    count: the two rows below still read "not measured" and the button still
//    offers the same two, so a closing line here could only contradict them.
//
//    The press is what makes a closing line eligible, so this frame clicks. The
//    progress read starts IDLE (the button is disabled while a pass runs) and the
//    pass is settled underneath afterwards, which is the real sequence.
measureProgress = { running: false, done: 0, measured: 0, total: 0 }
measureStartReply = { ok: true, running: true, done: 0, measured: 0, total: 2 }
everythingMeasured = false
oneNowMeasured = false
await page.reload({ waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2400)
await openAssessment()
await page.getByRole('button', { name: /measure 3 servers/i }).click()
measureProgress = { running: false, done: 2, measured: 0, total: 2 }
await page.waitForTimeout(3400)
await shot('07-measured-nothing-says-nothing')

// 8. The same pass reaching one of the two. The closing line counts what was
//    MEASURED, and the row it measured has gained a verdict, so the button below
//    now offers one rather than two — the three numbers on this frame agree,
//    which is the property the attempt count could not hold.
measureProgress = { running: false, done: 0, measured: 0, total: 0 }
measureStartReply = { ok: true, running: true, done: 0, measured: 0, total: 2 }
oneNowMeasured = false
await page.reload({ waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2400)
await openAssessment()
await page.getByRole('button', { name: /measure 3 servers/i }).click()
// The verdict lands with the settle, so flip the row before the poll sees it end.
oneNowMeasured = true
measureProgress = { running: false, done: 2, measured: 1, total: 2 }
await page.waitForTimeout(3400)
await shot('08-measured-one-of-two-attempted')

// 9. The bulk action, in the state a reader meets it: the button names what it
//     does and the line beside it says what will be measured and what will be
//     left alone. It lives on the Servers view because the assessment view states
//     it changes nothing.
measureProgress = { running: false, done: 0, measured: 0, total: 0 }
everythingMeasured = false
sharingEnabled = true
await page.reload({ waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2400)
await shot('09-stub-what-evidence-allows')

// 10. What one press reports. Sharing is ON, so eligibility takes the verdict that
//     claims caller isolation: only the DECLARED row qualifies. The two MEASURED
//     rows are left alone -- the pre-flight compared their handshakes, which says
//     nothing about state a tool call would create -- and one of them additionally
//     declares env a shared backend would withhold. A frame that stubbed those
//     would document a state the product refuses to produce.
await page.getByRole('button', { name: /evidence allows/i }).click()
await page.waitForTimeout(1800)
await shot('10-stub-result-what-it-skipped')

// 11. A pass the button itself started, in flight. Nothing is stubbed until the
//     measurement finishes, because acting on a half-measured fleet would stub
//     whatever happened to be done by then. Captured past the first progress poll
//     so the line shows the pass's real position rather than the pending count.
measureProgress = { running: true, done: 1, measured: 1, total: 2 }
await page.reload({ waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2400)
await page.getByRole('button', { name: /evidence allows/i }).click()
await page.waitForTimeout(2600)
await shot('11-stub-waiting-on-measurement')

// 12. The new tier on the evidence view. Its wording is the whole point: MEASURED
//     is what the pre-flight can support, and "no divergence" is a narrower claim
//     than safe. Scrolled to the rows that carry it, since a frame of the header
//     would prove nothing about the tier.
measureProgress = { running: false, done: 0, measured: 0, total: 0 }
await page.reload({ waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2400)
await openAssessment()
await page.getByText('november-mcp', { exact: true }).scrollIntoViewIfNeeded()
await page.waitForTimeout(600)
await shot('12-measured-tier')

await context.close()
await browser.close()
srv.close()
console.log('done')
